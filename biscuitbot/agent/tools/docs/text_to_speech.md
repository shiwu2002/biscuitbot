# text_to_speech

把文本合成为语音音频并保存为本地 mp3 文件，返回文件路径。支持三个 provider：
- **openai**（gpt-4o-mini-tts，默认音色 alloy，需 API Key）
- **dashscope**（cosyvoice-v3-flash，默认音色 longxiaochun_v3，需 API Key）
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
| voice | string | 否 | provider 默认 | 音色（OpenAI alloy；DashScope longxiaochun_v3；edge-tts zh-CN-XiaoxiaoNeural）。DashScope 完整音色见下方「音色清单」 |
| rate | string | 否 | - | 语速，如 `+10%` 加快、`-20%` 减慢 |
| model | string | 否 | provider 默认 | 模型覆盖（gpt-4o-mini-tts / cosyvoice-v3-flash） |
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

# 用灵积（dashscope）合成，指定男性音色「龙安洋」
text_to_speech(
  text="大家好，我是 BiscuitBot 的语音助手。",
  provider="dashscope",
  voice="longanyang",
)

# 切换 OpenAI 服务商并指定输出路径（供后续剪辑使用）
text_to_speech(
  text="This is the official launch trailer.",
  provider="openai",
  voice="nova",
  output_path="generated/tts/trailer_vo.mp3",
)
```

## 音色清单（dashscope / cosyvoice-v3-flash）

dashscope 的 `voice` 参数填「音色参数」列。除少数标杆音色（`longanyang`、
`longanhuan`）无后缀外，其余均为 `_v3` 后缀。

### 男声音色

| 音色名称 | 音色参数 | 特征 | 年龄 | 语言 | 适用场景 |
|----------|----------|------|------|------|----------|
| 龙安洋 | longanyang | 阳光大男孩 | 20~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙杰力豆 | longjielidou_v3 | 阳光顽皮男 | 10岁 | 中文（普通话）、英文 | 智能玩具/儿童故事机 |
| 龙安粤 | longanyue_v3 | 欢脱粤语男 | 25~35岁 | 中文（粤语）、英文 | 方言 |
| 龙老铁 | longlaotie_v3 | 东北直率男 | 25~30岁 | 中文（东北话）、英文 | 方言 |
| 龙陕哥 | longshange_v3 | 原味陕北男 | 25~35岁 | 中文（陕西话）、英文 | 方言 |
| loongandy | loongandy_v3 | 美式英文男 | 30~35岁 | 美式英语 | 出海营销 |
| loongdavid | loongdavid_v3 | 美式英文男 | 35~40岁 | 美式英语 | 出海营销 |
| loongeric | loongeric_v3 | 英式英文男 | 35~40岁 | 英式英语 | 出海营销 |
| loongluca | loongluca_v3 | 英式英文男 | 25~30岁 | 英式英语 | 出海营销 |
| loongtomoya | loongtomoya_v3 | 日语男 | 30~35岁 | 日语 | 出海营销 |
| Yuuma | loongyuuma_v3 | 日语男 | 20~25岁 | 日语 | 出海营销 |
| Jihun | loongjihun_v3 | 韩语男 | 25~30岁 | 韩语 | 出海营销 |
| 龙飞 | longfei_v3 | 热血磁性男 | 30~35岁 | 中文（普通话）、英文 | 诗词朗诵 |
| 龙应询 | longyingxun_v3 | 年轻青涩男 | 20~25岁 | 中文（普通话）、英文 | 客服 |
| 龙安昀 | longanyun_v3 | 居家暖男 | 30~35岁 | 中文（普通话）、英文 | 语音助手 |
| 龙安朗 | longanlang_v3 | 清爽利落男 | 20~25岁 | 中文（普通话）、英文 | 语音助手 |
| 龙橙 | longcheng_v3 | 智慧青年男 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙泽 | longze_v3 | 温暖元气男 | 25~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙哲 | longzhe_v3 | 呆板大暖男 | 25~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙天 | longtian_v3 | 磁性理智男 | 30~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙浩 | longhao_v3 | 多情忧郁男 | 30~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙寒 | longhan_v3 | 温暖痴情男 | 30~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙安智 | longanzhi_v3 | 睿智轻熟男 | 25~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙三叔 | longsanshu_v3 | 沉稳质感男 | 25~45岁 | 中文（普通话）、英文 | 有声书 |
| 龙修 | longxiu_v3 | 博才说书男 | 25~35岁 | 中文（普通话）、英文 | 有声书 |
| 龙楠 | longnan_v3 | 睿智青年男 | 25~30岁 | 中文（普通话）、英文 | 有声书 |
| 龙逸尘 | longyichen_v3 | 洒脱活力男 | 20~30岁 | 中文（普通话）、英文 | 有声书 |
| 龙老伯 | longlaobo_v3 | 沧桑岁月爷 | 60岁以上 | 中文（普通话）、英文 | 有声书 |
| 龙猴哥 | longhouge_v3 | 经典猴哥 | 20~25岁 | 中文（普通话）、英文 | 短视频配音 |
| 龙硕 | longshuo_v3 | 博才干练男 | 25~30岁 | 中文（普通话）、英文 | 新闻播报 |
| 龙书 | longshu_v3 | 沉稳青年男 | 20~25岁 | 中文（普通话）、英文 | 新闻播报 |

### 女声音色

| 音色名称 | 音色参数 | 特征 | 年龄 | 语言 | 适用场景 |
|----------|----------|------|------|------|----------|
| 龙安欢（V3） | longanhuan_v3 | 欢脱元气女 | 20~30岁 | 中文（多方言）、英文 | 社交陪伴 |
| 龙安欢 | longanhuan | 欢脱元气女 | 20~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙呼呼 | longhuhu_v3 | 天真烂漫女童 | 6~10岁 | 中文（普通话）、英文 | 童声 |
| 龙泡泡 | longpaopao_v3 | 飞天泡泡音 | 6~15岁 | 中文（普通话）、英文 | 智能玩具/儿童故事机 |
| 龙仙 | longxian_v3 | 豪放可爱女 | 12岁 | 中文（普通话）、英文 | 智能玩具/儿童故事机 |
| 龙铃 | longling_v3 | 稚气呆板女 | 10岁 | 中文（普通话）、英文 | 智能玩具/儿童故事机 |
| 龙闪闪 | longshanshan_v3 | 戏剧化童声 | 6~15岁 | 中文（普通话）、英文 | 消费电子-儿童有声书 |
| 龙牛牛 | longniuniu_v3 | 阳光男童声 | 6~15岁 | 中文（普通话）、英文 | 消费电子-儿童有声书 |
| 龙嘉欣 | longjiaxin_v3 | 优雅粤语女 | 30~35岁 | 中文（粤语）、英文 | 方言 |
| 龙嘉怡 | longjiayi_v3 | 知性粤语女 | 25~30岁 | 中文（粤语）、英文 | 方言 |
| 龙安闽 | longanmin_v3 | 清纯萝莉女 | 18~25岁 | 中文（闽南话）、英文 | 方言 |
| loongkyong | loongkyong_v3 | 韩语女 | 25~30岁 | 韩语 | 出海营销 |
| Riko | loongriko_v3 | 二次元霓虹女 | 18~25岁 | 日语 | 出海营销 |
| loongtomoka | loongtomoka_v3 | 日语女 | 30~35岁 | 日语 | 出海营销 |
| loongabby | loongabby_v3 | 美式英文女 | 30~35岁 | 美式英语 | 出海营销 |
| loongannie | loongannie_v3 | 美式英文女 | 30~35岁 | 美式英语 | 出海营销 |
| loongava | loongava_v3 | 美式英文女 | 35~40岁 | 美式英语 | 出海营销 |
| loongbeth | loongbeth_v3 | 美式英文女 | 35~40岁 | 美式英语 | 出海营销 |
| loongbetty | loongbetty_v3 | 美式英文女 | 35~40岁 | 美式英语 | 出海营销 |
| loongcally | loongcally_v3 | 美式英文女 | 25~30岁 | 美式英语 | 出海营销 |
| loongcindy | loongcindy_v3 | 美式英文女 | 30~35岁 | 美式英语 | 出海营销 |
| loongdonna | loongdonna_v3 | 美式英文女 | 35~40岁 | 美式英语 | 出海营销 |
| loongemily | loongemily_v3 | 英式英文女 | 35~40岁 | 英式英语 | 出海营销 |
| loongluna | loongluna_v3 | 英式英文女 | 35~40岁 | 英式英语 | 出海营销 |
| Yuuna | loongyuuna_v3 | 日语女 | 18~25岁 | 日语 | 出海营销 |
| loongindah | loongindah_v3 | 印尼女 | 22~27岁 | 印尼语 | 出海营销 |
| 龙应笑 | longyingxiao_v3 | 清甜推销女 | 20~25岁 | 中文（普通话）、英文 | 电话销售 |
| 龙应静 | longyingjing_v3 | 低调冷静女 | 25~35岁 | 中文（普通话）、英文 | 客服 |
| 龙应聆 | longyingling_v3 | 温和共情女 | 25~30岁 | 中文（普通话）、英文 | 客服 |
| 龙应桃 | longyingtao_v3 | 温柔淡定女 | 25~30岁 | 中文（普通话）、英文 | 客服 |
| 龙小淳 | longxiaochun_v3 | 知性积极女 | 25~30岁 | 中文（普通话）、英文 | 语音助手 |
| 龙小夏 | longxiaoxia_v3 | 沉稳权威女 | 25~30岁 | 中文（普通话）、英文 | 语音助手 |
| YUMI | longyumi_v3 | 正经青年女 | 20~25岁 | 中文（普通话）、英文 | 语音助手 |
| 龙安温 | longanwen_v3 | 优雅知性女 | 25~35岁 | 中文（普通话）、英文 | 语音助手 |
| 龙安莉 | longanli_v3 | 利落从容女 | 25~35岁 | 中文（普通话）、英文 | 语音助手 |
| 龙应沐 | longyingmu_v3 | 优雅知性女 | 25~30岁 | 中文（普通话）、英文 | 语音助手 |
| 龙安台 | longantai_v3 | 嗲甜台湾女 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙华 | longhua_v3 | 元气甜美女 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙颜 | longyan_v3 | 温暖春风女 | 30~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙星 | longxing_v3 | 温婉邻家女 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙婉 | longwan_v3 | 细腻柔声女 | 20~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙嫱 | longqiang_v3 | 浪漫风情女 | 30~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙菲菲 | longfeifei_v3 | 甜美娇气女 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙安柔 | longanrou_v3 | 温柔闺蜜女 | 20~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙安灵 | longanling_v3 | 思维灵动女 | 20~30岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙安雅 | longanya_v3 | 高雅气质女 | 25~35岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙安亲 | longanqin_v3 | 亲和活泼女 | 20~25岁 | 中文（普通话）、英文 | 社交陪伴 |
| 龙妙 | longmiao_v3 | 抑扬顿挫女 | 25~30岁 | 中文（普通话）、英文 | 有声书 |
| 龙媛 | longyuan_v3 | 温暖治愈女 | 35~40岁 | 中文（普通话）、英文 | 有声书 |
| 龙悦 | longyue_v3 | 温暖磁性女 | 30~35岁 | 中文（普通话）、英文 | 有声书 |
| 龙婉君 | longwanjun_v3 | 细腻柔声女 | 20~30岁 | 中文（普通话）、英文 | 有声书 |
| 龙老姨 | longlaoyi_v3 | 烟火从容阿姨 | 60岁以上 | 中文（普通话）、英文 | 有声书 |
| 龙机器 | longjiqi_v3 | 呆萌机器人 | 20~30岁 | 中文（普通话）、英文 | 短视频配音 |
| 龙黛玉 | longdaiyu_v3 | 娇率才女音 | 15~25岁 | 中文（普通话）、英文 | 短视频配音 |
| 龙安燃 | longanran_v3 | 活泼质感女 | 30~40岁 | 中文（普通话）、英文 | 直播带货 |
| 龙安宣 | longanxuan_v3 | 经典直播女 | 30~40岁 | 中文（普通话）、英文 | 直播带货 |
| Bella3.0 | loongbella_v3 | 精准干练女 | 25~30岁 | 中文（普通话）、英文 | 新闻播报 |

## 注意事项

- **edge-tts 需要额外安装**：默认环境未内置，首次使用会提示执行
  `pip install 'biscuitbot[tts]'`。若报错「未安装 edge-tts」，先用 exec
  安装依赖再重试。
- **openai / dashscope 需 API Key**：未配置时返回配置错误，提示用户在
  WebUI 模型设置里配置对应 provider 的 apiKey。
- 文本为空、TTS 被禁用、provider 未知时均返回 `Error: ...` 说明。
- 生成的音频文件默认保存到工作区 `generated/tts/<日期>/` 下，文件名带
  时间戳避免覆盖。
- dashscope 音色参数须与模型配套：cosyvoice-v3-flash 对应上方 `_v3`
  音色清单；不要混用旧版 `_v2` 音色（如 `longxiaochun_v2`）。
