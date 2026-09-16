# -*- coding: utf-8 -*-
"""pytest 全局夹具。

1) 全局测试静音：跑测试时不许真实发声。
   play_sound 的取证日志照常记录（测试仍可断言"调用了播放"），但
   QSoundEffect/QMediaPlayer 的 play 被替换为空操作——测试套件在任何
   机器上跑都不应该让喇叭出声。

2) 无人值守环境（CI/自动化）下，模态 QMessageBox 弹窗会永久阻塞或直接崩溃
   （Fatal: Aborted）。设置对话框（如 modern_settings_dialog）保存开机自启失败时会弹模态
   QMessageBox.warning，所有调用 _save() 的测试在 CI 上都会因此卡死
   （定位手段：pytest -o timeout_method=thread --timeout=90 可 dump 出
   卡住的线程堆栈）。这里用 autouse fixture 全局把 QMessageBox 的静态弹窗
   方法替换为 no-op——任何测试都不会因模态弹窗卡死。需要断言弹窗行为的
   测试可自行 monkeypatch.setattr 覆盖。

   收窄（批 6-8a）：warning/information/critical/about 的返回值在产品代码中
   无分支用途，保持 no-op 即可；question() 是分支型 API，返回 None 不是合法
   StandardButton、会改变调用方分支语义，因此默认返回 StandardButton.No
   （安全拒绝，等同用户点“否”）。需要 Yes/No 特定答案的测试必须局部
   monkeypatch（test_agent_link / test_proactive 已如此）。
"""

import os
import sys

import pytest


@pytest.fixture(autouse=True)
def _mute_qt_audio(monkeypatch):
    try:
        from PySide6.QtMultimedia import QMediaPlayer, QSoundEffect
    except Exception:
        return
    monkeypatch.setattr(QSoundEffect, "play", lambda self: None)
    monkeypatch.setattr(QMediaPlayer, "play", lambda self: None)
    # 阻止 QSoundEffect 真正异步加载音频源：headless CI 上即使不 play，
    # setSource 后的异步加载与 processEvents 也可能在 QtMultimedia 后端触发
    # 原生 access violation。把 status 直接置 Ready 也让预热等待循环零泵事件。
    monkeypatch.setattr(QSoundEffect, "setSource", lambda self, source: None)
    monkeypatch.setattr(QSoundEffect, "status", lambda self: QSoundEffect.Status.Ready)
    # Windows headless 上 WAV 之外的音效预热会启动真实 QAudioDecoder 异步解码，
    # 其 processEvents 等待循环可能原生崩溃（macOS/Linux 无此现象）。此全局
    # 打桩只作用于 Windows，避免影响其它平台 QtMultimedia 退出时的析构顺序。
    if sys.platform == "win32":
        # 测试如需验证音效逻辑，应像 test_click_sound 一样局部替换
        # click_sound._pool.qt_multimedia_classes 为假类。
        try:
            from pet import click_sound as _click_sound_mod
        except Exception:
            return
        monkeypatch.setattr(_click_sound_mod._pool, "qt_multimedia_classes", lambda: None)


@pytest.fixture(autouse=True)
def _no_modal_message_boxes(monkeypatch):
    from PySide6.QtWidgets import QMessageBox

    # 纯防卡桩：warning/information/critical/about 的返回值无分支用途，统一 no-op。
    for method in ("warning", "information", "critical", "about"):
        monkeypatch.setattr(QMessageBox, method, staticmethod(lambda *a, **k: None))
    # question() 是分支型 API（调用方按返回值走 Yes/No 分支，如 pet/agent_link.py
    # set_enabled），返回 None 不是合法 StandardButton、会改变分支语义。这里默认
    # 返回 StandardButton.No（安全拒绝，等同用户点“否”）；需要 Yes/No 特定答案的
    # 测试必须局部 monkeypatch（test_agent_link / test_proactive 已如此）。
    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *a, **k: QMessageBox.StandardButton.No),
    )


@pytest.fixture(autouse=True)
def _isolated_local_env(monkeypatch):
    """默认让测试面对一台"什么都没装"的机器。

    ``local_env`` 会探测真实可执行文件、/Applications、托管 dsh home 与本地端口。
    不隔离的话，用例结果取决于跑测试这台机器装了什么——本机装了 dsh + Unsloth，
    于是 ``harness_launcher.launch_harness()`` 认为"dsh 由托管启动器掌管"而拒绝
    自启，一堆与本次改动无关的用例就红了（这正是"测试读到开发者真实机器"的
    老问题，见 ``_isolate_dsh_homes`` / ``_no_real_dsh_profile_write``）。

    需要覆盖探测行为的用例自行 monkeypatch 更靠里的探测点
    （tests/test_local_env.py 已经这么做）。
    """
    try:
        from pet import local_env
    except Exception:
        return
    monkeypatch.setattr(local_env, "_which", lambda name, path=None: None)
    monkeypatch.setattr(local_env, "_app_bundle_exists", lambda name: False)
    monkeypatch.setattr(local_env, "_port_open", lambda port, host="127.0.0.1": False)


