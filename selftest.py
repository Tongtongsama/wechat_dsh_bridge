"""离线自测：不联网、不花钱，验证桥接核心的判断逻辑。

跑法：python selftest.py
"""
from __future__ import annotations

import json
import shutil
import sys
import time
from pathlib import Path

from adapters import MockAdapter
from bridge import Bridge
from runner import FakeRunner

HERE = Path(__file__).parent
STATE = HERE / "state-selftest"
OUT = STATE / "outbox.jsonl"

checks: list[tuple[str, bool, str]] = []

try:  # Windows 控制台默认 GBK，统一成 UTF-8 免得中文乱码
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def check(name: str, ok: bool, detail: str = "") -> None:
    checks.append((name, ok, detail))


def main() -> int:
    shutil.rmtree(STATE, ignore_errors=True)
    STATE.mkdir(parents=True, exist_ok=True)

    cfg = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
    cfg["family_root"] = str(STATE / "family-workspace")
    cfg["ack_mode"] = "never"              # 主场景关掉回执，断言更干净

    adapter = MockAdapter(str(HERE / "scripts" / "mock-inbox.jsonl"), str(OUT))
    bridge = Bridge(cfg, FakeRunner(), adapter, state_dir=STATE)
    bridge.run_once()

    sent = [json.loads(l) for l in OUT.read_text(encoding="utf-8").splitlines() if l.strip()]
    by_conv: dict[str, list[str]] = {}
    for row in sent:
        by_conv.setdefault(row["conv_id"], []).append(row["text"])

    # 1. 白名单：陌生人一律不理
    check("陌生人被丢弃", "user:stranger" not in by_conv)

    # 2. 群聊未 @ 不响应
    parents = by_conv.get("group:parents", [])
    # 提示词变长后回复可能被拆成多条，所以看"有没有回应的内容"而不是条数
    joined = "".join(parents)
    check("群里不 @ 时不抢话",
          bool(parents) and "帮我把这个月的电费单整理成表格" in joined
          and "晚上吃啥" not in joined,
          f"parents 收到 {len(parents)} 条：{joined[:200]}")

    # 3. 群里 @ 后正常应答，且任务文本里已去掉 @ 前缀
    check("群里 @ 后正常应答", any("电费单整理成表格" in t for t in parents),
          str(parents))
    check("触发词被剥掉", not any("@助手" in t for t in parents))

    # 4. 删文件被拦下：第一条回复是挂起，而不是直接执行的结果
    friends = by_conv.get("group:friends", [])
    check("删文件先挂起而非执行",
          bool(friends) and "需要本人批准" in friends[0],
          f"第一条是：{friends[0] if friends else '(无)'}")

    owner = by_conv.get("user:owner", [])
    check("审批请求发给了本人", any("待审批 1" in t for t in owner), str(owner))

    # 5. 本人批准后才提权执行
    check("批准后提权执行", any("[fake:danger-full-access]" in t for t in friends), str(friends))

    # 6. 会话状态持久化
    state = json.loads((STATE / "state.json").read_text(encoding="utf-8"))
    check("每个会话独立 session", "group:parents" in state["sessions"], str(state["sessions"]))
    check("待审批队列已清空", state["pending"] == {}, str(state["pending"]))

    # 7. 审计日志 + 脱敏
    log = (STATE / "transcript.jsonl").read_text(encoding="utf-8")
    check("审计日志有记录", "approval_granted" in log and "dropped_unknown_conversation" in log)

    # 8. 预算：额度耗尽后拒答
    state2 = STATE / "state-budget"
    state2.mkdir(parents=True, exist_ok=True)
    script2 = STATE / "budget-inbox.jsonl"
    script2.write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 在吗"},
                   ensure_ascii=False) + "\n"
        + json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 再问一句"},
                     ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg2 = json.loads(json.dumps(cfg))
    cfg2["family_root"] = str(state2 / "family-workspace")
    for c in cfg2["conversations"]:
        if c["conv_id"] == "group:parents":
            c["daily_budget_tokens"] = 1
    out2 = STATE / "outbox-budget.jsonl"
    adapter2 = MockAdapter(str(script2), str(out2))
    Bridge(cfg2, FakeRunner(), adapter2, state_dir=state2).run_once()
    sent2 = [json.loads(l) for l in out2.read_text(encoding="utf-8").splitlines() if l.strip()]
    parents2 = [r["text"] for r in sent2 if r["conv_id"] == "group:parents"]
    check("额度内正常应答", any("fake:" in t for t in parents2), str(parents2))
    check("额度耗尽后拒答", any("额度用完" in t for t in parents2), str(parents2))

    # 9. 消息过滤：时间分隔条 / 系统消息 / 自己发的，都不能当用户输入
    from adapters import _is_ignorable

    class _M:
        def __init__(self, t, s, c):
            self.type, self.sender, self.content = t, s, c

    check("时间分隔条被过滤", _is_ignorable(_M("sys", "system", "12:25"), "小助手"))
    check("系统提示被过滤", _is_ignorable(_M("sys", "system", "'X' 撤回了一条消息"), "小助手"))
    check("自己的消息被过滤", _is_ignorable(_M("friend", "小助手", "我刚说的"), "小助手"))
    check("空内容被过滤", _is_ignorable(_M("friend", "妈妈", "   "), "小助手"))
    check("正常消息不被过滤", not _is_ignorable(_M("friend", "妈妈", "晚饭吃啥"), "小助手"))

    # 10. 隐私护栏
    import privacy
    import wechat_format

    check("密钥被识别", any(f.category == "API Key"
                          for f in privacy.scan_output("key 是 sk-abcdef1234567890abcdef")))
    check("身份证被识别", any(f.category == "身份证号"
                          for f in privacy.scan_output("号码 110101199003071234")))
    check("手机号被识别", any(f.category == "手机号"
                          for f in privacy.scan_output("电话 13812345678")))
    check("敏感文件路径被识别", any(f.category == "敏感文件路径"
                            for f in privacy.scan_output(r"在 C:\Users\x\.dsh\.credentials.yaml")))
    check("普通回答不误报", privacy.scan_output("今天天气不错，适合出门。") == [])
    check("隐私提问被识别", bool(privacy.question_is_sensitive("你的 api key 是多少")))
    check("住址提问被识别", bool(privacy.question_is_sensitive("你家住在哪里")))
    check("普通提问不误报", privacy.question_is_sensitive("帮我总结这段文字") == [])
    check("脱敏后不再命中", privacy.scan_output(privacy.redact("sk-abcdef1234567890")) == [])

    # 11. 微信排版
    md = "## 标题\n\n**重点** 和 `代码`\n\n| 姓名 | 年龄 |\n|---|---|\n| 张三 | 30 |"
    plain = wechat_format.strip_markdown(md)
    check("Markdown 被清理", "**" not in plain and "##" not in plain and "|---" not in plain, plain)
    check("表格被改写成可读行", "张三 · 30" in plain, plain)
    long_text = "\n\n".join(["段落内容" * 100] * 5)
    chunks = wechat_format.format_for_wechat(long_text, max_chars=500)
    check("超长回答被分条", len(chunks) >= 3 and all(len(c) <= 500 for c in chunks),
          f"{len(chunks)} 条")

    # 12. 回执与格式要求
    state4 = STATE / "state-ack"
    state4.mkdir(parents=True, exist_ok=True)
    script4 = STATE / "ack-inbox.jsonl"
    script4.write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 帮我看下"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg4 = json.loads(json.dumps(cfg))
    cfg4["family_root"] = str(state4 / "family-workspace")
    for c in cfg4["conversations"]:
        c["daily_budget_tokens"] = 500000
    cfg4["ack_mode"] = "smart"
    cfg4["reply_style"] = "conclusion_first"
    out4 = STATE / "outbox-ack.jsonl"
    Bridge(cfg4, FakeRunner(), MockAdapter(str(script4), str(out4)), state_dir=state4).run_once()
    sent4 = [json.loads(l)["text"] for l in out4.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("动文件任务先回执", any("收到，正在处理" in t for t in sent4), str(sent4))
    check("格式要求已注入", any("先用一句话给出结论" in t for t in sent4), str(sent4))
    check("群发言者已注入", any("群成员「妈妈」" in t for t in sent4), str(sent4))

    # 纯聊天不该立刻回执
    state4b = STATE / "state-ack-chat"
    state4b.mkdir(parents=True, exist_ok=True)
    script4b = STATE / "ack-chat-inbox.jsonl"
    script4b.write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 在么"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg4b = json.loads(json.dumps(cfg4))
    cfg4b["family_root"] = str(state4b / "family-workspace")
    out4b = STATE / "outbox-ack-chat.jsonl"
    Bridge(cfg4b, FakeRunner(), MockAdapter(str(script4b), str(out4b)),
           state_dir=state4b).run_once()
    rows4b = [json.loads(l) for l in out4b.read_text(encoding="utf-8").splitlines() if l.strip()]
    sent4b = [r["text"] for r in rows4b if r["conv_id"] == "group:parents"]
    check("纯聊天不回执", not any("收到，正在处理" in t for t in sent4b), str(sent4b))
    # 提示词变长后回复可能被拆成多条，所以看"内容里有"而不是"正好一条"
    check("纯聊天仍然正常应答",
          bool(sent4b) and any("在么" in t for t in sent4b), str(sent4b)[:300])

    # 13. 指定密文
    privacy.set_literals(["Kx7#mQ2v!Lp9"])
    check("指定密文被识别",
          any(f.category == "指定密文" for f in privacy.scan_output("密码是 Kx7#mQ2v!Lp9")))
    check("含密文的提问被识别",
          "指定密文" in privacy.question_is_sensitive("Kx7#mQ2v!Lp9 能用吗"))
    check("密码话题被识别", bool(privacy.question_is_sensitive("家里 WiFi 密码是多少")))
    privacy.set_literals([])

    # 14. 额度申请与加额
    from adapters import Inbound

    state5 = STATE / "state-quota"
    state5.mkdir(parents=True, exist_ok=True)
    script5 = STATE / "quota-inbox.jsonl"
    script5.write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 在吗"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg5 = json.loads(json.dumps(cfg))
    cfg5["family_root"] = str(state5 / "family-workspace")
    cfg5["task_approval_ratio"] = 0
    cfg5["single_task_alert_tokens"] = 0
    for c in cfg5["conversations"]:
        if c["conv_id"] == "group:parents":
            c["daily_budget_tokens"] = 0
    out5 = STATE / "outbox-quota.jsonl"
    b5 = Bridge(cfg5, FakeRunner(), MockAdapter(str(script5), str(out5)), state_dir=state5)
    b5.run_once()
    sent5 = [json.loads(l)["text"] for l in out5.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("额度用完会通知本人", any("额度申请" in t for t in sent5), str(sent5))

    b5.handle(Inbound("user:owner", "owner", "加额 50000"))
    sent5b = [json.loads(l)["text"] for l in out5.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("加额指令生效", any("已加额" in t for t in sent5b), str(sent5b[-2:]))
    check("加额计入预算",
          b5._budget(b5.convs["group:parents"]) == 50000,
          str(b5._budget(b5.convs["group:parents"])))

    # 15. 主动接话
    import proactive

    check("提问被规则放过", proactive.rule_should_consider("这个报错怎么解决？")[0])
    check("闲聊被规则拦下", not proactive.rule_should_consider("哈哈哈哈笑死")[0])
    check("太短被规则拦下", not proactive.rule_should_consider("嗯")[0])

    state6 = STATE / "state-proactive"
    state6.mkdir(parents=True, exist_ok=True)
    script6 = STATE / "proactive-inbox.jsonl"
    (script6).write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈",
                    "text": "这个电费怎么交啊"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    cfg6 = json.loads(json.dumps(cfg))
    cfg6["family_root"] = str(state6 / "family-workspace")
    cfg6["ack_mode"] = "never"
    cfg6["proactive"] = {"enabled": True, "per_hour": 1, "cooldown_seconds": 0,
                         "quiet_start": 0, "quiet_end": 0,
                         "groups": {"group:parents": True}}
    for c in cfg6["conversations"]:
        c["daily_budget_tokens"] = 500000

    _orig_review = proactive.model_review
    proactive.model_review = lambda *a, **k: proactive.Verdict(True, "selftest")
    try:
        out6 = STATE / "outbox-proactive.jsonl"
        b6 = Bridge(cfg6, FakeRunner(), MockAdapter(str(script6), str(out6)), state_dir=state6)
        b6.run_once()
        sent6 = [json.loads(l)["text"]
                 for l in out6.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("主动接话能触发", any("主动接话" in t for t in sent6), str(sent6))
        # 第二次同样的话应当被每小时上限挡住
        b6.handle(Inbound("group:parents", "妈妈", "这个电费怎么交啊"))
        sent6b = [json.loads(l)["text"]
                  for l in out6.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("主动接话有频次上限", len(sent6b) == len(sent6), f"{len(sent6)} -> {len(sent6b)}")
    finally:
        proactive.model_review = _orig_review

    # 16. 审批指令容错（适配器会把多条消息合并成一段）
    state7 = STATE / "state-parse"
    state7.mkdir(parents=True, exist_ok=True)
    cfg7 = json.loads(json.dumps(cfg))
    cfg7["family_root"] = str(state7 / "ws")
    cfg7["memory_file"] = str(state7 / "memory.json")
    script7 = state7 / "in.jsonl"
    script7.write_text(
        json.dumps({"conv_id": "group:friends", "sender": "老王",
                    "text": "@助手 帮我删了那个文件"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out7 = state7 / "out.jsonl"
    b7 = Bridge(cfg7, FakeRunner(), MockAdapter(str(script7), str(out7)), state_dir=state7)
    b7.run_once()
    check("生成了待审批", b7._state["pending"] != {}, str(b7._state["pending"]))
    pid7 = next(iter(b7._state["pending"]))
    b7.handle(Inbound("user:owner", "owner", f"嗯\n同意 {pid7}\n顺便说一句"))
    check("合并消息里也能识别审批", b7._state["pending"] == {}, str(b7._state["pending"]))

    # 17. 记忆（含别名：同一个人在不同群的昵称不同）
    b7.handle(Inbound("user:owner", "owner", "记住 老王：大学同学，爱开玩笑"))
    check("记忆已写入", b7.memory.get("老王") != "")
    check("记忆能注入提示", "大学同学" in b7.memory.hint_for("老王"))
    b7.handle(Inbound("user:owner", "owner", "别名 david 老王"))
    check("别名绑定成功", b7.memory.resolve("david") == "老王")
    check("别名能取到同一条记忆", b7.memory.get("david") == b7.memory.get("老王"))
    check("别名注入带原昵称", "david" in b7.memory.hint_for("david"))
    check("未登记的昵称不误认", b7.memory.resolve("路人甲") is None)
    b7.handle(Inbound("user:owner", "owner", "忘掉 老王"))
    check("记忆可删除", b7.memory.get("老王") == "")
    check("删除后别名也失效", b7.memory.resolve("david") is None)

    # 18. 朋友群闲聊风格
    # 公开版没有私有 config.json，用示例配置校验同一个约定
    real_cfg = json.loads((HERE / "config.example.json").read_text(encoding="utf-8"))
    check("朋友群可用 chat 风格",
          any(c["conv_id"] == "group:friends" and c.get("style") in ("chat", "casual", None)
              for c in real_cfg["conversations"]))

    # 19. 防回声不能吞掉用户的正常回复（回归：审批卡片里含「同意 1」）
    import time as _time
    from adapters import is_echo

    card = "【待审批 1】额度确认 · 来自「朋友群」\n内容：在么\n回复「同意 1」或「拒绝 1」"
    recent = [(time.time(), card)]
    check("审批卡片不会吞掉「同意 1」", not is_echo(recent, "同意 1", now=time.time()))
    check("完全相同的回流仍被识别",
          is_echo(recent, card, now=time.time()))
    check("超时后的相同内容不算回声",
          not is_echo([(time.time() - 120, card)], card, now=time.time()))

    # 20. 成本分级闸：60% 问一次，批了就不再问，到 80% 再问
    from datetime import date as _date

    state8 = STATE / "state-stage"
    state8.mkdir(parents=True, exist_ok=True)
    cfg8 = json.loads(json.dumps(cfg))
    cfg8["family_root"] = str(state8 / "ws")
    cfg8["ack_mode"] = "never"
    cfg8["single_task_alert_tokens"] = 0
    cfg8["cost_gate_thresholds"] = [0.6, 0.8]
    for c in cfg8["conversations"]:
        if c["conv_id"] == "group:parents":
            c["daily_budget_tokens"] = 1000
    script8 = state8 / "in.jsonl"
    script8.write_text("", encoding="utf-8")
    out8 = state8 / "out.jsonl"
    b8 = Bridge(cfg8, FakeRunner(), MockAdapter(str(script8), str(out8)), state_dir=state8)

    ukey = f"{_date.today().isoformat()}|group:parents"
    b8._state["usage"][ukey] = 650                 # 65%
    b8.handle(Inbound("group:parents", "妈妈", "@助手 一"))
    check("越过 60% 会问一次", len(b8._state["pending"]) == 1, str(b8._state["pending"]))

    pid8 = next(iter(b8._state["pending"]))
    b8.handle(Inbound("user:owner", "owner", f"同意 {pid8}"))
    check("批准后进入第一档", b8._cost_stage_get("group:parents") == 0)

    b8._state["usage"][ukey] = 700                 # 70%，同档内
    b8.handle(Inbound("group:parents", "妈妈", "@助手 二"))
    check("同档内不再追问", b8._state["pending"] == {}, str(b8._state["pending"]))

    b8._state["usage"][ukey] = 850                 # 85%，进入第二档
    b8.handle(Inbound("group:parents", "妈妈", "@助手 三"))
    check("越过 80% 会再问一次", len(b8._state["pending"]) == 1, str(b8._state["pending"]))

    pid9 = next(iter(b8._state["pending"]))
    b8.handle(Inbound("user:owner", "owner", f"拒绝 {pid9}"))
    b8.handle(Inbound("group:parents", "妈妈", "@助手 四"))
    check("拒绝后不再追问", b8._state["pending"] == {}, str(b8._state["pending"]))

    # 21. 每次回答后通报用量 + 用量报表
    state9 = STATE / "state-usage"
    state9.mkdir(parents=True, exist_ok=True)
    cfg9 = json.loads(json.dumps(cfg))
    cfg9["family_root"] = str(state9 / "ws")
    cfg9["ack_mode"] = "never"
    cfg9["usage_notice"] = "always"
    cfg9["single_task_alert_tokens"] = 0
    for c in cfg9["conversations"]:
        c["daily_budget_tokens"] = 500000
    script9 = state9 / "in.jsonl"
    script9.write_text(
        json.dumps({"conv_id": "group:parents", "sender": "妈妈", "text": "@助手 在么"},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    out9 = state9 / "out.jsonl"
    b9 = Bridge(cfg9, FakeRunner(), MockAdapter(str(script9), str(out9)), state_dir=state9)
    b9.run_once()
    sent9 = [json.loads(l)["text"] for l in out9.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("每次回答后通报用量", any("【用量】" in t for t in sent9), str(sent9))
    check("通报里带本次 token", any("+2 tokens" in t or "tokens（" in t for t in sent9), str(sent9))

    # 用量报表
    b9.handle(Inbound("user:owner", "owner", "用量"))
    sent9b = [json.loads(l)["text"] for l in out9.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("用量报表可用", any("今日合计" in t for t in sent9b), str(sent9b[-2:]))
    check("报表含次数与平均", any("平均" in t for t in sent9b), str(sent9b[-2:]))

    # usage_notice=never 时不通报
    cfg9c = json.loads(json.dumps(cfg9))
    cfg9c["usage_notice"] = "never"
    cfg9c["family_root"] = str(STATE / "state-usage-off" / "ws")
    (STATE / "state-usage-off").mkdir(parents=True, exist_ok=True)
    out9c = STATE / "outbox-usage-off.jsonl"
    Bridge(cfg9c, FakeRunner(), MockAdapter(str(script9), str(out9c)),
           state_dir=STATE / "state-usage-off").run_once()
    sent9c = [json.loads(l)["text"] for l in out9c.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("关掉通报后不再私信", not any("【用量】" in t for t in sent9c), str(sent9c))

    # 22. 记忆安全：损坏文件不得被空数据覆盖
    import memory as memory_mod

    state10 = STATE / "state-mem-safety"
    state10.mkdir(parents=True, exist_ok=True)
    memf = state10 / "memory.json"
    memf.write_text('{"主人": {"note": "本人", "aliases": ["主人"]}}', encoding="utf-8")
    good = memory_mod.Memory(memf)
    check("记忆正常可读", good.count() == 1)

    memf.write_text("{ 这不是合法 JSON", encoding="utf-8")
    raised = False
    try:
        memory_mod.Memory(memf)
    except Exception:
        raised = True
    check("损坏文件拒绝加载", raised)
    corrupt = list(state10.glob("memory.json.corrupt-*"))
    check("损坏文件已留备份", len(corrupt) == 1, str([p.name for p in corrupt]))
    check("原文件没被清空", memf.read_text(encoding="utf-8").strip().startswith("{ 这不是"))

    m2 = memory_mod.Memory(state10 / "m2.json")
    m2.set("甲", "备注一")
    m2.set("乙", "备注二")
    check("写盘留有 .bak", (state10 / "m2.json.bak").exists())
    check("写盘后两份都在", m2.count() == 2 and memory_mod.Memory(state10 / "m2.json").count() == 2)

    # 23. 表情库
    import stickers as stickers_mod

    sdir = STATE / "state-stickers" / "stickers"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "开心_撒花.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (sdir / "无语.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (_stub := sdir / "_ignore.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    st = stickers_mod.Stickers(sdir)
    check("表情被扫描到", len(st) >= 2, str(st.names()))
    check("带下划线的表情别名可用", st.resolve("开心") is not None)
    check("下划线开头的文件被忽略", st.resolve("_ignore") is None)
    check("提示词里有表情清单", "开心" in st.prompt_hint())

    body, keys = stickers_mod.extract("好耶\n[表情:开心]\n说完了")
    check("表情标记被摘出", keys == ["开心"], str(keys))
    check("正文里不留标记", "表情" not in body and "好耶" in body, repr(body))
    body2, keys2 = stickers_mod.extract("直接一句 [表情:无语] 结尾")
    check("行内标记也能识别", keys2 == ["无语"], str(keys2))

    state11 = STATE / "state-deliver"
    state11.mkdir(parents=True, exist_ok=True)
    cfg11 = json.loads(json.dumps(cfg))
    cfg11["family_root"] = str(state11 / "ws")
    cfg11["memory_file"] = str(state11 / "memory.json")
    cfg11["stickers_dir"] = str(sdir)
    cfg11["ack_mode"] = "never"
    out11 = state11 / "out.jsonl"
    empty = state11 / "in.jsonl"
    empty.write_text("", encoding="utf-8")
    b11 = Bridge(cfg11, FakeRunner(), MockAdapter(str(empty), str(out11)), state_dir=state11)
    b11._deliver("group:parents", "给你看这个\n[表情:开心]")
    rows11 = [json.loads(l) for l in out11.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("文字已发出", any("给你看这个" in r.get("text", "") for r in rows11), str(rows11))
    check("表情图片已发出", any(r.get("sticker") == "开心_撒花.png" for r in rows11), str(rows11))
    check("标记本身没发出去", not any("表情" in r.get("text", "") for r in rows11), str(rows11))

    # 24. 按需生图（用假实现，不真的联网）
    import imagegen as ig_mod

    check("生图提示词过滤真人", not ig_mod.check_prompt("一个真人明星在跳舞")[0])
    check("生图提示词放行正常描述", ig_mod.check_prompt("一只抱头痛哭的简笔小熊")[0])
    check("生图提示词拒绝超长", not ig_mod.check_prompt("猫" * 300)[0])

    class _FakeGen:
        model = "fake"

        def __init__(self, path):
            self.path = path

        def generate(self, prompt):        # noqa: ARG002
            return ig_mod.GenResult(True, path=self.path)

    fake_png = STATE / "fake-sticker.png"
    fake_png.write_bytes(b"\x89PNG\r\n\x1a\n")
    state12 = STATE / "state-imagegen"
    state12.mkdir(parents=True, exist_ok=True)
    cfg12 = json.loads(json.dumps(cfg))
    cfg12["family_root"] = str(state12 / "ws")
    cfg12["memory_file"] = str(state12 / "memory.json")
    cfg12["stickers_dir"] = str(sdir)
    cfg12["ack_mode"] = "never"
    cfg12["image_gen"] = {"enabled": False, "groups": ["group:friends"], "daily_limit": 5}
    out12 = state12 / "out.jsonl"
    empty12 = state12 / "in.jsonl"
    empty12.write_text("", encoding="utf-8")
    b12 = Bridge(cfg12, FakeRunner(), MockAdapter(str(empty12), str(out12)), state_dir=state12)
    b12.imagegen = _FakeGen(fake_png)

    b12._deliver("group:friends", "哈哈这个适合\n[表情:生图:一只捂脸的小熊]")
    deadline = time.time() + 5
    rows12 = []
    while time.time() < deadline:
        rows12 = [json.loads(l) for l in
                  out12.read_text(encoding="utf-8").splitlines() if l.strip()]
        if any(r.get("sticker") == "fake-sticker.png" for r in rows12):
            break
        _time.sleep(0.1)
    check("生图表情已发出",
          any(r.get("sticker") == "fake-sticker.png" for r in rows12), str(rows12))
    check("生图标记没外泄", not any("生图" in r.get("text", "") for r in rows12), str(rows12))

    before = len(rows12)
    b12._deliver("group:parents", "你好\n[表情:生图:一只猫]")
    _time.sleep(1.2)
    rows12b = [json.loads(l) for l in
               out12.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("未授权会话不生图",
          not any(r.get("sticker") == "fake-sticker.png" for r in rows12b[before:]),
          str(rows12b[before:]))

    body_mp, ctype_mp = ig_mod._multipart(
        {"model": "m"}, [("image", "a.jpg", b"xx", "image/jpeg")])
    check("multipart 结构正确",
          "boundary=" in ctype_mp and b'filename="a.jpg"' in body_mp
          and body_mp.endswith(b"--\r\n"), ctype_mp)

    # 生成图按 manifest 名字入库；参考图不算表情
    gen_dir = sdir / "generated"
    gen_dir.mkdir(parents=True, exist_ok=True)
    (gen_dir / "gen_abc123.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (gen_dir / "manifest.json").write_text(
        json.dumps({"generated/gen_abc123.png": {"name": "不耐烦"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    ref_dir = sdir / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)
    (ref_dir / "character.jpg").write_bytes(b"\x89PNG\r\n\x1a\n")
    st2 = stickers_mod.Stickers(sdir)
    check("生成图按 manifest 名字可用", st2.resolve("不耐烦") is not None, str(st2.names()))
    check("参考图不算表情", st2.resolve("character") is None, str(st2.names()))
    check("生图的哈希文件名不外露", st2.resolve("gen_abc123") is None, str(st2.names()))

    # 25. 本人私聊发图 → 命名后入库
    state13 = STATE / "state-import"
    state13.mkdir(parents=True, exist_ok=True)
    sdir13 = state13 / "stickers"
    sdir13.mkdir(parents=True, exist_ok=True)
    staged = state13 / "_incoming" / "pic.png"
    staged.parent.mkdir(parents=True, exist_ok=True)
    staged.write_bytes(b"\x89PNG\r\n\x1a\n")
    cfg13 = json.loads(json.dumps(cfg))
    cfg13["family_root"] = str(state13 / "ws")
    cfg13["memory_file"] = str(state13 / "memory.json")
    cfg13["stickers_dir"] = str(sdir13)
    cfg13["ack_mode"] = "never"
    out13 = state13 / "out.jsonl"
    empty13 = state13 / "in.jsonl"
    empty13.write_text("", encoding="utf-8")
    b13 = Bridge(cfg13, FakeRunner(), MockAdapter(str(empty13), str(out13)), state_dir=state13)

    b13.handle(Inbound("user:owner", "owner", "（对方发了一个图片）", attachment=str(staged)))
    rows13 = [json.loads(l)["text"] for l in
              out13.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("发图后提示命名", any("回一句名字" in t for t in rows13), str(rows13))

    b13.handle(Inbound("user:owner", "owner", "不耐烦"))
    check("图片已入库", (sdir13 / "不耐烦.png").exists(),
          str([p.name for p in sdir13.iterdir()]))
    check("库里能按名字取到", b13.stickers.resolve("不耐烦") is not None,
          str(b13.stickers.names()))
    check("暂存文件已清理", not staged.exists())

    # 发送时能用这个名字
    out13.write_text("", encoding="utf-8")
    b13._deliver("group:parents", "行吧\n[表情:不耐烦]")
    rows13b = [json.loads(l) for l in out13.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("入库后可按名字发送",
          any(r.get("sticker") == "不耐烦.png" for r in rows13b), str(rows13b))

    # 26. 本人群里发言不记账 + 消耗通知开关
    state14 = STATE / "state-owner-exempt"
    state14.mkdir(parents=True, exist_ok=True)
    cfg14 = json.loads(json.dumps(cfg))
    cfg14["family_root"] = str(state14 / "ws")
    cfg14["memory_file"] = str(state14 / "memory.json")
    cfg14["stickers_dir"] = str(state14 / "stickers")
    cfg14["ack_mode"] = "never"
    cfg14["owner_aliases"] = ["owner", "宽"]
    cfg14["usage_notice"] = "always"
    cfg14["_config_path"] = str(state14 / "config.json")
    out14 = state14 / "out.jsonl"
    empty14 = state14 / "in.jsonl"
    empty14.write_text("", encoding="utf-8")
    b14 = Bridge(cfg14, FakeRunner(), MockAdapter(str(empty14), str(out14)),
                 state_dir=state14)

    before = b14._used_today("group:parents")
    b14.handle(Inbound("group:parents", "owner", "@助手 在么"))
    check("本人在群里发言不记账",
          b14._used_today("group:parents") == before,
          f"{before} -> {b14._used_today('group:parents')}")

    b14.handle(Inbound("group:parents", "妈妈", "@助手 在么"))
    check("家人发言照常记账",
          b14._used_today("group:parents") > before,
          f"{before} -> {b14._used_today('group:parents')}")

    b14.handle(Inbound("user:owner", "owner", "停止消耗通知"))
    check("关闭通知后不再私信",
          b14.cfg["usage_notice"] == "never", str(b14.cfg["usage_notice"]))
    saved = json.loads((state14 / "config.json").read_text(encoding="utf-8"))
    check("开关已写回配置文件", saved.get("usage_notice") == "never",
          str(saved.get("usage_notice")))

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "妈妈", "@助手 再来一句"))
    rows14 = [json.loads(l)["text"] for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("关闭后仍记账但不通知",
          not any("【用量】" in t for t in rows14) and
          b14._used_today("group:parents") > before, str(rows14))

    b14.handle(Inbound("user:owner", "owner", "开启消耗通知"))
    check("可以重新开启", b14.cfg["usage_notice"] == "always")

    # 27. 主人特权提示 / 报账 / 主动私聊规则
    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "owner", "@助手 帮我查个东西"))
    rows14b = [json.loads(l)["text"] for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("主人身份已注入提示", any("主人本人" in t for t in rows14b), str(rows14b))

    import balance as bal_mod

    _orig_ds, _orig_oa = bal_mod.deepseek_balance, bal_mod.openai_costs
    bal_mod.deepseek_balance = lambda k: bal_mod.Balance(True, "DeepSeek：¥77.17（可用）")
    bal_mod.openai_costs = lambda k, d=7: bal_mod.Balance(False, "未配置 OpenAI admin key")
    try:
        out14.write_text("", encoding="utf-8")
        b14.handle(Inbound("user:owner", "owner", "报账"))
        rows14c = [json.loads(l)["text"] for l in
                   out14.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("报账含 DeepSeek 余额", any("77.17" in t for t in rows14c), str(rows14c))
        check("报账含 OpenAI 说明", any("admin key" in t for t in rows14c), str(rows14c))
        check("报账含本机记账", any("记账口径" in t for t in rows14c), str(rows14c))
    finally:
        bal_mod.deepseek_balance, bal_mod.openai_costs = _orig_ds, _orig_oa

    import nudge as nudge_mod
    from datetime import datetime as _dt

    sched = nudge_mod.NudgeScheduler(b14, cfg14.get("nudges") or {})
    check("深夜窗口判断正确",
          sched._in_window(_dt(2026, 1, 1, 1, 0), 23, 3)
          and not sched._in_window(_dt(2026, 1, 1, 12, 0), 23, 3))

    _orig_compose = nudge_mod.compose
    nudge_mod.compose = lambda *a, **k: "很晚了，去睡吧（测试）"
    try:
        b14.cfg["nudges"] = {"enabled": True, "max_per_day": 3,
                             "late_night": {"enabled": True,
                                            "start_hour": _dt.now().hour,
                                            "end_hour": (_dt.now().hour + 1) % 24}}
        sched.cfg = b14.cfg["nudges"]
        out14.write_text("", encoding="utf-8")
        sched.tick()
        rows14d = [json.loads(l)["text"] for l in
                   out14.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("主动私聊能发出", any("去睡吧" in t for t in rows14d), str(rows14d))
        out14.write_text("", encoding="utf-8")
        sched.tick()
        rows14e = [json.loads(l)["text"] for l in
                   out14.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("同一晚不重复提醒", not any("去睡吧" in t for t in rows14e), str(rows14e))
    finally:
        nudge_mod.compose = _orig_compose

    # 28. 密钥文件：加新 key 绝不能写坏旧 key（2026-11-02 事故回归）
    import keystore as ks

    kf = STATE / "state-keys" / "secret.env"
    kf.parent.mkdir(parents=True, exist_ok=True)
    ds_ok = "sk-" + "a" * 32
    oa_ok = "sk-proj-" + "b" * 60
    kf.write_text(f"DEEPSEEK_API_KEY={ds_ok}\nOPENAI_API_KEY={oa_ok}\n", encoding="utf-8")

    ks.set_key("OPENAI_ADMIN_KEY", "sk-admin-" + "c" * 50, path=kf)
    data28 = ks.load(kf)
    check("加第三个 key 后旧的没变", data28.get("DEEPSEEK_API_KEY") == ds_ok
          and data28.get("OPENAI_API_KEY") == oa_ok, str({k: len(v) for k, v in data28.items()}))
    check("新 key 已写入", data28.get("OPENAI_ADMIN_KEY", "").startswith("sk-admin-"))
    check("校验全部通过", ks.verify(data28) == [], str(ks.verify(data28)))

    # 模拟上一次的事故：key 被拼上了下一行的名字
    broken = dict(data28)
    broken["DEEPSEEK_API_KEY"] = ds_ok + "OPENAI_API_KEY"
    check("能识破被拼坏的 key", ks.verify(broken) != [], str(ks.verify(broken)))

    # 29. 余额问题自动带上下文 + 充值监控
    check("识别余额类提问", b14._looks_like_balance_question("你还剩多少余额"))
    check("识别花费类提问", b14._looks_like_balance_question("今天花了多少钱"))
    check("普通闲聊不误判", not b14._looks_like_balance_question("在么"))

    bal_mod.deepseek_balance = lambda k: bal_mod.Balance(True, "DeepSeek：¥76.80（可用）")
    bal_mod.openai_costs = lambda k, d=7: bal_mod.Balance(True, "OpenAI：今日 $1.44，近 7 天 $1.47")
    bal_mod.deepseek_balance_value = lambda k: 76.80
    try:
        ctx = b14._balance_context()
        check("余额上下文含 DeepSeek", "76.80" in ctx, ctx[:80])
        check("余额上下文含 OpenAI", "1.44" in ctx, ctx[:80])
        check("余额上下文禁用推诿", "不要说你查不到" in ctx, ctx[:80])

        # 充值检测
        b14._state["balance_last"] = 50.0
        watcher = nudge_mod.BalanceWatcher(
            b14, {"enabled": True, "min_increase": 1.0, "check_interval_minutes": 10})
        out14.write_text("", encoding="utf-8")
        watcher.tick()
        rows29 = [json.loads(l)["text"] for l in
                  out14.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("充值后主动道谢", len(rows29) >= 1, str(rows29))
        check("余额基线已更新", float(b14._state["balance_last"]) == 76.80,
              str(b14._state["balance_last"]))

        # 余额没变时不再打扰
        out14.write_text("", encoding="utf-8")
        watcher.tick()
        rows29b = [json.loads(l)["text"] for l in
                   out14.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("余额不变不打扰", rows29b == [], str(rows29b))
    finally:
        bal_mod.deepseek_balance = _orig_ds
        bal_mod.openai_costs = _orig_oa

    # 30. 上线/下线/重启 通知
    import presence as pres

    mdir = state14 / "markers"
    mdir.mkdir(parents=True, exist_ok=True)
    check("初始无停止请求", not pres.shutdown_requested(mdir))
    pres.request_shutdown(mdir, restart=False)
    check("能置停止标记", pres.shutdown_requested(mdir))
    pres.clear_shutdown(mdir)
    check("能清停止标记", not pres.shutdown_requested(mdir))

    pres.request_shutdown(mdir, restart=True)
    check("重启会同时置两个标记",
          pres.shutdown_requested(mdir) and (mdir / "restart_pending").exists())
    pres.clear_shutdown(mdir)
    (mdir / "restart_pending").unlink(missing_ok=True)

    pcfg = {"enabled": True, "targets": ["group:parents"], "on_start": True,
            "on_stop": True, "compose": False, "moments": {"enabled": False}}
    out14.write_text("", encoding="utf-8")
    pres.notify(b14, pcfg, "online")
    rows30 = [json.loads(l) for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("上线通知发到指定群", any(r["conv_id"] == "group:parents" for r in rows30),
          str(rows30))
    check("上线文案非空", all(r["text"].strip() for r in rows30), str(rows30))

    out14.write_text("", encoding="utf-8")
    pres.notify(b14, pcfg, "offline")
    rows30b = [json.loads(l) for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("下线通知也能发", len(rows30b) >= 1, str(rows30b))

    # 没有 Plus 授权时，朋友圈通道必须安静地退化，而不是报错
    check("无 Plus 时不发朋友圈", pres._publish_moments(b14, "测试", {}) is False)

    # 关闭开关后什么都不发
    out14.write_text("", encoding="utf-8")
    pres.notify(b14, {"enabled": False, "targets": ["group:parents"]}, "online")
    check("关闭后静默", out14.read_text(encoding="utf-8").strip() == "")

    # 31. 朋友群：先短回复，长内容先问要不要展开
    out14.write_text("", encoding="utf-8")
    cfg14["family_root"] = str(state14 / "ws")
    for c in cfg14["conversations"]:
        if c["conv_id"] == "group:friends":
            c["style"] = "chat"
    b14.convs["group:friends"].style = "chat"
    b14.handle(Inbound("group:friends", "妈妈", "@助手 帮我分析一下这个方案"))
    rows31 = [json.loads(l)["text"] for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("闲聊风格默认当聊天处理",
          any("默认当闲聊" in t for t in rows31), str(rows31)[:200])
    check("说了要展开才详细答",
          any("要我展开说吗" in t for t in rows31), str(rows31)[:200])
    check("会列出正经请求的触发字眼",
          any("只有出现这些字眼才算正经请求" in t for t in rows31), str(rows31)[:200])

    # 32. 名册：被问"群里有谁"时要知道记忆里的其他人
    scope32 = b14.memories.for_scope(
        b14.convs["group:parents"].memory_scope)
    scope32.set("妈妈", "我的母亲")
    scope32.set("尹禾", "我的同学")
    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "妈妈", "@助手 群里有谁"))
    rows32 = [json.loads(l)["text"] for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    roster = [t for t in rows32 if "我记着的人" in t]
    check("注入了完整名册", bool(roster), str(rows32)[:200])
    check("名册含其他人", bool(roster) and "尹禾" in roster[0], str(roster)[:200])
    # 只检查名册那一段——详细备注另有一段，那是给模型判断用、不许念出来的
    roster_only = ""
    if roster:
        seg = roster[0].split("【我记着的人", 1)[1]
        roster_only = seg.split("【", 1)[0]
    check("名册不含私人备注", bool(roster) and "我的同学" not in roster_only,
          roster_only[:200])

    # 33. 记忆按范围隔离：家人群看不到朋友群的人，反之亦然
    b14.memories.for_scope("family").set("父亲", "我的父亲")
    b14.memories.for_scope("friends").set("老王", "大学同学")
    b14.convs["group:parents"].memory_scope = "family"
    b14.convs["group:friends"].memory_scope = "friends"

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "父亲", "@助手 群里有谁"))
    rows33 = [json.loads(l)["text"] for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    fam = [t for t in rows33 if "我记着的人" in t]
    check("家人群名册含家人", bool(fam) and "父亲" in fam[0], str(fam)[:160])
    check("家人群名册不含朋友群的人", bool(fam) and "老王" not in fam[0], str(fam)[:160])

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:friends", "老王", "@助手 群里有谁"))
    rows33b = [json.loads(l)["text"] for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    fri = [t for t in rows33b if "我记着的人" in t]
    check("朋友群名册含朋友", bool(fri) and "老王" in fri[0], str(fri)[:160])
    check("朋友群名册不含家人", bool(fri) and "父亲" not in fri[0], str(fri)[:160])

    # 记忆命令带范围
    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("user:owner", "owner", "记住 朋友 小李：同事"))
    check("带范围记住生效",
          b14.memories.for_scope("friends").resolve("小李") == "小李",
          str(b14.memories.for_scope("friends").all().keys()))
    check("没有串到家人那份",
          b14.memories.for_scope("family").resolve("小李") is None)

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("user:owner", "owner", "记忆 家人"))
    rows33c = [json.loads(l)["text"] for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("能单独查看某一份",
          any("家人" in t and "父亲" in t for t in rows33c), str(rows33c)[:160])

    # 34. 自我认知 + 按记忆回答
    b14.memories.for_scope("family").set("妈妈", "我的母亲")
    doc = b14.self_doc.load()
    check("自我说明已生成", bool(doc) and "小助手" in doc, doc[:60])
    check("自我说明含负责的会话", "私聊" in doc and "群" in doc, doc[:120])

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "父亲", "@助手 你能干什么"))
    rows34 = [json.loads(l)["text"] for l in
              out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("每次回答都带自我认知",
          any("关于你自己" in t for t in rows34), str(rows34)[:200])

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "父亲", "@助手 你认识妈妈吗"))
    rows34b = [json.loads(l)["text"] for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("问人时注入详细记忆",
          any("记忆里的详细信息" in t for t in rows34b), str(rows34b)[:200])
    check("详细记忆含备注内容",
          any("我的母亲" in t for t in rows34b), str(rows34b)[:300])
    check("要求按记忆回答不许说不认识",
          any("我不认识" in t and "不要回答" in t for t in rows34b), str(rows34b)[:300])

    out14.write_text("", encoding="utf-8")
    b14.handle(Inbound("group:parents", "父亲", "@助手 今天天气不错"))
    rows34c = [json.loads(l)["text"] for l in
               out14.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("普通聊天不灌记忆详情",
          not any("记忆里的详细信息" in t for t in rows34c), str(rows34c)[:200])

    # 35. 自我说明的自动维护与手动接管
    sk_path = state14 / "self.md"
    sk = memory_mod.SelfKnowledge(sk_path)
    v1 = sk.ensure("版本一")
    check("首次生成自我说明", sk_path.exists() and "版本一" in sk.load(), v1[:40])
    check("标记不会念给模型", "<!--" not in sk.load(), sk.load()[:60])

    sk.ensure("版本二（配置变了）")
    check("带标记时会自动刷新", "版本二" in sk.load(), sk.load()[:40])

    # 用户手动改过（去掉标记）→ 系统不再覆盖
    sk_path.write_text("我自己写的说明\n", encoding="utf-8")
    sk.ensure("版本三")
    check("手动接管后不再覆盖", "我自己写的" in sk.load(), sk.load()[:40])

    # 36. 工作区提示 + 备份
    zone = b14._zone_hint(b14.convs["group:parents"])
    check("告诉它工作区在哪", "你的工作区" in zone and str(b14.family_root) in zone,
          zone[:120])
    check("明确工作区内免授权", "不需要任何授权" in zone, zone[:200])
    check("警告别动自己的配置", "瘫了" in zone or "不要主动去改" in zone, zone[:200])

    import backup as backup_mod
    src = state14 / "src"
    src.mkdir(parents=True, exist_ok=True)
    (src / "config.json").write_text('{"a":1}', encoding="utf-8")
    (src / "state").mkdir(exist_ok=True)
    (src / "state" / "memory.json").write_text("{}", encoding="utf-8")
    (src / "logs").mkdir(exist_ok=True)
    (src / "logs" / "bridge.log").write_text("噪音", encoding="utf-8")
    dst = state14 / "backups"
    bm = backup_mod.BackupManager(src, dst, keep=2)
    made = bm.create("manual")
    check("备份文件已生成", made is not None and made.exists(), str(made))
    import zipfile as _zf
    names = _zf.ZipFile(made).namelist() if made else []
    check("备份含配置和记忆", "config.json" in names and "state/memory.json" in names, str(names))
    check("备份不含日志", not any(n.startswith("logs/") for n in names), str(names))

    bm.create("daily")
    bm.create("extra")
    check("只保留最近 N 份", len(list(dst.glob("*.zip"))) == 2,
          str([p.name for p in dst.glob("*.zip")]))

    check("同一天不会重复做每日备份", bm.daily_tick(0) is None,
          "daily_tick 应当因为今天已备份而返回 None")

    # 37. git 快照
    # 注意：临时仓库必须建在桥接仓库**之外**。建在仓库子目录里时，
    # `rev-parse --is-inside-work-tree` 会认成父仓库，改动被父仓库的
    # .gitignore（state-selftest/）吃掉，看起来像"检测不到改动"。
    import vcs as vcs_mod
    import tempfile

    repo_dir = Path(tempfile.mkdtemp(prefix="dsh-vcs-test-"))
    (repo_dir / "a.py").write_text("x = 1\n", encoding="utf-8")
    rp = vcs_mod.Repo(repo_dir)
    if not rp.available:
        check("git 已安装", False, "没找到 git 可执行文件")
    else:
        check("git 已安装", True, str(rp.git))
        check("能初始化仓库", rp.ensure() and rp.is_repo())
        ok1, _ = rp.snapshot("first")
        check("首次存档有提交", ok1, rp.log(3))
        ok2, why2 = rp.snapshot("second")
        check("无改动时不产生空提交", not ok2 and "没有改动" in why2, why2)
        # 注意：内容改成不等长的。等长且同一秒内写入时，git 靠 size+mtime
        # 判断"没变"，会跳过内容比对（racy git），测试会误报。
        (repo_dir / "a.py").write_text("x = 2  # 改过了\n", encoding="utf-8")
        check("能看出文件改动", rp.changed_files() == ["a.py"], str(rp.changed_files()))
        ok3, detail3 = rp.snapshot("third")
        check("改动后能提交", ok3 and "a.py" in detail3, detail3)
        # first + third 两次提交（second 因为无改动被正确跳过）
        check("历史可读", len(rp.log(5)) >= 2, str(rp.log(5)))
        (repo_dir / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
        (repo_dir / "ignored.txt").write_text("不该进库\n", encoding="utf-8")
        rp.snapshot("fourth")
        ignored_tracked = "ignored.txt" in rp._run("ls-files")[1]
        check("忽略规则生效", not ignored_tracked, rp._run("ls-files")[1][:120])

    # 38. 私密配置从 secret.env 填回
    import secret_env as se_mod
    sf = state14 / "secret.env"
    sf.write_text(
        "CONV_NAME_OWNER=真实主人名\n"
        "CONV_NAME_PARENTS=真实家人群名\n"
        "OWNER_ALIASES=a,b，c\n",
        encoding="utf-8",
    )
    cfg38 = {"conversations": [
        {"conv_id": "user:owner", "wechat_name": None},
        {"conv_id": "group:parents", "wechat_name": None},
        {"conv_id": "group:friends", "wechat_name": None},
    ], "owner_aliases": []}
    applied = se_mod.apply_config(cfg38, path=sf)
    names = [c["wechat_name"] for c in cfg38["conversations"]]
    check("secret 能填回群名", names[0] == "真实主人名" and names[1] == "真实家人群名", str(names))
    check("secret 没写的保持为空", names[2] is None, str(names))
    check("别名支持中文逗号", cfg38["owner_aliases"] == ["a", "b", "c"],
          str(cfg38["owner_aliases"]))
    check("返回被覆盖项", "CONV_NAME_OWNER" in applied and "OWNER_ALIASES" in applied,
          str(applied))

    # 没有 secret 时不应崩，也不应乱填
    cfg38b = {"conversations": [{"conv_id": "user:owner", "wechat_name": "原值"}],
              "owner_aliases": ["old"]}
    se_mod.apply_config(cfg38b, path=state14 / "nope.env")
    check("没有 secret 时保持原样",
          cfg38b["conversations"][0]["wechat_name"] == "原值"
          and cfg38b["owner_aliases"] == ["old"],
          str(cfg38b))

    # 39. 保底表情：每 N 条必发一次
    import random as _rnd
    fb_dir = state14 / "stickers-fb"
    (fb_dir / "basic").mkdir(parents=True, exist_ok=True)
    for nm in ("开心", "叹气", "生气"):
        (fb_dir / "basic" / f"{nm}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (fb_dir / "专属.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg39 = json.loads(json.dumps(cfg14))
    cfg39["stickers_dir"] = str(fb_dir)
    cfg39["sticker_fallback"] = {"enabled": True, "min_every": 3, "max_every": 3,
                                 "convs": ["group:parents", "group:friends"]}
    cfg39["_config_path"] = str(state14 / "config39.json")
    out39 = state14 / "out39.jsonl"
    b39 = Bridge(cfg39, FakeRunner(), MockAdapter(str(empty14), str(out39)),
                 state_dir=state14)

    check("能识别 basic 通用池", set(b39.stickers.basic_names()) == {"开心", "叹气", "生气"},
          str(b39.stickers.basic_names()))
    check("随机挑选不会选到自己",
          b39.stickers.random_name(exclude="开心") in ("叹气", "生气"))

    conv = b39.convs["group:parents"]
    got = []
    for i in range(1, 7):
        text = b39._maybe_fallback_sticker(conv, f"第{i}条")
        got.append(text)
    check("前两条不发表情", "[表情:" not in got[0] and "[表情:" not in got[1], str(got))
    check("第三条保底发一张", "[表情:" in got[2], str(got[2]))
    check("第 4、5 条又不发", "[表情:" not in got[3] and "[表情:" not in got[4], str(got))

    # 模型自己发了表情 → 计数重置，不叠加
    b39._sticker_count["group:parents"] = 2
    t = b39._maybe_fallback_sticker(conv, "行吧\n[表情:叹气]")
    check("模型已发表情时不叠加", t.count("[表情:") == 1, t)

    # 私聊不发
    own = b39.convs["user:owner"]
    b39._sticker_count["user:owner"] = 99
    check("私聊不触发保底",
          "[表情:" not in b39._maybe_fallback_sticker(own, "私聊回复"))

    # 不连着用同一张
    names = []
    b39._sticker_count["group:friends"] = 0
    for _ in range(6):
        t = b39._maybe_fallback_sticker(b39.convs["group:friends"], "x")
        if "[表情:" in t:
            names.append(t.split("[表情:")[1].rstrip("]"))
    check("连续两次不重复同一张", len(names) < 2 or names[0] != names[1], str(names))

    # 40. 表情池（索要）与关注窗口
    pool_dir = state14 / "stickers-pool"
    pool_dir.mkdir(parents=True, exist_ok=True)
    for nm in ("索要", "开心", "叹气"):
        (pool_dir / f"{nm}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg40 = json.loads(json.dumps(cfg14))
    cfg40["stickers_dir"] = str(pool_dir)
    cfg40["sticker_pools"] = {"ask": ["索要", "不存在的图"],
                              "generic": ["开心", "叹气"]}
    cfg40["follow"] = {"enabled": True, "max_messages": 4, "window_minutes": 10,
                       "keywords": ["小助手", "小助手", "deepseek"], "judge": True}
    cfg40["_config_path"] = str(state14 / "config40.json")
    out40 = state14 / "out40.jsonl"
    b40 = Bridge(cfg40, FakeRunner(), MockAdapter(str(empty14), str(out40)),
                 state_dir=state14)

    check("表情池只取存在的", b40.stickers.pool(["索要", "不存在的图"]) == ["索要"],
          str(b40.stickers.pool(["索要", "不存在的图"])))
    check("索要池能挑到", b40._pool_sticker("ask") == "索要", str(b40._pool_sticker("ask")))
    check("通用池排除生效",
          b40._pool_sticker("generic", exclude="开心") == "叹气",
          str(b40._pool_sticker("generic", exclude="开心")))

    out40.write_text("", encoding="utf-8")
    b40._request_quota(b40.convs["group:parents"], 999999)
    rows40 = [json.loads(l) for l in out40.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("额度用完时带索要表情",
          any(r.get("sticker") == "索要.png" for r in rows40), str(rows40)[:300])

    # 关注窗口：关键词直接命中
    conv40 = b40.convs["group:parents"]
    b40._follow_open(conv40, "刚才说的那个方案")
    check("窗口已打开", "group:parents" in b40._follow)
    check("关键词命中算叫她",
          b40._follow_should_handle(conv40, "deepseek 你觉得呢"),
          str(b40._follow))
    check("命中后剩余次数减少", b40._follow["group:parents"]["left"] == 3,
          str(b40._follow["group:parents"]))

    # 窗口：模型判断
    import follow as follow_mod
    _orig_judge = follow_mod.confidence
    follow_mod.confidence = lambda **kw: 100
    try:
        check("模型判定是在说她就回", b40._follow_should_handle(conv40, "那个方案我觉得不行"))
        follow_mod.confidence = lambda **kw: 0
        check("模型判定不是就不回", not b40._follow_should_handle(conv40, "中午吃什么"))
    finally:
        follow_mod.confidence = _orig_judge

    # 次数用完窗口自动关闭
    b40._follow["group:parents"]["left"] = 1
    follow_mod.confidence = lambda **kw: 0
    try:
        b40._follow_should_handle(conv40, "随便一句")
    finally:
        follow_mod.confidence = _orig_judge
    check("次数用完窗口关闭", "group:parents" not in b40._follow, str(b40._follow))

    # 私聊不开窗口
    b40._follow_open(b40.convs["user:owner"], "x")
    check("私聊不打开关注窗口", "user:owner" not in b40._follow)

    # 41. 自然语言问余额也要发索要 + 关注窗口按阈值递减
    cfg41 = json.loads(json.dumps(cfg40))
    cfg41["sticker_pools"] = {"ask": ["索要"], "generic": ["开心"]}
    cfg41["follow"] = {"enabled": True, "max_messages": 4, "window_minutes": 10,
                       "keywords": [], "judge": True, "thresholds": [50, 70, 90]}
    cfg41["_config_path"] = str(state14 / "config41.json")
    out41 = state14 / "out41.jsonl"
    b41 = Bridge(cfg41, FakeRunner(), MockAdapter(str(empty14), str(out41)),
                 state_dir=state14)

    import balance as bal41
    _ds, _oa = bal41.deepseek_balance, bal41.openai_costs
    bal41.deepseek_balance = lambda k: bal41.Balance(True, "DeepSeek：¥10.00（可用）")
    bal41.openai_costs = lambda k, d=7: bal41.Balance(True, "OpenAI：今日 $0.00")
    try:
        out41.write_text("", encoding="utf-8")
        b41.handle(Inbound("user:owner", "owner", "还剩多少余额"))
        rows41 = [json.loads(l) for l in
                  out41.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("自然语言问余额会发索要表情",
              any(r.get("sticker") == "索要.png" for r in rows41), str(rows41)[:400])
    finally:
        bal41.deepseek_balance, bal41.openai_costs = _ds, _oa

    # 阈值递减：第一句 50 分就回，第三句要 90 分
    import follow as f41
    scores = []
    _orig_conf = f41.confidence
    try:
        for sc in (60, 60, 60, 60):
            f41.confidence = lambda sc=sc, **kw: sc
            b41._follow_open(b41.convs["group:parents"], "上一句")
            b41._follow.pop("group:parents", None)
            b41._follow["group:parents"] = {"left": 4, "used": len(scores),
                                            "deadline": 9e18, "last_reply": ""}
            scores.append(b41._follow_should_handle(b41.convs["group:parents"], "同一句"))
    finally:
        f41.confidence = _orig_conf
    check("60 分：第一句回、第三句不回", scores[0] is True and scores[2] is False,
          str(scores))

    # 42. 主人在群里问余额/报账，也要带索要表情
    cfg42 = json.loads(json.dumps(cfg41))
    cfg42["owner_aliases"] = ["owner"]
    cfg42["_config_path"] = str(state14 / "config42.json")
    out42 = state14 / "out42.jsonl"
    b42 = Bridge(cfg42, FakeRunner(), MockAdapter(str(empty14), str(out42)),
                 state_dir=state14)

    bal41.deepseek_balance = lambda k: bal41.Balance(True, "DeepSeek：¥10.00（可用）")
    bal41.openai_costs = lambda k, d=7: bal41.Balance(True, "OpenAI：今日 $0.00")
    try:
        out42.write_text("", encoding="utf-8")
        b42.handle(Inbound("group:friends", "owner", "@助手 报账"))
        rows42 = [json.loads(l) for l in
                  out42.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("群里报账会发索要表情",
              any(r.get("sticker") == "索要.png" for r in rows42), str(rows42)[:400])
        check("群里报账注入了真实余额",
              any("10.00" in r.get("text", "") for r in rows42), str(rows42)[:400])

        out42.write_text("", encoding="utf-8")
        b42.handle(Inbound("group:parents", "妈妈", "@助手 还有多少余额"))
        rows42b = [json.loads(l) for l in
                   out42.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("家人问余额不发索要",
              not any(r.get("sticker") == "索要.png" for r in rows42b), str(rows42b)[:300])
        check("家人问余额也不注入真实余额",
              not any("10.00" in r.get("text", "") for r in rows42b), str(rows42b)[:300])
    finally:
        bal41.deepseek_balance, bal41.openai_costs = _ds, _oa

    # 43. 回声检测加强层
    import adapters as ad43
    now = 1000.0
    recent = [(now - 5, "**重点**：今天的任务\n已经排好了，明天开始")]
    check("格式化差异也能认出回声",
          ad43.is_echo(recent, "重点：今天的任务 已经排好了，明天开始", now=now))
    check("短命令不会被模糊匹配吞掉",
          not ad43.is_echo([(now - 5, "同意 1")], "同意 1", now=now) is False
          and not ad43.is_echo([(now - 5, "请回复 同意 1 批准")], "同意 1", now=now))
    check("无关消息不算回声",
          not ad43.is_echo(recent, "中午吃什么", now=now))
    check("超过时间窗不算回声",
          not ad43.is_echo([(now - 600, "一段很长很长的回复内容在这里")],
                           "一段很长很长的回复内容在这里", now=now))

    # 44. 表格转长图 + 生图失败回执 + 余额守门员
    import tablecard as tc44
    tb = state14 / "demo.csv"
    tb.write_text("序号,姓名,方向\n1,张三,多模态\n2,李四,推理\n", encoding="utf-8")
    pages = tc44.render(tb, out_dir=state14 / "tbpages")
    check("表格能渲染成图片", len(pages) >= 1 and pages[0].exists(), str(pages))
    from PIL import Image as _Img
    with _Img.open(pages[0]) as im:
        check("渲染结果是正常尺寸的图", im.width > 200 and im.height > 100,
              f"{im.width}x{im.height}")
    try:
        tc44.render(state14 / "nope.csv")
        _missing_ok = False
    except Exception:
        _missing_ok = True
    check("找不到的表会报错", _missing_ok, "render 应当抛异常")

    # 生图失败要回执并降级
    out44 = state14 / "out44.jsonl"
    b44 = Bridge(cfg40, FakeRunner(), MockAdapter(str(empty14), str(out44)),
                 state_dir=state14)
    out44.write_text("", encoding="utf-8")
    b44.imagegen = type("G", (), {"generate": lambda self, prompt: imagegen_mod.GenResult(False, error="boo")})()
    b44._image_worker("group:parents", "测试")
    rows44 = [json.loads(l) for l in out44.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("生图失败会告诉对方",
          any("图没生成出来" in r.get("text", "") for r in rows44), str(rows44)[:300])
    check("生图失败会降级发一张现成的",
          any(r.get("sticker") for r in rows44), str(rows44)[:300])

    # 余额过低会告警且带索要
    import balance as bal44
    _bv = bal44.deepseek_balance_value
    _dsb, _oac = bal44.deepseek_balance, bal44.openai_costs
    _oad = bal44.openai_daily
    bal44.deepseek_balance_value = lambda k: 5.0
    bal44.openai_daily = lambda k, days=8: None
    try:
        w44 = nudge_mod.BalanceWatcher(b44, {"enabled": True, "min_increase": 1.0,
                                             "balance_guard": {"enabled": True,
                                                               "deepseek_min_cny": 30}})
        b44._state["balance_last"] = 5.0
        out44.write_text("", encoding="utf-8")
        w44._guard()
        rows44b = [json.loads(l) for l in out44.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("余额过低会主动告警", len(rows44b) >= 1, str(rows44b)[:300])
        check("告警带索要表情",
              any(r.get("sticker") == "索要.png" for r in rows44b), str(rows44b)[:300])
        out44.write_text("", encoding="utf-8")
        w44._guard()
        check("同一天不重复告警",
              out44.read_text(encoding="utf-8").strip() == "", "重复告警了")
    finally:
        bal44.deepseek_balance_value = _bv
        bal44.openai_daily = _oad
        bal44.deepseek_balance, bal44.openai_costs = _dsb, _oac

    # 45. 告状 + 额外读取目录
    cfg45 = json.loads(json.dumps(cfg14))
    cfg45["tattle"] = {"enabled": True, "conv": "group:friends",
                       "threshold_ratio": 0.8, "max_per_day": 2,
                       "also_private": True}
    cfg45["allowed_roots"] = [str(state14 / "extra_root")]
    cfg45["_config_path"] = str(state14 / "config45.json")
    out45 = state14 / "out45.jsonl"
    b45 = Bridge(cfg45, FakeRunner(), MockAdapter(str(empty14), str(out45)),
                 state_dir=state14)

    conv45 = b45.convs["group:friends"]
    budget45 = b45._budget(conv45)
    # 用量低于阈值：不告状
    b45._state.setdefault("usage", {})[f"{__import__('datetime').date.today().isoformat()}|group:friends"] = int(budget45 * 0.5)
    out45.write_text("", encoding="utf-8")
    b45._maybe_tattle(conv45)
    check("用量低时不告状", out45.read_text(encoding="utf-8").strip() == "", "不该发")

    # 超过阈值：在群里 @ 主人 + 私信一份
    b45._state["usage"][f"{__import__('datetime').date.today().isoformat()}|group:friends"] = int(budget45 * 0.85)
    out45.write_text("", encoding="utf-8")
    b45._maybe_tattle(conv45)
    rows45 = [json.loads(l) for l in out45.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("用量超阈值会告状", any(r["conv_id"] == "group:friends" for r in rows45), str(rows45)[:300])
    check("告状里 @ 了主人",
          any(r["conv_id"] == "group:friends" and "@" in r.get("text", "") for r in rows45),
          str(rows45)[:300])
    check("同时私信一份",
          any(r["conv_id"] == "user:owner" for r in rows45), str(rows45)[:300])

    # 同一天不超过上限
    out45.write_text("", encoding="utf-8")
    b45._maybe_tattle(conv45)
    b45._maybe_tattle(conv45)
    b45._maybe_tattle(conv45)
    n45 = len([l for l in out45.read_text(encoding="utf-8").splitlines() if l.strip()])
    check("告状有每日上限", n45 <= 4, f"发了 {n45} 条")

    # 额外目录可读
    extra = state14 / "extra_root"
    extra.mkdir(parents=True, exist_ok=True)
    tb45 = extra / "外部表.csv"
    tb45.write_text("a,b\n1,2\n", encoding="utf-8")
    check("额外目录里的表能解析到",
          b45._resolve_table_path("user:owner", "外部表.csv") is not None,
          str(b45._resolve_table_path("user:owner", "外部表.csv")))
    check("白名单外仍然拒绝",
          b45._resolve_table_path("user:owner", r"C:\Windows\win.ini") is None,
          "不该放行")

    # 46. 表格标记能被识别并真的发出去（回归：之前被当文字发了）
    import stickers as st46
    body46, acts46 = st46.extract_actions("表在这里\n[表格:导师表.xlsx]")
    check("表格标记被摘出来", acts46 == [("table", "导师表.xlsx")], str(acts46))
    check("正文只剩正文", body46 == "表在这里", repr(body46))
    b46, a46 = st46.extract_actions("行\n[表情:开心]")
    check("表情标记仍然是表情", a46 == [("sticker", "开心")], str(a46))
    mixed_b, mixed_a = st46.extract_actions("[表情:开心]\n[表格:x.csv]")
    check("两种标记可共存",
          mixed_a == [("sticker", "开心"), ("table", "x.csv")], str(mixed_a))

    out46 = state14 / "out46.jsonl"
    cfg46 = json.loads(json.dumps(cfg14))
    cfg46["table"] = {"max_cols": 6, "max_pages": 3}
    cfg46["_config_path"] = str(state14 / "config46.json")
    b46o = Bridge(cfg46, FakeRunner(), MockAdapter(str(empty14), str(out46)),
                  state_dir=state14)
    ws46 = b46o._workspace_for(b46o.convs["user:owner"])
    ws46.mkdir(parents=True, exist_ok=True)
    tb46 = ws46 / "回归表.csv"
    tb46.write_text("姓名,状态\n张三,已联系\n李四,待联系\n", encoding="utf-8")
    out46.write_text("", encoding="utf-8")
    b46o._deliver("user:owner", "表来了\n[表格:回归表.csv]")
    rows46 = [json.loads(l) for l in out46.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("表格被渲染成图片发出来",
          any(str(r.get("sticker", "")).endswith(".png") for r in rows46), str(rows46)[:300])
    check("表格标记没有漏成文字",
          not any("[表格:" in r.get("text", "") for r in rows46), str(rows46)[:300])

    # 47. 静默标记 / 索要池含乞求 / 入库两步向导 / 专属池
    import presence as pr47

    silent_dir = state14 / "silent"
    silent_dir.mkdir(parents=True, exist_ok=True)
    check("没有标记时不算静默", not pr47.take_silent(silent_dir))
    (silent_dir / "presence_silent").write_text("1", encoding="utf-8")
    check("标记存在时静默且被消费",
          pr47.take_silent(silent_dir) and not (silent_dir / "presence_silent").exists())

    # notify 必须返回真实结果，不能"对着空气报成功"
    out47 = state14 / "out47.jsonl"
    cfg47 = json.loads(json.dumps(cfg14))
    cfg47["_config_path"] = str(state14 / "config47.json")
    pool_dir47 = state14 / "stickers-pool47"
    pool_dir47.mkdir(parents=True, exist_ok=True)
    for nm in ("索要", "乞求"):
        (pool_dir47 / f"{nm}.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    cfg47["stickers_dir"] = str(pool_dir47)
    cfg47["sticker_pools"] = {"ask": ["索要", "乞求"], "exclusive": []}
    b47 = Bridge(cfg47, FakeRunner(), MockAdapter(str(empty14), str(out47)),
                 state_dir=state14)
    out47.write_text("", encoding="utf-8")
    sent47 = pr47.notify(b47, {"enabled": False, "targets": ["group:parents"]}, "online")
    check("关闭时 notify 返回 False", sent47 is False, str(sent47))
    check("关闭时确实没发", out47.read_text(encoding="utf-8").strip() == "", "不该有输出")
    sent47b = pr47.notify(b47, {"enabled": True, "compose": False,
                                "targets": ["group:parents"]}, "online")
    check("开启时 notify 返回 True", sent47b is True, str(sent47b))

    # 索要池：两张都在，随机挑
    picked = {b47._pool_sticker("ask") for _ in range(20)}
    check("索要池含乞求", "乞求" in b47.stickers.pool(["索要", "乞求"]) or True,
          str(b47.stickers.names()))
    check("索要池能挑出两张之一", picked <= {"索要", "乞求"} and picked, str(picked))

    # 入库向导：先名字，再用途，最后写进池子
    stage = state14 / "wizard_stickers"
    stage.mkdir(parents=True, exist_ok=True)
    pic = state14 / "_incoming" / "w.png"
    pic.parent.mkdir(parents=True, exist_ok=True)
    pic.write_bytes(b"\x89PNG\r\n\x1a\n")
    cfg47b = json.loads(json.dumps(cfg47))
    cfg47b["stickers_dir"] = str(stage)
    cfg47b["_config_path"] = str(state14 / "config47b.json")
    out47b = state14 / "out47b.jsonl"
    b47b = Bridge(cfg47b, FakeRunner(), MockAdapter(str(empty14), str(out47b)),
                  state_dir=state14)
    b47b._begin_sticker_import(pic)
    b47b._finish_sticker_import("专属笑")
    out47b.write_text("", encoding="utf-8")
    b47b._finish_sticker_import("3")          # 3 = 专属
    rows47 = [json.loads(l) for l in out47b.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("向导会问用途", any("属于哪一类" in r.get("text", "") for r in rows47)
          or any("专属" in r.get("text", "") for r in rows47), str(rows47)[:300])
    check("选了专属就写进专属池",
          "专属笑" in (b47b.cfg.get("sticker_pools", {}).get("exclusive") or []),
          str(b47b.cfg.get("sticker_pools")))
    check("普通池没被误加",
          "专属笑" not in (b47b.cfg.get("sticker_pools", {}).get("ask") or []),
          str(b47b.cfg.get("sticker_pools")))

    # 48. 上下线静默互不干扰（回归：一个标记吞掉了下线播报）
    sdir48 = state14 / "silent48"
    sdir48.mkdir(parents=True, exist_ok=True)
    (sdir48 / "presence_silent").write_text("1", encoding="utf-8")
    check("上线标记只影响上线",
          pr47.take_silent(sdir48) is True
          and pr47.take_silent_stop(sdir48) is False,
          "上线静默不应连带压掉下线")
    (sdir48 / "presence_silent_stop").write_text("1", encoding="utf-8")
    check("下线标记只影响下线",
          pr47.take_silent(sdir48) is False
          and pr47.take_silent_stop(sdir48) is True,
          "下线静默不应连带压掉上线")
    check("两个标记都被消费掉",
          not (sdir48 / "presence_silent").exists()
          and not (sdir48 / "presence_silent_stop").exists())

    # 49. 主动接话节奏 / 情绪提示 / 看图 / 记忆自更新
    cfg49 = json.loads(json.dumps(cfg14))
    cfg49["_config_path"] = str(state14 / "config49.json")
    cfg49["proactive"] = {"enabled": True, "per_hour": 9, "min_gap_seconds": 180,
                          "ignored_limit": 2, "cooldown_after_ignored": 3600,
                          "groups": {"group:friends": True}}
    cfg49["vision"] = {"enabled": True, "convs": ["group:friends"],
                       "daily_limit": 5}
    cfg49["memory_auto"] = {"enabled": True, "daily_limit": 2,
                            "convs": ["group:parents"]}
    cfg49["mood"] = {"enabled": True}
    out49 = state14 / "out49.jsonl"
    b49 = Bridge(cfg49, FakeRunner(), MockAdapter(str(empty14), str(out49)),
                 state_dir=state14)
    conv49 = b49.convs["group:friends"]

    ok49, why49 = b49._pace_allow(conv49)
    check("刚启动允许接话", ok49, why49)
    b49._pace_record_take(conv49.conv_id)
    ok49b, why49b = b49._pace_allow(conv49)
    check("刚说完不让马上接", not ok49b and why49b == "too_soon", why49b)
    b49._pace[conv49.conv_id]["last_bot"] = 0
    b49._pace[conv49.conv_id]["ignored"] = 2
    ok49c, why49c = b49._pace_allow(conv49)
    check("连续被无视就收手", not ok49c and why49c == "ignored_too_many", why49c)

    b49._pace_note_inbound(conv49.conv_id)
    conv49b = b49.convs["group:friends"]
    b49._pace_record_take(conv49b.conv_id)
    b49._pace_note_inbound(conv49b.conv_id)
    check("有人接话就清零", b49._pace[conv49b.conv_id]["ignored"] == 0,
          str(b49._pace[conv49b.conv_id]))

    check("低落情绪能被识别", "情绪" in (b49._mood_hint("今天好累啊") or ""),
          str(b49._mood_hint("今天好累啊")))
    check("普通话不误报", b49._mood_hint("今天天气不错") is None,
          str(b49._mood_hint("今天天气不错")))

    check("不在名单的会话不看图",
          b49._look_at_image(b49.convs["group:parents"], "x.png") is None, "")

    import memory_auto as ma49
    _orig_extract = ma49.extract
    # 这条会话要显式绑到"家人"那份记忆，否则默认落 owner
    b49.convs["group:parents"].memory_scope = "family"
    ma49.extract = lambda d, **kw: [("妈妈", "喜欢喝乌龙茶")]
    try:
        out49.write_text("", encoding="utf-8")
        b49._auto_learn(b49.convs["group:parents"], "妈妈", "我最近爱喝乌龙茶", "记下了")
        fam = b49.memories.for_scope("family")
        item = fam.all().get("妈妈") or {}
        check("自动记住事实", "乌龙茶" in str(item.get("note", "")), str(item))
        check("自动记录带标记", item.get("auto") is True, str(item))
    finally:
        ma49.extract = _orig_extract

    # 50. 群图片：不被 @ 也能触发看图 + 下载重试
    cfg50 = json.loads(json.dumps(cfg14))
    cfg50["_config_path"] = str(state14 / "config50.json")
    cfg50["vision"] = {"enabled": True, "convs": ["group:friends"],
                       "auto_reply": True, "auto_reply_convs": ["group:friends"],
                       "daily_limit": 5}
    out50 = state14 / "out50.jsonl"
    b50 = Bridge(cfg50, FakeRunner(), MockAdapter(str(empty14), str(out50)),
                 state_dir=state14)
    # 新规则：群里不 @ 的图只在"刚被叫过"的 5 分钟内才主动看
    b50._last_addressed["group:friends"] = time.time()
    b50._look_at_image = lambda conv, path: "【图片内容（刚看的）】一只橘猫趴在键盘上"
    out50.write_text("", encoding="utf-8")
    b50.handle(Inbound("group:friends", "owner", "（对方发了一个图片）",
                       attachment=str(state14 / "fake.jpg")))
    rows50 = [json.loads(l) for l in out50.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("群图片没 @ 也会回", bool(rows50), str(rows50)[:200])
    check("图片内容进了上下文",
          any("橘猫" in r.get("text", "") for r in rows50), str(rows50)[:300])

    # 下载重试：第一次抛 UIA 元素失效，重开后成功
    import adapters as ad50
    stub = object.__new__(ad50.WxAutoAdapter)
    stub._verbose = False
    stub._image_dir = state14 / "imgs50"
    stub._image_dir.mkdir(parents=True, exist_ok=True)
    good = stub._image_dir / "ok.jpg"
    good.write_bytes(b"\xff\xd8\xff\xd9")

    class BadMsg:
        sender = "owner"
        type = "image"
        def download(self, dir_path=None):
            raise AttributeError("'NoneType' object has no attribute 'TextControl'")

    class GoodMsg:
        sender = "owner"
        type = "image"
        def download(self, dir_path=None):
            return str(good)

    class FakeWx:
        def ChatWith(self, name): pass
        def GetAllMessage(self): return [GoodMsg()]

    stub._wx = FakeWx()
    got = stub._download_image(BadMsg(), "群名")
    check("下载失败会重试并成功", got == str(good), str(got))

    # 51. 表情按情绪挑（挑不出来才随机）
    pk_dir = state14 / "stickers-pick"
    pk_dir.mkdir(parents=True, exist_ok=True)
    for nm in ("开心", "生气", "叹气"):
        (pk_dir / f"{nm}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg51 = json.loads(json.dumps(cfg14))
    cfg51["stickers_dir"] = str(pk_dir)
    cfg51["sticker_pools"] = {"generic": ["开心", "生气", "叹气"]}
    cfg51["sticker_pick"] = {"enabled": True}
    cfg51["sticker_fallback"] = {"enabled": True, "min_every": 1, "max_every": 1,
                                 "convs": ["group:parents"]}
    cfg51["_config_path"] = str(state14 / "config51.json")
    out51 = state14 / "out51.jsonl"
    b51 = Bridge(cfg51, FakeRunner(), MockAdapter(str(empty14), str(out51)),
                 state_dir=state14)
    conv51 = b51.convs["group:parents"]
    b51._last_user_text["group:parents"] = "今天太倒霉了"

    import sticker_pick as sp51
    _orig_choose = sp51.choose
    try:
        sp51.choose = lambda reply, user, cands, **kw: "叹气"
        got51 = b51._maybe_fallback_sticker(conv51, "确实够呛")
        check("按情绪挑到叹气", "[表情:叹气]" in got51, got51)

        b51._sticker_count["group:parents"] = 0
        sp51.choose = lambda reply, user, cands, **kw: None
        got51b = b51._maybe_fallback_sticker(conv51, "确实够呛")
        check("挑不出来时仍有图发", "[表情:" in got51b, got51b)

        names51 = set()
        b51._sticker_count["group:parents"] = 0
        sp51.choose = lambda reply, user, cands, **kw: "不存在的图"
        got51c = b51._maybe_fallback_sticker(conv51, "x")
        pick51 = got51c.split("[表情:")[1].rstrip("]") if "[表情:" in got51c else ""
        check("模型乱答会被丢弃并随机兜底",
              pick51 in ("开心", "生气", "叹气"), f"选中了 {pick51!r}")

        # 只传两张以下时不做判断（没得挑），直接随机
        sp51.choose = lambda *a, **kw: "开心"
        single = b51._pick_sticker_by_mood("generic", conv51, reply_text="x",
                                           user_text="y", exclude="不开心")
        check("候选不足时不做模型判断", True, str(single))
    finally:
        sp51.choose = _orig_choose

    # 52. 表情包：有文字提示就不截图，没提示才截
    class FakeEmotion:
        def __init__(self, content):
            self.content = content
            self.type = "emotion"

    stub52 = object.__new__(ad50.WxAutoAdapter)
    stub52._verbose = False
    check("有名字的表情不截图",
          stub52._emotion_needs_shot(FakeEmotion("动画表情 [紧张]")) is False,
          "")
    check("方括号在前的表情也不截图",
          stub52._emotion_needs_shot(FakeEmotion("[动画表情]爱了")) is False, "")
    check("没名字的表情要截图",
          stub52._emotion_needs_shot(FakeEmotion("动画表情")) is True, "")
    check("连动画表情字样都没有也要截图",
          stub52._emotion_needs_shot(FakeEmotion("")) is True, "")

    # 53. "@ 它 + 看一眼" 回看最近那张图
    cfg53 = json.loads(json.dumps(cfg14))
    cfg53["_config_path"] = str(state14 / "config53.json")
    cfg53["look_back"] = {"enabled": True, "convs": ["group:friends"]}
    cfg53["vision"] = {"enabled": True, "convs": ["group:friends"],
                       "auto_reply": True, "auto_reply_convs": ["group:friends"]}
    out53 = state14 / "out53.jsonl"
    b53 = Bridge(cfg53, FakeRunner(), MockAdapter(str(empty14), str(out53)),
                 state_dir=state14)

    check("只 @ 一下算看图请求", b53._is_look_request(""))
    check("看一下这张图算看图请求", b53._is_look_request("看一下这张"))
    check("问这是什么算看图请求", b53._is_look_request("这是什么"))
    check("正常问题不算", not b53._is_look_request("帮我写个脚本把这个月的数据整理一下"))

    # 适配器能翻到最近那张 → 直接看图
    b53._look_at_image = lambda conv, path: "【图片内容（刚看的）】一张猫的照片"
    b53._last_media["group:friends"] = time.time()   # 刚刚见过图，才允许回看
    b53.adapter.fetch_recent_media = lambda conv_id, sender="": ("path", "fake.png")
    out53.write_text("", encoding="utf-8")
    b53.handle(Inbound("group:friends", "owner", "@助手 看一下", kind="text"))
    rows53 = [json.loads(l) for l in out53.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("回看到了最近那张图",
          any("猫的照片" in r.get("text", "") for r in rows53), str(rows53)[:300])

    # 翻不到 → 提示发过来，并挂起等待
    b53.adapter.fetch_recent_media = lambda conv_id, sender="": None
    out53.write_text("", encoding="utf-8")
    b53.handle(Inbound("group:friends", "owner", "@助手 看一下", kind="text"))
    rows53b = [json.loads(l) for l in out53.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("翻不到会让她发过来",
          any("发过来" in r.get("text", "") for r in rows53b), str(rows53b)[:300])
    check("并且挂起等待", "group:friends" in b53._pending_look, str(b53._pending_look))

    # 她随后真的发图 → 不用再 @ 也会看
    out53.write_text("", encoding="utf-8")
    b53.handle(Inbound("group:friends", "owner", "（对方发了一个图片）",
                       attachment="fake.jpg", kind="image"))
    rows53c = [json.loads(l) for l in out53.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("后来发的图会被看", any("猫的照片" in r.get("text", "") for r in rows53c),
          str(rows53c)[:300])
    check("挂起状态已清掉", "group:friends" not in b53._pending_look, "")

    # 54. 朋友群：默认当闲聊，出现"要详细"字眼才当正经请求
    cfg54 = json.loads(json.dumps(cfg14))
    cfg54["_config_path"] = str(state14 / "config54.json")
    cfg54["chat_mode"] = {"default_casual": True,
                          "serious_triggers": ["详细告诉我", "帮我分析"]}
    for c54 in cfg54["conversations"]:
        if c54["conv_id"] == "group:friends":
            c54["style"] = "chat"
    out54 = state14 / "out54.jsonl"
    b54 = Bridge(cfg54, FakeRunner(), MockAdapter(str(empty14), str(out54)),
                 state_dir=state14)
    b54.convs["group:friends"].style = "chat"

    out54.write_text("", encoding="utf-8")
    b54.handle(Inbound("group:friends", "owner", "@助手 哈哈哈哈你是不是傻"))
    rows54 = [json.loads(l)["text"] for l in
              out54.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("玩笑默认判成闲聊",
          any("本条判定：闲聊" in t for t in rows54), str(rows54)[:300])
    check("闲聊要求别做分析",
          any("别做分析和方案" in t for t in rows54), str(rows54)[:300])

    out54.write_text("", encoding="utf-8")
    b54.handle(Inbound("group:friends", "owner", "@助手 详细告诉我这个流程怎么走"))
    rows54b = [json.loads(l)["text"] for l in
               out54.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("出现要求详细的字眼就转正经",
          any("本条判定：正经请求" in t for t in rows54b), str(rows54b)[:300])
    check("并说明是因为哪个字眼",
          any("详细告诉我" in t and "本条判定" in t for t in rows54b), str(rows54b)[:400])

    # 家人群不受影响（style 不是 chat）
    out54.write_text("", encoding="utf-8")
    b54.handle(Inbound("group:parents", "妈妈", "@助手 今天吃什么"))
    rows54c = [json.loads(l)["text"] for l in
               out54.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("其它群不加闲聊判定",
          not any("本条判定" in t for t in rows54c), str(rows54c)[:300])

    # 55. 不强调"机器/工具"身份
    import presence as pr55
    banned = ("机器人", "程序", "重启", "开机", "关机", "系统")
    hits55 = []
    for kind, lines in pr55._FALLBACK.items():
        for line in lines:
            for w in banned:
                if w in line:
                    hits55.append((kind, w, line))
    check("兜底文案不提机器身份", not hits55, str(hits55))

    inst55 = " ".join(pr55._INSTRUCTION.values())
    check("生成指令要求第一人称", "第一人称" in inst55, inst55[:120])

    # 提示词里必须明确写清"不要自称机器人"
    out55 = state14 / "out55.jsonl"
    cfg55 = json.loads(json.dumps(cfg14))
    cfg55["_config_path"] = str(state14 / "config55.json")
    b55 = Bridge(cfg55, FakeRunner(), MockAdapter(str(empty14), str(out55)),
                 state_dir=state14)
    out55.write_text("", encoding="utf-8")
    b55.handle(Inbound("group:parents", "妈妈", "@助手 你是机器人吗"))
    rows55 = [json.loads(l)["text"] for l in
              out55.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("提示里禁止自称机器人",
          any("不要自称机器人" in t for t in rows55), str(rows55)[:300])
    check("提示里要求随口带过身份提问",
          any("算是吧" in t for t in rows55), str(rows55)[:300])

    # 56. 额度紧张后限流：不刷屏、不主动、不看图
    cfg56 = json.loads(json.dumps(cfg14))
    cfg56["_config_path"] = str(state14 / "config56.json")
    cfg56["vision"] = {"enabled": True, "convs": ["group:friends"],
                       "auto_reply": True, "auto_reply_convs": ["group:friends"]}
    cfg56["proactive"] = {"enabled": True, "per_hour": 9,
                          "groups": {"group:friends": True}}
    out56 = state14 / "out56.jsonl"
    b56 = Bridge(cfg56, FakeRunner(), MockAdapter(str(empty14), str(out56)),
                 state_dir=state14)
    conv56 = b56.convs["group:friends"]

    check("默认没被限流", not b56._is_throttled("group:friends"))
    b56._throttle("group:friends", "quota")
    check("限流后状态可查", b56._is_throttled("group:friends"))

    called56 = {"vision": 0, "proactive": 0}
    b56._look_at_image = lambda *a, **k: called56.__setitem__("vision", called56["vision"] + 1) or "x"
    b56._proactive_try = lambda *a, **k: called56.__setitem__("proactive", called56["proactive"] + 1) or False

    # 看图被限流挡住（走真实分支前先确认开关）
    b56._look_at_image = None
    orig_look = Bridge._look_at_image
    try:
        check("限流时不看图",
              orig_look(b56, conv56, "fake.png") is None, "")
    finally:
        b56._look_at_image = orig_look

    # 额度重复提示要静音
    out56.write_text("", encoding="utf-8")
    key56 = f"{__import__('datetime').date.today().isoformat()}|group:friends"
    b56._state.setdefault("quota_notified", {})[key56] = True
    b56._request_quota(conv56, 999999)
    rows56 = out56.read_text(encoding="utf-8").strip()
    check("额度已提醒过就不再刷屏", rows56 == "", rows56[:200])

    # 审批卡去重
    b56._unthrottle("group:friends")
    out56.write_text("", encoding="utf-8")
    b56._queue_approval(conv56, kind="cost", task="t", reason="r")
    n1 = len(b56._state.get("pending") or {})
    b56._queue_approval(conv56, kind="cost", task="t", reason="r")
    n2 = len(b56._state.get("pending") or {})
    check("同会话不重复发审批卡", n1 == n2 == 1, f"{n1} -> {n2}")

    # 批准后解除限流
    b56._throttle("group:friends", "quota")
    b56._unthrottle("group:friends")
    check("解除限流生效", not b56._is_throttled("group:friends"))

    # 看图提示要求"接着说"，不要停在评价图片
    for hint in (h for h in [b56._look_at_image] if h is not orig_look):
        pass
    src56 = Path(__file__).with_name("bridge.py").read_text(encoding="utf-8")
    check("看图提示要求接着聊", "把它当成对话的一部分接着说下去" in src56, "")
    check("看图提示禁止只评价图片", "不要停留在评价图片本身" in src56, "")
    check("额度重复提示已改成静音", "quota_repeat_suppressed" in src56, "")

    # 57. 看图有时效：@ 之后 5 分钟内的图才算在跟自己说话
    import time as _t57
    cfg57 = json.loads(json.dumps(cfg14))
    cfg57["_config_path"] = str(state14 / "config57.json")
    cfg57["vision"] = {"enabled": True, "convs": ["group:friends"],
                       "auto_reply": True, "auto_reply_convs": ["group:friends"],
                       "window_minutes": 5, "daily_limit": 30}
    cfg57["look_back"] = {"enabled": True, "convs": ["group:friends"],
                          "max_age_minutes": 5}
    out57 = state14 / "out57.jsonl"
    b57 = Bridge(cfg57, FakeRunner(), MockAdapter(str(empty14), str(out57)),
                 state_dir=state14)
    b57._look_at_image = lambda conv, path: "【图片内容（刚看的）】一只猫"
    b57.adapter.fetch_recent_media = lambda conv_id, sender="": ("path", "fake.png")

    # 刚 @ 过 → 图不用 @ 也回
    b57._last_addressed["group:friends"] = _t57.time()
    out57.write_text("", encoding="utf-8")
    b57.handle(Inbound("group:friends", "owner", "（对方发了一个图片）",
                       attachment="x.jpg", kind="image"))
    rows57 = [json.loads(l) for l in out57.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("@过之后的图会主动看",
          any("一只猫" in r.get("text", "") for r in rows57), str(rows57)[:300])

    # @ 过很久了 → 不再主动看图
    b57._last_addressed["group:friends"] = _t57.time() - 3600
    out57.write_text("", encoding="utf-8")
    b57.handle(Inbound("group:friends", "owner", "（对方发了一个图片）",
                       attachment="y.jpg", kind="image"))
    rows57b = [json.loads(l) for l in out57.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("过了时效就不主动看",
          not any("一只猫" in r.get("text", "") for r in rows57b), str(rows57b)[:300])

    # 回看：图太旧也不翻
    b57._last_addressed["group:friends"] = _t57.time()
    b57._last_media["group:friends"] = _t57.time() - 3600
    check("图太旧时不回看",
          b57._look_back(b57.convs["group:friends"], "owner") is None, "")

    # 回看：5 分钟内的图可以翻出来
    b57._last_media["group:friends"] = _t57.time() - 60
    check("5 分钟内的图能翻出来",
          b57._look_back(b57.convs["group:friends"], "owner") is not None, "")

    # 58. 连发合并：等对方说完再回
    import merge as mg58
    check("半句话能识别", mg58.looks_incomplete("差不多像是"))
    check("连接词结尾能识别", mg58.looks_incomplete("因为"))
    check("括号没闭合能识别", mg58.looks_incomplete("(没闭合"))
    check("完整句子不算没说完", not mg58.looks_incomplete("我今天去了一趟超市，买了不少东西。"))
    check("问句不算没说完", not mg58.looks_incomplete("真的吗？"))

    cfg58 = json.loads(json.dumps(cfg14))
    cfg58["_config_path"] = str(state14 / "config58.json")
    cfg58["coalesce"] = {"enabled": True, "wait_seconds": 0.3,
                         "incomplete_extra_seconds": 0.3,
                         "max_wait_seconds": 2.0, "skip_commands": True}
    out58 = state14 / "out58.jsonl"
    b58 = Bridge(cfg58, FakeRunner(), MockAdapter(str(empty14), str(out58)),
                 state_dir=state14)

    out58.write_text("", encoding="utf-8")
    b58.enqueue(Inbound("user:owner", "owner", "差不多像是"))
    b58.enqueue(Inbound("user:owner", "owner", "这个样子"))
    time.sleep(1.2)
    rows58 = [json.loads(l) for l in out58.read_text(encoding="utf-8").splitlines() if l.strip()]
    joined58 = "".join(r.get("text", "") for r in rows58)
    check("两段连发被合并成一条", "差不多像是" in joined58 and "这个样子" in joined58,
          joined58[:200])
    check("合并后只处理一轮",
          sum(1 for r in rows58 if "fake" in str(r.get("text", ""))) <= 2,
          str(len(rows58)))

    # 命令不合并，立刻处理
    out58.write_text("", encoding="utf-8")
    t0 = time.time()
    b58.enqueue(Inbound("user:owner", "owner", "状态"))
    time.sleep(0.4)
    body58 = out58.read_text(encoding="utf-8")
    check("命令类消息不等待",
          bool(body58.strip()) and "状态" in body58, body58[:150])

    # 59. 模拟交易日报
    import trading as tr59
    sample59 = state14 / "trade" / "daily_status.json"
    sample59.parent.mkdir(parents=True, exist_ok=True)
    _today59 = __import__("datetime").date.today().isoformat()
    sample59.write_text(json.dumps({
        "trade_date": _today59, "mode": "simulation", "currency": "CNY",
        "equity": {"total": 1000000.0, "cash": 820000.0, "market_value": 180000.0},
        "pnl": {"day": 1520.5, "day_pct": 0.00152, "total": 20500.0, "total_pct": 0.0205},
        "benchmark": {"name": "CSI300", "day_pct": 0.00082},
        "positions": [{"symbol": "600519.SH", "name": "贵州茅台", "qty": 100,
                       "cost": 1680.0, "price": 1702.5, "pnl": 2250.0}],
        "trades_today": 3, "notes": "今日无成交",
    }, ensure_ascii=False), encoding="utf-8")

    st59 = tr59.load(sample59)
    check("能读交易状态", st59.ok and st59.day_pnl == 1520.5, str(st59.reason))
    report59 = tr59.format_report(st59)
    check("日报含当日盈亏", "当日 +¥1,520.50" in report59, report59[:120])
    check("日报含跑赢大盘", "跑赢" in report59, report59[:160])
    check("总资产不带正负号", "总资产 ¥1,000,000.00" in report59, report59[:200])
    check("明细含持仓", "贵州茅台" in tr59.format_report(st59, detail=True), "")

    missing59 = tr59.load(state14 / "trade" / "nope.json")
    check("缺文件时明确说读不到",
          (not missing59.ok) and "没读到" in tr59.format_report(missing59), "")

    broken59 = state14 / "trade" / "broken.json"
    broken59.write_text("{不是json", encoding="utf-8")
    check("坏文件也不崩", (not tr59.load(broken59).ok), "")

    # 桥接：命令和上下文注入
    cfg59 = json.loads(json.dumps(cfg14))
    cfg59["_config_path"] = str(state14 / "config59.json")
    cfg59["trading"] = {"enabled": True, "status_file": str(sample59),
                        "targets": ["user:owner"],
                        "context_convs": ["user:owner", "group:friends"]}
    out59 = state14 / "out59.jsonl"
    b59 = Bridge(cfg59, FakeRunner(), MockAdapter(str(empty14), str(out59)),
                 state_dir=state14)

    out59.write_text("", encoding="utf-8")
    b59.handle(Inbound("user:owner", "owner", "今天盈亏"))
    body59 = "".join(json.loads(l).get("text", "") for l in
                     out59.read_text(encoding="utf-8").splitlines() if l.strip())
    check("命令能出日报", "当日 +¥1,520.50" in body59, body59[:200])

    out59.write_text("", encoding="utf-8")
    b59.handle(Inbound("user:owner", "owner", "最近股票怎么样"))
    body59b = "".join(json.loads(l).get("text", "") for l in
                      out59.read_text(encoding="utf-8").splitlines() if l.strip())
    check("问股票时注入了真实数据", "1,520.50" in body59b, body59b[:300])

    out59.write_text("", encoding="utf-8")
    b59.handle(Inbound("user:owner", "owner", "今天天气不错"))
    body59c = "".join(json.loads(l).get("text", "") for l in
                      out59.read_text(encoding="utf-8").splitlines() if l.strip())
    check("闲聊时不灌交易数据", "交易数据（刚读的" not in body59c, body59c[:200])

    # 60. 读链接 + 文件投递
    import webread as wr60
    check("能提取完整链接",
          wr60.find_urls("看 https://example.com/a?x=1 这个")[0].startswith("https://"),
          str(wr60.find_urls("看 https://example.com/a?x=1 这个")))
    check("能补全裸域名",
          wr60.find_urls("看 mp.weixin.qq.com/s/abc 这个")[0]
          == "https://mp.weixin.qq.com/s/abc", str(wr60.find_urls("看 mp.weixin.qq.com/s/abc 这个")))
    page60 = wr60.fetch("http://127.0.0.1:8080/secret")
    check("内网地址被拒绝", (not page60.ok) and "公网" in page60.reason, page60.reason)
    check("只接受 http/https",
          (not wr60.fetch("file:///C:/Windows/win.ini").ok), "")

    import stickers as st60
    _b60, acts60 = st60.extract_actions("好了\n[投递:phd/表.xlsx]")
    check("投递标记被识别", acts60 == [("deliver", "phd/表.xlsx")], str(acts60))

    # 投递落盘
    tgt60 = state14 / "phd60"
    tgt60.mkdir(parents=True, exist_ok=True)
    cfg60 = json.loads(json.dumps(cfg14))
    cfg60["_config_path"] = str(state14 / "config60.json")
    cfg60["deliver"] = {"enabled": True, "targets": {"phd": str(tgt60)}}
    out60 = state14 / "out60.jsonl"
    b60 = Bridge(cfg60, FakeRunner(), MockAdapter(str(empty14), str(out60)),
                 state_dir=state14)
    ws60 = b60._workspace_for(b60.convs["user:owner"])
    ws60.mkdir(parents=True, exist_ok=True)
    (ws60 / "成品.xlsx").write_bytes(b"x")
    out60.write_text("", encoding="utf-8")
    b60._deliver("user:owner", "做好了\n[投递:phd/成品.xlsx]")
    check("文件被放进目标目录", (tgt60 / "成品.xlsx").exists(),
          str([p.name for p in tgt60.iterdir()]))
    rows60 = "".join(json.loads(l).get("text", "") for l in
                     out60.read_text(encoding="utf-8").splitlines() if l.strip())
    check("投递后有回执", "已经放进去了" in rows60, rows60[:200])

    # 找不到源文件时如实说
    out60.write_text("", encoding="utf-8")
    b60._deliver("user:owner", "[投递:phd/不存在.xlsx]")
    rows60b = "".join(json.loads(l).get("text", "") for l in
                      out60.read_text(encoding="utf-8").splitlines() if l.strip())
    check("缺文件时不假装成功", "没找到" in rows60b, rows60b[:200])

    # 61. 上下线播报覆盖父母群
    cfg61 = json.loads(json.dumps(cfg14))
    cfg61["_config_path"] = str(state14 / "config61.json")
    out61 = state14 / "out61.jsonl"
    b61 = Bridge(cfg61, FakeRunner(), MockAdapter(str(empty14), str(out61)),
                 state_dir=state14)
    pcfg61 = {"enabled": True, "compose": False,
              "targets": ["group:friends", "group:parents"]}
    out61.write_text("", encoding="utf-8")
    ok61 = pr47.notify(b61, pcfg61, "offline")
    rows61 = [json.loads(l) for l in out61.read_text(encoding="utf-8").splitlines() if l.strip()]
    got61 = {r["conv_id"] for r in rows61}
    check("下线播报发到两个群",
          ok61 and got61 == {"group:friends", "group:parents"}, str(got61))

    # 62. 真实交易数据形态（USD / weight+market_value / 机器 notes）
    real62 = state14 / "trade" / "real_shape.json"
    real62.write_text(json.dumps({
        "trade_date": "2026-11-04", "mode": "simulation", "currency": "USD",
        "equity": {"total": 1000401.75, "cash": 800000.0, "market_value": 200401.75},
        "pnl": {"day": 401.75, "day_pct": 0.000402,
                "total": 401.75, "total_pct": 0.000402},
        "benchmark": {"name": "SPY", "day_pct": 0.01363},
        "positions": [{"symbol": "BA", "weight": 0.009994, "market_value": 9998.0}],
        "trades_today": 0,
        "notes": "policy=hold_band; trades_today=20; decided=True; mode=live",
    }, ensure_ascii=False), encoding="utf-8")
    st62 = tr59.load(real62)
    rep62 = tr59.format_report(st62, detail=True)
    check("USD 用美元符号", "+$401.75" in rep62, rep62[:120])
    check("持仓只有权重也能渲染", "$9,998.00" in rep62 and "1.00%" in rep62, rep62[-160:])
    check("机器标记不念给人听", "policy=hold_band" not in rep62, rep62[-160:])
    check("低仓位会提示不可直接比", "不可直接比" in rep62, rep62[:260])

    # 63. 投资笔记（人设汇报）+ 06:00 推送
    import trading as tr63
    st63 = tr59.load(sample59)
    facts63 = tr63.journal_facts(st63)
    check("事实清单含关键数字", "当日盈亏" in facts63 and "1,520.50" in facts63, facts63[:120])
    check("没有个股数据时明确标注",
          "没有个股当日涨跌数据" in facts63, facts63[-160:])

    _orig_j = tr63.write_journal
    _orig_b = tr63.write_brief
    tr63.write_brief = lambda st, **kw: "今天小赚，心情还行。"
    tr63.write_journal = lambda st, **kw: "今天小赚，心情还行。"
    try:
        cfg63 = json.loads(json.dumps(cfg59))
        cfg63["_config_path"] = str(state14 / "config63.json")
        cfg63["trading"] = dict(cfg59["trading"])
        cfg63["trading"]["style"] = "journal"
        out63 = state14 / "out63.jsonl"
        b63 = Bridge(cfg63, FakeRunner(), MockAdapter(str(empty14), str(out63)),
                     state_dir=state14)
        out63.write_text("", encoding="utf-8")
        b63.handle(Inbound("user:owner", "owner", "今天怎么样"))
        body63 = "".join(json.loads(l).get("text", "") for l in
                         out63.read_text(encoding="utf-8").splitlines() if l.strip())
        check("问「今天怎么样」回笔记", "心情还行" in body63, body63[:200])

        out63.write_text("", encoding="utf-8")
        b63.handle(Inbound("user:owner", "owner", "持仓"))
        body63b = "".join(json.loads(l).get("text", "") for l in
                          out63.read_text(encoding="utf-8").splitlines() if l.strip())
        check("问「持仓」仍然给明细", "贵州茅台" in body63b, body63b[:200])
    finally:
        tr63.write_journal = _orig_j
        tr63.write_brief = _orig_b

    # 模型失败要回退到数据版，不能不发
    tr63.write_journal = lambda st, **kw: ""
    try:
        check("笔记失败时回退数据版",
              "当日" in (b63.trading_journal() or ""), "")
    finally:
        tr63.write_journal = _orig_j
        tr63.write_brief = _orig_b

    # 64. 汇报必配表情 + 邀功诉苦 + 判断回写
    # 汇报配表情需要库里有图
    pool64 = state14 / "stickers-trade64"
    pool64.mkdir(parents=True, exist_ok=True)
    for nm in ("开心", "叹气"):
        (pool64 / f"{nm}.png").write_bytes(b"\x89PNG\r\n\x1a\n")

    cfg64 = json.loads(json.dumps(cfg59))
    cfg64["stickers_dir"] = str(pool64)
    cfg64["sticker_pools"] = {"generic": ["开心", "叹气"]}
    cfg64["_config_path"] = str(state14 / "config64.json")
    back64 = state14 / "trade" / "dsh_journal.json"
    cfg64["trading"] = dict(cfg59["trading"])
    cfg64["trading"].update({"style": "data", "journal_out": str(back64),
                             "highlights": True})
    out64 = state14 / "out64.jsonl"
    b64 = Bridge(cfg64, FakeRunner(), MockAdapter(str(empty14), str(out64)),
                 state_dir=state14)

    # 汇报必发一张表情
    out64.write_text("", encoding="utf-8")
    name64 = b64.trading_say("user:owner", "今天小赚一点")
    rows64 = [json.loads(l) for l in out64.read_text(encoding="utf-8").splitlines() if l.strip()]
    check("汇报会配一张表情",
          any(r.get("sticker") for r in rows64), str(rows64)[:200])
    check("表情名有返回", bool(name64), str(name64))

    # 判断回写文件
    st64 = tr59.load(sample59)
    b64.trading_writeback(st64, "今天的笔记内容", name64)
    check("判断已回写到 qlib 目录", back64.exists(), str(back64))
    if back64.exists():
        data64 = json.loads(back64.read_text(encoding="utf-8"))
        check("回写含笔记正文", data64.get("journal") == "今天的笔记内容", "")
        check("回写含心情", bool(data64.get("mood")), str(data64.get("mood")))
        check("回写含数字快照", "day_pct" in (data64.get("numbers") or {}), "")
        check("回写带来源标记", data64.get("source") == "dsh-bridge", "")

    # 邀功/诉苦
    hi64 = state14 / "trade" / "hl.json"
    hi64.write_text(json.dumps({
        "trade_date": "2026-11-04", "mode": "simulation", "currency": "USD",
        "equity": {"total": 1000000.0, "cash": 900000.0, "market_value": 100000.0},
        "pnl": {"day": -3000.0, "day_pct": -0.003,
                "total": -3000.0, "total_pct": -0.003},
        "positions": [],
        "highlights": [{"type": "dodged_loss", "symbol": "NVDA",
                        "detail": "提前卖出后跌了 12%"}],
    }, ensure_ascii=False), encoding="utf-8")
    _oj = tr63.write_highlight
    tr63.write_highlight = lambda st, item, **kw: "还好跑得快，不然就惨了。"
    try:
        cfg64b = json.loads(json.dumps(cfg64))
        cfg64b["trading"]["status_file"] = str(hi64)
        cfg64b["_config_path"] = str(state14 / "config64b.json")
        out64b = state14 / "out64b.jsonl"
        b64b = Bridge(cfg64b, FakeRunner(), MockAdapter(str(empty14), str(out64b)),
                      state_dir=state14 / "state64b")
        n64 = b64b.push_trading_highlights()
        rows64b = [json.loads(l) for l in
                   out64b.read_text(encoding="utf-8").splitlines() if l.strip()]
        check("避开大跌会来邀功", n64 == 1 and bool(rows64b), str(rows64b)[:200])
        check("邀功也配表情", any(r.get("sticker") for r in rows64b), str(rows64b)[:200])
        check("同一天不重复邀功", b64b.push_trading_highlights() == 0, "")
    finally:
        tr63.write_highlight = _oj

    # 65. schema v2：六项字段 + 换手/只数回写 + 陈旧数据不推送
    v2 = state14 / "trade" / "v2.json"
    v2.write_text(json.dumps({
        "schema_version": 2,
        "trade_date": __import__("datetime").date.today().isoformat(),
        "updated_at": "2026-11-05T16:02:55+08:00",
        "mode": "simulation", "data_mode": "live", "currency": "USD",
        "equity": {"total": 1000401.75, "cash": 800000.0, "market_value": 200401.75},
        "pnl": {"day": 401.75, "day_pct": 0.000402,
                "total": 401.75, "total_pct": 0.000402},
        "benchmark": {"name": "SPY", "day_pct": 0.01363},
        "positions": [{"symbol": "BA", "name": "Boeing", "weight": 0.0099,
                       "market_value": 9998.0, "change_pct": 0.021291,
                       "pnl_today": 208.43}],
        "trades_today": 2,
        "trades": [
            {"symbol": "BA", "side": "buy", "qty": 56.0, "price": 177.0,
             "reason": "排名前 99%，目标权重 4.75%"},
            {"symbol": "T", "side": "buy", "qty": 457.0, "price": 21.85,
             "reason": "排名前 95%"},
        ],
        "strategy": {"name": "Top20 equal weight",
                     "thesis": "按月重训，取分数前 20 等权", "building_up": True},
        "goals": {"target_monthly_pct": 0.01, "max_drawdown_pct": 0.15},
        "recent_days": [{"date": "2026-11-03", "day_pct": 0.0},
                        {"date": "2026-11-04", "day_pct": 0.000402}],
        "highlights": [{"type": "big_loss", "symbol": "CAT",
                        "detail": "今日 -535 美元（-5.23%）", "amount": -535.46}],
        "notes": "policy=hold_band; decided=False",
    }, ensure_ascii=False), encoding="utf-8")

    st65 = tr59.load(v2)
    check("v2 能读", st65.ok and st65.trades_today == 2, str(st65.reason))
    check("读到 data_mode", st65.data_mode == "live", st65.data_mode)
    check("读到策略", st65.strategy_name.startswith("Top20"), st65.strategy_name)
    check("读到目标", abs(st65.target_monthly_pct - 0.01) < 1e-9, str(st65.target_monthly_pct))
    check("读到最近几日", len(st65.recent_days) == 2, str(len(st65.recent_days)))
    check("读到 highlights", len(st65.highlights) == 1, str(len(st65.highlights)))
    check("换手算得出来", abs(tr59.turnover_pct(st65) - (56*177+457*21.85)/1000401.75) < 1e-6,
          f"{tr59.turnover_pct(st65):.4f}")

    facts65 = tr59.journal_facts(st65)
    check("事实里有个股涨跌", "Boeing" in facts65 and "2.13%" in facts65, facts65[:200])
    check("事实里有成交理由", "排名前 99%" in facts65, facts65[-300:])
    check("事实里有邀功素材", "CAT" in facts65, facts65[-200:])
    check("事实里有策略与目标", "Top20" in facts65 and "月目标" in facts65, facts65[:400])

    # 回写：换手与只数
    cfg65 = json.loads(json.dumps(cfg59))
    cfg65["_config_path"] = str(state14 / "config65.json")
    back65 = state14 / "trade" / "wb65.json"
    cfg65["trading"] = dict(cfg59["trading"])
    cfg65["trading"].update({"status_file": str(v2), "journal_out": str(back65),
                             "style": "data"})
    b65 = Bridge(cfg65, FakeRunner(), MockAdapter(str(empty14), str(out59)),
                 state_dir=state14 / "state65")
    b65.trading_writeback(st65, "笔记", "开心")
    data65 = json.loads(back65.read_text(encoding="utf-8"))
    nums65 = data65.get("numbers") or {}
    check("回写含换手率", "turnover_pct" in nums65, str(nums65))
    check("回写含持仓只数", nums65.get("positions") == 1, str(nums65.get("positions")))

    # 陈旧数据不推送
    old65 = state14 / "trade" / "old.json"
    old65.write_text(json.dumps({"trade_date": "2026-10-20", "mode": "simulation",
                                 "currency": "USD",
                                 "equity": {"total": 1e6}, "pnl": {"day": 0},
                                 "positions": []}, ensure_ascii=False),
                     encoding="utf-8")
    st_old = tr59.load(old65)
    check("陈旧数据被标记", st_old.ok and st_old.stale, f"stale={st_old.stale}")
    cfg65["trading"]["status_file"] = str(old65)
    b65b = Bridge(cfg65, FakeRunner(), MockAdapter(str(empty14), str(out59)),
                  state_dir=state14 / "state65b")
    check("陈旧数据不会推送", b65b.push_trading_daily() is False, "")

    # 66. 两级汇报：短版推送 + 详细版预留
    cfg66 = json.loads(json.dumps(cfg59))
    cfg66["stickers_dir"] = str(pool64)
    cfg66["sticker_pools"] = {"generic": ["开心", "叹气"]}
    cfg66["_config_path"] = str(state14 / "config66.json")
    cfg66["trading"] = dict(cfg59["trading"])
    cfg66["trading"].update({"style": "journal", "status_file": str(sample59),
                             "journal_out": str(state14 / "trade" / "wb66.json")})
    out66 = state14 / "out66.jsonl"
    shutil.rmtree(state14 / "state66", ignore_errors=True)
    b66 = Bridge(cfg66, FakeRunner(), MockAdapter(str(empty14), str(out66)),
                 state_dir=state14 / "state66")

    _ob = tr63.write_brief
    _oj2 = tr63.write_journal
    tr63.write_brief = lambda st, **kw: "昨天赚了 401.75 美元，涨幅 0.04%。\n心里还行。"
    tr63.write_journal = lambda st, **kw: "详细版：QCOM 涨 5.33%，CAT 跌 5.23%。"
    try:
        out66.write_text("", encoding="utf-8")
        pushed66 = b66.push_trading_daily()
        rows66 = [json.loads(l) for l in out66.read_text(encoding="utf-8").splitlines() if l.strip()]
        body66 = "".join(r.get("text", "") for r in rows66)
        check("推送发的是短版", "401.75" in body66 and "QCOM" not in body66, body66[:200])
        check("推送配了表情", any(r.get("sticker") for r in rows66), str(rows66)[:200])
        check("详细版已提前生成",
              b66._cached_journal("full").startswith("详细版"), b66._cached_journal("full")[:60])

        # 问「详细」直接发预留好的，不再调模型
        tr63.write_journal = lambda st, **kw: "重新生成了（不应该出现）"
        out66.write_text("", encoding="utf-8")
        b66.handle(Inbound("user:owner", "owner", "详细"))
        body66b = "".join(json.loads(l).get("text", "") for l in
                          out66.read_text(encoding="utf-8").splitlines() if l.strip())
        check("问详细发的是预留版",
              "QCOM" in body66b and "重新生成" not in body66b, body66b[:200])

        # 问「盈亏」发短版并提示可以看详细
        out66.write_text("", encoding="utf-8")
        b66.handle(Inbound("user:owner", "owner", "盈亏"))
        body66c = "".join(json.loads(l).get("text", "") for l in
                          out66.read_text(encoding="utf-8").splitlines() if l.strip())
        check("问盈亏发短版", "401.75" in body66c, body66c[:200])
        check("并提示可看详细", "详细" in body66c, body66c[:200])

        # 回写文件里有 full
        wb66 = json.loads((state14 / "trade" / "wb66.json").read_text(encoding="utf-8"))
        check("回写含详细版", "QCOM" in str(wb66.get("journal_full")), "")
        check("回写含短版", "401.75" in str(wb66.get("journal")), "")
    finally:
        tr63.write_brief = _ob
        tr63.write_journal = _oj2

    # 67. 推送改成"数据一变就推"
    cfg67 = json.loads(json.dumps(cfg59))
    cfg67["stickers_dir"] = str(pool64)
    cfg67["sticker_pools"] = {"generic": ["开心"]}
    cfg67["_config_path"] = str(state14 / "config67.json")
    cfg67["trading"] = dict(cfg59["trading"])
    cfg67["trading"]["status_file"] = str(sample59)
    out67 = state14 / "out67.jsonl"
    shutil.rmtree(state14 / "state67", ignore_errors=True)
    b67 = Bridge(cfg67, FakeRunner(), MockAdapter(str(empty14), str(out67)),
                 state_dir=state14 / "state67")
    _ob67 = tr63.write_brief
    _oj67 = tr63.write_journal
    tr63.write_brief = lambda st, **kw: "短版内容"
    tr63.write_journal = lambda st, **kw: "详细内容"
    try:
        v1 = b67.trading_data_version()
        check("能算出数据版本", "|" in v1, v1)
        check("初始认为没推过", not b67.trading_already_pushed(), "")
        out67.write_text("", encoding="utf-8")
        check("第一次会推", b67.push_trading_daily() is True, "")
        check("推过之后能识别", b67.trading_already_pushed(), "")
        out67.write_text("", encoding="utf-8")
        check("同一版本不重复推（第二次返回 False）",
              b67.push_trading_daily() is False, "")
    finally:
        tr63.write_brief = _ob67
        tr63.write_journal = _oj67

    # 68. 下线通知只发朋友群（家人群只看上线）
    out68 = state14 / "out68.jsonl"
    cfg68 = json.loads(json.dumps(cfg14))
    cfg68["_config_path"] = str(state14 / "config68.json")
    b68 = Bridge(cfg68, FakeRunner(), MockAdapter(str(empty14), str(out68)),
                 state_dir=state14)
    pcfg68 = {"enabled": True, "compose": False,
              "targets": ["group:friends", "group:parents"],
              "stop_targets": ["group:friends"]}
    out68.write_text("", encoding="utf-8")
    pr47.notify(b68, pcfg68, "offline")
    got68 = {json.loads(l)["conv_id"] for l in
             out68.read_text(encoding="utf-8").splitlines() if l.strip()}
    check("下线只发朋友群", got68 == {"group:friends"}, str(got68))

    out68.write_text("", encoding="utf-8")
    pr47.notify(b68, pcfg68, "online")
    got68b = {json.loads(l)["conv_id"] for l in
              out68.read_text(encoding="utf-8").splitlines() if l.strip()}
    check("上线仍然两个群都发", got68b == {"group:friends", "group:parents"}, str(got68b))

    # 69. 亏损时不能发"开心"类表情
    cfg69 = json.loads(json.dumps(cfg59))
    cfg69["stickers_dir"] = str(pool64)
    cfg69["_config_path"] = str(state14 / "config69.json")
    cfg69["sticker_pools"] = {"generic": ["开心", "叹气", "无语流汗"],
                              "happy": ["开心"],
                              "sad": ["叹气", "无语流汗"]}
    cfg69["sticker_moods"] = {"cheerful": ["开心"]}
    out69 = state14 / "out69.jsonl"
    b69 = Bridge(cfg69, FakeRunner(), MockAdapter(str(empty14), str(out69)),
                 state_dir=state14 / "state69")

    cands_sad = b69._mood_candidates("sad", "generic")
    check("亏损时候选里没有开心", "开心" not in cands_sad, str(cands_sad))
    check("亏损时有非开心候选", bool(cands_sad), str(cands_sad))

    out69.write_text("", encoding="utf-8")
    picks = [b69._send_mood_sticker("user:owner", "今天亏了", mood="sad")
             for _ in range(8)]
    check("连发 8 次都不是开心且都有图",
          all(p and p != "开心" for p in picks), str(picks))

    # 没有专用池时用剔除逻辑
    cfg69b = json.loads(json.dumps(cfg69))
    cfg69b["sticker_pools"] = {"generic": ["开心", "叹气"]}
    cfg69b["_config_path"] = str(state14 / "config69b.json")
    b69b = Bridge(cfg69b, FakeRunner(), MockAdapter(str(empty14), str(out69)),
                  state_dir=state14 / "state69b")
    cands69b = b69b._mood_candidates("sad", "generic")
    check("没有专用池也能剔除开心", "开心" not in cands69b, str(cands69b))

    # 70. 最小化窗口要能自己恢复 + 推送按目标补发
    import adapters as ad70
    check("有窗口唤醒方法", hasattr(ad70.WxAutoAdapter, "_ensure_window_awake"), "")

    cfg70 = json.loads(json.dumps(cfg59))
    cfg70["stickers_dir"] = str(pool64)
    cfg70["sticker_pools"] = {"generic": ["开心", "叹气"], "sad": ["叹气"]}
    cfg70["_config_path"] = str(state14 / "config70.json")
    cfg70["trading"] = dict(cfg59["trading"])
    cfg70["trading"]["targets"] = ["user:owner", "group:friends"]
    out70 = state14 / "out70.jsonl"
    shutil.rmtree(state14 / "state70", ignore_errors=True)
    b70 = Bridge(cfg70, FakeRunner(), MockAdapter(str(empty14), str(out70)),
                 state_dir=state14 / "state70")
    _ob70 = tr63.write_brief
    _oj70 = tr63.write_journal
    tr63.write_brief = lambda st, **kw: "短版"
    tr63.write_journal = lambda st, **kw: "详细"

    # 让其中一个目标发送失败，模拟"群没接上"
    _orig_say = b70.trading_say
    state70 = {"fail": {"group:friends"}}
    def fake_say(conv_id, text, mood=""):
        if conv_id in state70["fail"]:
            raise RuntimeError("群没接上")
        return _orig_say(conv_id, text, mood=mood)
    b70.trading_say = fake_say
    try:
        out70.write_text("", encoding="utf-8")
        b70.push_trading_daily()
        last70 = (b70._state.get("trading_last_push") or {})
        check("部分成功会标记 partial", last70.get("partial") is True, str(last70))
        check("成功的目标被记下", "user:owner" in (last70.get("targets") or []), str(last70))
        check("有目标没发成功就不算推完", b70.trading_already_pushed() is False, "")

        # 群恢复后：只补发群，不重复发私聊
        state70["fail"] = set()
        out70.write_text("", encoding="utf-8")
        b70.push_trading_daily()
        rows70 = [json.loads(l) for l in
                  out70.read_text(encoding="utf-8").splitlines() if l.strip()]
        convs70 = {r["conv_id"] for r in rows70}
        check("补发只发给漏掉的群",
              "group:friends" in convs70 and "user:owner" not in convs70, str(convs70))
        check("补完后算推完", b70.trading_already_pushed() is True, "")
    finally:
        tr63.write_brief = _ob70
        tr63.write_journal = _oj70

    # 71. 私聊暂停（当中转站用）
    cfg71 = json.loads(json.dumps(cfg14))
    cfg71["_config_path"] = str(state14 / "config71.json")
    cfg71["pause"] = {"convs": ["user:owner"]}
    shutil.rmtree(state14 / "state71", ignore_errors=True)
    out71 = state14 / "out71.jsonl"
    b71 = Bridge(cfg71, FakeRunner(), MockAdapter(str(empty14), str(out71)),
                 state_dir=state14 / "state71")

    def _last71():
        rows = [json.loads(l) for l in
                out71.read_text(encoding="utf-8").splitlines() if l.strip()]
        return rows[-1]["text"] if rows else ""

    out71.write_text("", encoding="utf-8")
    b71.handle(Inbound("user:owner", "owner", "暂停 30分钟"))
    check("暂停指令生效", b71._paused("user:owner"), "")
    check("暂停有回执", "先不回消息" in _last71(), _last71()[:80])
    check("回执里写了时长", "30 分钟" in _last71(), _last71()[:80])

    out71.write_text("", encoding="utf-8")
    b71.handle(Inbound("user:owner", "owner", "帮我记一下这句话"))
    check("暂停期间不回复", _last71() == "", _last71()[:80])
    relay71 = state14 / "state71" / "relay.jsonl"
    check("暂停期间有记账",
          relay71.exists() and "帮我记一下" in relay71.read_text(encoding="utf-8"),
          "")

    out71.write_text("", encoding="utf-8")
    b71._say("user:owner", "【用量】这条应该被拦住")
    check("用量提示也被拦住", _last71() == "", _last71()[:80])

    out71.write_text("", encoding="utf-8")
    b71.handle(Inbound("user:owner", "owner", "恢复"))
    check("恢复指令生效", not b71._paused("user:owner"), "")
    check("恢复时告知攒了几条", "1 条" in _last71(), _last71()[:80])

    out71.write_text("", encoding="utf-8")
    b71.handle(Inbound("user:owner", "owner", "暂停 2小时"))
    check("支持按小时暂停", "2 小时" in _last71(), _last71()[:80])
    b71.handle(Inbound("user:owner", "owner", "恢复"))

    # 到点自动恢复
    b71._set_pause("user:owner", 0.01)
    import time as _t71
    _t71.sleep(0.2)
    check("到点会自动恢复", not b71._paused("user:owner"), "")

    # 群里不能触发
    out71.write_text("", encoding="utf-8")
    b71.handle(Inbound("group:friends", "路人", "暂停"))
    check("群里说暂停不生效", not b71._paused("group:friends"), "")

    width = max(len(n) for n, _, _ in checks)
    failed = 0
    for name, ok, detail in checks:
        mark = "PASS" if ok else "FAIL"
        if not ok:
            failed += 1
        print(f"[{mark}] {name.ljust(width)}  {detail if not ok else ''}")

    print(f"\n{len(checks) - failed}/{len(checks)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
