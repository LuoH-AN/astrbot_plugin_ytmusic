import asyncio
import os
import re
import tempfile
from typing import Any, Optional
from urllib.parse import urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.star import Context, Star, register


NETEASE_API_BASE = "https://music.luoh.org"
NETEASE_LEVEL = "exhigh"
NETEASE_RANDOM_CN_IP = "true"
API_TIMEOUT = 20
DOWNLOAD_TIMEOUT = 180


@register(
    "astrbot_plugin_ytmusic",
    "LuoH-AN",
    "通过硬编码的网易云 API 点歌的插件,使用 `点歌 歌名` 触发。",
    "3.0.0",
)
class NetEaseMusicPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config or {}
        self.api_base = NETEASE_API_BASE.rstrip("/")
        self.send_card: bool = bool(self.config.get("send_card", True))
        self.send_audio: bool = bool(self.config.get("send_audio", True))
        self.max_duration: int = int(self.config.get("max_duration", 600))

    @filter.command("点歌")
    async def order_song(self, event: AstrMessageEvent, song_name: str = ""):
        keyword = (song_name or "").strip()
        if not keyword:
            msg_str = (event.message_str or "").strip()
            keyword = re.sub(r"^/?点歌\s*", "", msg_str).strip()

        if not keyword:
            yield event.plain_result("请在「点歌」后输入歌曲名,例如: 点歌 晴天")
            return

        yield event.plain_result(f"正在为你搜索:{keyword} ...")

        try:
            track = await asyncio.to_thread(self._search_song, keyword)
        except Exception as e:
            logger.exception("[ncmusic] 搜索失败")
            yield event.plain_result(f"搜索失败:{e}")
            return

        if not track:
            yield event.plain_result(f"未找到与「{keyword}」相关的歌曲。")
            return

        song_id = str(track["id"])
        title = track.get("title") or "Unknown"
        artists = track.get("artists") or "未知"
        album = track.get("album") or "未知"
        duration = int(track.get("duration_seconds") or 0)
        thumbnail = track.get("thumbnail") or ""
        play_url = f"https://music.163.com/#/song?id={song_id}"

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
            audio_info = await asyncio.to_thread(self._get_audio_url, song_id)
        except Exception as e:
            logger.warning(f"[ncmusic] 获取播放链接失败:{e}")

        audio_url = audio_info.get("url") or ""
        if self.send_card:
            await self._try_send_qq_music_card(
                event,
                song_id,
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
            yield event.plain_result("未拿到可播放音频链接,可能需要登录 Cookie 或接口被网易限制。")
            return

        try:
            audio_path = await asyncio.to_thread(
                self._download_audio,
                song_id,
                audio_url,
                audio_info.get("type") or audio_info.get("encodeType"),
            )
        except Exception as e:
            logger.warning(f"[ncmusic] 音频下载失败:{e}")
            yield event.plain_result(f"音频下载失败:{e}\n播放链接:{audio_url}")
            return

        if audio_path and os.path.exists(audio_path):
            yield event.chain_result([Record(file=audio_path)])
        else:
            yield event.plain_result(f"音频文件下载失败。播放链接:{audio_url}")

    def _api_get(self, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
        import requests

        url = f"{self.api_base}/{endpoint.lstrip('/')}"
        query = {"randomCNIP": NETEASE_RANDOM_CN_IP, **params}
        resp = requests.get(url, params=query, timeout=API_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if not isinstance(data, dict):
            raise RuntimeError("API 返回格式不是 JSON object")
        return data

    def _search_song(self, keyword: str) -> Optional[dict[str, Any]]:
        data = self._api_get("search", {"keywords": keyword, "limit": 10})
        songs = (data.get("result") or {}).get("songs") or []
        song = next((item for item in songs if item.get("id")), None)
        if not song:
            return None

        track = self._normalize_song(song)
        try:
            detail = self._song_detail(str(track["id"]))
        except Exception as e:
            logger.debug(f"[ncmusic] 获取歌曲详情失败:{e}")
            detail = {}

        return {**track, **{k: v for k, v in detail.items() if v}}

    def _song_detail(self, song_id: str) -> dict[str, Any]:
        data = self._api_get("song/detail", {"ids": song_id})
        songs = data.get("songs") or []
        if not songs:
            return {}
        return self._normalize_song(songs[0])

    def _normalize_song(self, song: dict[str, Any]) -> dict[str, Any]:
        album = song.get("album") or song.get("al") or {}
        artists = song.get("artists") or song.get("ar") or []
        duration_ms = song.get("duration") or song.get("dt") or 0
        try:
            duration_seconds = int(duration_ms) // 1000
        except (TypeError, ValueError):
            duration_seconds = 0

        return {
            "id": song.get("id"),
            "title": song.get("name") or "Unknown",
            "artists": self._join_artists(artists),
            "album": album.get("name") or "未知",
            "duration_seconds": duration_seconds,
            "thumbnail": album.get("picUrl") or album.get("blurPicUrl") or "",
        }

    def _get_audio_url(self, song_id: str) -> dict[str, Any]:
        for use_unblock in (False, True):
            params = {
                "id": song_id,
                "level": NETEASE_LEVEL,
            }
            if use_unblock:
                params["unblock"] = "true"

            data = self._api_get("song/url/v1", params)
            items = data.get("data") or []
            if items and isinstance(items[0], dict):
                item = items[0]
                url = item.get("url") or item.get("proxyUrl") or ""
                if url:
                    return {
                        "url": url,
                        "type": item.get("type") or item.get("encodeType") or "",
                        "level": item.get("level") or NETEASE_LEVEL,
                        "source": "song/url/v1:unblock" if use_unblock else "song/url/v1",
                    }

        fallback = self._api_get("song/url/match", {"id": song_id})
        url = fallback.get("data") or fallback.get("proxyUrl") or ""
        if isinstance(url, str) and url:
            return {"url": url, "type": "", "level": "match"}
        return {}

    def _download_audio(self, song_id: str, audio_url: str, ext_hint: str = "") -> str:
        import requests

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
        with requests.get(
            audio_url,
            headers=headers,
            timeout=(10, DOWNLOAD_TIMEOUT),
            stream=True,
        ) as resp:
            if resp.status_code != 200:
                body = ""
                try:
                    body = resp.text[:200]
                except Exception:
                    pass
                raise RuntimeError(f"音频源返回 {resp.status_code}:{body}")

            ext = self._guess_audio_ext(
                audio_url,
                resp.headers.get("content-type", ""),
                resp.headers.get("content-disposition", ""),
                ext_hint,
            )
            tmp = tempfile.NamedTemporaryFile(
                delete=False,
                suffix=f".{ext}",
                prefix=f"ncm_{song_id}_",
            )
            try:
                with tmp:
                    for chunk in resp.iter_content(chunk_size=64 * 1024):
                        if chunk:
                            tmp.write(chunk)
            except Exception:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass
                raise
            return tmp.name

    @staticmethod
    def _join_artists(artists: list[dict[str, Any]]) -> str:
        names = [item.get("name", "") for item in artists if item.get("name")]
        return ", ".join(names) if names else "未知"

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
        song_id: str,
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
        play_url = f"https://music.163.com/#/song?id={song_id}"
        payload = [
            {
                "type": "music",
                "data": {
                    "type": "custom",
                    "url": play_url,
                    "audio": audio_url or play_url,
                    "title": title or "网易云音乐",
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
            logger.debug(f"[ncmusic] QQ 自定义音乐卡片发送失败:{e}")

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
        logger.info("[ncmusic] 插件已卸载")
