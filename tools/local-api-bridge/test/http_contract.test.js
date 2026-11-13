"use strict";

const test = require("node:test");
const assert = require("node:assert/strict");
const { createBridgeServer } = require("../server");
const { ApiBizError } = require("../lib/helpers");

class FakeSession {
  constructor({ username }) {
    this.username = username;
    this.token = null;
    this.expiresAt = null;
    this.busy = null;
    this.connected = false;
  }
  isReady() {
    return this.connected;
  }
  async ensureConnected() {
    this.connected = true;
    return { ok: true, ready: true };
  }
  disconnect() {
    this.connected = false;
    return { ok: true };
  }
  getStatus() {
    if (!this.connected) {
      throw new ApiBizError("not_connected", "Bot is not connected");
    }
    return { username: this.username, ready: true, alive: true, busy: this.busy, pos: [0, 64, 0], health: 20 };
  }
  getEvents(params) {
    return {
      since: Number(params.since || 0),
      last_seq: 1,
      total: 1,
      has_more: false,
      events: [{ seq: 1, t: Date.now(), type: "spawn", pos: [0, 64, 0] }],
    };
  }
}

async function withServer(fn) {
  const ctx = createBridgeServer({
    mcHost: "127.0.0.1",
    mcPort: 25565,
    log: () => {},
    sessionFactory: (cfg) => new FakeSession(cfg),
  });
  await new Promise((resolve) => ctx.server.listen(0, "127.0.0.1", resolve));
  const addr = ctx.server.address();
  const base = `http://127.0.0.1:${addr.port}`;
  try {
    await fn(base, ctx);
  } finally {
    ctx.disconnectAll();
    await new Promise((resolve) => ctx.server.close(resolve));
  }
}

test("HTTP contract: /healthz, 401 unauthorized, 404 unknown_api, 400 bad_json", async () => {
  await withServer(async (base) => {
    const hz = await fetch(`${base}/healthz`);
    assert.equal(hz.status, 200);
    const hzJson = await hz.json();
    assert.equal(hzJson.ok, true);

    // Matches `python -m mcagent.smoke --dry` probe expectation
    const unauth = await fetch(`${base}/api/v1/status`);
    assert.equal(unauth.status, 401);
    const unauthJson = await unauth.json();
    assert.equal(unauthJson.ok, false);
    assert.equal(unauthJson.error.code, "unauthorized");

    const notFound = await fetch(`${base}/api/v1/nonexistent`);
    assert.equal(notFound.status, 404);
    const nfJson = await notFound.json();
    assert.equal(nfJson.error.code, "unknown_api");

    const badJson = await fetch(`${base}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{broken",
    });
    assert.equal(badJson.status, 400);
    const bjPayload = await badJson.json();
    assert.equal(bjPayload.error.code, "bad_json");
  });
});

test("Auth & multi-teammate session isolation: §1.1 duplicate login invalidates old token, distinct usernames stay independent", async () => {
  await withServer(async (base) => {
    const badLogin = await fetch(`${base}/api/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username: "a", password: "" }),
    });
    assert.equal(badLogin.status, 401);
    assert.equal((await badLogin.json()).error.code, "invalid_credentials");

    // Teammate A first login
    const loginA1 = await (
      await fetch(`${base}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "bot_alice", password: "dev" }),
      })
    ).json();
    assert.equal(loginA1.ok, true);
    assert.equal(loginA1.bot_username, "bot_alice");

    // Teammate B logs into the same bridge concurrently
    const loginB = await (
      await fetch(`${base}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "bot_bob", password: "dev" }),
      })
    ).json();
    assert.equal(loginB.ok, true);

    // Teammate A logs in again -> old tokenA1 must immediately return 401 invalid_token (§1.1)
    const loginA2 = await (
      await fetch(`${base}/api/v1/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: "bot_alice", password: "dev" }),
      })
    ).json();
    assert.equal(loginA2.ok, true);
    assert.notEqual(loginA1.token, loginA2.token);

    const oldAStatus = await fetch(`${base}/api/v1/status`, {
      headers: { Authorization: `Bearer ${loginA1.token}` },
    });
    assert.equal(oldAStatus.status, 401);
    assert.equal((await oldAStatus.json()).error.code, "invalid_token");

    // New tokenA2 and Teammate B's tokenB both remain valid
    const newAStatus = await (
      await fetch(`${base}/api/v1/status`, {
        headers: { Authorization: `Bearer ${loginA2.token}` },
      })
    ).json();
    assert.equal(newAStatus.ok, true);
    assert.equal(newAStatus.result.username, "bot_alice");

    const bStatus = await (
      await fetch(`${base}/api/v1/status`, {
        headers: { Authorization: `Bearer ${loginB.token}` },
      })
    ).json();
    assert.equal(bStatus.ok, true);
    assert.equal(bStatus.result.username, "bot_bob");

    // Events query + body merge (§3.6: body takes precedence)
    const evRes = await (
      await fetch(`${base}/api/v1/events?since=0`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${loginB.token}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ since: 5 }),
      })
    ).json();
    assert.equal(evRes.ok, true);
    assert.equal(evRes.result.since, 5);

    // Logout (§1.2)
    const outRes = await fetch(`${base}/api/v1/auth/logout`, {
      method: "POST",
      headers: { Authorization: `Bearer ${loginB.token}` },
    });
    assert.equal(outRes.status, 200);
    const afterLogout = await fetch(`${base}/api/v1/status`, {
      headers: { Authorization: `Bearer ${loginB.token}` },
    });
    assert.equal(afterLogout.status, 401);
  });
});
