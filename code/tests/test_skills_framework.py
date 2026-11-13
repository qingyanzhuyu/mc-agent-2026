"""技能框架离线单测（verified: offline）：声明契约、一步一动作、L2 原语控制流、技能菜单与选择校验。

这里验的是**机制**，不是 Minecraft 物理：`GridWorld` 没有重力、没有挖掘时间、没有寻路失败。
原语的真机证据在 `probe --pathfinding --manual-only`（它导入的就是 `skills/nav.py` 同一份代码）。
"""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

from mcagent import probe_path
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient
from mcagent.runtime import Replan, State
from mcagent.skills import REGISTRY, Param, Skill, bind_args, contract_errors, load_all, make, nav, rec, register
from mcagent.skills import base as skill_base
from mcagent.skills.items import best_pickaxe
from mcagent.skills.testing import GridWorld, drive


class DigColumn(Skill):
    """测试用技能：挖面前一列的若干格。"""

    name = "dig_column"
    description = "挖面前一列方块"
    params = {
        "block": Param(str, "要什么", choices=("stone", "dirt")),
        "count": Param(int, "几格", default=2, min=1, max=3),
    }
    goal = "acq:{block}"
    verified = "live"
    closed = False

    @classmethod
    def blocked(cls, state) -> str:
        return "" if best_pickaxe(state) else "背包没有镐"

    def can_start(self, state) -> bool:
        return not self.blocked(state) and not (self.args["block"] == "dirt" and self.args["count"] == 3)

    def run(self, ex, state):
        try:
            for y in range(64, 64 + self.args["count"]):
                yield rec("dig", ex.dig([1, y, 0]), pos=[1, y, 0])
        finally:
            type(self).closed = True


def reply(content: str) -> tuple[int, str]:
    return 200, json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}]})


class ParamTests(unittest.TestCase):
    def test_bind_fills_defaults_and_reports_every_problem(self):
        bound, errs = bind_args(DigColumn, {"block": "stone"})
        self.assertEqual((bound, errs), ({"block": "stone", "count": 2}, []))
        _, errs = bind_args(DigColumn, {"block": "gold", "count": 9, "pos": [1, 2, 3]})
        self.assertEqual(len(errs), 3)
        self.assertTrue(any("不认识这些参数" in e and "pos" in e for e in errs))
        _, errs = bind_args(DigColumn, {})
        self.assertTrue(any("缺少必填参数: block" in e for e in errs))
        self.assertEqual(bind_args(DigColumn, [1])[1], ["args 必须是 JSON object"])

    def test_bool_and_nan_are_not_numbers(self):
        self.assertTrue(Param(int).check("n", True))
        self.assertTrue(Param(float).check("n", float("nan")))
        self.assertTrue(Param(str).check("s", "  "))
        self.assertEqual(Param(float, min=0, max=1).check("n", 1), [])

    def test_instance_binds_args_and_templated_goal(self):
        skill = DigColumn(block="dirt", count=1)
        self.assertEqual((skill.args, skill.goal), ({"block": "dirt", "count": 1}, "acq:dirt"))
        with self.assertRaises(ValueError):
            DigColumn(block="stone", count=0)


