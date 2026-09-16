# -*- coding: utf-8 -*-
"""``cordis.patch.yml`` 补丁层的文本级读写。

dsh 的 profile 由若干"层"叠出来：``package.json`` 里 ``dsh.profile.bundles``
列的 bundle 层，然后是本文件代表的补丁层，最后是 ``--patch`` 覆盖层。dsh
自己生成的 profile 模板里就写着 "Edit cordis.patch.yml, not this file"——
补丁层正是留给外部往 profile 里加东西的地方；profile 声明
``patchReload: live`` 时改它会**热加载**，不用重启 dsh、不打断正在进行的对话。

托管 home（Unsloth Studio 等）的桥接挂载就走这条路：只插一条 ``insert``
记录，完全不碰 ``package.json``。代价是得自己保证 YAML 合法——项目没有
PyYAML 依赖，也不打算为一个插件把它引进打包体积里，所以这里是**受控的
文本手术**：

* 只认 dsh 约定的"顶层 YAML 数组"这一种形态；
* 文法是刻意做窄的——顶层（第 0 列）只允许 ``- `` 开头的新条目、``#`` 注释、
  空行、以及空数组字面量 ``[]``；条目的内容一律是缩进行；
* 出现任何超出这套文法的行，抛 :class:`PatchLayerError` 让调用方放弃改写，
  **绝不去猜**——猜错的代价是把用户手写的补丁层改坏。

注释与空行按"原样段落"保留在原位，增删只作用于条目段，因此用户原有的
补丁条目不会被重排或丢失。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

#: 桥接条目的 id；文本里出现它即认定"这是本插件的条目"。
BRIDGE_ENTRY_ID = "dsh-pet-bridge"
#: 条目上方的说明注释（删除条目时一并清掉）。
MARKER_COMMENT = "# dsh-pet 桌宠桥接（由桌宠自动维护；关闭联动会移除这一段）"

#: 空数组字面量——dsh 新建 profile 时补丁层的初始内容。
EMPTY_ARRAY = "[]"

_RAW = "raw"
_ITEM = "item"


class PatchLayerError(ValueError):
    """补丁层不是"顶层数组"形态，或含本模块不认识的语法。"""


@dataclass
class _Segment:
    kind: str
    lines: list[str] = field(default_factory=list)


def build_bridge_entry(entry_id: str = BRIDGE_ENTRY_ID,
                       package_name: str = "@dsh-pet/bridge") -> str:
    """桥接条目的文本（含说明注释），末尾带换行。"""
    return (
        f"{MARKER_COMMENT}\n"
        "- insert:\n"
        f"    - id: {entry_id}\n"
        f"      name: '{package_name}'\n"
    )


def has_entry(text: str, entry_id: str = BRIDGE_ENTRY_ID) -> bool:
    """补丁层里是否已有该条目的 insert（只看条目段，不看注释）。"""
    return any(
        seg.kind == _ITEM and entry_id in "\n".join(seg.lines)
        for seg in _parse(text)
    )


def upsert_entry(text: str, entry: str, entry_id: str = BRIDGE_ENTRY_ID) -> str:
    """插入条目（已存在则先删后插，保证内容刷新到最新）。"""
    return _render(_segments_without_entry(_parse(text), entry_id), entry)


def remove_entry(text: str, entry_id: str = BRIDGE_ENTRY_ID) -> str:
    """删除条目；不存在时原样返回（幂等，且不重排未改动的文件）。"""
    segments = _parse(text)
    stripped = _segments_without_entry(segments, entry_id)
    if len(stripped) == len(segments):
        return text
    return _render(stripped, None)


# ---------------------------------------------------------------- 解析 / 渲染

def _parse(text: str) -> list[_Segment]:
    segments: list[_Segment] = []
    raw: list[str] = []
    item: list[str] | None = None

    def flush_raw() -> None:
        if raw:
            segments.append(_Segment(_RAW, list(raw)))
            raw.clear()

    def flush_item() -> None:
        nonlocal item
        if item is not None:
            segments.append(_Segment(_ITEM, list(item)))
            item = None

    for line in text.splitlines():
        if line[:1] == "-":
            flush_item()
            flush_raw()
            item = [line]
            continue
        if item is not None and (line[:1] in (" ", "\t")):
            item.append(line)
            continue
        flush_item()
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            if stripped == EMPTY_ARRAY:
                # 空数组字面量：只是"还没有条目"的占位，不当作内容保留
                continue
            raise PatchLayerError(
                f"补丁层出现无法识别的顶层内容（既不是条目也不是注释）: {line!r}"
            )
        raw.append(line)
    flush_item()
    flush_raw()
    return segments


def _render(segments: list[_Segment], entry: str | None) -> str:
    """把段落拼回文本。

    渲染是**规范化**的：段落之间统一空一行，原样段落不留首尾空行。这样反复
    upsert 收敛到同一个结果，不会每插一次就多长一个空行。
    """
    parts: list[str] = []
    for seg in segments:
        lines = list(seg.lines)
        if seg.kind == _RAW:
            while lines and not lines[0].strip():
                lines.pop(0)
            while lines and not lines[-1].strip():
                lines.pop()
        block = "\n".join(lines).rstrip("\n")
        if block.strip():
            parts.append(block)
    if entry is not None:
        parts.append(entry.rstrip("\n"))
    elif not any(seg.kind == _ITEM for seg in segments):
        # 条目被删光：补回空数组字面量。只剩注释的文档 YAML 解析出来是 null，
        # 不是 dsh 期望的"空的补丁层"。
        parts.append(EMPTY_ARRAY)
    if not parts:
        return EMPTY_ARRAY + "\n"
    return "\n\n".join(parts) + "\n"


def _segments_without_entry(segments: list[_Segment], entry_id: str) -> list[_Segment]:
    """剔除该条目的条目段，以及紧贴在它上面的本插件说明注释。

    说明注释与用户原有的注释可能落在同一个"原样段落"里（中间只隔空行），
    所以这里只从段落**尾部**摘掉那一行，段落其余内容原样保留——否则删插件
    会把用户自己写的补丁层说明一起删掉。
    """
    kept: list[_Segment] = []
    for seg in segments:
        if seg.kind == _ITEM and entry_id in "\n".join(seg.lines):
            if kept and kept[-1].kind == _RAW:
                trimmed = _strip_marker_comment(kept[-1].lines)
                if trimmed is not None:
                    if trimmed:
                        kept[-1] = _Segment(_RAW, trimmed)
                    else:
                        kept.pop()
            continue
        kept.append(seg)
    return kept


def _strip_marker_comment(lines: list[str]) -> list[str] | None:
    """段落尾部若是本插件的说明注释就摘掉它（连同其后空行）；否则返回 None。"""
    trimmed = list(lines)
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    if not trimmed:
        return None
    last = trimmed[-1].strip()
    if not last.startswith("#") or "dsh-pet" not in last:
        return None
    trimmed.pop()
    while trimmed and not trimmed[-1].strip():
        trimmed.pop()
    return trimmed


# ---------------------------------------------------------------- 文件读写

def read(path: Path) -> str | None:
    """读补丁层；不存在返回 None，读取失败抛 OSError。"""
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def write_atomic(path: Path, text: str) -> None:
    """原子替换（同目录临时文件 + rename），避免热加载读到写了一半的文件。"""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
