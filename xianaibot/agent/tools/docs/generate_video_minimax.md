# generate_video_minimax

通过 MiniMax H3（默认模型 `MiniMax-H3`）视频大模型生成或编辑视频。支持多模态输入：文本 + 首尾帧 + 参考图（主体/风格）+ 参考视频 + 参考音频。任务异步执行，完成后返回下载到本地的视频文件路径。

> **模式按输入自动判定**：有参考素材（参考图 / 参考视频 / 参考音频）→ 多模态参考生视频（r2va）；有 `image_urls` → 图生视频（i2va，首帧 / 尾帧）；否则 → 文生视频（t2va）。MiniMax H3 独有「参考图」与「首尾帧」的区分，是三家厂商里多模态能力最强的一家。

## 何时使用

用户要求生成一段视频、把图片变成视频、或需要**用参考图（主体/风格）+ 参考视频 + 参考音频**精确控制生成内容时使用。也可作为火山方舟 Seedance、可灵之外的备选厂商。本工具一次调用即可完成「创建任务 → 轮询 → 取下载地址 → 下载」，无需写脚本或 exec。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、运镜、景别、构图、光影、氛围、节奏 |
| image_urls | array | 否 | - | **首帧 / 尾帧**（图生视频 i2va）：1 张 = 首帧，2 张 = 首帧 + 尾帧，**最多 2 张**；本地路径 / 公网 URL / base64 data URL（本地自动转 base64）。**与 `reference_images` 互斥** |
| reference_images | array | 否 | - | **参考图**（主体外观 / 风格参考，不是首尾帧），**最多 9 张**；与 `image_urls` 互斥；本地路径自动转 base64 |
| video_urls | array | 否 | - | 参考视频列表（**仅公网 HTTP(S) URL**，不支持本地文件），**最多 3 段** |
| audio_urls | array | 否 | - | 参考音频列表（本地路径 / 公网 URL / base64 data URL），**最多 3 段**且**不能单独使用**（须同时传 `reference_images` 或 `video_urls`） |
| ratio | string | 否 | 16:9 | 画幅：adaptive / 21:9 / 16:9 / 4:3 / 1:1 / 3:4 / 9:16。**模式相关**：文生视频（t2va）必填且不接受 `adaptive`（缺省用 `16:9`）；图生视频由首帧推导、传了也被忽略；参考生视频（r2va）可选 |
| duration | integer | 否 | 5 | 时长（秒）**4–15**，超出自动截断到区间内 |
| resolution | string | 否 | 768P | 清晰度：**768P / 2K**（各模式都必须下发；480p、720p 自动映射为 768P，1080p、4K 映射为 2K） |
| watermark | boolean | 否 | false | 是否添加 AIGC 水印（映射后端的 `aigc_watermark`，默认关闭） |
| model | string | 否 | MiniMax-H3 | 模型覆盖（**大小写敏感**：`MiniMax-H3` 或 `MiniMax-H3-Max`） |

> 本工具**没有** `seed`（随机种子）参数，也没有 `generate_audio` 开关——MiniMax H3 不支持，传入会被 `**kwargs` 静默吸收。

## 调用示例

```
# 文生视频（t2va，ratio 必填且不接受 adaptive）
generate_video_minimax(prompt="一只橘猫在钢琴前弹奏，特写镜头，电影感", ratio="16:9")

# 首尾帧指定（image_urls 语义为首帧 / 尾帧，最多 2 张）
generate_video_minimax(
  prompt="镜头从窗外缓慢推入室内，光线渐变",
  image_urls=["/path/to/first.jpg", "/path/to/last.jpg"],
)

# 多模态参考（参考图 + 参考视频 + 参考音频，三者需搭配使用）
generate_video_minimax(
  prompt="用参考图中的角色在参考视频的运镜节奏下表演，配合参考音频的口型",
  reference_images=["/path/to/char.jpg"],
  video_urls=["https://example.com/motion.mp4"],
  audio_urls=["/path/to/voice.mp3"],
  ratio="9:16",
  duration=8,
)

# 2K 清晰度 + AIGC 水印
generate_video_minimax(prompt="海浪拍打礁石，长镜头", resolution="2K", watermark=true)
```

