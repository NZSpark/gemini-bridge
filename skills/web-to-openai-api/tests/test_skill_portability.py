"""可移植性回归测试：整份 skill 拷到别处也必须能用，而且**不许假通过**。

这套 skill 是靠**拷贝**交付的（各桥项目的 `skills/` 目录本身不进版本库），所以
「拷走还能不能自证」是它的核心契约。最容易坏的两个方向：

  1. 某个入口依赖仓库里的其它东西（写死路径、引用仓库根）→ 拷走就废；
  2. 找不到共享源时，把「0 个共享文件 + 空串指纹」当成「一致」→ 调用方（门禁 R5）
     会报出一个**假 PASS**。这一条实测复现过，也正是家族头号红线
     「不能确认成功就不报告成功」的反面例子，所以必须钉死。

区分力：把 `family_sync.require_canonical()` 的护栏去掉（退回「0 个文件也算一致」），
`test_copy_without_canonical_never_fakes_a_pass` 与
`test_gate_r5_does_not_pass_without_a_canonical` 必须一起变红。

运行：<venv>/bin/python -m pytest skills/web-to-openai-api/tests -q
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
ENTRYPOINTS = (
    "recon.py",
    "smoke_test.py",
    "gate.py",
    "check_templates.py",
    "regression_kit.py",
    "family_sync.py",
)
# 缓存与历史备份不属于「要发布的东西」，拷贝时不该带上
NOT_PUBLISHED = ("__pycache__", ".pytest_cache", ".skill-family-backup")


def run(*args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )


def copy_skill(tmp_path: Path) -> Path:
    """整份 skill 拷成干净副本（不带缓存 / 备份），模拟「发布到别处」。"""
    dest = tmp_path / "copied-skill"
    shutil.copytree(SKILL_DIR, dest, ignore=shutil.ignore_patterns(*NOT_PUBLISHED))
    return dest


def pin_copy(skill: Path) -> str:
    """把副本的钉写成它**自己**算出的期望值。

    独立用法不能依赖源仓库的钉是否新鲜（源仓库正在改共享文件时它本来就是过期的），
    所以这里先让工具算出期望钉，再写回副本的 frontmatter。
    """
    report = skill / "own-pin.json"
    run("scripts/family_sync.py", "check", "--self-only", "--json", str(report), cwd=skill)
    pin = json.loads(report.read_text(encoding="utf-8"))["expected_pin"]
    md = skill / "SKILL.md"
    text = md.read_text(encoding="utf-8")
    if re.search(r"(?m)^family-source: .*$", text):
        text = re.sub(r"(?m)^family-source: .*$", f"family-source: {pin}", text, count=1)
    else:
        lines = text.splitlines()
        insert = 1 if lines and lines[0].strip() == "---" else 0
        lines.insert(insert, f"family-source: {pin}")
        text = "\n".join(lines) + "\n"
    md.write_text(text, encoding="utf-8")
    return pin


def rewrite_family(skill: Path, *, canonical: str, members: list[dict]) -> None:
    path = skill / "family.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["canonical_skill"] = canonical
    data["members"] = members
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def test_every_entrypoint_runs_from_a_copy(tmp_path: Path) -> None:
    """拷到别处后，每个脚本入口都要能跑（`--help` 即结论，不依赖仓库）。"""
    skill = copy_skill(tmp_path)
    for script in ENTRYPOINTS:
        proc = run("scripts/" + script, "--help", cwd=skill)
        assert proc.returncode == 0, f"{script} 异地 --help 失败：{proc.stderr[-400:]}"
    # 模板校验是「真跑回归模板」，异地也要跑得动（取一条，快）
    proc = run("scripts/check_templates.py", "--only", "p0a", cwd=skill)
    assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-300:]


def test_copy_without_canonical_never_fakes_a_pass(tmp_path: Path) -> None:
    """共享源不存在时必须报环境错误（退出码 2），且不许出现任何「一致 / OK」。"""
    skill = copy_skill(tmp_path)
    rewrite_family(skill, canonical="no-such-canonical",
                   members=[{"name": "Self", "skill": ".", "role": "canonical"}])
    proc = run("scripts/family_sync.py", "check", cwd=skill)
    assert proc.returncode == 2, (
        f"找不到共享源应当是环境错误：rc={proc.returncode}\n{proc.stdout[-500:]}"
    )
    combined = proc.stdout + proc.stderr
    assert "找不到共享源" in combined, combined[-400:]
    assert "[OK" not in proc.stdout, "不许把「没检查过」输出成 OK"
    assert "e3b0c442" not in proc.stdout, "空串 sha256 指纹正是假通过的签名"


def test_copy_can_become_its_own_canonical(tmp_path: Path) -> None:
    """文档承诺的独立用法：`canonical_skill = "."` 让本副本自成共享源，check 应通过。"""
    skill = copy_skill(tmp_path)
    rewrite_family(skill, canonical=".",
                   members=[{"name": "Self", "skill": ".", "role": "canonical"}])
    pin = pin_copy(skill)
    assert "#" in pin and pin.split("#")[1], f"期望钉不合法：{pin}"
    proc = run("scripts/family_sync.py", "check", "--self-only", cwd=skill)
    assert proc.returncode == 0, proc.stdout[-600:] + proc.stderr[-300:]
    assert "共享内容 = 0 个文件" not in proc.stdout, "自成共享源时不该是 0 个文件"


def test_gate_r5_does_not_pass_without_a_canonical(tmp_path: Path) -> None:
    """门禁 R5（同族共享源自洽）在共享源缺失时必须是 FAIL，而不是 PASS。"""
    skill = copy_skill(tmp_path)
    rewrite_family(skill, canonical="no-such-canonical",
                   members=[{"name": "Self", "skill": ".", "role": "canonical"}])
    project = tmp_path / "proj"
    (project / "app").mkdir(parents=True)
    (project / "app" / "config.py").write_text(
        'import os\n\nX = os.environ.get("Y", "1")\n', encoding="utf-8"
    )
    report = tmp_path / "gate.json"
    proc = run("scripts/gate.py", "--root", str(project), "--phase", "debug",
               "--skip-pytest", "--skip-templates", "--json", str(report), cwd=skill)
    results = json.loads(report.read_text(encoding="utf-8"))["results"]
    r5 = next(r for r in results if r["gate"] == "R5")
    assert r5["status"] == "FAIL", f"R5 不该给 PASS：{r5['evidence']}"
    assert proc.returncode != 0, "有 FAIL 时门禁退出码必须非 0"
