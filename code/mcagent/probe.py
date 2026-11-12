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
from .runtime import State

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe")


def bench(fn: Callable[[], object], n: int = 10, warmup: int = 1) -> dict:
    for _ in range(warmup):
        try:
            fn()
        except ApiError:
            pass
    lat, errs = [], 0
    for _ in range(n):
        t0 = time.time()
        try:
            fn()
            lat.append((time.time() - t0) * 1000)
        except ApiError:
            errs += 1
            lat.append((time.time() - t0) * 1000)
        time.sleep(0.05)
    return {
        "n": n,
        "err": errs,
        "p50": round(statistics.median(lat), 1) if lat else None,
        "min": round(min(lat), 1) if lat else None,
        "max": round(max(lat), 1) if lat else None,
        "all": [round(x) for x in lat],
    }


def main(argv: list[str]) -> int:
    do_dig = "--no-dig" not in argv
    do_move = "--no-move" not in argv

    cfg = Config.load()
    if not cfg.has_credentials:
        print("没有凭据，先填 mcagent/.env")
        return 2

    c = Client(cfg)
    s = State()
    res: dict = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "base": cfg.base}

    c.login()
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
            r = c.move_to(x + 20, z, rng=1)
            dt = time.time() - t0
            res["move_20"] = {"ms": round(dt * 1000), "ok": True, "moved_to": r.get("pos")}
            print(f"  耗时 {dt:.2f}s -> {r.get('pos')}  (≈{20 / max(dt, 0.01):.1f} 格/秒)")
        except ApiError as e:
            res["move_20"] = {"ok": False, "code": e.code, "ms": round((time.time() - t0) * 1000)}
            print(f"  失败: {e}")
        time.sleep(1)
        try:
            r = c.move_to(s.pos[0] - 20, s.pos[2], rng=1) if False else c.move_to(x, z, rng=1)
            print(f"  回位 -> {r.get('pos')}")
        except ApiError as e:
            print(f"  回位失败: {e}")

    # ---- 5. 动作类：挖一块
    if do_dig:
        print("\n[5] dig 一块（挑最近的 dirt/stone/grass）")
        target = None
        for types in (["dirt", "grass_block", "coarse_dirt"], ["stone", "cobblestone"], ["oak_log", "birch_log"]):
            try:
                r = c.find_blocks(types=types, radius=12, limit=3)
                blocks = r.get("blocks") or []
                if isinstance(blocks, list) and blocks:
                    target = blocks[0]
                    break
            except ApiError as e:
                print(f"  find {types} 失败: {e}")
        if target:
            print(f"  目标 {target['name']} @ {target['pos']} dist={target.get('dist')}")
            t0 = time.time()
            try:
                r = c.dig(target["pos"])
                dt = time.time() - t0
                res["dig"] = {"ms": round(dt * 1000), "ok": True, "dig_ms": r.get("dig_ms"), "gained": r.get("gained")}
                print(f"  耗时 {dt:.2f}s  服务端 dig_ms={r.get('dig_ms')}  gained={r.get('gained')}")
            except ApiError as e:
                res["dig"] = {"ok": False, "code": e.code}
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
            c.posture(sneak=True)
            c.posture(sneak=False)
            s.set_status(c.status())
            cycles.append((time.time() - t0) * 1000)
        except ApiError as e:
            print(f"  失败: {e}")
            break
        time.sleep(0.1)
    if cycles:
        res["cycle"] = {"p50": round(statistics.median(cycles), 1), "all": [round(x) for x in cycles]}
        print(f"  p50={res['cycle']['p50']}ms  -> 纯开销；真实每动作 = 这个 + 动作本身")

    # ---- 7. 错误路径的延迟（缺材料合成）
    print("\n[7] 错误路径（craft 缺材料）")
    try:
        t0 = time.time()
        c.craft("diamond_pickaxe")
        print("  意外成功？")
    except ApiError as e:
        dt = (time.time() - t0) * 1000
        res["craft_missing"] = {"ms": round(dt), "code": e.code, "extra": e.extra}
        print(f"  {e.code} ({dt:.0f}ms) extra={json.dumps(e.extra, ensure_ascii=False)[:200]}")

    # ---- 落盘
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, time.strftime("probe-%Y%m%d-%H%M%S.json"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(res, f, ensure_ascii=False, indent=2)
    print(f"\n结果已写入 {path}")

    print("\n===== 三个决策数 =====")
    st50 = (res.get("status") or {}).get("p50")
    mv = (res.get("move_20") or {}).get("ms")
    dg = (res.get("dig") or {}).get("ms")
    print(f"T_status   = {st50} ms/次")
    print(f"T_move20   = {mv} ms")
    print(f"T_dig1     = {dg} ms")
    if st50:
        for k in (2, 3, 5):
            print(f"  -> 若每动作配 {k} 次观察: 纯开销 {k * st50 / 1000:.1f}s/动作")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
