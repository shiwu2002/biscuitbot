# generate_video

通过视频生成大模型生成或编辑视频，默认走火山引擎方舟（Volcengine Ark）Seedance，也可切换为可灵（Kling，默认模型 `kling-3.0`）或 MiniMax H3（默认模型 `MiniMax-H3`）。支持文生视频、图生视频、以及基于参考图 / 参考视频 / 参考音频的编辑，任务异步执行，完成后返回下载到本地的视频文件路径。

## 何时使用

用户要求生成一段视频、把图片变成视频、或基于参考素材编辑视频时使用。本工具一次调用即可完成「创建任务 → 轮询 → 下载」，无需写脚本或 exec。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、运镜、景别、构图、光影、氛围、节奏 |
| image_urls | array | 否 | - | 参考图列表（本地路径 / 公网 URL / base64 data URL，本地自动转 base64）。**微信/渠道收到的图片本地路径可直接传入，无需上传公网**。**首个元素可作为首帧参考**（片段连贯：把上一段视频尾帧放首位）。**MiniMax H3 下语义为「首帧 / 尾帧」**：1 张 = 首帧，2 张 = 首帧 + 尾帧；与 `reference_images` 互斥 |
| reference_images | array | 否 | - | **参考图**（主体的外观 / 风格参考，不是首尾帧）。**仅 MiniMax H3 使用**，火山方舟与可灵会忽略。最多 9 张；与 `image_urls` 互斥；本地路径自动转 base64 |
| video_urls | array | 否 | - | 参考视频列表（**仅公网 HTTP(S) URL**，不支持本地文件）。MiniMax H3 下为「参考视频」，最多 3 段 |
| audio_urls | array | 否 | - | 参考音频列表（本地路径 / 公网 URL / base64 data URL）。MiniMax H3 下为「参考音频」，最多 3 段且**不能单独使用**（需同时传 `reference_images` 或 `video_urls`） |
| ratio | string | 否 | 16:9 | 画幅：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive。**MiniMax H3**：文生视频必填且不接受 `adaptive`（缺省用 `16:9`）；图生视频由首帧推导、传了也被忽略；参考生视频可选 |
| duration | integer | 否 | 5 | 时长（秒），2.0 支持 4–15，2.5 支持到 30；**MiniMax H3 支持 4–15**，超出自动截断 |
| resolution | string | 否 | - | 清晰度：480p / 720p / 1080p / 4K（4K 仅 2.5）。**仅文生/图生视频可用**，带参考素材（r2v）时勿传。**MiniMax H3 例外：各模式都需要**，取值 768P / 2K（480p、720p 自动映射为 768P，1080p、4K 映射为 2K） |
| generate_audio | boolean | 否 | true | 是否开启音画同步生成音频（**默认开启**：未传参时自动打开音效，传 `false` 可关闭）。MiniMax H3 无此开关，忽略 |
| seed | integer | 否 | -1（随机） | 随机种子；固定 seed 可复现/微调结果，范围 -1 ~ 2^32-1。MiniMax H3 不支持，忽略 |
| watermark | boolean | 否 | false | 是否添加水印（默认关闭 = 去水印）。MiniMax H3 下对应后端的 `aigc_watermark` |
| model | string | 否 | 配置默认 | 模型覆盖：doubao-seedance-2-5-260628 / doubao-seedance-2-0-260128 / doubao-seedance-2-0-mini-260615 / Endpoint ID；MiniMax H3 为 `MiniMax-H3` |

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

# MiniMax H3：首尾帧指定（image_urls 在此语义为首帧 / 尾帧）
generate_video(prompt="镜头从窗外缓慢推入室内，光线渐变", image_urls=["/path/to/first.jpg", "/path/to/last.jpg"])

# MiniMax H3：多模态参考（参考图 + 参考视频 + 参考音频，三者需搭配使用）
generate_video(
  prompt="用参考图中的角色在参考视频的运镜节奏下表演，配合参考音频的口型",
  reference_images=["/path/to/char.jpg"],
  video_urls=["https://example.com/motion.mp4"],
  audio_urls=["/path/to/voice.mp3"],
)
```

## 提示词写作

提示词按「主体 → 动作 → 环境 → 运镜 → 风格」组织，越具体越好：

- **主体**：谁 / 什么（外貌、材质、数量）。
- **动作**：在做什么、如何运动（快慢、幅度、轨迹）。
- **环境**：时代、地点、时间、光线、天气、氛围。
- **运镜**：镜头语言（特写/全景、推拉摇移、跟随、手持、无人机）。
- **风格**：写实 / 电影感 / 动漫 / 3D / 纪录片等，可加参考画质词（如「浅景深」「赛博朋克色调」）。

示例对比：

```
# 弱
"一只猫在弹钢琴"

