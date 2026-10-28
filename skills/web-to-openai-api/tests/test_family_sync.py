"""同族共享源同步器的回归测试（纯离线，用临时目录造一个「共享源 + 成员」世界）。

为什么这些测试值钱：`family_sync.py` 的 `--apply` 会**真写别的仓库的文件**。它必须满足三条：
  1. 默认 dry-run：不带 `--apply` 一个字节都不许动；
  2. 写前备份：原文件进 `.skill-family-backup/<时间戳>/`；
  3. 不碰站点专属文件（`references/case-study-*.md` 之类）。
这三条只要坏一条，代价是别人仓库里的知识被静默覆盖，所以逐条断言。

运行：`.venv/bin/python -m pytest skills/web-to-openai-api/tests -q`
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
FAMILY_SYNC = SKILL_DIR / "scripts" / "family_sync.py"
CHECK_TEMPLATES = SKILL_DIR / "scripts" / "check_templates.py"
GATE = SKILL_DIR / "scripts" / "gate.py"


def run(script: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run(
        [sys.executable, str(script), *args], cwd=str(cwd), capture_output=True, text=True, env=env
    )


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def fsync(canonical: Path, *args: str) -> subprocess.CompletedProcess:
    """固定指向临时世界的 family.json（**不能**用工具默认的那个真实 family.json）。"""
    return run(FAMILY_SYNC, *args, "--family", str(canonical / "family.json"),
               cwd=canonical.parent)


def expected_pin(canonical: Path) -> str:
    """从工具自己打印的「期望钉」里取，避免测试里重复实现一遍哈希算法。"""
    out = fsync(canonical, "check", "--self-only")
    line = next(ln for ln in out.stdout.splitlines() if ln.startswith("钉（"))
    return line.split("：", 1)[1].strip()


SHARED_BODY = "共享正文：四阶段 + 硬规则 + 事故档案\n"


def refresh_pin(canonical: Path) -> str:
    """共享源内容变了 → 钉必须跟着更新（这正是 check 要抓的 STALE-PIN）。

    顺序很重要：先落正文、再算指纹、最后写 frontmatter —— 因为指纹只算正文，
    写 frontmatter 不会再改变指纹（这正是「共享正文 + 站点专属 frontmatter」的设计）。
    """
    write(canonical / "SKILL.md", f"---\nname: web-to-openai-api\nversion: 9.9.9\n---\n{SHARED_BODY}")
    pin = expected_pin(canonical)
    write(canonical / "SKILL.md",
          f"---\nname: web-to-openai-api\nversion: 9.9.9\nfamily-source: {pin}\n---\n{SHARED_BODY}")
    return pin


def build_world(tmp_path: Path) -> tuple[Path, Path]:
    """造一个最小世界：共享源（canonical）+ 一个成员。返回 (canonical, member)。"""
    canonical = tmp_path / "canonical"
    member = tmp_path / "member"
    write(canonical / "references" / "design.md", "设计：选择器用 || 候选串\n")
    write(canonical / "scripts" / "tool.py", "print('shared tool')\n")
    write(canonical / "references" / "case-study-canonical.md", "共享源的站点案例\n")

    family = {
        "family": "web-to-openai-api",
        "version": "9.9.9",
        "canonical_skill": ".",
        "pin_field": "family-source",
        "frontmatter_stripped": ["SKILL.md"],
        "shared": ["SKILL.md", "references/*.md", "scripts/*.py"],
        "site_specific": ["references/case-study-*.md"],
        "members": [
            {"name": "Canonical", "skill": ".", "role": "canonical"},
            {"name": "Member", "skill": "../member", "role": "member"},
        ],
    }
    write(canonical / "family.json", json.dumps(family, ensure_ascii=False, indent=2))
    pin = refresh_pin(canonical)

    for rel in ("references/design.md", "scripts/tool.py"):
        target = member / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(canonical / rel, target)
    # 成员：frontmatter 是自己的（站点专属），正文与共享源一致
    write(member / "SKILL.md",
          f"---\nname: web-to-openai-api\nversion: 2.0.0\nsite: member\n"
          f"family-source: {pin}\n---\n{SHARED_BODY}")
    write(member / "references" / "case-study-member.md", "成员自己的站点案例（必须保住）\n")
    return canonical, member


def test_check_green_when_everything_matches(tmp_path: Path) -> None:
    canonical, _ = build_world(tmp_path)
    out = fsync(canonical, "check")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "[OK  ] Member" in out.stdout
    assert "共享内容" in out.stdout
    assert "成员独有" in out.stdout  # 站点专属文件只做提示，不判定为不一致


def test_check_reports_drift_missing_and_stale_pin(tmp_path: Path) -> None:
    canonical, member = build_world(tmp_path)
    write(canonical / "references" / "design.md", "设计：阈值一律用秒\n")  # 共享源前进（钉随之过期）
    (member / "scripts" / "tool.py").unlink()  # 成员缺文件
    out = fsync(canonical, "check")
    assert out.returncode == 1
    assert "references/design.md" in out.stdout
    assert "scripts/tool.py" in out.stdout
    assert "钉过期" in out.stdout
    assert "[DRIFT] Member" in out.stdout


def test_check_marks_unpinned_member(tmp_path: Path) -> None:
    canonical, member = build_world(tmp_path)
    write(member / "SKILL.md", "---\nname: web-to-openai-api\nversion: 9.9.9\n---\n成员\n")
    out = fsync(canonical, "check")
    assert out.returncode == 1
    assert "未钉版本" in out.stdout


def test_self_only_ignores_member_drift(tmp_path: Path) -> None:
    canonical, member = build_world(tmp_path)
    (member / "references" / "design.md").write_text("被改坏了\n", encoding="utf-8")
    assert fsync(canonical, "check", "--self-only").returncode == 0


def test_sync_defaults_to_dry_run(tmp_path: Path) -> None:
    canonical, member = build_world(tmp_path)
    write(canonical / "references" / "design.md", "设计：改过的新内容\n")
    refresh_pin(canonical)
    before = (member / "references" / "design.md").read_text(encoding="utf-8")
    out = fsync(canonical, "sync", "Member")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "dry-run，不写任何文件" in out.stdout
    assert (member / "references" / "design.md").read_text(encoding="utf-8") == before
    assert not (member / ".skill-family-backup").exists()


def test_sync_apply_writes_backs_up_and_keeps_site_files(tmp_path: Path) -> None:
    canonical, member = build_world(tmp_path)
    write(canonical / "references" / "design.md", "设计：改过的新内容\n")
    write(canonical / "references" / "new-shared.md", "新的共享文件\n")
    refresh_pin(canonical)
    old_member_skill = (member / "SKILL.md").read_text(encoding="utf-8")

    out = fsync(canonical, "sync", "Member", "--apply")
    assert out.returncode == 0, out.stdout + out.stderr
    assert "改过的新内容" in (member / "references" / "design.md").read_text(encoding="utf-8")
    assert (member / "references" / "new-shared.md").exists()
    backups = list((member / ".skill-family-backup").glob("*/SKILL.md"))
    assert backups, "被覆盖的原文件必须先备份"
    assert backups[0].read_text(encoding="utf-8") == old_member_skill
    # 站点专属文件一律不动（这是「收敛共享源」不伤各项目知识的前提）
    assert "成员自己的站点案例" in (member / "references" / "case-study-member.md").read_text(
        encoding="utf-8"
    )
    assert not (member / "references" / "case-study-canonical.md").exists()
    assert fsync(canonical, "check").returncode == 0


def test_sync_refuses_stale_canonical_pin(tmp_path: Path) -> None:
    """共享源的钉自己就不新鲜时必须拒绝同步：否则会把过期的钉复制到所有成员。"""
    canonical, _ = build_world(tmp_path)
    write(canonical / "references" / "design.md", "设计：改过但没更新钉\n")
    out = fsync(canonical, "sync", "Member", "--apply")
    assert out.returncode == 2
    assert "钉不新鲜" in out.stderr


def test_sync_refuses_when_member_is_canonical(tmp_path: Path) -> None:
    canonical, _ = build_world(tmp_path)
    out = fsync(canonical, "sync", "Canonical", "--apply")
    assert out.returncode == 2
    assert "不能同步 canonical" in out.stderr


def test_unknown_member_is_a_usage_error(tmp_path: Path) -> None:
    canonical, _ = build_world(tmp_path)
    assert fsync(canonical, "diff", "Nope").returncode == 2


def test_offline_member_does_not_fail_check(tmp_path: Path) -> None:
    """成员没 clone 在本机时按 N/A 跳过，不是漂移（--require-all 才升级为失败）。"""
    canonical, _ = build_world(tmp_path)
    family_path = canonical / "family.json"
    family = json.loads(family_path.read_text(encoding="utf-8"))
    family["members"].append({"name": "NotCloned", "skill": "../nope", "role": "member"})
    write(family_path, json.dumps(family, ensure_ascii=False, indent=2))
    refresh_pin(canonical)
    out = fsync(canonical, "check")
    assert out.returncode == 0
    assert "[N/A ] NotCloned" in out.stdout
    assert fsync(canonical, "check", "--require-all").returncode == 1


def test_tooling_scripts_expose_a_cli() -> None:
    """门禁与模板校验器都要能 `--help`（防止被改成只能 import 的库而断掉文档里的命令）。"""
    for script in (CHECK_TEMPLATES, GATE, FAMILY_SYNC):
        proc = run(script, "--help", cwd=SKILL_DIR)
        assert proc.returncode == 0, f"{script.name}: {proc.stderr}"
        assert "usage" in proc.stdout.lower()


def test_templates_are_runnable_and_falsifiable() -> None:
    """把 25 条回归模板真跑一遍：fixed 全绿 + 每个变异只红对应用例（区分力）。"""
    proc = run(CHECK_TEMPLATES, cwd=SKILL_DIR)
    assert proc.returncode == 0, proc.stdout[-1500:] + proc.stderr[-500:]
    assert "结论：通过" in proc.stdout


def test_unknown_template_variant_is_usage_error() -> None:
    assert run(CHECK_TEMPLATES, "--only", "nope", cwd=SKILL_DIR).returncode == 2
