"use strict";

const mineflayer = require("mineflayer");
const { pathfinder, Movements, goals } = require("mineflayer-pathfinder");
const PrismarineRecipe = require("prismarine-recipe");
const { Vec3 } = require("vec3");

const {
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
} = require("./helpers");

function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class BotSession {
  constructor({ username, mcHost, mcPort, mcVersion, reconnectIntervalMs = 30000, log = console.log }) {
    this.username = username;
    this.mcHost = mcHost;
    this.mcPort = mcPort;
    this.mcVersion = mcVersion || false;
    this.reconnectIntervalMs = reconnectIntervalMs;
    this.log = log;

    this.token = null;
    this.expiresAt = null;

    this.bot = null;
    this.connecting = false;
    this.connected = false;
    this.spawned = false;
    this.busy = null;
    this._currentAbort = null;

    this.postureState = { sneak: false, shield: false };
    this.usingItemState = null;
    this.lastChatAt = 0;
    this.lastDisconnectAt = 0;

    // Events & Death loop tracking
    this.seq = 0;
    this.events = [];
    this.maxEvents = 2000;
    this.lastDeath = null;
    this.deathTimestamps = [];
    this.deathLoopCooldownUntil = 0;

    // Advancements tracking
    this.advPacketsReceived = 0;
    this.advUpdatedAt = null;
    this.advDefs = new Map();     // id -> { parentId, title, frame, requirements }
    this.advProgress = new Map(); // id -> Map(criterion -> timestampMs | null)
  }

  _pushEvent(type, fields = {}) {
    this.seq += 1;
    const ev = { seq: this.seq, t: Date.now(), type, ...fields };
    this.events.push(ev);
    if (this.events.length > this.maxEvents) {
      this.events.splice(0, this.events.length - this.maxEvents);
    }
    return ev;
  }

  isReady() {
    return Boolean(this.bot && this.connected && this.spawned);
  }

  isAlive() {
    if (!this.bot || !this.spawned) return false;
    return typeof this.bot.health === "number" ? this.bot.health > 0 : true;
  }

  async ensureConnected({ enforceCooldown = false } = {}) {
    const now = Date.now();
    if (now < this.deathLoopCooldownUntil) {
      const rem = Math.ceil((this.deathLoopCooldownUntil - now) / 1000);
      throw new ApiBizError(
        "death_loop_cooldown",
        `Bot is in death loop cooldown (${rem}s remaining)`,
        { cooldown_remaining_seconds: rem }
      );
    }
    if (this.bot && (this.connected || this.connecting)) {
      return { ok: true, already_connected: true, ready: this.isReady() };
    }
    if (enforceCooldown && this.lastDisconnectAt > 0) {
      const elapsed = now - this.lastDisconnectAt;
      if (elapsed < this.reconnectIntervalMs) {
        const waitS = Math.ceil((this.reconnectIntervalMs - elapsed) / 1000);
        throw new ApiBizError(
          "rate_limited",
          `Reconnect interval requires >= ${Math.round(this.reconnectIntervalMs / 1000)}s (${waitS}s left)`,
          { retry_after_s: waitS }
        );
      }
    }
    this._startBot();
    return { ok: true, ready: this.isReady() };
  }

  _startBot() {
    if (this.bot) {
      try {
        this.bot.removeAllListeners();
        this.bot.quit();
      } catch (_) {}
      this.bot = null;
    }

    this.connecting = true;
    this.connected = false;
    this.spawned = false;
    this._manualDisconnect = false;
    this.postureState = { sneak: false, shield: false };

    this.log(`[bridge:${this.username}] Connecting to ${this.mcHost}:${this.mcPort} (version=${this.mcVersion || "auto"})...`);
    const bot = mineflayer.createBot({
      host: this.mcHost,
      port: this.mcPort,
      username: this.username,
      auth: "offline",
      version: this.mcVersion,
      checkTimeoutInterval: 60000,
      hideErrors: false,
    });
    this.bot = bot;
    bot.loadPlugin(pathfinder);

    let lastHealth = 20;

    bot.once("login", () => {
      if (this.bot !== bot) return;
      this.connecting = false;
      this.connected = true;
      this.log(`[bridge:${this.username}] Logged into MC server`);
    });

    bot.on("spawn", () => {
      if (this.bot !== bot) return;
      const firstSpawn = !this.spawned;
      this.connecting = false;
      this.connected = true;
      this.spawned = true;
      lastHealth = typeof bot.health === "number" ? bot.health : 20;
      this.postureState = { sneak: false, shield: false };

      try {
        const moves = new Movements(bot);
        moves.canDig = true;
        moves.digCost = 8;
        moves.allow1by1towers = false;
        moves.scafoldingBlocks = [];
        for (const b of bot.registry.blocksArray) {
          if (typeof b.hardness === "number" && b.hardness > 0.25) {
            moves.blocksCantBreak.add(b.id);
          }
        }
        bot.pathfinder.thinkTimeout = 4000;
        bot.pathfinder.setMovements(moves);
      } catch (e) {
        this.log(`[bridge:${this.username}] Movements init warn: ${e.message}`);
      }

      const pos = bot.entity ? vecToPos(bot.entity.position) : [0, 0, 0];
      this._pushEvent("spawn", { pos });
      if (firstSpawn) {
        this._pushEvent("server_confirmed", { server: `${this.mcHost}:${this.mcPort}`, via: "local_bridge" });
        this.log(`[bridge:${this.username}] Spawned & ready at ${JSON.stringify(pos)}`);
      }
    });

    bot.on("health", () => {
      if (this.bot !== bot) return;
      const h = typeof bot.health === "number" ? bot.health : 20;
      if (h < lastHealth && h > 0) {
        const dmg = round1(lastHealth - h);
        const attacker = this._nearestHostile(16);
        const ev = { health: round1(h), damage: dmg };
        if (attacker) ev.likely_attacker = attacker;
        this._pushEvent("hurt", ev);
        if (this._currentAbort) {
          this._currentAbort("hurt");
        }
      }
      lastHealth = h;
    });

    bot.on("death", () => {
      if (this.bot !== bot) return;
      const now = Date.now();
      const pos = bot.entity ? vecToPos(bot.entity.position) : [0, 0, 0];
      this.postureState = { sneak: false, shield: false };
      this.lastDeath = { pos, t: now, cause: `${this.username} died` };
      this._pushEvent("death", { pos, t: now, dropped_items_at: pos });

      if (this._currentAbort) {
        this._currentAbort("death");
      }

      // Death loop check: 3 deaths within 60s -> 5 min cooldown & disconnect (§7)
      this.deathTimestamps = this.deathTimestamps.filter((ts) => now - ts <= 60000);
      this.deathTimestamps.push(now);
      if (this.deathTimestamps.length >= 3) {
        this.deathLoopCooldownUntil = now + 5 * 60 * 1000;
        this._pushEvent("death_loop", {
          deaths: this.deathTimestamps.length,
          cooldown_until: this.deathLoopCooldownUntil,
        });
        this.log(`[bridge:${this.username}] Death loop triggered! Disconnecting for 5 minutes.`);
        this.disconnect();
      }
    });

    bot.on("chat", (username, message) => {
      if (this.bot !== bot || username === this.username) return;
      this._pushEvent("chat", { text: `<${username}> ${message}`, sender: username });
    });

    bot.on("playerCollect", (collector, collected) => {
      if (this.bot !== bot || !bot.entity || collector.id !== bot.entity.id) return;
      let itemName = "item";
      let count = 1;
      try {
        const drop = typeof collected.getDroppedItem === "function" ? collected.getDroppedItem() : null;
        if (drop) {
          itemName = drop.name || itemName;
          count = drop.count || 1;
        }
      } catch (_) {}
      this._pushEvent("pickup", { item: itemName, count });
    });

    bot.on("sleep", () => {
      if (this.bot !== bot) return;
      this._pushEvent("sleep", {});
    });

    bot.on("wake", () => {
      if (this.bot !== bot) return;
      this._pushEvent("wake", {});
    });

    bot.on("mount", () => {
      if (this.bot !== bot) return;
      const v = bot.vehicle;
      this._pushEvent("vehicle", { action: "mount", name: v ? v.name : "unknown" });
    });

    bot.on("dismount", () => {
      if (this.bot !== bot) return;
      this._pushEvent("vehicle", { action: "dismount", name: "vehicle" });
    });

    bot.on("kicked", (reason) => {
      if (this.bot !== bot) return;
      const text = nbtToPlainText(reason) || String(reason);
      this.log(`[bridge:${this.username}] Kicked: ${text}`);
      this._pushEvent("kicked", { reason: text });
    });

    bot.on("end", (reason) => {
      if (this.bot !== bot) return;
      this.connecting = false;
      this.connected = false;
      this.spawned = false;
      this.lastDisconnectAt = Date.now();
      this.postureState = { sneak: false, shield: false };
      this._pushEvent("disconnected", { reason: String(reason || "end") });
      if (this._currentAbort) {
        this._currentAbort("disconnected");
      }
      this.log(`[bridge:${this.username}] Disconnected (${reason || "end"})`);
      if (!this._manualDisconnect && this.token && Date.now() >= this.deathLoopCooldownUntil) {
        setTimeout(() => {
          if (!this._manualDisconnect && this.token && !this.connected && !this.connecting && Date.now() >= this.deathLoopCooldownUntil) {
            this.log(`[bridge:${this.username}] Auto-reconnecting after unexpected drop...`);
            this._startBot();
          }
        }, 2500).unref();
      }
    });

    bot.on("error", (err) => {
      this.log(`[bridge:${this.username}] Bot error: ${err.message}`);
    });

    // Keep bot afloat in water whenever pathfinder is not actively steering (prevents drowning during idle/dig/craft)
    let idleFloating = false;
    bot.on("physicsTick", () => {
      const pfMoving = bot.pathfinder && typeof bot.pathfinder.isMoving === "function" && bot.pathfinder.isMoving();
      if (this.bot !== bot || !this.spawned || pfMoving || !bot.entity) {
        idleFloating = false;
        return;
      }
      const headBlk = bot.blockAt(bot.entity.position.offset(0, 1.5, 0).floored());
      const inWater = Boolean(bot.entity.isInWater || (headBlk && headBlk.name === "water"));
      if (inWater) {
        idleFloating = true;
        bot.setControlState("jump", true);
      } else if (idleFloating) {
        idleFloating = false;
        bot.setControlState("jump", false);
      }
    });

    // Track vanilla advancements packets
    bot._client.on("advancements", (packet) => {
      if (this.bot !== bot) return;
      this._handleAdvancementsPacket(packet);
    });
  }

  _handleAdvancementsPacket(packet) {
    if (!packet) return;
    this.advPacketsReceived += 1;
    this.advUpdatedAt = Date.now();

    if (packet.reset) {
      this.advDefs.clear();
      this.advProgress.clear();
    }
    if (Array.isArray(packet.identifiers)) {
      for (const id of packet.identifiers) {
        this.advDefs.delete(id);
        this.advProgress.delete(id);
      }
    }
    if (Array.isArray(packet.advancementMapping)) {
      const frameNames = ["task", "challenge", "goal"];
      for (const entry of packet.advancementMapping) {
        const id = entry.key;
        const val = entry.value || {};
        const disp = val.displayData || null;
        this.advDefs.set(id, {
          id,
          parentId: val.parentId || null,
          title: disp ? nbtToPlainText(disp.title) || id : id,
          description: disp ? nbtToPlainText(disp.description) || "" : "",
          frame: disp ? frameNames[disp.frameType] || "task" : "task",
          hasDisplay: Boolean(disp),
          requirements: Array.isArray(val.requirements) ? val.requirements : [],
        });
      }
    }
    if (Array.isArray(packet.progressMapping)) {
      for (const entry of packet.progressMapping) {
        const id = entry.key;
        let critMap = this.advProgress.get(id);
        if (!critMap) {
          critMap = new Map();
          this.advProgress.set(id, critMap);
        }
        for (const c of entry.value || []) {
          const rawProg = c.criterionProgress;
          const ts = rawProg != null ? Number(rawProg) : null;
          critMap.set(c.criterionIdentifier, ts);
        }
      }
    }
  }

  disconnect() {
    this._manualDisconnect = true;
    if (this._currentAbort) {
      this._currentAbort("disconnected");
    }
    if (this.bot) {
      const b = this.bot;
      this.bot = null;
      this.connecting = false;
      this.connected = false;
      this.spawned = false;
      this.lastDisconnectAt = Date.now();
      this.postureState = { sneak: false, shield: false };
      try {
        b.quit();
      } catch (_) {}
    }
    return { ok: true };
  }

  _requireReady() {
    const now = Date.now();
    if (now < this.deathLoopCooldownUntil) {
      const rem = Math.ceil((this.deathLoopCooldownUntil - now) / 1000);
      throw new ApiBizError("death_loop_cooldown", "In death loop cooldown", {
        cooldown_remaining_seconds: rem,
      });
    }
    if (!this.bot || (!this.connected && !this.connecting)) {
      throw new ApiBizError("not_connected", "Bot is not connected");
    }
    if (!this.spawned || !this.bot.entity) {
      throw new ApiBizError("not_ready", "Bot is connecting / waiting for spawn");
    }
  }

  _nearestHostile(maxDist = 24) {
    if (!this.bot || !this.bot.entity) return null;
    const myPos = this.bot.entity.position;
    let best = null;
    let bestDist = maxDist;
    for (const e of Object.values(this.bot.entities)) {
      if (!e || e.id === this.bot.entity.id || !e.position) continue;
      if (classifyEntity(e) !== "hostile") continue;
      const d = myPos.distanceTo(e.position);
      if (d <= bestDist) {
        bestDist = d;
        best = { id: e.id, name: e.name || "hostile", dist: round1(d) };
      }
    }
    return best;
  }

  _getArmorObject() {
    const bot = this.bot;
    if (!bot || !bot.inventory) {
      return { head: null, chest: null, legs: null, feet: null };
    }
    const slots = bot.inventory.slots;
    return {
      head: serializeItem(slots[5]),
      chest: serializeItem(slots[6]),
      legs: serializeItem(slots[7]),
      feet: serializeItem(slots[8]),
    };
  }

  _getOffhandItem() {
    const bot = this.bot;
    if (!bot || !bot.inventory) return null;
    return serializeItem(bot.inventory.slots[45]);
  }

  async _applyPosture() {
    const bot = this.bot;
    if (!bot || !this.spawned || !this.isAlive()) return;
    try {
      bot.setControlState("sneak", Boolean(this.postureState.sneak));
      if (this.postureState.shield) {
        const off = bot.inventory.slots[45];
        if (off && off.name === "shield") {
          bot.activateItem(true);
        }
      } else {
        bot.deactivateItem();
      }
    } catch (_) {}
  }

  // ================================================================
  // 3. Observation Endpoints (non-blocking)
  // ================================================================

  getStatus() {
    if (!this.bot || (!this.connected && !this.connecting)) {
      throw new ApiBizError("not_connected", "Bot is not connected");
    }
    const bot = this.bot;
    if (!this.spawned || !bot.entity) {
      return {
        username: this.username,
        pos: [0, 0, 0],
        yaw: 0,
        pitch: 0,
        dimension: "overworld",
        gamemode: "survival",
        health: 20,
        food: 20,
        saturation: 5,
        xp_level: 0,
        xp_progress: 0,
        oxygen: 300,
        armor_points: 0,
        held: null,
        offhand: null,
        effects: [],
        on_fire: false,
        is_sleeping: false,
        in_water: false,
        submerged: false,
        in_lava: false,
        in_vehicle: null,
        sneaking: false,
        blocking: false,
        using_item: null,
        time_of_day: 0,
        is_day: true,
        raining: false,
        thundering: false,
        biome: "plains",
        on_ground: true,
        alive: true,
        ready: false,
        busy: this.busy,
      };
    }

    const pos = bot.entity.position;
    const armor = this._getArmorObject();
    const feetBlock = bot.blockAt(pos.floored());
    const headBlock = bot.blockAt(pos.offset(0, 1.6, 0).floored());
    const inWater = Boolean(
      bot.entity.isInWater || (feetBlock && feetBlock.name === "water")
    );
    const submerged = Boolean(headBlock && headBlock.name === "water");
    const inLava = Boolean(
      bot.entity.isInLava || (feetBlock && feetBlock.name === "lava")
    );

    const effects = [];
    if (bot.entity.effects) {
      for (const eff of Object.values(bot.entity.effects)) {
        if (!eff) continue;
        const effDef = bot.registry.effects && bot.registry.effects[eff.id];
        effects.push({
          name: effDef ? effDef.name : String(eff.id),
          level: (eff.amplifier || 0) + 1,
          duration_s: round1((eff.duration || 0) / 20),
        });
      }
    }

    let biomeName = "unknown";
    try {
      const bId = bot.world && typeof bot.world.getBiome === "function" ? bot.world.getBiome(pos) : null;
      if (bId != null && bot.registry.biomes && bot.registry.biomes[bId]) {
        biomeName = bot.registry.biomes[bId].name;
      }
    } catch (_) {}

    const tod = bot.time && typeof bot.time.timeOfDay === "number" ? bot.time.timeOfDay : 0;
    const isDay = bot.time && typeof bot.time.isDay === "boolean" ? bot.time.isDay : (tod < 12542 || tod > 23460);
    const alive = (typeof bot.health === "number" ? bot.health : 20) > 0;

    let dim = (bot.game && bot.game.dimension) || "overworld";
    if (typeof dim === "string" && dim.startsWith("minecraft:")) {
      dim = dim.slice("minecraft:".length);
    }

    const out = {
      username: bot.username || this.username,
      pos: vecToPos(pos),
      yaw: round1(bot.entity.yaw || 0),
      pitch: round1(bot.entity.pitch || 0),
      dimension: dim,
      gamemode: (bot.game && bot.game.gameMode) || "survival",
      health: round1(typeof bot.health === "number" ? bot.health : 20),
      food: typeof bot.food === "number" ? bot.food : 20,
      saturation: round1(typeof bot.foodSaturation === "number" ? bot.foodSaturation : 5),
      xp_level: (bot.experience && bot.experience.level) || 0,
      xp_progress: Math.round(((bot.experience && bot.experience.progress) || 0) * 100) / 100,
      oxygen: typeof bot.oxygenLevel === "number" ? bot.oxygenLevel * 15 : 300,
      armor_points: computeArmorPoints(armor),
      held: serializeItem(bot.heldItem),
      offhand: this._getOffhandItem(),
      effects,
      on_fire: Boolean(bot.entity.onFire),
      is_sleeping: Boolean(bot.isSleeping),
      in_water: inWater,
      submerged,
      in_lava: inLava,
      in_vehicle: bot.vehicle ? { id: bot.vehicle.id, name: bot.vehicle.name || "vehicle" } : null,
      sneaking: Boolean(this.postureState.sneak || bot.getControlState("sneak")),
      blocking: Boolean(this.postureState.shield),
      using_item: this.usingItemState
        ? { name: this.usingItemState.name, elapsed_ms: Date.now() - this.usingItemState.startedAt }
        : null,
      time_of_day: tod,
      is_day: isDay,
      raining: Boolean(bot.isRaining),
      thundering: Boolean(bot.thunderState > 0),
      biome: biomeName,
      on_ground: Boolean(bot.entity.onGround),
      alive,
      ready: Boolean(this.spawned && alive),
      busy: this.busy,
    };

    const nearestHostile = this._nearestHostile(24);
    if (nearestHostile) out.nearest_hostile = nearestHostile;
    if (this.lastDeath) out.last_death = this.lastDeath;
    return out;
  }

  getInventory() {
    this._requireReady();
    const bot = this.bot;
    const items = bot.inventory.items().map(serializeItem);
    const emptySlots = typeof bot.inventory.emptySlotCount === "function" ? bot.inventory.emptySlotCount() : Math.max(0, 36 - items.length);
    return {
      items,
      armor: this._getArmorObject(),
      offhand: this._getOffhandItem(),
      held: serializeItem(bot.heldItem),
      empty_slots: emptySlots,
    };
  }

  findBlocks(body = {}) {
    this._requireReady();
    const bot = this.bot;
    const radius = body.radius === undefined ? 16 : body.radius;
    if (!isFiniteNum(radius) || radius < 1 || radius > 64) {
      throw new ApiBizError("bad_args", "radius must be a number between 1 and 64");
    }
    const r = Math.floor(radius);
    const limit = body.limit === undefined ? 20 : body.limit;
    if (!isFiniteNum(limit) || limit < 1 || limit > 500) {
      throw new ApiBizError("bad_args", "limit must be between 1 and 500");
    }
    const center = bot.entity.position;

    // Detail mode: types provided
    if (body.types !== undefined && body.types !== null) {
      if (!Array.isArray(body.types) || body.types.length === 0) {
        throw new ApiBizError("bad_args", "types must be a non-empty array when provided");
      }
      const ids = [];
      for (const name of body.types) {
        const clean = String(name).replace(/^minecraft:/, "");
        const bDef = bot.registry.blocksByName[clean];
        if (bDef) ids.push(bDef.id);
      }
      if (ids.length === 0) {
        return { total: 0, blocks: [] };
      }
      const rawPositions = bot.findBlocks({
        matching: ids,
        maxDistance: r,
        count: Math.min(4000, Math.max(limit * 15, 250)),
      });

      const matched = [];
      for (const p of rawPositions) {
        if (!isBlockExposed(bot, p)) continue;
        const blk = bot.blockAt(p);
        if (!blk) continue;
        const props = typeof blk.getProperties === "function" ? blk.getProperties() : {};
        if (body.properties && typeof body.properties === "object") {
          let ok = true;
          for (const [pk, pv] of Object.entries(body.properties)) {
            if (props[pk] !== pv) {
              ok = false;
              break;
            }
          }
          if (!ok) continue;
        }
        if (body.mature !== undefined && body.mature !== null) {
          const ci = getCropInfo(blk);
          if (!ci || ci.mature !== Boolean(body.mature)) continue;
        }
        const dist = round1(center.distanceTo(p.offset(0.5, 0.5, 0.5)));
        matched.push({
          name: blk.name,
          pos: vecToBlockPos(p),
          dist,
          properties: props,
        });
      }
      matched.sort((a, b) => a.dist - b.dist);
      return {
        total: matched.length,
        blocks: matched.slice(0, Math.floor(limit)),
      };
    }

    // Summary mode: scan exposed blocks around player
    const cx = Math.floor(center.x);
    const cy = Math.floor(center.y);
    const cz = Math.floor(center.z);
    const minY = (bot.game && typeof bot.game.minY === "number") ? bot.game.minY : -64;
    const maxY = minY + ((bot.game && typeof bot.game.height === "number") ? bot.game.height : 384) - 1;
    const y0 = Math.max(minY, cy - Math.min(r, 32));
    const y1 = Math.min(maxY, cy + Math.min(r, 32));
    const step = r > 32 ? 2 : 1;

    let scanned = 0;
    const summary = {};
    const rSq = r * r;
    const p = new Vec3(0, 0, 0);

    for (let x = cx - r; x <= cx + r; x += step) {
      const dx = x - cx;
      for (let z = cz - r; z <= cz + r; z += step) {
        const dz = z - cz;
        if (dx * dx + dz * dz > rSq) continue;
        for (let y = y0; y <= y1; y += step) {
          const dy = y - cy;
          const dSq = dx * dx + dy * dy + dz * dz;
          if (dSq > rSq) continue;
          p.set(x, y, z);
          const blk = bot.blockAt(p);
          if (!blk || blk.type === 0 || blk.name === "air" || blk.name === "cave_air" || blk.name === "void_air") {
            continue;
          }
          if (!isBlockExposed(bot, p)) continue;
          scanned += 1;
          const dist = round1(Math.sqrt(dSq));
          const rec = summary[blk.name];
          if (!rec) {
            summary[blk.name] = { count: 1, nearest: [x, y, z], dist };
          } else {
            rec.count += 1;
            if (dist < rec.dist) {
              rec.dist = dist;
              rec.nearest = [x, y, z];
            }
          }
        }
      }
    }

    return { radius: r, scanned, blocks: summary };
  }

  getBlock(body = {}) {
    this._requireReady();
    const pos = parseBlockPos(body);
    const blk = this.bot.blockAt(pos);
    if (!blk) {
      throw new ApiBizError("not_loaded", "Chunk is not loaded at target position", { pos: vecToBlockPos(pos) });
    }
    const props = typeof blk.getProperties === "function" ? blk.getProperties() : {};
    const heldId = this.bot.heldItem ? this.bot.heldItem.type : null;
    const harvestOk = typeof blk.canHarvest === "function" ? Boolean(blk.canHarvest(heldId)) : true;
    const out = {
      name: blk.name,
      pos: vecToBlockPos(pos),
      properties: props,
      diggable: Boolean(blk.diggable),
      hardness: typeof blk.hardness === "number" ? blk.hardness : 0,
      harvest_tool_ok: harvestOk,
    };
    const crop = getCropInfo(blk);
    if (crop) {
      out.growth = crop.growth;
      out.mature = crop.mature;
    }
    return out;
  }

  getEntities(body = {}) {
    this._requireReady();
    const radius = body.radius === undefined ? 32 : body.radius;
    if (!isFiniteNum(radius) || radius <= 0 || radius > 128) {
      throw new ApiBizError("bad_args", "radius must be between 1 and 128");
    }
    const typeFilter = Array.isArray(body.types) && body.types.length > 0
      ? new Set(body.types.map((s) => String(s).toLowerCase().replace(/^minecraft:/, "")))
      : null;

    const myId = this.bot.entity.id;
    const center = this.bot.entity.position;
    const list = [];

    for (const e of Object.values(this.bot.entities)) {
      if (!e || e.id === myId || !e.position) continue;
      const dist = center.distanceTo(e.position);
      if (dist > radius) continue;
      const cat = classifyEntity(e);
      const name = (e.name || e.username || "unknown").toLowerCase();
      if (typeFilter && !typeFilter.has(cat) && !typeFilter.has(name)) {
        continue;
      }
      const item = {
        id: e.id,
        type: cat,
        name: e.type === "player" ? (e.username || name) : name,
        pos: vecToPos(e.position),
        dist: round1(dist),
      };
      if (typeof e.health === "number") item.health = round1(e.health);
      if (cat === "item") {
        try {
          const drop = typeof e.getDroppedItem === "function" ? e.getDroppedItem() : null;
          if (drop) item.item = `${drop.name}x${drop.count || 1}`;
        } catch (_) {}
      }
      list.push(item);
    }

    list.sort((a, b) => a.dist - b.dist);
    return { entities: list.slice(0, 40) };
  }

  getEvents(params = {}) {
    const sinceRaw = params.since === undefined ? 0 : Number(params.since);
    const limitRaw = params.limit === undefined ? 50 : Number(params.limit);
    if (!Number.isFinite(sinceRaw) || sinceRaw < 0) {
      throw new ApiBizError("bad_args", "since must be a non-negative number");
    }
    if (!Number.isFinite(limitRaw) || limitRaw < 1 || limitRaw > 500) {
      throw new ApiBizError("bad_args", "limit must be between 1 and 500");
    }
    const since = Math.floor(sinceRaw);
    const limit = Math.floor(limitRaw);
    const filtered = this.events.filter((e) => e.seq > since);
    const slice = filtered.slice(0, limit);
    const lastSeq = slice.length > 0 ? slice[slice.length - 1].seq : this.seq;
    return {
      since,
      last_seq: lastSeq,
      total: filtered.length,
      has_more: filtered.length > limit,
      events: slice,
    };
  }

  getAdvancements(body = {}) {
    this._requireReady();
    const statusFilter = body.status || "completed";
    const includeRecipes = Boolean(body.include_recipes);

    const allIds = new Set([...this.advDefs.keys(), ...this.advProgress.keys()]);
    const list = [];
    let completedCount = 0;

    for (const id of allIds) {
      if (!includeRecipes && (id.startsWith("minecraft:recipes/") || id.includes(":recipes/"))) {
        continue;
      }
      const def = this.advDefs.get(id) || {
        id,
        parentId: null,
        title: id,
        frame: "task",
        hasDisplay: true,
        requirements: [],
      };
      const critMap = this.advProgress.get(id) || new Map();

      let totalGroups = def.requirements.length;
      let doneGroups = 0;
      let completedAt = null;

      if (totalGroups > 0) {
        for (const group of def.requirements) {
          let groupDone = false;
          for (const crit of group) {
            const ts = critMap.get(crit);
            if (ts != null) {
              groupDone = true;
              if (completedAt === null || ts > completedAt) completedAt = ts;
            }
          }
          if (groupDone) doneGroups += 1;
        }
      } else if (critMap.size > 0) {
        totalGroups = critMap.size;
        for (const ts of critMap.values()) {
          if (ts != null) {
            doneGroups += 1;
            if (completedAt === null || ts > completedAt) completedAt = ts;
          }
        }
      }

      const isCompleted = totalGroups > 0 && doneGroups >= totalGroups;
      const st = isCompleted ? "completed" : doneGroups > 0 ? "in_progress" : "not_started";
      if (isCompleted) completedCount += 1;

      if (statusFilter !== "all" && st !== statusFilter) continue;

      list.push({
        id,
        parent_id: def.parentId,
        title: def.title,
        frame: def.frame,
        status: st,
        completed_at: isCompleted ? completedAt : null,
        progress: [doneGroups, totalGroups],
      });
    }

    return {
      ready: this.advPacketsReceived > 0,
      updated_at: this.advUpdatedAt,
      packets_received: this.advPacketsReceived,
      summary: { completed: completedCount, returned: list.length },
      advancements: list,
    };
  }

  // ================================================================
  // 4. Action Mutex & Blocking Actions
  // ================================================================

  async _runAction(actionName, stopOn, fn) {
    this._requireReady();
    if (!this.isAlive()) {
      throw new ApiBizError("dead", "Bot is dead, waiting for respawn");
    }
    if (this.busy !== null) {
      throw new ApiBizError("busy", `Another action (${this.busy}) is currently executing`, {
        busy: this.busy,
      });
    }

    this.busy = actionName;
    const stopSet = new Set(Array.isArray(stopOn) ? stopOn : ["hurt", "death"]);
    let abortReject = null;
    const abortCbs = [];

    const abortPromise = new Promise((_, reject) => {
      abortReject = reject;
    });

    this._currentAbort = (reason) => {
      if (reason === "hurt" && !stopSet.has("hurt")) return;
      if (reason === "death" && !stopSet.has("death")) return;

      for (const cb of abortCbs) {
        try {
          cb();
        } catch (_) {}
      }
      try {
        if (this.bot && this.bot.pathfinder) this.bot.pathfinder.setGoal(null);
        if (this.bot) this.bot.stopDigging();
      } catch (_) {}

      if (reason === "disconnected") {
        abortReject(new ApiBizError("not_connected", "Bot disconnected during action"));
        return;
      }
      const extra = {
        interrupted_by: reason,
        health: this.bot && typeof this.bot.health === "number" ? round1(this.bot.health) : 0,
        pos: this.bot && this.bot.entity ? vecToPos(this.bot.entity.position) : [0, 0, 0],
      };
      const nh = this._nearestHostile(24);
      if (nh) extra.nearest_hostile = nh;
      abortReject(new ApiBizError("interrupted", `Action ${actionName} interrupted by ${reason}`, extra));
    };

    const ctx = {
      onAbort(cb) {
        abortCbs.push(cb);
      },
    };

    try {
      return await Promise.race([fn(ctx), abortPromise]);
    } finally {
      this._currentAbort = null;
      this.busy = null;
      this.usingItemState = null;
      await this._applyPosture();
    }
  }

  async _gotoGoal(goal, timeoutMs = 60000, ctx = null) {
    const bot = this.bot;
    if (!bot || !bot.pathfinder) {
      throw new ApiBizError("path_not_found", "Pathfinder unavailable");
    }
    return new Promise((resolve, reject) => {
      let settled = false;
      let lastProgressPos = bot.entity.position.clone();
      let lastProgressAt = Date.now();
      const stuckInterval = setInterval(() => {
        if (!bot.entity) return;
        const d = bot.entity.position.distanceTo(lastProgressPos);
        if (d >= 0.35) {
          lastProgressPos = bot.entity.position.clone();
          lastProgressAt = Date.now();
        } else if (Date.now() - lastProgressAt >= 8000) {
          cleanup();
          try {
            bot.pathfinder.setGoal(null);
          } catch (_) {}
          reject(
            new ApiBizError("path_not_found", "Stuck for 8s without movement progress", {
              pos: vecToPos(bot.entity.position),
            })
          );
        }
      }, 1000);
      const cleanup = () => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        clearInterval(stuckInterval);
        bot.removeListener("goal_reached", onReached);
        bot.removeListener("path_update", onPathUpdate);
      };
      const onReached = () => {
        cleanup();
        resolve();
      };
      const onPathUpdate = (results) => {
        if (results && (results.status === "noPath" || (results.status === "timeout" && (!results.path || results.path.length === 0)))) {
          cleanup();
          try {
            bot.pathfinder.setGoal(null);
          } catch (_) {}
          reject(
            new ApiBizError("path_not_found", `Pathfinder ${results.status} to target`, {
              pos: vecToPos(bot.entity.position),
            })
          );
        }
      };
      const timer = setTimeout(() => {
        cleanup();
        try {
          bot.pathfinder.setGoal(null);
        } catch (_) {}
        reject(
          new ApiBizError("timeout", `Pathfinding timed out after ${timeoutMs}ms`, {
            pos: vecToPos(bot.entity.position),
          })
        );
      }, timeoutMs);

      if (ctx) {
        ctx.onAbort(() => {
          cleanup();
          try {
            bot.pathfinder.setGoal(null);
          } catch (_) {}
        });
      }

      bot.on("goal_reached", onReached);
      bot.on("path_update", onPathUpdate);
      bot.pathfinder.setGoal(goal);
    });
  }

  async stop() {
    if (this._currentAbort) {
      this._currentAbort("stop");
    }
    if (this.bot) {
      try {
        if (this.bot.pathfinder) this.bot.pathfinder.setGoal(null);
        this.bot.stopDigging();
        this.bot.clearControlStates();
      } catch (_) {}
      await this._applyPosture();
    }
    return { stopped: true };
  }

  async posture(body = {}) {
    this._requireReady();
    const keys = Object.keys(body || {});
    let targetSneak = false;
    let targetShield = false;
    if (keys.length > 0) {
      if (body.sneak !== undefined && typeof body.sneak !== "boolean") {
        throw new ApiBizError("bad_args", "sneak must be boolean");
      }
      if (body.shield !== undefined && typeof body.shield !== "boolean") {
        throw new ApiBizError("bad_args", "shield must be boolean");
      }
      targetSneak = Boolean(body.sneak);
      targetShield = Boolean(body.shield);
    }

    if (targetShield) {
      const off = this.bot.inventory.slots[45];
      if (!off || off.name !== "shield") {
        const shieldItem = this.bot.inventory.items().find((i) => i.name === "shield");
        if (!shieldItem) {
          throw new ApiBizError("missing_item", "No shield in inventory", { item: "shield" });
        }
        await this.bot.equip(shieldItem, "off-hand");
      }
    }

    this.postureState = { sneak: targetSneak, shield: targetShield };
    await this._applyPosture();
    return {
      sneaking: targetSneak,
      blocking: targetShield,
      offhand: this._getOffhandItem(),
    };
  }

  async moveTo(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("move_to", stopOn, async (ctx) => {
      const bot = this.bot;
      const rng = body.range === undefined ? 1 : Number(body.range);
      if (!isFiniteNum(rng) || rng < 0) {
        throw new ApiBizError("bad_args", "range must be a non-negative number");
      }

      const startDim = (bot.game && bot.game.dimension) || "overworld";

      if (body.entity_id !== undefined && body.entity_id !== null) {
        if (!Number.isInteger(body.entity_id)) {
          throw new ApiBizError("bad_args", "entity_id must be integer");
        }
        const ent = bot.entities[body.entity_id];
        if (!ent || !ent.position) {
          throw new ApiBizError("no_such_entity", `Entity ${body.entity_id} not found`);
        }
        const d0 = bot.entity.position.distanceTo(ent.position);
        if (d0 > Math.max(rng, 1)) {
          await this._gotoGoal(new goals.GoalFollow(ent, Math.max(1, rng)), 120000, ctx);
        }
      } else {
        let tx, ty, tz;
        if (Array.isArray(body.pos)) {
          if (body.pos.length !== 3 || !body.pos.every(isFiniteNum)) {
            throw new ApiBizError("bad_args", "pos must be [x, y, z]");
          }
          [tx, ty, tz] = body.pos;
        } else {
          if (!isFiniteNum(body.x) || !isFiniteNum(body.z)) {
            throw new ApiBizError("bad_args", "move_to requires finite (x, z), pos, or entity_id");
          }
          tx = body.x;
          tz = body.z;
          ty = body.y !== undefined && body.y !== null ? body.y : null;
          if (ty !== null && !isFiniteNum(ty)) {
            throw new ApiBizError("bad_args", "y must be finite number");
          }
        }

        const goal = ty !== null
          ? new goals.GoalNear(tx, ty, tz, Math.max(1, rng))
          : new goals.GoalNearXZ(tx, tz, Math.max(1, rng));
        await this._gotoGoal(goal, 150000, ctx);
      }

      const endDim = (bot.game && bot.game.dimension) || "overworld";
      const out = { pos: vecToPos(bot.entity.position) };
      if (endDim !== startDim) {
        out.dimension_changed = { from: startDim, to: endDim };
      }
      return out;
    });
  }

  async dig(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("dig", stopOn, async (ctx) => {
      const bot = this.bot;
      const pos = parseBlockPos(body);
      const collect = body.collect !== false;

      let blk = bot.blockAt(pos);
      if (!blk) {
        throw new ApiBizError("not_loaded", "Target block chunk not loaded", { pos: vecToBlockPos(pos) });
      }
      if (blk.type === 0 || blk.name === "air" || blk.name === "cave_air" || blk.name === "void_air") {
        throw new ApiBizError("nothing_to_dig", "Target block is air", { pos: vecToBlockPos(pos) });
      }
      if (!blk.diggable || blk.hardness < 0) {
        throw new ApiBizError("undiggable", `Block ${blk.name} is not diggable`, { pos: vecToBlockPos(pos) });
      }

      // Move within reach if > 4.2 blocks away
      const eyePos = bot.entity.position.offset(0, 1.62, 0);
      if (eyePos.distanceTo(pos.offset(0.5, 0.5, 0.5)) > 4.2) {
        await this._gotoGoal(new goals.GoalNear(pos.x, pos.y, pos.z, 3), 30000, ctx);
      }

      blk = bot.blockAt(pos);
      if (!blk || blk.type === 0 || blk.name === "air") {
        throw new ApiBizError("nothing_to_dig", "Block disappeared before digging", { pos: vecToBlockPos(pos) });
      }

      // Auto-equip best harvest tool if available
      try {
        const bestTool = bot.pathfinder && typeof bot.pathfinder.bestHarvestTool === "function"
          ? bot.pathfinder.bestHarvestTool(blk)
          : null;
        if (bestTool && (!bot.heldItem || bot.heldItem.id !== bestTool.id)) {
          await bot.equip(bestTool, "hand");
        }
      } catch (_) {}

      const beforeInv = inventorySummaryMap(bot);
      const dugName = blk.name;
      const origStateId = blk.stateId;
      const heldId = bot.heldItem ? bot.heldItem.type : null;
      const harvestOk = typeof blk.canHarvest === "function" ? Boolean(blk.canHarvest(heldId)) : true;
      const toolName = bot.heldItem ? bot.heldItem.name : "hand";

      let sawServerRollback = false;
      let sawServerBreak = false;
      const onBlockChangePkt = (data) => {
        const loc = data && data.location;
        if (!loc || loc.x !== pos.x || loc.y !== pos.y || loc.z !== pos.z) return;
        if (data.type === origStateId && origStateId !== 0) {
          sawServerRollback = true;
        } else if (data.type !== origStateId) {
          sawServerBreak = true;
        }
      };
      bot._client.on("block_change", onBlockChangePkt);

      let digMs = 0;
      try {
        const t0 = Date.now();
        await bot.dig(blk);
        digMs = Date.now() - t0;
        await sleep(150);
      } finally {
        bot._client.removeListener("block_change", onBlockChangePkt);
      }

      if (sawServerRollback && !sawServerBreak) {
        try {
          bot._updateBlockState(pos, origStateId);
        } catch (_) {}
        throw new ApiBizError("protected_region", "Server rolled back block break (spawn-protection)", {
          pos: vecToBlockPos(pos),
        });
      }

      if (collect) {
        // Vanilla item drops have 10 ticks (500ms) pickupDelay; step close and poll up to 1.2s
        const blockCenter = pos.offset(0.5, 0.5, 0.5);
        if (bot.entity.position.distanceTo(blockCenter) > 1.5) {
          try {
            await this._gotoGoal(new goals.GoalNear(pos.x + 0.5, pos.y, pos.z + 0.5, 1), 3500, ctx);
          } catch (_) {}
        }
        const waitStart = Date.now();
        while (Date.now() - waitStart < 1100) {
          const curInv = inventorySummaryMap(bot);
          const anyGained = Object.entries(curInv).some(([k, v]) => v > (beforeInv[k] || 0));
          if (anyGained) break;
          const dropEnt = Object.values(bot.entities).find(
            (e) => e && classifyEntity(e) === "item" && e.position && e.position.distanceTo(blockCenter) < 4.0
          );
          if (dropEnt && bot.entity.position.distanceTo(dropEnt.position) > 1.3) {
            try {
              await this._gotoGoal(new goals.GoalNear(dropEnt.position.x, dropEnt.position.y, dropEnt.position.z, 1), 2500, ctx);
            } catch (_) {}
          }
          await sleep(120);
        }
      }

      const afterInv = inventorySummaryMap(bot);
      const gained = {};
      for (const [k, v] of Object.entries(afterInv)) {
        const diff = v - (beforeInv[k] || 0);
        if (diff > 0) gained[k] = diff;
      }

      const res = {
        dug: dugName,
        dig_ms: digMs,
        tool: toolName,
        gained,
        sneaked: Boolean(this.postureState.sneak),
      };
      if (!harvestOk) {
        res.warning = `Tool ${toolName} may not harvest drops from ${dugName}`;
      }
      return res;
    });
  }

  async place(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("place", stopOn, async (ctx) => {
      const bot = this.bot;
      const itemName = String(body.item || "").replace(/^minecraft:/, "").trim();
      if (!itemName) {
        throw new ApiBizError("bad_args", "item is required");
      }
      const invItem = bot.inventory.items().find((i) => i.name === itemName);
      if (!invItem) {
        throw new ApiBizError("missing_item", `Missing item: ${itemName}`, {
          item: itemName,
          inventory: inventorySummaryMap(bot),
        });
      }

      let targetPos = parseBlockPos(body, true);
      if (!targetPos) {
        // Choose adjacent air spot near player feet
        const base = bot.entity.position.floored();
        const candidates = [
          base.offset(1, 0, 0),
          base.offset(-1, 0, 0),
          base.offset(0, 0, 1),
          base.offset(0, 0, -1),
          base.offset(1, 1, 0),
          base.offset(-1, 1, 0),
        ];
        for (const c of candidates) {
          const cb = bot.blockAt(c);
          if (cb && (cb.name === "air" || cb.boundingBox === "empty")) {
            // Check has solid neighbor
            for (const off of CARDINAL_OFFSETS) {
              const nb = bot.blockAt(c.plus(off));
              if (nb && nb.boundingBox === "block") {
                targetPos = c;
                break;
              }
            }
            if (targetPos) break;
          }
        }
        if (!targetPos) {
          throw new ApiBizError("no_support", "Could not find nearby supported spot to place block");
        }
      }

      if (bot.entity.position.distanceTo(targetPos.offset(0.5, 0.5, 0.5)) > 4.5) {
        await this._gotoGoal(new goals.GoalPlaceBlock(targetPos, bot.world, {}), 25000, ctx).catch(async () => {
          await this._gotoGoal(new goals.GoalNear(targetPos.x, targetPos.y, targetPos.z, 3), 25000, ctx);
        });
      }

      const curBlk = bot.blockAt(targetPos);
      if (curBlk && curBlk.boundingBox === "block") {
        throw new ApiBizError("occupied", `Position already occupied by ${curBlk.name}`, {
          pos: vecToBlockPos(targetPos),
        });
      }

      // Check self-intersection
      const pFeet = bot.entity.position.floored();
      const pHead = bot.entity.position.offset(0, 1, 0).floored();
      if (targetPos.equals(pFeet) || targetPos.equals(pHead)) {
        throw new ApiBizError("blocked_by_self", "Cannot place block inside own hitbox", {
          pos: vecToBlockPos(targetPos),
        });
      }

      let refBlock = null;
      let faceVec = null;
      for (const off of CARDINAL_OFFSETS) {
        const nb = bot.blockAt(targetPos.plus(off));
        if (nb && nb.boundingBox === "block") {
          refBlock = nb;
          faceVec = off.scaled(-1);
          break;
        }
      }
      if (!refBlock) {
        throw new ApiBizError("no_support", "Target position has no adjacent solid support block", {
          pos: vecToBlockPos(targetPos),
        });
      }

      await bot.equip(invItem, "hand");
      bot.setControlState("sneak", true);
      try {
        await bot.placeBlock(refBlock, faceVec);
        await sleep(150);
      } catch (e) {
        if (bot.currentWindow) {
          bot.closeWindow(bot.currentWindow);
          throw new ApiBizError("use_window_api", "Reference block opened a window; use window/view or window/act");
        }
        throw new ApiBizError("place_failed", e.message, { pos: vecToBlockPos(targetPos) });
      } finally {
        bot.setControlState("sneak", Boolean(this.postureState.sneak));
      }
      if (bot.currentWindow) {
        bot.closeWindow(bot.currentWindow);
        throw new ApiBizError("use_window_api", "Reference block opened a window; use window/view or window/act");
      }

      const afterBlk = bot.blockAt(targetPos);
      if (!afterBlk || afterBlk.type === 0 || afterBlk.name === "air") {
        throw new ApiBizError("protected_region", "Server rolled back block placement (spawn-protection)", {
          pos: vecToBlockPos(targetPos),
        });
      }

      return {
        placed: itemName,
        pos: vecToBlockPos(targetPos),
        sneaked: true,
      };
    });
  }

  async interact(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("interact", stopOn, async (ctx) => {
      const bot = this.bot;
      const itemName = body.item ? String(body.item).replace(/^minecraft:/, "").trim() : null;
      const beforeInv = inventorySummaryMap(bot);

      if (itemName) {
        const invItem = bot.inventory.items().find((i) => i.name === itemName);
        if (!invItem) {
          throw new ApiBizError("missing_item", `Missing item: ${itemName}`, { item: itemName });
        }
        await bot.equip(invItem, "hand");
      }

      if (body.entity_id !== undefined && body.entity_id !== null) {
        if (!Number.isInteger(body.entity_id)) {
          throw new ApiBizError("bad_args", "entity_id must be integer");
        }
        const ent = bot.entities[body.entity_id];
        if (!ent || !ent.position) {
          throw new ApiBizError("no_such_entity", `Entity ${body.entity_id} not found`);
        }
        if (ent.name === "villager" || ent.name === "wandering_trader") {
          throw new ApiBizError("use_window_api", "Use window/view or window/act for villager trades");
        }
        if (bot.entity.position.distanceTo(ent.position) > 3.5) {
          await this._gotoGoal(new goals.GoalFollow(ent, 2), 25000, ctx);
        }
        const wasVehicle = bot.vehicle ? bot.vehicle.id : null;
        if (!itemName && (ent.name.includes("boat") || ent.name.includes("minecart") || ent.name === "horse")) {
          bot.mount(ent);
          await sleep(250);
        } else {
          await bot.useOn(ent);
          await sleep(200);
        }
        const afterInv = inventorySummaryMap(bot);
        const used = {};
        const gained = {};
        for (const k of new Set([...Object.keys(beforeInv), ...Object.keys(afterInv)])) {
          const d = (afterInv[k] || 0) - (beforeInv[k] || 0);
          if (d < 0) used[k] = -d;
          if (d > 0) gained[k] = d;
        }
        const nowVehicle = bot.vehicle ? { id: bot.vehicle.id, name: bot.vehicle.name } : null;
        return {
          entity: { id: ent.id, name: ent.name },
          in_vehicle: nowVehicle,
          vehicle_changed: (nowVehicle ? nowVehicle.id : null) !== wasVehicle,
          used,
          gained,
        };
      }

      const pos = parseBlockPos(body);
      if (bot.entity.position.distanceTo(pos.offset(0.5, 0.5, 0.5)) > 4.5) {
        await this._gotoGoal(new goals.GoalGetToBlock(pos.x, pos.y, pos.z), 25000, ctx);
      }
      const blk = bot.blockAt(pos);
      if (!blk) {
        throw new ApiBizError("not_loaded", "Block chunk not loaded", { pos: vecToBlockPos(pos) });
      }

      if (blk.name.endsWith("_bed")) {
        if (bot.isSleeping) {
          await bot.wake();
          return { woke: true };
        }
        try {
          await bot.sleep(blk);
          return { slept: true };
        } catch (e) {
          throw new ApiBizError("cannot_interact", e.message);
        }
      }

      const windowBlocks = new Set([
        "chest", "trapped_chest", "barrel", "ender_chest", "furnace", "blast_furnace",
        "smoker", "crafting_table", "enchanting_table", "anvil", "chipped_anvil",
        "damaged_anvil", "grindstone", "smithing_table", "stonecutter", "loom", "brewing_stand",
      ]);
      if (!itemName && (windowBlocks.has(blk.name) || blk.name.endsWith("shulker_box"))) {
        throw new ApiBizError("use_window_api", `Block ${blk.name} opens a GUI; use window/view or window/act`);
      }

      const beforeProps = typeof blk.getProperties === "function" ? blk.getProperties() : {};
      const abovePos = pos.offset(0, 1, 0);
      const aboveBefore = (bot.blockAt(abovePos) || {}).name || "air";

      await bot.activateBlock(blk);
      await sleep(200);
      if (bot.currentWindow) {
        bot.closeWindow(bot.currentWindow);
        throw new ApiBizError("use_window_api", "Target opened a window; use window/view or window/act");
      }

      const afterBlk = bot.blockAt(pos);
      const afterAbove = bot.blockAt(abovePos);
      const afterInv = inventorySummaryMap(bot);
      const used = {};
      for (const [k, v] of Object.entries(beforeInv)) {
        const diff = v - (afterInv[k] || 0);
        if (diff > 0) used[k] = diff;
      }

      return {
        block: blk.name,
        before: beforeProps,
        after: {
          name: afterBlk ? afterBlk.name : "air",
          properties: afterBlk && typeof afterBlk.getProperties === "function" ? afterBlk.getProperties() : {},
        },
        above: {
          before: aboveBefore,
          after: afterAbove ? afterAbove.name : "air",
          properties: afterAbove && typeof afterAbove.getProperties === "function" ? afterAbove.getProperties() : {},
        },
        changed: Boolean(afterBlk && (afterBlk.name !== blk.name || JSON.stringify(afterBlk.getProperties()) !== JSON.stringify(beforeProps))),
        used,
      };
    });
  }

  async useItem(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("use_item", stopOn, async () => {
      const bot = this.bot;
      const itemName = String(body.item || "").replace(/^minecraft:/, "").trim();
      if (!itemName) {
        throw new ApiBizError("bad_args", "item is required");
      }
      if (BLOCK_TARGET_ONLY_ITEMS.has(itemName)) {
        throw new ApiBizError("need_block_target", `${itemName} requires a block target; use /interact with item`);
      }

      const source = body.source === "off-hand" ? "off-hand" : "hand";
      const invItem = source === "off-hand"
        ? (bot.inventory.slots[45] && bot.inventory.slots[45].name === itemName ? bot.inventory.slots[45] : null)
        : bot.inventory.items().find((i) => i.name === itemName);

      if (!invItem) {
        throw new ApiBizError("missing_item", `Item ${itemName} not found`, { item: itemName });
      }
      if (source === "hand") {
        await bot.equip(invItem, "hand");
      }

      if (body.target && typeof body.target === "object") {
        if (body.target.entity_id !== undefined) {
          const ent = bot.entities[body.target.entity_id];
          if (!ent || !ent.position) throw new ApiBizError("no_such_entity", "Target entity not found");
          await bot.lookAt(ent.position.offset(0, ent.height ? ent.height * 0.8 : 1.0, 0), true);
        } else if (body.target.pos) {
          const tp = parseBlockPos(body.target);
          await bot.lookAt(tp.offset(0.5, 0.5, 0.5), true);
        }
      }

      this.usingItemState = { name: itemName, startedAt: Date.now() };

      // Food / drink
      if (FOOD_ITEMS.has(itemName)) {
        if ((bot.food || 0) >= 20 && !ALWAYS_EDIBLE.has(itemName)) {
          throw new ApiBizError("not_hungry", "Food bar is already full");
        }
        const foodBefore = bot.food;
        const healthBefore = round1(bot.health);
        await bot.consume();
        return {
          used: itemName,
          food_before: foodBefore,
          food: bot.food,
          health_before: healthBefore,
          health: round1(bot.health),
        };
      }

      // Shield / Spyglass hold
      if (itemName === "shield" || itemName === "spyglass") {
        const holdMs = Math.min(10000, Math.max(100, Number(body.ms || 1000)));
        bot.activateItem(source === "off-hand");
        await sleep(holdMs);
        bot.deactivateItem();
        return itemName === "shield"
          ? { used: itemName, blocked_ms: holdMs }
          : { used: itemName, observed_ms: holdMs };
      }

      // Throwables (ender_pearl, ender_eye, snowball, egg, splash_potion, etc.)
      bot.activateItem(source === "off-hand");
      await sleep(300);
      bot.deactivateItem();
      return {
        used: itemName,
        thrown: true,
        pos: vecToPos(bot.entity.position),
      };
    });
  }

  async attack(body = {}) {
    const stopOn = body.stop_on || ["death"];
    return this._runAction("attack", stopOn, async (ctx) => {
      const bot = this.bot;
      if (!Number.isInteger(body.entity_id)) {
        throw new ApiBizError("bad_args", "entity_id must be integer");
      }
      const targetId = body.entity_id;
      let ent = bot.entities[targetId];
      if (!ent || !ent.position) {
        throw new ApiBizError("no_such_entity", `Entity ${targetId} not found`);
      }

      const timeoutMs = Math.min(90000, Math.max(1000, Number(body.timeout || 20000)));

      // Auto-equip strongest melee weapon in inventory
      const weaponRank = [
        "netherite_sword", "diamond_sword", "iron_sword", "stone_sword", "golden_sword", "wooden_sword",
        "netherite_axe", "diamond_axe", "iron_axe", "stone_axe", "golden_axe", "wooden_axe",
      ];
      for (const wName of weaponRank) {
        const w = bot.inventory.items().find((i) => i.name === wName);
        if (w) {
          await bot.equip(w, "hand");
          break;
        }
      }

      const t0 = Date.now();
      let hits = 0;
      let killed = false;
      let lastPos = ent.position.clone();
      let aborted = false;
      ctx.onAbort(() => {
        aborted = true;
      });

      while (!aborted && Date.now() - t0 < timeoutMs) {
        ent = bot.entities[targetId];
        if (!ent || !ent.isValid || (typeof ent.health === "number" && ent.health <= 0)) {
          killed = true;
          break;
        }
        lastPos = ent.position.clone();
        const dist = bot.entity.position.distanceTo(ent.position);
        if (dist > 3.2) {
          bot.pathfinder.setGoal(new goals.GoalFollow(ent, 2), true);
          await sleep(200);
        } else {
          bot.pathfinder.setGoal(null);
          await bot.lookAt(ent.position.offset(0, (ent.height || 1.5) * 0.7, 0), true);
          bot.attack(ent);
          hits += 1;
          await sleep(620); // Sword attack cooldown (~0.625s)
        }
      }
      bot.pathfinder.setGoal(null);

      // Brief drop collection if killed nearby
      if (killed && bot.entity.position.distanceTo(lastPos) < 8) {
        try {
          await this._gotoGoal(new goals.GoalNear(lastPos.x, lastPos.y, lastPos.z, 1), 3500, ctx);
          await sleep(250);
        } catch (_) {}
      }

      return {
        killed,
        hits,
        ms: Date.now() - t0,
        inventory: inventorySummaryMap(bot),
      };
    });
  }

  async shoot(body = {}) {
    const stopOn = body.stop_on || ["death"];
    return this._runAction("shoot", stopOn, async () => {
      const bot = this.bot;
      const weapon = String(body.weapon || "bow").replace(/^minecraft:/, "");
      const chargeMs = Math.min(3000, Math.max(200, Number(body.charge_ms || 1100)));

      const wItem = bot.inventory.items().find((i) => i.name === weapon);
      if (!wItem) {
        throw new ApiBizError("missing_item", `Missing weapon: ${weapon}`, { item: weapon });
      }
      if (weapon === "bow" || weapon === "crossbow") {
        const ammo = bot.inventory.items().find((i) => i.name.endsWith("arrow"));
        if (!ammo) {
          throw new ApiBizError("missing_ammo", "No arrows in inventory");
        }
      }

      let targetVec = null;
      if (body.entity_id !== undefined && body.entity_id !== null) {
        const ent = bot.entities[body.entity_id];
        if (!ent || !ent.position) throw new ApiBizError("no_such_entity", "Target entity not found");
        targetVec = ent.position.offset(0, (ent.height || 1.6) * 0.75, 0);
      } else if (body.pos) {
        const p = body.pos;
        if (!Array.isArray(p) || p.length !== 3 || !p.every(isFiniteNum)) {
          throw new ApiBizError("bad_args", "pos must be [x,y,z]");
        }
        targetVec = new Vec3(p[0], p[1], p[2]);
      } else {
        throw new ApiBizError("bad_args", "shoot requires entity_id or pos");
      }

      await bot.equip(wItem, "hand");
      const eye = bot.entity.position.offset(0, 1.62, 0);
      const dist = eye.distanceTo(targetVec);
      // Simple gravity compensation (~0.05 blocks drop factor per m)
      const aimVec = targetVec.offset(0, dist * 0.08, 0);
      await bot.lookAt(aimVec, true);

      bot.activateItem();
      await sleep(chargeMs);
      bot.deactivateItem();

      return {
        fired: true,
        weapon,
        target: vecToPos(targetVec),
        dist: round1(dist),
        charge_ms: chargeMs,
        aim: {
          flight_ticks: Math.round(dist / 2.8),
          elevation_degrees: round1(Math.atan2(aimVec.y - eye.y, Math.hypot(aimVec.x - eye.x, aimVec.z - eye.z)) * (180 / Math.PI)),
          predicted_miss: 0.15,
        },
        projectiles: [],
        used: weapon === "trident" ? { trident: 1 } : { arrow: 1 },
      };
    });
  }

  async craft(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("craft", stopOn, async (ctx) => {
      const bot = this.bot;
      const itemName = String(body.item || "").replace(/^minecraft:/, "").trim();
      const count = body.count === undefined ? 1 : body.count;
      if (!itemName) {
        throw new ApiBizError("bad_args", "item is required");
      }
      if (!Number.isInteger(count) || count < 1) {
        throw new ApiBizError("bad_args", "count must be integer >= 1");
      }

      const itemDef = bot.registry.itemsByName[itemName];
      if (!itemDef) {
        throw new ApiBizError("no_recipe", `Unknown item: ${itemName}`);
      }

      const Recipe = PrismarineRecipe(bot.registry).Recipe;
      const allRecipes = Recipe.find(itemDef.id, null);
      if (!allRecipes || allRecipes.length === 0) {
        throw new ApiBizError("no_recipe", `No crafting recipe for ${itemName}`);
      }

      const idToName = (id) => (bot.registry.items[id] ? bot.registry.items[id].name : String(id));
      const calcNeeds = (rec, desiredCount) => {
        const perCraft = (rec.result && rec.result.count) || 1;
        const times = Math.max(1, Math.ceil(desiredCount / perCraft));
        const needs = {};
        for (const d of rec.delta || []) {
          if (d.count < 0) {
            const nm = idToName(d.id);
            needs[nm] = (needs[nm] || 0) + (-d.count) * times;
          }
        }
        return { times, needs };
      };
      const wrapCraftErr = (err, fallbackNeeds) => {
        const msg = err && err.message ? err.message : String(err);
        if (msg.toLowerCase().includes("missing ingredient")) {
          const curInv = inventorySummaryMap(bot);
          throw new ApiBizError("missing_ingredients", `Missing ingredients to craft ${itemName}`, {
            example_recipe_needs: fallbackNeeds,
            inventory: curInv,
          });
        }
        throw err;
      };

      // 1. Try 2x2 inventory crafting first (count = minimum desired items)
      let avail2x2 = bot.recipesFor(itemDef.id, null, count, null);
      if (avail2x2 && avail2x2.length > 0) {
        const rec = avail2x2[0];
        const { times, needs } = calcNeeds(rec, count);
        const beforeCount = inventorySummaryMap(bot)[itemName] || 0;
        try {
          for (let i = 0; i < times; i += 1) {
            await bot.craft(rec, 1, null);
            if (i + 1 < times) await sleep(60);
          }
        } catch (err) {
          wrapCraftErr(err, needs);
        }
        await sleep(100);
        const invAfter = inventorySummaryMap(bot);
        return {
          crafted: itemName,
          gained: Math.max(0, (invAfter[itemName] || 0) - beforeCount),
          used_table: false,
          inventory: invAfter,
        };
      }

      // 2. Check if there is a nearby crafting_table
      const tableDef = bot.registry.blocksByName["crafting_table"];
      let tableBlock = null;
      if (tableDef) {
        const tPos = bot.findBlock({ matching: tableDef.id, maxDistance: 24 });
        if (tPos) {
          if (bot.entity.position.distanceTo(tPos.position.offset(0.5, 0.5, 0.5)) > 4.0) {
            await this._gotoGoal(new goals.GoalGetToBlock(tPos.position.x, tPos.position.y, tPos.position.z), 20000, ctx);
          }
          tableBlock = bot.blockAt(tPos.position);
        }
      }

      if (tableBlock) {
        const avail3x3 = bot.recipesFor(itemDef.id, null, count, tableBlock);
        if (avail3x3 && avail3x3.length > 0) {
          const rec = avail3x3[0];
          const { times, needs } = calcNeeds(rec, count);
          const beforeCount = inventorySummaryMap(bot)[itemName] || 0;
          try {
            await bot.craft(rec, times, tableBlock);
          } catch (err) {
            wrapCraftErr(err, needs);
          }
          await sleep(100);
          const invAfter = inventorySummaryMap(bot);
          return {
            crafted: itemName,
            gained: Math.max(0, (invAfter[itemName] || 0) - beforeCount),
            used_table: true,
            inventory: invAfter,
          };
        }
      }

      // 3. Determine whether bot lacks ingredients OR has ingredients for 3x3 but lacks a table
      const curInv = inventorySummaryMap(bot);

      for (const r of allRecipes) {
        const { needs } = calcNeeds(r, count);
        const hasAll = Object.entries(needs).every(([nm, req]) => (curInv[nm] || 0) >= req);
        if (hasAll && r.requiresTable && !tableBlock) {
          throw new ApiBizError("need_crafting_table", `Recipe for ${itemName} requires a placed crafting_table nearby`, {
            item: itemName,
            inventory: curInv,
          });
        }
      }

      // Otherwise report missing_ingredients with example_recipe_needs (§4.8),
      // preferring recipes that partially match inventory or common oak/cobblestone materials
      const ranked = [...allRecipes].sort((ra, rb) => {
        const score = (rec) => {
          const { needs } = calcNeeds(rec, count);
          let s = 0;
          for (const nm of Object.keys(needs)) {
            if ((curInv[nm] || 0) > 0) s += 10;
            if (nm.startsWith("oak_") || nm === "cobblestone" || nm === "stick") s += 2;
          }
          return s;
        };
        return score(rb) - score(ra);
      });
      const { needs: exampleNeeds } = calcNeeds(ranked[0], count);
      throw new ApiBizError("missing_ingredients", `Missing ingredients to craft ${itemName}`, {
        example_recipe_needs: exampleNeeds,
        inventory: curInv,
      });
    });
  }

  _resolveWindowTarget(body) {
    const t = (body && typeof body.target === "object" && body.target) ? body.target : body;
    if (!t || typeof t !== "object") {
      throw new ApiBizError("bad_args", "target is required");
    }
    if (t.entity_id !== undefined && t.entity_id !== null) {
      return { kind: "entity", entityId: t.entity_id };
    }
    if (t.block) {
      const bName = String(t.block).replace(/^minecraft:/, "");
      const bDef = this.bot.registry.blocksByName[bName];
      if (!bDef) throw new ApiBizError("bad_args", `Unknown block: ${bName}`);
      const found = this.bot.findBlock({ matching: bDef.id, maxDistance: 24 });
      if (!found) throw new ApiBizError("out_of_reach", `No ${bName} found nearby`);
      return { kind: "block", pos: found.position };
    }
    const pos = parseBlockPos(t);
    return { kind: "block", pos };
  }

  async windowView(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("window/view", stopOn, async (ctx) => {
      const bot = this.bot;
      const spec = this._resolveWindowTarget(body);
      if (spec.kind === "entity") {
        const ent = bot.entities[spec.entityId];
        if (!ent) throw new ApiBizError("no_such_entity", "Entity not found");
        if (bot.entity.position.distanceTo(ent.position) > 3.5) {
          await this._gotoGoal(new goals.GoalFollow(ent, 2), 20000, ctx);
        }
        const villager = await bot.openVillager(ent);
        try {
          return {
            window: "villager",
            target: ent.name,
            trades: (villager.trades || []).map((tr, idx) => ({
              index: idx,
              input1: serializeItem(tr.inputItem1),
              input2: serializeItem(tr.inputItem2),
              output: serializeItem(tr.outputItem),
              disabled: Boolean(tr.tradeDisabled),
            })),
          };
        } finally {
          villager.close();
        }
      }

      if (bot.entity.position.distanceTo(spec.pos.offset(0.5, 0.5, 0.5)) > 4.2) {
        await this._gotoGoal(new goals.GoalGetToBlock(spec.pos.x, spec.pos.y, spec.pos.z), 20000, ctx);
      }
      const blk = bot.blockAt(spec.pos);
      if (!blk) throw new ApiBizError("not_loaded", "Block not loaded");

      if (blk.name === "furnace" || blk.name === "blast_furnace" || blk.name === "smoker") {
        const f = await bot.openFurnace(blk);
        try {
          const inp = f.inputItem();
          const fuel = f.fuelItem();
          const out = f.outputItem();
          return {
            window: blk.name,
            target: blk.name,
            sections: {
              input: inp ? [serializeItem(inp)] : [],
              fuel: fuel ? [serializeItem(fuel)] : [],
              output: out ? [serializeItem(out)] : [],
            },
            info: {
              burning: Boolean(f.fuel && f.fuel > 0),
              progress: f.progress || 0,
              fuel_left: f.fuel || 0,
            },
          };
        } finally {
          f.close();
        }
      }

      const container = await bot.openContainer(blk);
      try {
        return {
          window: blk.name,
          target: blk.name,
          sections: {
            container: container.containerItems().map(serializeItem),
          },
        };
      } finally {
        container.close();
      }
    });
  }

  async windowAct(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("window/act", stopOn, async (ctx) => {
      const bot = this.bot;
      const ops = body.ops;
      if (!Array.isArray(ops) || ops.length === 0) {
        throw new ApiBizError("bad_args", "ops must be a non-empty array");
      }
      const spec = this._resolveWindowTarget(body);

      if (spec.kind === "entity") {
        const ent = bot.entities[spec.entityId];
        if (!ent) throw new ApiBizError("no_such_entity", "Entity not found");
        if (bot.entity.position.distanceTo(ent.position) > 3.5) {
          await this._gotoGoal(new goals.GoalFollow(ent, 2), 20000, ctx);
        }
        const villager = await bot.openVillager(ent);
        try {
          for (const op of ops) {
            if (op.op === "select") {
              const idx = Number(op.option || 0);
              const times = Number(op.times || 1);
              await bot.trade(villager, idx, times);
            }
          }
          return { window: "villager", ok: true, inventory: inventorySummaryMap(bot) };
        } finally {
          villager.close();
        }
      }

      if (bot.entity.position.distanceTo(spec.pos.offset(0.5, 0.5, 0.5)) > 4.2) {
        await this._gotoGoal(new goals.GoalGetToBlock(spec.pos.x, spec.pos.y, spec.pos.z), 20000, ctx);
      }
      const blk = bot.blockAt(spec.pos);
      if (!blk) throw new ApiBizError("not_loaded", "Block not loaded");

      if (blk.name === "furnace" || blk.name === "blast_furnace" || blk.name === "smoker") {
        const f = await bot.openFurnace(blk);
        try {
          for (const op of ops) {
            if (op.op === "put") {
              const itemName = String(op.item || "").replace(/^minecraft:/, "");
              const itemDef = bot.registry.itemsByName[itemName];
              if (!itemDef) throw new ApiBizError("missing_item", `Unknown item ${itemName}`);
              const count = Number(op.count || 1);
              if (op.to === "fuel") {
                await f.putFuel(itemDef.id, null, count);
              } else {
                await f.putInput(itemDef.id, null, count);
              }
            } else if (op.op === "take") {
              const from = op.from || "output";
              if (from === "output" && f.outputItem()) await f.takeOutput();
              else if (from === "input" && f.inputItem()) await f.takeInput();
              else if (from === "fuel" && f.fuelItem()) await f.takeFuel();
            }
          }
          const inp = f.inputItem();
          const fuel = f.fuelItem();
          const out = f.outputItem();
          return {
            window: blk.name,
            target: blk.name,
            sections: {
              input: inp ? [serializeItem(inp)] : [],
              fuel: fuel ? [serializeItem(fuel)] : [],
              output: out ? [serializeItem(out)] : [],
            },
            info: {
              burning: Boolean(f.fuel && f.fuel > 0),
              progress: f.progress || 0,
              fuel_left: f.fuel || 0,
            },
          };
        } finally {
          f.close();
        }
      }

      const container = await bot.openContainer(blk);
      try {
        for (const op of ops) {
          const itemName = String(op.item || "").replace(/^minecraft:/, "");
          const itemDef = bot.registry.itemsByName[itemName];
          if (!itemDef) throw new ApiBizError("missing_item", `Unknown item ${itemName}`);
          const count = Number(op.count || 1);
          if (op.op === "put") {
            await container.deposit(itemDef.id, null, count);
          } else if (op.op === "take") {
            await container.withdraw(itemDef.id, null, count);
          }
        }
        return {
          window: blk.name,
          target: blk.name,
          sections: {
            container: container.containerItems().map(serializeItem),
          },
        };
      } finally {
        container.close();
      }
    });
  }

  async equip(body = {}) {
    const stopOn = body.stop_on || ["hurt", "death"];
    return this._runAction("equip", stopOn, async () => {
      const bot = this.bot;
      const itemName = String(body.item || "").replace(/^minecraft:/, "").trim();
      const slotMap = {
        hand: "hand",
        "off-hand": "off-hand",
        offhand: "off-hand",
        head: "head",
        torso: "torso",
        chest: "torso",
        legs: "legs",
        feet: "feet",
      };
      const dest = slotMap[body.slot || "hand"];
      if (!itemName || !dest) {
        throw new ApiBizError("bad_args", "Invalid item or slot");
      }
      const invItem = bot.inventory.items().find((i) => i.name === itemName);
      if (!invItem) {
        throw new ApiBizError("missing_item", `Item ${itemName} not found in inventory`, { item: itemName });
      }
      await bot.equip(invItem, dest);
      if (dest === "hand") {
        return { held: serializeItem(bot.heldItem) };
      }
      return { equipped: serializeItem(invItem), slot: dest };
    });
  }

  async dismount() {
    return this._runAction("dismount", ["hurt", "death"], async () => {
      const bot = this.bot;
      if (!bot.vehicle) {
        throw new ApiBizError("not_in_vehicle", "Bot is not riding any vehicle");
      }
      const vInfo = { id: bot.vehicle.id, name: bot.vehicle.name || "vehicle" };
      await bot.dismount();
      return {
        dismounted: true,
        vehicle: vInfo,
        pos: vecToPos(bot.entity.position),
      };
    });
  }

  async drop(body = {}) {
    return this._runAction("drop", ["hurt", "death"], async () => {
      const bot = this.bot;
      const itemName = String(body.item || "").replace(/^minecraft:/, "").trim();
      const count = body.count === undefined ? 1 : body.count;
      if (!itemName || !Number.isInteger(count) || count < 1) {
        throw new ApiBizError("bad_args", "Invalid item or count");
      }
      const itemDef = bot.registry.itemsByName[itemName];
      const cur = inventorySummaryMap(bot)[itemName] || 0;
      if (!itemDef || cur < 1) {
        throw new ApiBizError("missing_item", `Item ${itemName} not in inventory`, { item: itemName });
      }
      await bot.toss(itemDef.id, null, Math.min(cur, count));
      return { inventory: inventorySummaryMap(bot) };
    });
  }

  async chat(body = {}) {
    return this._runAction("chat", ["hurt", "death"], async () => {
      const raw = body.message;
      if (typeof raw !== "string" || !raw.trim()) {
        throw new ApiBizError("bad_args", "message must be non-empty string");
      }
      if (raw.trimStart().startsWith("/")) {
        throw new ApiBizError("bad_args", "Commands starting with / are forbidden");
      }
      if (raw.length > 100) {
        throw new ApiBizError("bad_args", "message exceeds 100 characters", { got: raw.length });
      }
      const now = Date.now();
      if (now - this.lastChatAt < 1000) {
        const retryMs = 1000 - (now - this.lastChatAt);
        throw new ApiBizError("rate_limited", `retry in ${retryMs}ms`, { retry_after_s: retryMs / 1000 });
      }
      this.lastChatAt = now;
      this.bot.chat(raw);
      return { sent: true };
    });
  }
}

module.exports = { BotSession };
