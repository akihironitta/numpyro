from __future__ import division

import random as pyrandom
from collections import namedtuple

import jax.lax as lax
import jax.numpy as np
import jax.random as random
from jax import grad, jit, partial, value_and_grad
from jax.flatten_util import ravel_pytree
from jax.ops import index_update
from jax.scipy import special
from jax.tree_util import register_pytree_node, tree_multimap

import numpy as onp


def dual_averaging(t0=10, kappa=0.75, gamma=0.05):
    """
    Dual Averaging is a scheme to solve convex optimization problems. It belongs
    to a class of subgradient methods which uses subgradients to update parameters
    (in primal space) of a model. Under some conditions, the averages of generated
    parameters during the scheme are guaranteed to converge to an optimal value.
    However, a counter-intuitive aspect of traditional subgradient methods is
    "new subgradients enter the model with decreasing weights" (see :math:`[1]`).
    Dual Averaging scheme solves that phenomenon by updating parameters using
    weights equally for subgradients (which lie in a dual space), hence we have
    the name "dual averaging".
    This class implements a dual averaging scheme which is adapted for Markov chain
    Monte Carlo (MCMC) algorithms. To be more precise, we will replace subgradients
    by some statistics calculated during an MCMC trajectory. In addition,
    introducing some free parameters such as ``t0`` and ``kappa`` is helpful and
    still guarantees the convergence of the scheme.

    **References**
    [1] `Primal-dual subgradient methods for convex problems`,
    Yurii Nesterov
    [2] `The No-U-turn sampler: adaptively setting path lengths in Hamiltonian Monte Carlo`,
    Matthew D. Hoffman, Andrew Gelman
    """
    def init_fn(prox_center=0.):
        x_t = 0.
        x_avg = 0.  # average of primal sequence
        g_avg = 0.  # average of dual sequence
        t = 0
        return x_t, x_avg, g_avg, t, prox_center

    def update_fn(g, state):
        x_t, x_avg, g_avg, t, prox_center = state
        t = t + 1
        # g_avg = (g_1 + ... + g_t) / t
        g_avg = (1 - 1 / (t + t0)) * g_avg + g / (t + t0)
        # According to formula (3.4) of [1], we have
        #     x_t = argmin{ g_avg . x + loc_t . |x - x0|^2 },
        # hence x_t = x0 - g_avg / (2 * loc_t),
        # where loc_t := beta_t / t, beta_t := (gamma/2) * sqrt(t).
        x_t = prox_center - (t ** 0.5) / gamma * g_avg
        # weight for the new x_t
        weight_t = t ** (-kappa)
        x_avg = (1 - weight_t) * x_avg + weight_t * x_t
        return x_t, x_avg, g_avg, t, prox_center

    return init_fn, update_fn


def welford_covariance(diagonal=True):
    """
    Implements Welford's online method for estimating (co)variance (see :math:`[1]`).
    Useful for adapting diagonal and dense mass structures for HMC.

    **References**
    [1] `The Art of Computer Programming`,
    Donald E. Knuth
    """
    def init_fn(size):
        # TODO: replace by a better pattern
        mean = np.zeros(size)
        if diagonal:
            m2 = np.zeros(size)
        else:
            m2 = np.zeros((size, size))
        n = 0
        return mean, m2, n

    def update_fn(sample, state):
        mean, m2, n = state
        n = n + 1
        delta_pre = sample - mean
        mean = mean + delta_pre / n
        delta_post = sample - mean
        if diagonal:
            m2 = m2 + delta_pre * delta_post
        else:
            m2 = m2 + np.outer(delta_post, delta_pre)
        return mean, m2, n

    def final_fn(state, regularize=False):
        mean, m2, n = state
        # TODO: when n=1, return 0; we temporarily do not check for that case
        # because lax.cond is not yet available
        cov = m2 / (n - 1)
        if regularize:
            # Regularization from Stan
            scaled_cov = (n / (n + 5)) * cov
            shrinkage = 1e-3 * (5 / (n + 5))
            if diagonal:
                cov = scaled_cov + shrinkage
            else:
                cov = scaled_cov + shrinkage * np.identity(mean.shape[0], dtype=mean.dtype)
        return cov

    return init_fn, update_fn, final_fn