## 提示词写作

提示词按「主体 → 动作 → 环境 → 运镜 → 风格」组织，越具体越好（完整的五个维度与示例见 `docs/generate_video_seedance.md` 的「提示词写作」一节，三个厂商通用）。多模态参考场景下，提示词重点写「**如何组合这些参考素材**」——哪张图是主体、哪个视频提供运镜节奏、哪段音频决定口型。

> 通用提示词手法（图生图怎么写、否定式表述、画幅、文字渲染、安全边界）见 `media-generation-craft` 技能目录下的 `references/prompt-engineering.md`。

## 多片段拼接工作流

生成长视频时拆分片段、逐段生成再拼接：每个片段单独调用 `generate_video_minimax`（保持角色 / 场景一致），全部片段生成后用 FFmpeg concat 合并。完整步骤见 `docs/generate_video_seedance.md` 的「多片段拼接工作流」（三个厂商通用）。

## 厂商注意事项（MiniMax H3）

- **密钥 / 启用**：在「模型厂商」页添加厂商 `minimax`，填写 API Key。H3 与 MiniMax 聊天接口**共用同一个静态 Key**（`Authorization: Bearer`，不需要可灵那样的 JWT 签名）。也可在 config 的 `tools.minimax_video.apiKey` 显式配置。配置了上述任一密钥即启用本工具。
- **base URL**：官方默认 `https://api.minimaxi.com`（中国大陆；海外用 `api.minimax.io`）。**apiBase 留空即用默认**；若把它同时当 LLM 厂商用，填了带 `/v1` 的聊天式地址也没关系，工具会自动归一化到裸域。
- **模型名大小写敏感**，v2 端点可选 `MiniMax-H3`（默认）与 `MiniMax-H3-Max`（更快，但只支持 480P / 768P，不支持 2K）。若配置里残留的是其他厂商模型名（切厂商未切模型），工具会自动回退为 `MiniMax-H3`。
- **`resolution` 各模式都必须下发**（与 Seedance 的 r2v「勿传 resolution」规则相反）；取值 `768P` / `2K`，工具的词表会自动就近映射（480p、720p → 768P；1080p、4K → 2K）。
- **`ratio` 是模式相关的**：文生视频（t2va）必填且不接受 `adaptive`；图生视频（i2va）由首帧推导、传了会被忽略；参考生视频（r2va）可选。
- **`image_urls`（首帧 / 尾帧）与 `reference_images`（参考图）互斥**，同传会直接报错；参考音频**不能单独使用**，须搭配参考图或参考视频。
- **素材上限**：首帧 ≤1、尾帧 ≤1、参考图 ≤9、参考视频 ≤3、参考音频 ≤3；素材文件合计 ≤12、请求体 ≤64MB。**本地文件转 base64 后体积约膨胀 1/3**，体积过大时改用公网 URL 或压缩素材。
- **参考视频只接受公网 HTTP(S) URL**；参考图片 / 音频支持本地路径（自动 base64）与公网 URL。
- 请求走 `POST /v2/video_generation` 的多模态 `content[]` 数组（`text` / `image_url` / `video_url` / `audio_url`，媒体项各带角色）。任务成功响应返回的是 `file_id` 而非视频地址，工具会自动再调一次文件接口换取下载地址；轮询端点与状态词表有多个文档版本，工具内置了自动探测与兼容，无需手动指定。
- **耗时较长**（2K / 15 秒任务更久）工具内部会自动轮询直至完成；若提示超时，可调大 config 的 `tools.minimax_video.maxPollAttempts`。
- 结果会下载到媒体目录并持久化，返回本地 `path`；可用该路径作为后续剪辑工具的输入，或通过 message 工具交付给用户。
- 参数校验失败（互斥、超限、体积过大等）会以 `Error: ...` 文本返回而**不抛异常**，按提示调整参数后重试即可。
