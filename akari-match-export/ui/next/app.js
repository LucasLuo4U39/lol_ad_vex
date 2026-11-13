/* =====================================================================
   LoLAdvisor 一体化单页（app.html 专属逻辑）
   左侧窄栏 + 主区多栏：选人棋盘 / 曲线与推荐 / 赛前实力与建议事件。
   数据：/api/home（主页与段位）+ /draft/state（桌面版代理选人服务，
   见 contracts.md 第 7 节）。demo 模式纯静态。
   ===================================================================== */
(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  const IS_DEMO = params.get("demo") === "1";
  const RANK = params.get("rank");
  const DEMO = window.LOL_DEMO;
  const UI = DEMO.UI;
  UI.bootTheme(params);
  // T42：确保 data-theme 总是显式存在（看门狗自诊断依赖该标记区分「早期中断」与「绑定段未完成」）
  if (!document.documentElement.dataset.theme) document.documentElement.dataset.theme = "light";
  const ROLE_CN = DEMO.roleCn;
  const ROLES = DEMO.roles;
  const $ = (id) => document.getElementById(id);
  const THEME_KEY = "loladvisor.theme";

  function themeIsDark() {
    return document.documentElement.dataset.theme === "dark";
  }
  function setTheme(dark) {
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    try { localStorage.setItem(THEME_KEY, dark ? "dark" : "light"); } catch (e) {}
    syncThemeUi();
    if (lastDraft) renderCurve(lastDraft);
  }
  function syncThemeUi() {
    const sw = $("swTheme");
    if (sw) sw.setAttribute("aria-checked", themeIsDark() ? "true" : "false");
    const hint = $("themeHint");
    if (hint) hint.textContent = themeIsDark() ? "深色电竞（当前）" : "浅色暖色（当前）";
  }
  const PHASE_CN = {
    BAN_PICK: "选人阶段", PLANNING: "预选阶段", FINALIZATION: "确认阶段",
    GAME_STARTING: "即将开始", IN_GAME: "对局进行中", END_OF_GAME: "结算中", LAST_DRAFT: "最后阵容",
  };
  const SRC_CN = { live: "实时模式", replay: "回放模式", spectator: "观战模式" };
  const GOLD_CN = { sgp: "SGP 1 分钟口袋金币", estimate: "装备估算", live: "实时" };
  const bridge = window.lolAdvisor || {};

  /* ---------------------------------------------------------- 片段 */
  function avatarHtml(champ, champId, cls) {
    const name = champ || "";
    const init = name.trim().slice(0, 1) || "·";
    const img = !IS_DEMO && champId
      ? '<img src="/icon/' + champId + '.png" alt="" onerror="this.remove()">'
      : "";
    return '<span class="avatar ' + (cls || "") + '" style="' + UI.avatarStyle(name) + '">' +
      '<span class="init">' + UI.esc(init) + "</span>" + img + "</span>";
  }

  function slotHtml(p, right) {
    const side = right ? " right" : "";
    if (!p || !p.champ) {
      const roleCn = (p && p.role && ROLE_CN[p.role]) || "";
      return '<div class="draft-slot' + side + ' blank">' +
        '<span class="avatar avatar-40 pending"><span class="init">' + UI.esc(roleCn ? roleCn.slice(0, 1) : "·") + "</span></span>" +
        '<div class="draft-info"><div class="draft-champ">待选择</div><div class="draft-meta">&nbsp;</div></div></div>';
    }
    const tag = p.isMe ? '<span class="me-tag">我</span>' : "";
    const rank = p.rank ? '<span class="draft-rank">' + UI.fmtInt(p.rank) + "</span>" : "";
    return '<div class="draft-slot' + side + (p.isMe ? " me" : "") + '">' +
      avatarHtml(p.champ, p.champId, "avatar-40" + (p.locked ? "" : " dim")) +
      '<div class="draft-info">' +
      '<div class="draft-champ">' + UI.esc(p.champ) + tag + "</div>" +
      '<div class="draft-meta">' + UI.esc(p.name || "") + " " + rank + "</div>" +
      "</div></div>";
  }

  function boardHtml(state) {
    const roleIdx = (r) => { const i = ROLES.indexOf(r.role); return i === -1 ? 99 : i; };
    const my = (state.myTeam || []).slice().sort((a, b) => roleIdx(a) - roleIdx(b));
    const en = (state.theirTeam || []).slice().sort((a, b) => roleIdx(a) - roleIdx(b));
    const rows = ROLES.map((role) => ({ role, me: null, en: null }));
    my.forEach((p) => {
      const slot = rows.find((r) => r.role === p.role && !r.me) || rows.find((r) => !r.me);
      if (slot) slot.me = p;
    });
    en.forEach((p) => {
      const slot = rows.find((r) => r.role === p.role && !r.en) || rows.find((r) => !r.en);
      if (slot) slot.en = p;
    });
    return '<div class="draft-head"><div class="teamlabel" style="color:var(--blue)">我方</div><div class="c">位置</div><div class="r teamlabel" style="color:var(--red)">敌方</div></div>' +
      rows.map((r) =>
        '<div class="draft-row">' +
        slotHtml(r.me, false) +
        '<div class="vs-col"><span class="role">' + UI.esc(ROLE_CN[r.role] || r.role) + "</span></div>" +
        slotHtml(r.en, true) +
        "</div>"
      ).join("");
  }

  function bansHtml(state) {
    const bans = state.bans || {};
    const tile = (b) => {
      if (!b || !b.name) return '<span class="ban-tile blank">·</span>';
      return '<span class="ban-tile">' + avatarHtml(b.name, b.id, "avatar-28") + "</span>";
    };
    const pad = (arr) => { const a = arr.slice(0, 5); while (a.length < 5) a.push(null); return a.map(tile).join(""); };
    const hasBans = (bans.mine || []).length + (bans.theirs || []).length > 0;
    if (!hasBans) return "";
    return '<div class="bans-line">' +
      '<div class="ban-row"><span class="ban-label">我方禁用</span>' + pad(bans.mine || []) + "</div>" +
      '<div class="ban-mid">禁用</div>' +
      '<div class="ban-row right">' + pad(bans.theirs || []) + '<span class="ban-label">敌方禁用</span></div>' +
      "</div>";
  }

  function oddsHtml(state) {
    const p = state.winProb;
    return '<div class="odds full">' +
      '<div class="odds-nums"><span class="side">我方</span><b class="b" id="pBlue">' + UI.fmtPct(p) + '</b><b class="r" id="pRed">' + UI.fmtPct(1 - p) + '</b><span class="side">敌方</span></div>' +
      '<div class="odds-bar"><div class="odds-fill" style="--p:' + UI.clamp(p, 0, 1) + '"></div></div>' +
      "</div>";
  }

  function attributionHtml(state) {
    /* 胜率归因 chips（T48）：正值绿/负值红、pp 一位小数；attribution 缺失时隐藏整行。
       单位兼容（T58）：接口 delta 是概率差（0.021 = +2.1pp），需 ×100；demo-data 直接给
       pp（2.1 / -1.3 / 0.9）。概率差实际不会超过 ~0.3，故 |delta| ≤ 0.5 判为概率差，
       否则视为已是 pp（阈值取 0.5 而非 1，避免把演示的 0.9pp 误乘成 90pp）。 */
    const attrs = state.attribution || [];
    if (!attrs.length) return "";
    return '<div class="attr-row"><span class="attr-label">归因</span>' +
      attrs.slice(0, 5).map((a) => {
        const raw = Number(a.delta) || 0;
        /* 后端 T58 起直接给 pp；旧后端/演示数据没有 pp，退回按量级判断（|delta|≤0.5 视为概率差） */
        const pp = a.pp !== null && a.pp !== undefined
          ? Number(a.pp)
          : (Math.abs(raw) <= 0.5 ? raw * 100 : raw);
        const cls = pp > 0 ? "pos" : pp < 0 ? "neg" : "zero";
        return '<span class="attr-chip ' + cls + '"><span class="k">' + UI.esc(a.nameCn || a.key || "—") + "</span>" +
          '<span class="v">' + (pp > 0 ? "+" : "") + pp.toFixed(1) + "pp</span></span>";
      }).join("") + "</div>";
  }

  /* ---------------------------------------------------------- 曲线（胜率-时间，T25 + T31 常驻己方英雄线） */
  const HERO_PALETTE = ["#007aff", "#34c759", "#ff453a", "#af52de", "#32ade6", "#ff2d55"];
  const TEAM_PALETTE = ["#7db8ff", "#6fe3a4", "#f5c94f", "#ff8a7d", "#c9a8ff"];
  const heroSel = {};
  const HEROES_PARAM = params.get("heroes") || "";
  (HEROES_PARAM ? HEROES_PARAM.split(",") : []).forEach((k) => { if (k) heroSel[k.trim()] = true; });

  function colorOf(state, key) {
    const list = state.candidates || [];
    const idx = list.findIndex((c) => c.champKey === key);
    if (idx >= 0) return HERO_PALETTE[idx % HERO_PALETTE.length];
    return "hsl(" + UI.hueOf(key) + " 60% 55%)";
  }

  function heroLabel(state, key) {
    const list = state.candidates || [];
    const c = list.find((x) => x.champKey === key);
    return c ? c.champ : key;
  }

  function teamCurveList(state) {
    /* 己方已锁英雄曲线：只画已锁且 teamCurves 有数据的（缺失/未锁不画） */
    const tc = state.teamCurves || {};
    const out = [];
    (state.myTeam || []).forEach((row, i) => {
      if (!row.locked || !row.champKey) return;
      let hc = tc[row.champKey];
      if (!hc) {
        const lk = String(row.champKey).toLowerCase();
        const found = Object.keys(tc).find((k) => k.toLowerCase() === lk);
        if (found) hc = tc[found];
      }
      if (!hc || !hc.winProb || !hc.winProb.length) return;
      out.push({
        key: row.champKey,
        color: TEAM_PALETTE[i % TEAM_PALETTE.length],
        label: row.champ || row.champKey,
        values: hc.winProb,
        fill: false,
        kind: "team",
      });
    });
    return out;
  }

  function renderCurve(state) {
    const LINEUP = UI.cssVar("--curve-main") || "#b45309";
    const DOT_RING = UI.cssVar("--dot-ring") || "#fffcf8";
    const minutes = (state.curve && state.curve.minutes) || [];
    const lineupVals = (state.curve && state.curve.winProb) || (state.history || []).map((h) => h.p);
    const teamKeys = {};
    const series = [{ key: "__lineup", color: LINEUP, label: "当前阵容", values: lineupVals, fill: true, kind: "lineup" }];
    teamCurveList(state).forEach((t) => {
      teamKeys[String(t.key).toLowerCase()] = true;
      series.push(t);
    });
    Object.keys(heroSel).forEach((k) => {
      if (!heroSel[k]) return;
      if (teamKeys[String(k).toLowerCase()]) return; // 己方已锁英雄已在「我方」组显示，不重复叠加
      let hc = (state.heroCurves || {})[k];
      if (!hc) {
        const lk = String(k).toLowerCase();
        const found = Object.keys(state.heroCurves || {}).find((x) => x.toLowerCase() === lk);
        if (found) hc = state.heroCurves[found];
      }
      if (!hc || !hc.winProb || !hc.winProb.length) return;
      series.push({ key: k, color: colorOf(state, k), label: heroLabel(state, k), values: hc.winProb, fill: false, kind: "overlay" });
    });
    let lo = Infinity, hi = -Infinity;
    series.forEach((s) => s.values.forEach((v) => { if (v < lo) lo = v; if (v > hi) hi = v; }));
    if (!isFinite(lo)) { lo = 0.4; hi = 0.8; }
    const span = Math.max(0.05, hi - lo);
    lo -= span * 0.18; hi += span * 0.12;
    lo = Math.max(0, lo); hi = Math.min(1, hi);
    const W = 600, H = 110, P = 12;
    const xs = (i) => minutes.length
      ? P + (minutes[Math.min(i, minutes.length - 1)] / 40) * (W - 2 * P)
      : P + (i / Math.max(1, lineupVals.length - 1)) * (W - 2 * P);
    const ys = (v) => H - P - ((v - lo) / (hi - lo)) * (H - 2 * P);
    let html = "";
    series.forEach((s) => {
      let vals = s.values;
      if (!vals || !vals.length) return;
      if (vals.length === 1) vals = [vals[0], vals[0]];
      const n = vals.length;
      const d = vals.map((v, i) => (i ? "L" : "M") + xs(i).toFixed(2) + "," + ys(v).toFixed(2)).join("");
      if (s.fill) {
        html += '<path fill="url(#appGrad)" d="' + d + " L" + xs(n - 1).toFixed(2) + "," + (H - P) + " L" + xs(0).toFixed(2) + "," + (H - P) + ' Z"></path>';
      }
      const width = s.kind === "lineup" ? 2.8 : s.kind === "team" ? 1.6 : 2.2;
      const opacity = s.kind === "team" ? 0.85 : 1;
      html += '<path fill="none" stroke="' + s.color + '" stroke-width="' + width + '" stroke-opacity="' + opacity + '" stroke-linecap="round" stroke-linejoin="round" d="' + d + '"></path>';
      html += '<circle r="' + (s.kind === "team" ? 2.2 : 3.2) + '" fill="' + s.color + '" stroke="' + DOT_RING + '" stroke-width="1.5" cx="' + xs(n - 1).toFixed(2) + '" cy="' + ys(vals[n - 1]).toFixed(2) + '"></circle>';
    });
    $("curveSeries").innerHTML = html;
    $("curveY").innerHTML =
      "<span>" + (hi * 100).toFixed(0) + "%</span>" +
      "<span>" + ((lo + hi) / 2 * 100).toFixed(0) + "%</span>" +
      "<span>" + (lo * 100).toFixed(0) + "%</span>";
    const chips = ['<span class="legend-chip" style="--c:' + LINEUP + '"><i></i>当前阵容</span>'];
    const teams = series.filter((s) => s.kind === "team");
    if (teams.length) {
      chips.push('<span class="legend-group">我方</span>');
      teams.forEach((s) => {
        chips.push('<span class="legend-chip team" style="--c:' + s.color + '"><i></i>' + UI.esc(s.label) + "</span>");
      });
    }
    const overlays = series.filter((s) => s.kind === "overlay");
    if (overlays.length) {
      chips.push('<span class="legend-group">叠加</span>');
      overlays.forEach((s) => {
        chips.push('<span class="legend-chip" style="--c:' + s.color + '"><i></i>' + UI.esc(s.label) +
          '<button type="button" class="x" data-hero="' + UI.esc(s.key) + '" aria-label="移除 ' + UI.esc(s.label) + ' 曲线">×</button></span>');
      });
    }
    $("curveLegend").innerHTML = chips.join("") || "";
    $("curveLegend").querySelectorAll(".x").forEach((b) => {
      b.addEventListener("click", () => {
        heroSel[b.dataset.hero] = false;
        if (lastDraft) { renderCurve(lastDraft); $("cands").innerHTML = candsHtml(lastDraft); wireCandClicks(); }
      });
    });
  }

  function wireCandClicks() {
    $("cands").querySelectorAll(".lb-item").forEach((el) => {
      el.addEventListener("click", () => {
        const k = el.dataset.hero;
        if (!k) return;
        heroSel[k] = !heroSel[k];
        if (lastDraft) { renderCurve(lastDraft); $("cands").innerHTML = candsHtml(lastDraft); wireCandClicks(); }
      });
    });
  }

  function candsHtml(state) {
    if (state.frozen) {
      $("candRole").textContent = "";
      return '<div class="faint" style="font-size:12.5px;padding:10px 2px">对局进行中，推荐已暂停</div>';
    }
    $("candRole").textContent = state.myRoleCn ? "· " + state.myRoleCn : "";
    const list = state.candidates || [];
    return list.map((c, i) => {
      const d = (c.prob - state.winProb) * 100;
      const up = d >= 0;
      const picked = !!heroSel[c.champKey];
      return '<div class="lb-item' + (i === 0 ? " top" : "") + (picked ? " picked" : "") + '" data-hero="' + UI.esc(c.champKey) + '" style="--pick:' + colorOf(state, c.champKey) + '" title="' + (picked ? "已叠加曲线，点击取消" : "点击叠加该英雄的胜率曲线") + '">' +
        '<span class="lb-rank">' + (i + 1) + "</span>" +
        avatarHtml(c.champ, c.champId, "avatar-40") +
        '<span class="lb-main"><span class="lb-name">' + UI.esc(c.champ) + '</span><span class="lb-reason">' + UI.esc(c.reason || "样本不足") + "</span></span>" +
        '<span class="lb-score"><b class="' + (up ? "pos" : "neg") + '">' + (up ? "+" : "") + d.toFixed(1) + "pp</b><span>" + UI.fmtPct(c.prob) + "</span></span>" +
        "</div>";
    }).join("") ||
      '<div class="faint" style="font-size:12.5px;padding:10px 2px">' +
      (state.myRole ? "暂无推荐" : "位置未分配，先在客户端分配位置后再看推荐") + "</div>";
  }

  function factorsText(state) {
    const f = state.factors || {};
    const bits = [];
    const rs = state.rankStatus || {};
    if (f.myRank && f.theirRank) {
      bits.push("段位 " + UI.fmtInt(f.myRank) + " : " + UI.fmtInt(f.theirRank));
    } else if (state.rankError) {
      bits.push("段位 " + state.rankError);
    } else if (rs.state === "resolving") {
      bits.push("段位解析中");
    } else if (rs.state === "failed") {
      bits.push("段位解析失败");
    } else if (rs.state === "unconnected") {
      bits.push("段位未连接");
    }
    if (f.champEdge !== null && f.champEdge !== undefined) bits.push("版本 " + UI.signed(f.champEdge, 1) + "pp");
    if (f.laneEdge !== null && f.laneEdge !== undefined) bits.push("对位 " + UI.signed(f.laneEdge, 1) + "pp");
    return bits.join(" · ") || ("模型 AUC " + (state.modelAuc != null ? state.modelAuc : "—"));
  }

  /* ---------------------------------------------------------- 段位三态（T19 组件） */
  function rankKindOf(s, lcu) {
    const rk = s.rank;
    const st = s.rankStatus || null;
    const use = st || ((lcu && (lcu.lastError != null || lcu.lastNote != null || lcu.queries != null || lcu.connected === false)) ? lcu : null);
    if (rk) return "ok";
    if (use && use.state) return use.state;
    if (use && (use.connected === false || use.port == null)) return "unconnected";
    if (use && (use.lastError || use.lastNote || (use.lookupReasons && Object.keys(use.lookupReasons).length))) return "failed";
    if (use && (use.port || use.queries != null)) return "resolving";
    return "resolving";
  }

  function rankStateHtml(kind, st) {
    st = st || {};
    if (kind === "unconnected") {
      return '<div class="rank-state"><div class="t1">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v5M15 3v5M6 8h12a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><path d="M12 14v7"/></svg>' +
        "客户端未连接</div>" +
        '<div class="hint">请启动并登录英雄联盟客户端；连接后将自动发现并解析双方段位。</div></div>';
    }
    if (kind === "resolving") {
      return '<div class="rank-state"><div class="t1"><span class="spin-mini"></span>正在解析段位…</div>' +
        '<div class="hint">已连接客户端（端口 ' + UI.esc(st.port || "—") + "），正在读取双方段位数据，请稍候。</div></div>";
    }
    const lr = st.lookupReasons || {};
    const names = Object.keys(lr);
    let reason = st.lastError || st.lastNote || "";
    if (!reason && names.length) reason = names[0] + "：" + lr[names[0]];
    if (!reason) reason = "未知原因";
    const http = st.lastHttpStatus ? "（HTTP " + st.lastHttpStatus + "）" : "";
    let hint = "已尝试 " + (st.queries || 0) + " 次查询" + (st.errors ? "，失败 " + st.errors + " 次" : "") + "；恢复后会自动更新。";
    if (names.length && st.lastError) hint += " 另有：" + names[0] + "：" + lr[names[0]];
    return '<div class="rank-state fail"><div class="t1">' +
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 4 3 19h18L12 4z"/><path d="M12 10v4"/><circle cx="12" cy="16.8" r=".5" fill="currentColor"/></svg>' +
      "解析失败</div>" +
      '<div class="reason">' + UI.esc(reason) + " " + http + "</div>" +
      '<div class="hint">' + UI.esc(hint) + "</div></div>";
  }

  /* ---------------------------------------------------------- 选人板块 */
  function draftEmptyHtml(d) {
    const notStarted = !!(d && d.notStarted);
    const icon = notStarted
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v5M15 3v5M6 8h12a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><path d="M12 14v7"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/></svg>';
    const lcu = (d && d.lcu) || {};
    const hint = notStarted
      ? (d.detail || "选人助手服务未启动。")
      : (lcu.connected
        ? "LCU 已连接（端口 " + lcu.port + "）· 进入 BP 后自动刷新"
        : "LCU 未连接：请确认客户端已启动并登录");
    const actions = notStarted
      ? '<div class="empty-actions"><button type="button" class="btn btn-primary btn-sm" id="btnRetryDraft">重新检测</button></div>'
      : "";
    const slots = notStarted ? "" :
      '<div class="row row-8" style="margin-top:8px">' +
      ["上", "打", "中", "下", "辅"].map((c) => '<span class="avatar avatar-40 pending"><span class="init">' + c + "</span></span>").join("") +
      "</div>";
    return '<div class="empty" style="padding:36px 20px">' +
      '<div class="empty-icon">' + icon + "</div>" +
      '<div class="empty-title">' + UI.esc((d && d.message) || "等待进入选人阶段…") + "</div>" +
      '<div class="empty-hint">' + UI.esc(hint) + "</div>" + slots + actions + "</div>";
  }

  let lastDraft = null;
  function renderDraft(draft) {
    const draftMain = $("draftMain");
    const curveCard = $("curveCard");
    const candCard = $("candCard");
    const dot = $("dotDraft");
    if (!draft || !draft.ok) {
      lastDraft = null;
      draftMain.innerHTML = draftEmptyHtml(draft);
      curveCard.classList.add("hidden");
      candCard.classList.add("hidden");
      $("pillPhase").textContent = draft && draft.notStarted ? "未启动" : "等待选人";
      $("pillClock").textContent = "--";
      $("pillRole").textContent = "位置 —";
      $("draftSub").textContent = "";
      if (dot) { dot.className = "status-dot off"; }
      const btn = $("btnRetryDraft");
      if (btn) btn.addEventListener("click", () => (IS_DEMO ? resumeDemo() : (draftFail = 0, pollDraft())));
      return;
    }
    lastDraft = draft;
    curveCard.classList.remove("hidden");
    candCard.classList.remove("hidden");
    if (dot) { dot.className = "status-dot live"; }
    const frozen = !!draft.frozen;
    $("pillPhase").textContent = frozen ? (PHASE_CN[draft.phase] || "对局中") : (PHASE_CN[draft.phase] || draft.phase || "选人阶段");
    $("pillClock").textContent = frozen ? "—" : (draft.left > 0 ? Math.ceil(draft.left) + "s" : "--");
    $("pillRole").textContent = "位置 " + (draft.myRoleCn || "—");
    $("draftSub").textContent = frozen ? "对局中" : "";
    draftMain.innerHTML = bansHtml(draft) + boardHtml(draft) + oddsHtml(draft) + attributionHtml(draft);

    renderCurve(draft);
    $("curveNote").textContent = frozen ? "开局前最后阵容 · 敌方由实时接口补全" : ("阵容走势 · 0–40 分钟 · " + factorsText(draft));
    $("cands").innerHTML = candsHtml(draft);
    wireCandClicks();
    updateTime();
  }

  /* ---------------------------------------------------------- 主页板块 */
  function renderHome(home) {
    if (!home) return;
    const s = home.state || {};
    const lcu = home.lcu || {};
    const dot = $("dotLcu");
    if (dot) { dot.className = "status-dot " + (lcu.connected ? "live" : "off"); }
    $("pillLcu").className = "pill" + (lcu.connected ? " pill-green" : "");
    $("pillLcu").innerHTML = '<span class="status-dot' + (lcu.connected ? " live" : " off") + '"></span>' +
      (lcu.connected ? "LCU 已连接（端口 " + lcu.port + "）" : "LCU 未连接");
    $("pillMode").textContent = s.ok ? (SRC_CN[s.source] || s.source) + (s.model ? " · " + s.model : "") : "等待对局";
    /* 版本号：窄栏只显示短串（v2.3.12），完整串放 title/dataset 供版本比对，避免 76px 栏溢出 */
    const ver = home.version;
    if (ver) {
      const full = String(ver);
      const rail = $("railVersion");
      rail.dataset.version = full;
      rail.title = full;
      rail.textContent = full.split(/\s+/)[0];
    }

    /* 段位三态 */
    const rkKind = rankKindOf(s, lcu);
    const rkSt = s.rankStatus || lcu || {};
    $("rankPill").className = "pill " + (rkKind === "ok" ? "pill-green" : rkKind === "failed" ? "pill-red" : "pill-amber");
    $("rankPill").textContent = { ok: "已解析", unconnected: "未连接", resolving: "解析中", failed: "失败" }[rkKind] || rkKind;
    const rk = s.rank;
    const ownOnly = rk && rk.own != null && rk.team == null && rk.enemy == null;
    if (rkKind === "ok" && ownOnly) {
      /* 未开局：只有本人段位（/api/home 用 LCU 快照补的），不伪造双方均值 */
      const q = String(rkSt.queueType || "").indexOf("FLEX") >= 0 ? "灵活排位" : "单双排";
      const tierText = [
        rkSt.tierCn || rkSt.tier || "",
        rkSt.division || "",
        rkSt.lp != null ? rkSt.lp + " LP" : "",
      ].filter(Boolean).join(" ");
      $("rankKv").innerHTML =
        '<span class="k">' + q + '</span><span class="v">' + UI.esc(tierText || "—") + "</span>" +
        '<span class="k">段位分</span><span class="v">' + UI.fmtInt(rk.own) + "</span>" +
        '<span class="k">双方均值</span><span class="v faint">进入对局后显示</span>';
    } else if (rkKind === "ok" && rk) {
      $("rankKv").innerHTML =
        '<span class="k">我方均值</span><span class="v">' + UI.fmtInt(rk.team) + "</span>" +
        '<span class="k">敌方均值</span><span class="v">' + UI.fmtInt(rk.enemy) + "</span>" +
        '<span class="k">差值</span><span class="v ' + (rk.diff > 0 ? "pos" : rk.diff < 0 ? "neg" : "zero") + '">' + UI.signed(rk.diff) + "</span>" +
        '<span class="k">自己</span><span class="v">' + UI.fmtInt(rk.own) + "</span>";
    } else {
      $("rankKv").innerHTML = rankStateHtml(rkKind, rkSt);
    }

    /* 建议与事件（对局中显示） */
    const advice = s.advice || [];
    const feed = s.feed || [];
    if (s.ok) {
      $("adviceNote").textContent = s.goldSource ? "· 金币 " + (GOLD_CN[s.goldSource] || s.goldSource) : "";
      $("advice").innerHTML = advice.slice(0, 2).map((a) =>
        '<div class="advice-item ' + (a.source === "llm" ? "llm" : "rule") + '">' +
        '<span class="src">' + (a.source === "llm" ? "LLM" : "规则") + "</span>" +
        "<span>" + UI.esc(a.text) + "</span></div>"
      ).join("") || '<div class="faint" style="font-size:12.5px">暂无建议</div>';
      $("feed").innerHTML = feed.slice().reverse().slice(0, 3).map((ev) =>
        '<li class="feed-item ' + (ev.side || "none") + '">' +
        '<span class="feed-time">' + UI.mmss(ev.t) + '</span><span class="feed-dot"></span>' +
        '<span class="txt">' + UI.esc(ev.text) + "</span></li>"
      ).join("") || '<li class="feed-item"><span class="txt faint">暂无事件</span></li>';
    } else {
      $("adviceNote").textContent = "";
      $("advice").innerHTML = '<div class="faint" style="font-size:12.5px">进入对局后自动给出建议</div>';
      $("feed").innerHTML = '<li class="feed-item"><span class="txt faint">暂无事件</span></li>';
    }
  }

  function updateTime() {
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");
    const ss = String(now.getSeconds()).padStart(2, "0");
    $("pillUpdate").textContent = "更新 " + hh + ":" + mm + ":" + ss + (IS_DEMO ? " · 演示" : "");
  }

  function showLoading() {
    $("draftMain").innerHTML =
      '<div class="skeleton sk-line" style="width:120px;margin-bottom:12px"></div>' +
      [1, 2, 3, 4, 5].map(() =>
        '<div class="row" style="gap:14px;padding:6px 0">' +
        '<span class="skeleton sk-avatar"></span><span class="skeleton sk-line grow"></span>' +
        '<span class="skeleton sk-avatar"></span><span class="skeleton sk-line grow"></span></div>'
      ).join("") +
      '<div class="skeleton" style="height:9px;margin-top:12px"></div>';
    $("curveCard").classList.remove("hidden");
    $("candCard").classList.remove("hidden");
    $("curveLegend").innerHTML = "";
    $("curveSeries").innerHTML = "";
    $("junglerPanel").classList.add("hidden");
    $("cands").innerHTML = '<div class="skeleton sk-line" style="margin:8px 0"></div>'.repeat(3);
    $("rankKv").innerHTML = '<div class="skeleton sk-line" style="margin:8px 0"></div>'.repeat(3);
  }

  /* ---------------------------------------------------------- 主页视图（个人资料，T33） */
  let lastProfile = null;
  let lastAvatarKey = "";
  function relTime(iso) {
    if (!iso) return "";
    const d = new Date(iso);
    if (isNaN(d.getTime())) return "";
    const diff = Date.now() - d.getTime();
    if (diff < 0) return "刚刚";
    const mins = Math.floor(diff / 60000);
    if (mins < 1) return "刚刚";
    if (mins < 60) return mins + " 分钟前";
    const hrs = Math.floor(mins / 60);
    if (hrs < 24) return hrs + " 小时前";
    const days = Math.floor(hrs / 24);
    if (days < 7) return days + " 天前";
    return (d.getMonth() + 1) + "月" + d.getDate() + "日";
  }

  function rankCardHtml(r) {
    if (!r) return '<div class="rank-empty">暂无数据<span class="hint">需要客户端在线</span></div>';
    if (r.unranked) return '<div class="rank-empty">未定级<span class="hint">完成定级赛后显示段位</span></div>';
    const total = (r.wins || 0) + (r.losses || 0);
    const wr = total ? Math.round(((r.wins || 0) / total) * 1000) / 10 : 0;
    return '<div class="rank-body">' +
      '<div class="rank-tier">' + UI.esc(r.tierCn || r.tier || "—") +
      '<span class="div">' + UI.esc(r.division || "") + "</span>" +
      '<span class="pill pill-accent" style="margin-left:auto">' + (r.lp || 0) + " LP</span></div>" +
      '<div class="rank-meta"><span>胜 ' + (r.wins || 0) + "</span><span>负 " + (r.losses || 0) + "</span><span>胜率 " + wr + "%</span></div>" +
      "</div>";
  }

  function matchRowHtml(m) {
    const wl = m.win
      ? '<span class="pill pill-green match-pill">胜</span>'
      : '<span class="pill pill-red match-pill">负</span>';
    const kda = (m.kills != null && m.deaths != null && m.assists != null)
      ? m.kills + " / " + m.deaths + " / " + m.assists
      : "—";
    return '<div class="match-row">' +
      avatarHtml(m.champ, m.champId, "avatar-44") +
      '<span class="match-info"><span class="match-champ">' + UI.esc(m.champ || "—") + '</span><span class="match-time" style="display:block">' + UI.esc(relTime(m.playedAt)) + "</span></span>" +
      '<span class="match-kda">' + kda + "</span>" +
      '<span class="match-len">' + UI.mmss(m.gameTime) + "</span>" +
      wl + "</div>";
  }

  function renderProfile(p) {
    lastProfile = p;
    if (!p || !p.ok) {
      lastAvatarKey = "";
      $("profileAvatar").innerHTML = '<span class="init">?</span>';
      $("profileName").textContent = "—";
      $("profileLevel").textContent = "等级 —";
      $("profileLcu").className = "pill";
      $("profileLcu").innerHTML = '<span class="status-dot off"></span>客户端未连接';
      $("profileUpdate").textContent = "更新 —";
      $("rankSolo").innerHTML = rankCardHtml(null);
      $("rankFlex").innerHTML = rankCardHtml(null);
      $("matchesNote").textContent = "";
      $("matchList").innerHTML =
        '<div class="empty" style="padding:30px 16px">' +
        '<div class="empty-icon"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 3v5M15 3v5M6 8h12a1 1 0 0 1 1 1v4a1 1 0 0 1-1 1H6a1 1 0 0 1-1-1V9a1 1 0 0 1 1-1z"/><path d="M12 14v7"/></svg></div>' +
        '<div class="empty-title">' + UI.esc((p && p.message) || "客户端未连接") + "</div>" +
        '<div class="empty-hint">' + UI.esc((p && p.detail) || "请启动并登录英雄联盟客户端。") + "</div>" +
        "</div>";
      return;
    }
    const s = p.summoner || {};
    /* 头像 <img> 每 15s 重绘会重新拉取（no-store）；iconId/首字母没变就不重建 */
    const initial = (s.name || "?").trim().slice(0, 1) || "?";
    const avatarKey = (IS_DEMO ? "demo" : String(s.iconId || "")) + "|" + initial;
    if (avatarKey !== lastAvatarKey) {
      lastAvatarKey = avatarKey;
      const img = !IS_DEMO && s.iconId
        ? '<img src="/icon/profile/' + s.iconId + '.png" alt="" onerror="this.remove()">'
        : "";
      $("profileAvatar").innerHTML = '<span class="init">' + UI.esc(initial) + "</span>" + img;
    }
    $("profileName").textContent = s.name || "—";
    $("profileLevel").textContent = "等级 " + (s.level != null ? s.level : "—");
    $("profileLcu").className = "pill pill-green";
    $("profileLcu").innerHTML = '<span class="status-dot live"></span>LCU 已连接';
    const now = new Date();
    $("profileUpdate").textContent = "更新 " + String(now.getHours()).padStart(2, "0") + ":" + String(now.getMinutes()).padStart(2, "0") + (IS_DEMO ? " · 演示" : "");
    $("rankSolo").innerHTML = rankCardHtml(p.solo);
    $("rankFlex").innerHTML = rankCardHtml(p.flex);
    const matches = p.matches || [];
    $("matchesNote").textContent = matches.length ? "共 " + matches.length + " 局" : "";
    $("matchList").innerHTML = matches.map(matchRowHtml).join("") ||
      '<div class="faint" style="font-size:12.5px;padding:10px 2px">暂无对局记录</div>';
  }

  function showView(name) {
    $("viewDraft").classList.toggle("hidden", name !== "draft");
    $("viewProfile").classList.toggle("hidden", name !== "home");
    document.querySelectorAll(".rail-item[data-view]").forEach((b) => {
      b.classList.toggle("on", b.dataset.view === name);
    });
    document.title = "LoLAdvisor · " + (name === "home" ? "主页" : "选人助手");
  }

  /* ---------------------------------------------------------- 版本自刷新（T38） */
  function pageVersion() {
    /* T58：自身版本只认 URL 的 ?v= 或 rail 上缓存完整版本号的 dataset；
       不读 rail 的可见文本（现被截成短串且可能是占位符，误读会导致版本误判、假重启横幅）。 */
    if (window.__APPV) return String(window.__APPV);
    const v = params.get("v");
    if (v) {
      window.__APPV = v;
      return v;
    }
    const rail = $("railVersion");
    if (rail && rail.dataset && rail.dataset.version) return String(rail.dataset.version);
    return "";
  }

  let reloadCount = 0;
  try {
    reloadCount = parseInt(sessionStorage.getItem("app.reloadCount") || "0", 10) || 0;
  } catch (e) {
    reloadCount = 0;
  }

  function showVersionBar(srvVersion) {
    let bar = document.getElementById("versionBar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "versionBar";
      bar.setAttribute("role", "alert");
      bar.style.cssText =
        "position:fixed;left:50%;bottom:16px;transform:translateX(-50%);z-index:300;" +
        "display:flex;align-items:center;gap:12px;padding:10px 14px;border-radius:12px;" +
        "background:var(--accent-soft);border:1px solid rgba(180,83,9,.35);" +
        "color:var(--text);font-size:13px;box-shadow:var(--shadow-float);max-width:calc(100vw - 32px);";
      bar.innerHTML = '<span>检测到新版本，请重启应用以完成更新。</span>' +
        '<button type="button" class="btn btn-sm btn-primary">立即重载</button>';
      bar.querySelector("button").addEventListener("click", () => {
        try { location.reload(); } catch (e) {}
      });
      document.body.appendChild(bar);
    }
    const sp = bar.querySelector("span");
    if (sp) sp.textContent = "检测到新版本 " + UI.esc(srvVersion || "") + "，请重启应用以完成更新。";
  }

  function normVer(s) {
    return String(s || "").replace(/^v/i, "").trim();
  }

  function checkVersion(home) {
    /* 比对 /api/home.version 与页面标记；不一致先重载（≤2 次防抖），仍不一致提示重启 */
    if (IS_DEMO) return true;
    if (!home || !home.version) return true;
    const marker = pageVersion();
    if (!marker) return true;
    if (normVer(marker) === normVer(home.version)) return true;
    if (reloadCount < 2) {
      try { sessionStorage.setItem("app.reloadCount", String(reloadCount + 1)); } catch (e) {}
      try { location.reload(); } catch (e) {}
      return false;
    }
    showVersionBar(home.version);
    return false;
  }

  /* ---------------------------------------------------------- 打野雷达（T53） */
  const JUNGLER_STATE_CN = { unseen: "未见", spotted: "已现身", tracking: "追踪中" };
  let lastJungler = null;
  let junglerTimer = null;

  function renderJungler(j) {
    const panel = $("junglerPanel");
    if (!j || !j.ok) {
      panel.classList.add("hidden");
      return;
    }
    panel.classList.remove("hidden");
    const st = j.state || "unseen";
    const pill = $("jpStatePill");
    pill.textContent = JUNGLER_STATE_CN[st] || st;
    pill.className = "pill " + (st === "tracking" ? "pill-green" : st === "spotted" ? "pill-amber" : "");
    /* 采集缩略图 */
    const cap = j.capture || {};
    const frame = $("jpFrame");
    const note = $("jpNote");
    if (!IS_DEMO && cap.available && cap.url) {
      frame.hidden = false;
      frame.src = cap.url + (cap.url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
      note.textContent = "采集 " + (cap.fps || 1) + " fps";
      if (!junglerTimer) {
        junglerTimer = setInterval(() => {
          const j2 = lastJungler;
          if (j2 && j2.capture && j2.capture.available && j2.capture.url) {
            frame.src = j2.capture.url + (j2.capture.url.indexOf("?") >= 0 ? "&" : "?") + "t=" + Date.now();
          }
        }, 700);
      }
    } else {
      frame.hidden = true;
      frame.removeAttribute("src");
      note.textContent = IS_DEMO ? "采集未启动（演示）" : "采集未启动";
    }
    /* 敌方打野卡（T58：显示优先 *Cn 中文，缺失回退机器键；头像仍用机器键 champ 取图） */
    const jg = j.jungler || {};
    const ls = j.lastSeen;
    const pd = j.predicted;
    const champName = jg.champCn || jg.champ || "敌方打野";
    /* 副标题：机器键回退时 name 是中文英雄名（会与标题重复），重复则不显示 */
    const subName = jg.name && jg.name !== champName ? jg.name : "";
    let html = '<div class="top">' +
      avatarHtml(jg.champ, jg.champId, "avatar-40") +
      '<span class="who"><span class="name">' + UI.esc(champName) + "</span>" +
      (subName ? '<span class="sub" style="display:block">' + UI.esc(subName) + "</span>" : "") + "</span>";
    const laneCn = j.threatLaneCn || j.threatLane;
    if (laneCn) html += '<span class="pill pill-red">警惕' + UI.esc(laneCn) + "</span>";
    html += "</div>";
    const lsText = ls
      ? "上次现身 " + UI.esc(ls.zoneCn || ls.zone || "—") + " · " + (ls.ago != null ? ls.ago + " 秒前" : "—")
      : "尚未现身";
    if (pd) {
      html += '<div class="line"><span>' + lsText + "</span></div>";
      html += '<div class="line"><span>预测 <b>' + UI.esc(pd.zoneCn || pd.zone || "—") + "</b>" +
        (pd.eta != null ? " · 预计 " + pd.eta + " 秒后" : "") + "</span></div>";
      html += '<div class="conf"><span>置信</span><div class="progress"><div class="progress-fill accent" style="--p:' + UI.clamp(pd.confidence || 0, 0, 1) + '"></div></div><span>' + Math.round((pd.confidence || 0) * 100) + "%</span></div>";
    } else {
      html += '<div class="line"><span>' + lsText + "</span></div>";
      html += '<div class="line"><span class="faint">等待更多视野信息…</span></div>';
    }
    $("jpJungler").innerHTML = html;
    /* 开野路线时间轴 */
    const route = j.route || [];
    $("jpRoute").innerHTML = route.length
      ? route.map((c) => {
          const cls = c.current ? " current" : c.done ? " done" : "";
          return '<div class="jp-node' + cls + '"><span class="t">' + UI.mmss(c.time) + '</span><span class="dot"></span><span class="c">' + UI.esc(c.campCn || c.camp || "") + "</span></div>";
        }).join("")
      : "";
  }

  /* ---------------------------------------------------------- 数据源 */
  let draftFail = 0;
  async function pollHome() {
    if (typeof bridge.getHome === "function") {
      try {
        const h = await bridge.getHome();
        if (checkVersion(h)) renderHome(h);
        return;
      } catch (e) {}
    }
    try {
      const res = await fetch("/api/home", { cache: "no-store" });
      if (res.ok) {
        const h = await res.json();
        if (checkVersion(h)) renderHome(h);
      }
    } catch (e) { /* 下轮再试 */ }
  }

  async function pollDraft() {
    try {
      const res = await fetch("/draft/state", { cache: "no-store" });
      if (res.ok) {
        draftFail = 0;
        const d = await res.json();
        if (d && d.v && window.__htmlv && d.v !== window.__htmlv) { location.reload(); return; }
        if (d && d.v) window.__htmlv = d.v;
        renderDraft(d);
      } else {
        draftFail++;
        if (draftFail >= 2) renderDraft(DEMO.draft.notStarted);
      }
    } catch (e) {
      draftFail++;
      if (draftFail >= 2) renderDraft(DEMO.draft.notStarted);
    }
  }

  function connectDraftSse() {
    try {
      const es = new EventSource("/draft/events");
      es.onmessage = (e) => {
        try {
          const d = JSON.parse(e.data);
          draftFail = 0;
          renderDraft(d);
        } catch (err) {}
      };
      es.onerror = () => {
        try { es.close(); } catch (e) {}
        setTimeout(pollDraft, 2000);
      };
    } catch (e) {
      setTimeout(pollDraft, 2000);
    }
  }

  let profileFail = 0;
  async function pollProfile() {
    if (typeof bridge.getProfile === "function") {
      try { renderProfile(await bridge.getProfile()); return; } catch (e) {}
    }
    try {
      const res = await fetch("/api/profile", { cache: "no-store" });
      if (res.ok) {
        profileFail = 0;
        renderProfile(await res.json());
      } else {
        profileFail++;
        if (profileFail >= 2) renderProfile(DEMO.profile.unconnected);
      }
    } catch (e) {
      profileFail++;
      if (profileFail >= 2) renderProfile(DEMO.profile.unconnected);
    }
  }

  let junglerFail = 0;
  async function pollJungler() {
    if (typeof bridge.getJungler === "function") {
      try {
        const j = await bridge.getJungler();
        lastJungler = j;
        renderJungler(j);
        return;
      } catch (e) {}
    }
    try {
      const res = await fetch("/api/jungler", { cache: "no-store" });
      if (res.ok) {
        junglerFail = 0;
        const j = await res.json();
        lastJungler = j;
        renderJungler(j);
      } else {
        junglerFail++;
        if (junglerFail >= 2) { lastJungler = null; renderJungler(null); }
      }
    } catch (e) {
      junglerFail++;
      if (junglerFail >= 2) { lastJungler = null; renderJungler(null); }
    }
  }

  /* ---------------------------------------------------------- 演示模式 */
  const JUNGLER = params.get("jungler");
  let demoStep = 5;
  let demoTimer = null;

  function demoHome() {
    const h = DEMO.home.default();
    if (RANK && DEMO.rankStates[RANK]) h.state = DEMO.applyRankState(h.state, RANK);
    return h;
  }

  function demoJungler() {
    return DEMO.jungler[JUNGLER] || DEMO.jungler.tracking;
  }

  function applyDemo() {
    renderDraft(DEMO.draft.state(demoStep));
    renderHome(demoHome());
    renderJungler(demoJungler());
  }

  function resumeDemo() {
    if (demoTimer) clearInterval(demoTimer);
    applyDemo();
    demoTimer = setInterval(() => {
      demoStep = (demoStep + 1) % DEMO.draft.steps.length;
      applyDemo();
    }, 10000);
  }

  /* ---------------------------------------------------------- 启动（自愈，T39） */
  function escBanner(s) {
    return String(s == null ? "" : s).replace(/[<>&"']/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function showBootErrorBar(msg, code) {
    let bar = document.getElementById("bootFailBar");
    if (!bar) {
      bar = document.createElement("div");
      bar.id = "bootFailBar";
      bar.setAttribute("role", "alert");
      document.body.appendChild(bar);
    }
    bar.style.cssText =
      "position:fixed;top:0;left:0;right:0;z-index:400;display:flex;align-items:center;gap:12px;" +
      "padding:10px 16px;background:#ffebeb;border-bottom:1px solid rgba(215,0,21,.4);" +
      "color:#a30012;font-size:13px;line-height:1.5;" +
      "font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;";
    bar.innerHTML = '<span style="flex:1">页面初始化异常：' + escBanner(msg) + "（" + escBanner(code) + "）</span>" +
      '<button type="button" style="flex:none;padding:5px 12px;border:0;border-radius:8px;background:#d70015;color:#fff;font-size:12px;cursor:pointer;font-family:inherit">重试</button>';
    const btn = bar.querySelector("button");
    if (btn) btn.addEventListener("click", () => { try { location.reload(); } catch (e) {} });
  }

  let bootError = null;
  try {
    const PROFILE_DEMO = params.get("profile") === "offline" ? DEMO.profile.unconnected : DEMO.profile.data();
    /* 窄栏版本号：起始占位 v—，有 ?v= 时立刻用自身版本短串填充（完整串留给 /api/home） */
    if (window.__APPV) {
      const rail = $("railVersion");
      rail.dataset.version = String(window.__APPV);
      rail.title = String(window.__APPV);
      rail.textContent = String(window.__APPV).split(/\s+/)[0];
    }
    $("btnNavHome").addEventListener("click", () => {
      showView("home");
      renderProfile(lastProfile || PROFILE_DEMO);
    });
    $("btnNavDraft").addEventListener("click", () => showView("draft"));
    $("btnSettings").addEventListener("click", () => {
      const pop = $("settingsPop");
      pop.classList.toggle("hidden");
      if (!pop.classList.contains("hidden")) {
        syncThemeUi();
        if (typeof bridge.openSettings === "function") { try { bridge.openSettings(); } catch (e) {} }
      }
    });
    $("btnCloseSettings").addEventListener("click", () => $("settingsPop").classList.add("hidden"));
    $("swTheme").addEventListener("click", () => setTheme(!themeIsDark()));
    $("btnSelftest").addEventListener("click", () => {
      if (typeof bridge.runSelftest === "function") { try { bridge.runSelftest(); return; } catch (e) {} }
      UI.toast(IS_DEMO ? "演示模式：接入桌面版后将运行自检" : "自检将在桌面版接入后提供");
    });

    if (params.get("panel") === "1") {
      $("settingsPop").classList.remove("hidden");
      syncThemeUi();
    }

    if (IS_DEMO) {
      const initial = params.get("state") || "default";
      if (!params.get("nopanel")) {
        UI.attachDemoPanel({
          default: resumeDemo,
          loading: () => { if (demoTimer) clearInterval(demoTimer); showLoading(); },
          empty: () => { if (demoTimer) clearInterval(demoTimer); renderDraft(DEMO.draft.waiting); renderHome(demoHome()); renderJungler(null); },
          error: () => { if (demoTimer) clearInterval(demoTimer); renderDraft(DEMO.draft.notStarted); renderHome(demoHome()); renderJungler(null); },
        }, "一体化页");
      }
      if (initial === "loading") showLoading();
      else if (initial === "empty") { renderDraft(DEMO.draft.waiting); renderHome(demoHome()); renderJungler(null); }
      else if (initial === "error") { renderDraft(DEMO.draft.notStarted); renderHome(demoHome()); renderJungler(null); }
      else resumeDemo();
      renderProfile(PROFILE_DEMO);
      if (params.get("view") === "home") showView("home");
    } else {
      showLoading();
      pollHome();
      pollDraft();
      pollProfile();
      pollJungler();
      connectDraftSse();
      setInterval(pollHome, 2000);
      setInterval(pollDraft, 4000);
      setInterval(pollProfile, 15000);
      setInterval(pollJungler, 2000);
      setInterval(updateTime, 30000);
    }
  } catch (e) {
    bootError = e;
    try { console.error("[LoLAdvisor] 启动失败:", e); } catch (err) {}
    showBootErrorBar(e && e.message ? e.message : String(e), "boot");
  }
  window.__LOL_APP_BOOTED__ = bootError ? "error:boot" : true;
  if (bootError) {
    document.documentElement.dataset.booted = "error";
  } else {
    document.documentElement.dataset.booted = "1";
    /* T45：启动成功——清除可能存在的失败横幅，并重置看门狗自愈计数（每次窗口恢复自愈机会） */
    const staleBar = document.getElementById("bootFailBar");
    if (staleBar && staleBar.parentNode) staleBar.parentNode.removeChild(staleBar);
    try { sessionStorage.removeItem("app.bootWatchdog"); } catch (e) {}
    try { sessionStorage.removeItem("app.bootDiagnosis"); } catch (e) {}
  }
})();
