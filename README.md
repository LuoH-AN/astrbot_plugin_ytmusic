# astrbot_plugin_ytmusic 网易云版

通过网易云音乐点歌的 AstrBot 插件。在群里发 `点歌 晴天` 即可触发：返回歌曲信息、封面、网易云链接，可选发送 QQ 音乐自定义卡片，可选下载音频后以语音消息发送。

当前版本已改为使用硬编码 API：

```text
https://music.luoh.org
```

## 功能

- `点歌 <歌名>` 调用 `https://music.luoh.org/search` 搜索网易云歌曲
- 调用 `https://music.luoh.org/song/detail` 补全封面、专辑、歌手信息
- 调用 `https://music.luoh.org/song/url/v1` 获取播放链接,拿不到时自动用 `unblock=true` 重试
- 可选发送 QQ 自定义音乐卡片(仅 aiocqhttp)
- 可选把音频下载到临时文件后用 `Record` 发送
- 发送语音前会检测 `ffmpeg`,缺失时自动安装 `imageio-ffmpeg` 并把内置 ffmpeg 加入运行时 `PATH`

## 安装

把仓库 clone 到 AstrBot 插件目录：

```bash
cd /AstrBot/data/plugins
git clone https://github.com/LuoH-AN/astrbot_plugin_ytmusic
```

安装依赖：

```bash
pip install -r astrbot_plugin_ytmusic/requirements.txt
```

reload 插件。

## 配置项

| 字段 | 默认值 | 说明 |
|---|---|---|
| `send_card` | `true` | 是否尝试发送 QQ 自定义音乐卡片(仅 aiocqhttp)。 |
| `send_audio` | `true` | 是否下载音频并发送语音。 |
| `max_duration` | `600` | 超过该秒数的歌跳过音频下载,仍返回歌曲信息和链接。 |

## 说明

- API 地址已硬编码在 `main.py` 的 `NETEASE_API_BASE = "https://music.luoh.org"`。
- 默认音质参数为 `NETEASE_LEVEL = "exhigh"`。
- 搜索和取链接都会带 `randomCNIP=true`,取不到官方播放 URL 时会自动用 `unblock=true` 重试。
- 播放链接可能会过期,插件会在每次点歌时重新获取。
- 如果返回 `url: null`,通常是网易限制、Cookie 缺失或接口部署侧问题。
- 如果系统没有 `ffmpeg`,插件会在第一次发送语音时尝试自动准备。自动安装需要当前 Python 环境能执行 `pip install imageio-ffmpeg`。

## 许可证

MIT
