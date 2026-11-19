"""退化重复检测 — 拦截模型陷入"哈哈哈哈..."死循环的垃圾输出。

问题现场：
    某 vision 模型对图片返回 1693 字符的"哈哈哈哈哈哈"，耗时 131 秒。
    事后截断已无意义（接收方早已收到垃圾），必须在流式 chunk 层面实时拦截。

双判据（满足任一即判退化）：
    1. 单字符连跑 >= min_run（默认 24）
    2. n-gram 重复率 >= ratio（默认 n=15, ratio=0.5）

用法：
    det = DegenerateDetector()
    for chunk in stream:
        if det.is_degenerate(chunk + tail):
            # 立即停止发送，切换备用模型
            break
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass


@dataclass
class DegenerateConfig:
    min_run: int = 24          # 单字符连跑阈值
    ngram_n: int = 15          # n-gram 窗口
    ngram_ratio: float = 0.5   # 最高频 n-gram 占比阈值
    min_chars: int = 30        # 短文本不判 n-gram


class DegenerateDetector:
    def __init__(self, config: DegenerateConfig | None = None):
        self.config = config or DegenerateConfig()
        self._run_re = re.compile(r"(.)\1{%d,}" % (self.config.min_run - 1))

    def is_degenerate(self, text: str) -> bool:
        if not text:
            return False
        if self._run_re.search(text):
            return True
        cfg = self.config
        if len(text) < cfg.min_chars:
            return False
        # 按固定块切分（非滑动窗口）：周期性文本用滑动窗口会被移位打散，
        # 导致每个 gram 都不同。固定分块才能让"同一段反复"真正聚到一起。
        grams = [text[i : i + cfg.ngram_n] for i in range(0, len(text), cfg.ngram_n)]
        grams = [g for g in grams if len(g) == cfg.ngram_n]
        if len(grams) < 2:
            return False
        top = Counter(grams).most_common(1)[0][1]
        return top / len(grams) >= cfg.ngram_ratio

    def reason(self, text: str) -> str:
        """给出判定原因，便于记录到 failed_payload。"""
        if not text:
            return "empty"
        if self._run_re.search(text):
            m = self._run_re.search(text)
            return f"char_run>={self.config.min_run} (ch={m.group(1)!r})" if m else "char_run"
        cfg = self.config
        if len(text) >= cfg.min_chars:
            grams = [text[i : i + cfg.ngram_n] for i in range(0, len(text), cfg.ngram_n)]
            grams = [g for g in grams if len(g) == cfg.ngram_n]
            if len(grams) >= 2:
                g, c = Counter(grams).most_common(1)[0]
                if c / len(grams) >= cfg.ngram_ratio:
                    return f"ngram_repeat={c}/{len(grams)} ({g!r})"
        return "normal"
