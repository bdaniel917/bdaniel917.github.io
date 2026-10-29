import unittest

import numpy as np

from simulate import electrode_input_filter, fixed_point_fir, lowpass, simulate, source_signal


class RecordingChainTests(unittest.TestCase):
    def test_source_is_deterministic_and_has_expected_length(self):
        t1, x1 = source_signal(fs=1000, duration=0.5)
        t2, x2 = source_signal(fs=1000, duration=0.5)
        self.assertEqual(len(t1), 500)
        np.testing.assert_array_equal(t1, t2)
        np.testing.assert_array_equal(x1, x2)

    def test_lowpass_rejects_invalid_cutoff(self):
        with self.assertRaises(ValueError):
            lowpass(np.ones(10), fs=100, cutoff_hz=50)

    def test_lowpass_suppresses_high_frequency_more_than_low_frequency(self):
        fs = 1000
        t = np.arange(fs) / fs
        low = np.sin(2 * np.pi * 8 * t)
        high = np.sin(2 * np.pi * 180 * t)
        y_low = lowpass(low, fs, 30)
        y_high = lowpass(high, fs, 30)
        self.assertGreater(np.std(y_low[100:]), np.std(y_high[100:]))

    def test_electrode_rc_response_attenuates_high_frequency(self):
        fs = 4000
        t = np.arange(fs) / fs
        low = np.sin(2 * np.pi * 10 * t)
        high = np.sin(2 * np.pi * 900 * t)
        low_out = electrode_input_filter(low, fs, 1e6, 100)
        high_out = electrode_input_filter(high, fs, 1e6, 100)
        self.assertGreater(np.std(low_out[100:]), np.std(high_out[100:]))

    def test_fixed_point_fir_preserves_constant_level(self):
        y = fixed_point_fir(np.full(100, 12345, dtype=np.int64))
        np.testing.assert_allclose(y[5:-5], 12345, atol=1)

    def test_monte_carlo_is_reproducible(self):
        first, rows1 = simulate(trials=8, seed=19)
        second, rows2 = simulate(trials=8, seed=19)
        self.assertEqual(first, second)
        self.assertEqual(rows1, rows2)
        self.assertEqual(first["trials"], 8)


if __name__ == "__main__":
    unittest.main()
