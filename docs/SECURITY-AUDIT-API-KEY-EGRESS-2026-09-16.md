# API Key 输入路径与外泄风险审计（2026-09-16）

审计对象：本仓库 `pet/` 全部涉及凭证与出网请求的代码。
方法：逐文件通读 + 运行时实测（真实配置目录）+ 用"任何对系统钥匙串的访问立即
抛错"的 pytest 插件跑全量套件，确认没有隐藏的凭证读取路径。

一句话结论：**没有发现向非用户配置主机外发 API Key 的路径，也没有遥测/上报**。
主要风险是三条"配置错配/可被诱导"的现实路径，以及一处默认开启的后台外发。

---

## 0. 本次同时修掉的一个真实问题：测试会读系统钥匙串

跑测试套件时，macOS 会反复弹出"python 想使用钥匙串中储存的机密信息"。

根因：`pet.chat.models.SecretStore` 默认走系统钥匙串，而 `Config.reload()` 的
明文迁移与 `Config.resolve_api_key()` 都会用它；**conftest 里没有全局夹具拦住
它**。只要用例构造过设置对话框（AI 设置页会解析已存 key 来显示）或碰到过带明文
key 的 provider，就会读真实钥匙串。每个 `python` 进程一次弹窗，跑一遍套件就是
几十上百次。

已修：`tests/conftest.py` 新增 autouse 夹具 `_fake_keyring`，全程用内存假钥匙串；
`test_chat_subsystem` / `test_config_domains` / `test_config_instance` /
`test_config_key_migration` 这些本来就在验证 keyring 行为的用例自行覆盖，不受影响。

实测（见 §2）确认：**产品自身的启动路径一次都不碰钥匙串**，所以修好测试后不会再
有这类弹窗；只有用户真正用聊天/识屏/余额时才会读一次（macOS 上点"始终允许"即可）。

## 0b. 用户实测发现的第二个问题：空 API Key 连本地模型时反复弹钥匙串（已修）

场景：在 设置 → AI 与对话 里把 API 地址指向本地部署（localhost / 自建网关），
**API Key 留空**，结果每次用到凭据都弹一次系统授权框。

这是**产品**路径的问题，与 §0 的测试问题无关，三条原因叠加：

1. **输入框留空 ≠ 没有 Key**。凭据存在系统钥匙串里，留空只表示"不修改"
   （`ai_settings_page._capture_current_draft`、`settings_dialog._key_status` 的
   注释都写明"留空保持不变"）。于是每次解析凭据都会去读一次钥匙串，而
   `resolve_api_key` 的调用点散布在**发送消息、余额查询、识屏、设置页状态**等
   热路径上。
2. **macOS 对未授权条目的每一次读取都弹一次授权框**，`SecretStore` 每次
   `SecretStore()` 都新建实例、没有任何缓存——等于"每发一条消息弹一次"。
3. **用户没有任何办法表达"这个服务不需要 Key"**：历史遗留的旧 Key 会一直被
   翻出来（既弹窗，也会被当作 Bearer 发给本地端口）。

修复（`tests/test_keychain_access_policy.py` 全覆盖）：

| 改动 | 位置 |
|---|---|
| `SecretStore` 进程级缓存 + 新增 `delete()`（删除后留"已知为空"负缓存，不再回读） | `pet/chat/models.py` |
| `ProviderConfig` 新增持久化的**非机密**标记 `api_key_required`（默认 True，老配置行为不变） | `pet/chat/models.py` |
| `resolve_api_key`：标记为不需要 Key 时**直接返回空，绝不触碰钥匙串** | `pet/config.py` |
| 明文迁移跳过"不需要 Key"的 provider（那也是一次钥匙串访问） | `pet/config.py` |
| 两个设置界面各加一个「不需要 API Key（本地部署）」开关；勾选并保存会**真的删掉**钥匙串里那条陈旧凭据 | `pet/chat/ai_settings_page.py`、`pet/chat/settings_dialog.py` |
| 旧对话框的 `_key_status` 不再读钥匙串（它过去**每次渲染**都读一次） | `pet/chat/settings_dialog.py` |

