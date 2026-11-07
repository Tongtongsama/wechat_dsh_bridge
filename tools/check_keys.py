"""校验 secret.env 的格式，供 cmd 快捷方式调用。"""
from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> int:
    import keystore

    data = keystore.load()
    if not data:
        print("secret.env 里没有识别到任何 key，检查是不是写成了 NAME=value 的形式")
        return 1

    print("识别到的 key：")
    for name, value in data.items():
        print(f"  {name:<18} {keystore.mask(value)}")

    problems = keystore.verify(data)
    if problems:
        print("\n格式有问题：")
        for p in problems:
            print("  -", p)
        return 1

    print("\n校验：全部正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