def velocity_verlet(potential_fn, kinetic_fn):
    r"""
    Second order symplectic integrator that uses the velocity verlet algorithm
    for position `z` and momentum `r`.
    """
    def init_fn(z, r):
        # TODO: init using the cache of potential_energy and z_grad?
        potential_energy, z_grad = value_and_grad(potential_fn)(z)
        return z, r, potential_energy, z_grad

    def update_fn(step_size, state):
        """
        Single step velocity verlet.
        """
        z, r, _, z_grad = state
        r = tree_multimap(lambda r, z_grad: r - 0.5 * step_size * z_grad, r, z_grad)  # r(n+1/2)
        r_grad = grad(kinetic_fn)(r)
        z = tree_multimap(lambda z, r_grad: z + step_size * r_grad, z, r_grad)  # z(n+1)
        potential_energy, z_grad = value_and_grad(potential_fn)(z)
        r = tree_multimap(lambda r, z_grad: r - 0.5 * step_size * z_grad, r, z_grad)  # r(n+1)
        return z, r, potential_energy, z_grad

    return init_fn, update_fn


def find_reasonable_step_size(potential_fn, kinetic_fn, momentum_generator, position,
                              init_step_size):
    # We are going to find a step_size which make accept_prob (Metropolis correction)
    # near the target_accept_prob. If accept_prob:=exp(-delta_energy) is small,
    # then we have to decrease step_size; otherwise, increase step_size.
    target_accept_prob = np.log(0.8)

    _, vv_update = velocity_verlet(potential_fn, kinetic_fn)
    z = position
    potential_energy, z_grad = value_and_grad(potential_fn)(z)

    def _body_fn(state):
        step_size, _, direction = state
        # scale step_size: increase 2x or decrease 2x depends on direction;
        # direction=1 means keep increasing step_size, otherwise decreasing step_size.
        # Note that the direction is -1 if delta_energy is `NaN`, which may be the
        # case for a diverging trajectory (e.g. in the case of evaluating log prob
        # of a value simulated using a large step size for a constrained sample site).
        step_size = (2.0 ** direction) * step_size
        r = momentum_generator()  # generate r upon calling
        _, r_new, potential_energy_new, _ = vv_update(step_size,
                                                      (z, r, potential_energy, z_grad))
        energy_current = kinetic_fn(r) + potential_energy
        energy_new = kinetic_fn(r_new) + potential_energy_new
        delta_energy = energy_new - energy_current
        direction_new = np.where(target_accept_prob < -delta_energy, 1, -1)
        return step_size, direction, direction_new

    step_size, _, _ = lax.while_loop(lambda sdd: (sdd[1] == 0) | (sdd[1] == sdd[2]),
                                     _body_fn, (init_step_size, 0, 0))
    return step_size


adapt_window = namedtuple("adapt_window", ["start", "end"])


def build_adaptation_schedule(num_steps):
    adaptation_schedule = []
    # from Stan, for small num_steps
    if num_steps < 20:
        adaptation_schedule.append(adapt_window(0, num_steps - 1))
        return adaptation_schedule

    # We separate num_steps into windows:
    #   start_buffer + window 1 + window 2 + window 3 + ... + end_buffer
    # where the length of each window will be doubled for the next window.
    # We won't adapt mass matrix during start and end buffers; and mass
    # matrix will be updated at the end of each window. This is helpful
    # for dealing with the intense computation of sampling momentum from the
    # inverse of mass matrix.
    start_buffer_size = 75  # from Stan
    end_buffer_size = 50  # from Stan
    init_window_size = 25  # from Stan
    if (start_buffer_size + end_buffer_size + init_window_size) > num_steps:
        start_buffer_size = int(0.15 * num_steps)
        end_buffer_size = int(0.1 * num_steps)
        init_window_size = num_steps - start_buffer_size - end_buffer_size

    adaptation_schedule.append(adapt_window(start=0, end=start_buffer_size - 1))
    end_window_start = num_steps - end_buffer_size

    next_window_size = init_window_size
    next_window_start = start_buffer_size
    while next_window_start < end_window_start:
        cur_window_start, cur_window_size = next_window_start, next_window_size
        # Ensure that slow adaptation windows are monotonically increasing
        if 3 * cur_window_size <= end_window_start - cur_window_start:
            next_window_size = 2 * cur_window_size
        else:
            cur_window_size = end_window_start - cur_window_start
        next_window_start = cur_window_start + cur_window_size
        adaptation_schedule.append(adapt_window(cur_window_start, next_window_start - 1))
    adaptation_schedule.append(adapt_window(end_window_start, num_steps - 1))
    return adaptation_schedule


