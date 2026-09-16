# -*- coding: utf-8 -*-
"""托管 dsh home（Unsloth Studio 等）的原生适配测试。

覆盖三块：
1. ``pet.dsh_patch_layer``——``cordis.patch.yml`` 的文本手术（幂等、不吞用户内容、
   形态不认识就拒绝改写）；
2. ``pet.dsh_homes``——托管 home 的发现与归属判定；
3. ``DshMonitor`` 的跨 home 安装编排——托管 home 走补丁层（不碰 package.json、
   不需要 pnpm），标准 home 仍走 pnpm + bundles。

回归的原始症状：桌宠只认 ``DSH_HOME`` / ``~/.dsh``，Unsloth 拉起的 dsh 完全看
不见，"安装成功"却装到了没在跑的实例上，只能靠外部脚本手工挂载。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from pet import agent_link
from pet import dsh_homes as dsh_homes_mod
from pet import dsh_patch_layer as patch_mod
from pet.agent_link import DshMonitor

TEMPLATE = (
    "# Your patch layer for this dsh profile, applied after every bundle layer:\n"
    "# a top-level YAML array of loader patch entries (id-targeted config\n"
    "# overrides, disables, and insert lists; `!!js` expressions allowed).\n"
    "[]\n"
)


@pytest.fixture(autouse=True)
def _tmp_standard_home(tmp_path, monkeypatch):
    """本模块用例一律用一个不存在的标准 home。

    否则 ``DSH_PROFILE_HOME`` 会落到跑测试这台机器上真实的 dsh 安装（开发者本机
    的 ``~/.dsh``，或 ``DSH_HOME`` 指向的 Unsloth home），用例就会去读、甚至去改
    它。需要"标准 home 也参与安装"的用例自行把它建出来。
    """
    monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", tmp_path / "dot-dsh")
    monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)


# ============================================================================
# 1. 补丁层文本手术
# ============================================================================
class TestPatchLayer:
    def test_upsert_into_empty_template(self):
        out = patch_mod.upsert_entry(TEMPLATE, patch_mod.build_bridge_entry())
        assert patch_mod.has_entry(out)
        assert "id: dsh-pet-bridge" in out
        assert "name: '@dsh-pet/bridge'" in out
        # 空数组字面量必须被换掉，否则 YAML 里同时存在 [] 和条目 → 解析报错
        assert "\n[]" not in out

    def test_upsert_is_idempotent(self):
        once = patch_mod.upsert_entry(TEMPLATE, patch_mod.build_bridge_entry())
        twice = patch_mod.upsert_entry(once, patch_mod.build_bridge_entry())
        assert once == twice, "反复挂载不能每次多长一段/一个空行"

    def test_upsert_keeps_user_entries(self):
        text = "- id: foo\n  config:\n    a: 1\n\n- insert:\n    - id: bar\n      name: 'x'\n"
        out = patch_mod.upsert_entry(text, patch_mod.build_bridge_entry())
        assert "- id: foo" in out and "    a: 1" in out
        assert "- id: bar" in out and "name: 'x'" in out

    def test_remove_restores_empty_array(self):
        out = patch_mod.remove_entry(patch_mod.upsert_entry(
            TEMPLATE, patch_mod.build_bridge_entry()))
        assert not patch_mod.has_entry(out)
        assert out.strip().endswith("[]"), "删光条目后要留回空数组字面量"

    def test_remove_keeps_template_comments_and_user_comments(self):
        """回归：删插件不能把用户原有的补丁层注释一起删掉。

        说明注释与用户注释中间只隔空行时，它们会落进同一个"原样段落"；
        整段丢弃就会吞掉用户内容。
        """
        out = patch_mod.remove_entry(patch_mod.upsert_entry(
            TEMPLATE, patch_mod.build_bridge_entry()))
        assert "Your patch layer for this dsh profile" in out
        assert "`!!js` expressions allowed" in out

        text = "# comment\n- insert:\n    - id: user-thing\n      name: 'y'\n\n# trailing note\n"
        out = patch_mod.remove_entry(patch_mod.upsert_entry(text, patch_mod.build_bridge_entry()))
        assert "# trailing note" in out, "条目后面的用户注释必须原样留着"

    def test_remove_without_entry_leaves_file_untouched(self):
        text = "- id: foo\n  config:\n    a: 1\n"
        assert patch_mod.remove_entry(text) == text, "没装过就别重排人家的文件"

    def test_ignores_previously_patched_file(self):
        """已经是手工脚本挂过的文件：再挂一次收敛到同一种形态。"""
        legacy = (
            "# dsh-pet 桌宠桥接：挂载 Node 半侧（agent 状态事件 → 本地 jsonl）\n"
            "- insert:\n    - id: dsh-pet-bridge\n      name: '@dsh-pet/bridge'\n"
        )
        once = patch_mod.upsert_entry(legacy, patch_mod.build_bridge_entry())
        assert once.count("id: dsh-pet-bridge") == 1, "不能插成两条"
        assert once == patch_mod.upsert_entry(once, patch_mod.build_bridge_entry())

    @pytest.mark.parametrize("bad", ["foo: 1\n", "- id: a\nnot-indented\n", "{}\n"])
    def test_rejects_unknown_shape(self, bad):
        with pytest.raises(patch_mod.PatchLayerError):
            patch_mod.upsert_entry(bad, patch_mod.build_bridge_entry())

    def test_write_atomic_has_no_leftover_temp(self, tmp_path):
        target = tmp_path / "cordis.patch.yml"
        patch_mod.write_atomic(target, "[]\n")
        assert target.read_text(encoding="utf-8") == "[]\n"
        assert list(tmp_path.glob("*.tmp")) == []


# ============================================================================
# 2. 托管 home 发现
# ============================================================================
class TestDshHomeDiscovery:
    def _make_unsloth(self, tmp_path: Path) -> Path:
        root = tmp_path / "agents"
        (root / "dsh" / "profiles" / "web").mkdir(parents=True)
        for name in ("aaa", "bbb"):
            (root / ".tmp" / f"unsloth-dsh-{name}" / "profiles").mkdir(parents=True)
        return root

    def test_discovers_persisted_and_transient_homes(self, tmp_path):
        root = self._make_unsloth(tmp_path)
        homes = dsh_homes_mod.discover_managed_homes({"UNSLOTH_AGENTS_DIR": str(root)})
        names = [h.path.name for h in homes]
        assert names[0] == "dsh", "持久 home 必须排在最前（它一直有效）"
        assert set(names[1:]) == {"unsloth-dsh-aaa", "unsloth-dsh-bbb"}
        assert all(h.managed for h in homes)
        assert all(str(h.path).startswith(str(root)) for h in homes)

    def test_missing_homes_are_not_listed(self, tmp_path):
        homes = dsh_homes_mod.discover_managed_homes(
            {"UNSLOTH_AGENTS_DIR": str(tmp_path / "nope")})
        assert homes == [], "启动器没起过 dsh 时不该凭空报出 home"

    def test_is_managed_path_by_path_not_launch_mode(self, tmp_path):
        root = self._make_unsloth(tmp_path)
        env = {"UNSLOTH_AGENTS_DIR": str(root)}
        assert dsh_homes_mod.is_managed_path(root / "dsh", env)
        assert dsh_homes_mod.is_managed_path(root / ".tmp" / "unsloth-dsh-aaa", env)
        assert not dsh_homes_mod.is_managed_path(tmp_path / "elsewhere" / ".dsh", env)

    def test_order_homes_dedupes_and_prefers_standard_first(self, tmp_path):
        a = dsh_homes_mod.DshHome(tmp_path / "a", dsh_homes_mod.KIND_STANDARD, "std")
        b = dsh_homes_mod.DshHome(tmp_path / "a", dsh_homes_mod.KIND_MANAGED, "mgr")
        c = dsh_homes_mod.DshHome(tmp_path / "c", dsh_homes_mod.KIND_MANAGED, "mgr")
        ordered = dsh_homes_mod.order_homes([a], [b, c])
        assert [h.path.name for h in ordered] == ["a", "c"]
        assert ordered[0].kind == dsh_homes_mod.KIND_STANDARD


class TestDshHomesIntegration:
    def test_dsh_home_env_pointing_at_unsloth_is_treated_as_managed(self, tmp_path, monkeypatch):
        """从 unsloth 的 shell 里拉起桌宠时 DSH_HOME 就指着托管 home。

        挂载策略必须按路径判定，否则"从 Dock 启动走补丁层、从 shell 启动改
        package.json"，行为随启动方式漂移。
        """
        root = tmp_path / "agents"
        (root / "dsh" / "profiles" / "web").mkdir(parents=True)
        monkeypatch.setattr(agent_link, "_managed_dsh_homes",
                            lambda: dsh_homes_mod.discover_managed_homes(
                                {"UNSLOTH_AGENTS_DIR": str(root)}))
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", root / "dsh")
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)

        homes = agent_link.dsh_homes()
        assert [h.kind for h in homes] == [dsh_homes_mod.KIND_MANAGED]
        assert homes[0].path == root / "dsh"

    def test_standard_home_plus_managed_homes(self, tmp_path, monkeypatch):
        root = tmp_path / "agents"
        (root / "dsh" / "profiles" / "web").mkdir(parents=True)
        standard = tmp_path / "dot-dsh"
        (standard / "profiles" / "web").mkdir(parents=True)
        monkeypatch.setattr(agent_link, "_managed_dsh_homes",
                            lambda: dsh_homes_mod.discover_managed_homes(
                                {"UNSLOTH_AGENTS_DIR": str(root)}))
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", standard)

        homes = agent_link.dsh_homes()
        assert [h.kind for h in homes] == [
            dsh_homes_mod.KIND_STANDARD, dsh_homes_mod.KIND_MANAGED,
        ]


# ============================================================================
# 3. 托管 home 的安装 / 卸载编排
# ============================================================================
def _make_managed_home(root: Path, *, name: str = "dsh",
                       profile: str = "web") -> dsh_homes_mod.DshHome:
    home = root / name
    (home / "profiles" / profile).mkdir(parents=True)
    (home / "profiles" / profile / "package.json").write_text(
        json.dumps({"name": f"dsh-profile-{profile}", "dependencies": {}}),
        encoding="utf-8",
    )
    (home / "profiles" / profile / "cordis.patch.yml").write_text(TEMPLATE, encoding="utf-8")
    return dsh_homes_mod.DshHome(home, dsh_homes_mod.KIND_MANAGED, "Unsloth Studio（临时实例）",
                                 "Unsloth（测试）")


@pytest.fixture
def plugin_dir(tmp_path) -> Path:
    plugin = tmp_path / "bundled" / "dsh-pet-bridge"
    plugin.mkdir(parents=True)
    (plugin / "index.js").write_text("// bridge v1\n", encoding="utf-8")
    (plugin / "package.json").write_text(
        json.dumps({"name": "@dsh-pet/bridge", "main": "index.js"}), encoding="utf-8")
    # 副本里绝不能带 node_modules：插件要用宿主的 @deepseek-ai/dsh-llm
    (plugin / "node_modules").mkdir()
    return plugin


class TestManagedHomeInstall:
    def test_install_writes_patch_layer_copy_and_link(self, tmp_path, plugin_dir, monkeypatch):
        home = _make_managed_home(tmp_path / "agents")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [home])

        names, errs = agent_link._install_managed_home(home, plugin_dir)

        assert errs == []
        assert [p.name for p in names] == ["web"]
        profile = home.path / "profiles" / "web"
        patch = (profile / "cordis.patch.yml").read_text(encoding="utf-8")
        assert patch_mod.has_entry(patch)
        copy = home.path / "profiles" / "dsh-pet-bridge"
        assert (copy / "index.js").read_text(encoding="utf-8") == "// bridge v1\n"
        assert not (copy / "node_modules").exists(), "副本不能带 node_modules"
        link = profile / "node_modules" / "@dsh-pet" / "bridge"
        assert link.exists() and (link / "index.js").is_file()

    def test_install_does_not_touch_package_json(self, tmp_path, plugin_dir):
        """托管 home 的 package.json 归启动器，桌宠一个字都不许改。"""
        home = _make_managed_home(tmp_path / "agents")
        manifest = home.path / "profiles" / "web" / "package.json"
        before = manifest.read_text(encoding="utf-8")

        agent_link._install_managed_home(home, plugin_dir)

        assert manifest.read_text(encoding="utf-8") == before

    def test_install_is_idempotent(self, tmp_path, plugin_dir):
        home = _make_managed_home(tmp_path / "agents")
        agent_link._install_managed_home(home, plugin_dir)
        patch_file = home.path / "profiles" / "web" / "cordis.patch.yml"
        first = patch_file.read_text(encoding="utf-8")
        mtime = patch_file.stat().st_mtime_ns

        agent_link._install_managed_home(home, plugin_dir)

        assert patch_file.read_text(encoding="utf-8") == first
        assert patch_file.stat().st_mtime_ns == mtime, "内容没变就不该重写（热加载会被无谓触发）"

    def test_refresh_updates_stale_copy(self, tmp_path, plugin_dir):
        home = _make_managed_home(tmp_path / "agents")
        agent_link._install_managed_home(home, plugin_dir)
        copy = home.path / "profiles" / "dsh-pet-bridge" / "index.js"
        copy.write_text("// bridge v0\n", encoding="utf-8")

        assert agent_link._managed_bridge_copy_stale(home.path, plugin_dir) is True
        agent_link._install_managed_home(home, plugin_dir)
        assert copy.read_text(encoding="utf-8") == "// bridge v1\n"
        assert agent_link._managed_bridge_copy_stale(home.path, plugin_dir) is False

    def test_install_skips_home_without_profile(self, tmp_path, plugin_dir):
        home = dsh_homes_mod.DshHome(tmp_path / "agents" / "dsh",
                                     dsh_homes_mod.KIND_MANAGED, "Unsloth", "Unsloth")
        (home.path / "profiles").mkdir(parents=True)

        names, errs = agent_link._install_managed_home(home, plugin_dir)

        assert names == []
        assert errs and "还没有 dsh profile" in errs[0]

    def test_install_refuses_to_corrupt_unrecognized_patch_layer(self, tmp_path, plugin_dir):
        home = _make_managed_home(tmp_path / "agents")
        patch_file = home.path / "profiles" / "web" / "cordis.patch.yml"
        patch_file.write_text("insert: not-a-list\n", encoding="utf-8")

        names, errs = agent_link._install_managed_home(home, plugin_dir)

        assert names == []
        assert errs and "补丁层格式无法识别" in errs[0]
        assert patch_file.read_text(encoding="utf-8") == "insert: not-a-list\n", "宁可挂不上也不改坏"

    def test_bridge_copy_is_not_mistaken_for_a_profile(self, tmp_path, plugin_dir):
        """回归：profiles/dsh-pet-bridge 自己的 package.json name 就是 @dsh-pet/bridge。

        老实现把它当成一个 dsh profile 去"安装"，会去刷新它自己的 link。
        """
        home = _make_managed_home(tmp_path / "agents")
        agent_link._install_managed_home(home, plugin_dir)

        assert [p.name for p in agent_link._real_profiles_of(home.profiles_dir)] == ["web"]


class TestManagedHomeUninstall:
    def test_uninstall_removes_all_three_artifacts(self, tmp_path, plugin_dir):
        home = _make_managed_home(tmp_path / "agents")
        agent_link._install_managed_home(home, plugin_dir)
        profile = home.path / "profiles" / "web"

        problems = agent_link._uninstall_managed_home(home)

        assert problems == []
        assert not patch_mod.has_entry(
            (profile / "cordis.patch.yml").read_text(encoding="utf-8"))
        assert not (profile / "node_modules" / "@dsh-pet" / "bridge").exists()
        assert not (home.path / "profiles" / "dsh-pet-bridge").exists()

    def test_uninstall_is_idempotent(self, tmp_path, plugin_dir):
        home = _make_managed_home(tmp_path / "agents")
        assert agent_link._uninstall_managed_home(home) == []
        assert agent_link._uninstall_managed_home(home) == []


class TestInstallBridgeAcrossHomes:
    def test_managed_only_install_needs_no_pnpm(self, tmp_path, plugin_dir, monkeypatch):
        """托管 home 是纯文件操作：没装 node/pnpm 也必须挂得上（macOS 常态）。"""
        home = _make_managed_home(tmp_path / "agents")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [home])
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", tmp_path / "dot-dsh")
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))
        monkeypatch.setattr(agent_link, "_pnpm_command", lambda: None)
        monkeypatch.setattr(agent_link.shutil, "which", lambda name: None)

        ok, message = DshMonitor.install_bridge()

        assert ok is True, message
        assert "1 个 dsh 实例" in message
        profile = home.path / "profiles" / "web"
        assert patch_mod.has_entry((profile / "cordis.patch.yml").read_text(encoding="utf-8"))

    def test_standard_home_without_pnpm_still_reports_problem(self, tmp_path, plugin_dir, monkeypatch):
        """反向：标准 home 少 pnpm 时仍要给出可操作的提示，不能被托管 home 的成功掩盖。"""
        standard = tmp_path / "dot-dsh"
        (standard / "profiles" / "web").mkdir(parents=True)
        (standard / "profiles" / "web" / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [])
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", standard)
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))
        monkeypatch.setattr(agent_link, "_pnpm_command", lambda: None)

        # 有 node 但没 pnpm：提示要指向 pnpm 的安装办法
        monkeypatch.setattr(agent_link.shutil, "which",
                            lambda name: "/usr/bin/node" if name == "node" else None)
        ok, message = DshMonitor.install_bridge()
        assert ok is False
        assert "pnpm" in message

        # node 也没有：提示要指向 Node.js
        monkeypatch.setattr(agent_link.shutil, "which", lambda name: None)
        ok, message = DshMonitor.install_bridge()
        assert ok is False
        assert "Node" in message or "node" in message

    def test_install_covers_standard_and_managed(self, tmp_path, plugin_dir, monkeypatch):
        standard = tmp_path / "dot-dsh"
        profile = standard / "profiles" / "web"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text("{}", encoding="utf-8")
        home = _make_managed_home(tmp_path / "agents")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [home])
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", standard)
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))
        monkeypatch.setattr(agent_link, "_pnpm_command", lambda: ["pnpm"])

        def fake_add(profile_dir, *args):
            data = json.loads((profile_dir / "package.json").read_text(encoding="utf-8"))
            data.setdefault("dependencies", {})[agent_link.DSH_PLUGIN_NAME] = "link:x"
            data.setdefault("dsh", {}).setdefault("profile", {}).setdefault("bundles", [])
            (profile_dir / "package.json").write_text(json.dumps(data), encoding="utf-8")
            return 0, "", []

        monkeypatch.setattr(agent_link, "_run_pnpm_repairing_specs", fake_add)

        ok, message = DshMonitor.install_bridge()

        assert ok is True, message
        assert "2 个 dsh 实例" in message
        assert "~/" in message or str(standard) in message, "多 home 时标签要能区分是哪一个"
        assert "Unsloth（测试）" in message

    def test_uninstall_covers_managed_homes(self, tmp_path, plugin_dir, monkeypatch):
        home = _make_managed_home(tmp_path / "agents")
        agent_link._install_managed_home(home, plugin_dir)
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [home])
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", tmp_path / "dot-dsh")
        monkeypatch.setattr(agent_link, "_pnpm_command", lambda: None)

        assert DshMonitor.uninstall_bridge() is True
        assert not patch_mod.has_entry(
            (home.path / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8"))


class TestAdoptManagedHomes:
    def test_adopt_attaches_new_transient_home(self, tmp_path, plugin_dir, monkeypatch):
        """临时 home 是每次启动新建的：巡检必须能把它挂上（一次性安装留不住）。"""
        home = _make_managed_home(tmp_path / "agents", name=".tmp/unsloth-dsh-new")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [home])
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))

        adopted = DshMonitor.adopt_managed_homes()

        assert adopted == ["Unsloth（测试）/web"]
        assert patch_mod.has_entry(
            (home.path / "profiles" / "web" / "cordis.patch.yml").read_text(encoding="utf-8"))

    def test_adopt_is_a_noop_when_nothing_managed(self, monkeypatch, plugin_dir):
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [])
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))
        assert DshMonitor.adopt_managed_homes() == []

    def test_adopt_never_touches_standard_home(self, tmp_path, plugin_dir, monkeypatch):
        """自愈路径只许碰托管 home：用户自己的 ~/.dsh 不许被静默安装。"""
        standard = tmp_path / "dot-dsh"
        profile = standard / "profiles" / "web"
        profile.mkdir(parents=True)
        (profile / "package.json").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [])
        monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
        monkeypatch.setattr(agent_link, "DSH_PROFILE_HOME", standard)
        monkeypatch.setattr(DshMonitor, "bundled_plugin_dir", classmethod(lambda cls: plugin_dir))

        assert DshMonitor.adopt_managed_homes() == []
        assert (profile / "package.json").read_text(encoding="utf-8") == "{}"


class TestManagedHomePollScheduling:
    def test_poll_skips_when_previous_run_still_active(self, tmp_path):
        mon = DshMonitor("dsh", tmp_path)
        started: list = []

        def fake_spawn(target):
            started.append(target)

        mon.schedule_managed_home_poll(spawn=fake_spawn)
        mon.schedule_managed_home_poll(spawn=fake_spawn)
        assert len(started) == 1, "上一次还在跑时不该再起一个线程"

        # 跑完一轮后允许再次巡检
        started[0]()
        mon.schedule_managed_home_poll(spawn=fake_spawn)
        assert len(started) == 2
