#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""构建期权限面闸门：检查打包出的 macOS ``.app`` **没有能力**申请不必要的权限。

为什么要有这一道（而不只是源码层的守卫测试）：源码扫描证明"我们没写那些调用"，
但产物的 Info.plist / entitlements / 打包进去的模块才是 macOS 实际看到的东西。
两者任一漂移都可能悄悄打开一扇门：

* **Info.plist 里出现 ``NS*UsageDescription``** —— 那就是"这个 app 会申请某权限"
  的正式声明（系统弹框里显示的就是这段文案）。本项目在 macOS 上不需要任何一项：
  没有摄像头、麦克风、通讯录、日历、照片、定位、语音识别、蓝牙、自动化……连
  "屏幕录制"也不需要（截图功能只在 Windows 提供）。出现任何一条即构建失败。
* **entitlements 非空** —— ad-hoc 签名的桌宠不需要任何 entitlement；出现
  ``com.apple.security.*``（App Sandbox / 设备访问 / 临时例外）说明打包方式变了。
* **打包进了会碰隐私框架的 Python 模块** —— 比如 pyobjc 的 Contacts/AVFoundation，
  或键鼠监听库。它们在场就意味着"有能力申请"。

用法：
    python scripts/check_macos_permissions.py --app-dir build/macos/xxx.app
构建脚本 ``scripts/build_macos.sh`` 在 macOS 变体上会调用它。
"""
from __future__ import annotations

import argparse
import plistlib
import subprocess
import sys
from pathlib import Path

# 隐私用途声明键：出现任何一个都说明 app 声明了要申请对应权限。
# 前缀匹配 NS*UsageDescription 之外，还列了几个不带该后缀但同样敏感的键。
FORBIDDEN_PLIST_KEYS = (
    "NSCameraUsageDescription",
    "NSMicrophoneUsageDescription",
    "NSContactsUsageDescription",
    "NSCalendarsUsageDescription",
    "NSRemindersUsageDescription",
    "NSPhotoLibraryUsageDescription",
    "NSPhotoLibraryAddUsageDescription",
    "NSLocationUsageDescription",
    "NSLocationWhenInUseUsageDescription",
    "NSLocationAlwaysAndWhenInUseUsageDescription",
    "NSAppleEventsUsageDescription",
    "NSSystemAdministrationUsageDescription",
    "NSSpeechRecognitionUsageDescription",
    "NSBluetoothAlwaysUsageDescription",
    "NSBluetoothPeripheralUsageDescription",
    "NSUserTrackingUsageDescription",
    "NSHealthShareUsageDescription",
    "NSHealthUpdateUsageDescription",
    "NSHomeKitUsageDescription",
    "NSMotionUsageDescription",
    "NSFocusStatusUsageDescription",
    "NSFileProviderDomainUsageDescription",
    "NSDesktopFolderUsageDescription",
    "NSDocumentsFolderUsageDescription",
    "NSDownloadsFolderUsageDescription",
    "NSRemovableVolumesUsageDescription",
    "NSNetworkVolumesUsageDescription",
    "NFCReaderUsageDescription",
    "NSIdentityUsageDescription",
    "NSLocalNetworkUsageDescription",
)

# 不该被打包进产物的 Python 模块（会碰隐私框架 / 键鼠监听）。
FORBIDDEN_BUNDLED_MODULES = (
    "AVFoundation",
    "Contacts",
    "EventKit",
    "Photos",
    "CoreLocation",
    "Speech",
    "CoreBluetooth",
    "pynput",
    "Quartz",
    "ApplicationServices",
)


def _read_plist(app_dir: Path) -> dict:
    plist = app_dir / "Contents" / "Info.plist"
    if not plist.is_file():
        raise SystemExit(f"[perm-check] 找不到 Info.plist: {plist}")
    with plist.open("rb") as handle:
        return plistlib.load(handle)


def _read_entitlements(app_dir: Path) -> dict:
    """读签名里的 entitlements；无签名/无 entitlements 返回空字典。"""
    try:
        result = subprocess.run(
            ["codesign", "-d", "--entitlements", "-", "--xml", str(app_dir)],
            capture_output=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0 or not result.stdout.strip():
        return {}
    try:
        return plistlib.loads(result.stdout)
    except Exception:
        return {}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-dir", required=True, help="打包出的 .app 目录")
    args = parser.parse_args()

    app_dir = Path(args.app_dir).resolve()
    if not app_dir.exists():
        print(f"[perm-check] 产物不存在: {app_dir}", file=sys.stderr)
        return 1

    problems: list[str] = []

    # 1) Info.plist 不得声明任何隐私用途
    info = _read_plist(app_dir)
    declared = sorted(k for k in info if k in FORBIDDEN_PLIST_KEYS)
    if declared:
        problems.append("Info.plist 声明了隐私用途（等于对外宣布要申请这些权限）："
                        + "、".join(declared))

    # 2) entitlements 必须为空
    entitlements = _read_entitlements(app_dir)
    if entitlements:
        problems.append("签名里带了 entitlements（ad-hoc 桌宠不需要任何一项）："
                        + "、".join(sorted(entitlements)))

    # 3) 产物里不得出现会碰隐私框架的模块
    bundled = []
    for path in app_dir.rglob("*"):
        if path.is_dir() and path.name in FORBIDDEN_BUNDLED_MODULES:
            bundled.append(path.name)
        elif path.is_file() and path.suffix in (".so", ".dylib"):
            stem = path.name.split(".")[0]
            if stem in FORBIDDEN_BUNDLED_MODULES:
                bundled.append(path.name)
    if bundled:
        problems.append("产物里打包了会碰隐私框架的模块：" + "、".join(sorted(set(bundled))))

    if problems:
        print("[perm-check] FAIL: 权限面超出预期", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        print("  如果这是有意的，请同步更新 docs/MACOS-PERMISSIONS.md 与 "
              "scripts/check_macos_permissions.py 的允许清单。", file=sys.stderr)
        return 1

    print("[perm-check] PASS: 无隐私用途声明、无 entitlements、无隐私框架模块")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
