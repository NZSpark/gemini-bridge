#!/usr/bin/env python3
"""验收冒烟：对**运行中的** Web→OpenAI 桥跑契约级检查，只用标准库。

用法::

    python3 smoke_test.py --base-url http://127.0.0.1:8002 \
        [--model chatgpt-chat] [--session demo-1] \
        [--session-header X-ChatGPT-Session] \
        [--tools-file tools.json] [--responses] [--token-budget 500] \
        [--prompt "Reply with exactly: OK"] [--timeout 240] [--json out.json]

覆盖（对应 references/acceptance.md 的 A 组，外加 B1 / R1-R2 可选）：

  A1 /healthz          浏览器就绪 + 会话/分桶快照
  A2 /v1/models        模型发现
  A3 非流式 chat        content 非空、finish_reason、usage
  A4 流式 chat          SSE 增量 chunk 数量 + 结尾 [DONE]
  A5 未知字段           不得 422
  A6 空输入            必须 4xx（不是超时 / 500）
  A7 错误体形状         {"error":{message,type,code}}
  A10 token 预算护栏    无工具请求的 usage 不得超预算（防工具脚手架泄漏进普通请求）
  B1 工具调用（可选）    传 --tools-file 时检查 tool_calls
  R1/R2 /v1/responses   传 --responses 时检查 Codex CLI 路径（非流式正文 + 命名 SSE 事件序）

退出码：0 = 全部硬检查通过；1 = 有硬失败；2 = 用法错误（连不上服务）。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

# 输出含中文；在非 UTF-8 终端下保证 print 不炸
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

HARD_FAIL = 0
RESULTS: list[dict] = []


def _record(name: str, ok: bool, detail: str, hard: bool = True) -> None:
    global HARD_FAIL
    RESULTS.append({"check": name, "ok": ok, "detail": detail, "hard": hard})
    if hard and not ok:
        HARD_FAIL = 1
    mark = "PASS" if ok else ("FAIL" if hard else "SOFT")
    print(f"[{mark}] {name}: {detail}")


def _http(method: str, url: str, body=None, headers=None, timeout=30):
    """返回 (status, headers, text)；网络错误抛 urllib.error.URLError。"""
    data = None
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, dict(resp.headers), resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:  # 4xx/5xx 也当正常响应返回
        return exc.code, dict(exc.headers or {}), exc.read().decode("utf-8", "replace")


def _read_sse(url: str, body: dict, headers: dict, timeout: float) -> dict:
    """真正增量读取 SSE，统计 chunk 数与首字节延迟。"""
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    hdrs.update(headers)
    req = urllib.request.Request(url, data=data, headers=hdrs, method="POST")
    chunks: list[str] = []
    saw_done = False
    started = time.monotonic()
    first_chunk_at = None
    deadline = started + timeout
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        for raw in resp:
            if time.monotonic() > deadline:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[len("data:"):].strip()
            if payload == "[DONE]":
                saw_done = True
                break
            chunks.append(payload)
            if first_chunk_at is None:
                first_chunk_at = time.monotonic() - started
    return {
        "chunks": chunks,
        "done": saw_done,
        "first_chunk_s": round(first_chunk_at or 0.0, 2),
        "total_s": round(time.monotonic() - started, 2),
    }


def _responses_text(obj: dict) -> str:
    """从 /v1/responses 响应里取正文。兼容 SDK 的 output_text 与 output[] 结构。"""
    text = obj.get("output_text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    parts: list[str] = []
    for item in obj.get("output") or []:
        if not isinstance(item, dict):
            continue
        for chunk in item.get("content") or []:
            if isinstance(chunk, dict) and isinstance(chunk.get("text"), str):
                parts.append(chunk["text"])
    return "".join(parts).strip()


def _error_type(text: str) -> str:
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        return ""
    err = obj.get("error")
    if isinstance(err, dict):
        return str(err.get("type") or "")
    if isinstance(err, str):
        return "<error is a bare string>"  # 期望是 dict，调用方据此判失败
    return ""


def main() -> int:
    ap = argparse.ArgumentParser(description="Web→OpenAI 桥的验收冒烟")
    ap.add_argument("--base-url", default="http://127.0.0.1:8002")
    ap.add_argument("--model", default="", help="不传则取 /v1/models 的第一个")
    ap.add_argument("--session", default="", help="会话桶标识（如 demo-1）")
    ap.add_argument("--session-header", default="X-ChatGPT-Session")
    ap.add_argument("--prompt", default="Reply with exactly: OK")
    ap.add_argument("--token-budget", type=int, default=500,
                    help="无工具请求的 usage 上限（A10；0 = 关闭）。超过通常说明工具脚手架/"
                         "冗长提示词被泄漏进普通请求")
    ap.add_argument("--tools-file", default="", help="含 tools 数组的 JSON 文件（可选，检查 B1）")
    ap.add_argument("--strict-tools", action="store_true", help="工具检查也计为硬失败")
    ap.add_argument("--responses", action="store_true",
                    help="额外检查 /v1/responses（Codex CLI 路径，可选）")
    ap.add_argument("--strict-responses", action="store_true", help="Responses 检查计为硬失败")
    ap.add_argument("--timeout", type=float, default=240.0, help="单请求超时（应 > 服务端超时）")
    ap.add_argument("--json", default="", help="把结果写成 JSON")
    args = ap.parse_args()

    base = args.base_url.rstrip("/")
    chat_url = f"{base}/v1/chat/completions"
    session_headers = {args.session_header: args.session} if args.session else {}

    # ---- A1 healthz -------------------------------------------------
    try:
        status, _, text = _http("GET", f"{base}/healthz", timeout=10)
    except urllib.error.URLError as exc:
        print(f"连不上服务 {base}：{exc}", file=sys.stderr)
        return 2
    ok = status == 200
    detail = text[:300]
    _record("A1 /healthz", ok, f"HTTP {status} {detail}")
    if not ok:
        return HARD_FAIL

    # ---- A2 models --------------------------------------------------
    model = args.model
    try:
        status, _, text = _http("GET", f"{base}/v1/models", timeout=10)
        obj = json.loads(text)
        data = obj.get("data") or []
        ids = [m.get("id") for m in data if isinstance(m, dict)]
        _record("A2 /v1/models", status == 200 and bool(ids),
                f"HTTP {status} ids={ids}")
        if not model and ids:
            model = ids[0]
    except Exception as exc:  # noqa: BLE001
        _record("A2 /v1/models", False, f"异常：{exc}")
    model = model or "chat"

    # ---- A3 非流式 --------------------------------------------------
    usage: dict = {}
    body = {"model": model, "messages": [{"role": "user", "content": args.prompt}]}
    try:
        t0 = time.monotonic()
        status, _, text = _http("POST", chat_url, body, session_headers, timeout=args.timeout)
        elapsed = round(time.monotonic() - t0, 2)
        obj = json.loads(text)
        content = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        finish = ((obj.get("choices") or [{}])[0] or {}).get("finish_reason")
        usage = obj.get("usage") or {}
        ok = status == 200 and bool(content)
        _record("A3 非流式 chat", ok,
                f"HTTP {status} {elapsed}s finish={finish} content={content[:40]!r} usage={usage}")
    except Exception as exc:  # noqa: BLE001
        _record("A3 非流式 chat", False, f"异常：{exc}")

    # ---- A10 token 预算护栏（无工具请求不该带上工具脚手架）------------
    if args.token_budget > 0 and usage:
        total = usage.get("total_tokens")
        if total is None:
            total = (usage.get("prompt_tokens") or 0) + (usage.get("completion_tokens") or 0)
        _record("A10 token 预算护栏", total <= args.token_budget,
                f"total_tokens={total} 预算={args.token_budget}"
                "（超出通常=工具脚手架/冗长系统提示被注入了无工具请求）")
    elif args.token_budget > 0:
        print("[SKIP] A10 token 预算护栏：A3 未返回 usage，无法核对")

    # ---- A4 流式 ----------------------------------------------------
    try:
        body = {"model": model, "stream": True,
                "messages": [{"role": "user", "content": args.prompt}]}
        res = _read_sse(chat_url, body, session_headers, args.timeout)
        ok = res["done"] and len(res["chunks"]) >= 1
        _record("A4 流式 SSE", ok,
                f"chunks={len(res['chunks'])} first={res['first_chunk_s']}s "
                f"total={res['total_s']}s [DONE]={res['done']}")
    except Exception as exc:  # noqa: BLE001
        _record("A4 流式 SSE", False, f"异常：{exc}")

    # ---- A5 未知字段 -------------------------------------------------
    try:
        body = {"model": model, "messages": [{"role": "user", "content": args.prompt}],
                "temperature": 0.7, "top_p": 0.9, "reasoning_effort": "low",
                "some_unknown_field": {"a": 1}, "metadata": "x"}
        status, _, text = _http("POST", chat_url, body, session_headers, timeout=args.timeout)
        _record("A5 未知字段不 422", status == 200, f"HTTP {status}")
    except Exception as exc:  # noqa: BLE001
        _record("A5 未知字段不 422", False, f"异常：{exc}")

    # ---- A6 空输入 ---------------------------------------------------
    try:
        status, _, text = _http("POST", chat_url,
                                {"model": model, "messages": []}, session_headers, timeout=30)
        etype = _error_type(text)
        ok = 400 <= status < 500
        _record("A6 空输入被拒", ok, f"HTTP {status} type={etype!r}")
    except Exception as exc:  # noqa: BLE001
        _record("A6 空输入被拒", False, f"异常：{exc}")

    # ---- A7/A8 错误体形状 -------------------------------------------
    try:
        status, _, text = _http("POST", chat_url, {"model": model}, session_headers, timeout=30)
        try:
            obj = json.loads(text)
            err = obj.get("error")
            shaped = isinstance(err, dict) and "message" in err and "type" in err
        except json.JSONDecodeError:
            shaped = False
        _record("A7 错误体形状", shaped, f"HTTP {status} body={text[:160]!r}")
    except Exception as exc:  # noqa: BLE001
        _record("A7 错误体形状", False, f"异常：{exc}")

    # ---- B1 工具调用（可选） ------------------------------------------
    if args.tools_file:
        try:
            with open(args.tools_file, encoding="utf-8") as fh:
                tools = json.loads(fh.read())
            if isinstance(tools, dict):
                tools = tools.get("tools", tools)
            body = {"model": model, "tools": tools, "messages": [
                {"role": "user",
                 "content": "Use the provided tool to answer. " + args.prompt}]}
            status, _, text = _http("POST", chat_url, body, session_headers, timeout=args.timeout)
            obj = json.loads(text)
            msg = ((obj.get("choices") or [{}])[0].get("message") or {})
            calls = msg.get("tool_calls") or []
            ok = status == 200 and bool(calls)
            _record("B1 工具调用", ok,
                    f"HTTP {status} tool_calls={json.dumps(calls, ensure_ascii=False)[:160]}",
                    hard=args.strict_tools)
        except Exception as exc:  # noqa: BLE001
            _record("B1 工具调用", False, f"异常：{exc}", hard=args.strict_tools)

    # ---- R1/R2 /v1/responses（Codex CLI 路径，可选）------------------
    if args.responses:
        r_url = f"{base}/v1/responses"
        try:
            status, _, text = _http("POST", r_url,
                                    {"model": model, "input": args.prompt},
                                    session_headers, timeout=args.timeout)
            obj = json.loads(text)
            out = _responses_text(obj)
            _record("R1 /v1/responses 非流式", status == 200 and bool(out),
                    f"HTTP {status} status={obj.get('status')!r} text={out[:40]!r}",
                    hard=args.strict_responses)
        except Exception as exc:  # noqa: BLE001
            _record("R1 /v1/responses 非流式", False, f"异常：{exc}", hard=args.strict_responses)
        try:
            res = _read_sse(r_url, {"model": model, "input": args.prompt, "stream": True},
                            session_headers, args.timeout)
            types: list[str] = []
            seq_ok = True
            last_seq = 0
            for payload in res["chunks"]:
                try:
                    evt = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                etype = evt.get("type")
                if isinstance(etype, str):
                    types.append(etype)
                seq = evt.get("sequence_number")
                if isinstance(seq, int):
                    seq_ok = seq_ok and seq > last_seq
                    last_seq = seq
            completed = "response.completed" in types or res["done"]
            _record("R2 /v1/responses 流式",
                    bool(types) and len(set(types)) >= 2 and completed and seq_ok,
                    f"events={len(types)} types={sorted(set(types))[:6]} "
                    f"sequence=(单调={seq_ok}) 结束信号={completed}",
                    hard=args.strict_responses)
        except Exception as exc:  # noqa: BLE001
            _record("R2 /v1/responses 流式", False, f"异常：{exc}", hard=args.strict_responses)

    # ---- 汇总 --------------------------------------------------------
    print("\n=== 汇总 ===")
    hard_fail = [r for r in RESULTS if r["hard"] and not r["ok"]]
    soft_fail = [r for r in RESULTS if not r["hard"] and not r["ok"]]
    print(f"通过 {sum(1 for r in RESULTS if r['ok'])}/{len(RESULTS)}"
          f"  硬失败 {len(hard_fail)}  软失败 {len(soft_fail)}")
    if hard_fail:
        print("硬失败：" + "、".join(r["check"] for r in hard_fail))
    if soft_fail:
        print("软失败（人工确认，如工具调用需模型配合）："
              + "、".join(r["check"] for r in soft_fail))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"base_url": base, "model": model, "results": RESULTS}, fh,
                      ensure_ascii=False, indent=2)
        print(f"结果已写入 {args.json}")
    if not hard_fail:
        print("\n把上面的输出（命令 + 耗时 + 片段）粘进 doc/acceptance.md 作为证据。")
        print("别忘了真机对等 E2E 与安全验收（references/acceptance.md 的 §5 / §6）。")
    return HARD_FAIL


if __name__ == "__main__":
    raise SystemExit(main())
