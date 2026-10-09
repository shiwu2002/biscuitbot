# generate_video_kling

通过可灵（Kling，默认模型 `kling-3.0`）视频大模型生成或编辑视频。支持文生视频、图生视频、以及基于参考视频的视频生视频，任务异步执行，完成后返回下载到本地的视频文件路径。

> 可灵不支持参考音频、参考图（风格）、随机种子与水印——这些参数不在本工具的参数表中，传入会被静默忽略。需要这些能力的场景请改用 `generate_video_seedance` 或 `generate_video_minimax`。

## 何时使用

用户要求生成一段视频、把图片变成视频、或对已有视频做变换时使用。本工具一次调用即可完成「创建任务 → 轮询 → 下载」，无需写脚本或 exec。也可作为火山方舟 Seedance 之外的备选厂商。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、运镜、景别、构图、光影、氛围、节奏 |
| image_urls | array | 否 | - | 参考图列表（本地路径 / 公网 URL / base64 data URL，本地自动转 base64）。**单张作首帧；多张时前段作首帧参考、最后一张作尾帧锚点**。微信/渠道收到的图片本地路径可直接传入（中转网关实测可用） |
| video_urls | array | 否 | - | 参考视频列表（**仅公网 HTTP(S) URL**，不支持本地文件）。**每次任务最多 1 段**，且此时长上限降为 10 秒 |
| ratio | string | 否 | 16:9 | 画幅：**仅支持 16:9 / 9:16 / 1:1**，其余取值会自动丢弃 |
| duration | integer | 否 | 5 | 时长（秒）3–15；带参考视频时 3–10，超出自动截断 |
| resolution | string | 否 | - | 清晰度：720p / 1080p / 4k（可灵 3.0 支持） |
| generate_audio | boolean | 否 | true | 是否开启原生音频（`settings.audio=native`，**默认开启**：未传参时自动打开，传 `false` 可关闭） |
| model | string | 否 | kling-3.0 | 模型覆盖：kling-3.0 / kling-3.0-pro / kling-3.0-turbo / kling-v3-omni / kling-video-o1 等（**模型名内嵌在请求 URL 路径**） |

> 本工具**没有** `reference_images`（参考图风格）、`audio_urls`（参考音频）、`seed`、`watermark` 参数——可灵官方不支持这些能力，传入会被 `**kwargs` 静默吸收。

## 调用示例

```
# 文生视频
generate_video_kling(prompt="一只橘猫在钢琴前弹奏，特写镜头，电影感")

# 图生视频（单图首帧；本地路径自动转 base64）
generate_video_kling(prompt="让照片里的猫转头看向镜头", image_urls=["/path/to/cat.jpg"])

# 首尾帧锚定（多图时最后一张作尾帧）
generate_video_kling(
  prompt="镜头从窗外缓慢推入室内，光线渐变",
  image_urls=["/path/to/first.jpg", "/path/to/last.jpg"],
)

# 视频生视频（参考视频，最多 1 段，时长上限 10s）
generate_video_kling(
  prompt="把视频里的白天改成黄昏，运镜不变",
  video_urls=["https://example.com/clip.mp4"],
  ratio="9:16",
  duration=8,
)

# 关闭原生音频
generate_video_kling(prompt="海浪拍打礁石，长镜头", generate_audio=false)
```

## 提示词写作

提示词按「主体 → 动作 → 环境 → 运镜 → 风格」组织，越具体越好（完整的五个维度与示例见 `docs/generate_video_seedance.md` 的「提示词写作」一节，三个厂商通用）：

```
# 弱
"一只猫在弹钢琴"

# 强
"一只毛茸茸的橘猫坐在三角钢琴前，前爪按动琴键，特写镜头，暖黄色台灯打光，
 浅景深，电影感，节奏舒缓"
```

> 通用提示词手法（图生图怎么写、否定式表述、画幅、文字渲染、安全边界）见 `media-generation-craft` 技能目录下的 `references/prompt-engineering.md`。

## 多片段拼接工作流

生成长视频时拆分片段、逐段生成再拼接：每个片段单独调用 `generate_video_kling`（保持角色 / 场景一致），全部片段生成后用 FFmpeg concat 合并。完整步骤见 `docs/generate_video_seedance.md` 的「多片段拼接工作流」（三个厂商通用）。

## 厂商注意事项（可灵）

- **密钥 / 启用**：在「模型厂商」页添加厂商 `kling`，填写密钥为 `AccessKey:SecretKey`（冒号分隔，工具自动生成 HS256 JWT 签名）、控制台新建的单个 API Key、或中转网关的静态 token。也可在 config 的 `tools.kling_video.apiKey` 显式配置。配置了上述任一密钥即启用本工具。
- **base URL**：官方默认 `https://api-beijing.klingai.com`（中国大陆新域名；海外用 `api-singapore.klingai.com`；旧域名 `api.klingai.com` 会 401）。「模型厂商」页的 apiBase 留空即用默认。
- **模型名内嵌在 URL 路径**：如 `POST /image-to-video/kling-3.0`。可灵官方**没有 `/models` 枚举接口**，下拉会直接给出内置模型列表（`kling-3.0` / `kling-3.0-pro` / `kling-3.0-turbo` / `kling-v3-omni` / `kling-video-o1` 等）。
- **文生视频的提示词在顶层 `prompt`**（官方不接受 `contents` 里的提示词，会报 1201 `prompt cannot be empty`）；图 / 视频生视频才用 `contents`。
- **轮询路径按任务类型区分**：`GET /v1/videos/text2video/{task_id}`（文生）、`/v1/videos/image2video/{task_id}`（图生）、`/v1/videos/video2video/{task_id}`（参考视频）；统一 `GET /v1/videos/{task_id}` 对 3.0 任务返回 404。工具已自动派生正确路径。
- **参数差异**：可灵**不支持**随机种子 / 参考音频 / 参考图（风格）/ 水印，传入会被忽略并记日志；画幅**仅 16:9 / 9:16 / 1:1**，超出的值自动丢弃；时长 3–15（带参考视频 3–10），超出自动截断。
- **参考视频只接受公网 HTTP(S) URL**，不支持本地文件，且每次任务最多 1 段。
- **耗时较长**（通常数分钟）工具内部会自动轮询直至完成；若提示超时，可调大 config 的 `tools.kling_video.maxPollAttempts`。
- 结果会下载到媒体目录并持久化，返回本地 `path`；可用该路径作为后续剪辑工具的输入，或通过 message 工具交付给用户。
- 参数校验失败会以 `Error: ...` 文本返回而**不抛异常**，按提示调整参数后重试即可。