class ContractTests(unittest.TestCase):
    def variant(self, **attrs) -> type:
        return type("Variant", (DigColumn,), attrs)

    def test_valid_declaration_passes(self):
        self.assertEqual(contract_errors(DigColumn), [])

    def test_coordinates_and_entity_ids_cannot_be_parameters(self):
        for name in ("pos", "x", "entity_id"):
            errs = contract_errors(self.variant(params={name: Param(int, default=0)}, goal="g"))
            self.assertTrue(any("J17" in e for e in errs), name)
        errs = contract_errors(self.variant(params={"where": Param(list, default=None)}, goal="g"))
        self.assertTrue(any("只能是 str/int/float/bool" in e for e in errs))

    def test_bad_declarations_are_rejected(self):
        def plain_run(self, ex, state):
            return None

        cases = {
            "name": self.variant(name="DigColumn"),
            "description": self.variant(description=""),
            "goal": self.variant(goal="acq:{missing}"),
            "verified": self.variant(verified="trust-me"),
            "run": self.variant(run=plain_run),
            "自相矛盾": self.variant(params={"count": Param(int, default=9, max=3)}, goal="g"),
            "Param": self.variant(params={"count": int}, goal="g"),
        }
        for key, cls in cases.items():
            self.assertTrue(any(key in e for e in contract_errors(cls)), key)
        self.assertEqual(contract_errors(object), ["必须继承 skills.base.Skill"])

    def test_register_rejects_bad_and_duplicate_and_make_builds(self):
        with patch.dict(REGISTRY, clear=True):
            self.assertIs(register(DigColumn), DigColumn)
            self.assertIs(register(DigColumn), DigColumn)  # 同一个类重复注册（模块重载）不算冲突
            with self.assertRaises(ValueError):
                register(self.variant())
            with self.assertRaises(TypeError):
                register(self.variant(name="other_skill", description=""))
            self.assertEqual(make("dig_column", {"block": "stone"}).args["count"], 2)
            with self.assertRaises(ValueError):
                make("teleport")
        self.assertNotIn("dig_column", REGISTRY)

    def test_discovery_skips_infrastructure_modules(self):
        loaded = load_all()
        self.assertIn("nav", loaded)
        self.assertFalse({"base", "registry", "testing"} & set(loaded))


class StepTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        DigColumn.closed = False
        self.world = GridWorld(blocks={(1, 64, 0): "stone", (1, 65, 0): "stone"}, items={"wooden_pickaxe": 1})
        self.ex, self.state = self.world.session()

    def test_step_fires_one_action_then_reports_done(self):
        skill = DigColumn(block="stone")
        self.assertFalse(skill.step(self.ex, self.state))
        self.assertEqual((self.ex.acts, skill.last["pos"]), (["dig"], [1, 64, 0]))
        self.assertFalse(skill.step(self.ex, self.state))
        self.assertTrue(skill.step(self.ex, self.state))
        self.assertEqual(self.ex.acts, ["dig", "dig"])
        self.assertTrue(DigColumn.closed)

    def test_abort_runs_cleanup_and_next_start_is_fresh(self):
        skill = DigColumn(block="stone")
        skill.step(self.ex, self.state)
        skill.abort()
        self.assertTrue(DigColumn.closed)
        skill.abort()  # 幂等
        self.assertFalse(skill.step(self.ex, self.state))
        self.assertEqual(skill.last["pos"], [1, 64, 0])  # 重新从头观察，不续跑

    def test_failure_ends_the_flow_and_cooldown_is_per_goal(self):
        self.world.dig = lambda *a, **k: (_ for _ in ()).throw(Replan("挖不动"))
        skill = DigColumn(block="stone")
        with self.assertRaises(Replan):
            skill.step(self.ex, self.state)
        self.assertTrue(DigColumn.closed)
        skill.mark_failed()
        self.assertTrue(DigColumn(block="stone").is_cooling())
        self.assertFalse(DigColumn(block="dirt").is_cooling())

    def test_drive_rejects_two_actions_in_one_step(self):
        def greedy(ex):
            ex.dig([1, 64, 0])
            ex.dig([1, 65, 0])
            yield rec("dig")

        with self.assertRaisesRegex(AssertionError, "2 个阻塞动作"):
            drive(greedy(self.ex), self.ex)
        with self.assertRaisesRegex(AssertionError, "空转"):
            drive(iter(lambda: rec("noop"), None), self.ex, max_steps=5)


