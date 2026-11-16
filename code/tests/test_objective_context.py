"""战略决策上下文（loop/context.py）组装、禁透传核验、可选字段接口与长度预算单测（verified: offline）。

覆盖：
1. 必须具备的六类信息完整存在（可选未达成目标含 id/标题/分值/预计剩余成本/前置是否满足、
   被挂起目标与唤醒条件、背包摘要、剩余比赛时间与昼夜、最近 5 个目标结果与失败分类、当前 Objective 及已用时间）；
2. 严禁出现 `current_goal_hint()`、写死科技顺序、任何坐标三元组/`pos=`、`entity_id`；
3. 可选扩展接口 `subgoal_predicates` 与 `reservations` 可在不依赖 `subgoals.py`/`reserve.py` 存在时独立工作；
4. 打印并断言一份典型中盘上下文字符数严格 <= 2000。
"""
from __future__ import annotations

import re
import unittest

from mcagent.loop import GOALS, CampaignPlanner, reset_world_memo
from mcagent.loop.context import ObjectiveRecord, build_decision_context
from mcagent.planner.objective import OBJECTIVE_SYSTEM_PROMPT, Objective
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import base as skill_base
from mcagent.skills import make


class ObjectiveContextTests(unittest.TestCase):
    def setUp(self) -> None:
        skill_base._COOLDOWN.clear()
        reset_world_memo()

    def test_typical_context_has_all_required_fields_and_stays_under_2000_chars(self) -> None:
        state = State()
        state.set_status({
            "ready": True,
            "alive": True,
            "health": 17,
            "food": 16,
            "dimension": "overworld",
            "is_day": False,
            "time_of_day": 15000,
            "pos": [148.5, 63.0, -292.25],
            "nearest_hostile": {"id": 91, "entity_id": 91, "name": "skeleton", "pos": [151, 63, -290], "dist": 4.1},
        })
        state.set_inventory({
            "items": [
                {"name": "oak_log", "count": 2, "slot": 0},
                {"name": "birch_planks", "count": 6, "slot": 1},
                {"name": "stick", "count": 4, "slot": 2},
                {"name": "wooden_pickaxe", "count": 1, "slot": 3},
                {"name": "cobblestone", "count": 5, "slot": 4},
                {"name": "bread", "count": 3, "slot": 5},
                {"name": "coal", "count": 2, "slot": 6},
            ]
        })

        planner = CampaignPlanner(mode="campaign", strategy="llm", min_verified="offline", max_seconds=28800.0)
        # 标记 smelt_iron 被环境挂起
        planner.record_environment_block(
            make("smelt_iron", {"count": 3}),
            EnvBlocked(
                "半径 64 内无裸露铁矿 pos=[148, 63, -292]",
                missing_predicate="exposed_iron",
                searched_volume="半径 64",
                wake_on="chunk_moved",
            ),
            state,
        )
        # 填充 6 条历史目标记录，验证只保留最近 5 条
        for idx, (gid, outc, fkind, dt, detail) in enumerate([
            ("old_goal_0", "achieved", "none", 22.0, ""),
            ("prep_wood", "achieved", "none", 19.0, "完成起步砍树"),
            ("prep_wooden_pickaxe", "achieved", "none", 14.0, ""),
            ("mine_stone", "achieved", "none", 28.0, ""),
            ("smelt_iron", "env_blocked", "env_blocked", 95.0, "死洞不可达 [140, 52, -280] entity_id=91"),
            ("upgrade_tools", "budget_exceeded", "timeout", 150.0, "工作台被保护区阻挡 pos=[148,63,-292]"),
        ]):
            planner.recent_objectives.append(
                ObjectiveRecord(goal_id=gid, outcome=outc, failure_kind=fkind, elapsed_s=dt, detail=detail)
            )

        cur_obj = Objective(
            goal_id="upgrade_tools",
            budget_s=150.0,
            why="优先补齐石镐解锁铁器链",
            source="llm",
            started_at=100.0,
        )
        ctx = planner.build_context(
            state,
            current_objective=cur_obj,
            current_objective_elapsed_s=42.0,
            now=planner._started_mono + 600.0,
        )
        rendered = ctx.render()
        print(f"\n[P9 典型决策上下文字符数: {len(rendered)} / 2000]\n{rendered}\n")

        self.assertLessEqual(len(rendered), 2000)
        # 1) 有资格且未达成目标（id、标题、分值、预计剩余成本、前置是否满足）
        self.assertIn("可选目标", rendered)
        self.assertIn("upgrade_tools: 获得升级（合成石镐）", rendered)
        self.assertIn("5分", rendered)
        self.assertIn("预计剩余", rendered)
        self.assertIn("前置=", rendered)
        # 已达成的 prep_wood / prep_wooden_pickaxe / mine_stone 不再混入未达成菜单
        menu_part = ctx.render_menu()
        self.assertNotIn("- prep_wood:", menu_part)
        self.assertNotIn("- mine_stone:", menu_part)
        # 2) 被挂起目标及唤醒条件
        self.assertIn("被挂起目标", rendered)
        self.assertIn("smelt_iron", rendered)
        self.assertIn("唤醒=chunk_moved", rendered)
        # 3) 背包摘要
        self.assertIn("背包明细：", rendered)
        self.assertIn("cobblestonex5", rendered)
        self.assertIn("wooden_pickaxex1", rendered)
        # 4) 剩余比赛时间和昼夜
        self.assertIn("剩余比赛时间=28200s", rendered)
        self.assertIn("昼夜=夜晚", rendered)
        # 5) 最近 5 个目标结果与失败分类（第 0 条被滑窗裁掉）
        self.assertNotIn("old_goal_0", rendered)
        self.assertEqual(len(ctx.recent_objectives), 5)
        self.assertIn("失败分类=env_blocked", rendered)
        self.assertIn("失败分类=timeout", rendered)
        # 6) 当前 Objective 及已用时间
        self.assertIn("当前Objective：upgrade_tools（来源=llm, 已用=42s/150s", rendered)

    def test_context_and_prompt_forbid_goal_hint_tech_order_coords_and_entity_id(self) -> None:
        state = State()
        state.set_status({
            "ready": True,
            "alive": True,
            "health": 20,
            "food": 20,
            "pos": [-42.7, 68.0, 315.9],
            "nearest_hostile": {"id": 555, "entity_id": 555, "name": "creeper", "pos": [-40, 68, 312], "dist": 5.0},
        })
        state.set_inventory({"items": []})
        planner = CampaignPlanner(mode="campaign", strategy="llm", min_verified="offline")
        hint = planner.rules.current_goal_hint(state)
        self.assertTrue(hint)  # 规则侧本身有 hint，但严禁透传进战略上下文

        ctx = build_decision_context(
            state,
            ledger=GOALS,
            current_objective=Objective("mine_stone", 120.0, "避开 [10, 64, -20] entity_id=555", "llm", 0.0),
            current_objective_elapsed_s=15.0,
            recent_objectives=[
                ObjectiveRecord("prep_wood", "respawn", "combat_or_death", 30.0, "死于 pos=[-42.7,68.0,315.9] entity_id=555")
            ],
        )
        rendered = ctx.render()

        # 不透传规则答案与写死科技顺序
        self.assertNotIn("当前目标指引", rendered)
        self.assertNotIn("优先推进", rendered)
        for text in (rendered, OBJECTIVE_SYSTEM_PROMPT):
            self.assertNotIn("空手先采木", text)
            self.assertNotIn("木 -> 木镐", text)
            self.assertNotIn("木 → 木镐", text)

        # 不泄露任何真实坐标或实体 id
        self.assertNotIn("-42.7", rendered)
        self.assertNotIn("315.9", rendered)
        self.assertNotIn("555", rendered)
        self.assertIsNone(re.search(r"\[\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+", rendered))

        # 菜单按静态台账声明顺序展示（prep_wood 在 upgrade_tools 前），而非按 _rank() ROI 把 upgrade_tools 排第一
        ids = [c.goal_id for c in ctx.eligible_goals]
        self.assertLess(ids.index("prep_wood"), ids.index("upgrade_tools"))

    def test_optional_subgoal_and_reservation_fields_work_without_external_modules(self) -> None:
        state = State()
        state.set_status({"ready": True, "alive": True, "health": 20, "food": 20, "is_day": True})
        state.set_inventory({"items": [{"name": "iron_ingot", "count": 6, "slot": 0}]})

        planner = CampaignPlanner(
            mode="campaign",
            strategy="llm",
            min_verified="offline",
            subgoal_provider=lambda _st: {"station_ready(furnace)": True, "tool_ready(stone_pickaxe)": True},
            reserve_provider=lambda _st: ["iron_tools 预留 iron_ingotx3(consume)", "可分配 iron_ingot=3"],
        )
        rendered = planner.build_context(state).render()
        self.assertIn("子目标谓词：", rendered)
        self.assertIn("station_ready(furnace)=True", rendered)
        self.assertIn("物资预留：", rendered)
        self.assertIn("iron_ingotx3", rendered)
        self.assertLessEqual(len(rendered), 2000)


if __name__ == "__main__":
    unittest.main()
