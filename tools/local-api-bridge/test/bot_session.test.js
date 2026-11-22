"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { Vec3 } = require("vec3");
const { BotSession, buildPathfinderMovements } = require("../lib/bot_session");

function readySession() {
  const session = new BotSession({ username: "test" });
  const bot = new EventEmitter();
  const keys = new Map();
  bot.entity = { id: 1, position: new Vec3(0.5, 64, 0.5), yaw: 0, pitch: 0, onGround: true };
  bot.health = 20;
  bot.entities = { 1: bot.entity };
  bot.inventory = { slots: [], items: () => [] };
  bot.pathfinder = { setGoal() {} };
  bot.world = { raycast: () => null };
  bot.look = async (yaw, pitch) => Object.assign(bot.entity, { yaw, pitch });
  bot.lookAt = async (point) => { bot.lookTarget = point; };
  bot.setControlState = (name, value) => keys.set(name, value);
  bot.getControlState = (name) => keys.get(name) ?? false;
  bot.clearControlStates = () => keys.clear();
  bot.stopDigging = () => {};
  bot.deactivateItem = () => {};
  bot.blockAt = () => null;
  session.bot = bot;
  session.connected = true;
  session.spawned = true;
  return session;
}

test("look bypasses a running control action; stop releases its keys and restores jump posture", async () => {
  const session = readySession();
  const { bot } = session;
  assert.deepEqual(await session.posture({ jump: true }), {
    sneaking: false, jumping: true, blocking: false, offhand: null,
  });
  assert.equal(session.getStatus().jumping, true);

  const running = session.control({ forward: true, ms: 3000 });
  assert.equal(bot.getControlState("forward"), true);
  await assert.rejects(session.control({ left: true }), { code: "busy" });
  assert.deepEqual(await session.look({ yaw: 1.5, pitch: 0.1 }), { yaw: 1.5, pitch: 0.1 });
  await session.look({ pos: [0.25, 64.75, 0.5] });
  assert.deepEqual(bot.lookTarget, new Vec3(0.25, 64.75, 0.5));

  const stopped = await session.stop();
  assert.equal(stopped.action, "control");
  assert.equal(stopped.released, true);
  assert.equal(session.busy, null);
  assert.equal(bot.getControlState("forward"), false);
  assert.equal(bot.getControlState("jump"), true);
  assert.equal((await running).interrupted_by, "stop");

  const posture = await session.posture({ sneak: true });
  assert.equal(posture.jumping, false);
  assert.equal(bot.getControlState("jump"), false);
  assert.equal(bot.getControlState("sneak"), true);
});

test("stop keeps the action lock until the interrupted action finishes cleanup", async () => {
  const session = readySession();
  let finishAction;
  const running = session._runAction("test", [], () => new Promise((resolve) => {
    finishAction = resolve;
  }));
  const actionError = assert.rejects(running, { code: "interrupted", extra: {
    cause: "stop", health: 20, pos: [0.5, 64, 0.5],
  } });
  const stopping = session.stop();
  await actionError;
  assert.equal(session.busy, "test");
  await assert.rejects(session.control({ jump: true }), { code: "busy" });
  finishAction();
  assert.equal((await stopping).released, true);
  assert.equal(session.busy, null);
});

test("move_to returns stalled when its pathfinder watchdog sees no movement", async (t) => {
  const session = readySession();
  let now = 10000;
  t.mock.method(Date, "now", () => now);
  let watchdog;
  t.mock.method(global, "setInterval", (fn) => { watchdog = fn; });
  session.bot.setControlState("forward", true);
  const action = session.moveTo({ x: 30, z: 30 });
  const result = assert.rejects(action, { code: "stalled" });
  now += 8000;
  watchdog();
  await result;
  assert.equal(session.busy, null);
  assert.equal(session.bot.getControlState("forward"), false);

  session.bot.setControlState("forward", true);
  const noPathAction = session.moveTo({ x: 40, z: 40 });
  const noPathResult = assert.rejects(noPathAction, { code: "path_not_found" });
  session.bot.emit("path_update", { status: "noPath", path: [] });
  await noPathResult;
  assert.equal(session.bot.getControlState("forward"), false);
});

