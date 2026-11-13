"""官方寻路与手动兜底探测：`python -m mcagent.probe --pathfinding ...`

裸 `move_to` 的障碍能力（wall/gap/pillar/climb/water）+ 三项手动兜底序列 + 珍珠落点。
手动兜底序列直接跑 `skills/nav.py` 里的 L2 原语——probe 验的就是正式上场的那份代码。
运行方式与证据标准见 docs/02「寻路 probe 的运行方式与证据标准」。
"""
from __future__ import annotations

import json
import math
import statistics
import time

from .client import ApiError, Client, Config
from .probe_fixture import MANUAL_CASES, OUT_DIR, PATH_CASES, SEQUENCE_CASES, path_template, validate_fixture
from .runtime import Executor, Fatal, Replan, State
from .skills import nav
from .skills.base import failure as _unwrap_api_error
from .skills.items import pick_placeable, total_placeable


class _SingleShotExecutor(Executor):
    """probe 专用：任何动作都不自动重试。原语默认会重试可恢复错误，那会污染单次测量证据。"""

    def act(self, name, fn, retries=0, backoff=0.6, **kwargs):
        return super().act(name, fn, 0, backoff, **kwargs)


def _run_manual_case(name: str, case: dict, witnesses: list, rng: float,
                     c: Client, state: State, ex: Executor, row: dict) -> dict:
    """跑一项手动兜底序列：nav 原语逐步执行并留痕，最后 move_to 到目标验证确实走得过去。"""
    steps: list[dict] = []
    row["manual_steps"] = steps
    item = case.get("item")

    if name == "manual_breach":
        flow = nav.breach_1x2(ex, state, witnesses)
    elif name == "manual_bridge":
        flow = nav.sneak_bridge(ex, state, witnesses, item)
    elif name == "manual_step_up":
        # probe 独有的取证：往自身脚部碰撞箱直接 place 是否被拒（能否原地跳柱 vs 必须 1x2 交替垫高）
        feet = state.refresh_status(c, max_age_s=0).get("pos") or state.pos
        feet_pos = [math.floor(feet[0]), math.floor(feet[1]), math.floor(feet[2])]
        probe_item = pick_placeable(state, item)
        try:
            r_self = ex.place(probe_item, feet_pos)
            row["self_feet_place"] = {"ok": True, "pos": feet_pos, "result": r_self}
        except (ApiError, Replan) as self_err:
            sc, sex = _unwrap_api_error(self_err)
            row["self_feet_place"] = {"ok": False, "pos": feet_pos, "code": sc, "extra": sex}
        flow = nav.step_up_1x2(ex, state, witnesses, item)
    else:
        raise ValueError(f"未知手动探测类型: {name}")

    for record in flow:  # 逐条追加：中途失败也保留已完成的子步骤
        steps.append(record)
    res = ex.move_to(pos=case["target"], rng=rng)
    steps.append({"op": "move_to", "pos": case["target"], "result": res})
    return res


