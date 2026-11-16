"""策略层世界资源台账脱敏注入与 EnvBlocked(chunk_moved) 配合离线单测（verified: offline）。

覆盖：
1. `build_state_summary(state)` 在台账为空（或当前维度无可用条目）时不输出多余噪声行；
2. `build_state_summary(state)` 在台账存在 `wood`/`lava`/`iron_ore`/`structure` 时追加
   `已知环境资源：...`，保留向后兼容签名与第 1 行玩家自身 `位置=...`，且新增文本与 `format_memo_summary`
   严守 J17（无任何三维坐标三元组与目标资源坐标数字）；
3. `CampaignPlanner(..., use_llm=True)` 传入 mock LLM 时，发给模型的提示词包含 `已知环境资源` 且无资源坐标；
   当 `acq:wood` 被 `EnvBlocked(..., missing_predicate="exposed_log", wake_on="chunk_moved")` 挂起后，
   `ready_registry` 与 `RulePlanner` 正确将 `gather_wood` 挂起并调度 `scout_island` 探索，
   同区块内保持挂起、跨区块后自动唤醒 `gather_wood`（含 `MainLoop` 端到端闭环验证）。
"""
from __future__ import annotations

import json
import re
import tempfile
import unittest
from unittest.mock import patch

from mcagent.loop import (
    CampaignPlanner,
    LoopConfig,
    MainLoop,
    RulePlanner,
    SoakObserver,
    build_state_summary,
    reset_world_memo,
)
from mcagent.loop.starter_skills import ScoutIsland
from mcagent.planner import llm as llm_mod
from mcagent.planner import skill_choice
from mcagent.planner.llm import LLMClient
from mcagent.runtime import EnvBlocked, State
from mcagent.skills import REGISTRY, Param, Skill, make
from mcagent.skills import base as skill_base
from mcagent.skills.base import rec
from mcagent.skills.testing import CountingExecutor
from mcagent.skills.world_memo import format_memo_summary, memo_for, reset_all_world_memos
from tests.test_loop_soak import RichGridWorld

_COORD_TRIPLET_RE = re.compile(r"-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?\s*,\s*-?\d+(?:\.\d+)?")
_BRACKETED_COORD_RE = re.compile(r"[\[(]\s*-?\d+[^|\n]*-?\d+[^|\n]*-?\d+\s*[\])]")


def _make_llm_reply(skill: str, args: dict, why: str = "测试选择") -> tuple[int, str]:
    content = json.dumps({"skill": skill, "args": args, "why": why}, ensure_ascii=False)
    return 200, json.dumps({
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 24, "total_tokens": 144},
    })


def _seed_sample_resources(state: State) -> tuple[str, ...]:
    """向台账注入 wood/lava/iron_ore/structure 样本，返回绝不能泄漏进摘要的原始坐标文本。"""
    memo = memo_for(state)
    memo.ingest_find_blocks({
        "blocks": [
            {"name": "minecraft:spruce_log", "pos": [123, 68, -456], "dist": 35.2},
            {"name": "lava", "pos": [-987, 61, 345], "dist": 52.0},
            {"name": "iron_ore", "pos": [432, 49, -765], "dist": 16.4},
            {"name": "hay_block", "pos": [1024, 67, -888], "dist": 41.2},
        ],
    })
    memo.mark_reachable([-987, 61, 345])
    return ("123", "68", "-456", "-987", "61", "345", "432", "49", "-765", "1024", "67", "-888")


def _exposed_log_blocked() -> EnvBlocked:
    return EnvBlocked(
        "半径 32 内暂无可砍裸露原木",
        missing_predicate="exposed_log",
        searched_volume="半径 32",
        wake_on="chunk_moved",
    )


class _StubGatherWood(Skill):
    name = "gather_wood"
    description = "测试用原木采集技能：x < 16 时抛出 exposed_log EnvBlocked"
    params = {"count": Param(int, "数量", default=3, min=1, max=16)}
    goal = "acq:wood"
    verified = "offline"

    def run(self, ex, state):
        if state.pos[0] < 16:
            raise _exposed_log_blocked()
        for dy in range(int(self.args["count"])):
            pos = [23, 64 + dy, 0]
            yield rec("dig", ex.dig(pos, collect=True), pos=pos)
            state.refresh_inventory(ex.client)


