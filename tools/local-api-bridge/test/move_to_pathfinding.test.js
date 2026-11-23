"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const { Vec3 } = require("vec3");
const mcData = require("minecraft-data")("1.21.11");
const { goals } = require("mineflayer-pathfinder");
const {
  BotSession,
  buildPathfinderMovements,
  createSafeGoto,
  EXTRA_AVOID_BLOCKS,
} = require("../lib/bot_session");

function mockMoveSession(pos = [0.5, 64, 0.5]) {
  const bot = new EventEmitter();
  bot.registry = mcData;
  bot.game = { dimension: "overworld", minY: -64, height: 384 };
  bot.health = 20;
  bot.entity = { id: 1, position: new Vec3(pos[0], pos[1], pos[2]), onGround: true, velocity: new Vec3(0, 0, 0), eyeHeight: 1.62, yaw: 0, pitch: 0 };
  bot.entities = { 1: bot.entity };
  bot.inventory = { slots: [], items: () => [] };
  bot.vehicle = null;
  bot.world = { getColumn: () => ({}) };
  bot.blockAt = () => ({ name: "air", boundingBox: "empty" });
  bot.controlState = { forward: false, back: false, left: false, right: false, jump: false, sprint: false, sneak: false };
  bot.lookCalls = [];
  bot.look = (yaw, pitch) => { bot.lookCalls.push({ yaw, pitch }); };
  bot.setControlState = (k, v) => { bot.controlState[k] = v; };
  bot.getControlState = (k) => Boolean(bot.controlState[k]);
  bot.clearControlStates = () => { for (const k of Object.keys(bot.controlState)) bot.controlState[k] = false; };
  bot.stopDigging = () => {};
  bot.deactivateItem = () => {};

  let stateGoal = null;
  let moving = false;
  let movements = { allowParkour: true, allowSprinting: true };
  bot.pathfinder = {
    get goal() { return stateGoal; },
    get movements() { return movements; },
    setMovements(m) { movements = m; bot.emit("path_reset", "movements_updated"); },
    setGoal(g) { stateGoal = g; bot.emit("goal_updated", g, false); },
    isMoving: () => moving,
    isMining: () => false,
    isBuilding: () => false,
    _setMoving(v) { moving = v; },
  };
  bot.pathfinder.goto = createSafeGoto(bot);

  const session = new BotSession({ username: "mover", log: () => {} });
  session.bot = bot;
  session.connected = true;
  session.spawned = true;
  return { session, bot };
}

test("createSafeGoto rejects noPath and timeout with empty path and clears goal (v1.3.21)", async () => {
  for (const status of ["noPath", "timeout"]) {
    const { bot } = mockMoveSession([0.5, 64, 0.5]);
    const goal = new goals.GoalBlock(10, 64, 0);
    const p = bot.pathfinder.goto(goal);
    bot.emit("path_update", { status, path: [] });
    await assert.rejects(p, (err) => err.name === (status === "noPath" ? "NoPath" : "Timeout"));
    assert.equal(bot.pathfinder.goal, null);
  }
});

test("createSafeGoto clears non-empty path on noPath reject and ignores partial empty path (v1.3.21)", async () => {
  const { bot } = mockMoveSession([0.5, 64, 0.5]);
  const goal = new goals.GoalBlock(10, 64, 0);
  const p = bot.pathfinder.goto(goal);
  const results = { status: "noPath", path: [new Vec3(1.5, 64, 0.5), new Vec3(2.5, 64, 0.5)] };
  bot.emit("path_update", results);
  assert.equal(results.path.length, 0);
  await assert.rejects(p, { name: "NoPath" });
  assert.equal(bot.pathfinder.goal, null);

  let settled = false;
  const p2 = bot.pathfinder.goto(goal).then((v) => { settled = true; return v; });
  bot.emit("path_update", { status: "partial", path: [] });
  await new Promise((r) => setTimeout(r, 25));
  assert.equal(settled, false);
  assert.equal(bot.pathfinder.goal, goal);
  bot.entity.position.set(10.5, 64, 0.5);
  bot.emit("goal_reached", goal);
  assert.deepEqual(await p2, { reached: true, partial: false });
  assert.equal(bot.pathfinder.goal, null);
});

test("createSafeGoto allowPartial walks partial path and still rejects empty noPath (v1.3.21)", async () => {
  const { bot } = mockMoveSession([0.5, 64, 0.5]);
  const goal = new goals.GoalBlock(50, 64, 0);
  const p = bot.pathfinder.goto(goal, { allowPartial: true });
  bot.emit("path_update", { status: "noPath", path: [new Vec3(5.5, 64, 0.5), new Vec3(10.5, 64, 0.5)] });
  bot.pathfinder._setMoving(true);
  bot.emit("physicsTick");
  bot.emit("physicsTick");
  bot.entity.position.set(10.5, 64, 0.5);
  bot.pathfinder._setMoving(false);
  bot.emit("physicsTick");
  assert.deepEqual(await p, { reached: false, partial: true, reason: "noPath" });
  assert.equal(bot.pathfinder.goal, null);

  const p2 = bot.pathfinder.goto(goal, { allowPartial: true });
  bot.emit("path_update", { status: "noPath", path: [] });
  await assert.rejects(p2, { name: "NoPath" });
});

