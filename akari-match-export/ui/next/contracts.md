# LoLAdvisor 新界面 · 接口契约与字段对照（供 ui-dev 接入）

> ui-designer（Phase A）交付。三个新页面位于 `ui/next/`，共用 `ui/design-system.css` 与
> `ui/next/demo-data.js`。本文件说明：①home 页期望的最小接口（需 ui-dev 新实现）；
> ②选人助手 / 仪表盘与现有服务端接口的字段对照（以服务端代码为准，未做任何改动）。

---

## 1. 文件与资源约定

| 文件 | 说明 |
|---|---|
| `ui/design-system.css` | 唯一样式源（tokens + 组件），三页通过 `../design-system.css` 引用 |
| `ui/next/demo-data.js` | 演示模式共用样例数据 + 共享 UI 工具（`window.LOL_DEMO`），三页通过 `demo-data.js` 引用 |
| `ui/next/home.html` | 桌面主窗口页（Phase B 由 desktop_app.py 渲染） |
| `ui/next/champ_select.html` | 选人助手页（替换 `assets/champ_select.html`） |
| `ui/next/dashboard.html` | Web 仪表盘页（替换 `advisor_dashboard.html`） |

**接入时建议**：把 `ui/` 整个目录复制进打包资源（如 `assets/ui/`），页面由服务器按原路径
serve（`/` → `assets/ui/next/champ_select.html` 等），相对引用即保持有效。零外部资源：
无 CDN、无网络字体、无外链图片；图标全部为内联 SVG，英雄头像在无图标时自动降级为
「首字 + 渐变色块」头像（不产生 404）。

**演示模式**：任意页面加 `?demo=1` 即纯静态渲染完整样例数据（不发起任何网络请求）。
附加参数 `&state=loading|empty|error|default` 可让页面直接以指定状态启动，用于无头截图。
右下角会出现「演示模式」面板，可手动切换四种状态。真实接入后 `?demo=1` 不应出现在入口 URL。

---

## 2. 选人助手页（champ_select.html）— 现有接口，无需改动

页面完全兼容 `champ_select_server.py` 现有接口：

| 接口 | 方法 | 用途 |
|---|---|---|
| `/` | GET | 页面（需改为 serve 新页面文件） |
| `/api/state` | GET | 状态轮询（页面 4s 兜底轮询） |
| `/api/events` | GET（SSE） | 实时推送（断线自动重连，2s 重试） |
| `/icon/<champId>.png` | GET | 英雄头像（LCU 代理；404 时页面自动降级为首字头像） |

`/api/state` 字段全集（以 `_assemble()` 为准，字段名原样使用）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | `false` 为等待态（只有 `message`/`lcu`） |
| `source` | string | `live` / `demo` / `in_game`（冻结时） |
| `phase` | string | `BAN_PICK` / `PLANNING` / `FINALIZATION` / `GAME_STARTING` / `IN_GAME` / `END_OF_GAME` / `LAST_DRAFT` |
| `left` | number | 本阶段剩余秒数（冻结时为 0） |
| `myRole` / `myRoleCn` | string | 我的位置（`MIDDLE` / `中单`） |
| `myTeam` / `theirTeam` | array | 每项：`{role, roleCn, champ, champKey, champId, locked, rank, name, isMe}` |
| `bans` | object | `{mine:[{id,name}], theirs:[{id,name}]}` |
| `winProb` | number | 我方胜率 0–1 |
| `factors` | object | `{myRank, theirRank, champEdge, laneEdge, picked, enemyPicked}` |
| `history` | array | `[{t:剩余秒, p:胜率}]`，最多 40 点 |
| `candidates` | array | 每项：`{key, champ, champKey, champId, prob, opgg, cohort, cohortGames, matchup, personal, score, reason}` |
| `modelAuc` | number | 草稿模型 AUC |
| `lcu` | object | `{connected: bool, port: int|null}` |
| `rankStatus` | object（T18 新增） | 段位诊断三态，见下方「段位三态」 |
| `rankError` | string（T18 新增） | 单行展示用：`lastError` 或 `lastNote` 或「LCU 未连接」 |
| `v` | int | 页面文件 mtime，用于「服务端页面与脚本版本不一致时自动刷新」 |
| `frozen` | bool（可选） | 进游戏后冻结最后阵容；`source` 变 `in_game` |

