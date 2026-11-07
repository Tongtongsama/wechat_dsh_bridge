"""真实入口：把微信消息接到 DSH。

跑法：python bridge_main.py
Ctrl+C 退出。
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import presence
import secret_env
from adapters import WxAutoAdapter
from bridge import Bridge
from runner import DshRunner

CONFIG = HERE / "config.json"
STATE = HERE / "state"
LOG = HERE / "logs" / "bridge.log"


def redirect_to_log() -> None:
    """自己把 stdout/stderr 写进日志文件。

    这样启动时可以直接用 pythonw（无控制台），不必再套一层 `cmd /c ... >> log`——
    那层 cmd 每启动一次就闪一个黑框，是用户看到"自动打开 cmd"的元凶。
    """
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOG, "a", encoding="utf-8", buffering=1)
    sys.stdout = fh
    sys.stderr = fh
    print(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====", flush=True)


def safe_handle(bridge, msg) -> None:
    """线程池里抛的异常默认会被吞掉，消息就静默消失了。这里兜住并打日志。"""
    try:
        bridge.handle(msg)
    except Exception:
        print(f"[错误] 处理消息失败 conv={msg.conv_id} text={msg.text[:60]!r}")
        traceback.print_exc()


def main() -> int:
    if not CONFIG.exists():
        print(f"缺少配置文件：{CONFIG}")
        return 2

    redirect_to_log()

    injected = secret_env.apply()
    print(f"[启动] 凭据：{injected or '(环境已有)'}")

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    cfg["_config_path"] = str(CONFIG)      # 主动回复开关要写回这里
    # 群名、主人昵称这类私密值存在 secret.env 里，启动时填回配置。
    # 这样仓库里的 config.json 是干净的，可以放心推到 GitHub。
    refilled = secret_env.apply_config(cfg)
    if refilled:
        print(f"[启动] 从 secret 填入私密配置：{', '.join(refilled)}")
    expected = cfg.get("wechat", {}).get("expected_account")
    print(f"[启动] 期望挂载账号：{expected!r}")

    # 微信窗口可能被关到托盘、或启动瞬间还没就绪。挂不上就等，不要把服务弄死。
    adapter = None
    attempt = 0
    while adapter is None:
        # 等待期间也要响应停止请求，否则"停止.cmd"发的信号它看不见，
        # 只能被超时强杀（实测踩过：微信窗口关着时停止要干等 20 秒）。
        if presence.shutdown_requested(STATE):
            print("[退出] 挂载微信之前就收到了停止请求，直接退出")
            presence.clear_shutdown(STATE)
            return 0
        attempt += 1
        try:
            # 图片入库：只在本人的私聊里下载图片，暂存在表情库的 _incoming 下
            sdir = Path(str(cfg.get("stickers_dir", "stickers")))
            if not sdir.is_absolute():
                sdir = CONFIG.parent / sdir
            adapter = WxAutoAdapter(
                cfg["conversations"],
                expected_account=expected,
                sweep_seconds=float(cfg.get("sweep_seconds", 60)),
                image_convs=set((cfg.get("vision") or {}).get("convs") or [])
                | {cfg["owner"]["conv_id"]},
                image_dir=sdir / "_incoming",
            )
        except Exception as exc:
            print(f"[等待 {attempt}] 挂载失败：{exc}")
            print("[等待] 请确认备用号微信窗口是打开的（点托盘图标），20 秒后重试…")
            # 拆成小步 sleep，这样停止请求最多 1 秒就能被看到
            for _ in range(20):
                if presence.shutdown_requested(STATE):
                    print("[退出] 等待挂载期间收到停止请求，退出")
                    presence.clear_shutdown(STATE)
                    return 0
                time.sleep(1)

    bridge = Bridge(cfg, DshRunner(), adapter, state_dir=STATE)

    print("[启动] 监听中，以下会话会被响应：")
    for conv in cfg["conversations"]:
        trig = "任何消息" if conv.get("trigger") == "always" else f"需含 {'/'.join([cfg['bot_mention'], *conv.get('aliases', [])])}"
        print(f"   - {conv['name']:<18} {trig}")

    print("[启动] 就绪。Ctrl+C 退出。\n")

    # ---------- 上线 / 重启完成 通知 ----------
    pcfg = cfg.get("presence") or {}
    if presence.shutdown_requested(STATE):
        # 上次异常退出留下的标记，清掉免得下次开机就自动下线
        presence.clear_shutdown(STATE)
    # 静默拆成两个标记，各管一件事：
    #   presence_silent       → 只压这次的上线/恢复播报
    #   presence_silent_stop  → 只压这次的下线播报
    # 之前一个标记管全场，导致"为了安静重启"设的标记把之后的正常下线也吞了。
    silent = presence.take_silent(STATE)
    silent_stop = presence.take_silent_stop(STATE)
    if silent:
        print("[启动] 本次不发上线播报")

    restart_marker = STATE / "restart_pending"
    if restart_marker.exists():
        if not silent:
            presence.notify(bridge, pcfg, "resumed")
        try:
            restart_marker.unlink()
        except Exception:
            pass
    elif pcfg.get("on_start", True) and not silent:
        presence.notify(bridge, pcfg, "online")

    # 不同会话并发跑，避免一个 100 秒的任务堵住所有人；
    # 同一会话内部由 Bridge 的会话锁串行，微信 UI 操作由适配器的 IO 锁串行。
    from concurrent.futures import ThreadPoolExecutor

    workers = int(cfg.get("max_workers", 3))
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="conv")
    print(f"[启动] 并发会话数：{workers}\n")

    # 主动私聊（深夜提醒等）。默认关，config 里 nudges.enabled 打开
    nudge_thread = None
    ncfg = cfg.get("nudges") or {}
    if ncfg.get("enabled"):
        from nudge import NudgeScheduler

        nudge_thread = NudgeScheduler(bridge, ncfg)
        nudge_thread.start()
        print(f"[主动] 已启用，每 {ncfg.get('check_interval_minutes', 10)} 分钟检查一次\n")

    # 余额监控：充钱后主动来道谢
    watch_thread = None
    wcfg = cfg.get("balance_watch") or {}
    if wcfg.get("enabled"):
        from nudge import BalanceWatcher

        watch_thread = BalanceWatcher(bridge, wcfg)
        watch_thread.start()
        print(f"[余额] 监控已启用，每 {wcfg.get('check_interval_minutes', 10)} 分钟查一次\n")
    try:
        while True:
            # 收到停止请求 → 体面地说一句再见再退出
            if presence.shutdown_requested(STATE):
                kind = "restarting" if restart_marker.exists() else "offline"
                # 先存档（git 可精确回退单个文件），再打 zip，最后推 GitHub
                try:
                    ok, detail = bridge.repo.snapshot("auto: 停止前快照")
                    if ok:
                        print(f"[存档] {detail}")
                except Exception:
                    traceback.print_exc()
                try:
                    made = bridge.backups.create("shutdown")
                    if made:
                        print(f"[备份] 已保存 {made.name}")
                except Exception:
                    traceback.print_exc()
                if bridge._push_on_shutdown:
                    try:
                        ok, detail = bridge.push_to_github()
                        print(f"[推送] {'已同步到 GitHub' if ok else '失败：' + detail[:200]}")
                    except Exception:
                        traceback.print_exc()
                sent = False
                if pcfg.get("on_stop", True) and not silent_stop:
                    sent = presence.notify(bridge, pcfg, kind)
                presence.clear_shutdown(STATE)
                if sent:
                    print(f"[退出] 收到停止请求，已发出「{kind}」通知")
                elif silent_stop:
                    print("[退出] 本次静默，未发下线通知")
                else:
                    print("[退出] 下线通知没发出去（检查 presence.enabled / targets）")
                break
            try:
                batch = adapter.poll()
                for msg in batch:
                    # 先合并连发的短消息，再交给处理线程
                    bridge.enqueue(msg)
                time.sleep(0.5 if batch else 1.0)
            except Exception:
                print("[错误] 单轮处理异常，已隔离，继续运行：")
                traceback.print_exc()
                time.sleep(2)
    except KeyboardInterrupt:
        print("\n[退出] 收到 Ctrl+C")
        pool.shutdown(wait=False, cancel_futures=True)
        adapter.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