效果：勾上开关后，该 provider 的凭据解析、连接测试、界面渲染**一次都不再访问
系统钥匙串**；未勾选的既有用户也至少从"每次调用一次"降到"每进程一次"。

---

## 1. 凭证的存放与加载

| 环节 | 位置 | 结论 |
|---|---|---|
| 存储 | 系统钥匙串，service = `dsh-pet-standalone`，account = `provider/<id>`（`pet/chat/models.py:85-99`） | 默认且唯一 |
| 落盘兜底 | keyring 不可用时只留内存；写盘走 `_redacted_data()` 剔除 `api_key`/`vision_api_key`（`pet/config.py:1397-1413`） | 磁盘上无明文 |
| 明文迁移 | `Config._migrate_plaintext_keys_to_keyring()`（`pet/config.py:993-1033`） | 老版本遗留明文一次性搬进钥匙串并丢弃 |
| 解析 | `Config.resolve_api_key()`：钥匙串优先，回退内存明文（`pet/config.py:1392-1395`） | 单一入口 |
| provider 间隔离 | `_merge_chat_data` 对缺失 `api_key_ref` 的 provider 按自身 id 归位（`pet/config.py:405-413`） | 不串 key |

**实测**：本机 `~/Library/Application Support/dsh-pet-standalone*/config*.json`
（含 webm / webm-chat 两个变体与会话文件）中，`sk-*` 形态明文与
`api_key`/`vision_api_key` 非空值**均为 0 命中**。

**实测**：把 `keyring.get_password`/`set_password` 换成抛异常的桩后构造真实
`Config()`，访问次数 **0**——启动路径不读钥匙串。

---

## 2. 出网调用点清单

| # | 位置 | 目标主机 | 主机来源 | 携带凭证 | 携带用户数据 | TLS 可关 | 触发 |
|---|---|---|---|---|---|---|---|
| 1 | `pet/chat/providers.py:120-121` | `config.base_url` | 用户配置 | Bearer key（`:119`） | 是：system prompt + 历史 + 用户文本 + 附件图片 | 可 | 用户发消息 |
| 2 | `pet/chat/providers.py:59-60` | 界面**未保存**的 URL（`chat/ai_settings_page.py:446-461`） | 用户配置(未落盘) | 表单为空时回退钥匙串已有 key（`:456`） | 仅 `ping` | 可 | 用户点"测试连接" |
| 3 | `pet/vision.py:381-386, 396-397` | `vision_base_url or base_url`（`:324-325`） | 用户配置 | 聊天 key 或独立视觉 key（`:365-380`） | 是：整窗 JPEG + 前台进程名/标题 + 陪伴记忆 | 可 | 右键"看看屏幕"；或后台识屏 |
| 4 | `pet/balance.py:88-91` | 同一 provider 的 `base_url` + `/user/balance` | 用户配置 | Bearer key（`:87`） | 否 | 可 | 用户操作；或定时器（默认关） |
| 5 | `pet/music_lyric.py:245` ← `321`/`339`, `351`, `367`/`385` | c.y.qq.com、lrclib.net、music.163.com | **硬编码** | 无 | 是：当前曲目 title/artist | 否 | 后台自动（切歌即发） |
| 6 | `pet/updater.py:43-47` ← `26`, `32-36` | api.github.com、jsdelivr CDN | **硬编码** | 无 | 否 | 否 | 菜单"检查更新"，启动路径无调用 |
| 7 | `pet/voice_chime_service.py:83-90` | `wss://speech.platform.bing.com/...`（edge_tts 库内硬编码） | **硬编码(第三方库)** | 无（库内公开 token） | 否（生成的报时句） | 否 | **后台自动 + 默认开启** |
| 8 | `pet/dsh_responder.py:36-42` | `http://127.0.0.1:{3080,38080,$DSH_PORT}/api/respond` | 硬编码回环 | 无 | 是：sessionId/approvalId/审批结果或答案 | 无 TLS | 用户点气泡按钮 |
| 9 | `pet/harness_launcher.py:45`；`pet/local_env.py` | 127.0.0.1 候选端口 | 硬编码回环 | 无 | 否（只 connect 不发数据） | 无 TLS | 后台（每 3s / 面板打开时） |
| 10 | `pet/harness_launcher.py:334,363` | `http://127.0.0.1:{port}` → 系统浏览器 | 硬编码回环 | 无 | 否 | 由浏览器 | 用户操作 / `harness_autostart`（默认关） |
| 11 | `pet/agent_link.py:546-548, 591-597` | npm registry（`npx --yes @deepseek-ai/dsh` 亦同） | 非应用内配置 | pet 不附加；npm/pnpm 自带 `~/.npmrc` token | 否 | 不由 pet 控制 | 用户安装；开启联动后后台自检会重跑 |
| 12 | `context_menus/*`、`quick_launch.py` | chat.deepseek.com / github.com / pan.quark.cn 等 | **硬编码** | 无 | 否 | 由浏览器 | 用户点菜单 |

