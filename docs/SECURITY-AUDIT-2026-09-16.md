# 安全审计：macOS 权限面 + 经典应用安全（2026-09-16）

审计对象：本仓库 `pet/`、`scripts/`、`integrations/dsh-pet-bridge/`、打包脚本与构建产物。
目标平台：**macOS / Apple Silicon**。
方法：逐文件通读 + 本机实测（`codesign`/`PlistBuddy`/`stat`/真实 socket 起停）+ 用
"任何对系统钥匙串的访问立即抛错"的 pytest 插件跑全量套件。

一句话结论：**桌宠在 macOS 上不申请任何隐私权限**（无用途声明、无 entitlement、
无隐私框架模块），唯一触碰的敏感资源是用户自己填的 API Key；经典应用安全侧修掉
2 条值得修的（依赖修复的信任边界、跨主机重定向泄露凭据）与 3 条加固项，其余以
"本机实测 + 逐条说明为何不可利用"记录在案。

---

## A. macOS 权限面

### A.1 它实际需要什么

| 能力 | 需要授权 | 依据 |
|---|---|---|
| 出网（用户自配的模型服务 / 歌词源 / 报时 TTS） | **不需要** | macOS 对普通 app 出网不做授权 |
| 读写 `~/Library/Application Support/dsh-pet-standalone*/` | **不需要** | 不在 TCC 保护范围 |
| 系统钥匙串（用户自填的 API Key） | 需要（一次性） | `pet/chat/models.py:85-145` |
| 写 `~/Library/LaunchAgents/`（用户显式开关的开机自启） | **不需要** | `pet/autostart.py:61-62, 233-249` |
| 气泡通知 | **不需要** | `pet/desktop_notify.py` 是自绘窗，不用 `UNUserNotificationCenter` |
| 窗口层级 / Dock 图标 | **不需要** | `pet/platform_mac.py:31-61`、`pet/app.py:2619-2631` 只调 `setLevel:` / `setActivationPolicy:` |

### A.2 为什么"申请不到"而不只是"没申请"

实测构建产物（`codesign -d --entitlements -` / `PlistBuddy -c Print`）：

* **entitlements 为空**——无 `com.apple.security.*`，无 sandbox，无 hardened runtime，
  ad-hoc 签名（`flags=0x2(adhoc)`，`TeamIdentifier` 未设置）；
* **`Info.plist` 里零条 `NS*UsageDescription`**——因此需要用途字符串的隐私 API
  （摄像头、麦克风、通讯录、日历、照片、定位、语音识别、蓝牙、自动化……）被调用时
  系统会**直接终止进程**而不是弹框：这个 app 连"申请"这个动作都做不出来；
* 源码扫描（`tests/test_macos_permission_surface.py`）在 `pet/` 全树只命中
  `objc`（窗口层级）与 `AppKit`（注释），其余 40 余个 TCC 触发型符号零命中。

不需要用途字符串的**屏幕录制**是唯一必须靠代码关死的类别，见 A.3。

### A.3 屏幕录制：在 macOS 上被主动关死（本次修复）

原本 `pet/vision.py:279-290` 的 `capture_screen_bytes()` **没有任何平台判定**，
直接 `PIL.ImageGrab.grab(all_screens=True)`——在 macOS 上它会调用
`/usr/sbin/screencapture`，从而触发「屏幕录制」授权。而这个功能在 macOS 上本来就是
残的：`vision.foreground_window_info()`（`pet/vision.py:96-98`）是 Win32 实现，
非 Windows 恒返回 `None`，「看看屏幕」只剩一张没有上下文的截图。

修复（两层）：

* `pet/vision.py:229-247` 新增 `screen_capture_supported()`（非 Windows 恒 False）；
  `capture_screen_bytes()` 抛 `VisionError`、`capture_window_rect()` 返回 `None`；
* `pet/app.py:430-441` 在 macOS 上**不接线** `on_look_screen`，菜单项按项目
  CONTEXT.md 的 "Capability Unavailable" 语义整体消失，而不是留一个点了就弹权限框的入口。

### A.4 唯一被允许的敏感资源：系统钥匙串

`pet/chat/models.py::SecretStore`（service `dsh-pet-standalone`），实测走
纯 ctypes 的 Security 框架绑定（`keyring/backends/macOS/api.py` 用
`ctypes.CDLL(find_library('Security'))`，不需要 pyobjc）。约束与实测：

