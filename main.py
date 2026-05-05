import asyncio
import os
import re
import tempfile
from typing import Callable, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Record, Image

# 这些主机名(以及它们的子域)会被改写到反代。
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
        # 把原 host 作为 proxy_base 的首段路径
        new_path = base_path + "/" + host + (u.path or "/")
        # 避免 // 连在一起
        new_path = re.sub(r"/+", "/", new_path)
        return urlunparse((base_scheme, base_netloc, new_path, u.params, u.query, u.fragment))

    return rewrite


@register(
    "astrbot_plugin_ytmusic",
    "LuoH-AN",
    "通过 YouTube Music 点歌的插件,使用 `点歌 歌名` 触发",
    "1.3.0",
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

        if self.send_audio:
            try:
                audio_path = await asyncio.to_thread(self._download_audio, video_id)
                if audio_path and os.path.exists(audio_path):
                    yield event.chain_result([Record(file=audio_path)])
                else:
                    yield event.plain_result("音频文件下载失败,可能是网络/版权问题。")
            except Exception as e:
                logger.warning(f"[ytmusic] 下载音频失败: {e}")
                yield event.plain_result(f"音频下载失败:{e}")

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

    def _download_audio(self, video_id: str) -> Optional[str]:
        try:
            import yt_dlp
        except ImportError:
            logger.error("[ytmusic] 未安装 yt-dlp,请执行: pip install yt-dlp")
            return None

        if self.proxy_base:
            _install_yt_dlp_patch(self._rewrite_url)

        tmp_dir = tempfile.gettempdir()
        outtmpl = os.path.join(tmp_dir, f"ytm_{video_id}.%(ext)s")
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": outtmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "192",
                }
            ],
        }
        if not self.proxy_base and self.proxy:
            ydl_opts["proxy"] = self.proxy

        url = f"https://music.youtube.com/watch?v={video_id}"
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        for ext in ("mp3", "m4a", "webm", "opus", "ogg"):
            path = os.path.join(tmp_dir, f"ytm_{video_id}.{ext}")
            if os.path.exists(path):
                return path
        return None

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


def _install_yt_dlp_patch(rewriter: Callable[[str], Optional[str]]) -> None:
    """对 yt_dlp 的 RequestDirector.send 打补丁,把出站 URL 全部走反代。幂等。"""
    try:
        from yt_dlp.networking.common import RequestDirector
    except Exception as e:
        logger.warning(f"[ytmusic] 无法 patch yt_dlp(可能版本过旧): {e}")
        return

    if getattr(RequestDirector.send, "_ytm_proxy_patched", False):
        RequestDirector._ytm_rewriter = rewriter
        return

    original_send = RequestDirector.send
    RequestDirector._ytm_rewriter = rewriter

    def patched_send(self, request):
        rw = getattr(RequestDirector, "_ytm_rewriter", None)
        if rw is not None:
            new_url = rw(request.url)
            if new_url:
                request.url = new_url
                # yt_dlp 的 Request 对象从 url 推导 Host,不需要手动改 header
        return original_send(self, request)

    patched_send._ytm_proxy_patched = True
    RequestDirector.send = patched_send
    logger.info("[ytmusic] 已为 yt_dlp 安装反代 URL 重写补丁")