@pytest.fixture(autouse=True)
def _fake_keyring(monkeypatch):
    """全程用内存假钥匙串，**绝不碰系统钥匙串**。

    为什么必须全局拦：``Config.reload()`` 的明文迁移与 ``resolve_api_key()`` 都会
    走 ``pet.chat.models.SecretStore``；只要用例里出现过一个带明文 api_key 的
    provider，或构造过设置对话框（AI 设置页会解析已存 key 来显示），就会读真实的
    系统钥匙串。在 macOS 上那是一次 SecurityAgent 授权弹窗——跑一遍测试套件会
    弹几十上百次，用户只会看到"python 一直在要钥匙串权限"，而且每次都要点。
    这是测试对开发者机器的真实副作用（与 ``_no_real_dsh_profile_write`` 同因），
    也顺带让结果不再依赖"这台机器有没有钥匙串"。

    需要验证 keyring 行为的用例自行 monkeypatch 覆盖本夹具（test_chat_subsystem /
    test_config_domains / test_config_instance / test_config_key_migration 都是）。
    """

    class _InMemorySecretStore:
        """接口与真实 SecretStore 一致（available / get / set）。"""

        _values: dict[str, str] = {}

        def __init__(self, service_name: str = "dsh-pet-standalone-test") -> None:
            self.service_name = service_name

        @property
        def available(self) -> bool:
            return True

        def get(self, ref):
            if not ref:
                return ""
            return self._values.get(f"{self.service_name}/{ref}", "")

        def set(self, ref, value):
            if not ref:
                return False
            self._values[f"{self.service_name}/{ref}"] = str(value)
            return True

        def delete(self, ref):
            if not ref:
                return False
            self._values.pop(f"{self.service_name}/{ref}", None)
            return True

    # 把真实实现挂到模块属性上，供"要验证真实 SecretStore 行为"的用例取回
    # （tests/test_keychain_access_policy.py）。这是测试侧的摆放，产品文件不变。
    import pet.chat.models as _chat_models

    monkeypatch.setattr(_chat_models, "_REAL_SECRET_STORE_FOR_TESTS",
                        _chat_models.SecretStore, raising=False)
    monkeypatch.setattr("pet.chat.models.SecretStore", _InMemorySecretStore)
    try:
        from pet.chat import settings_dialog as _settings_dialog
    except Exception:
        return
    # 该模块在导入期就把 SecretStore 绑进了自己的命名空间，光换 models 里的不够。
    monkeypatch.setattr(_settings_dialog, "SecretStore", _InMemorySecretStore)


@pytest.fixture(autouse=True)
def _no_real_dsh_profile_write(monkeypatch):
    """禁止测试触发对**真实** ~/.dsh/profiles 的桥接 link 自检。

    产品在启动路径会自检桥接 link（陈旧则跑 `pnpm add` 刷成当前构建，见
    DshMonitor.schedule_link_refresh_check）；测试若启用 dsh 联动又没打桩，
    那个后台线程会读到开发者/CI 机器上的真实 profile 并真的执行 pnpm，
    改写用户配置——测试绝对不许有这种副作用。这里只拦"起真线程"这一步，
    自检逻辑本身仍可测（用例自行注入 spawn，或直接调 refresh_stale_bridge_links）。
    """
    try:
        from pet.agent_link import DshMonitor
    except Exception:
        return
    monkeypatch.setattr(
        DshMonitor, "_spawn_link_check", staticmethod(lambda target: None),
    )


