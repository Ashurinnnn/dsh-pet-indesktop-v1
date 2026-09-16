# -*- coding: utf-8 -*-
"""「称呼与名字」可自定义的测试。

两块用户可见的名字：

* **桌宠自己的名字**——角色 id ``shenshen`` 是目录名/配置值，必须保持稳定
  ASCII，但**展示**给用户的一律走 ``catalog.character_display_name()``，
  并且用户能在设置里改（``character_aliases``）。
* **它怎么称呼用户**——内置文案里的「主人」统一换成 ``{user}`` 占位符，
  由 ``user_address`` 模块的进程级当前值填充，设置页可改。

回归的原始症状：界面/气泡里露出一串 ASCII 的 ``shenshen``；用户想换个称呼
却只能去改内置 JSON。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PySide6.QtWidgets import QApplication

from pet import catalog, persona_phrases, user_address
from pet.user_address import DEFAULT_ADDRESS


@pytest.fixture(autouse=True)
def _reset_address():
    """每个用例后恢复默认称呼：进程级状态不许泄漏到别的用例。"""
    yield
    user_address.restore_default()
    persona_phrases.reload_builtin_presets()


# ============================================================================
# 1. 称呼模块
# ============================================================================
class TestAddressNormalization:
    def test_empty_falls_back_to_default(self):
        assert user_address.clean_address("") == DEFAULT_ADDRESS
        assert user_address.clean_address("   ") == DEFAULT_ADDRESS
        assert user_address.clean_address(None) == DEFAULT_ADDRESS

    def test_braces_are_stripped(self):
        """花括号会破坏模板占位符语义，必须清理掉而不是原样写进台词。"""
        assert user_address.clean_address("{user}") == "user"
        assert user_address.clean_address("老{板}") == "老板"

    def test_length_is_bounded(self):
        assert len(user_address.clean_address("名" * 50)) == user_address.MAX_LENGTH

    def test_whitespace_is_collapsed(self):
        assert user_address.clean_address("  老   板  ") == "老 板"

    def test_set_current_fill_roundtrip(self):
        assert user_address.set_address("老板") == "老板"
        assert user_address.current() == "老板"
        assert user_address.fill("{user}，{name} 完成啦") == "老板，{name} 完成啦"
        assert user_address.restore_default() == DEFAULT_ADDRESS


class TestTemplatePlaceholder:
    def test_render_template_injects_user(self):
        text = persona_phrases.render_template("{user}，{name} 完成啦", {"name": "DSH"})
        assert text == f"{DEFAULT_ADDRESS}，DSH 完成啦"

    def test_explicit_value_wins_over_injected_default(self):
        text = persona_phrases.render_template("{user}，{name}", {"user": "上游值", "name": "DSH"})
        assert text == "上游值，DSH"

    def test_address_change_is_picked_up(self):
        user_address.set_address("铲屎官")
        assert persona_phrases.render_template("{user}在吗") == "铲屎官在吗"


class TestPickerFillsAddressInFallbacks:
    """回归：兜底文案过去是**原样返回**的，写在上面的 {user} 会直接显示给用户。

    注意范围：兜底串只替换**称呼**，其余占位符保持"原文交给调用方格式化"的既有
    契约（见 test_persona_presets 的回归用例——那里断言 fallback 必须原样返回
    ``{text}``）。所以这里用不含其它占位符的串来断言。
    """

    def test_get_fallback_gets_address(self):
        picker = persona_phrases.PhrasePicker()
        out = picker.get("legacy", "绝对不存在的key", "{user}，完成啦")
        assert out == f"{DEFAULT_ADDRESS}，完成啦"

    def test_custom_fallback_gets_address(self):
        picker = persona_phrases.PhrasePicker()
        out = picker.custom({}, "绝对不存在的key", "{user}～在忙")
        assert out == f"{DEFAULT_ADDRESS}～在忙"

    def test_custom_for_agent_fallback_gets_address(self):
        picker = persona_phrases.PhrasePicker()
        out = picker.custom_for_agent({}, "dsh", "绝对不存在的key", "{user}，看这里")
        assert out == f"{DEFAULT_ADDRESS}，看这里"

    def test_other_placeholders_stay_verbatim(self):
        """兜底串里除称呼之外的占位符仍然原样返回（调用方负责格式化）。"""
        picker = persona_phrases.PhrasePicker()
        out = picker.custom({}, "balance.result", "余额情况：{text}", text="0")
        assert out == "余额情况：{text}"

    def test_builtin_preset_uses_configured_address(self):
        user_address.set_address("老板")
        picker = persona_phrases.PhrasePicker()
        out = picker.get(
            "whale_maid", "activity.default", "兜底",
            name="DSH", sessionName="s1", label="文件", tool="exec",
        )
        assert "老板" in out
        assert "主人" not in out

    def test_unknown_placeholders_stay_verbatim(self):
        """兜底渲染不能把"未来才会有的字段"变成空串或报错。"""
        picker = persona_phrases.PhrasePicker()
        out = picker.get("legacy", "绝对不存在的key", "{futureField} 在跑", name="DSH")
        assert out == "{futureField} 在跑"


class TestShippedPhrasesAreAddressFree:
    def test_presets_have_no_hardcoded_address(self):
        """内置预设里不许再写死「主人」：否则换称呼时这些话不会跟着变。"""
        for path in sorted((Path(persona_phrases.__file__).parent / "persona_presets").glob("*.json")):
            text = path.read_text(encoding="utf-8")
            assert DEFAULT_ADDRESS not in text, f"{path.name} 里还有写死的称呼"

    def test_every_preset_is_still_valid_json(self):
        for path in sorted((Path(persona_phrases.__file__).parent / "persona_presets").glob("*.json")):
            assert isinstance(json.loads(path.read_text(encoding="utf-8")), dict)


# ============================================================================
# 2. 角色显示名
# ============================================================================
class TestCharacterDisplayName:
    def test_builtin_character_has_a_readable_name(self):
        assert catalog.character_display_name("shenshen") == catalog.BUILTIN_CHARACTER_NAMES["shenshen"]
        assert catalog.character_display_name("shenshen") != "shenshen", "不许把目录 id 摆到界面上"

    def test_unknown_character_falls_back_to_id(self):
        assert catalog.character_display_name("user_made_fish") == "user_made_fish"

    def test_manifest_name_wins(self, tmp_path, monkeypatch):
        video_dir = tmp_path / "custom" / "videos"
        video_dir.mkdir(parents=True)
        (tmp_path / "custom" / "manifest.json").write_text(
            json.dumps({"name": "自定义名字"}), encoding="utf-8")
        monkeypatch.setattr(catalog, "resolve_character_video_dir", lambda cid: video_dir)
        assert catalog.character_display_name("custom") == "自定义名字"

    def test_config_alias_is_the_user_facing_override(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        assert cfg.character_alias("shenshen") == ""
        cfg.set_character_alias("shenshen", "小鲸鱼")
        assert cfg.character_alias("shenshen") == "小鲸鱼"
        cfg.set_character_alias("shenshen", "")
        assert cfg.character_alias("shenshen") == "", "清空即恢复内置名"

    def test_alias_length_is_bounded(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.set_character_alias("shenshen", "名" * 100)
        assert len(cfg.character_alias("shenshen")) == 24


# ============================================================================
# 3. 配置持久化
# ============================================================================
class TestConfigPersistence:
    def test_user_address_roundtrips_through_reload(self, tmp_path):
        """回归：reload() 只搬白名单里的键，漏登记会让这个设置"存得下、读不回"。"""
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.set("user_address", "老板")
        cfg.save()

        assert Config(tmp_path).get("user_address") == "老板"

    def test_set_syncs_render_layer_immediately(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.set("user_address", "老板")
        assert user_address.current() == "老板", "不等 save()，改了就要立刻生效"

    def test_set_cleans_value(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.set("user_address", "  {老}板  ")
        assert cfg.get("user_address") == "老板"

    def test_loading_config_publishes_address(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.set("user_address", "铲屎官")
        cfg.save()
        user_address.restore_default()

        Config(tmp_path)
        assert user_address.current() == "铲屎官"


# ============================================================================
# 4. 设置页
# ============================================================================
class TestSettingsSurface:
    def _dialog(self, tmp_path):
        QApplication.instance() or QApplication([])
        from pet.config import Config
        from pet.modern_settings_dialog import ModernSettingsDialog

        cfg = Config(tmp_path)
        return cfg, ModernSettingsDialog(cfg)

    def test_rows_show_defaults_as_placeholders(self, tmp_path):
        _cfg, dlg = self._dialog(tmp_path)
        assert dlg.pet_name_edit.placeholderText() == catalog.character_display_name("shenshen")
        assert dlg.user_address_edit.placeholderText() == DEFAULT_ADDRESS
        assert dlg.pet_name_edit.text() == "", "没改过就该是空的"
        assert dlg.user_address_edit.text() == DEFAULT_ADDRESS

    def test_save_writes_alias_and_address(self, tmp_path):
        cfg, dlg = self._dialog(tmp_path)
        dlg.pet_name_edit.setText("小鲸鱼")
        dlg.user_address_edit.setText("老板")
        dlg._save()

        reloaded = type(cfg)(tmp_path)
        assert reloaded.get("character_aliases") == {"shenshen": "小鲸鱼"}
        assert reloaded.get("user_address") == "老板"

    def test_clearing_name_removes_alias(self, tmp_path):
        cfg, dlg = self._dialog(tmp_path)
        cfg.set_character_alias("shenshen", "旧名字")
        cfg.save()

        _cfg2, dlg2 = self._dialog(tmp_path)
        dlg2.pet_name_edit.setText("")
        dlg2._save()

        assert type(cfg)(tmp_path).get("character_aliases") == {}
