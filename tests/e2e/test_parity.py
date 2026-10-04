"""E2E 对等测试：Playwright 直连 Gemini vs GeminiBridge，比较结果判定功能正确。

判定矩阵（doc/e2e_test_design.md §1.2）：直连是"上游能力基线"——
直连达标而 bridge 不达标 = 代码缺陷（FAIL）；双侧都不达标 = 环境问题（SKIP）。

运行（真实访问 Gemini，串行，约 15~25 分钟）：

    GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_parity -v

开关：E2E_HEADED=1 直连浏览器可见；E2E_PORT 改 bridge 端口；E2E_FULL=1 启用 B8。
未设置 GEMINI_E2E 时全部 skip，常规测试套件不受影响、不发起网络请求。
"""

import json
import os
import shutil
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from gemini_web import config
from gemini_web.models import ChatMessage
from gemini_web.prompting import build_prompt

from .bridge import BridgeClient, BridgeServer, PROJECT_ROOT
from .direct import DirectGeminiClient

GATE = os.environ.get("GEMINI_E2E") == "1"
E2E_FULL = os.environ.get("E2E_FULL") == "1"
E2E_HEADED = os.environ.get("E2E_HEADED") == "1"
PORT = int(os.environ.get("E2E_PORT") or config.PORT)
BASE_URL = f"http://127.0.0.1:{PORT}"
MODEL_ID = "gemini-chat"

PROFILE = (PROJECT_ROOT / config.USER_DATA_DIR).resolve()
PROFILE_E2E = PROFILE.parent / (PROFILE.name + "_e2e")

SERVER: Optional[BridgeServer] = None
BRIDGE: Optional[BridgeClient] = None
DIRECT: Optional[DirectGeminiClient] = None

# (case_id, side, seconds) —— tearDownModule 打印汇总
_TIMINGS: List[Tuple[str, str, float]] = []


def _record(case_id: str, side: str, seconds: float) -> None:
    _TIMINGS.append((case_id, side, seconds))


def _copy_profile() -> None:
    """把原 profile 复制给直连浏览器（两个 Chromium 不能共享同一目录）。"""
    if not PROFILE.exists():
        raise unittest.SkipTest(
            f"缺少登录目录 {PROFILE}——请先完成 T4.2 真实登录（HEADLESS=false 手动登录）"
        )
    if PROFILE_E2E.exists():
        shutil.rmtree(PROFILE_E2E, ignore_errors=True)
    if PROFILE_E2E.exists():
        raise unittest.SkipTest(f"无法刷新 profile 副本 {PROFILE_E2E}（是否被占用？）")
    shutil.copytree(
        PROFILE, PROFILE_E2E, ignore=shutil.ignore_patterns('Singleton*', 'RunningChromeVersion')
    )
    # 清掉从原目录带过来的 Chromium 单实例锁，否则副本起不来
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        path = PROFILE_E2E / name
        try:
            path.unlink()
        except OSError:
            pass


def _ensure_direct() -> DirectGeminiClient:
    """懒加载直连浏览器：只跑 B 组协议用例时无需启动它。"""
    global DIRECT
    if DIRECT is None:
        client = DirectGeminiClient(str(PROFILE_E2E), headless=not E2E_HEADED)
        try:
            client.start()
        except Exception as exc:  # noqa: BLE001
            raise unittest.SkipTest(f"直连浏览器启动失败（环境问题）：{exc}") from exc
        DIRECT = client
    return DIRECT


def setUpModule() -> None:
    if not GATE:
        return
    _copy_profile()

    global SERVER, BRIDGE
    SERVER = BridgeServer(BASE_URL)
    SERVER.ensure_started()  # 起不来 = 真实故障，直接报错并附日志路径

    status, body = SERVER.healthz()
    if status != 200 or not isinstance(body, dict) or body.get("init_error"):
        raise unittest.SkipTest(
            f"bridge 浏览器未就绪（status={status}, body={body}）——"
            "多半是登录态/profile 问题，属环境问题"
        )
    BRIDGE = BridgeClient(BASE_URL, config.SESSION_KEY_HEADER)


