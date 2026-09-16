# -*- coding: utf-8 -*-
"""本机推理 / agent 环境探测。

桌宠要"自己适配"，第一步是知道这台机器上跑的是什么。以 macOS 上最常见的
四种组合为例，它们的 dsh 来源、home 位置、服务端口都不一样：

===================================  ==========================  ==================
环境                                   dsh 从哪来                   dsh home
===================================  ==========================  ==================
官方 dsh（npm -g / npx）              PATH 上的 dsh                ``~/.dsh``
Unsloth Studio（``unsloth start``）   unsloth 拉起                 ``~/.unsloth/…``
Ollama（本地推理，非 agent）           —                            —
llama.cpp / llama-server              —                            —
===================================  ==========================  ==================

**为什么要探测而不是猜**：桌宠的"启动 DeepSeek Harness"和"随桌宠自启动"
会照着官方路径去起一个 dsh。在 Unsloth 机器上那就等于凭空多出一个 home 不同、
互不认识的第二个 dsh——用户看到两个界面、状态也是断的。知道本机是哪种环境，
才能把"自己起一个"换成"告诉用户去它自己的入口起"。

这里的探测一律**只读**：看可执行文件在不在、看目录在不在、连一下本地端口，
不启动任何东西、不改任何配置。
"""
from __future__ import annotations

import os
import shutil
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import dsh_homes as dsh_homes_mod

# 环境类别
KIND_AGENT = "agent"
KIND_INFERENCE = "inference"

# 本地推理服务的默认端口（用于"在不在跑"的探测）
OLLAMA_PORT = 11434
LLAMA_CPP_PORT = 8080
LM_STUDIO_PORT = 1234

# dsh web 的候选端口：官方默认 + 桌宠自启动默认（见 harness_launcher）
DSH_DEFAULT_PORTS = (3080, 38080)

_PROBE_TIMEOUT_S = 0.2


def _which(name: str, path: str | None = None) -> str | None:
    """Node/PATH 查找的接缝：测试要能把它打桩成"这台机器什么都没装"，
    而直接 patch ``shutil.which`` 会影响整个进程（别的模块也在用）。"""
    return shutil.which(name, path=path)

# 托管 home "像是正在被用"的窗口：dsh 跑着时 sessions/ 一直在写，mtime 会保持新鲜；
# 而托管启动器的临时 home 用完会留在磁盘上，几天前的残留不算"在用"。
MANAGED_LIVE_WINDOW_S = 6 * 3600.0


@dataclass(frozen=True)
class Stack:
    """一种本机环境/服务的探测结果。"""

    key: str
    name: str
    kind: str
    installed: bool
    running: bool
    detail: str = ""

    @property
    def present(self) -> bool:
        return self.installed or self.running


def _app_bundle_exists(name: str) -> bool:
    """macOS 上某 .app 是否存在（用户也可能装在 ~/Applications）。"""
    if sys.platform != "darwin":
        return False
    return any(
        (root / f"{name}.app").exists()
        for root in (Path("/Applications"), Path.home() / "Applications")
    )


def _port_open(port: int, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=_PROBE_TIMEOUT_S):
            return True
    except OSError:
        return False


def unsloth_agents_root() -> Path | None:
    """Unsloth Studio 的 agents 根目录（存在才返回）。"""
    root = dsh_homes_mod.unsloth_agents_dir()
    return root if root.is_dir() else None


