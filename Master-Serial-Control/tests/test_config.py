"""Master 串口控制配置读取与约束的离线单元测试。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH


class ConfigTests(unittest.TestCase):
    def test_default_config_is_valid(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.assertEqual(config.serial_port, "AUTO")
        self.assertEqual(config.read_timeout_seconds, 0.2)
        self.assertEqual(config.write_timeout_seconds, 2.0)
        self.assertEqual(config.command_timeout_seconds, 3.0)
        self.assertEqual(config.status_timeout_seconds, 3.0)
        self.assertEqual(config.max_line_bytes, 4096)
        self.assertTrue(config.log_directory.is_absolute())

    def test_rejects_invalid_timeout(self) -> None:
        with DEFAULT_CONFIG_PATH.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        raw["command_timeout_seconds"] = 0

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaises(ValueError):
                AppConfig.load(path)


if __name__ == "__main__":
    unittest.main()
