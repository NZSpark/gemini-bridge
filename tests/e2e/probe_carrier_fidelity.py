"""真实探测：工具调用载体（```tool_call 围栏 vs 历史纯文本行）在 Gemini 网页版
「渲染 → DOM 取回」这一跳里是否保得住内容。

背景与完整论证见 `doc/code_block_fence.md`：纯文本 ``TOOL_CALL: {...}`` 行会被网页版
当 markdown **段落**渲染（CommonMark 消费反斜杠转义、HTML 折叠连续空白），代码块内部
则按字面保留。本脚本用**运输层回声**（让模型逐字节搬运一段给定 payload，它只做搬运、
不做创作）来隔离载体变量：

    * 不带 ``tools``：避免 bridge 注入自己的格式指令，污染「模型听谁」这一变量；
    * 两侧 payload 完全相同，只有载体指令不同。

用法（真实登录 profile 已就绪、bridge 已在跑）：

    GEMINI_E2E=1 .venv/bin/python -m tests.e2e.probe_carrier_fidelity

结果写入 ``output/carrier_fidelity_probe.txt``。
默认不被 pytest 收集（文件名不以 test_ 开头）；会真实发起 2 次网页请求（约 1 分钟）。
"""

import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.toolcalls import _iter_balanced_objects, parse_tool_calls  # noqa: E402
from tests.e2e.bridge import BridgeClient, BridgeServer, PROJECT_ROOT  # noqa: E402

# 与 doc/code_block_fence.md §6.2 同一个 88 字节 payload：`\"`×2、`\n`×2、4/8 空格缩进。
PAYLOAD = r'{"name":"bash","arguments":{"command":"printf \"hi\"; echo done\n    x = 1\n        y = 2"}}'
COMMAND = 'printf "hi"; echo done\n    x = 1\n        y = 2'
VALID_NAMES = {"bash"}

PORT = int(os.environ.get("E2E_PORT") or config.PORT or 8001)
BASE_URL = f"http://127.0.0.1:{PORT}"

PROMPT_LINE = (
    "下面这段文本是一条工具调用记录，载体是**纯文本行**。请把它原样输出为一行纯文本："
    "首行前缀 TOOL_CALL: ，其后紧跟 JSON 本体；不要加代码围栏，不要改动任何字符、"
    "空格、换行转义或反斜杠。\n\n" + PAYLOAD
)
PROMPT_FENCE = (
    "下面这段文本是一条工具调用记录，载体是**代码围栏**。请把一个 info string 为 "
    "tool_call 的围栏原样输出，围栏内只有这一行 JSON；不要改动任何字符、空格、"
    "换行转义或反斜杠。\n\n```tool_call\n" + PAYLOAD + "\n```"
)
# 候选混合载体：标记行走纯文本（只为“可识别”），JSON 走代码围栏（只为“逐字节保真”）。
PROMPT_HYBRID = (
    "下面这段文本是一条工具调用记录。请分两部分原样输出：先单独一行纯文本 "
    "TOOL_CALL: ，然后另起一个 info string 为 tool_call 的代码围栏，围栏内只有这一行 "
    "JSON；不要改动任何字符、空格、换行转义或反斜杠。\n\n```tool_call\n" + PAYLOAD + "\n```"
)
# 对照臂：已知语言的围栏，用来回答“围栏的 info string 到底会不会进 DOM”。
PROMPT_CONTROL = (
    "请原样输出下面这段内容，用一个 info string 为 python 的代码围栏包住，"
    "不要改动任何字符：\n\n```python\nprint('hi')\n```"
)


def _json_candidate(text: str) -> str:
    for obj in _iter_balanced_objects(text):
        return obj
    return ""


