"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { Vec3 } = require("vec3");
const mcData = require("minecraft-data")("1.21.11");
const Chunk = require("prismarine-chunk")("1.21.11");
const Block = require("prismarine-block")("1.21.11");
const { BotSession, scanExposed } = require("../lib/bot_session");

function makeWorldSession(columns, pos) {
  const getColumn = (cx, cz) => columns.get(`${cx},${cz}`) || null;
  const bot = new EventEmitter();
  bot.registry = mcData;
  bot.game = { minY: -64, height: 384 };
  bot.entity = { id: 1, position: new Vec3(pos[0], pos[1], pos[2]), onGround: true };
  bot.inventory = { slots: [], items: () => [] };
  bot.world = { getColumn };
  bot.blockAt = (p) => {
    const x = Math.floor(p.x);
    const y = Math.floor(p.y);
    const z = Math.floor(p.z);
    if (y < -64 || y >= 320) return null;
    const col = getColumn(x >> 4, z >> 4);
    if (!col) return null;
    const sid = col.getBlockStateId({ x: x & 15, y, z: z & 15 });
    const b = Block.fromStateId(sid, 0);
    b.position = new Vec3(x, y, z);
    return b;
  };
  const session = new BotSession({ username: "scanner", log: () => {} });
  session.bot = bot;
  session.connected = true;
  session.spawned = true;
  return session;
}

function setBlock(columns, x, y, z, stateId) {
  const cx = x >> 4;
  const cz = z >> 4;
  const key = `${cx},${cz}`;
  let col = columns.get(key);
  if (!col) {
    col = new Chunk({ minY: -64, worldHeight: 384 });
    columns.set(key, col);
  }
  col.setBlockStateId(new Vec3(x & 15, y, z & 15), stateId);
}

function referenceScan(session, radius, want, max) {
  const bot = session.bot;
  const me = bot.entity.position;
  const minY = bot.game.minY;
  const maxY = bot.game.minY + bot.game.height - 1;
  const see = new Uint8Array(Math.max(...bot.registry.blocksArray.map((b) => b.maxStateId)) + 1);
  for (const b of bot.registry.blocksArray) {
    if (b.boundingBox === "empty" || b.transparent) see.fill(1, b.minStateId, b.maxStateId + 1);
  }
  const sid = (x, y, z) => {
    if (y < minY || y > maxY) return -1;
    const c = bot.world.getColumn(x >> 4, z >> 4);
    if (!c) return -1;
    return c.getBlockStateId({ x: x & 15, y, z: z & 15 });
  };
  const open = (x, y, z) => { const v = sid(x, y, z); return v >= 0 && see[v] === 1; };
  const out = [];
  const r = Math.ceil(radius) + 1;
  const cx = Math.floor(me.x);
  const cy = Math.floor(me.y);
  const cz = Math.floor(me.z);
  for (let x = cx - r; x <= cx + r; x += 1) {
    for (let z = cz - r; z <= cz + r; z += 1) {
      const dxz = (x - me.x) ** 2 + (z - me.z) ** 2;
      if (dxz > radius * radius) continue;
      for (let y = Math.max(cy - r, minY); y <= Math.min(cy + r, maxY); y += 1) {
        const d2 = dxz + (y - me.y) ** 2;
        if (d2 > radius * radius) continue;
        const v = sid(x, y, z);
        if (v < 0 || !want(v)) continue;
        if (
          open(x + 1, y, z) || open(x - 1, y, z) ||
          open(x, y + 1, z) || open(x, y - 1, z) ||
          open(x, y, z + 1) || open(x, y, z - 1)
        ) {
          out.push([x, y, z, Math.sqrt(d2), v]);
        }
      }
    }
  }
  out.sort((a, b) => a[3] - b[3]);
  return out.slice(0, max);
}

test("findBlocks with types supports radius up to 128 on negative coords and clamps >128 (v1.3.20)", async () => {
  const columns = new Map();
  const cane = mcData.blocksByName.sugar_cane.defaultState;
  const stone = mcData.blocksByName.stone.defaultState;
  setBlock(columns, -12216, 80, -12232, cane); // ~79.3 blocks
  setBlock(columns, -12160, 80, -12279, cane); // ~119.5 blocks
  setBlock(columns, -12140, 80, -12279, cane); // ~139.5 blocks (> 128)
  // Completely enclosed buried cane must NOT be returned
  for (let dx = -1; dx <= 1; dx += 1) {
    for (let dy = -1; dy <= 1; dy += 1) {
      for (let dz = -1; dz <= 1; dz += 1) {
        setBlock(columns, -12270 + dx, 70 + dy, -12270 + dz, stone);
      }
    }
  }
  setBlock(columns, -12270, 70, -12270, cane);

  const session = makeWorldSession(columns, [-12279.5, 80, -12279.5]);

  const r64 = await session.findBlocks({ types: ["sugar_cane"], radius: 64 });
  assert.equal(r64.total, 0);

  const r128 = await session.findBlocks({ types: ["sugar_cane"], radius: 128 });
  assert.equal(r128.total, 2);
  assert.deepEqual(r128.blocks.map((b) => b.pos), [[-12216, 80, -12232], [-12160, 80, -12279]]);

  const r200 = await session.findBlocks({ types: ["sugar_cane"], radius: 200 });
  assert.equal(r200.total, 2, "radius > 128 should clamp to 128");

  const summary = await session.findBlocks({ radius: 128 });
  assert.equal(summary.radius, 64);
  assert.equal(summary.blocks.sugar_cane, undefined);

  await assert.rejects(session.findBlocks({ types: "sugar_cane" }), { code: "bad_args" });
  await assert.rejects(session.findBlocks({ types: ["sugar_cane"], radius: 0 }), { code: "bad_args" });
  await assert.rejects(session.findBlocks({ properties: { age: 7 } }), { code: "bad_args" });
});

