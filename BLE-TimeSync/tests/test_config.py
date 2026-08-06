from __future__ import annotations

import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.assertEqual(config.gateways["68"], "ESP32S3-Gateway-68")
        self.assertEqual(set(config.gateways), {"68", "69", "70"})
        self.assertTrue(config.control_characteristic_uuid.endswith("9abf"))
        self.assertTrue(config.gateway_status_characteristic_uuid.endswith("9ac0"))
        self.assertEqual(config.calibration_warmup_samples, 5)
        self.assertEqual(config.calibration_samples, 25)
        self.assertEqual(config.calibration_interval_ms, 1000)
        self.assertEqual(config.scan_timeout_seconds, 30.0)
        self.assertEqual(config.notify_timeout_seconds, 3.0)
        self.assertTrue(config.write_with_response)
        self.assertEqual(config.compensation_method, "net_rtt_median")


if __name__ == "__main__":
    unittest.main()
