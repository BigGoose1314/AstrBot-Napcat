"""
网易云点歌插件 - AstrBot Plugin (v1.5.0)

直连网易云官方 API，无需部署任何外部服务。

功能：
  /网易搜歌 <关键词>     - 搜索网易云音乐歌曲，返回列表
  /网易点歌 <序号>       - 点播搜索结果中的歌曲（语音消息 / 音乐卡片 / 图文）
  /网易点歌帮助          - 显示帮助信息

特性：
  - 使用 pyncm 库直接调用网易云官方 API，零外部依赖服务
  - 语音优先模式（默认开启）：下载到本地发送语音，30 分钟内重复点播走缓存秒发
  - 插件启动时自动清理残留缓存文件
  - 语音消息双模式：URL 直发 或 下载到本地再发送
  - 支持 WebUI 配置 VIP cookie，点播 VIP 歌曲完整版
  - Cookie 仅在配置界面可见，群聊中绝不会暴露
  - 调试模式：WebUI 开关一键输出详细 API 请求/响应日志

依赖：pip install pyncm
"""

import time
import json
from typing import Dict, List, Optional

from astrbot.api.event import filter, AstrMessageEvent, MessageEventResult
from astrbot.api.star import Context, Star, register
from astrbot.api import logger, AstrBotConfig
from astrbot.api.message_components import Plain, Image

from .netease_api import (
    NeteaseCloudMusicAPI,
    mask_cookie,
    parse_search_result,
    parse_song_url_result,
)

from .music_card import (
    build_sign_params,
    sign_music_card,
    send_card_via_astrbot,
    send_voice_via_astrbot,
    send_voice_download_then_send,
    cleanup_stale_tempfiles,
)


PLUGIN_TAG = "[网易云点歌]"
VERSION = "1.5.0"


def _log_debug_guard(condition: bool, msg: str):
    if condition:
        logger.debug(f"{PLUGIN_TAG} {msg}")


