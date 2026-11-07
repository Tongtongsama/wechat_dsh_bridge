"""按人记忆：记住"谁是谁、什么脾气、有什么偏好"，回复时带上。

身份问题（重要）：微信 UI 自动化只能拿到**会话内显示的昵称**，拿不到 wxid。
同一个人在不同群可能显示不同昵称，改昵称还会变。所以记忆不能直接以昵称为主键，
而是"一个人 = 一个规范名 + 若干别名"：

    {
      "老王": {"note": "大学同学，爱开玩笑", "aliases": ["老王", "david", "W"]}
    }

数据安全（2026-11 事故后加固）：曾经出现过"读失败 → 内存为空 → 存一条新的 →
整个文件被顶掉"的数据丢失。所以现在：
  1. 读失败**绝不**当空处理：损坏文件改名留证，并在此后拒绝静默覆盖；
  2. 每次写盘都是"临时文件 + 原子替换"，并保留一份 .bak；
  3. 调用方负责把路径解析成绝对路径，避免多进程因工作目录不同写到不同地方。
"""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any


class SelfKnowledge:
    """小助手对"自己是谁、能干什么"的说明。

    单独存成一个文件（`self.md`），不混进人物名册——否则"群里有谁"
    会把自己也列出来，很怪。

    内容每次回答前都会读一次，所以改这个文件立刻生效，不用重启。

    自动维护的约定：文件首行是一条 HTML 注释标记。带标记时系统会在
    配置变化后自动刷新（比如新开了生图能力）；只要你把那一行删掉，
    系统就不再碰它，完全按你写的来。
    """

    MARKER = "<!--"

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> str:
        try:
            if self.path.exists():
                raw = self.path.read_text(encoding="utf-8")
                # 去掉自动维护标记，别把它念给模型听
                lines = [ln for ln in raw.splitlines() if not ln.lstrip().startswith(self.MARKER)]
                return "\n".join(lines).strip()
        except Exception:
            pass
        return ""

    def ensure(self, default: str) -> str:
        body = default.strip() + "\n"
        header = ("<!-- 自动生成。可以直接改；改完把这一行删掉，"
                  "系统就不会再自动刷新它了。 -->\n")
        full = header + body
        try:
            if not self.path.exists():
                self.path.write_text(full, encoding="utf-8")
                return body.strip()
            current = self.path.read_text(encoding="utf-8")
            # 还是"自动维护"状态且内容过期 → 刷新
            if current.lstrip().startswith(self.MARKER) and current != full:
                self.path.write_text(full, encoding="utf-8")
                return body.strip()
        except Exception:
            pass
        return self.load()


class MemoryStore:
    """按范围分开的记忆：家人群一份、朋友群一份、本人一份。

    为什么要分：家人群和朋友群是两拨人，混在一起有两个问题——
      · 名册被互相看见（在朋友群报出「妈妈、父亲」很怪，甚至泄露家庭信息）；
      · 同名的人可能在不同群指不同人，共用一个别名表会串。

    每个 scope 一个独立文件（`memory.<scope>.json`），沿用 Memory 那套
    原子写 + 校验 + 读失败不覆盖的保护。
    """

    LABELS = {"family": "家人", "friends": "朋友", "owner": "本人"}

    def __init__(self, base_path: str | Path, scopes: list[str] | None = None,
                 legacy_path: str | Path | None = None) -> None:
        base = Path(base_path)
        base.parent.mkdir(parents=True, exist_ok=True)
        names = scopes or ["family", "friends", "owner"]
        self.stores: dict[str, Memory] = {}
        for s in names:
            self.stores[s] = Memory(base.with_name(f"{base.name}.{s}.json"))

        # 首次迁移：老的单一 memory.json 内容复制进每个 scope，一条都不丢
        if legacy_path is not None:
            legacy = Path(legacy_path)
            if legacy.exists():
                try:
                    raw = json.loads(legacy.read_text(encoding="utf-8"))
                except Exception:
                    raw = {}
                if isinstance(raw, dict) and raw:
                    for _s, store in self.stores.items():
                        if not store.all():
                            for person, item in raw.items():
                                if isinstance(item, str):
                                    store.set(str(person), item)
                                elif isinstance(item, dict):
                                    store.set(str(person), str(item.get("note", "")),
                                              [str(a) for a in (item.get("aliases") or [])])

    def for_scope(self, scope: str) -> Memory:
        return (self.stores.get(scope)
                or self.stores.get("owner")
                or next(iter(self.stores.values())))

    def resolve_any(self, alias: str) -> str | None:
        for store in self.stores.values():
            person = store.resolve(alias)
            if person:
                return person
        return None

    def label(self, scope: str) -> str:
        return self.LABELS.get(scope, scope)

    def scope_by_label(self, token: str) -> str | None:
        tok = (token or "").strip()
        for scope, label in self.LABELS.items():
            if tok in (scope, label):
                return scope
        return None

    def combined_roster(self) -> str:
        parts = []
        for scope, store in self.stores.items():
            if store.count():
                parts.append(f"{self.label(scope)}：{'、'.join(store.all().keys())}")
        return "\n".join(parts)


