/* AIAMC Harness 双模式可视化控制台前端逻辑 */
(() => {
  "use strict";

  const state = {
    mode: "local", // "match" | "local"
    theme: localStorage.getItem("aiamc_theme") || "dark",
    bootstrap: null,
    runs: [],
    runFilter: "all",
    selectedRunId: null,
    runDetail: null,
    lastRenderSig: "",
    dryOutputActive: false,
    process: null,
    pollTimer: null,
    // 轨迹视图变换
    traj: {
      zoom: 1,
      panX: 0,
      panY: 0,
      dragging: false,
      lastMouseX: 0,
      lastMouseY: 0,
      screenPoints: [],
    },
  };

  const $ = (id) => document.getElementById(id);

  function escapeHtml(s) {
    return String(s ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtDur(sec) {
    const s = Math.max(0, Math.floor(Number(sec) || 0));
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const r = s % 60;
    const pad = (n) => String(n).padStart(2, "0");
    return h > 0 ? `${h}:${pad(m)}:${pad(r)}` : `${pad(m)}:${pad(r)}`;
  }

  function fmtNum(n) {
    const v = Number(n) || 0;
    if (v >= 1_000_000) return `${(v / 1_000_000).toFixed(2)}M`;
    if (v >= 10_000) return `${(v / 1_000).toFixed(1)}k`;
    return v.toLocaleString("en-US");
  }

  let toastTimer = null;
  function showToast(msg, tone = "info") {
    const el = $("toast");
    el.textContent = msg;
    el.className = `toast ${tone}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.add("hidden"), 3400);
  }

  async function apiGet(url) {
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  async function apiPost(url, payload = {}) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }

  // ==========================================================================
  // 主题与双模式切换（Match vs Local）
  // ==========================================================================

  function applyTheme(theme) {
    state.theme = theme;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("aiamc_theme", theme);
    $("btnTheme").textContent = theme === "dark" ? "☀" : "☾";
    renderTrajectory();
  }

  function setMode(mode) {
    state.mode = mode === "match" ? "match" : "local";
    document.documentElement.setAttribute("data-mode", state.mode);

    $("btnModeMatch").classList.toggle("active", state.mode === "match");
    $("btnModeMatch").setAttribute("aria-selected", String(state.mode === "match"));
    $("btnModeLocal").classList.toggle("active", state.mode === "local");
    $("btnModeLocal").setAttribute("aria-selected", String(state.mode === "local"));

    const isMatch = state.mode === "match";
    $("matchPanel").classList.toggle("hidden", !isMatch);
    $("localPanel").classList.toggle("hidden", isMatch);

    $("fieldBotUser").classList.toggle("hidden", isMatch);
    $("fieldMatchUser").classList.toggle("hidden", !isMatch);
    $("fieldLlmProfile").classList.toggle("hidden", isMatch);
    $("fieldCustomModel").classList.toggle("hidden", isMatch || $("selectLlmProfile").value !== "custom");
    $("fieldRunMode").classList.toggle("hidden", isMatch);
    $("wrapStopOnDeath").classList.toggle("hidden", isMatch);
    $("wrapAccountReserved").classList.toggle("hidden", !isMatch);

    if (isMatch) {
      $("modeCardTitle").textContent = "🏆 正式比赛模式（Official）";
      $("modeEnvPill").textContent = "OFFICIAL MATCH";
      $("modeCardDesc").textContent =
        "严格锁定 NMO 官方端点与 AIA 官方代理（LLM_MODEL=default，并发 ≤ 2），自动校验 12 项赛前门禁并防止误踢队友。";
      $("selectRunMode").value = "campaign";
      syncRunModeFields();
      if (Number($("inputMaxSeconds").value) <= 600) {
        $("inputMaxSeconds").value = "28800";
      }
      $("chkDisconnect").checked = false;
      runPreflight(false);
    } else {
      $("modeCardTitle").textContent = "🧪 日常自搭服测试模式";
      $("modeEnvPill").textContent = "LOCAL-TARGET";
      $("modeCardDesc").textContent =
        "连接本地或局域网联调网关，使用隔离测试角色与可选自备模型，不会触碰正式比赛账号与官方服务器。";
      if (Number($("inputMaxSeconds").value) >= 20000) {
        $("inputMaxSeconds").value = "240";
      }
      $("chkDisconnect").checked = true;
      probeBridge(true);
    }
  }

  function syncRunModeFields() {
    const isFocus = state.mode === "local" && $("selectRunMode").value === "focus";
    $("fieldFocusSkill").classList.toggle("hidden", !isFocus);
    $("fieldGoal").classList.toggle("hidden", isFocus);
  }

  function applyPreset(name) {
    setMode("local");
    $("selectRunMode").value = "campaign";
    syncRunModeFields();
    if (name === "cold240") {
      $("selectStrategy").value = "llm";
      $("selectLlmProfile").value = "custom";
      $("inputMaxSeconds").value = "240";
      $("inputGoal").value = "";
      $("selectMinVerified").value = "offline";
      $("chkIncludeDraft").checked = true;
    } else if (name === "short180") {
      $("selectStrategy").value = "llm";
      $("selectLlmProfile").value = "custom";
      $("inputMaxSeconds").value = "180";
      $("inputGoal").value = "upgrade_tools";
      $("selectMinVerified").value = "offline";
      $("chkIncludeDraft").checked = false;
    } else if (name === "iron360") {
      $("selectStrategy").value = "llm";
      $("selectLlmProfile").value = "custom";
      $("inputMaxSeconds").value = "360";
      $("inputGoal").value = "smelt_iron";
      $("selectMinVerified").value = "offline";
      $("chkIncludeDraft").checked = false;
    } else if (name === "rule180") {
      $("selectStrategy").value = "rule";
      $("selectLlmProfile").value = "none";
      $("inputMaxSeconds").value = "180";
      $("inputGoal").value = "";
      $("selectMinVerified").value = "offline";
      $("chkIncludeDraft").checked = true;
    }
    $("fieldCustomModel").classList.toggle("hidden", $("selectLlmProfile").value !== "custom");
    showToast(`已应用预设：${name}`, "ok");
  }

  // ==========================================================================
  // Preflight & 本地网关探活
  // ==========================================================================

  async function runPreflight(live = false) {
    $("preflightSummary").textContent = live ? "正在执行 15 项联网 RTT 门禁..." : "正在执行 12 项静态门禁...";
    try {
      const data = await apiGet(`/api/preflight?source=match&live=${live ? 1 : 0}`);
      const list = $("preflightList");
      list.classList.remove("hidden");
      if (data.ok) {
        $("preflightSummary").innerHTML = `<span style="color:var(--ok)">✅ 门禁通过</span>（${data.warns} 提醒 · ${escapeHtml(data.env_file)}）`;
      } else {
        $("preflightSummary").innerHTML = `<span style="color:var(--bad)">⛔ ${data.blocks} 项阻塞</span>（${data.warns} 提醒 · ${escapeHtml(data.env_file)}）`;
      }
      list.innerHTML = (data.checks || [])
        .map((c) => {
          const cls = c.ok ? "ok" : c.level === "BLOCK" ? "bad" : "warn";
          const icon = c.ok ? "✅" : c.level === "BLOCK" ? "⛔" : "⚠️";
          return `<div class="pf-row ${cls}">
            <span>${icon} ${escapeHtml(c.name)}</span>
            <span class="pf-detail">${escapeHtml(c.detail)}</span>
          </div>`;
        })
        .join("");
    } catch (err) {
      $("preflightSummary").textContent = `自检失败：${err.message}`;
    }
  }

  async function probeBridge(silent = false) {
    const base = $("localEndpoint").value.trim();
    const box = $("bridgeStatus");
    box.className = "bridge-status";
    box.textContent = "正在探测本地联调网关 /healthz ...";
    try {
      const data = await apiGet(`/api/probe-bridge?base=${encodeURIComponent(base)}`);
      if (data.ok) {
        box.className = "bridge-status ok";
        const bots = data.data && typeof data.data.bots === "number" ? ` · 在线 bot: ${data.data.bots}` : "";
        box.textContent = `● 网关在线 · ${data.ms}ms (${data.url})${bots}`;
      } else {
        box.className = "bridge-status bad";
        box.textContent = `○ 网关未启动或不可达 · ${data.error || "连接失败"}`;
      }
    } catch (err) {
      box.className = "bridge-status bad";
      box.textContent = `○ 探测失败: ${err.message}`;
      if (!silent) showToast(err.message, "bad");
    }
  }

  // ==========================================================================
  // 启动 / 停止 / Dry 自检
  // ==========================================================================

  function collectLaunchPayload() {
    const isMatch = state.mode === "match";
    return {
      mode: state.mode,
      api_base: isMatch ? $("matchEndpoint").value : $("localEndpoint").value.trim(),
      bot_user: $("inputBotUser").value.trim(),
      strategy: $("selectStrategy").value,
      llm_profile: isMatch ? "official" : $("selectLlmProfile").value,
      llm_custom_model: $("inputCustomModel").value.trim(),
      max_seconds: Number($("inputMaxSeconds").value || 0),
      max_steps: Number($("inputMaxSteps").value || 0),
      min_verified: $("selectMinVerified").value,
      run_mode: isMatch ? "campaign" : $("selectRunMode").value,
      goal: $("inputGoal").value.trim(),
      focus_skill: $("inputFocusSkill").value.trim(),
      focus_args: $("inputFocusArgs").value.trim() || "{}",
      include_draft: $("chkIncludeDraft").checked,
      use_checkpoint: $("chkCheckpoint").checked,
      disconnect_on_exit: $("chkDisconnect").checked,
      stop_on_death: !isMatch && $("chkStopOnDeath").checked,
      account_reserved: isMatch && $("chkAccountReserved").checked,
    };
  }

  async function handleStart() {
    const payload = collectLaunchPayload();
    $("btnStart").disabled = true;
    try {
      const res = await apiPost("/api/run/start", payload);
      showToast(`已启动 (${state.mode === "match" ? "比赛模式" : "自搭服测试"})`, "ok");
      state.selectedRunId = res.run_id;
      state.dryOutputActive = false;
      state.lastRenderSig = "";
      resetTrajView();
      await refreshAll();
    } catch (err) {
      showToast(err.message, "bad");
    } finally {
      $("btnStart").disabled = false;
    }
  }

  async function handleStop(force = false) {
    try {
      const res = await apiPost(force ? "/api/run/kill" : "/api/run/stop", {});
      showToast(res.message || "已发送停止信号", "ok");
      await refreshAll();
    } catch (err) {
      showToast(err.message, "bad");
    }
  }

  async function handleDry() {
    const payload = collectLaunchPayload();
    $("btnDry").disabled = true;
    state.dryOutputActive = true;
    activateTab("console");
    $("consoleBox").textContent = "正在执行离线契约干跑 (mcagent.loop --dry) ...";
    try {
      const res = await apiPost("/api/run/dry", payload);
      $("consoleBox").textContent =
        `[dry] exit_code=${res.returncode} (${res.ms}ms)\n\n${res.stdout || ""}${res.stderr ? "\n--- STDERR ---\n" + res.stderr : ""}`;
      showToast(res.ok ? "Dry 契约干跑全部通过" : `Dry 返回码 ${res.returncode}`, res.ok ? "ok" : "bad");
    } catch (err) {
      $("consoleBox").textContent = `Dry 检查失败: ${err.message}`;
      showToast(err.message, "bad");
    } finally {
      $("btnDry").disabled = false;
    }
  }

  // ==========================================================================
  // 进程状态 & 运行列表渲染
  // ==========================================================================

  function renderProcessStatus(proc) {
    state.process = proc;
    const running = Boolean(proc && proc.running);
    const badge = $("procBadge");
    const text = $("procBadgeText");
    badge.className = `proc-badge ${running ? proc.state : "idle"}`;
    if (running) {
      const modeLabel = proc.mode === "match" ? "比赛运行中" : "测试运行中";
      text.textContent = `${modeLabel} · ${fmtDur(proc.elapsed_s)} (PID ${proc.pid})`;
    } else if (proc && proc.state === "exited") {
      text.textContent = `已结束 (rc=${proc.returncode})`;
    } else {
      text.textContent = "待命";
    }

    $("btnStart").classList.toggle("hidden", running);
    $("btnStop").classList.toggle("hidden", !running);
    $("btnKill").classList.toggle("hidden", !running);
  }

  function renderRunsList() {
    const list = $("runsList");
    const prevScroll = list.scrollTop;
    const filtered = state.runs.filter((r) => state.runFilter === "all" || r.source === state.runFilter);
    $("runsCountPill").textContent = String(filtered.length);
    if (!filtered.length) {
      list.innerHTML = `<div class="empty-hint">暂无匹配的运行记录</div>`;
      return;
    }
    list.innerHTML = filtered
      .map((r) => {
        const selected = r.id === state.selectedRunId ? "selected" : "";
        const srcCls = r.source === "official" ? "official" : "local";
        const srcTag = r.source === "official" ? "OFF" : "LOC";
        const activeDot = r.is_active ? "🟢 " : "";
        return `<div class="run-item ${selected}" data-run-id="${escapeHtml(r.id)}">
          <div class="run-item-top">
            <span class="run-item-name" title="${escapeHtml(r.id)}">${activeDot}${escapeHtml(r.label)}</span>
            <span class="source-badge ${srcCls}">${srcTag}</span>
          </div>
          <div class="run-item-stats">
            <span>${r.server_score}分 / ${r.goals_count}目标</span>
            <span>${fmtDur(r.elapsed_s)} · ${r.deaths}死</span>
          </div>
        </div>`;
      })
      .join("");

    list.querySelectorAll(".run-item").forEach((el) => {
      el.addEventListener("click", () => {
        const rid = el.getAttribute("data-run-id");
        if (rid && rid !== state.selectedRunId) {
          state.selectedRunId = rid;
          state.dryOutputActive = false;
          state.lastRenderSig = "";
          resetTrajView();
          renderRunsList();
          loadSelectedRun();
        }
      });
    });
    list.scrollTop = prevScroll;
  }

  // ==========================================================================
  // 核心遥测仪表盘渲染
  // ==========================================================================

  function renderRunDetail(d) {
    state.runDetail = d;
    if (!d) return;

    const v = d.vitals || {};
    const sc = d.scores || {};
    const tok = d.tokens || {};
    const meta = d.meta || {};
    const isOff = d.source === "official";

    $("runSourceBadge").className = `source-badge ${isOff ? "official" : "local"}`;
    $("runSourceBadge").textContent = isOff ? "OFFICIAL" : "LOCAL-TARGET";
    $("runTitle").textContent = d.id.split("/").slice(1).join("/") || d.id;
    $("runSubtitle").textContent = [
      meta.bot_user ? `bot=${meta.bot_user}` : "",
      meta.strategy ? `strategy=${meta.strategy}` : "",
      meta.llm_model ? `model=${meta.llm_model}` : "",
      meta.min_verified ? `min_ver=${meta.min_verified}` : "",
    ]
      .filter(Boolean)
      .join(" · ") || "历史存档";

    $("runGitMeta").textContent = d.git && d.git.commit ? `${d.git.branch || ""} @ ${d.git.commit}` : "";

    // Brain strip
    const obj = v.active_objective;
    $("brainObjective").textContent = obj ? `${obj.goal_id} (${obj.title})` : (meta.target_goal || "全主线自主推进");
    $("brainObjectiveSub").textContent = obj
      ? `${obj.source} · ${obj.trigger} · +${obj.score}分`
      : "按共享子图 DAG 边际 ROI 选目标";

    if (v.active_skill) {
      const aStr = Object.keys(v.active_skill_args || {}).length
        ? " " + JSON.stringify(v.active_skill_args)
        : "";
      $("brainSkill").textContent = `${v.active_skill}${aStr}`;
      $("brainSkillSub").textContent = v.active_skill_reason || `已运行 ${v.active_skill_elapsed_s}s`;
    } else {
      $("brainSkill").textContent = d.is_active ? "规划中..." : "已结束";
      $("brainSkillSub").textContent = v.stop_reason ? `结束原因：${v.stop_reason}` : "当前无活跃技能";
    }

    $("brainOp").textContent = v.last_op || "-";
    $("brainOpSub").textContent =
      v.last_op_ms != null
        ? `${v.last_op_ok === false ? "⚠ 失败" : "✓ 成功"} · ${v.last_op_ms}ms`
        : "等待动作回报";

    // KPI 1: Score
    $("kpiServerScore").textContent = String(sc.server_score_v2 ?? 0);
    $("kpiAdvCount").textContent = `${sc.server_advancements ?? 0} 服务端进度`;
    $("kpiInferredScore").textContent = String(sc.inferred_score_v2 ?? 0);
    $("kpiGoalsCount").textContent = String((sc.goals_reached || []).length);

    // KPI 2: Vitals
    const hp = v.hp != null ? Number(v.hp) : null;
    const food = v.food != null ? Number(v.food) : null;
    $("barHp").style.width = hp != null ? `${Math.max(0, Math.min(100, (hp / 20) * 100))}%` : "0%";
    $("valHp").textContent = hp != null ? `${hp}/20` : "-";
    $("barFood").style.width = food != null ? `${Math.max(0, Math.min(100, (food / 20) * 100))}%` : "0%";
    $("valFood").textContent = food != null ? `${food}/20` : "-";
    const deaths = Number(v.deaths || 0);
    $("kpiDeaths").textContent = `${deaths} 死亡`;
    $("kpiDeaths").className = `death-chip ${deaths > 0 ? "has-deaths" : ""}`;
    $("kpiPos").textContent = Array.isArray(v.pos)
      ? `(${v.pos[0].toFixed(1)}, ${v.pos[1].toFixed(1)}, ${v.pos[2].toFixed(1)})`
      : "pos: -";
    $("kpiDist").textContent = `总路程 ${v.distance_m ?? 0}m`;

    // KPI 3: Time & Steps
    $("kpiElapsed").textContent = fmtDur(v.elapsed_s);
    $("kpiBudgetLabel").textContent = v.max_seconds > 0 ? `/ ${fmtDur(v.max_seconds)}` : "/ 不限";
    const pctElapsed = v.max_seconds > 0 ? Math.min(100, (v.elapsed_s / v.max_seconds) * 100) : (d.is_active ? 100 : 0);
    $("barElapsed").style.width = `${pctElapsed}%`;
    $("kpiSteps").textContent = fmtNum(v.steps || 0);
    const rate = v.elapsed_s > 5 ? ((v.steps || 0) / (v.elapsed_s / 60)).toFixed(1) : "0.0";
    $("kpiStepRate").textContent = `${rate} 步/分`;
    $("kpiStopReason").textContent = d.is_active ? "● LIVE" : (v.stop_reason || "完成");

    // KPI 4: Tokens
    $("kpiTokens").textContent = fmtNum(tok.total_tokens || 0);
    const llmCalls = Number(tok.llm_calls || 0);
    $("kpiLlmStats").textContent =
      llmCalls > 0 ? `${tok.llm_ok || 0}/${llmCalls} 次` : (tok.total_tokens > 0 ? "无调用明细" : "0/0 次");
    $("kpiProjIn").textContent = `${tok.proj_8h_in_pct ?? 0}%`;
    $("kpiLlmMs").textContent = tok.llm_avg_ms > 0 ? `${Math.round(tok.llm_avg_ms)}ms` : "-";
    $("barQuota").style.width = `${Math.min(100, tok.proj_8h_in_pct || 0)}%`;

    // Charts & panels
    renderTrajectory();
    renderCurveSvg(d.curve || []);
    renderGoalsAndLandmarks(sc.goals_reached || [], d.landmarks || {});
    renderSkillBars(d.skill_stats || []);
    renderTimeline(d.events || []);
    renderIncidents(d.incidents || []);

    $("summaryBox").textContent = d.summary_md || "暂无 summary.md（运行结束时由 SoakObserver 自动落盘）";
    if (!state.dryOutputActive) {
      $("consoleBox").textContent =
        d.console_log && d.console_log.length
          ? d.console_log.join("\n")
          : "该运行无 console.log 输出。点击左侧「启动运行」或「Dry 自检」可查看实时终端输出。";
    }
  }

  // ==========================================================================
  // 2D 空间轨迹雷达 Canvas（支持缩放、拖拽、悬停探点）
  // ==========================================================================

  function resetTrajView() {
    state.traj.zoom = 1;
    state.traj.panX = 0;
    state.traj.panY = 0;
  }

  function colorByY(y) {
    if (y < 16) return "#a855f7";
    if (y < 57) return "#06b6d4";
    if (y <= 90) return "#10b981";
    return "#f59e0b";
  }

  function renderTrajectory() {
    const canvas = $("trajCanvas");
    if (!canvas) return;
    const dpr = window.devicePixelRatio || 1;
    const rect = canvas.getBoundingClientRect();
    const w = Math.max(300, Math.floor(rect.width));
    const h = Math.max(220, Math.floor(rect.height));
    const targetW = Math.round(w * dpr);
    const targetH = Math.round(h * dpr);
    if (canvas.width !== targetW || canvas.height !== targetH) {
      canvas.width = targetW;
      canvas.height = targetH;
    }

    const ctx = canvas.getContext("2d");
    ctx.setTransform(targetW / w, 0, 0, targetH / h, 0, 0);

    // Background
    ctx.fillStyle = "#070b12";
    ctx.fillRect(0, 0, w, h);

    // Subtle radar grid
    ctx.strokeStyle = "rgba(148, 163, 184, 0.08)";
    ctx.lineWidth = 1;
    const gridStep = 36;
    for (let x = gridStep; x < w; x += gridStep) {
      ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, h); ctx.stroke();
    }
    for (let y = gridStep; y < h; y += gridStep) {
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(w, y); ctx.stroke();
    }

    const trajData = state.runDetail && state.runDetail.trajectory;
    const pts = (trajData && trajData.points) || [];
    const bounds = trajData && trajData.bounds;
    state.traj.screenPoints = [];

    if (!pts.length || !bounds) {
      $("trajMeta").textContent = "该运行无坐标轨迹点（例如纯 progress_curve 存档）";
      ctx.fillStyle = "#64748b";
      ctx.font = "13px sans-serif";
      ctx.textAlign = "center";
      ctx.fillText("暂无三维坐标轨迹点", w / 2, h / 2);
      return;
    }

    $("trajMeta").textContent =
      `采样 ${trajData.total_raw_points} 点 · X[${bounds.min_x.toFixed(0)}..${bounds.max_x.toFixed(0)}] · Y[${bounds.min_y.toFixed(0)}..${bounds.max_y.toFixed(0)}] · Z[${bounds.min_z.toFixed(0)}..${bounds.max_z.toFixed(0)}]`;

    const spanX = Math.max(12, bounds.max_x - bounds.min_x);
    const spanZ = Math.max(12, bounds.max_z - bounds.min_z);
    const cx = (bounds.min_x + bounds.max_x) / 2;
    const cz = (bounds.min_z + bounds.max_z) / 2;
    const pad = 34;
    const baseScale = Math.min((w - pad * 2) / spanX, (h - pad * 2) / spanZ);
    const scale = baseScale * state.traj.zoom;

    const toScreen = (x, z) => [
      w / 2 + (x - cx) * scale + state.traj.panX,
      h / 2 + (z - cz) * scale + state.traj.panY,
    ];

    // Crosshair at start
    const [sx0, sy0] = toScreen(pts[0].x, pts[0].z);
    ctx.strokeStyle = "rgba(148, 163, 184, 0.18)";
    ctx.setLineDash([4, 4]);
    ctx.beginPath(); ctx.moveTo(sx0, 0); ctx.lineTo(sx0, h); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(0, sy0); ctx.lineTo(w, sy0); ctx.stroke();
    ctx.setLineDash([]);

    // Draw trajectory segments colored by Y altitude
    ctx.lineWidth = 2.2;
    ctx.lineCap = "round";
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1];
      const b = pts[i];
      if (Math.hypot(b.x - a.x, b.z - a.z) > 100) continue; // skip teleport
      const [x1, y1] = toScreen(a.x, a.z);
      const [x2, y2] = toScreen(b.x, b.z);
      ctx.strokeStyle = colorByY(b.y);
      ctx.beginPath();
      ctx.moveTo(x1, y1);
      ctx.lineTo(x2, y2);
      ctx.stroke();
    }

    // Store projected points & draw special markers
    for (let i = 0; i < pts.length; i++) {
      const p = pts[i];
      const [px, py] = toScreen(p.x, p.z);
      state.traj.screenPoints.push({ px, py, p });

      if (p.kind === "goal") {
        ctx.fillStyle = "#fde047";
        ctx.beginPath();
        ctx.arc(px, py, 5, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#070b12";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      } else if (p.kind === "death") {
        ctx.strokeStyle = "#ef4444";
        ctx.lineWidth = 2.5;
        ctx.beginPath();
        ctx.moveTo(px - 5, py - 5); ctx.lineTo(px + 5, py + 5);
        ctx.moveTo(px + 5, py - 5); ctx.lineTo(px - 5, py + 5);
        ctx.stroke();
      }
    }

    // Start marker
    ctx.fillStyle = "#e2e8f0";
    ctx.beginPath();
    ctx.arc(sx0, sy0, 4.5, 0, Math.PI * 2);
    ctx.fill();

    // Current position pulsing halo
    const last = pts[pts.length - 1];
    const [lx, ly] = toScreen(last.x, last.z);
    ctx.fillStyle = "rgba(16, 185, 129, 0.25)";
    ctx.beginPath();
    ctx.arc(lx, ly, 11, 0, Math.PI * 2);
    ctx.fill();
    ctx.fillStyle = "#10b981";
    ctx.beginPath();
    ctx.arc(lx, ly, 5, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = "#ffffff";
    ctx.lineWidth = 1.6;
    ctx.stroke();

    // Compass & scale bar
    ctx.fillStyle = "rgba(203, 213, 225, 0.7)";
    ctx.font = "11px monospace";
    ctx.textAlign = "right";
    ctx.fillText("N (-Z) ↑", w - 10, 18);
    const barMeters = Math.max(1, Math.round(48 / Math.max(0.05, scale)));
    const barPx = barMeters * scale;
    ctx.textAlign = "left";
    ctx.fillRect(12, h - 16, Math.min(120, Math.max(18, barPx)), 2);
    ctx.fillText(`${barMeters} 格`, 12, h - 22);
  }

  function setupTrajInteractions() {
    const canvas = $("trajCanvas");
    const tip = $("trajTooltip");

    canvas.addEventListener("wheel", (e) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.18 : 0.85;
      state.traj.zoom = Math.max(0.4, Math.min(12, state.traj.zoom * factor));
      renderTrajectory();
    });

    canvas.addEventListener("mousedown", (e) => {
      state.traj.dragging = true;
      state.traj.lastMouseX = e.clientX;
      state.traj.lastMouseY = e.clientY;
    });
    window.addEventListener("mouseup", () => { state.traj.dragging = false; });
    window.addEventListener("mousemove", (e) => {
      if (!state.traj.dragging) return;
      state.traj.panX += e.clientX - state.traj.lastMouseX;
      state.traj.panY += e.clientY - state.traj.lastMouseY;
      state.traj.lastMouseX = e.clientX;
      state.traj.lastMouseY = e.clientY;
      renderTrajectory();
    });

    canvas.addEventListener("mousemove", (e) => {
      if (state.traj.dragging) return;
      const rect = canvas.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      let best = null;
      let bestDist = 14;
      for (const sp of state.traj.screenPoints) {
        const d = Math.hypot(sp.px - mx, sp.py - my);
        if (d < bestDist) {
          bestDist = d;
          best = sp;
        }
      }
      if (!best) {
        tip.classList.add("hidden");
        return;
      }
      const p = best.p;
      tip.classList.remove("hidden");
      tip.textContent = `t=${fmtDur(p.t)} | (${p.x}, Y=${p.y}, ${p.z}) | ${p.skill || "-"} · ${p.op}`;
      const maxLeft = Math.max(8, rect.width - (tip.offsetWidth || 240) - 8);
      tip.style.left = `${Math.max(8, Math.min(maxLeft, mx + 12))}px`;
      tip.style.top = `${Math.max(8, my - 30)}px`;
    });

    canvas.addEventListener("mouseleave", () => tip.classList.add("hidden"));
    $("btnZoomIn").addEventListener("click", () => { state.traj.zoom = Math.min(12, state.traj.zoom * 1.3); renderTrajectory(); });
    $("btnZoomOut").addEventListener("click", () => { state.traj.zoom = Math.max(0.4, state.traj.zoom * 0.75); renderTrajectory(); });
    $("btnZoomReset").addEventListener("click", () => { resetTrajView(); renderTrajectory(); });
    window.addEventListener("resize", renderTrajectory);
  }

  // ==========================================================================
  // 得分 & Token SVG 曲线图
  // ==========================================================================

  function renderCurveSvg(rawCurve) {
    const wrap = $("curveSvgWrap");
    const curve = (rawCurve || []).map((c) => ({
      t: Number(c.t) || 0,
      server_score: Number(c.server_score) || 0,
      inferred_score: Number(c.inferred_score) || 0,
      tokens: Number(c.tokens) || 0,
    }));
    if (!curve.length) {
      $("curveMeta").textContent = "暂无进度曲线采样";
      wrap.innerHTML = `<div class="empty-hint">等待 progress_curve.jsonl 采样...</div>`;
      return;
    }

    const W = 620, H = 280;
    const padL = 42, padR = 52, padT = 18, padB = 30;
    const cw = W - padL - padR;
    const ch = H - padT - padB;

    const maxT = Math.max(10, curve[curve.length - 1].t);
    const maxScore = Math.max(20, ...curve.map((c) => Math.max(c.server_score, c.inferred_score)));
    const maxTok = Math.max(1000, ...curve.map((c) => c.tokens));

    const xOf = (t) => padL + (t / maxT) * cw;
    const yScore = (s) => padT + ch - (s / maxScore) * ch;
    const yTok = (tk) => padT + ch - (tk / maxTok) * ch;

    const buildStepPath = (getter) =>
      curve
        .map((c, idx) => {
          const x = xOf(c.t).toFixed(1);
          const y = yScore(getter(c)).toFixed(1);
          if (idx === 0) return `M ${x} ${y}`;
          const prevY = yScore(getter(curve[idx - 1])).toFixed(1);
          return `L ${x} ${prevY} L ${x} ${y}`;
        })
        .join(" ");

    const scorePath = buildStepPath((c) => c.server_score);
    const infPath = buildStepPath((c) => c.inferred_score);
    const areaPath =
      curve.length > 1
        ? `${scorePath} L ${xOf(curve[curve.length - 1].t).toFixed(1)} ${(padT + ch).toFixed(1)} L ${xOf(curve[0].t).toFixed(1)} ${(padT + ch).toFixed(1)} Z`
        : "";
    const tokPath = curve
      .map((c, idx) => `${idx === 0 ? "M" : "L"} ${xOf(c.t).toFixed(1)} ${yTok(c.tokens).toFixed(1)}`)
      .join(" ");

    const gridLines = [0, 0.25, 0.5, 0.75, 1]
      .map((f) => {
        const y = (padT + ch * (1 - f)).toFixed(1);
        const sLbl = Math.round(maxScore * f);
        const tLbl = fmtNum(Math.round(maxTok * f));
        return `<line x1="${padL}" y1="${y}" x2="${W - padR}" y2="${y}" stroke="rgba(148,163,184,0.14)" stroke-dasharray="3 3"/>
          <text x="${padL - 6}" y="${Number(y) + 4}" text-anchor="end" fill="#8b9bb4" font-size="10.5" font-family="monospace">${sLbl}</text>
          <text x="${W - padR + 6}" y="${Number(y) + 4}" text-anchor="start" fill="#a855f7" font-size="10" font-family="monospace">${tLbl}</text>`;
      })
      .join("");

    const xTicks = [0, 0.25, 0.5, 0.75, 1]
      .map((f) => {
        const x = (padL + cw * f).toFixed(1);
        return `<text x="${x}" y="${H - 8}" text-anchor="middle" fill="#8b9bb4" font-size="10.5" font-family="monospace">${fmtDur(maxT * f)}</text>`;
      })
      .join("");

    const last = curve[curve.length - 1];
    const peakScore = Math.max(...curve.map((c) => c.server_score));
    $("curveMeta").textContent = `${curve.length} 个采样点 · 最高服务端得分 ${peakScore} · 累计 Token ${fmtNum(last.tokens)}`;

    wrap.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="xMidYMid meet">
      <defs>
        <linearGradient id="scoreGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#10b981" stop-opacity="0.32"/>
          <stop offset="100%" stop-color="#10b981" stop-opacity="0.02"/>
        </linearGradient>
      </defs>
      ${gridLines}
      ${xTicks}
      ${areaPath ? `<path d="${areaPath}" fill="url(#scoreGrad)"/>` : ""}
      ${curve.length > 1 ? `<path d="${infPath}" fill="none" stroke="#06b6d4" stroke-width="1.8" stroke-dasharray="5 3"/>` : ""}
      ${curve.length > 1 ? `<path d="${scorePath}" fill="none" stroke="#10b981" stroke-width="2.5"/>` : ""}
      ${curve.length > 1 ? `<path d="${tokPath}" fill="none" stroke="#a855f7" stroke-width="1.8" opacity="0.88"/>` : ""}
      <circle cx="${xOf(last.t).toFixed(1)}" cy="${yScore(last.inferred_score).toFixed(1)}" r="3.4" fill="#06b6d4"/>
      <circle cx="${xOf(last.t).toFixed(1)}" cy="${yTok(last.tokens).toFixed(1)}" r="3.4" fill="#a855f7"/>
      <circle cx="${xOf(last.t).toFixed(1)}" cy="${yScore(last.server_score).toFixed(1)}" r="4.2" fill="#10b981" stroke="#fff" stroke-width="1.4"/>
    </svg>`;
  }

  // ==========================================================================
  // 目标、技能、事件流、异常包、交付清单渲染
  // ==========================================================================

  function renderGoalsAndLandmarks(goals, landmarks) {
    const box = $("goalsChips");
    const totalPts = goals.reduce((acc, g) => acc + (Number(g.score) || 0), 0);
    $("goalsTotalBadge").textContent = `${goals.length} 项 · +${totalPts} 分`;
    if (!goals.length) {
      box.innerHTML = `<div class="empty-hint">尚未达成计分或里程碑目标</div>`;
    } else {
      box.innerHTML = goals
        .map((g) => {
          const pts = g.score > 0 ? `<span class="g-pts">+${g.score}</span>` : `<span class="muted">前置</span>`;
          const t = g.t != null ? `<span class="g-t">@${fmtDur(g.t)}</span>` : "";
          return `<div class="goal-chip" title="${escapeHtml(g.id)} · ${escapeHtml(g.tier)}">
            ${pts}
            <span>${escapeHtml(g.title || g.id)}</span>
            ${t}
          </div>`;
        })
        .join("");
    }

    const lmBar = $("landmarksBar");
    const biomes = (landmarks && landmarks.biomes) || [];
    const lms = (landmarks && landmarks.landmarks) || [];
    if (biomes.length || lms.length) {
      lmBar.classList.remove("hidden");
      lmBar.innerHTML =
        `<span class="muted">已记忆地标/群系：</span>` +
        lms.map((x) => `<span class="lm-chip">📍 ${escapeHtml(x)}</span>`).join("") +
        biomes.map((x) => `<span class="lm-chip">🌿 ${escapeHtml(x)}</span>`).join("");
    } else {
      lmBar.classList.add("hidden");
    }
  }

  function renderSkillBars(rows) {
    const box = $("skillBars");
    if (!rows.length) {
      $("skillSummaryBadge").textContent = "0 技能";
      box.innerHTML = `<div class="empty-hint">暂无技能执行记录</div>`;
      return;
    }
    const top = rows.slice(0, 8);
    const maxS = Math.max(1, ...top.map((r) => r.total_s || 0.1));
    $("skillSummaryBadge").textContent = `共 ${rows.length} 技能`;
    box.innerHTML = top
      .map((r) => {
        const w = Math.max(4, Math.round(((r.total_s || 0.1) / maxS) * 100));
        const meta = `✓${r.ok} ↻${r.replan}${r.env_blocked ? " ⏸" + r.env_blocked : ""}${r.fail ? " ✕" + r.fail : ""}`;
        return `<div class="sbar-item">
          <div class="sbar-head">
            <span>${escapeHtml(r.skill)} <small class="muted">(${r.steps}步)</small></span>
            <span>${r.total_s}s · <small class="muted">${meta}</small></span>
          </div>
          <div class="sbar-track"><div class="sbar-fill" style="width:${w}%"></div></div>
        </div>`;
      })
      .join("");
  }

  function renderTimeline(events) {
    const box = $("timelineList");
    const prevScroll = box.scrollTop;
    const cat = $("eventCategoryFilter").value;
    const q = $("eventSearch").value.trim().toLowerCase();
    const filtered = events
      .filter((e) => cat === "all" || e.category === cat)
      .filter((e) => !q || `${e.title} ${e.detail} ${e.badge}`.toLowerCase().includes(q))
      .slice()
      .reverse();

    if (!filtered.length) {
      box.innerHTML = `<div class="empty-hint">没有匹配的事件记录</div>`;
      return;
    }
    box.innerHTML = filtered
      .slice(0, 160)
      .map(
        (e) => `<div class="ev-row ${escapeHtml(e.tone || "")}">
          <span class="ev-t">${fmtDur(e.t)}</span>
          <span class="ev-badge">${escapeHtml(e.badge)}</span>
          <div class="ev-body">
            <div class="ev-title">${escapeHtml(e.title)}</div>
            ${e.detail ? `<div class="ev-detail">${escapeHtml(e.detail)}</div>` : ""}
          </div>
        </div>`
      )
      .join("");
    box.scrollTop = prevScroll;
  }

  function renderIncidents(incidents) {
    $("incCountPill").textContent = String(incidents.length);
    const box = $("incidentsList");
    if (!incidents.length) {
      box.innerHTML = `<div class="empty-hint">本次运行 0 异常单 🎉</div>`;
      return;
    }
    box.innerHTML = incidents
      .map(
        (inc, idx) => `<div class="inc-row" data-inc-idx="${idx}">
          <div>
            <span class="sev-pill ${escapeHtml(inc.severity)}">${escapeHtml(inc.severity)}</span>
            <strong class="mono"> ${escapeHtml(inc.code)}</strong>
            <span class="muted"> · ${escapeHtml(inc.skill)}</span>
            <div style="margin-top:3px;font-size:12.5px">${escapeHtml(inc.title)}</div>
          </div>
          <span class="mono muted">×${inc.count} · 点击查看</span>
        </div>`
      )
      .join("");

    box.querySelectorAll(".inc-row").forEach((el) => {
      el.addEventListener("click", () => {
        const inc = incidents[Number(el.getAttribute("data-inc-idx"))];
        if (!inc) return;
        $("incModalTitle").textContent = `[${inc.severity}] ${inc.code} · ${inc.filename}`;
        $("incReplayCmd").textContent = inc.replay_cmd;
        $("incJsonBox").textContent = JSON.stringify(inc.payload, null, 2);
        $("incidentDialog").showModal();
      });
    });
  }

  function renderHandover(h) {
    if (!h) return;
    const box = $("handoverContent");
    const secretOk = !h.tracked_secrets || h.tracked_secrets.length === 0;
    const cmdRow = (label, cmd) => `
      <div style="margin-top:8px">
        <div class="muted" style="font-size:11.5px">${escapeHtml(label)}</div>
        <div class="cmd-copy-row">
          <code>${escapeHtml(cmd)}</code>
          <button type="button" class="btn xs copy-cmd-btn" data-cmd="${escapeHtml(cmd)}">复制</button>
        </div>
      </div>`;

    box.innerHTML = `
      <div class="ho-box">
        <h3>仓库实时盘点（${escapeHtml(h.git.branch)} @ ${escapeHtml(h.git.commit)}）</h3>
        <div class="compliance-locks">
          <span class="lock-chip ok">技能总计 ${h.skills.total}（live ${h.skills.live} / offline ${h.skills.offline}）</span>
          <span class="lock-chip ok">目标台账 ${h.goals.total}（计分 ${h.goals.scoring_count} 项 / ${h.goals.max_score} 分上限）</span>
          <span class="lock-chip ${secretOk ? "ok" : ""}" style="${secretOk ? "" : "background:var(--bad-soft);color:var(--bad)"}">
            ${secretOk ? "✓ Git 零密钥追踪" : "⛔ 发现已追踪密钥: " + escapeHtml(h.tracked_secrets.join(", "))}
          </span>
        </div>
        ${(h.warnings || []).map((w) => `<div class="ho-warn" style="margin-top:8px">⚠ ${escapeHtml(w)}</div>`).join("")}
      </div>

      <div class="handover-grid">
        <div class="ho-box">
          <h3>① 交给主办方直接跑的最小运行包</h3>
          <div class="ho-list">
            ${h.runtime_package
              .map(
                (i) => `<div class="ho-item">
                  <span>${i.exists ? "✅" : "❌"} <code>${escapeHtml(i.path)}</code></span>
                  <span class="muted">${escapeHtml(i.desc)}</span>
                </div>`
              )
              .join("")}
          </div>
        </div>
        <div class="ho-box">
          <h3>② 40 分工程评审五部分材料包（补充）</h3>
          <div class="ho-list">
            ${h.review_package
              .map(
                (i) => `<div class="ho-item">
                  <span>${i.exists ? "✅" : "❌"} <code>${escapeHtml(i.path)}</code></span>
                  <span class="muted">${escapeHtml(i.desc)}</span>
                </div>`
              )
              .join("")}
          </div>
        </div>
      </div>

      <div class="ho-box">
        <h3>🚫 严禁打进主办方运行包的内容</h3>
        <div class="ho-list">
          ${h.forbidden
            .map(
              (i) => `<div class="ho-item">
                <span>⛔ <code>${escapeHtml(i.path)}</code></span>
                <span class="muted">${escapeHtml(i.reason)}</span>
              </div>`
            )
            .join("")}
        </div>
      </div>

      <div class="ho-box">
        <h3>🚀 主办方一键安装与比赛启动命令</h3>
        ${cmdRow("1. 锁定依赖安装", h.commands.install)}
        ${cmdRow("2. 赛前 12+3 项自检", h.commands.preflight)}
        ${cmdRow(`3. 正式 8h 启动（直接解锁 ${h.skills.live} 项 live 技能 + ${(h.goals.stages && h.goals.stages.graduated) || 0} 项 graduated 目标）`, h.commands.match_conservative)}
        ${cmdRow(`4. 显式全开参数版（含 --min-verified offline --include-draft，共 ${h.skills.total} 技能 / ${h.goals.total} 台账）`, h.commands.match_full)}
        ${cmdRow("5. 无账号离线全量回归验收", h.commands.offline_verify)}
      </div>
    `;

    box.querySelectorAll(".copy-cmd-btn").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(btn.getAttribute("data-cmd") || "");
          showToast("已复制到剪贴板", "ok");
        } catch {
          showToast("复制失败，请手动选中复制", "bad");
        }
      });
    });
  }

  // ==========================================================================
  // 标签页 & 数据拉取轮询
  // ==========================================================================

  function activateTab(name) {
    document.querySelectorAll(".tab-btn").forEach((b) => {
      b.classList.toggle("active", b.getAttribute("data-tab") === name);
    });
    document.querySelectorAll(".tab-panel").forEach((p) => {
      p.classList.toggle("active", p.id === `tab-${name}`);
    });
    $("timelineTools").classList.toggle("hidden", name !== "timeline");
  }

  async function loadSelectedRun() {
    if (!state.selectedRunId) return;
    try {
      const detail = await apiGet(`/api/run?id=${encodeURIComponent(state.selectedRunId)}`);
      if (detail.process) renderProcessStatus(detail.process);
      const sig = [
        detail.id,
        detail.is_active ? "1" : "0",
        detail.vitals ? detail.vitals.elapsed_s : 0,
        detail.vitals ? detail.vitals.steps : 0,
        (detail.events || []).length,
        (detail.curve || []).length,
        (detail.console_log || []).length,
        (detail.summary_md || "").length,
      ].join("|");
      if (!detail.is_active && sig === state.lastRenderSig) {
        return;
      }
      state.lastRenderSig = sig;
      renderRunDetail(detail);
    } catch (err) {
      console.error("loadSelectedRun failed:", err);
    }
  }

  async function refreshAll() {
    try {
      const data = await apiGet("/api/runs");
      renderProcessStatus(data.process);
      state.runs = data.runs || [];
      if (!state.selectedRunId && state.runs.length) {
        state.selectedRunId = (data.process && data.process.run_id) || state.runs[0].id;
      }
      renderRunsList();
      await loadSelectedRun();
    } catch (err) {
      console.error("refreshAll failed:", err);
    }
  }

  async function bootstrap() {
    applyTheme(state.theme);
    try {
      const b = await apiGet("/api/bootstrap");
      state.bootstrap = b;
      $("gitBadge").textContent = `${b.git.branch || "main"} @ ${b.git.commit || "-"}`;

      // Populate env defaults
      const devVals = (b.env_dev && b.env_dev.values) || {};
      const matchVals = (b.env_match && b.env_match.values) || {};
      if (devVals.MC_LOCAL_API_BASE) $("localEndpoint").value = devVals.MC_LOCAL_API_BASE;
      if (devVals.LLM_CUSTOM_MODEL) $("inputCustomModel").placeholder = `默认: ${devVals.LLM_CUSTOM_MODEL}`;
      if (matchVals.MC_API_BASE && [...$("matchEndpoint").options].some((o) => o.value === matchVals.MC_API_BASE)) {
        $("matchEndpoint").value = matchVals.MC_API_BASE;
      }
      $("matchUserReadonly").textContent =
        matchVals.MC_USER || (devVals.MC_USER ? `(继承 .env: ${devVals.MC_USER})` : "(未在 .env.match 配置)");

      // Populate datalists
      $("goalsDatalist").innerHTML = (b.goals_catalog || [])
        .map((g) => `<option value="${escapeHtml(g.id)}">${escapeHtml(g.title)} (+${g.score} · ${g.stage})</option>`)
        .join("");
      $("skillsDatalist").innerHTML = (b.skills_catalog || [])
        .map((s) => `<option value="${escapeHtml(s.name)}">[${s.verified}] ${escapeHtml(s.description)}</option>`)
        .join("");

      renderProcessStatus(b.process);
      state.runs = b.runs || [];
      renderHandover(b.handover);

      if (b.process && b.process.running && b.process.mode) {
        setMode(b.process.mode);
        state.selectedRunId = b.process.run_id;
      } else {
        setMode("local");
        if (state.runs.length) state.selectedRunId = state.runs[0].id;
      }
      renderRunsList();
      await loadSelectedRun();
    } catch (err) {
      showToast(`初始化失败: ${err.message}`, "bad");
    }

    // 串行轮询：防止慢请求堆叠阻塞浏览器 6 连接池（运行中 2.5s，空闲 6s）
    let polling = false;
    let pollTick = 0;
    const schedulePoll = () => {
      const running = Boolean(state.process && state.process.running);
      const delay = running ? 2500 : 6000;
      state.pollTimer = setTimeout(async () => {
        if (!polling && (running || document.visibilityState === "visible")) {
          polling = true;
          pollTick += 1;
          try {
            if (running && state.selectedRunId && pollTick % 4 !== 0) {
              await loadSelectedRun();
            } else {
              await refreshAll();
            }
          } finally {
            polling = false;
          }
        }
        schedulePoll();
      }, delay);
    };
    schedulePoll();
  }

  // ==========================================================================
  // 事件绑定
  // ==========================================================================

  function bindEvents() {
    $("btnModeMatch").addEventListener("click", () => setMode("match"));
    $("btnModeLocal").addEventListener("click", () => setMode("local"));
    $("btnTheme").addEventListener("click", () => applyTheme(state.theme === "dark" ? "light" : "dark"));
    $("btnRefresh").addEventListener("click", () => { refreshAll(); showToast("已刷新", "ok"); });
    $("btnOpenHandover").addEventListener("click", () => {
      activateTab("handover");
      $("tab-handover").scrollIntoView({ behavior: "smooth", block: "start" });
    });

    $("btnPreflightStatic").addEventListener("click", () => runPreflight(false));
    $("btnPreflightLive").addEventListener("click", () => runPreflight(true));
    $("btnApplyMatch").addEventListener("click", async () => {
      try {
        const res = await apiPost("/api/preflight/apply-match", {});
        showToast(res.message || "已应用 .env.match", res.ok ? "ok" : "bad");
        runPreflight(false);
      } catch (err) {
        showToast(err.message, "bad");
      }
    });
    $("btnProbeBridge").addEventListener("click", () => probeBridge(false));

    document.querySelectorAll(".chip-btn[data-preset]").forEach((btn) => {
      btn.addEventListener("click", () => applyPreset(btn.getAttribute("data-preset")));
    });

    document.querySelectorAll(".mini-pill[data-sec]").forEach((btn) => {
      btn.addEventListener("click", () => {
        $("inputMaxSeconds").value = btn.getAttribute("data-sec");
      });
    });

    $("selectLlmProfile").addEventListener("change", () => {
      const v = $("selectLlmProfile").value;
      $("fieldCustomModel").classList.toggle("hidden", state.mode === "match" || v !== "custom");
      if (v === "none") $("selectStrategy").value = "rule";
      else $("selectStrategy").value = "llm";
    });
    $("selectStrategy").addEventListener("change", () => {
      if (state.mode === "local" && $("selectStrategy").value === "rule") {
        $("selectLlmProfile").value = "none";
        $("fieldCustomModel").classList.add("hidden");
      } else if (state.mode === "local" && $("selectLlmProfile").value === "none") {
        $("selectLlmProfile").value = "custom";
        $("fieldCustomModel").classList.remove("hidden");
      }
    });
    $("selectRunMode").addEventListener("change", syncRunModeFields);

    $("btnStart").addEventListener("click", handleStart);
    $("btnStop").addEventListener("click", () => handleStop(false));
    $("btnKill").addEventListener("click", () => handleStop(true));
    $("btnDry").addEventListener("click", handleDry);

    document.querySelectorAll(".filter-pill[data-run-filter]").forEach((btn) => {
      btn.addEventListener("click", () => {
        state.runFilter = btn.getAttribute("data-run-filter");
        document.querySelectorAll(".filter-pill").forEach((b) => b.classList.toggle("active", b === btn));
        renderRunsList();
      });
    });

    document.querySelectorAll(".tab-btn[data-tab]").forEach((btn) => {
      btn.addEventListener("click", () => activateTab(btn.getAttribute("data-tab")));
    });

    $("eventCategoryFilter").addEventListener("change", () => {
      if (state.runDetail) renderTimeline(state.runDetail.events || []);
    });
    $("eventSearch").addEventListener("input", () => {
      if (state.runDetail) renderTimeline(state.runDetail.events || []);
    });

    $("btnCloseIncModal").addEventListener("click", () => $("incidentDialog").close());
    $("btnCopyReplay").addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText($("incReplayCmd").textContent || "");
        showToast("已复制离线重放命令", "ok");
      } catch {
        showToast("复制失败", "bad");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    bindEvents();
    setupTrajInteractions();
    bootstrap();
  });
})();
