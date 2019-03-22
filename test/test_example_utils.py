from jax import lax
import jax.numpy as np

from numpyro.examples.datasets import load_dataset, MNIST


def test_data_load():
    def mean_pixels(i, mean_pix):
        batch, _ = fetch(i, idx)
        return mean_pix + np.sum(batch) / batch.size

    init, fetch = load_dataset(MNIST, batch_size=128, split='train')
    num_batches, idx = init()
    assert lax.fori_loop(0, num_batches, mean_pixels, np.float32(0.)) / num_batches < 0.15