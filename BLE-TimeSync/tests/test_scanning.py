from __future__ import annotations

import logging
from types import SimpleNamespace
import unittest
from unittest.mock import patch

raise unittest.SkipTest("legacy BLE scanner is not used by Mode2 coordinator")

from app.ble_client import BleTimeClient
from app.config import AppConfig, DEFAULT_CONFIG_PATH


class ScanMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.device = SimpleNamespace(address="10:20:BA:41:B8:41", name=None)

    def test_merges_name_and_services_across_advertisement_updates(self) -> None:
        first_adv = SimpleNamespace(
            local_name=None,
            service_uuids=[self.config.service_uuid.upper()],
            rssi=-40,
        )
        first = BleTimeClient._merge_scan_result(None, self.device, first_adv, self.config)
        self.assertEqual(first.name, "(unnamed)")
        self.assertTrue(first.is_target)

        scan_response = SimpleNamespace(
            local_name="node-ESP32S3-TIMESYNC-lab",
            service_uuids=[],
            rssi=-30,
        )
        merged = BleTimeClient._merge_scan_result(
            first, self.device, scan_response, self.config
        )
        self.assertEqual(merged.name, "node-ESP32S3-TIMESYNC-lab")
        self.assertIn(self.config.service_uuid, merged.service_uuids)
        self.assertEqual(merged.rssi, -30)
        self.assertTrue(merged.is_target)

    def test_does_not_accept_arbitrary_unnamed_device(self) -> None:
        advertisement = SimpleNamespace(local_name=None, service_uuids=[], rssi=-25)
        result = BleTimeClient._merge_scan_result(
            None, self.device, advertisement, self.config
        )
        self.assertFalse(result.is_target)


class ScanFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_unfiltered_active_discover_then_matches(self) -> None:
        config = AppConfig.load(DEFAULT_CONFIG_PATH)
        device = SimpleNamespace(
            address="10:20:BA:41:B8:41", name="ESP32S3-TimeSync"
        )
        advertisement = SimpleNamespace(
            local_name="ESP32S3-TimeSync",
            service_uuids=[config.service_uuid],
            rssi=-48,
        )

        class FakeScanner:
            arguments: dict[str, object] = {}

            @classmethod
            async def discover(cls, **kwargs: object) -> dict[str, tuple[object, object]]:
                cls.arguments = kwargs
                return {device.address: (device, advertisement)}

        logger = logging.getLogger("test.scan")
        with patch("app.ble_client._bleak_classes", return_value=(object, FakeScanner)):
            results = await BleTimeClient.scan(config, logger)

        self.assertEqual(
            FakeScanner.arguments,
            {
                "timeout": 30.0,
                "return_adv": True,
                "scanning_mode": "active",
            },
        )
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0].is_target)
        self.assertEqual(results[0].address, "10:20:BA:41:B8:41")


if __name__ == "__main__":
    unittest.main()
