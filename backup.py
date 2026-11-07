"""备份：把桥接目录打包到"agent 够不着"的地方。

为什么要放在外面：给 agent 一个可自由读写的特区，它就有能力把自己的
配置、记忆、表情库改坏。备份如果放在特区里，它一样能删掉——等于没有。

所以默认备份到 `%USERPROFILE%\\dsh-bridge-backups`，在 agent 的工作区
之外。它要写那里必须经过主人批准。

触发时机：
  · 每次体面停止之前（手动"停止.cmd"、重启）
  · 每天第一次超过设定时间（默认 20:00）的巡检
  · 主人在私聊里发「备份」
"""
from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from pathlib import Path

# 不打包的东西：日志、缓存、备份本身
_EXCLUDE_DIRS = {"logs", "__pycache__", ".git", "backups"}
_EXCLUDE_SUFFIX = {".pyc", ".log", ".tmp"}


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _EXCLUDE_DIRS]
        for name in filenames:
            p = Path(dirpath) / name
            if p.suffix.lower() in _EXCLUDE_SUFFIX:
                continue
            yield p


class BackupManager:
    def __init__(self, source_root: str | Path, dest_root: str | Path,
                 keep: int = 10) -> None:
        self.source = Path(source_root)
        self.dest = Path(dest_root).expanduser()
        self.keep = max(1, int(keep))
        self._state_file = self.dest / "backups.json"

    # ---------- 记录 ----------
    def _load_state(self) -> dict:
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self, data: dict) -> None:
        try:
            self.dest.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def last_time(self) -> float:
        try:
            return float(self._load_state().get("last_ts", 0))
        except Exception:
            return 0.0

    # ---------- 打包 ----------
    def create(self, label: str = "manual") -> Path | None:
        if not self.source.exists():
            return None
        try:
            self.dest.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None

        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.dest / f"{self.source.name}-{label}-{stamp}.zip"
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                for path in _iter_files(self.source):
                    try:
                        zf.write(path, path.relative_to(self.source).as_posix())
                    except Exception:
                        continue
        except Exception:
            try:
                target.unlink(missing_ok=True)
            except Exception:
                pass
            return None

        state = self._load_state()
        state["last_ts"] = time.time()
        state["last_file"] = target.name
        state["last_label"] = label
        self._save_state(state)
        self._prune()
        return target

    def _prune(self) -> None:
        try:
            files = sorted(self.dest.glob("*.zip"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[self.keep:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass

    # ---------- 每日巡检 ----------
    def daily_tick(self, after_hour: int = 20) -> Path | None:
        """每天在设定时间之后备份一次，当天已备过就跳过。"""
        now = time.localtime()
        if now.tm_hour < after_hour:
            return None
        today = time.strftime("%Y-%m-%d")
        last = self.last_time()
        if last and time.strftime("%Y-%m-%d", time.localtime(last)) == today:
            return None
        return self.create("daily")

    def list_backups(self, limit: int = 5) -> list[str]:
        try:
            files = sorted(self.dest.glob("*.zip"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception:
            return []
        return [f"{p.name}（{p.stat().st_size / 1024 / 1024:.1f} MB）" for p in files[:limit]]
