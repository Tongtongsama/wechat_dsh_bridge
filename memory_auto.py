"""从对话里自动提取值得长期记住的事。

为什么要它：现在的记忆全靠主人手动「记住 xxx：...」，实际用起来没人愿意
一条条录。让它自己从聊天里捡——偏好、约定、计划、身份关系。

克制原则（宁可少记，不要记错）：
  · 只收客观事实，不收情绪、玩笑、临时状态；
  · 一次对话最多 2 条；
  · 带 `auto` 标记，主人在「记忆」里能一眼看出哪些是自动记的；
  · 同一范围每天上限，超了就不再提取；
  · 任何异常返回空列表。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"

_SYSTEM = (
    "从下面这段微信对话里，提取**值得长期记住**的客观事实。\n"
    "只收这几类：身份/关系、稳定偏好、明确的约定、具体的计划或截止时间。\n"
    "不要收：情绪、玩笑、闲聊、一次性的安排、对方没说清的东西。\n"
    "输出格式：每行 `人名|事实`，事实不超过 30 字，最多 2 行。\n"
    "如果没有任何值得记的，只输出 NONE。不要解释。"
)


def extract(dialogue: str, *, api_key: str, model: str = "deepseek-flash",
            timeout: float = 25.0) -> list[tuple[str, str]]:
    if not api_key or not (dialogue or "").strip():
        return []

    import llm

    raw = llm.chat(_SYSTEM, dialogue[:1500], api_key=api_key,
                   model=model, timeout=timeout)
    if not raw:
        return []

    if not raw or raw.upper().startswith("NONE"):
        return []

    out: list[tuple[str, str]] = []
    for line in raw.splitlines():
        line = line.strip().strip("-·* ")
        if "|" not in line:
            continue
        person, _, fact = line.partition("|")
        person, fact = person.strip()[:20], fact.strip()[:60]
        if person and fact and len(fact) >= 3:
            out.append((person, fact))
        if len(out) >= 2:
            break
    return out
