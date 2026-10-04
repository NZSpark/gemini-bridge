"""端到端：Markdown IO 能力（阶段 9）走真实 Gemini 桥接链路。

链路：本地 .md 文件 → render_view（带行号/围栏标注）→ bridge /v1/chat/completions
     → 模型返回 TOOL_CALL: edit_markdown → generate_edit 解析 → apply_edit
     → write_md（dry-run）→ 校验 diff 与围栏结构。

运行（真实访问 Gemini，默认 skip）：

    GEMINI_E2E=1 .venv/bin/python -m unittest tests.e2e.test_markdown_io_e2e -v

未设置 GEMINI_E2E 时全部 skip，不影响常规套件、不发起网络请求。
"""

import os
import tempfile
import unittest
from pathlib import Path

from gemini_web import config
from gemini_web.markdown_io import (
    Anchor,
    LocateError,
    MarkdownError,
    apply_edit,
    generate_edit,
    locate,
    parse_md,
    read_md,
    render_view,
    scan_fences,
    text_lines,
    verify,
    write_md,
)

from .bridge import BridgeClient, BridgeServer

GATE = os.environ.get("GEMINI_E2E") == "1"
PORT = int(os.environ.get("E2E_PORT") or config.PORT)
BASE_URL = f"http://127.0.0.1:{PORT}"
MODEL_ID = "gemini-chat"

SERVER = None
BRIDGE = None

SAMPLE_MD = (
    "# 项目说明\n"
    "\n"
    "这是引言。\n"
    "\n"
    "```json\n"
    '{"name": "demo", "version": "1.0"}\n'
    "```\n"
    "\n"
    "## 安装\n"
    "\n"
    "```bash\n"
    "pip install demo\n"
    "```\n"
    "\n"
    "## 用法\n"
    "\n"
    "运行 demo 即可。\n"
)


def setUpModule():
    global SERVER, BRIDGE
    if not GATE:
        return
    SERVER = BridgeServer(BASE_URL)
    SERVER.ensure_started()
    BRIDGE = BridgeClient(BASE_URL, config.SESSION_KEY_HEADER)


def tearDownModule():
    if SERVER is not None:
        SERVER.stop()


@unittest.skipUnless(GATE, "设置 GEMINI_E2E=1 才运行真实端到端测试")
class MarkdownIoE2ETests(unittest.TestCase):
    """真实模型链路。每个用例自建临时文件，互不影响。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "README.md"
        self.path.write_text(SAMPLE_MD, encoding="utf-8")

    def tearDown(self):
        self.tmpdir.cleanup()

    def _llm(self, prompt: str) -> str:
        """把 render_view 的提示词送进 bridge，取回模型原始文本。"""
        status, body = BRIDGE.chat(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_ID,
            session="e2e-md",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "edit_markdown",
                        "description": "按行号区间替换 Markdown 内容",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "integer"},
                                "end": {"type": "integer"},
                                "new_text": {"type": "string"},
                            },
                            "required": ["start", "end", "new_text"],
                        },
                    },
                }
            ],
        )
        self.assertEqual(status, 200, f"bridge 返回非 200：{body}")
        return body["choices"][0]["message"]["content"]

    # --- E2E-MD-1：读取 + 围栏扫描，原文件字节保真 -------------------------
    def test_e2e_read_preserves_bytes(self):
        doc = read_md(self.path)
        self.assertEqual(doc.text, SAMPLE_MD)
        self.assertEqual([f.lang for f in doc.fences], ["json", "bash"])

    # --- E2E-MD-2：锚点定位，围栏内不参与结构匹配 ---------------------------
    def test_e2e_locate_anchors(self):
        doc = parse_md(SAMPLE_MD)
        self.assertEqual(locate(doc, Anchor(kind="fence", lang="json"))[0], 5)
        with self.assertRaises(LocateError):
            locate(doc, Anchor(kind="text", text="pip install demo"))  # 仅出现在围栏内，应被跳过
        start, end = locate(doc, Anchor(kind="heading", heading="安装"))
        self.assertEqual(start, 9)
        self.assertLess(end, 14)

    # --- E2E-MD-3：模型返回 TOOL_CALL，落盘前 dry-run ---------------------
    def test_e2e_model_edit_dry_run(self):
        doc = read_md(self.path)
        try:
            start, end, new_text = generate_edit(
                doc,
                "把『## 用法』这一节正文改成：用法已更新，见在线文档。",
                self._llm,
            )
        except MarkdownError as exc:
            self.skipTest(f"模型未按约定返回 edit_markdown：{exc}")

        edited = apply_edit(doc, start, end, new_text)
        diff = write_md(edited, dry_run=True)
        self.assertEqual(self.path.read_text(encoding="utf-8"), SAMPLE_MD)  # dry-run 不落盘
        self.assertTrue(diff.strip())
        self.assertEqual(len(edited.fences), len(doc.fences))
        self.assertEqual(verify(edited), [])

    # --- E2E-MD-4：模型编辑后结构仍然健康（含围栏成对） -------------------
    def test_e2e_edit_keeps_fences_paired(self):
        doc = read_md(self.path)
        try:
            start, end, new_text = generate_edit(
                doc,
                "在文档末尾追加一小节『## 许可』，正文一行即可。",
                self._llm,
            )
        except MarkdownError as exc:
            self.skipTest(f"模型未按约定返回 edit_markdown：{exc}")
        edited = apply_edit(doc, start, end, new_text)
        rescan = scan_fences(text_lines(edited))
        self.assertEqual([f.lang for f in rescan], [f.lang for f in doc.fences])

    # --- E2E-MD-5：整块替换保留语言标签 --------------------------------
    def test_e2e_fence_block_replace_keeps_lang(self):
        doc = read_md(self.path)
        start, end = locate(doc, Anchor(kind="fence", lang="json"))
        edited = apply_edit(doc, start, end, '```json\n{"name": "demo", "version": "2.0"}\n```')
        self.assertEqual([f.lang for f in edited.fences], ["json", "bash"])
        diff = write_md(edited, dry_run=True)
        self.assertIn("2.0", diff)

    # --- E2E-MD-6：render_view 的围栏边界标注确实发给模型 ----------------
    def test_e2e_render_view_marks_fences(self):
        view = render_view(read_md(self.path))
        self.assertIn("[[fence:json]]", view)
        self.assertIn("[[fence:bash]]", view)
        status, body = BRIDGE.chat(
            messages=[{"role": "user", "content": "仅回复 OK 两个字\n\n" + view}],
            model=MODEL_ID,
            session="e2e-md",
        )
        self.assertEqual(status, 200, f"bridge 返回非 200：{body}")
        self.assertTrue(body["choices"][0]["message"]["content"].strip())
