"""把一条微信消息变成一次 DSH agent 运行。

真实实现走 `dsh --profile headless --json`（一次性执行、打印事件流、退出）。
权限完全由 DSH_PERMISSION_MODE 决定：

    workspace-write     沙箱=workspace-write，approval=ask
    danger-full-access  沙箱=全盘，approval=never

注意 approval=ask 在 headless 里没有可应答的 UI，所以"需要审批"的动作
会直接失败而不是挂起——这正是我们要的默认行为：先拒绝，再由桥接层
去微信找本人批准。

--json 事件流实测样例：
    {"type":"session","sessionId":"session-...","cwd":"..."}
    {"type":"status","phase":"turn_start","turn":1}
    {"type":"thinking","text":""}
    {"type":"text","text":"收到"}
    {"type":"status","phase":"step_end",...,"usage":{"totalTokens":6290}}
    {"type":"final","text":"收到"}
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

def _find_dsh() -> tuple[str, str]:
    """找 DSH 桌面端可执行文件和 CLI 入口。

    优先看环境变量，再试几个常见安装位置——写死某个人的安装目录，
    别人 clone 下来就跑不了。
    """
    candidates: list[Path] = []
    env_exe = os.environ.get("DSH_EXE")
    if env_exe:
        candidates.append(Path(env_exe))
    for base in (
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "DeepSeek Harness",
        Path(os.environ.get("PROGRAMFILES", "")) / "DeepSeek Harness",
        Path(os.environ.get("PROGRAMFILES(X86)", "")) / "DeepSeek Harness",
    ):
        candidates.append(base / "DeepSeek Harness.exe")

    for exe in candidates:
        if exe and exe.exists():
            cli = (exe.parent / "resources" / "app.asar" / "dsh" / "node_modules"
                   / "@deepseek-ai" / "dsh-desktop-host" / "lib" / "cli.js")
            if cli.exists():
                return str(exe), str(cli)
            return str(exe), ""
    raise FileNotFoundError(
        "找不到 DeepSeek Harness，请设置环境变量 DSH_EXE 指向 "
        "'DeepSeek Harness.exe'"
    )


try:
    DSH_EXE, DSH_CLI = _find_dsh()
except FileNotFoundError:
    DSH_EXE, DSH_CLI = "", ""

# 输出里出现这些字样，就认为任务因权限不足而没做成
_DENIAL_PATTERNS = re.compile(
    r"requires approval|approval (?:was )?denied|permission denied|"
    r"sandbox (?:blocked|denied)|not permitted|not allowed to|"
    r"拒绝访问|需要(?:你的)?审批|超出沙箱|不允许",
    re.IGNORECASE,
)


@dataclass
class RunResult:
    ok: bool
    text: str
    session_id: Optional[str] = None
    denied: bool = False
    mode: str = "workspace-write"
    elapsed_s: float = 0.0
    tokens: int = 0


class AgentRunner:
    """接口：给定会话与任务，跑一次 agent，返回文本。"""

    def run(
        self,
        *,
        conv_id: str,
        task: str,
        workspace: Path,
        session_id: Optional[str],
        mode: str,
        timeout_s: int = 300,
    ) -> RunResult:
        raise NotImplementedError


class DshRunner(AgentRunner):
    def run(
        self,
        *,
        conv_id: str,
        task: str,
        workspace: Path,
        session_id: Optional[str],
        mode: str,
        timeout_s: int = 300,
    ) -> RunResult:
        workspace.mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        env["ELECTRON_RUN_AS_NODE"] = "1"
        env["DSH_PERMISSION_MODE"] = mode
        env.pop("DSH_SESSION_ID", None)   # 别继承父会话，避免串台

        cmd = [DSH_EXE, "--expose-internals", DSH_CLI, "--profile", "headless", "--json"]
        if session_id:
            cmd += ["--session-id", session_id]
        cmd += [task]

        started = time.time()
        # 桥接跑在 pythonw（无控制台）下，子进程不禁止窗口就会弹出黑框
        quiet: dict = {}
        if hasattr(subprocess, "CREATE_NO_WINDOW"):
            info = subprocess.STARTUPINFO()
            info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            info.wShowWindow = 0
            quiet = {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": info}
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                **quiet,
            )
        except subprocess.TimeoutExpired:
            return RunResult(
                ok=False,
                text=f"（任务超时，超过 {timeout_s} 秒已中止）",
                session_id=session_id,
                mode=mode,
                elapsed_s=time.time() - started,
            )

        return self._parse(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            session_id=session_id,
            mode=mode,
            elapsed_s=time.time() - started,
        )

    # ---------- 事件流解析 ----------
    @staticmethod
    def _parse(
        *,
        stdout: str,
        stderr: str,
        returncode: int,
        session_id: Optional[str],
        mode: str,
        elapsed_s: float,
    ) -> RunResult:
        sid = session_id
        tokens = 0
        final_text: Optional[str] = None
        text_parts: list[str] = []
        raw_blob: list[str] = []

        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            if not line.startswith("{"):
                raw_blob.append(line)
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                raw_blob.append(line)
                continue
            if not isinstance(evt, dict):
                continue

            etype = evt.get("type")
            if etype == "session" and isinstance(evt.get("sessionId"), str):
                sid = evt["sessionId"] or sid
            elif etype == "final" and isinstance(evt.get("text"), str):
                final_text = evt["text"]
            elif etype == "text" and isinstance(evt.get("text"), str):
                text_parts.append(evt["text"])
            elif etype == "status":
                usage = evt.get("usage")
                if isinstance(usage, dict):
                    tokens += int(usage.get("totalTokens") or 0)
                # 状态事件里也可能带错误原因
                if evt.get("phase") in ("error", "aborted"):
                    raw_blob.append(json.dumps(evt, ensure_ascii=False))

        blob = "\n".join([stdout, stderr, *raw_blob])
        denied = bool(_DENIAL_PATTERNS.search(blob))

        if final_text is not None:
            text = final_text
        elif text_parts:
            text = "".join(text_parts)
        elif raw_blob:
            text = "\n".join(raw_blob[:20])
        elif returncode != 0:
            text = f"（运行失败）{stderr.strip()[:800]}"
        else:
            text = "（已完成，但没有文本输出）"

        return RunResult(
            ok=returncode == 0 and not denied,
            text=text.strip(),
            session_id=sid,
            denied=denied,
            mode=mode,
            elapsed_s=elapsed_s,
            tokens=tokens,
        )


class FakeRunner(AgentRunner):
    """离线自测用：不联网、不花钱，按任务文本返回可预期结果。"""

    def run(
        self,
        *,
        conv_id: str,
        task: str,
        workspace: Path,
        session_id: Optional[str],
        mode: str,
        timeout_s: int = 300,
    ) -> RunResult:
        workspace.mkdir(parents=True, exist_ok=True)
        sid = session_id or f"sess-fake-{abs(hash(conv_id)) % 10000:04d}"

        risky = ("删", "rm ", "format", "关机", "移除", "清空")
        if mode == "workspace-write" and any(k in task for k in risky):
            return RunResult(
                ok=False,
                text="（这个操作需要你的审批，已挂起）",
                session_id=sid,
                denied=True,
                mode=mode,
                tokens=120,
            )
        return RunResult(
            ok=True,
            text=f"[fake:{mode}] 收到「{task}」",
            session_id=sid,
            mode=mode,
            tokens=len(task),
        )