def warmup_adapter(num_steps, find_reasonable_step_size=None,
                   adapt_step_size=True, adapt_mass_matrix=True,
                   diag_mass=True, target_accept_prob=0.8):
    ss_init, ss_update = dual_averaging()
    mm_init, mm_update, mm_final = welford_covariance(diagonal=diag_mass)
    adaptation_schedule = np.array(build_adaptation_schedule(num_steps))
    num_windows = len(adaptation_schedule)

    def init_fn(step_size=1.0, inverse_mass_matrix=None, mass_matrix_size=None):
        if find_reasonable_step_size is not None:
            step_size = find_reasonable_step_size(step_size)
        ss_state = ss_init(np.log(10 * step_size))

        if inverse_mass_matrix is None:
            assert mass_matrix_size is not None
            if diag_mass:
                inverse_mass_matrix = np.ones(mass_matrix_size)
            else:
                inverse_mass_matrix = np.identity(mass_matrix_size)
        mm_state = mm_init(inverse_mass_matrix.shape[-1])

        window_idx = 0
        return step_size, inverse_mass_matrix, ss_state, mm_state, window_idx

    def _update_at_window_end(state):
        step_size, inverse_mass_matrix, ss_state, mm_state, window_idx = state

        if adapt_step_size:
            if find_reasonable_step_size is not None:
                step_size = find_reasonable_step_size(step_size)
            ss_state = ss_init(np.log(10 * step_size))

        if adapt_mass_matrix:
            inverse_mass_matrix = mm_final(mm_state, regularize=True)
            mm_state = mm_init(inverse_mass_matrix.shape[-1])

        return step_size, inverse_mass_matrix, ss_state, mm_state, window_idx

    def update_fn(t, accept_prob, z_flat, state):
        step_size, inverse_mass_matrix, ss_state, mm_state, window_idx = state

        # update step size state
        if adapt_step_size:
            ss_state = ss_update(target_accept_prob - accept_prob, ss_state)
            # note: at the end of warmup phase, use average of log step_size
            # TODO: should we make sure that we won't update step_size if t >= num_steps?
            log_step_size, log_step_size_avg, *_ = ss_state
            step_size = np.where(t == (num_steps - 1),
                                 np.exp(log_step_size_avg),
                                 np.exp(log_step_size))

        # update mass matrix state
        is_middle_window = (0 < window_idx) & (window_idx < (num_windows - 1))
        if adapt_mass_matrix:
            mm_state = lax.cond(is_middle_window,
                                (z_flat, mm_state), lambda args: mm_update(*args),
                                mm_state, lambda x: x)

        t_at_window_end = t == adaptation_schedule[window_idx, 1]
        window_idx = np.where(t_at_window_end, window_idx + 1, window_idx)
        state = step_size, inverse_mass_matrix, ss_state, mm_state, window_idx
        # TODO: enable lax.cond when https://github.com/google/jax/issues/514 is resolved
        # state = lax.cond(t_at_window_end & is_middle_window,
        #                  state, _update_at_window_end, state, lambda x: x)
        if t_at_window_end & is_middle_window:
            state = _update_at_window_end(state)
        return state

    return init_fn, update_fn


