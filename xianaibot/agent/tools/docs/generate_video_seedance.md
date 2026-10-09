# generate_video_seedance

通过火山引擎方舟（Volcengine Ark）Seedance 视频大模型生成或编辑视频。支持文生视频、图生视频、以及基于参考图 / 参考视频 / 参考音频的编辑，任务异步执行，完成后返回下载到本地的视频文件路径。

> 本工具是三家视频生成工具中**默认推荐**的一家（火山方舟 Seedance）。另有 `generate_video_kling`（可灵）与 `generate_video_minimax`（MiniMax H3）；三家工具自包含、各自独立启用。

## 何时使用

用户要求生成一段视频、把图片变成视频、或基于参考素材编辑视频时使用。本工具一次调用即可完成「创建任务 → 轮询 → 下载」，无需写脚本或 exec。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、运镜、景别、构图、光影、氛围、节奏 |
| image_urls | array | 否 | - | 参考图列表（本地路径 / 公网 URL / base64 data URL，本地自动转 base64）。**微信/渠道收到的图片本地路径可直接传入，无需上传公网**。**首个元素可作为首帧参考**（片段连贯：把上一段视频尾帧放首位） |
| video_urls | array | 否 | - | 参考视频列表（**仅公网 HTTP(S) URL**，不支持本地文件）。传入即进入 r2v（参考生视频）模式 |
| audio_urls | array | 否 | - | 参考音频列表（本地路径 / 公网 URL / base64 data URL）。用于音画同步 / 口型参考 |
| ratio | string | 否 | 16:9 | 画幅：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive |
| duration | integer | 否 | 5 | 时长（秒），2.0 支持 4–15，2.5 支持到 30 |
| resolution | string | 否 | - | 清晰度：480p / 720p / 1080p / 4K（4K 仅 2.5）。**仅文生/图生视频可用**；带参考素材（r2v）时模型按参考素材自动推导分辨率，**勿传**（传了会被工具自动忽略） |
| generate_audio | boolean | 否 | true | 是否开启音画同步生成音频（**默认开启**：未传参时自动打开音效，传 `false` 可关闭） |
| seed | integer | 否 | -1（随机） | 随机种子；固定 seed 可复现/微调结果，范围 -1 ~ 2^32-1 |
| watermark | boolean | 否 | false | 是否添加水印（默认关闭 = 去水印） |
| model | string | 否 | 配置默认 | 模型覆盖：doubao-seedance-2-5-260628 / doubao-seedance-2-0-260128 / doubao-seedance-2-0-fast-260128 / doubao-seedance-2-0-mini-260615 / Endpoint ID（ep-...） |

## 调用示例

```
# 文生视频
generate_video_seedance(prompt="一只橘猫在钢琴前弹奏，特写镜头，电影感")

# 图生视频（参考图，本地路径自动转 base64）
generate_video_seedance(prompt="让照片里的猫转头看向镜头", image_urls=["/path/to/cat.jpg"])

# 多模态编辑（参考图 + 参考视频 + 参考音频）
generate_video_seedance(
  prompt="将视频中的香水替换成图片里的面霜，运镜不变",
  image_urls=["/path/to/cream.jpg"],
  video_urls=["https://example.com/gift.mp4"],
  audio_urls=["/path/to/voice.mp3"],
  ratio="9:16",
  duration=8,
)

# 首帧指定（image_urls 首个元素作为首帧参考）
generate_video_seedance(
  prompt="镜头从窗外缓慢推入室内，光线渐变",
  image_urls=["/path/to/first.jpg"],
)

# 固定随机种子复现结果
generate_video_seedance(prompt="海浪拍打礁石，长镜头", seed=12345, duration=10)
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

> 通用提示词手法（图生图怎么写、否定式表述、画幅、文字渲染、安全边界）见 `media-generation-craft` 技能目录下的 `references/prompt-engineering.md`；本节的写法同样适用于可灵与 MiniMax H3。

## 模型版本

- **默认推荐**：`doubao-seedance-2-0-mini-260615`（稳定版，版权拦截最少）。
- **Seedance 2.5**（`doubao-seedance-2-5-260628`）能力更强（支持 4K、时长到 30s），但更易触发隐私 / 版权策略拦截（HTTP 400）；确有 4K / 长时长需求时再显式指定。
- 若遇 HTTP 400，优先检查提示词是否包含敏感关键词（品牌名、人物名、具体商标），简化提示词可绕过限制。

## 多片段拼接工作流

生成长视频时：

1. 先完成故事 / 分镜规划。
2. 将长视频拆分为约 10 秒的独立片段，按场景顺序编号。
3. 每个片段单独调用 `generate_video_seedance`，保持角色 / 场景资产一致性；片段间通过共享关键视觉元素（服装、背景、色调）衔接。**让片段首尾连贯**：把上一段的尾帧作为下一段 `image_urls` 的首个元素（`image_urls` 的第一个元素会被当作首帧参考）。
4. 全部片段生成后，用 FFmpeg concat 合并：

```bash
echo "file 'clip_1.mp4'" > list.txt
echo "file 'clip_2.mp4'" >> list.txt
echo "file 'clip_3.mp4'" >> list.txt
echo "file 'clip_4.mp4'" >> list.txt
ffmpeg -f concat -safe 0 -i list.txt -c copy output.mp4
```

## 厂商注意事项（火山方舟 Seedance）

- **密钥 / 启用**：在「模型厂商」页配置火山方舟（volcengine）密钥即启用本工具。密钥也可来自 config 的 `tools.seedance_video.apiKey` 或环境变量 `ARK_API_KEY`。**只要配置了上述任一密钥，本工具即可被智能体调用**。
- **base URL**：「模型厂商」页 volcengine 厂商的 apiBase 优先，留空用官方默认 `https://ark.cn-beijing.volces.com/api/v3`。
- **r2v 模式不要传 `resolution`**：带参考图 / 参考视频 / 参考音频（r2v）时，模型按参考素材自动推导分辨率，显式传入会被方舟拒绝（`not valid ... in r2v`）。**工具已自动忽略该参数**，无需手动规避。这一点与 MiniMax H3 相反（H3 各模式都必须下发清晰度）。
- **参考视频只接受公网可访问的 HTTP(S) URL**；本地视频文件需先上传到可访问地址。
- **参考图片 / 音频支持本地路径**（自动 base64）与公网 URL；**微信/渠道收到的图片本地路径可直接传入，无需先上传到公网图床**。
- **音效默认开启**：未显式传 `generate_audio` 时自动打开音效；带参考视频 / 参考音频时始终开启；显式传 `false` 才会关闭。
- **模型名回退**：若配置里残留其他厂商模型名（切厂商未切模型），工具会自动回退为默认 Seedance 模型。
- **耗时较长**（通常数分钟）工具内部会自动轮询直至完成；若提示超时，可调大 config 的 `tools.seedance_video.maxPollAttempts`。
- 结果会下载到媒体目录并持久化，返回本地 `path`；可用该路径作为后续剪辑工具的输入，或通过 message 工具交付给用户。
- 参数校验失败（互斥、超限、体积过大等）会以 `Error: ...` 文本返回而**不抛异常**，按提示调整参数后重试即可。