| 约束 | 实现 | 验证 |
|---|---|---|
| 唯一访问点 | `keyring` 只允许在 `SecretStore` 里 import | `tests/test_macos_permission_surface.py::TestCredentialStoreIsSinglePoint` |
| 按需读取 | 标记「不需要 API Key」的 provider **一次都不碰** | `pet/config.py::resolve_api_key`；`tests/test_keychain_access_policy.py` |
| 进程级缓存 | macOS 每次未授权读取都弹框，故一个进程只读一次 | `SecretStore._cache`；同上 |
| 不落盘 | 写盘剔除明文 `api_key`/`vision_api_key` | `pet/config.py::_redacted_data`；本机 `~/Library/Application Support/dsh-pet-standalone*/**` 实测零明文 |
| 不读无关条目 | 不读浏览器密码库 / `login.keychain` 文件 / SSH 私钥 / 邮件 | 同扫描器 |

---

## B. 经典应用安全

### B.1 发现（按严重度）

**B1｜中，条件可达｜依赖规格"自动修复"的候选可被他人植入 → dsh 内代码执行（已修）**
`pet/agent_link.py::_suggest_path_replacement` 会为失效的本地路径依赖猜一个替代
目录，`_repair_missing_dependency_specs`（`pet/agent_link.py:1112-1153`）随后把它
写进 profile 的 `package.json`，而 dsh 下次加载该 profile 会**执行** `link:` 指向
目录里的 `index.js`。旧实现的候选可以落在别人可写的目录里——macOS 的
`/Applications` 就是 `drwxrwxr-x root:admin`，另一个 admin 账号能往里面放目录。
触发无需用户点开关：启动自检 `pet/agent_link.py:1962-1982` 也会走到。
**修**：新增 `_exclusively_owned()`（`pet/agent_link.py:1033-1057`）——候选及其
**所有祖先目录**只要有一层对 group/other 可写就弃用。实测
`/Applications`、`/tmp` 被拒，`~`、`~/.dsh` 通过。回归见
`tests/test_security_hardening.py::TestDependencyRepairTrust`。

**B2｜中，同用户｜跨主机重定向会把 Bearer 凭据转发出去（已修）**
`urllib` 默认跟随重定向，而 `HTTPRedirectHandler.redirect_request` **保留
Authorization**（只丢 Content-Length / Content-Type）。用户把 base_url 配成明文
http 的局域网地址（本机真实配置就是 `http://192.168.x.x:8904`）时，中途改一个
`Location` 就能同时拿走 API Key 与聊天内容。调用点：`pet/chat/providers.py`
（聊天）、`pet/vision.py`（识屏）、`pet/balance.py`（余额）。
**修**：`pet/chat/providers.py:14-40` 新增 `_SameHostRedirectHandler` 并在模块导入时
`install_opener`——scheme/host/port 任一变化即拒（同主机内的路径重定向仍放行）。
回归见 `tests/test_security_hardening.py::TestRedirectCredentialContainment`。

**B3｜中低，同用户｜进程身份判定 fail-open → 可杀任意同用户进程（已修）**
`pet/child_pet_cleanup.py::_is_pet_process` 旧实现在 POSIX 上"读不到镜像就放行"
（`return os.name != "nt"`），而 macOS 没有 `/proc`——"读不到"是常态。于是往数据
目录写一份 `{"pid": 目标}` 的 `runtime-*.json`，用户点「退出所有小肥鱼」时就会
SIGTERM 掉编辑器、终端等任意同用户进程（Windows 侧反而 fail-closed）。
**修**：新增 `_posix_image_path()`（macOS 用 libproc `proc_pidpath`，纯 ctypes）与
`_own_image_path()`；**取不到身份一律返回 False**；镜像一致后再用三值
`_command_confirms_pet()` 看 argv（源码运行时所有 Python 进程镜像相同，只比镜像会
误伤）。回归见 `tests/test_security_hardening.py::TestProcessIdentityFailClosed`。

