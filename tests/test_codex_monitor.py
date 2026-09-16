# -*- coding: utf-8 -*-
"""OpenAI Codex 联动的测试。

Codex 事件来源是它自己写的会话 rollout（``~/.codex/sessions/**/rollout-*.jsonl``），
本模块覆盖三层：

1. 记录 → 状态/工具名的映射（纯函数，不怕 Codex 版本演进时悄悄改语义）；
2. ``CodexMonitor`` 的扫描 + 增量 tail（含子代理过滤与"不重放历史"）；
3. 注册面（配置默认键、内置键、窗口门、显示名）——少一处就会表现为
   "设置页有开关但勾了没反应"。
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtWidgets import QApplication

from pet import agent_link
from pet.agent_link import (
    AgentLinkManager,
    CodexMonitor,
    codex_record_state,
    codex_record_tool,
    codex_session_is_subagent,
)

# 一条真实 rollout 的 session_meta（字段取自本机 2026-09 的实测样本，值已脱敏）
USER_META = {
    "type": "session_meta",
    "payload": {
        "session_id": "s-1", "cwd": "/tmp/work", "originator": "codex_work_desktop",
        "cli_version": "0.154.0-alpha.6.2", "source": "vscode", "thread_source": "user",
    },
}
GUARDIAN_META = {
    "type": "session_meta",
    "payload": {"session_id": "s-2", "source": {"subagent": {"other": "guardian"}},
                "thread_source": "guardian_review"},
}


def _record(payload_type: str, **extra) -> dict:
    return {"timestamp": "2026-09-16T00:00:00.000Z", "ordinal": 1,
            "type": "response_item", "payload": {"type": payload_type, **extra}}


def _event(payload_type: str, **extra) -> dict:
    return {"timestamp": "2026-09-16T00:00:00.000Z", "ordinal": 1,
            "type": "event_msg", "payload": {"type": payload_type, **extra}}


def _rollout(root: Path, name: str, meta: dict, *, day: str = "2026/09/16") -> Path:
    """在 <root>/<YYYY>/<MM>/<DD>/ 下建一份 rollout 文件。"""
    directory = root.joinpath(*day.split("/"))
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"rollout-2026-09-16T00-00-00-{name}.jsonl"
    path.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def _append(path: Path, *records: dict) -> None:
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")


# ============================================================================
# 1. 记录映射
# ============================================================================
class TestCodexRecordMapping:
    def test_task_lifecycle_drives_state(self):
        assert codex_record_state(_event("task_started")) == "working"
        assert codex_record_state(_event("task_complete")) == "idle"

    def test_reasoning_is_thinking_and_tools_are_working(self):
        assert codex_record_state(_record("reasoning")) == "thinking"
        assert codex_record_state(_record("custom_tool_call", name="exec")) == "working"
        assert codex_record_state(
            _record("function_call", name="spawn_agent", namespace="collaboration")) == "working"

    def test_bookkeeping_records_do_not_drive_state(self):
        for payload_type in ("token_count", "item_completed", "thread_settings_applied",
                             "turn_context", "world_state", "compacted"):
            assert codex_record_state(_event(payload_type)) == "", payload_type
            assert codex_record_state({"type": payload_type, "payload": {}}) == ""

    def test_malformed_records_are_inert(self):
        assert codex_record_state({}) == ""
        assert codex_record_state({"type": "event_msg", "payload": "nope"}) == ""
        assert codex_record_tool({}) == ""
        assert codex_record_tool({"payload": {"type": "reasoning"}}) == ""

    def test_tool_name_keeps_namespace(self):
        assert codex_record_tool(_record("custom_tool_call", name="exec")) == "exec"
        assert codex_record_tool(
            _record("function_call", name="spawn_agent", namespace="collaboration")
        ) == "collaboration.spawn_agent"
        # 工具记录之外的记录不产生工具名（否则状态机会被灌一堆空工具事件）
        assert codex_record_tool(_event("task_started")) == ""

    def test_subagent_session_detection(self):
        assert codex_session_is_subagent(GUARDIAN_META["payload"]) is True
        assert codex_session_is_subagent({"thread_source": "subagent"}) is True
        assert codex_session_is_subagent({"source": {"subagent": {"thread_spawn": {}}}}) is True
        assert codex_session_is_subagent(USER_META["payload"]) is False
        assert codex_session_is_subagent({}) is False, "认不出来时按用户会话处理（宁可多报）"


class TestToolLabelNamespaceFallback:
    def test_namespaced_tool_falls_back_to_last_segment(self):
        assert AgentLinkManager.tool_label("collaboration.spawn_agent") == "正在派活给子代理"
        assert AgentLinkManager.tool_label("exec") == "正在跑命令"
        assert AgentLinkManager.tool_label("") == ""
        assert AgentLinkManager.tool_label("no_such_tool") == ""

    def test_codex_tool_names_have_labels(self):
        for tool in ("exec", "exec_command", "apply_patch", "read_file", "write_file",
                     "list_dir", "update_plan", "view_image"):
            assert AgentLinkManager.tool_label(tool), tool


# ============================================================================
# 2. 监视器：扫描 + 增量 tail
# ============================================================================
class TestCodexMonitor:
    def _monitor(self, tmp_path) -> tuple[CodexMonitor, list[str], list[str]]:
        QApplication.instance() or QApplication([])
        cfg_dir = tmp_path / "cfg"
        cfg_dir.mkdir(exist_ok=True)
        states: list[str] = []
        tools: list[str] = []
        mon = CodexMonitor(cfg_dir, base_dir=tmp_path / "sessions")
        mon.state_changed.connect(lambda _agent, state: states.append(state))
        mon.activity.connect(lambda _agent, tool: tools.append(tool))
        return mon, states, tools

    def test_tails_user_session_and_maps_states(self, tmp_path):
        root = tmp_path / "sessions"
        path = _rollout(root, "abc", USER_META)
        mon, states, tools = self._monitor(tmp_path)

        mon._poll()  # 首次：发现文件、backfill 到末尾

        _append(path,
                _event("task_started"),
                _record("reasoning"),
                _record("custom_tool_call", name="exec"),
                _event("task_complete"))
        mon._poll()

        assert states == ["working", "thinking", "working", "idle"]
        assert tools == ["exec"]

    def test_does_not_replay_history_on_first_poll(self, tmp_path):
        """开启联动不该把昨天的会话重放成"刚刚完成一轮"。"""
        root = tmp_path / "sessions"
        path = _rollout(root, "old", USER_META)
        _append(path, _event("task_started"), _event("task_complete"))
        mon, states, _tools = self._monitor(tmp_path)

        mon._poll()

        assert states == [], "历史记录只用于 backfill，不产生事件"

    def test_subagent_rollouts_are_ignored(self, tmp_path):
        """子代理会话交错写 task_started/task_complete，会让完成气泡刷爆。"""
        root = tmp_path / "sessions"
        user = _rollout(root, "user", USER_META)
        sub = _rollout(root, "guardian", GUARDIAN_META)
        mon, states, _tools = self._monitor(tmp_path)
        mon._poll()

        _append(sub, _event("task_started"), _event("task_complete"))
        _append(user, _event("task_started"))
        mon._poll()

        assert states == ["working"], "只有用户会话的事件能进状态机"

    def test_ignores_rollouts_outside_active_window(self, tmp_path):
        import os
        import time as _time

        root = tmp_path / "sessions"
        path = _rollout(root, "stale", USER_META)
        old = _time.time() - CodexMonitor._ACTIVE_WINDOW_S - 3600
        os.utime(path, (old, old))
        mon, states, _tools = self._monitor(tmp_path)
        mon._poll()

        _append(path, _event("task_started"))
        os.utime(path, (old, old))
        mon._poll()

        assert states == [], "窗口外的陈年会话不该被 tail（否则历史完成会当成刚发生）"

    def test_discovers_rollout_created_after_start(self, tmp_path):
        """Codex 是新开会话就新写一份 rollout：巡检要能把它纳进来。"""
        root = tmp_path / "sessions"
        _rollout(root, "first", USER_META)
        mon, states, _tools = self._monitor(tmp_path)
        mon._poll()

        fresh = _rollout(root, "second", USER_META)
        mon._last_scan = 0.0  # 绕过降频，等价于下一次巡检
        mon._poll()
        _append(fresh, _event("task_started"))
        mon._poll()

        assert states == ["working"]

    def test_missing_sessions_dir_is_not_an_error(self, tmp_path):
        """没装 Codex 的机器：监视器照常轮询，只是永远没有事件。"""
        mon, states, tools = self._monitor(tmp_path)
        mon._poll()
        assert (states, tools) == ([], [])

    def test_truncated_line_in_rollout_is_skipped(self, tmp_path):
        root = tmp_path / "sessions"
        path = _rollout(root, "garbled", USER_META)
        mon, states, _tools = self._monitor(tmp_path)
        mon._poll()
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        _append(path, _event("task_started"))
        mon._poll()
        assert states == ["working"], "坏行不能带崩整轮读取"


# ============================================================================
# 3. 注册面
# ============================================================================
class TestCodexRegistration:
    def test_builtin_key_and_defaults(self):
        from pet import config

        assert "codex" in config._AGENT_LINK_BUILTIN_KEYS
        assert config._default_agent_link_data()["codex"] is False, "默认关（与其它 Agent 一致）"

    def test_custom_agent_key_may_not_shadow_builtin(self):
        from pet import config

        cleaned = config._clean_custom_agents([{"key": "codex", "path": "/tmp/x.jsonl"}])
        assert cleaned == [], "自定义 Agent 不许占用内置键"

    def test_config_roundtrip_keeps_codex_toggle(self):
        from pet import config

        cleaned = config._clean_agent_link_data({"codex": True})
        assert cleaned["codex"] is True

    def test_window_gate_counts_codex(self):
        class _Cfg:
            def __init__(self, data):
                self._data = data

            def get(self, key, default=None):
                return self._data.get(key, default)

        from pet.window_optional_services import WindowFeatureGateMixin

        host = WindowFeatureGateMixin()
        host.cfg = _Cfg({"agent_link": {"codex": True}})
        assert host._agent_link_wanted() is True

        host.cfg = _Cfg({"agent_link": {"dsh": False, "codex": False}})
        assert host._agent_link_wanted() is False

    def test_manager_mounts_codex_monitor(self, tmp_path):
        QApplication.instance() or QApplication([])

        class _Cfg:
            dir = tmp_path

            def __init__(self):
                self.data = {"agent_link": {"report_gates": {}}}

            def get(self, key, default=None):
                return self.data.get(key, default)

            def save(self):
                pass

        mgr = AgentLinkManager(None, _Cfg())
        try:
            assert isinstance(mgr.monitors.get("codex"), CodexMonitor)
            assert mgr.agent_names["codex"] == "Codex"
        finally:
            mgr.shutdown()

    def test_absent_hint_points_at_codex_sessions(self, tmp_path, monkeypatch):
        """开启联动但没装 Codex 时，提示要指向真实存在的判定依据。"""
        QApplication.instance() or QApplication([])

        class _Cfg:
            dir = tmp_path

            def __init__(self):
                self.data = {"agent_link": {"report_gates": {}}}

            def get(self, key, default=None):
                return self.data.get(key, default)

            def save(self):
                pass

        bubbles: list[str] = []

        class _Win:
            def show_bubble(self, text, **kwargs):
                bubbles.append(str(text))

        mgr = AgentLinkManager(_Win(), _Cfg())
        try:
            monkeypatch.setattr(agent_link.Path, "home", classmethod(lambda cls: tmp_path))
            mgr._warn_if_agent_absent("codex")
            assert bubbles and "Codex" in bubbles[0]
        finally:
            mgr.shutdown()
