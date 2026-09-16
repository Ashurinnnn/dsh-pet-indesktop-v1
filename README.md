# dsh-pet-indesktop-v1（个人 fork · macOS / Apple Silicon）

<p align="center">
  <img alt="平台" src="https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-macOS%20Apple%20Silicon-000000">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13-3776AB">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-orange"></a>
  <img alt="版本" src="https://img.shields.io/badge/%E7%89%88%E6%9C%AC-v4.3.0-blue">
</p>

这是基于[MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 的**个人 fork**，
只针对我自己的使用环境做改造：**macOS（Apple Silicon）**。

上游是一个基于 **Python + PySide6** 的独立桌面宠物（透明无边框、置顶、可拖动、动画播放、
系统托盘、可选 AI 对话与本地 agent 联动）。上游的完整文档保留在
**[README-UPSTREAM.md](README-UPSTREAM.md)** —— 功能清单、设置项、素材规范、打包说明都在那里，
本文件只讲**这个 fork 改了什么**。

---

## 这个 fork 改了什么

### 1. 原生适配 Unsloth Studio 拉起的 dsh（不再需要手工补丁脚本）

上游只认 `DSH_HOME` / `~/.dsh`，而 `unsloth start dsh [--persist]` 把 dsh home 放在
`~/.unsloth/studio/auth/agents/{dsh,.tmp/unsloth-dsh-*}`；桌宠从 Dock/Finder 启动时又拿不到
启动器注入的 `DSH_HOME`（GUI 进程不继承那个 shell 的环境）。结果就是"安装成功"却装到了
没在跑的实例上，之前只能靠一个外部脚本手工挂载。

现在桌宠会**自己发现**所有 dsh home 并按归属分别处理：

* **标准 home**（`DSH_HOME` / `~/.dsh`）：沿用上游方式，pnpm + `dsh.profile.bundles`；
* **托管 home**（Unsloth 等启动器创建的）：**不碰它的 `package.json`**（那是启动器的地盘），
  只往 `cordis.patch.yml` 补丁层插一条 `insert`、再落一份插件副本。这条路**不需要
  node/pnpm**（纯文件操作），而且补丁层带 `patchReload: live`——挂上即生效，不用重启 dsh、
  不打断正在进行的对话；
* **临时 home** 每次启动都是新目录，所以开启联动后会按分钟巡检，发现新实例就挂上
  （只碰托管 home，绝不动你自己的 `~/.dsh`）。

归属按**路径**判定而不是按启动方式，免得出现"从 Dock 启动走一种挂法、从 shell 启动走另一种"。

### 2. agent 联动新增 OpenAI Codex

桌宠现在能把 Codex 也当作被监视的 agent：只读 tail Codex 自己写的会话 rollout
（`~/.codex/sessions/**/rollout-*.jsonl`），`task_started` → 忙碌、`task_complete` → 空闲、
`reasoning` → 思考中、工具调用 → 活动气泡。

**不改 Codex 的任何配置、不装插件、不联网、不解析消息正文**（只认 `type` 字段）。
子代理会话（guardian / thread_spawn）会被过滤掉——否则每回收一个子代理就会把"任务完成"
气泡刷爆。首次读取走 backfill 跳到文件末尾，不会把历史会话重放成"刚刚在干活"。

内置 agent 的键与展示名收敛到 `pet/agent_registry.py` 单一真相源，配置 schema、窗口联动门、
设置页开关行都从它派生。

### 3. 名字与称呼都能自己改

* **桌宠的名字**：内置角色不再显示 ASCII 的目录 id `shenshen`，而是「深深」；想改就在
  **设置 → 外观 → 称呼与名字** 里填（留空恢复内置名）。界面、聊天窗、灵动岛统一走同一个显示名。
* **它怎么称呼你**：内置文案里的「主人」全部改成 `{user}` 占位符，可以在同一处换成任何称呼
  （自己的名字、昵称……）。自定义台词里也能用 `{user}`。

### 4. 本机环境探测与自适应

新增 `pet/local_env.py`：识别官方 dsh / Unsloth Studio / Ollama / llama.cpp / LM Studio，
在 **设置 → 应用启动 → 本机环境** 里只读展示（含"重新探测"）。探测一律只读——看可执行文件、
看目录、连本地端口，不启动任何东西。

据此做的自适应：

* **dsh 由托管启动器掌管时，桌宠不再另起一个实例**——过去"启动 DeepSeek Harness"会照官方
  路径起第二个 dsh（home 不同、互不认识，桌宠的桥接还挂在另一个 home 上），现在会提示你去
  它自己的入口启动；
* 端口候选（配置 → `DSH_PORT` → 3080 → 38080）统一由 `local_env` 给出，状态探测与启动器
  不再各维护一份。

### 5. 隐私与权限：只申请真正需要的

* **macOS 上不申请任何隐私权限**：构建产物的 entitlements 为空、`Info.plist` 里没有一条
  `NS*UsageDescription`。摄像头、麦克风、通讯录、日历、照片、定位、语音识别、蓝牙、自动化
  这些需要用途声明的 API，被调用时系统会**直接终止进程**而不是弹框——也就是说它连"申请"
  这个动作都做不出来。
* **屏幕录制被主动关死**：截图功能是 Windows 专有的（在 macOS 上拿不到前台窗口信息，本来就
  是残的），而 `PIL.ImageGrab` 在 macOS 会调 `screencapture` 触发屏幕录制授权。现在这两个
  入口在非 Windows 直接拒绝，菜单项也按"能力不可用"整体消失。
* **钥匙串只在必要时读**：唯一用途是保存你自己填的模型 API Key。用**设置 → AI 与对话 →
  「不需要 API Key」** 标记本地部署的服务后，该服务一次都不碰钥匙串，也不会把历史遗留的旧
  Key 发到本地端口；同一凭据一个进程只读一次（macOS 对未授权条目的每次读取都会弹一次框）。
* 详细边界见 **[docs/MACOS-PERMISSIONS.md](docs/MACOS-PERMISSIONS.md)**，
  完整安全审计见 **[docs/SECURITY-AUDIT-2026-09-16.md](docs/SECURITY-AUDIT-2026-09-16.md)**
  与 **[docs/SECURITY-AUDIT-API-KEY-EGRESS-2026-09-16.md](docs/SECURITY-AUDIT-API-KEY-EGRESS-2026-09-16.md)**。

### 6. 顺手修掉的几个真实缺陷

| 位置 | 问题 |
|---|---|
| `pet/agent_link.py` | 依赖规格"自动修复"的候选目录可能落在**别人可写**的地方（macOS 的 `/Applications` 就是 `drwxrwxr-x root:admin`），而那份代码会被 dsh 执行。现在候选及其**所有祖先**只要有一层对 group/other 可写就弃用 |
| `pet/chat/providers.py` | `urllib` 跟随重定向时**保留 `Authorization`**：base_url 是明文 http 的局域网地址时，改一个 `Location` 就能拿走 Bearer 凭据与聊天内容。现在跨 scheme/host/port 的重定向一律拒绝 |
| `pet/child_pet_cleanup.py` | 进程身份判定在 POSIX 上 **fail-open**（macOS 没有 `/proc`，"读不到镜像"是常态），伪造一份 `runtime-*.json` 就能杀掉任意同用户进程。现在 fail-closed |
| `pet/collision_ipc.py` | 本地 IPC socket 默认 0755 且无鉴权 → 显式用户级访问 + listen 后收紧到 0600 |
| `pet/config.py`、`pet/chat/session_store.py` | 配置与会话文件（**聊天原文**）默认 0644 → 收紧到 0600，数据目录 0700 |
| `scripts/fix_bridge_bundle.py` | 传相对 `--dist` 时（CI 就是这么传的）路径被 node 二次解析成双重路径，冒烟必然失败、构建被误判成"违反零依赖红线" |

---

## 安装（macOS / Apple Silicon）

从 [Releases](../../releases) 下载 `dsh-pet-standalone-webm-chat-macos-arm64.zip`，解压后把
`.app` 拖进「应用程序」。首次打开如果被 Gatekeeper 拦下，右键 →「打开」，或执行：

```bash
xattr -dr com.apple.quarantine /Applications/dsh-pet-standalone-webm-chat.app
```

> 本仓库的构建是 **ad-hoc 签名**（未做 Apple 公证），所以首次打开会有一次安全提示。

### 从源码运行

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pet
```

### 自己构建 .app

```bash
./scripts/build_macos.sh --variants webm-chat --dist build/macos
```

构建脚本内会依次跑三道自检：中文编码检查、**macOS 权限面检查**（产物不得有隐私用途声明 /
entitlements / 隐私框架模块）、桥接插件零依赖冒烟。任一不过即构建失败。

---

## 与上游的关系

* 上游：**[MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop)** ——
  功能文档、问题反馈、绝大多数功能都在那里，请优先支持上游。
* 本 fork 只做上面列的这些改造，目标是"在我这台 macOS 上开箱即用"。
* 上游 README（完整功能说明）保留在 [README-UPSTREAM.md](README-UPSTREAM.md)。

## 测试

```bash
./.venv/bin/python -m pytest -q          # 全量套件
./.venv/bin/python -m ruff check pet/ tests/ scripts/
```

本 fork 新增的测试：`test_dsh_homes.py`（托管 home 发现与补丁层挂载）、
`test_codex_monitor.py`、`test_user_address.py`、`test_local_env.py`、
`test_keychain_access_policy.py`、`test_macos_permission_surface.py`、
`test_security_hardening.py`。

## 许可

**MIT**，与上游一致。原始版权归 Merzlin（见 [LICENSE](LICENSE)）；本 fork 的修改同样以 MIT 发布。

---

<sub>本 fork 的所有改动都在 `main` 分支上，提交信息按"一个主题一个提交"组织，便于对照阅读。</sub>