test("scanExposed matches brute-force scan across single, indirect, direct, and legacy chunks (v1.3.20)", async () => {
  const columns = new Map();
  const stone = mcData.blocksByName.stone.defaultState;
  const glass = mcData.blocksByName.glass.defaultState;
  const gold = mcData.blocksByName.gold_block.defaultState;
  const diamond = mcData.blocksByName.diamond_ore.defaultState;

  const col0 = new Chunk({ minY: -64, worldHeight: 384 });
  const stoneSecIdx = (0 - (-64)) >> 4;
  const glassSecIdx = (16 - (-64)) >> 4;
  col0.sections[stoneSecIdx] = new Chunk.section({ noSizePrefix: true, singleValue: stone, maxBitsPerBlock: col0.maxBitsPerBlock });
  col0.sections[glassSecIdx] = new Chunk.section({ noSizePrefix: true, singleValue: glass, maxBitsPerBlock: col0.maxBitsPerBlock });
  col0.setBlockStateId(new Vec3(3, 35, 4), gold);
  col0.setBlockStateId(new Vec3(15, 32, 0), diamond);
  columns.set("0,0", col0);

  const colDirect = new Chunk({ minY: -64, worldHeight: 384 });
  let placed = 0;
  for (let ly = 0; ly < 16 && placed < 300; ly += 1) {
    for (let lz = 0; lz < 16 && placed < 300; lz += 1) {
      for (let lx = 0; lx < 16 && placed < 300; lx += 1) {
        const b = mcData.blocksArray[placed % mcData.blocksArray.length];
        colDirect.setBlockStateId(new Vec3(lx, ly, lz), b.defaultState);
        placed += 1;
      }
    }
  }
  colDirect.setBlockStateId(new Vec3(14, 15, 14), gold);
  assert.equal(colDirect.sections[stoneSecIdx].data.palette, undefined);
  columns.set("-1,0", colDirect);

  const legacyMap = new Map();
  legacyMap.set("2,10,2", gold);
  legacyMap.set("2,11,2", diamond);
  columns.set("1,0", {
    getBlockStateId(p) { return legacyMap.get(`${p.x},${p.y},${p.z}`) || 0; },
  });

  const session = makeWorldSession(columns, [2.3, 14.7, 3.8]);

  const wantSparse = (v) => v === gold || v === diamond;
  const gotSparse = await scanExposed(session, 24, wantSparse, 100);
  const refSparse = referenceScan(session, 24, wantSparse, 100);
  assert.deepEqual(gotSparse.map((x) => [x[0], x[1], x[2], x[4]]), refSparse.map((x) => [x[0], x[1], x[2], x[4]]));

  const wantStone = (v) => v === stone;
  const gotStone = await scanExposed(session, 20, wantStone, 50);
  const refStone = referenceScan(session, 20, wantStone, 50);
  assert.equal(gotStone.length, refStone.length);
  assert.deepEqual(gotStone.map((x) => +x[3].toFixed(6)), refStone.map((x) => +x[3].toFixed(6)));

  const wantGlass = (v) => v === glass;
  const gotGlass = await scanExposed(session, 10, wantGlass, 40);
  const refGlass = referenceScan(session, 10, wantGlass, 40);
  assert.equal(gotGlass.length, refGlass.length);
  assert.deepEqual(gotGlass.map((x) => +x[3].toFixed(6)), refGlass.map((x) => +x[3].toFixed(6)));
});

test("scanExposed radius=128 over 17x17 chunks is fast for sparse and dense queries (v1.3.20)", async () => {
  const columns = new Map();
  const stone = mcData.blocksByName.stone.defaultState;
  const grass = mcData.blocksByName.grass_block.defaultState;
  const dirt = mcData.blocksByName.dirt.defaultState;
  const cane = mcData.blocksByName.sugar_cane.defaultState;

  for (let cx = -8; cx <= 8; cx += 1) {
    for (let cz = -8; cz <= 8; cz += 1) {
      const col = new Chunk({ minY: -64, worldHeight: 384 });
      for (let si = 0; si < 8; si += 1) {
        col.sections[si] = new Chunk.section({ noSizePrefix: true, singleValue: stone, maxBitsPerBlock: col.maxBitsPerBlock });
      }
      for (let lx = 0; lx < 16; lx += 1) {
        for (let lz = 0; lz < 16; lz += 1) {
          col.setBlockStateId(new Vec3(lx, 64, lz), dirt);
          col.setBlockStateId(new Vec3(lx, 65, lz), grass);
        }
      }
      columns.set(`${cx},${cz}`, col);
    }
  }
  setBlock(columns, 80, 66, 12, cane);
  setBlock(columns, -110, 66, -20, cane);

  const session = makeWorldSession(columns, [0.5, 66, 0.5]);

  const tSparse = Date.now();
  const sparse = await session.findBlocks({ types: ["sugar_cane"], radius: 128 });
  const sparseMs = Date.now() - tSparse;
  assert.equal(sparse.total, 2);
  assert.ok(sparseMs < 300, `expected sparse radius=128 scan < 300ms, got ${sparseMs}ms`);

  const tDense = Date.now();
  const dense = await session.findBlocks({ types: ["grass_block"], radius: 128, limit: 20 });
  const denseMs = Date.now() - tDense;
  assert.equal(dense.blocks.length, 20);
  assert.ok(denseMs < 600, `expected dense radius=128 scan < 600ms, got ${denseMs}ms`);
});
