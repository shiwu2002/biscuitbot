---
name: seedance
tier: user
description: 使用火山引擎方舟（Volcengine Ark）Seedance 2.0 视频大模型生成或编辑视频：文生视频、图生视频、参考图+参考视频编辑，输出可下载的视频 URL。当用户要求生成一段视频、把图片变成视频、或基于参考素材编辑视频时使用。
metadata: {"biscuitbot":{"emoji":"🎬","requires":{"env":["ARK_API_KEY"],"pkgs":["volcengine-python-sdk[ark]"]}}}
---

# Seedance 2.0 视频生成 / 编辑

通过火山引擎方舟（Volcengine Ark）Seedance 2.0 视频大模型生成或编辑视频。模型根据文本提示词（可选参考图 / 参考视频 / 参考音频）创作一段新视频，任务异步执行，完成后返回视频 URL。

## 前置条件

1. **API Key**：环境变量 `ARK_API_KEY`。用户已配置密钥 `ark-<REDACTED>`，使用前先 `export ARK_API_KEY=ark-<REDACTED>`。未设置该变量时技能不可用（依赖校验会提示 `ENV: ARK_API_KEY`）。
2. **SDK**：`volcengine-python-sdk[ark]`（导入名仍为 `volcenginesdkarkruntime`），首次使用时安装：
   ```bash
   pip install "volcengine-python-sdk[ark]"
   ```
3. **模型开通**：`doubao-seedance-2-0-260128` 需在方舟控制台开通，并可能需要公测权限。

## 工作流（3 步）

1. **创建任务**：`client.content_generation.tasks.create(model=..., content=[...], ratio=..., duration=..., generate_audio=..., watermark=...)` 返回 `task_id`。
2. **轮询状态**：`client.content_generation.tasks.get(task_id=...)` 直到 `status == "succeeded"`（或 `"failed"`），每 30 秒查询一次。
3. **取结果**：`result.content.video_url` 即为生成的视频地址，可下载或预览。

`content` 数组支持以下条目（按需组合）：

| 类型 | 写法 | role |
|---|---|---|
| 文本（必填） | `{"type":"text","text":"..."}` | — |
| 参考图 | `{"type":"image_url","image_url":{"url":"..."},"role":"reference_image"}` | `reference_image` |
| 参考视频 | `{"type":"video_url","video_url":{"url":"..."},"role":"reference_video"}` | `reference_video` |
| 参考音频 | `{"type":"audio_url","audio_url":{"url":"..."},"role":"reference_audio"}` | `reference_audio` |

## 一键脚本

把下面的脚本写入 `/tmp/seedance_video.py` 后运行。脚本参数全部来自命令行，无需改代码：

