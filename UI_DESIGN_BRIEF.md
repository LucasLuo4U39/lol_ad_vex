# UI 设计简报 — LoLAdvisor 界面全面重写（第三轮 UI 专项）

> 版本 1.0（2026-11-12）。目标读者：ui-designer（设计与前端实现）、ui-dev（接入与打包）、qa-tester（UI 验收）。本简报是第三轮 UI 专项的唯一权威方向文档；冲突时以本文件为准。

## 1. 目标

把 LoLAdvisor 的三个界面全部重写为**统一 Web 技术、苹果风格（Apple-inspired）**的现代界面，交付标准是「精致、克制、像一件认真打磨过的产品」，**拒绝粗制滥造**。

用户原话方向：**苹果的风格**。

- 「苹果风格」取其精髓而非照搬：大量留白、清晰的信息层级、细腻材质（半透明毛玻璃）、大圆角卡片、SF Pro 式的字体栈、平滑且含蓄的动效、浅色为主的高对比柔和配色。
- 不追求花哨：动画克制，数据优先，任何动效都必须有目的（反馈、引导视线、状态过渡）。

## 2. 范围（三个界面，风格统一）

| # | 界面 | 现状 | 重写后 |
|---|---|---|---|
| A | 桌面主窗口 | tkinter 深色单窗口（desktop_app.py） | 改为 Web 页面由 Edge（WebView2 / app 模式）渲染，与选人页同一技术栈；主窗口承担：状态总览、启动/打开选人助手与仪表盘的入口、设置、自检信息 |
| B | 选人助手页 | assets/champ_select.html（Edge 独立窗口） | 全面重写（保留现有数据契约与自动刷新/重连逻辑） |
| C | Web 仪表盘 | advisor_dashboard.html（浏览器） | 全面重写 |

三者共用一套设计系统（tokens、组件、字体、动效），视觉上必须像是同一个产品。

## 3. 设计系统方向（tokens 初稿，ui-designer 可定稿微调）

- **浅色为主**（Apple 默认气质），可选暗色模式（第三轮优先浅色，暗色留作 v2.3.x）。
  - 页面背景 `#F5F5F7`，卡片 `#FFFFFF`（或 rgba 白 + 毛玻璃），分隔 `#E8E8ED`。
  - 主文本 `#1D1D1F`，次要 `#6E6E73`，弱化 `#86868B`。
  - 强调蓝 `#0071E3`（Apple 蓝）；蓝方 `#007AFF`、红方 `#FF453A`；胜率上涨绿 `#34C759`、下跌红 `#FF3B30`。
  - 渐变仅用于胜率曲线、关键数字高亮等少数位置，纯度低、柔和。
- **字体栈**：`-apple-system, "SF Pro Text", "Segoe UI Variable", "Segoe UI", "PingFang SC", "Microsoft YaHei", system-ui, sans-serif`；数字与时间用 `font-variant-numeric: tabular-nums`；标题 weight 600-700，正文 400-500。
- **圆角**：卡片 16px、大容器 24px、按钮/输入 10px、胶囊 pill 999px。
- **阴影**：多层柔和（如 `0 1px 2px rgba(0,0,0,.04), 0 8px 24px rgba(0,0,0,.06)`），避免深色硬阴影。
- **材质**：顶栏/浮动面板用 `backdrop-filter: blur(20px) saturate(1.8)` + 半透明底（WebView2 支持；必须提供无 blur 的降级底色）。
- **动效**：150–350ms，缓动 `cubic-bezier(.2,0,0,1)`（Apple 式）；数据更新用数字过渡/进度条平滑动画；页面切换淡入；**禁止**弹跳、闪烁、跑马灯等廉价效果。
- **组件库**（每个组件必须有 hover/active/focus/disabled 态）：卡片、pill、进度条、环形图（胜率）、平滑曲线图、表格/榜单、toast（错误提示）、空态、骨架屏 loading。

## 4. 数据契约（硬约束）

新页面**不得**要求大改服务端；必须兼容现有接口：

- 选人助手页：读 `champ_select_server.py` 的 `/state`、`/icon/<id>.png`、SSE/轮询逻辑，字段名原样沿用（`winProb`、`bans`、`candidates`、`factors`、`history`、`phase`、`frozen` 等）。只读服务端代码确认字段全集。
- 仪表盘：读 `advisor_server.py` 的 `/api/state` 返回形状（胜率曲线、事件概率、建议、事件流、`teamDiff.grubs` 等；注意服务端字段名以代码为准，历史 bug 如 grub/grubs 已修，按代码对齐）。
- 桌面主窗口：新页面数据可来自进程内新加的最小接口（由 ui-dev 在 Phase B 实现，ui-designer 先按提案的 shape 做 demo 数据并在 `ui/next/contracts.md` 写明期望接口）。
- 所有新页面必须支持 `?demo=1`（或等价）**静态演示模式**：不依赖服务器即可渲染完整样例数据，用于无头截图自检。

## 5. 质量红线（验收标准，qa-tester 逐条核对）

1. 零 console 报错、零 404/缺失资源、无坏链图标。
2. 三态齐全：loading（骨架屏）、空态（有引导文案）、错误态（可恢复、可重试）。
3. 响应式：800px–1920px 窗口宽度下布局不破；桌面主窗口默认 ~1180×760。
4. 无头截图自检：用 Edge `--headless=new --screenshot` 输出每个界面 ≥3 张状态截图（默认/加载中/空态或错误态），随交付提交。
5. 中文文案统一、无错别字、无半截文本；术语与现有产品一致（选人阶段、巢虫、口袋金币等）。
6. 基础可访问性：焦点可见、正文对比度 ≥4.5:1、可键盘操作主要按钮。
7. 性能：首屏可交互 <1s（本地资源，禁引外网 CDN）；动画仅用 transform/opacity，避免布局抖动。
8. 全部资源本地化：字体不依赖网络，图标用内联 SVG 或系统 emoji 之外的本地文件，禁引外部 CDN（离线环境）。

## 6. 流程与分工

- **Phase A（现在即可并行开始，ui-designer）**：设计系统 + 三个新页面，全部落在新目录 `akari-match-export/ui/`（如 `ui/design-system.css`、`ui/next/champ_select.html`、`ui/next/dashboard.html`、`ui/next/home.html`、`ui/next/contracts.md`、`ui/next/previews/*.png`）。**不得修改任何现有项目文件**（第二轮成员正在编辑它们）。交付物：设计 tokens 说明 + 页面 + 演示模式 + 无头截图 + 与现有接口的字段对照表。
- **Phase B（第二轮 T5-T8 全部完成并打包后，ui-dev）**：把新页面接入 champ_select_server.py / advisor_server.py / desktop_app.py（主窗口 tkinter → Web 渲染），联调真实数据，跑 build_exe.cmd 打包 **v2.3** 并 selftest。
- **Phase C（qa-tester）**：按第 5 节清单验收 + 三界面冒烟 + 与 ui-designer 的截图做视觉回归；通过后 lead 交付用户。

## 7. 目录约定（新文件）

```
akari-match-export/ui/
  design-system.css      # tokens + 组件样式（唯一样式源）
  next/
    home.html            # 桌面主窗口页面（Phase A 做 demo 版）
    champ_select.html    # 选人助手页
    dashboard.html       # Web 仪表盘
    contracts.md         # 期望的接口契约（供 ui-dev 实现）
    demo-data.js         # 演示模式共用样例数据
    previews/            # Edge 无头截图（≥3 张/界面）
```