_TreeInfo = namedtuple("_TreeInfo", ["z_left", "r_left", "z_left_grad",
                                     "z_right", "r_right", "z_right_grad",
                                     "z_proposal", "z_proposal_pe", "z_proposal_grad",
                                     "depth", "weight", "r_sum", "turning", "diverging",
                                     "sum_accept_probs", "num_proposals"])


# let JAX recognize _TreeInfo structure
# ref: https://github.com/google/jax/issues/446
# TODO: remove this when namedtuple is supported in JAX
register_pytree_node(
    _TreeInfo,
    lambda xs: (tuple(xs), None),
    lambda _, xs: _TreeInfo(*xs)
)


@jit
def _is_turning(inverse_mass_matrix, r_left, r_right, r_sum):
    r_left, _ = ravel_pytree(r_left)
    r_right, _ = ravel_pytree(r_right)
    r_sum, _ = ravel_pytree(r_sum)

    if inverse_mass_matrix.ndim == 2:
        v_left = np.matmul(inverse_mass_matrix, r_left)
        v_right = np.matmul(inverse_mass_matrix, r_right)
    elif inverse_mass_matrix.ndim == 1:
        v_left = np.multiply(inverse_mass_matrix, r_left)
        v_right = np.multiply(inverse_mass_matrix, r_right)

    # This implements dynamic termination criterion (ref [2], section A.4.2).
    turning_at_left = np.dot(v_left, r_sum - r_left) <= 0
    turning_at_right = np.dot(v_right, r_sum - r_right) <= 0
    return turning_at_left | turning_at_right


@partial(jit, static_argnums=(3,))
def _uniform_transition_prob(current_subtree, new_subtree, rng, use_multinomial_sampling):
    # This function computes transition prob for subtrees (ref [2], section A.3.1).
    if use_multinomial_sampling:
        # e^new_weight / (e^new_weight + e^current_weight)
        transition_prob = special.expit(new_subtree.weight - current_subtree.weight)
    else:
        # For the special case that the weights of both subtrees are both 0,
        # we set transition prob to 0.5 (any is fine, because the probability
        # of picking the proposal from both subtrees is 0 at the end!)
        transition_prob = np.where(
            (current_subtree.weight > 0) | (new_subtree.weight > 0),
            new_subtree.weight / (current_subtree.weight + new_subtree.weight),
            0.5
        )
    return transition_prob


@partial(jit, static_argnums=(3,))
def _biased_transition_prob(current_tree, new_tree, rng, use_multinomial_sampling):
    # This function computes transition prob for main trees (ref [2], section A.3.2).
    if use_multinomial_sampling:
        transition_prob = np.exp(new_tree.weight - current_tree.weight)
    else:
        transition_prob = new_tree.weight / current_tree.weight
    # If new tree is turning or diverging, we won't move the proposal
    # to the new tree.
    transition_prob = np.where(new_tree.turning | new_tree.diverging,
                               0.0, np.clip(transition_prob, a_max=1.0))
    return transition_prob


