"""D1 基线测量：把"8 小时能排多少动作"这件事量出来。

这三个数是后面所有计划的输入：
  T_status  —— 一次观察的开销
  T_action  —— 一次动作的实际耗时
  叠加规律  —— 一个动作配几次观察才不亏

    python -m mcagent.probe                # 完整
    python -m mcagent.probe --no-dig       # 不动世界
    python -m mcagent.probe --no-move
结果写到 mcagent/probe/probe-YYYYmmdd-HHMMSS.json
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Callable

from .client import ApiError, Client, Config
from .runtime import Executor, Fatal, Replan, State

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")


def _unwrap_api_error(e: BaseException) -> tuple[str, dict]:
    if isinstance(e, ApiError):
        return e.code, dict(e.extra)
    cause = e.__cause__
    if isinstance(cause, ApiError):
        return cause.code, dict(cause.extra)
    return type(e).__name__.lower(), {}


def bench(fn: Callable[[], object], n: int = 10, warmup: int = 1, pause_s: float = 0.26) -> dict:
    for _ in range(warmup):
        try:
            fn()
        except ApiError:
            pass
        time.sleep(pause_s)
    lat, errs = [], 0
    for _ in range(n):
        t0 = time.time()
        try:
            fn()
            lat.append((time.time() - t0) * 1000)
        except ApiError:
            errs += 1
            lat.append((time.time() - t0) * 1000)
        time.sleep(pause_s)
    return {
        "n": n,
        "err": errs,
        "p50": round(statistics.median(lat), 1) if lat else None,
        "min": round(min(lat), 1) if lat else None,
        "max": round(max(lat), 1) if lat else None,
        "all": [round(x) for x in lat],
    }


def main(argv: list[str]) -> int:
    if "--pathfinding" in argv:
        return pathfinding_main(argv)
    do_dig = "--no-dig" not in argv
    do_move = "--no-move" not in argv

    cfg = Config.load()
    if not cfg.has_credentials:
        print("没有凭据，先填 mcagent/.env")
        return 2

    c = Client(cfg)
    s = State()
    ex = Executor(c, s)
    res: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "base": cfg.base, "max_rps": cfg.max_rps}

    st = c.wait_ready(timeout=120)
    s.set_status(st)
    print(f"起点: {s.summary()}\n")

    # ---- 1. 观察类开销
    print("[1] 观察接口")
    res["status"] = bench(lambda: s.set_status(c.status()), n=20)
    res["inventory"] = bench(lambda: s.set_inventory(c.inventory()), n=6)
    res["events"] = bench(lambda: s.drain(c), n=8)
    for k in ("status", "inventory", "events"):
        r = res[k]
        print(f"  {k:10s} p50={r['p50']}ms  max={r['max']}ms  err={r['err']}/{r['n']}")

    # ---- 2. 搜索类（radius 对耗时的杠杆）
    print("\n[2] find_blocks（半径是主要成本）")
    res["find_blocks"] = {}
    for radius in (16, 32, 64):
        try:
            r = bench(lambda r=radius: c.find_blocks(types=["oak_log"], radius=r, limit=5), n=5)
            res["find_blocks"][radius] = r
            print(f"  radius={radius:2d}  p50={r['p50']}ms")
        except ApiError as e:
            print(f"  radius={radius:2d}  失败: {e}")

    print("\n[2b] find_blocks 摘要模式（无 types）")
    try:
        r = bench(lambda: c.find_blocks(radius=32), n=3)
        res["find_blocks_summary"] = r
        print(f"  p50={r['p50']}ms")
    except ApiError as e:
        print(f"  失败: {e}")

    # ---- 3. 实体/进度
    print("\n[3] get_entities / advancements")
    try:
        res["get_entities"] = bench(lambda: c.get_entities(radius=32), n=4)
        print(f"  get_entities p50={res['get_entities']['p50']}ms")
    except ApiError as e:
        print(f"  get_entities 失败: {e}")
    try:
        adv = c.advancements()
        res["advancements_count"] = len(adv.get("advancements") or [])
        res["advancements_ready"] = adv.get("ready")
        res["advancements"] = bench(lambda: c.advancements(), n=3)
        print(f"  advancements p50={res['advancements']['p50']}ms  "
              f"可见 {res['advancements_count']} 条  ready={res['advancements_ready']}")
    except ApiError as e:
        print(f"  advancements 失败: {e}")

    # ---- 4. 动作类：走 20 格
    if do_move:
        print("\n[4] move_to 20 格")
        x, y, z = s.pos[0], s.pos[1], s.pos[2]
        t0 = time.time()
        try:
            r = ex.move_to(x + 20, z, rng=1)
            dt = time.time() - t0
            res["move_20"] = {"ms": round(dt * 1000), "ok": True, "moved_to": r.get("pos")}
            print(f"  耗时 {dt:.2f}s -> {r.get('pos')}  (≈{20 / max(dt, 0.01):.1f} 格/秒)")
        except (ApiError, Replan, Fatal) as e:
            code, _ = _unwrap_api_error(e)
            res["move_20"] = {"ok": False, "code": code, "ms": round((time.time() - t0) * 1000)}
            print(f"  失败: {e}")
        time.sleep(1)
        try:
            r = ex.move_to(x, z, rng=1)
            print(f"  回位 -> {r.get('pos')}")
        except (ApiError, Replan, Fatal) as e:
            print(f"  回位失败: {e}")

    # ---- 5. 动作类：挖一块
    if do_dig:
        import math
        print("\n[5] dig 一块（挑最近的非脚下 dirt/stone/grass）")
        target = None
        fx, fy, fz = math.floor(s.pos[0]), s.pos[1], math.floor(s.pos[2])
        for types in (["dirt", "grass_block", "coarse_dirt"], ["stone", "cobblestone"], ["oak_log", "birch_log"]):
            try:
                r = c.find_blocks(types=types, radius=12, limit=8)
                blocks = r.get("blocks") or []
                if isinstance(blocks, list):
                    cand = [
                        b for b in blocks
                        if isinstance(b.get("pos"), list) and len(b["pos"]) == 3
                        and not (b["pos"][0] == fx and b["pos"][2] == fz and b["pos"][1] < fy)
                    ]
                    if cand:
                        target = cand[0]
                        break
            except ApiError as e:
                print(f"  find {types} 失败: {e}")
        if target:
            print(f"  目标 {target['name']} @ {target['pos']} dist={target.get('dist')}")
            t0 = time.time()
            try:
                r = ex.dig(target["pos"])
                dt = time.time() - t0
                res["dig"] = {"ms": round(dt * 1000), "ok": True, "dig_ms": r.get("dig_ms"), "gained": r.get("gained")}
                print(f"  耗时 {dt:.2f}s  服务端 dig_ms={r.get('dig_ms')}  gained={r.get('gained')}")
            except (ApiError, Replan, Fatal) as e:
                code, _ = _unwrap_api_error(e)
                res["dig"] = {"ok": False, "code": code, "ms": round((time.time() - t0) * 1000)}
                print(f"  失败: {e}")
        else:
            print("  附近没找到可挖的方块")

    # ---- 6. 一个"观察-动作"周期的真实开销
    print("\n[6] 一个动作周期的完整开销（status + act + status）")
    cycles = []
    for _ in range(5):
        t0 = time.time()
        try:
            s.set_status(c.status())
            ex.posture(sneak=True)
            ex.posture(sneak=False)
            s.set_status(c.status())
            cycles.append((time.time() - t0) * 1000)
        except (ApiError, Replan, Fatal) as e:
            print(f"  失败: {e}")
            break
        time.sleep(0.1)
    if cycles:
        res["cycle"] = {"p50": round(statistics.median(cycles), 1), "all": [round(x) for x in cycles]}
        print(f"  p50={res['cycle']['p50']}ms  -> 纯开销；真实每动作 = 这个 + 动作本身")

    # ---- 7. 错误路径的延迟（缺材料合成）
    print("\n[7] 错误路径（craft 缺材料）")
    try:
        # 先在计时外刷新背包快照，避免把 Executor 的合成前 GET /inventory 混入错误路径耗时
        s.refresh_inventory(c)
    except ApiError:
        pass
    t0 = time.time()
    try:
        ex.craft("diamond_pickaxe")
        print("  意外成功？")
    except (ApiError, Replan, Fatal) as e:
        dt = (time.time() - t0) * 1000
        code, extra = _unwrap_api_error(e)
        res["craft_missing"] = {"ms": round(dt), "code": code, "extra": extra}
        print(f"  {code} ({dt:.0f}ms) extra={json.dumps(extra, ensure_ascii=False)[:200]}")

    # ---- 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, time.strftime("probe-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {path}")

    print("\n===== 三个决策数 =====")
    st50 = (res.get("status") or {}).get("p50")
    mv_row = res.get("move_20") or {}
    dg_row = res.get("dig") or {}
    mv = mv_row.get("ms") if mv_row.get("ok") else None
    dg = dg_row.get("ms") if dg_row.get("ok") else None
    print(f"T_status   = {st50} ms/次")
    print(f"T_move20   = {mv} ms")
    print(f"T_dig1     = {dg} ms")
    if st50:
        for k in (2, 3, 5):
            print(f"  -> 若每动作配 {k} 次观察: 纯开销 {k * st50 / 1000:.1f}s/动作")
    return 0

# ---------------------------------------------------------------- 官方寻路能力探测（独立于旧基线输出结构）
# 先测无损的 move20 / dig1（采集 C1），再测障碍寻路与会产生摔伤的 pearl
PATH_CASES = ("move20", "dig1", "wall", "gap", "pillar", "climb_y", "water", "pearl")


def path_template() -> dict:
    """坐标来自赛前测试场 API 观察，模板刻意不预置世界坐标。"""
    return {
        "purpose": "precompetition-pathfinding-only",
        "notes": "只用于赛前已获准的测试场；每个 case 的 start/target 必须来自当前 API 观察。",
        "cases": {name: {
            "start": None, "target": None, "witness_blocks": [],
            "description": {
                "wall": "石墙两侧，witness_blocks 列出挡路的 stone；记录是否绕行或挖开",
                "gap": "有安全底板的坑两侧，witness_blocks 列出待填的空气格；包内有垫块",
                "pillar": "同 x/z 高处目标，witness_blocks 列出待搭柱空气格；包内有垫块",
                "climb_y": "现成阶梯/坡道顶部，目标 y 高于起点；与 pillar 分开测",
                "water": "浅水池内起点到池外安全岸边；witness_blocks 含水格",
                "pearl": "可见的宽大安全落地区，目标为瞄准点；包内有 ender_pearl",
                "move20": "平坦已观察通路，水平距离严格 20 格",
                "dig1": "可达的单块 stone，脚下及承重块除外；记录工具",
            }[name],
        } for name in PATH_CASES},
    }


def validate_fixture(fixture: dict, selected: list[str]) -> None:
    import math
    if fixture.get("purpose") != "precompetition-pathfinding-only":
        raise ValueError("fixture 必须标记 purpose=precompetition-pathfinding-only")
    def pos_ok(pos):
        return (isinstance(pos, list) and len(pos) == 3
                and all(isinstance(v, (float, int)) and not isinstance(v, bool) and math.isfinite(v) for v in pos))
    for name in selected:
        case = fixture.get("cases", {}).get(name, {})
        if not all(pos_ok(case.get(k)) for k in ("start", "target")):
            raise ValueError(f"{name}: 先用官方 API 观察并填入 start/target [x,y,z]")
        rng = case.get("range")
        if rng is not None and (not isinstance(rng, (int, float)) or isinstance(rng, bool) or not math.isfinite(rng) or rng <= 0):
            raise ValueError(f"{name}: range 必须为正有限数")
        witnesses = case.get("witness_blocks")
        if not isinstance(witnesses, list) or not all(pos_ok(v) for v in witnesses):
            raise ValueError(f"{name}: witness_blocks 必须为坐标数组")
        if name in ("wall", "gap", "pillar", "water") and not witnesses:
            raise ValueError(f"{name}: 必须提供要对比的 witness_blocks")
        start, target = case["start"], case["target"]
        if name == "move20" and not math.isclose(math.hypot(target[0]-start[0], target[2]-start[2]), 20, abs_tol=0.01):
            raise ValueError("move20 起点到目标的水平距离必须为 20 格")
        if name in ("pillar", "climb_y") and target[1] <= start[1]:
            raise ValueError(f"{name}: 目标必须高于起点")
        if name == "pillar" and start[::2] != target[::2]:
            raise ValueError("pillar 必须同 x/z，避免把爬坡当成搭柱")


def pathfinding_main(argv: list[str]) -> int:
    import argparse
    import math
    from datetime import datetime, timezone
    from pathlib import Path
    from .client import DEFAULT_BASE
    from .runtime import Safety, SafetyMonitor

    parser = argparse.ArgumentParser(description="赛前官方寻路 probe；默认离线，不登录。")
    parser.add_argument("--pathfinding", action="store_true")
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--case", choices=PATH_CASES, action="append", dest="cases")
    parser.add_argument("--dry", action="store_true")
    parser.add_argument("--snapshot", action="store_true", help="只读采集当前坐标与周边方块并输出夹具草稿（不移动、不挖放）")
    parser.add_argument("--live", action="store_true", help="已获人类账号登录授权并在 docs/05 占位后使用")
    parser.add_argument("--local-target", action="store_true", help="仅允许联调，永不计入官方结论")
    parser.add_argument("--void-guard", action="store_true", help="显式对 gap/pillar 开启虚空支撑护栏标定（默认关闭防误掐寻路）")
    parser.add_argument("--samples", type=int, default=5)
    parser.add_argument("--pearl-trials", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    selected = args.cases or list(PATH_CASES)
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
    ex = Executor(c, state)
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
                if name in ("gap", "pillar") and any(n not in ("air", "cave_air", "void_air") for n in names):
                    raise ValueError("坑/柱待填格不是空气，夹具需重置")
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
                    void_guard=bool(args.void_guard and name in ("gap", "pillar")),
                )
                action = "dig" if name == "dig1" else "use_item" if name == "pearl" else "move_to"
                action_args = ({"pos": case["target"]} if name == "dig1" else
                               {"item": "ender_pearl", "target": {"pos": case["target"]}} if name == "pearl" else
                               {"pos": case["target"], "rng": rng})
                row["action"] = {"name": action, "args": action_args}
                phase = "action"
                epoch_before = ex._preempt_epoch
                t = time.monotonic()
                try:
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
                    row["walked_horizontal"] = math.hypot(actual[0]-before_pos[0], actual[2]-before_pos[2])

                if name == "pearl":
                    row["teleported"] = (row.get("result") or {}).get("teleported")
                    row["status"] = "observed" if (row["api_ok"] and row["teleported"] is True) else "inconclusive"
                elif name in ("move20", "dig1"):
                    row["status"] = "observed" if row["api_ok"] else "inconclusive"
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


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
