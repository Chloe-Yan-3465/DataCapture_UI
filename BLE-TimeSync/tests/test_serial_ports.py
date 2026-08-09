from __future__ import annotations

from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.serial_client import CoordinatorSerialClient


class SerialPortTests(unittest.TestCase):
    def test_marks_configured_coordinator_port_case_insensitively(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)

        class FakeListPorts:
            @staticmethod
            def comports() -> list[object]:
                return [
                    SimpleNamespace(device="com14", description="ESP32-S3", hwid="USB VID:PID")
                ]

        with patch("app.serial_client._serial_modules", return_value=(object, FakeListPorts)):
            ports = CoordinatorSerialClient.list_ports(config)
        self.assertEqual(len(ports), 1)
        self.assertTrue(ports[0].is_configured)


if __name__ == "__main__":
    unittest.main()
