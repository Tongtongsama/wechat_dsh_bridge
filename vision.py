"""看图：把图片内容描述成文字，喂给只懂文字的 agent。

为什么要这一层：bridge 里跑的模型（DeepSeek）是纯文本的，图片直接给它
是浪费——它只会看到"对方发了一个图片"。所以先用 OpenAI 的视觉模型把图
转成一段描述，再把描述当上下文交给 agent。

成本控制：
  · 先缩到长边 768，token 数量级下降；
  · 提示词要求"只描述看到的东西，不超过 80 字"；
  · 调用失败一律返回空串，不阻塞回复。
"""
from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.request
from pathlib import Path

API_URL = "https://api.openai.com/v1/chat/completions"

_SYSTEM = (
    "你在帮一个看不见图片的助手描述图片。用中文、80 字以内，"
    "只描述画面里客观存在的东西：是什么、写了什么字、谁在做什么、什么情绪。"
    "不确定的不要编。不要评论、不要建议。\n"
    "如果是聊天截图，忽略里面的头像和昵称，只描述真正的画面内容。"
)


def _data_url(path: Path, max_px: int = 768) -> str | None:
    try:
        from PIL import Image

        with Image.open(path) as im:
            im = im.convert("RGB")
            if max(im.size) > max_px:
                scale = max_px / max(im.size)
                im = im.resize((int(im.width * scale), int(im.height * scale)))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=82)
            raw = buf.getvalue()
    except Exception:
        try:
            raw = Path(path).read_bytes()
        except Exception:
            return None
    return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")


def describe(path: str | Path, *, api_key: str, model: str = "gpt-4o-mini",
             timeout: float = 40.0, max_px: int = 768) -> str:
    """返回图片描述；任何失败都返回空串。"""
    p = Path(path)
    if not p.exists() or not api_key:
        return ""
    url = _data_url(p, max_px=max_px)
    if not url:
        return ""

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": [
                {"type": "text", "text": "这张图里有什么？"},
                {"type": "image_url", "image_url": {"url": url}},
            ]},
        ],
        "max_tokens": 160,
        "temperature": 0.2,
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
        return str(body["choices"][0]["message"]["content"]).strip()
    except urllib.error.HTTPError:
        return ""
    except Exception:
        return ""
