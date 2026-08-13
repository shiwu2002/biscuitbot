# generate_video

通过火山引擎方舟（Volcengine Ark）Seedance 视频大模型生成或编辑视频。支持文生视频、图生视频、以及基于参考图 / 参考视频 / 参考音频的编辑，任务异步执行，完成后返回下载到本地的视频文件路径。

## 何时使用

用户要求生成一段视频、把图片变成视频、或基于参考素材编辑视频时使用。相比旧的 seedance 技能，本工具一次调用即可完成「创建任务 → 轮询 → 下载」，无需写脚本或 exec。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、运镜、景别、构图、光影、氛围、节奏 |
| image_urls | array | 否 | - | 参考图列表（本地路径 / 公网 URL / base64 data URL，本地自动转 base64） |
| video_urls | array | 否 | - | 参考视频列表（**仅公网 HTTP(S) URL**，不支持本地文件） |
| audio_urls | array | 否 | - | 参考音频列表（本地路径 / 公网 URL / base64 data URL） |
| ratio | string | 否 | 16:9 | 画幅：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive |
| duration | integer | 否 | 5 | 时长（秒），2.0 支持 4–15，2.5 支持到 30 |
| resolution | string | 否 | - | 清晰度：480p / 720p / 1080p / 4K（4K 仅 2.5） |
| generate_audio | boolean | 否 | false | 是否开启音画同步生成音频 |
| watermark | boolean | 否 | true | 是否添加水印 |
| model | string | 否 | 配置默认 | 模型覆盖：doubao-seedance-2-5-260628 / doubao-seedance-2-0-260128 / Endpoint ID |

## 调用示例

```
# 文生视频
generate_video(prompt="一只橘猫在钢琴前弹奏，特写镜头，电影感")

# 图生视频（参考图，本地路径自动转 base64）
generate_video(prompt="让照片里的猫转头看向镜头", image_urls=["/path/to/cat.jpg"])

# 多模态编辑（参考图 + 参考视频）
generate_video(
  prompt="将视频中的香水替换成图片里的面霜，运镜不变",
  image_urls=["/path/to/cream.jpg"],
  video_urls=["https://example.com/gift.mp4"],
  ratio="9:16",
  duration=8,
)
```

## 注意事项

- 参考**视频**只接受公网可访问的 HTTP(S) URL；本地视频文件需先上传到可访问地址。
- 参考**图片 / 音频**支持本地路径（自动 base64）与公网 URL。
- 视频生成耗时较长（通常数分钟），工具内部会自动轮询直至完成。
- 结果会下载到媒体目录并持久化，返回本地 `path`；可用该路径作为后续剪辑工具的输入，或通过 message 工具交付给用户。
- 需要 `ARK_API_KEY`（或 config 的 `tools.seedance_video.apiKey`），且 `tools.seedance_video.enabled` 为 true。
