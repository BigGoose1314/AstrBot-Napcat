# 网易云点歌插件

AstrBot 网易云音乐点歌插件，纯 Python 直连官方 API，零外部服务依赖。

## 功能

| 指令 | 说明 | 示例 |
|------|------|------|
| `/网易搜歌 <关键词>` | 搜索歌曲，返回列表 | `/网易搜歌 晴天` |
| `/网易点歌 <序号>` | 点播搜索结果中的歌曲 | `/网易点歌 1` |
| `/网易点歌帮助` | 显示帮助信息 | `/网易点歌帮助` |

## 特性

- 纯 Python 直连网易云官方 API（通过 NeteaseCloudMusic SDK），无需部署任何中间服务
- **语音优先点歌**：下载歌曲到本地后发送 QQ 语音消息，同一首歌 30 分钟内重复点播自动复用缓存，秒发
- 音乐卡片降级链：oiapi.net 签名卡片 → 下载语音 → 图文，逐级自动降级
- 支持 WebUI 配置 VIP Cookie，点播 VIP 歌曲完整版
- Cookie 仅在配置界面可见，群聊中绝不会暴露
- 支持 AI 自然语言点歌（LLM Tool：说句"我想听晴天"就能自动搜歌点歌）
- 调试模式一键开关，输出完整 API 请求/响应日志
- 设备 UA 伪装，降低封号风险
- 封面自动补齐（搜索 API 缺失时通过详情 API 回填）
- 启动自动清理残留缓存文件，磁盘不留垃圾

## 安装

1. 下载 `astrbot_plugin_netease_music.zip`
2. AstrBot WebUI → 插件管理 → 上传插件 → 启用

> ⚠️ **注意**：新版 AstrBot 使用 Python 3.12，但仍兼容 Python 3.10。由于 setuptools 81.0.0 是最后一个支持 NeteaseCloudMusic SDK 所需 PKG 功能的版本，插件安装时会自动执行 `pip install setuptools==81.0.0`。如果遇到 `setuptools` 相关安装报错，请手动执行该命令后重试。

## 配置

在 AstrBot WebUI 插件配置面板中设置：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `cookie` | text | 否 | 网易云音乐会员 Cookie。登录 music.163.com 后从 F12 → Application → Cookies 复制完整字符串。配置后可点 VIP 歌曲完整版，留空则仅免费歌曲/30s 试听 |
| `search_limit` | int | 否 | 搜索结果数量，默认 10 |
| `cache_timeout` | int | 否 | 搜索结果缓存时间（秒），默认 300 |
| `bitrate` | int | 否 | 音频比特率，默认 128000（语音场景推荐）。320000=极高。128k 与 320k 在 QQ 语音中听感几乎无差别，但下载快一倍 |
| `prefer_voice` | bool | 否 | 语音优先模式，默认开启，点歌直接发语音（带 30 分钟缓存复用）。关闭则优先音乐卡片 |
| `voice_send_mode` | text | 否 | 语音发送方式：`download`=下载到本地再发送（默认，稳定）；`direct`=URL 直发 |
| `voice_temp_dir` | text | 否 | 语音缓存目录，留空用系统临时目录。必须与 NapCat 在同一文件系统 |
| `device_ua` | text | 否 | 浏览器 UA 字符串，用于伪装请求来源降低封号风险。留空自动使用 Chrome/Win10 默认 UA |
| `debug` | bool | 否 | 调试模式，开启后在控制台输出详细 API 请求/响应日志 |

## Cookie 获取

> ⚠️ Cookie 仅在 WebUI 配置面板填写，群聊消息中绝不会暴露。

1. 浏览器打开 https://music.163.com 并登录会员账号
2. F12 → Application → Cookies → 复制所有 cookie
3. 格式示例：`MUSIC_U=xxx; __csrf=xxx; ...`
4. 粘贴到 WebUI 插件配置的 `cookie` 字段

## 发送策略

**QQ 群（NapCat / LLOneBot 等）：**

