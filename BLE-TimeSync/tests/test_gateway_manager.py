from __future__ import annotations

import logging
from types import SimpleNamespace
import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.gateway_manager import GatewayManager, GatewayRuntime


class _MemoryLog:
    def write(self, _record: object) -> None:
        pass


class GatewayManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.manager = GatewayManager(
            self.config,
            _MemoryLog(),  # type: ignore[arg-type]
            logging.getLogger("test.gateway-manager"),
        )

    def _runtime(self, device_id: str, connected: bool, synchronized: bool) -> GatewayRuntime:
        client = SimpleNamespace(is_connected=connected)
        engine = SimpleNamespace()
        return GatewayRuntime(
            device_id=device_id,
            device_name=device_id,
            client=client,  # type: ignore[arg-type]
            engine=engine,  # type: ignore[arg-type]
            synchronized=synchronized,
        )

    def test_ready_requires_ble_connection_and_initial_time_sync(self) -> None:
        self.manager.gateways = {
            "68": self._runtime("68", True, True),
            "69": self._runtime("69", True, True),
            "70": self._runtime("70", True, False),
        }

        self.assertEqual(set(self.manager.ready_gateways), {"68", "69"})
        self.assertEqual(self.manager.offline_gateway_ids, ["70"])
        self.assertFalse(self.manager.all_gateways_ready)

    def test_all_slaves_ready_only_when_68_69_70_are_ready(self) -> None:
        self.manager.gateways = {
            device_id: self._runtime(device_id, True, True)
            for device_id in ("68", "69", "70")
        }

        self.assertTrue(self.manager.all_gateways_ready)
        self.assertEqual(self.manager.offline_gateway_ids, [])

    def test_manager_has_no_capture_control_api(self) -> None:
        self.assertFalse(hasattr(self.manager, "start_all"))
        self.assertFalse(hasattr(self.manager, "stop_all"))


if __name__ == "__main__":
    unittest.main()
