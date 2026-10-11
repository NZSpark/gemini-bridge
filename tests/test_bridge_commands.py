"""Bridge 聊天命令（``/bridge ...``）：解析、执行、作用域与两条协议路径的一致性。

对照 ``doc/bridge_command.md`` §7 的测试清单：

* 合法完整命令被直接应答，**不触发浏览器生成**（``send_chat`` 全程不被调用）；
* 非法 / 缺失 / 多余参数与未知选项返回可理解的用法；
* 多行文本、代码块、引用或普通句子里的命令字样不会意外执行；
* 只检查最后一条用户消息（历史里的 ``/bridge session reset`` 不得每轮重放）；
* Chat Completions 与 Responses 两条路径行为一致；
* 指定 ``X-Gemini-Session`` 时只影响预期的会话桶；
* 浏览器未启动时只读命令仍能给出可靠状态，普通请求照旧 503；
* reseed / 落盘偏好的一次性标记跨重启不会丢失，也不会重复应用；
* 回复不泄露 ``.env``、令牌或其它桶的信息。

全部用假页 + 真 driver（不启浏览器、不联网）：命令本身不碰页面，只看会话状态。
"""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from gemini_web import bridge_commands, config, models, streaming  # noqa: E402
from gemini_web.driver import DEFAULT_SESSION_KEY, GeminiWebDriver  # noqa: E402
from gemini_web.models import ChatCompletionRequest, ChatMessage  # noqa: E402
from gemini_web.server import app  # noqa: E402


def _user(text: str):
    return [ChatMessage(role="user", content=text)]


