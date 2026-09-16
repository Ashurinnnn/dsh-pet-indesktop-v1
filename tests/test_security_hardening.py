# -*- coding: utf-8 -*-
"""安全加固回归：把审计里真正修掉的那几条钉住。

对应 findings（详见 docs/SECURITY-AUDIT-2026-09-16.md）：

* **依赖规格自动修复的候选必须只有本人可写**——该候选会被写进 package.json 并被
  dsh 执行，等于一条信任边界；macOS 的 ``/Applications`` 是 ``drwxrwxr-x root:admin``。
* **跨主机重定向必须拒绝**——urllib 跟随时保留 Authorization，明文 http 的局域网
  地址被改一个 Location 就能拿走 Bearer 凭据。
* **进程身份判定必须 fail-closed**——旧实现在 POSIX 上"读不到镜像就放行"，
  macOS 没有 /proc，于是伪造一份 runtime 标记就能杀掉任意同用户进程。
* **私事数据不许 0644**——配置/会话/数据目录收紧到 0600/0700。
* **本地 IPC socket 要用户级访问**。
"""
from __future__ import annotations

import os
import stat
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from pet import agent_link


# ============================================================================
# 1. 依赖修复候选的信任边界
# ============================================================================
class TestDependencyRepairTrust:
    def test_group_writable_path_is_rejected(self, tmp_path):
        """别人可写的目录不能当候选——那份代码会被 dsh 执行。"""
        shared = tmp_path / "shared"
        shared.mkdir()
        target = shared / "pkg"
        target.mkdir()
        os.chmod(shared, 0o775)

        assert agent_link._exclusively_owned(target) is False

    def test_other_writable_path_is_rejected(self, tmp_path):
        shared = tmp_path / "shared"
        shared.mkdir()
        target = shared / "pkg"
        target.mkdir()
        os.chmod(shared, 0o707)

        assert agent_link._exclusively_owned(target) is False

    def test_private_path_is_accepted(self, tmp_path):
        private = tmp_path / "private"
        private.mkdir()
        target = private / "pkg"
        target.mkdir()
        os.chmod(private, 0o700)
        os.chmod(target, 0o700)

        assert agent_link._exclusively_owned(target) is True

    def test_missing_path_is_rejected(self, tmp_path):
        assert agent_link._exclusively_owned(tmp_path / "nope" / "x") is False

    def test_suggestion_skips_untrusted_candidates(self, tmp_path, monkeypatch):
        """版本号改名场景：只有可信候选会被建议。"""
        old_dir = tmp_path / "old" / "pkg-0.12.80.tgz"
        old_dir.parent.mkdir(parents=True)
        # 兄弟候选：一个在别人可写的目录里，一个在私有目录里
        public = tmp_path / "public"
        public.mkdir()
        os.chmod(public, 0o777)
        (public / "pkg-0.13.6.tgz").write_text("x", encoding="utf-8")

        assert agent_link._suggest_path_replacement(old_dir) is None, (
            "唯一候选落在他人可写目录时，必须当作没有候选"
        )


# ============================================================================
# 2. 跨主机重定向
# ============================================================================
class TestRedirectCredentialContainment:
    def _handler(self):
        from pet.chat import providers  # noqa: F401  触发 install_opener

        handler = urllib.request._opener.handlers  # type: ignore[attr-defined]
        return next(h for h in handler if type(h).__name__ == "_SameHostRedirectHandler")

    class _Req(urllib.request.Request):
        """真 Request：父类 redirect_request 会用到 get_method/header_items 等。"""

        def __init__(self, url):
            super().__init__(url, headers={"Authorization": "Bearer sk-secret"})

    def test_cross_host_redirect_is_refused(self):
        handler = self._handler()
        with pytest.raises(urllib.error.HTTPError) as excinfo:
            handler.redirect_request(
                self._Req("https://api.deepseek.com/v1/chat/completions"),
                None, 307, "Temporary Redirect", {},
                "https://attacker.example.com/v1/chat/completions",
            )
        assert "跨主机重定向" in str(excinfo.value.reason)

    def test_port_change_is_refused(self):
        handler = self._handler()
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(
                self._Req("http://192.168.1.10:8904/v1/chat/completions"),
                None, 307, "Temporary Redirect", {},
                "http://192.168.1.10:9999/v1/chat/completions",
            )

    def test_scheme_downgrade_is_refused(self):
        handler = self._handler()
        with pytest.raises(urllib.error.HTTPError):
            handler.redirect_request(
                self._Req("https://api.deepseek.com/v1/chat/completions"),
                None, 302, "Found", {},
                "http://api.deepseek.com/v1/chat/completions",
            )

    def test_same_host_path_redirect_is_allowed(self):
        """同主机内的路径重定向（补斜杠、跳 /v1）必须继续放行。"""
        handler = self._handler()
        result = handler.redirect_request(
            self._Req("https://api.deepseek.com/chat/completions"),
            None, 301, "Moved Permanently", {},
            "https://api.deepseek.com/v1/chat/completions",
        )
        assert result is not None

    def test_opener_is_installed_process_wide(self):
        from pet.chat import providers  # noqa: F401

        names = {type(h).__name__ for h in urllib.request._opener.handlers}  # type: ignore[attr-defined]
        assert "_SameHostRedirectHandler" in names


