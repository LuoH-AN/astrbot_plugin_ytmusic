import asyncio
import os
import re
import tempfile
from typing import Callable, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Record, Image

# 这些主机名(以及它们的子域)会被改写到反代,仅用于元信息与封面拉取。
# 音频文件不再由本插件下载,所以也不会经过反代。
# 必须与 worker.js 里 PATH_PROXY_HOSTS 的 allowlist 保持一致。
_PROXY_TARGETS = ("youtube.com", "googlevideo.com", "ytimg.com", "youtu.be")


def _make_url_rewriter(proxy_base: str) -> Callable[[str], Optional[str]]:
    """返回一个函数:原 URL -> 反代 URL(若不在反代范围内返回 None)。
    路径式反代:https://host/path?q -> <proxy_base>/host/path?q
    """
    if not proxy_base:
        return lambda _u: None

    from urllib.parse import urlparse, urlunparse

    base = proxy_base.strip()
    if "://" not in base:
        base = "https://" + base
    base = base.rstrip("/")
    parsed_base = urlparse(base)
    base_scheme = parsed_base.scheme or "https"
    base_netloc = parsed_base.netloc
    base_path = parsed_base.path  # 可能为空

    def rewrite(url: str) -> Optional[str]:
        if not url:
            return None
        u = urlparse(url)
        host = (u.hostname or "").lower()
        if not host:
            return None
        if not any(host == t or host.endswith("." + t) for t in _PROXY_TARGETS):
            return None
        new_path = base_path + "/" + host + (u.path or "/")
        new_path = re.sub(r"/+", "/", new_path)
        return urlunparse((base_scheme, base_netloc, new_path, u.params, u.query, u.fragment))

    return rewrite