class Memory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._load_failed = False
        self._data: dict[str, dict[str, Any]] = self._load()

    # ---------- 读写 ----------
    def _load(self) -> dict[str, dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw: Any = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            # 关键：不能当成"没有记忆"继续跑，否则下一次保存会把原文件顶掉。
            backup = self.path.with_name(
                f"{self.path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            )
            try:
                shutil.copy2(self.path, backup)
            except Exception:
                backup = None
            self._load_failed = True
            raise RuntimeError(
                f"记忆文件解析失败（{exc}）。原文件已备份到 {backup}，"
                "为避免覆盖已有数据，本次拒绝启动记忆功能。"
            ) from exc

        if not isinstance(raw, dict):
            self._load_failed = True
            raise RuntimeError("记忆文件格式不对（顶层不是对象），已拒绝覆盖。")

        out: dict[str, dict[str, Any]] = {}
        for person, value in raw.items():
            if isinstance(value, str):          # 兼容旧格式：昵称 -> 备注
                out[str(person)] = {"note": value, "aliases": [str(person)]}
            elif isinstance(value, dict):
                aliases = value.get("aliases") or [person]
                out[str(person)] = {
                    "note": str(value.get("note", "")),
                    "aliases": [str(a) for a in aliases if str(a).strip()],
                }
        return out

    def _save(self) -> None:
        if self._load_failed:
            raise RuntimeError("记忆曾读取失败，拒绝写盘以免覆盖原文件。")
        payload = json.dumps(self._data, ensure_ascii=False, indent=2)
        # 先留一份上一版，再原子替换：中途崩溃也不会留下半个文件
        if self.path.exists():
            try:
                shutil.copy2(self.path, self.path.with_suffix(self.path.suffix + ".bak"))
            except Exception:
                pass
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.replace(tmp, self.path)

    # ---------- 查找 ----------
    def resolve(self, alias: str) -> str | None:
        key = (alias or "").strip()
        if not key:
            return None
        if key in self._data:
            return key
        for person, item in self._data.items():
            if key in item.get("aliases", []):
                return person
        return None

    def get(self, alias: str) -> str:
        person = self.resolve(alias)
        if not person:
            return ""
        return self._data[person].get("note", "")

    # ---------- 修改 ----------
    def set(self, person: str, note: str, aliases: list[str] | None = None, auto: bool = False) -> None:
        name = (person or "").strip()
        if not name:
            return
        note = (note or "").strip()
        existing = self._data.get(name, {"note": "", "aliases": []})
        merged = list(dict.fromkeys([*existing.get("aliases", []), name, *(aliases or [])]))
        if note:
            item = {"note": note, "aliases": merged}
            if auto:
                item["auto"] = True
            self._data[name] = item
        else:
            self._data.pop(name, None)
        self._save()

    def add_alias(self, alias: str, person: str) -> bool:
        a, p = (alias or "").strip(), (person or "").strip()
        if not a or not p:
            return False
        canonical = self.resolve(p) or p
        item = self._data.get(canonical)
        if not item:
            return False
        if a not in item["aliases"]:
            item["aliases"].append(a)
            self._save()
        return True

    def forget(self, person: str) -> bool:
        canonical = self.resolve(person)
        if canonical and canonical in self._data:
            del self._data[canonical]
            self._save()
            return True
        return False

    def all(self) -> dict[str, dict[str, Any]]:
        return {k: dict(v) for k, v in self._data.items()}

    def count(self) -> int:
        return len(self._data)

    def hint_for(self, alias: str, max_chars: int = 400) -> str:
        person = self.resolve(alias)
        if not person:
            return ""
        note = self._data[person].get("note", "")
        if not note:
            return ""
        if len(note) > max_chars:
            note = note[:max_chars] + "…"
        if person != (alias or "").strip():
            return f"【关于「{alias}」，也就是「{person}」】{note}"
        return f"【关于「{person}」】{note}"
