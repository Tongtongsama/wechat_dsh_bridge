"""隐私护栏：判断"这个问题该不该直接答"和"这段回答能不能直接发"。

两道闸：
  1. 入口闸 question_is_sensitive()：家人问到隐私类话题，先问本人；
  2. 出口闸 scan_output()：回答里出现了密钥/证件号/手机号等，先扣住不外发。

入口闸是"意图猜测"，必然有漏；出口闸是"结果检测"，才是真正的兜底。
所以两个都要有。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------- #
# 出口闸：真正的敏感内容
# --------------------------------------------------------------------------- #
@dataclass
class Finding:
    category: str
    sample: str          # 已脱敏的样例，只留首尾


_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("API Key", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),
    ("密钥/令牌", re.compile(
        r"(?i)\b(api[_\-\s]?key|access[_\-\s]?key|secret[_\-\s]?key|"
        r"token|passwd|password|passphrase)\b\s*[:=]\s*\S{8,}")),
    ("私钥文件", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    ("银行卡号", re.compile(r"(?<!\d)\d{16,19}(?!\d)")),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("邮箱", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")),
]

# 路径本身不算秘密，但这些文件里装的是秘密
_SENSITIVE_FILES = re.compile(
    r"(?i)(\.credentials\.ya?ml|secret\.env|\.ssh[\\/]|id_rsa|"
    r"id_ed25519|\.netrc|\.git-credentials|login data|cookies)"
)


_LITERALS: list[str] = []


def set_literals(values) -> None:
    """登记"指定密文"。命中就一律按隐私处理，不依赖正则能覆盖这个形态。

    入口闸和出口闸都会用它——所以某个具体密码即使长得不像密钥，
    也能被拦下来（这是给"我就是要测这个密码"这类场景准备的）。
    """
    global _LITERALS
    _LITERALS = [v for v in (values or []) if isinstance(v, str) and len(v) >= 4]


def scan_literals(text: str) -> list[Finding]:
    hits: list[Finding] = []
    for lit in _LITERALS:
        if lit and lit in (text or ""):
            hits.append(Finding("指定密文", _mask(lit)))
            break
    return hits


def _mask(s: str) -> str:
    s = s.strip()
    if len(s) <= 8:
        return s[0] + "***" if s else ""
    return f"{s[:4]}…{s[-4:]}"


def scan_output(text: str) -> list[Finding]:
    """扫描一段准备发出去的文字。"""
    found: list[Finding] = []
    for category, pat in _SECRET_PATTERNS:
        for m in pat.finditer(text or ""):
            found.append(Finding(category, _mask(m.group(0))))
    if _SENSITIVE_FILES.search(text or ""):
        found.append(Finding("敏感文件路径", _mask(_SENSITIVE_FILES.search(text).group(0))))
    found.extend(scan_literals(text))
    # 同类只报一次，避免刷屏
    seen: set[str] = set()
    uniq: list[Finding] = []
    for f in found:
        if f.category in seen:
            continue
        seen.add(f.category)
        uniq.append(f)
    return uniq


# --------------------------------------------------------------------------- #
# 入口闸：可能是打探隐私的问题
# --------------------------------------------------------------------------- #
_SENSITIVE_TOPICS: list[tuple[str, re.Pattern[str]]] = [
    ("密钥/凭据", re.compile(
        r"(?i)(api[\-\s]?key|密钥|令牌|token|密码|口令|凭据|credential|secret|passwd)")),
    ("证件/账号", re.compile(r"(身份证|护照|社保|银行卡|卡号|账号|工号|学号|车牌)")),
    ("联系方式", re.compile(r"(手机号|电话号码|电话|住址|家庭地址|地址|邮箱|微信密码)")),
    ("住址/行程", re.compile(r"(住哪|家在哪|住在|门牌|小区|几号楼|行程|航班|车次|定位)")),
    ("财务", re.compile(r"(工资|收入|存款|余额|账单|转账|支付密码)")),
]


def question_is_sensitive(text: str) -> list[str]:
    """返回命中的话题类别；空列表表示不敏感。"""
    hits: list[str] = []
    for label, pat in _SENSITIVE_TOPICS:
        if pat.search(text or ""):
            hits.append(label)
    if scan_literals(text):
        hits.append("指定密文")
    return hits


def redact(text: str) -> str:
    """把敏感内容替换掉，用于"拒绝后仍要给个说法"的场景。"""
    out = text or ""
    for _category, pat in _SECRET_PATTERNS:
        out = pat.sub(lambda m: _mask(m.group(0)), out)
    out = _SENSITIVE_FILES.sub("[敏感文件]", out)
    return out