@pytest.fixture(autouse=True)
def _isolate_dsh_homes(tmp_path, monkeypatch):
    """默认只让测试看见 monkeypatch 出来的 dsh home。

    ``agent_link.dsh_homes()`` 默认会把 ``~/.dsh`` 与托管启动器（Unsloth Studio
    的 ``~/.unsloth/studio/auth/agents/{dsh,.tmp/unsloth-dsh-*}``）的 home 全算上。
    测试若不隔离，用例就会读到开发者/CI 机器上**真实**的 dsh 安装：真的跑 pnpm、
    真的改人家的 ``cordis.patch.yml``——测试绝不许有这种副作用（同
    ``_no_real_dsh_profile_write`` 的动机）。

    需要覆盖多 home 行为的用例自行 monkeypatch 这两个钩子。
    """
    try:
        from pet import agent_link
        from pet import dsh_homes as dsh_homes_mod
    except Exception:
        return
    monkeypatch.setattr(agent_link, "_managed_dsh_homes", lambda: [])
    monkeypatch.setattr(agent_link, "_fallback_standard_home", lambda: None)
    # dsh_homes 自己也要隔离：local_env 直接调它的 discover_managed_homes，
    # 不经过 agent_link 那两个钩子。**只替换"默认值"这一档**——显式传 env 的
    # 调用（用例在指定自己的假机器）仍走真实实现，否则 test_dsh_homes 里那些
    # `discover_managed_homes({"UNSLOTH_AGENTS_DIR": ...})` 会被一起改掉。
    real_unsloth_agents_dir = dsh_homes_mod.unsloth_agents_dir

    def _no_default_unsloth(env=None):
        # local_env 会把 os.environ 显式传进来（不是 None），所以判据是"有没有
        # 显式指定 UNSLOTH_AGENTS_DIR"，而不是 env 是不是 None。
        environ = os.environ if env is None else env
        if str(environ.get(dsh_homes_mod.UNSLOTH_AGENTS_DIR_ENV) or "").strip():
            return real_unsloth_agents_dir(environ)
        return tmp_path / "no-such-unsloth-agents"

    monkeypatch.setattr(dsh_homes_mod, "unsloth_agents_dir", _no_default_unsloth)


@pytest.fixture(autouse=True)
def _close_session_writers():
    """会话异步写盘（B8）：每个测试结束后关闭所有后台 writer，
    避免守护线程在 tmp_path 已清理后继续写盘（WinError 145 之类的 teardown 竞态）。"""
    yield
    try:
        from pet.chat import session_store
        session_store.reset_writers_for_tests()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _clear_click_sound_pool():
    """每测后清空点击音效池（测试债防线）。

    conftest 只静音了 play()，但设置保存等路径的 warm_click_sound_effects
    会真实创建 QSoundEffect/QMediaPlayer/QAudioOutput。这些 QtMultimedia
    原生对象跨测试累积后，在共享 QApplication 下随机 access violation /
    Fatal abort（全量套件崩溃点会漂移：click_sound 预热循环、气泡图片
    processEvents 均观测到）。每测后 clear() 复位原生对象缓存。
    """
    yield
    try:
        from pet import click_sound
        click_sound._pool.clear()
    except Exception:
        pass


@pytest.fixture(autouse=True)
def _close_qt_top_level_widgets():
    """在测试后收口仍存活的应用级后台资源与 collision IPC 会话。"""
    yield
    # webm_clip 的「会话结束」闸门是进程级 latch（issue #111）：测试里置位后
    # 不复位会让后续用例静默拒绝一切 reader 启动（报错点离真因很远）。
    try:
        from pet import webm_clip as _webm_clip_mod
        _webm_clip_mod._reset_session_ending_for_tests()
    except Exception:
        pass
    # collision IPC：stop 仍存活的 CollisionIpcSession（finally 语义）。
    # 会话若在测试里未 stop，其 QThread 被 GC 时仍在跑 → 后续无关测试的
    # processEvents 处 native abort（QThread: Destroyed while thread is still
    # running，崩溃点漂移、Linux exit 139 根因）。
    try:
        from pet.collision_ipc import _stop_live_sessions_for_tests
        _stop_live_sessions_for_tests()
    except Exception:
        pass
    try:
        from pet.agent_link import AgentLinkManager, BaseAgentMonitor
        AgentLinkManager._shutdown_live_for_tests()
        BaseAgentMonitor._shutdown_live_for_tests()
    except Exception:
        pass
    # AppShell / 多窗共享子系统：待办服务的无主 QTimer 与共享 proactive 的
    # timer/bridge 从 Qt C++ 侧强引用住整个 shell 对象图（Python gc 回收不掉），
    # 解释器退出 GC 才最终化 → 原生访问违规（test_single_process_shared 的
    # flag_on 族逐用例单独跑亦复现，崩溃点 "Garbage-collecting / no Python
    # frame"）。按同族防线逐对象停表 + 过继 QApplication。
    try:
        from pet.multi_window_shared import SharedSubsystems
        SharedSubsystems._shutdown_live_for_tests()
    except Exception:
        pass
    try:
        from pet.app import AppShell
        AppShell._shutdown_live_for_tests()
    except Exception:
        pass
    try:
        from pet.library import MovieLibrary
        MovieLibrary._shutdown_live_for_tests()
    except Exception:
        pass
    # dsh_state：QTimer 只停了不算完——在途在线探测线程（daemon + 阻塞 socket）
    # 回来后仍会跨线程 emit；先 stop() 换代作废其结果，再销毁顶层窗口，
    # 否则 deleteLater + processEvents 收尾时 worker 向已销毁 QObject emit
    # （macOS 全量套件 segfault：conftest._close_qt_top_level_widgets + socket 线程）。
    try:
        from pet import dsh_state
        dsh_state._shutdown_live_for_tests()
    except Exception:
        pass
    # 销毁残留顶层窗口（QDialog/QWidget）：只用 deleteLater，绝不用 close()。
    # close() 会触发 closeEvent → _write_config → warm_click_sound_effects 的
    # 副作用（t4 曾因此崩溃）；deleteLater 走 DeferredDelete，Qt 安全销毁且不
    # 触发 closeEvent。清理掉泄漏的 C++ 对话框，避免其悬空事件在后续测试的
    # processEvents 引爆（崩溃点漂移、access violation）。
    # 逐对象定向派发自己排的 DeferredDelete，绝不调用共享 QApplication 的全局
    # processEvents()。全局冲刷会把其他测试遗留的排队事件（跨线程 queued 调用、
    # 历史 timer、别处排的删除任务）一并派发到正在销毁/已销毁的原生对象上，
    # 把历史 QObject 生命周期集中引爆在当前测试 —— 这正是全量套件偶发
    # 0xC0000005 access violation、且崩溃点随当前测试漂移的机制
    # （docs/QT-LIFECYCLE-FULL-SUITE-STABILIZATION-2026-09.md §2、§4：全局冲刷与
    # 进程级 sendPostedEvents 都已被否决；本轮崩溃点就是这里的 app.processEvents()）。
    try:
        import shiboken6
        from PySide6.QtCore import QCoreApplication, QEvent
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None:
            for widget in list(app.topLevelWidgets()):
                try:
                    if not shiboken6.isValid(widget):
                        continue  # C++ 侧已销毁的半死窗口：跳过
                    widget.deleteLater()
                    # 接收者定向：只处理这一个对象的 DeferredDelete，不触碰共享队列里
                    # 其他对象的删除任务与排队事件。
                    QCoreApplication.sendPostedEvents(widget, QEvent.Type.DeferredDelete)
                except RuntimeError:
                    pass
    except Exception:
        pass


