"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const mcDataFactory = require("minecraft-data");
const { patchBrokenPickaxeMaterials } = require("../lib/bot_session");

test("patchBrokenPickaxeMaterials restores mineable/pickaxe on 1.21.11 incorrect_for_* blocks", () => {
  const mcData = mcDataFactory("1.21.11");
  const pickaxeSpeeds = mcData.materials["mineable/pickaxe"];
  assert.ok(pickaxeSpeeds, "mineable/pickaxe material table should exist");

  const diamondPickId = mcData.itemsByName.diamond_pickaxe.id;
  const ironPickId = mcData.itemsByName.iron_pickaxe.id;
  assert.equal(pickaxeSpeeds[diamondPickId], 8);
  assert.equal(pickaxeSpeeds[ironPickId], 6);

  patchBrokenPickaxeMaterials(mcData);

  for (const name of [
    "obsidian",
    "crying_obsidian",
    "ancient_debris",
    "diamond_ore",
    "deepslate_diamond_ore",
    "iron_ore",
    "deepslate_iron_ore",
    "gold_ore",
    "redstone_ore",
    "lapis_ore",
  ]) {
    const blk = mcData.blocksByName[name];
    assert.ok(blk, `${name} should exist`);
    assert.equal(blk.material, "mineable/pickaxe", `${name} material should be mineable/pickaxe`);
  }

  assert.equal(mcData.blocksByName.oak_log.material, "mineable/axe");
  assert.equal(mcData.blocksByName.dirt.material, "mineable/shovel");
});

test("patchBrokenPickaxeMaterials is safe on null or registries without mineable/pickaxe", () => {
  assert.equal(patchBrokenPickaxeMaterials(null), 0);
  assert.equal(patchBrokenPickaxeMaterials({ materials: {} }), 0);
});
