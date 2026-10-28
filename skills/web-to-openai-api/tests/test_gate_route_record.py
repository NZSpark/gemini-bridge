"""D4「路线决策有留档」判据的回归测试（离线，用临时目录造最小项目 + 跑真 gate.py）。

为什么必须钉住：旧判据是「文档里出现 `路线` 或 `recon` 就算留档」，于是
**一份写着「本轮没有跑 recon」的验收记录也会让 D4 变 PASS** —— DeepseekBridge 实测踩到。
这是家族头号红线（不能确认成功就不报告成功）的反面：把「没做过」判成「做过了」。

区分力：
  1. 只提到 recon / 泛泛谈 A/B/C 三路线 → **必须 SKIP**（旧判据在这里会红）；
  2. 写出「路线：B」这种实际选择 → **必须 PASS**（不许为了修 1 而退化成永远 SKIP）。

运行：`.venv/bin/python -m pytest skills/web-to-openai-api/tests -q`
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
GATE = SKILL_DIR / "scripts" / "gate.py"

#: 只是**提到** recon，还明确说了没跑；绝不能算留档。
DESIGN_MENTION_ONLY = """# 设计

选择器集中在配置，错误码有文档，会话模型与契约见下。

## 路线
本轮没有对真实站点跑 `recon.py --network-log`，所以没有新的 A/B/C 路线结论。
三条路线（A 接口回放 / B DOM 自动化 / C 混合）的取舍见 skill 的 references/design.md。
"""

#: 真的做了决策：写明选了哪条。
DESIGN_DECIDED = """# 设计

选择器集中在配置，错误码有文档，会话模型与契约见下。

## 路线
- 路线：B DOM 自动化，依据：recon 报告里没有任何可复放的 JSON POST 接口。
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_project(root: Path, design_md: str) -> None:
    write(root / "app" / "__init__.py", "")
    write(root / "app" / "config.py", 'TIMEOUT = 3\n')
    write(root / "README.md", "# 最小项目\n")
    write(root / "doc" / "design.md", design_md)
    write(root / "tests" / "test_smoke.py", "def test_nothing():\n    assert True\n")


def run_gate_design_phase(root: Path, tmp_path: Path) -> dict:
    """跑真 `gate.py --phase design`，从 JSON 里取 D4 的判词。"""
    out = tmp_path / f"gate-{root.name}.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), "--phase", "design", "--json", str(out)],
        cwd=str(root),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    payload = json.loads(out.read_text(encoding="utf-8"))
    return next(r for r in payload["results"] if r["gate"] == "D4")


def test_mentioning_recon_is_not_a_route_record(tmp_path: Path) -> None:
    """「本轮没跑 recon」这种否定陈述不得让 D4 PASS（旧判据在这种写法下会假 PASS）。"""
    root = tmp_path / "mention"
    build_project(root, DESIGN_MENTION_ONLY)
    d4 = run_gate_design_phase(root, tmp_path)
    assert d4["status"] == "SKIP", f"D4 把「提到 recon」当成了路线留档：{d4['evidence']}"


def test_explicit_route_choice_is_recorded(tmp_path: Path) -> None:
    """写了「路线：B」→ D4 必须 PASS，并点名是哪份文档（不许退化成永远 SKIP）。"""
    root = tmp_path / "decided"
    build_project(root, DESIGN_DECIDED)
    d4 = run_gate_design_phase(root, tmp_path)
    assert d4["status"] == "PASS", f"D4 漏报真实的路线留档：{d4['evidence']}"
    assert "design.md" in d4["evidence"], d4["evidence"]
