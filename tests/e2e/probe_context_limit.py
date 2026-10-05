"""真实探测 Gemini 网页版「最大可用上下文」的 e2e 探测脚本。

目的：为 `SESSION_MAX_TOKENS` 找出一个**真实可用**的数值。
单位与桥接层一致：`gemini_web.prompting.estimate_tokens`（不是 Gemini 的真实 token）。

策略（见用户要求）：
  * 从 1000000 起测；可用即采用。
  * 不可用则每次乘 0.8，直到找到第一个可用值。

判定「可用 / 不可用」：
  * 新开对话 -> 灌入 `target_tokens` 估算量的内容 -> 问一个极短问题。
  * 若页面出现 `CAP_NOTICE_PATTERNS`（含 `context length limit` 等）
    或回复明显是到顶提示 -> 不可用（overflow）。
  * 若拿到正常回复 -> 可用。

用法（真实登录 profile 已就绪时）：
    python -m tests.e2e.probe_context_limit
结果写入 output/context_limit_probe.txt，并打印建议的 SESSION_MAX_TOKENS。

注意：本脚本会真实驱动网页会话、产生大量输入，可能耗时数分钟并触发风控，
默认不纳入 pytest 自动收集（文件名不以 test_ 开头）。
"""

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.prompting import estimate_tokens  # noqa: E402
from tests.e2e.direct import DirectGeminiClient  # noqa: E402

START_TOKENS = int(os.environ.get("PROBE_START_TOKENS", "1000000"))
RATIO = 0.8
MIN_TOKENS = 20_000
PROBE_QUESTION = "请只回复两个字：收到"
# 单轮输入用可读文本填充；用 ASCII 时约 4 char/token。
FILLER_UNIT = "The quick brown fox jumps over the lazy dog. "


def _chars_for_tokens(target_tokens: int) -> int:
    """按 estimate_tokens 的规则（非 CJK 约 4 char/token）反推所需字符数。"""
    return max(1, target_tokens * 4)


def _build_prompt(target_tokens: int) -> str:
    chars = _chars_for_tokens(target_tokens)
    repeats = chars // len(FILLER_UNIT) + 1
    filler = (FILLER_UNIT * repeats)[:chars]
    return filler + "\n\n" + PROBE_QUESTION


_CAP_RES = [re.compile(p, re.IGNORECASE) for p in config.CAP_NOTICE_PATTERNS]


def _is_overflow(reply: str) -> bool:
    if not reply:
        return False
    return any(rx.search(reply) for rx in _CAP_RES)


def probe_once(client: DirectGeminiClient, target_tokens: int) -> bool:
    """返回 True 表示该 est_tokens 可用（未 overflow）。"""
    prompt = _build_prompt(target_tokens)
    actual = estimate_tokens(prompt)
    print(f"[probe] target={target_tokens} est_tokens={actual} chars={len(prompt)}", flush=True)
    try:
        reply = client.ask(prompt, new_chat=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[probe] 发送/接收异常，按不可用处理：{exc}", flush=True)
        return False
    if _is_overflow(reply):
        print("[probe] 检测到到顶/overflow 提示，不可用", flush=True)
        return False
    print(f"[probe] 正常回复片段：{reply[:60]!r}", flush=True)
    return True


def find_max_usable(user_data_dir: str, headless: bool = True) -> int:
    client = DirectGeminiClient(user_data_dir=user_data_dir, headless=headless)
    client.start()
    try:
        target = START_TOKENS
        last_ok = None
        while target >= MIN_TOKENS:
            if probe_once(client, target):
                last_ok = target
                break
            target = int(target * RATIO)
            time.sleep(1.0)
        if last_ok is None:
            raise RuntimeError(f"从 {START_TOKENS} 降到 {MIN_TOKENS} 仍无可用的上下文值")
        return last_ok
    finally:
        client.close()


def main() -> int:
    user_data_dir = str(Path(config.USER_DATA_DIR).resolve())
    print(f"[probe] 使用 profile：{user_data_dir}")
    value = find_max_usable(user_data_dir, headless=config.HEADLESS)
    out = Path("output/context_limit_probe.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        f"SESSION_MAX_TOKENS={value}\n"
        f"# est_tokens 单位；起始 {START_TOKENS}，步进 ×{RATIO}\n",
        encoding="utf-8",
    )
    print(f"\n[probe] 最大可用值（est_tokens）= {value}")
    print(f"[probe] 已写入 {out}")
    print(f"建议 .env：SESSION_MAX_TOKENS={value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
