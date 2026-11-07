"""把回复收拾成适合在微信里读的样子。

微信不渲染 Markdown——`**粗体**`、`| 表格 |`、` ```代码``` ` 在手机上都是
一堆原始符号，家人看不懂。这里做保守的清理 + 按段落分条。
"""
from __future__ import annotations

import re

_FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`]+)`")
_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_ITALIC = re.compile(r"(?<![*\w])\*([^*\n]+)\*(?!\*)")
_UNDER_BOLD = re.compile(r"__([^_]+)__")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_BULLET = re.compile(r"^\s{0,3}[-*+]\s+", re.M)
_TABLE_SEP = re.compile(r"^\s*\|?[\s:\-|]+\|?\s*$", re.M)
_TABLE_ROW = re.compile(r"^\s*\|(.+)\|\s*$", re.M)


def strip_markdown(text: str) -> str:
    out = text or ""
    out = _FENCE.sub(lambda m: m.group(1).rstrip(), out)      # 代码块保留内容，去掉围栏
    out = _INLINE_CODE.sub(r"\1", out)
    out = _BOLD.sub(r"\1", out)
    out = _UNDER_BOLD.sub(r"\1", out)
    out = _ITALIC.sub(r"\1", out)
    out = _HEADING.sub("", out)

    # 表格：去掉分隔行，单元格之间用「 · 」连接，比一堆竖线好读
    out = _TABLE_SEP.sub("", out)
    out = _TABLE_ROW.sub(
        lambda m: " · ".join(c.strip() for c in m.group(1).split("|") if c.strip()),
        out,
    )
    out = _BULLET.sub("· ", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def split_chunks(text: str, max_chars: int = 1400) -> list[str]:
    """按段落切成多条，避免一条超长消息在微信里被折叠或发送失败。"""
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    buf = ""
    for para in text.split("\n\n"):
        while len(para) > max_chars:                 # 单段就超长，硬切
            if buf:
                chunks.append(buf.strip())
                buf = ""
            chunks.append(para[:max_chars])
            para = para[max_chars:]
        if not buf:
            buf = para
        elif len(buf) + 2 + len(para) <= max_chars:
            buf = f"{buf}\n\n{para}"
        else:
            chunks.append(buf.strip())
            buf = para
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def format_for_wechat(
    text: str,
    *,
    max_chars: int = 1400,
    strip: bool = True,
) -> list[str]:
    body = strip_markdown(text) if strip else (text or "")
    return split_chunks(body, max_chars=max_chars)