def tearDownModule() -> None:
    if DIRECT is not None:
        DIRECT.close()
    if SERVER is not None:
        SERVER.stop()
    if _TIMINGS:
        print("\n[E2E 耗时汇总]（bridge/direct 比值 > 3 视为可疑，仅提示）")
        by_case: Dict[str, Dict[str, float]] = {}
        for case_id, side, secs in _TIMINGS:
            by_case.setdefault(case_id, {})[side] = secs
        for case_id, sides in by_case.items():
            line = "  ".join(f"{side}={secs:.1f}s" for side, secs in sides.items())
            ratio = ""
            if "direct" in sides and "bridge" in sides and sides["direct"] > 0:
                r = sides["bridge"] / sides["direct"]
                ratio = f"  ratio={r:.2f}" + ("  <-- WARNING" if r > 3 else "")
            print(f"  {case_id:<8} {line}{ratio}")


def _cjk_ratio(text: str) -> float:
    if not text:
        return 0.0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿" or "　" <= ch <= "ヿ")
    return cjk / len(text)


# 统一工具定义（C 组用）
TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询指定城市的当前天气",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string", "description": "城市名"}},
                "required": ["city"],
            },
        },
    }
]


@unittest.skipUnless(GATE, "E2E 需 GEMINI_E2E=1（真实访问 Gemini，见 doc/e2e_test_design.md）")
class E2ECase(unittest.TestCase):
    maxDiff = None

    # ---------- 辅助 ----------

    _UPSTREAM_TYPES = {"timeout", "upstream_error", "context_length_exceeded"}

    @property
    def direct(self) -> DirectGeminiClient:
        return _ensure_direct()

    @property
    def bridge(self) -> BridgeClient:
        return BRIDGE  # type: ignore[return-value]

    @staticmethod
    def _user(text: str) -> Dict[str, Any]:
        return {"role": "user", "content": text}

    def guard_upstream(self, status: int, body: Any) -> None:
        """上游/环境类失败（超时、上游错误、本地繁忙）→ SKIP，不算 bridge 缺陷。"""
        if status in (502, 503, 504):
            raise unittest.SkipTest(f"上游/环境问题：HTTP {status} {str(body)[:300]}")
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict) and err.get("type") in self._UPSTREAM_TYPES:
                raise unittest.SkipTest(f"上游/环境问题：{str(body)[:300]}")

    def chat_or_skip(self, prompt: str, *, session: str, **kw: Any) -> Tuple[int, Any]:
        """发一轮非流式请求；上游类失败直接 SKIP。"""
        status, raw = self.bridge.chat([self._user(prompt)], session=session, **kw)
        self.guard_upstream(status, raw)
        return status, raw

    @staticmethod
    def _content_of(data: Any) -> str:
        if not isinstance(data, dict):
            return ""
        try:
            return data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            return ""

    def run_parity(
        self,
        case_id: str,
        prompt: str,
        predicate: Callable[[str], bool],
        *,
        retries: int = 1,
        need_direct: bool = True,
    ) -> Tuple[str, str, Any]:
        """执行"直连 + bridge"两侧请求并按基线原则判定；返回 (direct, bridge, raw)。"""
        # ---- 直连侧（基线）----
        d_text = ""
        if need_direct:
            for attempt in range(1, 2 + retries):
                t0 = time.monotonic()
                try:
                    d_text = self.direct.ask(prompt)
                except Exception as exc:  # noqa: BLE001
                    if attempt > retries:
                        raise unittest.SkipTest(
                            f"{case_id}: 直连访问失败（环境问题）：{exc}"
                        ) from exc
                    continue
                _record(case_id, "direct", time.monotonic() - t0)
                if predicate(d_text):
                    break
            if not predicate(d_text):
                raise unittest.SkipTest(
                    f"{case_id}: 直连基线重试后仍不达标（环境/上游问题，非 bridge 缺陷）。"
                    f"原文开头：{d_text[:200]!r}"
                )

        # ---- bridge 侧（被测对象）----
        b_text = ""
        raw: Any = {}
        for attempt in range(1, 2 + retries):
            session = f"e2e-{case_id}-r{attempt}"
            t0 = time.monotonic()
            status, raw = self.bridge.chat([self._user(prompt)], session=session)
            self.guard_upstream(status, raw)
            _record(case_id, "bridge", time.monotonic() - t0)
            b_text = self._content_of(raw) if status == 200 else f"<HTTP {status}> {raw}"
            if predicate(b_text):
                break
        if not predicate(b_text):
            self.fail(
                f"{case_id}: 直连基线达标，但 bridge 不达标（代码缺陷）\n"
                f"--- direct({len(d_text)} chars) ---\n{d_text[:800]}\n"
                f"--- bridge({len(b_text)} chars) ---\n{b_text[:800]}"
            )
        return d_text, b_text, raw

    def assert_chat_schema(self, raw: Any) -> None:
        """B3：非流式响应的 OpenAI 结构断言。"""
        self.assertIsInstance(raw, dict, f"响应不是 JSON 对象：{raw!r}")
        self.assertTrue(str(raw.get("id", "")).startswith("chatcmpl-"), raw.get("id"))
        self.assertEqual(raw.get("object"), "chat.completion")
        self.assertEqual(raw.get("model"), MODEL_ID)
        choices = raw.get("choices")
        self.assertTrue(choices, "choices 为空")
        choice = choices[0]
        self.assertIn(choice.get("finish_reason"), {"stop", "tool_calls"})
        message = choice.get("message") or {}
        self.assertEqual(message.get("role"), "assistant")
        self.assertIsInstance(message.get("content"), str)
        usage = raw.get("usage")
        self.assertIsInstance(usage, dict)
        self.assertGreaterEqual(usage.get("total_tokens", -1), 0)
        # B5（行为无关）：saved_files 一致性——声明了的文件必须真实存在
        saved = raw.get("saved_files")
        self.assertIsInstance(saved, list, "saved_files 应为列表")
        for path in saved:
            p = Path(path)
            if not p.is_absolute():
                p = PROJECT_ROOT / p
            self.assertTrue(p.exists(), f"saved_files 声称存在但磁盘上没有：{path}")

    def assert_no_injection_leak(self, text: str) -> None:
        """A6：注入块不得泄漏进模型回复（抓错节点时会整段带出 prompt）。"""
        for token in ("[上下文重建]", "[工具调用说明]", "[任务状态]", "TOOL_CALL"):
            self.assertNotIn(token, text, f"回复中泄漏了注入块 {token!r}")