def pathfinding_main(argv: list[str]) -> int:
    import argparse
    from datetime import datetime, timezone
    from pathlib import Path
    from .client import DEFAULT_BASE
    from .runtime import Safety, SafetyMonitor

    parser = argparse.ArgumentParser(description="赛前官方寻路 probe；默认离线，不登录。")
    parser.add_argument("--pathfinding", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--case", choices=PATH_CASES, action="append", dest="cases")
    parser.add_argument("--manual-only", action="store_true", help="仅运行 climb_xz 与三项手动兜底序列")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--snapshot", action="store_true", help="只读采集当前坐标与周边方块并输出夹具草稿（不移动、不挖放）")
    parser.add_argument("--live", action="store_true", help="已获人类账号登录授权并在 docs/05 占位后使用")
    parser.add_argument("--local-target", action="store_true", help="仅允许联调，永不计入官方结论")
    parser.add_argument("--void-guard", action="store_true", help="显式对跨坑/搭桥/搭柱开启虚空支撑护栏标定（默认关闭防误掐寻路）")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--pearl-trials", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    selected = args.cases or (list(MANUAL_CASES) if args.manual_only else list(PATH_CASES))
    if not 1 <= args.pearl_trials <= 20:
        parser.error("pearl-trials 必须为 1..20")
    if args.samples < 1 or args.samples > 100:
        parser.error("samples 必须为 1..100")

    if args.snapshot:
        if args.dry or not args.live:
            parser.error("--snapshot 需要配合 --live（及非官方端点时的 --local-target）确认只读上线许可")
        cfg = Config.load()
        official = cfg.base == DEFAULT_BASE
        if not official and not args.local_target:
            parser.error("非官方端点：只可显式 --local-target 做联调")
        c = Client(cfg)
        try:
            st = c.wait_ready()
            inv = c.inventory()
            nearby = c.find_blocks(radius=24, limit=30)
            stones = c.find_blocks(types=["stone"], radius=16, limit=10)
            waters = c.find_blocks(types=["water"], radius=24, limit=10)
            tpl = path_template()
            if st.get("pos"):
                p = st["pos"]
                tpl["cases"]["move20"]["start"] = list(p)
                tpl["cases"]["move20"]["target"] = [round(p[0] + 20.0, 2), p[1], p[2]]
            snap_out = {
                "source": "official" if official and not args.local_target else "local-target",
                "status": st,
                "inventory": inv,
                "nearby_summary": nearby,
                "stones": stones,
                "waters": waters,
                "fixture_draft": tpl,
            }
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(snap_out, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                print(json.dumps(snap_out, ensure_ascii=False, indent=2))
            return 0
        finally:
            try:
                c.disconnect()
            except Exception:
                pass

    if not args.fixture:
        print(json.dumps(path_template(), ensure_ascii=False, indent=2))
        if args.live:
            parser.error("live 必须先提供已观察的 fixture")
        return 0
    fixture = json.loads(args.fixture.read_text(encoding="utf-8"))
    validate_fixture(fixture, selected)
    if args.dry or not args.live:
        print(f"离线校验通过：{', '.join(selected)}；未发请求、未登录")
        return 0
    cfg = Config.load()
    official = cfg.base == DEFAULT_BASE
    if not official and not args.local_target:
        parser.error("非官方端点：只可显式 --local-target 做联调，不能测官方寻路结论")
    # 显式 local-target 标记优先；即使 URL 是官方也不能误写成官方数据。
    source = "official" if official and not args.local_target else "local-target"
    c, state = Client(cfg), State()
    ex = _SingleShotExecutor(c, state)
    out = args.output or Path(OUT_DIR) / datetime.now(timezone.utc).strftime("pathfinding-%Y%m%dT%H%M%S-%fZ.json")
    report = {"schema": "pathfinding-v1", "source": source, "base": cfg.base, "max_rps": cfg.max_rps,
              "started_at": datetime.now(timezone.utc).isoformat(), "fixture": fixture,
              "cases": [], "C1": {}, "complete": False,
              "interpretation": "API 成功/拒绝均保留原始证据；需结合轨迹、方块前后变化与背包变化复核真实物理行为。"}
    def save():
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(".tmp")
        tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out)
    def observe():
        st = state.refresh_status(c, max_age_s=0)
        state.invalidate_inventory()
        return {"status": st, "inventory": state.refresh_inventory(c)}
    def blocks(positions):
        return [{"pos": pos, "block": c.get_block(pos)} for pos in positions]
    class RecordingMonitor(SafetyMonitor):
        def tick(self):
            hz = super().tick()
            timeline.append({"elapsed_s": round(time.monotonic()-started, 3),
                             "status": state.snapshot(), "support": state.get_support(), "hazard": hz})
            return hz
    monitor = None
    connected = False
    timeline: list[dict] = []
    started = time.monotonic()
    safety = Safety(state.config)
    try:
        # wait_ready 自动复用缓存 token；不强制 login 踢掉现有会话。
        state.set_status(c.wait_ready())
        connected = True
        status_samples = []
        lim = getattr(c, "_limiter", None)
        for _ in range(args.samples):
            if lim is not None:
                rem = getattr(lim, "_next", 0.0) - time.monotonic()
                if rem > 0:
                    time.sleep(rem + 0.002)
            t = time.monotonic()
            state.refresh_status(c, max_age_s=0)
            status_samples.append(round((time.monotonic()-t)*1000, 2))
        report["C1"]["T_status"] = {"all_ms": status_samples, "p50_ms": statistics.median(status_samples)}
        save()
        runs = [(name, trial+1) for name in selected for trial in range(args.pearl_trials if name == "pearl" else 1)]
        for name, trial in runs:
            case = fixture["cases"][name]
            rng = float(case["range"]) if case.get("range") is not None else 1.0
            tol_h = max(1.8, rng + 0.8)
            row: dict = {"case": name, "trial": trial, "status": "inconclusive", "setup": {}, "samples": []}
            report["cases"].append(row)
            save()
            # 准备阶段也经 Executor；不把去起点的时间混入 case/C1。
            state.set_risk_context(near_water=name == "water", near_void=False, void_guard=False)
            timeline = []
            started = time.monotonic()
            monitor = RecordingMonitor(c, state, ex, safety=safety)
            monitor.start()
            phase = "setup"
            preempted = False
            fatal_abort = False
            try:
                row["setup"] = ex.act("move_to", lambda st=case["start"], r=rng: c.move_to(pos=st, rng=r), retries=0)
                before = observe()
                row["before"] = before
                before_pos = before["status"].get("pos") or [float("inf")] * 3
                start_h_err = math.hypot(before_pos[0] - case["start"][0], before_pos[2] - case["start"][2])
                start_y_err = abs(before_pos[1] - case["start"][1])
                if start_h_err > tol_h or start_y_err > 1.5:
                    raise ValueError(f"未到 fixture 起点 (horiz={start_h_err:.2f}, dy={start_y_err:.2f})")
                urgent_start = [h for h in safety.hazards(state) if h["severity"] == "urgent"]
                if urgent_start:
                    raise ValueError(f"起点存在紧急安全风险 ({urgent_start[0]['kind']})，不能执行本 case")
                witnesses = case["witness_blocks"]
                row["blocks_before"] = blocks(witnesses)
                names = [b["block"].get("name", "").removeprefix("minecraft:") for b in row["blocks_before"]]
                if name == "wall" and "stone" not in names:
                    raise ValueError("石墙夹具没有观察到挡路 stone")
                if name == "manual_breach" and all(n in ("air", "cave_air", "void_air", "water") for n in names):
                    raise ValueError("manual_breach 的 witness_blocks 没有待挖实心障碍块")
                if name in ("gap", "pillar", "manual_bridge", "manual_step_up") and any(
                    n not in ("air", "cave_air", "void_air") for n in names
                ):
                    raise ValueError("待填格不是空气，夹具需重置")
                if name in ("manual_bridge", "manual_step_up") and total_placeable(state, case.get("item")) < len(witnesses):
                    raise ValueError(f"{name} 需要至少 {len(witnesses)} 块可放置方块")
                if name == "water" and not any(n in ("water", "bubble_column") for n in names):
                    raise ValueError("水池夹具没有观察到水")
                if name == "dig1":
                    row["dig_target_before"] = c.get_block(case["target"])
                    if row["dig_target_before"].get("name") not in ("stone", "minecraft:stone"):
                        raise ValueError("T_dig1 要求单块 stone，不能混用其他方块")
                    feet = before["status"]["pos"]
                    if (math.floor(feet[0]) == math.floor(case["target"][0]) and
                            math.floor(feet[2]) == math.floor(case["target"][2]) and case["target"][1] < feet[1]):
                        raise ValueError("拒绝测量挖脚下承重块")
                if name == "pearl":
                    if float(before["status"].get("health", 20)) <= 6.0:
                        raise ValueError("血量 <=6，停止投掷末影珍珠防摔落致死")
                    if state.count("ender_pearl") < 1:
                        raise ValueError("缺少 ender_pearl，不能测落点")

                # 坑/柱已有安全底板，默认不开 void_guard 以免起跳/跨坑瞬间被护栏自掐；仅 --void-guard 显式标定时开启。
                state.set_risk_context(
                    near_water=name == "water",
                    near_void=False,
                    void_guard=bool(args.void_guard and name in ("gap", "pillar", "manual_bridge", "manual_step_up")),
                )
                is_seq = name in SEQUENCE_CASES
                if is_seq:
                    action = name
                    action_args = {"target": case["target"], "witness_blocks": witnesses, "rng": rng}
                elif name == "climb_xz":
                    action = "move_to"
                    action_args = {"x": case["target"][0], "z": case["target"][2], "rng": rng}
                elif name == "dig1":
                    action = "dig"
                    action_args = {"pos": case["target"]}
                elif name == "pearl":
                    action = "use_item"
                    action_args = {"item": "ender_pearl", "target": {"pos": case["target"]}}
                else:
                    action = "move_to"
                    action_args = {"pos": case["target"], "rng": rng}
                row["action"] = {"name": action, "args": action_args}
                phase = "action"
                epoch_before = ex._preempt_epoch
                t = time.monotonic()
                try:
                    if is_seq:
                        result = _run_manual_case(name, case, witnesses, rng, c, state, ex, row)
                    else:
                        result = ex.act(action, lambda: getattr(c, action)(**action_args), retries=0)
                    row["result"] = result
                    row["api_ok"] = True
                    row["outcome"] = "ok"
                except (ApiError, Replan) as act_err:
                    code, extra = _unwrap_api_error(act_err)
                    row["api_ok"] = False
                    row["outcome"] = f"rejected:{code}"
                    row["error"] = {"type": type(act_err).__name__, "code": code, "phase": "action",
                                    "extra": extra, "message": str(act_err)}
                finally:
                    row["elapsed_ms"] = round((time.monotonic()-t)*1000, 2)
                    preempted = ex._preempt_epoch != epoch_before

                row["after"] = observe()
                row["blocks_after"] = blocks(witnesses)
                actual = row["after"]["status"].get("pos")
                if actual:
                    row["target_error_3d"] = math.dist(actual, case["target"])
                    row["target_error_horizontal"] = math.hypot(actual[0]-case["target"][0], actual[2]-case["target"][2])
                    row["target_error_y"] = actual[1]-case["target"][1]
                    row["actual_y_gain"] = round(actual[1]-before_pos[1], 2)
                    row["walked_horizontal"] = math.hypot(actual[0]-before_pos[0], actual[2]-before_pos[2])

                after_names = [b["block"].get("name", "").removeprefix("minecraft:") for b in row["blocks_after"]]
                if name == "pearl":
                    row["teleported"] = (row.get("result") or {}).get("teleported")
                    row["status"] = "observed" if (row["api_ok"] and row["teleported"] is True) else "inconclusive"
                elif name in ("move20", "dig1"):
                    row["status"] = "observed" if row["api_ok"] else "inconclusive"
                elif name == "climb_xz":
                    err_code = (row.get("error") or {}).get("code")
                    arrived_xz = row["api_ok"] and row.get("target_error_horizontal", 99) <= tol_h
                    row["status"] = "observed" if (
                        arrived_xz or err_code in ("path_not_found", "timeout", "out_of_reach")
                    ) else "inconclusive"
                elif is_seq:
                    err_code = (row.get("error") or {}).get("code")
                    arrived_3d = (
                        row["api_ok"]
                        and row.get("target_error_horizontal", 99) <= tol_h
                        and abs(row.get("target_error_y", 99)) <= 1.5
                    )
                    blocks_verified = (
                        all(n in ("air", "cave_air", "void_air") for n in after_names)
                        if name == "manual_breach"
                        else all(n and n not in ("air", "cave_air", "void_air") for n in after_names)
                    )
                    known_reject = err_code in (
                        "blocked_by_self", "no_support", "place_failed", "cannot_place",
                        "path_not_found", "timeout", "out_of_reach", "protected_region",
                        "occupied", "undiggable",
                    )
                    row["status"] = "observed" if ((arrived_3d and blocks_verified) or known_reject) else "inconclusive"
                else:
                    # 障碍寻路能力测试：服务端正常返回 path_not_found/timeout 同样是明确的能力观测结论
                    err_code = (row.get("error") or {}).get("code")
                    if row["api_ok"] or err_code in ("path_not_found", "timeout", "out_of_reach"):
                        row["status"] = "observed"
                    else:
                        row["status"] = "inconclusive"

                if name in ("move20", "dig1") and row["api_ok"]:
                    if name == "dig1":
                        valid = (row.get("result") or {}).get("dug") in ("stone", "minecraft:stone")
                        if valid:
                            report["C1"]["T_dig1"] = {"ms": row["elapsed_ms"], "case": name}
                    else:
                        valid = (
                            row.get("target_error_horizontal", 99) <= tol_h
                            and abs(row.get("target_error_y", 99)) <= 1.5
                            and row.get("walked_horizontal", 0) >= 18.0
                        )
                        if valid:
                            report["C1"]["T_move20"] = {
                                "ms": row["elapsed_ms"],
                                "walked_horizontal": round(row.get("walked_horizontal", 0), 2),
                                "case": name,
                            }
            except (ApiError, Replan, Fatal, ValueError) as e:
                code, extra = _unwrap_api_error(e)
                row["error"] = {"type": type(e).__name__, "code": code, "phase": phase,
                                "extra": extra, "message": str(e)}
                row["api_ok"] = False
                row["status"] = "inconclusive"
                if isinstance(e, Fatal):
                    fatal_abort = True
            finally:
                # 关 monitor 后取完整事件尾部；仅真实抢占或紧急危险标为干扰，软意图（如珍珠扣血 heal）不抹除有效样本。
                ex.stop()
                monitor.close(timeout=60)
                monitor = None
                state.set_risk_context(near_water=False, near_void=False, void_guard=False)
                row["samples"] = timeline
                urgent_in_timeline = any(
                    (sample.get("hazard") or {}).get("severity") == "urgent" for sample in timeline
                )
                if preempted or urgent_in_timeline:
                    row["safety_interference"] = True
                    row["status"] = "inconclusive"
                    report["C1"].pop("T_move20" if name == "move20" else "T_dig1" if name == "dig1" else "unused", None)
                try:
                    row["events"] = state.drain(c)
                    row["observed_events"] = list(state.events)
                    if "after" not in row:
                        row["after"] = observe()
                    if "blocks_before" in row and "blocks_after" not in row:
                        row["blocks_after"] = blocks(case["witness_blocks"])
                except Exception as e:
                    row["cleanup_observation_error"] = str(e)
                save()
            snap_after = state.snapshot()
            urgent_after = [h for h in safety.hazards(state) if h["severity"] == "urgent"]
            if fatal_abort or not snap_after.get("alive", True) or urgent_after:
                report["aborted_after"] = name
                break
        errors = [r["target_error_3d"] for r in report["cases"]
                  if r["case"] == "pearl" and r["status"] == "observed" and r.get("teleported") is True
                  and "target_error_3d" in r]
        report["pearl_error"] = {"n": len(errors), "all_3d": errors,
                                 "p50_3d": statistics.median(errors) if errors else None,
                                 "max_3d": max(errors) if errors else None}
        report["complete"] = (len(report["cases"]) == len(runs)
                              and all(r["status"] == "observed" for r in report["cases"]))
    except (ApiError, Replan, Fatal, ValueError, KeyboardInterrupt) as e:
        report["error"] = {"type": type(e).__name__, "message": str(e)}
        # 登录后 ready 超时仍须清理；无 token/认证失败时不要在清理路径反复尝试登录。
        if getattr(c, "token", None) and getattr(e, "code", "") not in (
                "invalid_credentials", "unauthorized", "invalid_token", "no_profile"):
            connected = True
    finally:
        if connected:
            ex.stop()
            if monitor:
                monitor.close(timeout=60)
            try:
                c.disconnect()
            except Exception as e:
                report["disconnect_error"] = str(e)
        save()
        print(f"{source} 原始证据：{out}")
    return 0 if report["complete"] else 1