# 强
"一只毛茸茸的橘猫坐在三角钢琴前，前爪按动琴键，特写镜头，暖黄色台灯打光，
 浅景深，电影感，节奏舒缓"
```

## 模型版本

- **默认推荐**：`doubao-seedance-2-0-mini-260615`（稳定版，版权拦截最少）。
- **Seedance 2.5**（`doubao-seedance-2-5-260628`）能力更强（支持 4K、时长到 30s），但更易触发隐私 / 版权策略拦截（HTTP 400）；确有 4K / 长时长需求时再显式指定。
- 若遇 HTTP 400，优先检查提示词是否包含敏感关键词（品牌名、人物名、具体商标），简化提示词可绕过限制。

## 多片段拼接工作流

生成长视频时：

1. 先完成故事 / 分镜规划。
2. 将长视频拆分为约 10 秒的独立片段，按场景顺序编号。
3. 每个片段单独调用 `generate_video`，保持角色 / 场景资产一致性；片段间通过共享关键视觉元素（服装、背景、色调）衔接。
4. 全部片段生成后，用 FFmpeg concat 合并：

```bash
echo "file 'clip_1.mp4'" > list.txt
echo "file 'clip_2.mp4'" >> list.txt
echo "file 'clip_3.mp4'" >> list.txt
echo "file 'clip_4.mp4'" >> list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4
```

## 厂商选择（Seedance / 可灵 / MiniMax H3）

默认厂商为火山方舟 Seedance（`provider = "volcengine"`）。可在「模型厂商」页配置**可灵**（`provider = "kling"`）后切换：

- 在「模型厂商」页添加厂商 `kling`，填写密钥为 `AccessKey:SecretKey`（冒号分隔，工具自动生成 JWT 签名）、控制台新建的单个 API Key、或中转网关的静态 token。
- 在「视频生成」设置页把厂商切到「可灵」，模型填 `kling-3.0`（默认，或你的可灵模型 ID）。可灵官方**没有 `/models` 枚举接口**，下拉会直接给出内置模型列表（`kling-3.0` / `kling-3.0-pro` / `kling-3.0-turbo` / `kling-v3-omni` / `kling-video-o1` 等）。
- 可灵官方 API 默认 `https://api-beijing.klingai.com`（中国大陆新域名；海外用 `api-singapore.klingai.com`；旧域名 `api.klingai.com` 会 401）。「模型厂商」页的 apiBase 留空即用默认。
- 请求走可灵 3.0 的 `settings` + `options` + （`contents` 或顶层 `prompt`）结构，模型名内嵌在 URL 路径（如 `POST /image-to-video/kling-3.0`）。**文生视频的提示词在顶层 `prompt`**（官方不接受 `contents` 里的提示词，会报 1201 `prompt cannot be empty`）；图/视频生视频才用 `contents`。
- 轮询路径按任务类型区分：`GET /v1/videos/text2video/{task_id}`（文生）、`/v1/videos/image2video/{task_id}`（图生）、`/v1/videos/video2video/{task_id}`（参考视频）；统一 `GET /v1/videos/{task_id}` 对 3.0 任务返回 404。

### MiniMax H3

用 `provider = "minimax"` 走 MiniMax H3：

- 在「模型厂商」页添加厂商 `minimax`，填写 API Key。H3 与 MiniMax 聊天接口**共用同一个静态 Key**（`Authorization: Bearer`，不需要可灵那样的 JWT 签名）。
- 官方 API 默认 `https://api.minimaxi.com`（中国大陆；海外用 `api.minimax.io`）。**apiBase 留空即用默认**；若把它同时当 LLM 厂商用，填了带 `/v1` 的聊天式地址也没关系，工具会自动归一化到裸域。
- 模型名大小写敏感，必须精确为 `MiniMax-H3`。若配置里残留的是 Seedance 模型名（切厂商未切模型），工具会自动回退为 `MiniMax-H3`。
- 请求走 `POST /v2/video_generation` 的多模态 `content[]` 数组（`text` / `image_url` / `video_url` / `audio_url`，媒体项各带角色）。**模式按输入自动判定**：有参考素材 → 多模态参考生视频（r2va）；有 `image_urls` → 图生视频（i2va，首帧 / 尾帧）；否则 → 文生视频（t2va）。
- 任务成功响应返回的是 `file_id` 而非视频地址，工具会自动再调一次文件接口换取下载地址；轮询端点与状态词表有多个文档版本，工具内置了自动探测与兼容，无需手动指定。

