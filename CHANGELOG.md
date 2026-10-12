# 更新日志

## [v1.5.0] - 2026-10-11

### ✨ 新增

- **语音缓存复用**：按歌曲ID缓存下载的音频文件（30 分钟有效期），同一首歌重复点播直接秒发，无需重新下载
- **启动自动清理**：插件加载时清理上次运行残留的过期缓存文件（超过 1 小时），异常退出不再堆积垃圾文件
- **语音双模式**：`voice_send_mode` 可切换 `download`（先下载到本地再发送，稳定）/ `direct`（URL 直发），下载失败自动回退

### ⚙️ 优化

- **语音优先模式默认开启**：点歌直接发送语音消息，跳过音乐卡片（可在配置中关闭）
- **音质默认降为 128kbps**：QQ 语音最终压缩为 silk 格式，128k 与 320k 听感几乎无差别，下载体积减半，发送更快
- 新下载的缓存文件采用临时名 + 原子 rename，避免并发点歌时读到半成品

---

## [v1.3.0] - 2026-08-08

### ✨ 新增

- **SnowLuma 与 NapCat 共存支持**：音乐卡片发送三层自动降级（NapCat Json组件 → SnowLuma 标准格式 → 裸JSON简写），覆盖所有 OneBot v11 协议端
- **语音消息发送**：卡片失败后自动降级为语音（OneBot record / AstrBot Record），发送失败继续降级为图文

### 🐛 修复

- 搜索 API 返回字符串 data 时 `'str' object has no attribute 'get'` 崩溃
- HTTP 搜索 API `picUrl` 字段为 `null` 导致封面链接缺失域名
- SDK Cookie 校验失败后错误清空 Cookie，导致 VIP 歌曲仍为 30s 试听
- 搜索改为 HTTP 直连公共 API（`music.163.com/api/search/get`），取代 SDK 加密搜索，提升稳定性
- Cookie 通过请求参数直传，绕过 SDK 严格格式校验（`missing key: MUSIC_A_T` 等）

### ⚙️ 优化

- 指令更名：`/搜歌` → `/网易搜歌`，`/点歌` → `/网易点歌`，`/点歌帮助` → `/网易点歌帮助`
- 插件重命名：`astrbot_plugin_wangyiyun_diange` → `astrbot_plugin_netease_music`
- LLM Tool 名：`wangyiyun_diange` → `netease_music`
- 安装依赖新增 `setuptools==81.0.0`，兼容 Python 3.12 环境 `py_mini_racer` 编译

---

## [v1.2.0] - 2026-07-11

### ✨ 新增

- 初始版本，基于 NeteaseCloudMusic SDK 直连网易云官方 API
- `/搜歌` 搜索歌曲，`/点歌` 点播歌曲，`/点歌帮助` 显示帮助
- WebUI 配置 VIP Cookie，点播 VIP 歌曲完整版
- 调试模式，输出详细 API 请求/响应日志
- 搜索结果缓存