页面保留的行为：SSE + 轮询双通道、`v` 版本检查自动 reload、后台静音音频防休眠
（仅在非 demo 模式注册，`pointerdown` 一次性触发）、等待态提示文案、冻结态「推荐已暂停」。

---

## 3. Web 仪表盘（dashboard.html）— 现有接口，无需改动

页面完全兼容 `advisor_server.py` 的 `/api/state`（`State.as_json()` 为准）：

| 字段 | 类型 | 说明 |
|---|---|---|
| `ok` | bool | `false` = 等待态 |
| `source` | string | `live` / `replay` / `spectator` |
| `model` | string | `live_model3` / `live_model5` |
| `rank` | object\|null | `{own, team, enemy, diff, vsTeam}`；解析成功时非空 |
| `rankStatus` | object（T19 新增） | `rank_lookup.status()` 诊断快照，见下方「段位三态」 |
| `attribution` | array（T48 新增） | `[{key, nameCn, delta}]`，胜率归因（正值利好我方）；`delta` 为**概率差**（0.021 = +2.1pp），前端 ×100 后按 `pp` 显示；缺失时页面隐藏归因行 |
| `waiting` / `ended` | bool | 等待 / 回放结束 |
| `clock` | string | `"14:35"` |
| `gameTime` | number | 对局秒 |
| `winProb` | number\|null | 我方胜率 0–1 |
| `ownTeam` | string | `ORDER` / `CHAOS` |
| `selfName` / `selfChampion` | string | 视角玩家 |
| `goldSource` | string | `live` / `sgp` / `estimate` |
| `goldEstimated` | bool | 金币是否估算 |
| `own` | object | `{level, kills, deaths, assists, cs, gold}` |
| `teamDiff` | object | `{kills, deaths, assists, cs, level, dragons, barons, heralds, grubs, atakhan, towers, inhibitors}` |
| `events` | object | `{own_kill_30s, enemy_kill_30s, own_gank_60s, enemy_gank_60s}` |
| `advice` | array | `[{source:"rule"|"llm", text}]` |
| `feed` | array | `[{t:对局秒, text, side:"own"|"enemy"|null}]` |
| `history` | object | `{t, winProb, killDiff, csDiff, levelSumDiff, ownKill30, enemyKill30, ownGank60, enemyGank60}`，等长数组 |
| `llm` | object | `{enabled, lastAt, error, tips}` |

> ⚠ 注意：`teamDiff.grubs` 为「巢虫」（历史 bug `grub` 已修），新页面按 `grubs` 读取；
> 无 `grubs` 字段的旧快照回放会显示 0，不会报错。

**段位三态（T18/T19，字段名以 `rank_lookup.status()` 为准）**：

三处透传已定型：

1. **champ_select `/api/state`（T18 已实现）**：`rankStatus` + `rankError`。
   `rankError` = `lastError` 或 `lastNote` 或「LCU 未连接」，供单行展示；
   页面在胜率曲线脚注的「段位 …」片段使用（过长自动省略号，完整文案在 title）。
2. **desktop `/api/home`（T18 已实现）**：`lcu` 对象直接合并 `resolver.status()`
   全字段（`connected/port` 之外带 `lastError/lastHttpStatus/lastNote/queries/
   errors/unranked/lookupReasons/source`，无 `state`）。home 页在
   `state.rankStatus` 缺失时用 `lcu` 字段降级推导三态。
3. **advisor_server `/api/state`（as_json 的 rankStatus 待 T17/lead 授权后补）**：
   home/dashboard 的 `state.rankStatus` 届时自动生效；当前前端降级为「解析中」。

```jsonc
"rankStatus": {
  "state": "ok" | "unconnected" | "resolving" | "failed",
  "port": 48620,                    // null = 未发现客户端
  "source": "lockfile",             // lockfile | psutil | rediscovered
  "lastError": "LCU HTTP 503（/lol-ranked/v1/current-ranked-stats）", // 连接/HTTP 错误
  "lastHttpStatus": 503,            // 最近一次 HTTP 状态码
  "lastNote": null,                 // 查无此人 / 未定级 等提示
  "queries": 9, "errors": 3, "unranked": 0,
  "lookupReasons": { "蓝方下路#1003": "查不到召唤师（名字不匹配或非本区）" }
}
```

