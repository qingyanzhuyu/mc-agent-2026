"""四层基础设施：并发缓存、急停锁存、速率上限与非阻塞模型故障注入。"""
import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from mcagent.client import ApiError, Client, Config, RequestLimiter
from mcagent.runtime import Executor, RuntimeConfig, SafetyMonitor, State
from mcagent.planner.scheduler import DecisionScheduler
from mcagent.probe import path_template, pathfinding_main, validate_fixture


class FakeClient:
    def __init__(self):
        self.st = {"ready": True, "alive": True, "health": 20, "pos": [0, 64, 0], "on_ground": True}
        self.status_calls = self.inventory_calls = self.stops = 0
        self.block = {"name": "air"}
        self.evs = []
        self.block_calls = []

    def status(self):
        self.status_calls += 1
        return dict(self.st)

    def inventory(self):
        self.inventory_calls += 1
        return {"items": []}

    def events(self, **kwargs):
        evs, self.evs = self.evs, []
        return {"events": evs, "last_seq": kwargs["since"] + len(evs)}

    def stop(self):
        self.stops += 1
        return {}

    def get_block(self, pos):
        self.block_calls.append(pos)
        if isinstance(self.block, Exception):
            raise self.block
        return self.block


class CacheTests(unittest.TestCase):
    def test_forced_status_refresh_with_unchanged_clock(self):
        c, s = FakeClient(), State()
        with patch("mcagent.runtime.time.monotonic", return_value=100.0):
            s.refresh_status(c)
            c.st["pos"] = [2, 64, 0]
            for ttl in (0, -1):
                with self.subTest(max_age_s=ttl):
                    before = c.status_calls
                    self.assertEqual(s.refresh_status(c, max_age_s=ttl)["pos"], c.st["pos"])
                    self.assertEqual(c.status_calls, before + 1)
            s.refresh_status(c)
            s.refresh_status(c, max_age_s=1)
            self.assertEqual(c.status_calls, 3)

    def test_place_keeps_player_pos_but_updates_other_state_fields(self):
        c, s = FakeClient(), State()
        s.set_status(c.st)
        before = s.status_at
        ex = Executor(c, s, log=lambda *_: None)
        result = {"placed": "cobblestone", "pos": [1, 63, 0], "health": 18, "food": 19}
        c.place = Mock(return_value=result)
        self.assertEqual(ex.place("cobblestone", pos=[1, 63, 0]), result)
        self.assertEqual(s.pos, c.st["pos"])
        self.assertEqual(s.st["health"], 18)
        self.assertEqual(s.st["food"], 19)
        self.assertEqual(s.status_at, before)
        self.assertEqual(result["pos"], [1, 63, 0])

    def test_dismount_still_updates_player_pos(self):
        c, s = FakeClient(), State()
        s.set_status(c.st)
        ex = Executor(c, s, log=lambda *_: None)
        result = {"dismounted": True, "pos": [12.5, 64, 10.5]}
        c.dismount = Mock(return_value=result)
        self.assertEqual(ex.dismount(), result)
        self.assertEqual(s.pos, result["pos"])
        result["pos"][0] = 99
        self.assertEqual(s.pos[0], 12.5)

    def test_simultaneous_status_reads_coalesce_and_expire(self):
        c, s = FakeClient(), State()
        entered, release = threading.Event(), threading.Event()
        original = c.status
        def slow():
            entered.set()
            release.wait(1)
            return original()
        c.status = slow
        results = []
        workers = [threading.Thread(target=lambda: results.append(s.refresh_status(c))) for _ in range(6)]
        for t in workers:
            t.start()
        self.assertTrue(entered.wait(1))
        release.set()
        for t in workers:
            t.join(1)
        self.assertEqual(len(results), 6)
        self.assertEqual(c.status_calls, 1)
        results[0]["pos"][0] = 99
        self.assertEqual(s.pos[0], 0)
        s.status_at -= 2
        s.refresh_status(c)
        self.assertEqual(c.status_calls, 2)

    def test_inventory_dirty_action_and_pickup_only(self):
        c, s = FakeClient(), State()
        ex = Executor(c, s, log=lambda *_: None)
        s.refresh_inventory(c)
        s.refresh_inventory(c)
        self.assertEqual(c.inventory_calls, 1)
        ex.act("dig", lambda: {"dug": "stone"})
        self.assertTrue(s.inventory_dirty)
        s.refresh_inventory(c)
        self.assertEqual(c.inventory_calls, 2)
        c.evs = [{"seq": 1, "type": "pickup", "item": "stone", "count": 1}]
        s.drain(c)
        self.assertTrue(s.inventory_dirty)
        s.refresh_inventory(c)
        self.assertEqual(c.inventory_calls, 3)

    def test_action_pos_does_not_make_old_health_fresh(self):
        s = State()
        s.set_status({"health": 20, "pos": [0, 64, 0]})
        before = s.status_at
        s.patch_action({"pos": [2, 64, 0]})
        self.assertEqual(s.status_at, before)
        self.assertEqual(s.pos, [2, 64, 0])

    def test_inflight_inventory_invalidation_is_not_lost(self):
        s, c = State(), FakeClient()
        def inventory():
            s.invalidate_inventory()
            return {"items": []}
        c.inventory = inventory
        s.refresh_inventory(c)
        self.assertTrue(s.inventory_dirty)

    def test_old_status_cannot_overwrite_action_result(self):
        s, c = State(), FakeClient()
        s.set_status(c.st)
        s.status_at -= 2
        def old_read():
            s.patch_action({"pos": [2, 64, 0]})
            return c.st
        c.status = old_read
        self.assertEqual(s.refresh_status(c)["pos"], [2, 64, 0])
        self.assertGreater(time.monotonic()-s.status_at, 1)


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.c = FakeClient()
        self.s = State(replace(RuntimeConfig(), stop_cooldown_s=10))
        self.ex = Executor(self.c, self.s, log=lambda *_: None)
        self.mon = SafetyMonitor(self.c, self.s, self.ex)

    def tick(self, **status):
        self.c.st.update(status)
        self.s.status_at = float("-inf")
        return self.mon.tick()

    def test_place_does_not_trigger_false_void_stop_before_next_status(self):
        self.s.set_risk_context(void_guard=True)
        self.c.st["pos"] = [0.5, 64, 0.5]
        blocks = {(0, 63, 0): "stone", (1, 63, 0): "cobblestone"}
        self.c.get_block = Mock(side_effect=lambda p: {"name": blocks.get(tuple(p), "air")})
        self.c.place = Mock(return_value={"placed": "cobblestone", "pos": [1, 63, 0]})
        with patch("mcagent.runtime.time.monotonic", return_value=100.0):
            self.mon.tick()
            self.ex.place("cobblestone", pos=[1, 63, 0])
            self.mon.tick()
        self.assertEqual(self.c.status_calls, 1)  # 下一次真实 /status 之前的缓存窗口
        self.assertEqual(self.c.stops, 0)
        self.assertIsNone(self.s.safety_intent)
        self.assertEqual(self.s.pos, [0.5, 64, 0.5])

    def test_sustained_lava_stops_once_even_after_cooldown(self):
        self.tick(in_lava=True)
        self.mon._triggered["escape_lava"] -= 30
        for _ in range(4):
            self.tick()
        self.assertEqual(self.c.stops, 1)
        self.tick(in_lava=False)
        self.tick(in_lava=True)
        self.assertEqual(self.c.stops, 2)
        self.tick(in_lava=False)
        self.tick(in_lava=True)
        self.assertEqual(self.c.stops, 2)  # flapping during cooldown
        self.mon._triggered["escape_lava"] -= 30
        self.tick()
        self.assertEqual(self.c.stops, 3)

    def test_soft_low_health_keeps_move_but_stops_combat(self):
        self.ex.active_action = "move_to"
        self.tick(health=8)
        self.assertEqual(self.c.stops, 0)
        self.assertEqual(self.s.safety_intent["kind"], "heal")
        self.ex.active_action = "attack"
        self.tick()
        self.tick()
        self.assertEqual(self.c.stops, 1)

    def test_new_urgent_hazard_bypasses_other_kind_cooldown(self):
        self.tick(in_lava=True)
        self.tick(submerged=True, oxygen=10)
        self.tick()
        self.assertEqual(self.c.stops, 2)

    def test_failed_stop_retries_but_failed_escape_does_not_restop(self):
        original = self.c.stop
        self.c.stop = Mock(side_effect=ApiError("network"))
        with self.assertRaises(ApiError):
            self.tick(in_lava=True)
        self.c.stop = original
        self.mon.on_hazard = Mock(side_effect=ValueError("escape failed"))
        with self.assertRaises(ValueError):
            self.tick()
        self.tick()
        self.assertEqual(self.c.stops, 1)

    def test_risk_intervals(self):
        self.tick()
        self.assertEqual(self.mon.next_interval(), 4)
        for changes in ({"time_of_day": 18000}, {"dimension": "the_nether"},
                        {"nearest_hostile": {"dist": 16}}, {"health": 13}, {"in_water": True}):
            base = dict(self.c.st)
            self.tick(**changes)
            self.assertEqual(self.mon.next_interval(), 1)
            self.c.st = base
        self.s.set_risk_context(near_water=True)
        self.assertEqual(self.mon.next_interval(), 1)

    def test_void_guard_negative_coords_half_slab_and_unknown(self):
        self.s.set_risk_context(void_guard=True)
        self.tick(pos=[-0.5, 64.5, -1.5])
        self.assertEqual(self.c.block_calls[-1], [-1, 64, -2])
        self.assertEqual(self.s.safety_intent["kind"], "void_risk")
        self.c.block = {"name": "stone_slab"}
        self.tick()
        self.assertIsNone(self.s.safety_intent)
        self.c.block = ApiError("not_loaded")
        self.tick()
        self.assertIn("查询失败", self.s.safety_intent["why"])

    def test_void_guard_aabb_overhang_on_bridge_edge(self):
        # 复现 1.21.11 靶场实测：非潜行走上单格桥时中心点 [-46.3, 64, 102.5] 落在空气格 [-47, 63, 102]，
        # 但 on_ground=True 且碰撞箱 +0.35 角踩在邻格 [-46, 63, 102] 实心块上，不应误触发 void_risk /stop。
        self.s.set_risk_context(void_guard=True)
        blocks = {
            (-47, 63, 102): "air",
            (-46, 63, 102): "dirt",
        }
        self.c.get_block = Mock(side_effect=lambda p: {"name": blocks.get(tuple(p), "air")})
        self.tick(pos=[-46.3, 64.0, 102.5], on_ground=True, sneaking=False)
        self.assertEqual(self.c.stops, 0)
        self.assertIsNone(self.s.safety_intent)
        self.assertEqual(self.s.support.get("aabb_support"), {"pos": [-46, 63, 102], "name": "dirt"})

        # 若一旦真离地（on_ground=False）或四角全为空气，则必须立即触发 void_risk 急停
        self.tick(pos=[-46.3, 64.0, 102.5], on_ground=False, sneaking=False)
        self.assertEqual(self.c.stops, 1)
        self.assertEqual(self.s.safety_intent["kind"], "void_risk")

    def test_jump_not_void_but_descent_without_support_is_risk(self):
        self.tick(on_ground=False, pos=[0, 65, 0])
        self.tick(pos=[0, 66, 0])
        self.assertEqual(self.c.stops, 0)
        self.tick(pos=[0, 64, 0])
        self.assertEqual(self.c.stops, 1)
        self.mon._triggered["void_risk"] -= 30
        self.tick(pos=[0, 62, 0])
        self.assertEqual(self.c.stops, 1)

    def test_lava_stops_before_events_and_block_queries(self):
        self.c.get_block = Mock(side_effect=lambda _: self.assertEqual(self.c.stops, 1) or {"name": "air"})
        self.s.set_risk_context(void_guard=True)
        self.tick(in_lava=True)
        self.assertGreaterEqual(self.c.stops, 1)