**B4｜低中，同用户｜碰撞 IPC 无鉴权、socket 默认 0755（已加固）**
`pet/collision_ipc.py` 的服务名可预测（sha256(APP_DIR_NAME+uid)[:20]），
创建 `QLocalServer` 后直接 `listen`，全仓无 `setSocketOptions`。本机实测：默认
`socketOptions = NoOptions`，socket 落在 `$TMPDIR`（每用户 0700）下、mode **0755**。
同用户任意进程可连入注入幽灵桌宠状态；`probe` 分支（`pet/collision_ipc.py:518-534`）
还能反复逼协调者退位，造成碰撞功能 DoS。跨用户被 `$TMPDIR` 的 0700 挡住，**不是
跨用户漏洞**。
**修**：listen 前显式 `setSocketOptions(UserAccessOption)`，listen 成功后
`_restrict_socket_file()` 把 socket 收紧到 0600。
残留 socket 的清理路径本身是谨慎的（`pet/collision_ipc.py:262-274` 只在持锁且
`_probe_live_server()` 确认无人应答时才 `removeServer`），未发现误删他人 socket。

**B5｜低中｜私事数据默认 0644/0755（已加固）**
全仓写盘无 `mode` 参数、无 `chmod`/`umask`；本机 umask 0022，实测 config.json
0644、sessions/ 0755、会话文件（**聊天原文**）0644。macOS 上被 `~/Library`(0700)
兜住故跨用户不可读，但 Linux 的 `$HOME` 常见 0755。另外 `temp + os.replace` 会把
目标文件的模式换成临时文件的模式，用户手动 `chmod 600` 的设置会被一次保存改回 0644。
**修**：`pet/config.py` 新增 `_chmod_private`/`_mkdir_private`，配置与数据目录收紧到
0600/0700（先收紧临时文件再 replace，替换后补一次）；`pet/chat/session_store.py`
的原子写同样收紧到 0600。回归见 `tests/test_security_hardening.py::TestPrivateFileModes`。
**未修**：`agent-events/*.jsonl`、`dsh-pet-bridge/`、LaunchAgent plist 仍是默认权限
（内容敏感度低且 macOS 下被 0700 父目录兜住），留作后续。

**B6｜低，同用户｜`DSH_PET_INSTANCE`/`--instance` 未净化即拼路径（未修，记录）**
`pet/config.py:597-598` 用 `instance_id` 拼 `config-{instance_id}.json`，
`pet/chat/session_store.py:472-479` 拼 `sessions{suffix}`。`DSH_PET_INSTANCE='../../x'`
能越出数据目录写一个可预测名字的 JSON。需要能控制进程环境（＝已能以该用户执行
程序），不构成权限边界突破。仓库已有先例可复用：
`pet/slot_manager.py:554-561` 的 `re.sub(r"[^A-Za-z0-9_-]","_",s)`。

**B7｜低，macOS 不可利用｜共享临时目录固定路径 + 锁文件跟随符号链接（未修）**
`pet/webm_clip.py:110` 固定名 `$TMPDIR/dsh-pet-media-meta-cache.json`，
`:725-771` 的锁文件用 `open(..., "a+b")` 跟随符号链接。macOS 的 `$TMPDIR` 是每用户
0700，跨用户不可达；这是 Linux 共享 `/tmp` 上的问题。缓存本体（`:774-800`）走
PID 后缀 tmp + replace，是安全的。

**B8｜低，同用户｜dsh 桥接控制队列无鉴权（未修，记录）**
`pet/dsh_control.py:15-21` 固定桥目录，`:52-113` 写
`watchdog-request-<uuid>.json`（`mkstemp` → 0600）；消费端
`integrations/dsh-pet-bridge/index.js:351-383` 轮询该目录，`:267-286` 只白名单
operation，不校验文件属主/权限。同用户任意进程可写入请求 → interrupt/replan 用户的
agent 会话。跨用户被 `~/Library`(0700) + `Application Support`(0700) 挡住，
**不是跨用户漏洞**。建议：消费端校验 uid + `mode & 0o077 == 0`，或 payload 带
per-user token。

**B9｜低/信息｜更新通道未认证且可变，但无自动执行路径（未修，记录）**
`pet/updater.py:32-36` 的回退源是 `cdn.jsdelivr.net/gh/<repo>@main/update.json`
（`@main` 可变）；`:135-164` 的 `download()` 无 hash/签名校验。但 `download()` 与
`pick_asset()` 在生产代码**零调用**（只命中 `tests/test_updater.py`），生产唯一入口
`pet/app.py:1940 latest_release()` 的结果只用于气泡文案（`pet/app.py:207-216`）。
最坏是伪造一条"发现新版本"的提示。另 `pet/agent_link.py:536-556` 在缺 pnpm 时会
自动 `npm install -g pnpm`（可变 registry、无钉选）——供应链上是真实一环。

### B.2 逐区结论

