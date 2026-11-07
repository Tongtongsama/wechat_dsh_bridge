"""判断"这条群消息是不是在接着跟我说话"。

@ 过一次之后开一个窗口，接下来几条消息不要求再 @。但群里消息很杂，
全回会变成刷屏，所以用一次极小的模型调用做判断——比规则准，比整轮任务便宜。

**判不准就不回**（fail-closed）：宁可漏一句，也不要乱插话。

注意：调用统一走 `llm.chat`。`deepseek-flash` 是推理模型，token 给太少时
思考过程会把额度吃光、正文返回空——这里以前 max_tokens=8，导致 50 次判断
全部拿到 0 分，关注窗口实际只剩关键词在起作用。
"""
from __future__ import annotations

import llm

_SYSTEM = (
    "你在判断一条微信群消息有多大程度是在跟某个机器人说话。\n"
    "你会看到机器人上一条回复，和新来的一条群消息。\n"
    "下面这些情况分数高：\n"
    "  · 在延续上一条回复的话题（追问、回应、反驳、补充）；\n"
    "  · 明显在对机器人说话（用「你」称呼、要求它做事、问它问题）；\n"
    "纯路人聊天、换话题、跟别人说话，分数低。\n"
    "只输出一个 0 到 100 的整数，表示把握有多大。不要解释。"
)


def confidence(new_message: str, last_reply: str, *, api_key: str,
               model: str = "deepseek-flash", timeout: float = 40.0) -> int:
    """返回 0-100 的把握。任何异常都按 0 处理（不回）。"""
    if not api_key or not (new_message or "").strip():
        return 0

    raw = llm.chat(
        _SYSTEM,
        f"机器人上一条回复：{last_reply or '（无）'}\n\n新消息：{new_message}",
        api_key=api_key, model=model, timeout=timeout)
    if not raw:
        return 0

    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        return 0
    return max(0, min(100, int(digits[:3])))