@register(
    "astrbot_plugin_netease_music",
    "BigGoose",
    "网易云点歌",
    VERSION,
    "https://github.com/BigGoose1314/astrbot_plugin_netease_music",
)
class WangYiYunDiange(Star):
    """网易云点歌插件主类"""

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self._api: Optional[NeteaseCloudMusicAPI] = None
        self._cache: Dict[str, tuple] = {}

        # 清理上次运行残留的语音缓存文件（异常退出可能遗留）
        try:
            cleanup_stale_tempfiles(
                dest_dir=self.voice_temp_dir or None, logger=logger
            )
        except Exception as e:
            logger.warning(f"{PLUGIN_TAG} 缓存清理失败（不影响使用）: {e}")

        self._log_config_summary()

    # ── 配置 / API ──

    @property
    def api(self) -> NeteaseCloudMusicAPI:
        if self._api is None:
            cookie = self.config.get("cookie", "")
            bitrate = self.config.get("bitrate", 128000)
            debug = self.config.get("debug", False)
            device_ua = self.config.get("device_ua", "")
            self._api = NeteaseCloudMusicAPI(
                cookie=cookie, bitrate=bitrate,
                debug=debug, logger=logger,
                device_ua=device_ua,
            )
            logger.info(
                f"{PLUGIN_TAG} API 初始化 | "
                f"bitrate={bitrate} debug={'ON' if debug else 'OFF'} "
                f"cookie={'已配置' if cookie else '未配置'}"
            )
        return self._api

    @property
    def is_debug(self) -> bool:
        return bool(self.config.get("debug", False))

    @property
    def voice_mode(self) -> str:
        """语音发送方式：direct=URL 直发；download=先下载到本地再发送（推荐）"""
        return str(self.config.get("voice_send_mode", "download") or "download").strip().lower()

    @property
    def prefer_voice(self) -> bool:
        """语音优先：跳过音乐卡片，直接下载发送语音消息（默认开启）"""
        return bool(self.config.get("prefer_voice", True))

    @property
    def voice_temp_dir(self) -> str:
        """语音临时文件目录（留空用系统临时目录；必须对 NapCat 可见）"""
        return str(self.config.get("voice_temp_dir", "") or "").strip()

    def _log_config_summary(self):
        cfg = {
            "cookie": mask_cookie(self.config.get("cookie", "")),
            "search_limit": self.config.get("search_limit", 10),
            "cache_timeout": self.config.get("cache_timeout", 300),
            "bitrate": self.config.get("bitrate", 128000),
            "voice_send_mode": self.voice_mode,
            "prefer_voice": self.prefer_voice,
            "voice_temp_dir": self.voice_temp_dir or "(系统临时目录)",
            "debug": self.config.get("debug", False),
        }
        logger.info(f"{PLUGIN_TAG} v{VERSION} 已加载 | 配置: {cfg}")

    async def terminate(self):
        logger.info(f"{PLUGIN_TAG} 插件卸载中...")
        if self._api:
            await self._api.close()

    # ── 缓存 ──

    def _get_cache_key(self, event: AstrMessageEvent) -> str:
        msg_obj = event.message_obj
        group_id = getattr(msg_obj, "group_id", None) or "private"
        return f"{group_id}:{event.get_sender_id()}"

    def _get_cached_songs(self, event: AstrMessageEvent) -> Optional[List[Dict]]:
        key = self._get_cache_key(event)
        cached = self._cache.get(key)
        if cached is None:
            return None
        songs, ts = cached
        timeout = self.config.get("cache_timeout", 300)
        if time.time() - ts < timeout:
            _log_debug_guard(self.is_debug, f"缓存命中: {key} 剩余={timeout - (time.time() - ts):.0f}s")
            return songs
        _log_debug_guard(self.is_debug, f"缓存过期: {key}")
        del self._cache[key]
        return None

    def _cache_songs(self, event: AstrMessageEvent, songs: List[Dict]):
        key = self._get_cache_key(event)
        self._cache[key] = (songs, time.time())
        _log_debug_guard(self.is_debug, f"缓存写入: {key} {len(songs)}首")

    # ═══════════════════════════════════════
    #  /网易点歌帮助
    # ═══════════════════════════════════════

    @filter.command("网易点歌帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        has_cookie = bool((self.config.get("cookie", "") or "").strip())
        vip_line = "✅ 已配置（可点 VIP 歌曲完整版）" if has_cookie else "❌ 未配置（仅免费歌曲/30s 试听）"
        debug_line = "🐛 调试：开启" if self.is_debug else "🐛 调试：关闭"

        yield event.plain_result(
            "🎵 **网易云点歌插件** v" + VERSION + "\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔍 /网易搜歌 <关键词>    搜索歌曲\n"
            "   例：/网易搜歌 晴天\n\n"
            "▶ /网易点歌 <序号>       点播歌曲\n"
            "   例：/网易点歌 1\n\n"
            "ℹ /网易点歌帮助          显示帮助\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📤 发送方式：音乐卡片 → 语音 → 图文（自动降级）\n"
            "   可在配置中开启「语音优先」直接发语音\n"
            "   语音下载模式：插件先下载到本地再发送，更稳定\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 {vip_line}\n"
            f"{debug_line}\n"
            "⚙ Cookie 请在 WebUI 插件配置中设置\n"
            "   如需解析会员歌曲，请检查你的网易云账号是否有会员。建议使用小号弄。"
        )

    # ═══════════════════════════════════════
    #  /网易搜歌
    # ═══════════════════════════════════════

    @filter.command("网易搜歌")
    async def cmd_search(self, event: AstrMessageEvent):
        keyword = self._extract_arg(event.message_str, "网易搜歌")
        if not keyword:
            yield event.plain_result("请提供搜索关键词，例如：\n/网易搜歌 晴天\n/网易搜歌 周杰伦 稻香")
            return

        sender = event.get_sender_id()
        logger.info(f"{PLUGIN_TAG} 搜索 | user={sender} keyword='{keyword}'")

        try:
            limit = self.config.get("search_limit", 10)
            raw = await self.api.search(keyword, limit=limit)
        except Exception as e:
            logger.error(f"{PLUGIN_TAG} 搜索异常 | {type(e).__name__}: {e}", exc_info=self.is_debug)
            yield event.plain_result(f"搜索失败: {str(e)}")
            return

        if raw.get("code") != 200:
            logger.error(f"{PLUGIN_TAG} API错误 | code={raw.get('code')} msg='{raw.get('message')}'")
            yield event.plain_result("搜索失败，请稍后重试")
            return

        songs = parse_search_result(raw)
        if not songs:
            yield event.plain_result(f"未找到与「{keyword}」相关的歌曲")
            return

        self._cache_songs(event, songs)

        lines = [f"🔍 搜索「{keyword}」的结果：\n"]
        for i, s in enumerate(songs, 1):
            dur = self._fmt_duration(s.get("duration", 0))
            lines.append(f"  {i}. {s['name']} - {s['artists']}")
            lines.append(f"     💿 {s['album']}  ⏱ {dur}")

        lines.append(f"\n共 {len(songs)} 首 | 输入 /网易点歌 <序号> 点播")
        if not self.api.has_vip_cookie:
            lines.append("💡 配置 VIP Cookie 后可点 VIP 歌曲完整版")

        yield event.plain_result("\n".join(lines))

    # ═══════════════════════════════════════
    #  /网易点歌
    # ═══════════════════════════════════════

    @filter.command("网易点歌")
    async def cmd_play(self, event: AstrMessageEvent):
        text = self._extract_arg(event.message_str, "网易点歌")
        if not text:
            yield event.plain_result("请提供歌曲序号，例如：/网易点歌 1\n（先用 /网易搜歌 搜索歌曲）")
            return

        try:
            index = int(text.split()[0]) - 1
        except ValueError:
            yield event.plain_result("序号必须是数字，例如：/网易点歌 1")
            return

        songs = self._get_cached_songs(event)
        if not songs:
            yield event.plain_result("⚠️ 请先搜索歌曲！\n使用 /网易搜歌 <关键词> 搜索，然后 /网易点歌 <序号> 点播")
            return
        if index < 0 or index >= len(songs):
            yield event.plain_result(f"序号超出范围 (1-{len(songs)})，请重新输入")
            return

        song = songs[index]
        sid = song["id"]
        name = song["name"]
        artists = song["artists"]
        album = song["album"]
        cover = song["cover"]
        dur = self._fmt_duration(song.get("duration", 0))

        sender = event.get_sender_id()
        logger.info(f"{PLUGIN_TAG} 点播 | user={sender} song='{name} - {artists}' id={sid}")

        # 修复封面：HTTP搜索API可能不返回picUrl，通过SDK歌曲详情补充
        if not cover.startswith("https://"):
            try:
                detail_raw = await self.api.get_song_detail([sid])
                detail_songs = detail_raw.get("songs", [])
                if detail_songs:
                    detail_al = detail_songs[0].get("al", {})
                    detail_cover = detail_al.get("picUrl", "")
                    if detail_cover:
                        cover = detail_cover + "?param=300y300"
                        song["cover"] = cover  # 回写缓存
                        _log_debug_guard(self.is_debug, f"封面补齐: {cover[:60]}...")
            except Exception as e:
                _log_debug_guard(self.is_debug, f"封面补齐失败: {e}")

        yield event.plain_result(f"🔍 正在获取「{name} - {artists}」...")

        # 获取播放链接
        try:
            url_raw = await self.api.get_song_url(sid)
        except Exception as e:
            logger.error(f"{PLUGIN_TAG} get_song_url 异常 | {type(e).__name__}: {e}", exc_info=self.is_debug)
            yield event.plain_result(f"获取播放链接失败: {str(e)}")
            return

        url_info = parse_song_url_result(url_raw)
        has_cookie = self.api.has_vip_cookie
        _log_debug_guard(self.is_debug, f"URL 解析: {url_info} has_cookie={has_cookie}")

        text_parts = [
            f"🎵 {name}",
            f"🎤 歌手：{artists}",
            f"💿 专辑：{album}",
            f"⏱ 时长：{dur}",
            f"🖼 封面：{cover}",
        ]

        if url_info and url_info.get("url"):
            play_url = url_info["url"]
            br_kbps = url_info.get("br", 0) // 1000
            is_trial = url_info.get("is_free_trial", False)

            text_parts.append(f"🎧 音质：{br_kbps}kbps")
            text_parts.append(f"▶ 链接：{play_url}")

            if is_trial:
                text_parts.append("⚠️ VIP 歌曲 — 当前仅 30 秒试听")
                text_parts.append("   在 WebUI 中配置 VIP Cookie 后可听完整版")
            elif has_cookie:
                text_parts.append("✅ VIP 歌曲 · 完整版")
            else:
                text_parts.append("✅ 免费歌曲")
        else:
            if has_cookie:
                text_parts.append("⚠️ 无法获取播放链接，Cookie 可能已过期")
            else:
                text_parts.append("⚠️ 无法获取，可能是 VIP 歌曲")
                text_parts.append("   请在 WebUI 配置 VIP Cookie 后重试")

        message_text = "\n".join(text_parts)

        # qq_official 平台：卡和语音均不支持，直跳图文
        _msg_obj = getattr(event, "message_obj", None)
        _plat = str(getattr(_msg_obj, "platform", "") or getattr(_msg_obj, "platform_name", "")).lower()
        _is_qq_official = "qq_official" in _plat or "openqq" in _plat

        play_url = url_info.get("url") if url_info else ""
        has_audio = bool(play_url)

        card_sent = False
        voice_sent = False

        if not _is_qq_official:
            # ── 语音优先模式：跳过卡片，直接下载发送语音 ──
            if self.prefer_voice and has_audio:
                yield event.plain_result(f"⬇ 正在获取「{name} - {artists}」...")
                voice_sent = await self._send_voice(event, play_url, cache_key=str(sid))

            # ── 优先尝试 oiapi 音乐卡片签名 ──
            if not voice_sent and has_audio and cover:
                try:
                    card_params = build_sign_params(
                        url=play_url,
                        song=name,
                        singer=artists,
                        cover=cover,
                        fmt="163",
                    )
                    ark_info = sign_music_card(card_params, logger=logger)
                    if ark_info:
                        card_sent = await send_card_via_astrbot(event, ark_info, logger=logger)
                except Exception as e:
                    logger.warning(f"{PLUGIN_TAG} 卡片发送异常: {e}")

            # ── 卡片失败/未发，降级为语音发送 ──
            if not card_sent and not voice_sent and has_audio:
                if self.voice_mode == "download":
                    yield event.plain_result(f"⬇ 正在获取「{name} - {artists}」...")
                voice_sent = await self._send_voice(event, play_url, cache_key=str(sid))

        # ── 语音失败，降级为图文发送 ──
        if not card_sent and not voice_sent:
            try:
                if cover:
                    full_cover = cover if "?param=" in cover else cover + "?param=500y500"
                    chain = [Plain(message_text + "\n"), Image.fromURL(full_cover)]
                    yield event.chain_result(chain)
                else:
                    yield event.plain_result(message_text)
            except Exception as e:
                logger.warning(f"{PLUGIN_TAG} 图文发送失败，回退纯文本 | {e}")
                yield event.plain_result(message_text)

    # ═══════════════════════════════════════
    #  辅助
    # ═══════════════════════════════════════

    async def _send_voice(self, event: AstrMessageEvent, play_url: str, cache_key: str = "") -> bool:
        """
        按配置的语音模式发送语音消息。
        download 模式：先下载到本地再发（带缓存复用，失败自动回退 URL 直发）。
        direct 模式：直接把 URL 交给协议端下载。
        """
        if self.voice_mode == "download":
            try:
                ok = await send_voice_download_then_send(
                    event, play_url,
                    temp_dir=self.voice_temp_dir or None,
                    cache_key=cache_key or None,
                    logger=logger,
                )
                if ok:
                    return True
                logger.info(f"{PLUGIN_TAG} 本地下载发送未成功，回退 URL 直发")
            except Exception as e:
                logger.warning(f"{PLUGIN_TAG} 本地下载发送异常: {e}")
        try:
            return await send_voice_via_astrbot(event, play_url, logger=logger)
        except Exception as e:
            logger.warning(f"{PLUGIN_TAG} 语音发送异常: {e}")
            return False

    @staticmethod
    def _extract_arg(raw_text: str, cmd: str) -> str:
        t = raw_text.strip()
        for prefix in (f"/{cmd}", cmd):
            if t.startswith(prefix):
                t = t[len(prefix):].strip()
                break
        return t

    @staticmethod
    def _fmt_duration(ms: int) -> str:
        if ms <= 0:
            return "00:00"
        s = ms // 1000
        return f"{s // 60:02d}:{s % 60:02d}"

    # ═══════════════════════════════════════
    #  LLM 工具（AI 自然语言点歌）
    # ═══════════════════════════════════════

    @filter.llm_tool("netease_music")
    async def llm_diange(
        self, event: AstrMessageEvent, keyword: str, index: int = -1
    ) -> str:
        """
        网易云音乐搜索和点歌工具。
        当用户想听歌、点歌、放歌、搜索歌曲时调用此工具。
        - 搜索: 提供 keyword（歌名或歌手），不传 index 或 index=-1，返回搜索结果列表供用户选择。
        - 点歌: 提供 keyword 和 index（从0开始），直接向用户发送音乐卡片或图文消息。

        Args:
            keyword (string): 歌名或歌手关键词
            index (number): 歌曲序号，-1 仅搜索返回列表，0 播第一首，以此类推
        """
        # 搜索
        try:
            raw = await self.api.search(keyword, limit=10)
        except Exception:
            return "搜索失败，请稍后重试"

        songs = parse_search_result(raw)
        if not songs:
            return f"未找到与「{keyword}」相关的歌曲"

        # 仅搜索模式：返回列表给 AI
        if index < 0:
            items = []
            for i, s in enumerate(songs):
                dur = self._fmt_duration(s.get("duration", 0))
                items.append({
                    "index": i,
                    "name": s["name"],
                    "artist": s["artists"],
                    "album": s["album"],
                    "duration": dur,
                })
            result = {"keyword": keyword, "total": len(songs), "songs": items}
            return f"搜索结果 ({len(songs)}首):\n{json.dumps(result, ensure_ascii=False)}"

        # 点歌模式：选第 index 首
        if index >= len(songs):
            return f"序号 {index} 超出范围，共 {len(songs)} 首"

        song = songs[index]
        sid = song["id"]
        name = song["name"]
        artists = song["artists"]
        album = song["album"]
        cover = song["cover"]
        dur = self._fmt_duration(song.get("duration", 0))

        # 补齐封面
        if not cover.startswith("https://"):
            try:
                detail_raw = await self.api.get_song_detail([sid])
                detail_songs = detail_raw.get("songs", [])
                if detail_songs:
                    c = detail_songs[0].get("al", {}).get("picUrl", "")
                    if c:
                        cover = c + "?param=300y300"
            except Exception:
                pass

        # 获取播放链接
        try:
            url_raw = await self.api.get_song_url(sid)
        except Exception:
            return "获取播放链接失败"
        url_info = parse_song_url_result(url_raw)
        has_cookie = self.api.has_vip_cookie

        text_parts = [
            f"🎵 {name}",
            f"🎤 歌手：{artists}",
            f"💿 专辑：{album}",
            f"⏱ 时长：{dur}",
            f"🖼 封面：{cover}",
        ]
        play_url = ""
        if url_info and url_info.get("url"):
            play_url = url_info["url"]
            br_kbps = url_info.get("br", 0) // 1000
            is_trial = url_info.get("is_free_trial", False)
            text_parts.append(f"🎧 音质：{br_kbps}kbps")
            text_parts.append(f"▶ 链接：{play_url}")
            if is_trial:
                text_parts.append("⚠️ 仅试听，配置 VIP Cookie 后可听完整版")
            elif has_cookie:
                text_parts.append("✅ VIP 完整版")
            else:
                text_parts.append("✅ 免费歌曲")
        else:
            text_parts.append("⚠️ 无法获取播放链接")

        message_text = "\n".join(text_parts)

        # qq_official 平台跳过卡片
        _msg_obj2 = getattr(event, "message_obj", None)
        _plat2 = str(getattr(_msg_obj2, "platform", "") or getattr(_msg_obj2, "platform_name", "")).lower()
        _is_qq_official2 = "qq_official" in _plat2 or "openqq" in _plat2

        card_sent = False
        voice_sent = False
        if not _is_qq_official2:
            # 语音优先模式：直接下载发送语音
            if self.prefer_voice and play_url:
                try:
                    voice_sent = await self._send_voice(event, play_url, cache_key=str(sid))
                except Exception:
                    pass

            # 尝试音乐卡片
            if not voice_sent and play_url and cover:
                try:
                    params = build_sign_params(
                        url=play_url, song=name, singer=artists,
                        cover=cover, fmt="163",
                    )
                    ark = sign_music_card(params, logger=logger)
                    if ark:
                        card_sent = await send_card_via_astrbot(event, ark, logger=logger)
                except Exception:
                    pass

            # 降级语音
            if not card_sent and not voice_sent and play_url:
                try:
                    voice_sent = await self._send_voice(event, play_url, cache_key=str(sid))
                except Exception:
                    pass

        if not card_sent and not voice_sent:
                try:
                    if cover:
                        full_cover = cover if "?param=" in cover else cover + "?param=500y500"
                        chain = [Plain(message_text + "\n"), Image.fromURL(full_cover)]
                        await event.send(event.chain_result(chain))
                    else:
                        await event.send(event.plain_result(message_text))
                except Exception:
                    await event.send(event.plain_result(message_text))

        return f"已点播「{name} - {artists}」"