| 区域 | 结论 |
|---|---|
| 命令注入 | **干净**。全仓无 `shell=True` / `os.system` / `os.popen`；所有 `subprocess` 与 `startDetached` 均 argv 数组；HTTP 响应与桥接 JSONL 字段无一到达命令行 |
| 路径穿越/写入 | **已修 B1、B5**；B6/B7 记录在案。`pet/file_eater.py` 干净——被拖入的文件名从不参与路径构造（唯一问题：`:85-99` 在 GUI 线程做无界 `os.walk`，拖入 `/` 会冻结界面） |
| 本地 IPC | **已加固 B4**；B8 记录在案。`pet/dsh_responder.py:35-40` 把审批决定 POST 到 `127.0.0.1:<port>/api/respond`，无鉴权也不验证对端身份，端口取自固定清单（`pet/local_env.py:209-230`）——先占端口者能收下并自回 accepted |
| 反序列化 | **干净**。全仓无 `eval`/`exec`/`pickle`/`marshal`/`__import__`；`importlib` 仅用于读打包资源。**桌宠从不解析 YAML**：`pet/dsh_patch_layer.py:86-123` 是窄文法按行文本手术，看不懂就抛 `PatchLayerError`，`pet/agent_link.py:879-882` 跳过不写；插入内容是常量字面量（`pet/agent_link.py:864` 的无参 `build_bridge_entry()`）。`!!js` 求值在 dsh 侧，是 dsh 的信任边界 |
| HTTP 响应 | **基本干净**。修了 B2；两处低危：`pet/music_lyric.py:338-342, 384-387` 把响应里的 `songmid`/`song_id` 未 urlencode 拼进硬编码主机的后续查询（非 SSRF）。无响应体按长度/类型被采信落盘，无响应里的 URL 被再次 fetch |
| 供应链 | **轻**（B9）。`requirements.txt` 全为 `>=` 下限、无 hash |
| 文件权限 | **已加固 B5** |
| 自启动 | **干净**。`pet/autostart.py:233-249` 只写 Label / ProgramArguments / RunAtStart=True 三个键（非 frozen 时追加 WorkingDirectory），**无 KeepAlive**；ProgramArguments 来自 `:213-217` 的 `sys.executable` + `--slot 0`；本机实际 plist 已逐字核对。没有任何一项来自配置/拖入文件/网络，故"恶意配置让自启执行任意程序"不成立 |

### B.3 未能确定

* B1 未在真机复现完整链条（没有真的造一个 stale `link:` 依赖）；判定基于代码路径
  逐行阅读 + `_exclusively_owned` 的实测行为。
* `integrations/dsh-pet-bridge/index.js` 其余约 1800 行只做了定向审查（控制队列、
  事件写出、路径构造）。
* Windows 分支（命名管道默认 ACL、`os.startfile`、`taskkill`）仅读代码未实测——
  本次目标平台是 macOS。
* 跨用户连碰撞 socket 只由 mode/父目录权限推断，未以第二个 UID 实发 connect。
* `pet/webm_clip.py` 缓存被投毒后 `:1251-1254` 的 `int()`/`float()` 异常是否被上层
  收口未追完（macOS 上该路径跨用户不可达）。

---

## C. 两道权限闸门（防止回归）

| 闸门 | 位置 | 失败后果 |
|---|---|---|
| 源码级 | `tests/test_macos_permission_surface.py` | 出现 TCC 触发型 API / 绕过 `SecretStore` 读钥匙串 / 屏幕捕获缺平台判定 → 测试失败 |
| 产物级 | `scripts/check_macos_permissions.py`（`scripts/build_macos.sh` 自动调用） | 产物出现 `NS*UsageDescription` / entitlements / 隐私框架模块 → **构建失败** |

豁免表（`ALLOWED`）里的条目必须真的还在用，否则测试失败——防止它退化成万能后门。

## D. 用户可自行验证

```bash
# 声明了哪些隐私用途？（本仓库产物应为空）
/usr/libexec/PlistBuddy -c "Print" \
  /Applications/dsh-pet-standalone-webm-chat.app/Contents/Info.plist | grep -i Usage

# 带了什么 entitlement？（应为空）
codesign -d --entitlements - /Applications/dsh-pet-standalone-webm-chat.app

# 系统设置 → 隐私与安全性：桌宠不应出现在任何一个分类下
open "x-apple.systempreferences:com.apple.preference.security?Privacy"
```
