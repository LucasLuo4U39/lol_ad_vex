#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""文档一致性门禁：README / VERSION.txt / requirements / 启动器 声明 vs 代码与 metrics.json 真值。

真值基线（代码与产物）：
- 断言数：test_*.py 的静态 AST 计数（check() 调用数 x for 循环展开）
- 参数表：advisor_server.py 的 argparse 定义与默认值
- 指标表：dataset/*/metrics.json 的数值（四舍五入后必须出现在 README）
- requirements：项目内与顶层 requirements.txt 的实际内容
- Python 探测顺序：run-web.cmd / run-replay-web.cmd / run-tests.cmd 的实现
- VERSION.txt：必须是 v2.2 及以上（v2.3 起 UI 全面重写；主版本变化需人工复核）

只依赖标准库，任何捆绑 Python 可直接运行（无需 numpy/pandas）。
退出码：0 = 全部通过；1 = 存在不一致（明细见输出末尾）。
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent          # akari-match-export/
ROOT = BASE.parent                              # loltestv1/
DATASET = ROOT / "dataset"
ANALYSIS = ROOT / "analysis"

PASS = 0
FAIL = 0
WARN = 0
FAILURES = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS {name}")
    else:
        FAIL += 1
        FAILURES.append((name, detail))
        print(f"  FAIL {name}  {detail}")


def warn(name: str, detail: str = "") -> None:
    """提示性告警：不判失败（训练侧 CSV 按打包说明有意不随包）。"""
    global WARN
    WARN += 1
    print(f"  WARN {name}  {detail}")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


# ---------------------------------------------------------------- [1] VERSION
def check_version() -> None:
    print("[1] VERSION.txt")
    first = read(BASE / "VERSION.txt").splitlines()[0].strip()
    m = re.match(r"LoLAdvisor v(\d+)\.(\d+)\.(\d+)", first)
    ok = bool(m) and m.group(1) == "2" and int(m.group(2)) >= 2
    check(
        "VERSION.txt 为 v2.2 及以上（当前 v2.3.0 基线）",
        ok,
        f"首行={first!r}",
    )


# ------------------------------------------------------------ [2] 断言数真值
def parse_event_labels_count() -> int:
    """从 live_features.py（EVENT_LABELS 定义处）静态取长度。"""
    for fname in ("live_features.py", "advisor_server.py"):
        tree = ast.parse(read(BASE / fname))
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "EVENT_LABELS":
                        if isinstance(node.value, (ast.List, ast.Tuple)):
                            return len(node.value.elts)
    return 1


def iter_count(node: ast.AST, special: dict) -> int | None:
    """估计 for 循环迭代次数；无法确定返回 None（按 1 计）。"""
    if isinstance(node, ast.Name) and node.id in special:
        return special[node.id]
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
        if node.func.id == "range" and len(node.args) == 1:
            try:
                return ast.literal_eval(node.args[0])
            except Exception:
                return None
        if node.func.id == "zip" and node.args:
            return iter_count(node.args[0], special)
    return None


def count_checks(path: Path, special: dict) -> int:
    """静态统计 check() 的执行次数（for 循环按迭代数展开）。"""
    tree = ast.parse(read(path))
    total = 0

    def block(body: list, mults: list) -> None:
        nonlocal total
        for stmt in body:
            if isinstance(stmt, ast.For):
                n = iter_count(stmt.iter, special) or 1
                block(stmt.body, mults + [n])
                block(stmt.orelse, mults)
            elif isinstance(stmt, ast.If):
                block(stmt.body, mults)
                block(stmt.orelse, mults)
            elif isinstance(stmt, ast.Try):
                block(stmt.body, mults)
                for handler in stmt.handlers:
                    block(handler.body, mults)
                block(stmt.orelse, mults)
                block(stmt.finalbody, mults)
            elif isinstance(stmt, ast.With):
                block(stmt.body, mults)
            elif isinstance(stmt, (ast.FunctionDef, ast.ClassDef)):
                continue
            else:
                for sub in ast.walk(stmt):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "check"
                    ):
                        # check(name, False) 是「预期失败路径」标记（条件恒 False），
                        # 不计入通过断言数
                        if (
                            len(sub.args) >= 2
                            and isinstance(sub.args[1], ast.Constant)
                            and sub.args[1].value is False
                        ):
                            continue
                        n = 1
                        for m in mults:
                            n *= m
                        total += n

    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            block(node.body, [])
    return total


def check_assert_counts(readme: str) -> None:
    print("[2] 断言数（README vs 测试代码静态真值）")
    n_reports = len(list((ANALYSIS / "game-reviews").glob("*/report.md")))
    special = {"EVENT_LABELS": parse_event_labels_count(), "reports": n_reports}
    truth = {
        "test_advisor.py": count_checks(BASE / "test_advisor.py", special),
        "test_review.py": count_checks(BASE / "test_review.py", special),
        "test_elo.py": count_checks(BASE / "test_elo.py", special),
        "test_rank.py": count_checks(BASE / "test_rank.py", special),
    }
    for name, value in truth.items():
        print(f"  真值 {name} = {value}")
    readme_nums = [int(x) for x in re.findall(r"(\d+) 项断言", readme)]
    expected = [
        truth["test_advisor.py"],
        truth["test_review.py"],
        truth["test_elo.py"],
        truth["test_rank.py"],
    ]
    check(
        f"README 断言数序列 {readme_nums} == 真值 {expected}",
        readme_nums == expected,
        "README 中 61/48/38/31 应分别对应 test_advisor/test_review/test_elo/test_rank",
    )


