"""微信 → DSH 桥接核心。

职责（和微信方案无关，换适配器不影响这里）：
  1. 白名单路由：只认配置里列出的会话，其他一律丢弃并记日志
  2. 触发判断：群里必须 @ 或带前缀才响应，私聊直接响应
  3. 会话隔离：每个会话独立工作目录 + 独立 DSH session
  4. 权限分档：默认 workspace-write；需要更高权限时挂起并找本人批准
  5. 预算控制：每会话每日额度
  6. 全量审计日志（自动脱敏）
"""
from __future__ import annotations

import json
import os
import random
import re
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from adapters import Adapter, Inbound
import balance as balance_mod
import follow as follow_mod
import backup as backup_mod
import vcs as vcs_mod
import vision as vision_mod
import webread as webread_mod
import imagegen as imagegen_mod
import merge as merge_mod
import memory as memory_mod
import memory_auto as memory_auto_mod
import nudge as nudge_mod
import privacy
import proactive
import sticker_pick as sticker_pick_mod
import stickers as stickers_mod
import tablecard as tablecard_mod
import trading as trading_mod
import wechat_format
from runner import AgentRunner, RunResult

_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{8})[A-Za-z0-9_\-]+"),          # API key
    re.compile(r"(?i)(token|secret|password)\s*[:=]\s*\S+"),        # 形如 token: xxx
]

# 微信里 @ 之后常跟不可见字符（U+2005 等），不能只 strip 普通空格
_INVISIBLE = " \t\r\n\u2005\u200b\u00a0\u3000"

# 微信里读长文的体验很差，让 agent 先给结论
_FORMAT_HINT = (
    "【回复格式要求】先用一句话给出结论，需要展开时再空一行分点说明；"
    "不要使用 Markdown 符号、表格或代码围栏。\n\n"
)


def redact(text: str) -> str:
    for pat in _SECRET_PATTERNS:
        text = pat.sub(lambda m: (m.group(1) + "****") if m.groups() else "****", text)
    return text


@dataclass
class Conversation:
    conv_id: str
    kind: str                    # "group" | "private"
    name: str
    daily_budget_tokens: int = 200_000
    trigger: str = "always"      # "always" | "@bot"   群默认 @bot
    aliases: list[str] = field(default_factory=list)
    wechat_name: str | None = None   # 微信里显示的会话名，真人名称要一字不差
    style: str = "default"           # "default" | "chat"（像日常聊天那样简短）
    memory_scope: str = "owner"      # 该会话用哪一份记忆：family / friends / owner


@dataclass
class Pending:
    pid: str
    conv_id: str
    task: str
    session_id: Optional[str]
    created: float
    reason: str
    kind: str = "task"        # task=权限升级 / question=隐私提问 / draft=回答被扣住
    draft: str = ""           # kind=draft 时待发的内容


