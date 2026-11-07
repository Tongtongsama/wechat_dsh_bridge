# wechat_dsh_bridge

用微信指挥你电脑上的 AI agent。

你在手机上发一句「帮我把这周的报销整理成表格」，电脑上跑着的 agent 真的会去翻文件、做表格、把成品发回微信。家人朋友也能用，但权限、额度、审批都分层管住。

> Windows 专用。基于微信桌面端的 UI 自动化（[wxauto4](https://github.com/cluic/wxauto)），**不是**注入或改包，微信本身不需要改动。

---

## 它是什么

一个在你自己电脑上常驻的 Python 桥接进程：

```
微信消息 ──► 轮询/监听 ──► 权限与额度闸门 ──► DSH agent 执行（沙箱内）
                 ▲                                    │
                 └──────── 回复 / 图片 / 文件 ◄────────┘
```

微信是入口也是出口。agent 在受控沙箱里干活，产物通过微信发回。

### 能做到的事

| 能力 | 说明 |
|---|---|
| **多会话分级** | 私聊、家人群、朋友群各自独立的触发方式、额度、记忆范围 |
| **权限分层** | 只读 / 工作区可写 / 全盘可写三档，危险操作走微信审批 |
| **成本闸门** | 按 token 用量分档提醒，超限降级或暂停，可「加额」继续 |
| **审批卡片** | 删文件、执行命令、花钱这类动作，先发卡片给本人，回「同意 1」才执行 |
| **读图** | 收到图片先转成文字描述再交给 agent；只在「被叫到」后的时间窗内自动看 |
| **读链接** | 自动抓取消息里的网页正文（忽略图片），内网地址直接拒绝 |
| **表格转长图** | csv/xlsx 渲染成分页 PNG，手机直接看，不用开电脑 |
| **文件投递** | agent 在沙箱里写不了外面，用 `[投递:xxx]` 标记由桥接代为搬运 |
| **表情** | 按情绪挑表情；亏损/挫败时不会发笑脸 |
| **连发合并** | 把一句话分几段发的习惯合并成一条，不逐句搭理 |
| **人设化汇报** | 可选：把数据日报写成第一人称的投资笔记（配表情） |
| **暂停回复** | 私聊发「暂停」当纯消息中转站用，不回复不花 token |
| **记忆** | 按范围存人设与人物备注；群聊只给名字，不念别人的私人备注 |
| **上下线播报** | 启动/退出时说一句，可配置发给哪些群 |
| **备份与存档** | 定时 git 快照 + zip 备份，可选推送到你自己的仓库 |

---

## 快速开始

### 0. 需要什么

- Windows 10/11
- 微信桌面版 4.x（登录**一个专用小号**，不要用主号）
- Python 3.12（`pythonw.exe` 必须存在）
- [DeepSeek Harness](https://github.com/deepseek-ai) 桌面端（提供 `dsh` CLI 和沙箱）

### 1. 装依赖

```powershell
python -m pip install -r requirements.txt
```

### 2. 配密钥

密钥单独存放，不进仓库：

```
%USERPROFILE%\.dsh\wechat-bridge\secret.env
```

```ini
DEEPSEEK_API_KEY=sk-...
# 可选：读图、生图、余额查询
OPENAI_API_KEY=sk-...
OPENAI_ADMIN_KEY=sk-admin-...
# 可选：自动推送备份到自己的仓库
GITHUB_TOKEN=ghp_...
```

### 3. 配会话

复制 `config.example.json` 为 `config.json`，改三处：

```json
{
  "conversations": [
    { "conv_id": "user:owner",    "name": "我（本人）", "trigger": "always" },
    { "conv_id": "group:parents", "name": "家人群",    "trigger": "@bot", "aliases": ["@小助手"] },
    { "conv_id": "group:friends", "name": "朋友群",    "trigger": "@bot", "aliases": ["@小助手"] }
  ]
}
```

- `name`：微信群名或好友昵称，**必须和微信里显示的一致**
- `trigger`：`always` 每条都回；`@bot` 需要 @ 才回
- `aliases`：除了 `bot_mention` 之外还能用的称呼

### 4. 起

```powershell
.\bridge.ps1 -Action start     # 启动
.\bridge.ps1 -Action status    # 看状态
.\bridge.ps1 -Action stop      # 停止（会说一句"我先撤了"）
```

或者双击 `start.cmd` / `stop.cmd` / `status.cmd`。

日志在 `logs/bridge.log`，第一行应该看到：

```
[wxauto] 已挂载 pid=12345，账号=你的小号 ✅
[wxauto] 已接入 '家人群' → group:parents（读到 3 条）
```

---

## 重要：微信窗口必须"看得见"

这是这套方案最容易踩的坑，也是绝大多数"它怎么不回我"的原因。

微信的 UI 自动化依赖窗口渲染出列表；以下状态会让**会话列表读不到、所有会话打不开**：

| 状态 | 窗口坐标 | 症状 |
|---|---|---|
| 最小化 | `-21333,-21333` | `GetSession()` 返回 0 |
| 收进托盘 | `-32000,-32000` | 同上 |
| 窗口过窄 | 宽度 < 600px | 左侧列表被折叠 |

`IsIconic()` 对这个 Qt 窗口**不可靠**（最小化时也可能返回 False），所以桥接改为按坐标判断，并在挂载、轮询、发消息前自动恢复窗口。

排查工具：

```powershell
.\check-wechat.cmd
```

它会打印：找到的窗口、能否挂载、读到几个会话、逐个会话能否打开。

---

## 配置要点

完整字段见 `config.example.json`，几个关键的：

```json
{
  "permissions": {
    "base_mode": "workspace-write",
    "elevated_mode": "danger-full-access"
  },
  "cost_gate_thresholds": [0.6, 0.8],
  "pause":  { "convs": ["user:owner"] },
  "web_read": { "enabled": true, "max_links": 2, "max_chars": 3000 },
  "deliver": { "targets": { "documents": "%USERPROFILE%\\Documents" } },
  "presence": { "targets": ["group:friends"], "stop_targets": ["group:friends"] },
  "coalesce": { "wait_seconds": 2.0, "incomplete_extra_seconds": 3.0 }
}
```

### 安全边界（默认是收紧的）

- **会话白名单**：只有 `conversations` 里列出的会话会被响应，其他人发消息一律忽略
- **粗体权限**：agent 默认只能写自己的工作区；需要全盘时走审批
- **审批不可绕过**：危险动作先发卡片，本人回「同意 N」才执行；不回就等于拒绝
- **密钥落盘位置**：`~/.dsh/wechat-bridge/secret.env`，仓库里永远没有
- **链接抓取拒绝内网**：`127.0.0.1`、`10.x`、`192.168.x`、`169.254.x` 全部拒绝，防止被外部消息当成打内网的跳板

---

## 命令

私聊里发这些（不需要 @）：

| 命令 | 作用 |
|---|---|
| `状态` | 运行状态、额度、待审批 |
| `用量` / `报账` | 用量明细 |
| `加额 50000` | 给某个会话加额度 |
| `同意 1` / `拒绝 1` | 处理审批 |
| `记住 老王：大学同学` | 存一条人物记忆 |
| `别名 david 老王` | 把不同群的昵称归到同一个人 |
| `暂停` / `暂停 30分钟` / `恢复` | 暂停回复，当中转站用 |
| `备份` / `存档` | 立刻备份 / git 快照 |

agent 回复里可以带动作标记（不会被对方看到）：

```
[表格:报表.xlsx]        渲染成分页长图发出去
[表情:开心]             从表情库里发一张
[投递:documents/a.pdf]  由桥接把文件放进配置好的目录
```

---

## 测试

```powershell
python selftest.py
```

391 项检查，全部离线（用 `MockAdapter` + `FakeRunner`，不需要微信、不花 token、不联网）。

覆盖：触发与回声、审批容错、额度与成本闸门、记忆与别名、连发合并、暂停、表情情绪、表格渲染、链接抓取、文件投递、交易日报、窗口恢复判定等。

---

## 已知限制

- **wxauto 免费版只能监听一个会话**，所以用了两级轮询：先读会话列表指纹，再对活跃会话做兜底扫描
- **微信 UI 自动化偶尔会崩**（`COMError: 灾难性故障`），表现为会话列表读不到；重启微信客户端通常能恢复，桥接会自动重连
- **只在 Windows 上验证过**
- **不能发原生表情包**：微信没有开放接口，只能作为图片文件发送
- **图片下载对别人的图片不稳定**：失败时退化为截图识别

---

## 目录结构

```
bridge.py          主逻辑：触发、闸门、回复、动作分派
bridge_main.py     进程入口：轮询循环 + 后台线程
adapters.py        微信适配层（真实 / Mock）
runner.py          调 DSH CLI 跑一次 agent
memory.py          分范围记忆 + 人设
nudge.py           定时任务：问候、余额监控
presence.py        上下线播报
proactive.py       主动接话
trading.py         投资笔记（可选扩展）
tablecard.py       csv/xlsx → 分页 PNG
webread.py         网页正文抓取（含内网防护）
stickers.py        表情库、动作标记解析
vcs.py / backup.py git 快照、zip 备份、推送
keystore.py        安全的密钥读写
```

---

## License

MIT，见 [LICENSE](LICENSE)。
