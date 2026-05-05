# astrbot_plugin_ytmusic

通过 YouTube Music 点歌的 AstrBot 插件。在群里发 `点歌 晴天` 即可触发：返回歌曲信息卡片 + 缩略图，可选发送 QQ 音乐自定义卡片，可选下载并发送音频。

## 功能

- `点歌 <歌名>` 通过 [`ytmusicapi`](https://github.com/sigma67/ytmusicapi) 在 YouTube Music 上搜索
- 返回歌曲标题 / 歌手 / 时长 / 链接 + 缩略图
- 可选发送 QQ 自定义音乐卡片（仅 aiocqhttp）
- 可选用 `yt-dlp` 下载音频（mp3）并以语音消息发送
- **支持通过 Cloudflare Worker 反向代理访问 YouTube**（解决出口 IP 被墙 / 被 YT 反爬的场景）
- cookies 支持本地路径或 http(s) URL（带本地缓存与定时刷新）

## 安装

把仓库 clone 到 AstrBot 插件目录，或者在 AstrBot 控制台用插件市场安装（如果已上架）：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/LuoH-AN/astrbot_plugin_ytmusic
```

依赖：

```bash
pip install ytmusicapi yt-dlp
# 系统层面还要 ffmpeg(转 mp3 用)
apt install ffmpeg   # 或对应发行版的包管理器
```

reload 插件即可。

## 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `proxy_base` | `https://ytproxy.luoh.org` | 路径式反代基址。请求 `https://music.youtube.com/foo` 会被改写成 `<proxy_base>/music.youtube.com/foo`，由 Worker 反代到上游。设为空则关闭反代。 |
| `proxy` | `""` | 兜底 HTTP/SOCKS 代理（如 `http://127.0.0.1:7890`）。仅在 `proxy_base` 为空时使用。 |
| `cookies_file` | `/AstrBot/data/music.youtube.com_cookies.txt` | YouTube cookies（Netscape 格式）。可填本地路径或 `http(s)://` URL。yt-dlp 用它绕过 "Sign in to confirm you're not a bot"。 |
| `cookies_refresh_seconds` | `3600` | URL 形式 cookies 的本地缓存有效期（秒）。 |
| `send_card` | `true` | 是否尝试发 QQ 自定义音乐卡片（仅 aiocqhttp）。 |
| `send_audio` | `true` | 是否下载并发音频文件。 |
| `max_duration` | `600` | 超过该秒数的歌跳过音频下载（仍发卡片/链接）。 |

## 部署反代 Worker（可选）

仓库里 `worker.js` 是配套的 Cloudflare Worker，三种模式：

1. **DOMAIN_MAP**：精确域名映射（Telegram / Discord 等）
2. **WILDCARD**：`<host>.bot.luoh.org` 通配反代（需 Total TLS）
3. **PATH_PROXY_HOSTS**：路径式反代 `ytproxy.luoh.org/<host>/<path>` ← **本插件默认用这个**

### 部署步骤

1. CF 控制台创建一个 Worker，把 `worker.js` 内容粘进去。如果你不需要 Telegram / Discord 反代，可以把 `DOMAIN_MAP` 清空；如果你的 zone 不是 `luoh.org`，把 `PATH_PROXY_HOSTS` 里的 host 改成你自己的域名。
2. **DNS**：加一条 A 记录 `ytproxy` → `192.0.2.1`，**橙色云**。
3. **Workers Route**：路由 `ytproxy.luoh.org/*` → 该 Worker。
4. SSL：单层子域，Universal SSL 自动覆盖，不用 Total TLS。

### 验证

```bash
curl -I https://ytproxy.luoh.org/music.youtube.com/
curl -I https://ytproxy.luoh.org/i.ytimg.com/
curl https://ytproxy.luoh.org/example.com/   # 应该 403,allowlist 不放行
```

## 关于 cookies

YouTube 现在对来自数据中心 IP 的匿名请求基本都要 cookie 验证。导出方法：

1. Chrome / Edge 装 [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/cclelndahbckbenkjhflpdbgdldlbecc)；Firefox 用 [cookies.txt](https://addons.mozilla.org/en-US/firefox/addon/cookies-txt/)
2. **登录** youtube.com（建议小号）
3. 在 YouTube 任意页打开扩展 → Export → 下载 `cookies.txt`
4. 放到服务器（默认路径 `/AstrBot/data/music.youtube.com_cookies.txt`），`chmod 600`

或者把这个文件放到一个**带访问控制**的 URL 上，`cookies_file` 直接填 URL。插件会带浏览器 UA 拉取，缓存到本地临时目录，按 `cookies_refresh_seconds` 过期重拉。

> 警告：cookies 是完整登录态，**别公开分享 / 别提交进 git**。

## 已知行为

- 日志里偶尔出现 `无效或过期的文件 token: <uuid>` 是 AstrBot 自己的 `/file/<token>` 临时下载链接过期，跟本插件 / YouTube cookies 无关，不影响功能。
- yt-dlp 第一次启动慢是正常的，之后会复用进程级解析器。
- 反代模式下 yt-dlp 不需要再单独配 `proxy`；插件会自动把出站 URL 全部劫持到 `proxy_base`。

## 许可证

MIT