test("control hits a visible boat and stops at intervening blocks", async () => {
  const session = readySession();
  const { bot } = session;
  bot.entity.eyeHeight = 0.4;
  const boat = { id: 2, name: "oak_boat", type: "object", position: new Vec3(0.5, 64, -1.5), width: 1.375, height: 0.5625 };
  bot.entities[2] = boat;
  const attacks = [];
  bot.attack = (entity) => attacks.push(entity.id);
  let swings = 0;
  bot.swingArm = () => { swings += 1; };

  const hit = await session.control({ hit: true });
  assert.equal(hit.hits[0].entity.id, boat.id);
  assert.deepEqual(attacks, [boat.id]);

  bot.world.raycast = () => ({ name: "stone", position: new Vec3(0, 64, -1), intersect: new Vec3(0.5, 64.4, 0) });
  const miss = await session.control({ hit: true });
  assert.equal(miss.hits[0].entity, null);
  assert.deepEqual(miss.hits[0].block, { name: "stone", pos: [0, 64, -1] });
  assert.equal(swings, 1);
  assert.deepEqual(attacks, [boat.id]);
  await assert.rejects(session.control({ hit: 2, hit_interval_ms: 100, ms: 100 }), { code: "bad_args" });
  await assert.rejects(session.control({ jump: true, ms: 3001 }), { code: "bad_args" });
});

test("attack respects max_hits and accepts an interval override", async () => {
  const session = readySession();
  const { bot } = session;
  bot.entities[2] = { id: 2, isValid: true, health: 20, position: new Vec3(0.5, 64, -1.5) };
  let hits = 0;
  bot.attack = () => { hits += 1; };
  const result = await session.attack({ entity_id: 2, max_hits: 2, interval_ms: 50 });
  assert.equal(hits, 2);
  assert.equal(result.hits, 2);
  assert.equal(result.killed, false);
  await assert.rejects(session.attack({ entity_id: 2, max_hits: 0 }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 2, interval_ms: 5001 }), { code: "bad_args" });
});

test("buildPathfinderMovements disables sprinting, parkour, towers, and hard-block breaking", () => {
  class FakeMovements {
    constructor() {
      this.allowSprinting = true;
      this.allowParkour = true;
      this.allow1by1towers = true;
      this.scafoldingBlocks = [1, 2];
      this.blocksCantBreak = new Set();
    }
  }
  const fakeBot = {
    registry: {
      blocksArray: [
        { id: 10, name: "short_grass", hardness: 0 },
        { id: 11, name: "oak_leaves", hardness: 0.2 },
        { id: 12, name: "stone", hardness: 1.5 },
      ],
    },
  };
  const moves = buildPathfinderMovements(fakeBot, FakeMovements);
  assert.equal(moves.canDig, true);
  assert.equal(moves.digCost, 8);
  assert.equal(moves.allow1by1towers, false);
  assert.equal(moves.allowParkour, false);
  assert.equal(moves.allowSprinting, false);
  assert.deepEqual(moves.scafoldingBlocks, []);
  assert.equal(moves.blocksCantBreak.has(10), false);
  assert.equal(moves.blocksCantBreak.has(11), false);
  assert.equal(moves.blocksCantBreak.has(12), true);
});

test("windowView and windowAct classify mineflayer windowOpen timeout as retryable window_timeout", async () => {
  const session = readySession();
  const { bot } = session;
  bot.blockAt = (pos) => ({ name: "furnace", position: pos });
  bot.openFurnace = async () => {
    throw new Error("Event windowOpen did not fire within timeout of 20000ms");
  };
  await assert.rejects(
    session.windowView({ target: { pos: [1, 64, 0] } }),
    { code: "window_timeout" }
  );
  await assert.rejects(
    session.windowAct({ target: { pos: [1, 64, 0] }, ops: [{ op: "take", from: "output" }] }),
    { code: "window_timeout" }
  );

  bot.blockAt = (pos) => ({ name: "chest", position: pos });
  bot.openContainer = async () => {
    throw new Error("Event windowOpen did not fire within timeout of 20000ms");
  };
  await assert.rejects(
    session.windowView({ target: { pos: [1, 64, 0] } }),
    { code: "window_timeout" }
  );

  bot.entities[9] = { id: 9, name: "villager", position: new Vec3(1.5, 64, 0.5) };
  bot.openVillager = async () => {
    throw new Error("Event windowOpen did not fire within timeout of 20000ms");
  };
  await assert.rejects(
    session.windowView({ target: { entity_id: 9 } }),
    { code: "window_timeout" }
  );
});

test("craft classifies mineflayer updateSlot timeout as retryable window_timeout", async () => {
  const session = readySession();
  const { bot } = session;
  bot.registry = {
    itemsByName: { oak_planks: { id: 5, name: "oak_planks" }, oak_log: { id: 17, name: "oak_log" } },
    items: { 5: { id: 5, name: "oak_planks" }, 17: { id: 17, name: "oak_log" } },
    blocksByName: {},
    recipes: { 5: [{ inShape: [[17]], result: { id: 5, count: 4 } }] },
  };
  bot.recipesFor = () => [{ result: { id: 5, count: 4 }, delta: [{ id: 17, count: -1 }] }];
  bot.craft = async () => {
    throw new Error("Event updateSlot:0 did not fire within timeout of 20000ms");
  };
  await assert.rejects(
    session.craft({ item: "oak_planks", count: 4 }),
    { code: "window_timeout" }
  );
  assert.equal(session.busy, null);
});

