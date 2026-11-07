"""兼容层：修 wxauto4 在部分微信客户端上找不到左侧导航「聊天」标签的问题。

现象：`WeChat()` 初始化报

    LookupError: Find Control Timeout:
    {ClassName: 'mmui::XTabBarItem', Name: '微信', ControlType: ButtonControl}

原因：这个客户端把左侧导航里"聊天"那一项的**无障碍名**写成了 `WeChat`（英文），
而 wxauto4 的控件名硬编码为中文「微信」（编译在 .pyd 里，改不了语言表）。
同一条导航栏里其它项（通讯录、朋友圈、收藏）都是中文，所以只有这一项对不上。

修法：在 UIA 层把「按名字精确找 微信」改成「按正则匹配 微信 或 WeChat」。
用正则而不是直接替换成 WeChat——中文客户端下标签本来就是「微信」，
写死英文反而会把正常环境弄坏。
"""
from __future__ import annotations

_ALIASES: dict[str, str] = {
    "微信": "^(微信|WeChat)$",
}


def install() -> bool:
    """幂等安装。返回是否本次真正打了补丁。"""
    try:
        import wxauto4.uia.uiautomation as auto
    except Exception:
        return False

    base = getattr(auto, "Control", None)
    if base is None or getattr(base, "_dsh_name_alias", False):
        return False

    original = base.__init__

    def patched(self, *args, **kwargs):
        name = kwargs.get("Name")
        if isinstance(name, str) and name in _ALIASES:
            kwargs.pop("Name", None)
            kwargs["RegexName"] = _ALIASES[name]
        return original(self, *args, **kwargs)

    base.__init__ = patched
    base._dsh_name_alias = True
    return True


INSTALLED = install()