`state` 判定（ui-dev T18 实现口径）：`lastError`/`lastNote` 非空 → `failed`；
`rank_cache` 有值或 `queries>0` → `ok`；其余 → `resolving`；无 resolver → `unconnected`。

T58（v2.3.13）：未开局时 `/api/home` 会用 LCU 段位快照补 `rank = { "own": <段位分> }`
（只填本人，不伪造 team/enemy），并在 `rankStatus` 里带 `tier/tierCn/division/lp/queueType`、
`source="lcu-ranked"`。前端据此渲染「单双排/灵活排位 + 段位 + 段位分 + 双方均值（进入对局后显示）」，
而不是把 three 个空值渲染成「—」。LCU 的 `NONE/UNRANKED` 一律归一为未定级。

前端展示优先级：`rank` 非空 → `ok`（数值 + 绿「已解析」徽章）；否则按
`rankStatus.state`（home 还可用 `lcu` 诊断字段推导）显示三态卡：①客户端未连接
（启动引导文案）②正在解析段位…（琥珀徽章）③解析失败:<原因>（红徽章，原因取
`lastError`/`lastNote`/`lookupReasons` 首条，附 `lastHttpStatus`）。`rankStatus`
完全缺失的旧接口降级为「解析中」（不臆断未连接）。
演示：`?demo=1&rank=unconnected|resolving|failed`（三页均支持）；截图
`ui/next/previews/rank-{unconnected,resolving,failed}.png`。

---

## 4. 主窗口（home.html）— 期望新增的最小接口契约

home 页是全新页面，需要 ui-dev 在 Phase B 给 desktop_app.py（或内嵌服务）新增一个
进程内最小 HTTP 接口（建议 `GET /api/home`，端口可复用现有桌面版端口或选人助手端口）。

```jsonc
{
  "ok": true,                                  // false 时显示错误态
  "appTitle": "LoLAdvisor",
  "version": "v2.3.0",                         // APP_VERSION
  "lcu": {                                     // T18：合并 resolver.status() 全字段
    "connected": true, "port": 48620,
    "source": "lockfile", "lastError": null, "lastHttpStatus": null,
    "lastNote": null, "queries": 12, "errors": 0, "unranked": 0,
    "lookupReasons": {}
  },
  "draft": {                                   // 选人助手入口状态（open_draft_window 相关）
    "available": true,
    "running": true,                           // _DRAFT_SERVER 是否存活
    "url": "http://127.0.0.1:8788"
  },
  "dashboard": {                               // Web 仪表盘入口状态
    "available": true,
    "running": false,                          // advisor_server 是否另行启动（桌面版默认不启动）
    "url": "http://127.0.0.1:8765"
  },
  "settings": {                                // 对应 desktop_app.py CLI 参数
    "interval": 5.0,                           // --interval
    "llm": false,                              // --no-llm 取反
    "llmInterval": 20.0,                       // --llm-interval
    "ranks": true,                             // --no-ranks 取反
    "autoOpenDraft": false,                    // 新建议：进 BP 自动弹选人助手（可留 false）
    "rankCache": "C:\\...\\rank_cache.json"    // --rank-cache
  },
  "selftest": {                                // 来自 run_selftest() 的输出（缓存最近一次）
    "passed": true,
    "at": "2026-11-12 09:58",
    "lines": ["live_model3: 1200 棵树 · 34 特征", "…", "SELFTEST OK"]
  },
  "diag": {                                    // 来自 run_diag() 的摘要（可选）
    "at": "2026-11-12 09:57",
    "processes": 412,
    "league": 4,
    "lcuPort": 48620
  },
  "state": { /* 直接内嵌 advisor_server.State.as_json() 的完整形状（见第 3 节） */ }
}
```

**WebView2 桥（可选但推荐）**：home 页优先探测 `window.lolAdvisor` 对象，
存在时用桥调用代替 `window.open`，从而在桌面窗口里直接打开选人助手/仪表盘：

