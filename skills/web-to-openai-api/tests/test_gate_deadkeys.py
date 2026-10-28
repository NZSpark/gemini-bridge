"""C2「死键」判据的回归测试（离线，用临时目录造一个最小项目 + 跑真 gate.py）。

为什么这两个方向都必须钉住：canonical 自己就写着
`MAX_UPSTREAM_RETRIES = env_int("CHATGPT_RETRIES", 2)` —— **环境变量名 ≠ 属性名**是家族里的正常写法。
旧判据「在配置包之外找键名字面量」会把这种正常接线报成死键（DeepseekBridge 实测被误报 2 个），
而误报比漏报更坏：它会骗人去删掉一个**正在生效**的配置项。
但反过来，如果判据放松成「只要 env_* 读过就算」，这条检查就退化成永远 PASS —— 同样没有价值。

所以：
  1. 键名只在配置模块里、绑定的属性被读 → **不许**报死键（`test_bound_attribute_read`）；
  2. 键读进来了却没有任何读取路径 → **必须**报死键（`test_no_reader`）。
把第 1 条对着旧判据跑必须变红 —— 那是这次修复的区分力证明。

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

CONFIG_PY = '''"""最小配置模块：演示「环境变量名 ≠ 属性名」的接线方式。"""

import os


def env_int(key: str, default: int) -> int:
    return int(os.environ.get(key, default))


def env_float(key: str, default: float) -> float:
    return float(os.environ.get(key, default))


def env_str(key: str, default: str) -> str:
    return os.environ.get(key, default)


RESPONSE_TIMEOUT_S = env_float("DEMO_TIMEOUT", 180)
MAX_UPSTREAM_RETRIES = env_int("DEMO_RETRIES", 2)
BUTTON_TEXTS = [
    t for t in env_str("DEMO_TEXTS", "a||b").split("||")
]
'''

ROUTES = 'ROUTES = ("/healthz", "/v1/models", "/v1/chat/completions")\n'

USER_WIRED = "from app import config as cfg\n\n" + ROUTES + '''

def runtime_settings():
    """属性被读了 —— 三个 env 键都因此有了生效路径。"""
    return cfg.RESPONSE_TIMEOUT_S, cfg.MAX_UPSTREAM_RETRIES, cfg.BUTTON_TEXTS
'''

USER_DEAD = "from app import config as cfg\n\n" + ROUTES + '''

def runtime_settings():
    """谁都没读那些属性 —— 三个 env 键都是死键。"""
    return None
'''

ENV_EXAMPLE = "DEMO_TIMEOUT=180\nDEMO_RETRIES=2\nDEMO_TEXTS=a||b\n"


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build_project(root: Path, *, wired: bool) -> None:
    write(root / "app" / "__init__.py", "")
    write(root / "app" / "config.py", CONFIG_PY)
    write(root / "app" / "user.py", USER_WIRED if wired else USER_DEAD)
    write(root / ".env.example", ENV_EXAMPLE)
    write(root / "README.md", "# 最小项目\n")
    write(root / "tests" / "test_smoke.py", "def test_nothing():\n    assert True\n")


def run_gate_code_phase(root: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess, dict]:
    """跑真 `gate.py --phase code`，从它的 JSON 里取 C2 的判词（不看人眼输出）。"""
    out = tmp_path / "gate.json"
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, str(GATE), "--root", str(root), "--phase", "code", "--json", str(out)],
        cwd=str(root), capture_output=True, text=True, env=env,
    )
    # gate 的 JSON 形状：{"root": …, "phases": […], "results": [{gate, phase, title, status, evidence, hint}], "verdict": 0/1}
    payload = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {"results": []}
    c2 = next(r for r in payload["results"] if r["gate"] == "C2")
    return proc, c2


def test_bound_attribute_read_is_not_a_dead_key(tmp_path: Path) -> None:
    """键名只出现在配置模块里，但绑定的属性被读 → C2 必须 PASS，整段门禁退出码 0。"""
    root = tmp_path / "wired"
    build_project(root, wired=True)
    proc, c2 = run_gate_code_phase(root, tmp_path)
    assert c2["status"] == "PASS", f"C2 误报正常接线：{c2['evidence']}"
    assert proc.returncode == 0, f"② 编码阶段本应全绿：{proc.stdout[-800:]}"


def test_env_key_without_any_reader_is_still_dead(tmp_path: Path) -> None:
    """键被 env_* 读进来了、但绑定的属性谁都没读 → C2 必须 FAIL 并点名（不许退化成永远 PASS）。"""
    root = tmp_path / "dead"
    build_project(root, wired=False)
    proc, c2 = run_gate_code_phase(root, tmp_path)
    assert c2["status"] == "FAIL", f"C2 漏报死键：{c2['evidence']}"
    assert "DEMO_TIMEOUT" in c2["evidence"], c2["evidence"]
    assert proc.returncode == 1, "有 FAIL 时门禁退出码必须是 1"
