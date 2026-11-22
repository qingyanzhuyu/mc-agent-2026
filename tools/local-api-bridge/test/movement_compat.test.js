"use strict";

const assert = require("node:assert/strict");
const { EventEmitter, once } = require("node:events");
const test = require("node:test");
const protocol = require("minecraft-protocol");
const registry = require("prismarine-registry");
const injectPhysics = require("mineflayer/lib/plugins/physics");
const { Vec3 } = require("vec3");
const { installMovementCompatibility } = require("../lib/movement_compat");

function fixture(version = "1.21.11") {
  const data = registry(version);
  return Object.assign(new EventEmitter(), {
    version,
    registry: data,
    supportFeature: data.supportFeature,
    _client: Object.assign(new EventEmitter(), { write() {} }),
    entity: { id: 10, position: new Vec3(0, 64, 0), velocity: new Vec3(0.2, 0.1, 0.3), yaw: Math.PI, pitch: 0 },
    entities: {},
  });
}

test("fishing reel pulls only the hooked local player toward the owner", () => {
  const bot = fixture();
  const client = bot._client;
  const bobber = bot.registry.entitiesByName.fishing_bobber;
  const index = bobber.metadataKeys.indexOf("hooked_entity");
  const hook = { name: bobber.name, position: new Vec3(1, 65, 0), metadata: { [index]: bot.entity.id + 2 } };
  bot.entities[11] = hook;
  bot.entities[12] = { position: new Vec3(6, 64, -3) };
  installMovementCompatibility(bot);

  const spawn = { type: bobber.id, entityId: 11, objectData: 12 };
  const reel = () => client.emit("entity_status", { entityId: 11, entityStatus: 31 });
  client.emit("spawn_entity", spawn);
  reel();
  assert.deepEqual(bot.entity.velocity.toArray(), [0.2, 0.1, 0.3]);
  hook.metadata[index] = bot.entity.id + 1;
  reel();
  for (const [axis, expected] of Object.entries({ x: 0.7, y: 0, z: 0 })) {
    assert.ok(Math.abs(bot.entity.velocity[axis] - expected) < 1e-9);
  }

  const velocity = bot.entity.velocity.toArray();
  client.emit("entity_destroy", { entityIds: [11] });
  reel();
  client.emit("spawn_entity", spawn);
  bot.emit("respawn");
  reel();
  client.emit("spawn_entity", spawn);
  client.emit("state", "configuration");
  reel();
  assert.deepEqual(bot.entity.velocity.toArray(), velocity);
});

test("decoded 1.21.11 position packet applies independent velocity flags", async () => {
  const bot = fixture();
  injectPhysics(bot, {});
  installMovementCompatibility(bot);

  const packet = { teleportId: 3, x: 5, y: 0, z: 6, dx: 0.5, dy: 0, dz: 0.7, yaw: 0, pitch: 0, flags: { y: true, dx: true } };
  const serializer = protocol.createSerializer({ state: "play", version: bot.version, isServer: true });
  const deserializer = protocol.createDeserializer({ state: "play", version: bot.version, isServer: false });
  const decoded = once(deserializer, "data");
  serializer.pipe(deserializer);
  serializer.end({ name: "position", params: packet });
  const [{ data }] = await decoded;
  bot._client.emit(data.name, data.params);
  assert.deepEqual(bot.entity.position.toArray(), [5, 64, 6]);
  assert.deepEqual(bot.entity.velocity.toArray(), [0.7, 0, 0.7]);

  bot._client.emit("position", {
    ...packet,
    teleportId: -1,
    x: 0,
    y: 0,
    z: 0,
    dx: 0,
    dy: 0,
    dz: 0,
    flags: { x: true, y: true, z: true, yaw: true, pitch: true },
  });
  assert.deepEqual(bot.entity.velocity.toArray(), [0.7, 0, 0.7]);
});

test("teleport can rotate previous velocity before adding new velocity", () => {
  const bot = fixture();
  bot.entity.velocity.set(0, 0, 1);
  injectPhysics(bot, {});
  installMovementCompatibility(bot);
  bot._client.emit("position", {
    teleportId: 1,
    x: 5,
    y: 64,
    z: 5,
    dx: 0.25,
    dy: 0,
    dz: 0,
    yaw: 90,
    pitch: 30,
    flags: { dx: true, dy: true, dz: true, yawDelta: true },
  });
  const { x, y, z } = bot.entity.velocity;
  assert.ok(Math.abs(x - (0.25 - Math.sqrt(3) / 2)) < 1e-9);
  assert.ok(Math.abs(y + 0.5) < 1e-9);
  assert.ok(Math.abs(z) < 1e-9);
});

test("legacy teleport handling remains with Mineflayer", () => {
  const bot = fixture("1.20.4");
  injectPhysics(bot, {});
  installMovementCompatibility(bot);
  bot._client.emit("position", { teleportId: 5, x: 5, y: 64, z: 6, yaw: 0, pitch: 0, flags: 1 });
  assert.deepEqual(bot.entity.velocity.toArray(), [0.2, 0, 0]);
});