```js
window.lolAdvisor = {
  getHome:   async () => homeData,        // 返回上面的 /api/home 形状
  openDraft: () => { /* open_draft_window() */ },
  openDashboard: () => { /* 打开 http://127.0.0.1:8765 */ },
  runSelftest: () => { /* 后台跑 run_selftest()，下轮 getHome 带新 selftest */ },
  openDiag: () => { /* 打开 diag.log */ },
  setSetting: (key, value) => { /* 写配置，下轮 getHome 生效 */ }
};
```

无桥时页面自动退回：`fetch("/api/home")` 轮询 + `window.open(url)` 打开入口。
（ui-dev 只需实现其一即可；两者都实现则桥优先。）

---

## 5. 给 ui-dev 的接入注意事项

1. **serve 路径**：`champ_select_server.py` 的 `HTML` 常量与 `advisor_server.py` 的
   `_serve_dashboard()` 需改为指向新页面；打包时把 `ui/` 整个目录纳入
   `prepare_assets.py` 与 `.spec`（`design-system.css`、`demo-data.js` 必须与 HTML 同层级相对路径）。
2. **desktop_app.py 改造**：tkinter 主窗口改为 Web 渲染（WebView2 或 Edge app 模式加载
   `ui/next/home.html`），进程内新增 `/api/home`（第 4 节契约）或注入 `window.lolAdvisor` 桥；
   保留 `--diag` / `--selftest` / `--demo` 等 CLI 行为不变。
3. **`?demo=1` 不要出现在生产入口**：页面只在带参时进入演示；截图自检用
   `file:///.../ui/next/<page>.html?demo=1&state=<default|loading|empty|error>`。
4. **英雄头像**：demo 模式不发任何图标请求；live 模式请求 `/icon/<id>.png`，
   失败自动降级首字头像（与现有行为一致）。
5. **版本自刷新**：champ_select 页保留 `state.v`（HTML mtime）不一致时 `location.reload()`
   的逻辑；接入后请确认 `v` 仍随新页面文件 mtime 更新。
6. **零 console 报错红线**：页面不依赖任何外部资源；接入时请勿引入 CDN 引用。
7. **响应式红线**：800–1920px 宽度下三页布局均可用（已按 1180×760 主窗口调优；
   800px 断点切换单列布局）。改动样式请只改 `ui/design-system.css`。

---

## 6. 单窗口外壳（shell.html）— 配置注入契约（T14 新增）

桌面版 v2.3.1 起主窗口加载 `ui/next/shell.html`：左侧导航（主页 / 选人助手 / 仪表盘）+
内容区。「主页」视图内嵌 home.html；「选人助手」内嵌 champ_select 服务；「仪表盘」内嵌
advisor_server 服务（均为 iframe）。外壳与 `ui/next/shell.js` 只引用 `../design-system.css`，
暖色设计系统与动效约束不变。

**配置注入（二选一，两者同时存在时 URL 参数优先）**：

1. `window.LOL_SHELL`（推荐，WebView2 可在 document 创建前注入）：

```js
window.LOL_SHELL = {
  version: "v2.3.1",
  defaultView: "home",                 // "home" | "draft" | "dashboard"
  home:      { url: "home.html" },     // 相对路径或完整 URL
  draft:     { url: "http://127.0.0.1:8788", running: true  },  // running=false → 「未启动」空态
  dashboard: { url: "http://127.0.0.1:8765", running: false },
};
```

2. URL 参数：`?homeUrl=…&draftUrl=…&dashboardUrl=…&view=…`；另支持
   `?demo=1`（三个 iframe 指向同目录本地演示页，静态自检）与 `?offline=1`（强制显示
   未启动空态，配合 `&view=draft|dashboard` 截图用）。

**行为约定**：

- `running === false` 时对应视图显示「未启动」空态（含启动引导文案 + 重新检测按钮），
  不渲染 iframe；`home` 视图始终内嵌（本地页面恒可用）。
- 演示模式不发起任何网络请求：iframe src 固定为 `home.html?demo=1` /
  `champ_select.html?demo=1` / `dashboard.html?demo=1`。
- 未启动空态的「重新检测」会清掉 iframe 缓存重新加载对应 URL。

**ui-dev 接线建议**：desktop_app 用 WebView2 `AddScriptToExecuteOnDocumentCreated`
注入 `window.LOL_SHELL`（`running` 由 `_DRAFT_SERVER.is_alive()` 与端口探测实时刷新，
或首次注入 + shell 轮询 `/api/home` 更新）；也可以由进程内 HTTP 服务渲染 shell 时在
`<head>` 注入同名脚本。打开选人助手/仪表盘的按钮改为切换 shell 视图（`?view=draft` 或
桥调用），不再 `window.open` 新窗口。

