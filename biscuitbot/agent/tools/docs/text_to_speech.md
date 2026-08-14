# text_to_speech

把文本合成为语音音频并保存为本地 mp3 文件，返回文件路径。支持三个 provider：
- **openai**（gpt-4o-mini-tts，默认音色 alloy，需 API Key）
- **dashscope**（cosyvoice-v2，默认音色 longxiaochun，需 API Key）
- **edge-tts**（免费、无需 API Key，默认音色 zh-CN-XiaoxiaoNeural）

未显式指定 provider 时使用配置 `config.tts.provider`（默认 edge-tts）。

## 何时使用

用户要求生成语音、给视频配音、朗读文案、把文字转成音频时使用。生成的音频
文件路径可作为后续剪辑工具（如 jianying-editor）的输入，或通过 message 工具
直接交付给用户。

## 参数

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| text | string | 是 | - | 要合成语音的文本内容 |
| provider | string | 否 | 配置默认 | 服务商：openai / dashscope / edge-tts |
| voice | string | 否 | provider 默认 | 音色（OpenAI alloy；DashScope longxiaochun；edge-tts zh-CN-XiaoxiaoNeural） |
| rate | string | 否 | - | 语速，如 `+10%` 加快、`-20%` 减慢 |
| model | string | 否 | provider 默认 | 模型覆盖（gpt-4o-mini-tts / cosyvoice-v2） |
| output_path | string | 否 | 工作区 generated/tts/ | 保存音频的文件路径 |

## 调用示例

```
# 用默认 provider（edge-tts）合成中文语音
text_to_speech(text="欢迎使用 BiscuitBot，今天给大家带来一条产品宣传片。")

# 指定音色与语速，加快 10%
text_to_speech(
  text="这是我们的最新功能，上手零门槛。",
  voice="zh-CN-XiaoxiaoNeural",
  rate="+10%",
)

# 切换 OpenAI 服务商并指定输出路径（供后续剪辑使用）
text_to_speech(
  text="This is the official launch trailer.",
  provider="openai",
  voice="nova",
  output_path="generated/tts/trailer_vo.mp3",
)
```

## 注意事项

- **edge-tts 需要额外安装**：默认环境未内置，首次使用会提示执行
  `pip install 'biscuitbot[tts]'`。若报错「未安装 edge-tts」，先用 exec
  安装依赖再重试。
- **openai / dashscope 需 API Key**：未配置时返回配置错误，提示用户在
  WebUI 模型设置里配置对应 provider 的 apiKey。
- 文本为空、TTS 被禁用、provider 未知时均返回 `Error: ...` 说明。
- 生成的音频文件默认保存到工作区 `generated/tts/<日期>/` 下，文件名带
  时间戳避免覆盖。
