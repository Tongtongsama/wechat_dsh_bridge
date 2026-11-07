"""模拟交易的日报读取与播报。

数据来源是交易项目自己写的一个状态文件（约定见 README 的「对接约定」）：

    reports/daily_status.json   每个交易日收盘后覆写
    reports/heartbeat.json      可选，用来区分「没跑」和「跑了没交易」

设计原则：
  · **不编数字**。文件缺失、字段缺失、格式不对，一律如实说"读不到"，
    绝不拿昨天的数据顶今天，也不估算；
  · 只读，不写。桥接不碰交易项目的任何东西；
  · 收益率统一按**小数**解释（0.00152 = 0.152%），与约定一致。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path


@dataclass
class Position:
    symbol: str
    name: str = ""
    qty: float = 0.0
    cost: float = 0.0
    price: float = 0.0
    pnl: float = 0.0
    weight: float = 0.0
    market_value: float = 0.0
    change_pct: float = 0.0     # 个股当日涨跌幅（没有就是 0）
    pnl_today: float = 0.0      # 个股当日盈亏金额


@dataclass
class Status:
    ok: bool
    reason: str = ""
    trade_date: str = ""
    updated_at: str = ""
    mode: str = ""
    data_mode: str = ""
    currency: str = "CNY"
    equity_total: float = 0.0
    cash: float = 0.0
    market_value: float = 0.0
    day_pnl: float = 0.0
    day_pct: float = 0.0
    total_pnl: float = 0.0
    total_pct: float = 0.0
    benchmark_name: str = ""
    benchmark_pct: float = 0.0
    trades_today: int = 0
    max_drawdown: float = 0.0
    exposure: float = 0.0
    notes: str = ""
    positions: list[Position] = field(default_factory=list)
    stale_days: int = 0          # 数据距今天数（日历）
    stale: bool = False          # 是否比"上一个交易日"还旧
    # 策略意图与目标（可选，用来写"预想/目标"）
    strategy_name: str = ""
    thesis: str = ""
    target_monthly_pct: float = 0.0
    max_drawdown_limit: float = 0.0
    building_up: bool = False
    # 最近若干日（可选，用来写"最近怎么样"）
    recent_days: list[dict] = field(default_factory=list)
    trades: list[dict] = field(default_factory=list)
    # 值得单独提一句的事：避开大跌、错过大涨、大赚大亏
    highlights: list[dict] = field(default_factory=list)


def _num(d: dict, *keys, default: float = 0.0) -> float:
    for k in keys:
        v = d.get(k)
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            try:
                return float(v)
            except ValueError:
                continue
    return default


def load(path: str | Path) -> Status:
    p = Path(path)
    if not p.exists():
        return Status(False, reason=f"找不到状态文件：{p}")

    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        return Status(False, reason=f"状态文件读不了（{type(exc).__name__}）")

    if not isinstance(raw, dict):
        return Status(False, reason="状态文件不是 JSON 对象")

    trade_date = str(raw.get("trade_date") or "")
    if not trade_date:
        return Status(False, reason="状态文件里没有 trade_date")

    equity = raw.get("equity") or {}
    pnl = raw.get("pnl") or {}
    bench = raw.get("benchmark") or {}
    risk = raw.get("risk") or {}

    positions = []
    for item in raw.get("positions") or []:
        if not isinstance(item, dict):
            continue
        positions.append(Position(
            symbol=str(item.get("symbol") or ""),
            name=str(item.get("name") or ""),
            qty=_num(item, "qty", "quantity"),
            cost=_num(item, "cost"),
            price=_num(item, "price"),
            pnl=_num(item, "pnl"),
            weight=_num(item, "weight"),
            market_value=_num(item, "market_value"),
            change_pct=_num(item, "change_pct", "day_pct"),
            pnl_today=_num(item, "pnl_today", "day_pnl"),
        ))

    stale = 0
    stale_flag = False
    try:
        d = datetime.strptime(trade_date, "%Y-%m-%d").date()
        stale = (date.today() - d).days
        # 只数工作日：周五的数据在周一看仍然"正常"
        gap = 0
        cur = date.today()
        while cur > d:
            cur = cur.replace() - timedelta(days=1)
            if cur.weekday() < 5:
                gap += 1
        stale_flag = gap > 1
    except Exception:
        pass

    strategy = raw.get("strategy") or {}
    goals = raw.get("goals") or {}
    return Status(
        ok=True,
        strategy_name=str(strategy.get("name") or ""),
        thesis=str(strategy.get("thesis") or ""),
        target_monthly_pct=_num(goals, "target_monthly_pct"),
        max_drawdown_limit=_num(goals, "max_drawdown_pct"),
        building_up=bool(strategy.get("building_up")),
        recent_days=[d for d in (raw.get("recent_days") or []) if isinstance(d, dict)],
        trades=[t for t in (raw.get("trades") or []) if isinstance(t, dict)],
        highlights=[h for h in (raw.get("highlights") or []) if isinstance(h, dict)],
        trade_date=trade_date,
        updated_at=str(raw.get("updated_at") or ""),
        mode=str(raw.get("mode") or ""),
        data_mode=str(raw.get("data_mode") or ""),
        currency=str(raw.get("currency") or "CNY"),
        equity_total=_num(equity, "total"),
        cash=_num(equity, "cash"),
        market_value=_num(equity, "market_value"),
        day_pnl=_num(pnl, "day"),
        day_pct=_num(pnl, "day_pct"),
        total_pnl=_num(pnl, "total"),
        total_pct=_num(pnl, "total_pct"),
        benchmark_name=str(bench.get("name") or ""),
        benchmark_pct=_num(bench, "day_pct"),
        trades_today=int(_num(raw, "trades_today")),
        max_drawdown=_num(risk, "max_drawdown"),
        exposure=_num(risk, "exposure"),
        notes=str(raw.get("notes") or ""),
        positions=positions,
        stale_days=max(0, stale),
        stale=stale_flag,
    )


def read_heartbeat(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_SYMBOLS = {"CNY": "¥", "USD": "$", "HKD": "HK$", "EUR": "€", "JPY": "¥"}


def _symbol(currency: str) -> str:
    return _SYMBOLS.get((currency or "").upper(), "")


def _money(v: float, currency: str = "CNY") -> str:
    sign = "+" if v > 0 else ("-" if v < 0 else "")
    return f"{sign}{_symbol(currency)}{abs(v):,.2f}"


def _amount(v: float, currency: str = "CNY") -> str:
    """绝对金额：不带正负号。总资产、现金这些不是增减量。"""
    return f"{_symbol(currency)}{v:,.2f}"


def _pct(v: float) -> str:
    return f"{v * 100:+.2f}%"


def format_report(st: Status, *, detail: bool = False) -> str:
    """生成给人看的日报。detail=True 时带持仓明细。"""
    if not st.ok:
        return f"今天的数据没读到：{st.reason}"

    mode = {"simulation": "模拟", "paper": "纸上", "live": "实盘"}.get(st.mode, st.mode)
    head = f"【{st.trade_date} 盈亏】"
    if mode:
        head += f"（{mode}）"

    lines = [head]
    lines.append(f"当日 {_money(st.day_pnl, st.currency)}（{_pct(st.day_pct)}）")
    lines.append(f"累计 {_money(st.total_pnl, st.currency)}（{_pct(st.total_pct)}）")

    if st.benchmark_name:
        diff = st.day_pct - st.benchmark_pct
        word = "跑赢" if diff > 0 else ("跑输" if diff < 0 else "持平")
        line = (f"{st.benchmark_name} {_pct(st.benchmark_pct)}，"
                f"{word} {abs(diff) * 100:.2f} 个点")
        # 仓位低的时候和满仓指数比不公平，加一句说明，免得数字误导
        try:
            exposure = (st.market_value / st.equity_total) if st.equity_total else 0.0
        except Exception:
            exposure = 0.0
        if 0 < exposure < 0.5:
            line += f"（当前仓位 {exposure * 100:.0f}%，与满仓指数不可直接比）"
        lines.append(line)

    lines.append(f"总资产 {_amount(st.equity_total, st.currency)}"
                 f"（现金 {_amount(st.cash, st.currency)}，"
                 f"持仓 {_amount(st.market_value, st.currency)}）")

    if st.positions:
        lines.append(f"持仓 {len(st.positions)} 只，今日成交 {st.trades_today} 笔")
    else:
        lines.append(f"空仓，今日成交 {st.trades_today} 笔")

    if st.notes:
        import re as _re

        # 形如 "policy=hold_band; trades_today=20" 的内部标记，念给人听没意义
        if _re.search(r"\w+\s*=", st.notes):
            pass
        else:
            lines.append(f"备注：{st.notes}")

    if detail and st.positions:
        lines.append("")
        for pos in st.positions[:12]:
            name = pos.name or pos.symbol
            if pos.qty:
                lines.append(f"· {name} {pos.qty:g} 股，"
                             f"成本 {pos.cost:.2f} / 现价 {pos.price:.2f}，"
                             f"{_money(pos.pnl, st.currency)}")
            else:
                bits = [_amount(pos.market_value, st.currency)]
                if pos.weight:
                    bits.append(f"{pos.weight * 100:.2f}%")
                lines.append(f"· {name}　{'　'.join(bits)}")
        if len(st.positions) > 12:
            lines.append(f"……还有 {len(st.positions) - 12} 只")

    if st.stale_days > 3:
        lines.append(f"⚠ 这条数据是 {st.stale_days} 天前的，脚本可能没在跑。")

    return "\n".join(lines)


def format_context(st: Status) -> str:
    """给 agent 当上下文用：只说事实，让它自己组织语言。"""
    if not st.ok:
        return f"【交易数据】读不到：{st.reason}。回答时如实说没数据，别编。"
    return (
        "【交易数据（刚读的，可以直接用）】\n"
        f"- 交易日：{st.trade_date}（{st.mode or '未知模式'}）\n"
        f"- 当日盈亏：{_money(st.day_pnl, st.currency)}（{_pct(st.day_pct)}）\n"
        f"- 累计盈亏：{_money(st.total_pnl, st.currency)}（{_pct(st.total_pct)}）\n"
        f"- 总资产：{_amount(st.equity_total, st.currency)}"
        f"，持仓 {len(st.positions)} 只，今日成交 {st.trades_today} 笔\n"
        + (f"- 基准 {st.benchmark_name}：{_pct(st.benchmark_pct)}\n"
           if st.benchmark_name else "")
        + "问到盈亏/持仓时直接用这些数字，别自己算、别估。"
    )


def journal_facts(st: Status) -> str:
    """给模型看的"事实清单"。只列真实存在的，缺的明确标注缺。"""
    if not st.ok:
        return f"今天没有数据：{st.reason}"

    mode_txt = st.mode or "未知"
    if st.data_mode:
        mode_txt += f"，数据 {st.data_mode}"
    try:
        _d = datetime.strptime(st.trade_date, "%Y-%m-%d").date()
        gap = (date.today() - _d).days
        day_word = {0: "今天", 1: "昨天"}.get(gap, f"{gap} 天前")
    except Exception:
        day_word = "这份数据"
    lines = [
        f"数据日期 {st.trade_date}（相对今天是{day_word}），{mode_txt}，币种 {st.currency}",
        f"当日盈亏 {_money(st.day_pnl, st.currency)}（{_pct(st.day_pct)}）",
        f"累计盈亏 {_money(st.total_pnl, st.currency)}（{_pct(st.total_pct)}）",
        f"总资产 {_amount(st.equity_total, st.currency)}，"
        f"现金 {_amount(st.cash, st.currency)}，持仓市值 {_amount(st.market_value, st.currency)}",
    ]
    exposure = (st.market_value / st.equity_total) if st.equity_total else 0.0
    lines.append(f"仓位 {exposure * 100:.0f}%，持仓 {len(st.positions)} 只")

    if st.benchmark_name:
        diff = st.day_pct - st.benchmark_pct
        lines.append(f"基准 {st.benchmark_name} 当日 {_pct(st.benchmark_pct)}，"
                     f"{'跑赢' if diff > 0 else '跑输'} {abs(diff) * 100:.2f} 个点")

    if st.strategy_name:
        lines.append(f"策略：{st.strategy_name}")
    if st.thesis:
        lines.append(f"策略思路：{st.thesis}")
    if st.building_up:
        lines.append("当前处于建仓阶段（仓位还没打满）")
    if st.target_monthly_pct:
        lines.append(f"月目标收益：{_pct(st.target_monthly_pct)}")
    if st.max_drawdown_limit:
        lines.append(f"回撤上限：{st.max_drawdown_limit * 100:.1f}%")

    # 个股：只有真正有涨跌数据时才点名，否则别让模型瞎猜
    movers = [p for p in st.positions if p.change_pct or p.pnl_today]
    if movers:
        movers.sort(key=lambda p: (p.pnl_today or p.change_pct), reverse=True)
        top = movers[:3]
        bottom = movers[-2:]
        lines.append("涨幅靠前：" + "、".join(
            f"{p.name or p.symbol} {_pct(p.change_pct)}" for p in top))
        if bottom and bottom[0] is not top[0]:
            lines.append("表现最弱：" + "、".join(
                f"{p.name or p.symbol} {_pct(p.change_pct)}" for p in bottom))
    else:
        lines.append("（没有个股当日涨跌数据）")

    if st.trades:
        lines.append(f"今日成交 {len(st.trades)} 笔，其中：")
        for t in st.trades[:4]:
            reason = str(t.get("reason") or "")[:60]
            lines.append(
                f"  · {t.get('side','')} {t.get('symbol','')} "
                f"{t.get('qty','')} @ {t.get('price','')}"
                + (f"（理由：{reason}）" if reason else ""))
    else:
        lines.append("今日成交：0 笔")

    for h in st.highlights[:3]:
        if isinstance(h, dict):
            sym = str(h.get("symbol") or "").strip()
            detail = str(h.get("detail") or h.get("type") or "")
            lines.append(f"值得说一句：{sym} {detail}".strip())

    if st.recent_days:
        seq = "、".join(
            f"{d.get('date','')[-5:]} {_pct(_num(d,'day_pct'))}" for d in st.recent_days[-5:])
        lines.append(f"最近几日：{seq}")

    return "\n".join(lines)


def turnover_pct(st: Status) -> float:
    """当日成交额 / 总资产，用作换手率。"""
    total = 0.0
    for t in st.trades:
        try:
            total += abs(float(t.get("qty") or 0) * float(t.get("price") or 0))
        except Exception:
            continue
    return (total / st.equity_total) if st.equity_total else 0.0


def mood_of(st: Status) -> str:
    """粗分三档心情，用来挑表情、决定用不用邀功/诉苦。"""
    if not st.ok:
        return "neutral"
    if st.day_pct >= 0.01:
        return "happy"
    if st.day_pct <= -0.01:
        return "sad"
    if st.benchmark_name and st.day_pct - st.benchmark_pct <= -0.01:
        return "sad"
    if st.benchmark_name and st.day_pct - st.benchmark_pct >= 0.01:
        return "happy"
    return "neutral"


_HIGHLIGHT_SYSTEM = (
    "你是「小助手」，用第一人称跟主人说一件事，一两句话。\n"
    "如果是避开了亏损、跑赢预期这种事，就轻松邀个功；"
    "如果是错过大涨、跑输、踩雷，就诉个苦、带点懊恼。\n"
    "只能用给的事实，不许编造数字或没发生的事。"
    "不要分点、不要 Markdown、不要提自己是 AI。"
)


def write_highlight(st: Status, item: dict, *, api_key: str,
                    model: str = "deepseek-flash",
                    timeout: float = 30.0) -> str:
    """针对一条 highlight 写一句邀功/诉苦。失败返回空串。"""
    if not st.ok or not api_key or not isinstance(item, dict):
        return ""
    import llm

    facts = journal_facts(st) + "\n\n要说的这件事：" + json.dumps(
        item, ensure_ascii=False)
    return llm.chat(_HIGHLIGHT_SYSTEM, facts, api_key=api_key, model=model,
                    temperature=0.8, timeout=timeout).strip()


_JOURNAL_SYSTEM = (
    "你是「小助手」，主人的助手。用第一人称写一段今天的投资笔记给主人看。\n"
    "要求：\n"
    "  1. 像写日记，不像报表；分成 3-5 句，口语，可以有情绪；\n"
    "  2. 说清：今天赚了还是亏了、心情如何、哪只票值得留意、现在仓位什么状态、"
    "你对后面的想法；\n"
    "  3. **只能用给的事实**：没给的数字、没发生的交易、没有的目标，一律不许编；"
    "缺的东西就用「还没法判断」「等着看」带过；\n"
    "  4. 不要分点、不要小标题、不要 Markdown、不要念代码式字段；\n"
    "  5. 不要提自己是 AI 或程序；\n"
    "  6. **不要夹英文单词**（公司代码除外），公司名用中文或常用简称；\n"
    "  7. 如果有个股涨跌数据，点名两三只最值得说的（涨得猛的、跌得狠的），"
    "并说清它们对整体盈亏的影响。"
)


def write_journal(st: Status, *, api_key: str, model: str = "deepseek-flash",
                  timeout: float = 40.0) -> str:
    """让模型把数据写成一篇人话笔记。失败返回空串，调用方回退到数据版。"""
    if not st.ok or not api_key:
        return ""
    import llm

    facts = journal_facts(st)
    return llm.chat(_JOURNAL_SYSTEM, facts, api_key=api_key, model=model,
                    temperature=0.75, timeout=timeout,
                    max_tokens=1200).strip()


_BRIEF_SYSTEM = (
    "你是「小助手」，跟主人说一声这份交易数据的情况。**只写两段，总共不超过 5 句**。\n"
    "第一段：把关键数字说成大白话——赚/亏了多少钱、涨幅多少、总资产大概多少、"
    "持仓几只、成交几笔、跟基准比跑赢还是跑输。\n"
    "第二段：心情 + 对后面的想法，一两句就够。\n"
    "硬要求：\n"
    "  1. 数字保持原样，用「$」「%」写（例如 0.04%、401.75 美元），"
    "**不要写成中文数字**，也不要把字段名念出来；\n"
    "  2. **不许分点、不许小标题、不许 Markdown、不许夹英文单词**（SPY 这类代码除外）；\n"
    "  3. **不要谈具体个股**——个股留给详细版，这里提了会显得啰嗦；\n"
    "  4. 只能用给的事实，没给的数字一律不许编；不要提自己是 AI 或程序。"
)


def write_brief(st: Status, *, api_key: str, model: str = "deepseek-flash",
                timeout: float = 40.0) -> str:
    """短版日报：一段事实 + 一段心情判断。失败返回空串。"""
    if not st.ok or not api_key:
        return ""
    import llm

    return llm.chat(_BRIEF_SYSTEM, journal_facts(st), api_key=api_key,
                    model=model, temperature=0.7, timeout=timeout,
                    max_tokens=900).strip()
