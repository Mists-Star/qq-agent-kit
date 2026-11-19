"""安全切句 — 流式回复按句发送，且绝不把成对括号拆成两句。

问题现场：
    流式输出 "这个【重要】的事情" 若在 "。" 处粗暴切分，
    会把 【】 拆到两句，第二句只剩一个字符/颜文字。

算法：单遍扫描 + 括号栈。仅当栈为空且遇到句末标点时才切句。
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
    '"': '"',
    "'": "'",
}

SENTENCE_ENDS = set("。！？!?；;…")


def safe_sentence_cut(text: str, *, keep_punct: bool = True) -> list[str]:
    """把文本切成句子列表，保证成对括号不被拆开。

    Args:
        text: 输入文本。
        keep_punct: 是否保留句末标点。
    """
    if not text:
        return []
    sentences: list[str] = []
    buf: list[str] = []
    stack: list[str] = []

    for ch in text:
        buf.append(ch)
        if ch in BRACKET_PAIRS:
            # 引号允许自闭合：栈内已有同类就当闭合处理
            if ch in ('"', "'") and stack and stack[-1] == ch:
                stack.pop()
            else:
                stack.append(BRACKET_PAIRS[ch])
        elif stack and ch == stack[-1]:
            stack.pop()
        elif ch in SENTENCE_ENDS and not stack:
            _flush(buf, sentences, keep_punct)
    _flush(buf, sentences, keep_punct)
    return [s for s in sentences if s]


def _flush(buf: list[str], out: list[str], keep_punct: bool) -> None:
    s = "".join(buf).strip()
    if not s:
        buf.clear()
        return
    if not keep_punct and s and s[-1] in SENTENCE_ENDS:
        s = s[:-1].strip()
    if s:
        out.append(s)
    buf.clear()


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
    buf: list[str] = []
    stack: list[str] = []

    def feed(chunk: str) -> list[str]:
        out: list[str] = []
        for ch in chunk:
            buf.append(ch)
            if ch in BRACKET_PAIRS:
                if ch in ('"', "'") and stack and stack[-1] == ch:
                    stack.pop()
                else:
                    stack.append(BRACKET_PAIRS[ch])
            elif stack and ch == stack[-1]:
                stack.pop()
            elif ch in SENTENCE_ENDS and not stack:
                _flush(buf, out, keep_punct)
        return out

    def flush() -> list[str]:
        out: list[str] = []
        _flush(buf, out, keep_punct)
        return out

    class _Cutter:
        pass

    cutter = _Cutter()
    cutter.feed = feed  # type: ignore[attr-defined]
    cutter.flush = flush  # type: ignore[attr-defined]
    return cutter
