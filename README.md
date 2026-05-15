# astrbot_plugin_ytmusic

通过 YouTube Music 点歌的 AstrBot 插件。在群里发 `点歌 晴天` 即可触发：返回歌曲信息卡片 + 缩略图，可选发送 QQ 音乐自定义卡片，可选发送音频语音消息。

**v2 起架构变更**：服务器只负责搜索 / 封面 / 卡片(走反代,出口流量极少),**音频下载由你本地机器上跑的独立 API 承担**(见 `download_api/`),这样服务器 IP 不再直接接触 YouTube 的下载流量,大大降低风控/封号风险。

## 功能

- `点歌 <歌名>` 通过 [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) 在 YouTube Music 上搜索
- 返回歌曲标题 / 歌手 / 时长 / 链接 + 缩略图
- 可选发送 QQ 自定义音乐卡片(仅 aiocqhttp)
- 可选请求本地下载 API 拿到 mp3 后以语音消息发送
- **元信息走 Cloudflare Worker 反代,音频走本地 API**,服务器零下载

## 架构

```
                          ┌─────────────────────────────┐
   群消息: 点歌 晴天  ──►  │  AstrBot + 本插件 (公网服务器) │
                          │                             │
                          │  ① ytmusicapi 搜索/封面 ──── ►  Cloudflare Worker 反代 ──► YT Music API
                          │                             │
                          │  ② GET /download/{videoId}  │
                          └────────┬────────────────────┘
                                   │ HTTP
                                   ▼
                          ┌─────────────────────────────┐
                          │  download_api (本地机器)     │
                          │  yt-dlp + ffmpeg            │
                          │  监听 0.0.0.0:3000          │
                          └────────┬────────────────────┘
                                   │
                                   ▼
                              YouTube Music
```

## 安装

把仓库 clone 到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/LuoH-AN/astrbot_plugin_ytmusic
```

服务端依赖(轻量,不需要 yt-dlp / ffmpeg)：

```bash
pip install ytmusicapi requests
```

reload 插件。

**还要在本地起 `download_api/`(否则点歌只能返回卡片,没语音)**：

```bash
cd astrbot_plugin_ytmusic/download_api
pip install -r requirements.txt
apt install ffmpeg
python app.py
```

详见 [`download_api/README.md`](download_api/README.md)。

## 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `proxy_base` | `https://ytproxy.luoh.org` | 路径式反代基址,仅用于元信息/封面拉取。设为空则关闭反代。 |
| `proxy` | `""` | 兜底 HTTP/SOCKS 代理。仅在 `proxy_base` 为空时使用。 |
| `download_api_base` | `""` | **必填**。本地下载 API 地址,例如 `http://127.0.0.1:3000` 或经隧道暴露后的 `https://xxx.ngrok-free.app`。 |
| `download_api_key` | `""` | 鉴权密钥,需与 API 端 `YTM_API_KEY` 一致。 |
| `download_api_timeout` | `300` | 调用下载 API 的总超时秒数。 |
| `send_card` | `true` | 是否尝试发 QQ 自定义音乐卡片(仅 aiocqhttp)。 |
| `send_audio` | `true` | 是否调用下载 API 拉音频并发送语音。 |
| `max_duration` | `600` | 超过该秒数的歌跳过音频下载(仍发卡片/链接)。 |

## 让服务器访问本地 API

服务器需要某种方式连到你本机的 3000 端口。常见做法:

- **反向隧道**:ngrok / cloudflared / frp
- **VPN**:tailscale / wireguard,服务器和本机互通后填内网 IP
- **同机部署**:AstrBot 也跑在本机时,直接填 `http://127.0.0.1:3000`

> 务必给本地 API 设 `YTM_API_KEY`,否则别人扫到你的开放端口就能用你的 IP 跑 yt-dlp。

## 部署元信息反代 Worker(可选但推荐)

仓库里 `worker.js` 是配套的 Cloudflare Worker(路径式反代),用来让服务器拉 YT 元信息时不暴露真实出口 IP。部署步骤:

1. CF 控制台创建 Worker,粘 `worker.js`。如果 zone 不是 `luoh.org`,把 `PATH_PROXY_HOSTS` 改成你自己的域名。
2. **DNS**:加一条 A 记录 `ytproxy` → `192.0.2.1`,**橙色云**。
3. **Workers Route**:`ytproxy.luoh.org/*` → 该 Worker。
4. SSL:单层子域,Universal SSL 自动覆盖。

### 验证

```bash
curl -I https://ytproxy.luoh.org/music.youtube.com/
curl -I https://ytproxy.luoh.org/i.ytimg.com/
curl https://ytproxy.luoh.org/example.com/   # 应该 403
```

## 关于 cookies

cookies 现在**只在本地 API 端配置**,通过 `YTM_COOKIES_FILE` 环境变量指定 Netscape 格式文件的路径。导出方法:

1. Chrome / Edge 装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/cclelndahbckbenkjhflpdbgdldlbecc);Firefox 用 [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. **登录** youtube.com(建议小号)
3. 在 YouTube 任意页打开扩展 → Export → 下载 `cookies.txt`
4. 放到本机,设 `YTM_COOKIES_FILE` 指向它,`chmod 600`

> 警告:cookies 是完整登录态,别公开分享 / 别提交进 git。

## 从 v1 升级

- 配置里删掉 `cookies_file` / `cookies_refresh_seconds`(不再有用)
- 新增 `download_api_base`,填你本地 API 地址
- 服务端可以卸载 `yt-dlp` 和 `ffmpeg`(本插件不再用)
- 在本机按 `download_api/README.md` 起服务

## 许可证

MIT
