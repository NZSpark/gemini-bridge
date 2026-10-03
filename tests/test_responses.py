"""Responses 流式事件：item id / call_id / output_index 一致性（对照 doc/update_codex.md §2.2）。"""

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from gemini_web import config  # noqa: E402
from gemini_web.models import ChatCompletionRequest, ChatMessage  # noqa: E402
from gemini_web.responses import stream_responses  # noqa: E402


def _collect(agen):
    async def run():
        return [item async for item in agen]

    return asyncio.run(run())


def _parse_events(lines):
    """把 SSE 文本行解析成 [(event_type, payload_dict)]，忽略注释保活行。"""
    events = []
    current = None
    for raw in lines:
        for line in raw.splitlines():
            if line.startswith("event: "):
                current = line[len("event: "):].strip()
            elif line.startswith("data: ") and current:
                events.append((current, json.loads(line[len("data: "):])))
                current = None
    return events


class FakeDriver:
    """最小 driver：needs_seed / send_chat / sent_prompt。"""

    def __init__(self, reply):
        self.reply = reply
        self.page = object()

    def needs_seed(self, key=None):
        return False

    def sent_prompt(self, key=None):
        return "prompt"

    async def send_chat(self, prompt, on_delta=None, seeded_prompt=None, key=None):
        if on_delta:
            await on_delta(self.reply)
        return self.reply, []


def _request(stream=True, tools=None):
    return ChatCompletionRequest(
        model="gemini-chat",
        messages=[ChatMessage(role="user", content="hi")],
        stream=stream,
        tools=tools,
    )


class StreamResponsesTextTests(unittest.TestCase):
    def _run(self, reply="plain text reply"):
        driver = FakeDriver(reply)
        req = _request(stream=True)
        lines = _collect(stream_responses(req, driver, None))
        return _parse_events(lines)

    def test_message_item_events_present_for_text(self):
        events = self._run()
        types = [t for t, _ in events]
        self.assertIn("response.output_item.added", types)
        self.assertIn("response.content_part.added", types)
        self.assertIn("response.output_text.done", types)
        self.assertIn("response.output_item.done", types)

    def test_message_item_id_consistent(self):
        events = self._run("hello world")
        added = next(p for t, p in events if t == "response.output_item.added")
        item_id = added["item"]["id"]
        deltas = [p for t, p in events if t == "response.output_text.delta"]
        self.assertTrue(all(d["item_id"] == item_id for d in deltas))
        done = next(p for t, p in events if t == "response.output_text.done")
        self.assertEqual(done["item_id"], item_id)
        item_done = next(p for t, p in events if t == "response.output_item.done")
        self.assertEqual(item_done["item"]["id"], item_id)

    def test_single_output_index_zero(self):
        events = self._run()
        indices = {
            p["output_index"] for t, p in events if "output_index" in p
        }
        self.assertEqual(indices, {0})


TOOL_REPLY = """TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "ls"}}
TOOL_CALL: {"name": "exec_command", "arguments": {"cmd": "pwd"}}"""

TOOLS = [{
    "type": "function",
    "function": {"name": "exec_command", "description": "run", "parameters": {}},
}]


class StreamResponsesToolTests(unittest.TestCase):
    def _run(self):
        driver = FakeDriver(TOOL_REPLY)
        req = _request(stream=True, tools=TOOLS)
        lines = _collect(stream_responses(req, driver, None))
        return _parse_events(lines)

    def test_no_message_item_when_tools(self):
        events = self._run()
        added = [p for t, p in events if t == "response.output_item.added"]
        self.assertTrue(added)
        self.assertTrue(all(p["item"]["type"] == "function_call" for p in added))

    def test_indices_unique_and_done_count_matches_added(self):
        events = self._run()
        added = [p for t, p in events if t == "response.output_item.added"]
        done = [p for t, p in events if t == "response.output_item.done"]
        self.assertEqual(len(added), 2)
        self.assertEqual(len(done), len(added))
        self.assertEqual(len({p["output_index"] for p in added}), len(added))
        self.assertEqual(
            {p["output_index"] for p in added},
            {p["output_index"] for p in done},
        )

    def test_item_and_call_ids_consistent_within_item(self):
        events = self._run()
        added = [p for t, p in events if t == "response.output_item.added"]
        for add in added:
            item_id = add["item"]["id"]
            call_id = add["item"]["call_id"]
            idx = add["output_index"]
            deltas = [
                p for t, p in events
                if t == "response.function_call_arguments.delta" and p["output_index"] == idx
            ]
            self.assertTrue(deltas)
            self.assertTrue(all(d["item_id"] == item_id for d in deltas))
            done = next(
                p for t, p in events
                if t == "response.output_item.done" and p["output_index"] == idx
            )
            self.assertEqual(done["item"]["id"], item_id)
            self.assertEqual(done["item"]["call_id"], call_id)

    def test_completed_output_matches_added_ids(self):
        events = self._run()
        completed = next(p for t, p in events if t == "response.completed")
        output = completed["response"]["output"]
        added = [p for t, p in events if t == "response.output_item.added"]
        self.assertEqual(len(output), len(added))
        self.assertEqual(
            {item["id"] for item in output},
            {p["item"]["id"] for p in added},
        )


class NonStreamingFromChatTests(unittest.TestCase):
    def test_tool_calls_shape(self):
        from gemini_web.responses import from_chat_response

        resp = from_chat_response(
            "", "gemini-chat", 3,
            tool_calls=[{"name": "exec_command", "arguments": {"cmd": "ls"}}],
        )
        self.assertEqual(resp["output"][0]["type"], "function_call")
        self.assertIn("call_id", resp["output"][0])


if __name__ == "__main__":
    unittest.main()
