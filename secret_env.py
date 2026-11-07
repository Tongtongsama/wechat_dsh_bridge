"""从受 ACL 保护的 secret.env 读取凭据，不落进代码和日志。

注意：不要命名成 secrets.py，那会遮蔽标准库的 secrets 模块。
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_PATH = Path.home() / ".dsh" / "wechat-bridge" / "secret.env"


def load(path: Path | None = None) -> dict[str, str]:
    path = path or DEFAULT_PATH
    if not path.exists():
        return {}
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def apply(path: Path | None = None) -> list[str]:
    """把 secret.env 里的键注入当前进程环境（已有同名变量不覆盖）。"""
    injected = []
    for k, v in load(path).items():
        if not os.environ.get(k):
            os.environ[k] = v
            injected.append(k)
    return injected


# secret.env 里可以覆盖的私密配置项。
# 群名、主人昵称这类东西不该出现在会推送到 GitHub 的 config.json 里，
# 所以它们放在 secret 里，启动时填回配置。
_NAME_KEYS = {
    "user:owner": "CONV_NAME_OWNER",
    "group:parents": "CONV_NAME_PARENTS",
    "group:friends": "CONV_NAME_FRIENDS",
}


def apply_config(config: dict, path: Path | None = None) -> list[str]:
    """把 secret.env 里的私密配置覆盖进 config（原地修改，返回被覆盖的项）。

    只覆盖**有值**的那些，secret 里没写的保持 config.json 原样，
    这样即使不用 secret 也能跑（只是群名可能为空）。
    """
    data = load(path)
    applied: list[str] = []

    for conv in config.get("conversations") or []:
        key = _NAME_KEYS.get(str(conv.get("conv_id")))
        if not key:
            continue
        val = (data.get(key) or "").strip()
        if val:
            conv["wechat_name"] = val
            applied.append(key)

    aliases = (data.get("OWNER_ALIASES") or "").strip()
    if aliases:
        config["owner_aliases"] = [
            a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()
        ]
        applied.append("OWNER_ALIASES")

    return applied


