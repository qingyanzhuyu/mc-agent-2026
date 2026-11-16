"use strict";

const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const test = require("node:test");
const mineflayer = require("mineflayer");
const { Vec3 } = require("vec3");
const { BotSession } = require("../lib/bot_session");

function makeSession(t) {
  const bot = Object.assign(new EventEmitter(), {
    _client: new EventEmitter(),
    entity: { id: 22, position: new Vec3(0, 64, 0) },
    entities: {},
    health: 20,
    blockAt: () => null,
    getControlState: () => false,
    loadPlugin: () => {},
  });
  t.mock.method(mineflayer, "createBot", () => bot);
  const session = new BotSession({ username: "stub-bot", log: () => {} });
  session._startBot();
  return { bot, session };
}

test("status reports last physics tick displacement and resets on position discontinuities", (t) => {
  const { bot, session } = makeSession(t);
  const expectMotion = (speed, velocity) => {
    const status = session.getStatus();
    assert.equal(status.speed, speed);
    assert.deepEqual(status.velocity, velocity);
  };

  expectMotion(null, null);
  bot.emit("spawn");
  bot.emit("physicsTick");
  expectMotion(null, null);
  bot.entity.position.set(0.12, 63.85, 0.16);
  bot.emit("physicsTick");
  expectMotion(4, [2.4, -3, 3.2]);

  bot.entity.position.set(700, 90, -950);
  bot.emit("forcedMove");
  expectMotion(null, null);
  bot.emit("physicsTick");
  expectMotion(null, null);
  bot.emit("physicsTick");
  expectMotion(0, [0, 0, 0]);

  bot.vehicle = { id: 33, name: "boat" };
  bot.emit("mount");
  bot.emit("physicsTick");
  expectMotion(null, null);
  bot.vehicle = null;
  bot.emit("dismount");
  bot.emit("physicsTick");
  expectMotion(null, null);
  bot.emit("physicsTick");
  expectMotion(0, [0, 0, 0]);

  bot.emit("respawn");
  expectMotion(null, null);
  bot.emit("spawn");
  bot.emit("physicsTick");
  bot.emit("physicsTick");
  expectMotion(0, [0, 0, 0]);
  bot.health = 0;
  bot.emit("death");
  expectMotion(null, null);
});

test("hurt events attribute actual health loss to matching self damage packets", (t) => {
  const { bot, session } = makeSession(t);
  bot.emit("spawn");
  bot.entities[50] = { id: 50, name: "zombie", position: new Vec3(1, 64, 0) };
  bot._client.emit("registry_data", {
    id: "minecraft:damage_type",
    entries: [{ key: "minecraft:drown" }, { key: "minecraft:mob_attack" }],
  });

  const damage = (sourceTypeId, sourceCauseId, entityId = bot.entity.id) => {
    bot._client.emit("damage_event", { entityId, sourceTypeId, sourceCauseId, sourceDirectId: 0 });
  };
  const setHealth = (health) => {
    bot.health = health;
    bot.emit("health");
  };

  damage(0, 0);
  damage(1, 51, 50); // Entity 50 took damage; ignore it for the bot's health change.
  setHealth(18);
  setHealth(17); // Missing metadata must not reuse the prior damage packet.
  damage(1, 51);
  setHealth(13);
  damage(999, 0);
  setHealth(12);

  const hurt = session.getEvents().events.filter((event) => event.type === "hurt");
  assert.deepEqual(hurt.map(({ health, damage, source, source_entity }) => [health, damage, source, source_entity]), [
    [18, 2, "drown", undefined],
    [17, 1, undefined, undefined],
    [13, 4, "mob_attack", { id: 50, name: "zombie" }],
    [12, 1, undefined, undefined],
  ]);
  assert.equal(hurt[0].likely_attacker.id, 50);
});