class RequestTests(unittest.TestCase):
    def test_protocol_stop_on_defaults(self):
        c = Client(Config("https://unused", "", ""))
        c.call = Mock(return_value={})
        for action, args, expected in (("move_to", {"x": 1, "z": 2}, ["hurt", "death"]),
                                       ("dig", {"pos": [0, 0, 0]}, ["hurt", "death"]),
                                       ("attack", {"entity_id": 1}, ["death"]),
                                       ("shoot", {"entity_id": 1}, ["death"])):
            getattr(c, action)(**args)
            self.assertEqual(c.call.call_args.args[1]["stop_on"], expected)
        c.move_to(1, 2, stop_on=["death"])
        self.assertEqual(c.call.call_args.args[1]["stop_on"], ["death"])

    def test_limiter_serializes_requests_and_prioritizes_stop(self):
        limiter = RequestLimiter(4)
        limiter.acquire()
        order, times = [], []
        def worker(name):
            limiter.acquire(urgent=name == "stop")
            order.append(name)
            times.append(time.monotonic())
        threads = [threading.Thread(target=worker, args=(name,)) for name in ("a", "b", "stop", "c", "d")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        self.assertEqual(order[0], "stop")
        self.assertTrue(all(b-a >= .245 for a, b in zip(times, times[1:])))
        self.assertGreaterEqual(times[4]-times[0], 1)

    def test_invalid_config_rejected(self):
        for rate in (0, 5, float("nan")):
            with self.assertRaises(ValueError):
                Config("unused", "", "", max_rps=rate)
        with patch.dict("os.environ", {"MC_STATUS_TTL_S": "nan"}):
            with self.assertRaises(ValueError):
                RuntimeConfig.load()


class SchedulerTests(unittest.TestCase):
    def test_timeout_does_not_wait_and_late_answer_is_discarded(self):
        release = threading.Event()
        started = threading.Event()
        def ask(_):
            started.set()
            release.wait(1)
            return "late"
        cfg = replace(RuntimeConfig(), model_timeout_s=.02, model_min_interval_s=.001)
        scheduler = DecisionScheduler(ask, cfg)
        self.assertFalse(scheduler.notify("skill_done", "", iron_complete=False))
        self.assertTrue(scheduler.notify("skill_done", "", iron_complete=True))
        self.assertTrue(started.wait(1))
        self.assertIsNone(scheduler.poll(lambda: "rules"))
        time.sleep(.03)
        t = time.monotonic()
        self.assertEqual(scheduler.poll(lambda: "rules"), "rules")
        self.assertLess(time.monotonic()-t, .01)
        self.assertFalse(scheduler.notify("uncovered", "", iron_complete=True))
        release.set()
        scheduler._worker.join(1)
        self.assertIsNone(scheduler.poll(lambda: "rules"))

    def test_failure_count_phase_invalidation_and_review(self):
        scheduler = DecisionScheduler(lambda _: "goal", replace(RuntimeConfig(), model_min_interval_s=.001))
        self.assertFalse(scheduler.notify("review", "", iron_complete=True))
        self.assertFalse(scheduler.notify("skill_failed", "", skill="wood", iron_complete=True))
        self.assertFalse(scheduler.notify("skill_failed", "", skill="stone", iron_complete=True))
        self.assertTrue(scheduler.notify("skill_failed", "", skill="stone", iron_complete=True))
        scheduler._worker.join(1)
        self.assertEqual(scheduler.poll(lambda: "rules"), "goal")
        time.sleep(.002)
        self.assertTrue(scheduler.notify("uncovered", "", iron_complete=True))
        scheduler.invalidate()
        scheduler._worker.join(1)
        self.assertIsNone(scheduler.poll(lambda: "rules"))


class ProbeTests(unittest.TestCase):
    def test_empty_template_cannot_run_live_and_dry_never_connects(self):
        fixture = path_template()
        with self.assertRaises(ValueError):
            validate_fixture(fixture, ["wall"])
        with patch("mcagent.probe_path.Client") as client, patch("builtins.print"):
            self.assertEqual(pathfinding_main(["--pathfinding", "--dry"]), 0)
            client.assert_not_called()

    def test_fixture_separates_pillar_climbing_and_real_20_blocks(self):
        fixture = path_template()
        for name, case in fixture["cases"].items():
            case.update(start=[0, 64, 0], target=[0, 68, 0], witness_blocks=[[0, 65, 0]])
        fixture["cases"]["move20"]["target"] = [20, 64, 0]
        validate_fixture(fixture, list(fixture["cases"]))
        fixture["cases"]["pillar"]["target"] = [3, 68, 0]
        with self.assertRaises(ValueError):
            validate_fixture(fixture, ["pillar"])
        fixture["cases"]["move20"]["target"] = [10, 64, 0]
        with self.assertRaises(ValueError):
            validate_fixture(fixture, ["move20"])

class ProbeIntegrationTests(unittest.TestCase):
    def run_probe(self, fail=False):
        import json
        import tempfile
        from pathlib import Path
        from mcagent.client import DEFAULT_BASE
        fixture = path_template()
        fixture["cases"]["move20"].update(start=[0, 64, 0], target=[20, 64, 0], witness_blocks=[])
        client = FakeClient()
        client.wait_ready = lambda: client.st
        client.disconnect = Mock(return_value={})
        def move_to(**kw):
            if fail and kw["pos"][0] == 20:
                raise ApiError("path_not_found")
            client.st["pos"] = kw["pos"]
            return {"pos": kw["pos"]}
        client.move_to = move_to
        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp)/"fixture.json", Path(tmp)/"report.json"
            source.write_text(json.dumps(fixture))
            with patch("mcagent.probe_path.Client", return_value=client), patch("mcagent.probe.Config.load", return_value=Config(DEFAULT_BASE, "", "")), patch("mcagent.runtime.SafetyMonitor.start"), patch("builtins.print"):
                rc = pathfinding_main(["--pathfinding", "--live", "--local-target", "--fixture", str(source),
                                       "--case", "move20", "--samples", "1", "--output", str(output)])
            report = json.loads(output.read_text(encoding="utf-8"))
        client.disconnect.assert_called_once()
        return rc, report

    def test_local_target_evidence_and_success_only_c1(self):
        rc, report = self.run_probe()
        self.assertEqual(rc, 0)
        self.assertEqual(report["source"], "local-target")
        self.assertEqual(report["cases"][0]["target_error_3d"], 0)
        self.assertIn("T_move20", report["C1"])
        self.assertNotIn("T_dig1", report["C1"])

    def test_failed_path_still_saves_evidence_and_disconnects(self):
        rc, report = self.run_probe(fail=True)
        self.assertEqual(rc, 1)
        self.assertFalse(report["complete"])
        self.assertEqual(report["cases"][0]["status"], "inconclusive")
        self.assertIn("after", report["cases"][0])
        self.assertNotIn("T_move20", report["C1"])

    def test_wall_rejection_gap_jump_pearl_soft_heal_and_block_center_tolerance(self):
        import json
        import tempfile
        from pathlib import Path
        from mcagent.client import DEFAULT_BASE

        fixture = path_template()
        fixture["cases"]["wall"].update(start=[0, 64, 0], target=[4, 64, 0], witness_blocks=[[2, 64, 0]])
        fixture["cases"]["pillar"].update(start=[0, 64, 0], target=[0, 67, 0], witness_blocks=[[0, 65, 0]])
        fixture["cases"]["pearl"].update(start=[0, 64, 0], target=[15, 64, 0], witness_blocks=[])
        fixture["cases"]["move20"].update(start=[0, 64, 0], target=[20, 64, 0], witness_blocks=[])

        client = FakeClient()
        client.wait_ready = lambda: dict(client.st)
        client.disconnect = Mock(return_value={})
        client.inventory = lambda: {"items": [{"name": "ender_pearl", "count": 5, "slot": 0}]}
        client.advancements = lambda **_: {"advancements": [], "ready": True}
        client.find_blocks = lambda **_: {"blocks": {}}
        client.get_entities = lambda **_: {"entities": []}

        def get_block(pos):
            if pos == [2, 64, 0]:
                return {"name": "stone"}
            return {"name": "air"}

        client.get_block = get_block

        def move_to(x=None, z=None, y=None, entity_id=None, rng=1, pos=None, stop_on=None):
            target = list(pos) if pos is not None else [x, client.st["pos"][1] if y is None else y, z]
            if target == [4, 64, 0]:
                raise ApiError("path_not_found", "blocked by stone wall")
            if target == [20, 64, 0]:
                # 模拟真实寻路停在方块中心附近（距整数坐标约 0.72 > 0.5）
                ended = [19.4, 64.0, 0.4]
                client.st["pos"] = ended
                return {"pos": ended}
            if target == [0, 64, 0]:
                ended = [0.5, 64.0, 0.5]
                client.st["pos"] = ended
                return {"pos": ended}
            client.st["pos"] = target
            return {"pos": target}

        def use_item(item, target=None, **kw):
            self.assertEqual(item, "ender_pearl")
            # 连投 3 次扣血到 8（触发软 heal 意图但不发 /stop）
            client.st["health"] = max(7, client.st["health"] - 4)
            landed = [14.6, 64.0, 0.3]
            client.st["pos"] = landed
            return {"used": item, "thrown": True, "teleported": True, "pos": landed}

        client.move_to = move_to
        client.use_item = use_item

        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "fixture.json", Path(tmp) / "report.json"
            source.write_text(json.dumps(fixture))
            with (
                patch("mcagent.probe_path.Client", return_value=client),
                patch("mcagent.probe.Config.load", return_value=Config(DEFAULT_BASE, "u", "p")),
                patch("builtins.print"),
            ):
                rc = pathfinding_main([
                    "--pathfinding", "--live", "--local-target",
                    "--fixture", str(source),
                    "--case", "wall", "--case", "pillar", "--case", "pearl", "--case", "move20",
                    "--pearl-trials", "3", "--samples", "1", "--output", str(output),
                ])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(report["complete"])
        by_case = {}
        for r in report["cases"]:
            by_case.setdefault(r["case"], []).append(r)
        self.assertEqual(by_case["wall"][0]["status"], "observed")
        self.assertEqual(by_case["wall"][0]["outcome"], "rejected:path_not_found")
        self.assertEqual(by_case["pillar"][0]["status"], "observed")
        self.assertFalse(by_case["pillar"][0].get("safety_interference", False))
        self.assertEqual(report["pearl_error"]["n"], 3)
        self.assertIn("T_move20", report["C1"])

    def test_legacy_probe_catches_replan_and_writes_report(self):
        import json
        import tempfile
        from pathlib import Path
        from mcagent import probe as probe_mod

        client = FakeClient()
        client.wait_ready = lambda timeout=120: dict(client.st)
        client.find_blocks = lambda **_: {"blocks": {}}
        client.get_entities = lambda **_: {"entities": []}
        client.advancements = lambda **_: {"advancements": [], "ready": True}
        client.posture = lambda **_: {"sneaking": False, "blocking": False}

        def craft(item, count=1, stop_on=None):
            raise ApiError("missing_ingredients", "need diamonds", {"needed": 3})

        client.craft = craft

        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch.object(probe_mod, "OUT_DIR", tmp),
                patch.object(probe_mod, "Client", return_value=client),
                patch.object(probe_mod.Config, "load", return_value=Config("https://mock", "u", "p")),
                patch.object(probe_mod, "bench", side_effect=lambda fn, n=1, warmup=0: (fn(), {"n": 1, "err": 0, "p50": 1.0, "min": 1.0, "max": 1.0, "all": [1]})[1]),
                patch.object(probe_mod.time, "sleep"),
                patch("builtins.print"),
            ):
                rc = probe_mod.main(["--no-move", "--no-dig"])
            files = list(Path(tmp).iterdir())
            self.assertEqual(rc, 0)
            self.assertEqual(len(files), 1)
            saved = json.loads(files[0].read_text(encoding="utf-8"))
            self.assertEqual(saved["craft_missing"]["code"], "missing_ingredients")
            self.assertEqual(saved["craft_missing"]["extra"], {"needed": 3})


