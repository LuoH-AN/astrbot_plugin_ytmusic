import asyncio
import os
import re
import tempfile
from typing import Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.api.message_components import Plain, Record, Image

try:
    from ytmusicapi import YTMusic
except ImportError:
    YTMusic = None

try:
    import yt_dlp
except ImportError:
    yt_dlp = None


@register(
    "astrbot_plugin_ytmusic",
    "AstrBot User",
    "通过 YouTube Music 点歌的插件,使用 `点歌 歌名` 触发",
    "1.0.0",
)
class YTMusicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.proxy: Optional[str] = self.config.get("proxy") or os.environ.get("HTTPS_PROXY")
        self.send_card: bool = bool(self.config.get("send_card", True))
        self.send_audio: bool = bool(self.config.get("send_audio", True))
        self.max_duration: int = int(self.config.get("max_duration", 600))

        self.ytm = self._init_ytmusic()

        if yt_dlp is None:
            logger.warning("[ytmusic] 未安装 yt-dlp,无法下载音频。pip install yt-dlp")

    def _init_ytmusic(self):
        if YTMusic is None:
            logger.error("[ytmusic] 未安装 ytmusicapi,请执行: pip install ytmusicapi")
            return None
        try:
            if self.proxy:
                import requests
                session = requests.Session()
                session.proxies.update({"http": self.proxy, "https": self.proxy})
                return YTMusic(requests_session=session)
            return YTMusic()
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

        chain = []
        if thumbnail:
            chain.append(Image.fromURL(thumbnail))
        chain.append(Plain(info_text))
        yield event.chain_result(chain)

        if self.send_card:
            await self._try_send_qq_music_card(event, video_id, title, artists, thumbnail)

        if duration and duration > self.max_duration:
            yield event.plain_result(
                f"歌曲时长超过 {self.max_duration} 秒,跳过音频下载发送。"
            )
            return

        if self.send_audio and yt_dlp is not None:
            try:
                audio_path = await asyncio.to_thread(self._download_audio, video_id)
                if audio_path and os.path.exists(audio_path):
                    yield event.chain_result([Record(file=audio_path)])
                else:
                    yield event.plain_result("音频文件下载失败,可能是网络/版权问题。")
            except Exception as e:
                logger.exception("[ytmusic] 下载音频失败")
                yield event.plain_result(f"音频下载失败:{e}")

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
        if yt_dlp is None:
            return None
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
        if self.proxy:
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
