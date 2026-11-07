"""版本控制：给桥接目录做 git 快照。

为什么需要：zip 备份只能整体回到"上次停止时"，做不到"只回退某个文件"。
git 可以精确到"这个文件上一次干净的样子是什么"。

设计要点：
  · git 装在固定路径，但桥接是用 pythonw 起的、没有控制台，PATH 可能不含 git，
    所以先查 PATH 再退回常见安装位置；
  · 子进程一律 CREATE_NO_WINDOW，否则每次提交都会弹黑窗；
  · 没有改动时不产生空提交（避免历史被噪音淹没）；
  · 任何失败都只返回错误信息，绝不抛异常打断桥接。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

_CANDIDATES = (
    r"C:\Program Files\Git\cmd\git.exe",
    r"C:\Program Files (x86)\Git\cmd\git.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\cmd\git.exe"),
)

_CREATE_NO_WINDOW = 0x08000000


def find_git() -> str | None:
    found = shutil.which("git")
    if found:
        return found
    for c in _CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


class Repo:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.git = find_git()

    @property
    def available(self) -> bool:
        return bool(self.git)

    def _run(self, *args: str, timeout: float = 30.0,
             extra_env: dict[str, str] | None = None) -> tuple[int, str]:
        if not self.git:
            return 127, "没有找到 git"
        env = None
        if extra_env:
            env = dict(os.environ)
            env.update(extra_env)
        try:
            proc = subprocess.run(
                [self.git, *args],
                cwd=str(self.root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                creationflags=_CREATE_NO_WINDOW,
                env=env,
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            # 只去尾部空白！porcelain 状态输出形如 " M a.py"，
            # 首行那个状态空格是有意义的，strip() 会把路径啃掉一个字符。
            return proc.returncode, out.rstrip()
        except Exception as exc:
            return 1, f"{type(exc).__name__}: {exc}"

    def is_repo(self) -> bool:
        if not self.available:
            return False
        code, _ = self._run("rev-parse", "--is-inside-work-tree")
        return code == 0

    def ensure(self) -> bool:
        """不是仓库就初始化，并设好本地提交身份（不碰全局配置）。"""
        if not self.available:
            return False
        if self.is_repo():
            return True
        code, out = self._run("init")
        if code != 0:
            return False
        self._run("config", "user.name", "DSH Bridge")
        self._run("config", "user.email", "bridge@localhost")
        self._run("config", "core.autocrlf", "false")
        return True

    def changed_files(self) -> list[str]:
        if not self.is_repo():
            return []
        code, out = self._run("status", "--porcelain")
        if code != 0 or not out:
            return []
        files = []
        for line in out.splitlines():
            line = line.rstrip()
            if not line:
                continue
            # 格式固定是 "XY <path>"（前两位状态，第三位空格）
            path = line[3:] if len(line) > 3 else line[2:]
            files.append(path.strip().strip('"'))
        return files

    def snapshot(self, message: str) -> tuple[bool, str]:
        """有改动就提交，返回（是否产生提交, 说明）。"""
        if not self.ensure():
            return False, "git 不可用，跳过存档"

        changes = self.changed_files()
        if not changes:
            return False, "没有改动"

        code, out = self._run("add", "-A")
        if code != 0:
            return False, f"add 失败：{out}"

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        code, out = self._run("commit", "-m", f"{message}（{stamp}）")
        if code != 0:
            return False, f"commit 失败：{out}"

        return True, f"已提交 {len(changes)} 个文件：" + "、".join(changes[:8]) + (
            " …" if len(changes) > 8 else "")

    def log(self, count: int = 5) -> list[str]:
        if not self.is_repo():
            return []
        code, out = self._run("log", f"-{count}", "--pretty=format:%h %ad %s",
                              "--date=format:%m-%d %H:%M")
        return out.splitlines() if code == 0 and out else []

    def last_commit_of(self, path: str) -> str:
        if not self.is_repo():
            return ""
        code, out = self._run("log", "-1", "--pretty=format:%h %ad %s",
                              "--date=format:%m-%d %H:%M", "--", path)
        return out if code == 0 else ""

    # ---------- 远端 ----------
    def set_remote(self, url: str, name: str = "origin") -> bool:
        if not self.is_repo():
            return False
        code, _ = self._run("remote", "get-url", name)
        if code == 0:
            code, _ = self._run("remote", "set-url", name, url)
        else:
            code, _ = self._run("remote", "add", name, url)
        return code == 0

    def remote_url(self, name: str = "origin") -> str:
        if not self.is_repo():
            return ""
        code, out = self._run("remote", "get-url", name)
        return out if code == 0 else ""

    def push(self, branch: str = "master", *, user: str = "", token: str = "",
             creds_file: Path | None = None, timeout: float = 180.0) -> tuple[bool, str]:
        """推送到远端，用 HTTP Basic 头认证。

        两种做法都试过：
          · `credential.helper=store --file=...` —— 在这台机器上 git 仍然去
            要用户名（helper 没接管），失败；
          · `http.extraheader=Authorization: Basic ...` —— 可靠，且不落盘。
        所以用后者。token 不写进 .git/config，也不留文件。
        """
        if not self.is_repo():
            return False, "不是 git 仓库"

        args: list[str] = []
        if user and token:
            import base64

            raw = f"{user}:{token}".encode("utf-8")
            basic = base64.b64encode(raw).decode("ascii")
            args = ["-c", f"http.extraheader=Authorization: Basic {basic}"]

        args += ["push", "-u", "origin", branch]
        code, out = self._run(*args, timeout=timeout,
                              extra_env={"GIT_TERMINAL_PROMPT": "0"})
        safe = out.replace(token, "***") if token else out
        return code == 0, safe

    @staticmethod
    def _lock_down(path: Path) -> None:
        """凭据文件只允许当前用户读。"""
        try:
            subprocess.run(
                ["icacls", str(path), "/inheritance:r",
                 "/grant:r", f"{os.environ.get('USERNAME', '')}:(R,W)"],
                capture_output=True, timeout=20,
                creationflags=_CREATE_NO_WINDOW,
            )
        except Exception:
            pass

