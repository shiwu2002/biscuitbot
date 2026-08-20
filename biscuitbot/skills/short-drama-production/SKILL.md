---
name: short-drama-production
tier: user
description: AI短剧/长视频全流程制作：以 cinematic_director 导演工具驱动 11 阶段状态机（建项目→剧本→空间规划→资产→分镜→视频生成→审核→成片），剧本交给宫本编剧、角色/场景参考图用 generate_image、角色配音与台词用 text_to_speech、视频用 generate_video、剪辑交给阿伟。当用户要做短剧、长视频、把小说/故事/剧本改编成影视作品时使用。
metadata: {"biscuitbot":{"emoji":"🎭"}}
---

# AI短剧/长视频全流程制作（cinematic\_director 驱动）

> 本技能把「短剧/长视频生产」固化为 **cinematic\_director 工具的 11 阶段状态机**：
> 建项目 → 剧本 → 剧本审核 →（可选用户审核）→ **空间规划** → 世界观/角色资产 →
> 资产审核 → 资产锁定 → 分镜 → 视频生成 → 视频审核 → 成片。
> 阶段顺序、审核 Gate、数据冻结由工具**代码强制**，不要跳过、不要自作主张改流程。
> 导演工具只做编排与管控，**不生成视频/图片/音频**——具体产出分别交给
> `generate_image` / `text_to_speech` / `generate_video`。

## 何时使用

用户想拍短剧、做长视频、把小说/故事/剧本改编成影视、做多镜头分镜预告片时使用。
本技能**不做字幕**——字幕烧录不在流程内，如需字幕请另行处理。

## 工具与员工对照表

| 职责            | 调用                                                            |
| ------------- | ------------------------------------------------------------- |
| 导演编排（11阶段状态机） | `cinematic_director`（主 Agent 驱动）                              |
| 剧本创作/优化       | `invoke_employee(employee_id="screenwriter", ...)`（宫本）        |
| 角色/场景/道具参考图   | `generate_image`（文生图，含角色三视图合成图）                               |
| 角色声线 / 对白台词音频 | `text_to_speech`（多角色独立声线）                                     |
| 视频生成          | `generate_video`（Seedance，喂 compile\_prompt 产物）               |
| 剪辑 / 成片拼接     | `invoke_employee(employee_id="clip-master", ...)`（阿伟）或 ffmpeg |
| 查询项目当前状态      | `cinematic_director(action="status", ...)`                    |

> 员工也支持中文姓名点名：宫本 / 阿伟。分派任务描述越具体越好。

## 流程总览（11 阶段状态机）

```
create_project           → init
write_script             → script_analysis（先请宫本写/优化剧本）
review_script            → script_review（可选 request_user_review/approve_script）
set_floorplan            → spatial_planning（强制，先声明平面图）
add_asset (CHAR/LOC/PROP)→ world_building → character_design
review_assets + lock_assets → asset_lock
plan_shot                → storyboard
compile_prompt → generate_video → quality_check（record_qc）
record_shot_result → final_edit（阿伟剪辑 / ffmpeg 拼接成片）
```

**硬性规则：**

1. 全程用 `cinematic_director` 驱动，每步 action 前先 `status` 确认当前阶段，**不得跳阶段**；
2. 空间规划是**强制阶段**——生成任何资产前必须先 `set_floorplan`；
3. 角色资产（CHAR）必须有 `reference_image`（**正/侧/背三视图合成图**）与 `voice`（声线），有对白的镜头生成前必须 `attach_audio`；
4. 剧本/资产审核不通过（分数 < 85 或否决）会被工具硬阻塞，须按 `note` 重做。

***

## 第一步：建项目（init）

```
cinematic_director(
  action="create_project",
  project_id="<小写slug，如 snow-forest>",
  name="<项目名>",
  logline="<一句话梗概>",
  style="<写实电影/国漫/3D…>",
  ratio="9:16",            # 短剧默认竖屏
  fps=24,
  color_palette="<色彩体系>",      # 可选
  reference_works="<对标影视作品>"  # 可选
)
```

***

## 第二步：剧本（script\_analysis）

> **⚠️ 必做：请宫本编剧创作/优化剧本，不要自己写。**
> 自写剧本容易出现对白互动少、角色无个性、冲突不足。

### 2.1 先派给宫本

```
invoke_employee(
  employee_id="screenwriter",
  task="""为短剧《<剧名>》写分镜剧本。要求：
1. 故事梗概：<题材/背景/核心冲突>
2. 角色设定：<每个角色的身份、性格、说话风格>
3. 时长/镜头数：<目标总时长、大致镜头数>
4. 多角色互动丰富，每角色台词有个性
5. 按镜头拆分，每镜头含：场景描述、角色动作、对白台词、情绪/音效提示"""
)
```

