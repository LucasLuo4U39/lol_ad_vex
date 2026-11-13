/* =====================================================================
   LoLAdvisor 单窗口外壳（shell.html 专属逻辑）
   左侧导航 + 三视图（主页 / 选人助手 / 仪表盘），后两者 iframe 内嵌。
   配置注入约定见 ui/next/contracts.md 第 6 节（window.LOL_SHELL / URL 参数）。
   ===================================================================== */
(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  const IS_DEMO = params.get("demo") === "1";
  const CFG = window.LOL_SHELL || {};
  const VIEWS = ["home", "draft", "dashboard"];
  const META = {
    home: { label: "主页", file: "home.html", svc: null, port: null },
    draft: { label: "选人助手", file: "champ_select.html", svc: "draft", port: 8788 },
    dashboard: { label: "仪表盘", file: "dashboard.html", svc: "dashboard", port: 8765 },
  };
  const $ = (id) => document.getElementById(id);

  const urls = {
    home: params.get("homeUrl") || (CFG.home && CFG.home.url) || "home.html",
    draft: params.get("draftUrl") || (CFG.draft && CFG.draft.url) || "http://127.0.0.1:8788",
    dashboard: params.get("dashboardUrl") || (CFG.dashboard && CFG.dashboard.url) || "http://127.0.0.1:8765",
  };

  function svcRunning(view) {
    if (view === "home") return true;
    if (params.get("offline") === "1") return false; // 自检参数：模拟未启动（优先）
    if (IS_DEMO) return true; // 演示：iframe 指向本地演示页，总是可用
    const c = CFG[META[view].svc] || {};
    return c.running !== false;
  }

  function frameSrc(view) {
    if (IS_DEMO) return META[view].file + "?demo=1";
    return urls[view];
  }

  function attach(view) {
    const running = svcRunning(view);
    const frame = $(`frame-${view}`);
    const loading = $(`loading-${view}`);
    const offline = $(`offline-${view}`);
    if (!running) {
      if (frame) frame.classList.add("hidden");
      if (loading) loading.classList.add("hidden");
      if (offline) offline.classList.remove("hidden");
      updateFoot();
      return;
    }
    if (offline) offline.classList.add("hidden");
    if (!frame.dataset.loaded) {
      frame.dataset.loaded = "1";
      frame.src = frameSrc(view);
      frame.addEventListener("load", () => {
        if (loading) loading.classList.add("hidden");
      });
    }
    updateFoot();
  }

  function retry(view) {
    const frame = $(`frame-${view}`);
    const loading = $(`loading-${view}`);
    const offline = $(`offline-${view}`);
    if (offline) offline.classList.add("hidden");
    if (loading) loading.classList.remove("hidden");
    if (frame) {
      frame.classList.remove("hidden");
      frame.dataset.loaded = "";
      frame.src = frameSrc(view);
    }
  }

  function showView(name) {
    VIEWS.forEach((v) => $(`view-${v}`).classList.toggle("on", v === name));
    document.querySelectorAll(".nav-item").forEach((b) => {
      const on = b.dataset.view === name;
      b.classList.toggle("on", on);
      if (on) b.setAttribute("aria-current", "page");
      else b.removeAttribute("aria-current");
    });
    document.title = "LoLAdvisor · " + META[name].label;
  }

  function updateFoot() {
    const draft = $(`footDraft`);
    const dash = $(`footDash`);
    const dot = (on) => `<span class="status-dot${on ? " live" : " off"}"></span>`;
    if (draft) {
      const on = svcRunning("draft");
      draft.innerHTML = dot(on) + "<span>选人助手 " + (on ? "已启动" : "未启动") + "</span>";
    }
    if (dash) {
      const on = svcRunning("dashboard");
      dash.innerHTML = dot(on) + "<span>仪表盘 " + (on ? "已启动" : "未启动") + "</span>";
    }
    const ver = $(`footVersion`);
    if (ver && CFG.version) ver.textContent = "LoLAdvisor " + CFG.version;
  }

  /* ---------------------------------------------------------------- 启动 */
  document.querySelectorAll(".nav-item").forEach((b) => {
    b.addEventListener("click", () => {
      showView(b.dataset.view);
      attach(b.dataset.view);
    });
  });
  const btnRetryDraft = $(`btnRetryDraft`);
  if (btnRetryDraft) btnRetryDraft.addEventListener("click", () => retry("draft"));
  const btnRetryDash = $(`btnRetryDash`);
  if (btnRetryDash) btnRetryDash.addEventListener("click", () => retry("dashboard"));

  const initial = params.get("view");
  const defaultView = CFG.defaultView || "home";
  showView(VIEWS.includes(initial) ? initial : VIEWS.includes(defaultView) ? defaultView : "home");
  VIEWS.forEach(attach);
  updateFoot();
})();
