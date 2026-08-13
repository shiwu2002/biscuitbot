---
name: seedance
tier: user
description: Seedance 视频生成的提示词技巧与工作流指南（配合 generate_video 工具使用）。当需要为视频生成编写高质量提示词、规划分镜，或指导用户迭代优化视频时使用。真正生成视频请调用 generate_video 工具，不要写脚本或 exec。
metadata: {"biscuitbot":{"emoji":"🎬","requires":{"env":["ARK_API_KEY"]}}}
---

# Seedance 视频生成指南

本技能只提供**提示词技巧与工作流建议**；真正生成/编辑视频请直接调用 `generate_video` 工具（一次函数调用完成创建任务、轮询、下载）。

> ⚠️ 不要写 Python 脚本、不要用 exec、不要手动轮询——`generate_video` 工具已内置全部逻辑。

## 调用工具

```
generate_video(prompt="…", image_urls=[…], video_urls=[…], audio_urls=[…], ratio=…, duration=…, resolution=…, generate_audio=…, watermark=…)
```

- **文本输入** `prompt`（必填）：描述主体、运镜、景别、构图、光影、氛围、节奏。
- **图片输入** `image_urls`：本地路径或公网 URL（本地自动转 base64）。
- **视频输入** `video_urls`：仅公网 HTTP(S) URL（不支持本地文件）。
- **音频输入** `audio_urls`：本地路径或公网 URL。
- 模型支持 2.0（`doubao-seedance-2-0-260128`）与 2.5（`doubao-seedance-2-5-260628`，默认，支持到 30 秒、4K、多模态参考）。

## 提示词技巧

写提示词时按「主体 → 动作 → 环境 → 运镜 → 风格」组织，越具体越好：

- **主体**：谁/什么（外貌、材质、数量）。
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

## 工作流建议

1. 把用户的一句话需求**精化为完整视频方案**（主题、分镜、镜头描述、光影、节奏），再调用工具。
2. 文生视频：只传 `prompt`（+ `ratio`/`duration`）。
3. 图生视频：传 `image_urls`（首帧 / 参考画面）。
4. 参考图 + 参考视频编辑：同时传 `image_urls` 与 `video_urls`。
5. 拿到返回的本地视频路径后，可交给剪辑工具（如 jianying-editor）或通过 message 工具交付给用户。
6. 迭代优化：基于上一次结果调整提示词，逐次逼近效果。

## 常见问题

| 问题 | 处理 |
|---|---|
| 未配置密钥 | 设置 `ARK_API_KEY` 环境变量，或 config 的 `tools.seedance_video.apiKey` |
| 工具未启用 | config 设置 `tools.seedance_video.enabled = true` |
| 参考视频用本地文件 | 视频输入仅支持公网 URL，本地文件需先上传 |
| 生成过慢 | 属正常现象，工具会自动轮询直至完成 |