### 2.2 把宫本的剧本写入导演工具

`write_script` 需要三层数据：**scenes**（场景）、**shots**（分镜）、**story**（剧情补充，含人物/环境描述词，这些 prompt 是后续 `add_asset` 的 `appearance` 来源）。

```
cinematic_director(
  action="write_script",
  project_id="<slug>",
  scenes=[
    {"id":"Scene001","location":"雪林","time":"黄昏","function":"逃亡开场","duration":8},
    ...
  ],
  shots=[
    {"id":"Shot001","scene_id":"Scene001","shot_size":"全景","camera":"跟随",
     "lens":"35mm","action":"主角在暴雪中奔跑","emotion":"紧张",
     "sound":"风声呼啸","dialogue":true,
     "asset_refs":["CHAR001","LOC001"],
     "spatial":{...}},   # 空间站位（可选，写剧本没给就用第五步 plan_shot 的 spatial 补）
    ...
  ],
  story={
    "plot":"<整体剧情>",
    "characters":[{"id":"CHAR001","name":"林远","prompt":"<稳定描述，将作为 add_asset 的 appearance>"}],
    "environments":[{"id":"LOC001","name":"雪林","prompt":"<稳定描述>"}],
    "spatial":"<空间布局文字描述>",
    "worldview":"<世界观/风格/氛围>"
  }
)
```

> 分镜拆好后确认每镜头台词时长 ≤ 15.2s（Seedance r2v 音频硬上限）。

### 2.3 剧本审核（script\_review）

```
cinematic_director(action="review_script", project_id="<slug>",
  score=88, passed=true, note="结构完整，冲突充足")
```

- 分数 < 85 或 `passed=false` → 工具会打回，按 `note` 让宫本重写后再 `write_script`；
- **可选用户审核门**：想让用户确认剧情，先 `request_user_review`，用户批准后
  `approve_script(approved=true)`；否决则 `approved=false` 打回重写。

***

## 第三步：空间规划（spatial\_planning，强制）

> **必须做。** 生成任何资产前先声明全局 2D 平面图（俯视，原点西南角，+x 东、+y 北，单位米）。
> 声明后进入「空间一致性硬门」：LOC 必须带坐标，镜头必须给站位，编译 Prompt 时校验
> 「界内 / 建筑不叠放 / 人物不悬空 / 运动不穿墙」。

```
cinematic_director(
  action="set_floorplan",
  project_id="<slug>",
  width=40,        # 用地宽度（米）
  length=30,       # 用地深度（米）
  unit="meter"
)
```

***

## 第四步：世界观/角色资产（world\_building → character\_design）

> 每类资产先用 `generate_image` 出参考图（**角色要正/侧/背三视图合成图**），
> 角色再用 `text_to_speech` 出声线，然后 `add_asset` 落库。

### 4.1 生成参考图（generate\_image）

```
generate_image(
  prompt="<角色的稳定描述，正/侧/背三视图合成图>",
  aspect_ratio="9:16",
  count=1
)
generate_image(
  prompt="<场景的稳定描述>",
  aspect_ratio="9:16",
  count=1
)
```

> 工具会把图片自动持久化为 artifact 并返回**本地路径**，把该路径作为
> `add_asset` 的 `reference_image` 传入；如需在已有图上迭代可传
> `reference_images=["<本地路径>"]`。

### 4.2 角色声线（text\_to\_speech）

```
text_to_speech(
  text="<角色自我介绍，20-30字>",
  voice="<角色音色，见下>",
  model="cosyvoice-v3-flash",
  provider="dashscope",
  output_path="<工作区>/cinematic/<slug>/tts/voice_<角色名>.mp3"
)
```

**常用音色对照表**

| 角色类型    | 音色ID              | 特点   |
| ------- | ----------------- | ---- |
| 年轻书生/文人 | `longcheng_v3`    | 儒雅清朗 |
| 老年长者/村长 | `longlaobo_v3`    | 慈祥沧桑 |
| 壮年汉子/武将 | `longlaotie_v3`   | 粗犷豪放 |
| 年轻女性    | `longxiaochun_v3` | 温柔甜美 |
| 中年男性    | `longanyang`      | 沉稳有力 |

### 4.3 落库资产（add\_asset）

