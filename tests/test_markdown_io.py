"""T9.1–T9.3：读取、围栏扫描、行视图。"""

import unittest
from pathlib import Path

from gemini_web.markdown_io import (
    MarkdownError,
    parse_md,
    read_md,
    scan_fences,
    text_lines,
)

NL = chr(10)
CR = chr(13)
F = chr(96) * 3
F4 = chr(96) * 4
T = chr(126) * 3


class ReadMdTests(unittest.TestCase):
    def test_lf_no_trailing(self):
        d = parse_md("# T" + NL + "x")
        self.assertEqual(d.newline, NL)
        self.assertFalse(d.trailing_newline)
        self.assertEqual([l.text for l in d.lines], ["# T", "x"])
        self.assertEqual(d.text, "# T" + NL + "x")

    def test_lf_trailing(self):
        raw = "# T" + NL + "x" + NL
        d = parse_md(raw)
        self.assertTrue(d.trailing_newline)
        self.assertEqual(d.text, raw)

    def test_crlf_roundtrip(self):
        raw = "# T" + CR + NL + F + "json" + CR + NL + "{}" + CR + NL + F + CR + NL
        d = parse_md(raw)
        self.assertEqual(d.newline, CR + NL)
        self.assertTrue(d.trailing_newline)
        self.assertEqual(d.text, raw)
        self.assertNotIn(CR + CR, d.text)

    def test_crlf_no_trailing(self):
        raw = "a" + CR + NL + "b"
        d = parse_md(raw)
        self.assertEqual(d.text, raw)

    def test_empty_file(self):
        d = parse_md("")
        self.assertEqual(d.lines, [])
        self.assertFalse(d.trailing_newline)
        self.assertEqual(d.text, "")

    def test_read_md_from_disk(self):
        p = Path(self.id().replace(".", "_") + ".md")
        p.write_text("# T" + NL + F + "bash" + NL + "ls" + NL + F + NL, encoding="utf-8")
        try:
            d = read_md(p)
            self.assertEqual(d.path, p)
            self.assertEqual([(f.start, f.end, f.lang) for f in d.fences], [(2, 4, "bash")])
        finally:
            p.unlink()


class FenceScanTests(unittest.TestCase):
    def _fences(self, raw):
        return [(f.start, f.end, f.lang, f.marker, f.length) for f in parse_md(raw).fences]

    def test_basic_json_block(self):
        raw = "# T" + NL + F + "json" + NL + "{" + NL + "}" + NL + F + NL
        self.assertEqual(self._fences(raw), [(2, 5, "json", "`", 3)])

    def test_multiple_blocks_and_langs(self):
        raw = F + "bash" + NL + "ls" + NL + F + NL + "mid" + NL + F + "text" + NL + "x" + NL + F + NL
        self.assertEqual(self._fences(raw), [(1, 3, "bash", "`", 3), (5, 7, "text", "`", 3)])

    def test_no_lang(self):
        raw = F + NL + "x" + NL + F + NL
        self.assertEqual(self._fences(raw), [(1, 3, "", "`", 3)])

    def test_long_wrapper_with_inner_short_fence(self):
        raw = F4 + "md" + NL + F + "inner" + NL + F + NL + F4 + NL
        self.assertEqual(self._fences(raw), [(1, 4, "md", "`", 4)])

    def test_indent_three_is_fence(self):
        raw = "   " + F + "json" + NL + "{}" + NL + "   " + F + NL
        self.assertEqual(self._fences(raw), [(1, 3, "json", "`", 3)])

    def test_indent_four_is_not_fence(self):
        raw = "    " + F + "json" + NL + "x" + NL
        self.assertEqual(self._fences(raw), [])

    def test_tilde_fence(self):
        raw = T + "text" + NL + "y" + NL + T + NL
        self.assertEqual(self._fences(raw), [(1, 3, "text", "~", 3)])

    def test_tilde_not_closed_by_backtick(self):
        raw = T + "text" + NL + F + NL + "z" + NL + T + NL
        self.assertEqual(self._fences(raw), [(1, 4, "text", "~", 3)])

    def test_short_fence_not_closing_long(self):
        raw = F4 + "md" + NL + F + NL + "after" + NL + F4 + NL
        self.assertEqual(self._fences(raw), [(1, 4, "md", "`", 4)])

    def test_unclosed_raises(self):
        with self.assertRaises(MarkdownError) as ctx:
            parse_md(F + "json" + NL + "x" + NL)
        self.assertIn("1", str(ctx.exception))

    def test_backtick_info_with_backtick_ignored(self):
        # 反引号围栏的信息串不得含反引号
        raw = F + "js" + F + NL + "x" + NL
        self.assertEqual(self._fences(raw), [])

    def test_info_with_extra_words(self):
        raw = F + "python title=foo" + NL + "x" + NL + F + NL
        self.assertEqual(self._fences(raw)[0][2], "python")


