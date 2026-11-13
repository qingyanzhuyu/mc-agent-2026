"use strict";

const { Vec3 } = require("vec3");

class ApiBizError extends Error {
  constructor(code, message = "", extra = {}, http = 200) {
    super(message || code);
    this.code = code;
    this.msg = message || code;
    this.extra = extra || {};
    this.http = http;
  }
}

function isFiniteNum(v) {
  return typeof v === "number" && Number.isFinite(v);
}

function round1(n) {
  return Math.round(Number(n) * 10) / 10;
}

function vecToPos(v) {
  if (!v) return [0, 0, 0];
  return [round1(v.x), round1(v.y), round1(v.z)];
}

function vecToBlockPos(v) {
  return [Math.floor(v.x), Math.floor(v.y), Math.floor(v.z)];
}

function parseBlockPos(body, allowMissing = false) {
  if (!body || typeof body !== "object") {
    if (allowMissing) return null;
    throw new ApiBizError("bad_args", "Missing coordinate object");
  }
  if (body.pos !== undefined && body.pos !== null) {
    if (!Array.isArray(body.pos) || body.pos.length !== 3 || !body.pos.every(isFiniteNum)) {
      throw new ApiBizError("bad_args", "pos must be [x, y, z] finite numbers");
    }
    return new Vec3(Math.floor(body.pos[0]), Math.floor(body.pos[1]), Math.floor(body.pos[2]));
  }
  if (body.x !== undefined || body.y !== undefined || body.z !== undefined) {
    if (!isFiniteNum(body.x) || !isFiniteNum(body.y) || !isFiniteNum(body.z)) {
      throw new ApiBizError("bad_args", "x, y, z must be finite numbers");
    }
    return new Vec3(Math.floor(body.x), Math.floor(body.y), Math.floor(body.z));
  }
  if (allowMissing) return null;
  throw new ApiBizError("bad_args", "Expected pos: [x,y,z] or {x,y,z}");
}

function nbtToPlainText(val) {
  if (val == null) return null;
  if (typeof val === "string") return val;
  if (typeof val === "number" || typeof val === "boolean") return String(val);
  if (typeof val === "object") {
    if ("value" in val && Object.keys(val).length <= 2 && "type" in val) {
      return nbtToPlainText(val.value);
    }
    const parts = [];
    if (val.text) parts.push(nbtToPlainText(val.text));
    if (val.translate) parts.push(nbtToPlainText(val.translate));
    if (Array.isArray(val.extra)) {
      for (const ex of val.extra) {
        const s = nbtToPlainText(ex);
        if (s) parts.push(s);
      }
    }
    if (parts.length > 0) return parts.join("");
  }
  return null;
}

const ARMOR_POINTS_BY_NAME = {
  leather_helmet: 1, leather_chestplate: 3, leather_leggings: 2, leather_boots: 1,
  golden_helmet: 2, golden_chestplate: 5, golden_leggings: 3, golden_boots: 1,
  chainmail_helmet: 2, chainmail_chestplate: 5, chainmail_leggings: 4, chainmail_boots: 1,
  iron_helmet: 2, iron_chestplate: 6, iron_leggings: 5, iron_boots: 2,
  diamond_helmet: 3, diamond_chestplate: 8, diamond_leggings: 6, diamond_boots: 3,
  netherite_helmet: 3, netherite_chestplate: 8, netherite_leggings: 6, netherite_boots: 3,
  turtle_helmet: 2,
};

function computeArmorPoints(armorObj) {
  let pts = 0;
  for (const k of ["head", "chest", "legs", "feet"]) {
    const it = armorObj && armorObj[k];
    if (it && it.name && ARMOR_POINTS_BY_NAME[it.name]) {
      pts += ARMOR_POINTS_BY_NAME[it.name];
    }
  }
  return Math.min(20, pts);
}

function serializeItem(it) {
  if (!it) return null;
  const out = {
    name: it.name,
    count: it.count,
    slot: it.slot,
  };
  if (typeof it.maxDurability === "number" && it.maxDurability > 0) {
    const used = typeof it.durabilityUsed === "number" ? it.durabilityUsed : 0;
    out.durability = [Math.max(0, it.maxDurability - used), it.maxDurability];
  }
  if (Array.isArray(it.enchants) && it.enchants.length > 0) {
    out.enchantments = it.enchants.map((e) => ({ name: e.name, level: e.lvl }));
  }
  if (it.customName) {
    const cn = nbtToPlainText(it.customName);
    if (cn) out.custom_name = cn;
  }
  return out;
}

function inventorySummaryMap(bot) {
  const counts = {};
  if (!bot || !bot.inventory) return counts;
  for (const it of bot.inventory.items()) {
    counts[it.name] = (counts[it.name] || 0) + it.count;
  }
  return counts;
}

const CARDINAL_OFFSETS = [
  new Vec3(1, 0, 0),
  new Vec3(-1, 0, 0),
  new Vec3(0, 1, 0),
  new Vec3(0, -1, 0),
  new Vec3(0, 0, 1),
  new Vec3(0, 0, -1),
];

