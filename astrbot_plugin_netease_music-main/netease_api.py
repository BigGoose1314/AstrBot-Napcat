"""
网易云音乐官方 API 交互模块
直连网易云官方 API，无需部署任何中间服务。

- 搜索：通过公共 HTTP API 直连（https://music.163.com/api/search/get），无需加密
- 播放链接/详情：通过 NeteaseCloudMusic SDK（内部处理 weapi 加密参数），支持 Cookie 鉴权

依赖：pip install NeteaseCloudMusic requests
"""

import time
import json
import logging
import asyncio
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List

import requests
from NeteaseCloudMusic import NeteaseCloudMusicApi


# Cookie 脱敏保留前后字符数
_COOKIE_MASK_KEEP = 8

# 线程池（SDK 和 requests 都是同步的）
_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ncm")

# HTTP 请求头（模拟浏览器）
_HTTP_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://music.163.com/",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
}


def mask_cookie(cookie_text: str) -> str:
    """Cookie 脱敏"""
    if not cookie_text:
        return "(无)"
    if len(cookie_text) <= _COOKIE_MASK_KEEP * 2:
        return cookie_text[:4] + "***" + cookie_text[-4:] if len(cookie_text) > 8 else "***"
    return cookie_text[:_COOKIE_MASK_KEEP] + " ***MASKED*** " + cookie_text[-_COOKIE_MASK_KEEP:]


class NeteaseCloudMusicAPI:
    """
    网易云音乐官方 API 封装（纯 Python，零外部服务）

    搜索使用公共 HTTP API（稳定，不需要加密），
    歌曲 URL 和详情使用 NeteaseCloudMusic SDK（支持 Cookie / weapi 加密）。
    """

    TAG = "[网易云API]"

    # 公共搜索 API 地址
    SEARCH_API_URL = "https://music.163.com/api/search/get"

    def __init__(self, cookie: str = "", bitrate: int = 128000,
                 debug: bool = False, logger: Optional[logging.Logger] = None,
                 device_ua: str = ""):
        self.cookie = cookie.strip()
        self.bitrate = bitrate
        self.debug = debug
        self.device_ua = device_ua.strip()
        self._logger = logger or logging.getLogger("netease_api")

        # 初始化 SDK（用于需要加密的 API）
        self._sdk = NeteaseCloudMusicApi()
        if self.cookie:
            try:
                self._sdk.cookie = self.cookie
                self._log_info(f"Cookie 已加载 (长度={len(self.cookie)})")
            except Exception as e:
                self._log_warning(f"Cookie 设置失败: {e}，将通过请求参数传递 Cookie")
        else:
            self._log_info("未配置 Cookie，将以匿名模式运行")

        # HTTP session（用于公共 API）
        self._http = requests.Session()
        if self.device_ua:
            try:
                dev_info = json.loads(self.device_ua)
                custom_ua = dev_info.get("ua", "")
                if custom_ua:
                    self._http.headers["User-Agent"] = custom_ua
                    self._log_info(f"已应用设备 UA: {custom_ua[:60]}...")
            except (json.JSONDecodeError, KeyError):
                self._http.headers["User-Agent"] = self.device_ua
                self._log_info(f"已应用自定义 UA: {self.device_ua[:60]}...")
        else:
            self._http.headers.update(_HTTP_HEADERS)
        if self.cookie:
            # 直接把 cookie 也设置到 HTTP session
            for item in self.cookie.split(";"):
                item = item.strip()
                if "=" in item:
                    k, v = item.split("=", 1)
                    self._http.cookies.set(k.strip(), v.strip())

    @property
    def has_vip_cookie(self) -> bool:
        return len(self.cookie) > 20

    # ── 日志 ──

    def _log_debug(self, msg: str):
        if self.debug:
            self._logger.debug(f"{self.TAG} {msg}")

    def _log_info(self, msg: str):
        self._logger.info(f"{self.TAG} {msg}")

    def _log_error(self, msg: str):
        self._logger.error(f"{self.TAG} {msg}")

    def _log_warning(self, msg: str):
        self._logger.warning(f"{self.TAG} {msg}")

    # ── 搜索（直连公共 HTTP API，稳定无加密）──

    def _search_sync(self, keywords: str, limit: int, offset: int) -> Dict[str, Any]:
        """同步搜索 — 使用公共 HTTP API"""
        t0 = time.time()
        params = {"s": keywords, "type": 1, "limit": limit, "offset": offset}
        self._log_debug(f"[搜索] url={self.SEARCH_API_URL} params={params}")

        try:
            resp = self._http.get(self.SEARCH_API_URL, params=params, timeout=10)
            elapsed = time.time() - t0
            raw = resp.json()
            self._log_debug(
                f"[搜索响应] HTTP={resp.status_code} 耗时={elapsed:.3f}s\n"
                f"{json.dumps(raw, ensure_ascii=False)[:2000]}"
            )

            code = raw.get("code", -1)
            result = raw.get("result", {})
            if not isinstance(result, dict):
                self._log_warning(f"搜索返回异常 result 类型: {type(result).__name__}")
                return {"code": code, "result": {}, "message": "API返回格式异常"}

            song_count = len(result.get("songs", []))
            self._log_debug(f"[搜索结果] code={code} 歌曲数={song_count}")
            return {"code": 200 if code == 200 else code, "result": result}

        except requests.Timeout:
            self._log_error(f"[搜索超时] {keywords}")
            return {"code": -1, "result": {}, "message": "请求超时"}
        except requests.RequestException as e:
            self._log_error(f"[搜索网络错误] {e}")
            return {"code": -1, "result": {}, "message": f"网络错误: {e}"}
        except Exception as e:
            self._log_error(f"[搜索异常] {type(e).__name__}: {e}")
            return {"code": -1, "result": {}, "message": str(e)}

    # ── 歌曲 URL（通过 SDK，支持 weapi 加密 + Cookie）──

    def _get_song_url_sync(self, song_id: int) -> Dict[str, Any]:
        """同步获取歌曲 URL — 通过 SDK（Cookie 直接传参，绕过SDK校验器）"""
        t0 = time.time()
        self._log_debug(f"[获取URL] song_id={song_id} bitrate={self.bitrate} cookie={'有' if self.cookie else '无'}")

        query = {
            "id": song_id,
            "level": "standard",
            "timestamp": int(time.time() * 1000),
        }
        # 直接传 cookie 参数，不依赖 SDK 的 _sdk.cookie（避开严格校验）
        if self.cookie:
            query["cookie"] = self.cookie

        try:
            raw = self._sdk.request("/song/url/v1", query)
            elapsed = time.time() - t0
            self._log_debug(
                f"[URL响应] 耗时={elapsed:.3f}s\n"
                f"{json.dumps(raw, ensure_ascii=False)[:2000]}"
            )

            code = raw.get("code", -1)
            # song/url/v1: {"code":200, "data":{"code":200, "data":[...]}}
            outer = raw.get("data", {})
            inner = (outer or {}).get("data", []) if isinstance(outer, dict) else []
            return {"code": 200 if code == 200 else code, "data": inner if isinstance(inner, list) else []}

        except Exception as e:
            elapsed = time.time() - t0
            self._log_error(f"[URL异常] {type(e).__name__}: {e}")
            return {"code": -1, "data": [], "message": str(e)}

    # ── 歌曲详情（通过 SDK）──

    def _get_song_detail_sync(self, song_ids: List[int]) -> Dict[str, Any]:
        """同步获取歌曲详情 — 通过 SDK（Cookie 直接传参）"""
        ids_str = ",".join(str(sid) for sid in song_ids)
        self._log_debug(f"[详情] ids={ids_str}")

        query = {"ids": ids_str}
        if self.cookie:
            query["cookie"] = self.cookie

        try:
            raw = self._sdk.request("/song/detail", query)
            code = raw.get("code", -1)
            data = raw.get("data", {})
            songs = (data or {}).get("songs", []) if isinstance(data, dict) else []
            self._log_debug(f"[详情结果] code={code} 歌曲数={len(songs)}")
            return {"code": 200 if code == 200 else code, "songs": songs if isinstance(songs, list) else []}

        except Exception as e:
            self._log_error(f"[详情异常] {type(e).__name__}: {e}")
            return {"code": -1, "songs": [], "message": str(e)}

    # ── 异步封装 ──

    async def search(self, keywords: str, limit: int = 10, offset: int = 0) -> Dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(
            _EXECUTOR, self._search_sync, keywords, limit, offset
        )

    async def get_song_url(self, song_id: int, level: str = "standard") -> Dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(
            _EXECUTOR, self._get_song_url_sync, song_id
        )

    async def get_song_detail(self, song_ids: List[int]) -> Dict[str, Any]:
        return await asyncio.get_event_loop().run_in_executor(
            _EXECUTOR, self._get_song_detail_sync, song_ids
        )

    async def close(self):
        try:
            self._http.close()
        except Exception:
            pass


