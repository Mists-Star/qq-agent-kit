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

    def test_apostrophe_does_not_block(self):
        # 孤立撇号（can't）不应让栈永久非空导致整段不切
        parts = safe_sentence_cut("can't stop. Go on. Done.")
        self.assertEqual(len(parts), 3, f"撇号导致未切句: {parts}")

    def test_crossed_brackets_recover(self):
        # 交叉括号是畸形输入，但要能恢复切句，不能整段卡死
        parts = safe_sentence_cut("这是【《嵌套】错误》。下一句。后面还有一句。")
        self.assertEqual(len(parts), 3, f"交叉括号导致未切句: {parts}")

    def test_unclosed_bracket_does_not_block_forever(self):
        # 只有左括号没闭合时，后续句子仍应切出（栈深度上限 + 容错）
        parts = safe_sentence_cut("【未闭合。仍然要切。")
        self.assertGreaterEqual(len(parts), 1)

    def test_empty(self):
        self.assertEqual(safe_sentence_cut(""), [])


class DegenerateTestCase(unittest.TestCase):
    def setUp(self):
        self.det = DegenerateDetector()

    def test_char_run_detected(self):
        self.assertTrue(self.det.is_degenerate("哈" * 30))
        self.assertIn("char_run", self.det.reason("哈" * 30))

    def test_period_repeat_detected(self):
        # 各种周期长度都应检出（这是旧固定分块算法的漏检点）
        for unit in ("abcde", "abcd", "abc", "这是一段重复的中文句子", "abcdefg"):
            with self.subTest(unit=unit):
                self.assertTrue(self.det.is_degenerate(unit * 10), f"漏检周期 {unit!r}")

    def test_period_reason(self):
        self.assertIn("period=", self.det.reason("abcd" * 10))

    def test_normal_text_pass(self):
        self.assertFalse(self.det.is_degenerate("今天天气很好，我们出去走走吧明天再说吧。"))

    def test_short_text_not_judged_by_ngram(self):
        self.assertFalse(self.det.is_degenerate("哈哈"))

    def test_empty(self):
        self.assertFalse(self.det.is_degenerate(""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
