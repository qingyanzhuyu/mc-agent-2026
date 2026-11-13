"use strict";

const fs = require("node:fs");
const path = require("node:path");
const http = require("node:http");
const crypto = require("node:crypto");
const { URL } = require("node:url");

const { ApiBizError } = require("./lib/helpers");
const { BotSession } = require("./lib/bot_session");

function loadDotEnv(envPath = path.join(__dirname, ".env")) {
  if (!fs.existsSync(envPath)) return;
  const content = fs.readFileSync(envPath, "utf8");
  for (const rawLine of content.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#")) continue;
    const eq = line.indexOf("=");
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let val = line.slice(eq + 1).trim();
    if (
      (val.startsWith('"') && val.endsWith('"')) ||
      (val.startsWith("'") && val.endsWith("'"))
    ) {
      val = val.slice(1, -1);
    }
    if (process.env[key] === undefined) {
      process.env[key] = val;
    }
  }
}

loadDotEnv();

function sanitizeBotName(raw) {
  const cleaned = String(raw || "")
    .trim()
    .replace(/[^A-Za-z0-9_]/g, "_")
    .slice(0, 16);
  if (cleaned.length < 2) return null;
  return cleaned;
}

function sendJson(res, statusCode, payload, extraHeaders = null) {
  const data = JSON.stringify(payload);
  const headers = {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(data),
    ...(extraHeaders || {}),
  };
  res.writeHead(statusCode, headers);
  res.end(data);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let total = 0;
    req.on("data", (chunk) => {
      total += chunk.length;
      if (total > 1024 * 1024) {
        reject(new ApiBizError("bad_json", "Payload too large", {}, 400));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      if (chunks.length === 0) {
        resolve({});
        return;
      }
      const raw = Buffer.concat(chunks).toString("utf8").trim();
      if (!raw) {
        resolve({});
        return;
      }
      try {
        const parsed = JSON.parse(raw);
        if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
          reject(new ApiBizError("bad_json", "JSON body must be an object", {}, 400));
          return;
        }
        resolve(parsed);
      } catch (_) {
        reject(new ApiBizError("bad_json", "Invalid JSON body", {}, 400));
      }
    });
    req.on("error", (err) => reject(err));
  });
}

const KNOWN_ROUTES = new Set([
  "POST auth/login",
  "POST auth/logout",
  "POST connect",
  "POST disconnect",
  "GET status",
  "GET inventory",
  "POST find_blocks",
  "POST get_block",
  "POST get_entities",
  "GET events",
  "POST events",
  "POST advancements",
  "POST move_to",
  "POST dig",
  "POST place",
  "POST interact",
  "POST use_item",
  "POST attack",
  "POST shoot",
  "POST craft",
  "POST window/view",
  "POST window/act",
  "POST equip",
  "POST posture",
  "POST dismount",
  "POST drop",
  "POST chat",
  "POST stop",
]);

