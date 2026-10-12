"""
QQ 音乐卡片签名模块
通过 oiapi.net 签名 API 生成 QQ 音乐共享卡片，支持各大音乐平台。

API 文档: https://oiapi.net/doc/id/78.html

本模块还包含语音消息发送能力：
  - send_voice_via_astrbot           直接把音频 URL 交给协议端下载（快但成功率依赖协议端网络）
  - send_voice_download_then_send    插件先下载到本地再发送（稳定，推荐）
                                     NapCat / LLOneBot 收到本地音频文件后会自动转 silk 格式。
"""

import os
import json
import time
import uuid
import asyncio
import logging
import tempfile
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Tuple

import requests

# 签名 API 地址
_SIGN_URL = "https://oiapi.net/api/QQMusicJSONArk/"
# 各平台 format 参数
_FMT_NETEASE = "163"
# HTTP 超时
_HTTP_TIMEOUT = 10

# ── 音频下载配置 ──
# 下载线程池（避免阻塞事件循环）
_DL_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="ncm_dl")
# 下载请求头（网易云防盗链检查 Referer）
_DL_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
# 单个音频最大下载体积（80MB，防御异常大文件）
_MAX_DOWNLOAD_BYTES = 80 * 1024 * 1024
# 下载超时 (连接超时, 读取超时) 秒
_DL_TIMEOUT = (10, 120)
# 语音缓存有效期（秒）：同一首歌在有效期内重复点播直接复用本地文件，无需重新下载
_CACHE_TTL_SECONDS = 1800
# 临时文件默认子目录（位于系统临时目录下）
_DEFAULT_TEMP_SUBDIR = "astrbot_ncm_voice"


def build_sign_params(
    url: str,
    song: str = "",
    singer: str = "",
    cover: str = "",
    jump: str = "",
    fmt: str = _FMT_NETEASE,
) -> dict:
    """构建签名请求参数"""
    return {
        "url": url,
        "format": fmt,
        "song": song,
        "singer": singer,
        "cover": cover,
        "jump": jump or url,
    }


def sign_music_card(params: dict, logger: Optional[logging.Logger] = None) -> Optional[Dict]:
    """
    调用 oiapi.net 签名 API，获取 QQ 音乐卡片数据。

    Args:
        params: 签名参数，见 build_sign_params()
        logger: 日志器

    Returns:
        {"data": ark_json_dict, "token": "xxx"}  成功
        None                                     失败
    """
    log = logger or logging.getLogger("music_card")
    try:
        resp = requests.get(_SIGN_URL, params=params, timeout=_HTTP_TIMEOUT)
        if resp.status_code != 200:
            log.warning(f"[卡片签名] HTTP {resp.status_code}")
            return None
        data = resp.json()
    except requests.Timeout:
        log.warning("[卡片签名] 请求超时")
        return None
    except requests.RequestException as e:
        log.warning(f"[卡片签名] 网络错误: {e}")
        return None
    except json.JSONDecodeError:
        log.warning("[卡片签名] 返回非 JSON")
        return None

    # code=1 表示签名成功
    if data.get("code") != 1 or not data.get("data"):
        log.warning(f"[卡片签名] 签名失败: code={data.get('code')} msg={data.get('message', '')}")
        return None

    ark_data = data["data"]
    token = ark_data.get("config", {}).get("token", "")
    ark_message = data.get("message", json.dumps(ark_data, ensure_ascii=False))  # 序列化字符串，给 SnowLuma 用
    log.info(f"[卡片签名] 成功 | token={token[:20] if token else '无'}...")
    return {"data": ark_data, "token": token, "message": ark_message}