class NavPrimitiveTests(unittest.TestCase):
    def test_probe_runs_the_same_primitives(self):
        self.assertIs(probe_path.nav, nav)

    def test_breach_equips_best_pickaxe_then_digs_only_solid_blocks(self):
        world = GridWorld(blocks={(1, 65, 0): "stone", (1, 64, 0): "stone"},
                          items={"wooden_pickaxe": 1, "stone_pickaxe": 1})
        ex, state = world.session()
        steps = drive(nav.breach_1x2(ex, state, [[1, 65, 0], [1, 64, 0], [1, 66, 0]]), ex)
        self.assertEqual([s["op"] for s in steps], ["equip", "dig", "dig"])
        self.assertEqual(steps[0]["item"], "stone_pickaxe")
        self.assertEqual(world.blocks, {})

    def test_breach_without_pickaxe_still_digs_by_hand(self):
        world = GridWorld(blocks={(1, 64, 0): "dirt"})
        ex, state = world.session()
        self.assertEqual([s["op"] for s in drive(nav.breach_1x2(ex, state, [[1, 64, 0]]), ex)], ["dig"])

    def test_bridge_sneaks_places_walks_out_when_far_and_releases(self):
        world = GridWorld(items={"dirt": 3, "cobblestone": 8})
        ex, state = world.session()
        blocks = [[x, 63, 0] for x in range(1, 6)]
        steps = drive(nav.sneak_bridge(ex, state, blocks), ex)
        self.assertEqual([s["op"] for s in steps],
                         ["posture", "place", "place", "place", "move_to_bridge", "place", "place", "posture"])
        self.assertEqual(steps[4]["pos"], [3.5, 64, 0.5])
        self.assertTrue(all(world.blocks[tuple(b)] == "cobblestone" for b in blocks))  # 用背包里最多的那种
        self.assertFalse(world.st["sneaking"])

    def test_bridge_releases_sneak_when_preempted_or_out_of_blocks(self):
        world = GridWorld(items={"dirt": 1})
        ex, state = world.session()
        flow = nav.sneak_bridge(ex, state, [[1, 63, 0], [2, 63, 0]])
        next(flow)
        self.assertTrue(world.st["sneaking"])
        flow.close()  # 相当于循环切走技能时的 abort()
        self.assertFalse(world.st["sneaking"])

        with self.assertRaises(Replan):
            drive(nav.sneak_bridge(ex, state, [[1, 63, 0], [2, 63, 0]]), ex)
        self.assertFalse(world.st["sneaking"])
        self.assertEqual(world.blocks, {(1, 63, 0): "dirt"})

    def test_step_up_alternates_columns_instead_of_placing_into_itself(self):
        world = GridWorld(items={"dirt": 5})
        ex, state = world.session()
        steps = drive(nav.step_up_1x2(ex, state, [[1, 64, 0], [0, 64, 0], [0, 65, 0]], "dirt"), ex)
        self.assertEqual([s["op"] for s in steps], ["place", "step_up_to", "place", "place"])
        self.assertEqual(steps[1]["pos"], [1.5, 65, 0.5])
        self.assertEqual(world.items["dirt"], 2)
        # 已有方块的格直接跳过，重做一遍不再消耗材料
        ex2, state2 = world.session()
        again = drive(nav.step_up_1x2(ex2, state2, [[1, 64, 0], [0, 64, 0], [0, 65, 0]], "dirt"), ex2)
        self.assertEqual([s["op"] for s in again], [])
        self.assertEqual(world.items["dirt"], 2)


