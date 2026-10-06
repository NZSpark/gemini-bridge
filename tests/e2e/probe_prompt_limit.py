"""真实探测「prompt 多长会让 Gemini 网页版失去响应」的 e2e 探测脚本。

目的：给 `PROMPT_MAX_CHARS` 找一个**实测依据**（该值此前是 100000 的拍脑袋上界，
见 doc/update.md §15），并验证「多条 tool 结果拼接」这条真实路径会不会把
prompt 顶到上限。

策略：对**正在运行的服务**（默认 127.0.0.1:8001）发 `/v1/chat/completions`
非流式请求，每个用例独占一个会话桶（新页面 + 干净会话）；记录
「HTTP 状态 / 耗时 / 回复内容 / 是否答对标记问题」。

判定与用途：
  * 能正常答出标记问题 -> 该长度可用；
  * 超时 / 报错 / 答不出 -> 该长度不可用（这是「失去响应」的复现）。
  * `tools-5x20k` 用例刻意构造 5 条 20KB 的工具结果（每条都在
    `TOOL_RESULT_MAX_CHARS` 之内，但合计超过 `PROMPT_MAX_CHARS`）：
    用来验证「单条有上限、总量没预算」这条缺口。

用法（真实登录 profile 已被别的实例占用时也安全——本脚本只发 HTTP）：
    .venv/bin/python -m tests.e2e.probe_prompt_limit
    PROBE_BASE_URL=http://127.0.0.1:8001 PROBE_CASES=filler-20k,tools-5x20k ...
结果写入 output/prompt_limit_probe.txt。

注意：会真实驱动网页会话（每个用例一条 Gemini 消息），请勿在额度紧张时整跑。
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gemini_web import config  # noqa: E402
from tests.e2e.bridge import BridgeClient  # noqa: E402

BASE_URL = os.environ.get("PROBE_BASE_URL", "http://127.0.0.1:8001")
REQUEST_TIMEOUT_S = float(os.environ.get("PROBE_TIMEOUT_S", "300"))
OUT_PATH = Path("output/prompt_limit_probe.txt")

FILLER_UNIT = "The quick brown fox jumps over the lazy dog. "
FILLER_QUESTION = "以上是背景资料。请用一句中文回答：这段资料里反复出现的是哪种动物？"
# 答案关键词：命中即认为模型真的读到了正文（不是空回复 / 到顶提示）
FILLER_ANSWER = ("狐狸", "fox", "Fox")

# 工具结果用例：每份“文件”首行带唯一标记，便于判断哪几份在拼装后存活
TOOL_RESULT_CHARS = 20000
TOOL_RESULT_COUNT = 5
TOOL_QUESTION = (
    "上面是几份文件的内容。请逐行列出每一份文件的首行标记"
    "（形如 FILE-<编号>-FIRST-LINE），有几份就列几行，不要解释。"
)


def _filler(chars: int) -> str:
    repeats = chars // len(FILLER_UNIT) + 1
    return (FILLER_UNIT * repeats)[:chars]


def _filler_case(chars: int) -> List[Dict[str, Any]]:
    return [{"role": "user", "content": _filler(chars) + "\n\n" + FILLER_QUESTION}]


def _tool_result(text_len: int, index: int) -> str:
    head = f"FILE-{index}-FIRST-LINE\n"
    body_unit = f"FILE-{index} body line.\n"
    repeats = (text_len - len(head)) // len(body_unit) + 1
    return (head + body_unit * repeats)[:text_len]


def _tools_case() -> List[Dict[str, Any]]:
    messages: List[Dict[str, Any]] = [
        {"role": "user", "content": TOOL_QUESTION},
        {"role": "assistant", "content": "TOOL_CALL: read"},
    ]
    messages.extend(
        {
            "role": "tool",
            "tool_call_id": f"call_{i}",
            "content": _tool_result(TOOL_RESULT_CHARS, i),
        }
        for i in range(1, TOOL_RESULT_COUNT + 1)
    )
    return messages


def _repeat_case(turns: int, chars: int) -> List[List[Dict[str, Any]]]:
    """同一会话里连续发 turns 条 chars 字符的消息——验证「累积」效应。

    单次长 prompt 已实测能用（20K/60K/100K 都答对了），所以「太长的 prompt
    导致失去响应」如果为真，最可能的形式是**网页会话被累计灌长**：每轮都合规，
    但对话总量无上限（SESSION_MAX_TOKENS 在 .env 里是 1000000，实际上从不触发轮转）。

    :return: 每轮各自的 messages（依次发往同一个会话桶）
    """
    out: List[List[Dict[str, Any]]] = []
    for index in range(1, turns + 1):
        messages = _filler_case(chars)
        messages[0]["content"] = (
            f"（第 {index} 轮）\n" + messages[0]["content"]
        )
        out.append(messages)
    return out


# 每个用例的取值：单轮 = List[messages]；多轮 = List[List[messages]]（同一个桶里依次发）
CASES: Dict[str, Any] = {
    "filler-20k": lambda: _filler_case(20_000),
    "filler-60k": lambda: _filler_case(60_000),
    "filler-100k": lambda: _filler_case(100_000),
    "tools-5x20k": _tools_case,
    "repeat-60k-x3": lambda: _repeat_case(3, 60_000),
}


def _raw_prompt_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        total += len(content) if isinstance(content, str) else 0
    return total


def _turn_ok(name: str, reply: str) -> tuple[bool, str]:
    if name.startswith("filler-") or name.startswith("repeat-"):
        ok = bool(reply) and any(token in reply for token in FILLER_ANSWER)
        return ok, ("回复命中动物关键词" if ok else "回复未命中关键词（视为不可用）")
    seen = sorted({n for n in range(1, TOOL_RESULT_COUNT + 1)
                   if f"FILE-{n}-FIRST-LINE" in reply})
    ok = len(seen) == TOOL_RESULT_COUNT
    return ok, f"命中首行标记 {seen}/{list(range(1, TOOL_RESULT_COUNT + 1))}"


def run_case(client: BridgeClient, name: str) -> Dict[str, Any]:
    case = CASES[name]()
    turns: List[List[Dict[str, Any]]] = case if case and isinstance(case[0], list) else [case]
    key = f"probe-len-{name}"
    print(
        f"\n[probe] 用例 {name}：{len(turns)} 轮，单轮原始消息 "
        f"{_raw_prompt_chars(turns[0])} 字符 -> 桶 {key}", flush=True,
    )
    rows = [_run_turn(client, name, key, index, messages)
            for index, messages in enumerate(turns, start=1)]
    head = rows[-1]
    return {
        "case": name,
        "raw_chars": _raw_prompt_chars(turns[0]),
        "status": head["status"],
        "elapsed_s": head["elapsed_s"],
        "ok": all(r["ok"] for r in rows),
        "detail": " | ".join(
            f"第{r['turn']}轮 {r['elapsed_s']}s {'OK' if r['ok'] else 'FAIL'}" for r in rows
        ),
        "reply_head": head["reply_head"],
        "error": next((r["error"] for r in rows if r.get("error")), None),
        "turns": rows,
    }


def _run_turn(
    client: BridgeClient,
    name: str,
    key: str,
    index: int,
    messages: List[Dict[str, Any]],
) -> Dict[str, Any]:
    started = time.monotonic()
    status = 0
    body: Any = None
    try:
        status, body = client.chat(messages, session=key)
    except Exception as exc:  # noqa: BLE001  连接中断 / 读超时
        body = {"error": f"{type(exc).__name__}: {exc}"}
    elapsed = time.monotonic() - started

    reply = ""
    error = None
    if isinstance(body, dict):
        choices = body.get("choices") or []
        if choices:
            reply = ((choices[0].get("message") or {}).get("content") or "")
        error = body.get("error")
    else:
        error = str(body)[:400]

    ok, detail = _turn_ok(name, reply)

    row = {
        "turn": index,
        "case": name,
        "status": status,
        "elapsed_s": round(elapsed, 1),
        "ok": ok,
        "detail": detail,
        "reply_head": reply[:200].replace("\n", " "),
        "error": None if status == 200 else str(error)[:300] if error else None,
    }
    print(
        f"[probe] 用例 {name} 第 {index} 轮：status={status} 耗时={row['elapsed_s']}s "
        f"可用={ok}（{detail}）", flush=True,
    )
    if row["reply_head"]:
        print(f"[probe]   回复片段：{row['reply_head'][:120]!r}", flush=True)
    if row["error"]:
        print(f"[probe]   错误：{row['error']}", flush=True)
    return row


def _resolve_base_url() -> str:
    return BASE_URL


def main() -> int:
    client = BridgeClient(_resolve_base_url(), config.SESSION_KEY_HEADER)
    probe = client.healthz()
    if probe is None or probe[0] != 200:
        print(f"[probe] 服务不可达：{BASE_URL}/healthz -> {probe}", flush=True)
        print("[probe] 请先启动服务（本脚本不自己拉起，避免抢浏览器 profile）。", flush=True)
        return 2
    print(f"[probe] 服务可用：{json.dumps(probe[1], ensure_ascii=False)[:200]}", flush=True)

    wanted: Optional[List[str]] = None
    raw_cases = os.environ.get("PROBE_CASES", "").strip()
    if raw_cases:
        wanted = [c.strip() for c in raw_cases.split(",") if c.strip()]
    names = wanted or list(CASES)
    unknown = [n for n in names if n not in CASES]
    if unknown:
        print(f"[probe] 未知用例：{unknown}（可选：{list(CASES)}）", flush=True)
        return 2

    rows: List[Dict[str, Any]] = []
    for name in names:
        rows.append(run_case(client, name))
        time.sleep(1.0)

    usable = [r for r in rows if r["ok"]]
    biggest = max(usable, key=lambda r: r["raw_chars"]) if usable else None
    lines = ["# prompt 长度真机探测（/v1/chat/completions 非流式）", f"# base_url={BASE_URL}",
             ""]
    for row in rows:
        lines.append(
            f"{row['case']:>14} | 单轮原始 {row['raw_chars']:>7} 字符 | status={row['status']} "
            f"| 可用={row['ok']} | {row['detail']}"
        )
        for turn in row.get("turns") or []:
            lines.append(
                f"{'':>14} | 第{turn['turn']}轮 status={turn['status']} "
                f"{turn['elapsed_s']:>6}s 可用={turn['ok']} {turn['detail']}"
            )
        if row["error"]:
            lines.append(f"{'':>14} | 错误：{row['error']}")
    if biggest:
        lines.append("")
        lines.append(f"# 实测可用的最大原始长度：{biggest['raw_chars']} 字符（用例 {biggest['case']}）")
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[probe] 结果已写入 {OUT_PATH}", flush=True)
    for line in lines:
        print(f"[probe] {line}", flush=True)
    return 0 if all(r["ok"] for r in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
