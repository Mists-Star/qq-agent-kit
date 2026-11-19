"""切句与退化检测单测。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.degenerate import DegenerateDetector  # noqa: E402
from core.splitter import safe_sentence_cut, stream_cutter  # noqa: E402


class SplitterTestCase(unittest.TestCase):
    def test_bracket_not_torn(self):
        text = "这是【重要】的事情。第二句。"
        parts = safe_sentence_cut(text)
        self.assertTrue(any("【重要】" in p for p in parts), f"括号被拆开了: {parts}")
        self.assertTrue(all(len(p) > 1 for p in parts))

    def test_nested_brackets(self):
        text = "他说{这是【嵌套】的内容}。结束。"
        parts = safe_sentence_cut(text)
        self.assertTrue(any("{这是【嵌套】的内容}" in p for p in parts))

    def test_plain_sentences(self):
        parts = safe_sentence_cut("第一句。第二句！第三句？")
        self.assertEqual(len(parts), 3)

    def test_stream_matches_batch(self):
        text = "这是【测试】内容。另一句。"
        cut = stream_cutter()
        streamed: list[str] = []
        for ch in text:
            streamed.extend(cut.feed(ch))
        streamed.extend(cut.flush())
        self.assertEqual(streamed, safe_sentence_cut(text))

    def test_empty(self):
        self.assertEqual(safe_sentence_cut(""), [])


class DegenerateTestCase(unittest.TestCase):
    def setUp(self):
        self.det = DegenerateDetector()

    def test_char_run_detected(self):
        self.assertTrue(self.det.is_degenerate("哈" * 30))
        self.assertIn("char_run", self.det.reason("哈" * 30))

    def test_ngram_repeat_detected(self):
        self.assertTrue(self.det.is_degenerate("abcde" * 12))

    def test_normal_text_pass(self):
        self.assertFalse(self.det.is_degenerate("今天天气很好，我们出去走走吧。"))

    def test_short_text_not_judged_by_ngram(self):
        self.assertFalse(self.det.is_degenerate("哈哈"))

    def test_empty(self):
        self.assertFalse(self.det.is_degenerate(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