class _BridgeTestBase(unittest.TestCase):
    """真 driver + 临时状态文件：命令测试不需要浏览器。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-command-")
        self._patch = mock.patch.object(
            config, "SESSION_FILE", Path(self.tmp.name) / "state.json"
        )
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self.addCleanup(self.tmp.cleanup)
        self.driver = GeminiWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))

    def run_command(self, text, session_key=None):
        return asyncio.run(
            bridge_commands.handle_command(_user(text), self.driver, session_key)
        )


# ==================== 命令清单与帮助 ====================


class HelpTests(_BridgeTestBase):
    def test_bare_bridge_is_help(self):
        reply = self.run_command("/bridge")
        self.assertIn("命令列表", reply)
        self.assertIn("/bridge status", reply)

    def test_help_lists_whole_registry(self):
        reply = self.run_command("/bridge help")
        for spec in bridge_commands.REGISTRY:
            self.assertIn(spec.syntax, reply, f"帮助里缺少 {spec.syntax}")

    def test_every_registered_syntax_parses_to_that_group(self):
        """帮助里列的命令必须**真能**被识别（防漂移），且归属的主题正确。"""
        for spec in bridge_commands.REGISTRY:
            probe = spec.syntax.split(" [")[0]  # 去掉可选参数段
            probe = probe.replace("<URL>", "https://example.test/x")
            command = bridge_commands.parse_command(probe)
            self.assertIsNotNone(command, f"{spec.syntax} 无法被识别")
            self.assertNotEqual(command.action, "invalid", spec.syntax)
            self.assertIn(spec.group, ("", *bridge_commands.HELP_TOPICS))
            if spec.group:
                self.assertEqual(
                    bridge_commands.parse_command(f"/bridge help {spec.group}").subaction,
                    spec.group,
                )

    def test_topic_help_filters_to_that_group(self):
        reply = self.run_command("/bridge help session")
        self.assertIn("/bridge session reset", reply)
        self.assertNotIn("/bridge settings", reply)

    def test_help_marks_readonly_and_stateful(self):
        reply = self.run_command("/bridge help")
        self.assertIn("只读", reply)
        self.assertIn("改状态", reply)

    def test_unknown_topic_returns_usage(self):
        reply = self.run_command("/bridge help nosuch")
        self.assertIn("没有这个帮助主题", reply)
        self.assertIn("用法", reply)

    def test_help_does_not_leak_config_secrets(self):
        with mock.patch.object(config, "RESET_TOKEN", "s3cret-token"), \
             mock.patch.object(config, "BRIDGE_TOKEN", "another-secret"):
            reply = self.run_command("/bridge help")
        self.assertNotIn("s3cret-token", reply)
        self.assertNotIn("another-secret", reply)


# ==================== 解析 ====================


class ParseTests(_BridgeTestBase):
    def test_recognizes_each_command(self):
        self.assertEqual(bridge_commands.parse_command("/bridge status").action, "status")
        self.assertEqual(bridge_commands.parse_command("/bridge models").action, "models")
        bare = bridge_commands.parse_command("/bridge session")
        self.assertEqual((bare.action, bare.subaction), ("session", "status"))
        self.assertEqual(
            bridge_commands.parse_command("/bridge session reset").subaction, "reset"
        )
        self.assertEqual(
            bridge_commands.parse_command("/bridge session reseed").subaction, "reseed"
        )
        setting = bridge_commands.parse_command("/bridge settings save-files on")
        self.assertEqual((setting.action, setting.name, setting.value),
                         ("settings", "save-files", True))

    def test_case_insensitive_namespace(self):
        self.assertIsNotNone(bridge_commands.parse_command("/Bridge status"))

    def test_extra_args_are_rejected_with_usage(self):
        for text in (
            "/bridge status extra",
            "/bridge models 1",
            "/bridge session status now",
            "/bridge session reset --force",
            "/bridge settings save-files on off",
            "/bridge help session settings",
        ):
            command = bridge_commands.parse_command(text)
            self.assertEqual(command.action, "invalid", text)
            self.assertIn("用法", command.reason, text)

    def test_unknown_subcommands_are_rejected(self):
        for text in (
            "/bridge nosuch",
            "/bridge session nosuch",
            "/bridge settings nosuch on",
        ):
            self.assertEqual(bridge_commands.parse_command(text).action, "invalid", text)

    def test_unsupported_setting_value_is_rejected(self):
        command = bridge_commands.parse_command("/bridge settings save-files maybe")
        self.assertEqual(command.action, "invalid")
        self.assertIn("不支持取值", command.reason)

    def test_link_commands_are_not_silently_implemented(self):
        """本项目不做网页会话绑定：ChatGPTBridge 的 link / unlink 必须明确报错。"""
        for text in (
            "/bridge session link https://gemini.google.com/app/x",
            "/bridge session unlink",
        ):
            self.assertEqual(bridge_commands.parse_command(text).action, "invalid", text)

    def test_plain_text_and_other_slash_commands_are_not_commands(self):
        for text in (
            "你好",
            "/reset",
            "/clear",
            "/help",
            "/bridgefoo",
            "请执行 /bridge status",
            "引用：> /bridge status",
        ):
            self.assertIsNone(bridge_commands.parse_command(text), text)

    def test_multiline_and_code_blocks_are_not_commands(self):
        for text in (
            "/bridge status\nmore",
            "```\n/bridge session reset\n```",
            "说明：\n/bridge status",
        ):
            self.assertIsNone(bridge_commands.parse_command(text), text)

    def test_surrounding_blank_lines_still_count_as_one_line(self):
        """整条消息只有命令、只是前后多了空行时仍算命令（与 ChatGPTBridge 一致）。"""
        self.assertEqual(bridge_commands.parse_command("\n\n/bridge status\n\n").action, "status")

    def test_is_command_only_for_last_user_message(self):
        self.assertTrue(bridge_commands.is_command(_user("/bridge status")))
        self.assertTrue(bridge_commands.is_command(_user("/bridge")))
        self.assertFalse(bridge_commands.is_command(_user("你好")))
        self.assertFalse(bridge_commands.is_command(_user("/bridge status\nmore")))
        # 历史里的旧命令不得被当成命令（只看最后一条）
        history = [
            ChatMessage(role="user", content="/bridge session reset"),
            ChatMessage(role="assistant", content="好的"),
            ChatMessage(role="user", content="继续"),
        ]
        self.assertFalse(bridge_commands.is_command(history))
        # 最后一条不是 user 消息时也不是命令
        self.assertFalse(
            bridge_commands.is_command([ChatMessage(role="assistant", content="/bridge status")])
        )
        self.assertFalse(bridge_commands.is_command([]))


# ==================== status / models ====================


class StatusTests(_BridgeTestBase):
    def test_reports_unavailable_without_browser(self):
        reply = self.run_command("/bridge status")
        self.assertIn("请求处理：不可用", reply)
        self.assertIn("浏览器：未初始化", reply)
        self.assertIn("下一步", reply)

    def test_reports_ready_when_page_exists(self):
        self.driver.page = object()  # 只读命令不碰页面内容，任何真值对象即可
        reply = self.run_command("/bridge status")
        self.assertIn("请求处理：可用", reply)
        self.assertIn("浏览器：已就绪", reply)
        self.assertNotIn("下一步", reply)

    def test_ready_browser_with_unopened_bucket_page_is_not_reported_as_unavailable(self):
        """桶页面是惰性创建的：它还没开 ≠ 服务不可用（实测踩到过的谎报）。"""
        self.driver.page = object()  # 浏览器就绪，但 ua:curl 这个桶还没被请求过
        reply = self.run_command("/bridge status", "ua:curl")
        self.assertIn("请求处理：可用", reply)
        self.assertIn("浏览器：已就绪", reply)
        self.assertIn("当前桶页面：未打开", reply)
        self.assertIn("不代表服务不可用", reply)
        self.assertNotIn("下一步", reply)

    def test_reports_bucket_and_scoping(self):
        reply = self.run_command("/bridge status", "agent-a")
        self.assertIn("当前桶：agent-a", reply)
        self.assertIn(f"会话分桶：{'开启' if config.SESSION_SCOPING else '关闭'}", reply)

    def test_init_error_is_flattened_to_one_line(self):
        self.driver.init_error = "boom\nsecond line\nthird"
        reply = self.run_command("/bridge status")
        self.assertIn("boom", reply)
        self.assertNotIn("second line", reply)

    def test_status_is_readonly(self):
        before = self.driver.session_keys()
        self.run_command("/bridge session reset", "b1")
        self.run_command("/bridge status", "b1")
        self.assertEqual(self.driver.session_keys(), sorted({DEFAULT_SESSION_KEY, "b1", *before}))
        # 命令没碰页面：仍未被初始化
        self.assertIsNone(self.driver.page)


class ModelsTests(_BridgeTestBase):
    def test_models_reply_matches_advertised_models(self):
        reply = self.run_command("/bridge models")
        for card in models.advertised_models():
            self.assertIn(card["id"], reply)

    def test_models_reply_matches_v1_models_endpoint(self):
        client = TestClient(app)
        with mock.patch("gemini_web.server.driver", self.driver):
            payload = client.get("/v1/models").json()
        endpoint_ids = [card["id"] for card in payload["data"]]
        reply = self.run_command("/bridge models")
        for model_id in endpoint_ids:
            self.assertIn(model_id, reply)
        self.assertEqual(len(endpoint_ids), len(models.advertised_models()))


# ==================== session ====================


class SessionStatusTests(_BridgeTestBase):
    def test_fresh_bucket_needs_seed(self):
        reply = self.run_command("/bridge session status", "fresh")
        self.assertIn("需要播种", reply)
        self.assertIn("轮数：0", reply)

    def test_bucket_with_history_reports_incremental(self):
        state = self.driver._state("b1")
        state.has_history = True
        state.turns = 3
        state.est_tokens = 120
        reply = self.run_command("/bridge session", "b1")
        self.assertIn("已有上下文", reply)
        self.assertIn("轮数：3", reply)
        self.assertIn("估算 tokens：120", reply)

    def test_pending_rotation_is_reported(self):
        self.driver.reset_session("b1")
        reply = self.run_command("/bridge session status", "b1")
        self.assertIn("已请求重置", reply)
        self.assertNotIn("删除", reply)

    def test_reseed_request_after_history_is_distinguished(self):
        state = self.driver._state("b1")
        state.has_history = True
        state.turns = 2
        self.driver.reseed_session("b1")
        reply = self.run_command("/bridge session status", "b1")
        self.assertIn("已排队重新播种", reply)
        self.assertIn("重复内容", reply)

    def test_cap_hit_is_reported(self):
        state = self.driver._state("b1")
        state.cap_hit = True
        state.last_error = "context_length_exceeded"
        reply = self.run_command("/bridge session status", "b1")
        self.assertIn("上下文长度上限", reply)
        self.assertIn("上次错误：context_length_exceeded", reply)

    def test_does_not_list_other_buckets(self):
        self.driver._state("other-agent").has_history = True
        reply = self.run_command("/bridge session status", "b1")
        self.assertNotIn("other-agent", reply)


class SessionResetTests(_BridgeTestBase):
    def test_reset_marks_only_current_bucket(self):
        reply = self.run_command("/bridge session reset", "agent-a")
        self.assertTrue(self.driver._state("agent-a").pending_rotation)
        self.assertFalse(self.driver._state("agent-b").pending_rotation)
        self.assertFalse(self.driver._state(DEFAULT_SESSION_KEY).pending_rotation)
        self.assertIn("agent-a", reply)

    def test_reset_receipt_explains_scope_and_semantics(self):
        reply = self.run_command("/bridge session reset", "agent-a")
        self.assertIn("只影响这个会话桶", reply)
        self.assertIn("不是删除客户端的对话历史", reply)
        self.assertIn("POST /session/reset?session=agent-a", reply)

    def test_reset_is_persisted_for_the_next_round(self):
        self.run_command("/bridge session reset", "agent-a")
        reloaded = GeminiWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))
        self.assertTrue(reloaded._state("agent-a").pending_rotation)


class SessionReseedTests(_BridgeTestBase):
    def test_reseed_keeps_session_but_requires_seed(self):
        state = self.driver._state("b1")
        state.has_history = True
        state.turns = 4
        self.run_command("/bridge session reseed", "b1")
        state = self.driver._state("b1")
        self.assertFalse(state.has_history)
        self.assertFalse(state.pending_rotation)  # 不换网页会话
        self.assertEqual(state.turns, 4)  # 统计不动，只有“下一轮要播种”这一件事变了

    def test_reseed_is_one_shot_flag_and_survives_restart(self):
        self.driver._state("b1").has_history = True
        self.driver._state("b2").has_history = True
        self.driver._save_session_state(key="b1")
        self.driver._save_session_state(key="b2")
        self.run_command("/bridge session reseed", "b1")
        reloaded = GeminiWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))
        self.assertTrue(reloaded.needs_seed("b1"))
        self.assertFalse(reloaded.needs_seed("b2"))  # 其它桶不受影响

    def test_reseed_receipt_warns_about_duplicates(self):
        reply = self.run_command("/bridge session reseed", "b1")
        self.assertIn("重复内容", reply)
        self.assertIn("当前", reply)


# ==================== settings save-files ====================


class SaveFilesSettingTests(_BridgeTestBase):
    def test_status_defaults_to_global_config(self):
        with mock.patch.object(config, "SAVE_FILES", False):
            reply = self.run_command("/bridge settings save-files status", "b1")
        self.assertIn("跟随全局配置", reply)
        self.assertIn("当前生效：关闭", reply)
        self.assertIn("save_files 字段", reply)

    def test_set_on_is_per_bucket_and_persisted(self):
        reply = self.run_command("/bridge settings save-files on", "b1")
        self.assertIn("开启", reply)
        self.assertEqual(self.driver.save_files_preference("b1"), True)
        self.assertIsNone(self.driver.save_files_preference("b2"))
        reloaded = GeminiWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))
        self.assertEqual(reloaded.save_files_preference("b1"), True)
        self.assertIsNone(reloaded.save_files_preference("b2"))

    def test_set_off_overrides_global_default(self):
        with mock.patch.object(config, "SAVE_FILES", True):
            self.run_command("/bridge settings save-files off", "b1")
            reply = self.run_command("/bridge settings save-files", "b1")
        self.assertEqual(self.driver.save_files_preference("b1"), False)
        self.assertIn("当前生效：关闭", reply)

    def test_default_clears_preference(self):
        self.run_command("/bridge settings save-files on", "b1")
        reply = self.run_command("/bridge settings save-files default", "b1")
        self.assertIsNone(self.driver.save_files_preference("b1"))
        self.assertIn("跟随全局配置", reply)

    def test_status_when_enabled_shows_destination_and_behavior(self):
        with mock.patch.object(config, "OUTPUT_DIR", "/tmp/gemini-bridge-output"):
            self.run_command("/bridge settings save-files on", "b1")
            reply = self.run_command("/bridge settings save-files status", "b1")
        self.assertIn("/tmp/gemini-bridge-output", reply)
        self.assertIn("新建", reply)
        self.assertIn("不会覆盖", reply)
        self.assertIn("save-files off", reply)

    def test_settings_does_not_touch_global_config(self):
        with mock.patch.object(config, "SAVE_FILES", False):
            self.run_command("/bridge settings save-files on", "b1")
            self.assertFalse(config.SAVE_FILES)
            self.assertIsNone(self.driver.save_files_preference("b2"))


class SaveFilesResolutionTests(unittest.TestCase):
    """优先级：请求字段 > 本桶偏好 > ``config.SAVE_FILES``。"""

    class _PreferenceDriver:
        def __init__(self, value):
            self.value = value

        def save_files_preference(self, key=None):
            return self.value

    class _BrokenDriver:
        def save_files_preference(self, key=None):
            return "yes"  # 非 bool：必须当成“未设置”，否则会静默写出文件

    def test_request_field_wins(self):
        driver = self._PreferenceDriver(False)
        self.assertTrue(bridge_commands.save_files_enabled(driver, "b", True))
        self.assertFalse(bridge_commands.save_files_enabled(driver, "b", False))

    def test_bucket_preference_wins_over_config(self):
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertTrue(bridge_commands.save_files_enabled(self._PreferenceDriver(True), "b", None))
        with mock.patch.object(config, "SAVE_FILES", True):
            self.assertFalse(bridge_commands.save_files_enabled(self._PreferenceDriver(False), "b", None))

    def test_falls_back_to_config(self):
        with mock.patch.object(config, "SAVE_FILES", True):
            self.assertTrue(bridge_commands.save_files_enabled(self._PreferenceDriver(None), "b", None))
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertFalse(bridge_commands.save_files_enabled(self._PreferenceDriver(None), "b", None))

    def test_fake_driver_and_non_bool_preference_do_not_enable(self):
        with mock.patch.object(config, "SAVE_FILES", False):
            self.assertFalse(bridge_commands.save_files_enabled(object(), "b", None))
            self.assertFalse(bridge_commands.save_files_enabled(self._BrokenDriver(), "b", None))


# ==================== 路由：两条协议路径 + 浏览器不可用 ====================


class _RouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="bridge-command-route-")
        self.state_patch = mock.patch.object(
            config, "SESSION_FILE", Path(self.tmp.name) / "state.json"
        )
        self.state_patch.start()
        self.addCleanup(self.state_patch.stop)
        self.addCleanup(self.tmp.cleanup)

        self.driver = GeminiWebDriver(user_data_dir=str(Path(self.tmp.name) / "profile"))
        self.driver.page = object()
        self.driver.send_chat = mock.AsyncMock(return_value=("pong", []))
        self.driver.save_extracted_files = mock.MagicMock(return_value=["/tmp/x.py"])
        driver_patch = mock.patch("gemini_web.server.driver", self.driver)
        driver_patch.start()
        self.addCleanup(driver_patch.stop)
        task_patch = mock.patch.object(config, "TASK_SNAPSHOT_ENABLED", False)
        task_patch.start()
        self.addCleanup(task_patch.stop)
        self.client = TestClient(app)

    def post_chat(self, text, session=None, stream=False):
        headers = {"X-Gemini-Session": session} if session else {}
        return self.client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-chat",
                "messages": [{"role": "user", "content": text}],
                "stream": stream,
            },
            headers=headers,
        )

    def post_responses(self, text, session=None, stream=False):
        headers = {"X-Gemini-Session": session} if session else {}
        return self.client.post(
            "/v1/responses",
            json={"model": "gemini-chat", "input": text, "stream": stream},
            headers=headers,
        )


class ChatRouteCommandTests(_RouteTests):
    def test_command_is_answered_locally(self):
        res = self.post_chat("/bridge status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("[Bridge]", res.json()["choices"][0]["message"]["content"])
        self.driver.send_chat.assert_not_called()

    def test_command_usage_is_zero_prompt_tokens(self):
        payload = self.post_chat("/bridge models").json()
        self.assertEqual(payload["usage"]["prompt_tokens"], 0)
        self.assertGreater(payload["usage"]["completion_tokens"], 0)

    def test_command_stream_uses_normal_sse_shape(self):
        res = self.post_chat("/bridge status", stream=True)
        self.assertEqual(res.status_code, 200)
        body = res.text
        self.assertIn('"role": "assistant"', body)
        self.assertIn('"finish_reason": "stop"', body)
        self.assertTrue(body.rstrip().endswith("data: [DONE]"))
        self.assertIn("[Bridge]", body)
        self.driver.send_chat.assert_not_called()

    def test_command_works_without_browser(self):
        self.driver.page = None
        res = self.post_chat("/bridge status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("未初始化", res.json()["choices"][0]["message"]["content"])

    def test_streaming_command_works_without_browser(self):
        self.driver.page = None
        res = self.post_chat("/bridge help", stream=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("[Bridge]", res.text)

    def test_plain_prompt_without_browser_still_503(self):
        self.driver.page = None
        res = self.post_chat("你好")
        self.assertEqual(res.status_code, 503)
        self.driver.send_chat.assert_not_called()

    def test_plain_prompt_goes_upstream(self):
        res = self.post_chat("你好")
        self.assertEqual(res.status_code, 200)
        self.driver.send_chat.assert_awaited()

    def test_command_does_not_record_task_snapshot(self):
        with mock.patch("gemini_web.server.tasks.record") as record:
            self.post_chat("/bridge session reset", session="agent-a")
        record.assert_not_called()

    def test_invalid_command_returns_usage_and_changes_nothing(self):
        res = self.post_chat("/bridge session nosuch", session="agent-a")
        self.assertEqual(res.status_code, 200)
        self.assertIn("用法", res.json()["choices"][0]["message"]["content"])
        self.assertFalse(self.driver._state("agent-a").pending_rotation)
        self.driver.send_chat.assert_not_called()

    def test_command_scope_is_the_requested_bucket_only(self):
        self.post_chat("/bridge session reset", session="agent-a")
        self.assertTrue(self.driver._state("agent-a").pending_rotation)
        self.assertFalse(self.driver._state("agent-b").pending_rotation)

    def test_status_reply_does_not_leak_tokens_or_other_buckets(self):
        self.driver._state("other-agent").has_history = True
        with mock.patch.object(config, "RESET_TOKEN", "s3cret-token"):
            body = self.post_chat("/bridge status", session="agent-a").json()
        content = body["choices"][0]["message"]["content"]
        self.assertNotIn("s3cret-token", content)
        self.assertNotIn("other-agent", content)

    def test_bucket_save_files_preference_controls_saving(self):
        self.driver.send_chat = mock.AsyncMock(
            return_value=("说明", [{"lang": "python", "code": "print(1)"}])
        )
        # 默认（未设置 + SAVE_FILES=false）不落盘
        with mock.patch.object(config, "SAVE_FILES", False):
            self.post_chat("写点代码", session="agent-a")
            self.driver.save_extracted_files.assert_not_called()
            # 本桶开了落盘：只有这个桶写文件
            self.post_chat("/bridge settings save-files on", session="agent-a")
            self.post_chat("再写点代码", session="agent-a")
            self.driver.save_extracted_files.assert_called_once()
            self.post_chat("另一个桶写代码", session="agent-b")
            self.assertEqual(self.driver.save_extracted_files.call_count, 1)

    def test_request_field_still_overrides_bucket_preference(self):
        self.driver.send_chat = mock.AsyncMock(
            return_value=("说明", [{"lang": "python", "code": "print(1)"}])
        )
        self.post_chat("/bridge settings save-files on", session="agent-a")
        self.driver.save_extracted_files.reset_mock()
        self.client.post(
            "/v1/chat/completions",
            json={
                "model": "gemini-chat",
                "messages": [{"role": "user", "content": "写点代码"}],
                "save_files": False,
            },
            headers={"X-Gemini-Session": "agent-a"},
        )
        self.driver.save_extracted_files.assert_not_called()


class ResponsesRouteCommandTests(_RouteTests):
    def test_command_is_answered_locally(self):
        res = self.post_responses("/bridge status")
        self.assertEqual(res.status_code, 200)
        payload = res.json()
        self.assertEqual(payload["status"], "completed")
        text = payload["output"][0]["content"][0]["text"]
        self.assertIn("[Bridge]", text)
        self.driver.send_chat.assert_not_called()

    def test_command_usage_is_zero_prompt_tokens(self):
        payload = self.post_responses("/bridge models").json()
        self.assertEqual(payload["usage"]["input_tokens"], 0)

    def test_command_works_without_browser(self):
        self.driver.page = None
        res = self.post_responses("/bridge session status")
        self.assertEqual(res.status_code, 200)
        self.assertIn("[Bridge]", res.json()["output"][0]["content"][0]["text"])

    def test_plain_prompt_without_browser_still_503(self):
        self.driver.page = None
        self.assertEqual(self.post_responses("你好").status_code, 503)
        self.driver.send_chat.assert_not_called()

    def test_streaming_command_emits_responses_events(self):
        res = self.post_responses("/bridge status", stream=True)
        self.assertEqual(res.status_code, 200)
        body = res.text
        self.assertIn("event: response.output_text.delta", body)
        self.assertIn("event: response.completed", body)
        self.assertIn("[Bridge]", body)
        self.driver.send_chat.assert_not_called()

    def test_streaming_command_works_without_browser(self):
        self.driver.page = None
        res = self.post_responses("/bridge help", stream=True)
        self.assertEqual(res.status_code, 200)
        self.assertIn("event: response.completed", res.text)

    def test_reset_scope_is_the_requested_bucket_only(self):
        self.post_responses("/bridge session reset", session="agent-a")
        self.assertTrue(self.driver._state("agent-a").pending_rotation)
        self.assertFalse(self.driver._state("agent-b").pending_rotation)


class CommandStreamEncodingTests(unittest.TestCase):
    def test_stream_command_reply_is_openai_shaped(self):
        request = ChatCompletionRequest(
            model="gemini-chat",
            messages=[ChatMessage(role="user", content="/bridge status")],
        )

        async def collect():
            return [line async for line in streaming._stream_command_reply(request, "hi")]

        lines = asyncio.run(collect())
        self.assertIn('"role": "assistant"', lines[0])
        self.assertIn('"content": "hi"', lines[1])
        self.assertIn('"finish_reason": "stop"', lines[-2])
        self.assertEqual(lines[-1], "data: [DONE]\n\n")


if __name__ == "__main__":
    unittest.main()
