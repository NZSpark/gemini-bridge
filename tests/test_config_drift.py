import pytest
from pathlib import Path
from gemini_web import config

def test_config_env_example_keys_match():
    """验证 .env.example 中定义的所有配置键与 config.py 中的配置属性完全一致，防止配置项漂移。"""
example_path = Path(".env.example")
assert example_path.exists(), ".env.example 必须存在"

env_keys = set()
for line in example_path.read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    if "=" in line:
        key = line.split("=", 1)[0].strip()
        env_keys.add(key)

# 检查核心配置变量是否都在 .env.example 中
expected_keys = {
    "HOST", "PORT", "HEADLESS", "GEMINI_DEBUG", "GEMINI_TIMEOUT",
    "POLL_INTERVAL_S", "STABLE_POLLS", "LEN_STABLE_POLLS", "MAX_SESSION_BUCKETS",
    "MAX_SESSION_STATE_CACHE", "ENABLE_RESPONSES_API", "TASK_SNAPSHOT_ENABLED",
    "OUTPUT_MAX_FILES", "OUTPUT_MAX_AGE_DAYS"
}

missing_keys = expected_keys - env_keys
assert not missing_keys, f".env.example 缺失以下配置项: {missing_keys}"


