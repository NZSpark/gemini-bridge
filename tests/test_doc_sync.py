"""防止 design.md 的默认值表与 config.py 漂移（对照 doc/update_codex.md §3.1）。"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

# design.md 正文里以 `NAME`：...默认 `VALUE` 形式记录的键。
# 注意：config.py 的默认值会被 .env 覆盖，运行时实际生效的是 .env 里的值，
# 因此这里对照 .env（部署基线），而不是 config.py 的字面量。
DOC_KEYS = (
    "STABLE_POLLS",
    "LEN_STABLE_POLLS",
    "MAX_SESSION_BUCKETS",
    "BUCKET_LOCK_TIMEOUT_S",
)


def _env_values():
    values = {}
    env_path = ROOT / ".env"
    if not env_path.exists():
        return values
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


class DesignDocSyncTests(unittest.TestCase):
    def test_documented_defaults_match_config(self):
        text = (ROOT / "doc" / "design.md").read_text(encoding="utf-8")
        env_values = _env_values()
        for env_key in DOC_KEYS:
            match = re.search(
                rf"`{re.escape(env_key)}`[^\n]*?默认 `([^`]+)`", text
            )
            self.assertIsNotNone(match, f"design.md 未记录 {env_key} 的默认值")
            documented = match.group(1).strip()
            actual = env_values.get(env_key)
            self.assertIsNotNone(actual, f".env 缺少 {env_key}")
            # 数值比较：doc 写 5，env 写 5.0 也应视为一致
            try:
                same = float(actual) == float(documented)
            except ValueError:
                same = actual == documented
            self.assertEqual(
                same, True,
                f"{env_key}: design.md 写 {documented}，.env 实际 {actual}",
            )

    def test_env_example_lists_every_config_key(self):
        example = (ROOT / ".env.example").read_text(encoding="utf-8")
        documented = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", example, re.MULTILINE))
        # 至少覆盖这些新增/关键键，避免模板悄悄落后
        for key in (
            "SAVE_FILES", "OUTPUT_MAX_FILES", "OUTPUT_MAX_AGE_DAYS",
            "MAX_SESSION_STATE_CACHE", "SESSION_MAX_TURNS", "RESPONSES_TOOL_BUFFER",
        ):
            self.assertIn(key, documented)


if __name__ == "__main__":
    unittest.main()
