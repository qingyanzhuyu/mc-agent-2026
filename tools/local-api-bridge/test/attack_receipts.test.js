"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const { Vec3 } = require("vec3");
const { BotSession } = require("../lib/bot_session");

function makeAttackSession({ targetPos, blockAtCursor = () => null }) {
  let goalCalls = 0;
  let attacks = 0;
  const lookTargets = [];
  const cursorDists = [];
  const session = new BotSession({ username: "fighter", log: () => {} });
  const bot = new EventEmitter();
  bot.health = 18.4;
  bot.heldItem = { name: "iron_sword" };
  bot.entity = { id: 1, position: new Vec3(0, 64, 0), eyeHeight: 1.62, yaw: 0, pitch: 0, onGround: true };
  bot.entities = {
    1: bot.entity,
    42: { id: 42, name: "zombie", isValid: true, height: 1.95, width: 0.6, position: targetPos },
  };
  bot.inventory = { slots: [], items: () => [{ name: "iron_sword", count: 1 }] };
  bot.equip = async () => {};
  bot.lookAt = async (pos) => { lookTargets.push(pos.clone()); };
  bot.attack = () => { attacks += 1; };
  bot.blockAtCursor = (maxDist) => {
    cursorDists.push(maxDist);
    return blockAtCursor(maxDist, lookTargets[lookTargets.length - 1]);
  };
  bot.pathfinder = { setGoal: () => { goalCalls += 1; } };
  bot.setControlState = () => {};
  bot.getControlState = () => false;
  bot.clearControlStates = () => {};
  bot.stopDigging = () => {};
  bot.deactivateItem = () => {};
  bot.blockAt = () => null;
  bot.world = { raycast: () => null };
  session.bot = bot;
  session.connected = true;
  session.spawned = true;
  return { session, bot, getGoalCalls: () => goalCalls, getAttacks: () => attacks, lookTargets, cursorDists };
}

test("attack with chase=false and feedback=true stands ground, records hit_log, and emits attack_hit", async () => {
  const { session, getGoalCalls, getAttacks } = makeAttackSession({ targetPos: new Vec3(1.8, 64, 0) });
  const res = await session.attack({ entity_id: 42, max_hits: 2, interval_ms: 50, chase: false, feedback: true });
  assert.equal(getGoalCalls(), 0);
  assert.equal(getAttacks(), 2);
  assert.equal(res.hits, 2);
  assert.equal(res.stop_reason, "max_hits");
  assert.equal(res.hit_log.length, 2);
  assert.equal(res.hit_log[0].hit, 1);
  assert.equal(res.hit_log[0].entity_id, 42);
  assert.equal(res.hit_log[0].bot_hp, 18.4);
  const attackEvents = session.getEvents().events.filter((e) => e.type === "attack_hit");
  assert.equal(attackEvents.length, 2);
});

test("attack with chase=false respects timeout window and returns out_of_reach without pathfinding", async () => {
  const { session, getGoalCalls, getAttacks } = makeAttackSession({ targetPos: new Vec3(6, 64, 0) });
  const res = await session.attack({ entity_id: 42, max_hits: 1, timeout: 120, chase: false });
  assert.equal(getGoalCalls(), 0);
  assert.equal(getAttacks(), 0);
  assert.equal(res.hits, 0);
  assert.equal(res.stop_reason, "out_of_reach");
  assert.ok(res.ms >= 100 && res.ms < 1000);
});

test("attack with chase=false returns blocked_by_wall when solid block occludes target", async () => {
  const { session, getAttacks } = makeAttackSession({
    targetPos: new Vec3(2.2, 64, 0),
    blockAtCursor: () => ({ name: "stone", boundingBox: "block", position: new Vec3(1, 65, 0), intersect: new Vec3(1.0, 65.62, 0) }),
  });
  const res = await session.attack({ entity_id: 42, max_hits: 1, chase: false });
  assert.equal(getAttacks(), 0);
  assert.equal(res.hits, 0);
  assert.equal(res.stop_reason, "blocked_by_wall");
  assert.deepEqual(res.blocking_block, { name: "stone", pos: [1, 65, 0] });
});

test("legacy attack call without chase/feedback preserves exact legacy response and GoalFollow behavior", async () => {
  const { session, getGoalCalls, getAttacks } = makeAttackSession({ targetPos: new Vec3(1.8, 64, 0) });
  const res = await session.attack({ entity_id: 42, max_hits: 1, interval_ms: 50 });
  assert.equal(getGoalCalls(), 2); // setGoal(GoalFollow) + setGoal(null) in finally
  assert.equal(getAttacks(), 1);
  assert.equal(res.hits, 1);
  assert.equal(res.interval_ms, 50);
  assert.equal(res.hit_log, undefined);
  assert.equal(res.stop_reason, undefined);
  assert.equal(session.getEvents().events.filter((e) => e.type === "attack_hit").length, 0);
});