---

## 7. 一体化单页（app.html）— 数据契约与代理约定（T22 新增）

桌面版 v2.3.3 起主窗口加载 `ui/next/app.html`（**取代 shell 三视图方案**：左侧窄栏 +
主区多栏，选人助手直接作为页面板块，不套 iframe、不另开窗口；shell.html 保留为可选
外壳）。样式仍只引用 `../design-system.css`，暖色系统与动效约束不变。

| 数据 | 接口 | 说明 |
|---|---|---|
| 主页/段位/建议事件 | `GET /api/home` | 与 home 页同源同形状（`lcu` 已合并 rank 诊断字段，见第 4 节） |
| 选人状态 | `GET /draft/state` | **桌面版把 `/draft/*` 代理到 `http://127.0.0.1:8788/*`（同源，无 CORS）**；响应形状 = champ_select_server `/api/state` 全集（含 `rankStatus`/`rankError`/`v`，及下方 `curve`/`heroCurves`） |
| 选人推送 | `GET /draft/events` | SSE 透传（推荐）；代理不支持 SSE 时页面自动降级为 4s 轮询 `/draft/state` |

**胜率-时间曲线字段（T25，形状为草案，由 ui-dev/model-dev 最终定稿）**：
`/draft/state`（与 `/api/state`）新增两个可选字段，缺失时曲线面板自动回退为原
「选人过程曲线」（`history`，横轴按点序展开）：

```jsonc
"curve":      { "minutes": [0,1,2,...,40], "winProb": [0.50,...,0.63] },  // 当前阵容胜率-时间（0-40 分钟）
"heroCurves": { "Orianna": { "minutes": [0,...,40], "winProb": [...] },    // champKey → 该英雄在我方分路的胜率-时间曲线
                "Viktor": { ... }, "Ahri": { ... } },
"teamCurves": { "Garen": { "minutes": [0,...,40], "winProb": [...] },      // T31：己方已锁英雄曲线（形状同 heroCurves）
                "LeeSin": { ... }, "Ahri": { ... }, "Jinx": { ... }, "Thresh": { ... } },
"attribution": [ { "key": "killDiff", "nameCn": "击杀差", "delta": 0.021, "pp": 2.1 },  // T48：胜率归因（delta 概率差，0.021=+2.1pp）
                 { "key": "csDiff", "nameCn": "补刀差", "delta": -0.013, "pp": -1.3 },  // T58：pp = delta×100，前端优先用 pp
                 { "key": "rank", "nameCn": "段位", "delta": 0.009, "pp": 0.9 } ]
```

**归因 chips（T48）**：`/api/state` 与 `/draft/state` 均可携带 `attribution`（形状同上；
T47 model-dev 输出为准）。对局页（app.html）在胜率条下方渲染归因 chips 行
（标签「归因」+ 每项 `nameCn + pp.toFixed(1) + "pp"`，正值绿/负值红/零中性，
最多 5 枚，缺失时隐藏整行；T58 起后端直接给 `pp`（`delta×100`），前端优先用 `pp`，
缺失时回退 `|delta|≤0.5 视为概率差 ×100` 的兼容逻辑）；选人页（champ_select）把因素行升级为同款 chips
（「因素」标签：段位差（分）/ 版本 / 对位（pp），段位三态文本同样以 chip 呈现）。

展示规格（已实现）：横轴 0–40 分钟（刻度每 5 分钟）、纵轴自适应（40%–80% 区间含余量）；
主曲线 = 当前阵容（琥珀 #B45309 实线 + 渐变填充）；**己方已锁英雄曲线常驻显示**
（1.6px 细线 + 85% 透明度 + 小端点的可区分色系，缺失/未锁不画，图例「我方」组）；
候选榜单行点击切换叠加候选曲线（2.2px、图例「叠加」组 + 选中态高亮 + × 取消；己方
已锁英雄不重复叠加）；悬停提示叠加说明。演示：`?demo=1&heroes=Orianna` 预叠加一条
候选曲线（自检截图用）。

**行为约定**：