def _measure(label: str, text: str) -> Dict[str, Any]:
    """两项是分开的：运输层保真（严格 json.loads）与解析器实际交付（含修复启发式）。"""
    candidate = _json_candidate(text)
    try:
        parsed = json.loads(candidate)
    except Exception as exc:  # noqa: BLE001
        parsed = f"FAIL: {exc}"
    command = ""
    if isinstance(parsed, dict):
        command = ((parsed.get("arguments") or {}).get("command") or "")
    calls = parse_tool_calls(text, VALID_NAMES)
    parsed_command = (calls[0]["arguments"].get("command") or "") if calls else ""
    runs = re.findall(r" {2,}", text)
    return {
        "label": label,
        "first_line": (text.strip().splitlines() or [""])[0][:40],
        "bytes": len(text.encode("utf-8")),
        "escaped_quotes": text.count('\\"'),
        "four_space_runs": len(re.findall(r" {4}", text)),
        "max_space_run": max((len(r) for r in runs), default=0),
        "strict_json": "OK" if isinstance(parsed, dict) else str(parsed),
        "strict_command_ok": command == COMMAND,
        "parsed_calls": len(calls),
        "parsed_command_ok": parsed_command == COMMAND,
        "raw": text,
    }


def _report(rows: List[Dict[str, Any]]) -> str:
    out = ["载体保真探测（运输层回声；不带 tools，bridge 不注入格式指令）", ""]
    out.append(f"payload: {len(PAYLOAD.encode('utf-8'))} bytes | "
               f'escaped-quotes={PAYLOAD.count(chr(92) + chr(34))} | '
               f"4-space runs={len(re.findall(r' {4}', PAYLOAD))} | "
               f"json.loads={'OK' if json.loads(PAYLOAD) else 'FAIL'}")
    out.append("")
    keys = ["label", "bytes", "escaped_quotes", "four_space_runs", "max_space_run",
            "first_line", "strict_json", "strict_command_ok", "parsed_calls",
            "parsed_command_ok"]
    out.append("| " + " | ".join(keys) + " |")
    out.append("| " + " | ".join("---" for _ in keys) + " |")
    for row in rows:
        out.append("| " + " | ".join(str(row[k]) for k in keys) + " |")
    for row in rows:
        out.append("")
        out.append(f"--- {row['label']} 原文 ---")
        out.append(row["raw"])
    return "\n".join(out)


def main() -> int:
    if os.environ.get("GEMINI_E2E") != "1":
        print("需要 GEMINI_E2E=1（本脚本会真实访问 Gemini 网页版）")
        return 2

    server = BridgeServer(BASE_URL)
    if server.healthz(timeout=3.0) is None:
        print(f"bridge 未运行（{BASE_URL}），请先启动服务或设 E2E_PORT")
        return 2

    client = BridgeClient(BASE_URL, config.SESSION_KEY_HEADER)
    rows: List[Dict[str, Any]] = []
    arms = [
        ("line", PROMPT_LINE, "probe-carrier-line"),
        ("fence", PROMPT_FENCE, "probe-carrier-fence"),
        ("hybrid", PROMPT_HYBRID, "probe-carrier-hybrid"),
        ("control", PROMPT_CONTROL, "probe-carrier-control"),
    ]
    only = os.environ.get("PROBE_ARMS")
    if only:
        wanted = {name.strip() for name in only.split(",")}
        arms = [arm for arm in arms if arm[0] in wanted]
    for label, prompt, session in arms:
        try:
            status, body = client.chat(
                [{"role": "user", "content": prompt}], session=session
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[{label}] 请求失败：{exc}")
            continue
        if status != 200 or not isinstance(body, dict):
            print(f"[{label}] HTTP {status}: {str(body)[:300]}")
            continue
        text = ((body.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rows.append(_measure(label, text))
        print(f"[{label}] 已取回 {len(text)} 字符", flush=True)

    report = _report(rows)
    print("\n" + report)
    out_path = PROJECT_ROOT / "output" / "carrier_fidelity_probe.txt"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report + "\n", encoding="utf-8")
    print(f"\n写入 {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