@partial(jit, static_argnums=(5, 6))
def _combine_tree(current_tree, new_tree, inverse_mass_matrix, going_right, rng,
                  use_multinomial_sampling, get_transition_prob, iterative_build):
    # Now we combine the current tree and the new tree. Note that outside
    # leaves of the combined tree are determined by the direction.
    z_left, r_left, z_left_grad, z_right, r_right, r_right_grad = lax.cond(
        going_right,
        (current_tree, new_tree),
        lambda trees: (trees[0].z_left, trees[0].r_left,
                       trees[0].z_left_grad, trees[1].z_right,
                       trees[1].r_right, trees[1].z_right_grad),
        (new_tree, current_tree),
        lambda trees: (trees[0].z_left, trees[0].r_left,
                       trees[0].z_left_grad, trees[1].z_right,
                       trees[1].r_right, trees[1].z_right_grad)
    )

    transition_prob = get_transition_prob(current_tree, new_tree, rng, use_multinomial_sampling)
    transition = random.bernoulli(rng, transition_prob)
    z_proposal, z_proposal_pe, z_proposal_grad = lax.cond(
        transition,
        new_tree, lambda tree: (tree.z_proposal, tree.z_proposal_pe, tree.z_proposal_grad),
        current_tree, lambda tree: (tree.z_proposal, tree.z_proposal_pe, tree.z_proposal_grad)
    )

    tree_depth = current_tree.depth + 1

    if use_multinomial_sampling:
        tree_weight = np.logaddexp(current_tree.weight, new_tree.weight)
    else:
        tree_weight = current_tree.weight + new_tree.weight

    r_sum = tree_multimap(np.add, current_tree.r_sum, new_tree.r_sum)

    # Checks either the new tree is turning or the combined tree is turning.
    if iterative_build:
        turning = False
    else:
        turning = new_tree.turning | _is_turning(inverse_mass_matrix, r_left, r_right, r_sum)

    diverging = new_tree.diverging

    sum_accept_probs = current_tree.sum_accept_probs + new_tree.sum_accept_probs
    num_proposals = current_tree.num_proposals + new_tree.num_proposals

    return _TreeInfo(z_left, r_left, z_left_grad, z_right, r_right, r_right_grad,
                     z_proposal, z_proposal_pe, z_proposal_grad,
                     tree_depth, tree_weight, r_sum, turning, diverging,
                     sum_accept_probs, num_proposals)


@jit
def _get_leaf(tree, going_right):
    return lax.cond(going_right,
                    tree,
                    lambda tree: (tree.z_right, tree.r_right, tree.z_right_grad),
                    tree,
                    lambda tree: (tree.z_left, tree.r_left, tree.z_left_grad))


@partial(jit, static_argnums=(0, 1, 10))
def _build_basetree(vv_update, kinetic_fn, z, r, z_grad, step_size, going_right,
                    energy_current, slice_exp_term, max_sliced_energy,
                    use_multinomial_sampling):
    step_size = np.where(going_right, step_size, -step_size)
    z_new, r_new, potential_energy_new, z_new_grad = vv_update(
        step_size,
        (z, r, energy_current, z_grad)
    )

    energy_new = potential_energy_new + kinetic_fn(r_new)
    # Handles the NaN case.
    energy_new = np.where(np.isnan(energy_new), np.inf, energy_new)
    delta_energy = energy_new - energy_current
    sliced_energy = delta_energy - slice_exp_term

    if use_multinomial_sampling:
        tree_weight = -delta_energy
    else:
        tree_weight = np.where(sliced_energy <= 0, 1.0, 0.0)

    diverging = sliced_energy > max_sliced_energy
    accept_prob = np.clip(np.exp(-delta_energy), a_max=1.0)
    return _TreeInfo(z_new, r_new, z_new_grad, z_new, r_new, z_new_grad,
                     z_new, potential_energy_new, z_new_grad,
                     depth=0, weight=tree_weight, r_sum=r_new, turning=False,
                     diverging=diverging, sum_accept_probs=accept_prob, num_proposals=1)


def _build_subtree(depth, vv_update, kinetic_fn, z, r, z_grad, inverse_mass_matrix, step_size,
                   going_right, rng,  energy_current, slice_exp_term, max_sliced_energy,
                   use_multinomial_sampling):
    if depth == 0:
        return _build_basetree(vv_update, kinetic_fn, z, r, z_grad, step_size, going_right,
                               energy_current, slice_exp_term, max_sliced_energy,
                               use_multinomial_sampling)

    key, doubling_key = random.split(rng)
    # Builds the first half of tree.
    half_tree = _build_subtree(depth - 1, vv_update, kinetic_fn, z, r, z_grad,
                               inverse_mass_matrix, step_size, going_right, key,
                               energy_current, slice_exp_term, max_sliced_energy,
                               use_multinomial_sampling)

    # Checks conditions to stop doubling.
    # If we meet that condition, there is no need to build the other tree.
    if half_tree.turning | half_tree.diverging:
        return half_tree
    else:
        return _double_tree(half_tree, vv_update, kinetic_fn, _uniform_transition_prob,
                            inverse_mass_matrix, step_size, going_right, doubling_key,
                            energy_current, slice_exp_term, max_sliced_energy,
                            use_multinomial_sampling)


