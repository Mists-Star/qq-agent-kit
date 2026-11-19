"""安全切句 — 流式回复按句发送，且绝不把成对括号拆成两句。

问题现场：
    流式输出 "这个【重要】的事情" 若在 "。" 处粗暴切分，
    会把 【】 拆到两句，第二句只剩一个字符/颜文字。

算法：单遍扫描 + 括号栈。仅当栈为空且遇到句末标点时才切句。

健壮性（针对审查发现的边界）：
    - 引号（' "）不作为切句阻断：撇号 "can't" 会让栈永久非空。
    - 交叉括号容错：闭合符匹配栈中任意位置时，弹出到该位置，
      避免畸形输入让栈卡死、整段不切句。
    - 栈深度上限：防大量未闭合括号导致内存/逻辑问题。

batch 与 stream 共用同一份 _Cutter 状态机，杜绝两份实现互相"自洽假通过"。
"""
from __future__ import annotations

BRACKET_PAIRS: dict[str, str] = {
    "【": "】",
    "《": "》",
    "「": "」",
    "『": "』",
    "（": "）",
    "〈": "〉",
    "‹": "›",
    "{": "}",
    "[": "]",
    "(": ")",
    "<": ">",
}

SENTENCE_ENDS = set("。！？!?；;….")  # 含英文句点
MAX_STACK = 64


class _Cutter:
    """切句状态机，batch 与 stream 共用。"""

    def __init__(self, keep_punct: bool = True):
        self.keep_punct = keep_punct
        self.buf: list[str] = []
        self.stack: list[str] = []

    def feed(self, chunk: str) -> list[str]:
        out: list[str] = []
        for ch in chunk:
            self.buf.append(ch)
            if ch in BRACKET_PAIRS:
                if len(self.stack) < MAX_STACK:
                    self.stack.append(BRACKET_PAIRS[ch])
            elif ch in BRACKET_PAIRS.values():
                # 匹配栈中任意位置的同类闭合符：弹出到该处（交叉括号容错）
                if ch in self.stack:
                    idx = len(self.stack) - 1 - self.stack[::-1].index(ch)
                    del self.stack[idx:]
            elif ch in SENTENCE_ENDS and not self.stack:
                self._flush(out)
        return out

    def flush(self) -> list[str]:
        out: list[str] = []
        self._flush(out)
        return out

    def _flush(self, out: list[str]) -> None:
        s = "".join(self.buf).strip()
        self.buf.clear()
        if not s:
            return
        if not self.keep_punct and s[-1] in SENTENCE_ENDS:
            s = s[:-1].strip()
        if s:
            out.append(s)


def safe_sentence_cut(text: str, *, keep_punct: bool = True) -> list[str]:
    """把文本切成句子列表，保证成对括号不被拆开。"""
    if not text:
        return []
    cut = _Cutter(keep_punct)
    out = cut.feed(text)
    out.extend(cut.flush())
    return [s for s in out if s]


def stream_cutter(*, keep_punct: bool = True):
    """返回一个增量切句器，供流式场景逐 chunk 喂入。

    用法：
        cut = stream_cutter()
        for chunk in llm_stream:
            for sentence in cut.feed(chunk):
                await send(sentence)
        for sentence in cut.flush():
            await send(sentence)
    """
    return _Cutter(keep_punct)
