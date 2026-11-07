"""主动私聊：在合适的时机主动给本人发消息。

设计取向：
  - **不猜、不打扰**：只在明确的时间规则（比如深夜还在用电脑）触发，默认一天最多几次；
  - **每次措辞都不同**：文案由模型现写（一条几十 token），不是固定模板；
  - 所有触发记录在 state.json，重启不会重复轰炸。

当前规则：
  late_night —— 深夜（默认 23:00–03:00）还在用电脑，提醒休息，一晚最多一次。
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

API_URL = "https://api.deepseek.com/chat/completions"

# compose 用的系统提示（原来内联在 payload 里，改走 llm.chat 后提出来）
_COMPOSE_SYSTEM = (
    "你在给主人发一条简短的微信消息。要求：一两句话，口语，"
    "不要解释你自己是 AI，不要用 Markdown，不要加引号。"
    "每次措辞都要和上次不同。"
)


def compose(instruction: str, *, api_key: str, model: str = "deepseek-flash",
            timeout: float = 40.0) -> str:
    """用一次极小的直连请求现写一句话，保证每次不一样。"""
    if not api_key:
        return ""
    import llm

    return llm.chat(_COMPOSE_SYSTEM, instruction, api_key=api_key, model=model,
                    temperature=1.15, timeout=timeout)


class BalanceWatcher(threading.Thread):
    """盯 DeepSeek 余额：充值了就主动告诉主人一声。

    只在**余额明显上涨**时说话（默认涨 ≥ 1 元），余额自然消耗不会触发。
    第一次运行只记基线，不报"你充了钱"。
    """

    def __init__(self, bridge, cfg: dict) -> None:
        super().__init__(daemon=True, name="balance-watch")
        self.bridge = bridge
        self.cfg = cfg or {}
        self._stop = threading.Event()

    def _guard(self) -> None:
        """余额守门员：太低或花得异常就主动吭一声，一天各一次。"""
        gcfg = self.cfg.get("balance_guard") or {}
        if not gcfg.get("enabled"):
            return

        import balance as bal

        key = self.bridge._env_api_key()
        today = time.strftime("%Y-%m-%d")
        flags = self.bridge._state.setdefault("balance_alerts", {})

        # 1) 余额过低
        low = float(gcfg.get("deepseek_min_cny", 30))
        amount = bal.deepseek_balance_value(key)
        if amount is not None and amount < low and flags.get("low") != today:
            text = compose(
                f"主人的 DeepSeek 余额只剩 ¥{amount:.2f}（低于 ¥{low:.0f} 的警戒线）。"
                "提醒他该充值了，语气自然、别太催，可以说得轻松点。",
                api_key=key,
            ) or f"余额只剩 ¥{amount:.2f} 了，记得充点。"
            self.bridge._say_with_pool(self.bridge.owner_conv, text, "ask")
            flags["low"] = today
            with self.bridge._state_lock:
                self.bridge._save_state()
            self.bridge._log("balance_low_alert", amount=amount, threshold=low)

        # 2) 花费异常：今天比前几天的均值高出若干倍
        factor = float(gcfg.get("daily_spike_factor", 3))
        floor = float(gcfg.get("min_daily_usd", 0.5))
        if flags.get("spike") == today:
            return
        try:
            import keystore

            admin = keystore.load().get("OPENAI_ADMIN_KEY") or os.environ.get(
                "OPENAI_ADMIN_KEY", "")
        except Exception:
            admin = os.environ.get("OPENAI_ADMIN_KEY", "")
        series = bal.openai_daily(admin, days=8)
        if not series or len(series) < 3:
            return
        today_usd = next((v for d, v in series if d == today), 0.0)
        prior = [v for d, v in series if d != today and v > 0]
        if not prior or today_usd < floor:
            return
        avg = sum(prior) / len(prior)
        if avg > 0 and today_usd >= avg * factor:
            text = compose(
                f"OpenAI 今天已经花了 ${today_usd:.2f}，是平时（${avg:.2f}）的"
                f"{today_usd / avg:.1f} 倍。提醒主人一句，语气轻松点，问要不要看看是哪里在花。",
                api_key=key,
            ) or f"今天 OpenAI 花了 ${today_usd:.2f}，比平时贵不少。"
            self.bridge._say(self.bridge.owner_conv, text)
            flags["spike"] = today
            with self.bridge._state_lock:
                self.bridge._save_state()
            self.bridge._log("balance_spike_alert", today=today_usd, avg=avg)

    def tick(self) -> None:
        if not self.cfg.get("enabled"):
            return

        try:
            self._guard()
        except Exception:
            pass

        # 顺手做每日备份巡检（备份模块自己有"今天是否已备"的判断）
        try:
            if getattr(self.bridge, "backups", None):
                made = self.bridge.backups.daily_tick(self.bridge._backup_after_hour)
                if made:
                    self.bridge._log("backup_daily", file=made.name)
        except Exception:
            pass

        import balance as bal

        key = self.bridge._env_api_key()
        cur = bal.deepseek_balance_value(key)
        if cur is None:
            return

        last = self.bridge._state.get("balance_last")
        if last is None:
            with self.bridge._state_lock:
                self.bridge._state["balance_last"] = cur
                self.bridge._save_state()
            return

        try:
            last_f = float(last)
        except Exception:
            last_f = 0.0

        delta = cur - last_f
        if delta < float(self.cfg.get("min_increase", 1.0)):
            with self.bridge._state_lock:
                self.bridge._state["balance_last"] = cur
                self.bridge._save_state()
            return

        text = compose(
            f"主人刚给 DeepSeek 充了钱：余额从 ¥{last_f:.2f} 涨到 ¥{cur:.2f}。"
            "跟他说一声你已经看到了，语气自然、带点开心，别太夸张。",
            api_key=key,
        )
        if not text:
            text = f"看到余额从 ¥{last_f:.2f} 涨到 ¥{cur:.2f} 了，收到，谢谢补粮。"
        # 充值通知顺便讨点粮——用「索要」表情池
        self.bridge._say_with_pool(self.bridge.owner_conv, text, "ask")
        self.bridge._log("balance_topup_detected", before=last_f, after=cur, text=text)

        with self.bridge._state_lock:
            self.bridge._state["balance_last"] = cur
            self.bridge._save_state()

    def run(self) -> None:
        interval = max(1.0, float(self.cfg.get("check_interval_minutes", 10))) * 60
        if self._stop.wait(90):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            if self._stop.wait(interval):
                break

    def stop(self) -> None:
        self._stop.set()


class NudgeScheduler(threading.Thread):
    """后台线程：按规则决定要不要主动发消息。"""

    def __init__(self, bridge, cfg: dict) -> None:
        super().__init__(daemon=True, name="nudge")
        self.bridge = bridge
        self.cfg = cfg or {}
        self._stop = threading.Event()

    # ---------- 规则 ----------
    def _in_window(self, now: datetime, start: int, end: int) -> bool:
        h = now.hour
        return (start <= h < end) if start < end else (h >= start or h < end)

    def _sent(self, key: str) -> str:
        return str(self.bridge._state.get("nudges", {}).get(key, ""))

    def _mark(self, key: str, value: str) -> None:
        with self.bridge._state_lock:
            self.bridge._state.setdefault("nudges", {})[key] = value
            self.bridge._save_state()

    def _count_today(self, today: str) -> int:
        data = self.bridge._state.get("nudges", {}) or {}
        return sum(1 for k, v in data.items() if k.endswith("|" + today) and v == today)

    # ---------- 一轮检查 ----------
    def tick(self) -> None:
        if not self.cfg.get("enabled"):
            return
        now = datetime.now()
        today = now.date().isoformat()

        if self._count_today(today) >= int(self.cfg.get("max_per_day", 3)):
            return

        # 定时问候：早上第一次检查时发一条
        g = self.cfg.get("greetings") or {}
        morning = g.get("morning") or {}
        if morning.get("enabled", True):
            start = int(morning.get("start_hour", 8))
            end = int(morning.get("end_hour", 11))
            mkey = "greeting_morning|" + today
            if start <= now.hour < end and self._sent(mkey) != today:
                text = compose(
                    "现在是大早上，跟主人打个招呼。一两句话，问问他今天安排，"
                    "或者随口关心一句（睡好没、早饭吃没）。别太正经，别说教。",
                    api_key=self.bridge._env_api_key(),
                ) or "早上好，今天打算干点啥？"
                self.bridge._say(self.bridge.owner_conv, text)
                self._mark(mkey, today)
                self.bridge._log("greeting_sent", kind="morning", text=text)

        # 交易日报：不再死等某个时间点，改成"数据一变就推"。
        # 电脑不是常开，脚本什么时候跑完不确定；只要 daily_status.json 的
        # trade_date/updated_at 变了，就说明有新数据，立刻报一次。
        tcfg = getattr(self.bridge, "cfg", {}).get("trading") or {}
        if tcfg.get("enabled") and now.weekday() < 5:
            start_h = 5          # 太早不打扰
            end_h = 23
            try:
                start_h, end_h = (int(x) for x in str(
                    tcfg.get("window") or "05:00-23:00").split("-"))
            except Exception:
                pass
            tkey = "trading_daily|" + today
            # 数据刚更新时不立刻推：脚本可能还在写收尾日志，等一会儿更稳
            ready = True
            after_min = float(tcfg.get("after_minutes", 20))
            st_now = self.bridge.trading_status()
            if st_now is not None and getattr(st_now, "updated_at", ""):
                try:
                    from datetime import datetime as _dt

                    upd = _dt.fromisoformat(str(st_now.updated_at))
                    ready = (datetime.now(upd.tzinfo) - upd).total_seconds() >= after_min * 60
                except Exception:
                    ready = True
            if (start_h <= now.hour < end_h and self._sent(tkey) != today
                    and ready):
                if self.bridge.trading_data_version() and not self.bridge.trading_already_pushed():
                    if self.bridge.push_trading_daily():
                        self._mark(tkey, today)
                        try:
                            self.bridge.push_trading_highlights()
                        except Exception:
                            pass
                    else:
                        self.bridge._log("trading_push_failed")

        rule = self.cfg.get("late_night") or {}
        if rule.get("enabled", True):
            start = int(rule.get("start_hour", 23))
            end = int(rule.get("end_hour", 3))
            key = "late_night|" + today
            if self._in_window(now, start, end) and self._sent(key) != today:
                text = compose(
                    "现在是深夜（大约 {h} 点），主人还在用电脑。提醒他该休息了，"
                    "语气自然，可以带一点点抱怨或关心，别太正经。".format(h=now.hour),
                    api_key=self.bridge._env_api_key(),
                )
                if not text:
                    text = f"{now.hour} 点多了还在用电脑，早点睡吧。"
                self.bridge._say(self.bridge.owner_conv, text)
                self._mark(key, today)
                self.bridge._log("nudge_sent", rule="late_night", text=text)

    def run(self) -> None:
        interval = max(1.0, float(self.cfg.get("check_interval_minutes", 10))) * 60
        # 启动后先等一下，别和开机时的初始化抢资源
        if self._stop.wait(60):
            return
        while not self._stop.is_set():
            try:
                self.tick()
            except Exception:
                pass
            if self._stop.wait(interval):
                break

    def stop(self) -> None:
        self._stop.set()
