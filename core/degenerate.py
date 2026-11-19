"""退化重复检测 — 拦截模型陷入"哈哈哈哈..."或整段复读的垃圾输出。

问题现场：
    某 vision 模型对图片返回 1693 字符的"哈哈哈哈哈哈"，耗时 131 秒。
    事后截断已无意义（接收方早已收到垃圾），必须在流式 chunk 层面实时拦截。

判据（满足任一即判退化）：
    1. 单字符连跑 >= min_run（默认 24）
    2. 最短周期：文本由某个长度 1..max_period 的子串重复 >= min_repeats 次构成
       —— 能抓住 "abcd"*25、"这是重复的中文句子"*8 等任意长度周期，
          不受"周期是否整除固定块"影响。

用法：
    det = DegenerateDetector()
    for chunk in stream:
        if det.is_degenerate(chunk + tail):
            # 立即停止发送，切换备用模型
            break
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class DegenerateConfig:
    min_run: int = 24           # 单字符连跑阈值
    max_period: int = 32        # 检测的最长重复单元（覆盖绝大多数复读）
    min_repeats: int = 4        # 重复次数阈值
    min_chars: int = 24         # 短文本不判周期


class DegenerateDetector:
    def __init__(self, config: DegenerateConfig | None = None):
        self.config = config or DegenerateConfig()
        self._run_re = re.compile(r"(.)\1{%d,}" % (self.config.min_run - 1))

    def shortest_period(self, text: str) -> tuple[int, int] | None:
        """返回 (最短周期长度, 重复次数)；不满足重复阈值返回 None。

        用 KMP 前缀函数的思路：对每个候选周期 p，验证 text == unit * n
        （允许末尾不足一个周期的残段）。复杂度 O(max_period * len)。
        """
        cfg = self.config
        n = len(text)
        if n < cfg.min_chars:
            return None
        max_p = min(cfg.max_period, n // cfg.min_repeats)
        for p in range(1, max_p + 1):
            unit = text[:p]
            repeats = n // p
            if repeats < cfg.min_repeats:
                continue
            # 逐字符比对，允许末尾残段
            ok = True
            for i, ch in enumerate(text):
                if ch != unit[i % p]:
                    ok = False
                    break
            if ok:
                return p, repeats
        return None

    def is_degenerate(self, text: str) -> bool:
        if not text:
            return False
        if self._run_re.search(text):
            return True
        return self.shortest_period(text) is not None

    def reason(self, text: str) -> str:
        """给出判定原因，便于记录到 failed_payload。"""
        if not text:
            return "empty"
        m = self._run_re.search(text)
        if m:
            return f"char_run>={self.config.min_run} (ch={m.group(1)!r})"
        period = self.shortest_period(text)
        if period:
            p, repeats = period
            return f"period={p} repeats={repeats} (unit={text[:p]!r})"
        return "normal"