function createBridgeServer(opts = {}) {
  const mcHost = opts.mcHost || process.env.MC_HOST || "127.0.0.1";
  const mcPort = Number(opts.mcPort ?? process.env.MC_PORT ?? 25565);
  const mcVersion = opts.mcVersion || process.env.MC_VERSION || "1.21.11";
  const tokenTtlMs = Number(opts.tokenTtlMs ?? process.env.TOKEN_TTL_MS ?? 24 * 3600 * 1000);
  const reconnectIntervalMs = Number(
    opts.reconnectIntervalMs ?? process.env.RECONNECT_INTERVAL_MS ?? 30000
  );
  const log = opts.log || console.log;
  const sessionFactory =
    opts.sessionFactory ||
    ((cfg) => new BotSession(cfg));

  // username -> BotSession
  const sessionsByUser = new Map();
  // token -> BotSession
  const sessionsByToken = new Map();

  function authenticate(req) {
    const authHdr = req.headers["authorization"] || "";
    if (!authHdr || !authHdr.startsWith("Bearer ")) {
      throw new ApiBizError("unauthorized", "Missing Authorization: Bearer <token>", {}, 401);
    }
    const token = authHdr.slice("Bearer ".length).trim();
    if (!token) {
      throw new ApiBizError("unauthorized", "Empty Bearer token", {}, 401);
    }
    const session = sessionsByToken.get(token);
    if (!session || session.token !== token) {
      throw new ApiBizError("invalid_token", "Token is invalid or superseded by a newer login", {}, 401);
    }
    if (session.expiresAt && Date.now() > session.expiresAt) {
      sessionsByToken.delete(token);
      throw new ApiBizError("invalid_token", "Token expired", {}, 401);
    }
    return session;
  }

  const server = http.createServer(async (req, res) => {
    const t0 = Date.now();
    const parsedUrl = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
    const pathname = parsedUrl.pathname.replace(/\/+$/, "") || "/";
    const method = (req.method || "GET").toUpperCase();

    if (method === "GET" && pathname === "/healthz") {
      return sendJson(res, 200, {
        ok: true,
        mc_target: `${mcHost}:${mcPort}`,
        mc_version: mcVersion,
        active_bots: [...sessionsByUser.values()].map((s) => ({
          username: s.username,
          ready: s.isReady(),
          busy: s.busy,
        })),
      });
    }

    const prefix = "/api/v1/";
    if (!pathname.startsWith(prefix)) {
      return sendJson(res, 404, {
        ok: false,
        ms: Date.now() - t0,
        error: { code: "unknown_api", message: `Unknown endpoint: ${pathname}` },
      });
    }
    const route = pathname.slice(prefix.length);
    if (!KNOWN_ROUTES.has(`${method} ${route}`)) {
      return sendJson(res, 404, {
        ok: false,
        ms: Date.now() - t0,
        error: { code: "unknown_api", message: `Unknown route: ${method} /${route}` },
      });
    }

    try {
      let body = {};
      if (method === "POST") {
        body = await readBody(req);
      }

      // ---- 1.1 Login
      if (method === "POST" && route === "auth/login") {
        const botUsername = sanitizeBotName(body.username);
        const pass = String(body.password || "");
        if (!botUsername || !pass) {
          throw new ApiBizError(
            "invalid_credentials",
            "username (2-16 alnum/_) and non-empty password are required",
            {},
            401
          );
        }

        let session = sessionsByUser.get(botUsername);
        if (!session) {
          session = sessionFactory({
            username: botUsername,
            mcHost,
            mcPort,
            mcVersion,
            reconnectIntervalMs,
            log,
          });
          sessionsByUser.set(botUsername, session);
        } else if (session.token) {
          // §1.1 Exclusive login: invalidate previous token immediately
          sessionsByToken.delete(session.token);
        }

        const newToken = crypto.randomBytes(32).toString("hex");
        const expiresAtMs = Date.now() + tokenTtlMs;
        const expiresIso = new Date(expiresAtMs).toISOString();

        session.token = newToken;
        session.expiresAt = expiresAtMs;
        sessionsByToken.set(newToken, session);

        // Auto-connect to MC server on login (§1.1)
        try {
          await session.ensureConnected({ enforceCooldown: false });
        } catch (e) {
          log(`[auth] Auto-connect deferred for ${botUsername}: ${e.code}`);
        }

        log(`[http] POST /auth/login user=${botUsername} -> 200 (${Date.now() - t0}ms)`);
        return sendJson(res, 200, {
          ok: true,
          ms: Date.now() - t0,
          token: newToken,
          expires_at: expiresIso,
          bot_username: botUsername,
        });
      }

      // All other /api/v1/* endpoints require Bearer token
      const session = authenticate(req);

      // ---- 1.2 Logout
      if (method === "POST" && route === "auth/logout") {
        if (session.token) sessionsByToken.delete(session.token);
        session.token = null;
        session.disconnect();
        return sendJson(res, 200, { ok: true, ms: Date.now() - t0 });
      }

      let result;
      if (method === "POST" && route === "connect") {
        result = await session.ensureConnected({ enforceCooldown: true });
      } else if (method === "POST" && route === "disconnect") {
        result = session.disconnect();
      } else if (method === "GET" && route === "status") {
        result = session.getStatus();
      } else if (method === "GET" && route === "inventory") {
        result = session.getInventory();
      } else if (method === "POST" && route === "find_blocks") {
        result = session.findBlocks(body);
      } else if (method === "POST" && route === "get_block") {
        result = session.getBlock(body);
      } else if (method === "POST" && route === "get_entities") {
        result = session.getEntities(body);
      } else if ((method === "GET" || method === "POST") && route === "events") {
        const merged = {};
        for (const [k, v] of parsedUrl.searchParams.entries()) {
          merged[k] = v;
        }
        Object.assign(merged, body);
        result = session.getEvents(merged);
      } else if (method === "POST" && route === "advancements") {
        result = session.getAdvancements(body);
      } else if (method === "POST" && route === "move_to") {
        result = await session.moveTo(body);
      } else if (method === "POST" && route === "dig") {
        result = await session.dig(body);
      } else if (method === "POST" && route === "place") {
        result = await session.place(body);
      } else if (method === "POST" && route === "interact") {
        result = await session.interact(body);
      } else if (method === "POST" && route === "use_item") {
        result = await session.useItem(body);
      } else if (method === "POST" && route === "attack") {
        result = await session.attack(body);
      } else if (method === "POST" && route === "shoot") {
        result = await session.shoot(body);
      } else if (method === "POST" && route === "craft") {
        result = await session.craft(body);
      } else if (method === "POST" && route === "window/view") {
        result = await session.windowView(body);
      } else if (method === "POST" && route === "window/act") {
        result = await session.windowAct(body);
      } else if (method === "POST" && route === "equip") {
        result = await session.equip(body);
      } else if (method === "POST" && route === "posture") {
        result = await session.posture(body);
      } else if (method === "POST" && route === "dismount") {
        result = await session.dismount();
      } else if (method === "POST" && route === "drop") {
        result = await session.drop(body);
      } else if (method === "POST" && route === "chat") {
        result = await session.chat(body);
      } else if (method === "POST" && route === "stop") {
        result = await session.stop();
      } else {
        return sendJson(res, 404, {
          ok: false,
          ms: Date.now() - t0,
          error: { code: "unknown_api", message: `Unknown route: ${method} /${route}` },
        });
      }

      const ms = Date.now() - t0;
      if (route !== "status" && route !== "events") {
        log(`[http] ${method} /${route} (${session.username}) -> ok (${ms}ms)`);
      }
      return sendJson(res, 200, { ok: true, ms, result });
    } catch (err) {
      const ms = Date.now() - t0;
      if (err instanceof ApiBizError) {
        const httpCode = err.http || 200;
        if (httpCode !== 401 || route !== "status") {
          log(`[http] ${method} /${route} -> err=${err.code} http=${httpCode} (${ms}ms)`);
        }
        return sendJson(res, httpCode, {
          ok: false,
          ms,
          error: { code: err.code, message: err.msg, ...err.extra },
        });
      }
      console.error(`[http] Unexpected error on ${method} /${route}:`, err);
      return sendJson(res, 200, {
        ok: false,
        ms,
        error: { code: "bad_args", message: err && err.message ? err.message : String(err) },
      });
    }
  });

  function disconnectAll() {
    for (const s of sessionsByUser.values()) {
      try {
        s.disconnect();
      } catch (_) {}
    }
  }

  return { server, sessionsByUser, sessionsByToken, disconnectAll, config: { mcHost, mcPort, mcVersion } };
}

if (require.main === module) {
  const bridgeHost = process.env.BRIDGE_HOST || "127.0.0.1";
  const bridgePort = Number(process.env.BRIDGE_PORT || 8787);
  const { server, disconnectAll, config } = createBridgeServer();

  function shutdown(sig) {
    console.log(`\n[bridge] Caught ${sig}, disconnecting all bots...`);
    disconnectAll();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 1500).unref();
  }

  process.on("SIGINT", () => shutdown("SIGINT"));
  process.on("SIGTERM", () => shutdown("SIGTERM"));

  server.listen(bridgePort, bridgeHost, () => {
    console.log(
      `[bridge] NMO API v1.3.1 Local Bridge listening on http://${bridgeHost}:${bridgePort}/api/v1` +
        ` -> MC ${config.mcHost}:${config.mcPort} (version=${config.mcVersion})`
    );
  });
}

module.exports = { createBridgeServer, sanitizeBotName, loadDotEnv };
