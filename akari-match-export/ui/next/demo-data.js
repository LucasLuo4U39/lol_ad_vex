/* =====================================================================
   LoLAdvisor 演示模式共用数据 + 共享 UI 工具
   三页共用（home / champ_select / dashboard），纯静态、无服务器依赖。
   数据形状与现有服务端接口一一对应（见 contracts.md）。
   ===================================================================== */
(function (global) {
  "use strict";

  /* ---------------------------------------------------------------- 工具 */
  const UI = {
    esc(s) {
      return String(s ?? "").replace(/[&<>"']/g, (c) => (
        { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
      ));
    },
    fmtPct(v, d = 1) {
      return v === null || v === undefined || isNaN(v) ? "—" : (v * 100).toFixed(d) + "%";
    },
    fmtInt(v) {
      const n = Number(v);
      if (!isFinite(n)) return "—";
      return String(Math.round(n)).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
    },
    signed(v, d = 0) {
      const n = Number(v);
      if (!isFinite(n)) return "—";
      const s = d ? n.toFixed(d) : String(Math.round(n));
      return (n > 0 ? "+" : "") + s;
    },
    mmss(seconds) {
      const s = Math.max(0, Math.floor(Number(seconds) || 0));
      return String(Math.floor(s / 60)).padStart(2, "0") + ":" + String(s % 60).padStart(2, "0");
    },
    clamp(n, a, b) {
      return Math.max(a, Math.min(b, n));
    },
    hueOf(name) {
      let h = 0;
      const s = String(name || "");
      for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
      return h;
    },
    avatarStyle(name) {
      return "--h:" + this.hueOf(name);
    },
    countUp(el, to, opts = {}) {
      if (!el) return;
      const target = Number(to);
      if (!isFinite(target)) { el.textContent = "—"; return; }
      const fmt = opts.fmt;
      const text = (v) => (fmt ? fmt(v) : String(Math.round(v * 100) / 100));
      const from = el.__val;
      el.__val = target;
      const minDelta = opts.minDelta != null ? opts.minDelta : 1e-4;
      const reduced = global.matchMedia && global.matchMedia("(prefers-reduced-motion: reduce)").matches;
      if (reduced || from === undefined || !isFinite(from) || Math.abs(target - from) < minDelta) {
        el.textContent = text(target);
        return;
      }
      const ms = opts.ms || 650;
      const t0 = performance.now();
      const ease = (t) => 1 - Math.pow(1 - t, 3);
      const step = (now) => {
        const k = Math.min(1, (now - t0) / ms);
        el.textContent = text(from + (target - from) * ease(k));
        if (k < 1) requestAnimationFrame(step);
      };
      requestAnimationFrame(step);
    },
    setP(el, p) {
      if (el) el.style.setProperty("--p", this.clamp(Number(p) || 0, 0, 1));
    },
    setRing(svg, p) {
      if (!svg) return;
      const circle = svg.querySelector(".ring-value");
      if (!circle) return;
      const r = Number(circle.getAttribute("r") || 54);
      const c = 2 * Math.PI * r;
      const v = this.clamp(Number(p) || 0, 0, 1);
      circle.setAttribute("stroke-dasharray", (c * v).toFixed(2) + " " + c.toFixed(2));
      circle.setAttribute("stroke-dashoffset", "0");
    },
    drawCurve(svg, values, opts = {}) {
      const W = 600, H = opts.h || 150, P = opts.pad != null ? opts.pad : 10;
      let vals = (values || []).map(Number).filter(isFinite);
      const line = svg.querySelector(".curve-line");
      const area = svg.querySelector(".curve-area");
      const last = svg.querySelector(".curve-last");
      const clear = () => {
        if (line) line.setAttribute("d", "");
        if (area) area.setAttribute("d", "");
        if (last) last.setAttribute("cx", "-100");
      };
      if (!vals.length) { clear(); return null; }
      if (vals.length === 1) vals = [vals[0], vals[0]];
      let lo = opts.min != null ? opts.min : Math.min.apply(null, vals);
      let hi = opts.max != null ? opts.max : Math.max.apply(null, vals);
      if (hi - lo < 1e-6) { lo -= 0.02; hi += 0.02; }
      const span = hi - lo;
      const y0 = lo - span * 0.2, y1 = hi + span * 0.2;
      const n = vals.length;
      const x = (i) => P + (i / (n - 1)) * (W - 2 * P);
      const y = (v) => H - P - ((v - y0) / (y1 - y0)) * (H - 2 * P);
      const d = vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(2) + "," + y(v).toFixed(2)).join("");
      if (line) line.setAttribute("d", d);
      if (area) area.setAttribute("d", d + " L" + x(n - 1).toFixed(2) + "," + (H - P) + " L" + x(0).toFixed(2) + "," + (H - P) + " Z");
      if (last) { last.setAttribute("cx", x(n - 1)); last.setAttribute("cy", y(vals[n - 1])); }
      return { lo, hi };
    },
    toast(text, opts = {}) {
      let el = document.getElementById("toast");
      if (!el) {
        el = document.createElement("div");
        el.id = "toast";
        el.className = "toast";
        document.body.appendChild(el);
      }
      el.className = "toast" + (opts.type === "error" ? " toast-error" : "") + " show";
      el.innerHTML = '<span class="toast-dot"></span><span>' + this.esc(text) + "</span>";
      clearTimeout(el.__t);
      el.__t = setTimeout(() => el.classList.remove("show"), opts.ms || 3200);
    },
    cssVar(name) {
      try {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
      } catch (e) {
        return "";
      }
    },
    bootTheme(params) {
      /* 主题启动：?theme=dark|light 优先（写入存储），否则读 localStorage，默认浅色 */
      const KEY = "loladvisor.theme";
      let t = params && params.get ? params.get("theme") : "";
      if (t !== "dark" && t !== "light") t = "";
      if (t) {
        try { localStorage.setItem(KEY, t); } catch (e) {}
      } else {
        try { t = localStorage.getItem(KEY) || ""; } catch (e) { t = ""; }
      }
      if (t === "dark") document.documentElement.dataset.theme = "dark";
      else document.documentElement.removeAttribute("data-theme");
      return t;
    },
    attachDemoPanel(handlers, label) {
      document.body.classList.add("demo-active");
      const panel = document.createElement("div");
      panel.className = "demo-panel";
      panel.innerHTML =
        '<div class="t">' + this.esc(label) + ' · 演示模式</div>' +
        '<div class="demo-btns">' +
        '<button type="button" class="demo-btn on" data-state="default">默认</button>' +
        '<button type="button" class="demo-btn" data-state="loading">加载中</button>' +
        '<button type="button" class="demo-btn" data-state="empty">空态</button>' +
        '<button type="button" class="demo-btn" data-state="error">错误态</button>' +
        "</div>";
      panel.addEventListener("click", (e) => {
        const btn = e.target.closest(".demo-btn");
        if (!btn) return;
        panel.querySelectorAll(".demo-btn").forEach((b) => b.classList.remove("on"));
        btn.classList.add("on");
        const s = btn.dataset.state;
        (handlers[s] || handlers.default)();
      });
      document.body.appendChild(panel);
      return panel;
    },
  };

  /* ---------------------------------------------------------------- 英雄库 */
  const CHAMPS = {
    ahri: { cn: "阿狸", id: 103 },
    garen: { cn: "盖伦", id: 86 },
    leesin: { cn: "李青", id: 64 },
    jinx: { cn: "金克丝", id: 222 },
    thresh: { cn: "锤石", id: 412 },
    ksante: { cn: "奎桑提", id: 897 },
    vi: { cn: "蔚", id: 254 },
    syndra: { cn: "辛德拉", id: 134 },
    caitlyn: { cn: "凯特琳", id: 51 },
    lulu: { cn: "璐璐", id: 117 },
    zed: { cn: "劫", id: 238 },
    yasuo: { cn: "亚索", id: 157 },
    orianna: { cn: "奥莉安娜", id: 61 },
    viktor: { cn: "维克托", id: 112 },
    zoe: { cn: "佐伊", id: 142 },
    leblanc: { cn: "乐芙兰", id: 7 },
  };
  const ROLES = ["TOP", "JUNGLE", "MIDDLE", "BOTTOM", "UTILITY"];
  const ROLE_CN = { TOP: "上单", JUNGLE: "打野", MIDDLE: "中单", BOTTOM: "下路", UTILITY: "辅助" };

  /* ---------------------------------------------------------------- 选人助手 */
  const DRAFT_STEPS = [
    { left: 24, prob: 0.5, my: ["MIDDLE:ahri"], enemy: [], hist: [[24, 0.5]] },
    { left: 21, prob: 0.545, my: ["MIDDLE:ahri", "TOP:garen"], enemy: ["TOP:ksante"], hist: [[24, 0.5], [21, 0.545]] },
    { left: 18, prob: 0.578, my: ["MIDDLE:ahri", "TOP:garen", "JUNGLE:leesin"], enemy: ["TOP:ksante", "JUNGLE:vi"], hist: [[24, 0.5], [21, 0.545], [18, 0.578]] },
    { left: 15, prob: 0.552, my: ["MIDDLE:ahri", "TOP:garen", "JUNGLE:leesin"], enemy: ["TOP:ksante", "JUNGLE:vi", "MIDDLE:syndra"], hist: [[24, 0.5], [21, 0.545], [18, 0.578], [15, 0.552]] },
    { left: 12, prob: 0.601, my: ["MIDDLE:ahri", "TOP:garen", "JUNGLE:leesin", "BOTTOM:jinx"], enemy: ["TOP:ksante", "JUNGLE:vi", "MIDDLE:syndra", "BOTTOM:caitlyn"], hist: [[24, 0.5], [21, 0.545], [18, 0.578], [15, 0.552], [12, 0.601]] },
    { left: 9, prob: 0.634, my: ["MIDDLE:ahri", "TOP:garen", "JUNGLE:leesin", "BOTTOM:jinx", "UTILITY:thresh"], enemy: ["TOP:ksante", "JUNGLE:vi", "MIDDLE:syndra", "BOTTOM:caitlyn", "UTILITY:lulu"], hist: [[24, 0.5], [21, 0.545], [18, 0.578], [15, 0.552], [12, 0.601], [9, 0.634]] },
  ];

  const DRAFT_CANDIDATES = [
    { key: "orianna", champ: "奥莉安娜", champKey: "Orianna", champId: 61, prob: 0.642, opgg: 0.521, cohort: 0.538, cohortGames: 5217, matchup: 0.547, personal: null, score: 0.642, reason: "版本 52.1% · 对线 55% · 高分段 53.8%（5,217 场）" },
    { key: "viktor", champ: "维克托", champKey: "Viktor", champId: 112, prob: 0.628, opgg: 0.514, cohort: 0.524, cohortGames: 4832, matchup: 0.533, personal: null, score: 0.628, reason: "版本 51.4% · 对线 53% · 高分段 52.4%（4,832 场）" },
    { key: "ahri", champ: "阿狸", champKey: "Ahri", champId: 103, prob: 0.623, opgg: 0.517, cohort: 0.523, cohortGames: 4123, matchup: 0.548, personal: { games: 8, wins: 5 }, score: 0.626, reason: "版本 51.7% · 对线 55% · 你 5 胜 3 负" },
    { key: "syndra", champ: "辛德拉", champKey: "Syndra", champId: 134, prob: 0.617, opgg: 0.509, cohort: 0.517, cohortGames: 5944, matchup: 0.522, personal: null, score: 0.617, reason: "版本 50.9% · 对线 52% · 高分段 51.7%（5,944 场）" },
    { key: "zoe", champ: "佐伊", champKey: "Zoe", champId: 142, prob: 0.604, opgg: 0.501, cohort: 0.512, cohortGames: 2611, matchup: 0.512, personal: null, score: 0.604, reason: "版本 50.1% · 对线 51% · 高分段 51.2%（2,611 场）" },
    { key: "leblanc", champ: "乐芙兰", champKey: "Leblanc", champId: 7, prob: 0.596, opgg: 0.497, cohort: 0.508, cohortGames: 3052, matchup: 0.506, personal: { games: 3, wins: 1 }, score: 0.596, reason: "版本 49.7% · 对线 51% · 你 1 胜 2 负" },
  ];

  /* ---------------------------------------------------------------- 胜率-时间曲线（T22 追加，字段草案见 contracts.md） */
  function lineupCurve(prob) {
    const minutes = [], winProb = [];
    for (let m = 0; m <= 40; m++) {
      minutes.push(m);
      const rise = Math.min(1, m / 18); // 阵容影响在前 18 分钟逐渐到位
      const v = 0.5 + (prob - 0.5) * rise + 0.015 * Math.sin(m * 0.32) + m * 0.0008;
      winProb.push(Math.round(UI.clamp(v, 0.3, 0.85) * 1000) / 1000);
    }
    return { minutes, winProb };
  }

  function heroCurveOf(strength, seed) {
    const minutes = [], winProb = [];
    for (let m = 0; m <= 40; m++) {
      minutes.push(m);
      const v = strength + 0.045 * Math.sin(m * 0.35 + seed) + (m - 20) * 0.0005;
      winProb.push(Math.round(UI.clamp(v, 0.35, 0.85) * 1000) / 1000);
    }
    return { minutes, winProb };
  }

  const HERO_CURVE_DEFS = [
    { key: "Orianna", strength: 0.655, seed: 0.5 },
    { key: "Viktor", strength: 0.645, seed: 1.8 },
    { key: "Ahri", strength: 0.638, seed: 3.1 },
    { key: "Syndra", strength: 0.622, seed: 4.4 },
    { key: "Zoe", strength: 0.608, seed: 5.7 },
    { key: "Leblanc", strength: 0.596, seed: 7.0 },
  ];

  function heroCurves() {
    const out = {};
    HERO_CURVE_DEFS.forEach((d) => {
      out[d.key] = heroCurveOf(d.strength, d.seed);
    });
    return out;
  }

  /* 己方已锁英雄曲线（T31）：随选人步进逐步出现（key 与 myTeam.champKey 一致，小写别名） */
  const TEAM_CURVE_DEFS = {
    garen: { strength: 0.6, seed: 1.2 },
    leesin: { strength: 0.615, seed: 2.6 },
    ahri: { strength: 0.638, seed: 3.1 },
    jinx: { strength: 0.625, seed: 4.8 },
    thresh: { strength: 0.605, seed: 6.2 },
  };

  function teamCurves(step) {
    const out = {};
    (step.my || []).forEach((s) => {
      const key = s.split(":")[1];
      const def = TEAM_CURVE_DEFS[key];
      if (def) out[key] = heroCurveOf(def.strength, def.seed);
    });
    return out;
  }

  function draftState(stepIdx) {
    const step = DRAFT_STEPS[stepIdx] || DRAFT_STEPS[0];
    const my = {}, enemy = {};
    step.my.forEach((s) => { const p = s.split(":"); my[p[0]] = p[1]; });
    step.enemy.forEach((s) => { const p = s.split(":"); enemy[p[0]] = p[1]; });
    const teamRow = (role, side, isMine) => {
      const key = side[role];
      const c = key ? CHAMPS[key] : null;
      return {
        role, roleCn: ROLE_CN[role],
        champ: c ? c.cn : "",
        champKey: key || "",
        champId: c ? c.id : 0,
        locked: !!c,
        rank: isMine ? 5300 - ROLES.indexOf(role) * 90 : 5160 - ROLES.indexOf(role) * 95,
        name: role === "MIDDLE" && isMine ? "雾岛听风#777" : (isMine ? "蓝方" + ROLE_CN[role] + "#10" + ROLES.indexOf(role) : "红方" + ROLE_CN[role] + "#20" + ROLES.indexOf(role)),
        isMe: isMine && role === "MIDDLE",
      };
    };
    return {
      ok: true,
      source: "demo",
      phase: "BAN_PICK",
      left: step.left,
      myRole: "MIDDLE",
      myRoleCn: "中单",
      myTeam: ROLES.map((r) => teamRow(r, my, true)),
      theirTeam: ROLES.map((r) => teamRow(r, enemy, false)),
      bans: {
        mine: [{ id: CHAMPS.zed.id, name: CHAMPS.zed.cn }],
        theirs: [{ id: CHAMPS.yasuo.id, name: CHAMPS.yasuo.cn }],
      },
      winProb: step.prob,
      factors: {
        myRank: 5230,
        theirRank: 5085,
        champEdge: 1.6,
        laneEdge: 2.8,
        picked: Object.keys(my).length,
        enemyPicked: Object.keys(enemy).length,
      },
      attribution: [
        { key: "killDiff", nameCn: "击杀差", delta: 2.1 },
        { key: "csDiff", nameCn: "补刀差", delta: -1.3 },
        { key: "rank", nameCn: "段位", delta: 0.9 },
      ],
      rankStatus: {
        state: "ok", port: 48620, source: "lockfile",
        lastError: null, lastHttpStatus: null, lastNote: null,
        queries: 8, errors: 0, unranked: 0, lookupReasons: {},
      },
      rankError: "",
      history: step.hist.map((h) => ({ t: h[0], p: h[1] })),
      curve: lineupCurve(step.prob),
      heroCurves: heroCurves(),
      teamCurves: teamCurves(step),
      candidates: DRAFT_CANDIDATES,
      modelAuc: 0.69,
      lcu: { connected: true, port: 48620 },
    };
  }

  const draftWaiting = {
    ok: false,
    source: "live",
    message: "等待进入选人阶段…",
    lcu: { connected: false, port: null },
  };

  const draftError = {
    ok: false,
    source: "live",
    error: true,
    message: "无法获取选人数据（http://127.0.0.1:8788）",
    detail: "选人助手服务未响应。请确认 LoLAdvisor 已启动，或点击「重试」重新连接。",
    lcu: { connected: true, port: 48620 },
  };

  const draftNotStarted = {
    ok: false,
    source: "live",
    error: true,
    notStarted: true,
    message: "选人助手未启动",
    detail: "桌面版会把 /draft/* 代理到本机选人服务（127.0.0.1:8788）。请确认选人服务已启动，然后点击重新检测。",
    lcu: { connected: true, port: 48620 },
  };

  /* ---------------------------------------------------------------- 仪表盘 */
  function dashSeries(seconds) {
    const m = seconds / 60;
    return {
      winProb: UI.clamp(0.5 + 0.27 * Math.sin(m * 0.13) + 0.07 * Math.cos(m * 0.05) + m * 0.0035, 0.18, 0.88),
      kills: Math.round(1.5 + 2.6 * Math.sin(m * 0.21) + m * 0.16),
      deaths: Math.round(0.5 + 1.8 * Math.cos(m * 0.17) + m * 0.05),
      assists: Math.round(2 + 3.4 * Math.sin(m * 0.14 + 1) + m * 0.14),
      cs: Math.round(4 + 4.8 * Math.sin(m * 0.11) + m * 2.1),
      level: Math.round(1.2 + 1.5 * Math.sin(m * 0.16) + m * 0.2),
    };
  }

  const DASH_HISTORY = (() => {
    const h = { t: [], winProb: [], killDiff: [], csDiff: [], levelSumDiff: [], ownKill30: [], enemyKill30: [], ownGank60: [], enemyGank60: [] };
    for (let s = 0; s <= 1860; s += 30) {
      const m = s / 60;
      h.t.push(s);
      h.winProb.push(UI.clamp(0.5 + 0.27 * Math.sin(m * 0.13) + 0.07 * Math.cos(m * 0.05) + m * 0.0035, 0.18, 0.88));
      h.killDiff.push(Math.round(2.5 * Math.sin(m * 0.21) + m * 0.16));
      h.csDiff.push(Math.round(5 * Math.sin(m * 0.11) + m * 2.1));
      h.levelSumDiff.push(Math.round(1.6 * Math.sin(m * 0.16) + m * 0.2));
      h.ownKill30.push(UI.clamp(0.3 + 0.2 * Math.sin(m * 0.4), 0, 0.9));
      h.enemyKill30.push(UI.clamp(0.2 + 0.15 * Math.cos(m * 0.3), 0, 0.8));
      h.ownGank60.push(UI.clamp(0.25 + 0.18 * Math.sin(m * 0.25 + 1), 0, 0.9));
      h.enemyGank60.push(UI.clamp(0.15 + 0.12 * Math.cos(m * 0.35), 0, 0.8));
    }
    return h;
  })();

  function dashHistory(seconds) {
    const cut = Math.min(Math.floor(seconds / 30) * 30 + 30, DASH_HISTORY.t.length);
    const h = {};
    Object.keys(DASH_HISTORY).forEach((k) => { h[k] = DASH_HISTORY[k].slice(0, cut); });
    return h;
  }

  function dashState(seconds) {
    const m = seconds / 60;
    const s = dashSeries(seconds);
    const feeds = [
      { t: seconds - 335, text: "我方拿下火龙，小龙领先", side: "own" },
      { t: seconds - 260, text: "我方打野 Gank 中路，配合击杀敌方中单", side: "own" },
      { t: seconds - 185, text: "敌方集合推掉我方上路一塔", side: "enemy" },
      { t: seconds - 120, text: "我方拿下第二组巢虫", side: "own" },
      { t: seconds - 70, text: "我方辅助游走，中路再次击杀", side: "own" },
      { t: seconds - 42, text: "敌方拿下峡谷先锋", side: "enemy" },
      { t: seconds - 21, text: "我方击杀敌方打野", side: "own" },
    ].filter((f) => f.t >= 0);
    return {
      ok: true,
      source: "live",
      model: "live_model5",
      rank: { own: 5280, team: 5146, enemy: 4980, diff: 166, vsTeam: 134 },
      rankStatus: {
        state: "ok", port: 48620, source: "lockfile",
        lastError: null, lastHttpStatus: null, lastNote: null,
        queries: 12, errors: 0, unranked: 0, lookupReasons: {},
      },
      attribution: [
        { key: "killDiff", nameCn: "击杀差", delta: 2.1 },
        { key: "csDiff", nameCn: "补刀差", delta: -1.3 },
        { key: "rank", nameCn: "段位", delta: 0.9 },
      ],
      waiting: false,
      ended: false,
      clock: UI.mmss(seconds),
      gameTime: seconds,
      winProb: Math.round(s.winProb * 1000) / 1000,
      ownTeam: "ORDER",
      selfName: "雾岛听风#777",
      selfChampion: "阿狸",
      goldSource: "live",
      goldEstimated: false,
      own: {
        level: Math.min(18, 4 + Math.floor(m / 3)),
        kills: Math.max(0, Math.floor(m / 6) + 1),
        deaths: Math.max(0, Math.floor(m / 8)),
        assists: Math.floor(m / 4),
        cs: 20 + Math.floor(m * 8),
        gold: 500 + Math.floor(m * 60),
      },
      teamDiff: {
        kills: s.kills, deaths: -s.deaths, assists: s.assists, cs: s.cs, level: s.level,
        dragons: Math.floor(m / 9), barons: Math.floor(m / 22),
        heralds: Math.floor(m / 12), grubs: Math.min(6, Math.floor(m / 7) + 1),
        atakhan: Math.floor(m / 26), towers: Math.floor(m / 8), inhibitors: Math.floor(m / 24),
      },
      events: {
        own_kill_30s: Math.round(UI.clamp(0.35 + 0.16 * Math.sin(seconds / 90), 0.05, 0.9) * 100) / 100,
        enemy_kill_30s: Math.round(UI.clamp(0.24 + 0.14 * Math.cos(seconds / 75), 0.03, 0.8) * 100) / 100,
        own_gank_60s: Math.round(UI.clamp(0.3 + 0.15 * Math.sin(seconds / 110 + 1), 0.03, 0.85) * 100) / 100,
        enemy_gank_60s: Math.round(UI.clamp(0.18 + 0.12 * Math.cos(seconds / 95), 0.02, 0.7) * 100) / 100,
      },
      advice: [
        { source: "rule", text: "击杀与补刀双线领先，保持对线节奏，避免无视野深带。" },
        { source: "rule", text: "敌方打野 60 秒内可能发起 Gank，下路注意河道视野。" },
        { source: "rule", text: "小龙刷新在即，提前落位并在河道布置视野。" },
        { source: "llm", text: "经济领先且视野受控，可尝试在 25 分钟前控下二先锋，随后抱团推进中路二塔。" },
      ],
      feed: feeds,
      history: dashHistory(seconds),
      llm: {
        enabled: true,
        lastAt: seconds - 90,
        error: null,
        tips: ["经济领先且视野受控，可尝试在 25 分钟前控下二先锋，随后抱团推进中路二塔。"],
      },
    };
  }

  const dashWaiting = {
    ok: false, waiting: true, ended: false,
    source: "live", clock: "--:--", winProb: null,
    message: "等待对局…",
  };

  const dashError = {
    ok: false, waiting: true, ended: false, error: true,
    source: "live", clock: "--:--", winProb: null,
    message: "无法获取对局数据（http://127.0.0.1:8765）",
    detail: "实时助手服务未响应。请确认已通过 run-web.cmd 或桌面版启动，或点击「重试」重新连接。",
  };

  const dashEnded = {
    ok: false, waiting: false, ended: true,
    source: "replay", clock: "31:00", winProb: 0.712,
    message: "回放已结束",
  };

  /* ---------------------------------------------------------------- 段位三态（rank_lookup.status() 形状） */
  const RANK_STATES = {
    unconnected: {
      state: "unconnected", port: null, source: "lockfile",
      lastError: null, lastHttpStatus: null, lastNote: null,
      queries: 0, errors: 0, unranked: 0, lookupReasons: {},
    },
    resolving: {
      state: "resolving", port: 48620, source: "lockfile",
      lastError: null, lastHttpStatus: null, lastNote: null,
      queries: 6, errors: 0, unranked: 0, lookupReasons: {},
    },
    failed: {
      state: "failed", port: 48620, source: "lockfile",
      lastError: "LCU HTTP 503（/lol-ranked/v1/current-ranked-stats）",
      lastHttpStatus: 503, lastNote: null,
      queries: 9, errors: 3, unranked: 0,
      lookupReasons: { "蓝方下路#1003": "查不到召唤师（名字不匹配或非本区）" },
    },
  };

  function applyRankState(state, kind) {
    const out = JSON.parse(JSON.stringify(state));
    const rs = RANK_STATES[kind];
    if (!rs) return out;
    if (kind !== "ok") {
      out.rank = null;
      if (out.factors) {
        out.factors.myRank = null;
        out.factors.theirRank = null;
      }
    }
    out.rankStatus = JSON.parse(JSON.stringify(rs));
    out.rankError = rs.lastError || rs.lastNote || (kind === "unconnected" ? "LCU 未连接" : "");
    return out;
  }

  /* ---------------------------------------------------------------- 个人主页（T33，/api/profile 形状草案） */
  function profileData() {
    const now = Date.now();
    const positions = ["中路", "打野", "下路", "辅助", "上路"];
    const match = (champKey, champId, win, k, d, a, gameTime, hoursAgo, index) => ({
      champId,
      champ: CHAMPS[champKey] ? CHAMPS[champKey].cn : champKey,
      win,
      kills: k,
      deaths: d,
      assists: a,
      gameTime,
      playedAt: new Date(now - hoursAgo * 3600e3).toISOString(),
      /* T62：战绩检查补全字段 */
      queueId: 420,
      queueCn: index % 4 === 1 ? "灵活排位" : index % 5 === 3 ? "匹配" : "单双排",
      cs: Math.round((gameTime / 60) * 6.2),
      gold: Math.round((gameTime / 60) * 430),
      damage: Math.round((gameTime / 60) * 950),
      vision: 18 + (index % 14),
      positionCn: positions[index % positions.length],
      remake: false,
    });
    const matches = [
      match("ahri", 103, true, 8, 3, 11, 1732, 1, 0),
      match("leesin", 64, false, 5, 7, 9, 1891, 3, 1),
      match("jinx", 222, true, 12, 4, 8, 2104, 6, 2),
      match("thresh", 412, true, 1, 5, 19, 1655, 9, 3),
      match("garen", 86, false, 6, 8, 5, 1788, 14, 4),
      match("vi", 254, true, 7, 3, 12, 1942, 22, 5),
      match("syndra", 134, true, 9, 2, 7, 1602, 30, 6),
      match("caitlyn", 51, false, 4, 6, 10, 2241, 46, 7),
      match("lulu", 117, false, 2, 7, 14, 1568, 70, 8),
      match("zed", 238, true, 11, 5, 6, 1853, 96, 9),
      match("yasuo", 157, false, 3, 9, 4, 1745, 130, 10),
      match("orianna", 61, true, 6, 2, 13, 2008, 190, 11),
    ];
    const wins = matches.filter((m) => m.win).length;
    const kills = matches.reduce((s, m) => s + m.kills, 0);
    const deaths = matches.reduce((s, m) => s + m.deaths, 0);
    const assists = matches.reduce((s, m) => s + m.assists, 0);
    const minutes = matches.reduce((s, m) => s + m.gameTime, 0) / 60;
    const cs = matches.reduce((s, m) => s + m.cs, 0);
    const streakKind = matches[0].win ? "win" : "loss";
    let streak = 0;
    for (const m of matches) {
      if (m.win === (streakKind === "win")) streak += 1; else break;
    }
    const matchSummary = {
      games: matches.length,
      wins,
      losses: matches.length - wins,
      winRate: Math.round((wins * 1000) / matches.length) / 10,
      kdaRatio: Math.round(((kills + assists) / Math.max(1, deaths)) * 100) / 100,
      kills, deaths, assists,
      avgCsPerMin: Math.round((cs / minutes) * 10) / 10,
      avgDurationMin: Math.round((minutes / matches.length) * 10) / 10,
      streak, streakKind,
    };
    return {
      ok: true,
      summoner: { name: "雾岛听风#777", level: 328, iconId: 4868 },
      solo: { tier: "EMERALD", tierCn: "翡翠", division: "III", lp: 42, wins: 168, losses: 154, unranked: false },
      flex: { tier: null, tierCn: "", division: "", lp: 0, wins: 0, losses: 0, unranked: true },
      matches,
      matchSummary,
    };
  }
  const profileUnconnected = {
    ok: false,
    message: "客户端未连接",
    detail: "请启动并登录英雄联盟客户端；连接后将自动展示召唤师资料与近期对局。",
  };

  /* ---------------------------------------------------------------- 打野雷达（T53，/api/jungler 形状草案） */
  const JUNGLER_ROUTE = [
    { camp: "红BUFF", time: 95 },
    { camp: "石甲虫", time: 145 },
    { camp: "锋喙鸟", time: 200 },
    { camp: "蓝BUFF", time: 280 },
    { camp: "魔沼蛙", time: 340 },
  ];

  const junglerTracking = {
    ok: true,
    state: "tracking",
    jungler: { champId: 64, champ: "李青", name: "红方打野#2001" },
    lastSeen: { zone: "下河道", gameTime: 214, ago: 32 },
    predicted: { zone: "上野区", confidence: 0.76, eta: 45 },
    threatLane: "下路",
    route: JUNGLER_ROUTE.map((c, i) => ({ ...c, done: i < 3, current: i === 3 })),
    capture: { available: true, fps: 1, url: "/capture/frame.jpg" },
  };

  const junglerSpotted = {
    ok: true,
    state: "spotted",
    jungler: { champId: 64, champ: "李青", name: "红方打野#2001" },
    lastSeen: { zone: "上路河道", gameTime: 96, ago: 58 },
    predicted: { zone: "上野区", confidence: 0.42, eta: null },
    threatLane: null,
    route: JUNGLER_ROUTE.map((c, i) => ({ ...c, done: i < 2, current: i === 2 })),
    capture: { available: true, fps: 1, url: "/capture/frame.jpg" },
  };

  const junglerUnseen = {
    ok: true,
    state: "unseen",
    jungler: { champId: 64, champ: "李青", name: "红方打野#2001" },
    lastSeen: null,
    predicted: null,
    threatLane: null,
    route: JUNGLER_ROUTE.map((c) => ({ ...c, done: false, current: false })),
    capture: { available: true, fps: 1, url: "/capture/frame.jpg" },
  };

  /* ---------------------------------------------------------------- 主窗口（home 期望契约） */
  function homeDefault() {
    return {
      ok: true,
      appTitle: "LoLAdvisor",
      version: "v2.3.0",
      lcu: {
        connected: true, port: 48620, source: "lockfile",
        lastError: null, lastHttpStatus: null, lastNote: null,
        queries: 12, errors: 0, unranked: 0, lookupReasons: {},
      },
      draft: { available: true, running: true, url: "http://127.0.0.1:8788" },
      dashboard: { available: true, running: true, url: "http://127.0.0.1:8765" },
      settings: {
        interval: 5.0,
        llm: false,
        llmInterval: 20.0,
        ranks: true,
        autoOpenDraft: false,
        rankCache: "C:\\Users\\Lucas\\.lol-advisor\\rank_cache.json",
      },
      selftest: {
        passed: true,
        at: "2026-11-12 09:58",
        lines: [
          "live_model3: 1,200 棵树 · 34 特征",
          "live_model5: 1,340 棵树 · 36 特征",
          "model3 胜率: 0.6247",
          "model5 胜率（段位差 +1000）: 0.7123",
          "SELFTEST OK",
        ],
      },
      diag: {
        at: "2026-11-12 09:57",
        processes: 412,
        league: 4,
        lcuPort: 48620,
      },
      state: dashState(1320),
    };
  }

  function homeWaiting() {
    const h = homeDefault();
    h.state = {
      ok: false, waiting: true, ended: false,
      source: "live", clock: "--:--", winProb: null,
      message: "等待对局…",
    };
    return h;
  }

  function homeError() {
    const h = homeDefault();
    h.error = "内部服务未响应";
    h.detail = "实时采集服务启动失败。请点击「运行自检」定位问题，或重启 LoLAdvisor。";
    h.state = {
      ok: false, waiting: true, ended: false, error: true,
      source: "live", clock: "--:--", winProb: null,
      message: "采集服务异常",
    };
    return h;
  }

  /* ---------------------------------------------------------------- 导出 */
  global.LOL_DEMO = {
    UI,
    champs: CHAMPS,
    roles: ROLES,
    roleCn: ROLE_CN,
    draft: { steps: DRAFT_STEPS, state: draftState, waiting: draftWaiting, error: draftError, notStarted: draftNotStarted },
    dash: { state: dashState, waiting: dashWaiting, error: dashError, ended: dashEnded },
    home: { default: homeDefault, waiting: homeWaiting, error: homeError },
    profile: { data: profileData, unconnected: profileUnconnected },
    jungler: { tracking: junglerTracking, spotted: junglerSpotted, unseen: junglerUnseen },
    rankStates: RANK_STATES,
    applyRankState,
  };
})(window);