@pytest.fixture(scope="session", autouse=True)
def _close_webm_readers_at_session_end():
    """session 结束强收口所有 webm reader（测试债 #2 防线）。

    全量套件偶发 Windows access violation 的根因是 webm_clip `_reader`
    线程/ffmpeg 进程在测试收尾时未死干净、与解释器进程退出竞态。本 fixture
    在所有测试 teardown 之后做有界收口，目标：套件结束时无 webm-reader-*
    线程存活。只做收口，不改产品代码行为。

    收口对象（两类都覆盖）：
    1. 孤儿注册表（_ORPHAN_REGISTRY.holders()）：模块级强引用持有所有
       「退役池非空」的 clip —— 对每个 clip 调公开 cleanup()；
    2. 仍存活的 webm-reader-* 线程（clip 从未 stop、不在注册表中）：从
       threading 枚举反向定位其 owner clip（reader 线程的 _target 是
       clip._reader 绑定方法，__self__ 即 clip），再调公开 cleanup()。

    每轮 cleanup 后 reap 注册表（有界 join，正常 terminate 后毫秒级退出），
    轮数只作病态场景兜底；仍有存活线程则告警（防线不因自身变红）。
    """
    yield
    import logging
    import threading

    logger = logging.getLogger("pytest.conftest.webm")
    try:
        from pet import webm_clip as webm_clip_mod

        registry = webm_clip_mod._ORPHAN_REGISTRY

        def _clips_with_live_readers():
            clips = set(registry.holders())
            for t in threading.enumerate():
                if t.is_alive() and t.name.startswith("webm-reader-"):
                    owner = getattr(getattr(t, "_target", None), "__self__", None)
                    if owner is not None:
                        clips.add(owner)
            return clips

        for _ in range(5):
            clips = _clips_with_live_readers()
            if not clips:
                break
            for clip in clips:
                try:
                    clip.cleanup()
                except Exception:
                    pass  # clip 已随 Qt C++ 侧销毁等：交由 GC/产品自身兜底
            registry.reap()
        survivors = [
            t.name for t in threading.enumerate()
            if t.is_alive() and t.name.startswith("webm-reader-")
        ]
        if survivors:
            logger.warning(
                "session 结束仍有 %d 个 webm reader 线程存活: %s",
                len(survivors), survivors,
            )
    except Exception:
        pass  # 防线 fixture：任何异常都不应让套件本身变红
    # 会话结束闸门是进程级 latch（issue #111）：在最靠后的收口点再复位一次，
    # 保证无论哪个用例置位过都不会串到后续用例（那会让 reader 静默拒绝启动，
    # 报错点离真因很远）。
    try:
        from pet import webm_clip as _webm_clip_mod
        _webm_clip_mod._reset_session_ending_for_tests()
    except Exception:
        pass
