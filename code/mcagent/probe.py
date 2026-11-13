"""D1 基线测量：把"8 小时能排多少动作"这件事量出来。

这三个数是后面所有计划的输入：
  T_status  —— 一次观察的开销
  T_action  —— 一次动作的实际耗时
  叠加规律  —— 一个动作配几次观察才不亏

    python -m mcagent.probe                # 完整
    python -m mcagent.probe --no-dig       # 不动世界
    python -m mcagent.probe --no-move
    python -m mcagent.probe --pathfinding ...   # 转给 probe_path.py（寻路与手动兜底探测）
结果写到 mcagent/probe/probe-YYYYmmdd-HHMMSS.json

本文件只做基线测量。夹具在 probe_fixture.py，寻路实验在 probe_path.py。
"""
from __future__ import annotations

import json
import os
import statistics
import sys
import time
from typing import Callable

from .client import ApiError, Client, Config
from .probe_fixture import OUT_DIR, path_template, validate_fixture  # noqa: F401  后两个为旧导入路径保留
from .probe_path import pathfinding_main
from .runtime import Executor, Fatal, Replan, State
from .skills.base import failure as _unwrap_api_error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


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



if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
