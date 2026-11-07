"""额度查询与报账。

两家的能力差别很大，实测结论：

- **DeepSeek**：`GET /user/balance` 用普通 key 就能查，返回 CNY/USD 各一份。
- **OpenAI**：`/dashboard/billing/credit_grants` 明确要求 session key（只能在浏览器里用），
  `/organization/costs` 要求 key 具备 `api.usage.read` 权限。普通 project secret key
  两个都是 403。所以想自动查 OpenAI 额度，**必须单独配一把 admin key**。

拿不到就如实说"查不到"，不要编数字。
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass


@dataclass
class Balance:
    ok: bool
    text: str


def _get(url: str, key: str, timeout: float = 20.0) -> tuple[int, dict | str]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")[:200]
    except Exception as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def deepseek_balance(api_key: str) -> Balance:
    if not api_key:
        return Balance(False, "没有配置 DeepSeek key")
    code, body = _get("https://api.deepseek.com/user/balance", api_key)
    if code != 200 or not isinstance(body, dict):
        return Balance(False, f"查询失败（HTTP {code}）：{str(body)[:120]}")

    parts = []
    for info in body.get("balance_infos", []) or []:
        cur = info.get("currency", "?")
        total = str(info.get("total_balance", "?"))
        try:
            if float(total) == 0:      # 没钱的币种不用念出来
                continue
        except Exception:
            pass
        if cur == "CNY":
            parts.insert(0, f"¥{total}")
        else:
            parts.append(f"{cur} {total}")
    if not parts:
        return Balance(False, "接口返回里没有余额字段")
    avail = "可用" if body.get("is_available") else "不可用"
    return Balance(True, "DeepSeek：" + " / ".join(parts) + f"（{avail}）")


def deepseek_balance_value(api_key: str) -> float | None:
    """只取 CNY 余额数值，给"余额变化监控"用。"""
    if not api_key:
        return None
    code, body = _get("https://api.deepseek.com/user/balance", api_key)
    if code != 200 or not isinstance(body, dict):
        return None
    for info in body.get("balance_infos", []) or []:
        if info.get("currency") == "CNY":
            try:
                return float(info.get("total_balance"))
            except Exception:
                return None
    return None


def openai_daily(admin_key: str, days: int = 8) -> list[tuple[str, float]] | None:
    """按天返回 OpenAI 花费 [(YYYY-MM-DD, 美元), ...]，给"花费异常"检测用。"""
    if not admin_key:
        return None

    import time

    start = int(time.time()) - days * 86400
    url = (f"https://api.openai.com/v1/organization/costs"
           f"?start_time={start}&limit=31&bucket_width=1d")
    code, body = _get(url, admin_key)
    if code != 200 or not isinstance(body, dict):
        return None

    out: list[tuple[str, float]] = []
    for bucket in body.get("data", []) or []:
        try:
            day = time.strftime("%Y-%m-%d", time.localtime(int(bucket.get("start_time", 0))))
        except Exception:
            continue
        total = 0.0
        for item in bucket.get("results", []) or []:
            try:
                total += float((item.get("amount") or {}).get("value", 0))
            except Exception:
                pass
        out.append((day, total))
    return out


def openai_costs(admin_key: str, days: int = 7) -> Balance:
    """OpenAI 只支持"最近花费"，不支持"剩余额度"（官方没有这个公开接口）。

    注意单位：`amount.value` 直接就是**美元**，不是美分。
    一开始按美分算，把 $1.44 报成了 $0.01。
    """
    if not admin_key:
        return Balance(False, "未配置 OpenAI admin key（普通 secret key 查不了）")

    import time

    start = int(time.time()) - days * 86400
    url = (f"https://api.openai.com/v1/organization/costs"
           f"?start_time={start}&limit=31&bucket_width=1d")
    code, body = _get(url, admin_key)
    if code != 200 or not isinstance(body, dict):
        return Balance(False, f"查询失败（HTTP {code}）：{str(body)[:120]}")

    today_ts = int(time.mktime(time.strptime(time.strftime("%Y-%m-%d"), "%Y-%m-%d")))
    total = 0.0
    today = 0.0
    for bucket in body.get("data", []) or []:
        try:
            bucket_start = int(bucket.get("start_time", 0))
        except Exception:
            bucket_start = 0
        for item in bucket.get("results", []) or []:
            amount = item.get("amount") or {}
            try:
                val = float(amount.get("value", 0))
            except Exception:
                continue
            total += val
            if bucket_start >= today_ts:
                today += val

    return Balance(True, f"OpenAI：今日 ${today:.2f}，近 {days} 天 ${total:.2f}")