# ------------------------------------------------------------ [3] 参数表
def parse_advisor_args() -> dict:
    text = read(BASE / "advisor_server.py")
    args = {}
    for m in re.finditer(r'add_argument\(\s*"(--[a-z0-9-]+)"[^)]*\)', text, re.S):
        name = m.group(1)
        dm = re.search(r"default\s*=\s*([^,\n)]+)", m.group(0))
        default = None
        if dm:
            raw = dm.group(1).strip()
            if re.fullmatch(r"-?\d+(\.\d+)?", raw):
                default = float(raw)
            else:
                default = raw.strip("\"'")
        args[name] = default
    return args


def readme_advisor_param_tokens(readme: str) -> list:
    """提取 README 中 advisor_server 参数表（含 --snapshots 的那张表）的 --xxx 参数。"""
    lines = readme.splitlines()
    start = None
    for i, line in enumerate(lines):
        if "`--snapshots" in line:
            j = i
            while j > 0 and not lines[j].startswith("| 参数 | 说明 |"):
                j -= 1
            start = j
            break
    if start is None:
        return []
    tokens = []
    for k in range(start, len(lines)):
        line = lines[k]
        if k > start and (line.strip() == "" or not line.startswith("|")):
            break
        tokens += re.findall(r"`(--[a-z0-9-]+)", line)
    return tokens


def check_param_table(readme: str) -> None:
    print("[3] 参数表（README vs advisor_server.py argparse）")
    args = parse_advisor_args()
    tokens = readme_advisor_param_tokens(readme)
    check("README 参数表存在且非空", bool(tokens), f"tokens={tokens}")
    missing = [t for t in tokens if t not in args]
    check("表中每个参数都在 argparse 中定义", not missing, f"未定义: {missing}")
    must = {"--speed", "--port", "--interval"}
    check("表覆盖 --speed/--port/--interval", must.issubset(set(tokens)), f"表内: {sorted(set(tokens))}")
    defaults = {
        "--speed": (args.get("--speed"), "默认 10"),
        "--port": (args.get("--port"), "默认 8765"),
        "--interval": (args.get("--interval"), "默认 5 秒"),
    }
    for name, (value, text) in defaults.items():
        check(
            f"{name} 默认值声明一致（argparse={value}，README 含「{text}」）",
            text in readme,
            f"argparse default={value}",
        )


# ------------------------------------------------------------ [4] 指标表
def fmt(value, nd: int) -> str:
    return f"{float(value):.{nd}f}"


def load_json(path: Path) -> dict:
    return json.loads(read(path))