---

## 3. 风险点

### R1（中）：TLS 校验可由 UI 关闭，且失败提示主动引导用户关闭
`pet/chat/providers.py:14-24` 是唯一的 SSL 上下文工厂，`verify=False` 走
`ssl._create_unverified_context()`（`:17-18`，隐含 `check_hostname=False`）。
同一个 context 被三处**带凭证**的请求共用：`providers.py:60`、`:121`、
`vision.py:397`（key + 屏幕 JPEG）、`balance.py:91`。

UI 可达：`chat/ai_settings_page.py:185/309/460`、`chat/settings_dialog.py:120-121`。
更关键的是引导：`providers.py:30` 的 `_CERT_HINT` 在 `:70`（测试连接失败）与
`:125`（聊天失败）把"可在 AI 设置中勾选『跳过 SSL 证书验证』"直接写进用户可见提示。
用户照做后，能 MITM 该连接者可同时拿到 Bearer key、截屏 JPEG 与聊天全文。

建议：把该提示改成"仅在你完全清楚风险时使用"，并在勾选时二次确认；或把
`verify=False` 限制在回环/私有网段（`127.0.0.1`/`10.`/`192.168.`/`*.local`）。

### R2（中）：独立视觉端点配置不全时，视觉 Key 会被发到聊天主机
`pet/vision.py:324`：`base_url = p.base_url if p.vision_same_as_chat else (p.vision_base_url or p.base_url)`；
而 key 在 `:372-378` **只取**视觉 key。于是"独立视觉端点 + 地址留空"时：
视觉 Key → 聊天 base_url。UI 文案（`chat/ai_settings_page.py:171`"视觉 API 地址：
留空复用聊天服务地址"）正鼓励留空，路径现实可达——用户把另一家平台的 key 填进
"视觉 API Key"而地址留空，该 key 会以 Bearer 出现在聊天服务商的请求里。

反向错配（聊天 key → 视觉主机）**已被显式挡住**（`vision.py:366-370`），不是风险。

建议：`vision_same_as_chat=False` 且 `vision_base_url` 为空时，直接用聊天 key
（即视为同端点），或直接报错要求填写地址。

### R3（低-中）："测试连接"把钥匙串里的 key 发到"刚输入未保存"的 URL
`chat/ai_settings_page.py:446-461` 的 `provisional_config()`：URL 取输入框当前值，
key 在表单为空时回退钥匙串（`:456`）→ 发往该 URL。改一下 API 地址、点一下测试，
原服务的 key 就发到了新主机。旧对话框同型（`chat/settings_dialog.py:331-350, 368`）。