@register(
    "astrbot_plugin_ytmusic",
    "LuoH-AN",
    "通过 YouTube Music 点歌的插件,使用 `点歌 歌名` 触发。音频下载下放到本地 API。",
    "2.0.0",
)
class YTMusicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.proxy_base: str = (self.config.get("proxy_base") or "").strip().rstrip("/")
        self.proxy: Optional[str] = self.config.get("proxy") or os.environ.get("HTTPS_PROXY")
        self.send_card: bool = bool(self.config.get("send_card", True))
        self.send_audio: bool = bool(self.config.get("send_audio", True))
        self.max_duration: int = int(self.config.get("max_duration", 600))
        self.download_api_base: str = (self.config.get("download_api_base") or "").strip().rstrip("/")
        self.download_api_key: str = (self.config.get("download_api_key") or "").strip()
        self.download_api_timeout: int = int(self.config.get("download_api_timeout", 300))

        self._rewrite_url = _make_url_rewriter(self.proxy_base)
        self.ytm = self._init_ytmusic()

    def _init_ytmusic(self):
        try:
            from ytmusicapi import YTMusic
        except ImportError:
            logger.error("[ytmusic] 未安装 ytmusicapi,请执行: pip install ytmusicapi")
            return None
        try:
            import requests

            session = requests.Session()
            if self.proxy_base:
                _patch_requests_session(session, self._rewrite_url, self.proxy_base)
                logger.info(f"[ytmusic] ytmusicapi 走反代: {self.proxy_base}")
            elif self.proxy:
                session.proxies.update({"http": self.proxy, "https": self.proxy})
            return YTMusic(requests_session=session)
        except Exception as e:
            logger.error(f"[ytmusic] 初始化 YTMusic 失败: {e}")
            return None

    @filter.command("点歌")
    async def order_song(self, event: AstrMessageEvent, song_name: str = ""):
        keyword = (song_name or "").strip()
        if not keyword:
            msg_str = (event.message_str or "").strip()
            keyword = re.sub(r"^/?点歌\s*", "", msg_str).strip()

        if not keyword:
            yield event.plain_result("请在「点歌」后输入歌曲名,例如: 点歌 晴天")
            return

        if self.ytm is None:
            self.ytm = self._init_ytmusic()
        if self.ytm is None:
            yield event.plain_result("未安装 ytmusicapi,无法点歌。请执行 pip install ytmusicapi")
            return

        yield event.plain_result(f"正在为你搜索:{keyword} ...")

        try:
            track = await asyncio.to_thread(self._search_song, keyword)
        except Exception as e:
            logger.exception("[ytmusic] 搜索失败")
            yield event.plain_result(f"搜索失败:{e}")
            return

        if not track:
            yield event.plain_result(f"未找到与「{keyword}」相关的歌曲。")
            return

        video_id = track["videoId"]
        title = track.get("title", "Unknown")
        artists = ", ".join(a.get("name", "") for a in track.get("artists", []) if a.get("name"))
        duration = track.get("duration_seconds") or 0
        thumbs = track.get("thumbnails") or []
        thumbnail = thumbs[-1].get("url", "") if thumbs else ""
        play_url = f"https://music.youtube.com/watch?v={video_id}"

        info_text = (
            f"找到歌曲:\n"
            f"标题:{title}\n"
            f"歌手:{artists or '未知'}\n"
            f"时长:{self._fmt_duration(duration)}\n"
            f"链接:{play_url}"
        )

        # 缩略图也走反代,否则下游 image 渲染可能拉不到 i.ytimg.com
        chain = []
        if thumbnail:
            chain.append(Image.fromURL(self._maybe_rewrite(thumbnail)))
        chain.append(Plain(info_text))
        yield event.chain_result(chain)

        if self.send_card:
            await self._try_send_qq_music_card(event, video_id, title, artists, thumbnail)

        if duration and duration > self.max_duration:
            yield event.plain_result(
                f"歌曲时长超过 {self.max_duration} 秒,跳过音频下载发送。"
            )
            return

        if not self.send_audio:
            return

        if not self.download_api_base:
            yield event.plain_result(
                "未配置 download_api_base,无法获取音频。请在插件配置里填入本地下载 API 地址(例如 http://127.0.0.1:3000)。"
            )
            return

        try:
            audio_path = await asyncio.to_thread(self._download_audio_via_api, video_id)
        except Exception as e:
            logger.warning(f"[ytmusic] 调用下载 API 失败: {e}")
            yield event.plain_result(f"音频下载失败:{e}")
            return

        if audio_path and os.path.exists(audio_path):
            yield event.chain_result([Record(file=audio_path)])
        else:
            yield event.plain_result("音频文件下载失败,可能是网络/版权问题,或下载 API 不可达。")

    def _maybe_rewrite(self, url: str) -> str:
        if not self.proxy_base:
            return url
        new_url = self._rewrite_url(url)
        return new_url or url

    def _search_song(self, keyword: str) -> Optional[dict]:
        results = self.ytm.search(keyword, filter="songs", limit=5)
        if not results:
            results = self.ytm.search(keyword, filter="videos", limit=5)
        if not results:
            return None
        for r in results:
            if r.get("videoId"):
                dur = r.get("duration")
                if dur and isinstance(dur, str):
                    r["duration_seconds"] = self._parse_duration(dur)
                return r
        return None

    def _download_audio_via_api(self, video_id: str) -> Optional[str]:
        """调用外部下载 API,把返回的音频流写到临时文件并返回路径。"""
        import requests
        from urllib.parse import quote

        api_url = f"{self.download_api_base}/download/{quote(video_id, safe='')}"
        headers = {}
        if self.download_api_key:
            headers["X-API-Key"] = self.download_api_key

        try:
            resp = requests.get(
                api_url,
                headers=headers,
                timeout=self.download_api_timeout,
                stream=True,
            )
        except Exception as e:
            logger.warning(f"[ytmusic] 连接下载 API 失败: {api_url} -> {e}")
            return None

        if resp.status_code != 200:
            body = ""
            try:
                body = resp.text[:200]
            except Exception:
                pass
            logger.warning(f"[ytmusic] 下载 API 返回 {resp.status_code}: {body}")
            return None

        ext = "mp3"
        cd = resp.headers.get("content-disposition", "")
        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', cd, re.IGNORECASE)
        if m:
            ext_match = re.search(r"\.([a-zA-Z0-9]+)$", m.group(1))
            if ext_match:
                ext = ext_match.group(1).lower()

        out_path = os.path.join(tempfile.gettempdir(), f"ytm_{video_id}.{ext}")
        try:
            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=64 * 1024):
                    if chunk:
                        f.write(chunk)
        except Exception as e:
            logger.warning(f"[ytmusic] 写入下载文件失败: {e}")
            return None
        return out_path

    async def _try_send_qq_music_card(
        self,
        event: AstrMessageEvent,
        video_id: str,
        title: str,
        artists: str,
        thumbnail: str,
    ) -> None:
        platform = (event.get_platform_name() or "").lower()
        if "aiocqhttp" not in platform:
            return
        try:
            from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
                AiocqhttpMessageEvent,
            )
        except Exception:
            return
        if not isinstance(event, AiocqhttpMessageEvent):
            return

        client = event.bot
        play_url = f"https://music.youtube.com/watch?v={video_id}"
        payload = [
            {
                "type": "music",
                "data": {
                    "type": "custom",
                    "url": play_url,
                    "audio": play_url,
                    "title": title or "YouTube Music",
                    "content": artists or "",
                    "image": thumbnail or "",
                },
            }
        ]
        try:
            if event.get_group_id():
                await client.send_group_msg(
                    group_id=int(event.get_group_id()), message=payload
                )
            else:
                await client.send_private_msg(
                    user_id=int(event.get_sender_id()), message=payload
                )
        except Exception as e:
            logger.debug(f"[ytmusic] QQ 自定义音乐卡片发送失败:{e}")

    @staticmethod
    def _parse_duration(s: str) -> int:
        parts = s.strip().split(":")
        try:
            nums = [int(p) for p in parts]
        except ValueError:
            return 0
        total = 0
        for n in nums:
            total = total * 60 + n
        return total

    @staticmethod
    def _fmt_duration(sec: int) -> str:
        if not sec:
            return "未知"
        m, s = divmod(int(sec), 60)
        h, m = divmod(m, 60)
        if h:
            return f"{h:02d}:{m:02d}:{s:02d}"
        return f"{m:02d}:{s:02d}"

    async def terminate(self):
        logger.info("[ytmusic] 插件已卸载")


def _patch_requests_session(session, rewriter: Callable[[str], Optional[str]], proxy_base: str) -> None:
    """让 requests.Session 在发包前把命中的 URL 改写到反代路径式。"""
    from urllib.parse import urlparse

    proxy_host = urlparse(proxy_base if "://" in proxy_base else "https://" + proxy_base).hostname
    original_send = session.send

    def send(request, **kwargs):
        new_url = rewriter(request.url)
        if new_url:
            request.url = new_url
            # Host 头换成反代主机,否则 TLS SNI / 虚拟主机匹配会错
            if proxy_host:
                request.headers["Host"] = proxy_host
        return original_send(request, **kwargs)

    session.send = send
