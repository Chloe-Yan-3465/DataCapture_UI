from __future__ import annotations

import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.assertEqual(config.gateways, {"68": "68", "69": "69", "70": "70"})
        self.assertTrue(config.write_characteristic_uuid.endswith("9abd"))
        self.assertTrue(config.status_characteristic_uuid.endswith("9abe"))
        self.assertEqual(config.calibration_warmup_samples, 0)
        self.assertEqual(config.calibration_samples, 1)
        self.assertEqual(config.scan_timeout_seconds, 30.0)
        self.assertEqual(config.notify_timeout_seconds, 3.0)
        self.assertEqual(config.sync_interval_seconds, 30.0)
        self.assertEqual(config.sync_stagger_ms, 300)
        self.assertTrue(config.write_with_response)
        self.assertEqual(config.compensation_method, "net_rtt_median")


if __name__ == "__main__":
    unittest.main()
