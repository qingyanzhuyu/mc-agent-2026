#!/usr/bin/env python3
"""由官方数据包生成 docs/09-进度树与判据表.md。

用法（在仓库根目录）：
    python3 docs/99-原始记录/gen_criteria.py            # 用已归档的 criteria-1.21.11.json 重新生成 docs/09
    python3 docs/99-原始记录/gen_criteria.py --fetch    # 先从 misode/mcmeta 重新拉判据，再生成

数据源：misode/mcmeta 的 `1.21.11-data` 标签（原版数据包镜像，不是赛事服务器实测）。
分值来自同目录的 rules-v2-draft.md。中文判据是对官方 JSON 的人工转述，
有疑义时以 criteria-1.21.11.json 里的 `criteria` / `requirements` 原文为准。
"""
import concurrent.futures as cf
import json
import pathlib
import re
import sys
import urllib.request

HERE = pathlib.Path(__file__).resolve().parent
REF = "1.21.11-data"
RAW = "https://raw.githubusercontent.com/misode/mcmeta/%s/data/minecraft/advancement/%s.json"
TREE_JSON = HERE / "advancements-1.21.11.json"
CRIT_JSON = HERE / "criteria-1.21.11.json"
RULES = HERE / "rules-v2-draft.md"
OUT = HERE.parent / "09-进度树与判据表.md"
TABS = ["story", "nether", "end", "adventure", "husbandry"]
TAB_ZH = {"story": "Story 主线", "nether": "Nether 下界", "end": "End 末地",
          "adventure": "Adventure 冒险", "husbandry": "Husbandry 农牧"}

