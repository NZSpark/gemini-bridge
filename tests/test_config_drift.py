"""配置防漂移：`config.py` 与 `.env.example` 必须**双向**对齐（tasks.md T7.3）。

旧版只检查“期望的几个键在模板里”，于是模板悄悄落后很久也没人发现，
而文档里的默认值漂移更是完全没有守护（见 doc/update.md P1-3）。
这里改为**双向**校验，新增配置键忘记写进模板就会立刻报错。
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = ROOT / ".env.example"
CONFIG_PY = ROOT / "gemini_web" / "config.py"

# 这些键由 config.py 用 env_* 读取；解析源码得到真实清单，不手工维护
_ENV_CALL_RE = re.compile(r'env_(?:str|int|float|bool)\(\s*"([A-Z0-9_]+)"')
_KEY_ASSIGN_RE = re.compile(r'^([A-Z0-9_]+)\s*=', re.MULTILINE)


def _config_env_keys() -> set:
    """config.py 里通过 env_* 读取的所有环境变量名。"""
    source = CONFIG_PY.read_text(encoding="utf-8")
    return set(_ENV_CALL_RE.findall(source))


def _example_keys() -> set:
    keys = set()
    for line in ENV_EXAMPLE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        keys.add(line.split("=", 1)[0].strip())
    return keys


def test_env_example_exists():
    assert ENV_EXAMPLE.exists(), ".env.example 必须存在"


def test_every_config_key_is_in_env_example():
    """config.py 能读的键，模板里都必须有一份（否则新用户从零起步会缺配置）。"""
    missing = sorted(_config_env_keys() - _example_keys())
    assert not missing, f".env.example 缺少以下配置键：{missing}"


def test_env_example_has_no_dead_keys():
    """模板里的键必须真的被 config.py 读取（避免留下“改了不生效”的死配置）。"""
    dead = sorted(_example_keys() - _config_env_keys())
    assert not dead, (
        f".env.example 存在 config.py 根本不读取的键：{dead}"
        "（这类死配置会让用户改了没反应，见 update.md P1-1）"
    )


def test_env_example_keys_are_uppercase_identifiers():
    bad = sorted(k for k in _example_keys() if not re.fullmatch(r"[A-Z][A-Z0-9_]*", k))
    assert not bad, f".env.example 存在非法键名：{bad}"


@pytest.mark.parametrize("key", [
    "WEBSITE", "OUTPUT_PRUNE_INTERVAL_S", "EDIT_MARKDOWN_ALWAYS_REGISTER",
    "BRIDGE_TOKEN", "EDIT_MARKDOWN_ROOT",
])
def test_newly_added_keys_are_documented(key):
    """本轮新增/生效的键必须出现在模板里（T7.1 / T7.2 / T7.6 / T8.9 / T8.10）。"""
    assert key in _example_keys()


# ==================== 依赖防漂移（T8.4）====================


def _requirement_name(spec: str) -> str:
    """``fastapi>=0.110,<1.0`` -> ``fastapi``（去掉版本区间 / extras / 环境标记）。"""
    return re.split(r"[<>=!;\[]", spec.strip(), maxsplit=1)[0].strip().lower()


def _requirements_txt_names() -> set:
    names = set()
    for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            names.add(_requirement_name(line))
    return names


def _pyproject() -> dict:
    tomllib = pytest.importorskip("tomllib", reason="Python <3.11 无标准库 TOML 解析")
    with open(ROOT / "pyproject.toml", "rb") as fh:
        return tomllib.load(fh)


def test_pyproject_declares_python_and_runtime_deps():
    data = _pyproject()["project"]
    assert data["requires-python"].startswith(">=3.10"), "必须声明最低支持的 Python 版本"
    deps = [_requirement_name(d) for d in data["dependencies"]]
    # 关键依赖必须有版本区间，而不是无约束（否则上游大版本会静默进来）
    for spec in data["dependencies"]:
        assert any(op in spec for op in (">=", "==", "~=")), f"{spec} 没有版本约束"
    for name in ("fastapi", "uvicorn", "playwright", "pydantic"):
        assert name in deps, f"pyproject 缺少运行时依赖 {name}"


def test_requirements_txt_matches_pyproject_runtime_deps():
    """requirements.txt 与 pyproject 的运行时依赖必须同一套名字，防止两边各写一份。"""
    pyproject_names = {_requirement_name(d) for d in _pyproject()["project"]["dependencies"]}
    assert _requirements_txt_names() == pyproject_names, (
        "requirements.txt 与 pyproject.toml 的运行时依赖不一致："
        f"requirements={sorted(_requirements_txt_names())} pyproject={sorted(pyproject_names)}"
    )


def test_openai_is_dev_only():
    """openai SDK 只是联调/示例用，不能是运行时依赖（否则给所有用户增加安装负担）。"""
    data = _pyproject()["project"]
    runtime = {_requirement_name(d) for d in data["dependencies"]}
    dev = {_requirement_name(d) for d in data["optional-dependencies"]["dev"]}
    assert "openai" not in runtime
    assert "openai" in dev
    assert "pytest" in dev
