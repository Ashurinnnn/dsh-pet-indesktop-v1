# -*- coding: utf-8 -*-
"""桌宠对用户的称呼。

桌宠的台词里到处是"主人"——内置预设、兜底文案、歌词、识屏提示、菜单项。
用户想让它换个叫法（"老板"、"铲屎官"、自己的名字……）时，散落各处的中文词
没法一处处去改，所以约定：

* 内置文案里凡是称呼用户的地方，一律写占位符 ``{user}``（走
  :func:`pet.persona_phrases.render_template` 的文案自动填充）；
* 不走模板的字符串在**使用点**调 :func:`fill`；
* 当前称呼由 :class:`pet.config.Config` 在加载/写入时同步进来
  （:func:`set_address`），因此渲染层不必到处传 config。

为什么用"进程级当前值"而不是处处传参：与 ``agent_link.set_configured_pnpm_bin``
同一套做法——渲染发生在很多互不相识的模块里（监视器、歌词、菜单、识屏），
为一个展示用字符串给每条调用链加参数，代价远大于收益。写入口只有 Config 一处，
读取侧只读，不存在并发写。
"""
from __future__ import annotations

#: 内置默认称呼（历史文案里的那个词）。
DEFAULT_ADDRESS = "主人"
#: 模板占位符。
PLACEHOLDER = "{user}"
#: 用户自定义称呼的长度上限（中文名/昵称够用，也防止有人粘一整段进来）。
MAX_LENGTH = 12

_address = DEFAULT_ADDRESS


def clean_address(value: object) -> str:
    """清洗用户输入：去空白、限长、禁花括号（会破坏模板占位符语义）。

    空值回落默认称呼——"清空 = 恢复默认"比"清空 = 桌宠不说话"更符合直觉。
    """
    text = str(value or "").strip()
    text = text.replace("{", "").replace("}", "")
    text = " ".join(text.split())
    text = text[:MAX_LENGTH]
    return text or DEFAULT_ADDRESS


def set_address(value: object) -> str:
    """设置当前称呼（Config 加载/写入时调用），返回清洗后的值。"""
    global _address
    _address = clean_address(value)
    return _address


def current() -> str:
    """当前称呼。"""
    return _address


def fill(text: object) -> str:
    """把文本里的 ``{user}`` 换成当前称呼（非模板字符串用）。"""
    return str(text).replace(PLACEHOLDER, _address)


def restore_default() -> str:
    """恢复内置默认称呼（"恢复默认"入口/测试收口用）。"""
    return set_address(DEFAULT_ADDRESS)
