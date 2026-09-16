# -*- coding: utf-8 -*-
"""macOS 权限面守卫：确保桌宠**只**能申请它真正需要的东西。

背景：macOS 的隐私权限（通讯录、日历、照片、位置、麦克风、摄像头、屏幕录制、
辅助功能/输入监控、自动化、完全磁盘访问……）一旦被某个 API 调用就会弹系统授权框；
其中"屏幕录制"一条就足以读遍整块屏幕。本项目在 macOS 上真正需要的只有：

* **出网**（用户自己配的模型服务）——不需要任何授权；
* **系统钥匙串**，且**仅**在用户真的要用 API Key 时，由
  ``pet/chat/models.py::SecretStore`` 一处访问；用户把 provider 标成"不需要 Key"
  后一次都不碰。

除此之外一律不许出现。本文件用三道检查把它钉死：

1. 源码里不得出现任何会触发上述权限的 API（除非在 ALLOWED 里带理由登记）；
2. 凭证存储只能有一处访问点（``SecretStore``）；
3. 屏幕捕获必须被 ``vision.screen_capture_supported()`` 挡住——在非 Windows 上
   这个函数恒 False，所以 macOS 连"申请屏幕录制"的能力都没有。

产物级还有一道更硬的闸门：``scripts/check_macos_permissions.py`` 直接检查打包出的
``.app`` 的 Info.plist 与 entitlements（构建脚本会调它，不合格就构建失败）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

PET_DIR = Path(__file__).resolve().parent.parent / "pet"

# 会触发 macOS 隐私授权（TCC）或读取敏感数据的 API/符号。
# 每条都是"调用了就会让系统弹框或读到不该读的东西"，不是泛泛的关键词。
FORBIDDEN_PATTERNS: dict[str, str] = {
    # —— 个人信息 ——
    "CNContactStore": "通讯录",
    "CNContactFetchRequest": "通讯录",
    "EKEventStore": "日历/提醒",
    "PHPhotoLibrary": "照片图库",
    "PHAsset": "照片图库",
    "CLLocationManager": "定位",
    "HKHealthStore": "健康数据",
    "MPMediaLibrary": "媒体资料库",
    "SFSpeechRecognizer": "语音识别",
    "HMHomeManager": "家庭数据",
    "CMMotionActivityManager": "运动与健身",
    "NSUserTrackingUsageDescription": "广告追踪（ATT）",
    # —— 采集 ——
    "AVCaptureDevice": "摄像头/麦克风",
    "AVCaptureSession": "摄像头/麦克风",
    "screencapture": "屏幕录制",
    "CGWindowListCreateImage": "屏幕录制",
    "CGDisplayCreateImage": "屏幕录制",
    "SCShareableContent": "屏幕录制",
    "CGEventTapCreate": "辅助功能/输入监控（可记录全部键击）",
    "AXIsProcessTrusted": "辅助功能",
    "IOHIDCheckAccess": "输入监控",
    "addGlobalMonitorForEvents": "输入监控",
    "pynput": "键鼠监听",
    "import keyboard": "键鼠监听",
    "import mouse": "键鼠监听",
    "keyboard.Listener": "键鼠监听",
    "mouse.Listener": "键鼠监听",
    "sounddevice": "麦克风",
    "pyaudio": "麦克风",
    "QAudioSource": "麦克风",
    "QMediaDevices": "摄像头/麦克风",
    "QCamera": "摄像头",
    "QAudioInput": "麦克风",
    # —— 自动化 / 身份 ——
    "osascript": "自动化（AppleEvents）",
    "NSAppleScript": "自动化（AppleEvents）",
    "ScriptingBridge": "自动化（AppleEvents）",
    "LAContext": "Touch ID / 本机认证",
    "SecAccessControl": "钥匙串访问控制（本机认证）",
    # —— 其它 ——
    "CBCentralManager": "蓝牙",
    "IOBluetooth": "蓝牙",
    "NFCReaderSession": "NFC",
    # —— 敏感数据文件（不该被一个桌宠读）——
    "Login Data": "浏览器保存的密码库",
    "Cookies.binarycookies": "浏览器 Cookie",
    "Library/Mail": "邮件",
    "Library/Messages": "iMessage",
    "Library/Safari": "Safari 数据",
    ".ssh/id_": "SSH 私钥",
    "login.keychain": "登录钥匙串文件",
}

# 登记过的例外：为什么会出现在源码里、为什么无害。
ALLOWED: dict[str, str] = {
    # 屏幕录制相关符号只在"权限边界"这一处出现：vision.screen_capture_supported()
    # 与它的调用点。真实捕获（ImageGrab → /usr/sbin/screencapture）在非 Windows
    # 上被这个函数挡住，见下面的 TestScreenCaptureBoundary 行为测试。
    "screencapture": "pet/vision.py 的权限边界注释与平台判定",
    "ImageGrab": "pet/vision.py 的屏幕捕获，被 screen_capture_supported() 挡住",
    "keyring": "pet/chat/models.py::SecretStore——用户自己的 API Key，唯一访问点",
}

# 允许出现 FORBIDDEN_PATTERNS 的文件（除 ALLOWED 已解释的符号外，不允许任何命中）
def _pet_sources() -> list[Path]:
    return sorted(p for p in PET_DIR.rglob("*.py") if "__pycache__" not in p.parts)


def _scan() -> list[tuple[Path, int, str, str]]:
    """返回 [(文件, 行号, 命中的模式, 该行内容)]。"""
    hits: list[tuple[Path, int, str, str]] = []
    for path in _pet_sources():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue  # 纯注释行：说明文字不算调用
            for pattern, why in FORBIDDEN_PATTERNS.items():
                if pattern in line and pattern not in ALLOWED:
                    hits.append((path, lineno, pattern, line.strip()))
                    break
    return hits


class TestNoPersonalDataApis:
    def test_no_forbidden_permission_apis_in_source(self):
        """源码里不得出现任何会申请个人隐私权限的 API。

        失败时先问自己：这个功能在 macOS 上真的需要吗？本项目在 macOS 上只需要
        出网 + 用户自己给的 API Key，别的一概不需要。
        """
        hits = _scan()
        assert hits == [], "发现会申请隐私权限的 API：\n" + "\n".join(
            f"  {path.relative_to(PET_DIR.parent)}:{lineno} [{pattern}] {line}"
            for path, lineno, pattern, line in hits
        )

    def test_allowlist_entries_actually_exist(self):
        """ALLOWED 里的条目必须真的还在用：删了代码就该同步删登记，别留成万能后门。"""
        stale = []
        for symbol in ALLOWED:
            found = any(symbol in p.read_text(encoding="utf-8", errors="ignore")
                        for p in _pet_sources())
            if not found:
                stale.append(symbol)
        assert stale == [], f"这些豁免已经用不到了，请从 ALLOWED 删除：{stale}"

    def test_contacts_calendars_photos_are_absent_entirely(self):
        """个人信息类关键词连"出现"都不允许（连注释里提一嘴都不必）。"""
        banned = ("CNContactStore", "EKEventStore", "PHPhotoLibrary", "CLLocationManager",
                  "AVCaptureDevice", "CBCentralManager", "NFCReaderSession")
        offenders = [
            f"{p.relative_to(PET_DIR.parent)}:{i}"
            for p in _pet_sources()
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1)
            if any(b in line for b in banned)
        ]
        assert offenders == []


class TestCredentialStoreIsSinglePoint:
    def test_keyring_is_only_imported_by_secret_store(self):
        """凭证存储只能有一处访问点：SecretStore（便于审计与收紧）。"""
        offenders = []
        for path in _pet_sources():
            text = path.read_text(encoding="utf-8", errors="ignore")
            if re.search(r"^\s*(import keyring|from keyring)", text, re.MULTILINE):
                if path.name != "models.py" or path.parent.name != "chat":
                    offenders.append(str(path.relative_to(PET_DIR.parent)))
        assert offenders == [], (
            "keyring 只允许在 pet/chat/models.py::SecretStore 里 import；"
            f"以下文件绕过了唯一访问点：{offenders}"
        )

    def test_secret_store_is_the_only_credential_reader(self):
        """不许另起炉灶读钥匙串（比如直接调 Security/CryptoAPI 或 security CLI）。"""
        offenders = []
        for path in _pet_sources():
            text = path.read_text(encoding="utf-8", errors="ignore")
            for bad in ("SecItemCopyMatching", "SecItemAdd", "/usr/bin/security",
                        "security find-generic-password", "security find-internet-password"):
                if bad in text:
                    offenders.append(f"{path.relative_to(PET_DIR.parent)}: {bad}")
        assert offenders == []


class TestScreenCaptureBoundary:
    def test_supported_predicate_is_windows_only(self):
        from pet import vision

        assert vision.screen_capture_supported() is (sys.platform == "win32")

    @pytest.mark.skipif(sys.platform == "win32", reason="这是非 Windows 的边界")
    def test_capture_refuses_off_windows(self):
        """即便有人把菜单又接回来，捕获入口也必须直接拒绝——不弹权限框。"""
        from pet import vision

        with pytest.raises(vision.VisionError):
            vision.capture_screen_bytes()
        assert vision.capture_window_rect((0, 0, 100, 100)) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="这是非 Windows 的边界")
    def test_imagegrab_is_unreachable_without_the_guard(self):
        """ImageGrab 的 import 必须排在平台判定**之后**。

        排在前面就意味着 import 本身就可能触碰平台后端；更重要的是，这条断言让
        "以后有人把 guard 挪走"变成一次测试失败，而不是一次静默的权限申请。
        """
        import inspect

        from pet import vision

        src = inspect.getsource(vision.capture_screen_bytes)
        guard = src.find("screen_capture_supported()")
        grab = src.find("ImageGrab")
        assert guard != -1, "capture_screen_bytes 必须带平台判定"
        assert grab == -1 or guard < grab, "平台判定必须在 import ImageGrab 之前"

        src_rect = inspect.getsource(vision.capture_window_rect)
        assert "screen_capture_supported()" in src_rect

    def test_look_screen_callback_is_gated_at_wiring_site(self):
        """菜单项的可用性由"有没有接线"决定，接线处必须带平台判定。"""
        src = (PET_DIR / "app.py").read_text(encoding="utf-8")
        line = next((ln for ln in src.splitlines() if "win.on_look_screen" in ln and "=" in ln), "")
        assert line, "找不到 on_look_screen 接线"
        joined = "".join(
            src.splitlines()[i] for i in range(
                next(i for i, ln in enumerate(src.splitlines()) if "win.on_look_screen" in ln and "=" in ln) - 4,
                next(i for i, ln in enumerate(src.splitlines()) if "win.on_look_screen" in ln and "=" in ln) + 5,
            )
        )
        assert "screen_capture_supported" in joined, (
            "on_look_screen 的接线必须带 screen_capture_supported() 判定，"
            "否则 macOS 上会出现一个点了就申请「屏幕录制」的菜单项"
        )


class TestKeychainAccessPolicy:
    """钥匙串：唯一被允许的敏感权限，且必须"按需且可关闭"。"""

    def test_keychain_reads_are_memoized(self):
        # conftest 的 _fake_keyring 会把 SecretStore 换成内存假实现；这里要看真实实现
        from pet.chat.models import _REAL_SECRET_STORE_FOR_TESTS as real_store

        assert hasattr(real_store, "_cache"), "SecretStore 必须有进程级缓存（否则每次调用都弹授权框）"
        assert hasattr(real_store, "delete")

    def test_not_required_provider_short_circuits(self):
        """标记为"不需要 Key"的 provider，解析凭据时必须直接返回空。"""
        import inspect

        from pet.config import Config

        src = inspect.getsource(Config.resolve_api_key)
        assert "api_key_required" in src, "resolve_api_key 必须先看「不需要 Key」标记"