def _double_tree(current_tree, vv_update, kinetic_fn, get_transition_prob,
                 inverse_mass_matrix, step_size, going_right, rng,
                 energy_current, slice_exp_term, max_sliced_energy,
                 use_multinomial_sampling, max_tree_depth, iterative_build):
    key, transition_key = random.split(rng)
    # If we are going to the right, start from the right leaf of the current tree.
    z, r, z_grad = _get_leaf(current_tree, going_right)

    # Then build a new tree.
    if iterative_build:
        new_tree = _iterative_build_subtree(current_tree.depth, vv_update, kinetic_fn,
                                            z, r, z_grad, inverse_mass_matrix, step_size,
                                            going_right, key,
                                            energy_current, slice_exp_term, max_sliced_energy,
                                            use_multinomial_sampling, max_tree_depth)
    else:
        new_tree = _build_subtree(current_tree.depth, vv_update, kinetic_fn, z, r, z_grad,
                                  inverse_mass_matrix, step_size, going_right, key,
                                  energy_current, slice_exp_term, max_sliced_energy,
                                  use_multinomial_sampling)
    return _combine_tree(current_tree, new_tree, inverse_mass_matrix, going_right, transition_key,
                         use_multinomial_sampling, get_transition_prob)


@jit
def _leaf_idx_to_ckpt_idx(n):
    # computes the number of non-zero bits except the last bit
    # e.g. 6 -> 2, 7 -> 2, 13 -> 2
    _, idx_max = lax.while_loop(lambda nc: nc[0] > 0,
                                lambda nc: (nc[0] >> 1, nc[1] + (nc[0] & 1)),
                                (n >> 1, 0))
    # computes the number of last non-zero bits
    # e.g. 6 -> 0, 7 -> 3, 13 -> 1
    _, num_subtrees = lax.while_loop(lambda nc: (nc[0] & 1) != 0,
                                     lambda nc: (nc[0] >> 1, nc[1] + 1),
                                     (n, 0))
    idx_min = idx_max - num_subtrees + 1
    return idx_min, idx_max


def _is_iterative_turning(leaf_idx, inverse_mass_matrix, r, r_sum, r_ckpts, r_sum_ckpts):
    r, _ = ravel_pytree(r)
    r_sum, _ = ravel_pytree(r_sum)

    ckpt_idx_min, ckpt_idx_max = _leaf_idx_to_ckpt_idx(num_proposals)
    # we update checkpoints when leaf_idx is even
    r_ckpts, r_sum_ckpts = lax.cond(leaf_idx % 2 == 1,
                                    (r_ckpts, r_sum_ckpts),
                                    lambda x: x,
                                    (r_ckpts, r_sum_ckpts),
                                    lambda x: (index_update(x[0], ckpt_idx_max, r),
                                               index_update(x[1], ckpt_idx_max, r_sum)))

    def _body_fn(i):
        subtree_r_sum = r_sum - r_sum_ckpts[chpt_idx] + r_ckpts[chpt_idx]
        # XXX no need to unravel here
        return _is_turning(inverse_mass_matrix, r_ckpts[chpt_idx], r, subtree_r_sum)

    turning = lax.while_loop(lambda i: i <= ckpt_idx_max, _body_fn, ckpt_idx_min)
    return turning, r_ckpts, r_sum_ckpts