class TestAContentParity(E2ECase):
    """组 A：内容对等（双侧同断言，期望答案为独立 ground truth）。"""

    def test_a1_sentinel_parity(self):
        prompt = "请只回复这四个字符：K7Q9，不要输出任何其他内容。"
        d_text, b_text, raw = self.run_parity(
            "a1", prompt, lambda t: "K7Q9" in t
        )
        self.assert_chat_schema(raw)              # B3（复用同一次请求）
        self.assert_no_injection_leak(b_text)     # A6
        # 语言一致性不断言在此：哨兵回复本身是 ASCII；中文占比断言见 A4 长文

    def test_a2_fact_parity(self):
        prompt = "法国的首都会是哪座城市？只回答城市名。"
        self.run_parity("a2", prompt, lambda t: "巴黎" in t or "paris" in t.lower())

    def test_a4_longform_tail_sentinel(self):
        prompt = "请写一篇约600字的中文短文，主题是「一座桥」。要求：最后一行单独输出 END7。"
        d_text, b_text, _ = self.run_parity(
            "a4", prompt, lambda t: len(t) >= 300 and "END7" in t[-120:]
        )
        # 截断检测：bridge 与直连的长度应在同一量级（波动 vs 截断可区分）
        ratio = len(b_text) / max(len(d_text), 1)
        self.assertGreaterEqual(
            ratio, 0.4, f"bridge 回复明显偏短（疑似结束判定过早截断）：ratio={ratio:.2f}"
        )
        self.assertLessEqual(ratio, 2.5, f"bridge 回复明显偏长（疑似重复/串台）：ratio={ratio:.2f}")
        for name, text in (("direct", d_text), ("bridge", b_text)):
            self.assertGreaterEqual(
                _cjk_ratio(text), 0.3,
                f"{name} 长文不像中文（可能抓错节点）：{text[:120]!r}",
            )


