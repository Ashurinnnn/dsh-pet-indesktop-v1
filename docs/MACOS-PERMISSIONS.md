# macOS 权限面（Apple Silicon）

> 结论先说：**桌宠在 macOS 上不申请任何隐私权限。** 构建产物里没有一条
> `NS*UsageDescription`、没有 entitlement、也没有任何能碰隐私框架的模块。
> 这条性质由两道闸门守着（源码级测试 + 构建期产物检查），不是一句承诺。

目标平台：macOS / Apple Silicon。本文记录"它需要什么、为什么、以及怎么保证它不会
多要"，改动权限相关代码前请先读这里。

---

## 1. 它实际需要什么

| 能力 | 需要授权吗 | 说明 |
|---|---|---|
| 出网（HTTPS/WSS 到用户自己配的模型服务、歌词源、报时 TTS） | **不需要** | macOS 对普通 app 的出网不做授权 |
| 读写自己的数据目录 `~/Library/Application Support/dsh-pet-standalone*/` | **不需要** | 不在 TCC 保护范围内 |
| 系统钥匙串（用户自己填的 API Key） | 需要（一次性） | 见 §3，唯一被允许的敏感资源 |
| 写 `~/Library/LaunchAgents/`（开机自启，用户显式开关） | **不需要** | 用户自己目录下的文件 |
| 通知 | **不需要** | 用的是自绘气泡窗，不是 `UNUserNotificationCenter` |
| 窗口层级 / Dock 图标（objc runtime 调 `setLevel:`、`setActivationPolicy:`） | **不需要** | 纯 AppKit 窗口属性，不触发任何 TCC |

除此之外——摄像头、麦克风、通讯录、日历、提醒、照片、定位、健康、家庭、运动、
语音识别、蓝牙、NFC、广告追踪、自动化（AppleEvents）、辅助功能、输入监控、
屏幕录制、完全磁盘访问——**一个都不需要，也一个都申请不到**（见 §2）。

## 2. 为什么"申请不到"而不只是"没申请"

macOS 的隐私授权要走系统弹框，而弹框的前提是 app 声明了对应的用途字符串。
本项目产物的 `Info.plist` 里**没有任何 `NS*UsageDescription`**。因此即便有人在
将来误加了一行会调隐私 API 的代码：

* 调摄像头/麦克风/通讯录这类需要用途字符串的 API 时，**进程会被系统直接终止**，
  而不是弹框询问用户——也就是说这个 app 连"申请"这个动作都做不出来；
* 调屏幕录制这类不需要用途字符串的 API（`screencapture` /
  `CGWindowListCreateImage`）时才会弹框，所以这一类**必须在代码里关死**（见 §4）。

另外，ad-hoc 签名、无 sandbox、无 hardened runtime、entitlements 为空——没有任何
`com.apple.security.*` 例外可供越权。

## 3. 唯一被允许的敏感资源：系统钥匙串

用途只有一个：保存**用户自己填进来的**模型 API Key（`pet/chat/models.py::SecretStore`，
service 名 `dsh-pet-standalone`）。约束：

1. **唯一访问点**。`keyring` 只允许在 `SecretStore` 里 import；直接调
   `SecItemCopyMatching` 或 `security` CLI 会被测试拦下。
2. **按需读取**。用户只要把 provider 标成「不需要 API Key（本地部署）」
   （设置 → AI 与对话），该 provider 的凭据解析、连接测试、界面渲染**一次都不碰
   钥匙串**，并且保存时会**删掉**钥匙串里遗留的旧条目。
3. **进程级缓存**。macOS 对未授权条目的每一次读取都弹一次授权框，所以同一凭据
   一个进程只读一次——否则就是"每发一条消息弹一次窗"。
4. **不落盘**。写配置时明文 key 一律剔除；磁盘上只有钥匙串里的那一份。
5. **绝不读取与 API Key 无关的任何条目**。
   不读浏览器密码库、不读 `login.keychain` 文件、不读 SSH 私钥、不读邮件/iMessage。

## 4. 屏幕录制：在 macOS 上被主动关死

截图功能（右键「看看屏幕」/ 主动识屏）是 **Windows 专有**的：

* 它依赖的前台窗口信息 `vision.foreground_window_info()` 是 Win32 实现，
  非 Windows 恒返回 `None`——在 macOS 上"看看屏幕"只剩一张没有上下文的截图；
* 而 `PIL.ImageGrab` 在 macOS 上会调用 `/usr/sbin/screencapture`，**会触发
  「屏幕录制」授权申请**。那是一项能读遍整块屏幕（含密码框、私信、文档）的权限。

所以 macOS 上：

* 菜单项**不存在**（`on_look_screen` 不接线 → 按项目 CONTEXT.md 的
  "Capability Unavailable" 语义整体省略）；
* 捕获入口本身也硬拒绝（`vision.screen_capture_supported()` 在非 Windows 恒
  `False`，`capture_screen_bytes()` 抛 `VisionError`、`capture_window_rect()`
  返回 `None`）。

两层都做了，是因为"菜单项没了"只挡住正常路径，挡不住将来有人把入口接回去。

## 5. 两道闸门

### 源码级：`tests/test_macos_permission_surface.py`

* 扫描 `pet/**/*.py`，命中任何 TCC 触发型 API / 敏感数据文件即失败
  （通讯录、日历、照片、定位、摄像头、麦克风、屏幕录制、辅助功能、输入监控、
  自动化、蓝牙、NFC、浏览器密码库、SSH 私钥……）；
* 登记在 `ALLOWED` 里的例外必须真的还在用（防止豁免表变成万能后门）；
* 凭证存储只能有 `SecretStore` 一个访问点；
* 屏幕捕获必须被 `screen_capture_supported()` 挡住，且平台判定必须排在
  `import ImageGrab` **之前**；
* `on_look_screen` 的接线必须带平台判定。

### 产物级：`scripts/check_macos_permissions.py`（构建脚本自动调用）

直接检查打包出的 `.app`：

* `Info.plist` 不含任何 `NS*UsageDescription`（含文件夹访问、本地网络等）；
* 签名 entitlements 为空；
* 产物里没有 `AVFoundation`/`Contacts`/`EventKit`/`Photos`/`CoreLocation`/
  `Speech`/`CoreBluetooth`/`pynput`/`Quartz`/`ApplicationServices` 这些模块。

任一条不满足 → **构建失败**。新增一项权限时必须同时改本文与该脚本的允许清单，
让"多要一个权限"变成一次需要显式说明的改动。

## 6. 用户可见的验证方法

```bash
# 1) 声明了哪些隐私用途？（本仓库产物应为空）
/usr/libexec/PlistBuddy -c "Print" \
  /Applications/dsh-pet-standalone-webm-chat.app/Contents/Info.plist | grep -i Usage

# 2) 带了什么 entitlement？（应为空）
codesign -d --entitlements - /Applications/dsh-pet-standalone-webm-chat.app

# 3) 系统里它申请过/被拒过哪些权限（TTY 里看 TCC 数据库需要完全磁盘访问；
#    图形界面更省事）
open "x-apple.systempreferences:com.apple.preference.security?Privacy"
```

系统设置 → 隐私与安全性里，桌宠**不应出现在任何一个分类下**。唯一可能看到它的是
「钥匙串访问」的授权弹框（§3），点一次"始终允许"后不再出现。