# ═══════════════════════════════════════
#  响应解析工具函数
# ═══════════════════════════════════════


def parse_search_result(api_response: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    从搜索 API 响应提取歌曲列表
    统一格式: {"code": 200, "result": {"songs": [...]}}
    """
    if api_response.get("code") != 200:
        return []

    songs = api_response.get("result", {}).get("songs", [])
    result = []
    for song in songs:
        # 歌手字段兼容 ar / artists 两种命名
        artists_list = song.get("ar") or song.get("artists") or []
        artists = "/".join(
            a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")
        )
        album_info = song.get("al") or song.get("album") or {}
        result.append({
            "id": song.get("id"),
            "name": song.get("name", "未知"),
            "artists": artists or "未知歌手",
            "album": album_info.get("name", "未知专辑"),
            "cover": (album_info.get("picUrl") or "") + "?param=300y300",
            "duration": song.get("dt") or song.get("duration", 0),
        })
    return result


def parse_song_url_result(api_response: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    从歌曲 URL API 响应提取播放信息
    统一格式: {"code": 200, "data": [{"id":..., "url":"...", ...}]}
    """
    if api_response.get("code") != 200:
        return None

    data = api_response.get("data", [])
    if not data:
        return None

    item = data[0]
    url = item.get("url", "")
    if not url:
        return None

    free_trial = item.get("freeTrialInfo")
    return {
        "url": url,
        "br": item.get("br", 0),
        "type": item.get("type", "mp3"),
        "size": item.get("size", 0),
        "is_free_trial": free_trial is not None and bool(free_trial),
    }
