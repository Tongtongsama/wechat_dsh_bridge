"""主动接话的判断：先用规则筛，再让模型复核。

为什么不用 dsh headless 做复核：它单次启动固定开销就有几千 tokens，
而判断"这句话值不值得接"只需要几十 tokens。直连 API 便宜两个数量级。

安全取向：任何一环出错（网络失败、返回格式不认识、额度不足）一律**不接话**。
主动打扰的代价远大于少接一句。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

API_URL = "https://api.deepseek.com/chat/completions"

# 值得看一眼的信号
_HELP_WORDS = (
    "怎么", "如何", "为什么", "为啥", "什么", "能不能", "可不可以", "行不行",
    "有没有", "谁知道", "谁懂", "求助", "帮我", "请教", "推荐", "建议",
    "是不是", "对不对", "怎么办", "咋办",
)
# 明显是闲聊或情绪，不看
_SKIP_WORDS = (
    "哈哈", "笑死", "草", "卧槽", "晚安", "早安", "在吗", "吃了吗",
    "生日快乐", "新年快乐",
)


def rule_should_consider(text: str) -> tuple[bool, str]:
    """第一道：这条消息有没有可能是"需要帮忙的问题"。

    返回 (是否值得复核, 理由)。规则只负责"放过候选"，不负责最终决定。
    """
    t = (text or "").strip()
    if len(t) < 6:
        return False, "太短"
    if len(t) > 500:
        return False, "太长，更像粘贴内容而非提问"
    if any(w in t for w in _SKIP_WORDS) and len(t) < 12:
        return False, "像闲聊"

    has_question = t.endswith("？") or t.endswith("?") or any(w in t for w in _HELP_WORDS)
    if has_question:
        return True, "含疑问/求助信号"
    return False, "没有提问特征"


_REVIEW_PROMPT = """你是微信群里的一个助手（不是人类）。下面是群里刚出现的一句话，没有人 @ 你。
请判断你是否应该主动接一句。

只在同时满足这些条件时回答 YES：
- 你能给出具体、有用、一两句话能说完的帮助
- 不接话别人就会继续困惑

这些情况一律 NO：
- 闲聊、调侃、情绪表达、感叹
- 别人之间正在对话，插进去会打断
- 涉及隐私、金钱、医疗、法律、家庭矛盾、感情纠纷
- 你不确定，或只是凑热闹

只输出一行：YES 或 NO，然后一个空格，再跟不超过 15 字的理由。"""


@dataclass
class Verdict:
    take: bool
    reason: str


def model_review(
    text: str,
    *,
    speaker: str,
    group_name: str,
    api_key: str,
    model: str = "deepseek-chat",
    timeout: float = 20.0,
) -> Verdict:
    """第二道：让模型复核。任何异常都判定为"不接话"。"""
    if not api_key:
        return Verdict(False, "没有可用凭据，跳过")

    user = f"群名：{group_name}\n发言人：{speaker}\n这句话：{text}"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _REVIEW_PROMPT},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        "max_tokens": 40,
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        content = body["choices"][0]["message"]["content"].strip()
    except urllib.error.HTTPError as exc:
        return Verdict(False, f"复核请求失败 HTTP {exc.code}")
    except Exception as exc:                      # 网络/超时/格式，一律不接
        return Verdict(False, f"复核异常：{type(exc).__name__}")

    m = re.match(r"^\s*(YES|NO)\b[\s:：-]*(.*)$", content, re.IGNORECASE)
    if not m:
        return Verdict(False, "复核结果无法解析")
    take = m.group(1).upper() == "YES"
    return Verdict(take, m.group(2).strip()[:40] or content[:40])
