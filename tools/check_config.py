"""检查 config.json 的 JSON 格式，并尝试自动修复常见的中文标点错误。

最常见的坑：在记事本里手改配置时把 `,` 打成 `，`、把 `"` 打成 `“ ”`。
JSON 不认这些全角符号，整个文件就废了，桥接直接起不来。

用法：
    python tools/check_config.py          # 只检查
    python tools/check_config.py --fix    # 检查，发现问题就自动修复
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

# 只替换"结构性"位置的全角标点。中文正文里的逗号不能乱动，
# 所以逐个尝试，能解析成功就停。
_REPAIRS = [
    ("中文逗号 ，→ ,", lambda s: s.replace("，", ",")),
    ("中文冒号 ：→ :", lambda s: s.replace("：", ":")),
    ("中文引号 “”→ \"", lambda s: s.replace("“", '"').replace("”", '"')),
    ("中文方括号 ［］→ []", lambda s: s.replace("［", "[").replace("］", "]")),
    ("中文大括号 ｛｝→ {}", lambda s: s.replace("｛", "{").replace("｝", "}")),
    ("中文分号 ；→ ;", lambda s: s.replace("；", ";")),
]


def _try_load(text: str):
    try:
        return json.loads(text), None
    except json.JSONDecodeError as exc:
        return None, exc


def main(argv: list[str]) -> int:
    path = HERE / "config.json"
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as exc:
        print(f"读不了文件：{exc}")
        return 1

    cfg, err = _try_load(raw)
    applied: list[str] = []

    if cfg is None:
        print(f"JSON 格式错了：第 {err.lineno} 行第 {err.colno} 列")
        print(f"  {err.msg}")
        print(f"\n出错的位置是：{raw.splitlines()[err.lineno - 1].strip()[:80]}")

        if "--fix" not in argv:
            print("\n常见原因是用了中文标点（，：；“”）。")
            print("带上 --fix 参数可以自动修：python tools/check_config.py --fix")
            return 1

        print("\n尝试自动修复……")
        text = raw
        for label, fn in _REPAIRS:
            fixed = fn(text)
            if fixed == text:
                continue
            cand, cand_err = _try_load(fixed)
            if cand is not None:
                text = fixed
                applied.append(label)
                cfg = cand
                break
            text = fixed          # 继续叠加下一类修复再试
            applied.append(label)
            cfg = None
            err = cand_err

        if cfg is None:
            # 逐类叠加后仍不行，把最后结果再试一次
            cfg, err = _try_load(text)
        if cfg is None:
            print(f"自动修复没成功，还是第 {err.lineno} 行第 {err.colno} 列：{err.msg}")
            print("请手工检查这一行的引号和逗号。")
            return 1

        backup = path.with_name("config.json.before-fix")
        backup.write_text(raw, encoding="utf-8")
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"修复成功：{'、'.join(applied) or '规范化'}")
        print(f"原文件已备份为 {backup.name}")
    else:
        print("JSON 格式正常。")

    convs = cfg.get("conversations") or []
    print(f"\n会话 {len(convs)} 个：")
    for c in convs:
        budget = c.get("daily_budget_tokens")
        shown = f"{budget:,}" if isinstance(budget, int) else str(budget)
        print(f"  · {c.get('name')}  额度 {shown} tokens")

    fb = cfg.get("sticker_fallback") or {}
    if fb.get("enabled"):
        print(f"\n保底表情：每 {fb.get('min_every')}~{fb.get('max_every')} 条发一张，"
              f"范围 {', '.join(fb.get('convs') or [])}")

    pools = cfg.get("sticker_pools") or {}
    for name, names in pools.items():
        print(f"表情池 {name}：{'、'.join(names) if names else '（空）'}")

    fl = cfg.get("follow") or {}
    if fl.get("enabled"):
        print(f"\n关注窗口：{fl.get('max_messages')} 条 / {fl.get('window_minutes')} 分钟，"
              f"把握门槛 {fl.get('thresholds')}")
        print(f"  关键词：{'、'.join(fl.get('keywords') or [])}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
