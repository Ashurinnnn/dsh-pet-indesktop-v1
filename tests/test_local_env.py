# -*- coding: utf-8 -*-
"""本机环境探测（pet/local_env.py）与它在启动器/状态探测里的接线测试。

覆盖三层：
1. 纯探测逻辑（端口清单、环境识别、托管归属判定的"活跃度"门槛）；
2. 接线——harness_launcher 复用同一份端口清单、托管启动器掌管 dsh 时不再另起
   一个实例（这是本机真实场景下的关键行为：Unsloth 拉起的 dsh + 桌宠又按官方
   路径起一个 = 两个 home 不同、互不认识的实例）；
3. dsh_state 的端口候选与设置页「本机环境」面板。

所有用例都必须与跑测试这台机器无关：探测会去看可执行文件、/Applications、
真实 home 与本地端口，不隔离就会读到开发者的真实环境。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pet import dsh_homes as dsh_homes_mod
from pet import harness_launcher, local_env


@pytest.fixture(autouse=True)
def _isolated_machine(tmp_path, monkeypatch):
    """把"这台机器"换成一个空环境：没有可执行文件、没有 .app、没有真实 home。"""
    monkeypatch.setattr(local_env, "_which", lambda name, path=None: None)
    monkeypatch.setattr(local_env, "_app_bundle_exists", lambda name: False)
    monkeypatch.setattr(local_env, "_port_open", lambda port, host="127.0.0.1": False)
    monkeypatch.setattr(dsh_homes_mod, "default_standard_home", lambda: tmp_path / ".dsh")
    monkeypatch.setattr(dsh_homes_mod, "unsloth_agents_dir",
                        lambda env=None: tmp_path / "agents")
    monkeypatch.delenv("DSH_PORT", raising=False)


def _make_managed_home(root: Path, name: str = "dsh", *, age_s: float = 0.0) -> Path:
    home = root / name
    (home / "profiles" / "web").mkdir(parents=True)
    if age_s:
        stamp = time.time() - age_s
        os.utime(home, (stamp, stamp))
    return home


# ============================================================================
# 1. 纯探测逻辑
# ============================================================================
class TestDshServicePorts:
    def test_ordering_and_defaults(self):
        ports = local_env.dsh_service_ports(configured=1234, env={})
        assert ports == [1234, *local_env.DSH_DEFAULT_PORTS]

    def test_env_port_is_included_after_configured(self):
        assert local_env.dsh_service_ports(1234, {"DSH_PORT": "5678"}) == [1234, 5678, 3080, 38080]

    def test_dedupes(self):
        assert local_env.dsh_service_ports(3080, {"DSH_PORT": "3080"}) == [3080, 38080]

    @pytest.mark.parametrize("bad", [None, "", "abc", 0, -1, 70000])
    def test_invalid_values_are_ignored(self, bad):
        ports = local_env.dsh_service_ports(bad, {"DSH_PORT": str(bad)})
        assert ports == list(local_env.DSH_DEFAULT_PORTS)

    def test_find_running_dsh_returns_first_open(self, monkeypatch):
        monkeypatch.setattr(local_env, "_port_open", lambda port, host="127.0.0.1": port == 3080)
        assert local_env.find_running_dsh(None, {}) == 3080

    def test_find_running_dsh_none_when_all_closed(self):
        assert local_env.find_running_dsh(None, {}) is None


class TestDetectStacks:
    def test_empty_machine_reports_nothing_present(self):
        stacks = local_env.detect_stacks(env={"PATH": ""})
        assert {s.key for s in stacks} == {"dsh", "unsloth", "ollama", "llama_cpp", "lm_studio"}
        assert not any(s.present for s in stacks)

    def test_installed_binary_is_reported_with_evidence(self, monkeypatch):
        monkeypatch.setattr(
            local_env, "_which",
            lambda name, path=None: f"/opt/homebrew/bin/{name}" if name == "dsh" else None,
        )
        dsh = next(s for s in local_env.detect_stacks(env={"PATH": "/x"}) if s.key == "dsh")
        assert dsh.installed is True
        assert "/opt/homebrew/bin/dsh" in dsh.detail

    def test_running_service_is_detected_by_port(self, monkeypatch):
        monkeypatch.setattr(local_env, "_port_open",
                            lambda port, host="127.0.0.1": port == local_env.OLLAMA_PORT)
        ollama = next(s for s in local_env.detect_stacks(env={"PATH": ""}) if s.key == "ollama")
        assert ollama.running is True
        assert ollama.installed is False
        assert ollama.present is True

    def test_probe_ports_false_does_no_connections(self, monkeypatch):
        def _boom(port, host="127.0.0.1"):
            raise AssertionError("不该连接端口")

        monkeypatch.setattr(local_env, "_port_open", _boom)
        local_env.detect_stacks(probe_ports=False, env={"PATH": ""})

    def test_unsloth_reported_via_agents_root(self, tmp_path):
        _make_managed_home(tmp_path / "agents")
        unsloth = next(s for s in local_env.detect_stacks(env={"PATH": ""}) if s.key == "unsloth")
        assert unsloth.installed is True
        assert unsloth.running is True, "有活跃托管 home 就算在跑"


class TestManagedOwnerLiveness:
    """归属判定必须看"是不是真在用"，不能只看目录在不在。

    托管启动器的临时 home 用完会留在磁盘上；拿几天前的残留去否决用户"启动官方
    dsh"的意图，是过度推断。
    """

    def test_stale_home_does_not_claim_ownership(self, tmp_path):
        _make_managed_home(tmp_path / "agents", "unsloth-dsh-old",
                           age_s=local_env.MANAGED_LIVE_WINDOW_S + 3600)
        assert local_env.managed_homes_active() == []
        assert local_env.managed_dsh_owner() is None

    def test_fresh_home_claims_ownership(self, tmp_path):
        _make_managed_home(tmp_path / "agents")
        assert local_env.managed_dsh_owner() == "unsloth"

    def test_window_is_configurable(self, tmp_path):
        _make_managed_home(tmp_path / "agents", age_s=600)
        assert local_env.managed_homes_active(window_s=60) == []
        assert len(local_env.managed_homes_active(window_s=3600)) == 1


# ============================================================================
# 2. 接线：harness_launcher / dsh_state
# ============================================================================
class TestHarnessLauncherWiring:
    def test_candidate_ports_share_one_source(self, monkeypatch):
        monkeypatch.setenv("DSH_PORT", "9999")
        assert harness_launcher._candidate_ports(1234) == local_env.dsh_service_ports(1234)

    def test_already_running_wins_over_managed_owner(self, tmp_path, monkeypatch):
        """本机已有实例可复用时先复用，不管它是谁拉起的。"""
        _make_managed_home(tmp_path / "agents")
        monkeypatch.setattr(harness_launcher, "is_running", lambda port=0: int(port) == 3080)
        status, url = harness_launcher.launch_harness(open_browser=False)
        assert status == "already"
        assert url.endswith(":3080")

    def test_managed_owner_blocks_spawning_a_second_instance(self, tmp_path, monkeypatch):
        """Unsloth 掌管 dsh 时，桌宠不许再按官方路径起一个（会多出第二个 home）。"""
        _make_managed_home(tmp_path / "agents")
        monkeypatch.setattr(harness_launcher, "is_running", lambda port=0: False)
        spawned: list = []
        monkeypatch.setattr(harness_launcher, "_spawn", lambda cmd: spawned.append(cmd))

        status, info = harness_launcher.launch_harness(open_browser=False)

        assert status == "managed"
        assert info == "unsloth"
        assert spawned == [], "绝不能在托管实例之外再起一个 dsh"

    def test_stale_managed_home_does_not_block_official_launch(self, tmp_path, monkeypatch):
        _make_managed_home(tmp_path / "agents", age_s=local_env.MANAGED_LIVE_WINDOW_S + 3600)
        monkeypatch.setattr(harness_launcher, "is_running", lambda port=0: False)
        spawned: list = []
        monkeypatch.setattr(harness_launcher, "_spawn", lambda cmd: spawned.append(cmd))
        monkeypatch.setattr(harness_launcher, "_find_launch_command", lambda port=0: ["dsh", "x"])

        status, _info = harness_launcher.launch_harness(open_browser=False)

        assert status == "started"
        assert spawned == [["dsh", "x"]]

    def test_not_found_still_reported_without_managed_owner(self, monkeypatch):
        monkeypatch.setattr(harness_launcher, "is_running", lambda port=0: False)
        monkeypatch.setattr(harness_launcher, "_find_launch_command", lambda port=0: None)
        status, _info = harness_launcher.launch_harness(open_browser=False)
        assert status == "not-found"


class TestDshStateWiring:
    def test_candidate_ports_come_from_local_env(self, tmp_path, monkeypatch):
        QApplication.instance() or QApplication([])
        from pet.dsh_state import DshStateTracker

        monkeypatch.setenv("DSH_PORT", "9999")
        tracker = DshStateTracker(config_dir=tmp_path, port=1234)
        try:
            assert tracker._candidate_ports() == local_env.dsh_service_ports(1234)
            assert set(local_env.DSH_DEFAULT_PORTS) <= set(tracker._candidate_ports())
        finally:
            tracker.stop()


# ============================================================================
# 3. 设置页「本机环境」面板
# ============================================================================
class TestSettingsEnvPanel:
    def _dialog(self, tmp_path):
        QApplication.instance() or QApplication([])
        from pet.config import Config
        from pet.modern_settings_dialog import ModernSettingsDialog

        return ModernSettingsDialog(Config(tmp_path))

    def test_rows_cover_every_stack_plus_refresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(local_env, "_which",
                            lambda name, path=None: "/usr/bin/ollama" if name == "ollama" else None)
        dlg = self._dialog(tmp_path)
        labels = dlg.local_env_status_labels
        assert set(labels) == {"dsh", "unsloth", "ollama", "llama_cpp", "lm_studio"}
        assert labels["ollama"].text() == "已安装"
        assert labels["dsh"].text() == "未检测到"
        from PySide6.QtWidgets import QFrame

        assert dlg.findChild(QFrame, "settingRow_local_env_refresh") is not None

    def test_refresh_updates_labels_in_place(self, tmp_path, monkeypatch):
        dlg = self._dialog(tmp_path)
        label = dlg.local_env_status_labels["ollama"]
        assert label.text() == "未检测到"

        monkeypatch.setattr(local_env, "_port_open",
                            lambda port, host="127.0.0.1": port == local_env.OLLAMA_PORT)
        from pet import settings_env_panel

        settings_env_panel.refresh(dlg)
        assert label.text() == "运行中"

    def test_refresh_without_rows_is_silent(self, tmp_path):
        from pet import settings_env_panel

        class _Host:
            pass

        settings_env_panel.refresh(_Host())  # 不该抛