- `/draft/state` 连续失败（404/连接拒绝）→ 选人板块显示「选人助手未启动」空态
  （含代理说明 + 重新检测按钮）；返回 `ok:false` → 「等待 BP」空态（LCU 状态提示 +
  五位置占位）；`ok:true` → 禁用行、5 位置棋盘（「我」标记）、胜率条、曲线、6 候选
  完整渲染；`frozen` 时显示「对局进行中，推荐已暂停」。
- 建议与事件流来自 `/api/home` 的 `state.advice`/`state.feed`（对局进行中显示）；
  段位卡复用 T19 三态（`state.rankStatus` 缺失时用 `lcu` 诊断字段降级）。
- 窄栏「设置/自检」按钮优先调用 `window.lolAdvisor.openSettings`/`runSelftest` 桥，
  无桥时 toast 提示（与第 4 节桥契约一致）。
- 演示自检：`?demo=1`（右下角切换 BP 中/等待/未启动/加载中四态），
  `&rank=unconnected|resolving|failed` 复用段位三态样例；截图
  `ui/next/previews/app-{bp,waiting,offline}.png` + `app-narrow800.png`。

**ui-dev 接线建议**：home 服务（desktop_app 内嵌 HTTP）增加 `/draft/*` 反向代理到
`127.0.0.1:8788`（透传 SSE 头；不实现则页面自动轮询）；主窗口改为加载 app.html，
`/api/home` 沿用现有实现即可。

---

## 8. 主题（T28 · 深色电竞）

- **主题 key**：localStorage `loladvisor.theme`（`"light"` | `"dark"`，默认浅色暖色）。
- **URL 参数**：`?theme=dark|light`（优先级最高，同时回写 localStorage；无头截图用）。
- **实现**：`<html data-theme="dark">` + `ui/design-system.css` 内 `[data-theme="dark"]`
  全套 token 与组件覆盖——背景 #0E1015、卡片 #171A21 细描边、文字 #E8E6E3/#9BA0A8
  （对比 ≥4.5:1）、强调金色 #C9A227（hover 亮金 #E0B73C）、次强调蓝紫 #7C5CFF
  （焦点环/加载旋转/演示面板激活态）、语义色深底适配（蓝 #4D9FFF / 红 #FF5C4D /
  涨 #3DDC84 / 跌 #FF6B5E）、阴影加深、毛玻璃深色半透明、曲线主色变金（--curve-main）。
  布局/圆角/字体/动效完全不变。
- **入口**：app.html 设置浮层（窄栏「设置」按钮）含主题开关，切换即时生效并持久化，
  `?panel=1` 打开浮层供截图；champ_select / dashboard / home 启动时读同 key 自动应用
  （跨源各自记忆），不新增设置入口。
- **截图**：`ui/next/previews/app-dark-{bp,waiting,settings}.png` + `app-dark-narrow800.png`。

---

## 9. 个人主页（GET /api/profile）— 提案契约（T33）

桌面版 home 服务新增 `GET /api/profile`（LCU 数据聚合；无客户端/未登录时返回 `ok:false` +
`message`/`detail`）：

```jsonc
{
  "ok": true,
  "summoner": { "name": "雾岛听风#777", "level": 328, "iconId": 4868 },
  "solo": { "tier": "EMERALD", "tierCn": "翡翠", "division": "III", "lp": 42,
            "wins": 168, "losses": 154, "unranked": false },   // 无数据时为 null
  "flex": { "tier": null, "tierCn": "", "division": "", "lp": 0,
            "wins": 0, "losses": 0, "unranked": true },        // 未定级：unranked=true，其余可空
  "matches": [                                                  // 最近 ≥10 局，新→旧
    { "champId": 103, "champ": "阿狸", "win": true, "kills": 8, "deaths": 3,
      "assists": 11, "gameTime": 1732, "playedAt": "2026-11-11T22:41:00+08:00",
      // T62：战绩检查补全
      "queueId": 420, "queueCn": "单双排", "cs": 214, "gold": 14200,
      "damage": 32500, "vision": 28, "positionCn": "中路", "remake": false }
  ],
  "matchSummary": {                                             // T62：近 N 局汇总
    "games": 10, "wins": 6, "losses": 4, "winRate": 60.0,
    "kdaRatio": 3.2, "kills": 62, "deaths": 38, "assists": 96,
    "avgCsPerMin": 6.9, "avgDurationMin": 31.2,
    "streak": 2, "streakKind": "win"
  }
}
```

