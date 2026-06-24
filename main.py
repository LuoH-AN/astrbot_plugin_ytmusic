import asyncio
import os
import random
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register

from qqmusic_api import Client, Credential
from qqmusic_api.modules.search import SearchType
from qqmusic_api.modules.song import SongFileInfo, SongFileType


API_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 180
FFMPEG_BOOTSTRAP_TIMEOUT = 300
MAX_AUDIO_DOWNLOAD_BYTES = 80 * 1024 * 1024
MAX_VOICE_BYTES = 15 * 1024 * 1024
VOICE_SAMPLE_RATE = 24000
VOICE_BITRATE = "64k"
ORDER_SONG_PATTERN = r"^(?:@\S+\s+|\S+\s+)?/?点歌(?:\s+.*)?$"

# 音质逐级降级列表, 从高品质向低品质尝试, 取第一个有可用链接的音质.
SONG_FILE_TYPES: list[SongFileType] = [
    SongFileType.MP3_320,
    SongFileType.MP3_128,
    SongFileType.OGG_96,
]


@register(
    "astrbot_plugin_ytmusic",
    "LuoH-AN",
    "通过 QQ 音乐点歌的插件,使用 `点歌 歌名` 触发。",
    "4.0.0",
)
class QQMusicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.send_card: bool = bool(self.config.get("send_card", True))
        self.send_audio: bool = bool(self.config.get("send_audio", True))
        self.max_duration: int = int(self.config.get("max_duration", 600))
        self.musicid: str = str(self.config.get("musicid", "")).strip()
        self.musickey: str = str(self.config.get("musickey", "")).strip()
        self._ffmpeg_ready = False

    @property
    def has_credential(self) -> bool:
        """是否配置了可用的 QQ 音乐凭据."""
        return bool(self.musicid and self.musickey)

    def _build_credential(self) -> Credential:
        """根据配置项构造 QQ 音乐凭据."""
        return Credential(
            musicid=int(self.musicid) if self.musicid.isdigit() else 0,
            str_musicid=self.musicid,
            musickey=self.musickey,
        )

    @filter.regex(ORDER_SONG_PATTERN)
    async def order_song(self, event: AstrMessageEvent):
        keyword = self._extract_order_keyword(event)

        if not keyword:
            yield event.plain_result("请在「点歌」后输入歌曲名,例如: 点歌 晴天")
            return

        yield event.plain_result(f"正在为你搜索:{keyword} ...")

        try:
            track = await self._search_song(keyword)
        except Exception as e:
            logger.exception("[qqmusic] 搜索失败")
            yield event.plain_result(f"搜索失败:{e}")
            return

        if not track:
            yield event.plain_result(f"未找到与「{keyword}」相关的歌曲。")
            return

        song_id = track["id"]
        song_mid = track["mid"]
        title = track.get("title") or "Unknown"
        artists = track.get("artists") or "未知"
        album = track.get("album") or "未知"
        duration = int(track.get("duration_seconds") or 0)
        thumbnail = track.get("thumbnail") or ""
        play_url = f"https://y.qq.com/n/ryqq/songDetail/{song_mid}"

        info_text = (
            f"找到歌曲:\n"
            f"标题:{title}\n"
            f"歌手:{artists}\n"
            f"专辑:{album}\n"
            f"时长:{self._fmt_duration(duration)}\n"
            f"链接:{play_url}"
        )

        chain = []
        if thumbnail:
            chain.append(Image.fromURL(thumbnail))
        chain.append(Plain(info_text))
        yield event.chain_result(chain)

        audio_info: dict[str, Any] = {}
        try:
            audio_info = await self._get_audio_url(song_mid)
        except Exception as e:
            logger.warning(f"[qqmusic] 获取播放链接失败:{e}")

        audio_url = audio_info.get("url") or ""
        if self.send_card:
            await self._try_send_qq_music_card(
                event,
                song_mid,
                title,
                artists,
                thumbnail,
                audio_url,
            )

        if duration and duration > self.max_duration:
            yield event.plain_result(
                f"歌曲时长超过 {self.max_duration} 秒,跳过音频下载发送。"
            )
            return

        if not self.send_audio:
            return

        if not audio_url:
            yield event.plain_result("未拿到可播放音频链接,可能需要登录 QQ 音乐会员或歌曲无版权。")
            return

        try:
            audio_path = await asyncio.to_thread(
                self._download_audio,
                song_mid,
                audio_url,
                audio_info.get("ext", ""),
            )
        except Exception as e:
            logger.warning(f"[qqmusic] 音频下载失败:{e}")
            yield event.plain_result(f"音频下载失败:{e}\n播放链接:{audio_url}")
            return

        if audio_path and os.path.exists(audio_path):
            if not self._has_ffmpeg():
                yield event.plain_result("检测到 ffmpeg 未安装,正在自动准备语音处理组件...")
            try:
                await asyncio.to_thread(self._ensure_ffmpeg)
            except Exception as e:
                logger.warning(f"[qqmusic] 自动准备 ffmpeg 失败:{e}")
                yield event.plain_result(
                    f"音频已下载,但自动准备 ffmpeg 失败:{e}\n播放链接:{audio_url}"
                )
                return
            try:
                audio_path = await asyncio.to_thread(
                    self._prepare_voice_file,
                    audio_path,
                    song_mid,
                )
            except Exception as e:
                logger.warning(f"[qqmusic] 语音压缩失败:{e}")
                yield event.plain_result(
                    f"音频已下载,但语音压缩失败:{e}\n播放链接:{audio_url}"
                )
                return
            yield event.chain_result([Record(file=audio_path)])
        else:
            yield event.plain_result(f"音频文件下载失败。播放链接:{audio_url}")

    def _extract_order_keyword(self, event: AstrMessageEvent) -> str:
        msg = (event.message_str or "").strip()
        msg = re.sub(r"^\[At:[^\]]+\]\s*", "", msg)
        msg = re.sub(r"^\[CQ:at,[^\]]+\]\s*", "", msg)
        msg = re.sub(r"^@\S+\s+", "", msg)

        match = re.search(r"(?:^|\s)/?点歌(?:\s+|$)(.*)$", msg)
        if not match:
            return ""
        return match.group(1).strip()

    async def _search_song(self, keyword: str) -> Optional[dict[str, Any]]:
        """通过 QQ 音乐搜索歌曲, 返回首条结果的归一化信息."""
        async with Client(self._build_credential_safe()) as client:
            result = await client.search.search_by_type(keyword, search_type=SearchType.SONG, num=10)
            songs = result.song or []
            if not songs:
                return None
            return self._normalize_song(songs[0])

    def _build_credential_safe(self) -> Optional[Credential]:
        """配置了凭据则返回凭据, 否则返回 None 使用匿名访问."""
        return self._build_credential() if self.has_credential else None

    def _normalize_song(self, song: Any) -> dict[str, Any]:
        """将 QQ 音乐 Song 模型归一化为插件内部使用的字典."""
        try:
            thumbnail = song.cover_url() or ""
        except Exception:
            thumbnail = ""
        artists = ", ".join(singer.name for singer in song.singer if singer.name) if song.singer else "未知"
        return {
            "id": str(song.id),
            "mid": song.mid,
            "title": song.name or "Unknown",
            "artists": artists or "未知",
            "album": getattr(song.album, "name", "") or "未知",
            "duration_seconds": int(song.interval or 0),
            "thumbnail": thumbnail,
        }

    async def _get_audio_url(self, song_mid: str) -> dict[str, Any]:
        """逐级降级音质获取首个可用播放链接.

        Args:
            song_mid: 歌曲 Media MID.

        Returns:
            包含 url 与 ext 的字典; 无可用链接时返回空字典.
        """
        credential = self._build_credential_safe()
        async with Client(credential) as client:
            cdn_dispatch = await client.song.get_cdn_dispatch()
            cdn_list = cdn_dispatch.sip or []
            if not cdn_list:
                return {}

            cdn = random.choice(cdn_list)
            for file_type in SONG_FILE_TYPES:
                urls = await client.song.get_song_urls(
                    [SongFileInfo(mid=song_mid, file_type=file_type)],
                    file_type=file_type,
                )
                for info in urls.data or []:
                    if info.purl:
                        return {
                            "url": cdn + info.purl,
                            "ext": file_type.e.lstrip("."),
                            "type": file_type.e.lstrip("."),
                            "level": file_type.name,
                            "source": f"qqmusic:{file_type.name}",
                        }
        return {}

    def _download_audio(self, song_mid: str, audio_url: str, ext_hint: str = "") -> str:
        import requests

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            "Referer": "https://y.qq.com/",
            "Accept": "*/*",
            "Range": "bytes=0-",
        }
        with requests.get(
            audio_url,
            headers=headers,
            timeout=(10, DOWNLOAD_TIMEOUT),
            stream=True,
        ) as resp:
            if resp.status_code not in (200, 206):
                body = ""
                try:
                    body = resp.text[:200]
                except Exception:
                    pass
                raise RuntimeError(f"音频源返回 {resp.status_code}:{body}")

            content_length = resp.headers.get("content-length")
            if content_length and content_length.isdigit():
                size = int(content_length)
                if size > MAX_AUDIO_DOWNLOAD_BYTES:
                    raise RuntimeError(
                        f"音频源文件过大({self._fmt_bytes(size)}),跳过语音发送"
                    )

            ext = self._guess_audio_ext(
                audio_url,
                resp.headers.get("content-type", ""),
                resp.headers.get("content-disposition", ""),
                ext_hint,
            )
            tmp = tempfile.NamedTemporaryFile(
                delete=False,
                suffix=f".{ext}",
                prefix=f"qqm_{song_mid}_",
            )
            try:
                written = 0
                with tmp:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            written += len(chunk)
                            if written > MAX_AUDIO_DOWNLOAD_BYTES:
                                raise RuntimeError(
                                    f"音频源文件超过 {self._fmt_bytes(MAX_AUDIO_DOWNLOAD_BYTES)},跳过语音发送"
                                )
                            tmp.write(chunk)
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
            if written == 0:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise RuntimeError("音频源返回 0 字节,播放链接可能已失效或被风控")
            return tmp.name

    def _prepare_voice_file(self, source_path: str, song_mid: str) -> str:
        ffmpeg = self._ensure_ffmpeg()
        out_file = tempfile.NamedTemporaryFile(
            delete=False,
            suffix=".mp3",
            prefix=f"qqm_voice_{song_mid}_",
        )
        out_path = out_file.name
        out_file.close()

        cmd = [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            source_path,
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(VOICE_SAMPLE_RATE),
            "-b:a",
            VOICE_BITRATE,
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True, timeout=DOWNLOAD_TIMEOUT)
        except Exception:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise

        if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("ffmpeg 未生成语音文件")

        size = os.path.getsize(out_path)
        if size > MAX_VOICE_BYTES:
            try:
                os.unlink(out_path)
            except OSError:
                pass
            raise RuntimeError(
                f"压缩后语音仍过大({self._fmt_bytes(size)}),跳过发送"
            )
        return out_path

    def _has_ffmpeg(self) -> bool:
        return bool(shutil.which("ffmpeg"))

    def _ensure_ffmpeg(self) -> str:
        if self._ffmpeg_ready:
            ffmpeg_path = shutil.which("ffmpeg")
            if ffmpeg_path:
                return ffmpeg_path

        ffmpeg_path = shutil.which("ffmpeg")
        if ffmpeg_path:
            self._ffmpeg_ready = True
            return ffmpeg_path

        exe = self._get_imageio_ffmpeg_exe()
        shim_path = self._install_ffmpeg_shim(exe)
        shim_dir = str(shim_path.parent)
        path_parts = os.environ.get("PATH", "").split(os.pathsep)
        if shim_dir not in path_parts:
            os.environ["PATH"] = shim_dir + os.pathsep + os.environ.get("PATH", "")

        ffmpeg_path = shutil.which("ffmpeg")
        if not ffmpeg_path:
            raise RuntimeError("ffmpeg 已下载但未能加入 PATH")

        self._ffmpeg_ready = True
        logger.info(f"[qqmusic] ffmpeg ready: {ffmpeg_path}")
        return ffmpeg_path

    def _get_imageio_ffmpeg_exe(self) -> str:
        try:
            import imageio_ffmpeg
        except ImportError:
            logger.info("[qqmusic] 未找到 imageio-ffmpeg,尝试自动安装")
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "imageio-ffmpeg>=0.5.1",
                ],
                check=True,
                timeout=FFMPEG_BOOTSTRAP_TIMEOUT,
            )
            import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if not exe or not os.path.exists(exe):
            raise RuntimeError("imageio-ffmpeg 未返回可用的 ffmpeg")
        return exe

    def _install_ffmpeg_shim(self, source: str) -> Path:
        shim_dir = Path(tempfile.gettempdir()) / "astrbot_plugin_ytmusic_ffmpeg"
        shim_dir.mkdir(parents=True, exist_ok=True)
        shim_name = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
        shim_path = shim_dir / shim_name

        if shim_path.exists():
            try:
                if shim_path.resolve() == Path(source).resolve():
                    return shim_path
            except OSError:
                pass
            shim_path.unlink()

        try:
            shim_path.symlink_to(source)
        except OSError:
            shutil.copy2(source, shim_path)

        if os.name != "nt":
            mode = shim_path.stat().st_mode
            shim_path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        return shim_path

    @staticmethod
    def _fmt_bytes(size: int) -> str:
        if size >= 1024 * 1024:
            return f"{size / 1024 / 1024:.1f}MB"
        if size >= 1024:
            return f"{size / 1024:.1f}KB"
        return f"{size}B"

    @staticmethod
    def _guess_audio_ext(
        audio_url: str,
        content_type: str = "",
        content_disposition: str = "",
        ext_hint: str = "",
    ) -> str:
        allowed = {"mp3", "m4a", "aac", "flac", "wav", "ogg", "opus"}
        hint = (ext_hint or "").lower().strip(".")
        if hint in allowed:
            return hint

        m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)"?', content_disposition, re.I)
        if m:
            ext_match = re.search(r"\.([a-zA-Z0-9]+)$", m.group(1))
            if ext_match and ext_match.group(1).lower() in allowed:
                return ext_match.group(1).lower()

        path = urlparse(audio_url).path
        ext_match = re.search(r"\.([a-zA-Z0-9]+)$", path)
        if ext_match and ext_match.group(1).lower() in allowed:
            return ext_match.group(1).lower()

        media_type = content_type.split(";", 1)[0].lower().strip()
        return {
            "audio/mpeg": "mp3",
            "audio/mp3": "mp3",
            "audio/mp4": "m4a",
            "audio/aac": "aac",
            "audio/flac": "flac",
            "audio/x-flac": "flac",
            "audio/wav": "wav",
            "audio/ogg": "ogg",
            "application/ogg": "ogg",
        }.get(media_type, "mp3")

    async def _try_send_qq_music_card(
        self,
        event: AstrMessageEvent,
        song_mid: str,
        title: str,
        artists: str,
        thumbnail: str,
        audio_url: str,
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
        play_url = f"https://y.qq.com/n/ryqq/songDetail/{song_mid}"
        payload = [
            {
                "type": "music",
                "data": {
                    "type": "custom",
                    "url": play_url,
                    "audio": audio_url or play_url,
                    "title": title or "QQ音乐",
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
            logger.debug(f"[qqmusic] QQ 自定义音乐卡片发送失败:{e}")

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
        logger.info("[qqmusic] 插件已卸载")