class Bridge:
    def __init__(
        self,
        config: dict[str, Any],
        runner: AgentRunner,
        adapter: Adapter,
        state_dir: str | Path = "state",
    ) -> None:
        self.cfg = config
        self.runner = runner
        self.adapter = adapter
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.log_path = self.state_dir / "transcript.jsonl"

        self.family_root = Path(config["family_root"]).resolve()
        self.owner_conv = config["owner"]["conv_id"]
        self.bot_mention = config.get("bot_mention", "@助手")

        self.convs: dict[str, Conversation] = {
            c["conv_id"]: Conversation(**c) for c in config["conversations"]
        }
        self.paused = False          # 暂停后只有本人的私聊还能用
        self._state_lock = threading.RLock()
        self._conv_locks: dict[str, threading.Lock] = {}
        self._last_quota_conv: str | None = None
        self._pending_sticker: str | None = None   # 等本人给名字的图片
        self._follow: dict[str, dict] = {}   # 刚回过话的会话：还能接着说几条
        # 主动接话的节奏控制：上次说话时间、被无视次数、暂停到什么时候
        self._pace: dict[str, dict] = {}
        self._last_user_text: dict[str, str] = {}   # 挑表情时用来判断情绪
        self._pending_look: dict[str, dict] = {}    # "等一下图"的会话
        # 额度紧张、等主人批准的会话：暂停主动接话和看图，避免越等越吵
        self._throttled: dict[str, dict] = {}
        self._last_addressed: dict[str, float] = {}   # 最近一次被 @ 的时间
        self._last_media: dict[str, float] = {}       # 最近一次看到图/表情的时间
        # 连发合并：先等对方说完，再一起处理
        self._merge_buf: dict[str, dict] = {}
        self._merge_lock = threading.Lock()
        self._merge_thread: threading.Thread | None = None
        self._dispatch_pool = None
        self._sticker_count: dict[str, int] = {}   # 各会话距上次发表情过了几条
        self._last_fallback: dict[str, str] = {}   # 上次保底用的哪张，避免连着用
        self._proactive: dict = dict(config.get("proactive", {}))
        # 记忆路径必须绝对化：相对路径会随工作目录变化，多进程（桥接 + 测试脚本）
        # 会写到不同文件、或互相用过期副本覆盖
        mem_path = Path(str(config.get("memory_file", "state/memory.json")))
        if not mem_path.is_absolute():
            base = Path(str(config.get("_config_path") or "config.json")).resolve().parent
            mem_path = base / mem_path
        # 按范围分开：家人群 / 朋友群 / 本人各一份。老的单文件会自动迁移过去。
        self.memories = memory_mod.MemoryStore(
            mem_path.with_suffix(""), scopes=["family", "friends", "owner"],
            legacy_path=mem_path,
        )
        self.memory = self.memories.for_scope("owner")   # 兼容旧调用点
        self.self_doc = memory_mod.SelfKnowledge(mem_path.with_name("self.md"))
        self.self_doc.ensure(self._default_self_doc(config))

        # 备份放到 agent 工作区之外：特区里的东西它能删，备份在里面等于没备份
        bcfg = config.get("backup") or {}
        dest = Path(str(bcfg.get("dest") or (Path.home() / "dsh-bridge-backups"))).expanduser()
        self.backups = backup_mod.BackupManager(
            Path(str(config.get("_config_path") or "config.json")).resolve().parent,
            dest, keep=int(bcfg.get("keep", 10)),
        )
        self._backup_after_hour = int(bcfg.get("daily_after_hour", 20))

        # 从 secret.env 读取 GitHub token，用于停止时自动推送
        self._push_on_shutdown = bool(config.get("push_on_shutdown", False))
        self._github_user = str(config.get("github_user") or "your-github-name")
        self._github_repo = str(config.get("github_repo") or "wechatdsh")

        # git 快照：能精确回退单个文件，补 zip 备份做不到的那部分
        self.repo = vcs_mod.Repo(self.backups.source)

        # 表情库：本地图片即表情，目录同样绝对化
        self.stickers = None
        if config.get("sticker_enabled", True):
            sdir = Path(str(config.get("stickers_dir", "stickers")))
            if not sdir.is_absolute():
                base = Path(str(config.get("_config_path") or "config.json")).resolve().parent
                sdir = base / sdir
            self.stickers = stickers_mod.Stickers(sdir)

        # 按需生图（可选）：没配 key 就整个功能不启用
        self.imagegen = None
        self._image_fail_notified = False
        ig_cfg = dict(config.get("image_gen", {}) or {})
        if ig_cfg.get("enabled"):
            key = os.environ.get(str(ig_cfg.get("api_key_env", "OPENAI_API_KEY")), "")
            if not key:
                print(f"[生图] 已启用但环境里没有 {ig_cfg.get('api_key_env')}，本次跳过", flush=True)
            else:
                cdir = Path(str(ig_cfg.get("cache_dir", "stickers/generated")))
                if not cdir.is_absolute():
                    base = Path(str(config.get("_config_path") or "config.json")).resolve().parent
                    cdir = base / cdir
                refs = []
                for r in (ig_cfg.get("reference_images") or []):
                    rp = Path(str(r))
                    if not rp.is_absolute():
                        rp = Path(str(config.get("_config_path") or "config.json")).resolve().parent / rp
                    if rp.exists():
                        refs.append(rp)
                    else:
                        print(f"[生图] 参考图不存在，已跳过：{rp}", flush=True)
                self.imagegen = imagegen_mod.ImageGen(
                    base_url=str(ig_cfg.get("base_url", "https://api.openai.com/v1")),
                    api_key=key,
                    model=str(ig_cfg.get("model", "gpt-image-1")),
                    cache_dir=cdir,
                    size=str(ig_cfg.get("size", "1024x1024")),
                    quality=str(ig_cfg.get("quality", "")),
                    timeout=float(ig_cfg.get("timeout_s", 60)),
                    max_px=int(ig_cfg.get("max_px", 512)),
                    reference_images=refs,
                    style_prefix=str(ig_cfg.get("style_prefix", "")),
                )
                print(f"[生图] 已启用 模型={self.imagegen.model} 缓存={cdir} "
                      f"参考图={len(refs)} 张", flush=True)
        # 登记"指定密文"：即使长得不像密钥，也一律按隐私处理
        privacy.set_literals(config.get("secret_literals", []))
        self._state = self._load_state()
        if config.get("reset_approvals_on_start", True):
            self._reset_approvals(reason="启动")

    def _conv_lock(self, conv_id: str) -> threading.Lock:
        return self._conv_locks.setdefault(conv_id, threading.Lock())

    # ---------- 状态 ----------
    def _load_state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        return {"sessions": {}, "usage": {}, "pending": {}, "seq": 0}

    def _save_state(self) -> None:
        with self._state_lock:
            (self.state_dir / "state.json").write_text(
                json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def _log(self, event: str, **fields: Any) -> None:
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event}
        for k, v in fields.items():
            row[k] = redact(v) if isinstance(v, str) else v
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    # ---------- 工具 ----------
    def _workspace_for(self, conv: Conversation) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", conv.conv_id)
        return self.family_root / safe

    def _maybe_fallback_sticker(self, conv: Conversation, text: str) -> str:
        """保底发表情：每回复 N 条，挑其中一条附一张通用表情。

        为什么需要：模型倾向于"只在特别合适时才发表情"，实际频率太低，
        群里显得干巴。用一个概率下限兜住：不到 N 条不发，到 N 条必发一次。

        几条约束：
          · 只在配置的群聊里生效（私聊不刷表情）；
          · 模型自己已经带了表情就重置计数，不叠加；
          · 不连着用同一张；
          · 优先从 stickers/basic/ 里挑（那批是"万能"的）。
        """
        fb = self.cfg.get("sticker_fallback") or {}
        if not fb.get("enabled", True) or self.stickers is None:
            return text
        convs = fb.get("convs") or []
        if conv.conv_id not in convs:
            return text

        # 模型已经挑过表情了：重置计数，尊重它的判断
        _body, actions = stickers_mod.extract_actions(text)
        if actions:
            self._sticker_count[conv.conv_id] = 0
            return text

        self._sticker_count[conv.conv_id] = self._sticker_count.get(conv.conv_id, 0) + 1
        threshold = random.randint(int(fb.get("min_every", 3)), max(
            int(fb.get("min_every", 3)), int(fb.get("max_every", 5))))
        if self._sticker_count[conv.conv_id] < threshold:
            return text

        # 跟主人私聊时优先用"专属"那批；没有专属就退回通用
        pool = "generic"
        if conv.conv_id == self.owner_conv and self._pool_sticker("exclusive"):
            pool = "exclusive"

        # 挑哪一张：先让模型按情绪选，选不出来再随机兜底
        name = self._pick_sticker_by_mood(
            pool, conv, reply_text=text,
            user_text=self._last_user_text.get(conv.conv_id, ""),
            exclude=self._last_fallback.get(conv.conv_id))
        if not name:
            name = self._pool_sticker(pool,
                                      exclude=self._last_fallback.get(conv.conv_id))
        if not name:
            return text

        self._sticker_count[conv.conv_id] = 0
        self._last_fallback[conv.conv_id] = name
        self._log("sticker_fallback", conv_id=conv.conv_id, name=name,
                  after=threshold)
        return text + f"\n[表情:{name}]"

    # ---------- 关注窗口：@ 过之后能接着说几句 ----------
    def _follow_open(self, conv: Conversation, reply: str) -> None:
        """回完一句话，打开一个小窗口，接下来几条消息不必再 @。"""
        cfg = self.cfg.get("follow") or {}
        if not cfg.get("enabled", True) or conv.kind != "group":
            return
        self._follow[conv.conv_id] = {
            "left": int(cfg.get("max_messages", 4)),
            "deadline": time.time() + float(cfg.get("window_minutes", 10)) * 60,
            "last_reply": (reply or "")[:600],
            "used": 0,
        }

    def _follow_should_handle(self, conv: Conversation, text: str) -> bool:
        """这条群消息要不要当成"在跟我说话"来处理。"""
        cfg = self.cfg.get("follow") or {}
        if not cfg.get("enabled", True):
            return False
        state = self._follow.get(conv.conv_id)
        if not state:
            return False
        if time.time() > state.get("deadline", 0) or state.get("left", 0) <= 0:
            self._follow.pop(conv.conv_id, None)
            return False

        # 关键词命中：直接算叫她，不花钱判断
        lowered = (text or "").lower()
        for kw in cfg.get("keywords") or []:
            if kw and str(kw).lower() in lowered:
                state["left"] = state.get("left", 0) - 1
                self._log("follow_keyword", conv_id=conv.conv_id, keyword=kw,
                          left=state["left"])
                return True

        if not cfg.get("judge", True):
            state["left"] = state.get("left", 0) - 1
            return True

        # 把握的门槛随窗口内第几句逐步抬高：第一句 50 分就回，
        # 越往后越谨慎（70 → 85 → 95），避免窗口拖长后乱插话。
        thresholds = cfg.get("thresholds") or [50, 70, 85, 95]
        used = int(state.get("used", 0))
        need = int(thresholds[min(used, len(thresholds) - 1)])

        score = follow_mod.confidence(
            new_message=text, last_reply=state.get("last_reply", ""),
            api_key=self._env_api_key())
        state["used"] = used + 1
        state["left"] = state.get("left", 0) - 1
        yes = score >= need
        self._log("follow_judged", conv_id=conv.conv_id, score=score,
                  need=need, yes=yes, left=state["left"])
        if state["left"] <= 0:
            self._follow.pop(conv.conv_id, None)
        return yes

    # ---------- 表情池 ----------
    def _pool_sticker(self, pool: str, exclude: str | None = None) -> str | None:
        """从配置的表情池里随机挑一张（只挑库里真实存在的）。"""
        if self.stickers is None:
            return None
        pools = self.cfg.get("sticker_pools") or {}
        names = list(pools.get(pool) or [])
        if not names and pool == "generic":
            names = self.stickers.basic_names()
        cands = [n for n in self.stickers.pool(names) if n != exclude]
        return random.choice(cands) if cands else None

    def _send_pool_sticker(self, conv_id: str, pool: str) -> None:
        """只发一张表情图，不带文字。"""
        name = self._pool_sticker(pool)
        if not name:
            self._log("pool_sticker_missing", conv_id=conv_id, pool=pool)
            return
        self._log("pool_sticker", conv_id=conv_id, pool=pool, name=name)
        self._deliver(conv_id, f"[表情:{name}]")

    def _say_with_pool(self, conv_id: str, text: str, pool: str) -> None:
        name = self._pool_sticker(pool)
        if name:
            self._log("pool_sticker", conv_id=conv_id, pool=pool, name=name)
            self._deliver(conv_id, text + f"\n[表情:{name}]")
        else:
            self._say(conv_id, text)

    def push_to_github(self) -> tuple[bool, str]:
        """把本地提交推到 GitHub。token 从 secret.env 现取，不写进仓库。"""
        try:
            import keystore

            token = keystore.load().get("GITHUB_TOKEN") or ""
        except Exception:
            token = ""
        if not token:
            return False, "secret.env 里没有 GITHUB_TOKEN"
        if not self.repo.remote_url():
            return False, "还没有配置远端仓库"
        ok, out = self.repo.push("main", user=self._github_user, token=token)
        return ok, out

    def _zone_hint(self, conv: Conversation) -> str:
        """告诉它"这块地是你的，随便折腾"——之前没说过，它才会到处撞权限。"""
        ws = self._workspace_for(conv)
        return (
            f"【你的工作区】{ws}\n"
            "这个目录里你可以自由读写、新建文件、建子目录、跑脚本，不需要任何授权。"
            "要做文件相关的活儿，就在这里面做。\n"
            "这个目录**以外**的路径默认要主人批准：不要主动去改桥接自己的配置、"
            "记忆、表情库——那些改坏了整个服务就起不来了。"
        )

    def _used_today(self, conv_id: str) -> int:
        key = f"{date.today().isoformat()}|{conv_id}"
        return int(self._state["usage"].get(key, 0))

    def _charge(self, conv_id: str, tokens: int) -> None:
        key = f"{date.today().isoformat()}|{conv_id}"
        with self._state_lock:
            self._state["usage"][key] = int(self._state["usage"].get(key, 0)) + int(tokens)
            self._state.setdefault("runs", {})
            self._state["runs"][key] = int(self._state["runs"].get(key, 0)) + 1
            # 必须立刻落盘：否则进程重启后用量回退，每日额度形同虚设
            self._save_state()

    def _runs_today(self, conv_id: str) -> int:
        return int(self._state.get("runs", {}).get(
            f"{date.today().isoformat()}|{conv_id}", 0))

    def _notify_usage(self, conv: Conversation, speaker: str, tokens: int,
                      elapsed: float = 0.0) -> None:
        """按配置把本次消耗通报给本人，用来定位是哪个对话在烧钱。"""
        if tokens <= 0:
            return
        mode = str(self.cfg.get("usage_notice", "always"))
        if mode == "never":
            return
        if mode == "big":
            threshold = int(self.cfg.get("single_task_alert_tokens", 0) or 0)
            if not threshold or tokens < threshold:
                return
        used = self._used_today(conv.conv_id)
        who = f" · {speaker}" if speaker else ""

        # 本人的私聊不设限额，只报告花了多少
        if conv.conv_id == self.owner_conv:
            self._say(
                self.owner_conv,
                f"【用量】{conv.name}{who}：+{tokens:,} tokens（{elapsed:.0f}s）\n"
                f"今日累计 {used:,}（不限额）",
            )
            return

        budget = self._budget(conv)
        pct = used / budget if budget else 0
        self._say(
            self.owner_conv,
            f"【用量】{conv.name}{who}：+{tokens:,} tokens（{elapsed:.0f}s）\n"
            f"今日 {used:,} / {budget:,}（{pct:.0%}）",
        )

    def _reset_approvals(self, reason: str) -> None:
        """清空待审批并让编号归零。

        审批编号只是"当下这条请求"的临时标识，不是账本流水号，留着只会
        越滚越大、看着像欠了一堆事。所以启动时和跨天时都重来。
        """
        today = date.today().isoformat()
        changed = False
        if self._state.get("pending"):
            self._state["pending"] = {}
            changed = True
        if int(self._state.get("seq", 0)):
            self._state["seq"] = 0
            changed = True
        if self._state.get("seq_date") != today:
            self._state["seq_date"] = today
            changed = True
        if changed:
            self._save_state()
            self._log("approvals_reset", reason=reason)

    def _bonus(self, conv_id: str) -> int:
        key = f"{date.today().isoformat()}|{conv_id}"
        return int(self._state.get("bonus", {}).get(key, 0))

    def _budget(self, conv: Conversation) -> int:
        return conv.daily_budget_tokens + self._bonus(conv.conv_id)

    def _add_bonus(self, conv_id: str, amount: int) -> int:
        self._unthrottle(conv_id)
        key = f"{date.today().isoformat()}|{conv_id}"
        with self._state_lock:
            self._state.setdefault("bonus", {})
            self._state["bonus"][key] = int(self._state["bonus"].get(key, 0)) + int(amount)
            self._save_state()
            return self._state["bonus"][key]

    def _next_pid(self) -> str:
        if self._state.get("seq_date") != date.today().isoformat():
            self._reset_approvals(reason="跨天")
        with self._state_lock:
            self._state["seq"] = int(self._state["seq"]) + 1
            self._save_state()
            return str(self._state["seq"])

    # ---------- 回执 ----------
    _ACTION_WORDS = (
        "查", "找", "搜", "读", "写", "改", "删", "整理", "生成", "跑", "运行",
        "下载", "上传", "翻译", "总结", "分析", "计算", "统计",
        "文件", "表格", "文件夹", "目录", "代码", "脚本", "文档",
        "excel", "word", "pdf", "csv", "截图", "帮我",
    )

    def _looks_like_action(self, text: str) -> bool:
        """这句话是不是需要动文件/跑东西。纯闲聊不该先回一句"收到"。"""
        t = (text or "").lower()
        return any(w in t for w in self._ACTION_WORDS)

    def _delayed_ack(self, conv_id: str, done: threading.Event, delay: float) -> None:
        """没判定为动文件时，等一会儿还不回才补回执，避免纯聊天也刷一句。"""
        if done.wait(delay):
            return
        try:
            self._say(conv_id, "收到，正在处理…")
            self._log("ack_delayed", conv_id=conv_id, delay_s=delay)
        except Exception:
            pass

    # ---------- 主流程 ----------
    def handle(self, msg: Inbound) -> None:
        conv = self.convs.get(msg.conv_id)

        if conv is None:
            self._log("dropped_unknown_conversation", conv_id=msg.conv_id,
                      sender=msg.sender, text=msg.text)
            return

        # 同一会话串行（避免两份任务抢同一个 DSH session），不同会话可并行
        with self._conv_lock(conv.conv_id):
            self._handle(conv, msg)

    def _handle(self, conv: Conversation, msg: Inbound) -> None:
        text = msg.text.strip()

        # 暂停中：除了"恢复"指令，其他一律只记账不应答（当中转站用）
        if self._paused(conv.conv_id):
            if self._handle_pause(conv, text):
                return
            self._relay_log(conv, msg)
            self._last_user_text[conv.conv_id] = text
            return

        # 暂停/恢复指令（未暂停时也要能触发）
        if self._handle_pause(conv, text):
            return
        self._pace_note_inbound(conv.conv_id)
        if text:
            self._last_user_text[conv.conv_id] = text
        if msg.attachment:
            self._last_media[conv.conv_id] = time.time()
        text, triggered = self._apply_trigger(conv, text)

        # 图片要**在"没 @ 就丢弃"之前**处理：群里的图往往不 @ 它，
        # 但看图本身就应该算一次"有人在跟它说话"。
        vision_hint = None
        if msg.attachment:
            # 私聊里的真图片优先走"入库"流程；其余（群里图片、表情截屏、
            # 私聊里的表情截屏）一律先看图。之前把 owner 整体排除在外，
            # 导致私聊发无名字表情时它只会说"没显示出来"。
            is_import = (conv.conv_id == self.owner_conv and msg.kind != "emotion")
            if not is_import:
                vision_hint = self._look_at_image(conv, msg.attachment)
            if vision_hint and not triggered:
                vcfg = self.cfg.get("vision") or {}
                window = float(vcfg.get("window_minutes", 5)) * 60
                since = time.time() - float(self._last_addressed.get(conv.conv_id, 0))
                if (vcfg.get("auto_reply", True)
                        and conv.conv_id in (vcfg.get("auto_reply_convs") or [])
                        and since <= window):
                    triggered = True
                    self._log("vision_auto_reply", conv_id=conv.conv_id)
                elif since > window:
                    # 过了时效就默认不是在跟自己说话，等对方重新 @
                    self._log("vision_auto_reply_expired", conv_id=conv.conv_id,
                              since_addressed_s=int(since))

        # "@ 它 + 看一眼/什么都不说" → 翻聊天记录找最近那张图
        look_pending = None
        if (triggered and conv.conv_id in (self.cfg.get("look_back") or {}).get("convs", [])
                and self._is_look_request(text) and not msg.attachment):
            hint = self._look_back(conv, msg.sender)
            if hint:
                vision_hint = hint
            else:
                look_pending = msg.sender
                self._pending_look[conv.conv_id] = {
                    "sender": msg.sender,
                    "deadline": time.time() + 180,
                }
                self._say(conv.conv_id, "没翻到最近的图，你发过来我看看？")

        # 刚才说"等一下图"的人现在发图了 → 直接当她在叫我
        if msg.attachment:
            st = self._pending_look.get(conv.conv_id)
            if (st and time.time() < st.get("deadline", 0)
                    and (not st.get("sender") or st.get("sender") == msg.sender)):
                self._pending_look.pop(conv.conv_id, None)
                triggered = True
                self._log("look_pending_hit", conv_id=conv.conv_id)

        if triggered:
            self._last_addressed[conv.conv_id] = time.time()

        if not triggered:
            if self._proactive_try(conv, msg, text):
                return
            # 刚回过她的话之后的一小段窗口里，允许"接着说"而不必再 @
            if self._follow_should_handle(conv, text):
                triggered = True
            else:
                self._log("ignored_no_trigger", conv_id=conv.conv_id, text=text)
                return

        if not text:
            return

        # 本人私聊发来图片 → 走"存进表情库"流程
        # 只有真正的图片才走入库向导：表情截屏不该弹"起名字"
        if (conv.conv_id == self.owner_conv and msg.attachment
                and msg.kind != "emotion"):
            self._begin_sticker_import(Path(msg.attachment))
            return

        self._note_new_sender(conv, msg.sender)

        # 本人私聊里可以直接审批挂起的请求、也可以开关服务
        if conv.conv_id == self.owner_conv:
            if self._try_control(text):
                return
            if self._try_resolve_approval(text):
                return
            if self._pending_sticker and self._finish_sticker_import(text):
                return

        if self.paused and conv.conv_id != self.owner_conv:
            self._log("paused_drop", conv_id=conv.conv_id, text=text)
            return

        used = self._used_today(conv.conv_id)

        # 本人自己在群里说话（就算 @ 了）不算家人的用量：不记账、不受额度闸
        owner_speaking = conv.kind == "group" and self._is_owner_speaker(msg.sender)

        self._log("inbound", conv_id=conv.conv_id, sender=msg.sender, text=text)

        # 入口闸：家人问到隐私类话题，先问本人能不能答（本人的私聊不拦）
        if conv.conv_id != self.owner_conv and not owner_speaking:
            topics = privacy.question_is_sensitive(text)
            if topics:
                self._log("sensitive_question", conv_id=conv.conv_id,
                          topics=topics, text=text)
                self._queue_approval(
                    conv, kind="question", task=text,
                    reason="家人问了隐私类问题：" + "、".join(topics),
                )
                return

        # 额度用完：向本人申请加额（本人的私聊不设限额，只记账）
        if conv.conv_id != self.owner_conv and not owner_speaking and used >= self._budget(conv):
            self._request_quota(conv, used)
            return

        # 成本分级闸：只在跨过 60%、80% 这些档位时各问一次，不逐条唠叨
        if conv.conv_id != self.owner_conv and not owner_speaking:
            budget = self._budget(conv)
            needed = self._cost_stage_needed(used, budget)
            if needed >= 0:
                if self._cost_denied_get(conv.conv_id):
                    self._say(conv.conv_id, "本人说额度先不再花了，等他加额度或者明天再试。")
                    self._log("cost_denied_block", conv_id=conv.conv_id)
                    return
                if needed > self._cost_stage_get(conv.conv_id):
                    if any(p.get("conv_id") == conv.conv_id and p.get("kind") == "cost"
                           for p in self._state["pending"].values()):
                        self._say_with_pool(conv.conv_id, "已经在等本人确认了，稍等一下。",
                                        "ask")
                        self._log("cost_gate_dedup", conv_id=conv.conv_id)
                        return
                    thresholds = self.cfg.get("cost_gate_thresholds") or []
                    pct = int(float(thresholds[needed]) * 100)
                    ratio = used / budget if budget else 0
                    last = needed + 1 >= len(thresholds)
                    tail = ("再往上就真没额度了，建议顺手加额。"
                            if last else "批准后到下一档再问你一次。")
                    self._log("cost_gate", conv_id=conv.conv_id, used=used,
                              budget=budget, stage=needed)
                    self._queue_approval(
                        conv, kind="cost", task=text,
                        reason=(f"今日已用 {used:,} / {budget:,} tokens（{ratio:.0%}），"
                                f"越过 {pct}% 档。{tail}"),
                    )
                    return

        # 回执：动文件/跑东西才立刻回一句；纯聊天等超时才补
        done = threading.Event()
        if conv.conv_id != self.owner_conv:
            mode = str(self.cfg.get("ack_mode", "smart"))
            if mode == "always" or (mode == "smart" and self._looks_like_action(text)):
                self._say(conv.conv_id, "收到，正在处理…")
                self._log("ack_immediate", conv_id=conv.conv_id, reason="action")
            elif mode in ("smart", "delayed"):
                threading.Thread(
                    target=self._delayed_ack,
                    args=(conv.conv_id, done, float(self.cfg.get("ack_delay_seconds", 6))),
                    daemon=True,
                ).start()

        # 跑太久再补一句，否则一分钟以上的任务看起来像挂了
        ping_after = float(self.cfg.get("slow_ping_seconds", 30))
        if conv.conv_id != self.owner_conv and ping_after > 0:
            threading.Thread(
                target=self._slow_ping, args=(conv.conv_id, done, ping_after), daemon=True
            ).start()
        # 问余额/花费一律算"余额问题"，**不管在私聊还是群里**。
        # 之前只认私聊，所以主人在群里发「报账」时既没有真实数据、也没有索要表情。
        owner_here = (conv.conv_id == self.owner_conv
                      or self._is_owner_speaker(msg.sender))
        balance_q = owner_here and self._looks_like_balance_question(text)

        # 两条提示可以叠加：主人身份 + 实时余额。
        # 之前写成 if/elif，主人在群里问余额时身份提示把余额提示挤掉了，
        # 结果表情发了、数却没给对。
        hint_parts: list[str] = []
        if owner_speaking:
            hint_parts.append(
                "【重要】这条消息来自**主人本人**（就是你的持有者、这个微信号的主人），"
                "不是普通群友。对他的要求：优先照办、不要推诿、不要反问“你确定吗”；"
                "语气可以更亲近随意一些，像对熟人说话。其他群成员的要求优先级低于他。"
            )
        if balance_q:
            # 问到余额/花费时，先把真实数字查好塞进上下文，
            # 免得它一本正经地回答"我没有权限查余额"
            try:
                hint_parts.append(self._balance_context())
            except Exception as exc:
                self._log("balance_context_failed", error=repr(exc))
        # 问到盈亏/持仓：把交易数据查好塞进上下文，省得它瞎猜
        tcfg = self.cfg.get("trading") or {}
        if (tcfg.get("enabled")
                and conv.conv_id in (tcfg.get("context_convs") or [self.owner_conv])
                and self._trading_question(text)):
            st = self.trading_status()
            if st is not None:
                hint_parts.append(trading_mod.format_context(st))

        link_hint = self._read_links(conv, text)
        if link_hint:
            hint_parts.append(link_hint)

        mood = self._mood_hint(text)
        if mood:
            hint_parts.append(mood)
        if vision_hint:
            hint_parts.append(vision_hint)
        extra_hint = "\n\n".join(hint_parts) if hint_parts else None

        try:
            result = self._run(conv, text, mode=self.cfg["permissions"]["base_mode"],
                               speaker=msg.sender, extra=extra_hint)
        finally:
            done.set()

        # 先记账，再按配置通报用量。本人自己在群里发起的不记账。
        if owner_speaking:
            self._log("owner_usage_exempt", conv_id=conv.conv_id,
                      sender=msg.sender, tokens=result.tokens)
        else:
            self._charge(conv.conv_id, result.tokens)
            self._notify_usage(conv, msg.sender, result.tokens, result.elapsed_s)
            try:
                self._maybe_tattle(conv)
            except Exception as exc:
                self._log("tattle_failed", conv_id=conv.conv_id, error=repr(exc))

        # 空输出必须让用户知道，不能静默：之前 key 失效时 run 返回空文本，
        # 格式化层把空字符串当"没内容"直接吞掉，用户那边就是彻底的没反应。
        if not (result.text or "").strip():
            self._log("empty_reply", conv_id=conv.conv_id, ok=result.ok,
                      tokens=result.tokens, mode=result.mode)
            self._say(conv.conv_id, "这次没处理成功，稍后再发一次试试。")
            if conv.conv_id != self.owner_conv:
                self._say(self.owner_conv,
                          f"【异常】「{conv.name}」这次返回了空内容"
                          f"（ok={result.ok}），已回告家人。")
            return

        if result.denied:
            self._queue_approval(conv, kind="task", task=text,
                                 reason=result.text, session_id=result.session_id)
            return

        # 出口闸：回答里带密钥/证件号/手机号等，先扣住
        if conv.conv_id != self.owner_conv:
            findings = privacy.scan_output(result.text)
            if findings:
                self._log("output_held", conv_id=conv.conv_id,
                          categories=[f.category for f in findings])
                self._queue_approval(
                    conv, kind="draft", task=text,
                    reason="回答里有敏感内容：" + "、".join(f.category for f in findings),
                    draft=result.text, session_id=result.session_id,
                )
                return

        self._deliver(conv.conv_id, result.text)
        self._follow_open(conv, result.text)
        try:
            threading.Thread(
                target=self._auto_learn,
                args=(conv, msg.sender, text, result.text),
                daemon=True).start()
        except Exception:
            pass
        if balance_q:
            # 问余额 → 顺势伸个手
            self._send_pool_sticker(conv.conv_id, "ask")
        self._log("outbound", conv_id=conv.conv_id, text=result.text,
                  session_id=result.session_id, mode=result.mode,
                  tokens=result.tokens, elapsed_s=round(result.elapsed_s, 2))

    # ---------- 成本分级闸 ----------
    def _is_owner_speaker(self, sender: str) -> bool:
        """判断某个群里的发言人是不是本人（按配置的别名，或记忆里归到「主人」）。"""
        s = (sender or "").strip()
        if not s:
            return False
        names = [str(n).strip() for n in (self.cfg.get("owner_aliases") or []) if str(n).strip()]
        if s in names:
            return True
        try:
            return self.memories.resolve_any(s) == "主人"
        except Exception:
            return False

    def _cost_key(self, conv_id: str) -> str:
        return f"{date.today().isoformat()}|{conv_id}"

    def _cost_stage_get(self, conv_id: str) -> int:
        """本人已批准到的档位；-1 表示一档都没批。"""
        return int(self._state.get("cost_stage", {}).get(self._cost_key(conv_id), -1))

    def _cost_stage_set(self, conv_id: str, stage: int) -> None:
        with self._state_lock:
            self._state.setdefault("cost_stage", {})[self._cost_key(conv_id)] = stage
            self._save_state()

    def _cost_denied_get(self, conv_id: str) -> bool:
        return bool(self._state.get("cost_denied", {}).get(self._cost_key(conv_id)))

    def _cost_denied_set(self, conv_id: str, value: bool) -> None:
        with self._state_lock:
            self._state.setdefault("cost_denied", {})[self._cost_key(conv_id)] = value
            self._save_state()

    def _cost_stage_needed(self, used: int, budget: int) -> int:
        """按当前用量算出"本该批到哪一档"（-1 表示还没越过第一档）。"""
        thresholds = self.cfg.get("cost_gate_thresholds") or []
        if not thresholds or budget <= 0:
            return -1
        ratio = used / budget
        needed = -1
        for i, th in enumerate(thresholds):
            if ratio >= float(th):
                needed = i
        return needed

    def _note_new_sender(self, conv: Conversation, sender: str) -> None:
        """第一次见到某个发言人、又没记忆时提醒本人一次，方便随时建底账。"""
        if conv.kind != "group" or not sender:
            return
        seen = self._state.setdefault("seen_senders", {})
        if sender in seen:
            return
        seen[sender] = time.time()
        self._save_state()
        if self.memory.resolve(sender):
            return
        self._log("new_sender", conv_id=conv.conv_id, sender=sender)
        self._say(
            self.owner_conv,
            f"新发言人：「{sender}」在「{conv.name}」说话了。\n"
            f"要记住他就发「记住 {sender}：<他是谁>」；"
            f"如果是已认识的某人换了昵称，发「别名 {sender} <人名>」。",
        )

    # ---------- 内部 ----------
    def _apply_trigger(self, conv: Conversation, text: str) -> tuple[str, bool]:
        if conv.kind == "private" or conv.trigger == "always":
            return text.strip(_INVISIBLE), True
        for token in [self.bot_mention, *conv.aliases]:
            if token and token in text:
                # 微信 @ 后面常跟 U+2005（四分之一全角空格）等不可见字符，一并剥掉
                return text.replace(token, "", 1).strip(_INVISIBLE), True
        return text, False

    def _default_self_doc(self, config: dict) -> str:
        """首次运行时生成的自我说明，尽量从配置里取，保证和实际行为一致。"""
        convs = config.get("conversations") or []
        lines = []
        for c in convs:
            name = c.get("name") or c.get("conv_id")
            need = c.get("trigger") or c.get("triggers")
            if need and str(need).strip().lower() not in ("always", "*"):
                lines.append(f"- 「{name}」（群）：需要 @ 才回，触发词 {need}")
            else:
                lines.append(f"- 「{name}」（私聊）：任何消息都回")
        feats = ["读写文档、执行命令（需批准）", "记住/查询人物与事实"]
        if config.get("sticker_enabled", True):
            feats.append("发表情包")
        feats.append("把表格( csv/xlsx )渲染成多页长图直接发出来")
        if (config.get("image_gen") or {}).get("enabled"):
            feats.append("按描述现生成表情图")
        if (config.get("nudges") or {}).get("enabled"):
            feats.append("主动提醒（如深夜催睡）")
        if (config.get("balance_watch") or {}).get("enabled"):
            feats.append("查余额/花费、充值后主动告知")
        return (
            "我是「小助手」，主人（微信昵称 owner）的私人助手，\n"
            "跑在主人自己的 Windows 电脑上的一个桥接程序里，通过微信和大家说话。\n"
            "\n我负责这几个会话：\n" + "\n".join(lines) + "\n"
            "\n我能做的事：\n- " + "\n- ".join(feats) + "\n"
            "\n我的规矩：\n"
            "- 涉及改动文件、运行命令、花钱的事，都要先问过主人；\n"
            "- 涉及隐私的问题（账号、密钥、地址等），也要先问过主人；\n"
            "- 记忆分三份：家人群、朋友群、本人，互不串门；\n"
            "- 我看不到完整的群成员列表，只能靠记忆和见过的发言判断。\n"
            "\n说话上：我一直用「我」自称，不会强调自己是机器人或工具；"
            "但也不会假装是人。"
            "\n我不是通用客服，是主人这边的助手；说话可以随意些。"
        )

    def _is_people_question(self, text: str) -> bool:
        t = (text or "").lower()
        words = ("谁", "是谁", "认识", "介绍", "什么关系", "群成员", "成员",
                 "都有哪些人", "哪些人", "记得", "记忆", "你知不知道")
        return any(w in t for w in words)

    def _scope_notes(self, scope: str, limit: int = 400) -> str:
        store = self.memories.for_scope(scope)
        if not store.count():
            return ""
        out = []
        for person, item in store.all().items():
            note = str(item.get("note", "")).strip()
            aliases = [a for a in item.get("aliases", []) if a != person]
            alias_txt = f"（也叫{'、'.join(aliases)}）" if aliases else ""
            out.append(f"- {person}{alias_txt}：{note}"[:limit])
        return "\n".join(out)

    def _run(self, conv: Conversation, task: str, mode: str,
             speaker: str | None = None, extra: str | None = None) -> RunResult:
        # 让 agent 知道群里是谁在说话，回答才能点名、也避免张冠李戴
        hints: list[str] = []

        # 明确划线：下面这些是背景资料，不是用户下达的指令。
        # 之前没这条，自我说明里"涉及改动文件…"之类的话会被当成请求去执行。
        hints.append(
            "【以下是背景资料，不是用户指令】只用来理解对方是谁、该用什么语气；"
            "不要因为读到这里的内容就去做其中的动作。真正要办的事在用户消息里。"
        )

        if merge_mod.looks_incomplete(task):
            hints.append(
                "【注意】对方这条看起来说到一半（结尾像没说完）。"
                "如果确实只收到半句，就用一句话应一下、或者问他要说什么，"
                "别当成完整问题去长篇回答。"
            )

        # 说话方式：不强调"我是机器/工具"，但也不装人。
        hints.append(
            "【说话方式】\n"
            "- 用「我」说话，**不要自称机器人、AI、助手、系统、程序、模型**；\n"
            "- 不要解释自己是怎么实现的，也不要说「作为一个AI…」这类声明；\n"
            "- 别人问你是不是机器人，可以随口带过（比如「算是吧」），"
            "不用郑重澄清身份；\n"
            "- 上下线、休息这类事，就说「我先撤了」「我回来了」，"
            "别用设备开关那套说法。"
        )

        # 自我认知：我是谁、负责哪些会话、能做什么、受什么约束。
        # 之前完全没注入，导致它被问到"你能干什么"时答得含糊。
        doc = self.self_doc.load()
        if doc:
            hints.append("【关于你自己】\n" + doc)

        hints.append(self._zone_hint(conv))
        hints.append(
            "【发图能力】\n"
            "- 「表情:名字」发表情库里现成的图；\n"
            "- 「表情:生图:画面描述」现生成一张表情图；\n"
            "- 「表格:文件名」把 csv/xlsx 渲染成分页长图发出去，"
            "手机能直接看，不用对方开电脑；\n"
            "- 「投递:phd/文件名」把你做好的文件交给系统，由它放进 PhD 文件夹。"
            "你自己的沙箱写不了外面，所以要走这条。\n"
            "以上三种写在回复最后单独一行，把外面的「」换成英文方括号，"
            "系统就会替你发，这一行不会显示给对方。"
        )
        if extra:
            hints.append(extra)
        scope_mem = self.memories.for_scope(conv.memory_scope)

        if conv.kind == "group" and speaker:
            hints.append(
                f"【群聊】这条消息来自群成员「{speaker}」，回答时可以直接称呼对方；"
                "不确定的信息不要当成他说的。"
            )
            note = scope_mem.hint_for(speaker)
            if note:
                hints.append(note)

        # 名册：只给名字，不给别人的私人备注（群聊里念出来不合适）。
        # 只列**本会话所属范围**的人，家人群不会看到朋友群的名册，反之亦然。
        if scope_mem.count():
            names = "、".join(scope_mem.all().keys())
            hints.append(
                f"【我记着的人（{self.memories.label(conv.memory_scope)}）】{names}\n"
                "有人问群成员/都有谁/认识谁时，就按这个名单回答；"
                "同时说明你只能从记忆和见过的发言判断，看不到完整的群成员列表。"
                "不要念别人的私人备注。"
            )

        # 问到和人有关的事时，把这一份记忆的详细内容也给它——
        # 否则它只知道名字、不知道是谁，就会答"我不认识"。
        if self._is_people_question(task):
            notes = self._scope_notes(conv.memory_scope)
            if notes:
                hints.append(
                    "【记忆里的详细信息】\n" + notes + "\n"
                    "回答和这些人有关的问题时，**直接依据上面的内容**；"
                    "记忆里有的就照实说，不要回答“我不认识”“我不知道”。"
                    "只答对方问的那部分，不要整段念出别人的备注。"
                )
        if conv.style == "chat":
            # 关键：默认当闲聊/玩笑，只有出现明确"要正经详细"的字眼才当任务。
            # 之前只写了"默认短回复"，它照样会把"哈哈那你是不是傻"当正经问题解答。
            cm = self.cfg.get("chat_mode") or {}
            triggers = cm.get("serious_triggers") or []
            raw = (task or "")
            hit = next((t for t in triggers if t and t in raw), None)
            serious = bool(hit) or bool(cm.get("default_casual") is False)

            hints.append(
                "【这是朋友群，默认当闲聊/玩笑，不是提问】\n"
                "- 除非对方明确要求，否则**不要把它当成问题去解答**：跟着接梗、起哄、"
                "开玩笑回去就行；\n"
                "- 不要列点、不要给方案、不要总结、不要「首先其次最后」；\n"
                "- 宁可回得糙一点、短一点，也别一本正经地分析一个玩笑；\n"
                "- 拿不准是玩笑还是真问，就按玩笑处理。\n"
                f"- 只有出现这些字眼才算正经请求：{'、'.join(triggers) if triggers else '（未配置）'}。"
                "出现时才详细回答。\n"
                "- 有一种中间情况：确实是问题、但没要求多详细。这种先一两句话说个结论，"
                "然后问一句「要我展开说吗？」，别直接长篇。"
            )
            if serious:
                hints.append(
                    f"【本条判定：正经请求】因为出现了「{hit}」这类字眼，"
                    "这次按正常回答问题处理，可以给结构化的详细内容。"
                )
            else:
                hints.append(
                    "【本条判定：闲聊】没有出现要求详细说明的字眼，"
                    "按闲聊回，别做分析和方案。"
                    + ("如果对方其实想要详细答案，他会明确说要求展开。"
                       if cm.get("default_casual", True) else "")
                )
        if self.stickers and len(self.stickers):
            self.stickers.refresh()          # 新丢进来的图不用重启就能用
            hints.append(self.stickers.prompt_hint())
            hints.append(
                "【注意】表情包由系统管理，你不需要读取或写入 stickers 目录，"
                "也不要去翻文件——直接用「表情:名字」或「表情:生图:画面描述」即可。"
            )
        if self._image_allowed(conv.conv_id):
            hints.append(
                "【可生图】如果现成表情都不贴切，可以写 [表情:生图:画面描述] 让系统现场生成一张。"
                "描述控制在一句话内，用简笔/卡通风格，不要出现真人、真人明星或具体人名。"
            )
        if (conv.conv_id != self.owner_conv
                and self.cfg.get("reply_style") == "conclusion_first"):
            hints.append(_FORMAT_HINT.strip())
        if hints:
            task = "\n".join(hints) + "\n\n" + task

        sid = self._state["sessions"].get(conv.conv_id)
        result = self.runner.run(
            conv_id=conv.conv_id,
            task=task,
            workspace=self._workspace_for(conv),
            session_id=sid,
            mode=mode,
            timeout_s=int(self.cfg.get("timeout_s", 300)),
        )
        if result.session_id:
            self._state["sessions"][conv.conv_id] = result.session_id
        self._save_state()
        return result

    _HOLD_REASON = {
        "task": "这个操作需要本人批准，已经转过去了，稍等。",
        "question": "这个涉及隐私，我先问一下本人能不能告诉你。",
        "draft": "这条回答里有私人信息，我先问一下本人能不能发给你。",
        "cost": "额度有点紧了，我先问一下本人要不要继续处理。",
    }

    def _queue_approval(
        self,
        conv: Conversation,
        *,
        kind: str,
        task: str,
        reason: str,
        draft: str = "",
        session_id: Optional[str] = None,
    ) -> None:
        # 同一个会话已经有同类待审批 → 不再重复发卡，避免刷屏
        for exist in self._state.get("pending", {}).values():
            if (exist.get("conv_id") == conv.conv_id
                    and exist.get("kind") == kind):
                self._log("approval_dedup", conv_id=conv.conv_id, kind=kind)
                return

        pid = self._next_pid()
        pending = Pending(
            pid=pid,
            conv_id=conv.conv_id,
            task=task,
            session_id=session_id,
            created=time.time(),
            reason=reason,
            kind=kind,
            draft=draft,
        )
        self._state["pending"][pid] = pending.__dict__
        self._save_state()
        self._log("approval_queued", conv_id=conv.conv_id, pid=pid, kind=kind, task=task)

        self._say(conv.conv_id, self._HOLD_REASON.get(kind, self._HOLD_REASON["task"]))

        label = {"task": "操作", "question": "隐私问题", "draft": "待发回答",
                 "cost": "额度确认"}[kind]
        lines = [
            f"【待审批 {pid}】{label} · 来自「{conv.name}」",
            f"内容：{task[:300]}",
            f"原因：{reason[:300]}",
        ]
        if kind == "draft" and draft:
            lines.append(f"拟发内容：{draft[:400]}{'…' if len(draft) > 400 else ''}")
        lines.append(f"回复「同意 {pid}」或「拒绝 {pid}」")
        self._say(self.owner_conv, "\n".join(lines))

    # ---------- 主动接话 ----------
    _PROACTIVE_HINT = (
        "【主动接话】群里有人说了这句话，没有人 @ 你。你判断值得接，"
        "那就自然地、简短地接一句；不要长篇，不要客套，不要复述对方的话。"
    )

    def _proactive_on(self, conv: Conversation) -> bool:
        if not self._proactive.get("enabled"):
            return False
        if conv.kind != "group":
            return False
        return bool(self._proactive.get("groups", {}).get(conv.conv_id, False))

    def _in_quiet_hours(self) -> bool:
        start = int(self._proactive.get("quiet_start", 23))
        end = int(self._proactive.get("quiet_end", 8))
        if start == end:
            return False
        hour = datetime.now().hour
        return (start <= hour < end) if start < end else (hour >= start or hour < end)

    def _proactive_quota_ok(self, conv: Conversation) -> tuple[bool, str]:
        now = time.time()
        key = f"{date.today().isoformat()}|{conv.conv_id}"
        hist = [t for t in self._state.setdefault("proactive", {}).get(key, [])
                if now - t < 3600]
        self._state["proactive"][key] = hist
        if len(hist) >= int(self._proactive.get("per_hour", 3)):
            return False, f"已达每小时 {len(hist)} 次上限"
        cooldown = float(self._proactive.get("cooldown_seconds", 600))
        if hist and now - hist[-1] < cooldown:
            return False, f"冷却中（{int(cooldown - (now - hist[-1]))}s）"
        return True, ""

    def _proactive_record(self, conv: Conversation) -> None:
        key = f"{date.today().isoformat()}|{conv.conv_id}"
        with self._state_lock:
            self._state.setdefault("proactive", {}).setdefault(key, []).append(time.time())
            self._save_state()

    # ---------- 主动接话的节奏 ----------
    # ---------- 看图 ----------
    def _look_at_image(self, conv: Conversation, path: str) -> str | None:
        """把图片转成文字描述。没有配置/超限/失败都返回 None。"""
        if self._is_throttled(conv.conv_id):
            self._log("vision_skipped", conv_id=conv.conv_id, reason="throttled")
            return None
        vcfg = self.cfg.get("vision") or {}
        if not vcfg.get("enabled"):
            return None
        if conv.conv_id not in (vcfg.get("convs") or []):
            return None

        key = date.today().isoformat()
        used = int((self._state.get("vision") or {}).get(key, 0))
        limit = int(vcfg.get("daily_limit", 30))
        if used >= limit:
            self._log("vision_skipped", reason="daily_limit", used=used)
            return None

        try:
            import keystore

            api_key = keystore.load().get("OPENAI_API_KEY", "")
        except Exception:
            api_key = os.environ.get("OPENAI_API_KEY", "")

        text = vision_mod.describe(
            path, api_key=api_key,
            model=str(vcfg.get("model", "gpt-4o-mini")),
            max_px=int(vcfg.get("max_px", 768)))
        if not text:
            self._log("vision_failed", conv_id=conv.conv_id)
            return None

        with self._state_lock:
            self._state.setdefault("vision", {})[key] = used + 1
            self._save_state()
        self._log("vision_described", conv_id=conv.conv_id, chars=len(text),
                  used_today=used + 1)
        return (
            f"【图片内容（刚看的）】{text}\n"
            "这是对方刚发的东西，**把它当成对话的一部分接着说下去**："
            "顺着图片的内容回应、接话、追问，或者接着刚才的话题往下聊。\n"
            "不要停留在评价图片本身（别说「这图挺好看」「这表情挺帅」这种），"
            "也不要说你看不到图。"
        )

    # ---------- 记忆自更新 ----------
    def _auto_learn(self, conv: Conversation, speaker: str, user_text: str,
                    reply: str) -> None:
        """后台从这轮对话里捡值得记的事实。失败静默。"""
        cfg = self.cfg.get("memory_auto") or {}
        if not cfg.get("enabled"):
            return
        if conv.conv_id not in (cfg.get("convs") or []):
            return

        scope = conv.memory_scope
        today = date.today().isoformat()
        with self._state_lock:
            budget = self._state.setdefault("memory_auto", {})
            used = int(budget.get(f"{today}|{scope}", 0))
            if used >= int(cfg.get("daily_limit", 5)):
                return

        dialogue = f"{speaker}：{user_text}\n小助手：{reply}"
        try:
            facts = memory_auto_mod.extract(dialogue, api_key=self._env_api_key())
        except Exception:
            return
        if not facts:
            return

        store = self.memories.for_scope(scope)
        added = 0
        for person, fact in facts:
            existing = store.all().get(person) or {}
            if fact and fact in str(existing.get("note", "")):
                continue
            store.set(person, fact, auto=True)
            added += 1
            self._log("memory_auto_learned", scope=scope, person=person, fact=fact)
        if added:
            with self._state_lock:
                budget[f"{today}|{scope}"] = used + added
                self._save_state()

    # ---------- 情绪感知（轻量规则版） ----------
    _MOOD_WORDS = (
        "累", "烦", "难受", "崩溃", "不想", "emo", "郁闷", "焦虑", "失眠",
        "压抑", "撑不住", "好烦", "心累", "想哭", "委屈", "生气", "火大",
    )

    def _mood_hint(self, text: str) -> str | None:
        cfg = self.cfg.get("mood") or {}
        if not cfg.get("enabled", True):
            return None
        words = cfg.get("keywords") or self._MOOD_WORDS
        t = (text or "").lower()
        hit = next((w for w in words if w and w.lower() in t), None)
        if not hit:
            return None
        self._log("mood_detected", word=hit, text=text[:60])
        return (
            "【语气提示】对方这句话情绪不太好。先接住情绪（表示理解、别否定他的感受），"
            "别急着给建议、讲道理或列方案；语气放轻一点。"
        )

    def _pick_sticker_by_mood(self, pool: str, conv: Conversation, *,
                              reply_text: str, user_text: str,
                              exclude: str | None = None) -> str | None:
        """按对话情绪从池子里挑一张。失败返回 None（调用方退回随机）。"""
        pcfg = self.cfg.get("sticker_pick") or {}
        if not pcfg.get("enabled", True):
            return None
        if self.stickers is None:
            return None

        pools = self.cfg.get("sticker_pools") or {}
        names = list(pools.get(pool) or [])
        if not names and pool == "generic":
            names = self.stickers.basic_names()
        cands = [n for n in self.stickers.pool(names) if n != exclude]
        if len(cands) < 2:
            return None          # 只有一张就没得挑

        chosen = sticker_pick_mod.choose(
            reply_text, user_text, cands,
            api_key=self._env_api_key(),
            model=str(pcfg.get("model", "deepseek-flash")))
        # 防御：模型可能答一个不在候选里的名字，不能直接拿去发
        if chosen not in cands:
            self._log("sticker_pick_fallback", conv_id=conv.conv_id, pool=pool,
                      reason="not_in_candidates", got=str(chosen)[:30])
            return None
        self._log("sticker_picked", conv_id=conv.conv_id, pool=pool, name=chosen)
        return chosen

    # ---------- "看一眼"唤醒 ----------
    _LOOK_RE = re.compile(
        r"(看看|看一下|看一眼|瞧瞧|瞅瞅|这是啥|这是什么|什么意思|"
        r"图片|图|表情|上面|刚才|那张|这张|上图)")

    def _is_look_request(self, text: str) -> bool:
        t = (text or "").strip()
        if not t:
            return True                       # 只 @ 了一下，什么都没说
        if len(t) > 24:
            return False
        return bool(self._LOOK_RE.search(t))

    def _look_back(self, conv: Conversation, sender: str) -> str | None:
        """回看这个人在本会话里最近发的一张图/表情，转成描述。

        只认 `look_back.max_age_minutes` 以内的图：太久以前的图多半跟现在的话题
        无关，硬翻出来答非所问更糟。
        """
        lcfg = self.cfg.get("look_back") or {}
        max_age = float(lcfg.get("max_age_minutes", 5)) * 60
        last = float(self._last_media.get(conv.conv_id, 0))
        if not last or (time.time() - last) > max_age:
            self._log("look_back_too_old", conv_id=conv.conv_id,
                      age_min=round((time.time() - last) / 60, 1) if last else None)
            return None

        fetch = getattr(self.adapter, "fetch_recent_media", None)
        if fetch is None:
            return None
        try:
            got = fetch(conv.conv_id, sender)
        except Exception as exc:
            self._log("look_back_failed", conv_id=conv.conv_id, error=repr(exc))
            return None
        if not got:
            return None
        kind, value = got
        if kind == "caption":
            return (f"【图片内容（刚从聊天记录里翻到的）】对方发的表情是「{value}」。"
                    "把它当成对话的一部分接着聊：回应这个表情背后的意思，"
                    "或者顺着刚才的话题继续，不要只评价这个表情。")
        self._log("look_back_found", conv_id=conv.conv_id, sender=sender)
        return self._look_at_image(conv, value)

    # ---------- 额度紧张时的静默 ----------
    def _throttle(self, conv_id: str, reason: str) -> None:
        st = self._throttled.setdefault(conv_id, {})
        st["reason"] = reason
        st["since"] = st.get("since") or time.time()
        self._log("conv_throttled", conv_id=conv_id, reason=reason)

    def _is_throttled(self, conv_id: str) -> bool:
        return conv_id in self._throttled

    def _unthrottle(self, conv_id: str) -> None:
        if self._throttled.pop(conv_id, None):
            self._log("conv_unthrottled", conv_id=conv_id)

    # ---------- 连发合并 ----------
    _SKIP_MERGE = (
        "同意", "拒绝", "批准", "不行", "记住", "忘掉", "别名", "加额",
        "状态", "用量", "报账", "余额", "备份", "存档", "推送", "历史",
        "暂停", "恢复", "主动", "开启", "停止", "关闭",
    )

    def enqueue(self, msg: Inbound) -> None:
        """消息入口：短消息先等等，看对方是不是还要接着说。"""
        cfg = self.cfg.get("coalesce") or {}
        text = (msg.text or "").strip()
        skip = (not cfg.get("enabled", True)
                or (cfg.get("skip_commands", True)
                    and text.startswith(self._SKIP_MERGE)))

        if skip:
            self._dispatch(msg)
            return

        now = time.time()
        with self._merge_lock:
            buf = self._merge_buf.get(msg.conv_id)
            if buf and buf["sender"] == msg.sender and now <= buf["deadline"]:
                buf["texts"].append(msg.text)
                if msg.attachment:
                    buf["attachment"] = msg.attachment
                    buf["kind"] = msg.kind
                buf["msg"] = msg
                buf["deadline"] = self._merge_deadline(buf, now)
                self._log("merge_extended", conv_id=msg.conv_id,
                          parts=len(buf["texts"]))
            else:
                if buf:
                    self._flush_one(msg.conv_id)
                self._merge_buf[msg.conv_id] = {
                    "first": now,
                    "deadline": 0.0,
                    "sender": msg.sender,
                    "texts": [msg.text],
                    "attachment": msg.attachment,
                    "kind": msg.kind,
                    "msg": msg,
                }
                self._merge_buf[msg.conv_id]["deadline"] = self._merge_deadline(
                    self._merge_buf[msg.conv_id], now)

        if self._merge_thread is None:
            self._merge_thread = threading.Thread(
                target=self._merge_loop, daemon=True, name="merge")
            self._merge_thread.start()

    def _merge_deadline(self, buf: dict, now: float) -> float:
        cfg = self.cfg.get("coalesce") or {}
        wait = float(cfg.get("wait_seconds", 2.0))
        extra = float(cfg.get("incomplete_extra_seconds", 3.0))
        cap = float(cfg.get("max_wait_seconds", 8.0))
        joined = "\n".join(buf.get("texts") or [])
        budget = wait + (extra if merge_mod.looks_incomplete(joined) else 0.0)
        return min(buf.get("first", now) + cap, now + budget)

    def _merge_loop(self) -> None:
        while True:
            time.sleep(0.15)
            due = []
            now = time.time()
            with self._merge_lock:
                for conv_id, buf in list(self._merge_buf.items()):
                    if now >= buf["deadline"]:
                        due.append(conv_id)
                for conv_id in due:
                    self._flush_one(conv_id)

    def _flush_one(self, conv_id: str) -> None:
        """把攒着的几段合并成一条交给处理流程。调用方需持锁或确保独占。"""
        buf = self._merge_buf.pop(conv_id, None)
        if not buf:
            return
        parts = [t for t in buf["texts"] if (t or "").strip()]
        merged_text = "\n".join(parts)
        if len(parts) > 1:
            self._log("merge_flushed", conv_id=conv_id, parts=len(parts),
                      chars=len(merged_text))
        new_msg = Inbound(conv_id, buf["sender"], merged_text,
                          attachment=buf.get("attachment"),
                          kind=buf.get("kind") or "")
        self._dispatch(new_msg)

    def _dispatch(self, msg: Inbound) -> None:
        if self._dispatch_pool is None:
            from concurrent.futures import ThreadPoolExecutor

            self._dispatch_pool = ThreadPoolExecutor(
                max_workers=int(self.cfg.get("workers", 3)),
                thread_name_prefix="handle")

        def _run():
            try:
                self.handle(msg)
            except Exception:
                import traceback

                traceback.print_exc()

        self._dispatch_pool.submit(_run)

    # ---------- 模拟交易日报 ----------
    def _trading_path(self) -> Path | None:
        tcfg = self.cfg.get("trading") or {}
        raw = tcfg.get("status_file")
        if not raw:
            return None
        p = Path(os.path.expandvars(str(raw))).expanduser()
        return p if p.is_absolute() else (self.backups.source / p)

    def trading_status(self):
        path = self._trading_path()
        if path is None:
            return None
        return trading_mod.load(path)

    def trading_report(self, *, detail: bool = False) -> str | None:
        st = self.trading_status()
        if st is None:
            return None
        self._log("trading_report", ok=st.ok, date=st.trade_date,
                  reason=st.reason[:60] if not st.ok else "")
        return trading_mod.format_report(st, detail=detail)

    # ---------- 交易：配表情 / 邀功诉苦 / 回写判断 ----------
    def _mood_candidates(self, mood: str, pool: str = "generic",
                         exclude: str | None = None) -> list[str]:
        """按情绪筛候选。

        亏损/跑输时必须排除"开心类"表情：模型挑不出来时我们会随机兜底，
        随机池里若混着笑脸，就会出现"亏了钱发开心"的尴尬。
        """
        if self.stickers is None:
            return []
        pools = self.cfg.get("sticker_pools") or {}

        # 优先用"情绪专用池"（happy / sad / neutral），维护起来最直观
        if mood in ("happy", "sad", "neutral"):
            names = list(pools.get(mood) or [])
            cands = [n for n in self.stickers.pool(names) if n != exclude]
            if cands:
                return cands

        names = list(pools.get(pool) or [])
        if not names and pool == "generic":
            names = self.stickers.basic_names()
        cands = [n for n in self.stickers.pool(names) if n != exclude]

        # 没有专用池时退一步：低落情况剔掉"开心类"
        cheerful = set((self.cfg.get("sticker_moods") or {}).get("cheerful") or [])
        if mood == "sad" and cheerful:
            filtered = [n for n in cands if n not in cheerful]
            if filtered:
                return filtered
            # 当前池子全被排除了：去全库找非开心类的
            # 注意不能退回未过滤的 cands，否则"亏钱发笑脸"的 bug 会重现
            whole = [n for n in self.stickers.pool(self.stickers.names())
                     if n not in cheerful and n != exclude]
            return whole
        return cands

    def _send_mood_sticker(self, conv_id: str, text: str,
                           mood: str = "") -> str | None:
        """按这段话的情绪挑一张表情发出去。汇报必配一张。"""
        if self.stickers is None:
            return None
        conv = self.convs.get(conv_id)
        exclude = self._last_fallback.get(conv_id)
        cands = self._mood_candidates(mood, "generic", exclude)
        if not cands:
            # 为了"别连着用同一张"把候选排空了，那就允许重复——
            # 汇报必须配一张，宁可重复也不能没有，更不能退回笑脸。
            cands = self._mood_candidates(mood, "generic")

        name = None
        if conv is not None and len(cands) >= 2:
            pcfg = self.cfg.get("sticker_pick") or {}
            if pcfg.get("enabled", True):
                hint = {"sad": "（今天是亏损或跑输，语气低落）",
                        "happy": "（今天赚钱或跑赢，心情不错）"}.get(mood, "")
                chosen = sticker_pick_mod.choose(
                    text + hint, "", cands,
                    api_key=self._env_api_key(),
                    model=str(pcfg.get("model", "deepseek-flash")))
                if chosen in cands:
                    name = chosen
                    self._log("mood_sticker_picked", conv_id=conv_id,
                              mood=mood, name=name)
                elif chosen:
                    self._log("mood_sticker_rejected", conv_id=conv_id,
                              mood=mood, got=str(chosen)[:20])

        if not name:
            import random

            name = random.choice(cands) if cands else None
            if name:
                self._log("mood_sticker_random", conv_id=conv_id,
                          mood=mood, name=name)
        if not name:
            self._log("mood_sticker_missing", conv_id=conv_id, mood=mood)
            return None

        self._last_fallback[conv_id] = name
        self._deliver(conv_id, f"[表情:{name}]")
        return name

    def trading_writeback(self, st, journal: str, sticker: str | None,
                          full: str = "") -> bool:
        """把这次的判断写回 Qlib 项目，供它参考/对比。"""
        tcfg = self.cfg.get("trading") or {}
        raw = tcfg.get("journal_out")
        if not raw:
            return False
        target = Path(os.path.expandvars(str(raw))).expanduser()
        if not target.is_absolute():
            target = self.backups.source / target
        payload = {
            "schema_version": 1,
            "written_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "trade_date": getattr(st, "trade_date", ""),
            "mode": getattr(st, "mode", ""),
            "currency": getattr(st, "currency", ""),
            "mood": trading_mod.mood_of(st) if st is not None else "",
            "journal": journal or "",
            "journal_full": full or "",
            "sticker": sticker or "",
            "facts": trading_mod.journal_facts(st) if st is not None else "",
            "numbers": {
                "day_pnl": getattr(st, "day_pnl", 0.0),
                "day_pct": getattr(st, "day_pct", 0.0),
                "total_pct": getattr(st, "total_pct", 0.0),
                "equity": getattr(st, "equity_total", 0.0),
                "exposure": (getattr(st, "market_value", 0.0)
                             / getattr(st, "equity_total", 1.0)
                             if getattr(st, "equity_total", 0) else 0.0),
                "turnover_pct": trading_mod.turnover_pct(st) if st is not None else 0.0,
                "positions": len(getattr(st, "positions", []) or []),
                "trades_today": getattr(st, "trades_today", 0),
            },
            "source": "dsh-bridge",
        }
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_suffix(target.suffix + ".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, target)
        except Exception as exc:
            self._log("trading_writeback_failed", error=repr(exc))
            return False
        self._log("trading_writeback", file=str(target), chars=len(journal or ""))
        return True

    def trading_say(self, conv_id: str, text: str,
                    mood: str = "") -> str | None:
        """发一段交易汇报，并必配一张对应情绪的表情。返回选中的表情名。"""
        self._deliver(conv_id, text)
        return self._send_mood_sticker(conv_id, text, mood=mood or "")

    def push_trading_highlights(self) -> int:
        """有值得单独说的（避开大跌/错过大涨）就主动来邀功或诉苦。"""
        tcfg = self.cfg.get("trading") or {}
        if not tcfg.get("enabled") or not tcfg.get("highlights", True):
            return 0
        st = self.trading_status()
        if st is None or not st.ok or not st.highlights:
            return 0

        today = date.today().isoformat()
        sent_map = self._state.setdefault("trading_highlights", {})
        done = set(sent_map.get(today) or [])
        count = 0
        for item in st.highlights[:3]:
            key = f"{item.get('type','')}|{item.get('symbol','')}"
            if key in done:
                continue
            text = trading_mod.write_highlight(
                st, item, api_key=self._env_api_key(),
                model=str(tcfg.get("journal_model", "deepseek-flash")))
            if not text:
                continue
            kind = str(item.get("type") or "")
            mood = "happy" if kind in ("dodged_loss", "big_win") else "sad"
            self.trading_say(self.owner_conv, text, mood=mood)
            done.add(key)
            count += 1
            self._log("trading_highlight_sent", kind=item.get("type"),
                      symbol=item.get("symbol"))
        sent_map[today] = sorted(done)
        if count:
            with self._state_lock:
                self._save_state()
        return count

    # ---------- 交易：两级汇报 ----------
    def _journals(self) -> dict:
        return self._state.setdefault("trading_journals", {})

    def _store_journals(self, trade_date: str, brief: str, full: str) -> None:
        if not trade_date:
            return
        store = self._journals()
        store[trade_date] = {
            "brief": brief or "",
            "full": full or "",
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        }
        # 只留最近 10 天，别把 state 撑大
        for old in sorted(store)[:-10]:
            store.pop(old, None)
        with self._state_lock:
            self._save_state()

    def _cached_journal(self, kind: str) -> str:
        """取当前交易日已生成好的那版（brief / full）。"""
        st = self.trading_status()
        if st is None or not st.ok:
            return ""
        entry = self._journals().get(st.trade_date) or {}
        return str(entry.get(kind) or "")

    def trading_brief(self, *, generate: bool = True) -> str | None:
        """短版：一段事实 + 一段心情判断。优先用提前生成的。"""
        tcfg = self.cfg.get("trading") or {}
        st = self.trading_status()
        if st is None:
            return None
        if not st.ok:
            return trading_mod.format_report(st)
        if str(tcfg.get("style", "journal")) == "data":
            return trading_mod.format_report(st)

        cached = self._cached_journal("brief")
        if cached:
            return cached
        if not generate:
            return ""
        text = trading_mod.write_brief(
            st, api_key=self._env_api_key(),
            model=str(tcfg.get("journal_model", "deepseek-flash")))
        if not text:
            self._log("trading_brief_fallback")
            return trading_mod.format_report(st)
        self._store_journals(st.trade_date, text, self._cached_journal("full"))
        return text

    def trading_detail(self, *, generate: bool = True) -> str | None:
        """详细版：带个股分析。提前生成好，等主人问再发。"""
        tcfg = self.cfg.get("trading") or {}
        st = self.trading_status()
        if st is None:
            return None
        if not st.ok:
            return trading_mod.format_report(st, detail=True)

        cached = self._cached_journal("full")
        if cached:
            return cached
        if not generate:
            return ""
        text = trading_mod.write_journal(
            st, api_key=self._env_api_key(),
            model=str(tcfg.get("journal_model", "deepseek-flash")))
        if not text:
            return trading_mod.format_report(st, detail=True)
        self._store_journals(st.trade_date, self._cached_journal("brief"), text)
        return text

    def trading_journal(self) -> str | None:
        """以小助手口吻写一段投资笔记。失败回退到数据版。"""
        tcfg = self.cfg.get("trading") or {}
        st = self.trading_status()
        if st is None:
            return None
        if not st.ok:
            return trading_mod.format_report(st)
        if str(tcfg.get("style", "journal")) == "data":
            return trading_mod.format_report(st)

        text = trading_mod.write_journal(
            st, api_key=self._env_api_key(),
            model=str(tcfg.get("journal_model", "deepseek-flash")))
        if text:
            self._log("trading_journal", date=st.trade_date, chars=len(text))
            return text
        self._log("trading_journal_fallback", reason="模型没返回")
        return trading_mod.format_report(st)

    def trading_data_version(self) -> str:
        """数据版本：交易日 + 更新时间。变了就说明脚本又跑了一次。"""
        st = self.trading_status()
        if st is None or not st.ok:
            return ""
        return f"{st.trade_date}|{st.updated_at}"

    def trading_already_pushed(self) -> bool:
        last = self._state.get("trading_last_push") or {}
        if not last.get("version") or last["version"] != self.trading_data_version():
            return False
        return not last.get("partial", False)

    def _mark_trading_pushed(self, delivered=None, all_targets=None) -> None:
        delivered = set(delivered or [])
        wanted = set(all_targets or delivered)
        with self._state_lock:
            self._state["trading_last_push"] = {
                "version": self.trading_data_version(),
                "targets": sorted(delivered),
                "partial": bool(wanted - delivered),
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
            self._save_state()

    def push_trading_daily(self) -> bool:
        """收盘后把日报发到私聊和朋友群。读不到就如实说读不到。"""
        tcfg = self.cfg.get("trading") or {}
        if not tcfg.get("enabled"):
            return False
        # 同一份数据、同一个目标只发一次；漏掉的目标下一轮补发
        last = self._state.get("trading_last_push") or {}
        version = self.trading_data_version()
        if last.get("version") == version and not last.get("partial"):
            self._log("trading_already_pushed")
            return False
        already = set(last.get("targets") or []) if last.get("version") == version else set()
        st = self.trading_status()
        if st is None:
            return False
        if not st.ok:
            # 没数据就不发：宁可沉默，也别往群里丢一句"没读到"
            self._log("trading_daily_no_data", reason=st.reason[:80])
            return False
        if getattr(st, "stale", False):
            # 数据比上一个交易日还旧：说明脚本没跑，按"没读到"处理
            self._log("trading_daily_stale", date=st.trade_date,
                      days=st.stale_days)
            return False
        text = self.trading_brief() or trading_mod.format_report(st)
        if not text:
            return False
        # 详细版提前生成好，等主人问的时候直接发，不用再等模型
        try:
            self.trading_detail()
        except Exception:
            pass
        targets = list(tcfg.get("targets") or [self.owner_conv])
        sent = False
        sticker = None
        mood = trading_mod.mood_of(st)
        delivered = set(already)
        for target in targets:
            if target not in self.convs:
                continue
            if target in already:
                continue
            # 每个目标独立兜住异常：一个群发不出去，不能让另一个群也收不到
            try:
                sticker = self.trading_say(target, text, mood=mood) or sticker
                sent = True
                delivered.add(target)
            except Exception as exc:
                self._log("trading_send_failed", conv_id=target, error=repr(exc))
        if sent:
            self.trading_writeback(st, text, sticker,
                                   full=self._cached_journal("full"))
            self._mark_trading_pushed(delivered=delivered,
                                      all_targets=[t for t in targets if t in self.convs])
            self._log("trading_daily_sent", targets=sorted(delivered), sticker=sticker)
        return sent

    def _trading_question(self, text: str) -> bool:
        t = (text or "").lower()
        words = ("盈亏", "股票", "持仓", "交易", "赚了", "亏了", "大盘",
                 "收益", "回撤", "仓位", "qlib")
        return any(w in t for w in words)

    # ---------- 读链接 ----------
    def _read_links(self, conv: Conversation, text: str) -> str | None:
        cfg = self.cfg.get("web_read") or {}
        if not cfg.get("enabled") or conv.conv_id not in (cfg.get("convs") or []):
            return None
        urls = webread_mod.find_urls(text, limit=int(cfg.get("max_links", 2)))
        if not urls:
            return None

        budget = int(cfg.get("max_chars", 3000))
        parts = []
        for url in urls[:int(cfg.get("max_links", 2))]:
            page = webread_mod.fetch(
                url, max_chars=max(200, budget // len(urls)),
                timeout=float(cfg.get("timeout_s", 20)))
            if page.ok:
                parts.append(f"- {page.title or url}\n{page.text}")
                self._log("web_read", conv_id=conv.conv_id, ok=True, url=url[:80],
                          chars=len(page.text))
            else:
                parts.append(f"- {url}（没读到：{page.reason}）")
                self._log("web_read", conv_id=conv.conv_id, ok=False, url=url[:80],
                          reason=page.reason)
        if not parts:
            return None
        return ("【链接内容（刚抓的，图片已忽略）】\n" + "\n\n".join(parts)
                + "\n回答时可以直接引用里面的内容。")

    # ---------- 文件投递 ----------
    def _deliver_file(self, conv_id: str, spec: str) -> None:
        """把 agent 做好的文件放进指定目录（例如 PhD 文件夹）。

        为什么不让 agent 直接写：它跑在沙箱里，只能写自己的工作区。
        桥接进程本身没有这个限制，所以由它来搬——路径必须落在配置的
        `deliver_targets` 里，且不允许 `..` 逃逸。
        """
        dcfg = self.cfg.get("deliver") or {}
        if not dcfg.get("enabled", True):
            self._say(conv_id, "投递功能没开。")
            return

        targets = dcfg.get("targets") or {}
        spec = (spec or "").replace("\\", "/").strip()
        if "/" in spec:
            key, _, rel = spec.partition("/")
        else:
            key, rel = "", spec
        rel = rel or spec
        if key in targets:
            base = Path(os.path.expandvars(str(targets[key]))).expanduser()
        elif len(targets) == 1:
            base, rel = Path(next(iter(targets.values()))), spec
        else:
            self._say(conv_id, f"不认识的投递目标「{key or spec}」，可选：{'、'.join(targets)}")
            return

        if ".." in Path(rel).parts:
            self._say(conv_id, "路径里不能有 ..")
            return

        src = self._resolve_deliver_source(conv_id, rel)
        if src is None:
            self._say(conv_id, f"没找到要投递的文件：{rel}")
            self._log("deliver_missing", conv_id=conv_id, rel=rel)
            return

        dest = base / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if src.resolve() != dest.resolve():
                shutil.copy2(src, dest)
        except Exception as exc:
            self._say(conv_id, f"投递失败：{exc!r}")
            self._log("deliver_failed", conv_id=conv_id, rel=rel, error=repr(exc))
            return
        self._log("delivered", conv_id=conv_id, src=str(src), dest=str(dest))
        self._say(conv_id, f"已经放进去了：{dest}")

    def _resolve_deliver_source(self, conv_id: str, rel: str) -> Path | None:
        cand = Path(rel)
        if cand.is_absolute() and cand.exists():
            return cand
        conv = self.convs.get(conv_id)
        roots = []
        if conv is not None:
            roots.append(self._workspace_for(conv))
        roots.extend([self.family_root, self.backups.source])
        for root in roots:
            p = root / rel
            if p.exists():
                return p
        name = Path(rel).name
        for root in roots:
            hits = list(root.rglob(name))[:1]
            if hits:
                return hits[0]
        return None

    def _pace_state(self, conv_id: str) -> dict:
        return self._pace.setdefault(
            conv_id, {"last_bot": 0.0, "ignored": 0, "paused_until": 0.0,
                      "awaiting_reply": False})

    def _pace_note_inbound(self, conv_id: str) -> None:
        """有真人说话了：如果上一条是它主动插的，说明接住了，清零。"""
        st = self._pace.get(conv_id)
        if st and st.get("awaiting_reply"):
            st["awaiting_reply"] = False
            st["ignored"] = 0
            self._log("proactive_engaged", conv_id=conv_id)

    def _pace_allow(self, conv: Conversation) -> tuple[bool, str]:
        pcfg = self._proactive or {}
        st = self._pace_state(conv.conv_id)
        now = time.time()

        if now < float(st.get("paused_until", 0)):
            return False, "cooling_down"
        # 别刚说完又插一句
        min_gap = float(pcfg.get("min_gap_seconds", 180))
        if now - float(st.get("last_bot", 0)) < min_gap:
            return False, "too_soon"
        # 连续被无视就收手
        limit = int(pcfg.get("ignored_limit", 3))
        if int(st.get("ignored", 0)) >= limit:
            st["paused_until"] = now + float(pcfg.get("cooldown_after_ignored", 7200))
            st["ignored"] = 0
            self._log("proactive_silenced", conv_id=conv.conv_id,
                      pause_hours=float(pcfg.get("cooldown_after_ignored", 7200)) / 3600)
            return False, "ignored_too_many"
        return True, ""

    def _pace_record_take(self, conv_id: str) -> None:
        st = self._pace_state(conv_id)
        if st.get("awaiting_reply"):
            # 上一次主动插话没人接
            st["ignored"] = int(st.get("ignored", 0)) + 1
        st["last_bot"] = time.time()
        st["awaiting_reply"] = True

    def _proactive_try(self, conv: Conversation, msg: Inbound, text: str) -> bool:
        """返回 True 表示已经插话（调用方不要再记 ignored）。"""
        if not self._proactive_on(conv) or self.paused:
            return False
        if self._is_throttled(conv.conv_id):
            self._log("proactive_skipped", conv_id=conv.conv_id, reason="throttled")
            return False
        if self._in_quiet_hours():
            self._log("proactive_skipped", conv_id=conv.conv_id, reason="quiet_hours")
            return False
        ok, why = self._proactive_quota_ok(conv)
        if not ok:
            self._log("proactive_skipped", conv_id=conv.conv_id, reason=why)
            return False
        ok, why = self._pace_allow(conv)
        if not ok:
            self._log("proactive_skipped", conv_id=conv.conv_id, reason=why)
            return False
        if privacy.question_is_sensitive(text):
            self._log("proactive_skipped", conv_id=conv.conv_id, reason="sensitive_topic")
            return False
        if self._used_today(conv.conv_id) >= self._budget(conv):
            return False

        ok, why = proactive.rule_should_consider(text)
        if not ok:
            self._log("proactive_skipped", conv_id=conv.conv_id, reason=f"rule:{why}")
            return False

        verdict = proactive.model_review(
            text,
            speaker=msg.sender,
            group_name=conv.name,
            api_key=os.environ.get("DEEPSEEK_API_KEY", ""),
            model=str(self._proactive.get("review_model", "deepseek-chat")),
        )
        if not verdict.take:
            self._log("proactive_skipped", conv_id=conv.conv_id,
                      reason=f"model:{verdict.reason}")
            return False

        self._log("proactive_take", conv_id=conv.conv_id, sender=msg.sender,
                  text=text, reason=verdict.reason)
        self._proactive_record(conv)
        self._pace_record_take(conv.conv_id)

        result = self._run(conv, text, mode=self.cfg["permissions"]["base_mode"],
                           speaker=msg.sender, extra=self._PROACTIVE_HINT)
        self._charge(conv.conv_id, result.tokens)
        if not result.denied:
            self._deliver(conv.conv_id, result.text)
        self._log("outbound_proactive", conv_id=conv.conv_id, text=result.text,
                  tokens=result.tokens, reason=verdict.reason)
        return True

    def _handle_proactive_cmd(self, cmd: str) -> bool:
        if not cmd.startswith("主动回复"):
            return False
        rest = cmd[len("主动回复"):].strip()
        if not rest:
            state = "开" if self._proactive.get("enabled") else "关"
            groups = self._proactive.get("groups", {})
            lines = [f"主动回复：总开关{state}"]
            for c in self.convs.values():
                if c.kind == "group":
                    lines.append(f"· {c.name}：{'开' if groups.get(c.conv_id) else '关'}")
            lines.append(f"每小时上限 {self._proactive.get('per_hour', 3)} 次，"
                         f"冷却 {int(self._proactive.get('cooldown_seconds', 600))} 秒，"
                         f"静默 {self._proactive.get('quiet_start', 23)}:00–"
                         f"{self._proactive.get('quiet_end', 8)}:00")
            lines.append("用法：「主动回复 开」「主动回复 关」「主动回复 朋友群 开」")
            self._say(self.owner_conv, "\n".join(lines))
            return True

        # 解析：可能带群名，也可能是 开/关
        want = None
        for word, val in (("打开", True), ("开启", True), ("开", True),
                          ("关闭", False), ("关", False)):
            if rest.endswith(word) or rest == word:
                want = val
                rest = rest[: -len(word)].strip()
                break
        if want is None:
            self._say(self.owner_conv, "格式：「主动回复 开」「主动回复 朋友群 开」。")
            return True

        if not rest:
            self._proactive["enabled"] = want
            self._save_config()
            self._say(self.owner_conv, f"主动回复已{'开启' if want else '关闭'}。")
            self._log("proactive_toggle", enabled=want)
            return True

        matched = [c for c in self.convs.values() if c.kind == "group" and rest in c.name]
        if not matched:
            self._say(self.owner_conv, f"没找到叫「{rest}」的群。")
            return True
        groups = self._proactive.setdefault("groups", {})
        for c in matched:
            groups[c.conv_id] = want
        self._save_config()
        self._say(self.owner_conv,
                  f"「{matched[0].name}」的主动回复已{'开启' if want else '关闭'}。")
        self._log("proactive_group_toggle", conv_id=matched[0].conv_id, enabled=want)
        return True

    def _save_config(self) -> None:
        """主动回复开关要持久化，重启后不该自己变回去。"""
        try:
            path = Path(self.cfg.get("_config_path", "config.json"))
            self.cfg["proactive"] = self._proactive
            data = {k: v for k, v in self.cfg.items() if not k.startswith("_")}
            path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            self._log("config_save_failed", error=repr(exc))

    def _pick_scope(self, token: str, person: str = "") -> str | None:
        """命令里显式写的范围；没写就按"这个人已经在哪个范围"推断。"""
        scope = self.memories.scope_by_label(token)
        if scope:
            return scope
        if person:
            hits = [s for s, m in self.memories.stores.items() if m.resolve(person)]
            if len(hits) == 1:
                return hits[0]
        return None

    def _handle_memory_cmd(self, cmd: str) -> bool:
        # 看记忆
        if cmd in ("记忆", "记忆列表", "看看记忆") or cmd.startswith("记忆 "):
            arg = cmd[2:].strip() if cmd.startswith("记忆 ") else ""
            if arg:
                scope = self.memories.scope_by_label(arg)
                if not scope:
                    self._say(self.owner_conv, "范围只有三个：家人 / 朋友 / 本人。")
                    return True
                store = self.memories.for_scope(scope)
                if not store.count():
                    self._say(self.owner_conv, f"「{self.memories.label(scope)}」这份记忆是空的。")
                    return True
                lines = [f"「{self.memories.label(scope)}」共 {store.count()} 个人："]
                for person, item in store.all().items():
                    aliases = [a for a in item.get("aliases", []) if a != person]
                    alias_txt = f"（别名：{'、'.join(aliases)}）" if aliases else ""
                    lines.append(f"· {person}{alias_txt}：{item.get('note', '')}")
                self._say(self.owner_conv, "\n".join(lines))
                return True

            parts = []
            for scope, store in self.memories.stores.items():
                if not store.count():
                    continue
                parts.append(f"【{self.memories.label(scope)}】{store.count()} 人")
                for person, item in store.all().items():
                    aliases = [a for a in item.get("aliases", []) if a != person]
                    alias_txt = f"（{'、'.join(aliases)}）" if aliases else ""
                    parts.append(f"· {person}{alias_txt}：{item.get('note', '')}")
            if not parts:
                self._say(self.owner_conv,
                          "还没有任何记忆。\n"
                          "用法：「记住 家人 妈妈：我的母亲」「记住 朋友 老王：大学同学」\n"
                          "看某一份：「记忆 家人」；绑别名：「别名 朋友 david 老王」")
                return True
            parts.append("\n范围可选：家人 / 朋友 / 本人。例：记住 朋友 老王：...")
            self._say(self.owner_conv, "\n".join(parts))
            return True

        # 绑别名
        if cmd.startswith("别名"):
            raw = cmd[2:].strip()
            tokens = [t for t in re.split(r"[\s,，、]+", raw) if t]
            scope = None
            if tokens and self.memories.scope_by_label(tokens[0]):
                scope = self.memories.scope_by_label(tokens.pop(0))
            if len(tokens) < 2:
                self._say(self.owner_conv,
                          "用法：「别名 朋友 david 老王」——把昵称 david 归到老王名下。\n"
                          "不写范围时会按这个人已在的范围推断。")
                return True
            alias, person = tokens[0], tokens[1]
            scope = scope or self._pick_scope("", person)
            if not scope:
                self._say(self.owner_conv,
                          f"「{person}」在多个范围里都有，请指明：别名 家人/朋友/本人 {alias} {person}")
                return True
            store = self.memories.for_scope(scope)
            if store.add_alias(alias, person):
                self._say(self.owner_conv,
                          f"已把「{alias}」归到「{person}」名下（{self.memories.label(scope)}）。")
                self._log("memory_alias", scope=scope, alias=alias, person=person)
            else:
                self._say(self.owner_conv, f"「{self.memories.label(scope)}」里没有「{person}」。")
            return True

        # 忘掉
        if cmd.startswith("忘掉"):
            raw = cmd[2:].strip()
            tokens = [t for t in re.split(r"[\s,，、]+", raw) if t]
            scope = None
            if tokens and self.memories.scope_by_label(tokens[0]):
                scope = self.memories.scope_by_label(tokens.pop(0))
            person = tokens[0] if tokens else ""
            if not person:
                self._say(self.owner_conv, "用法：「忘掉 朋友 老王」")
                return True
            targets = [scope] if scope else list(self.memories.stores.keys())
            removed = [s for s in targets if self.memories.for_scope(s).forget(person)]
            if removed:
                self._say(self.owner_conv,
                          f"已忘掉「{person}」（{'、'.join(self.memories.label(s) for s in removed)}）。")
                self._log("memory_forget", person=person, scopes=removed)
            else:
                self._say(self.owner_conv, f"没有「{person}」的记忆。")
            return True

        # 记住
        if cmd.startswith("记住"):
            body = cmd[2:].strip()
            scope = None
            for sep in ("：", ":"):
                if sep in body:
                    head, note = body.split(sep, 1)
                    head, note = head.strip(), note.strip()
                    if not head or not note:
                        break
                    tokens = [t for t in re.split(r"[\s,，、]+", head) if t]
                    if len(tokens) >= 2 and self.memories.scope_by_label(tokens[0]):
                        scope = self.memories.scope_by_label(tokens[0])
                        person = " ".join(tokens[1:])
                    else:
                        person = head
                    scope = scope or self._pick_scope("", person) or "owner"
                    store = self.memories.for_scope(scope)
                    store.set(person, note)
                    self._say(self.owner_conv,
                              f"记住了（{self.memories.label(scope)}）：{person} → {note}\n"
                              f"如果他在微信里显示成别的昵称，用"
                              f"「别名 {self.memories.label(scope)} <昵称> {person}」绑一下。")
                    self._log("memory_set", scope=scope, person=person, note=note)
                    return True
            self._say(self.owner_conv,
                      "格式：「记住 家人 妈妈：我的母亲」\n"
                      "范围可省略，会按已有记录推断；新人默认记到「本人」。")
            return True

        return False

    _BALANCE_WORDS = (
        "余额", "额度", "还剩", "剩下", "报账", "充值", "充了", "欠费",
        "花了多少", "花多少", "花费", "成本", "credit", "balance",
    )

    def _looks_like_balance_question(self, text: str) -> bool:
        t = (text or "").lower()
        return any(w in t for w in self._BALANCE_WORDS)

    def _balance_context(self) -> str:
        """把实时余额塞进上下文，让 agent 能自然地回答"还剩多少钱"。"""
        keys = {}
        try:
            import keystore

            keys = keystore.load()
        except Exception:
            keys = {"DEEPSEEK_API_KEY": self._env_api_key(),
                    "OPENAI_ADMIN_KEY": os.environ.get("OPENAI_ADMIN_KEY", "")}

        ds = balance_mod.deepseek_balance(keys.get("DEEPSEEK_API_KEY") or self._env_api_key())
        admin_env = str(self.cfg.get("openai_admin_key_env", "OPENAI_ADMIN_KEY"))
        oa = balance_mod.openai_costs(
            keys.get("OPENAI_ADMIN_KEY") or os.environ.get(admin_env, ""))

        total = sum(self._used_today(c.conv_id) for c in self.convs.values())
        return (
            "【实时余额（刚查的，可以直接用）】\n"
            f"- {ds.text}\n"
            f"- {oa.text if oa.ok else 'OpenAI：' + oa.text}\n"
            f"- 本机今日记账 {total:,} tokens\n"
            "主人问余额/花费/充值相关问题时，直接用上面的数字回答，"
            "不要说你查不到或没有权限。"
        )

    def _env_api_key(self) -> str:
        """主动消息、余额查询这类内部调用用的 key（DeepSeek 的最便宜）。"""
        return os.environ.get("DEEPSEEK_API_KEY", "")

    def _handle_balance_cmd(self, cmd: str) -> bool:
        if cmd not in ("余额", "报账", "额度", "查余额"):
            return False
        lines = []

        lines.append(balance_mod.deepseek_balance(self._env_api_key()).text)

        admin_env = str(self.cfg.get("openai_admin_key_env", "OPENAI_ADMIN_KEY"))
        oa = balance_mod.openai_costs(os.environ.get(admin_env, ""))
        lines.append(oa.text if oa.ok else f"OpenAI：{oa.text}")

        total = sum(self._used_today(c.conv_id) for c in self.convs.values())
        lines.append(
            f"\n今日已用 {total:,} tokens（这是本机的记账口径，不是厂商账单）"
        )
        self._say_with_pool(self.owner_conv, "\n".join(lines), "ask")
        self._log("balance_report")
        return True

    def _set_usage_notice(self, on: bool) -> bool:
        """开关"每次回答后私信告知消耗"。关掉只是不打扰，记账照旧。"""
        self.cfg["usage_notice"] = "always" if on else "never"
        self._save_config()
        self._log("usage_notice_toggle", enabled=on)
        if on:
            self._say(self.owner_conv,
                      "已开启消耗通知：之后每次回答都会告诉你这次花了多少、今日累计多少。")
        else:
            self._say(self.owner_conv,
                      "已关闭消耗通知。消耗仍会照常记录，\n"
                      "想看统计随时发「用量」。要重新开启就发「开启消耗通知」。")
        return True

    def _handle_usage_cmd(self, cmd: str) -> bool:
        if cmd not in ("用量", "消耗", "花了多少"):
            return False
        rows = []
        total = 0
        for conv in self.convs.values():
            used = self._used_today(conv.conv_id)
            budget = self._budget(conv)
            runs = self._runs_today(conv.conv_id)
            total += used
            pct = used / budget if budget else 0
            avg = used // runs if runs else 0
            rows.append((used, conv.name, budget, pct, runs, avg))

        rows.sort(reverse=True)
        lines = [f"今日合计 {total:,} tokens"]
        for used, name, budget, pct, runs, avg in rows:
            lines.append(
                f"· {name}：{used:,} / {budget:,}（{pct:.0%}）"
                f"｜{runs} 次，平均 {avg:,}/次"
            )
        self._say(self.owner_conv, "\n".join(lines))
        self._log("usage_report", total=total)
        return True

    def _try_control(self, text: str) -> bool:
        """本人私聊里的开关指令。"""
        cmd = text.strip().strip("。！!")
        if cmd in ("详细", "详情", "展开", "具体说说", "个股", "详细说说",
                   "看详细", "交易详情"):
            tcfg = self.cfg.get("trading") or {}
            if not tcfg.get("enabled"):
                self._say(self.owner_conv, "交易日报没开。")
                return True
            text = self.trading_detail()
            if text:
                self.trading_say(self.owner_conv, text,
                                 mood=trading_mod.mood_of(self.trading_status()))
                st = self.trading_status()
                if st is not None:
                    self.trading_writeback(st, self._cached_journal("brief"),
                                           self._last_fallback.get(self.owner_conv),
                                           full=text)
            else:
                self._say(self.owner_conv, "今天还没有可展开的内容。")
            return True

        if cmd in ("今天盈亏", "盈亏", "今天怎么样", "投资笔记", "笔记",
                   "交易日报", "股市", "持仓"):
            tcfg = self.cfg.get("trading") or {}
            if not tcfg.get("enabled"):
                self._say(self.owner_conv, "交易日报没开（config 里 trading.enabled）。")
                return True
            if cmd == "持仓":
                text = self.trading_report(detail=True)
                self._say(self.owner_conv, text or "没配交易状态文件路径。")
            else:
                text = self.trading_brief()
                if text:
                    sticker = self.trading_say(
                    self.owner_conv, text,
                    mood=trading_mod.mood_of(self.trading_status()))
                    st = self.trading_status()
                    if st is not None:
                        self.trading_writeback(st, text, sticker,
                                               full=self._cached_journal("full"))
                    self._say(self.owner_conv, "想看详细的就跟我说「详细」。")
                else:
                    self._say(self.owner_conv, "没配交易状态文件路径。")
            return True

        if cmd in ("余额", "报账", "额度", "查余额"):
            return self._handle_balance_cmd(cmd)
        if cmd in ("备份", "打个备份", "备份一下"):
            target = self.backups.create("manual")
            if target is None:
                self._say(self.owner_conv, "备份失败，看看日志。")
            else:
                listing = self.backups.list_backups()
                self._say(self.owner_conv,
                          f"已备份：{target.name}\n存放位置：{self.backups.dest}\n\n"
                          "最近的备份：\n" + "\n".join(f"· {x}" for x in listing))
            self._log("backup_manual", ok=target is not None)
            return True

        if cmd in ("推送", "推送到github", "同步到github"):
            ok, out = self.push_to_github()
            if ok:
                self._say(self.owner_conv,
                          f"已推送到 github.com/{self._github_user}/{self._github_repo}\n"
                          + "\n".join(f"· {x}" for x in self.repo.log(3)))
            else:
                self._say(self.owner_conv, f"推送失败：{out[:400]}")
            self._log("github_push", ok=ok)
            return True

        if cmd in ("存档", "提交", "存一版", "git存档"):
            ok, detail = self.repo.snapshot("manual: 主人手动存档")
            recent = self.repo.log(5)
            body = f"{detail}\n"
            if recent:
                body += "\n最近提交：\n" + "\n".join(f"· {x}" for x in recent)
            self._say(self.owner_conv, body)
            self._log("vcs_snapshot", ok=ok, detail=detail[:200])
            return True

        if cmd in ("历史", "提交历史", "版本历史"):
            recent = self.repo.log(10)
            pending = self.repo.changed_files()
            body = "最近提交：\n" + ("\n".join(f"· {x}" for x in recent) if recent else "（还没有）")
            if pending:
                body += f"\n\n尚未存档的改动（{len(pending)} 个）：\n" + "\n".join(
                    f"· {x}" for x in pending[:10])
            else:
                body += "\n\n工作区是干净的。"
            self._say(self.owner_conv, body)
            return True

        if cmd in ("改动", "看看改了什么", "改了什么"):
            pending = self.repo.changed_files()
            if not pending:
                self._say(self.owner_conv, "工作区是干净的，没有未存档的改动。")
            else:
                self._say(self.owner_conv,
                          f"有 {len(pending)} 个文件改动还没存档：\n"
                          + "\n".join(f"· {x}" for x in pending[:20])
                          + "\n\n发「存档」就会提交这一版。")
            return True
        if cmd in ("开启消耗通知", "打开消耗通知", "开消耗通知", "开启用量通知",
                   "打开用量通知"):
            return self._set_usage_notice(True)
        if cmd in ("停止消耗通知", "关闭消耗通知", "停消耗通知", "关闭用量通知",
                   "停止用量通知"):
            return self._set_usage_notice(False)
        if cmd in ("用量", "消耗", "花了多少"):
            return self._handle_usage_cmd(cmd)
        if cmd.startswith("加额"):
            return self._handle_grant(cmd)
        if cmd.startswith(("记住", "忘掉", "别名", "记忆")):
            return self._handle_memory_cmd(cmd)
        if cmd.startswith("主动回复"):
            return self._handle_proactive_cmd(cmd)
        if cmd in ("暂停", "停止", "关闭", "停", "pause"):
            self.paused = True
            self._log("paused", by="owner")
            self._say(self.owner_conv, "已暂停：家人群的消息不再处理。你自己的私聊不受影响。\n发「继续」恢复。")
            return True
        if cmd in ("继续", "恢复", "开启", "resume"):
            self.paused = False
            self._log("resumed", by="owner")
            self._say(self.owner_conv, "已恢复：家人群的消息会正常处理。")
            return True
        if cmd in ("状态", "status", "情况"):
            state = "已暂停" if self.paused else "运行中"
            lines = [f"状态：{state}"]
            for conv in self.convs.values():
                used = self._used_today(conv.conv_id)
                bonus = self._bonus(conv.conv_id)
                extra = f"（含加额 {bonus:,}）" if bonus else ""
                lines.append(
                    f"- {conv.name}：今日 {used:,} / {self._budget(conv):,} tokens{extra}"
                )
            pending = len(self._state["pending"])
            lines.append(f"待审批：{pending} 条")
            on = "开" if self._proactive.get("enabled") else "关"
            lines.append(f"主动回复：总开关{on}，每小时 {self._proactive.get('per_hour', 3)} 次上限")
            notice = "关" if str(self.cfg.get("usage_notice", "always")) == "never" else "开"
            lines.append(f"消耗通知：{notice}（「开启消耗通知」/「停止消耗通知」）")

            pcfg = self.cfg.get("presence") or {}
            p_on = "开" if pcfg.get("enabled") else "关"
            targets = "、".join(pcfg.get("targets") or []) or "（没配目标）"
            lines.append(f"上下线播报：{p_on}，发到 {targets}")
            self._say(self.owner_conv, "\n".join(lines))
            return True
        return False

    def _request_quota(self, conv: Conversation, used: int) -> None:
        """额度用完：告诉家人已去申请，同时通知本人，每个会话每天只提醒一次。"""
        self._last_quota_conv = conv.conv_id
        key = f"{date.today().isoformat()}|{conv.conv_id}"
        notified = self._state.setdefault("quota_notified", {})
        if notified.get(key):
            # 今天已经通知过了：保持安静，别每来一条消息就重复同一句。
            self._log("quota_repeat_suppressed", conv_id=conv.conv_id)
            return

        notified[key] = True
        self._save_state()
        self._log("quota_exhausted", conv_id=conv.conv_id, used=used,
                  budget=self._budget(conv))
        self._throttle(conv.conv_id, "quota")

        self._say_with_pool(conv.conv_id,
                            "今天的额度用完了，我已经跟本人申请加额度，稍等。",
                            "ask")
        self._say(
            self.owner_conv,
            f"【额度申请】「{conv.name}」今日已用 {used:,} / {self._budget(conv):,} tokens。\n"
            f"加额：回复「加额 50000」给它加 5 万；「加额 全部 50000」给所有会话加。",
        )

    def _handle_grant(self, cmd: str) -> bool:
        """处理「加额」指令。"""
        if not cmd.startswith("加额"):
            return False
        m = re.search(r"(\d+)", cmd)
        if not m:
            self._say(self.owner_conv, "格式：「加额 50000」或「加额 朋友群 50000」。")
            return True
        amount = int(m.group(1))
        target = cmd.replace("加额", "", 1).replace(m.group(1), "", 1).strip()

        if target in ("全部", "所有", "all"):
            convs = list(self.convs.values())
        elif target:
            convs = [c for c in self.convs.values() if target in c.name or target == c.conv_id]
            if not convs:
                self._say(self.owner_conv, f"没找到叫「{target}」的会话。可用的有："
                          + "、".join(c.name for c in self.convs.values()))
                return True
        else:
            conv_id = getattr(self, "_last_quota_conv", None)
            convs = [self.convs[conv_id]] if conv_id in self.convs else list(self.convs.values())

        lines = []
        for c in convs:
            total = self._add_bonus(c.conv_id, amount)
            # 加额后重新计算档位，并把"拒绝"状态清掉
            self._cost_denied_set(c.conv_id, False)
            self._cost_stage_set(c.conv_id, -1)
            lines.append(f"· {c.name}：+{amount:,}（今日累计加额 {total:,}，"
                         f"总预算 {c.daily_budget_tokens + total:,}）")
            # 默认不通知家人：加额是后台运维动作，群里冒一句"本人给这边加了额度"很突兀
            if c.conv_id != self.owner_conv and self.cfg.get("notify_family_on_grant", False):
                self._say(c.conv_id, f"本人给这边加了 {amount:,} tokens 额度，可以继续了。")
        self._say(self.owner_conv, "已加额：\n" + "\n".join(lines))
        self._log("quota_granted", amount=amount,
                  targets=[c.conv_id for c in convs])
        return True

    _APPROVAL_RE = re.compile(r"^\s*(同意|批准|拒绝|不行)\s*(\d+)\s*[。.!！]?\s*$")

    def _try_resolve_approval(self, text: str) -> bool:
        # 适配器会把同一轮的多条消息合并成一条，所以逐行找指令；
        # 对整段做 fullmatch 会把 "嗯\n同意 3" 这种误判成普通聊天。
        decision = pid = None
        for line in (text or "").splitlines():
            m = self._APPROVAL_RE.match(line)
            if m:
                decision, pid = m.group(1), m.group(2)
                break
        if decision is None:
            return False
        pending = self._state["pending"].pop(pid, None)
        if not pending:
            self._say(self.owner_conv, f"没有编号 {pid} 的待审批请求。")
            return True

        conv = self.convs[pending["conv_id"]]
        kind = pending.get("kind", "task")
        self._save_state()

        if decision in ("拒绝", "不行"):
            msg = {
                "task": "本人拒绝了这次操作。",
                "question": "本人说这个不能告诉你。",
                "draft": "本人没同意发这条，我就不发了。",
                "cost": "本人说这次先不做了，额度留给更要紧的事。",
            }[kind]
            if kind == "cost":
                # 拒绝就是"今天别再花了"，之后不再逐条追问
                self._cost_denied_set(conv.conv_id, True)
            self._say(conv.conv_id, msg)
            self._log("approval_rejected", pid=pid, conv_id=conv.conv_id, kind=kind)
            return True

        self._log("approval_granted", pid=pid, conv_id=conv.conv_id, kind=kind)
        # 批准了就把这个会话的限流解除，恢复主动接话和看图
        self._unthrottle(conv.conv_id)

        if kind == "draft":
            # 已经生成好了，直接放行发送
            self._deliver(conv.conv_id, pending.get("draft", ""))
            self._log("outbound_released", conv_id=conv.conv_id, pid=pid)
            return True

        # task: 提权重跑原来的操作；question/cost: 用正常权限重跑
        mode = (self.cfg["permissions"]["elevated_mode"] if kind == "task"
                else self.cfg["permissions"]["base_mode"])
        prefix = {
            "task": "用户已批准上一个操作，继续执行：",
            "question": "用户（本人）已同意回答这个问题：",
            "cost": "本人已确认可以继续花额度，正常处理这个请求：",
        }[kind]
        if kind == "cost":
            # 记住本人批到了哪一档，同档之内不再重复打扰
            stage = max(self._cost_stage_needed(
                self._used_today(conv.conv_id), self._budget(conv)), 0)
            self._cost_stage_set(conv.conv_id, stage)
            self._cost_denied_set(conv.conv_id, False)
            self._log("cost_stage_approved", conv_id=conv.conv_id, stage=stage)

        result = self.runner.run(
            conv_id=conv.conv_id,
            task=f"{prefix}{pending['task']}",
            workspace=self._workspace_for(conv),
            session_id=pending["session_id"],
            mode=mode,
            timeout_s=int(self.cfg.get("timeout_s", 300)),
        )
        self._charge(conv.conv_id, result.tokens)

        # 本人批准 = 最终授权：原样发出，但把"发了什么"回报给本人留痕
        self._deliver(conv.conv_id, result.text)
        findings = privacy.scan_output(result.text) if conv.conv_id != self.owner_conv else []
        if findings:
            self._say(
                self.owner_conv,
                f"【知悉】第 {pid} 条已按你的批准发给「{conv.name}」，"
                f"其中含 {'、'.join(f.category for f in findings)}。",
            )
            self._log("outbound_approved_with_secrets", pid=pid,
                      categories=[f.category for f in findings])

        self._log("outbound_elevated", conv_id=conv.conv_id, text=result.text,
                  mode=result.mode, pid=pid, kind=kind, tokens=result.tokens)
        return True

    def _slow_ping(self, conv_id: str, done: threading.Event, delay: float) -> None:
        """任务超过阈值还没结束，补一句进度，避免看起来像挂了。"""
        if done.wait(delay):
            return
        try:
            self._say(conv_id, "还在处理，稍等一下…")
            self._log("slow_ping", conv_id=conv_id, delay_s=delay)
        except Exception:
            pass

    # ---------- 按需生图 ----------
    def _image_allowed(self, conv_id: str) -> bool:
        if not self.imagegen:
            return False
        groups = (self.cfg.get("image_gen", {}) or {}).get("groups", [])
        return conv_id in groups

    def _image_used_today(self) -> int:
        return int(self._state.get("imagegen", {}).get(date.today().isoformat(), 0))

    def _request_image(self, conv_id: str, prompt: str) -> None:
        """生图放后台线程：文字已经发出去了，图晚点到，不阻塞对话。"""
        if not self._image_allowed(conv_id):
            self._log("imagegen_skipped", conv_id=conv_id, reason="not_allowed")
            return
        limit = int((self.cfg.get("image_gen", {}) or {}).get("daily_limit", 20))
        used = self._image_used_today()
        if used >= limit:
            self._log("imagegen_skipped", conv_id=conv_id, reason="daily_limit", used=used)
            return
        self._log("imagegen_requested", conv_id=conv_id, prompt=prompt[:80])
        threading.Thread(
            target=self._image_worker, args=(conv_id, prompt), daemon=True
        ).start()

    def _image_worker(self, conv_id: str, prompt: str) -> None:
        try:
            result = self.imagegen.generate(prompt)
        except Exception as exc:
            result = imagegen_mod.GenResult(False, error=repr(exc))

        if not result.ok:
            self._log("imagegen_failed", conv_id=conv_id, error=result.error)
            # 请求方必须收到回执，否则它只能一遍遍重试、白烧 token。
            # 顺手降级成一张现成的通用表情，聊天不至于断在那里。
            self._say(conv_id, "图没生成出来，先用现成的顶一下。")
            self._send_pool_sticker(conv_id, "generic")
            # 一天只提醒一次，避免每张失败都炸私聊
            if not self._image_fail_notified:
                self._image_fail_notified = True
                self._say(self.owner_conv,
                          f"【生图失败】{result.error[:200]}\n"
                          "检查 image_gen 配置里的 key / 模型 / 地址是否可用。")
            return

        key = date.today().isoformat()
        with self._state_lock:
            self._state.setdefault("imagegen", {})
            self._state["imagegen"][key] = self._image_used_today() + 1
            self._save_state()

        try:
            self.adapter.send_file(conv_id, result.path)
            self._log("imagegen_sent", conv_id=conv_id, file=result.path.name,
                      cached=result.cached, used_today=self._image_used_today())
        except Exception as exc:
            self._log("imagegen_send_failed", conv_id=conv_id, error=repr(exc))
            self._say(conv_id, "图生成好了但没发出去，先用现成的顶一下。")
            self._send_pool_sticker(conv_id, "generic")

    # ---------- 告状 ----------
    def _owner_display_name(self) -> str:
        """主人在微信里的显示名，用来 @。"""
        conv = self.convs.get(self.owner_conv)
        return (conv.wechat_name if conv and conv.wechat_name else "主人")

    def _maybe_tattle(self, conv: Conversation) -> None:
        """某个群用得太多时，在群里 @ 主人告一状。

        这条消息本身的消耗**不算在这个群头上**，记账时归到主人名下——
        告状是为了替主人省钱，不该再让那个群因此更快用光额度。
        """
        t = self.cfg.get("tattle") or {}
        if not t.get("enabled") or conv.conv_id != t.get("conv"):
            return

        used = self._used_today(conv.conv_id)
        budget = self._budget(conv)
        if budget <= 0:
            return
        ratio = used / budget
        if ratio < float(t.get("threshold_ratio", 0.8)):
            return

        today = date.today().isoformat()
        sent = self._state.setdefault("tattle", {})
        if int(sent.get(today, 0)) >= int(t.get("max_per_day", 2)):
            return

        owner = self._owner_display_name()
        text = ""
        try:
            text = nudge_mod.compose(
                f"群「{conv.name}」今天已经用掉了 {used:,} tokens，占额度 "
                f"{int(ratio * 100)}%。用一两句话向主人告状，语气可以夸张、"
                f"带点委屈，说这个群太能聊了。别提技术细节。",
                api_key=self._env_api_key(),
            )
        except Exception:
            text = ""
        if not text:
            text = f"已经用掉 {int(ratio * 100)}% 的额度了，这个群太能聊了。"

        self._say(conv.conv_id, f"@{owner} {text}")
        if t.get("also_private", True):
            self._say(self.owner_conv,
                      f"【告状】「{conv.name}」今天已用 {used:,} / {budget:,} tokens"
                      f"（{int(ratio * 100)}%）。这条不计入该群额度。")

        sent[today] = int(sent.get(today, 0)) + 1
        with self._state_lock:
            self._save_state()
        self._log("tattle_sent", conv_id=conv.conv_id, used=used, budget=budget,
                  ratio=round(ratio, 3), charged_to="user:owner")

    def _resolve_table_path(self, conv_id: str, target: str) -> Path | None:
        """把 agent 说的文件名解析成真实路径。

        只允许桥接目录和工作区内部，避免它把系统里任意文件渲染成图发出去。
        """
        roots = [self.family_root, self.backups.source]
        # 额外允许的目录（桌面、项目目录等），在 config 的 allowed_roots 里配
        for extra in self.cfg.get("allowed_roots") or []:
            try:
                roots.append(Path(os.path.expandvars(str(extra))).expanduser())
            except Exception:
                continue
        cand = Path(target)
        if cand.is_absolute() and cand.exists():
            path = cand
        else:
            path = None
            for root in roots:
                p2 = (root / target)
                if p2.exists():
                    path = p2
                    break
            if path is None:
                # 只给了文件名：在工作区里找一层
                for root in roots:
                    hits = list(root.rglob(Path(target).name))[:1]
                    if hits:
                        path = hits[0]
                        break
        if path is None:
            return None
        resolved = path.resolve()
        for root in roots:
            try:
                resolved.relative_to(root.resolve())
                return path
            except Exception:
                continue
        return None

    def _request_table(self, conv_id: str, target: str) -> None:
        path = self._resolve_table_path(conv_id, target)
        if path is None:
            self._say(conv_id, f"没找到这个表格：{target}")
            self._log("tablecard_missing", conv_id=conv_id, target=target)
            return
        tcfg = self.cfg.get("table") or {}
        try:
            pages = tablecard_mod.render(
                path, max_cols=int(tcfg.get("max_cols", 6)),
                max_rows=tcfg.get("max_rows"),
                columns=tcfg.get("columns") or None)
        except Exception as exc:
            self._say(conv_id, f"表格渲染失败了：{exc}")
            self._log("tablecard_failed", conv_id=conv_id, file=path.name,
                      error=repr(exc))
            return
        limit = int(tcfg.get("max_pages", 12))
        dropped = max(0, len(pages) - limit)
        if dropped:
            self._say(conv_id, f"共 {len(pages)} 页，先发前面 {limit} 页；"
                               f"要全部的话说「全部发出」。")
            pages = pages[:limit]

        ok = 0
        for page in pages:
            try:
                self.adapter.send_file(conv_id, page)
                ok += 1
            except Exception as exc:
                self._log("tablecard_send_failed", conv_id=conv_id,
                          page=page.name, error=repr(exc))
        self._log("tablecard_sent", conv_id=conv_id, file=path.name,
                  pages=len(pages), sent=ok)
        if ok == 0:
            self._say(conv_id, "表格图没发出去，稍后再试一次。")

    # ---------- 图片入库 ----------
    def _begin_sticker_import(self, path: Path) -> None:
        # 注意用 `is None` 判断：Stickers 有 __len__，空库会被当成"没启用"
        if self.stickers is None:
            self._say(self.owner_conv, "表情库功能没开，这张图我存不了。")
            return
        self._pending_sticker = {"path": str(path), "step": "name"}
        self._log("sticker_import_pending", file=path.name,
                  size=path.stat().st_size if path.exists() else 0)
        self._say(
            self.owner_conv,
            "图片收到。回一句名字，我就把它存进表情库（比如「不耐烦翻白眼」）；\n"
            "回「跳过」就丢弃。",
        )

    def _finish_sticker_import(self, text: str) -> bool:
        """两步：先定名字，再问用途（普通 / 索要 / 专属）。"""
        t = (text or "").strip()
        if not t:
            return False
        st = self._pending_sticker or {}

        if t in ("跳过", "取消", "算了", "不要", "丢弃"):
            self._pending_sticker = None
            try:
                src = Path(st.get("path", ""))
                if src and src.exists():
                    src.unlink()
            except Exception:
                pass
            self._say(self.owner_conv, "已丢弃，没入库。")
            self._log("sticker_import_discarded")
            return True

        src = Path(st.get("path", "")) if st.get("path") else None

        # ---- 第二步：问用途 ----
        if st.get("step") == "purpose":
            name = st.get("name", "")
            if t in ("不用", "普通", "0", "1"):
                choice = "普通"
            elif t in ("2", "索要", "讨要"):
                choice = "索要"
            elif t in ("3", "专属"):
                choice = "专属"
            elif t in ("4", "都要", "索要+专属", "两个都要"):
                choice = "两个都要"
            else:
                self._say(self.owner_conv,
                          "回数字就行：1 普通 / 2 索要 / 3 专属 / 4 索要+专属")
                return True

            pools = self.cfg.setdefault("sticker_pools", {})
            ask = pools.setdefault("ask", [])
            exclusive = pools.setdefault("exclusive", [])
            if choice in ("索要", "两个都要") and name not in ask:
                ask.append(name)
            if choice in ("专属", "两个都要") and name not in exclusive:
                exclusive.append(name)
            self._save_config()
            self._pending_sticker = None
            self._log("sticker_pools_updated", name=name, choice=choice,
                      ask=list(ask), exclusive=list(exclusive))
            self._say(self.owner_conv,
                      f"好，「{name}」归到：{choice}。\n"
                      f"· 索要池：{'、'.join(ask) or '（空）'}\n"
                      f"· 专属池：{'、'.join(exclusive) or '（空）'}")
            return True

        # ---- 第一步：定名字并落盘 ----
        if not src or not src.exists():
            self._pending_sticker = None
            self._say(self.owner_conv, "原图不见了，请重新发一次。")
            return True

        name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", t)[:20].strip("_ ")
        if not name:
            self._say(self.owner_conv, "这个名字不能用，换一个吧（别用符号）。")
            return True

        suffix = src.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            suffix = ".png"
        dest = self.stickers.dir / f"{name}{suffix}"
        try:
            shutil.move(str(src), str(dest))
        except Exception as exc:
            self._log("sticker_import_failed", error=repr(exc))
            self._say(self.owner_conv, f"存档失败：{exc!r}")
            return True

        self.stickers.refresh()
        self._pending_sticker = {"path": str(dest), "step": "purpose", "name": name}
        self._log("sticker_imported", name=name, file=dest.name,
                  total=len(self.stickers))
        self._say(
            self.owner_conv,
            f"已存进表情库：「{name}」（库里现在 {len(self.stickers)} 个）。\n\n"
            "这张图属于哪一类？\n"
            "1 普通（只在和群里聊时随机用）\n"
            "2 索要（额度提示、问余额、充值到账时用）\n"
            "3 专属（只发给你）\n"
            "4 索要 + 专属\n\n"
            "回数字就行，回「不用」就当普通。",
        )
        return True

    # ---------- 私聊暂停（当中转站用） ----------
    _PAUSE_WORDS = ("暂停", "暂停回复", "暂停回信", "别回了", "先别回", "先别回复",
                    "pause", "/pause", "免打扰")
    _RESUME_WORDS = ("恢复", "恢复回复", "恢复回信", "继续", "继续回复",
                     "取消暂停", "resume", "/resume")

    def _pause_state(self) -> dict:
        return self._state.setdefault("pause", {})

    def _paused(self, conv_id: str) -> bool:
        entry = self._pause_state().get(conv_id)
        if not entry:
            return False
        until = float(entry.get("until") or 0)
        if until and time.time() >= until:
            # 到点自动恢复
            self._pause_state().pop(conv_id, None)
            with self._state_lock:
                self._save_state()
            self._log("pause_expired", conv_id=conv_id)
            return False
        return True

    def _set_pause(self, conv_id: str, seconds: float) -> None:
        now = time.time()
        self._pause_state()[conv_id] = {
            "since": now,
            "until": (now + seconds) if seconds else 0,
        }
        with self._state_lock:
            self._save_state()

    def _clear_pause(self, conv_id: str) -> int:
        """取消暂停，返回期间攒下的中转条数。"""
        self._pause_state().pop(conv_id, None)
        with self._state_lock:
            self._save_state()
        return len(self._relay_take(conv_id, peek=True))

    def _relay_log(self, conv: Conversation, msg: Inbound) -> None:
        """暂停期间收到的消息只记账，不回复、不花 token。"""
        path = Path(self.state_dir) / "relay.jsonl"
        try:
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({
                    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "conv_id": conv.conv_id,
                    "sender": msg.sender,
                    "text": msg.text,
                }, ensure_ascii=False) + "\n")
        except Exception:
            pass
        self._log("relay_logged", conv_id=conv.conv_id, chars=len(msg.text or ""))

    def _relay_take(self, conv_id: str, peek: bool = False) -> list[dict]:
        path = Path(self.state_dir) / "relay.jsonl"
        if not path.exists():
            return []
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("conv_id") == conv_id:
                rows.append(row)
        if rows and not peek:
            keep = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    row = json.loads(line)
                except Exception:
                    keep.append(line)
                    continue
                if row.get("conv_id") != conv_id:
                    keep.append(line)
            path.write_text("\n".join(keep) + ("\n" if keep else ""),
                            encoding="utf-8")
        return rows

    def _parse_pause_seconds(self, text: str) -> tuple[bool, float]:
        """认「暂停 30分钟」「暂停 2小时」「暂停」这类说法。"""
        import re as _re

        t = (text or "").strip().lower()
        is_pause = t.startswith(self._PAUSE_WORDS)
        is_resume = t in self._RESUME_WORDS or t.startswith(self._RESUME_WORDS)
        if not is_pause:
            return False, 0.0
        m = _re.search(r"(\d+(?:\.\d+)?)\s*(分钟|分|小时|时|h|m|秒|s)", t)
        if not m:
            return True, 0.0        # 不给时长 = 一直暂停
        value = float(m.group(1))
        unit = m.group(2)
        if unit in ("小时", "时", "h"):
            return True, value * 3600
        if unit in ("秒", "s"):
            return True, value
        return True, value * 60

    def _handle_pause(self, conv: Conversation, text: str) -> bool:
        """处理暂停/恢复指令。返回 True 表示这条消息已经消化掉。"""
        pcfg = self.cfg.get("pause") or {}
        allowed = pcfg.get("convs") or [self.owner_conv]
        if conv.conv_id not in allowed:
            return False

        t = (text or "").strip().lower()
        if t in self._RESUME_WORDS or t.startswith(self._RESUME_WORDS):
            if not self._paused(conv.conv_id):
                self._say(conv.conv_id, "现在没有暂停。")
                return True
            count = self._clear_pause(conv.conv_id)
            msg = "好，恢复正常回复。"
            if count:
                msg += f"（暂停期间收到 {count} 条，都记着）"
            self._deliver(conv.conv_id, msg, force=True)
            self._log("pause_cleared", conv_id=conv.conv_id, relayed=count)
            return True

        is_pause, seconds = self._parse_pause_seconds(text)
        if not is_pause:
            return False

        self._set_pause(conv.conv_id, seconds)
        if seconds:
            mins = int(seconds // 60) or 1
            span = f"{mins} 分钟" if mins < 60 else f"{mins // 60} 小时"
            self._deliver(conv.conv_id,
                          f"好，先不回消息，{span}后自动恢复。"
                          "（说「恢复」可以立刻恢复）", force=True)
        else:
            self._deliver(conv.conv_id,
                          "好，先不回消息，一直停着。"
                          "（说「恢复」可以立刻恢复）", force=True)
        self._log("pause_set", conv_id=conv.conv_id, seconds=seconds)
        return True

    def _deliver(self, conv_id: str, text: str, force: bool = False) -> None:
        """发回复：先摘掉所有动作标记，再按类型分派（表情图 / 表格长图）。"""
        if not force and self._paused(conv_id):
            self._log("deliver_suppressed_paused", conv_id=conv_id)
            return
        conv = self.convs.get(conv_id)
        if conv is not None:
            # 保底表情：模型没挑的时候，每 N 条补一张
            text = self._maybe_fallback_sticker(conv, text)

        body, actions = stickers_mod.extract_actions(text)
        if body:
            self._say(conv_id, body, force=force)

        for kind, value in actions:
            if kind == "table":
                self._request_table(conv_id, value)
                continue
            if kind == "deliver":
                self._deliver_file(conv_id, value)
                continue

            # 表情：可能是 [表情:名字]，也可能是 [表情:生图:描述]
            m = re.match(r"^\s*(?:生图|画)\s*[:：]\s*(.+)$", value)
            if m:
                self._request_image(conv_id, m.group(1))
                continue
            if not (self.stickers and len(self.stickers)):
                continue
            path = self.stickers.resolve(value)
            if path is None:
                self._log("sticker_missing", conv_id=conv_id, key=value)
                alt = self._pool_sticker("generic")
                alt_path = self.stickers.resolve(alt) if alt else None
                if alt_path is not None:
                    try:
                        self.adapter.send_file(conv_id, alt_path)
                        self._log("sticker_fallback_for_missing",
                                  conv_id=conv_id, asked=value, used=alt)
                    except Exception as exc:
                        self._log("sticker_send_failed", conv_id=conv_id,
                                  key=alt, error=repr(exc))
                continue
            try:
                self.adapter.send_file(conv_id, path)
                self._log("sticker_sent", conv_id=conv_id, key=value, file=path.name)
            except Exception as exc:
                self._log("sticker_send_failed", conv_id=conv_id, key=value,
                          error=repr(exc))

    def _say(self, conv_id: str, text: str, force: bool = False) -> None:
        # 暂停期间一律不发（用量提示、审批卡、主动消息都在这里被拦住）
        if not force and self._paused(conv_id):
            self._log("say_suppressed_paused", conv_id=conv_id)
            return
        # 微信不渲染 Markdown，且超长消息会被折叠，统一清理后分条发
        chunks = wechat_format.format_for_wechat(
            text,
            max_chars=int(self.cfg.get("reply_chunk_chars", 1400)),
            strip=bool(self.cfg.get("strip_markdown", True)),
        )
        for chunk in chunks:
            self.adapter.send(conv_id, chunk)

    # ---------- 循环 ----------
    def run_once(self) -> int:
        batch = self.adapter.poll()
        for msg in batch:
            self.handle(msg)
        return len(batch)
