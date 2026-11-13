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
        self.tick(pos=[-0.2, 64.5, -1.2])
        self.assertEqual(self.c.block_calls[-1], [-1, 64, -2])
        self.assertEqual(self.s.safety_intent["kind"], "void_risk")
        self.c.block = {"name": "stone_slab"}
        self.tick()
        self.assertIsNone(self.s.safety_intent)
        self.c.block = ApiError("not_loaded")
        self.tick()
        self.assertIn("查询失败", self.s.safety_intent["why"])

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
        with patch("mcagent.probe.Client") as client, patch("builtins.print"):
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
            with patch("mcagent.probe.Client", return_value=client), patch("mcagent.probe.Config.load", return_value=Config(DEFAULT_BASE, "", "")), patch("mcagent.runtime.SafetyMonitor.start"), patch("builtins.print"):
                rc = pathfinding_main(["--pathfinding", "--live", "--local-target", "--fixture", str(source),
                                       "--case", "move20", "--samples", "1", "--output", str(output)])
            report = json.loads(output.read_text())
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