function isAirOrTransparent(block) {
  if (!block) return false;
  if (block.name === "air" || block.name === "cave_air" || block.name === "void_air") {
    return true;
  }
  if (block.transparent) return true;
  if (block.boundingBox !== "block") return true;
  if (block.name === "water" || block.name === "lava") return true;
  return false;
}

function isBlockExposed(bot, pos) {
  for (const off of CARDINAL_OFFSETS) {
    const nb = bot.blockAt(pos.plus(off));
    if (isAirOrTransparent(nb)) return true;
  }
  return false;
}

const CROP_MAX_AGE = {
  wheat: 7,
  carrots: 7,
  potatoes: 7,
  beetroots: 3,
  nether_wart: 3,
  sweet_berry_bush: 3,
  cocoa: 2,
  torchflower_crop: 1,
  pitcher_crop: 4,
};

function getCropInfo(block) {
  if (!block) return null;
  const props = typeof block.getProperties === "function" ? block.getProperties() : {};
  const maxAge = CROP_MAX_AGE[block.name];
  if (maxAge === undefined || typeof props.age !== "number") return null;
  return {
    growth: [props.age, maxAge],
    mature: props.age >= maxAge,
  };
}

const HOSTILE_NAMES = new Set([
  "zombie", "zombie_villager", "husk", "drowned", "skeleton", "stray", "bogged",
  "creeper", "spider", "cave_spider", "enderman", "witch", "slime", "magma_cube",
  "phantom", "pillager", "vindicator", "evoker", "ravager", "vex", "warden",
  "blaze", "ghast", "wither_skeleton", "hoglin", "zoglin", "piglin_brute", "piglin",
  "zombified_piglin", "silverfish", "endermite", "shulker", "guardian", "elder_guardian",
  "breeze", "creaking", "ender_dragon", "wither",
]);

const ANIMAL_NAMES = new Set([
  "cow", "mooshroom", "sheep", "pig", "chicken", "rabbit", "horse", "donkey", "mule",
  "llama", "trader_llama", "wolf", "cat", "ocelot", "parrot", "fox", "bee", "goat",
  "frog", "camel", "sniffer", "armadillo", "turtle", "panda", "polar_bear", "axolotl",
  "squid", "glow_squid", "cod", "salmon", "tropical_fish", "pufferfish", "dolphin",
  "strider", "allay",
]);

function classifyEntity(e) {
  if (!e) return "other";
  if (e.type === "player") return "player";
  const name = (e.name || "").toLowerCase();
  if (name === "item" || e.type === "object" && name === "item") return "item";
  if (e.type === "hostile" || HOSTILE_NAMES.has(name)) return "hostile";
  if (e.type === "animal" || e.type === "passive" || e.type === "water_creature" || ANIMAL_NAMES.has(name)) {
    return "animal";
  }
  return "other";
}

const BLOCK_TARGET_ONLY_ITEMS = new Set([
  "flint_and_steel", "fire_charge", "water_bucket", "lava_bucket", "powder_snow_bucket",
  "bucket", "bone_meal", "wheat_seeds", "beetroot_seeds", "melon_seeds", "pumpkin_seeds",
  "torchflower_seeds", "pitcher_pod", "carrot", "potato", "nether_wart", "cocoa_beans",
  "sweet_berries", "glow_berries",
  "wooden_hoe", "stone_hoe", "iron_hoe", "golden_hoe", "diamond_hoe", "netherite_hoe",
]);

const FOOD_ITEMS = new Set([
  "apple", "golden_apple", "enchanted_golden_apple", "melon_slice", "sweet_berries",
  "glow_berries", "chorus_fruit", "carrot", "golden_carrot", "potato", "baked_potato",
  "poisonous_potato", "beetroot", "dried_kelp", "beef", "cooked_beef", "porkchop",
  "cooked_porkchop", "mutton", "cooked_mutton", "chicken", "cooked_chicken", "rabbit",
  "cooked_rabbit", "cod", "cooked_cod", "salmon", "cooked_salmon", "tropical_fish",
  "pufferfish", "bread", "cookie", "pumpkin_pie", "rotten_flesh", "spider_eye",
  "mushroom_stew", "beetroot_soup", "rabbit_stew", "suspicious_stew", "milk_bucket",
  "honey_bottle", "potion",
]);

const ALWAYS_EDIBLE = new Set([
  "golden_apple", "enchanted_golden_apple", "chorus_fruit", "milk_bucket", "honey_bottle", "potion",
]);

module.exports = {
  ApiBizError,
  isFiniteNum,
  round1,
  vecToPos,
  vecToBlockPos,
  parseBlockPos,
  nbtToPlainText,
  computeArmorPoints,
  serializeItem,
  inventorySummaryMap,
  CARDINAL_OFFSETS,
  isAirOrTransparent,
  isBlockExposed,
  getCropInfo,
  classifyEntity,
  BLOCK_TARGET_ONLY_ITEMS,
  FOOD_ITEMS,
  ALWAYS_EDIBLE,
};
