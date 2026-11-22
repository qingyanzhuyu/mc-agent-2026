"use strict";

// Ported from upstream Zyzzyva0381/mc-agent-api src/movement-compat.js (MIT, v1.3.18 commit 72ecd5d):
// 1. Apply vanilla fishing bobber reel-in impulse on hooked player client.
// 2. Preserve/rotate/replace velocity on MC 1.21+ position packets with dx/dy/dz & flags.

const TO_RADIANS = Math.PI / 180;

function installFishingPull(bot, bobber) {
  const hookedIndex = bobber?.metadataKeys?.indexOf("hooked_entity") ?? -1;
  if (hookedIndex < 0) return;

  const client = bot._client;
  const owners = new Map();
  client.on("spawn_entity", (packet) => {
    if (packet.type === bobber.internalId) owners.set(packet.entityId, packet.objectData);
  });
  client.on("entity_destroy", ({ entityIds }) => {
    for (const id of entityIds) owners.delete(id);
  });
  for (const event of ["login", "respawn"]) {
    bot.on(event, () => owners.clear());
  }
  client.on("state", (state) => {
    if (state !== "play") owners.clear();
  });

  client.on("entity_status", ({ entityId, entityStatus }) => {
    if (entityStatus !== 31 || !owners.has(entityId) || !bot.entity) return;
    const hook = bot.entities[entityId];
    const owner = bot.entities[owners.get(entityId)];
    if (hook?.name !== bobber.name || hook.metadata?.[hookedIndex] !== bot.entity.id + 1 || !owner?.position) {
      return;
    }

    // Vanilla applies this impulse on the client of the hooked player.
    const pull = owner.position.minus(hook.position).scaled(0.1);
    bot.entity.velocity.translate(pull.x, pull.y, pull.z);
  });
}

function teleportVelocity(entity, packet) {
  const { flags } = packet;
  let { x, y, z } = entity.velocity;
  if (flags.yawDelta) {
    // Convert Mineflayer angles to the protocol convention and rotate pitch before yaw.
    const oldYaw = Math.PI - entity.yaw;
    const oldPitch = -entity.pitch;
    const yaw = (flags.yaw ? oldYaw : 0) + packet.yaw * TO_RADIANS;
    const pitch = (flags.pitch ? oldPitch : 0) + packet.pitch * TO_RADIANS;
    const pitchDelta = oldPitch - Math.max(-Math.PI / 2, Math.min(Math.PI / 2, pitch));
    const yawDelta = oldYaw - yaw;
    const adjustedY = y * Math.cos(pitchDelta) + z * Math.sin(pitchDelta);
    const adjustedZ = z * Math.cos(pitchDelta) - y * Math.sin(pitchDelta);
    z = adjustedZ * Math.cos(yawDelta) - x * Math.sin(yawDelta);
    x = x * Math.cos(yawDelta) + adjustedZ * Math.sin(yawDelta);
    y = adjustedY;
  }

  return [
    packet.dx + (flags.dx ? x : 0),
    packet.dy + (flags.dy ? y : 0),
    packet.dz + (flags.dz ? z : 0),
  ];
}

function installTeleportVelocity(bot) {
  const client = bot._client;
  const velocities = new WeakMap();
  // The built-in handler handles position and acknowledgement, but overwrites velocity.
  client.prependListener("position", (packet) => {
    const entity = bot.entity;
    if (
      !entity?.velocity ||
      !Number.isInteger(packet.teleportId) ||
      packet.teleportId < 0 ||
      !packet.flags ||
      typeof packet.flags !== "object" ||
      ![packet.dx, packet.dy, packet.dz].every(Number.isFinite)
    ) {
      return;
    }
    velocities.set(packet, { entity, velocity: teleportVelocity(entity, packet) });
  });
  client.on("position", (packet) => {
    const saved = velocities.get(packet);
    velocities.delete(packet);
    if (saved && saved.entity === bot.entity) saved.entity.velocity.set(...saved.velocity);
  });
}

function installMovementCompatibility(bot) {
  if (!bot || !bot._client) return;
  const data = require("minecraft-data")(bot.version);
  if (!data) return;
  installFishingPull(bot, data.entitiesByName.fishing_bobber);
  const positionFields = data.protocol?.play?.toClient?.types?.packet_position?.[1];
  if (positionFields?.some((field) => field.name === "dx")) installTeleportVelocity(bot);
}

module.exports = { installMovementCompatibility, teleportVelocity };