### 参数差异对照

| 能力 / 参数 | 可灵（kling） | MiniMax H3（minimax） | 备注 |
|------|------|------|------|
| ratio | 仅 `16:9` / `9:16` / `1:1` | `adaptive`（仅 r2va）/ `21:9` / `16:9` / `4:3` / `1:1` / `3:4` / `9:16` | 可灵：超出值域自动丢弃；H3：t2va 必填且不接受 adaptive（缺省用 16:9），i2va 忽略 |
| duration | 3–15s（带参考视频 3–10s） | 4–15s | 超出自动截断到边界 |
| resolution | `720p` / `1080p` / `4k` | `768P` / `2K` | 可灵透传 `settings.resolution`（转小写）；H3 各模式都需下发，工具词表自动就近映射 |
| seed | 不支持 | 不支持 | 忽略并记日志 |
| watermark | 不支持 | 支持，映射为 `aigc_watermark` | 两者默认都是关闭 |
| audio_urls（参考音频） | 不支持 | 支持，最多 3 段 | H3 下不能单独使用，须搭配 `reference_images` 或 `video_urls`；可灵忽略 |
| image_urls | 本地路径 / 公网 URL / base64 data URL | 语义为「首帧 / 尾帧」，最多 2 张 | 本地路径自动转 base64；H3 下与 `reference_images` 互斥 |
| reference_images（参考图） | 不支持（忽略并记日志） | 支持，最多 9 张 | 仅 H3 使用；方舟与可灵都会忽略 |
| video_urls | 最多 1 段参考视频 | 最多 3 段参考视频 | 仅公网 HTTP(S) URL，不支持本地文件 |
| generate_audio | `settings.audio`（`native` / `off`） | 无对应参数 | 可灵默认开启（同 Seedance）；H3 忽略 |
| 素材总量 | - | 素材文件合计 ≤12，请求体 ≤64MB | 仅 H3 限制；本地文件转 base64 后体积约膨胀 1/3 |

## 注意事项

- 参考**视频**只接受公网可访问的 HTTP(S) URL；本地视频文件需先上传到可访问地址（三个厂商均如此）。
- **方舟（Seedance）**带参考图 / 参考视频 / 参考音频（r2v）时**不要传 `resolution`**：该模式下模型按参考素材自动推导分辨率，显式传入会被方舟拒绝（`not valid ... in r2v`）。工具已自动忽略该参数。**MiniMax H3 相反**：各模式都必须下发清晰度。
- **MiniMax H3** 的 `image_urls`（首帧 / 尾帧）与 `reference_images`（参考图）**互斥**，同传会直接报错；参考音频**不能单独使用**，须搭配参考图或参考视频。素材上限：参考图 ≤9、参考视频 ≤3、参考音频 ≤3、文件合计 ≤12，请求体 ≤64MB。
- 参考**图片 / 音频**支持本地路径（自动 base64）与公网 URL；**微信/渠道收到的图片本地路径可直接传入三个厂商，无需先上传到公网图床**。
- 视频生成耗时较长（通常数分钟；MiniMax H3 的 2K / 15 秒任务更久），工具内部会自动轮询直至完成。若提示超时，可调大 config 的 `tools.seedance_video.maxPollAttempts`。
- 结果会下载到媒体目录并持久化，返回本地 `path`；可用该路径作为后续剪辑工具的输入，或通过 message 工具交付给用户。
- 密钥：方舟可用环境变量 `ARK_API_KEY`（或 config 的 `tools.seedance_video.apiKey`）；可灵、MiniMax 请在「模型厂商」页配置对应厂商的密钥。均需 `tools.seedance_video.enabled` 为 true。
- 参数校验失败（互斥、超限、体积过大等）会以 `Error: ...` 文本返回而**不抛异常**，按提示调整参数后重试即可。
