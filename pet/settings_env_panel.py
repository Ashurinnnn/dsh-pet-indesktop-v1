# -*- coding: utf-8 -*-
"""设置页「本机环境」面板的行。

只是探测结果的展示层：探测逻辑在 :mod:`pet.local_env`（无 Qt 依赖，可单测），
这里负责把结果渲染成设置行的只读文案。放在独立模块是为了不撑大
``modern_settings_dialog.py``（那个文件有行数预算）。

刷新时机：设置对话框打开时探一次（每个端口 0.2s 超时，最多 5 项），点「重新探测」
再来一次。不做实时轮询——这是"看一眼这台机器上有什么"的信息面板，不是监控。
"""
from __future__ import annotations

from . import local_env


def _status_text(stack: local_env.Stack) -> str:
    if stack.running:
        return "运行中"
    if stack.installed:
        return "已安装"
    return "未检测到"


def _hint_for(stack: local_env.Stack) -> str:
    base = {
        "dsh": "官方 dsh（npm 全局安装或 npx）。桌宠的启动入口与在线状态探测都以它为准。",
        "unsloth": "托管启动器：它拉起的 dsh 用自己的 home（--persist 时固定，"
                   "默认每次新建临时 home）。桌宠会自动把桥接挂到这些实例上，"
                   "并且不再另起一个 dsh。",
        "ollama": "本地推理服务（OpenAI 兼容端口）。桌宠的 AI 对话可以指向它。",
        "llama_cpp": "本地推理服务 llama-server（OpenAI 兼容端口）。",
        "lm_studio": "本地推理服务（OpenAI 兼容端口）。",
    }.get(stack.key, "")
    return f"{base}　证据：{stack.detail}" if stack.detail else base


def env_rows(host) -> list:
    """「本机环境」设置行（只读）。每行一个环境，末尾一个「重新探测」。"""
    from PySide6.QtWidgets import QLabel, QPushButton

    from .settings_widgets import SettingRow

    labels: dict[str, object] = {}
    rows: list = []
    for stack in local_env.detect_stacks():
        label = QLabel(_status_text(stack), host)
        label.setObjectName("settingValue")
        label.setMinimumWidth(84)
        labels[stack.key] = label
        rows.append(SettingRow(
            f"local_env_{stack.key}", stack.name, _hint_for(stack), label,
        ))
    # 刷新时按 key 找回这些标签就地改字，不重建整页——设置页的行是启动时
    # 装配好的，重建会牵动搜索索引等状态。
    host.local_env_status_labels = labels

    button = QPushButton("重新探测", host)
    button.clicked.connect(lambda: refresh(host))
    rows.append(SettingRow(
        "local_env_refresh", "重新探测",
        "环境状态在设置界面打开时探测一次；装好新工具后点这里重探。",
        button,
    ))
    return rows


def refresh(host) -> None:
    """重新探测并就地更新状态标签（失败静默：展示层不该影响设置页可用性）。"""
    labels = getattr(host, "local_env_status_labels", None)
    if not isinstance(labels, dict):
        return
    try:
        stacks = {stack.key: stack for stack in local_env.detect_stacks()}
    except Exception:
        return
    for key, label in labels.items():
        stack = stacks.get(key)
        if stack is None:
            continue
        try:
            label.setText(_status_text(stack))
        except RuntimeError:  # 对话框已销毁
            return
