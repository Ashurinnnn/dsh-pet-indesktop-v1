# -*- coding: utf-8 -*-
"""dsh 安装位置（home）发现。

桌宠要把桥接插件挂到"正在运行的 dsh"上，第一步是知道它的 home 在哪。
历史实现只认 ``DSH_HOME`` 环境变量与 ``~/.dsh``，于是**托管启动器**拉起的
dsh 完全看不见。以 Unsloth Studio 为例（``unsloth start dsh``）::

    ~/.unsloth/studio/auth/agents/dsh/                     # --persist：长期固定
    ~/.unsloth/studio/auth/agents/.tmp/unsloth-dsh-XXXX/    # 默认：每次启动新建

这两种 home 都不在 ``~/.dsh`` 下，而把桌宠从 Dock/Finder 拉起时又拿不到
启动器注入的 ``DSH_HOME``（GUI 进程不继承那个 shell 的环境），结果桌宠
"安装成功"却装到了 ``~/.dsh``，真正在跑的 dsh 什么也没加载。用户只能靠
外部脚本手工挂载。

本模块把"候选 home"变成一份可枚举的清单，并标注每个 home 的**归属**：

``standard``
    用户自己装的 dsh（``DSH_HOME`` 或 ``~/.dsh``）。profiles 归用户所有，
    桥接按既有方式写进 manifest（pnpm + ``dsh.profile.bundles``）。
``managed``
    由启动器（Unsloth Studio 等）托管的 home。这类目录由启动器创建和维护，
    桌宠不改它的 ``package.json``，只往 ``cordis.patch.yml`` 补丁层插一行
    （见 :mod:`pet.dsh_patch_layer`）。

临时 home 每次启动都是新目录，所以托管 home 需要"发现即可挂"，而不是
只在用户点开关那一次安装。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# home 归属
KIND_STANDARD = "standard"
KIND_MANAGED = "managed"

# 托管启动器的固定标记：agents 根目录下，长期 home 名叫 dsh，临时 home 前缀如下。
UNSLOTH_AGENTS_DIR_ENV = "UNSLOTH_AGENTS_DIR"
UNSLOTH_TMP_PREFIX = "unsloth-dsh-"


@dataclass(frozen=True)
class DshHome:
    """一个 dsh home（含 ``profiles/`` 的那个目录）。"""

    path: Path
    kind: str
    source: str          # 展示给用户的来源说明
    label: str = ""      # 多 home 场景下的短标签（气泡/安装结果文案用）

    @property
    def managed(self) -> bool:
        return self.kind == KIND_MANAGED

    @property
    def profiles_dir(self) -> Path:
        return self.path / "profiles"

    @property
    def display(self) -> str:
        return self.label or self.source or str(self.path)


def default_standard_home() -> Path:
    """未设 ``DSH_HOME`` 时的默认 home（与 dsh 自身一致）。"""
    return Path.home() / ".dsh"


def resolve_standard_home(env: dict | None = None) -> tuple[Path, str]:
    """标准 home 及其来源说明：``DSH_HOME`` 优先，否则 ``~/.dsh``。"""
    environ = os.environ if env is None else env
    raw = str(environ.get("DSH_HOME") or "").strip()
    if raw:
        return Path(raw).expanduser(), "DSH_HOME 环境变量"
    return default_standard_home(), "默认位置 ~/.dsh"


def unsloth_agents_dir(env: dict | None = None) -> Path:
    """Unsloth Studio 的 agents 根目录（``UNSLOTH_AGENTS_DIR`` 可覆盖）。"""
    environ = os.environ if env is None else env
    override = str(environ.get(UNSLOTH_AGENTS_DIR_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".unsloth" / "studio" / "auth" / "agents"


def discover_managed_homes(env: dict | None = None) -> list[DshHome]:
    """托管启动器创建的 dsh home（长期在前，临时按新→旧）。

    只返回**目录存在**的 home：不存在的目录说明该启动器没起过 dsh，
    枚举它只会让"已安装"的判定出错。
    """
    root = unsloth_agents_dir(env)
    homes: list[DshHome] = []

    persisted = root / "dsh"
    if persisted.is_dir():
        homes.append(DshHome(persisted, KIND_MANAGED, "Unsloth Studio（--persist）",
                             "Unsloth（持久）"))

    tmp_root = root / ".tmp"
    try:
        candidates = [
            p for p in tmp_root.iterdir()
            if p.is_dir() and p.name.startswith(UNSLOTH_TMP_PREFIX)
        ]
    except OSError:
        candidates = []
    # 新的排前面：临时 home 是新进程正在用的那个，先挂它收益最大。
    candidates.sort(key=lambda p: _safe_mtime(p), reverse=True)
    for path in candidates:
        suffix = path.name[len(UNSLOTH_TMP_PREFIX):] or "?"
        homes.append(DshHome(path, KIND_MANAGED, "Unsloth Studio（临时实例）",
                             f"Unsloth（临时 {suffix}）"))

    return homes


def same_path(a: Path, b: Path) -> bool:
    """两个路径是否指向同一个位置（解析后比较；解不开就退回字符串比较）。"""
    return _identity(a) == _identity(b)


def is_managed_path(path: Path, env: dict | None = None) -> bool:
    """该路径是否落在托管启动器的 agents 目录里。

    归属按**路径**判定，不看启动方式：``DSH_HOME`` 恰好指着 Unsloth 的 home
    时（从 unsloth 的 shell 里拉起桌宠就会这样），它仍然是托管 home——否则
    同一台机器上"从 Dock 启动走补丁层、从 shell 启动改 package.json"，
    行为随启动方式漂移。
    """
    root = unsloth_agents_dir(env)
    try:
        return path.expanduser().resolve().is_relative_to(root.resolve())
    except OSError:
        return False


def order_homes(standard: list[DshHome], managed: list[DshHome]) -> list[DshHome]:
    """标准 home 在前、托管 home 在后，并按真实路径去重。

    同一目录被 ``DSH_HOME`` 和启动器同时指到时只留先出现的那条：先出现的是
    "标准"，说明用户显式指定了它，就按用户自己的 dsh 处理（走 manifest 那套），
    而不是按托管 home 只改补丁层。
    """
    seen: set[str] = set()
    unique: list[DshHome] = []
    for home in [*standard, *managed]:
        key = _identity(home.path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(home)
    return unique


def describe(homes: list[DshHome]) -> str:
    """把 home 清单渲染成一行给人看的说明（设置页/日志用）。"""
    return "；".join(f"{h.path}（{h.source}）" for h in homes) or "（未发现）"


def _identity(path: Path) -> str:
    try:
        return str(path.resolve())
    except OSError:
        return str(path)


def _safe_mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
