# generate_video_dashscope

通过阿里云百炼 / 灵积（DashScope）的**通义万相**（Tongyi Wanxiang）视频大模型生成视频。支持文生视频与图生视频（单张首帧）。任务异步执行，完成后返回下载到本地的视频文件路径。

> **模式按输入自动判定**：传了 `image_urls` → 图生视频（取第 1 张作首帧 `img_url`）；否则 → 文生视频。通义万相图生视频**只收单张图片**，多镜头请分次生成。

## 何时使用

用户要求生成一段视频、或把一张图片变成视频时使用。可作为火山方舟 Seedance、可灵、MiniMax 之外的备选厂商。本工具一次调用即可完成「提交任务 → 轮询 → 取 `video_url` → 下载落盘」，无需写脚本或 exec。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| prompt | string | 是 | - | 文本提示词：描述主体、动作、运镜、景别、构图、光影与氛围 |
| image_urls | array | 否 | - | 首帧图片（图生视频）：本地路径 / 公网 URL / base64 data URL（本地自动转 base64）。**只支持单张**，传多张会报错 |
| ratio | string | 否 | 16:9 | 画幅比例：16:9 / 9:16 / 1:1（其他取值回退 16:9） |
| duration | integer | 否 | 5 | 时长（秒）：wan2.6 系列 2–15；wan2.5 系列只支持 5 / 10（就近取档）；wan2.2 系列固定 5 秒 |
| resolution | string | 否 | 720P | 清晰度：480P / 720P / 1080P（1080p、4K 归入 1080P） |
| seed | integer | 否 | - | 随机种子；不传则随机。也可在设置页配置默认值 |
| model | string | 否 | wan2.6-t2v | 模型覆盖。常用：wan2.6-t2v、wan2.6-i2v、wan2.6-i2v-flash、wan2.5-t2v-preview、wan2.5-i2v-preview、wan2.2-t2v-plus、wan2.2-i2v-plus、wan2.2-i2v-flash |

## 调用示例

```
# 文生视频
generate_video_dashscope(prompt="一只橘猫在钢琴前弹奏，特写镜头，电影感", ratio="16:9", duration=5)

# 图生视频（单张首帧，本地图片自动转 base64）
generate_video_dashscope(
  prompt="镜头从窗外缓慢推入室内，光线渐变",
  image_urls=["/path/to/first.jpg"],
)

# 竖屏短视频 + 固定随机种子（便于复现）
generate_video_dashscope(prompt="雨夜霓虹街道，慢速环绕运镜", ratio="9:16", resolution="1080P", seed=42)
```

## 注意事项

- **异步任务式**：提交必须带 `X-DashScope-Async: enable`（接口不支持同步），随后轮询 `/api/v1/tasks/{task_id}` 直到 `SUCCEEDED` 取 `output.video_url`。默认轮询约 30 分钟，可在设置页调整 `maxPollAttempts` / `pollIntervalSec`。
- **base URL 会自动归一化**：「模型厂商」页 dashscope 的 apiBase 是 LLM 兼容模式地址（`.../compatible-mode/v1`），本工具会自动剥回裸域再拼原生 `/api/v1/...` 路径，因此同一个 apiBase 既能聊天也能生视频。
- **清晰度与比例映射为像素对**：DashScope 用 `size`（`"宽*高"`）。本工具按「清晰度 + 比例」查表得到像素对（如 720P + 16:9 → `1280*720`）；未收录的组合回退到该清晰度的 16:9，不臆造尺寸。
- **参考视频**（如需）仅支持公网 HTTP(S) URL，不支持本地文件。
- 提示词扩写（`parameters.prompt_extend`）与水印（`parameters.watermark`）在设置页配置，默认「自动扩写开、水印关」。