class AuditRegressionTests(unittest.TestCase):
    def test_place_bridging_does_not_trigger_extra_inventory_reads(self):
        c, s = FakeClient(), State()
        ex = Executor(c, s, log=lambda *_: None)
        for i in range(5):
            ex.act("place", lambda i=i: {"placed": "cobblestone", "pos": [i, 63, 0]})
        self.assertEqual(c.inventory_calls, 0)
        self.assertTrue(s.inventory_dirty)
        # craft 仍需在执行前拍一次快照以支持丢包判重
        ex.act("craft", lambda: {"crafted": "stick", "gained": 4})
        self.assertEqual(c.inventory_calls, 1)

    def test_restart_multipage_events_catches_up_without_false_safe_mode(self):
        s = State()
        now_ms = 2_000_000_000
        pages = [
            {
                "events": [{"seq": 1, "t": now_ms - 7_200_000, "type": "chat", "text": "old"}],
                "last_seq": 1,
                "has_more": True,
            },
            {
                "events": [
                    {"seq": 2, "t": now_ms - 7_000_000, "type": "death", "pos": [0, 64, 0]},
                    {"seq": 3, "t": now_ms - 6_990_000, "type": "death", "pos": [0, 64, 0]},
                    {"seq": 4, "t": now_ms, "type": "pickup", "item": "oak_log", "count": 1},
                ],
                "last_seq": 4,
                "has_more": False,
            },
            {
                "events": [{"seq": 5, "t": now_ms + 1000, "type": "chat", "text": "steady1"}],
                "last_seq": 5,
                "has_more": True,
            },
            {
                "events": [{"seq": 6, "t": now_ms + 2000, "type": "chat", "text": "steady2"}],
                "last_seq": 6,
                "has_more": False,
            },
        ]
        calls = 0

        class PagedClient:
            def events(self, since=0, limit=200):
                nonlocal calls
                idx = min(calls, len(pages) - 1)
                calls += 1
                return pages[idx]

        pc = PagedClient()
        evs = s.drain(pc)
        self.assertEqual(calls, 2)
        self.assertEqual(len(evs), 4)
        self.assertEqual(s.deaths_in(60), 0)
        self.assertFalse(s.in_safe_mode)
        # 进入稳态后每次只读一页，不挤占急停额度
        evs2 = s.drain(pc)
        self.assertEqual(calls, 3)
        self.assertEqual(len(evs2), 1)

    def test_sneak_bridge_step_down_and_safe_mode_hazard_ordering(self):
        c = FakeClient()
        s = State(replace(RuntimeConfig(), stop_cooldown_s=10))
        ex = Executor(c, s, log=lambda *_: None)
        mon = SafetyMonitor(c, s, ex)

        # 1) 潜行搭桥探边（on_ground=True, sneaking=True）：脚下中心虽是空气，不得误发 /stop
        s.set_risk_context(void_guard=True)
        c.st.update(pos=[10.25, 64.0, 10.0], on_ground=True, sneaking=True)
        s.status_at = float("-inf")
        mon.tick()
        self.assertEqual(c.stops, 0)
        self.assertIsNone(s.safety_intent)

        # 2) 高风险区起跳上升瞬间（64.0 -> 64.42）：不得误判坠落
        c.st.update(pos=[10.25, 64.42, 10.0], on_ground=False, sneaking=False)
        s.status_at = float("-inf")
        mon.tick()
        self.assertEqual(c.stops, 0)

        # 3) 普通地形下 1~2 格台阶（脚下 1 格空气、第 2 格泥土）：不得误判虚空
        s.set_risk_context(void_guard=False)
        c.st.update(pos=[0, 65.0, 0], on_ground=True)
        s.status_at = float("-inf")
        mon.tick()
        c.get_block = lambda pos: {"name": "air"} if pos[1] >= 64 else {"name": "dirt"}
        c.st.update(pos=[1, 64.2, 0], on_ground=False)
        s.status_at = float("-inf")
        mon.tick()
        self.assertEqual(c.stops, 0)

        # 4) safe_mode 期间掉入岩浆：safety_intent 必须优先暴露 escape_lava 而非被 safe_mode 遮蔽
        s.safe_mode_until = time.monotonic() + 60
        c.st.update(in_lava=True, on_ground=True)
        s.status_at = float("-inf")
        mon.tick()
        self.assertEqual(s.safety_intent["kind"], "escape_lava")

    def test_stop_on_supported_for_use_item_and_scheduler_urgent_events(self):
        from mcagent.planner.decision import validate
        from mcagent.planner.llm import LLMClient

        ok, errs = validate({
            "goal": "heal:eat",
            "action": "use_item",
            "args": {"item": "cooked_beef", "stop_on": ["death"]},
        })
        self.assertTrue(ok, errs)
        ok_posture, _ = validate({
            "goal": "sneak",
            "action": "posture",
            "args": {"sneak": True, "stop_on": ["death"]},
        })
        self.assertFalse(ok_posture)

        c = Client(Config("https://unused", "", ""))
        c.call = Mock(return_value={})
        c.use_item("cooked_beef", stop_on=["death"])
        self.assertEqual(c.call.call_args.args[1]["stop_on"], ["death"])

        # DecisionScheduler: routine 事件受 min_interval 节流，但 phase_changed/uncovered/连续失败立即放行
        cfg = replace(RuntimeConfig(), model_min_interval_s=30.0)
        sched = DecisionScheduler(lambda _: "plan_a", cfg)
        self.assertTrue(sched.notify("skill_done", "s1", iron_complete=True))
        sched._worker.join(1)
        self.assertEqual(sched.poll(lambda: "fallback"), "plan_a")
        self.assertFalse(sched.notify("skill_done", "s2", iron_complete=True))
        self.assertTrue(sched.notify("phase_changed", "entered_nether", iron_complete=True))
        sched._worker.join(1)
        self.assertEqual(sched.poll(lambda: "fallback"), "plan_a")

        # LLMClient.decide total_timeout 严格收敛单次调用超时预算
        seen_timeouts = []

        def slow_poster(url, headers, data, timeout, proxy):
            seen_timeouts.append(timeout)
            time.sleep(0.06)
            return 200, '{"choices":[{"message":{"content":"bad"}}]}'

        llm = LLMClient(key="k", poster=slow_poster, timeout=90)
        d, errs, used = llm.decide("state", attempts=3, total_timeout=0.08)
        self.assertIsNone(d)
        self.assertLessEqual(used, 2)
        self.assertTrue(all(t <= 0.09 for t in seen_timeouts))

    def test_manual_sequences_and_climb_xz_in_pathfinding_probe(self):
        import json
        import math
        import tempfile
        from pathlib import Path
        from mcagent.client import DEFAULT_BASE

        fixture = path_template()
        fixture["cases"]["climb_xz"].update(start=[0.5, 64, 0.5], target=[4.5, 66, 0.5], witness_blocks=[])
        fixture["cases"]["manual_breach"].update(
            start=[0.5, 64, 0.5],
            target=[3.5, 64, 0.5],
            witness_blocks=[[1, 65, 0], [1, 64, 0]],
        )
        fixture["cases"]["manual_bridge"].update(
            start=[0.5, 64, 0.5],
            target=[3.5, 64, 0.5],
            witness_blocks=[[1, 63, 0], [2, 63, 0]],
        )
        fixture["cases"]["manual_step_up"].update(
            start=[0.5, 64, 0.5],
            target=[0.5, 66, 0.5],
            witness_blocks=[[1, 64, 0], [0, 64, 0], [0, 65, 0]],
        )

        world = {(1, 65, 0): "stone", (1, 64, 0): "stone"}
        inv_counts = {"wooden_pickaxe": 1, "dirt": 10}
        client = FakeClient()
        client.st["pos"] = [0.5, 64.0, 0.5]
        client.wait_ready = lambda: dict(client.st)
        client.disconnect = Mock(return_value={})
        client.posture = lambda sneak=None, shield=None: {"sneaking": bool(sneak), "blocking": bool(shield)}
        equipped = []

        def inventory():
            return {"items": [{"name": k, "count": v, "slot": i} for i, (k, v) in enumerate(inv_counts.items()) if v > 0]}

        def get_block(pos):
            return {"name": world.get(tuple(pos), "air")}

        def equip(item, slot="hand", stop_on=None):
            equipped.append((item, slot))
            return {"equipped": item, "slot": slot}

        def dig(pos, collect=True, stop_on=None):
            key = tuple(pos)
            dug = world.pop(key, "air")
            return {"dug": dug, "dig_ms": 500, "gained": {}}

        def place(item, pos=None, stop_on=None):
            p = client.st["pos"]
            feet = [math.floor(p[0]), math.floor(p[1]), math.floor(p[2])]
            if list(pos) == feet:
                raise ApiError("blocked_by_self", "inside own hitbox")
            inv_counts[item] -= 1
            world[tuple(pos)] = item
            return {"placed": item, "pos": list(pos), "sneaked": True}

        move_calls = []

        def move_to(x=None, z=None, y=None, entity_id=None, rng=1, pos=None, stop_on=None):
            move_calls.append({"x": x, "z": z, "y": y, "pos": pos})
            if pos is not None:
                dest = [float(pos[0]), float(pos[1]), float(pos[2])]
            else:
                # 模拟省略 y 时自动爬上地面高度 66
                dest = [float(x), 66.0 if x == 4.5 else float(client.st["pos"][1]), float(z)]
            client.st["pos"] = dest
            return {"pos": dest}

        client.inventory = inventory
        client.get_block = get_block
        client.equip = equip
        client.dig = dig
        client.place = place
        client.move_to = move_to

        with tempfile.TemporaryDirectory() as tmp:
            source, output = Path(tmp) / "fixture.json", Path(tmp) / "report.json"
            source.write_text(json.dumps(fixture))
            with (
                patch("mcagent.probe_path.Client", return_value=client),
                patch("mcagent.probe.Config.load", return_value=Config(DEFAULT_BASE, "u", "p")),
                patch("mcagent.runtime.SafetyMonitor.start"),
                patch("builtins.print"),
            ):
                rc = pathfinding_main([
                    "--pathfinding", "--manual-only", "--live", "--local-target",
                    "--fixture", str(source), "--samples", "1", "--output", str(output),
                ])
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(rc, 0)
        self.assertTrue(report["complete"])
        by_case = {r["case"]: r for r in report["cases"]}
        self.assertEqual(set(by_case), {"climb_xz", "manual_breach", "manual_bridge", "manual_step_up"})
        self.assertTrue(all(r["status"] == "observed" and r["outcome"] == "ok" for r in by_case.values()))
        # climb_xz 动作调用必须省略 pos 与 y，仅传 x/z
        self.assertIsNone(by_case["climb_xz"]["action"]["args"].get("pos"))
        self.assertEqual(by_case["climb_xz"]["actual_y_gain"], 2.0)
        # manual_breach 应先显式装备 wooden_pickaxe 再挖开两格石墙
        self.assertIn(("wooden_pickaxe", "hand"), equipped)
        # manual_step_up 应记录脚底碰撞箱 place 被拒，并用 1x2 交替垫块登上 y=66
        self.assertEqual(by_case["manual_step_up"]["self_feet_place"]["code"], "blocked_by_self")
        self.assertEqual(by_case["manual_step_up"]["after"]["status"]["pos"][1], 66.0)
