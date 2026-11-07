"""按情绪挑表情：给一段对话，从表情库里选一张最贴切的。

为什么不让 agent 直接挑：agent 那条路径（回复里写 `[表情:名字]`）它自己会挑，
但"保底发表情"是桥接在模型没挑的时候补的，这时候没有人做判断，原来是随机抽。
随机的结果是——不管你说的是好事还是坏事，抽到哪张全看运气。

所以这里补一次极小的判断：只看最近一句对方的话 + 自己刚回的话，
从候选名字里挑一个。挑不出来就返回 None，让调用方退化成随机（不至于没图发）。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"

_SYSTEM = (
    "你要从表情库里挑一张最贴切的表情，配在下面这句回复后面。\n"
    "规则：\n"
    "  1. 只能从候选列表里挑一个，原样输出名字；\n"
    "  2. 必须和对话的情绪合得上（开心配开心，吐槽配吐槽，安抚配安抚）；\n"
    "  3. 拿不准、或者候选里确实没有合适的，只输出 NONE；\n"
    "  4. 只输出名字或 NONE，不要解释。"
)


def choose(reply_text: str, user_text: str, candidates: list[str], *,
           api_key: str, model: str = "deepseek-flash",
           timeout: float = 12.0) -> str | None:
    """返回候选里的一个名字；失败或没把握返回 None。"""
    if not api_key or not candidates:
        return None

    import llm

    listed = "、".join(candidates[:40])
    raw = llm.chat(
        _SYSTEM,
        f"对方说：{(user_text or '')[:300]}\n"
        f"我的回复：{(reply_text or '')[:300]}\n\n候选：{listed}",
        api_key=api_key, model=model, timeout=timeout)
    if not raw or raw.upper().startswith("NONE"):
        return None

    # 模型可能带引号/句号/前缀，做一次宽松匹配
    cleaned = re.sub(r'[「」"""\'。.，,\s]', "", raw)
    for name in candidates:
        if name and re.sub(r"\s", "", name) in cleaned:
            return name
    return None
