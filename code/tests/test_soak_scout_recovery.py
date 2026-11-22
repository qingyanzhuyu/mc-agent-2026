"""local-target INC-002: scout changes candidates after an observed navigation failure."""
import unittest

from mcagent.client import ApiError
from mcagent.loop import reset_world_memo
from mcagent.loop.starter_skills import VISITED_ANCHORS
from mcagent.runtime import Replan
from mcagent.skills import make
from mcagent.skills.base import _COOLDOWN
from mcagent.skills.testing import drive
from tests.test_loop_soak import RichGridWorld


class ScoutFailureWorld(RichGridWorld):
    def __init__(self, *, blocks=None, failures=None):
        super().__init__(blocks=blocks or {(18, 63, 0): "grass_block", (10, 63, 0): "grass_block"})
        self.failures = failures or {}
        self.block_reads = []

    def get_block(self, pos):
        self.block_reads.append(tuple(pos))
        return super().get_block(pos)

    def move_to(self, *args, **kwargs):
        dest = tuple(kwargs["pos"])
        if dest in self.failures:
            self.calls.append(("move_to", {"pos": list(dest)}))
            raise ApiError(self.failures[dest], "injected scout navigation failure")
        return super().move_to(*args, **kwargs)


class ScoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def tearDown(self):
        reset_world_memo()
        _COOLDOWN.clear()

    def test_failed_farthest_candidate_yields_then_moves_to_alternative(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "path_not_found"})
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["op"] for s in steps], ["move_to", "move_to"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual([s["pos"] for s in steps], [[18.5, 64, 0.5], [10.5, 64, 0.5]])
        self.assertEqual(VISITED_ANCHORS, [(10.5, 0.5)])

    def test_side_exposed_and_submerged_blocks_are_not_standing_positions(self):
        world = ScoutFailureWorld(blocks={
            (18, 63, 0): "grass_block", (18, 64, 0): "oak_log",
            (14, 63, 0): "sand", (14, 64, 0): "water",
            (10, 63, 0): "grass_block",
        })
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[10.5, 64, 0.5]])
        self.assertEqual(ex.acts, ["move_to"])

    def test_in_water_prefers_nearest_dry_land_over_farthest_destination(self):
        world = ScoutFailureWorld(blocks={(18, 63, 0): "grass_block", (6, 63, 0): "sand"})
        world.st["in_water"] = True
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual(steps[0]["pos"], [6.5, 64, 0.5])

    def test_all_failed_candidates_preserve_error_and_limit_action_attempts(self):
        blocks = {(x, 63, 0): "grass_block" for x in (6, 10, 14, 18, 22)}
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x, _, _ in blocks}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        ex, state = world.session()
        skill = make("scout_island")
        with self.assertRaisesRegex(Replan, "path_not_found"):
            drive(skill, ex, state)
        self.assertEqual(ex.acts, ["move_to"] * 3)
        self.assertEqual(skill.last["error_code"], "path_not_found")
        self.assertEqual(VISITED_ANCHORS, [])

    def test_repeated_failed_scouts_do_not_create_fictitious_visit_history(self):
        # ep-0004: repeated failures from below the surface used to mark each
        # unreachable destination visited, changing the next invocation's ranking.
        blocks = {(x, 63, 0): "grass_block" for x in (6, 10, 14, 18, 22)}
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x, _, _ in blocks}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"][1] = 60.0
        ex, state = world.session()
        real_visits = [(0.5, 0.5)]
        VISITED_ANCHORS.extend(real_visits)
        destinations = []
        for _ in range(3):
            before = len(world.calls)
            with self.assertRaisesRegex(Replan, "path_not_found"):
                drive(make("scout_island"), ex, state)
            destinations.append([args["pos"] for op, args in world.calls[before:] if op == "move_to"])
            self.assertEqual(VISITED_ANCHORS, real_visits)
        self.assertEqual([len(attempts) for attempts in destinations], [3, 3, 3])
        self.assertEqual(destinations[0], destinations[1])
        self.assertEqual(destinations[1], destinations[2])
        self.assertEqual(ex.acts, ["move_to"] * 9)

    def test_fallback_success_records_observed_arrival_instead_of_requested_target(self):
        class NearArrivalWorld(ScoutFailureWorld):
            def move_to(self, *args, **kwargs):
                result = super().move_to(*args, **kwargs)
                # A successful range=2 move can stop short of the requested point.
                self.st["pos"][0] -= 1.0
                return {**result, "pos": list(self.st["pos"])}

        world = NearArrivalWorld(failures={(18.5, 64, 0.5): "path_not_found"})
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([step["op"] for step in steps], ["move_to", "move_to"])
        self.assertEqual(steps[0]["error_code"], "path_not_found")
        self.assertEqual(steps[1]["pos"], [10.5, 64, 0.5])
        self.assertEqual(state.pos, [9.5, 64.0, 0.5])
        self.assertEqual(VISITED_ANCHORS, [(9.5, 0.5)])

    def test_failed_region_is_not_retried_at_neighboring_block(self):
        world = ScoutFailureWorld(
            blocks={(18, 63, 0): "grass_block", (17, 63, 0): "grass_block", (10, 63, 0): "grass_block"},
            failures={(18.5, 64, 0.5): "unreachable"},
        )
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[18.5, 64, 0.5], [10.5, 64, 0.5]])

    def test_abort_after_failed_move_starts_no_fallback_and_marks_no_visit(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "move_timeout"})
        ex, state = world.session()
        skill = make("scout_island")
        self.assertFalse(skill.step(ex, state))
        self.assertEqual(skill.last["error_code"], "move_timeout")
        skill.abort()
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(VISITED_ANCHORS, [])

    def test_safety_interruption_is_propagated_without_fallback(self):
        world = ScoutFailureWorld(failures={(18.5, 64, 0.5): "interrupted"})
        ex, state = world.session()
        with self.assertRaisesRegex(Replan, "interrupted"):
            drive(make("scout_island"), ex, state)
        self.assertEqual(ex.acts, ["move_to"])
        self.assertEqual(VISITED_ANCHORS, [])

    def test_unloaded_candidate_is_skipped_without_navigation(self):
        class UnloadedWorld(ScoutFailureWorld):
            def get_block(self, pos):
                if pos[0] == 18:
                    raise ApiError("not_loaded", "injected unloaded standing space")
                return super().get_block(pos)

        world = UnloadedWorld()
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertEqual([s["pos"] for s in steps], [[10.5, 64, 0.5]])

    def test_above_sea_pit_trapped_climbs_staircase_instead_of_looping(self):
        # 复现 local-target T4-P2 实测缺陷：角色在 Y=67 地表凹坑（高于海平面 Y=62）持镐被围困，
        # 所有高位地表候选 path_not_found 且水平脱困在同一凹坑内反复打转时，须触发 1x2 阶梯向上脱困。
        from mcagent.loop.starter_skills import RECENT_RECOVERY_LANDINGS

        blocks = {
            # 高位地表候选（全部不可直达）
            (10, 72, 0): "grass_block",
            (14, 72, 0): "grass_block",
            (18, 72, 0): "grass_block",
            # 脚下与凹坑内可水平挪动格 (0,67,1) 及其东侧石墙 (1,67,1)/(1,68,1)
            (0, 66, 0): "stone",
            (0, 66, 1): "stone",
            (1, 66, 0): "stone", (1, 67, 0): "stone", (1, 68, 0): "stone",
            (1, 66, 1): "stone", (1, 67, 1): "stone", (1, 68, 1): "stone",
            (-1, 66, 0): "stone", (-1, 67, 0): "stone", (-1, 68, 0): "stone",
            (-1, 66, 1): "stone", (-1, 67, 1): "stone", (-1, 68, 1): "stone",
            (0, 66, -1): "stone", (0, 67, -1): "stone", (0, 68, -1): "stone",
            (0, 66, 2): "stone", (0, 67, 2): "stone", (0, 68, 2): "stone",
        }
        failures = {(x + 0.5, 73, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 67.0, 0.5]
        world.items["wooden_pickaxe"] = 1
        RECENT_RECOVERY_LANDINGS.extend([(0.5, 67.0, 0.5), (0.5, 67.0, 1.5)])
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertGreaterEqual(state.pos[1], 68.0)
        self.assertIn("dig", [s["op"] for s in steps])
        self.assertEqual(list(RECENT_RECOVERY_LANDINGS), [])

    def test_pit_escape_without_pickaxe_uses_placeable_step_up(self):
        # 复现 local-target 木镐挖断后无镐场景：石壁无法徒手挖，但背包有 dirt/cobblestone 时须自动垫块爬升。
        from mcagent.loop.starter_skills import RECENT_RECOVERY_LANDINGS

        blocks = {
            (10, 72, 0): "grass_block",
            (14, 72, 0): "grass_block",
            (18, 72, 0): "grass_block",
            (0, 66, 0): "stone",
            (0, 66, 1): "stone",
            (1, 66, 0): "stone", (1, 67, 0): "stone", (1, 68, 0): "stone",
            (1, 66, 1): "stone", (1, 67, 1): "stone", (1, 68, 1): "stone",
            (-1, 66, 0): "stone", (-1, 67, 0): "stone", (-1, 68, 0): "stone",
            (-1, 66, 1): "stone", (-1, 67, 1): "stone", (-1, 68, 1): "stone",
            (0, 66, -1): "stone", (0, 67, -1): "stone", (0, 68, -1): "stone",
            (0, 66, 2): "stone", (0, 67, 2): "stone", (0, 68, 2): "stone",
        }
        failures = {(x + 0.5, 73, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 67.0, 0.5]
        world.items["dirt"] = 8
        RECENT_RECOVERY_LANDINGS.extend([(0.5, 67.0, 0.5), (0.5, 67.0, 1.5)])
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertGreaterEqual(state.pos[1], 68.0)
        self.assertIn("place", [s["op"] for s in steps])
        self.assertEqual(list(RECENT_RECOVERY_LANDINGS), [])

    def test_underground_overhead_fake_move_success_falls_back_to_staircase_dig(self):
        # 复现官方服 Y=52 洞穴假成功死循环：move_to 正上方 grass_block 返回 ok=true 但原地未动，
        # 必须拒绝假成功并切入 1x2 阶梯上挖脱困。
        class FakeOverheadMoveWorld(ScoutFailureWorld):
            def move_to(self, *args, **kwargs):
                dest = list(kwargs["pos"])
                # 模拟官方服务端：目标与当前同 X/Z 且高出 10+ 格时直接 ok=true 但不移动
                if abs(dest[0] - self.st["pos"][0]) <= 2.0 and abs(dest[2] - self.st["pos"][2]) <= 2.0 and dest[1] > self.st["pos"][1] + 2.0:
                    self.calls.append(("move_to", {"pos": dest}))
                    return {"pos": list(self.st["pos"])}
                return super().move_to(*args, **kwargs)

        blocks = {
            (0, 62, 0): "grass_block",  # 正上方地表
            (0, 51, 0): "stone",
            (1, 51, 0): "stone", (1, 52, 0): "stone", (1, 53, 0): "stone", (1, 54, 0): "stone",
            (-1, 51, 0): "stone", (-1, 52, 0): "stone", (-1, 53, 0): "stone",
            (0, 51, 1): "stone", (0, 52, 1): "stone", (0, 53, 1): "stone",
            (0, 51, -1): "stone", (0, 52, -1): "stone", (0, 53, -1): "stone",
        }
        world = FakeOverheadMoveWorld(blocks=blocks)
        world.st["pos"] = [0.5, 52.0, 0.5]
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertIn("dig", [s["op"] for s in steps])
        self.assertGreater(state.pos[1], 52.5)

    def test_survey_island_spiral_raises_replan_when_all_sectors_blocked_without_progress(self):
        # 复现地下封死时 survey_island 外扩全扇区 path_not_found 却标 completed 导致乒乓死循环
        from mcagent.skills.world_memo import memo_for

        class BlockedSurveyWorld(ScoutFailureWorld):
            def move_to(self, *args, **kwargs):
                self.calls.append(("move_to", {"pos": list(kwargs.get("pos") or [0, 52, 0])}))
                raise ApiError("path_not_found", "boxed in underground")

        blocks = {(x, 51, z): "stone" for x in range(-6, 7) for z in range(-6, 7)}
        world = BlockedSurveyWorld(blocks=blocks)
        world.st["pos"] = [0.5, 52.0, 0.5]
        world.st["dimension"] = "overworld"
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()
        memo = memo_for(state)
        for idx in range(2):
            memo.mark_sector_done(idx, dimension="overworld")
        with self.assertRaisesRegex(Replan, "全部移动受阻"):
            drive(make("survey_island", {"dimension": "overworld", "sectors": 2, "radius": 32}), ex, state)

    def test_deep_pit_without_pickaxe_climbs_multi_layer_alternating_step_up_to_rim(self):
        # 3 格深石坑（坑底站位 Y=60，坑壁 Y=60..62，坑沿站位 Y=63），无镐但有圆石：
        # 一次 scout_island 内应优先向上用两列交替 step_up_1x2 连续搭块并直接跨上坑沿，而非先水平乱走或只垫 1 格。
        blocks = {
            (10, 62, 0): "grass_block",
            (14, 62, 0): "grass_block",
            (18, 62, 0): "grass_block",
            # 1x2 坑底 (0, 59, 0) 与 (1, 59, 0)
            (0, 59, 0): "stone",
            (1, 59, 0): "stone",
        }
        # 四周 3 格高石墙（Y=60..62），坑沿顶面站位 Y=63
        for y in (59, 60, 61, 62):
            for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (0, 1), (1, 1)):
                blocks[(x, y, z)] = "stone"
        failures = {(x + 0.5, 63, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 60.0, 0.5]
        world.items["cobblestone"] = 12
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        place_steps = [s for s in steps if s["op"] == "place"]
        self.assertGreaterEqual(len(place_steps), 3)
        self.assertGreaterEqual(state.pos[1], 63.0)

    def test_two_block_shallow_pit_detected_and_climbed_out(self):
        # 2 格深凹坑（坑底站位 Y=62，地表草块 Y=63 即站位 Y=64，差值刚好 2 格不可跳出）：
        # 必须按候选站位 y+1 > cur.y + 1.5 触发搭路爬升（修复旧版用方块 y 的差 1 漏判）。
        blocks = {
            (10, 63, 0): "grass_block",
            (14, 63, 0): "grass_block",
            (18, 63, 0): "grass_block",
            (0, 61, 0): "stone",
            (1, 61, 0): "stone",
        }
        for y in (61, 62, 63):
            for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (0, 1), (1, 1)):
                blocks[(x, y, z)] = "stone"
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 62.0, 0.5]
        world.items["dirt"] = 4
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertIn("place", [s["op"] for s in steps])
        self.assertGreaterEqual(state.pos[1], 64.0)

    def test_wide_pit_approaches_wall_before_pillaring_out_to_rim(self):
        # 较宽石坑（坑壁距起点 3 格，在 x=3 处，Y=60..62）：搭柱前先贴近坑壁，再交替垫高并跨上坑沿 Y=63。
        blocks = {
            (12, 62, 0): "grass_block",
            (16, 62, 0): "grass_block",
            (20, 62, 0): "grass_block",
        }
        # 坑底 x in [-2..2], z in [-1..1]
        for x in range(-2, 3):
            for z in range(-1, 2):
                blocks[(x, 59, z)] = "stone"
        # 东侧坑壁在 x=3，Y=60..62（顶面可站立于 Y=63）
        for y in (59, 60, 61, 62):
            for z in range(-1, 2):
                blocks[(3, y, z)] = "stone"
        failures = {(x + 0.5, 63, 0.5): "path_not_found" for x in (12, 16, 20)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 60.0, 0.5]
        world.items["cobblestone"] = 12
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertIn("place", [s["op"] for s in steps])
        self.assertGreaterEqual(state.pos[1], 63.0)
        self.assertGreaterEqual(state.pos[0], 3.0)

    def test_pit_escape_harvests_adjacent_soft_dirt_when_inventory_empty(self):
        # 2 格深石壁坑且背包全空无镐，但坑底旁边有一块安全泥土（下方有石托底）：
        # 先自动挖取泥土进包，再用 step_up_1x2 垫块爬上坑沿。
        blocks = {
            (10, 63, 0): "grass_block",
            (14, 63, 0): "grass_block",
            (18, 63, 0): "grass_block",
            (0, 61, 0): "stone",
            (1, 61, 0): "stone",
            (0, 61, 1): "dirt",
            (0, 60, 1): "stone",  # 泥土下方实心托底
        }
        for y in (61, 62, 63):
            for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (1, 1), (-1, 1), (0, 2)):
                blocks[(x, y, z)] = "stone"
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 62.0, 0.5]
        world.items.clear()
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        ops = [s["op"] for s in steps]
        self.assertIn("dig", ops)
        self.assertIn("place", ops)
        self.assertGreaterEqual(state.pos[1], 64.0)

    def test_official_goal_near_rng_does_not_cause_step_up_fake_success(self):
        # 复现官方服实测 264.9s->265.9s 缺陷：GoalNear(rng=1) 在水平距 0.8 <= rng 时原地返 ok=true 导致搭块后跳不上去；
        # 且地下洞穴 cands 仅含同层不可达石块（high_stand_ys 为空、attempts=1）时仍须立即触发多层搭路爬升而非水平来回走。
        import math

        class OfficialGoalNearWorld(ScoutFailureWorld):
            def move_to(self, *args, **kwargs):
                dest = list(kwargs["pos"])
                rng = float(kwargs.get("rng", 1))
                cur = list(self.st["pos"])
                if tuple(dest) not in self.failures and math.hypot(dest[0] - cur[0], dest[2] - cur[2]) <= rng:
                    self.calls.append(("move_to", {"pos": dest, "rng": rng}))
                    return {"pos": cur}
                return super().move_to(*args, **kwargs)

        blocks = {
            (6, 52, 0): "stone",  # 仅同层不可达石块候选（standing Y=53，high_stand_ys 为空）
            (0, 52, 0): "stone",
            (1, 52, 0): "stone",
        }
        for y in (52, 53, 54, 55):
            for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (0, 1), (1, 1)):
                blocks[(x, y, z)] = "stone"
        world = OfficialGoalNearWorld(blocks=blocks, failures={(6.5, 53, 0.5): "path_not_found"})
        world.st["pos"] = [0.7, 53.0, 0.5]  # 距 [1.5, 54, 0.5] 水平仅 0.8 格
        world.items["cobblestone"] = 12
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertGreaterEqual(len([s for s in steps if s["op"] == "place"]), 3)
        self.assertGreaterEqual(state.pos[1], 56.0)

    def test_existing_one_block_ledge_does_not_stop_multi_layer_step_up(self):
        # 复现两格来回打转场景：旁边 (1, 53, 0) 已有 1 块垫脚石，Phase 1 未挖方块仅走上 Y=54 后，
        # 不得提前终止而跳过 Phase 2，必须继续两列交替 step_up_1x2 往上搭到坑沿 Y=57。
        blocks = {
            (6, 52, 0): "stone",
            (0, 52, 0): "stone",
            (1, 52, 0): "stone",
            (1, 53, 0): "cobblestone",  # 上一轮留下的 1 块垫脚石
        }
        for y in (52, 53, 54, 55, 56):
            for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (0, 1), (1, 1)):
                blocks[(x, y, z)] = "stone"
        world = ScoutFailureWorld(blocks=blocks, failures={(6.5, 53, 0.5): "path_not_found"})
        world.st["pos"] = [0.5, 53.0, 0.5]
        world.items["cobblestone"] = 12
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertIn("place", [s["op"] for s in steps])
        self.assertGreaterEqual(state.pos[1], 57.0)

    def test_sand_gravel_roof_pit_falls_back_and_climbs_out(self):
        # 沙滩/砂砾坑：西侧顶为 water（严格禁挖），其余三面顶为 sand/gravel（降级备选可挖），须成功向上挖出且绝不碰水侧。
        blocks = {
            (10, 63, 0): "grass_block",
            (14, 63, 0): "grass_block",
            (18, 63, 0): "grass_block",
            (0, 59, 0): "sandstone",
            # 西侧 (-1) 顶上有水，必须避开
            (-1, 60, 0): "sand", (-1, 61, 0): "sand", (-1, 62, 0): "sand", (-1, 63, 0): "water",
            # 东/南/北三面顶上均为 sand/gravel
            (1, 60, 0): "sand", (1, 61, 0): "sand", (1, 62, 0): "sand", (1, 63, 0): "sand",
            (0, 60, 1): "gravel", (0, 61, 1): "gravel", (0, 62, 1): "gravel", (0, 63, 1): "gravel",
            (0, 60, -1): "sand", (0, 61, -1): "sand", (0, 62, -1): "sand", (0, 63, -1): "sand",
        }
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 60.0, 0.5]
        world.items.clear()
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertGreaterEqual(state.pos[1], 61.0)
        dug_positions = [s["pos"] for s in steps if s["op"] == "dig"]
        self.assertTrue(dug_positions)
        self.assertTrue(all(p[0] != -1 for p in dug_positions))

    def test_protected_zone_pit_walks_out_of_protection_then_climbs(self):
        # 角色位于已知保护区 (0, 0) 半径 6 格内，东侧通道延伸到保护区外（x=7）后遇到坑壁（x=8）：
        # 须先移出保护区边界再开阶梯向上脱困。
        from mcagent.loop.starter_skills import PROTECTED_ZONES

        blocks = {
            (16, 63, 0): "grass_block",
            (20, 63, 0): "grass_block",
            (24, 63, 0): "grass_block",
        }
        for x in range(0, 8):
            blocks[(x, 59, 0)] = "stone"
        # 保护区外 x=8 处有可挖坑壁（Y=60..62）
        for y in (59, 60, 61, 62):
            blocks[(8, y, 0)] = "dirt"
        failures = {(x + 0.5, 64, 0.5): "path_not_found" for x in (16, 20, 24)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 60.0, 0.5]
        PROTECTED_ZONES.append((0.0, 0.0))
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        self.assertGreater(state.pos[0], 6.0)
        self.assertGreaterEqual(state.pos[1], 61.0)
        self.assertIn("dig", [s["op"] for s in steps])

    def test_pickaxe_harvests_adjacent_stone_for_step_up_when_no_placeables(self):
        # 背包有石镐但 0 垫块，四周坑壁顶上有水无法开阶梯，坑底旁边有安全托底石头：
        # 须装备石镐挖取石头获得圆石进包，再用 step_up_1x2 垫块爬上坑沿。
        blocks = {
            (10, 61, 0): "grass_block",
            (14, 61, 0): "grass_block",
            (18, 61, 0): "grass_block",
            (0, 59, 0): "stone",
            (1, 59, 0): "stone",
            (0, 59, 1): "stone",
            (0, 58, 1): "stone",  # 托底石块
        }
        # 2 格深石壁（Y=60, 61），在所有墙顶上方 Y=63 放水阻止 Phase 1 挖阶梯，迫使走 Phase 2 挖石取材 + 搭块爬升
        for x, z in ((-1, 0), (2, 0), (0, -1), (1, -1), (1, 1), (-1, 1), (0, 2)):
            blocks[(x, 59, z)] = "stone"
            blocks[(x, 60, z)] = "stone"
            blocks[(x, 61, z)] = "stone"
            blocks[(x, 63, z)] = "water"
        blocks[(0, 58, -1)] = "stone"
        failures = {(x + 0.5, 62, 0.5): "path_not_found" for x in (10, 14, 18)}
        world = ScoutFailureWorld(blocks=blocks, failures=failures)
        world.st["pos"] = [0.5, 60.0, 0.5]
        world.items.clear()
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()
        steps = drive(make("scout_island"), ex, state)
        ops = [s["op"] for s in steps]
        self.assertIn("equip", ops)
        self.assertIn("dig", ops)
        self.assertIn("place", ops)
        self.assertGreaterEqual(state.pos[1], 62.0)

    def test_underground_staircase_digs_continuously_up_to_surface_without_stopping_at_two(self):
        # 地下 Y=54 持石镐（目标地表 Y=61）：一次 scout_island 内应连续挖阶梯直抵地表附近（>=6 级），
        # 不再每挖 2 级就退出重规划，且阶梯开挖使用 collect=False 避免每块额外触发捡掉落物寻路。
        dig_collect_flags = []

        class TrackDigWorld(ScoutFailureWorld):
            def dig(self, pos, collect=True, *, stop_on=None):
                dig_collect_flags.append(bool(collect))
                return super().dig(pos, collect=collect, stop_on=stop_on)

        blocks = {
            (18, 60, 0): "grass_block",
            (22, 60, 0): "grass_block",
        }
        for x in range(-2, 10):
            for y in range(53, 61):
                for z in range(-2, 3):
                    blocks[(x, y, z)] = "stone"
        del blocks[(0, 54, 0)]
        del blocks[(0, 55, 0)]
        world = TrackDigWorld(
            blocks=blocks,
            failures={(18.5, 61, 0.5): "path_not_found", (22.5, 61, 0.5): "path_not_found"},
        )
        world.st["pos"] = [0.5, 54.0, 0.5]
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()
        drive(make("scout_island"), ex, state, max_steps=60)
        self.assertGreaterEqual(state.pos[1], 60.0)
        self.assertTrue(dig_collect_flags)
        self.assertTrue(all(flag is False for flag in dig_collect_flags))

    def test_mine_return_climb_out_stairs_uses_batch_prefetch_and_tight_rng(self):
        # 验证 mine_return._climb_out_stairs 启用并发批查 get_blocks_batch 且用 rng=0.4 避免官方服 GoalNear(rng=1) 假成功
        import math
        from mcagent.skills.mine_return import _climb_out_stairs

        class BatchOfficialClimbWorld(ScoutFailureWorld):
            def __init__(self, **kw):
                super().__init__(**kw)
                self.batch_calls = 0

            def get_blocks_batch(self, positions, max_workers=8, use_cache=True):
                self.batch_calls += 1
                return {tuple(p): self.get_block(p) for p in positions}

            def move_to(self, *args, **kwargs):
                dest = list(kwargs["pos"])
                rng = float(kwargs.get("rng", 1))
                cur = list(self.st["pos"])
                if math.hypot(dest[0] - cur[0], dest[2] - cur[2]) <= rng:
                    return {"pos": cur}
                return super().move_to(*args, **kwargs)

        blocks = {}
        for x in range(-2, 8):
            for y in range(52, 60):
                for z in range(-2, 3):
                    blocks[(x, y, z)] = "stone"
        del blocks[(0, 54, 0)]
        del blocks[(0, 55, 0)]
        world = BatchOfficialClimbWorld(blocks=blocks)
        world.st["pos"] = [0.7, 54.0, 0.5]  # 水平距 [1.5, 55, 0.5] 仅 0.8 <= rng=1
        world.items["stone_pickaxe"] = 1
        ex, state = world.session()
        drive(_climb_out_stairs(ex, state, target_y=60.0, max_steps=6), ex, state, max_steps=40)
        self.assertGreater(world.batch_calls, 0)
        self.assertGreaterEqual(state.pos[1], 58.0)


if __name__ == "__main__":
    unittest.main()