def detect_stacks(*, probe_ports: bool = True, env: dict | None = None) -> list[Stack]:
    """探测本机的 agent / 推理环境（顺序即展示顺序）。

    ``probe_ports=False`` 时不做任何端口连接（设置页只想列"装了什么"、
    或在没有网络权限的环境里调用时用）。
    """
    environ = os.environ if env is None else env
    found: list[Stack] = []

    # —— dsh（官方） ——
    dsh_bin = _which("dsh", environ.get("PATH"))
    dsh_home = dsh_homes_mod.default_standard_home()
    dsh_installed = bool(dsh_bin) or (dsh_home / "profiles").is_dir()
    found.append(Stack(
        key="dsh", name="DeepSeek Harness", kind=KIND_AGENT,
        installed=dsh_installed,
        running=any(_port_open(p) for p in DSH_DEFAULT_PORTS) if probe_ports else False,
        detail=str(dsh_bin) if dsh_bin else (str(dsh_home) if dsh_installed else ""),
    ))

    # —— Unsloth Studio（托管 dsh 的启动器） ——
    agents_root = unsloth_agents_root()
    unsloth_bin = _which("unsloth", environ.get("PATH"))
    managed_homes = dsh_homes_mod.discover_managed_homes(environ) if agents_root else []
    active_homes = managed_homes_active(environ) if managed_homes else []
    unsloth_installed = bool(agents_root) or bool(unsloth_bin) or _app_bundle_exists("Unsloth Studio")
    detail = ""
    if managed_homes:
        detail = f"{len(managed_homes)} 个 dsh home" + (
            f"，{len(active_homes)} 个活跃：{active_homes[0].path}" if active_homes
            else f"（均非近期写入）：{managed_homes[0].path}"
        )
    elif agents_root:
        detail = str(agents_root)
    elif unsloth_bin:
        detail = str(unsloth_bin)
    found.append(Stack(
        key="unsloth", name="Unsloth Studio", kind=KIND_AGENT,
        installed=unsloth_installed,
        # "在跑"的判据不是端口而是"有活跃的托管 home"——unsloth 拉起的 dsh
        # 就是它的实例，home 最近在写就说明它正跑着（见 managed_homes_active）。
        running=bool(active_homes),
        detail=detail,
    ))

    # —— Ollama ——
    ollama_bin = _which("ollama", environ.get("PATH"))
    found.append(Stack(
        key="ollama", name="Ollama", kind=KIND_INFERENCE,
        installed=bool(ollama_bin) or _app_bundle_exists("Ollama"),
        running=_port_open(OLLAMA_PORT) if probe_ports else False,
        detail=str(ollama_bin) if ollama_bin else f"127.0.0.1:{OLLAMA_PORT}",
    ))

    # —— llama.cpp / llama-server ——
    llama_bin = None
    for name in ("llama-server", "llama-cli", "llama"):
        llama_bin = _which(name, environ.get("PATH"))
        if llama_bin:
            break
    found.append(Stack(
        key="llama_cpp", name="llama.cpp", kind=KIND_INFERENCE,
        installed=bool(llama_bin),
        running=_port_open(LLAMA_CPP_PORT) if probe_ports else False,
        detail=str(llama_bin) if llama_bin else f"127.0.0.1:{LLAMA_CPP_PORT}",
    ))

    # —— LM Studio（macOS 上很常见，同样提供 OpenAI 兼容端口） ——
    found.append(Stack(
        key="lm_studio", name="LM Studio", kind=KIND_INFERENCE,
        installed=_app_bundle_exists("LM Studio"),
        running=_port_open(LM_STUDIO_PORT) if probe_ports else False,
        detail=f"127.0.0.1:{LM_STUDIO_PORT}",
    ))

    return found


def managed_homes_active(env: dict | None = None,
                         window_s: float = MANAGED_LIVE_WINDOW_S) -> list[dsh_homes_mod.DshHome]:
    """"像是正在被用"的托管 home：最近有写入的那些。

    为什么不能只看"目录存在"：托管启动器的临时 home 会一直留在磁盘上。拿一个
    几天前的残留目录去否决用户"启动官方 dsh"的意图，是过度推断（而且会让用户
    觉得桌宠坏了）。dsh 真在跑时 sessions/ 持续写入，mtime 是新鲜的。
    """
    now = time.time()
    active: list[dsh_homes_mod.DshHome] = []
    for home in dsh_homes_mod.discover_managed_homes(os.environ if env is None else env):
        try:
            fresh = now - home.path.stat().st_mtime <= window_s
        except OSError:
            fresh = False
        if fresh:
            active.append(home)
    return active


def managed_dsh_owner(env: dict | None = None) -> str | None:
    """本机 dsh 由托管启动器拉起时返回它（目前只有 Unsloth），否则 None。

    只在**看起来真在用**时才认领：见 :func:`managed_homes_active`。
    """
    if managed_homes_active(env):
        return "unsloth"
    return None


def dsh_service_ports(configured: int | None = None, env: dict | None = None) -> list[int]:
    """探测 dsh web 时要试的端口：配置端口 → DSH_PORT → 官方默认 → 自启动默认。

    顺序即优先级：用户显式配置的排最前，官方默认 3080 排在后（3080 只是
    Windows 上不宜**绑定**，作为客户端去连没问题）。
    """
    environ = os.environ if env is None else env
    ports: list[int] = []

    def _add(value) -> None:
        try:
            port = int(value)
        except (TypeError, ValueError):
            return
        if 0 < port < 65536 and port not in ports:
            ports.append(port)

    _add(configured)
    _add(environ.get("DSH_PORT"))
    for port in DSH_DEFAULT_PORTS:
        _add(port)
    return ports


def find_running_dsh(configured: int | None = None, env: dict | None = None) -> int | None:
    """正在监听的 dsh web 端口；都没有则 None。"""
    for port in dsh_service_ports(configured, env):
        if _port_open(port):
            return port
    return None


def summary_lines(stacks: list[Stack] | None = None) -> list[str]:
    """给人看的一行一条摘要（设置页/气泡/日志共用）。"""
    items = detect_stacks() if stacks is None else stacks
    lines: list[str] = []
    for stack in items:
        if stack.running:
            state = "运行中"
        elif stack.installed:
            state = "已安装"
        else:
            state = "未检测到"
        lines.append(f"{stack.name}：{state}" + (f"（{stack.detail}）" if stack.detail else ""))
    return lines


def describe_for_bubble() -> str:
    """一句话概括本机环境（"启动 dsh" 之类的动作前告诉用户它看到了什么）。"""
    items = detect_stacks(probe_ports=False)
    present = [s.name for s in items if s.installed]
    return "、".join(present) if present else "（没有检测到常见的 agent / 推理环境）"