class TestBProtocol(E2ECase):
    """组 B：协议正确性（静态规范，不依赖直连基线）。"""

    def test_b1_healthz(self):
        status, body = self.bridge.healthz()
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "ok")
        self.assertTrue(body.get("browser_ready"))
        self.assertIn("cluster", body)
        self.assertIn("session_keys", body)
        self.assertIsNone(body.get("init_error"))

    def test_b2_models(self):
        status, body = self.bridge.models()
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("object"), "list")
        ids = [m.get("id") for m in body.get("data") or []]
        self.assertIn(MODEL_ID, ids)

    def test_b4_stream_sequence(self):
        prompt = "请只回复这四个字符：S7R9，不要输出任何其他内容。"
        t0 = time.monotonic()
        res = self.bridge.chat_stream([self._user(prompt)], session="e2e-b4")
        _record("b4", "bridge", time.monotonic() - t0)
        self.guard_upstream(res["status"], res["raw_text"][:500])
        self.assertEqual(res["status"], 200, res["raw_text"][:500])
        chunks = res["chunks"]
        self.assertTrue(chunks, "流式无任何数据 chunk")

        first_delta = (chunks[0].get("choices") or [{}])[0].get("delta") or {}
        self.assertEqual(first_delta.get("role"), "assistant", "首 chunk 缺 role")

        ids = {c.get("id") for c in chunks}
        self.assertEqual(len(ids), 1, f"流式 id 不唯一：{ids}")
        created = {c.get("created") for c in chunks}
        self.assertEqual(len(created), 1, f"流式 created 不唯一：{created}")

        finishes = [
            (c.get("choices") or [{}])[0].get("finish_reason")
            for c in chunks
            if (c.get("choices") or [{}])[0].get("finish_reason")
        ]
        self.assertIn("stop", finishes, "缺少 finish_reason=stop 的收尾 chunk")
        self.assertTrue(res["done"], "缺少 data: [DONE] 收尾")
        self.assertIn("S7R9", res["text"], f"流拼接文本缺哨兵：{res['text'][:200]!r}")

        # 软观测：首字节耗时与 keep-alive（不判失败，仅记录）
        print(
            f"\n[b4 观测] first_data={res['first_data_s']}s "
            f"keepalives={res['keepalives']} elapsed={res['elapsed_s']:.1f}s"
        )

    def test_b6_responses_events(self):
        t0 = time.monotonic()
        res = self.bridge.responses_stream("请只回复 OK6", session="e2e-b6")
        _record("b6", "bridge", time.monotonic() - t0)
        self.guard_upstream(res["status"], res["raw_text"][:500])
        self.assertEqual(res["status"], 200, res["raw_text"][:500])
        names = res["names"]
        self.assertTrue(names, "无任何 Responses 事件")
        self.assertTrue(names[0].startswith("response."), names[0])
        self.assertIn("response.output_text.done", names, names)
        self.assertIn("response.completed", names, names)

        # sequence_number 必须严格递增（Codex 依赖事件顺序）
        seqs = [
            ev["data"].get("sequence_number")
            for ev in res["events"]
            if isinstance(ev["data"].get("sequence_number"), int)
        ]
        self.assertEqual(seqs, sorted(seqs), "sequence_number 非递增")
        self.assertEqual(len(seqs), len(res["events"]), "部分事件缺 sequence_number")

        completed = next(
            ev["data"] for ev in res["events"] if ev["name"] == "response.completed"
        )
        output = (completed.get("response") or {}).get("output") or []
        self.assertTrue(output, "completed.output 为空")
        text = ""
        item = output[0]
        if item.get("type") == "message":
            parts = item.get("content") or []
            text = "".join(p.get("text", "") for p in parts if isinstance(p, dict))
        self.assertIn("OK6", text, f"completed 输出缺哨兵：{text[:200]!r}")

    @unittest.skipUnless(E2E_FULL, "B8 需 E2E_FULL=1（openai SDK 联调）")
    def test_b8_openai_sdk_smoke(self):
        try:
            from openai import OpenAI
        except ImportError as exc:  # pragma: no cover
            raise unittest.SkipTest(f"openai 未安装：{exc}")
        client = OpenAI(base_url=BASE_URL + "/v1", api_key="e2e", timeout=300.0)
        t0 = time.monotonic()
        resp = client.chat.completions.create(
            model=MODEL_ID,
            messages=[{"role": "user", "content": "请只回复 SDK9"}],
        )
        _record("b8", "bridge", time.monotonic() - t0)
        self.assertIn("SDK9", resp.choices[0].message.content or "")


