"""表情库：本地图片即表情包。

命名规则（文件名就是它的含义）：

    stickers/开心_撒花.gif
    stickers/无语.png
    stickers/generated/gen_xxxx.png   ← 系统生成的，名字记在 generated/manifest.json

- 去掉扩展名就是名字；带下划线的，第一段也会注册成别名
  （`开心_撒花.gif` → 既能用「开心_撒花」，也能用「开心」）。
- 支持 png / jpg / jpeg / gif / webp。
- 以 `_` 或 `.` 开头的文件忽略（用于放测试图）。
- **递归扫描**，所以 generated/ 里生成过的图会自动入库，能按名字复用。

发出去的方式：agent 在回复末尾单独写一行 `[表情:名字]`，桥接把它摘掉、
发出对应图片。这行不会显示给对方。

生图：agent 写 `[表情:生图:画面描述]`，由桥接调 API 现生成。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

SUPPORTED = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_MARKER = re.compile(
    r"^\s*[\[【]\s*(?:表情|sticker|emoji)\s*[:：]\s*([^\]】]+?)\s*[\]】]\s*$", re.I)
_INLINE_MARKER = re.compile(
    r"[\[【]\s*(?:表情|sticker|emoji)\s*[:：]\s*([^\]】]+?)\s*[\]】]", re.I)


class Stickers:
    def __init__(self, directory: str | Path) -> None:
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._by_key: dict[str, Path] = {}
        self._scan()

    def _manifest(self) -> dict:
        """生图产物的人类可读名字：{相对路径: {name, prompt}}"""
        path = self.dir / "generated" / "manifest.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _scan(self) -> None:
        self._by_key.clear()
        manifest = self._manifest()
        for p in sorted(self.dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED:
                continue
            rel = p.relative_to(self.dir).as_posix()
            segs = rel.split("/")
            if any(part.startswith(("_", ".")) for part in segs):
                continue
            # ref/ 放的是生图参考图（角色设定图），不是表情
            if segs[0] == "ref":
                continue

            # 生成图只认 manifest 里的可读名字，不认 gen_<hash> 这种文件名
            if segs[0] == "generated":
                entry = manifest.get(rel)
                if isinstance(entry, dict) and str(entry.get("name", "")).strip():
                    self._by_key.setdefault(str(entry["name"]).strip(), p)
                continue

            stem = p.stem.strip()
            if not stem:
                continue
            self._by_key.setdefault(stem, p)
            # 下划线第一段做别名（仅手工放的、位于顶层的表情）
            head = re.split(r"[_\-—\s]+", stem)[0].strip()
            if head and head != stem:
                self._by_key.setdefault(head, p)

    def refresh(self) -> int:
        self._scan()
        return len(self._by_key)

    def resolve(self, key: str) -> Path | None:
        k = (key or "").strip()
        if not k:
            return None
        if k in self._by_key:
            return self._by_key[k]
        norm = k.replace(" ", "").lower()
        for name, path in self._by_key.items():
            if name.replace(" ", "").lower() == norm:
                return path
        return None

    def names(self) -> list[str]:
        return list(self._by_key.keys())

    def basic_names(self) -> list[str]:
        """`stickers/basic/` 里的通用表情——放"得意""开心""叹气""生气"这类
        几乎对任何消息都适用的，用来做保底随机。"""
        out = []
        for name, path in self._by_key.items():
            try:
                rel = path.relative_to(self.dir)
            except Exception:
                continue
            if rel.parts and rel.parts[0] == "basic":
                out.append(name)
        return sorted(set(out))

    def random_name(self, exclude: str | None = None) -> str | None:
        """随机挑一张，优先从 basic/ 里挑，其次全库。不会挑到 exclude。"""
        import random

        for pool in (self.basic_names(), self.names()):
            cands = [n for n in pool if n != exclude]
            if cands:
                return random.choice(cands)
        return None

    def pool(self, names: list[str]) -> list[str]:
        """从给定名字里挑出库里真实存在的那些（按原名返回，便于日志对账）。"""
        out = []
        for name in names or []:
            if self.resolve(name):
                out.append(name)
        return out

    def __len__(self) -> int:
        return len(self._by_key)

    def prompt_hint(self) -> str:
        names = self.names()
        if not names:
            return (
                "【表情包】现在库里没有现成表情。想发表情就写 "
                "「表情:生图:画面描述」，系统会现生成一张。"
            )
        basic = self.basic_names()
        listed = "、".join(names[:40])
        more = f" 等 {len(names)} 个" if len(names) > 40 else ""
        lines = [
            f"【表情包】现成可用的：{listed}{more}。",
            "发表情时在回复**最后单独一行**写「表情:名字」（换成英文方括号），系统替你发图，这行不显示给对方。",
            "选哪个：**必须和当前情绪对得上**。对不上就别硬套现成的，"
            "改用「表情:生图:画面描述」现生成一张——生图由系统承担，不用你操心延迟或费用。",
            "同一张表情别连着用；一次最多一个。",
        ]
        if basic:
            lines.append(
                f"通用表情（几乎任何场合都能用）：{'、'.join(basic[:20])}。"
                "拿不准发什么的时候，从这几个里挑。"
            )
        return "\n".join(lines)


def extract(text: str) -> tuple[str, list[str]]:
    """从回复里摘出 [表情:xxx] 标记，返回（清理后的文本, 表情名列表）。"""
    if not text:
        return "", []
    keys: list[str] = []
    kept: list[str] = []
    for line in text.splitlines():
        m = _MARKER.match(line)
        if m:
            keys.append(m.group(1).strip())
            continue
        kept.append(line)
    body = "\n".join(kept)

    for m in _INLINE_MARKER.finditer(body):
        keys.append(m.group(1).strip())
    if keys:
        body = _INLINE_MARKER.sub("", body)

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    seen: set[str] = set()
    uniq = [k for k in keys if not (k in seen or seen.add(k))]
    return body, uniq


# 除了表情，回复里还能出现"发表格"的动作标记。
# 之前 extract() 只认表情，于是 [表格:xxx] 被原样当成文字发了出去。
_ACTION_LINE = re.compile(
    r"^\s*[\[【]\s*(?P<kind>表情|sticker|emoji|表格|长图|表|投递|保存到|放到)\s*[:：]\s*(?P<val>[^\]】]+?)\s*[\]】]\s*$",
    re.I)
_ACTION_INLINE = re.compile(
    r"[\[【]\s*(?P<kind>表情|sticker|emoji|表格|长图|表|投递|保存到|放到)\s*[:：]\s*(?P<val>[^\]】]+?)\s*[\]】]",
    re.I)


def extract_actions(text: str) -> tuple[str, list[tuple[str, str]]]:
    """摘出所有动作标记，返回（正文, [(kind, value), ...]）。

    kind 只有两种：`sticker` 或 `table`。这样 `_deliver` 才能分派到
    发表情图 / 发表格长图两条不同的路径。
    """
    if not text:
        return "", []

    def kind_of(raw: str) -> str:
        if raw in ("表格", "长图", "表"):
            return "table"
        if raw in ("投递", "保存到", "放到"):
            return "deliver"
        return "sticker"

    actions: list[tuple[str, str]] = []
    kept: list[str] = []
    for line in text.splitlines():
        m = _ACTION_LINE.match(line)
        if m:
            actions.append((kind_of(m.group("kind")), m.group("val").strip()))
            continue
        kept.append(line)
    body = "\n".join(kept)

    for m in _ACTION_INLINE.finditer(body):
        actions.append((kind_of(m.group("kind")), m.group("val").strip()))
    if actions:
        body = _ACTION_INLINE.sub("", body)

    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    seen = set()
    uniq = []
    for kind, val in actions:
        key = (kind, val)
        if key not in seen:
            seen.add(key)
            uniq.append((kind, val))
    return body, uniq
