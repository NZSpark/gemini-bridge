#!/usr/bin/env python3
"""机械验证「事故档案 → 回归用例模板」这三件事：**能跑、能红、覆盖全**。

被验证的对象是 `references/regression-templates.md`（25 条可粘贴用例）与它依赖的
`scripts/regression_kit.py`。检查分三步（只用标准库，不需要 pytest）：

1. **抽取**：把文档里所有含 `def test_` 的 python 代码块 + `regression_kit.py` 复制进临时目录；
2. **能跑**：`TEMPLATE_VARIANT=fixed` 跑一遍 → 必须全部通过（否则模板是坏的 / 里面是空断言）；
3. **能红**：对 `regression_kit.BUG_TESTS` 里每个 `bug_id`，用 `TEMPLATE_VARIANT=<bug_id>` 再跑一遍
   → **只有**对应用例必须变红。变不红 = 这条用例没有区分力（把修复关掉也不报错）；
   红多了 = 用例之间互相污染。这就是「区分力实验」的机械化版本。
4. **覆盖**：解析 `SKILL.md` 事故档案表的代号（`P0-A…P0-I` / `A1…A16`），要求
   `BUG_TESTS` 里每条都有模板，并且登记的函数名真的存在于抽取出来的代码里。

用法::

    python3 scripts/check_templates.py [--doc PATH] [--skill PATH] [--kit PATH]
        [--only p0a] [--json out.json] [--keep]

退出码：`0` = 全过；`1` = 有不通过项（模板坏 / 无区分力 / 覆盖不全）；`2` = 用法或环境错误。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# 输出含中文；在非 UTF-8 终端下保证 print 不炸
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HERE = Path(__file__).resolve().parent
DEFAULT_DOC = HERE.parent / "references" / "regression-templates.md"
DEFAULT_SKILL = HERE.parent / "SKILL.md"
DEFAULT_KIT = HERE / "regression_kit.py"

BLOCK_RE = re.compile(r"```python\n(.*?)```", re.S)
TEST_DEF_RE = re.compile(r"^def (test_[A-Za-z0-9_]+)\(", re.M)
CODE_RE = re.compile(r"^\|\s*(P0-[A-I]|A\d{1,2})\s*\|", re.M)
RESULT_RE = re.compile(r"TEMPLATE_RESULT total=(\d+) failed=(\d+) names=(\S*)")

RUNNER = '''

# ---- check_templates.py 追加的运行器（模板本身不需要它；pytest 下请忽略）----
if __name__ == "__main__":
    import traceback

    _names = sorted(k for k, v in list(globals().items()) if k.startswith("test_") and callable(v))
    _failed = []
    for _name in _names:
        try:
            globals()[_name]()
        except BaseException:  # noqa: BLE001  连 CancelledError 一起抓（A16 的观察信号）
            _failed.append(_name)
            print(f"FAIL {_name}")
            traceback.print_exc()
        else:
            print(f"PASS {_name}")
    print(f"TEMPLATE_RESULT total={len(_names)} failed={len(_failed)} "
          f"names={','.join(_failed)}")
    raise SystemExit(1 if _failed else 0)
'''


def bug_id_of(code: str) -> str:
    """`P0-A` → `p0a`；`A16` → `a16`（与 regression_kit.BUG_TESTS 的键一致）。"""
    if code.startswith("P0-"):
        return "p0" + code[3:].lower()
    return "a" + code[1:].zfill(2)


def extract_tests(doc_text: str) -> tuple[str, list[str]]:
    """抽出含 `def test_` 的代码块，返回（拼接后的代码，块内定义的用例名列表）。"""
    chunks: list[str] = []
    names: list[str] = []
    for block in BLOCK_RE.findall(doc_text):
        found = TEST_DEF_RE.findall(block)
        if not found:
            continue  # 只抽用例块；文档里的其它代码块（适配示例等）跳过
        chunks.append(block.strip() + "\n")
        names.extend(found)
    return "\n\n".join(chunks), names


def run_variant(workdir: Path, variant: str, python: str, timeout: float) -> tuple[int, str]:
    env = dict(os.environ)
    env["TEMPLATE_VARIANT"] = variant
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    proc = subprocess.run(
        [python, str(workdir / "_templates_under_test.py")],
        cwd=str(workdir),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser(description="验证回归模板：能跑 / 能红 / 覆盖全")
    ap.add_argument("--doc", default=str(DEFAULT_DOC))
    ap.add_argument("--skill", default=str(DEFAULT_SKILL))
    ap.add_argument("--kit", default=str(DEFAULT_KIT))
    ap.add_argument("--only", default="", help="只验证某一条变异（调试用，如 p0a）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    ap.add_argument("--keep", action="store_true", help="保留临时目录（调试用）")
    ap.add_argument("--timeout", type=float, default=120.0)
    args = ap.parse_args()

    doc_path, skill_path, kit_path = Path(args.doc), Path(args.skill), Path(args.kit)
    for path in (doc_path, skill_path, kit_path):
        if not path.exists():
            print(f"缺文件：{path}", file=sys.stderr)
            return 2

    doc_text = doc_path.read_text(encoding="utf-8")
    skill_text = skill_path.read_text(encoding="utf-8")
    code, names = extract_tests(doc_text)
    if not names:
        print(f"{doc_path.name} 里没有抽到任何 `def test_` 代码块", file=sys.stderr)
        return 2

    # 覆盖检查：事故档案的每条反模式都要有模板
    documented = [bug_id_of(c) for c in dict.fromkeys(CODE_RE.findall(skill_text))]
    if not documented:
        print(f"{skill_path.name} 里没有解析到事故档案编号（P0-* / A*）", file=sys.stderr)
        return 2

    sys.path.insert(0, str(kit_path.parent))
    try:
        import regression_kit  # noqa: PLC0415  需要先插 sys.path
    except Exception as exc:  # noqa: BLE001
        print(f"导入 {kit_path.name} 失败：{exc}", file=sys.stderr)
        return 2
    bug_tests: dict[str, str] = dict(regression_kit.BUG_TESTS)

    problems: list[str] = []
    for bug_id in documented:
        if bug_id not in bug_tests:
            problems.append(f"事故档案有 {bug_id}，但 BUG_TESTS 里没有登记模板")
    for bug_id, test_name in bug_tests.items():
        if bug_id not in documented:
            problems.append(f"BUG_TESTS 里的 {bug_id} 在事故档案表里找不到对应行")
        if test_name not in names:
            problems.append(f"{bug_id} 登记的用例 {test_name} 不在文档的代码块里")

    workdir = Path(tempfile.mkdtemp(prefix="skill-templates-"))
    shutil.copy(kit_path, workdir / "regression_kit.py")
    (workdir / "_templates_under_test.py").write_text(code + RUNNER, encoding="utf-8")

    rows: list[dict] = []
    try:
        rc, out = run_variant(workdir, "fixed", sys.executable, args.timeout)
        match = RESULT_RE.search(out)
        total = int(match.group(1)) if match else 0
        failed_fixed = match.group(3).split(",") if match and match.group(3) else []
        ok_fixed = rc == 0 and not failed_fixed
        rows.append({"variant": "fixed", "expect": "全绿", "actually": f"failed={failed_fixed}",
                     "ok": ok_fixed})
        if not ok_fixed:
            problems.append(f"fixed 变体没有全绿（rc={rc}）：{', '.join(failed_fixed) or out[-500:]}")
        if total != len(names):
            problems.append(f"抽出 {len(names)} 个用例，实际跑了 {total} 个（有块没被收集？）")

        variants = [args.only] if args.only else sorted(bug_tests)
        for bug_id in variants:
            expected = bug_tests.get(bug_id)
            if expected is None:
                print(f"未知的变异 id：{bug_id}（可用：{', '.join(sorted(bug_tests))}）", file=sys.stderr)
                return 2
            rc, out = run_variant(workdir, bug_id, sys.executable, args.timeout)
            match = RESULT_RE.search(out)
            failed = match.group(3).split(",") if match and match.group(3) else []
            ok = rc != 0 and failed == [expected]
            if not ok:
                if match is None:
                    problems.append(f"{bug_id}: 根本没跑起来（rc={rc}）：{out.strip()[-300:]}")
                elif not failed:
                    problems.append(f"{bug_id}: 旧行为换回来后**没有任何用例变红** → {expected} 没有区分力")
                elif expected not in failed:
                    problems.append(f"{bug_id}: 变红的是 {failed}，不是登记的 {expected}")
                else:
                    problems.append(f"{bug_id}: 除 {expected} 外还有 {failed} 变红 → 用例互相污染")
            rows.append({"variant": bug_id, "expect": f"只有 {expected} 红",
                         "actually": f"failed={failed}", "ok": ok})
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            print(f"临时目录保留在 {workdir}")

    print(f"\n=== 回归模板验证（{doc_path.name}）===")
    print(f"抽出用例 {len(names)} 条；事故档案编号 {len(documented)} 条")
    for row in rows:
        print(f"[{'PASS' if row['ok'] else 'FAIL'}] {row['variant']:<8} 期望 {row['expect']:<22} "
              f"实际 {row['actually']}")
    if problems:
        print("\n问题：")
        for problem in problems:
            print(f"  - {problem}")
        print(f"\n结论：不通过（{len(problems)} 项）")
    else:
        print("\n结论：通过——模板能跑、每条都有区分力、覆盖了事故档案里的全部反模式。")

    if args.json:
        Path(args.json).write_text(
            json.dumps({"doc": str(doc_path), "tests": names, "documented": documented,
                        "rows": rows, "problems": problems}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"结果已写入 {args.json}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
