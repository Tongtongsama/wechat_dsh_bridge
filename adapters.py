"""微信接入层适配器。

Bridge 只认 Adapter 这个接口，具体用哪个微信方案由这里替换，核心逻辑不动。
"""
from __future__ import annotations

import ctypes
import json
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

# 必须在导入 wxauto4 之前装好兼容补丁（详见 wxauto_compat.py）
import wxauto_compat  # noqa: F401

user32 = ctypes.WinDLL("user32", use_last_error=True)
SW_RESTORE = 9
SW_SHOW = 5
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002


@dataclass
class Inbound:
    conv_id: str
    sender: str
    text: str
    ts: float = field(default_factory=time.time)
    attachment: str | None = None   # 图片等附件的本地路径（仅对配置的会话下载）
    kind: str = ""                 # 原始消息类型：text / image / emotion …


class Adapter(Protocol):
    def poll(self) -> list[Inbound]: ...
    def send(self, conv_id: str, text: str) -> None: ...
    def send_file(self, conv_id: str, path: str | Path) -> None: ...


# --------------------------------------------------------------------------- #
# 窗口工具
# --------------------------------------------------------------------------- #
def _visible_windows(pid: int) -> list[int]:
    found: list[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        p = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def _main_windows() -> list[tuple[int, int]]:
    """枚举所有微信主窗口 (pid, hwnd)。

    为什么不用 PowerShell 的 MainWindowHandle：
      · 窗口最小化时矩形被搬到 (-21333,-21333) 只剩 158x26，按尺寸筛会全丢掉；
      · 窗口收进托盘时 MainWindowHandle 直接变 0，连候选都没有。

    为什么不用 `mmui::MainWindow`：
      那是 UIA 层的类名，Win32 侧真实的窗口类是 Qt 的
      `Qt51514QWindowIcon`，标题是 `WeChat` / `微信`。按后者找才准。
    """
    found: list[tuple[int, int]] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        cls = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, cls, 256)
        if not cls.value.startswith("Qt"):
            return True                # 微信主窗口是 Qt 画的
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        title = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, title, n + 1)
        if title.value.strip() not in ("WeChat", "微信"):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append((int(pid.value), int(hwnd)))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    return found


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _activate(hwnd: int) -> bool:
    """把窗口切到前台。

    原来用「模拟按 ALT」的偏方解锁前台权限——那个偏方本身会抢焦点、
    还会在有的程序里展开菜单栏。改用 AttachThreadInput，这是标准做法，
    副作用最小。
    """
    if not hwnd:
        return False
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    tid_fg = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    tid_me = kernel32.GetCurrentThreadId()
    attached = False
    try:
        if tid_fg and tid_fg != tid_me:
            attached = bool(user32.AttachThreadInput(tid_me, tid_fg, True))
        user32.ShowWindow(hwnd, SW_RESTORE)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False
    finally:
        if attached:
            user32.AttachThreadInput(tid_me, tid_fg, False)


def force_foreground(hwnd: int) -> bool:
    """把窗口置前。wxauto4 的 WeChat() 挑前台窗口，多开时必须靠这个定向。"""
    time.sleep(0.15)
    return _activate(hwnd)


def restore_foreground(hwnd: int) -> bool:
    """把前台还给用户原来操作的窗口。"""
    return _activate(hwnd)


def _no_window_kwargs() -> dict:
    """不让子进程弹控制台窗口。

    桥接跑在 pythonw（无控制台）下时，任何 console 子进程都会被分配一个**新窗口**——
    这就是"时不时弹出 cmd 黑框"的来源。必须显式禁止。
    """
    import subprocess

    if not hasattr(subprocess, "CREATE_NO_WINDOW"):
        return {}
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": info}


def _weixin_pids() -> list[int]:
    ps = (
        "Get-Process Weixin -ErrorAction SilentlyContinue | "
        "Where-Object { $_.MainWindowHandle -ne 0 } | "
        "ForEach-Object { $_.Id }"
    )
    import subprocess

    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        **_no_window_kwargs(),
    )
    return [int(x) for x in (out.stdout or "").split() if x.strip().isdigit()]