class WorldMemoPolicyTests(unittest.TestCase):
    def setUp(self):
        llm_mod._reset_semaphores()
        skill_base._COOLDOWN.clear()
        reset_world_memo()
        reset_all_world_memos()
        self.addCleanup(llm_mod._reset_semaphores)
        self.addCleanup(skill_base._COOLDOWN.clear)
        self.addCleanup(reset_world_memo)
        self.addCleanup(reset_all_world_memos)

        self.state = State()
        self.state.set_status({
            "pos": [0.5, 64.0, 0.5],
            "dimension": "overworld",
            "is_day": True,
            "time_of_day": 1000,
            "health": 20,
            "food": 20,
        })
        self.state.set_inventory({"items": []})

    def _assert_no_3d_coords(self, text: str, raw_numbers: tuple[str, ...] = ()) -> None:
        self.assertIsNone(_COORD_TRIPLET_RE.search(text), f"发现未脱敏三维坐标三元组: {text!r}")
        self.assertIsNone(_BRACKETED_COORD_RE.search(text), f"发现未脱敏括号坐标: {text!r}")
        for num in raw_numbers:
            self.assertIsNone(
                re.search(rf"(?<![\d-]){re.escape(num)}(?!\d)", text),
                f"摘要/提示词泄漏了原始资源坐标数字 {num!r}: {text!r}",
            )

    def test_build_state_summary_empty_memo_has_no_extra_noise(self):
        self.assertEqual(format_memo_summary(self.state), "")
        summary = build_state_summary(self.state)
        lines = summary.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[0].startswith("位置=[0.5, 64.0, 0.5]"))
        self.assertNotIn("已知环境资源", summary)

        hinted = build_state_summary(self.state, goal_hint="优先推进 prep_wood")
        self.assertEqual(len(hinted.splitlines()), 5)
        self.assertNotIn("已知环境资源", hinted)
        self.assertIn("当前目标指引：优先推进 prep_wood", hinted)

        # 只有 protected/depleted 或跨维度记录时，当前维度摘要仍为空，不增加多余行
        memo = memo_for(self.state)
        memo.record_seen("wood", "oak_log", [11, 64, 11], dimension="overworld", status="protected")
        memo.record_seen("lava", "lava", [22, 62, 22], dimension="the_nether", status="untested")
        self.assertEqual(build_state_summary(self.state), summary)

    def test_build_state_summary_injects_sanitized_resources_without_3d_coords(self):
        baseline_lines = build_state_summary(self.state).splitlines()
        raw_coords = _seed_sample_resources(self.state)

        memo_text = format_memo_summary(self.state)
        self.assertTrue(memo_text)
        self._assert_no_3d_coords(memo_text, raw_coords)

        summary = build_state_summary(self.state, goal_hint="优先推进 prep_wood")
        lines = summary.splitlines()
        self.assertTrue(lines[0].startswith("位置=[0.5, 64.0, 0.5]"))

        added_lines = [line for line in lines if line not in baseline_lines and not line.startswith("当前目标指引：")]
        self.assertEqual(added_lines, [f"已知环境资源：{memo_text}"])
        known_line = added_lines[0]
        self.assertIn("原木: spruce_log", known_line)
        self.assertIn("熔岩: lava", known_line)
        self.assertIn("已确认可达", known_line)
        self.assertIn("铁矿: iron_ore", known_line)
        self.assertIn("结构: village(hay_block)", known_line)
        self._assert_no_3d_coords(known_line, raw_coords)

        # 第 1 行玩家自身位置保留，其他行（及全文对资源原始坐标数字）严格脱敏
        self._assert_no_3d_coords("\n".join(lines[1:]), raw_coords)
        self._assert_no_3d_coords(known_line, raw_coords)

    def test_campaign_planner_prompt_and_env_blocked_chunk_wake(self):
        raw_coords = _seed_sample_resources(self.state)
        captured_prompts: list[str] = []
        queued_replies = [
            _make_llm_reply("gather_wood", {"count": 3}, "先砍树"),
            _make_llm_reply("scout_island", {"radius": 32}, "外迁找木"),
            _make_llm_reply("gather_wood", {"count": 3}, "忽略禁用测试兜底"),
            _make_llm_reply("gather_wood", {"count": 3}, "忽略禁用测试兜底2"),
            _make_llm_reply("gather_wood", {"count": 3}, "换区后继续砍树"),
        ]

        def fake_poster(url, headers, data, timeout, proxy):
            body = json.loads(data)
            for msg in body.get("messages", []):
                if msg.get("role") == "user":
                    captured_prompts.append(msg["content"])
            return queued_replies.pop(0)

        llm = LLMClient(key="dummy-key", fast_proxy="", poster=fake_poster, log=None)
        planner = CampaignPlanner(mode="campaign", use_llm=True, llm=llm, min_verified="offline")
        rule_only = RulePlanner(REGISTRY)
        focused_rule = RulePlanner(REGISTRY, target_goal="prep_wood")

        with patch("mcagent.loop.policy.skill_choice.choose", wraps=skill_choice.choose) as choose_spy:
            # 1) 挂起前：ready_registry 含 gather_wood，模型提示词带「已知环境资源」且无资源坐标
            dec1 = planner.select(self.state, [])
            self.assertEqual(dec1.source, "llm")
            self.assertEqual(dec1.skill.name, "gather_wood")
            self.assertIn("gather_wood", choose_spy.call_args.kwargs["registry"])
            self.assertIn("已知环境资源：", captured_prompts[-1])
            known_prompt_lines = [ln for ln in captured_prompts[-1].splitlines() if ln.startswith("已知环境资源：")]
            self.assertEqual(len(known_prompt_lines), 1)
            self._assert_no_3d_coords(known_prompt_lines[0], raw_coords)
            non_player_pos_lines = "\n".join(
                ln for ln in captured_prompts[-1].splitlines() if not ln.startswith("位置=")
            )
            self._assert_no_3d_coords(non_player_pos_lines, raw_coords)

            # 2) gather_wood 抛出 EnvBlocked(missing_predicate="exposed_log", wake_on="chunk_moved")
            err = _exposed_log_blocked()
            gw = make("gather_wood")
            planner.record_environment_block(gw, err, self.state)
            rule_only.record_environment_block(gw, err, self.state)
            focused_rule.record_environment_block(gw, err, self.state)

            # 同区块 (0, 0) 内：RulePlanner 挂起 gather_wood 并改派 scout_island
            self.assertIsNotNone(rule_only.environment_block("acq:wood"))
            r_dec = rule_only.choose(self.state)
            self.assertEqual(r_dec.source, "rule")
            self.assertEqual(r_dec.skill.name, "scout_island")

            f_dec = focused_rule.choose(self.state)
            self.assertEqual(f_dec.skill.name, "scout_island")
            self.assertTrue(f_dec.environment_recovery)

            # CampaignPlanner(use_llm=True)：ready_registry 剔除 gather_wood，保留 scout_island
            dec2 = planner.select(self.state, [])
            ready_reg_suspended = choose_spy.call_args.kwargs["registry"]
            self.assertNotIn("gather_wood", ready_reg_suspended)
            self.assertIn("scout_island", ready_reg_suspended)
            self.assertEqual(dec2.source, "llm")
            self.assertEqual(dec2.skill.name, "scout_island")
            self.assertIn("已知环境资源：", captured_prompts[-1])
            ready_menu = captured_prompts[-1].split("可启动技能", 1)[1].split("暂不可启动", 1)[0]
            self.assertNotIn("gather_wood(", ready_menu)
            self._assert_no_3d_coords(
                [ln for ln in captured_prompts[-1].splitlines() if ln.startswith("已知环境资源：")][0],
                raw_coords,
            )

            # 若仍处于同一区块 (15.5, 64, 15.5 -> chunk (0, 0)) 且模型硬选 gather_wood，则校验拒绝并回退规则调度 scout_island
            self.state.set_status({"pos": [15.5, 64.0, 15.5], "dimension": "overworld", "is_day": True})
            dec_fb = planner.select(self.state, [])
            self.assertNotIn("gather_wood", choose_spy.call_args.kwargs["registry"])
            self.assertEqual(dec_fb.source, "llm_fallback_rule")
            self.assertEqual(dec_fb.skill.name, "scout_island")

            # 3) 跨入新区块 (16.5, 64, 0.5 -> chunk (1, 0))：自动唤醒 gather_wood
            self.state.set_status({"pos": [16.5, 64.0, 0.5], "dimension": "overworld", "is_day": True})
            r_wake = rule_only.choose(self.state)
            self.assertIsNone(rule_only.environment_block("acq:wood"))
            self.assertEqual(r_wake.skill.name, "gather_wood")

            f_wake = focused_rule.choose(self.state)
            self.assertIsNone(focused_rule.environment_block("acq:wood"))
            self.assertEqual(f_wake.skill.name, "gather_wood")

            dec3 = planner.select(self.state, [])
            self.assertIsNone(planner.rules.environment_block("acq:wood"))
            self.assertIn("gather_wood", choose_spy.call_args.kwargs["registry"])
            self.assertEqual(dec3.source, "llm")
            self.assertEqual(dec3.skill.name, "gather_wood")

    def test_mainloop_env_blocked_chunk_moved_with_llm_and_world_memo(self):
        world = RichGridWorld(
            pos=(0.5, 64.0, 0.5),
            blocks={
                (10, 63, 0): "grass_block",
                (21, 63, 0): "grass_block",
                (23, 64, 0): "oak_log",
                (23, 65, 0): "oak_log",
                (23, 66, 0): "oak_log",
            },
        )
        _, state = world.session()
        raw_coords = _seed_sample_resources(state)

        captured_prompts: list[str] = []
        replies = [
            _make_llm_reply("gather_wood", {"count": 3}, "先尝试砍树"),
            _make_llm_reply("scout_island", {"radius": 32}, "跨区块找树"),
            _make_llm_reply("gather_wood", {"count": 3}, "唤醒后砍树"),
        ]

        def fake_poster(url, headers, data, timeout, proxy):
            body = json.loads(data)
            for msg in body.get("messages", []):
                if msg.get("role") == "user":
                    captured_prompts.append(msg["content"])
            return replies.pop(0)

        registry = {_StubGatherWood.name: _StubGatherWood, ScoutIsland.name: ScoutIsland}
        llm = LLMClient(key="dummy-key", fast_proxy="", poster=fake_poster, log=None)
        planner = CampaignPlanner(
            mode="campaign",
            use_llm=True,
            llm=llm,
            min_verified="offline",
            registry=registry,
            target_goal="prep_wood",
        )
        config = LoopConfig(
            target_goal="prep_wood",
            stop_on_goal=True,
            idle_sleep_s=0.0,
            enable_monitor=False,
            max_steps=20,
            max_seconds=5.0,
        )

        with tempfile.TemporaryDirectory() as run_dir:
            observer = SoakObserver(run_dir, target_goal="prep_wood")
            result = MainLoop(
                world,
                state,
                CountingExecutor(world, state),
                planner,
                config=config,
                observer=observer,
                log=lambda *_: None,
            ).run()
            rows = [json.loads(line) for line in observer.timeline_path.read_text(encoding="utf-8").splitlines()]

        starts = [r["skill"] for r in rows if r.get("event") == "skill_start"]
        self.assertEqual(starts, ["gather_wood", "scout_island", "gather_wood"])
        self.assertEqual(result.stop_reason, "goal_reached")
        self.assertEqual(result.skills_replan, 0)
        self.assertEqual(rows[-1]["environment_blocked_goals"], {})
        self.assertEqual(len(captured_prompts), 3)
        for prompt in captured_prompts:
            known_lines = [ln for ln in prompt.splitlines() if ln.startswith("已知环境资源：")]
            self.assertEqual(len(known_lines), 1)
            self._assert_no_3d_coords(known_lines[0], raw_coords)
            non_player_pos_lines = "\n".join(
                ln for ln in prompt.splitlines() if not ln.startswith("位置=")
            )
            self._assert_no_3d_coords(non_player_pos_lines, raw_coords)


if __name__ == "__main__":
    unittest.main()