```
# 角色（CHAR 必须三视图 + 声线）
cinematic_director(
  action="add_asset",
  project_id="<slug>",
  kind="CHAR", id="CHAR001", name="林远",
  appearance="<与 story.characters[].prompt 一致的稳定描述>",
  reference_image="<CHAR001_turnaround.png 路径>",
  three_view=true,                        # 仅 CHAR，必须为 true
  voice="<voice_林远.mp3 路径>"            # 仅 CHAR，必填
)

# 场景（LOC，设平面图后 position 必填）
cinematic_director(
  action="add_asset",
  project_id="<slug>",
  kind="LOC", id="LOC001", name="雪林",
  appearance="<稳定描述>",
  reference_image="<LOC001.png 路径>",
  position={"x":20,"y":15},
  footprint={"width":8,"depth":6},        # 可选
  orientation=0                            # 可选
)

# 道具（PROP）同理，需 reference_image
```

### 4.4 资产审核 + 锁定（asset\_lock）

```
cinematic_director(action="review_assets", project_id="<slug>",
  score=90, passed=true, note="资产一致，可锁定")
cinematic_director(action="lock_assets", project_id="<slug>")
```

> 资产锁定后不可再修改；审核不通过按 `note` 重新生成参考图再 `add_asset`。

***

## 第五步：分镜（storyboard）

对每个镜头补充摄影参数（景别/机位/焦段/运镜/景深/光影已在剧本层给出，这里细化机位）。
**写剧本时没给空间站位的镜头，用 `plan_shot` 的 `spatial` 参数回填**——设了平面图后，
`compile_prompt` 会硬校验每个镜头的 `spatial.characters`（人物站位坐标），缺了会报
「missing spatial.characters」，此时不需要重写剧本，补写 `spatial` 即可解开：

```
cinematic_director(
  action="plan_shot",
  project_id="<slug>",
  shot_id="Shot001",
  camera="<机位/摄影机>",
  lens="35mm",
  movement="Tracking Shot",
  depth="浅景深",
  lighting="冷色逆光",
  spatial={                        # 可选：本镜头空间站位（人物/道具/机位，2D 俯视坐标）
    "characters":[
      {"asset_id":"CHAR001","position":{"x":20,"y":15},"facing":90},   # 朝东
      {"asset_id":"CHAR002","position":{"x":15,"y":12}}
    ],
    "camera":{"position":{"x":10,"y":5},"target":{"x":20,"y":15}}
  }
)
```

> `spatial.characters[].asset_id` 必须是本镜头 `asset_refs` 里的 CHAR；坐标在
> 平面图界内；机位 `position`/`target` 同样须在界内。所有镜头 plan 完后工具自动进入
> `video_generation`，此时若发现缺站位仍可再调 `plan_shot` 补 `spatial`。

***

## 第六步：视频生成（video\_generation）

> **有对白的镜头：先逐角色 TTS 台词 → attach\_audio → 再 compile\_prompt → generate\_video。**
> 对白音频超过 15.2s 会被 Seedance r2v 拒绝（HTTP 400），必须拆分或精简。

### 6.1 对白台词音频（text\_to\_speech，每角色独立声线）

```
text_to_speech(
  text="<林远这句台词>",
  voice="longcheng_v3",
  model="cosyvoice-v3-flash",
  provider="dashscope",
  output_path="<工作区>/cinematic/<slug>/tts/shot001_linYuan.mp3"
)
# 同一镜头多角色：各角色分开生成，再拼接（静音间隔 0.5s）
```

**拼接多角色音频**：用 ffmpeg concat 合成完整镜头音频，并校验时长：

```bash
ffmpeg -y -f lavfi -i anullsrc=r=24000:cl=mono -t 0.5 -c:a libmp3lame -q:a 9 silence_0.5s.mp3
ffmpeg -y -f concat -safe 0 -i concat_list.txt -c:a libmp3lame -q:a 2 shot001_combined.mp3
ffprobe -v error -show_entries format=duration -of csv=p=0 shot001_combined.mp3
```

> 拼接后时长必须 ≤ 15.2s。超时处理：精简台词 / 静音间隔 0.3s / 拆分成两个短镜头。

### 6.2 挂载音频（attach\_audio）

```
cinematic_director(
  action="attach_audio",
  project_id="<slug>",
  shot_id="Shot001",
  audio="<shot001_combined.mp3 路径>"
)
```

### 6.3 编译 Prompt（compile\_prompt）

```
cinematic_director(
  action="compile_prompt",
  project_id="<slug>",
  shot_id="Shot001",
  continuity=true    # 仅时间/动作连续的镜头；跨场景/硬切省略或 false
)
```

返回 `prompt` + `image_urls`（首帧参考图 + 空间图 + 角色/场景参考图）+ `audio_urls`，
**原样喂给 generate\_video**。

### 6.4 生成视频（generate\_video）

```
generate_video(
  prompt="<compile_prompt 返回的 prompt>",
  image_urls=["<compile_prompt 返回的 image_urls>"],
  audio_urls=["<compile_prompt 返回的 audio_urls>"],
  ratio="9:16",
  duration=<音频时长向上取整：≤4→4, 4-8→8, 8-12→12, 12-15→15>,
  generate_audio=true,   # ★ 必须开启：有对白→口型同步；无对白→环境音
  model="doubao-seedance-2-0-mini-260615"
)
```

