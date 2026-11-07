"""统一的小调用封装。

血泪教训：`deepseek-flash` 是**推理模型**，它先输出 `reasoning_content`（思考）
再输出 `content`（答案）。如果 max_tokens 给得小，token 会被思考全部吃光，
`content` 返回空串、`finish_reason` 是 `length`——调用方看起来"模型没回答"，
实际上是被截断了。

实测代价：
  · 关注窗口的判断 50 次全部拿到 0 分（只有关键词兜底在工作）
  · 记忆自更新一条都没学到

所以所有"只要一句话/一个词"的小调用都走这里：给的 token 上限足够宽，
并在 content 为空时自动加倍重试一次。成本仍然很低（一次几百 token）。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

API_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_MODEL = "deepseek-flash"
FIRST_TOKENS = 512
RETRY_TOKENS = 1200


def chat(system: str, user: str, *, api_key: str, model: str = DEFAULT_MODEL,
         temperature: float = 0.0, timeout: float = 40.0,
         max_tokens: int = FIRST_TOKENS) -> str:
    """返回答案正文；失败或空返回空串。会自动为"思考"留足 token。"""
    if not api_key:
        return ""

    budgets = (max_tokens, max(max_tokens * 2, RETRY_TOKENS))
    for budget in budgets:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "max_tokens": budget,
        }
        req = urllib.request.Request(
            API_URL,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {api_key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            choice = (body.get("choices") or [{}])[0]
            content = str((choice.get("message") or {}).get("content") or "").strip()
            if content:
                return content
            # content 为空 = 被思考截断，加倍再试
        except urllib.error.HTTPError:
            return ""
        except Exception:
            return ""
    return ""