class TestCToolParity(E2ECase):
    """组 C：工具调用对等（直连=同源指令的上游能力基线）。"""

    USER_PROMPT = "请先调用 get_weather 工具查询北京的天气，然后再根据结果作答。"

    @staticmethod
    def _tool_predicate(text: str) -> bool:
        upper = text.upper()
        return "TOOL_CALL" in upper and "GET_WEATHER" in upper

    def test_c1_tool_call_parity(self):
        # 直连：与 bridge 同源的工具指令（build_prompt 是共享的注入逻辑）
        prompt_text = build_prompt(
            [ChatMessage(role="user", content=self.USER_PROMPT)], tools=TOOLS
        )
        d_text = ""
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                d_text = self.direct.ask(prompt_text)
            except Exception as exc:  # noqa: BLE001
                raise unittest.SkipTest(f"c1: 直连失败（环境问题）：{exc}") from exc
            _record("c1", "direct", time.monotonic() - t0)
            if self._tool_predicate(d_text):
                break
        if not self._tool_predicate(d_text):
            raise unittest.SkipTest(
                "c1: 直连侧模型未输出工具调用标记（上游不配合，非 bridge 缺陷）。"
                f"原文开头：{d_text[:200]!r}"
            )

        # bridge：解析出的 tool_calls 必须与基线一致
        t0 = time.monotonic()
        status, raw = self.chat_or_skip(self.USER_PROMPT, session="e2e-c1", tools=TOOLS)
        _record("c1", "bridge", time.monotonic() - t0)
        self.assertEqual(status, 200, raw)
        message = (raw.get("choices") or [{}])[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []
        self.assertTrue(
            tool_calls,
            "直连基线已产生 TOOL_CALL 标记，但 bridge 解析不出 tool_calls"
            f"（DOM 提取/解析缺陷）。message={json.dumps(message, ensure_ascii=False)[:600]}",
        )
        first = tool_calls[0]
        self.assertEqual(first.get("function", {}).get("name"), "get_weather")
        args = json.loads(first.get("function", {}).get("arguments") or "{}")
        self.assertIsInstance(args, dict)

    def test_c2_tool_call_stream(self):
        res = self.bridge.chat_stream(
            [self._user(self.USER_PROMPT)], tools=TOOLS, session="e2e-c2"
        )
        _record("c2", "bridge", res["elapsed_s"])
        self.guard_upstream(res["status"], res["raw_text"][:500])
        self.assertEqual(res["status"], 200, res["raw_text"][:500])

        names: List[str] = []
        arg_parts: List[str] = []
        finishes: List[str] = []
        for chunk in res["chunks"]:
            for choice in chunk.get("choices") or []:
                if choice.get("finish_reason"):
                    finishes.append(choice["finish_reason"])
                delta = choice.get("delta") or {}
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        names.append(fn["name"])
                    if fn.get("arguments"):
                        arg_parts.append(fn["arguments"])

        self.assertIn("tool_calls", finishes, f"缺少 finish_reason=tool_calls：{finishes}")
        self.assertIn("get_weather", names, f"流式未收到工具名：{names}")
        args = json.loads("".join(arg_parts) or "{}")
        self.assertIsInstance(args, dict, f"流式 arguments 拼接后不可解析：{arg_parts!r}")


class TestDSession(E2ECase):
    """组 D：会话与上下文（分桶 / 多轮 / 重置播种）。"""

    def test_d1_multiturn_memory(self):
        turn1 = "请记住暗号 Zebra-42。只回复：已记住。"
        turn2 = "刚才的暗号是什么？只输出暗号本身。"
        predicate = lambda t: "Zebra-42" in t  # noqa: E731

        # 直连基线：原生多轮会话
        d_text = ""
        for attempt in range(2):
            t0 = time.monotonic()
            try:
                self.direct.ask(turn1)
                d_text = self.direct.ask(turn2)
            except Exception as exc:  # noqa: BLE001
                raise unittest.SkipTest(f"d1: 直连失败（环境问题）：{exc}") from exc
            _record("d1", "direct", time.monotonic() - t0)
            if predicate(d_text):
                break
        if not predicate(d_text):
            raise unittest.SkipTest(
                f"d1: 直连侧两轮后也记不住暗号（上游问题）。原文：{d_text[:200]!r}"
            )

        # bridge：同桶两轮（增量 prompt 链路）
        b_text = ""
        for attempt in range(1, 3):
            session = f"e2e-d1-r{attempt}"
            t0 = time.monotonic()
            status1, raw1 = self.chat_or_skip(turn1, session=session)
            status2, raw2 = self.chat_or_skip(turn2, session=session)
            _record("d1", "bridge", time.monotonic() - t0)
            self.assertEqual(status1, 200, raw1)
            b_text = self._content_of(raw2) if status2 == 200 else f"<HTTP {status2}> {raw2}"
            if predicate(b_text):
                break
        self.assertTrue(
            predicate(b_text),
            f"d1: 直连基线能记住，bridge 记不住（增量 prompt/会话链路缺陷）：{b_text[:400]!r}",
        )

    def test_d2_bucket_isolation(self):
        # 桶 A 存暗号；桶 B 无历史 → 不应看到（bridge 特有：分桶隔离）
        status, raw = self.chat_or_skip(
            "请记住暗号 Tiger-77。只回复：已记住。", session="e2e-d2a"
        )
        self.assertEqual(status, 200, raw)

        ask = (
            "如果你的对话历史中没有出现过暗号，请只回复 NONE；"
            "如果出现过，输出该暗号。"
        )
        b_text = ""
        for attempt in range(1, 3):
            status, raw = self.chat_or_skip(ask, session=f"e2e-d2b-r{attempt}")
            b_text = self._content_of(raw) if status == 200 else f"<HTTP {status}> {raw}"
            if "NONE" in b_text.upper() or "Tiger-77" in b_text:
                break
        self.assertNotIn(
            "Tiger-77", b_text,
            f"桶 B 看到了桶 A 的暗号（分桶隔离失效/上下文串台）：{b_text[:400]!r}",
        )
        self.assertIn("NONE", b_text.upper(), f"桶 B 行为异常：{b_text[:400]!r}")

    def test_d3_reset_then_seed_keeps_context(self):
        # 记暗号 -> /session/reset（触发下轮轮转+播种）-> 仍须记得
        turn1 = "请记住暗号 River-13。只回复：已记住。"
        turn2 = "刚才的暗号是什么？只输出暗号本身。"
        session = "e2e-d3"

        status, raw = self.chat_or_skip(turn1, session=session)
        self.assertEqual(status, 200, raw)

        status, body = self.bridge.reset_session(session)
        self.assertEqual(status, 200, body)
        self.assertEqual(body.get("status"), "ok")

        b_text = ""
        for attempt in range(1, 3):
            t0 = time.monotonic()
            status, raw = self.chat_or_skip(turn2, session=session)
            _record("d3", "bridge", time.monotonic() - t0)
            b_text = self._content_of(raw) if status == 200 else f"<HTTP {status}> {raw}"
            if "River-13" in b_text:
                break
        self.assertIn(
            "River-13", b_text,
            "重置后播种未保住上下文（新开对话+历史播种/任务快照链路缺陷）："
            f"{b_text[:400]!r}",
        )


if __name__ == "__main__":
    unittest.main()
