# -*- coding: utf-8 -*-
"""内置「联动 Agent」注册表：键与展示名。

**为什么单独一个模块**：这份清单有两个消费者，而它们处在不同的层——
``pet.config`` 用它定义 agent_link 的 schema（开关默认值、自定义键的保留字），
``pet.agent_link`` 用它装配监视器与气泡文案。config 是最内层模块，让它反向
import agent_link 会把 Qt 与整条监视器依赖拖进配置加载路径；这里只放数据、
零依赖，两边都往下依赖。

**新增一个内置 Agent**：本文件加一行 + ``AgentLinkManager.monitors`` 里挂一个
监视器（``AGENT_NAMES`` 直接用本表的展示名）。漏了任何一边，表现都是
"设置页有开关，勾了没反应"。
"""
from __future__ import annotations

#: 内置 Agent 的键（顺序即设置页的展示顺序）。
BUILTIN_AGENT_KEYS: tuple[str, ...] = ("dsh", "claude", "cursor", "opencode", "codex")

#: 键 → 用户看到的展示名。
AGENT_DISPLAY_NAMES: dict[str, str] = {
    "dsh": "DSH",
    "claude": "Claude Code",
    "cursor": "Cursor",
    "opencode": "OpenCode",
    "codex": "Codex",
}