test("craft aborts promptly on stop, closes open window, and releases busy lock", async () => {
  const session = readySession();
  const { bot } = session;
  let closedWin = null;
  bot.currentWindow = { id: 3, title: "Crafting" };
  bot.closeWindow = (w) => {
    closedWin = w;
    bot.currentWindow = null;
  };
  bot.registry = {
    itemsByName: { oak_planks: { id: 5, name: "oak_planks" }, oak_log: { id: 17, name: "oak_log" } },
    items: { 5: { id: 5, name: "oak_planks" }, 17: { id: 17, name: "oak_log" } },
    blocksByName: {},
    recipes: { 5: [{ inShape: [[17]], result: { id: 5, count: 4 } }] },
  };
  bot.recipesFor = () => [{ result: { id: 5, count: 4 }, delta: [{ id: 17, count: -1 }] }];
  // Simulate a hung Mineflayer bot.craft() that never resolves on its own
  bot.craft = () => new Promise(() => {});

  const craftPromise = session.craft({ item: "oak_planks", count: 4 });
  const craftRejected = assert.rejects(craftPromise, { code: "interrupted" });
  assert.equal(session.busy, "craft");

  const stopped = await session.stop();
  assert.equal(stopped.action, "craft");
  assert.equal(stopped.released, true);
  assert.equal(session.busy, null);
  assert.ok(closedWin && closedWin.id === 3, "expected open crafting window to be closed on abort");
  await craftRejected;
});

test("stop() and disconnect() release busy mutex when Mineflayer dig() hangs forever", async () => {
  const session = readySession();
  const { bot } = session;
  bot._client = { on() {}, removeListener() {} };
  const fakeBlock = { name: "stone", type: 1, stateId: 1, diggable: true, boundingBox: "block", position: new Vec3(1, 64, 0) };
  bot.blockAt = () => fakeBlock;
  bot.canDigBlock = () => true;
  // Simulate Mineflayer bot.dig() hanging forever after stopDigging()/quit()
  bot.dig = () => new Promise(() => {});

  const digPromise = session.dig({ pos: [1, 64, 0] });
  const digRejected = assert.rejects(digPromise, { code: "interrupted" });
  assert.equal(session.busy, "dig");

  const stopped = await session.stop();
  assert.equal(stopped.action, "dig");
  assert.equal(stopped.released, true);
  assert.equal(session.busy, null);
  await digRejected;

  // Also test disconnect() during a hung dig() leaves session.busy === null for reconnect
  const digPromise2 = session.dig({ pos: [1, 64, 0] });
  const digRejected2 = assert.rejects(digPromise2, { code: "not_connected" });
  assert.equal(session.busy, "dig");
  session.disconnect();
  assert.equal(session.busy, null);
  await digRejected2;
});

test("useItem(food) maps Mineflayer consume 'Promise timed out.' to timeout (not bad_args)", async () => {
  const session = readySession();
  const { bot } = session;
  bot.food = 14;
  bot.health = 10;
  let deactivated = false;
  bot.deactivateItem = () => { deactivated = true; };
  bot.equip = async () => {};
  bot.inventory.items = () => [{ name: "chicken", count: 2, slot: 36 }];
  bot.consume = async () => {
    throw new Error("Promise timed out.");
  };

  await assert.rejects(
    session.useItem({ item: "chicken" }),
    (err) => err && err.code === "timeout"
  );
  assert.equal(deactivated, true);
  assert.equal(session.busy, null);
});

test("control wait:false returns immediately, exposes held_controls, supports takeover and empty release (v1.3.14)", async () => {
  const session = readySession();
  const { bot } = session;

  const r1 = await session.control({ forward: true, sprint: true, ms: 500, wait: false });
  assert.deepEqual(r1.held, ["forward", "sprint"]);
  assert.equal(r1.release_in_ms, 500);
  assert.equal(session.busy, null);
  assert.equal(bot.getControlState("forward"), true);
  assert.equal(bot.getControlState("sprint"), true);
  const st1 = session.getStatus();
  assert.deepEqual(st1.held_controls.keys, ["forward", "sprint"]);
  assert.ok(st1.held_controls.release_in_ms > 0 && st1.held_controls.release_in_ms <= 500);

  // New control immediately takes over and releases previous keys
  const r2 = await session.control({ left: true, ms: 500, wait: false });
  assert.deepEqual(r2.held, ["left"]);
  assert.equal(bot.getControlState("forward"), false);
  assert.equal(bot.getControlState("sprint"), false);
  assert.equal(bot.getControlState("left"), true);

  // Empty {"wait": false} immediately releases held keys
  const r3 = await session.control({ wait: false });
  assert.deepEqual(r3.held, []);
  assert.deepEqual(r3.released, ["left"]);
  assert.equal(bot.getControlState("left"), false);
  assert.equal(session.getStatus().held_controls, null);
});

