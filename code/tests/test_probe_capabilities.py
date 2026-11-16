"""API 部署能力探针（probe --api-capabilities）离线单测。"""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from mcagent.client import DEFAULT_BASE, ApiError, Client, Config
from mcagent.probe import main
from tests.test_d0_core import FakeResponse, FakeSession


class ApiCapabilitiesProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.status = {"ready": True, "alive": True, "busy": None, "yaw": 0.5, "pitch": -0.25}
        self.client = Mock(spec=Client)
        self.client.wait_ready.return_value = self.status
        self.client.status.return_value = self.status
        self.client.control.side_effect = ApiError("bad_args")
        self.client.look.return_value = {"yaw": self.status["yaw"], "pitch": self.status["pitch"]}
        self.client.stop.return_value = {"stopped": False, "action": None, "released": True, "wait_ms": 0}
        self.client.supports.side_effect = lambda r: True if r in ("control", "look") else None

    def _run_live(self, base: str = DEFAULT_BASE, extra_argv: list[str] | None = None):
        out = io.StringIO()
        argv = ["--api-capabilities", "--live", *(extra_argv or [])]
        with (
            patch("mcagent.probe.Config.load", return_value=Config(base, "test", "offline")),
            patch("mcagent.probe.Client", return_value=self.client),
            redirect_stdout(out),
        ):
            rc = main(argv)
        return rc, json.loads(out.getvalue())

    def test_defaults_to_dry_without_loading_config(self) -> None:
        with (
            patch("mcagent.probe.Config.load") as config,
            patch("mcagent.probe.Client") as client,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["--api-capabilities"]), 0)
            self.assertEqual(main(["--api-capabilities", "--dry"]), 0)
        config.assert_not_called()
        client.assert_not_called()

    def test_records_route_raw_stop_shape_and_recognizes_campus_official_host(self) -> None:
        self.status.update({"speed": 0, "velocity": None, "jumping": False})

        rc, report = self._run_live(base="https://apiin.nmo.net.cn/api/v1")

        self.assertEqual(rc, 0)
        self.assertEqual(report["source"], "official")
        self.assertEqual(report["status_fields"], ["speed", "velocity", "jumping"])
        self.assertEqual(report["routes"]["control"], {"ok": False, "code": "bad_args", "supported": True})
        self.assertEqual(report["routes"]["look"]["supported"], True)
        self.assertTrue(report["routes"]["stop"]["result"]["released"])
        self.client.control.assert_called_once_with()
        self.client.look.assert_called_once_with(yaw=0.5, pitch=-0.25, pos=None, entity_id=None)
        self.client.stop.assert_called_once_with()
        self.client.disconnect.assert_called_once_with()

    def test_old_deployment_with_real_client_records_unsupported_api_without_fatal(self) -> None:
        def handler(method: str, url: str, headers: dict, body):
            route = url.rsplit("/", 1)[-1]
            if route == "status":
                return FakeResponse(200, {"ok": True, "result": self.status})
            if route in ("control", "look"):
                return FakeResponse(404, {"ok": False, "error": {"code": "unknown_api"}})
            if route == "stop":
                return FakeResponse(200, {"ok": True, "result": {"stopped": False, "action": None}})
            return FakeResponse(200, {"ok": True, "result": {}})

        cfg = Config("http://127.0.0.1:8787/api/v1", "test", "offline", token_file=None)
        real_client = Client(cfg, log=lambda *_: None)
        real_client.token = "tok"
        real_client.session = FakeSession(handler)

        out = io.StringIO()
        with (
            patch("mcagent.probe.Config.load", return_value=cfg),
            patch("mcagent.probe.Client", return_value=real_client),
            redirect_stdout(out),
        ):
            rc = main(["--api-capabilities", "--live", "--local-target"])
        report = json.loads(out.getvalue())

        self.assertEqual(rc, 0)
        self.assertEqual(report["source"], "local-target")
        self.assertEqual(report["routes"]["control"], {"ok": False, "code": "unsupported_api", "supported": False})
        self.assertEqual(report["routes"]["look"], {"ok": False, "code": "unsupported_api", "supported": False})
        self.assertNotIn("released", report["routes"]["stop"]["result"])

    def test_rechecks_idle_and_does_not_stop_or_disconnect_busy_client(self) -> None:
        self.client.status.side_effect = [self.status, {**self.status, "busy": "move_to"}]

        rc, report = self._run_live()

        self.assertEqual(rc, 1)
        self.assertEqual(report["blocked"]["route"], "stop")
        self.assertEqual(report["blocked"]["busy"], "move_to")
        self.client.stop.assert_not_called()
        self.client.disconnect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