async def send_card_via_astrbot(event, ark_info: Dict, logger: Optional[logging.Logger] = None) -> bool:
    """
    通过 AstrBot Json 组件 / OneBot 原生 API 发送音乐卡片。
    三层尝试，覆盖 NapCat / LLOneBot / SnowLuma 各版本。

    Args:
        event:    AstrMessageEvent 对象
        ark_info: sign_music_card() 的返回值 {"data": ..., "token": ..., "message": ...}
        logger:   日志器

    Returns:
        True  发送成功
        False 发送失败
    """
    log = logger or logging.getLogger("music_card")
    ark_str = json.dumps(ark_info["data"], ensure_ascii=False)

    # 方式1: NapCat 模式 — data 传 dict（AstrBot 发出 data.data 为对象）
    try:
        from astrbot.api.message_components import Json as JsonComponent
        json_card = JsonComponent(data=ark_info["data"], config={"token": ark_info["token"]})
        await event.send(event.chain_result([json_card]))
        log.info("[卡片发送] NapCat/LLOneBot 成功")
        return True
    except ImportError:
        pass
    except Exception:
        pass

    # 方式2: SnowLuma 标准格式 — data.data 为 JSON 字符串（所有 OneBot 协议端均接受）
    bot = getattr(event, "bot", None)
    if bot and hasattr(bot, "api"):
        try:
            msg = [{"type": "json", "data": {"data": ark_str}}]
            group_id = event.get_group_id() if hasattr(event, "get_group_id") else None
            if group_id:
                await bot.api.call_action("send_group_msg", group_id=group_id, message=msg)
            else:
                await bot.api.call_action("send_private_msg", user_id=event.get_sender_id(), message=msg)
            log.info("[卡片发送] OneBot(data.data字符串) 成功")
            return True
        except Exception:
            pass

    # 方式3: 裸 JSON 字符串简写（新版 SnowLuma 支持）
    if bot and hasattr(bot, "api"):
        try:
            msg = [{"type": "json", "data": ark_str}]
            group_id = event.get_group_id() if hasattr(event, "get_group_id") else None
            if group_id:
                await bot.api.call_action("send_group_msg", group_id=group_id, message=msg)
            else:
                await bot.api.call_action("send_private_msg", user_id=event.get_sender_id(), message=msg)
            log.info("[卡片发送] OneBot原生 成功")
            return True
        except Exception as e:
            log.warning(f"[卡片发送] OneBot原生 失败: {e}")

    return False


# ═══════════════════════════════════════
#  音频下载（带本地缓存复用）
# ═══════════════════════════════════════


def _default_temp_dir() -> str:
    """默认临时目录（系统临时目录下的 astrbot_ncm_voice 子目录）"""
    return os.path.join(tempfile.gettempdir(), _DEFAULT_TEMP_SUBDIR)


def _guess_ext(url: str, content_type: str = "") -> Optional[str]:
    """根据 URL 路径或 Content-Type 推断音频扩展名，无法确定返回 None"""
    path_part = urllib.parse.urlparse(url).path.lower()
    for ext in (".mp3", ".m4a", ".flac", ".wav", ".ogg", ".aac"):
        if path_part.endswith(ext):
            return ext
    ct = (content_type or "").lower()
    if "mpeg" in ct or "mp3" in ct:
        return ".mp3"
    if "mp4" in ct or "m4a" in ct or "aac" in ct:
        return ".m4a"
    if "flac" in ct:
        return ".flac"
    if "wav" in ct:
        return ".wav"
    if "ogg" in ct:
        return ".ogg"
    return None


def _get_cache_path(dest_dir: str, cache_key: str, ext: str) -> str:
    """按缓存键拼缓存文件路径（过滤非法字符，防路径穿越）"""
    safe_key = "".join(c for c in cache_key if c.isalnum() or c in "-_")[:64]
    return os.path.join(dest_dir, f"ncm_{safe_key}{ext}")


def _check_cache(fpath: str, ttl: int) -> Optional[Tuple[str, int]]:
    """
    检查缓存文件是否可用（存在、非空、未过期）。
    返回 (路径, 字节数) 或 None；过期/损坏的文件会被删除。
    """
    try:
        st = os.stat(fpath)
        if st.st_size <= 1024:
            raise OSError("缓存文件过小")
        if (time.time() - st.st_mtime) >= ttl:
            raise OSError("缓存已过期")
        return fpath, st.st_size
    except OSError:
        try:
            os.remove(fpath)
        except OSError:
            pass
        return None