test("moveTo range=0 uses GoalBlock and micro-aligns precisely to block center (v1.3.21)", async () => {
  const { session, bot } = mockMoveSession([99.6, 64, 200.5]);
  const seenGoals = [];
  bot.pathfinder.goto = async (goal) => {
    seenGoals.push(goal);
    bot.entity.position.set(100.18, 64, 200.5);
    return { reached: true, partial: false };
  };
  const origSetControl = bot.setControlState;
  let steerTimer = null;
  bot.setControlState = (k, v) => {
    origSetControl(k, v);
    if (k === "forward" && v && !steerTimer) {
      steerTimer = setInterval(() => {
        if (!bot.controlState.forward) return;
        const lastLook = bot.lookCalls.at(-1);
        const step = bot.controlState.sneak ? 0.065 : 0.2;
        bot.entity.position.x += -Math.sin(lastLook.yaw) * step;
        bot.entity.position.z += -Math.cos(lastLook.yaw) * step;
      }, 45);
    }
    if (k === "forward" && !v && steerTimer) { clearInterval(steerTimer); steerTimer = null; }
  };
  bot.clearControlStates = () => {
    for (const k of Object.keys(bot.controlState)) bot.controlState[k] = false;
    if (steerTimer) { clearInterval(steerTimer); steerTimer = null; }
  };

  const res = await session.moveTo({ x: 100, y: 64, z: 200, range: 0 });
  assert.equal(seenGoals.length, 1);
  assert.ok(seenGoals[0] instanceof goals.GoalBlock);
  assert.equal(seenGoals[0].x, 100);
  assert.equal(seenGoals[0].y, 64);
  assert.equal(seenGoals[0].z, 200);
  assert.ok(res.dist <= 0.15, `expected dist <= 0.15, got ${res.dist}`);
  assert.equal(Math.floor(bot.entity.position.x), 100);
});

test("moveTo entity_id defaults item=0.5, mob=2, and honors explicit range (v1.3.21)", async () => {
  const { session, bot } = mockMoveSession([0.5, 64, 0.5]);
  bot.entities[10] = { id: 10, name: "item", position: new Vec3(5.3, 64, 0.5) };
  bot.entities[20] = { id: 20, name: "villager", position: new Vec3(10.5, 64, 0.5) };
  const seen = [];
  bot.pathfinder.goto = async (goal) => {
    seen.push(goal);
    return { reached: true, partial: false };
  };
  bot.look = undefined;

  await session.moveTo({ entity_id: 10 });
  assert.ok(seen[0] instanceof goals.GoalBlock);

  await session.moveTo({ entity_id: 20 });
  assert.ok(seen[1] instanceof goals.GoalNear && seen[1].rangeSq === 4);

  await session.moveTo({ entity_id: 20, range: 0 });
  assert.ok(seen[2] instanceof goals.GoalBlock);

  await session.moveTo({ entity_id: 20, range: 1 });
  assert.ok(seen[3] instanceof goals.GoalNear && seen[3].rangeSq === 1);
});

test("moveTo retries without parkour/sprint after 2 stuck resets and restores afterward (v1.3.21)", async () => {
  const { session, bot } = mockMoveSession([0.5, 64, 0.5]);
  let movUpdates = 0;
  const origSetMov = bot.pathfinder.setMovements.bind(bot.pathfinder);
  bot.pathfinder.setMovements = (m) => { movUpdates += 1; origSetMov(m); };
  const seenWhileRunning = [];
  const origGoto = bot.pathfinder.goto;
  bot.pathfinder.goto = async (goal, opts) => {
    const p = origGoto(goal, opts);
    bot.emit("path_reset", "stuck");
    bot.emit("path_reset", "stuck");
    seenWhileRunning.push({
      parkour: bot.pathfinder.movements.allowParkour,
      sprint: bot.pathfinder.movements.allowSprinting,
    });
    bot.entity.position.set(10.5, 64, 0.5);
    bot.emit("goal_reached", goal);
    return p;
  };

  const res = await session.moveTo({ x: 10, y: 64, z: 0 });
  assert.equal(movUpdates, 1);
  assert.deepEqual(seenWhileRunning[0], { parkour: false, sprint: false });
  assert.equal(bot.pathfinder.movements.allowParkour, true);
  assert.equal(bot.pathfinder.movements.allowSprinting, true);
  assert.equal(res.dist, 0);
});

test("moveTo validates args and attaches target_solid / target_unloaded diagnostics (v1.3.21)", async () => {
  const { session, bot } = mockMoveSession([0.5, 64, 0.5]);
  await assert.rejects(session.moveTo({ x: 5, z: 0, range: -1 }), { code: "bad_args" });
  await assert.rejects(session.moveTo({ x: 5, z: 0, allow_partial: "yes" }), { code: "bad_args" });
  await assert.rejects(session.moveTo({ x: 5, z: 0, timeout: 0 }), { code: "bad_args" });

  bot.blockAt = () => ({ name: "stone", boundingBox: "block" });
  await assert.rejects(
    session.moveTo({ x: 5, y: 64, z: 0, range: 0 }),
    (err) => err.code === "path_not_found" && err.extra.target_solid === true && err.extra.dist_left > 4
  );

  bot.blockAt = () => null;
  const pUnloaded = session.moveTo({ x: 500, y: 64, z: 0 });
  bot.emit("path_update", { status: "noPath", path: [] });
  await assert.rejects(
    pUnloaded,
    (err) => err.code === "path_not_found" && err.extra.target_unloaded === true
  );
});

test("buildPathfinderMovements avoids hazardous blocks (v1.3.21)", () => {
  const { bot } = mockMoveSession();
  const moves = buildPathfinderMovements(bot);
  for (const name of EXTRA_AVOID_BLOCKS) {
    assert.ok(moves.blocksToAvoid.has(mcData.blocksByName[name].id), `expected ${name} in blocksToAvoid`);
  }
});