```bash
cat > /tmp/seedance_video.py <<'PY'
#!/usr/bin/env python3
"""Seedance 2.0 视频生成/编辑：文生视频、图生视频、参考图+参考视频编辑。

stdout 仅输出最终视频 URL；进度与错误走 stderr。退出码：0=成功 1=任务失败 2=配置错误。
依赖：pip install "volcengine-python-sdk[ark]"；环境变量 ARK_API_KEY。
"""
import argparse
import os
import sys
import time

from volcenginesdkarkruntime import Ark


def build_content(args: argparse.Namespace) -> list[dict]:
    content: list[dict] = [{"type": "text", "text": args.prompt}]
    if args.image:
        content.append(
            {"type": "image_url", "image_url": {"url": args.image}, "role": "reference_image"}
        )
    if args.video:
        content.append(
            {"type": "video_url", "video_url": {"url": args.video}, "role": "reference_video"}
        )
    if args.audio:
        content.append(
            {"type": "audio_url", "audio_url": {"url": args.audio}, "role": "reference_audio"}
        )
    return content


def main() -> None:
    parser = argparse.ArgumentParser(description="Seedance 2.0 视频生成/编辑")
    parser.add_argument("prompt", help="文本提示词")
    parser.add_argument("--image", help="参考图片 URL（可选）")
    parser.add_argument("--video", help="参考视频 URL（可选）")
    parser.add_argument("--audio", help="参考音频 URL（可选）")
    parser.add_argument("--ratio", default="16:9", help="画幅比例：16:9 / 9:16 / 1:1 / 4:3 / 3:4 / 21:9 / adaptive")
    parser.add_argument("--duration", type=int, default=5, help="视频时长（秒），4-15")
    parser.add_argument("--generate-audio", action="store_true", help="开启音画同步生成音频")
    parser.add_argument("--no-watermark", action="store_true", help="关闭水印")
    parser.add_argument("--model", default="doubao-seedance-2-0-260128", help="模型 ID")
    args = parser.parse_args()

    api_key = os.environ.get("ARK_API_KEY")
    if not api_key:
        print("错误：未设置环境变量 ARK_API_KEY，请先 export ARK_API_KEY=...", file=sys.stderr)
        sys.exit(2)

    client = Ark(api_key=api_key)
    create_result = client.content_generation.tasks.create(
        model=args.model,
        content=build_content(args),
        generate_audio=args.generate_audio,
        ratio=args.ratio,
        duration=args.duration,
        watermark=not args.no_watermark,
    )
    task_id = create_result.id
    print(f"[seedance] 任务已创建：{task_id}", file=sys.stderr)

    while True:
        result = client.content_generation.tasks.get(task_id=task_id)
        status = result.status
        if status == "succeeded":
            print(result.content.video_url)
            sys.exit(0)
        if status == "failed":
            print(f"任务失败：{result.error}", file=sys.stderr)
            sys.exit(1)
        print(f"[seedance] 状态 {status}，30 秒后重试...", file=sys.stderr)
        time.sleep(30)


if __name__ == "__main__":
    main()
PY
python3 /tmp/seedance_video.py "你的提示词"
```

### 用法

```bash
# 文生视频
python3 /tmp/seedance_video.py "一只橘猫在钢琴前弹奏，特写镜头，电影感"

# 图生视频（参考图）
python3 /tmp/seedance_video.py "让照片里的猫转头看向镜头" --image https://example.com/cat.jpg

# 视频编辑（参考图 + 参考视频）
python3 /tmp/seedance_video.py "将视频1礼盒中的香水替换成图片1中的面霜，运镜不变" \
  --image https://example.com/cream.jpg \
  --video https://example.com/gift.mp4
```

脚本 stdout 只输出最终视频 URL；进度打印到 stderr。退出码：0 成功，1 任务失败，2 配置错误。

### 参数

| 参数 | 说明 | 默认 |
|---|---|---|
| `prompt` | 文本提示词（必填） | — |
| `--image URL` | 参考图（可公开访问 URL） | 无 |
| `--video URL` | 参考视频（可公开访问 URL） | 无 |
| `--audio URL` | 参考音频 | 无 |
| `--ratio` | 画幅：`16:9` / `9:16` / `1:1` / `4:3` / `3:4` / `21:9` / `adaptive` | `16:9` |
| `--duration N` | 视频时长（秒），Seedance 2.0 支持 4–15 秒 | `5` |
| `--generate-audio` | 开启音画同步生成音频 | 关闭 |
| `--no-watermark` | 关闭水印 | 开启 |
| `--model` | 模型 ID（已开通也可用 Endpoint ID `ep-...`） | `doubao-seedance-2-0-260128` |

## 常见问题

| 问题 | 处理 |
|---|---|
| `ARK_API_KEY` 未设置 | 先 `export ARK_API_KEY=...` 再运行；不要硬编码进脚本 |
| 任务失败（failed） | 查看 stderr 的 `result.error`；常见为未开通模型、无公测权限或提示词不合规 |
| 参考素材加载失败 | 素材必须为可公开访问的 HTTP(S) URL；本地文件可先 `python3 -m http.server 8000` 临时托管 |
| 生成过慢 | 视频任务通常需要数分钟；脚本每 30 秒轮询一次，耐心等待 |

## 注意事项

- **API Key 只从环境变量读取**，绝不写入脚本或日志。
- 生成的 `video_url` 有有效期，需要持久化时应及时用 `curl -o` 下载到本地。
- 提示词中描述主体、运镜、氛围、时长节奏越具体越好。
- 长视频（≥10 秒）耗时明显更长，建议默认 5 秒。
