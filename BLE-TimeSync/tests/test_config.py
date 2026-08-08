from __future__ import annotations

import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH


class ConfigTests(unittest.TestCase):
    def test_default_config_matches_mode2_coordinator_serial_protocol(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.assertEqual(config.coordinator_name, "Mode2Coordinator")
        self.assertEqual(config.serial_port, "COM14")
        self.assertEqual(config.baud_rate, 115200)
        self.assertEqual(config.calibration_warmup_samples, 3)
        self.assertEqual(config.calibration_samples, 10)
        self.assertEqual(config.sync_interval_seconds, 10.0)


if __name__ == "__main__":
    unittest.main()
