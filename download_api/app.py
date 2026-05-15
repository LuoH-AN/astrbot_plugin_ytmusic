"""YouTube Music 音频下载 API。

设计目的:
    把 YouTube 音频下载这件"出口 IP 容易被风控"的事从公网服务器搬到本地机器。
    AstrBot 插件只调用 GET /download/{video_id},服务器自己不接触 YouTube。

启动:
    pip install -r requirements.txt
    # 系统层依赖 ffmpeg(yt-dlp 转 mp3 用),自行安装。
    # 可选环境变量:
    #   YTM_COOKIES_FILE   YouTube cookies(Netscape 格式),用于绕过登录验证
    #   YTM_API_KEY        鉴权密钥,设了之后请求需带 X-API-Key 头
    #   YTM_HOST           监听地址,默认 0.0.0.0
    #   YTM_PORT           监听端口,默认 3000
    python app.py

接口:
    GET /health                -> {"status":"ok", ...}
    GET /download/{video_id}   -> 音频文件流(默认 mp3)
"""
import logging
import os
import re
import tempfile
from typing import Optional

import yt_dlp
from fastapi import BackgroundTasks, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ytm-download-api")

COOKIES_FILE = os.environ.get("YTM_COOKIES_FILE", "").strip()
API_KEY = os.environ.get("YTM_API_KEY", "").strip()
HOST = os.environ.get("YTM_HOST", "0.0.0.0")
PORT = int(os.environ.get("YTM_PORT", "3000"))

# YouTube videoId 通常 11 位,放宽到 6-32 防止误拒
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
EXT_MEDIA_TYPES = {
    "mp3": "audio/mpeg",
    "m4a": "audio/mp4",
    "webm": "audio/webm",
    "opus": "audio/ogg",
    "ogg": "audio/ogg",
}

app = FastAPI(title="ytmusic-download-api", version="1.0.0")


def _check_auth(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def _cleanup(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


@app.get("/health")
def health():
    return {
        "status": "ok",
        "cookies_loaded": bool(COOKIES_FILE) and os.path.exists(COOKIES_FILE),
        "auth_required": bool(API_KEY),
    }


@app.get("/download/{video_id}")
def download(
    video_id: str,
    background: BackgroundTasks,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
):
    _check_auth(x_api_key)
    if not VIDEO_ID_RE.match(video_id):
        raise HTTPException(status_code=400, detail="bad video_id")

    tmp_dir = tempfile.gettempdir()
    out_template = os.path.join(tmp_dir, f"ytm_{video_id}.%(ext)s")

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": out_template,
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
    if COOKIES_FILE:
        if os.path.exists(COOKIES_FILE):
            ydl_opts["cookiefile"] = COOKIES_FILE
        else:
            log.warning(f"YTM_COOKIES_FILE 不存在: {COOKIES_FILE}")

    url = f"https://music.youtube.com/watch?v={video_id}"
    log.info(f"download start: {video_id}")
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])
    except Exception as e:
        log.exception("download failed")
        raise HTTPException(status_code=502, detail=f"download failed: {e}")

    for ext in ("mp3", "m4a", "webm", "opus", "ogg"):
        candidate = os.path.join(tmp_dir, f"ytm_{video_id}.{ext}")
        if os.path.exists(candidate):
            log.info(f"download done: {video_id} -> {candidate}")
            background.add_task(_cleanup, candidate)
            return FileResponse(
                candidate,
                media_type=EXT_MEDIA_TYPES.get(ext, "application/octet-stream"),
                filename=f"{video_id}.{ext}",
            )

    raise HTTPException(status_code=500, detail="audio file missing after yt-dlp run")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT)