- `tier` 用 LCU 常量（EMERALD/PLATINUM…）+ `tierCn` 中文名；前端兼容 `tierCn` 缺失（显示 tier）。
- 召唤师头像：`GET /icon/profile/<iconId>.png`（桌面版代理 LCU 头像资源；404 时前端降级
  首字渐变块）；对局英雄头像复用 `/icon/<champId>.png`。
- T62：`matches[]` 增加 `queueId/queueCn/cs/gold/damage/vision/positionCn/remake`，
  顶层增加 `matchSummary`（胜率、KDA 比值、场均补刀与时长、连胜/连败）；
  这些字段都是可选增强，前端渲染时按缺省值降级（不阻塞旧数据）。
- app.html 侧栏新增「主页」导航（与「选人」两视图切换，默认选人）；
  演示：`?demo=1&view=home`（完整样例）、`?profile=offline`（客户端未连接空态样例）、
  `&theme=dark` 深色；截图 `ui/next/previews/home-profile-*.png`。
- 展示规则：单双排/灵活排位两枚段位卡（段位名 + 分段 + LP + 胜/负/胜率；
  `unranked` → 「未定级」；`null` → 「暂无数据」）；对局列表每行 = 英雄头像 + 英雄名 +
  距今时间 + KDA + 对局时长 + 胜/负标签（绿胜红负）。

---

## 10. 打野雷达（GET /api/jungler + GET /capture/frame.jpg）— 提案契约（T53）

app.html 右下角「打野雷达」面板（选人视图底行第三列，浅色/深色自适应）。字段与
model-dev T47/T53 schema 对齐（以其最终输出为准）：

```jsonc
GET /api/jungler
{
  "ok": true,                        // false/无对局 → 前端隐藏面板
  "state": "unseen" | "spotted" | "tracking",   // 三态：未见 / 出现一次 / 追踪中
  "jungler": { "champId": 64, "champ": "LeeSin", "champCn": "李青", "name": "红方打野#2001" },
  "lastSeen": { "zone": "bot_river", "zoneCn": "下河道", "gameTime": 214, "ago": 32 } | null,
  "predicted": { "zone": "top_jungle", "zoneCn": "上野区", "confidence": 0.76, "eta": 45 } | null,
  "threatLane": "bot" | null,        // 机器值 top/mid/bot（null 隐藏「警惕」pill）
  "threatLaneCn": "下路" | null,     // 显示用中文
  "route": [                          // 开野路线时间轴（营地顺序 + 时间刻度）
    { "camp": "red_buff", "campCn": "红BUFF", "time": 95,  "done": true },
    { "camp": "blue_buff", "campCn": "蓝BUFF", "time": 280, "done": false, "current": true }
  ],
  "capture": { "available": true, "fps": 1, "url": "/capture/frame.jpg" }
}
GET /capture/frame.jpg               // 屏幕采集帧（JPEG，1-2fps；前端 <img> 每 700ms
                                     // 加 ?t=时间戳 刷新；不可用时前端显示占位 + 「采集未启动」）
```

**中文字段（T58）**：机器键 `champ/zone/threatLane/camp` 保留用于头像与逻辑判断；显示优先
`champCn/zoneCn/threatLaneCn/campCn`，缺失时回退机器键（旧接口仍可渲染）。`jungler.champ`
始终传机器键（`avatarHtml` 用它拼 `/icon/<champId>.png`），`name` 为召唤师名，若与 `champCn`
相同则前端不再重复显示副标题。

前端行为：`capture.available=false` 或演示模式 → 采集区显示地图占位（SVG 河道/路线线）
+「采集未启动」说明；`lastSeen/predicted` 为 null 时显示「尚未现身 / 等待更多视野信息」；
时间轴当前预测营地金色高亮、已完成营地淡化；状态 pill 绿（追踪中）/琥珀（已现身）/灰（未见）。
演示：`?demo=1&jungler=tracking|spotted|unseen`（默认 tracking），`&nopanel=1` 隐藏演示
面板供截图；截图 `ui/next/previews/jungler-*.png`。