test("attack falls back to 0.5-height ray when 0.8-height ray is occluded and aligns aim/distance", async () => {
  const targetPos = new Vec3(2.2, 64, 0);
  const eye = new Vec3(0, 65.62, 0);
  const { session, getAttacks, lookTargets, cursorDists } = makeAttackSession({
    targetPos,
    blockAtCursor: (_maxDist, aim) => {
      if (aim && aim.y > 65.3) {
        return { name: "stone_slab", boundingBox: "block", position: new Vec3(1, 65, 0), intersect: new Vec3(1.0, 65.58, 0) };
      }
      return null;
    },
  });
  const res = await session.attack({ entity_id: 42, max_hits: 1, interval_ms: 50, chase: false, feedback: true });
  assert.equal(getAttacks(), 1);
  assert.equal(res.hits, 1);
  assert.equal(res.stop_reason, "max_hits");
  assert.equal(res.hit_log[0].occluded, undefined);
  assert.equal(lookTargets.length, 2);
  assert.equal(cursorDists.length, 2);
  assert.ok(Math.abs(cursorDists[0] - eye.distanceTo(lookTargets[0])) < 1e-6);
  assert.ok(Math.abs(cursorDists[1] - eye.distanceTo(lookTargets[1])) < 1e-6);
});

test("attack with chase=true and feedback=true keeps GoalFollow and records occluded swing receipts", async () => {
  const { session, getGoalCalls, getAttacks } = makeAttackSession({
    targetPos: new Vec3(2.2, 64, 0),
    blockAtCursor: () => ({ name: "stone", boundingBox: "block", position: new Vec3(1, 65, 0), intersect: new Vec3(1.0, 65.62, 0) }),
  });
  const res = await session.attack({ entity_id: 42, max_hits: 1, interval_ms: 50, chase: true, feedback: true });
  assert.equal(getGoalCalls(), 2);
  assert.equal(getAttacks(), 1);
  assert.equal(res.hits, 1);
  assert.equal(res.stop_reason, "max_hits");
  assert.equal(res.hit_log.length, 1);
  assert.equal(res.hit_log[0].occluded, true);
  const attackEvents = session.getEvents().events.filter((e) => e.type === "attack_hit");
  assert.equal(attackEvents.length, 1);
  assert.equal(attackEvents[0].occluded, true);
});

test("attack rejects invalid chase, feedback, and timeout arguments with bad_args", async () => {
  const { session } = makeAttackSession({ targetPos: new Vec3(1.8, 64, 0) });
  await assert.rejects(session.attack({ entity_id: 42, chase: "false" }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, feedback: 1 }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, chase: false, timeout: 10 }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, chase: false, timeout: 99999 }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, chase: false, timeout: Number.NaN }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, chase: false, timeout: "500" }), { code: "bad_args" });
  await assert.rejects(session.attack({ entity_id: 42, chase: false, timeout: null }), { code: "bad_args" });
});

test("attack does not falsely flag a wall behind the target hitbox as occluded", async () => {
  const { session, getAttacks } = makeAttackSession({
    targetPos: new Vec3(2.0, 64, 0),
    blockAtCursor: () => ({ name: "stone", boundingBox: "block", position: new Vec3(2, 65, 0), intersect: new Vec3(2.5, 65.58, 0) }),
  });
  const res = await session.attack({ entity_id: 42, max_hits: 1, interval_ms: 50, chase: false, feedback: true });
  assert.equal(getAttacks(), 1);
  assert.equal(res.hits, 1);
  assert.equal(res.stop_reason, "max_hits");
  assert.equal(res.hit_log[0].occluded, undefined);
});

test("attack stops cleanly without phantom swing if target vanishes during lookAt", async () => {
  const { session, bot, getAttacks } = makeAttackSession({ targetPos: new Vec3(1.8, 64, 0) });
  bot.lookAt = async () => { delete bot.entities[42]; };
  const res = await session.attack({ entity_id: 42, max_hits: 1, chase: false, feedback: true });
  assert.equal(getAttacks(), 0);
  assert.equal(res.hits, 0);
  assert.equal(res.killed, true);
  assert.equal(res.stop_reason, "killed");
  assert.equal(res.hit_log.length, 0);
  assert.equal(session.getEvents().events.filter((e) => e.type === "attack_hit").length, 0);
});
