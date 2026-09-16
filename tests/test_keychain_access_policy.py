# -*- coding: utf-8 -*-
"""钥匙串访问策略：什么时候可以读、什么时候一次都不许读。

回归的原始症状：**用空 API Key 连本地部署的模型（只填 localhost 地址和端口）时，
macOS 反复弹"python 想使用钥匙串"的授权框。**

根因是三条叠在一起：

1. 输入框留空**不等于**"没有 Key"——凭据存在系统钥匙串里，留空只表示"不修改"，
   于是每次解析凭据都会去读一次钥匙串；而解析凭据的调用点散布在发送消息、余额
   查询、识屏、设置页状态等热路径上。
2. macOS 对未授权条目的**每一次**读取都会弹一次授权框——不缓存就是"每发一条
   消息弹一次"。
3. 用户没有任何办法表达"这个服务不需要 Key"，历史遗留的旧 Key 会一直被翻出来
   （既弹窗，也会被发给本地端口）。

对应修复：SecretStore 进程级缓存 + 新增 delete()；ProviderConfig 增加持久化的
非机密标记 ``api_key_required``；两个设置界面都加了"不需要 API Key"开关。
"""
from __future__ import annotations

import pytest
from PySide6.QtWidgets import QApplication

from pet.chat.models import ProviderConfig

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture
def keychain(monkeypatch):
    """把真实 SecretStore 背后的 keyring 换成一个记账用的假实现。

    conftest 的 `_fake_keyring` 会把整个 SecretStore 换掉；这里要验证的正是
    SecretStore 自己的行为，所以取回真实实现，只替换它下面那一层。
    """
    import keyring

    import pet.chat.models as models

    real_store = models._REAL_SECRET_STORE_FOR_TESTS
    real_store.clear_cache_for_tests()

    class _Keyring:
        def __init__(self):
            self.items: dict[tuple[str, str], str] = {}
            self.reads: list[str] = []
            self.writes: list[str] = []
            self.deletes: list[str] = []

        def get_password(self, service, ref):
            self.reads.append(ref)
            return self.items.get((service, ref))

        def set_password(self, service, ref, value):
            self.writes.append(ref)
            self.items[(service, ref)] = value

        def delete_password(self, service, ref):
            self.deletes.append(ref)
            if (service, ref) not in self.items:
                raise KeyError("no such item")
            del self.items[(service, ref)]

    fake = _Keyring()
    # 真实 SecretStore 在 __init__ 里 `import keyring` 并持有**模块对象**，
    # 所以要换的是模块上的函数，而不是给类塞属性。
    monkeypatch.setattr(keyring, "get_password", fake.get_password)
    monkeypatch.setattr(keyring, "set_password", fake.set_password)
    monkeypatch.setattr(keyring, "delete_password", fake.delete_password)
    # conftest 默认把 SecretStore 整个换成了内存假实现；本文件要验的正是它自己，
    # 所以换回真实实现（两处模块级绑定都要换）。
    monkeypatch.setattr(models, "SecretStore", real_store)
    try:
        from pet.chat import settings_dialog as _settings_dialog

        monkeypatch.setattr(_settings_dialog, "SecretStore", real_store)
    except Exception:
        pass
    yield real_store, fake
    real_store.clear_cache_for_tests()


# ============================================================================
# 1. SecretStore 本身
# ============================================================================
class TestSecretStoreCaching:
    def test_repeated_reads_hit_the_keychain_once(self, keychain):
        """回归核心：macOS 每次未授权读取都弹窗，所以同一 ref 一个进程只读一次。"""
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/a")] = "sk-1"

        assert store().get("provider/a") == "sk-1"
        assert store().get("provider/a") == "sk-1"
        assert store().get("provider/a") == "sk-1"

        assert fake.reads == ["provider/a"], "缓存命中后不得再碰系统钥匙串"

    def test_empty_result_is_cached_too(self, keychain):
        """读不到也要缓存：否则没配 Key 的用户每次都白弹一次窗。"""
        store, fake = keychain
        assert store().get("provider/none") == ""
        assert store().get("provider/none") == ""
        assert fake.reads == ["provider/none"]

    def test_set_updates_cache_without_reread(self, keychain):
        store, fake = keychain
        assert store().set("provider/a", "sk-new") is True
        assert store().get("provider/a") == "sk-new"
        assert fake.reads == [], "刚写进去的值不该再回头读一次"

    def test_delete_clears_cache_and_item(self, keychain):
        store, fake = keychain
        store().set("provider/a", "sk-old")
        assert store().delete("provider/a") is True
        assert fake.deletes == ["provider/a"]
        assert store().get("provider/a") == ""
        assert fake.reads == [], "删掉后读到的应是缓存的空值，不该再去读"

    def test_delete_missing_item_counts_as_success(self, keychain):
        store, fake = keychain
        assert store().delete("provider/never-existed") is True
        assert fake.deletes == ["provider/never-existed"]

    def test_unavailable_keyring_is_inert(self, monkeypatch):
        import pet.chat.models as models

        real_store = models._REAL_SECRET_STORE_FOR_TESTS
        real_store.clear_cache_for_tests()
        store = real_store()
        store._keyring = None  # 实例属性遮蔽模块引用 = keyring 不可用
        assert store.available is False
        assert store.get("provider/a") == ""
        assert store.set("provider/a", "x") is False
        assert store.delete("provider/a") is False


