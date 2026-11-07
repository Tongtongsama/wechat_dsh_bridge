"""检查微信客户端的 UI 自动化接口是否正常。

用途：桥接"不说话"的时候，先跑这个，一分钟内能分清是
  · 微信没开 / 关在托盘里
  · 开了但 UI 接口挂了（需要重启微信）
  · 接口正常，问题在桥接那边

用法：python tools/check_wechat.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    import wxauto_compat  # noqa: F401  （安装控件名兼容补丁）
    from adapters import _main_windows, _rect, force_foreground
    from wxauto4 import WeChat

    print("1) 找微信主窗口…")
    windows = _main_windows()
    if not windows:
        print("   没有找到主窗口 → 微信可能没开，或关在托盘里")
        print("   处理：双击托盘图标打开微信")
        return 1
    for pid, hwnd in windows:
        _, _, w, h = _rect(hwnd)
        print(f"   pid={pid} 窗口 {w}x{h}")

    print("\n2) 尝试挂载…")
    pid, hwnd = windows[0]
    force_foreground(hwnd)
    time.sleep(1.0)
    try:
        wx = WeChat(resize=False, ads=False)
        name = (wx.GetMyInfo() or {}).get("display_name")
        print(f"   挂载成功，账号：{name}")
    except Exception as exc:
        print(f"   挂载失败：{type(exc).__name__}: {exc}")
        return 1

    print("\n3) 读会话列表…")
    try:
        sessions = wx.GetSession()
    except Exception as exc:
        sessions = []
        print(f"   读取异常：{type(exc).__name__}")
    print(f"   读到 {len(sessions)} 个会话")
    if not sessions:
        print("   ⚠ 这是关键症状：会话列表读不到，说明微信的 UI 接口挂了")
        print("   处理：完全退出微信（托盘右键 → 退出），再重新打开登录")
        return 1

    print("\n4) 逐个打开聊天的测试（只读，不发消息）…")
    ok = 0
    for label in ("owner", "家人群"):
        hit = False
        for attempt in (1, 2, 3):
            try:
                wx.ChatWith(label)
                msgs = wx.GetAllMessage()
                print(f"   {label!r} → 成功（{len(msgs)} 条，第 {attempt} 次）")
                hit = True
                break
            except Exception:
                time.sleep(0.6)
        if not hit:
            print(f"   {label!r} → 3 次失败")
        ok += 1 if hit else 0

    print()
    if ok >= 2:
        print("✅ 微信接口正常。如果桥接还是不说话，去看 logs/bridge.log")
        return 0
    print("⚠ 接口部分可用（有的会话打不开）。建议重启微信客户端。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