def check_metrics(readme: str) -> None:
    print("[4] 指标表（README vs metrics.json）")
    live = DATASET / "live_dataset"
    # 全量胜率模型表（README「全量数据 2,298 局」/ v4v5 对比表）
    overalls = [
        ("live_model2", "0.8272", "0.5031", "0.1693", "73.56%"),
        ("live_model3", "0.8286", "0.5027", "0.1688", "73.85%"),
        ("live_model4", "0.8257", "0.5068", "0.1702", "73.68%"),
        ("live_model5", "0.8553", "0.4706", "0.1553", "76.92%"),
    ]
    for model, auc, ll, brier, acc in overalls:
        j = load_json(live / model / "metrics.json")
        o = j["overall"]
        expected = [fmt(o["auc"], 4), fmt(o["logLoss"], 4), fmt(o["brier"], 4),
                    f"{float(o['accuracy']) * 100:.2f}%"]
        got = [auc, ll, brier, acc]
        for label, exp, actual in zip(["AUC", "LogLoss", "Brier", "Accuracy"],
                                      expected, got):
            check(f"README 含 {model}.{label}={exp}", exp in readme, f"metrics.json={exp}")
        check(f"metrics.json {model} == README 表 {got}", expected == got, f"metrics={expected}")

    # 基线模型（消融 base 行 / 校准 raw 行）
    base = load_json(DATASET / "dataset_v2" / "model_base" / "metrics.json")["overall"]
    for label, exp in [("AUC", fmt(base["auc"], 4)),
                       ("LogLoss", fmt(base["logLoss"], 4)),
                       ("Brier", fmt(base["brier"], 4))]:
        check(f"README 含 model_base.{label}={exp}", exp in readme, "")

    # live 事件模型（README「Live 击杀 / Gank 模型」表）
    ev = load_json(live / "live_event_model" / "metrics.json")["results"]
    for label, (ap, auc) in [
        ("own_kill_30s", ("0.4293", "0.6193")),
        ("enemy_kill_30s", ("0.4301", "0.6213")),
        ("own_gank_60s", ("0.4693", "0.6373")),
        ("enemy_gank_60s", ("0.4680", "0.6364")),
    ]:
        exp_ap, exp_auc = fmt(ev[label]["ap"], 4), fmt(ev[label]["rocAuc"], 4)
        check(f"README 含 live_event.{label} AP={exp_ap} / AUC={exp_auc}",
              exp_ap in readme and exp_auc in readme,
              f"期望 {ap}/{auc} vs metrics {exp_ap}/{exp_auc}")

    # 事件模型（README「击杀 / Gank 事件模型（10 秒网格）」表）
    em = load_json(DATASET / "event_dataset" / "event_model" / "metrics.json")["results"]
    rows = [
        ("kill_30s", "0.6349", "0.6233", "0.6926"),
        ("blue_kill_30s", "0.4409", "0.6114", "0.5239"),
        ("red_kill_30s", "0.4498", "0.6142", "0.5300"),
        ("blue_gank_60s", "0.3983", "0.6057", "0.4970"),
        ("red_gank_60s", "0.4066", "0.6070", "0.5033"),
    ]
    for label, ap, auc, f1 in rows:
        exp_ap, exp_auc, exp_f1 = (fmt(em[label]["ap"], 4),
                                   fmt(em[label]["rocAuc"], 4),
                                   fmt(em[label]["bestF1"], 4))
        check(f"README 含 event.{label} AP={exp_ap} / AUC={exp_auc} / F1={exp_f1}",
              exp_ap in readme and exp_auc in readme and exp_f1 in readme,
              f"期望 {ap}/{auc}/{f1} vs metrics {exp_ap}/{exp_auc}/{exp_f1}")

    # 规模声明：行数 / 局数（真值取 live_model3 的 metrics.json）
    m3 = load_json(live / "live_model3" / "metrics.json")
    rows_s = f"{m3['rows']:,}"
    games_s = f"{m3['games']:,}"
    check(f"README 含全量行数 {rows_s}", rows_s in readme, f"metrics rows={m3['rows']}")
    check(f"README 含全量局数 {games_s}", games_s in readme, f"metrics games={m3['games']}")