# --------------------------------------------------------------------------- #
# 离线测试用
# --------------------------------------------------------------------------- #
class MockAdapter:
    def __init__(self, script_path: str, out_path: str) -> None:
        self._queue: list[Inbound] = []
        self._out = Path(out_path)
        self._out.parent.mkdir(parents=True, exist_ok=True)
        self._out.write_text("", encoding="utf-8")
        for line in Path(script_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("//"):
                continue
            row = json.loads(line)
            self._queue.append(Inbound(row["conv_id"], row.get("sender", "?"), row["text"]))

    def poll(self) -> list[Inbound]:
        batch, self._queue = self._queue, []
        return batch

    def send(self, conv_id: str, text: str) -> None:
        with self._out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"conv_id": conv_id, "text": text}, ensure_ascii=False) + "\n")

    def send_file(self, conv_id: str, path: str | Path) -> None:
        with self._out.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(
                {"conv_id": conv_id, "sticker": Path(path).name}, ensure_ascii=False
            ) + "\n")


# --------------------------------------------------------------------------- #
# 真实接入
# --------------------------------------------------------------------------- #
class WxAutoAdapter:
    """微信 4.1.8.107 + wxauto4 免费版（Windows UI Automation，非注入）。

    两级轮询，目的是把"抢前台焦点"的次数压到最低：

      1. 空闲时只读左侧会话列表 `GetSession()`——**不切会话、不激活窗口**，
         拿到每个会话的最后消息预览、时间和未读数；
      2. 只有当某个目标会话的 (时间, 预览) 变了或未读 > 0，才 `ChatWith` 打开它，
         读全文、回复，然后把前台还给用户一次。

    `ChatWith`/`SendMsg` 必须激活窗口（要操作搜索框和输入框），这是 UI 自动化的
    硬约束，无法关掉；所以只能减少调用次数，不能消除。

    免费版限制：`AddListenChat` 一个实例只允许监听一个聊天，所以不用事件监听。

    已知限制：窗口关到托盘后 UIA 找不到控件；会自动尝试 `Show()` 拉回。
    """

    def __init__(
        self,
        conversations: list[dict],
        *,
        expected_account: str,
        settle: float = 0.45,
        sweep_seconds: float = 60.0,
        image_convs: set[str] | None = None,
        image_dir: str | Path | None = None,
        verbose: bool = True,
    ) -> None:
        self._verbose = verbose
        self._settle = settle
        self._sweep_seconds = sweep_seconds
        self._last_sweep = time.time()
        # 只在指定会话里下载图片（家人群里图片多，全下会拖慢轮询）
        self._image_convs = set(image_convs or [])
        self._image_dir = Path(image_dir) if image_dir else None
        if self._image_dir:
            self._image_dir.mkdir(parents=True, exist_ok=True)
        self._name_by_conv: dict[str, str] = {}
        self._conv_by_name: dict[str, str] = {}
        for conv in conversations:
            name = conv.get("wechat_name")
            if not name:
                continue
            self._name_by_conv[conv["conv_id"]] = name
            self._conv_by_name[name] = conv["conv_id"]

        self._wx = self._attach(expected_account)
        self._self_name = (self._wx.GetMyInfo() or {}).get("display_name")

        # 启动时没能接上的会话：之后每轮轮询都重试，别让一个群永久失聪
        self._expected_account = expected_account
        self._pending_attach: set[str] = set()
        self._attach_retry_at: float = 0.0
        self._last_fp: dict[str, str] = {}
        self._session_fp: dict[str, tuple] = {}
        self._recent_sent: dict[str, list[tuple[float, str]]] = {}
        self._fail_streak = 0
        self._show_attempts = 0
        # 微信 UI 操作必须串行：上层即使并发（不同会话），底层也只有一个 UI 线程
        import threading as _threading

        self._io_lock = _threading.Lock()

        # 启动时把每个会话打开一次，记下"最后一条消息的指纹"作为锚点
        prev_fg = user32.GetForegroundWindow()
        for name in self._conv_by_name:
            try:
                self._wx.ChatWith(name)
                time.sleep(self._settle)
                msgs = self._wx.GetAllMessage()
                self._last_fp[name] = _fp(msgs[-1]) if msgs else ""
                self._log(f"已接入 {name!r} → {self._conv_by_name[name]}"
                          f"（读到 {len(msgs)} 条，锚点={'有' if msgs else '空会话'}）")
            except Exception as exc:
                self._log(f"接入 {name!r} 失败：{exc!r}")
        restore_foreground(prev_fg)

    # ---------- 挂载 ----------
    def _attach(self, expected: str):
        from wxauto4 import WeChat

        candidates = _main_windows()
        if not candidates:
            raise RuntimeError(
                "没有找到微信主窗口，请先登录微信（托盘图标双击打开）")

        # 记住用户当前的前台窗口：探测过程会短暂激活微信窗口，结束后必须还回去，
        # 否则后台重试会变成「每 20 秒抢一次焦点」。
        prev_fg = user32.GetForegroundWindow()
        tried = []
        try:
            for pid, hwnd in candidates:
                # 最小化的窗口矩形是 (-21333,-21333,158,26)，靠它判断尺寸会永远失败；
                # 托盘里藏着的窗口 IsWindowVisible 为 False。两种都先恢复出来。
                if user32.IsIconic(hwnd):
                    user32.ShowWindow(hwnd, SW_RESTORE)
                    time.sleep(0.3)
                if not user32.IsWindowVisible(hwnd):
                    user32.ShowWindow(hwnd, SW_SHOW)
                    time.sleep(0.3)

                # 只要窗口是正常的形态就试。
                # 原来这里卡 w < 500，结果用户把微信窗口拉窄到 435px 就再也挂不上了——
                # 窗口宽度跟"是不是主窗口"没有关系，前面已经按 Qt 类名 + 标题筛过了，
                # 这里只做最基本的健全性检查。
                _, _, w, _h = _rect(hwnd)
                if w < 120 or _h < 80:
                    tried.append(f"pid={pid}: 窗口尺寸异常 {w}x{_h}")
                    continue

                self._hwnd = hwnd
                self._ensure_window_awake()
                force_foreground(hwnd)
                time.sleep(0.5)
                try:
                    wx = WeChat(resize=False, ads=False)
                    name = (wx.GetMyInfo() or {}).get("display_name")
                except Exception as exc:
                    tried.append(f"pid={pid}: {exc!r}")
                    restore_foreground(prev_fg)
                    continue
                tried.append(f"pid={pid}: {name}")
                if name == expected:
                    self._hwnd = hwnd
                    self._log(f"已挂载 pid={pid}，账号={name} ✅")
                    return wx
                restore_foreground(prev_fg)
        finally:
            restore_foreground(prev_fg)

        raise RuntimeError(f"挂载失败：期望账号 {expected!r}，实际尝试 {tried}")

    # ---------- 轮询 ----------
    def poll(self) -> list[Inbound]:
        with self._io_lock:
            try:
                self._retry_pending_attach()
                batch = self._poll_locked()
                self._fail_streak = 0
                return batch
            except Exception as exc:
                self._fail_streak += 1
                self._log(f"轮询失败（连续 {self._fail_streak} 次）：{type(exc).__name__}")
                # 连续失败说明微信可能被关了/重启了：重新挂载一次
                if self._fail_streak >= 5:
                    self._fail_streak = 0
                    self._reconnect()
                return []

    def _reconnect(self) -> None:
        """掉线重连：微信被重启后，旧的窗口对象已经失效。"""
        try:
            self._log("尝试重新挂载微信…")
            wx = self._attach(self._expected_account)
            self._wx = wx
            self._self_name = (wx.GetMyInfo() or {}).get("display_name")
            self._last_fp.clear()
            self._pending_attach = set(self._conv_by_name)
            self._attach_retry_at = 0.0
            self._log("重新挂载成功，等待补接入各会话")
        except Exception as exc:
            self._log(f"重新挂载失败：{type(exc).__name__}")

    def _poll_locked(self) -> list[Inbound]:
        self._ensure_window_awake()
        try:
            sessions = self._wx.GetSession()
        except Exception as exc:
            self._on_failure(exc)
            return []

        snapshot: dict[str, tuple] = {}
        for s in sessions:
            name = getattr(s, "name", None)
            if not name:
                continue
            try:
                new_count = int(getattr(s, "new_count", 0) or 0)
            except Exception:
                new_count = 0
            snapshot[name] = (
                str(getattr(s, "time", "") or ""),
                str(getattr(s, "content", "") or ""),
                new_count,
            )

        todo: list[str] = []
        for name in self._conv_by_name:
            cur = snapshot.get(name)
            if cur is None:
                continue
            prev = self._session_fp.get(name)
            self._session_fp[name] = cur
            if prev is None:
                continue                      # 首次只记快照
            if cur != prev:
                todo.append(name)             # 时间/预览/未读变了 → 值得打开

        # 兜底扫描：会话列表的指纹会漏事（实测同一句话连发两次，预览文字完全相同，
        # 只看"最后一条预览"看不出变化）。所以每隔一段时间强制开一遍，
        # 反正消息游标是按"锚点指纹"比的，重复扫描不会重复回复。
        sweep = float(self._sweep_seconds or 0)
        if sweep > 0 and time.time() - self._last_sweep >= sweep:
            self._last_sweep = time.time()
            for name in self._conv_by_name:
                if name not in todo:
                    todo.append(name)
            self._log(f"兜底扫描：检查 {len(todo)} 个会话")

        if not todo:
            if self._fail_streak:
                self._log(f"✅ 读取恢复正常（此前连续失败 {self._fail_streak} 轮）")
                self._fail_streak = 0
            return []

        out: list[Inbound] = []
        prev_fg = user32.GetForegroundWindow()
        opened = False
        for name in todo:
            conv_id = self._conv_by_name[name]
            try:
                prev_fg = user32.GetForegroundWindow()
                self._wx.ChatWith(name)
                opened = True
                # 切完立刻把前台还回去，再等渲染。UIA 读控件树不要求窗口在前台，
                # 所以这 0.45 秒没必要继续霸着用户的焦点。
                restore_foreground(prev_fg)
                time.sleep(self._settle)
                msgs = self._wx.GetAllMessage()
            except Exception as exc:
                self._on_failure(exc)
                continue

            self._fail_streak = 0

            # 用"上次最后一条消息的指纹"作锚点，而不是消息条数：
            # 微信的消息列表可见范围会随窗口状态浮动（实测同一个群有时读到 1 条、
            # 有时读到 6 条），用条数做差会把历史当成新消息刷一遍。
            fp_anchor = self._last_fp.get(name, "")
            fresh = []
            if fp_anchor:
                idx = next(
                    (i for i in range(len(msgs) - 1, -1, -1) if _fp(msgs[i]) == fp_anchor),
                    None,
                )
                if idx is None:
                    self._log(f"{name!r} 找不到上次的锚点消息，重置基线（本轮不回复，避免刷屏）")
                    fresh = []
                else:
                    fresh = msgs[idx + 1:]
            elif len(msgs) == 1:
                # 基线是空会话，现在只有一条，那就是刚来的新消息
                fresh = msgs
            else:
                fresh = []

            if msgs:
                self._last_fp[name] = _fp(msgs[-1])

            texts: list[str] = []
            last_sender = name
            attachment: str | None = None
            last_kind = ""
            for msg in fresh:
                if _is_ignorable(msg, self._self_name):
                    continue
                sender = str(getattr(msg, "sender", None) or name)
                text = describe_message(msg)
                if self._looks_like_echo(conv_id, text):
                    self._log(f"丢弃回声：{text[:30]!r}")
                    continue
                mtype = str(getattr(msg, "type", "") or "").lower()
                if conv_id in self._image_convs:
                    got = None
                    if mtype == "image":
                        got = self._download_image(msg, name)
                    elif mtype == "emotion" and self._emotion_needs_shot(msg):
                        # 表情包多数自带文字提示（"动画表情 [紧张]"），有提示就不用截屏；
                        # 没有提示的才截一帧交给视觉模型，成本只花在真正缺信息的图上。
                        got = self._screenshot_message(msg)
                        if got:
                            self._log("表情改用截屏")
                    if got:
                        attachment = got
                        last_kind = mtype
                texts.append(text)
                last_sender = sender

            if texts or attachment:
                out.append(Inbound(conv_id, last_sender, "\n".join(texts),
                                   attachment=attachment, kind=last_kind))

        if opened:
            restore_foreground(prev_fg)       # 整个轮询只还原一次
        return out

    # ---------- 故障处理 ----------
    def _on_failure(self, exc: Exception) -> None:
        self._fail_streak += 1
        if self._fail_streak == 1:
            self._log(f"⚠ 读取失败，微信窗口可能被关到托盘了（{exc!r}）。尝试拉回窗口…")
        # 每失败 5 次尝试 Show() 拉回窗口（窗口被关到托盘时 UIA 找不到控件）
        if self._fail_streak % 5 == 0:
            self._show_attempts += 1
            try:
                self._wx.Show()
                self._log(f"已调用 Show() 尝试恢复窗口（第 {self._show_attempts} 次）")
            except Exception as e:
                self._log(f"Show() 失败：{e!r}")
        elif self._fail_streak % 30 == 0:
            self._log(f"⚠ 仍在失败（连续 {self._fail_streak} 轮）")

    # ---------- 发送 ----------
    def send(self, conv_id: str, text: str) -> None:
        self._ensure_window_awake()
        with self._io_lock:
            self._send_locked(conv_id, text)

    def send_file(self, conv_id: str, path: str | Path) -> None:
        self._ensure_window_awake()
        with self._io_lock:
            self._send_file_locked(conv_id, path)

    def _send_locked(self, conv_id: str, text: str) -> None:
        name = self._name_by_conv.get(conv_id)
        if not name:
            raise KeyError(f"没有为 {conv_id} 配置微信会话名")

        prev_fg = user32.GetForegroundWindow()
        self._wx.SendMsg(text, who=name)
        self._remember_sent(conv_id, text)

        # 自己刚发出去的消息会进列表，锚点要跟上。
        # 这里**不再** ChatWith —— SendMsg 已经停在那个会话里了，再切一次
        # 等于多发一次"激活窗口"，纯属多余地抢焦点。
        try:
            msgs = self._wx.GetAllMessage()
            if msgs:
                self._last_fp[name] = _fp(msgs[-1])
        except Exception:
            pass

        restore_foreground(prev_fg)

    def _send_file_locked(self, conv_id: str, path: str | Path) -> None:
        """发本地文件。实测：PNG/GIF 会在微信里显示成图片消息，不是文件卡片。"""
        name = self._name_by_conv.get(conv_id)
        if not name:
            raise KeyError(f"没有为 {conv_id} 配置微信会话名")
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(p)

        prev_fg = user32.GetForegroundWindow()
        self._wx.SendFiles(str(p), who=name)
        # 自己发的图片也会进消息列表，锚点要跟上（同样不重复 ChatWith）
        try:
            msgs = self._wx.GetAllMessage()
            if msgs:
                self._last_fp[name] = _fp(msgs[-1])
        except Exception:
            pass
        restore_foreground(prev_fg)

    def _ensure_window_awake(self) -> None:
        """确保微信窗口可见、够大——否则 UI 自动化读不到会话列表。

        实测三种"看不见"的状态都会让 GetSession() 返回 0：
          · 最小化：坐标 (-21333,-21333)      ← IsIconic 有时还报 False
          · 收进托盘：坐标 (-32000,-32000)
          · 窗口过窄：435px，聊天列表被折叠
        """
        hwnd = getattr(self, "_hwnd", 0)
        if not hwnd:
            return
        try:
            left, top, width, height = _rect(hwnd)
            off_screen = left <= -10000 or top <= -10000
            hidden = off_screen or not user32.IsWindowVisible(hwnd) \
                or user32.IsIconic(hwnd)
            if hidden:
                if hwnd == user32.GetForegroundWindow():
                    pass
                user32.ShowWindow(hwnd, SW_SHOW)
                user32.ShowWindow(hwnd, SW_RESTORE)
                time.sleep(0.6)
                left, top, width, height = _rect(hwnd)
                self._log(f"微信窗口原本不可见（{left},{top} {width}x{height}），已恢复")
            if width < 600:
                # 太窄时左侧会话列表会被折叠，UIA 直接读不到
                user32.MoveWindow(hwnd, max(left, 0), max(top, 0),
                                  1000, max(height, 700), True)
                time.sleep(0.5)
                self._log(f"微信窗口过窄（{width}px），已拉到 1000px")
        except Exception as exc:
            self._log(f"恢复窗口失败：{type(exc).__name__}")

    def _open_chat(self, name: str, attempts: int = 3) -> bool:
        """打开会话。微信的 UIA 偶发抽风，单次失败不代表真打不开。"""
        for i in range(1, attempts + 1):
            try:
                self._ensure_window_awake()
                self._wx.ChatWith(name)
                return True
            except Exception as exc:
                self._log(f"ChatWith {name!r} 第 {i} 次失败：{type(exc).__name__}")
                time.sleep(0.6 * i)
        return False

    def _retry_pending_attach(self) -> None:
        """把启动时没接上的会话再试一遍（最多每 30 秒一次）。"""
        if not self._pending_attach:
            return
        now = time.time()
        if now < self._attach_retry_at:
            return
        self._attach_retry_at = now + 30
        self._ensure_window_awake()
        for name in list(self._pending_attach):
            try:
                if not self._open_chat(name, attempts=1):
                    self._log(f"补接入 {name!r} 仍打不开")
                    continue
                msgs = self._wx.GetAllMessage()
                self._last_fp[name] = _fp(msgs[-1]) if msgs else ""
                self._pending_attach.discard(name)
                self._log(f"补接入 {name!r} 成功（{len(msgs)} 条，锚点=有）")
            except Exception as exc:
                self._log(f"补接入 {name!r} 仍失败：{type(exc).__name__}")

    def _name_of(self, conv_id: str) -> str | None:
        for name, cid in self._conv_by_name.items():
            if cid == conv_id:
                return name
        return None

    def fetch_recent_media(self, conv_id: str, sender: str = "") -> tuple[str, str] | None:
        """回看某人在本会话里最近发的一张图/表情。

        返回 ("path", 本地路径) 或 ("caption", 表情名字)，找不到返回 None。
        "@ 它 + 看一眼" 这种唤醒靠它把上一条内容捞出来。
        """
        name = self._name_of(conv_id)
        if not name:
            return None
        prev_fg = user32.GetForegroundWindow()
        try:
            self._wx.ChatWith(name)
            time.sleep(0.5)
            msgs = self._wx.GetAllMessage()
            cands = [m for m in msgs[-15:]
                     if str(getattr(m, "type", "") or "").lower() in ("image", "emotion")
                     and (not sender or str(getattr(m, "sender", "") or "") == sender)]
            if not cands:
                return None
            m = cands[-1]
            if str(getattr(m, "type", "")).lower() == "emotion":
                content = str(getattr(m, "content", "") or "")
                mm = _CAPTION_RE.search(content)
                if mm:
                    cap = mm.group(1).strip()
                    if cap in ("动画表情", "表情"):
                        cap = content[mm.end():].strip()
                    if cap:
                        return ("caption", cap)
                shot = self._screenshot_message(m)
                return ("path", shot) if shot else None
            got = self._download_image(m, name)
            return ("path", got) if got else None
        except Exception as exc:
            self._log(f"回看媒体失败：{exc!r}")
            return None
        finally:
            restore_foreground(prev_fg)

    def _emotion_needs_shot(self, msg) -> bool:
        """表情要不要靠截屏补充信息。

        微信把自定义表情的名字写在控件名里，形如「动画表情 [紧张]」或
        「[动画表情]爱了」。能读到名字就够 agent 理解了，不必再花钱看图。
        """
        content = str(getattr(msg, "content", "") or "")
        m = _CAPTION_RE.search(content)
        if not m:
            return True
        cap = m.group(1).strip()
        if cap in ("动画表情", "表情"):
            return not content[m.end():].strip()
        return False

    def _download_image(self, msg, conv_name: str | None = None) -> str | None:
        """把消息里的图片存到本地，返回路径。

        wxauto 的 download() 偶尔会抛
        `AttributeError: 'NoneType' object has no attribute 'TextControl'`
        ——那张消息的 UIA 元素失效了（被虚拟滚动回收，或是表情包）。
        实测同一批里有的能下、有的不能，所以这里**重开一次会话再试**，
        重试时按"同一发送者的最后一张图"重新拿元素。
        """
        target_dir = str(self._image_dir) if self._image_dir else None

        def attempt(obj):
            try:
                resp = obj.download(dir_path=target_dir)
            except Exception as exc:
                return None, exc
            if isinstance(resp, dict):
                resp = resp.get("path") or resp.get("file")
            if resp and Path(str(resp)).exists():
                return str(resp), None
            return None, RuntimeError(f"下载返回非路径：{resp!r}")

        path, err = attempt(msg)
        if path:
            return path

        sender = str(getattr(msg, "sender", "") or "")
        if conv_name:
            try:
                time.sleep(0.3)
                self._wx.ChatWith(conv_name)
                time.sleep(0.6)
                fresh = self._wx.GetAllMessage()
                cands = [m for m in fresh
                         if str(getattr(m, "type", "") or "").lower() == "image"
                         and str(getattr(m, "sender", "") or "") == sender]
                if cands:
                    path2, err2 = attempt(cands[-1])
                    if path2:
                        self._log("图片下载重试成功")
                        return path2
                    err = err2 or err
            except Exception as exc:
                err = exc

        # 最后兜底：直接截屏这条消息。
        # wxauto 的 download() 对"别人发的图"会报
        # AttributeError: 'NoneType' object has no attribute 'TextControl'
        # （实测自己发的能下、别人发的下不了，roll_into_view 也救不回来）。
        # 但把消息滚到可见位置后截屏是可靠的，视觉模型也只需要看到画面。
        shot = self._screenshot_message(msg)
        if shot:
            self._log("图片改用截屏兜底")
            return shot

        self._log(f"图片下载失败（{sender or '未知发送者'}）：{err!r}")
        return None

    def _screenshot_message(self, msg) -> str | None:
        """截取一条消息的屏幕区域，存成 PNG。需要窗口在前台。"""
        if not self._image_dir:
            return None
        prev_fg = user32.GetForegroundWindow()
        try:
            try:
                msg.roll_into_view()
            except Exception:
                pass
            hwnd = getattr(self, "_hwnd", 0)
            if hwnd:
                force_foreground(hwnd)
            time.sleep(0.45)
            try:
                rect = msg.control.BoundingRectangle
            except Exception:
                return None
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 40 or h < 40:
                return None
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
            name = f"shot_{int(time.time() * 1000)}.png"
            target = Path(self._image_dir) / name
            target.parent.mkdir(parents=True, exist_ok=True)
            img.save(target, "PNG")
            # 截屏会越攒越多，只留最近 40 张
            try:
                old = sorted(Path(self._image_dir).glob("shot_*.png"),
                             key=lambda x: x.stat().st_mtime, reverse=True)
                for f in old[40:]:
                    f.unlink(missing_ok=True)
            except Exception:
                pass
            return str(target)
        except Exception:
            return None
        finally:
            if prev_fg:
                restore_foreground(prev_fg)

    # ---------- 防回声 ----------
    def _remember_sent(self, conv_id: str, text: str) -> None:
        bucket = self._recent_sent.setdefault(conv_id, [])
        now = time.time()
        bucket.append((now, text))
        self._recent_sent[conv_id] = [(t, x) for t, x in bucket if now - t < 300][-20:]

    def _looks_like_echo(self, conv_id: str, text: str) -> bool:
        return is_echo(self._recent_sent.get(conv_id, []), text)

    def close(self) -> None:
        pass

    def _log(self, msg: str) -> None:
        if self._verbose:
            print(f"[wxauto] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# 消息过滤
# --------------------------------------------------------------------------- #
_SYSTEM_SENDERS = {"system", "self", ""}
_SYSTEM_TYPES = {"self", "sys", "system", "time", "recall", "tips"}

_TIME_ONLY_RE = __import__("re").compile(
    r"^(\d{1,2}:\d{2}|(\d{4}年)?\d{1,2}月\d{1,2}日.*\d{1,2}:\d{2}|"
    r"昨天.*\d{1,2}:\d{2}|星期.*\d{1,2}:\d{2})$"
)


def _is_ignorable(msg, self_name: str | None) -> bool:
    """过滤时间分隔条、系统提示、自己发的消息——这些不能当用户输入。"""
    sender = (getattr(msg, "sender", None) or "").strip()
    mtype = str(getattr(msg, "type", None) or "").strip().lower()
    content = (getattr(msg, "content", "") or "").strip()

    if sender in _SYSTEM_SENDERS:
        return True
    if mtype in _SYSTEM_TYPES:
        return True
    if self_name and sender == self_name:
        return True
    if not content:
        return True
    return bool(_TIME_ONLY_RE.match(content))


def _norm_space(s: str) -> str:
    return " ".join((s or "").split())


_TYPE_LABEL = {
    "emotion": "表情",
    "image": "图片",
    "voice": "语音",
    "video": "视频",
    "file": "文件",
    "link": "链接",
    "quote": "引用消息",
    "location": "位置",
    "personalcard": "名片",
    "merge": "合并转发",
}

# 这些内容就是类型名本身，没有额外信息
_BARE_CONTENT = {"", "图片", "语音", "视频", "文件", "动画表情", "表情", "[图片]", "[动画表情]"}


_CAPTION_RE = __import__("re").compile(r"[\[【]([^\]】]+)[\]】]")


def describe_message(msg) -> str:
    """把非文字消息转成 agent 能看懂的一句话，而不是把"动画表情"当正文。

    实测：自定义表情包的**文字提示**会被微信写进控件名，形如 `动画表情 [紧张]`。
    所以表情是可读的——把方括号里的提示词提出来交给 agent。
    """
    content = str(getattr(msg, "content", "") or "").strip()
    mtype = str(getattr(msg, "type", "") or "").strip().lower()

    if mtype == "emotion":
        m = _CAPTION_RE.search(content)
        if m:
            cap = m.group(1).strip()
            # 两种排布都见过：「动画表情 [紧张]」和「[动画表情]爱了」
            if cap in ("动画表情", "表情"):
                tail = content[m.end():].strip()
                if tail:
                    return f"（对方发了一个表情：{tail}）"
                return "（对方发了一个表情）"
            return f"（对方发了一个表情：{cap}）"
        return "（对方发了一个表情）"

    label = _TYPE_LABEL.get(mtype)
    if not label:
        return content
    if content in _BARE_CONTENT:
        return f"（对方发了一个{label}）"
    return f"（{label}）{content}"


def is_echo(
    recent: list[tuple[float, str]],
    text: str,
    *,
    now: float | None = None,
    window: float = 60.0,
) -> bool:
    """判断一条收到的消息是不是自己刚发出去的内容回流。

    只认**完全相同**（忽略空白差异），绝不用"包含"关系——审批卡片里写着
    「回复『同意 1』或『拒绝 1』」，用包含关系会把用户真正的「同意 1」也吞掉。
    窗口也收紧到 60 秒，免得几分钟后家人说同一句话被误杀。
    """
    now = time.time() if now is None else now
    probe = _norm_space(text)
    if len(probe) < 2:
        return False

    # 第二层：去掉标点/空白后再比。因为发出去的文字会被 format_for_wechat
    # 清洗过（去 Markdown、断行），回流时跟原串不完全相等。
    # 只对**够长**的消息启用前缀比对，短命令（「同意 1」）绝不能靠这个吞掉。
    probe_loose = _norm_loose(text)
    for ts, sent in recent:
        if now - ts >= window:
            continue
        if probe == _norm_space(sent):
            return True
        if len(probe_loose) >= 12:
            sent_loose = _norm_loose(sent)
            if not sent_loose:
                continue
            if probe_loose.startswith(sent_loose) or sent_loose.startswith(probe_loose):
                return True
    return False


def _norm_loose(text: str) -> str:
    """去掉空白和常见 Markdown 标记，只留可见文字，用于模糊回声比对。"""
    import re as _re

    s = _re.sub(r"[\s\*_`#>\-\u3000]+", "", text or "")
    return s.lower()


def _fp(msg) -> str:
    return f"{getattr(msg, 'type', '')}|{getattr(msg, 'sender', '')}|{getattr(msg, 'content', '')}"
