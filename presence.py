"""上线 / 下线 / 重启 / 升级 的对外通知。

现在的输出通道是**群消息**（免费版没有朋友圈接口）。等以后拿到 Plus 授权，
把 `_publish_moments()` 里的占位换成 `wx.Moments().Publish(...)` 就行，
上层状态机不用动。

为什么需要"请求标记"：
  Windows 上 `Stop-Process -Force` 是硬杀，进程没有机会说"我下线了"。
  所以停止流程改成：先在 state/ 里放一个 `shutdown_requested` 文件 →
  桥接在下一轮循环看到它 → 主动发完下线通知 → 自己退出。
  bridge.ps1 会等它体面退出，超时才强杀。
"""
from __future__ import annotations

import random
import time
from pathlib import Path

import nudge

# 文案兜底：模型不可用时用这些，仍然随机取，不至于每次都一样
_FALLBACK = {
    "online": [
        "我上线了，有事随时喊我。",
        "回来了，在的。",
        "我到了，随时能找我。",
        "醒了醒了，在。",
    ],
    "offline": [
        "我先下线了，有事留言，回来就看。",
        "撤了，明天见。",
        "先走一步，回头聊。",
    ],
    "restarting": [
        "我回去一下，几分钟就回来。",
        "稍等我一会儿，马上好。",
        "让我缓一下，很快。",
    ],
    "resumed": [
        "回来了，可以继续。",
        "好了，我在。",
        "我回来了，一切正常。",
    ],
    "upgraded": [
        "换新了，试试看。",
        "更新完了，应该更顺手。",
    ],
}

_INSTRUCTION = {
    "online": "你刚回来/上线。用第一人称发一句话告诉大家你在了，简短、自然，"
              "不要提机器人、程序、启动、开机这类词。",
    "offline": "你要先离开下线了。用第一人称说一句，简短、自然。"
               "不要提机器人、程序、关机这类词。",
    "restarting": "你要离开一小会儿马上回来。用第一人称说一句，简短。"
                  "不要说重启、程序、机器人。",
    "resumed": "你回来了。用第一人称说一句，简短自然。不要说重启完成、程序。",
    "upgraded": "你换了新版本回来。用第一人称说一句，简短自然。不要说更新、版本号、程序。",
}


def _marker(state_dir: Path, name: str) -> Path:
    return Path(state_dir) / name


def request_shutdown(state_dir: Path, *, restart: bool) -> None:
    d = Path(state_dir)
    d.mkdir(parents=True, exist_ok=True)
    _marker(d, "shutdown_requested").write_text(str(int(time.time())), encoding="utf-8")
    if restart:
        _marker(d, "restart_pending").write_text(str(int(time.time())), encoding="utf-8")


def shutdown_requested(state_dir: Path) -> bool:
    return _marker(state_dir, "shutdown_requested").exists()


def clear_shutdown(state_dir: Path) -> None:
    for name in ("shutdown_requested",):
        p = _marker(state_dir, name)
        try:
            p.unlink()
        except Exception:
            pass


def _compose(kind: str, bridge, cfg: dict) -> str:
    if cfg.get("compose", True):
        try:
            text = nudge.compose(_INSTRUCTION.get(kind, "发一句简短的状态通知。"),
                                 api_key=bridge._env_api_key())
            if text:
                return text
        except Exception:
            pass
    return random.choice(_FALLBACK.get(kind, ["状态更新。"]))


def _publish_moments(bridge, text: str, cfg: dict) -> bool:
    """发朋友圈。免费版没有这个能力，拿到 Plus 授权后在这里接。"""
    wx = getattr(getattr(bridge, "adapter", None), "_wx", None)
    if wx is None or not hasattr(wx, "Moments"):
        return False
    try:
        pyq = wx.Moments()
        if pyq is None:
            return False
        pyq.Publish(text, None, (cfg.get("moments") or {}).get("privacy") or {})
        return True
    except Exception:
        return False


def _take(state_dir: Path, name: str) -> bool:
    p = _marker(state_dir, name)
    if not p.exists():
        return False
    try:
        p.unlink()
    except Exception:
        pass
    return True


def take_silent_stop(state_dir: Path) -> bool:
    """只压制**下线**播报，用 `presence_silent_stop` 标记。"""
    return _take(state_dir, "presence_silent_stop")


def take_silent(state_dir: Path) -> bool:
    """消费"本次启动不播报"标记。

    以前是改 config.json 里的 enabled，但那只对**下一次启动**有效，
    跑起来的进程仍然按旧配置走，于是停止时该发的通知被静默跳过，
    而日志还写着"已发出"——对着空气报成功。
    改成文件标记：只影响这一次进程的生命周期，不污染配置。
    """
    return _take(state_dir, "presence_silent")


def notify(bridge, cfg: dict, kind: str) -> bool:
    """按配置把状态变化发出去。返回**是否真的发了**。"""
    if not (cfg or {}).get("enabled"):
        return False

    text = _compose(kind, bridge, cfg)
    sent_to: list[str] = []

    if (cfg.get("moments") or {}).get("enabled") and _publish_moments(bridge, text, cfg):
        sent_to.append("moments")

    # 下线类通知可以单独配目标（比如家人群只看上线，不看离开）
    targets = cfg.get("targets") or []
    if kind in ("offline", "restarting") and cfg.get("stop_targets") is not None:
        targets = cfg.get("stop_targets") or []

    for target in targets:
        if target in getattr(bridge, "convs", {}):
            try:
                bridge._say(target, text)
                sent_to.append(target)
            except Exception:
                pass

    bridge._log("presence_notified", kind=kind, text=text, targets=sent_to)
    return bool(sent_to)