# ============================================================================
# 3. 进程身份判定 fail-closed
# ============================================================================
class TestProcessIdentityFailClosed:
    def test_nonexistent_pid_is_not_a_pet(self):
        from pet.child_pet_cleanup import _is_pet_process

        assert _is_pet_process(999999) is False

    def test_unrelated_process_is_not_a_pet(self):
        from pet.child_pet_cleanup import _is_pet_process

        assert _is_pet_process(1) is False, "launchd 绝不是小肥鱼"

    def test_unreadable_image_fails_closed(self, monkeypatch):
        """回归核心：旧实现在 POSIX 上"读不到镜像就放行"。"""
        from pet import child_pet_cleanup

        monkeypatch.setattr(child_pet_cleanup, "_pid_image_path", lambda pid: None)
        monkeypatch.setattr(child_pet_cleanup, "_posix_image_path", lambda pid: None)
        assert child_pet_cleanup._is_pet_process(os.getpid()) is False

    def test_same_image_with_unrelated_command_is_rejected(self, monkeypatch):
        """镜像相同（源码运行时都是同一个解释器）时，还要命令行确认。"""
        from pet import child_pet_cleanup

        own = child_pet_cleanup._own_image_path()
        monkeypatch.setattr(child_pet_cleanup, "_pid_image_path", lambda pid: None)
        monkeypatch.setattr(child_pet_cleanup, "_posix_image_path", lambda pid: own)
        monkeypatch.setattr(child_pet_cleanup, "_command_confirms_pet", lambda pid: False)
        assert child_pet_cleanup._is_pet_process(1234) is False

    def test_same_image_with_pet_command_is_accepted(self, monkeypatch):
        from pet import child_pet_cleanup

        own = child_pet_cleanup._own_image_path()
        monkeypatch.setattr(child_pet_cleanup, "_pid_image_path", lambda pid: None)
        monkeypatch.setattr(child_pet_cleanup, "_posix_image_path", lambda pid: own)
        monkeypatch.setattr(child_pet_cleanup, "_command_confirms_pet", lambda pid: True)
        assert child_pet_cleanup._is_pet_process(os.getpid()) is True

    def test_command_check_is_three_valued(self, monkeypatch):
        """读不到命令行要返回 None（退回镜像判定），而不是当成"不是"。"""
        from pet import child_pet_cleanup

        monkeypatch.setattr(child_pet_cleanup.subprocess, "run",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no ps")))
        assert child_pet_cleanup._command_confirms_pet(os.getpid()) is None


# ============================================================================
# 4. 私事数据的文件权限
# ============================================================================
class TestPrivateFileModes:
    def test_config_and_dir_are_private(self, tmp_path):
        from pet.config import Config

        cfg = Config(tmp_path)
        assert cfg.save()
        assert stat.S_IMODE(os.stat(cfg.dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600

    def test_save_does_not_loosen_a_tightened_config(self, tmp_path):
        """用户手动 chmod 600 之后，一次保存不该把它改回 0644。"""
        from pet.config import Config

        cfg = Config(tmp_path)
        cfg.save()
        os.chmod(cfg.path, 0o600)
        cfg.set("scale", 0.85)
        cfg.save()
        assert stat.S_IMODE(os.stat(cfg.path).st_mode) == 0o600

    def test_session_files_are_private(self, tmp_path):
        from pet.chat.session_store import _atomic_write

        path = tmp_path / "sessions" / "s" / "x.json"
        _atomic_write(path, b'{"messages": []}')
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# ============================================================================
# 5. 本地 IPC socket 的用户级访问
# ============================================================================
class TestIpcSocketHardening:
    def test_listen_requests_user_access_option(self):
        import inspect

        from pet import collision_ipc

        src = inspect.getsource(collision_ipc._CollisionWorker._try_election_listen)
        assert "UserAccessOption" in src, "必须显式要求用户级访问"
        assert "_restrict_socket_file" in src, "listen 成功后要收紧 socket 文件"

    def test_socket_file_is_restricted_after_listen(self, tmp_path):
        """真起一个 QLocalServer，确认 socket 文件被收紧到 0600。"""
        pytest.importorskip("PySide6.QtNetwork")
        from PySide6.QtNetwork import QLocalServer
        from PySide6.QtWidgets import QApplication

        from pet import collision_ipc

        QApplication.instance() or QApplication([])
        name = f"dsh-pet-perm-test-{os.getpid()}"
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        try:
            assert server.listen(name), server.errorString()
            collision_ipc._CollisionWorker._restrict_socket_file(server)
            path = server.fullServerName()
            if os.path.exists(path):
                mode = stat.S_IMODE(os.stat(path).st_mode)
                assert mode & 0o077 == 0, f"socket 权限过宽: {oct(mode)}"
        finally:
            server.close()
            QLocalServer.removeServer(name)