建议：测试连接的 URL 与 key 必须来自同一个来源（要么都用表单值，要么都用已保存值）。

### R4（低）：主动识屏在开启后无逐次确认地外发整窗像素
`pet/proactive.py:434 → 447-475 → 616`，目标可能是与聊天不同的独立视觉端点
（`prefer_free_provider` 默认 True）。默认关闭且仅 Windows 生效（`config.py:187`、
`vision.foreground_window_info` 非 win32 返回 None），但一旦开启就是自动外发，
白名单里若有密码管理器/私人文档窗口，其内容会被逐次发出。

### R5（低）：歌词功能自动把"在听什么"发给三个硬编码第三方
`music_lyric_controller.py:582-587` → `music_lyric.py` 三个源。无凭证；开关默认关闭。
若播放器把非音乐内容填进 SMTC，同样会被外发。

### R6（低）：语音报时默认开启并访问硬编码第三方端点
`voice_chime_service.py:83-90`。不含凭证、不含用户数据（生成文本），但
"非用户配置主机 + 默认开启 + 后台自动"三条同时成立。建议首次使用时提示一次。

### R7（低）：审批决策以明文 HTTP 发往固定回环端口、无鉴权
`dsh_responder.py:36-42`。端口若被别的进程占用，数据仍会发出（成功判定在
`:49-50`，但内容已离开进程）。同机进程可读到"用户批了什么"。

### R8（提示）：桥接安装经 npm/pnpm 出网，凭证由 npm 自己携带
`agent_link.py:546-548`、`591-597`。pet 不附加凭证，但 npm 会读用户 `~/.npmrc`；
若其中配了私有 registry token，该 token 会随这些请求发出。内置插件
`integrations/dsh-pet-bridge/package.json` **无 dependencies**，正常无需下载依赖；
"是否必然访问 registry 元数据"未能确定。

---

## 4. 已确认无问题的部分

- **无遥测/analytics/崩溃上报**：`telemetry|analytics|sentry|mixpanel|umami|crash_report` 在 `pet/` 零命中。
- **更新检查不是后台自动**：只有菜单入口（`context_menus/registry.py:171-172` → `app.py:1915`），不带凭证。
- **聊天 key 不会被发到独立视觉端点**：`vision.py:366-380` 有显式守卫；逐行确认。
- **余额查询不跨服务商偷 key**：endpoint 由同一 provider 的 base_url 拼出（`balance.py:83`），
  `agent_link.py:4610-4616` 还额外要求 base_url 含 `deepseek.com`。
- **provider 之间不串 key**：`config.py:405-413` 归位/迁移。
- **key 不落盘**：`config.py:1397-1413` + 钥匙串默认路径；本机实测零明文。
- **除 `providers.py:18` 外无 TLS 降级点**：`CERT_NONE|check_hostname` 零命中。
- **本地 IPC 不经 TCP**：`collision_ipc.py` 走 QLocalServer/QLocalSocket。
- **子进程无外网能力**：`click_sound`（afplay/paplay/aplay）、`instance_launcher`（重启自身）、
  `webm_clip`（ffmpeg 只读本地文件）。
- **剪贴板只写不读、不发送**；**无 cookie 读取**。
- 整文件通读且确认无网络：`now_playing.py`、`node_runtime.py`、`dsh_state.py`、
  `festival*.py`、`edge_probe.py`、`dsh_control.py`、`dsh_patch_layer.py`。

---

## 5. 优先级建议

1. **R2**：视觉端点配置不全时的凭据错配——改动小、真实可达，建议尽快修。
2. **R1**：收敛"跳过 SSL"的引导文案 + 可选的回环/私网白名单。
3. **R3**：测试连接的 URL/key 同源。
4. **R6**：语音报时首次外发时提示一次。
5. R4/R5/R7/R8 属"用户已知的自动行为"，建议在设置页文案里写清，不必改逻辑。
