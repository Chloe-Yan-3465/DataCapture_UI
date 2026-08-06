from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
import unittest

from app.config import AppConfig, DEFAULT_CONFIG_PATH
from app.gateway_manager import GatewayManager, GatewayRuntime
from app.gateway_protocol import GatewayMessage, parse_gateway_message


class _MemoryLog:
    def write(self, _record: object) -> None:
        pass


class _FakeControlClient:
    def __init__(self, device_id: str) -> None:
        self.device_id = device_id
        self.is_connected = True
        self.gateway_messages: asyncio.Queue[GatewayMessage] = asyncio.Queue()
        self.payloads: list[bytes] = []

    def drain_gateway_messages(self) -> None:
        while not self.gateway_messages.empty():
            self.gateway_messages.get_nowait()

    async def send_control(self, payload: bytes) -> None:
        self.payloads.append(payload)
        command, session_text = payload.decode("ascii").split("+", 1)
        session_id = int(session_text)
        self.gateway_messages.put_nowait(
            parse_gateway_message(
                f"GW+{command}+SESSION={session_id}+FORWARDED+END"
            )
        )
        frames = "+FRAMES=123" if command == "STOP" else ""
        self.gateway_messages.put_nowait(
            parse_gateway_message(
                f"ACK+{command}+SESSION={session_id}+OK{frames}+END"
            )
        )


class GatewayManagerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.config = AppConfig.load(DEFAULT_CONFIG_PATH)
        self.manager = GatewayManager(
            self.config,
            _MemoryLog(),  # type: ignore[arg-type]
            logging.getLogger("test.gateway-manager"),
        )
        for device_id, device_name in self.config.gateways.items():
            client = _FakeControlClient(device_id)
            self.manager.gateways[device_id] = GatewayRuntime(
                device_id=device_id,
                device_name=device_name,
                client=client,  # type: ignore[arg-type]
                engine=SimpleNamespace(),  # type: ignore[arg-type]
                identity=GatewayMessage(
                    kind="gateway_state",
                    raw="",
                    device_id=device_id,
                    state="IDLE",
                    sync=True,
                ),
            )

    async def test_start_and_stop_use_one_session_for_all_gateways(self) -> None:
        start_results = await self.manager.start_all()
        self.assertEqual(self.manager.control_state, "RUNNING")
        self.assertEqual(len(start_results), 3)
        self.assertTrue(all(item.success and item.forwarded for item in start_results))
        self.assertEqual(len({item.session_id for item in start_results}), 1)

        stop_results = await self.manager.stop_all()
        self.assertEqual(self.manager.control_state, "IDLE")
        self.assertTrue(all(item.success for item in stop_results))
        self.assertTrue(all(item.frames == 123 for item in stop_results))
        for runtime in self.manager.gateways.values():
            client = runtime.client
            self.assertEqual(client.payloads[0].split(b"+")[0], b"START")  # type: ignore[attr-defined]
            self.assertEqual(client.payloads[1].split(b"+")[0], b"STOP")  # type: ignore[attr-defined]

    async def test_start_targets_only_online_synchronized_gateways(self) -> None:
        self.manager.gateways["70"].identity = GatewayMessage(
            kind="gateway_state",
            raw="",
            device_id="70",
            state="IDLE",
            sync=False,
        )
        results = await self.manager.start_all()
        self.assertEqual({item.device_id for item in results}, {"68", "69"})
        self.assertTrue(all(item.success for item in results))
        self.assertEqual(self.manager.control_state, "RUNNING_PARTIAL")
        client70 = self.manager.gateways["70"].client
        self.assertEqual(client70.payloads, [])  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
