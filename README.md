# astrbot_plugin_ytmusic QQ 音乐版

通过 QQ 音乐点歌的 AstrBot 插件。在群里发 `点歌 晴天` 或 `@人机 点歌 晴天` 即可触发：返回歌曲信息、封面、QQ音乐链接，可选发送 QQ 音乐自定义卡片，可选下载音频后以语音消息发送。

本版本基于 [`qqmusic-api-python`](https://github.com/luren-dc/QQMusicApi) 直接对接 QQ 音乐官方接口，不再依赖任何第三方转发 API。

## 功能

- `点歌 <歌名>` / `@人机 点歌 <歌名>` 调用 QQ 音乐搜索歌曲（`search_by_type`）
- 取搜索结果首条，解析标题、歌手、专辑、时长、封面
- 调用 `get_cdn_dispatch` + `get_song_urls` 获取播放链接，并按 `MP3_320 → MP3_128 → OGG_96` 逐级降级音质，取首个可用链接
- 可选发送 QQ 自定义音乐卡片（仅 aiocqhttp）
- 可选把音频下载到临时文件后用 `Record` 发送
- 发送语音前会检测 `ffmpeg`，缺失时自动安装 `imageio-ffmpeg` 并把内置 ffmpeg 加入运行时 `PATH`
- 发送语音前会把音频压成 24kHz/单声道/64k MP3，避免大文件导致 QQ `sendMsg` 超时

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
| `musicid` | `""` | QQ 音乐 MusicID，登录后获取，用于下载有版权歌曲。留空仍可搜索，但多数歌曲无法拿到音频链接。 |
| `musickey` | `""` | QQ 音乐 MusicKey，与 MusicID 配对使用，**请勿公开泄露**。 |
| `send_card` | `true` | 是否尝试发送 QQ 自定义音乐卡片（仅 aiocqhttp）。 |
| `send_audio` | `true` | 是否下载音频并发送语音。 |
| `max_duration` | `600` | 超过该秒数的歌跳过音频下载，仍返回歌曲信息和链接。 |

## 如何获取凭据

`musicid` / `musickey` 来自 QQ 音乐登录态。可使用 `qqmusic-api-python` 提供的登录示例扫码获取，登录成功后即可拿到这两个值：

- `musicid` 即登录返回的 `Credential.musicid`
- `musickey` 即登录返回的 `Credential.musickey`

填入插件配置即可。需要下载 VIP / 有版权歌曲时，请使用对应账号登录获取凭据。

## 说明

- 音质策略为逐级降级：优先 320kbps MP3，失败则尝试 128kbps MP3，最后尝试 96kbps OGG，取首个返回 `purl` 的结果。匿名访问通常只能拿到极少数歌曲，配置凭据可显著提升成功率。
- 播放链接（`purl` + CDN）有时效性，插件会在每次点歌时重新获取。
- 如果返回的链接为空，通常是该歌曲无版权、需要会员，或未配置凭据。
- 如果系统没有 `ffmpeg`，插件会在第一次发送语音时尝试自动准备。自动安装需要当前 Python 环境能执行 `pip install imageio-ffmpeg`。
- 为减少 aiocqhttp/NapCat 偶发 `sendMsg` 超时，插件会限制源音频下载大小，并在发送前压缩语音文件；压缩后仍过大时会跳过语音发送并返回播放链接。

## 许可证

MIT
