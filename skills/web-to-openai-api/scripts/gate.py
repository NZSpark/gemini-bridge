#!/usr/bin/env python3
"""四阶段门禁：把 SKILL.md 里四个阶段的**退出条件**串成一次可执行自检，退出码即结论。

阶段与退出条件（对应 SKILL.md 的阶段表）：

  ① 设计  能回答「会话活多久 / 谁轮转 / 失败怎么分类」；选择器与「生成中」信号来自实测
  ② 编码  /v1/models + 非流式 + 流式跑通；单测全绿且**不联网、不启浏览器**
  ③ 排错  已知失败都能映射到错误码；每个已修故障都有**有区分力**的回归用例
  ④ 验收  静态 / 契约 / 真机对等 / 安全四层都过；未过的项有名字、有原因、有下一步

设计原则：

- **默认离线**：不碰真实账号、不启浏览器。真机契约冒烟只有显式给 `--base-url` 才跑（否则如实
  SKIP，而不是伪造成通过）。
- **每个门都给证据**：文件、计数、命令退出码，而不是「应该没问题」。
- **退出码即结论**：`0` 全过；`1` 有 FAIL；`2` 连门禁都跑不起来（用法/环境错误）。
  `--strict` 把 SKIP 也当失败（CI 里要「零 skip」时用）。

用法::

    python3 scripts/gate.py                                  # 四阶段全部（离线）
    python3 scripts/gate.py --phase debug                    # 只跑 ③
    python3 scripts/gate.py --base-url http://127.0.0.1:8002 --token-budget 500
    python3 scripts/gate.py --json gate.json --strict
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 输出含中文；在非 UTF-8 终端下保证 print 不炸
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
PHASES = ("design", "code", "debug", "accept")
PHASE_TITLES = {
    "design": "① 设计",
    "code": "② 编码",
    "debug": "③ 排错",
    "accept": "④ 验收",
}

ENV_KEY_RE = re.compile(r"env_(?:int|float|bool|str)\(\s*\"([A-Z][A-Z0-9_]*)\"")
EXAMPLE_KEY_RE = re.compile(r"^([A-Z][A-Z0-9_]+)=", re.M)
README_ROW_RE = re.compile(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|\s*`([^`]*)`\s*\|", re.M)
DEFAULT_RE = r'env_(?:int|float|bool|str)\(\s*"{key}"\s*,\s*([^)\n]+?)\s*\)'
ERROR_CODE_RE = re.compile(
    r'"(unavailable|invalid_request_error|context_length_exceeded|upstream_busy|timeout'
    r'|upstream_error|composer_not_found|prompt_submission_failed|challenge_required'
    r'|usage_limit_reached|tool_call_parse_error|cancelled|server_error)"'
)
BROWSER_LAUNCH_RE = re.compile(r"async_playwright|sync_playwright|\.chromium\b|launch\(", re.I)
#: D4 的判据：必须写出「**选了哪条**」路线，形如 `路线：A` / `路线决策 B`。
#: 只提到 recon、或写「A/B/C 三条路线」这种泛泛描述**不算** ——
#: 否则「本轮没跑 recon」这种否定陈述也会被判成留档（实测假 PASS，家族头号红线）。
ROUTE_DECISION_RE = re.compile(
    r"(?:路线|route)\s*[:：]\s*\**\s*[ABC]\b"
    r"|(?:路线|route)\s*(?:选择|决策|结论|choice|decision)\s*[:：]?\s*\**\s*[ABC]\b",
    re.IGNORECASE,
)
DESIGN_KEYWORDS = ("选择器", "契约", "会话", "错误", "风险", "验收")
UNVERIFIED_MARKERS = ("未验证", "未复现", "已知限制", "未通过", "待验证")

# 死键判据用：env 绑定表达式与「模块级赋值目标」（列 0 的 `NAME =`）。
ENV_BIND_RE = re.compile(r'env_(?:int|float|bool|str)\(\s*"(?P<key>[A-Z][A-Z0-9_]*)"')
ASSIGN_TARGET_RE = re.compile(r"^([A-Za-z_]\w*)\s*=", re.M)

RESULTS: list[dict] = []


def record(gate: str, phase: str, title: str, status: str, evidence: str, hint: str = "") -> None:
    RESULTS.append(
        {"gate": gate, "phase": phase, "title": title, "status": status,
         "evidence": evidence, "hint": hint}
    )


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def run(cmd: list[str], cwd: Path, timeout: float) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        return 127, f"{exc}"
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout:g}s）：{' '.join(cmd)}"
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def tail(text: str, limit: int = 320) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return " / ".join(lines[-3:])[:limit]


def assign_target_before(cfg_text: str, call_start: int) -> str | None:
    """`env_*("KEY", …)` 之前最近的**模块级**赋值目标；必须与这次调用属于同一条语句。

    同一条语句 = 同一行，或赋值右侧的括号还没闭合（覆盖跨行的列表推导式：
    `CONTINUE_BUTTON_TEXTS = [\n    for t in env_str("CONTINUE_BUTTON_TEXTS", …)…]`）。
    判不出来就返回 None —— 宁可退回旧判据，也不要瞎认一个别的语句的属性名。
    """
    matches = list(ASSIGN_TARGET_RE.finditer(cfg_text, 0, call_start))
    if not matches:
        return None
    match = matches[-1]
    segment = cfg_text[match.end():call_start]
    if "\n" not in segment:
        return match.group(1)
    depth = 0
    for char in segment:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth < 0:
                return None
    return match.group(1) if depth > 0 else None


def bound_attrs(cfg_text: str) -> dict[str, set[str]]:
    """env 键 → 它被绑定到的配置属性名。"""
    out: dict[str, set[str]] = {}
    for match in ENV_BIND_RE.finditer(cfg_text):
        target = assign_target_before(cfg_text, match.start())
        if target:
            out.setdefault(match.group("key"), set()).add(target)
    return out


def key_is_wired(key: str, source: str, binds: dict[str, set[str]]) -> bool:
    """键名直接出现在配置包之外，或它绑定的属性在配置包之外被读过，都算接线。

    **两个方向都要管住**：只看键名 → 误报正常接线；只看「绑定过」 → 永远 PASS（检查退化）。
    """
    if re.search(rf"\b{re.escape(key)}\b", source):
        return True
    return any(re.search(rf"\b{re.escape(attr)}\b", source) for attr in binds.get(key, ()))


# ---------------------------------------------------------------- ① 设计


def phase_design(root: Path, args) -> None:
    candidates = [root / "doc" / "design.md", root / "doc" / "tasks.md", root / "README.md"]
    found: tuple[Path, int, list[str]] | None = None
    for path in candidates:
        if not path.exists():
            continue
        text = read(path)
        missing = [k for k in DESIGN_KEYWORDS if k not in text]
        hits = len(DESIGN_KEYWORDS) - len(missing)
        if found is None or hits > found[1]:
            found = (path, hits, missing)
    if found is None:
        record("D1", "design", "设计文档存在且覆盖六个议题", "FAIL",
               f"未找到任何候选（{' / '.join(str(c.relative_to(root)) for c in candidates)}）",
               "先写设计文档：选择器 / 契约 / 会话模型 / 错误语义 / 风险 / 验收标准")
    elif found[1] >= 4:
        record("D1", "design", "设计文档存在且覆盖六个议题", "PASS",
               f"{found[0].relative_to(root)} 命中 {found[1]}/6"
               + (f"，缺 {','.join(found[2])}" if found[2] else ""))
    else:
        record("D1", "design", "设计文档存在且覆盖六个议题", "FAIL",
               f"{found[0].relative_to(root)} 只命中 {found[1]}/6，缺 {','.join(found[2])}")

    # D2 选择器是配置，且候选用 `||` 串（站点改版只改配置）
    selector_hits: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if ".venv" in path.parts or "node_modules" in path.parts:
            continue
        text = read(path)
        if "SELECTOR" not in text.upper() or "||" not in text:
            continue
        selector_hits.append(str(path.relative_to(root)))
    if selector_hits:
        record("D2", "design", "选择器集中在配置且用候选串", "PASS",
               f"含 `||` 候选串的源文件 {len(selector_hits)} 个：{', '.join(selector_hits[:3])}")
    else:
        record("D2", "design", "选择器集中在配置且用候选串", "FAIL",
               "没有任何源文件同时含 SELECTOR 与 `||` 候选串",
               "把选择器挪进配置，候选用 `||` 串（附候选回退与 SELECTOR_VERSION）")

    # D3 错误码必须有文档（可观察判据的最低要求：至少写下来过）
    # 注意：**不能把 skill 自己的脚本算进「生产代码」**（gate.py 里就有整张错误码表），
    # 否则门禁会自己满足自己。
    tooling = {"gate.py", "check_templates.py", "regression_kit.py", "smoke_test.py", "recon.py"}
    code_files = [
        p for p in root.rglob("*.py")
        if ".venv" not in p.parts
        and "test" not in p.name
        and "skills" not in p.parts
        and p.name not in tooling
    ]
    used = sorted({m for p in code_files for m in ERROR_CODE_RE.findall(read(p))})
    docs = [root / "README.md"] + sorted((root / "doc").glob("*.md")) if (root / "doc").exists() else []
    doc_text = "\n".join(read(p) for p in docs if p.exists())
    undocumented = [c for c in used if c not in doc_text]
    if not used:
        record("D3", "design", "错误码有文档", "SKIP", "源码里没解析到任何错误码字面量")
    else:
        coverage = (len(used) - len(undocumented)) / len(used)
        status = "PASS" if coverage >= args.doc_coverage else "FAIL"
        record("D3", "design", "错误码有文档", status,
               f"覆盖 {len(used) - len(undocumented)}/{len(used)}（{coverage:.0%}，阈值 "
               f"{args.doc_coverage:.0%}）"
               + (f"，未记录：{', '.join(undocumented)}" if undocumented else ""),
               "每个错误码都要有「谁能观察到它」的判据，并写进文档")

    # D4 路线决策（A/B/C）留档
    # 判据落在「选了哪条」这个事实上，而不是「文档里出现过 recon / 路线」——
    # 「提到 recon」与「做了路线决策」是两件事，混为一谈就会把否定陈述判成 PASS。
    route_docs = [p for p in docs if p.exists() and ROUTE_DECISION_RE.search(read(p))]
    recon_reports = [p for p in root.rglob("recon*report*") if ".venv" not in p.parts]
    if route_docs or recon_reports:
        names = [str(p.relative_to(root)) for p in route_docs[:2] + recon_reports[:2]]
        record("D4", "design", "路线决策有留档（A/B/C）", "PASS", ", ".join(names))
    else:
        record("D4", "design", "路线决策有留档（A/B/C）", "SKIP",
               "没有机器可校验的路线结论（只提到 recon / 路线不算）",
               "跑 scripts/recon.py --network-log，把结论写成设计文档里的一行「路线：A|B|C」")


# ---------------------------------------------------------------- ② 编码


def config_source(root: Path) -> tuple[str, list[Path]]:
    files = sorted(p for p in root.rglob("config*.py") if ".venv" not in p.parts)
    files += sorted(p for p in root.rglob("config/*.py") if ".venv" not in p.parts)
    files += sorted(p for p in root.rglob("config/**/*.py") if ".venv" not in p.parts)
    uniq = sorted(set(files))
    return "\n".join(read(p) for p in uniq), uniq


def phase_code(root: Path, args) -> None:
    cfg_text, cfg_files = config_source(root)
    if not cfg_files:
        record("C1", "code", "配置三方一致", "FAIL", "找不到配置模块（config*.py / config/）")
        record("C2", "code", "没有死键", "SKIP", "找不到配置模块")
        record("C3", "code", "路由契约静态存在", "FAIL", "找不到配置模块")
        record("C4", "code", "单测不启浏览器", "SKIP", "找不到配置模块")
        return

    keys = sorted(set(ENV_KEY_RE.findall(cfg_text)))
    example = read(root / ".env.example")
    missing = [k for k in keys if k not in set(EXAMPLE_KEY_RE.findall(example))]
    readme = read(root / "README.md")
    rows = README_ROW_RE.findall(readme)
    drifted: list[str] = []
    for key, documented in rows:
        match = re.search(DEFAULT_RE.format(key=re.escape(key)), cfg_text)
        if not match:
            continue
        actual = match.group(1).strip().strip('"').strip("'")
        try:
            same = float(actual) == float(documented)
        except ValueError:
            same = str(actual).lower() == str(documented).lower()
        if not same:
            drifted.append(f"{key}: README={documented} config={actual}")
    detail = f"config {len(keys)} 键 / .env.example {len(EXAMPLE_KEY_RE.findall(example))} 键"
    detail += f"；README 表 {len(rows)} 行对照"
    status = "PASS" if not missing and not drifted else "FAIL"
    record("C1", "code", "配置三方一致", status,
           detail + (f"，缺 {missing}" if missing else "")
           + (f"，漂移 {drifted[:3]}" if drifted else ""),
           "默认值与 .env.example、README 三方一致，由漂移测试守护")

    others: list[str] = []
    for path in root.rglob("*.py"):
        if ".venv" in path.parts or path in cfg_files or "config" in path.parts:
            continue
        others.append(read(path))
    source = "\n".join(others)
    # 判据：看这个环境变量的**值**有没有生效路径，而不是看键名出现在哪。
    # `RESPONSE_TIMEOUT_S = env_float("DEEPSEEK_TIMEOUT", 180)` 是正常接线（键名不会出现在
    # 配置包之外，被读的是属性）；旧判据会把它误报成死键，而误报会骗人删掉正在生效的配置项。
    binds = bound_attrs(cfg_text)
    dead = [k for k in keys if not key_is_wired(k, source, binds)]
    if dead:
        record("C2", "code", "没有死键", "FAIL", f"{len(dead)} 个键没人读：{', '.join(dead[:6])}",
               "死键 = 虚假的保护感：要么接线，要么删")
    else:
        record("C2", "code", "没有死键", "PASS",
               f"{len(keys)} 个配置键都有生效路径"
               f"（键名直接出现，或绑定的属性被读过；{len(binds)} 个键按属性名判定）")

    required = ("/healthz", "/v1/models", "/v1/chat/completions")
    missing_routes = [r for r in required if r not in source and r not in cfg_text]
    record("C3", "code", "路由契约静态存在",
           "PASS" if not missing_routes else "FAIL",
           "三个必需路由都能在源码里找到" if not missing_routes
           else f"源码里找不到：{', '.join(missing_routes)}")

    tests_dir = root / "tests"
    offenders: list[str] = []
    if tests_dir.exists():
        for path in sorted(tests_dir.rglob("*.py")):
            if "e2e" in path.parts:
                continue  # E2E 是显式开关、允许真机
            text = read(path)
            for lineno, line in enumerate(text.splitlines(), 1):
                if BROWSER_LAUNCH_RE.search(line) and "import" in line:
                    offenders.append(f"{path.relative_to(root)}:{lineno}")
    if not tests_dir.exists():
        record("C4", "code", "单测不启浏览器", "FAIL", "没有 tests/ 目录")
    elif offenders:
        record("C4", "code", "单测不启浏览器", "FAIL",
               f"{len(offenders)} 处引用：{', '.join(offenders[:3])}",
               "单测默认不联网、不启浏览器；真机留给 tests/e2e（显式开关）")
    else:
        record("C4", "code", "单测不启浏览器", "PASS",
               "tests/（不含 e2e）里没有 playwright / 浏览器启动引用")


# ---------------------------------------------------------------- ③ 排错


def phase_debug(root: Path, args) -> None:
    ruff = root / ".venv" / "bin" / "ruff"
    ruff_cmd = [str(ruff)] if ruff.exists() else (["ruff"] if shutil.which("ruff") else [])
    if not ruff_cmd:
        record("R1", "debug", "静态检查全绿", "SKIP", "没找到 ruff", "pip install ruff")
    else:
        code, out = run([*ruff_cmd, "check", "."], root, args.timeout)
        record("R1", "debug", "静态检查全绿", "PASS" if code == 0 else "FAIL",
               f"`{' '.join(ruff_cmd)} check .` → rc={code}"
               + ("" if code == 0 else f"：{tail(out)}"),
               "豁免必须写明原因，不得静默忽略")

    if args.skip_pytest:
        record("R2", "debug", "单测全绿", "SKIP", "--skip-pytest")
    else:
        code, out = run([args.python, "-m", "pytest", "-o", "addopts=", "-q"], root, args.timeout)
        summary = " / ".join(
            ln for ln in out.strip().splitlines() if "passed" in ln or "failed" in ln or "error" in ln
        )[:200]
        record("R2", "debug", "单测全绿", "PASS" if code == 0 else "FAIL",
               f"rc={code} {summary or tail(out)}", "不许用 skip 掩盖失败")

    if args.skip_templates:
        record("R3", "debug", "事故档案反模式都有可红用例", "SKIP", "--skip-templates")
    else:
        script = HERE / "check_templates.py"
        if not script.exists():
            record("R3", "debug", "事故档案反模式都有可红用例", "SKIP",
                   "本仓库没有 skills/.../check_templates.py",
                   "把 skill 的 scripts/ 一起复制过来")
        else:
            code, out = run([args.python, str(script)], root, args.timeout)
            record("R3", "debug", "事故档案反模式都有可红用例", "PASS" if code == 0 else "FAIL",
                   f"rc={code} {tail(out, 200)}",
                   "每条反模式都要有一条「关掉修复就变红」的用例")

    ledgers = sorted((root / "doc").glob("update*.md")) if (root / "doc").exists() else []
    with_fault = [p for p in ledgers if "根因" in read(p) or "复盘" in read(p)]
    if with_fault:
        record("R4", "debug", "故障复盘台账存在", "PASS",
               f"{len(with_fault)} 份含根因/复盘：" + ", ".join(p.name for p in with_fault[:3]))
    else:
        record("R4", "debug", "故障复盘台账存在", "FAIL",
               f"doc/update*.md 找到 {len(ledgers)} 份，但没有含「根因 / 复盘」的内容",
               "每次真机故障 → 复盘文 + 回归测试 + 通用模式")

    # R5 家族共享源自洽：canonical 的钉必须等于当前共享内容的指纹（改了共享文件忘了更新钉 = 假共享源）
    sync_script = HERE / "family_sync.py"
    family_file = HERE.parent / "family.json"
    if args.skip_family or not sync_script.exists() or not family_file.exists():
        record("R5", "debug", "同族共享源自洽（钉新鲜）", "SKIP",
               "本仓库没有 family.json / family_sync.py",
               "多项目共用这套 skill 时接上 family.json，用一条命令守住「单一共享源」")
    else:
        code, out = run([args.python, str(sync_script), "check", "--self-only"], root, args.timeout)
        note = ""
        if code != 0:
            stale = [ln.strip() for ln in out.splitlines() if "钉过期" in ln or "未钉版本" in ln]
            note = "；".join(stale) or tail(out, 200)
        record("R5", "debug", "同族共享源自洽（钉新鲜）", "PASS" if code == 0 else "FAIL",
               f"rc={code}" + (f"：{note}" if note else "（共享指纹与 family-source 一致）"),
               "改了共享文件就要更新 SKILL.md 的 family-source 钉")


# ---------------------------------------------------------------- ④ 验收


def phase_accept(root: Path, args) -> None:
    if not args.base_url:
        record("V1", "accept", "契约冒烟（用户会用的那个接口）", "SKIP",
               "未提供 --base-url：不碰真实桥/账号",
               "起服务后：--base-url http://127.0.0.1:8002 [--token-budget 500]")
    else:
        smoke = HERE / "smoke_test.py"
        if not smoke.exists():
            record("V1", "accept", "契约冒烟", "SKIP", "找不到 smoke_test.py")
        else:
            cmd = [args.python, str(smoke), "--base-url", args.base_url]
            if args.token_budget:
                cmd += ["--token-budget", str(args.token_budget)]
            if args.responses:
                cmd += ["--responses"]
            code, out = run(cmd, root, args.timeout)
            record("V1", "accept", "契约冒烟（用户会用的那个接口）",
                   "PASS" if code == 0 else "FAIL", f"rc={code} {tail(out, 240)}",
                   "退出码 2 = 连不上服务；1 = 有硬失败")

    records = sorted((root / "doc").glob("acceptance*.md")) if (root / "doc").exists() else []
    good = [p for p in records if any(m in read(p) for m in UNVERIFIED_MARKERS)]
    if good:
        record("V2", "accept", "验收记录有证据且如实标注限制", "PASS",
               f"{', '.join(p.name for p in good[:3])} 含未验证/已知限制")
    elif records:
        record("V2", "accept", "验收记录有证据且如实标注限制", "FAIL",
               f"{', '.join(p.name for p in records[:3])} 没有任何未验证/已知限制标注",
               "未通过的项必须原样留成限制，绝不写成通过")
    else:
        status = "SKIP" if args.soft_accept_record else "FAIL"
        record("V2", "accept", "验收记录有证据且如实标注限制", status,
               "没有 doc/acceptance*.md",
               "按 references/acceptance.md §7 的模板留证据（命令 + 耗时 + 片段 + 未验证项）")

    gitignore = read(root / ".gitignore")
    missing_ignores = [p for p in ("user_data/", ".env", "output/") if p not in gitignore]
    if not gitignore:
        record("V3", "accept", "安全：敏感目录/回环/调试默认关", "FAIL", "没有 .gitignore")
    else:
        checks: list[str] = []
        if missing_ignores:
            checks.append(f".gitignore 缺 {', '.join(missing_ignores)}")
        cfg_text, _ = config_source(root)
        host_line = re.search(r'env_str\(\s*"(?:HOST|BIND[^"]*)"\s*,\s*"([^"]*)"', cfg_text)
        if host_line is None:
            checks.append("找不到 HOST 默认值（必须默认 127.0.0.1）")
        elif host_line.group(1) not in ("127.0.0.1", "localhost", ""):
            checks.append(f"HOST 默认是 {host_line.group(1)!r}，不是回环")
        debug_lines = re.findall(r'env_bool\(\s*"([A-Z0-9_]*DEBUG[A-Z0-9_]*)"\s*(?:,\s*([^)]*))?\)',
                                 cfg_text)
        if not debug_lines:
            checks.append("找不到 *DEBUG* 布尔开关（调试端点应默认关闭）")
        else:
            for name, default in debug_lines:
                if (default or "False").strip() not in ("False", "0", "false"):
                    checks.append(f"{name} 默认 {default.strip()}，调试端点默认必须关闭")
        record("V3", "accept", "安全：敏感目录/回环/调试默认关",
               "PASS" if not checks else "FAIL",
               "三连通过：.gitignore 覆盖敏感目录 + 只绑回环 + 调试默认关"
               if not checks else "；".join(checks),
               "转发的是你自己的登录会话：只绑 127.0.0.1，user_data/ 不进 git 也不进云盘")


# ---------------------------------------------------------------- 汇总


def main() -> int:
    ap = argparse.ArgumentParser(description="四阶段门禁（退出码即结论）")
    ap.add_argument("--root", default=".", help="桥项目根目录")
    ap.add_argument("--phase", default="all", choices=[*PHASES, "all"])
    ap.add_argument("--python", default="", help="跑 pytest / 脚本的解释器（默认 .venv 或当前）")
    ap.add_argument("--base-url", default="", help="给了才跑真机契约冒烟")
    ap.add_argument("--token-budget", type=int, default=0, help="冒烟的 A10 护栏（0=用脚本默认）")
    ap.add_argument("--responses", action="store_true", help="冒烟额外检查 /v1/responses")
    ap.add_argument("--doc-coverage", type=float, default=0.6, help="错误码文档覆盖率阈值")
    ap.add_argument("--strict", action="store_true", help="SKIP 也当失败（CI 零 skip）")
    ap.add_argument("--soft-accept-record", action="store_true", help="缺验收记录时降级为 SKIP")
    ap.add_argument("--skip-pytest", action="store_true")
    ap.add_argument("--skip-templates", action="store_true")
    ap.add_argument("--skip-family", action="store_true")
    ap.add_argument("--timeout", type=float, default=900.0)
    ap.add_argument("--json", default="")
    args = ap.parse_args()

    root = Path(args.root).resolve()
    if not root.exists():
        print(f"根目录不存在：{root}", file=sys.stderr)
        return 2
    if not args.python:
        venv_python = root / ".venv" / "bin" / "python"
        args.python = str(venv_python) if venv_python.exists() else sys.executable
    if not any(root.rglob("*.py")):
        print(f"{root} 里没有任何 .py：不像是一个桥项目", file=sys.stderr)
        return 2

    phases = PHASES if args.phase == "all" else (args.phase,)
    for phase in phases:
        {"design": phase_design, "code": phase_code,
         "debug": phase_debug, "accept": phase_accept}[phase](root, args)

    print(f"\n=== 四阶段门禁（{root}）===")
    for phase in phases:
        rows = [r for r in RESULTS if r["phase"] == phase]
        passed = sum(1 for r in rows if r["status"] == "PASS")
        print(f"\n{PHASE_TITLES[phase]}  （{passed}/{len(rows)} 通过）")
        for row in rows:
            print(f"  [{row['status']}] {row['gate']} {row['title']}")
            print(f"       证据：{row['evidence']}")
            if row["status"] != "PASS" and row["hint"]:
                print(f"       下一步：{row['hint']}")

    fails = [r for r in RESULTS if r["status"] == "FAIL"]
    skips = [r for r in RESULTS if r["status"] == "SKIP"]
    verdict = 0
    if fails or (args.strict and skips):
        verdict = 1
    print(f"\n=== 结论 ===\n通过 {sum(1 for r in RESULTS if r['status'] == 'PASS')}"
          f" / 失败 {len(fails)} / 跳过 {len(skips)}"
          f"{'（--strict：跳过也算失败）' if args.strict and skips else ''}")
    if fails:
        print("未通过：" + "、".join(f"{r['gate']} {r['title']}" for r in fails))
    if skips:
        print("跳过（如实标注，未证明通过）："
              + "、".join(f"{r['gate']} {r['title']}" for r in skips))
    if verdict == 0:
        print("门禁通过——四阶段退出条件都已满足（跳过项未验证，不算通过）。")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"root": str(root), "phases": list(phases), "results": RESULTS,
                        "verdict": verdict}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