class SkillChoiceTests(unittest.TestCase):
    def setUp(self):
        skill_base._COOLDOWN.clear()
        self.registry = {"dig_column": DigColumn}
        self.world = GridWorld(items={"wooden_pickaxe": 1})
        _, self.state = self.world.session()

    def test_menu_lists_signature_and_blocked_reason(self):
        text = skill_choice.menu(self.state, self.registry)
        self.assertIn("- dig_column(block: stone|dirt, count: int 1..3 = 2) — 挖面前一列方块", text)
        self.assertNotIn("暂不可启动", text)
        text = skill_choice.menu(State(), self.registry)
        self.assertIn("- （无）", text)
        self.assertIn("- dig_column：背包没有镐", text)

    def test_unverified_skills_stay_off_the_menu_by_default(self):
        draft = type("Draft", (DigColumn,), {"name": "draft_skill", "verified": "offline"})
        registry = {**self.registry, "draft_skill": draft}
        self.assertNotIn("draft_skill", skill_choice.menu(self.state, registry))
        self.assertIn("draft_skill", skill_choice.menu(self.state, registry, min_verified="offline"))
        ok, errs = skill_choice.validate_choice({"skill": "draft_skill", "args": {"block": "stone"}},
                                                self.state, registry)
        self.assertFalse(ok)
        self.assertIn("skill 必须是以下之一: dig_column", errs[0])

    def test_validate_feeds_back_actionable_errors(self):
        check = lambda obj, state=self.state: skill_choice.validate_choice(obj, state, self.registry)
        self.assertEqual(check({"skill": "dig_column", "args": {"block": "stone"}, "why": "要石头"}), (True, []))
        self.assertIn("JSON object", check("dig")[1][0])
        self.assertIn("收到 'fly'", check({"skill": "fly"})[1][0])
        errs = check({"skill": "dig_column", "args": {"block": "stone", "pos": [1, 64, 0]}, "why": 3})[1]
        self.assertTrue(any("why 必须是字符串" in e for e in errs))
        self.assertTrue(any("不认识这些参数: ['pos']" in e for e in errs))
        self.assertIn("背包没有镐", check({"skill": "dig_column", "args": {"block": "stone"}}, State())[1][0])
        self.assertIn("不可启动", check({"skill": "dig_column", "args": {"block": "dirt", "count": 3}})[1][0])

    def test_cooling_goal_is_refused_until_it_expires(self):
        choice = {"skill": "dig_column", "args": {"block": "stone"}}
        skill = skill_choice.from_choice(choice, self.state, self.registry)
        self.assertEqual((skill.name, skill.goal), ("dig_column", "acq:stone"))
        skill.mark_failed()
        with self.assertRaisesRegex(ValueError, "冷却"):
            skill_choice.from_choice(choice, self.state, self.registry)
        skill_choice.from_choice({"skill": "dig_column", "args": {"block": "dirt"}}, self.state, self.registry)

    def test_choose_reasks_with_errors_then_returns_bound_skill(self):
        seen: list[dict] = []
        replies = [reply('{"skill": "mine", "args": {"pos": [1, 2, 3]}}'),
                   reply('{"skill": "dig_column", "args": {"block": "dirt", "count": 1}, "why": "缺泥土"}')]

        def poster(url, headers, data, timeout, proxy):
            seen.append(json.loads(data))
            return replies[len(seen) - 1]

        llm = LLMClient(key="k", poster=poster)
        skill, errs, used = skill_choice.choose(llm, "hp=20 food=18", self.state, registry=self.registry,
                                                recent_failures="mine_ore: 半径 32 内没有裸露的铁矿")
        self.assertEqual((skill.args, errs, used), ({"block": "dirt", "count": 1}, [], 2))
        first = seen[0]["messages"][1]["content"]
        self.assertIn("dig_column(block: stone|dirt", first)
        self.assertIn("最近失败", first)
        self.assertIn("上次输出被校验拒绝", seen[1]["messages"][-1]["content"])

    def test_choose_returns_none_when_model_never_complies(self):
        llm = LLMClient(key="k", poster=lambda *a: reply("我觉得应该去挖矿"))
        skill, errs, used = skill_choice.choose(llm, "hp=20", self.state, registry=self.registry)
        self.assertIsNone(skill)
        self.assertEqual((errs, used), (["回复里找不到合法 JSON 对象"], 2))


if __name__ == "__main__":
    unittest.main()