# id -> (中文判据, capability)。判据逐条对照 criteria-1.21.11.json 写成。
ZH = {
    # ---- story
    "story/root": ("背包里出现工作台", "none"),
    "story/mine_stone": ("背包里出现圆石 / 黑石 / 深板岩圆石任一", "dig"),
    "story/upgrade_tools": ("背包里出现石镐", "craft"),
    "story/smelt_iron": ("背包里出现铁锭（来源不限）", "smelt"),
    "story/iron_tools": ("背包里出现铁镐", "craft"),
    "story/obtain_armor": ("背包里出现任意一件铁甲（四选一，**不要求穿上**）", "craft"),
    "story/lava_bucket": ("背包里出现岩浆桶", "interact"),
    "story/deflect_arrow": ("用盾牌格挡住一次弹射物伤害", "defend"),
    "story/form_obsidian": ("背包里出现黑曜石（来源不限：挖、箱子、猪灵交易）", "dig"),
    "story/mine_diamond": ("背包里出现钻石（来源不限）", "dig"),
    "story/enchant_item": ("在附魔台完成一次附魔", "enchant"),
    "story/shiny_gear": ("背包里出现任意一件钻石甲（四选一，**不要求穿上**）", "craft"),
    "story/enter_the_nether": ("切换维度到下界", "travel"),
    "story/cure_zombie_villager": ("由你喂下金苹果的僵尸村民完成转化（它须先带虚弱效果）", "state"),
    "story/follow_ender_eye": ("身处主世界要塞结构范围内（**不要求用过末影之眼**）", "travel"),
    "story/enter_the_end": ("切换维度到末地", "travel"),
    # ---- nether
    "nether/root": ("切换维度到下界", "travel"),
    "nether/return_to_sender": ("用恶魂自己的火球击杀恶魂", "combat"),
    "nether/find_bastion": ("身处堡垒遗迹结构范围内", "travel"),
    "nether/obtain_ancient_debris": ("背包里出现远古残骸（来源不限，一块即可）", "dig"),
    "nether/fast_travel": ("借下界通行，使主世界水平位移 ≥7000 格", "travel"),
    "nether/find_fortress": ("身处下界要塞结构范围内", "travel"),
    "nether/obtain_crying_obsidian": ("背包里出现哭泣的黑曜石", "dig"),
    "nether/distract_piglin": ("**未穿任何金甲**时：丢出猪灵喜爱的物品被成年猪灵捡起，或直接对成年猪灵使用金锭", "interact"),
    "nether/ride_strider": ("骑着炽足兽时，诡异菌钓竿的耐久发生变化（即用它加速一次）；只是骑上不算", "interact"),
    "nether/uneasy_alliance": ("在**主世界**击杀一只恶魂（`ghast`，快乐恶魂不算）", "combat"),
    "nether/loot_bastion": ("打开堡垒遗迹四种战利品箱中的任意一个", "interact"),
    "nether/netherite_armor": ("背包里**同时**有下界合金头盔、胸甲、护腿、靴子四件", "craft"),
    "nether/get_wither_skull": ("背包里出现凋灵骷髅头颅", "combat"),
    "nether/obtain_blaze_rod": ("背包里出现烈焰棒", "combat"),
    "nether/charge_respawn_anchor": ("用荧石给重生锚充能，且充到 **4 格满**", "interact"),
    "nether/explore_nether": ("到访下界全部 5 个群系", "travel"),
    "nether/ride_strider_in_overworld_lava": ("在主世界骑炽足兽，在岩浆上水平移动 ≥50 格", "interact"),
    "nether/summon_wither": ("召唤出凋灵（不要求击杀）", "build"),
    "nether/brew_potion": ("从酿造台取出一瓶酿造完成的药水（任意种类）", "brew"),
    "nether/create_beacon": ("激活信标（金字塔 ≥1 层）", "build"),
    "nether/all_potions": ("同一时刻身上带齐 17 种指定效果（含风袭、蛛网、渗浆、寄生、抗性）；不是酿造清单", "state"),
    "nether/create_full_beacon": ("信标金字塔达到 4 层", "build"),
    "nether/all_effects": ("同一时刻身上带齐 34 种指定效果", "state"),
    # ---- end
    "end/root": ("切换维度到末地", "travel"),
    "end/kill_dragon": ("击杀末影龙", "combat"),
    "end/dragon_egg": ("背包里出现龙蛋", "interact"),
    "end/enter_end_gateway": ("进入末地折跃门方块", "travel"),
    "end/respawn_dragon": ("召唤末影龙", "build"),
    "end/dragon_breath": ("背包里出现龙息", "interact"),
    "end/find_end_city": ("身处末地城结构范围内", "travel"),
    "end/elytra": ("背包里出现鞘翅", "travel"),
    "end/levitate": ("在漂浮效果下竖直上升 ≥50 格（只是被潜影贝打中不算）", "state"),
    # ---- adventure
    "adventure/root": ("击杀任意实体，或被任意实体击杀", "combat"),
    "adventure/voluntary_exile": ("击杀一只头戴不祥旗帜的袭击者（巡逻队 / 前哨站队长）", "combat"),
    "adventure/spyglass_at_parrot": ("用望远镜看向鹦鹉", "interact"),
    "adventure/kill_a_mob": ("击杀 41 种敌对生物中的任意一种", "combat"),
    "adventure/read_power_of_chiseled_bookshelf": ("比较器输入端紧贴雕纹书架（两者先后放置均可，朝向须对），不要求书架里有书", "build"),
    "adventure/trade": ("与村民或流浪商人完成一次交易", "trade"),
    "adventure/trim_with_any_armor_pattern": ("在锻造台用 18 种纹饰模板中的任意一种完成一次盔甲纹饰", "craft"),
    "adventure/honey_block_slide": ("贴着蜂蜜块下滑", "travel"),
    "adventure/ol_betsy": ("用弩射出一发", "ranged"),
    "adventure/lightning_rod_with_villager_no_fire": ("村民在场时避雷针引雷，且闪电未引燃任何方块", "build"),
    "adventure/fall_from_world_height": ("从 y≥319 自由下落到 y≤−59 并存活", "travel"),
    "adventure/salvage_sherd": ("刷出任一考古战利品，且背包里出现陶片", "interact"),
    "adventure/avoid_vibration": ("在幽匿感测体 / 尖啸体 / 监守者的感知范围内潜行，避免被探测", "stealth"),
    "adventure/sleep_in_bed": ("躺进床（进入睡眠状态即触发，不必睡到天亮）", "interact"),
    "adventure/hero_of_the_village": ("赢得一场袭击", "combat"),
    "adventure/spyglass_at_ghast": ("用望远镜看向恶魂", "interact"),
    "adventure/throw_trident": ("投出的三叉戟击中任意实体", "ranged"),
    "adventure/kill_mob_near_sculk_catalyst": ("在幽匿催发体附近击杀一只生物", "combat"),
    "adventure/shoot_arrow": ("射出的箭击中任意实体（弓、弩都算）", "ranged"),
    "adventure/kill_all_mobs": ("41 种敌对生物全部击杀过", "collect"),
    "adventure/totem_of_undying": ("不死图腾生效一次", "state"),
    "adventure/summon_iron_golem": ("召唤出铁傀儡", "build"),
    "adventure/trade_at_world_height": ("玩家在 y≥319 处完成一次交易", "trade"),
    "adventure/trim_with_all_exclusive_armor_patterns": ("用 8 种指定纹饰模板各完成一次纹饰", "collect"),
    "adventure/two_birds_one_arrow": ("弩射出的**同一支箭**击杀两只幻翼", "ranged"),
    "adventure/whos_the_pillager_now": ("用弩射出的箭击杀一只掠夺者", "ranged"),
    "adventure/arbalistic": ("弩射出的**同一支箭**击杀 5 种不同类型的实体", "ranged"),
    "adventure/craft_decorated_pot_using_only_sherds": ("用 4 片陶片合成饰纹陶罐", "craft"),
    "adventure/adventuring_time": ("到访 54 个指定群系", "collect"),
    "adventure/play_jukebox_in_meadows": ("在草甸群系里对唱片机使用唱片", "interact"),
    "adventure/walk_on_powder_snow_with_leather_boots": ("穿着皮革靴踩在细雪上", "equip"),
    "adventure/spyglass_at_dragon": ("用望远镜看向末影龙", "interact"),
    "adventure/very_very_frightening": ("引雷三叉戟召来的闪电击中村民", "combat"),
    "adventure/sniper_duel": ("用弹射物击杀骷髅，击杀瞬间**水平**距离 ≥50 格", "ranged"),
    "adventure/bullseye": ("弹射物从**水平** ≥30 格外击中标靶并输出信号强度 15（正中心）", "ranged"),
    "adventure/minecraft_trials_edition": ("身处试炼密室结构范围内", "travel"),
    "adventure/under_lock_and_key": ("对普通宝库使用试炼钥匙", "interact"),
    "adventure/blowback": ("用旋风人自己的风弹击杀旋风人", "combat"),
    "adventure/who_needs_rockets": ("被风弹爆炸推动后下落，竖直距离 ≥7 格（起算点未实测）", "use_item"),
    "adventure/lighten_up": ("用斧头刮一个**点亮的**、已氧化的铜灯（斑驳 / 锈蚀 / 氧化三档，带蜡也算）；普通铜块不算", "interact"),
    "adventure/revaulting": ("对不祥宝库使用不祥试炼钥匙", "interact"),
    "adventure/overoverkill": ("重锤下砸一击造成 ≥100 点伤害", "combat"),
    "adventure/heart_transplanter": ("把嘎吱之心放在两段同轴的苍白橡木原木之间（后补原木也算）", "build"),
    "adventure/crafters_crafting_crafters": ("由合成器合成出一个合成器", "craft"),
    "adventure/use_lodestone": ("手持指南针对磁石使用", "interact"),
    "adventure/brush_armadillo": ("用刷子刷犰狳", "interact"),
    "adventure/spear_many_mobs": ("长矛一次冲锋击中 5 只生物", "combat"),
    # ---- husbandry
    "husbandry/root": ("吃下或喝下任意物品", "interact"),
    "husbandry/safely_harvest_honey": ("蜂巢 / 蜂箱下方有营火烟时，用**玻璃瓶**采蜜", "interact"),
    "husbandry/breed_an_animal": ("繁殖任意一对动物", "interact"),
    "husbandry/allay_deliver_item_to_player": ("捡起悦灵丢给你的物品", "interact"),
    "husbandry/ride_a_boat_with_a_goat": ("坐上一条已载有山羊的船", "interact"),
    "husbandry/tame_an_animal": ("驯服任意动物", "interact"),
    "husbandry/make_a_sign_glow": ("对任意告示牌使用荧光墨囊", "interact"),
    "husbandry/fishy_business": ("用钓鱼竿钓上鳕鱼 / 鲑鱼 / 热带鱼 / 河豚任一（垃圾和宝藏不算）", "fishing"),
    "husbandry/silk_touch_nest": ("用精准采集工具破坏一个**内有 3 只蜜蜂**的天然蜂巢", "dig"),
    "husbandry/tadpole_in_a_bucket": ("用水桶装起一只蝌蚪", "interact"),
    "husbandry/obtain_sniffer_egg": ("背包里出现嗅探兽蛋", "interact"),
    "husbandry/place_dried_ghast_in_water": ("放下的失水恶魂方块处于含水状态", "build"),
    "husbandry/plant_seed": ("种下小麦种子 / 南瓜籽 / 西瓜籽 / 甜菜种子 / 下界疣 / 火把花种子 / 瓶子草荚果任一", "interact"),
    "husbandry/wax_on": ("对未涂蜡的铜质方块使用蜜脾", "interact"),
    "husbandry/bred_all_animals": ("26 种可繁殖动物全部繁殖过", "collect"),
    "husbandry/allay_deliver_cake_to_note_block": ("悦灵把蛋糕丢到音符盒处", "interact"),
    "husbandry/complete_catalogue": ("驯服全部 11 种猫", "collect"),
    "husbandry/tactical_fishing": ("用水桶装起一条**活鱼**（鳕鱼 / 鲑鱼 / 热带鱼 / 河豚）；不是钓鱼", "interact"),
    "husbandry/leash_all_frog_variants": ("对冷 / 温 / 暖三种青蛙各使用一次拴绳（可分次累计）", "interact"),
    "husbandry/feed_snifflet": ("用火把花种子喂幼年嗅探兽", "interact"),
    "husbandry/whole_pack": ("驯服全部 9 种狼", "collect"),
    "husbandry/remove_wolf_armor": ("用剪刀剪下狼身上的狼铠", "interact"),
    "husbandry/repair_wolf_armor": ("用犰狳鳞甲把狼身上受损的狼铠修到满耐久", "interact"),
    "husbandry/balanced_diet": ("40 种食物全部吃过", "collect"),
    "husbandry/obtain_netherite_hoe": ("背包里出现下界合金锄", "craft"),
    "husbandry/wax_off": ("用斧头刮掉铜质方块上的蜡", "interact"),
    "husbandry/axolotl_in_a_bucket": ("用水桶装起一只美西螈", "interact"),
    "husbandry/kill_axolotl_target": ("与美西螈并肩作战后，从它那里获得状态效果", "combat"),
    "husbandry/froglights": ("背包里**同时**有三种颜色的蛙明灯", "collect"),
    "husbandry/plant_any_sniffer_seed": ("种下火把花种子或瓶子草荚果", "interact"),
}


