"""GeminiBridge 的 HTTP 客户端与服务生命周期（标准库实现，无额外依赖）。

* :class:`BridgeServer` —— `/healthz` 探活；不通则以子进程拉起 uvicorn，
  并在 tearDown 时**只清理自己拉起的进程**（复用外部服务时不杀）。
* :class:`BridgeClient` —— OpenAI 兼容端点封装：非流式 / chat SSE / Responses SSE，
  统一带 `X-Gemini-Session` 分桶头。
"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
STARTUP_TIMEOUT_S = 90.0  # 浏览器冷启动可能要十几秒


class BridgeServer:
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.proc: Optional[subprocess.Popen] = None
        self.started_by_us = False
        self.log_path = Path(tempfile.gettempdir()) / "gemini_e2e_uvicorn.log"
        self._log_file = None

    def healthz(self, timeout: float = 3.0) -> Optional[Tuple[int, Dict[str, Any]]]:
        """返回 (status, body)；服务不可达返回 None。"""
        try:
            req = urllib.request.Request(self.base_url + "/healthz", method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, {}
        except Exception:  # noqa: BLE001
            return None

    def ensure_started(self) -> bool:
        """确保服务可用。返回 True 表示由本测试拉起（tearDown 需要清理）。"""
        probe = self.healthz(timeout=2.0)
        if probe is not None:
            self.started_by_us = False
            return False

        host = os.environ.get("HOST") or "127.0.0.1"
        port = self.base_url.rsplit(":", 1)[-1]
        self._log_file = open(self.log_path, "w", encoding="utf-8")
        self.proc = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn",
                "gemini_api_server:app",
                "--host", host,
                "--port", port,
            ],
            cwd=str(PROJECT_ROOT),
            stdout=self._log_file,
            stderr=subprocess.STDOUT,
        )
        self.started_by_us = True

        deadline = time.monotonic() + STARTUP_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(
                    f"uvicorn 启动即退出（exit={self.proc.returncode}），日志：{self.log_path}"
                )
            probe = self.healthz(timeout=2.0)
            if probe and probe[0] == 200:
                return True
            time.sleep(1.0)
        raise RuntimeError(f"bridge {STARTUP_TIMEOUT_S:.0f}s 内未就绪，日志：{self.log_path}")

    def stop(self) -> None:
        if self.proc is not None and self.started_by_us:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None


class BridgeClient:
    def __init__(self, base_url: str, session_header: str, timeout_s: float = 300.0):
        self.base_url = base_url
        self.session_header = session_header
        self.timeout_s = timeout_s

    # ---------- 底层 ----------

    def _headers(self, session: Optional[str]) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if session:
            headers[self.session_header] = session
        return headers

    def request(
        self,
        method: str,
        path: str,
        payload: Optional[Dict[str, Any]] = None,
        session: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> Tuple[int, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path, data=data, method=method,
            headers=self._headers(session),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout_s) as resp:
                body = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8")
            status = exc.code
        try:
            return status, json.loads(body)
        except Exception:  # noqa: BLE001
            return status, body

    def _open_stream(self, path: str, payload: Dict[str, Any], session: Optional[str]):
        req = urllib.request.Request(
            self.base_url + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            method="POST",
            headers=self._headers(session),
        )
        return urllib.request.urlopen(req, timeout=self.timeout_s)

    # ---------- 端点 ----------

    def healthz(self) -> Tuple[int, Any]:
        return self.request("GET", "/healthz")

    def models(self) -> Tuple[int, Any]:
        return self.request("GET", "/v1/models")

    def chat(
        self,
        messages: List[Dict[str, Any]],
        *,
        session: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: str = "gemini-chat",
        **extra: Any,
    ) -> Tuple[int, Any]:
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if tools:
            payload["tools"] = tools
        payload.update(extra)
        return self.request("POST", "/v1/chat/completions", payload, session=session)

    def chat_stream(
        self,
        messages: List[Dict[str, Any]],
        *,
        session: Optional[str] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        model: str = "gemini-chat",
        **extra: Any,
    ) -> Dict[str, Any]:
        """解析 chat SSE：返回 chunks / 拼接文本 / keep-alive 计时等。"""
        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if tools:
            payload["tools"] = tools
        payload.update(extra)

        result: Dict[str, Any] = {
            "status": 0, "chunks": [], "text": "", "keepalives": 0,
            "first_data_s": None, "elapsed_s": 0.0, "raw_text": "", "done": False,
        }
        started = time.monotonic()
        try:
            resp = self._open_stream("/v1/chat/completions", payload, session)
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            result["raw_text"] = exc.read().decode("utf-8")
            result["elapsed_s"] = time.monotonic() - started
            return result

        with resp:
            result["status"] = resp.status
            pieces: List[str] = []
            raw_lines: List[str] = []
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                raw_lines.append(line)
                if not line:
                    continue
                if line.startswith(":"):
                    result["keepalives"] += 1
                    continue
                if not line.startswith("data: "):
                    continue
                data_str = line[6:]
                if data_str == "[DONE]":
                    result["done"] = True
                    break
                try:
                    chunk = json.loads(data_str)
                except Exception:  # noqa: BLE001
                    continue
                if result["first_data_s"] is None:
                    result["first_data_s"] = time.monotonic() - started
                result["chunks"].append(chunk)
                for choice in chunk.get("choices") or []:
                    content = (choice.get("delta") or {}).get("content")
                    if content:
                        pieces.append(content)
            result["text"] = "".join(pieces)
            result["raw_text"] = "\n".join(raw_lines)
        result["elapsed_s"] = time.monotonic() - started
        return result

    def responses_stream(
        self,
        input_text: str,
        *,
        session: Optional[str] = None,
        model: str = "gemini-chat",
        **extra: Any,
    ) -> Dict[str, Any]:
        """解析 Responses 命名 SSE：返回事件名序列与数据载荷。"""
        payload: Dict[str, Any] = {"model": model, "input": input_text, "stream": True}
        payload.update(extra)
        result: Dict[str, Any] = {
            "status": 0, "events": [], "names": [], "raw_text": "",
            "first_event_s": None, "elapsed_s": 0.0,
        }
        started = time.monotonic()
        try:
            resp = self._open_stream("/v1/responses", payload, session)
        except urllib.error.HTTPError as exc:
            result["status"] = exc.code
            result["raw_text"] = exc.read().decode("utf-8")
            result["elapsed_s"] = time.monotonic() - started
            return result

        with resp:
            result["status"] = resp.status
            current_event: Optional[str] = None
            raw_lines: List[str] = []
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").rstrip("\n")
                raw_lines.append(line)
                if line.startswith("event: "):
                    current_event = line[7:].strip()
                elif line.startswith("data: "):
                    try:
                        data = json.loads(line[6:])
                    except Exception:  # noqa: BLE001
                        current_event = None
                        continue
                    name = current_event or data.get("type") or "?"
                    if result["first_event_s"] is None:
                        result["first_event_s"] = time.monotonic() - started
                    result["names"].append(name)
                    result["events"].append({"name": name, "data": data})
                    current_event = None
            result["raw_text"] = "\n".join(raw_lines)
        result["elapsed_s"] = time.monotonic() - started
        return result

    def reset_session(self, session: str) -> Tuple[int, Any]:
        return self.request("POST", f"/session/reset?session={session}", None)
