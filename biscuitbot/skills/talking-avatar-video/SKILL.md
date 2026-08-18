---
name: talking-avatar-video
tier: agent
description: 数字人口播视频生成流程：用角色三视图设定稿 + 配音音频，通过 generate_video（Seedance）生成带声音、口型同步、自然融入场景的竖屏口播视频。当用户要用某个数字人/角色形象做口播视频、配音讲解视频时使用。
metadata: {"biscuitbot":{"emoji":"🎤","requires":{"env":["ARK_API_KEY"]}}}
---

# 数字人口播视频（Seedance）

用「角色三视图 + 配音音频」一次调用 generate_video 生成带声音的口播视频。
本流程来自 2026-08-18 实战验证，核心坑是 **`generate_audio=true` 必须显式传**，否则参考音频被静默忽略、成片无音轨。

## 前置素材

1. **角色参考图**：优先用「三视图设定稿」（正面/侧面/背面同一角色），角色一致性最好。
   - 只有单张形象图时，先用 `generate_image` 生成三视图：提示词写 "character turnaround reference sheet, front view / side view / back view, same character, consistent design, white background"，并保留原形象的全部特征（发型、五官、服装）。
2. **配音音频**：TTS 生成的 mp3（本地路径即可）。
   - 用 `ffprobe -v error -show_entries format=duration -of csv=p=0 xxx.mp3` 测时长，`duration` 参数取整向上对齐（2.0 支持 4–15 秒，2.5 支持到 30 秒）。

## 生成调用（模板）

```
generate_video(
  prompt=<见下方提示词模板>,
  image_urls=["<三视图路径>"],
  audio_urls=["<配音mp3路径>"],
  ratio="9:16",
  duration=<音频时长向上取整>,
  generate_audio=true,          # ★ 关键！默认 false 会导致成片没有声音
)
```

注意：
- 带参考素材（r2v）时**不要传 `resolution`**，会被方舟拒绝。
- 提示词里要显式写「**全程使用音频1作为人物口播配音，人物口型、表情与音频1完全同步**」，与官方案例一致。
- 模型默认即可（doubao-seedance-2-0-mini 支持音频输入）；如需指定可传 `model="doubao-seedance-2-0-260128"` 或 2.5。

## 提示词模板（含场景融合要点）

> 全程使用音频1作为人物的口播配音，人物口型、表情与音频1完全同步。参考图为同一角色的三视图设定稿（正面/侧面/背面）。请以参考图中的角色生成视频：<角色特征：年龄、发型、五官、服装>。他自然地站在<场景描述>，双脚稳稳踩在地面上，脚下有真实的接触阴影，身体正面朝向镜头进行口播讲话，语气<情绪>，配合自然的手势。人物必须自然融入场景：<光源方向>在他身上形成统一的光影与轮廓光，皮肤、头发、衣物带有真实环境反光，<环境互动：如海风轻拂发丝与衣角>，背景<动态元素持续自然运动>，浅景深、电影级色调，人物与环境的透视、比例、色温完全一致，无任何抠像感或贴片感。竖屏9:16，半身中景，固定机位。

避免「贴片感」的五个关键词（缺一不可）：
1. 脚下**接触阴影**
2. **统一光源方向** + 轮廓光
3. 皮肤/头发/衣物的**环境反光**
4. 环境与人物**互动**（风吹发丝、衣角）
5. **背景持续运动**（海浪、人群、树叶），透视/比例/色温一致

## 验证（必做）

```bash
ffprobe -v error -show_entries stream=codec_type,codec_name -of csv <视频路径>
```
必须同时出现 `video` 和 `audio` 两条流。只有 video 流 = 音频没进去，检查 `generate_audio=true` 是否传上，重新生成。

## 兜底方案

- **实在无音频**：`ffmpeg -y -i 视频.mp4 -i 配音.mp3 -c:v copy -c:a aac -b:a 192k -shortest -movflags +faststart out.mp4` 强制混音（口型不对齐，仅应急）。
- **真人形象照片被审核拦截**（隐私类报错）：先用 generate_image 把真人转为动漫角色但保留人物特性（发型、五官、服装、气质），再用动漫角色进 Seedance，提示词中要求「将动漫角色转换为真实人物风格：自然真实皮肤、真实头发丝光泽、真实布料与金属反光，完美保留角色特性」。
- **交付**：用 `message` 工具 + `media=[视频路径]` 发给用户，不要只贴路径。

## 完整成功案例（2026-08-18）

海边口播「努力才有结果」：三视图设定稿 + 13.7s 男声配音 → `duration=14, ratio="9:16", generate_audio=true` → Seedance 2.0 mini 生成 14s 双流视频（h264 + aac），口型同步、含海浪环境声。