class TextLinesTests(unittest.TestCase):
    def test_in_fence_flags_and_lang(self):
        raw = F + "text" + NL + "# not a title" + NL + "Copy" + NL + F + NL + "# real title" + NL
        lines = text_lines(parse_md(raw))
        self.assertTrue(lines[0].in_fence)
        self.assertTrue(lines[1].in_fence)
        self.assertTrue(lines[2].in_fence)
        self.assertTrue(lines[3].in_fence)
        self.assertFalse(lines[4].in_fence)
        self.assertEqual(lines[1].lang, "text")
        self.assertEqual(lines[4].lang, "")

    def test_blank_detection(self):
        lines = text_lines(parse_md("a" + NL + "" + NL + "   " + NL + "b" + NL))
        self.assertFalse(lines[0].is_blank)
        self.assertTrue(lines[1].is_blank)
        self.assertTrue(lines[2].is_blank)

    def test_indent_only_outside_fence(self):
        raw = "  indented" + NL + F + "json" + NL + "      in-fence" + NL + F + NL
        lines = text_lines(parse_md(raw))
        self.assertEqual(lines[0].indent, 2)
        self.assertEqual(lines[2].indent, 0)


if __name__ == "__main__":
    unittest.main()


from gemini_web.markdown_io import (  # noqa: E402
    Anchor,
    LocateError,
    apply_edit,
    backup_md,
    generate_edit,
    locate,
    render_view,
    rollback_md,
    verify,
    write_md,
    _safe_fence,
)

DOC = (
    "# Title" + NL
    + "intro" + NL
    + F + "json" + NL
    + '{"a": 1}' + NL
    + F + NL
    + "## Section" + NL
    + "body line" + NL
    + "unique marker" + NL
    + "## Next" + NL
    + "tail" + NL
)


class LocateTests(unittest.TestCase):
    def setUp(self):
        self.doc = parse_md(DOC)

    def test_line_anchor(self):
        self.assertEqual(locate(self.doc, Anchor(kind="line", start=2, end=3)), (2, 3))

    def test_line_anchor_out_of_range(self):
        with self.assertRaises(LocateError):
            locate(self.doc, Anchor(kind="line", start=1, end=999))

    def test_heading_anchor(self):
        # ## Section 在第 6 行，到下一个 ## 之前（第 9 行前）=> 6..8
        self.assertEqual(locate(self.doc, Anchor(kind="heading", heading="Section")), (6, 8))

    def test_heading_skips_fence_hash(self):
        doc = parse_md(F + "text" + NL + "# fake" + NL + F + NL + "# Real" + NL)
        # 围栏内的 # fake 不应命中；命中第 4 行
        self.assertEqual(locate(doc, Anchor(kind="heading", heading="fake") if False else Anchor(kind="heading", heading="Real")), (4, 4))
        with self.assertRaises(LocateError):
            locate(doc, Anchor(kind="heading", heading="fake"))

    def test_fence_anchor_by_lang(self):
        self.assertEqual(locate(self.doc, Anchor(kind="fence", lang="json")), (3, 5))

    def test_fence_anchor_by_index(self):
        self.assertEqual(locate(self.doc, Anchor(kind="fence", index=1)), (3, 5))

    def test_text_anchor_unique(self):
        self.assertEqual(locate(self.doc, Anchor(kind="text", text="unique marker")), (8, 8))

    def test_text_anchor_multi_hit_reports_candidates(self):
        doc = parse_md("dup" + NL + "dup" + NL)
        with self.assertRaises(LocateError) as ctx:
            locate(doc, Anchor(kind="text", text="dup"))
        self.assertIn("1", str(ctx.exception))
        self.assertIn("2", str(ctx.exception))

    def test_text_anchor_skips_fence(self):
        doc = parse_md(F + "json" + NL + "needle" + NL + F + NL)
        with self.assertRaises(LocateError):
            locate(doc, Anchor(kind="text", text="needle"))


