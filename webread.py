"""把链接里的正文取出来，喂给只读文字的 agent。

为什么不把 URL 直接丢给 agent：
  · agent 那条路要自己决定去不去抓、抓多少，不可控；
  · 微信消息里的链接常常只有标题，URL 在控件属性里，得先解析出来；
  · 这里统一限长、限数、限域名，成本和安全都可控。

安全取向（重要）：
  · 只允许 http/https；
  · **拒绝内网地址**（127.0.0.1、10.x、192.168.x、169.254.x、::1 等），
    否则等于给外部消息开了一个打内网的洞；
  · 不跟随跳转到内网（每次请求都重新校验目标）；
  · 不解析图片、脚本、样式；
  · 正文截断到 N 字符，避免一次吃进几十万 token。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) dsh-bridge/1.0"
_URL_RE = re.compile(r"https?://[^\s<>\"'）)】\]]+", re.I)
# 微信链接卡片常常只显示域名+路径，没有 https:// 前缀
_BARE_RE = re.compile(r"(?<![\w./])((?:[\w-]+\.)+"
                        r"(?:com|cn|org|net|io|edu|gov|co|me))(/[^\s<>\"'）)】\]]*)?", re.I)
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg)[^>]*>.*?</\1>", re.I | re.S)
_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</(p|div|br|li|h[1-6]|tr|section|article)>", re.I)
_BLANK_RE = re.compile(r"[ \t\u3000]+")
_MULTI_NL = re.compile(r"\n{3,}")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)


@dataclass
class Page:
    ok: bool
    url: str = ""
    title: str = ""
    text: str = ""
    reason: str = ""


def find_urls(text: str, limit: int = 3) -> list[str]:
    """从一段文字里挑出 http(s) 链接，去重保序。"""
    out: list[str] = []
    raw = text or ""
    for m in _URL_RE.finditer(raw):
        url = m.group(0).rstrip(".,;:!?，。；：！？")
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            return out
    for m in _BARE_RE.finditer(raw):
        url = f"https://{m.group(1)}{m.group(2) or '/'}"
        if url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def _is_public_host(host: str) -> bool:
    """内网/保留地址一律拒绝。"""
    if not host:
        return False
    host = host.strip("[]").lower()
    if host in ("localhost", "localhost.localdomain"):
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return False
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast):
            return False
    return True


def _html_to_text(html: str, max_chars: int) -> tuple[str, str]:
    title = ""
    m = _TITLE_RE.search(html)
    if m:
        title = _html_unescape(_TAG_RE.sub("", m.group(1))).strip()[:120]

    body = _SCRIPT_RE.sub(" ", html)
    body = _BLOCK_RE.sub("\n", body)
    body = _TAG_RE.sub("", body)
    body = _html_unescape(body)
    body = _BLANK_RE.sub(" ", body)
    lines = [ln.strip() for ln in body.splitlines()]
    body = "\n".join(ln for ln in lines if ln)
    body = _MULTI_NL.sub("\n\n", body)
    return title, body[:max_chars]


def _html_unescape(s: str) -> str:
    import html as _html

    return _html.unescape(s)


def fetch(url: str, *, max_chars: int = 3000, timeout: float = 20.0) -> Page:
    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        return Page(False, url, reason="链接格式不对")
    if parsed.scheme not in ("http", "https"):
        return Page(False, url, reason=f"只支持 http/https，这条是 {parsed.scheme or '未知'}")
    if not _is_public_host(parsed.hostname or ""):
        return Page(False, url, reason="目标地址不是公网地址，已拒绝")

    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            # 跳转后的最终地址也要再校验一次
            final = resp.geturl()
            final_host = urllib.parse.urlparse(final).hostname or ""
            if not _is_public_host(final_host):
                return Page(False, url, reason="跳转到了非公网地址，已拒绝")
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if ctype and not any(t in ctype for t in
                                 ("text/", "json", "xml", "html")):
                return Page(False, url, reason=f"不是文本页面（{ctype.split(';')[0]}）")
            raw = resp.read(600_000)
    except urllib.error.HTTPError as exc:
        return Page(False, url, reason=f"HTTP {exc.code}")
    except Exception as exc:
        return Page(False, url, reason=f"{type(exc).__name__}")

    charset = "utf-8"
    m = re.search(r"charset=([\w-]+)", (ctype or ""), re.I)
    if m:
        charset = m.group(1)
    try:
        html = raw.decode(charset, "replace")
    except Exception:
        html = raw.decode("utf-8", "replace")

    title, text = _html_to_text(html, max_chars)
    if len(text) < 20:
        return Page(False, url, title=title, reason="页面几乎没有正文")
    return Page(True, url, title=title, text=text)