```
点歌（默认语音优先）
  ↓
⓪ 下载歌曲到本地 → 语音消息（30 分钟内重复点播走缓存，秒发）
  ↓ 语音优先已关闭 或 语音发送失败
① oiapi.net 音乐卡片签名 → QQ 原生音乐卡片
  ↓ 签名失败/发送异常
② 语音消息：插件先下载到本地再发送（NapCat 自动转 silk）
  ↓ 下载失败自动回退 URL 直发，仍失败
③ 降级 → 封面图 + 歌曲信息文本
```

**QQ 官方机器人（qq_official / openqq）：**

由于官方接口不支持卡片和语音消息，直接发送封面图+文本。插件会自动检测平台类型切换策略。

## 语音消息说明

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `prefer_voice` | 开 | 开启后跳过音乐卡片，直接下载发送语音消息 |
| `voice_send_mode` | `download` | `download` = 插件下载到本地再发送，稳定可靠；`direct` = URL 直发给 NapCat |
| `voice_temp_dir` | 空 | 下载模式的缓存目录，留空用系统临时目录 |

**download 模式工作原理：**

1. 插件从网易云 CDN 下载音频（mp3/m4a/flac）到本地缓存目录
2. 通过 OneBot `record` 消息发送本地文件，NapCat 自动转码为 QQ silk 语音格式
3. 文件按歌曲ID缓存 30 分钟，期间重复点播直接复用秒发
4. 缓存过期由下次下载自动覆盖；插件启动时清理 1 小时以上的残留文件（异常退出也不留垃圾）

**⚠️ 重要前提**：缓存目录必须与 NapCat **在同一台机器 / 同一文件系统**。如果 AstrBot 跑在 Docker 容器里而 NapCat 在宿主机，请把某个目录挂载为共享，并填写到 `voice_temp_dir`。

## AI 点歌（LLM Tool）

直接跟机器人说人话即可：

- "来首周杰伦的晴天"
- "我想听林俊杰的歌"
- "放一首稻香"

AI 会自动调用 `netease_music` 工具搜索并点歌，发送音乐卡片到群聊。

## 文件结构

```
astrbot_plugin_netease_music/
├── main.py              # 插件主类（指令处理 + LLM Tool）
├── netease_api.py       # 网易云 API 封装（搜索/URL/详情）
├── music_card.py        # QQ 音乐卡片签名（oiapi.net）
├── _conf_schema.json    # WebUI 配置定义
├── metadata.yaml        # 插件元数据
├── requirements.txt     # 依赖清单
└── README.md            # 本文档
```

## 依赖

- setuptools == 81.0.0（运行时构建依赖，安装时自动降级；Python 3.12 环境必须手动执行）
- NeteaseCloudMusic >= 0.1.0（网易云 API SDK，内部使用 py_mini_racer 运行 weapi 加密 JS）
- requests >= 2.25.0（HTTP 请求）

### Python 3.12 环境特别注意

NeteaseCloudMusic 依赖 `py_mini_racer`，该包在安装时需要 setuptools <= 81.0.0 才能正确执行 C 扩展编译。如果你的 AstrBot 运行在 Python 3.12 上且安装插件报错，请在 AstrBot 终端执行：

```bash
pip install setuptools==81.0.0
```

然后重新安装本插件。

## 已知问题

### SnowLuma 音乐卡片兼容性（已修复）

SnowLuma 旧版本对 JSON 消息的 `data` 字段有严格校验，导致音乐卡片发送失败（`retcode=1400`）。该问题已被 SnowLuma 在 [commit 65683bb](https://github.com/SnowLuma/SnowLuma/commit/65683bb44d18f88082b746e3f369eefc6233b71f) 中修复——`json` 类型的 `data`/`config` 字段现在接受 dict 对象，与 AstrBot `Json` 组件格式兼容。更新 SnowLuma 到包含此 commit 的版本即可正常使用音乐卡片，无需修改本插件。

## 许可证

MIT