def load_tree():
    """返回 {id: parent_id 或 None}，顺序即官方目录顺序。"""
    data = json.loads(TREE_JSON.read_text(encoding="utf-8"))
    parents = {}

    def walk(node):
        if isinstance(node, dict):
            if isinstance(node.get("id"), str) and "/" in node["id"]:
                parent = node.get("parent")
                parents[node["id"].replace("minecraft:", "")] = (
                    parent.replace("minecraft:", "") if isinstance(parent, str) else None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return parents


def fetch(ids):
    def one(adv_id):
        with urllib.request.urlopen(RAW % (REF, adv_id), timeout=30) as resp:
            raw = json.loads(resp.read().decode("utf-8"))
        return adv_id, {"parent": raw.get("parent", "").replace("minecraft:", "") or None,
                        "criteria": raw["criteria"],
                        "requirements": raw.get("requirements")}

    with cf.ThreadPoolExecutor(8) as pool:
        items = dict(pool.map(one, ids))
    payload = {"ref": REF, "source": "https://github.com/misode/mcmeta/tree/%s/data/minecraft/advancement" % REF,
               "count": len(items), "items": {k: items[k] for k in sorted(items)}}
    lines = ['{"ref": %s, "source": %s, "count": %d, "items": {' % (
        json.dumps(payload["ref"]), json.dumps(payload["source"]), payload["count"])]
    keys = list(payload["items"])
    for i, key in enumerate(keys):
        tail = "," if i < len(keys) - 1 else ""
        lines.append("%s: %s%s" % (json.dumps(key), json.dumps(payload["items"][key], separators=(",", ":"),
                                                               ensure_ascii=False), tail))
    lines.append("}}")
    CRIT_JSON.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_scores():
    scores = {}
    for line in RULES.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\| `minecraft:([a-z_/]+)` \| .+? \| \d \| (\S+) \| (.+?) \|", line)
        if m:
            scores[m.group(1)] = (m.group(2), m.group(3))
    return scores


def triggers(item):
    seen = []
    for crit in item["criteria"].values():
        name = crit["trigger"].replace("minecraft:", "")
        if name not in seen:
            seen.append(name)
    return " / ".join("`%s`" % t for t in seen)


def render(items, scores):
    by_tab = {t: [i for i in items if i.startswith(t + "/")] for t in TABS}
    children = {}
    for adv_id, item in items.items():
        children.setdefault(item["parent"], []).append(adv_id)

    def score_of(adv_id):
        if adv_id.endswith("/root"):
            return "0"
        return scores[adv_id][0]

    out = []
    out.append("# 09 · 进度树与判据表\n")
    out.append("> **本页由脚本生成，不要手改**：`python3 docs/99-原始记录/gen_criteria.py`。")
    out.append("> 判据来自 Minecraft 官方数据包（misode/mcmeta `%s` 标签）里每个 advancement 的" % REF)
    out.append("> `criteria` / `requirements`，原文归档在 `99-原始记录/criteria-1.21.11.json`；")
    out.append("> 分值来自 `99-原始记录/rules-v2-draft.md`。中文判据是人工转述，有疑义以 JSON 原文为准。")
    out.append(">")
    out.append("> 2026-11-13 重写说明：旧版判据是凭记忆写的，核对后发现二十多条有误")
    out.append("> （如下界合金甲写成“任意一件”、战术性钓鱼写成“钓到河豚”），且误称“判据在代码里”。")
    out.append("> 旧版的“深度 / 结构分”两列随 v2 草案取消深度计分一并删除；旧版本见 git 历史。\n")
    out.append("## 一、进度树（缩进 = parent 链，括号里是 v2 草案分值，`—` 为不计分）\n")
    out.append("父节点只表示显示上的归属，**完成子项不要求先完成父项**。\n")
    for tab in TABS:
        out.append("### %s\n" % tab)

        def walk(adv_id, depth):
            out.append("%s- `%s`（%s）" % ("  " * depth, adv_id, score_of(adv_id)))
            for child in children.get(adv_id, []):
                walk(child, depth + 1)

        walk(tab + "/root", 0)
        out.append("")
    out.append("## 二、判据表\n")
    out.append("“触发器”是官方 JSON 里的 `trigger` 名；`inventory_changed` 类只看背包里有没有，")
    out.append("`location` 类只看人站在哪，这两类最容易被想复杂。\n")
    cap_count = {}
    for tab in TABS:
        ids = sorted(by_tab[tab], key=lambda i: (-(int(score_of(i)) if score_of(i).isdigit() else -1), i))
        out.append("### %s（%d 项）\n" % (TAB_ZH[tab], len(ids)))
        out.append("| id | 分值 | 触发器 | 判据 | capability |")
        out.append("|---|---:|---|---|---|")
        for adv_id in ids:
            desc, cap = ZH[adv_id]
            cap_count[cap] = cap_count.get(cap, 0) + 1
            out.append("| `%s` | %s | %s | %s | `%s` |" % (adv_id, score_of(adv_id), triggers(items[adv_id]), desc, cap))
        out.append("")
    out.append("## 三、capability 归并\n")
    out.append("`capability` 是我们自己归的类，用来决定“哪几项共用同一个 skill”（%d 项）：\n" % len(items))
    out.append("| capability | 项数 |")
    out.append("|---|---:|")
    for cap, n in sorted(cap_count.items(), key=lambda kv: (-kv[1], kv[0])):
        out.append("| `%s` | %d |" % (cap, n))
    out.append("")
    out.append("- `collect`（清单 + 去重 + 覆盖检查）是枚举驱动，不是动作驱动，且 v2 草案里这类基本都不计分。")
    out.append("- 其余类别只需要一个 skill 加一张“目标 → 参数”的映射表。\n")
    out.append("## 四、这份表的边界\n")
    out.append("1. **判据成立 ≠ 接口做得到**。本页只回答“游戏怎么算达成”；接口能不能做、值不值得做，见 `docs/11`。")
    out.append("2. **数据源是原版数据包镜像，不是赛事服务器实测**。主办方若用数据包改过判据，以服务器为准。")
    out.append("3. **进度反馈缺口未解**：本地联调网关的 `/advancements` 六种 status 全返 0；")
    out.append("   官方服是否有数据待测（`docs/04` C10）。")
    out.append("4. `who_needs_rockets` 的竖直距离起算点、`ride_strider` 能否由接口触发加速，都需要真机确认（`docs/04` B13）。")
    OUT.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    parents = load_tree()
    if "--fetch" in sys.argv:
        fetch(sorted(parents))
    data = json.loads(CRIT_JSON.read_text(encoding="utf-8"))
    items = {k: data["items"][k] for k in parents if k in data["items"]}
    missing = sorted(set(parents) - set(items))
    unwritten = sorted(set(items) - set(ZH))
    assert not missing, "criteria json 缺项: %s" % missing
    assert not unwritten, "ZH 缺中文判据: %s" % unwritten
    render(items, load_scores())
    print("ok: %d 项 -> %s" % (len(items), OUT.name))


if __name__ == "__main__":
    main()