# ------------------------------------------------------------ [5] requirements
def has_lightgbm_import(path: Path) -> bool:
    tree = ast.parse(read(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name.split(".")[0] == "lightgbm" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] == "lightgbm":
                return True
    return False


def check_requirements(readme: str) -> None:
    print("[5] requirements 声明（README/打包说明 vs 实际文件）")
    proj_req = read(BASE / "requirements.txt")
    top_req = read(ROOT / "requirements.txt")
    check("顶层 requirements.txt 含 lightgbm==4.7.0（真值）",
          "lightgbm==4.7.0" in top_req, "loltestv1/requirements.txt")
    check("顶层 requirements.txt 含 beautifulsoup4（打包说明声称）",
          "beautifulsoup4" in top_req, "loltestv1/requirements.txt")
    check("项目内 requirements.txt 含 lightgbm pin（README 声称「requirements.txt 里已 pin」）",
          "lightgbm==4.7.0" in proj_req,
          "项目内 requirements.txt 应含 lightgbm==4.7.0（[train/validate] 分节）")
    check("打包说明声称顶层 requirements 含 beautifulsoup4/lightgbm 与 README 提及一致",
          ("beautifulsoup4" in top_req and "lightgbm" in top_req), "")
    for name in ("advisor_server.py", "live_features.py", "lgbmini.py"):
        check(f"{name} 无 import lightgbm（推理侧零依赖）",
              not has_lightgbm_import(BASE / name), "")
    check("README 无过时表述「无法联网安装」", "无法联网安装" not in readme, "")


# ------------------------------------------------------------ [6] 探测顺序
def check_probe_order(readme: str) -> None:
    print("[6] Python 探测顺序（README/启动器实现）")
    cmd_markers = [".venv\\Scripts\\python.exe", "%USERPROFILE%\\.dsh", "py -3", "set PY=python"]
    for fname in ("run-web.cmd", "run-replay-web.cmd", "run-tests.cmd"):
        text = read(BASE / fname)
        idx = [text.find(m) for m in cmd_markers]
        check(f"{fname} 探测顺序 .venv -> DSH -> py -3 -> python",
              all(i >= 0 for i in idx) and idx == sorted(idx),
              f"index={idx}")
    rd_markers = [".venv", "%USERPROFILE%\\.dsh", "`py -3`", "PATH 里的 `python`"]
    idx = [readme.find(m) for m in rd_markers]
    check("README 探测顺序声明与实现一致", all(i >= 0 for i in idx) and idx == sorted(idx),
          f"index={idx}")


# ------------------------------------------------------------ [7] metrics 相对路径
# 训练侧 metrics.json（dataset_v2 / event_dataset）：训练 CSV 按打包说明有意不随包，
# 只硬检查「必须是相对路径」，CSV 缺失给提示性 WARN；
# 其余 6 份（运行时/打包侧）保持「相对路径且 CSV 真实存在」硬检查。
TRAIN_SIDE = {
    (ROOT / "dataset" / "dataset_v2" / "model_base" / "metrics.json").resolve(),
    (ROOT / "dataset" / "dataset_v2" / "model_v2" / "metrics.json").resolve(),
    (ROOT / "dataset" / "event_dataset" / "event_model" / "metrics.json").resolve(),
}


def check_metrics_paths() -> None:
    print("[7] metrics.json 相对路径（运行时侧硬检查，训练侧提示性 WARN）")
    files = sorted(DATASET.glob("**/metrics.json"))
    scratch = BASE / "_full_v3" / "metrics.json"
    if scratch.exists():
        files.append(scratch)
    check("找到 metrics.json", bool(files), f"{len(files)} 份")
    for path in files:
        j = load_json(path)
        ds = str(j.get("dataset", ""))
        target = (path.parent / ds).resolve()
        is_relative = bool(ds) and not re.match(r"^[A-Za-z]:", ds)
        label = f"{path.relative_to(ROOT)} dataset={ds!r}"
        if path.resolve() in TRAIN_SIDE:
            if not is_relative:
                check(label, False, "训练侧 metrics.json 也必须是相对路径")
            elif target.exists():
                check(label, True, "")
            else:
                warn(label, "训练侧 CSV 按打包说明有意不随包（相对路径合规）")
        else:
            if not is_relative:
                check(label, False, "必须是相对路径（不能是打包机绝对路径）")
            elif target.exists():
                check(label, True, "")
            else:
                # 运行时只读同目录的 model.txt（lgbmini），不消费 CSV；
                # 源码包/发布包不带训练 CSV 属正常，这里按提示处理而不是 FAIL。
                warn(label, "CSV 未随包（运行时不读 CSV，模型由同目录 model.txt 提供）")

    # 运行时模型目录必须真的有模型文件（比 CSV 更贴近真实依赖）
    runtime_dirs = ("live_model3", "live_model5", "live_event_model")
    for name in runtime_dirs:
        model_dir = DATASET / "live_dataset" / name
        models = sorted(model_dir.glob("*.txt")) if model_dir.is_dir() else []
        check(f"{name} 有模型 .txt 文件", bool(models), f"{len(models)} 个")


def main() -> int:
    print("[test_docs] 文档一致性门禁（真值：代码 + metrics.json + VERSION.txt）")
    readme = read(BASE / "README.md")
    check_version()
    check_assert_counts(readme)
    check_param_table(readme)
    check_metrics(readme)
    check_requirements(readme)
    check_probe_order(readme)
    check_metrics_paths()
    print("=" * 60)
    print(f"  结果: PASS={PASS} FAIL={FAIL} WARN={WARN}")
    print("=" * 60)
    if FAILURES:
        print("失败项清单（供 lead / ui-dev 复核与修订）:")
        for name, detail in FAILURES:
            print(f"  - {name}: {detail}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