class ApplyEditTests(unittest.TestCase):
    def test_outside_bytes_unchanged(self):
        doc = parse_md(DOC)
        edited = apply_edit(doc, 2, 2, "INTRO CHANGED")
        before = DOC.split(NL)
        after = edited.text.split(NL)
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1], "INTRO CHANGED")
        self.assertEqual(after[2:], before[2:])

    def test_newline_style_preserved(self):
        raw = "a" + CR + NL + "b" + CR + NL
        doc = parse_md(raw)
        edited = apply_edit(doc, 1, 1, "A")
        self.assertEqual(edited.text, "A" + CR + NL + "b" + CR + NL)

    def test_trailing_newline_preserved(self):
        doc = parse_md("a" + NL + "b")
        edited = apply_edit(doc, 1, 1, "A")
        self.assertFalse(edited.trailing_newline)
        self.assertEqual(edited.text, "A" + NL + "b")

    def test_replace_with_multiline(self):
        doc = parse_md("a" + NL + "b" + NL + "c" + NL)
        edited = apply_edit(doc, 2, 2, "X" + NL + "Y")
        self.assertEqual(edited.text, "a" + NL + "X" + NL + "Y" + NL + "c" + NL)

    def test_delete_range(self):
        doc = parse_md("a" + NL + "b" + NL + "c" + NL)
        edited = apply_edit(doc, 2, 2, "")
        self.assertEqual(edited.text, "a" + NL + "c" + NL)

    def test_edit_rescans_fences(self):
        doc = parse_md("a" + NL + "b" + NL)
        edited = apply_edit(doc, 1, 1, F + "json" + NL + "{}" + NL + F)
        self.assertEqual([(f.start, f.end, f.lang) for f in edited.fences], [(1, 3, "json")])


class WriteMdTests(unittest.TestCase):
    def test_write_and_diff(self):
        p = Path(self.id().replace(".", "_") + ".md")
        p.write_text("a" + NL + "b" + NL, encoding="utf-8")
        try:
            doc = read_md(p)
            doc = apply_edit(doc, 1, 1, "A")
            diff = write_md(doc)
            self.assertIn("-a", diff)
            self.assertIn("+A", diff)
            self.assertEqual(p.read_text(encoding="utf-8"), "A" + NL + "b" + NL)
        finally:
            p.unlink()

    def test_dry_run_does_not_touch_file(self):
        p = Path(self.id().replace(".", "_") + ".md")
        p.write_text("a" + NL, encoding="utf-8")
        try:
            doc = apply_edit(read_md(p), 1, 1, "A")
            diff = write_md(doc, dry_run=True)
            self.assertIn("+A", diff)
            self.assertEqual(p.read_text(encoding="utf-8"), "a" + NL)
        finally:
            p.unlink()


class BackupTests(unittest.TestCase):
    def test_backup_and_rollback(self):
        p = Path(self.id().replace(".", "_") + ".md")
        bdir = Path(self.id().replace(".", "_") + "_bak")
        p.write_text("orig" + NL, encoding="utf-8")
        try:
            b = backup_md(p, backup_dir=str(bdir))
            self.assertTrue(b.backup_path.exists())
            p.write_text("changed" + NL, encoding="utf-8")
            rollback_md(b)
            self.assertEqual(p.read_text(encoding="utf-8"), "orig" + NL)
        finally:
            p.unlink()
            for f in bdir.glob("*"):
                f.unlink()
            bdir.rmdir()


class SafeFenceTests(unittest.TestCase):
    def test_plain_content(self):
        out = _safe_fence("hello", "text")
        self.assertTrue(out.startswith(F + "text" + NL))
        self.assertTrue(out.endswith(NL + F))

    def test_content_with_triple_backticks_upgrades(self):
        out = _safe_fence("a" + NL + F + NL + "b", "md")
        self.assertTrue(out.startswith(F4 + "md" + NL))
        self.assertTrue(out.endswith(NL + F4))
        d = parse_md(out + NL)
        self.assertEqual(len(d.fences), 1)


class VerifyTests(unittest.TestCase):
    def test_healthy_doc_no_issues(self):
        self.assertEqual(verify(parse_md(DOC)), [])


class RenderViewTests(unittest.TestCase):
    def test_fence_lines_marked(self):
        view = render_view(parse_md(F + "json" + NL + "{}" + NL + F + NL))
        self.assertIn("[[fence:json]]", view)
        self.assertIn("L1 ", view)


class GenerateEditTests(unittest.TestCase):
    def test_parses_tool_call(self):
        doc = parse_md("a" + NL + "b" + NL + "c" + NL)

        def fake_llm(prompt):
            self.assertIn("L1", prompt)
            return 'TOOL_CALL: {"name": "edit_markdown", "arguments": {"start": 2, "end": 2, "new_text": "B"}}'

        start, end, new_text = generate_edit(doc, "把 b 改成 B", fake_llm)
        self.assertEqual((start, end, new_text), (2, 2, "B"))

    def test_rejects_out_of_range(self):
        doc = parse_md("a" + NL)

        def fake_llm(prompt):
            return 'TOOL_CALL: {"name": "edit_markdown", "arguments": {"start": 5, "end": 5, "new_text": "x"}}'

        with self.assertRaises(MarkdownError):
            generate_edit(doc, "x", fake_llm)

    def test_rejects_missing_call(self):
        doc = parse_md("a" + NL)
        with self.assertRaises(MarkdownError):
            generate_edit(doc, "x", lambda prompt: "no tool call here")
