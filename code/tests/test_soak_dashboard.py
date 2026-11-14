"""双工位挂机本地控制台（tools/soak-autofix/dashboard.py）离线单测（verified: offline）。

覆盖：台账静态解析、当前轮实时摘要、/proc 守护进程识别、实时参数写入与越界拒绝、
启动参数校验与命令拼装、重复启动拦截、HTTP 层 Host / Origin / Content-Type 防护。
不启动真实守护、不连游戏服。
"""
from __future__ import annotations

import http.client
import importlib.util
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_dashboard():
    mod_path = REPO_ROOT / "tools" / "soak-autofix" / "dashboard.py"
    spec = importlib.util.spec_from_file_location("soak_dashboard_under_test", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


dash = _load_dashboard()


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.console = dash.Console(self.root, self.session)

    def tearDown(self):
        self.tmp.cleanup()

    def _init_state(self, **extra):
        st = {"target_branch": "feat/soak-autofix", "base_commit": "a" * 40, "current_tip": "b" * 40,
              "episodes": [], "incidents": {}, "commits": [], "player_model": "DeepSeek-V4.1-Flash"}
        st.update(extra)
        (self.session / "state.json").write_text(json.dumps(st), encoding="utf-8")

    def test_parse_ledger_reads_real_goals_file(self):
        text = (REPO_ROOT / "code" / "mcagent" / "loop" / "goals.py").read_text(encoding="utf-8")
        goals = {g["id"]: g for g in dash.parse_ledger(text)}
        self.assertEqual(goals["prep_wood"]["stage"], "graduated")
        self.assertEqual(goals["smelt_iron"]["stage"], "draft")
        self.assertEqual(goals["shiny_gear"]["score"], 60)
        self.assertEqual(goals["smelt_iron"]["skill"], "smelt_iron")
        self.assertIn("prep_diamond_pickaxe", goals)

    def test_live_episode_summary_running_and_finished(self):
        ep = self.session / "episodes" / "ep-0001"
        rows = [
            {"event": "run_start", "t": 1000.0, "git": {"commit": "abcdef1234"},
             "meta": {"use_llm": True, "llm_model": "DeepSeek-V4.1-Flash", "max_steps": 45,
                      "max_seconds": 240.0, "bot_user": "agy_soak_r001", "target_goal": "smelt_iron"}},
            {"event": "llm_call", "elapsed_s": 2.0, "ms": 1500, "ok": True, "skill": "gather_wood",
             "cumulative_total_tokens": 600},
            {"event": "skill_start", "elapsed_s": 2.1, "skill": "gather_wood", "args": {"count": 3}},
            {"event": "step", "elapsed_s": 5.0, "op": "dig", "hp": 20.0, "food": 18, "record": {"op": "dig"}},
            {"event": "skill_end", "elapsed_s": 9.0, "skill": "gather_wood", "outcome": "completed"},
            {"event": "llm_call", "elapsed_s": 10.0, "ms": 3500, "ok": False, "skill": None,
             "cumulative_total_tokens": 900},
            {"event": "skill_start", "elapsed_s": 10.5, "skill": "craft_gear", "args": {}},
            {"event": "step", "elapsed_s": 12.0, "op": "craft", "hp": 19.0, "food": 18,
             "record": {"op": "craft", "error_code": "need_crafting_table"}},
        ]
        _write_jsonl(ep / "timeline.jsonl", rows)
        _write_jsonl(ep / "progress_curve.jsonl", [{"deaths": 0, "goals_reached": ["prep_wood"],
                                                    "server_score_v2": 0, "inferred_score_v2": 0}])
        live = dash.summarize_live_episode(ep, now=1030.0)
        self.assertFalse(live["finished"])
        self.assertEqual(live["elapsed_s"], 30.0)
        self.assertEqual((live["steps"], live["max_steps"], live["max_seconds"]), (2, 45, 240.0))
        self.assertEqual(live["current_skill"], "craft_gear")
        self.assertEqual((live["skills_completed"], live["llm_calls"], live["llm_fail"]), (1, 2, 1))
        self.assertEqual(live["tokens"], 900)
        self.assertEqual((live["hp"], live["commit"], live["target_goal"]), (19.0, "abcdef1", "smelt_iron"))
        self.assertEqual(live["goals_reached"], ["prep_wood"])
        self.assertIn("need_crafting_table", live["recent"][0]["text"])

        rows.append({"event": "run_end", "elapsed_s": 240.3, "stop_reason": "max_seconds_reached",
                     "total_tokens": 1200, "goals_reached": {"prep_wood": 9.0}})
        _write_jsonl(ep / "timeline.jsonl", rows)
        done = dash.summarize_live_episode(ep, now=99999.0)
        self.assertTrue(done["finished"])
        self.assertEqual((done["elapsed_s"], done["tokens"]), (240.3, 1200))
        self.assertIsNone(done["current_skill"])
        self.assertIsNone(dash.summarize_live_episode(self.session / "episodes" / "ep-0009"))

        # 中断时连写两条 run_end 取第一条；重启复用轮次号后只看最后一次 run_start 之后
        rows.append({"event": "run_end", "elapsed_s": 3.8, "stop_reason": "stopped"})
        _write_jsonl(ep / "timeline.jsonl", rows)
        self.assertEqual(dash.summarize_live_episode(ep)["stop_reason"], "max_seconds_reached")
        rows.extend([
            {"event": "run_start", "t": 5000.0, "meta": {"max_steps": 80, "max_seconds": 420.0}},
            {"event": "step", "elapsed_s": 1.0, "op": "dig", "record": {}},
        ])
        _write_jsonl(ep / "timeline.jsonl", rows)
        again = dash.summarize_live_episode(ep, now=5010.0)
        self.assertEqual((again["finished"], again["steps"], again["max_steps"], again["elapsed_s"]),
                         (False, 1, 80, 10.0))

    def test_find_soak_processes_matches_session_by_resolved_path(self):
        proc = self.root / "proc"
        cwd_dir = self.root
        def mk(pid: str, argv: list[str]):
            d = proc / pid
            d.mkdir(parents=True)
            (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
            os.symlink(cwd_dir, d / "cwd")
        mk("101", ["python3", "tools/soak-autofix/soak_ctl.py", "run", "--session-dir", "session"])
        mk("102", ["python3", "/x/soak_ctl.py", "runner-loop", "--session-dir", str(self.session)])
        mk("103", ["python3", "/x/soak_ctl.py", "set-goal", "--session-dir", str(self.session)])
        mk("104", ["python3", "/x/soak_ctl.py", "run", "--session-dir", str(self.root / "other")])
        mk("105", ["python3", "-m", "mcagent.loop", "--run-dir", str(self.session)])
        (proc / "self").mkdir()
        found = dash.find_soak_processes(self.session, proc_root=proc)
        self.assertEqual([(p["pid"], p["role"]) for p in found], [(101, "run"), (102, "runner-loop")])

    def test_apply_settings_bounds_conflict_and_write(self):
        with self.assertRaises(ValueError):
            self.console.apply_settings({"episode_seconds": 300})  # 会话未初始化
        self._init_state()
        for bad in ({"episode_seconds": 5}, {"episode_steps": 100000},
                    {"clear_budget": True, "episode_steps": 50}, {"max_goal_attempts": 0}):
            with self.assertRaises(ValueError, msg=bad):
                self.console.apply_settings(bad)
        res = self.console.apply_settings({"episode_seconds": "420", "episode_steps": "80",
                                           "target_goal": "smelt_iron", "player_model": " Qwen3.6-Flash "})
        self.assertEqual((res["episode_seconds"], res["episode_steps"]), (420.0, 80))
        self.assertEqual((res["target_goal"], res["player_model"]), ("smelt_iron", "Qwen3.6-Flash"))
        st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
        self.assertEqual((st["episode_seconds"], st["episode_steps"]), (420.0, 80))
        # 空字符串表示不改该项；清除覆盖后回到启动参数
        res2 = self.console.apply_settings({"episode_seconds": "", "episode_steps": None, "clear_budget": True})
        self.assertIsNone(res2["episode_seconds"])
        self.assertEqual(res2["target_goal"], "smelt_iron")

    def test_apply_settings_hot_switches_fixer_model_and_snapshot_lists_catalog(self):
        self._init_state(fixer_preset="codex")
        with self.assertRaises(ValueError):
            self.console.apply_settings({"fixer_preset": "codex:gpt-9:max"})
        res = self.console.apply_settings({"fixer_preset": "agy:gemini-4-hydrogen-xhigh"})
        self.assertEqual(res["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        st = json.loads((self.session / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(st["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        # 空串表示不改
        self.assertEqual(self.console.apply_settings({"fixer_preset": ""})["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")

        snap = self.console.snapshot()
        self.assertEqual(snap["state"]["fixer_preset"], "agy:gemini-4-hydrogen-xhigh")
        groups = {c["group"] for c in snap["fixer_catalog"]}
        self.assertTrue({"Codex · GPT-6.1-Sol", "Codex · GPT-6-Astra", "agy · Gemini 4 Hydrogen",
                         "agy · Gemini 4 Carbon"} <= groups)
        carbon = [c["key"] for c in snap["fixer_catalog"] if c["group"] == "agy · Gemini 4 Carbon"]
        self.assertEqual(carbon, ["agy:gemini-4-carbon-low", "agy:gemini-4-carbon-medium", "agy:gemini-4-carbon-high"])
        self.assertIn("DeepSeek-V4.1-Flash", [m["name"] for m in snap["player_models"]])
        p = self.console._validate_start({"fixer_preset": "codex:gpt-6-astra:max"})
        cmd = self.console.build_run_cmd(p)
        self.assertEqual(cmd[cmd.index("--fixer-preset") + 1], "codex:gpt-6-astra:max")

    def test_apply_settings_rejects_unknown_goal_when_ledger_known(self):
        self._init_state()
        with patch.object(self.console, "ledger", return_value=[{"id": "smelt_iron", "stage": "draft"}]):
            with self.assertRaises(ValueError):
                self.console.apply_settings({"target_goal": "no_such_goal"})
            self.assertEqual(self.console.apply_settings({"target_goal": "smelt_iron"})["target_goal"], "smelt_iron")

    def test_start_validation_command_and_duplicate_guard(self):
        with self.assertRaises(ValueError):
            self.console.start({"branch": "main"})
        with self.assertRaises(ValueError):
            self.console.start({"fixer_preset": "nope"})
        with self.assertRaises(ValueError):
            self.console.start({"episode_seconds": 10})

        p = self.console._validate_start({"episodes": 0, "episode_seconds": 600, "episode_steps": 110,
                                          "goal": "smelt_iron", "fixer_preset": "agy", "llm": False,
                                          "skip_live_canary": True})
        cmd = self.console.build_run_cmd(p)
        self.assertEqual(cmd[2], "run")
        for flag, val in (("--session-dir", str(self.session)), ("--episodes", "0"),
                          ("--episode-seconds", "600.0"), ("--episode-steps", "110"),
                          ("--goal", "smelt_iron"), ("--fixer-preset", "agy"), ("--branch", "feat/soak-autofix")):
            self.assertEqual(cmd[cmd.index(flag) + 1], val)
        self.assertIn("--no-llm", cmd)
        self.assertIn("--skip-live-canary", cmd)
        self.assertEqual(cmd[cmd.index("--bot-user") + 1], dash.soak_ctl.DEFAULT_BOT_USER)
        self.assertNotIn("--rotate-user", cmd)
        cmd_rot = self.console.build_run_cmd(self.console._validate_start({"bot_user": "team_bot", "rotate_user": True}))
        self.assertEqual(cmd_rot[cmd_rot.index("--bot-user") + 1], "team_bot")
        self.assertIn("--rotate-user", cmd_rot)
        with self.assertRaises(ValueError):
            self.console._validate_start({"bot_user": "bad name"})

        with patch.object(dash, "find_soak_processes", return_value=[{"pid": 1, "role": "run"}]):
            with self.assertRaises(ValueError):
                self.console.start({})

        class FakePopen:
            def __init__(self, cmd, **kw):
                self.pid, self.cmd, self.kw = 4242, cmd, kw
            def poll(self):
                return None
        with patch.object(dash, "find_soak_processes", return_value=[]), \
             patch.object(self.console, "ledger", return_value=[]), \
             patch.object(dash.subprocess, "Popen", FakePopen):
            res = self.console.start({"episode_seconds": 300})
        self.assertEqual(res["pid"], 4242)
        self.assertTrue(self.console._child.kw["start_new_session"])
        self.assertIn("启动", (self.session / dash.RUN_LOG_NAME).read_text(encoding="utf-8"))
        saved = json.loads((self.session / dash.LAST_START_NAME).read_text(encoding="utf-8"))
        self.assertEqual(saved["episode_seconds"], 300.0)
        self.assertEqual(self.console.last_start_params()["episode_seconds"], 300.0)

    def test_stop_and_episode_detail_guard(self):
        self.console.request_stop()
        self.assertTrue((self.session / "STOP").exists())
        for bad in ("../state", "ep-1", ""):
            with self.assertRaises(ValueError):
                self.console.episode_detail(bad)

    def test_http_host_origin_and_content_type_guards(self):
        self._init_state()
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        httpd = dash.ThreadingHTTPServer(("127.0.0.1", port), dash.make_handler(self.console, port))
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            def req(method, path, body=None, headers=None):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                conn.request(method, path, body=body, headers=headers or {})
                r = conn.getresponse()
                data = r.read()
                conn.close()
                return r.status, data

            code, data = req("GET", "/api/snapshot")
            self.assertEqual(code, 200)
            snap = json.loads(data)
            self.assertEqual(snap["daemon"], "stopped")
            self.assertEqual(snap["state"]["player_model"], "DeepSeek-V4.1-Flash")

            code, _ = req("GET", "/", headers={"Host": "evil.example:80"})
            self.assertEqual(code, 403)
            code, _ = req("POST", "/api/stop", body="{}",
                          headers={"Content-Type": "application/json", "Origin": "http://evil.example"})
            self.assertEqual(code, 403)
            code, _ = req("POST", "/api/stop", body="x=1",
                          headers={"Content-Type": "application/x-www-form-urlencoded"})
            self.assertEqual(code, 415)
            self.assertFalse((self.session / "STOP").exists())
            code, data = req("POST", "/api/settings", body=json.dumps({"episode_steps": 9999}),
                             headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}"})
            self.assertEqual(code, 400)
            self.assertFalse(json.loads(data)["ok"])
            code, _ = req("POST", "/api/stop", body="{}", headers={"Content-Type": "application/json"})
            self.assertEqual(code, 200)
            self.assertTrue((self.session / "STOP").exists())
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