test("chat runs concurrently alongside blocking actions and does not release held controls (v1.3.19)", async () => {
  const session = readySession();
  const { bot } = session;
  const sent = [];
  bot.chat = (msg) => sent.push(msg);

  // 1) Chat while a blocking action holds busy lock
  let finishAction;
  const blocking = session._runAction("move_to", [], () => new Promise((resolve) => {
    finishAction = resolve;
  }));
  assert.equal(session.busy, "move_to");
  const chatRes1 = await session.chat({ message: "hello while moving" });
  assert.deepEqual(chatRes1, { sent: true });
  assert.equal(session.busy, "move_to");
  finishAction();
  await blocking;

  // 2) Chat while /control wait:false keys are held must NOT release them
  session.lastChatAt = 0;
  await session.control({ forward: true, ms: 1000, wait: false });
  assert.equal(bot.getControlState("forward"), true);
  const chatRes2 = await session.chat({ message: "still walking" });
  assert.deepEqual(chatRes2, { sent: true });
  assert.equal(bot.getControlState("forward"), true);
  assert.deepEqual(session.getStatus().held_controls.keys, ["forward"]);
  await session.stop();
  assert.equal(bot.getControlState("forward"), false);
  assert.deepEqual(sent, ["hello while moving", "still walking"]);

  // 3) Chat still rejects slash commands and dead bots
  session.lastChatAt = 0;
  await assert.rejects(session.chat({ message: "/tp 0 64 0" }), { code: "bad_args" });
  bot.health = 0;
  await assert.rejects(session.chat({ message: "dead chat" }), { code: "dead" });
});

test("observation endpoints return not_ready while world/inventory are missing during reconnect (v1.3.13)", async () => {
  const session = readySession();
  session.bot.world = null;
  assert.throws(() => session.getInventory(), { code: "not_ready" });
  assert.throws(() => session.getBlock({ pos: [0, 64, 0] }), { code: "not_ready" });
  assert.throws(() => session.getEntities({}), { code: "not_ready" });
  await assert.rejects(session.findBlocks({ radius: 4 }), { code: "not_ready" });
});

test("findBlocks scans spherical exposed blocks asynchronously via stateId and rejects unknown blocks (v1.3.13)", async () => {
  const session = readySession();
  const { bot } = session;
  bot.game = { minY: 0, height: 128 };
  const airBlock = { id: 0, name: "air", boundingBox: "empty", transparent: true, minStateId: 0, maxStateId: 0 };
  const stoneBlock = { id: 1, name: "stone", boundingBox: "block", transparent: false, minStateId: 1, maxStateId: 1 };
  const oreBlock = { id: 2, name: "diamond_ore", boundingBox: "block", transparent: false, minStateId: 2, maxStateId: 2 };
  bot.registry = {
    blocksArray: [airBlock, stoneBlock, oreBlock],
    blocksByName: { air: airBlock, stone: stoneBlock, diamond_ore: oreBlock },
    blocksByStateId: { 0: airBlock, 1: stoneBlock, 2: oreBlock },
  };
  const blocks = new Map([
    ["2,64,2", 2], // diagonal exposed diamond_ore within radius 4
    ["1,64,0", 1], // exposed stone
  ]);
  const fakeCol = {
    getBlockStateId(p) {
      return blocks.get(`${p.x},${p.y},${p.z}`) ?? 0;
    },
  };
  bot.world = {
    raycast: () => null,
    getColumn: (cx, cz) => (cx === 0 && cz === 0 ? fakeCol : null),
  };
  bot.blockAt = (pos) => {
    const sid = blocks.get(`${pos.x},${pos.y},${pos.z}`) ?? 0;
    const def = bot.registry.blocksByStateId[sid];
    return { ...def, position: pos, getProperties: () => ({}) };
  };

  const detail = await session.findBlocks({ types: ["diamond_ore"], radius: 4 });
  assert.equal(detail.total, 1);
  assert.deepEqual(detail.blocks[0].pos, [2, 64, 2]);

  const summary = await session.findBlocks({ radius: 4 });
  assert.equal(summary.scanned, 2);
  assert.equal(summary.blocks.diamond_ore.count, 1);
  assert.equal(summary.blocks.stone.count, 1);

  await assert.rejects(session.findBlocks({ types: ["nonexistent_block"], radius: 4 }), { code: "bad_args" });
});