def _iterative_build_subtree(depth, vv_update, kinetic_fn, z, r, z_grad,
                             inverse_mass_matrix, step_size, going_right, rng,
                             energy_current, slice_exp_term, max_sliced_energy,
                             use_multinomial_sampling, max_tree_depth):
    max_num_proposals = 2 ** depth

    def _cond_fn(state):
        tree, turning, _, _, _ = state
        return (tree.num_proposals < max_num_proposals) & ~turning & ~tree.diverging

    def _body_fn(state):
        current_tree, r_checkpoints, r_sum_checkpoints, rng = state
        rng, transition_rng = random.split(rng)
        z, r, z_grad = _get_leaf(current_tree, going_right)
        new_leaf = _build_basetree(vv_update, kinetic_fn, z, r, z_grad, step_size, going_right,
                                   energy_current, slice_exp_term, max_sliced_energy,
                                   use_multinomial_sampling)
        new_tree = _combine_tree(current_tree, new_leaf, inverse_mass_matrix, going_right,
                                 transition_rng, use_multinomial_sampling,
                                 _uniform_transition_prob, iterative_build=True)
        turning, r_checkpoints, r_sum_checkpoints = _is_iterative_turning(
            current_tree.num_proposals - 1,
            inverse_mass_matrix,
            new_leaf.r_right,
            new_tree.r_sum,
            r_checkpoints,
            r_sum_checkpoints
        )
        return new_tree, turning, r_checkpoints, r_sum_checkpoints, rng

    basetree = _build_basetree(vv_update, kinetic_fn, z, r, z_grad, step_size, going_right,
                               energy_current, slice_exp_term, max_sliced_energy,
                               use_multinomial_sampling)
    # TODO: we can create these checkpoints 1 time at build_tree method
    # and reuse it; but let's do this optimization later
    r_checkpoints = np.zeros((max_tree_depth, inverse_mass_matrix.shape[-1]),
                             dtype=inverse_mass_matrix.dtype)
    r_sum_checkpoints = np.zeros((max_tree_depth, inverse_mass_matrix.shape[-1]),
                                 dtype=inverse_mass_matrix.dtype)
    tree, turning, _, _, _ = lax.while_loop(
        _cond_fn,
        _body_fn,
        (basetree, False, r_checkpoints, r_sum_checkpoints, rng)
    )
    # update depth and turning condition
    return _TreeInfo(tree.z_left, tree.r_left, tree.z_left_grad,
                     tree.z_right, tree.r_right, tree.z_right_grad,
                     tree.z_proposal, tree.z_proposal_pe, tree.z_proposal_grad,
                     depth, tree.weight, tree.r_sum, turning, tree.diverging,
                     tree.sum_accept_probs, tree.num_proposals)


def build_tree(verlet_update, kinetic_fn, verlet_state, inverse_mass_matrix, step_size, rng,
               max_sliced_energy=1000., use_multinomial_sampling=True, max_tree_depth=10,
               iterative_build=True):
    """
    **References:**
    [1] `The No-U-Turn Sampler: Adaptively Setting Path Lengths in Hamiltonian Monte Carlo`,
    Matthew D. Hoffman, Andrew Gelman
    [2] `A Conceptual Introduction to Hamiltonian Monte Carlo`,
    Michael Betancourt
    """
    # TODO(fehiepsi): iterative_build flag will be depricated when
    # performance/memory usage is profiled.

    z, r, potential_energy, z_grad = verlet_state
    energy_current = potential_energy + kinetic_fn(r)
    key, subkey = random.split(rng)

    if use_multinomial_sampling:
        tree_weight = 0.
        slice_exp_term = 0.
    else:
        tree_weight = 1.
        slice_exp_term = -np.log(random.uniform(subkey, shape=()))

    tree = _TreeInfo(z, r, z_grad, z, r, z_grad, z, potential_energy, z_grad,
                     depth=0, weight=tree_weight, r_sum=r, turning=False, diverging=False,
                     sum_accept_probs=0., num_proposals=0)

    while (tree.depth < max_tree_depth) & ~tree.turning & ~tree.diverging:
        key, direction_key, doubling_key = random.split(key, 3)
        going_right = random.bernoulli(direction_key)
        tree = _double_tree(tree, verlet_update, kinetic_fn, _biased_transition_prob,
                            inverse_mass_matrix, step_size, going_right, doubling_key,
                            energy_current, slice_exp_term, max_sliced_energy,
                            use_multinomial_sampling, max_tree_depth, iterative_build)
    return tree


def set_rng_seed(rng_seed):
    pyrandom.seed(rng_seed)
    onp.random.seed(rng_seed)