> 带参考素材（r2v）时**不要传 resolution**；模型名必须带日期后缀（裸 `doubao-seedance-2-0-mini` 会 404）。

***

## 第七步：视频审核（quality\_check → record\_qc）

```
cinematic_director(
  action="record_qc",
  project_id="<slug>",
  shot_id="Shot001",
  character_score=92,   # 人物一致度
  scene_score=90,       # 场景一致度
  action_score=88       # 动作一致度
)
```

任一分值 < 85 → `qc_fail`，工具拒绝推进，需调整 `compile_prompt` 后重新 `generate_video` 再 `record_qc`。

***

## 第八步：成片（final\_edit）

### 8.1 记录镜头结果 + 尾帧

```
cinematic_director(
  action="record_shot_result",
  project_id="<slug>",
  shot_id="Shot001",
  video_path="<该镜头成片视频路径>"
)
# 需要首尾帧连贯的镜头：记录尾帧，供下一镜头 continuity=true 引用
cinematic_director(
  action="record_shot_frame",
  project_id="<slug>",
  shot_id="Shot001",
  last_frame="<该镜头尾帧图路径>"
)
```

### 8.2 全部镜头 final 后，交给阿伟剪辑 / ffmpeg 拼接

**优先派给剪辑员工阿伟（clip-master）：**

```
invoke_employee(
  employee_id="clip-master",
  task="""把以下 <N> 个镜头视频按顺序拼接成一部短剧成片，输出 final.mp4：
<逐个列出镜头视频路径与对应镜头/时长>。要求：直接复制流不重编码（-c copy）、
核对视频+音频两条流齐全、总时长=各镜头时长之和、分辨率 720×1280。"""
)
```

**或自己用 ffmpeg concat 拼接：**

```
# concat_list.txt：file '<视频路径>' 逐行
ffmpeg -y -f concat -safe 0 -i concat_list.txt -c copy final.mp4
ffprobe -v error -show_entries format=duration,size -show_entries stream=width,height,codec_name -of json final.mp4
```

确认：有 video 和 audio 两条流、总时长 = 各镜头之和、分辨率正确（720×1280）、无字幕。

***

## 避坑清单

| 坑       | 说明                             | 解决方案                                |
| ------- | ------------------------------ | ----------------------------------- |
| 跳阶段     | 状态机硬门                          | 每步前先 `status` 确认，按序执行               |
| 忘做空间规划  | 资产/镜头无坐标                       | `set_floorplan` 是强制阶段，先生成资产前必做      |
| 镜头缺站位    | compile\_prompt 报「missing spatial.characters」 | `plan_shot` 补 `spatial`（characters 含 asset\_id+position），不用重写剧本 |
| 自己写剧本   | 对白互动少、角色无个性                    | 必须调用宫本（screenwriter）                |
| 角色无三视图  | add\_asset 报错                  | CHAR 参考图必须是正/侧/背三视图合成图              |
| 角色无声线   | add\_asset 报错                  | CHAR 必须先生成 `voice`                  |
| 对白镜头无音频 | compile\_prompt 拒绝             | 有对白必须 `attach_audio`                |
| 音频超时    | r2v 模型音频 ≤ 15.2s               | 拼接后必须校验时长                           |
| 模型名错误   | `doubao-seedance-2-0-mini` 404 | 用 `doubao-seedance-2-0-mini-260615` |
| 无环境音    | 不传 `generate_audio` 静默         | 始终 `generate_audio=true`            |
| 分辨率报错   | r2v 模式传了 resolution            | 带参考素材时不传 resolution                 |
| 拼接无声    | concat 后音频丢失                   | 用 `-c copy` 直接复制流                   |

***

## 完整成功案例（2026-08-20）

**《异世秦途·魂穿》v2**（旧旁路流程成果，可作为编排参考）

- 3个角色：林远(`longcheng_v3`)、赵伯(`longlaobo_v3`)、大壮(`longlaotie_v3`)
- 5个镜头：8s + 15s + 15s + 12s + 8s = 58s
- 每角色独立声线样本 + 逐镜头对白拆分 + 静音间隔拼接
- 全部镜头 `generate_audio=true` 生成环境音
- ffmpeg concat 拼接成片，720×1280 竖屏，无字幕
- ⚠️ **教训**：v2 剧本自写、未调用宫本，对白互动太少（5镜头仅3段交互）。
  新流程用 cinematic\_director 状态机驱动时，剧本必须经宫本优化，且把阶段推进、
  审核 Gate、资产冻结全部交给导演工具硬校验。