def _download_audio_sync(
    url: str,
    dest_dir: str,
    cache_key: Optional[str] = None,
    ttl: int = _CACHE_TTL_SECONDS,
) -> Tuple[str, int, bool]:
    """
    同步下载音频文件到本地（线程池中执行）。

    传入 cache_key（如歌曲ID）时启用缓存复用：
      - 命中未过期缓存直接返回本地文件，不发起下载
      - 新文件先下载到临时名再原子 rename，避免并发读到半成品

    Returns:
        (文件路径, 字节数, 是否缓存命中)
    """
    os.makedirs(dest_dir, exist_ok=True)

    url_ext = _guess_ext(url)

    # 缓存查找（URL 能确定扩展名时直接查）
    if cache_key and url_ext:
        cached = _check_cache(_get_cache_path(dest_dir, cache_key, url_ext), ttl)
        if cached:
            return cached[0], cached[1], True

    resp = requests.get(url, headers=_DL_HEADERS, stream=True, timeout=_DL_TIMEOUT)
    try:
        resp.raise_for_status()

        # Content-Type 兜底推断扩展名
        ext = url_ext or _guess_ext(url, resp.headers.get("Content-Type", "")) or ".m4a"

        if cache_key:
            final_path = _get_cache_path(dest_dir, cache_key, ext)
            cached = _check_cache(final_path, ttl)
            if cached:
                return cached[0], cached[1], True
            # 先写临时文件，完成后原子 rename，避免并发重复下载时读到半成品
            tmp_path = final_path + f".dl_{uuid.uuid4().hex[:8]}"
            rename_to = final_path
        else:
            tmp_path = os.path.join(dest_dir, f"ncm_{uuid.uuid4().hex[:12]}{ext}")
            rename_to = None

        total = 0
        try:
            with open(tmp_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    total += len(chunk)
                    if total > _MAX_DOWNLOAD_BYTES:
                        raise IOError(f"音频超过大小限制 {_MAX_DOWNLOAD_BYTES // 1024 // 1024}MB，已中止")
            if total < 1024:
                raise IOError(f"下载内容过小（{total}B），可能不是有效音频")
            if rename_to:
                os.replace(tmp_path, rename_to)
                return rename_to, total, False
            return tmp_path, total, False
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    finally:
        resp.close()


async def download_audio(
    url: str,
    dest_dir: Optional[str] = None,
    cache_key: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> Tuple[str, int, bool]:
    """
    异步下载音频文件（内部转线程池，不阻塞事件循环）。

    Args:
        url:       音频直链
        dest_dir:  存放目录，None 则使用系统临时目录下的 astrbot_ncm_voice
        cache_key: 缓存键（如歌曲ID），传入后 30 分钟内重复点播同一首直接复用本地文件
        logger:    日志器

    Returns:
        (文件路径, 字节数, 是否缓存命中)
    """
    log = logger or logging.getLogger("music_card")
    if not dest_dir:
        dest_dir = _default_temp_dir()

    loop = asyncio.get_event_loop()
    fpath, size, from_cache = await loop.run_in_executor(
        _DL_EXECUTOR, _download_audio_sync, url, dest_dir, cache_key, _CACHE_TTL_SECONDS
    )
    if from_cache:
        log.info(f"[语音缓存] 命中 | {os.path.basename(fpath)} {size / 1024 / 1024:.1f}MB")
    else:
        log.info(f"[语音下载] 完成 | {os.path.basename(fpath)} {size / 1024 / 1024:.1f}MB")
    return fpath, size, from_cache


def cleanup_stale_tempfiles(
    dest_dir: Optional[str] = None,
    max_age: int = 3600,
    logger: Optional[logging.Logger] = None,
) -> int:
    """
    清理临时目录中过期的 ncm_* 缓存文件（插件启动时调用，回收上次异常退出的残留）。

    只删除 ncm_ 前缀的文件，不影响目录中的其他内容。

    Args:
        dest_dir: 临时目录，None 用默认目录
        max_age:  文件保留秒数，超过则删除
        logger:   日志器

    Returns:
        删除的文件数量
    """
    log = logger or logging.getLogger("music_card")
    if not dest_dir:
        dest_dir = _default_temp_dir()
    if not os.path.isdir(dest_dir):
        return 0

    now = time.time()
    removed = 0
    try:
        for fname in os.listdir(dest_dir):
            if not fname.startswith("ncm_"):
                continue
            fpath = os.path.join(dest_dir, fname)
            try:
                if now - os.stat(fpath).st_mtime > max_age:
                    os.remove(fpath)
                    removed += 1
            except OSError:
                pass
    except OSError as e:
        log.warning(f"[缓存清理] 扫描失败: {e}")

    if removed:
        log.info(f"[缓存清理] 已删除 {removed} 个过期缓存文件 | {dest_dir}")
    return removed


# ═══════════════════════════════════════
#  语音消息发送
# ═══════════════════════════════════════


def _to_file_uri(abs_path: str) -> str:
    """本地绝对路径转 OneBot file URI"""
    if os.name == "nt":
        return "file:///" + abs_path.replace("\\", "/")
    return "file://" + abs_path


async def send_local_voice_via_astrbot(
    event,
    file_path: str,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """
    发送本地音频文件为 QQ 语音消息。
    NapCat / LLOneBot 收到本地 mp3/m4a/flac 后会自动用 ffmpeg 转 silk。

    注意：file_path 必须是 NapCat 进程能够访问到的路径
    （与 NapCat 同一台机器 / 同一文件系统，Docker 需挂载共享目录）。

    Args:
        event:     AstrMessageEvent 对象
        file_path: 本地音频文件绝对路径
        logger:    日志器

    Returns:
        True  发送成功
        False 发送失败
    """
    log = logger or logging.getLogger("music_card")
    if not file_path or not os.path.exists(file_path):
        log.warning(f"[语音发送] 文件不存在: {file_path}")
        return False

    abs_path = os.path.abspath(file_path)
    file_uri = _to_file_uri(abs_path)

    # 方式1: OneBot 原生 record 消息（本地文件 URI）
    bot = getattr(event, "bot", None)
    if bot and hasattr(bot, "api"):
        record_msg = [{"type": "record", "data": {"file": file_uri}}]
        try:
            group_id = event.get_group_id() if hasattr(event, "get_group_id") else None
            if group_id:
                await bot.api.call_action("send_group_msg",
                    group_id=group_id, message=record_msg)
            else:
                await bot.api.call_action("send_private_msg",
                    user_id=event.get_sender_id(), message=record_msg)
            log.info(f"[语音发送] 本地文件成功 | {os.path.basename(abs_path)} "
                     f"({os.path.getsize(abs_path) // 1024}KB)")
            return True
        except Exception as e:
            log.warning(f"[语音发送] 本地文件失败: {e}")

    # 方式2: AstrBot Record 组件
    try:
        from astrbot.api.message_components import Record as RecordComp
        await event.send(event.chain_result([RecordComp.fromFileSystem(abs_path)]))
        log.info("[语音发送] Record(fromFileSystem) 组件成功")
        return True
    except Exception as e:
        log.warning(f"[语音发送] Record 组件失败: {e}")

    return False


async def send_voice_via_astrbot(event, audio_url: str, logger: Optional[logging.Logger] = None) -> bool:
    """
    通过 AstrBot 发送语音消息（URL 直发模式）。

    优先使用 OneBot 原生 record 消息，失败则用 AstrBot Record 组件。
    音频文件由协议端（NapCat）自行下载。

    Args:
        event:     AstrMessageEvent 对象
        audio_url: 音频文件直链 URL
        logger:    日志器

    Returns:
        True  发送成功
        False 发送失败
    """
    log = logger or logging.getLogger("music_card")
    if not audio_url:
        return False

    # 方式1: OneBot 原生 record 消息
    bot = getattr(event, "bot", None)
    if bot and hasattr(bot, "api"):
        record_msg = [{"type": "record", "data": {"file": audio_url}}]
        try:
            from astrbot.api.event.filter import EventMessageType
            group_id = event.get_group_id() if hasattr(event, "get_group_id") else None
            if group_id:
                await bot.api.call_action("send_group_msg",
                    group_id=group_id, message=record_msg)
            else:
                await bot.api.call_action("send_private_msg",
                    user_id=event.get_sender_id(), message=record_msg)
            log.info("[语音发送] OneBot 成功")
            return True
        except Exception as e:
            log.warning(f"[语音发送] OneBot 失败: {e}")

    # 方式2: AstrBot Record 组件
    try:
        from astrbot.api.message_components import Record as RecordComp
        await event.send(event.chain_result([RecordComp.fromURL(audio_url)]))
        log.info("[语音发送] Record 组件成功")
        return True
    except Exception as e:
        log.warning(f"[语音发送] Record 组件失败: {e}")

    return False


async def send_voice_download_then_send(
    event,
    audio_url: str,
    temp_dir: Optional[str] = None,
    cache_key: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """
    下载到本地后再发送语音消息（推荐模式，比 URL 直发稳定得多）。

    流程：下载音频（带缓存复用）→ 以本地文件发送 record。
    缓存文件保留在临时目录中供 30 分钟内的重复点播复用，
    由插件启动清理和 TTL 过期回收，不再发送后立即删除。
    任一步失败返回 False，由调用方决定降级策略。

    Args:
        event:     AstrMessageEvent 对象
        audio_url: 音频文件直链 URL
        temp_dir:  临时目录（必须对 NapCat 可见），None 用系统临时目录
        cache_key: 缓存键（建议传歌曲ID），传入后重复点播直接复用本地文件
        logger:    日志器

    Returns:
        True  发送成功
        False 发送失败
    """
    log = logger or logging.getLogger("music_card")
    if not audio_url:
        return False

    # 1. 下载（带缓存）
    try:
        fpath, size, from_cache = await download_audio(
            audio_url, dest_dir=temp_dir, cache_key=cache_key, logger=log
        )
    except Exception as e:
        log.warning(f"[语音下载] 失败: {type(e).__name__}: {e}")
        return False

    # 2. 发送本地文件
    ok = await send_local_voice_via_astrbot(event, fpath, logger=log)

    # 3. 发送失败时，本次新下载的文件直接删掉，避免坏文件占用缓存
    if not ok and not from_cache:
        try:
            os.remove(fpath)
        except OSError:
            pass

    return ok
