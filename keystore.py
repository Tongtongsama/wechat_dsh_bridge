"""密钥文件的安全读写。

2026-11-02 出过一次事故：往 secret.env 里加 OpenAI key 时，用正则 `sk-[A-Za-z0-9_-]+`
从整份文件里抓 key，结果贪婪匹配把下一行的 `..._KEY` 也吞进了 DeepSeek 的 key，
把一把好 key 写坏，导致所有任务报 AUTH 失败。

现在的规则（**任何写 key 的操作都必须走这里**）：

  1. 逐行解析 `NAME=value`，绝不对整份文件跑正则；
  2. 写之前备份，写完立刻回读校验——每个已知 key 的前缀和长度都要对得上；
  3. 校验不过就自动还原备份并报错，绝不留下写坏的文件；
  4. 原子替换（tmp + os.replace），中途崩溃不会留下半个文件。
"""
from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

DEFAULT_PATH = Path.home() / ".dsh" / "wechat-bridge" / "secret.env"

# 每个 key 的合法形态：前缀 + 最小长度
_RULES: dict[str, tuple[str, int]] = {
    "DEEPSEEK_API_KEY": ("sk-", 30),
    "OPENAI_API_KEY": ("sk-", 40),
    "OPENAI_ADMIN_KEY": ("sk-", 40),
}

_LINE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def load(path: Path | None = None) -> dict[str, str]:
    path = path or DEFAULT_PATH
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _LINE.match(line.strip())
        if m:
            out[m.group(1)] = m.group(2).strip()
    return out


def mask(value: str) -> str:
    if len(value) <= 8:
        return value[:2] + "***"
    return f"{value[:7]}…{value[-4:]} (len={len(value)})"


def verify(data: dict[str, str]) -> list[str]:
    """返回问题列表；空列表表示全部正常。"""
    problems = []
    for name, (prefix, minlen) in _RULES.items():
        val = data.get(name)
        if val is None:
            continue
        if not val.startswith(prefix):
            problems.append(f"{name} 前缀不对（应为 {prefix}）")
        elif len(val) < minlen:
            problems.append(f"{name} 长度异常（{len(val)} < {minlen}）")
        elif "=" in val or "_KEY" in val[10:]:
            problems.append(f"{name} 疑似把下一行拼进来了")
    return problems


def set_key(name: str, value: str, path: Path | None = None) -> dict[str, str]:
    """安全写入一个 key。任何异常都会还原原文件。"""
    path = path or DEFAULT_PATH
    value = (value or "").strip()
    if not value:
        raise ValueError("key 是空的")

    current = load(path)
    backup = None
    if path.exists():
        backup = path.with_name(f"{path.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)

    current[name] = value
    payload = "".join(f"{k}={v}\n" for k, v in current.items())
    tmp = path.with_name(path.name + ".tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, path)

    problems = verify(load(path))
    if problems:
        if backup and backup.exists():
            shutil.copy2(backup, path)
        raise RuntimeError("写入后校验失败，已还原：\n  " + "\n  ".join(problems))

    return load(path)
