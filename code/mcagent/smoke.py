"""D0 冒烟测试：登录 -> 等 ready -> 读状态 -> 拉一次事件。

跑通这个就说明：TLS/代理/token/ready 状态机/响应封装 全线通了。
    python -m mcagent.smoke
    python -m mcagent.smoke --dry    # 不登录，只验证端点可达
"""
from __future__ import annotations

import sys
import time

from .client import ApiError, Client, Config
from .runtime import State

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str]) -> int:
    cfg = Config.load()
    print(f"base      : {cfg.base}")
    print(f"凭据      : {'已配置' if cfg.has_credentials else '未配置'}")
    print(f"代理      : {cfg.proxy or '直连'}")

    if "--dry" in argv:
        c = Client(cfg)
        t0 = time.time()
        code = c.probe()
        dt = (time.time() - t0) * 1000
        print(f"探测结果  : {code}  ({dt:.0f}ms)")
        return 0 if code in ("unauthorized", "invalid_token") else 1

    if not cfg.has_credentials:
        print("\n没有凭据。先 cp .env.example .env，填 MC_USER / MC_PASS。")
        print("账号去 https://skin.nmo.net.cn/ 注册，并在里面先选定角色（否则 no_profile）。")
        return 2

    c = Client(cfg)
    s = State()

    print("\n[1] 登录")
    t0 = time.time()
    try:
        c.login()
    except ApiError as e:
        print(f"  失败: {e}")
        if e.code == "no_profile":
            print("  -> 皮肤站账号下没有选定角色，去 skin.nmo.net.cn 选一个")
        if e.code == "rate_limited":
            print("  -> 登录失败限流：同 IP 10 次/分锁 60s，等一会，别连点")
        return 3
    print(f"  成功 ({time.time() - t0:.1f}s) bot={c.bot_username}")

    print("\n[2] 等 ready（登录后要过代理大厅，5-15s）")
    t0 = time.time()
    try:
        st = c.wait_ready(timeout=120)
    except ApiError as e:
        print(f"  失败: {e}")
        return 4
    s.set_status(st)
    print(f"  ready=True ({time.time() - t0:.1f}s)")
    print(f"  {s.summary()}")
    print(f"  gamemode={st.get('gamemode')} dimension={st.get('dimension')} biome={st.get('biome')}")
    print(f"  armor={st.get('armor_points')} xp={st.get('xp_level')} food={st.get('food')}")

    print("\n[3] 背包")
    inv = c.inventory()
    s.set_inventory(inv)
    items = inv.get("items") or []
    print(f"  {len(items)} 格有物品，空位 {inv.get('empty_slots')}")
    for it in items[:10]:
        print(f"    {it.get('name')} x{it.get('count')} (slot {it.get('slot')})")

    print("\n[4] 事件流")
    t0 = time.time()
    evs = s.drain(c)
    print(f"  拉到 {len(evs)} 条，last_seq={s.last_seq} ({(time.time() - t0) * 1000:.0f}ms)")
    for e in evs[-5:]:
        print(f"    seq={e.get('seq')} {e.get('type')} {({k: v for k, v in e.items() if k not in ('seq', 't', 'type')})}")

    print("\n[5] 附近东西")
    try:
        r = c.find_blocks(radius=16, limit=8)
        blocks = r.get("blocks")
        if isinstance(blocks, dict):
            print(f"  附近种类: {list(blocks)[:12]}")
        else:
            for b in (blocks or [])[:5]:
                print(f"    {b.get('name')} @ {b.get('pos')} dist={b.get('dist')}")
    except ApiError as e:
        print(f"  find_blocks 失败: {e}")

    print("\n冒烟通过。下一步：python -m mcagent.probe")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