# ============================================================================
# 2. 明确"不需要 Key"时不读钥匙串
# ============================================================================
class TestResolveApiKeyPolicy:
    def _config(self, tmp_path, provider_kwargs):
        from pet.config import Config

        cfg = Config(tmp_path)
        settings = cfg.chat_settings()
        settings.providers["p"] = ProviderConfig(
            "p", api_key_ref="provider/p", **provider_kwargs)
        settings.active_provider = "p"
        cfg.set_chat_settings(settings)
        return cfg, cfg.chat_settings().providers["p"]

    def test_not_required_never_touches_the_keychain(self, tmp_path, keychain):
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/p")] = "sk-stale-secret"

        cfg, provider = self._config(tmp_path, {"api_key_required": False})

        assert cfg.resolve_api_key(provider) == ""
        assert fake.reads == [], "标记为不需要 Key 的服务一次都不许读系统钥匙串"

    def test_required_still_resolves_from_keychain(self, tmp_path, keychain):
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/p")] = "sk-live"

        cfg, provider = self._config(tmp_path, {"api_key_required": True})

        assert cfg.resolve_api_key(provider) == "sk-live"

    def test_default_is_required_for_existing_configs(self):
        """老配置没有这个键 → 必须保持既有行为，不能把用户的 Key 弄丢。"""
        assert ProviderConfig.from_dict("p", {}).api_key_required is True
        assert ProviderConfig.from_dict("p", {"api_key_required": False}).api_key_required is False

    def test_flag_roundtrips_through_dict(self):
        p = ProviderConfig.from_dict("p", {"api_key_required": False})
        assert p.to_dict()["api_key_required"] is False

    def test_plaintext_migration_skips_not_required_providers(self, tmp_path, keychain):
        """不需要 Key 的服务连明文迁移都不该做（那也是一次钥匙串访问）。"""
        from pet.config import Config

        store, fake = keychain
        cfg = Config(tmp_path)
        settings = cfg.chat_settings()
        settings.providers["p"] = ProviderConfig(
            "p", api_key_ref="provider/p", api_key="sk-plaintext",
            api_key_required=False)
        settings.active_provider = "p"
        cfg.set_chat_settings(settings)
        cfg.save()

        Config(tmp_path)  # 重新加载 → 走明文迁移

        assert fake.reads == [] and fake.writes == []


# ============================================================================
# 3. 设置界面：开关 + 不再在渲染期读钥匙串
# ============================================================================
class TestSettingsUiPolicy:
    def _page(self, tmp_path):
        QApplication.instance() or QApplication([])
        from pet.config import Config
        from pet.chat.ai_settings_page import _AiSettingsPage

        cfg = Config(tmp_path)
        return cfg, _AiSettingsPage(cfg)

    def test_switch_disables_key_field(self, tmp_path, keychain):
        _cfg, page = self._page(tmp_path)
        assert page.key.isEnabled() is True
        page.key_not_required.setChecked(True)
        assert page.key.isEnabled() is False

    def test_provisional_config_skips_keychain_when_not_required(self, tmp_path, keychain):
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/openai-main")] = "sk-stale"

        _cfg, page = self._page(tmp_path)
        page.key_not_required.setChecked(True)
        page.provisional_config()

        assert fake.reads == [], "测试连接也不许读钥匙串"

    def test_saving_not_required_deletes_stored_secret(self, tmp_path, keychain):
        """勾上开关保存 = 明确不要 Key：钥匙串里那条要真的删掉。

        留着它，下次请求还会把它翻出来发给本地端口——既多一次授权弹窗，
        也把旧凭据发错了地方。
        """
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/openai-main")] = "sk-stale"

        cfg, page = self._page(tmp_path)
        page.key_not_required.setChecked(True)
        page.save()

        assert fake.deletes == ["provider/openai-main"]
        saved = cfg.chat_settings().providers["openai-main"]
        assert saved.api_key_required is False
        assert cfg.resolve_api_key(saved) == ""
        assert fake.reads == []

    def test_saving_with_key_still_stores_it(self, tmp_path, keychain):
        store, fake = keychain
        cfg, page = self._page(tmp_path)
        page.key.setText("sk-fresh")
        page.save()

        assert "provider/openai-main" in fake.writes
        saved = cfg.chat_settings().providers["openai-main"]
        assert saved.api_key_required is True
        assert cfg.resolve_api_key(saved) == "sk-fresh"


class TestLegacyDialogPolicy:
    def _dialog(self, tmp_path):
        QApplication.instance() or QApplication([])
        from pet.config import Config
        from pet.chat.settings_dialog import ChatSettingsDialog

        return Config(tmp_path), ChatSettingsDialog(Config(tmp_path))

    def test_key_status_never_reads_keychain(self, tmp_path, keychain):
        """状态提示过去每次渲染都读一次钥匙串——打开设置就弹窗。"""
        store, fake = keychain
        fake.items[("dsh-pet-standalone", "provider/openai-main")] = "sk-stale"

        _cfg, dlg = self._dialog(tmp_path)

        assert fake.reads == [], "渲染期一次都不许读"

    def test_key_status_reports_not_required(self, tmp_path, keychain):
        _cfg, dlg = self._dialog(tmp_path)
        provider = dlg.settings.providers["openai-main"]
        provider.api_key_required = False
        assert "不需要" in dlg._key_status(provider)
