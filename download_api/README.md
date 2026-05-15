# ytmusic 下载 API

把 YouTube 音频下载从公网服务器搬到本地机器,服务端只调用这个 API。

## 启动

```bash
cd download_api
pip install -r requirements.txt
# 系统层依赖 ffmpeg(yt-dlp 转 mp3 用)
apt install ffmpeg     # 或对应发行版的包管理器

# 可选环境变量
export YTM_COOKIES_FILE=/path/to/cookies.txt  # 绕过 YT "Sign in to confirm..." 验证
export YTM_API_KEY=your-secret                # 鉴权,设了之后请求需带 X-API-Key 头
export YTM_HOST=0.0.0.0
export YTM_PORT=3000

python app.py
```

也可以用 uvicorn 直接拉:

```bash
uvicorn app:app --host 0.0.0.0 --port 3000
```

## 接口

### `GET /health`

```json
{ "status": "ok", "cookies_loaded": true, "auth_required": false }
```

### `GET /download/{video_id}`

请求头(可选):`X-API-Key: <密钥>` —— 与服务端 `YTM_API_KEY` 对齐。

成功:返回 `audio/mpeg`(或其它音频 MIME)文件流,`Content-Disposition: attachment; filename="<video_id>.mp3"`。

失败:
- `400` videoId 非法
- `401` 鉴权失败
- `502` yt-dlp 下载失败(YouTube 网络/版权/cookies 失效)
- `500` 转码后找不到产物文件

## 让服务端访问本地 API

本机 API 默认监听 `0.0.0.0:3000`,但你的 AstrBot 跑在公网服务器上,需要某种方式让服务器连过来。常见做法:

1. **反向隧道** —— ngrok / cloudflared / frp,把本机 3000 暴露成一个公网域名,然后在插件配置里填这个域名。
2. **私网互通** —— 服务器和本机都接到同一张 VPN(tailscale / wireguard)里,直接填 tailscale IP。
3. **同机部署** —— AstrBot 和这个 API 一起跑在本机(Docker / 裸机均可),插件填 `http://127.0.0.1:3000` 即可。

> 别把没设 `YTM_API_KEY` 的 API 直接暴露到公网,否则任何人都能用你本机的出口 IP 拉 YT 音频。
