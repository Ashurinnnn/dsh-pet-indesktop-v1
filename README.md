# dsh-pet-indesktop-v1（个人 fork · macOS / Apple Silicon）

<p align="center">
  <img alt="平台" src="https://img.shields.io/badge/%E5%B9%B3%E5%8F%B0-macOS%20Apple%20Silicon-000000">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.13-3776AB">
  <a href="LICENSE"><img alt="License" src="https://img.shields.io/badge/License-MIT-orange"></a>
  <img alt="版本" src="https://img.shields.io/badge/%E7%89%88%E6%9C%AC-v4.3.0-blue">
</p>

这是基于[MerZlin/dsh-pet-indesktop](https://github.com/MerZlin/dsh-pet-indesktop) 的**个人 fork**，
只针对我自己的使用环境进行修改：**macOS（Apple Silicon）**。

上游是一个基于 **Python + PySide6** 的独立桌面宠物（透明无边框、置顶、可拖动、动画播放、
系统托盘、可选 AI 对话与本地 agent 联动）。上游的完整文档保留在
**[README-UPSTREAM.md](README-UPSTREAM.md)** —— 功能清单、设置项、素材规范、打包说明都在那里，
本文件只讲**这个 fork 改了什么**。

---

## 这个 fork 的改动

### 1. 适配 Unsloth Studio 拉起的 dsh（不再需要手工补丁脚本）

上游只认 `DSH_HOME` / `~/.dsh`，而 `unsloth start dsh [--persist]` 把 dsh home 放在
`~/.unsloth/studio/auth/agents/{dsh,.tmp/unsloth-dsh-*}`；桌宠从 Dock/Finder 启动时又拿不到
启动器注入的 `DSH_HOME`（GUI 进程不继承那个 shell 的环境）。结果就是"安装成功"却装到了
没在跑的实例上，之前只能靠一个外部脚本手工挂载。

现在桌宠会**自己发现**所有 dsh home 并按归属分别处理

### 2. agent 联动新增 OpenAI Codex

桌宠现在能把 Codex 也当作被监视的 agent：只读 tail Codex 自己写的会话 rollout
（`~/.codex/sessions/**/rollout-*.jsonl`），`task_started` → 忙碌、`task_complete` → 空闲、
`reasoning` → 思考中、工具调用 → 活动气泡。


### 3. 名字与称呼都能自己改

可以随意修改名字和称呼啦

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

### 5. 隐私与权限：最小权限

* MacOS上以最小权限运行，删除了跨平台所包括的不需要的权限。对API key相关的部分进行了检查。


## 安装（macOS / Apple Silicon）

从 [Releases](../../releases) 下载 `dsh-pet-standalone-webm-chat-macos-arm64.zip`，解压后把
`.app` 拖进「应用程序」。



## 本仓库的构建是 **ad-hoc 签名**（未做 Apple 公证），所以首次打开会有一次安全提示。

### 从源码运行

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
./.venv/bin/python -m pet
```

### 自己构建 .app

```bash
./scripts/build_macos.sh --variants webm-chat --dist build/macos
```

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

