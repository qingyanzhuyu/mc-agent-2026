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
