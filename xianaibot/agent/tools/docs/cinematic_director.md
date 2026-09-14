# cinematic_director

AI 导演工作流编排工具。把「导演圣经 → 剧本 → 剧本审核 → **（可选用户审核）** → **空间规划** → 世界观/角色资产 → 资产审核 → 资产锁定 → 分镜 → 视频生成 → 视频审核 → 成片」固化为**代码强约束的 11 阶段状态机**：阶段顺序、审核 Gate、数据冻结都由本工具硬校验，非法操作被拒绝，解决 AI 视频人物/场景/光影漂移与流程偏移问题。剧本师步骤产出完整 `story`（整体剧情 + 人物/环境描述词 + 空间/大局描述词），并可经**可选用户审核门**让用户确认后再继续。空间规划是**强制阶段**——生成任何资产之前必须先声明平面图并锁定坐标体系。

## 何时使用

用户想拍短剧、把小说/故事/剧本改编成视频、做 AI 影视/分镜/预告片生产时使用。本工具**不生成视频/图片/音频**——它只做编排与管控；视频/图片/配音分别交给 `generate_video` / `generate_image` / `text_to_speech`。

## 11 阶段状态机

项目 `stage`：

```
init → script_analysis → script_review → spatial_planning → world_building
     → character_design → asset_lock → storyboard → video_generation
     → quality_check → final_edit
```

`script_review` 是**可选**阶段：只有调用 `request_user_review` 才会进入；不调用则 `review_script` 通过后直接进 `spatial_planning`。

全部镜头 `final` 后 `completed=true`。镜头 `shot.status`：`pending → prompted → qc_pass/qc_fail → final`。

**硬门（代码强制，不可绕过）：**

1. **三级审核 Gate**：`review_script`（剧本审核）不过 → 不能进入 spatial_planning；`review_assets`（资产审核）不过 → 不能进入 asset_lock；`record_qc`（视频审核）任一分数 < 阈值（默认 85）→ 镜头 `qc_fail`，`record_shot_result` 拒绝推进。
2. **可选用户审核门**：调用 `request_user_review` 后项目进入 `script_review`，必须等 `approve_script(approved=true)` 才能进 `spatial_planning`；`approved=false` 打回 `script_analysis` 重写。不调用则跳过此门。
3. **空间规划强制**：进入 spatial_planning 后必须 `set_floorplan` 才能进入 world_building；`add_asset(LOC)` 必须带 `position`；`compile_prompt` 强制校验「界内 / 建筑不叠放 / 人物不悬空 / 运动不穿墙」，不一致直接拒绝。
4. **资产优先 + 参考图/三视图/声线强制**：`add_asset` 必须有 `reference_image`；`CHAR` 额外强制 `three_view=true`（正/侧/背三视图合成图）与 `voice`（声线参考）；`lock_assets` 校验脚本引用的每个资产都已添加且有参考图、每个 CHAR 有三视图与声线。
5. **数据冻结**：`review_script` 通过后 `write_script` 拒绝改剧本；`lock_assets` 后 `add_asset` 拒绝改资产；资产一旦创建**无「编辑/删除」动作**，角色定义天然不可变。
6. **权限隔离**：本工具 `_scopes={"core"}`，只有主 Agent（导演）能改项目状态；`spawn` 出的子 Agent 无法调用本工具，只能用 `generate_image`/`read_file`/视觉能力**产出内容后回报**，由导演落库。
7. **首尾帧连贯（可选，智能体逐镜头抉择）**：`compile_prompt` 仅当显式 `continuity=true` 时要求上一镜头已 `record_shot_frame` 并自动把该尾帧作为首帧引用（`image_urls[0]`）；时间连续/动作连续的镜头设 `true`，跨场景/跨剧情/硬切应省略或设 `false`。不强制。
8. **音频强制（口型 + 音色一致）**：`CHAR` 资产必须提供 `voice` 声线参考（音色一致）；分镜里 `dialogue=true` 的镜头在 `compile_prompt` 前必须先 `attach_audio`（口型），否则拒绝。

## 动作（action）

### create_project —— 建项目 + 导演圣经定位 → stage=init
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID（小写 slug，如 `snow-forest`） |
| name | 是 | 项目名 |
| logline | 是 | 一句话梗概 |
| style | 是 | 视觉风格（写实电影/国漫/3D…） |
| ratio | 否 | 画幅，默认 16:9 |
| fps | 否 | 基准帧率，默认 24 |
| color_palette | 否 | 色彩体系 |
| reference_works | 否 | 对标影视作品 |

### set_floorplan —— 声明全局 2D 平面图（spatial_planning 阶段，强制）→ 开启空间一致性
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| width | 是 | 用地宽度（米，x 轴范围 `[0,width]`） |
| length | 是 | 用地深度（米，y 轴范围 `[0,length]`） |
| unit | 否 | 坐标单位，默认 `meter` |

平面图是**强制阶段**：`review_script` 通过后进入 `spatial_planning`，必须先 `set_floorplan` 才能进入 `world_building` 生成资产。设了平面图即开启空间一致性**硬门**——LOC 资产必须带 `position`，每个镜头必须提供 `spatial` 站位，`compile_prompt` 会校验「界内 / 建筑不叠放 / 人物不悬空 / 运动不穿墙」，不一致直接拒绝。坐标系约定：**2D 俯视平面图**，原点 `(0,0)` 在西南角，`+x` 向东、`+y` 向北（北在上）。

阶段约束：只能在 `spatial_planning` 调用（在 `review_script` 之后、`add_asset` 之前），调用后自动 → `world_building`。

### write_script —— 写场景 + 分镜 + 剧情（叙事层）→ stage=script_analysis
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| scenes | 是 | 场景列表：`{id, location, time, function, duration}` |
| shots | 是 | 分镜列表：`{id, scene_id, shot_size, camera, lens, action, emotion, sound, dialogue, asset_refs, spatial}` |
| story | 否 | **剧情补充优化**：整体剧情 + 人物/环境描述词 + 空间/大局描述词，见下 |

约束：每个 shot 的 `scene_id` 必须存在；`asset_refs` 必须含至少一个 `CHAR###` 与一个 `LOC###`。

`story`（剧本师先补齐剧情与各类描述词，供用户审核；其中人物/环境的 `prompt` 即后续 `add_asset` 的 `appearance` 来源）：

```json
"story": {
  "plot": "一家三口在暴雪中逃亡求生，途中遭遇追兵，最终在雪林小屋反击脱险",
  "characters": [
    {"id": "CHAR001", "name": "爸爸", "prompt": "45 岁男人，黑色羽绒服，胡茬严肃"},
    {"id": "CHAR002", "name": "妈妈", "prompt": "40 岁女人，深灰大衣，神情坚毅"}
  ],
  "environments": [
    {"id": "LOC001", "name": "雪林", "prompt": "黄昏暴雪的雪林，冷色月光"}
  ],
  "spatial": "雪林位于平面图东北部，小屋在西南角，人物从东侧入画向西逃亡",
  "worldview": "末日废土+暴雪求生，冷色主调，压抑而紧张的氛围"
}
```

`write_script` 会同步生成**剧本资产文档** `script.md`（Markdown），把剧情/人物/环境/场景/分镜整理成可读文档写入项目目录，供用户后续查阅剧情；`request_user_review` 时可直接读 `script.md` 呈现给用户审核。

shot 必填 `spatial`（空间布局站位，空间规划强制后每个镜头都要提供）：

```json
"spatial": {
  "camera":     {"position":{"x":30,"y":20}, "target":{"x":50,"y":40}, "facing":45},
  "characters": [{"asset_id":"CHAR001","position":{"x":50,"y":40},"facing":90,"motion_to":{"x":52,"y":40}}],
  "props":      [{"asset_id":"PROP001","position":{"x":45,"y":36},"facing":0}]
}
```

- `characters[].asset_id` 必须是 `asset_refs` 里的 `CHAR###`；`props[]` 同理须 `PROP###`。
- `position`：站位坐标（米）；`facing`：朝向角（0=北，90=东）；`motion_to`：可选运动终点（用于穿墙检测）。

### review_script —— 剧本审核 Gate → 通过后 stage=spatial_planning
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| score | 是 | 审核分数 0–100 |
| passed | 否 | 是否通过（`False` 一票否决） |
| threshold | 否 | 通过阈值，默认 85 |
| note | 否 | 备注 / 修改建议 |
| reviewer | 否 | 审核人（员工代号或姓名，记录是谁审的） |

### request_user_review —— 进入可选用户审核（script_analysis → script_review）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |

写完剧本后**询问用户是否需要审核剧情**；需要则调用本动作进入 `script_review` 并返回完整 `story`/`scenes`/`shots` 供呈现给用户。不需要则跳过，直接用 `review_script` 推进。

### approve_script —— 用户审核裁决（script_review → spatial_planning / script_analysis）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| approved | 是 | `true` 放行 → spatial_planning；`false` 打回 → script_analysis 重写 |
| note | 否 | 用户意见 / 修改建议 |

用户确认通过后 `approved=true` 才能进入后续 `set_floorplan`；打回则回到 `script_analysis` 重新 `write_script`。

### add_asset —— 锁定单个资产
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| kind | 是 | `CHAR`/`LOC`/`PROP` |
| id | 是 | 资产 ID，如 `CHAR001`（前缀+3 位数字） |
| name | 是 | 资产名 |
| appearance | 是 | **稳定外观短语**（prompt 直接引用的、保持一致的关键描述） |
| reference_image | 是 | 参考图路径（`generate_image` 生成的本地路径） |
| three_view | 是（CHAR） | **仅 CHAR**。`true` 表示 reference_image 是正/侧/背三视图合成图（视频生成强制） |
| voice | 是（CHAR） | **仅 CHAR**。声线参考（本地路径 / URL），做口型与音色一致 |
| position | 否 | **仅 LOC**。占地中心点 `{x,y}`（米）；**强制必填**（空间规划后 LOC 必须定位） |
| orientation | 否 | **仅 LOC**。建筑朝向角（0=北，90=东） |
| footprint | 否 | **仅 LOC**。占地尺寸 `{width,depth}`（米，以 `position` 为中心） |
| entrance | 否 | **仅 LOC**。入口点 `{x,y}` |

阶段约束：`LOC`/`PROP` 只能在 `world_building` 添加（全部齐备后自动 → `character_design`）；`CHAR` 只能在 `character_design` 添加。CHAR/PROP 的站位坐标是镜头级的（见 `write_script` 的 `spatial`），不在资产层设置。

### review_assets —— 资产审核 Gate → 通过后 stage=asset_lock
参数同 `review_script`（`score`/`passed`/`threshold`/`note`/`reviewer`）。要求所有 `CHAR###` 引用资产已添加。

### lock_assets —— 校验资产完备并锁定 → stage=storyboard（资产冻结）
参数仅 `project_id`。校验脚本引用的每个资产都已添加且有参考图，通过后锁定并推进。

### plan_shot —— 补摄影参数（storyboard 阶段）→ 全部齐备后自动 stage=video_generation
`project_id` + `shot_id` + 可选 `camera/lens/fps/movement/depth/lighting`。

### compile_prompt —— 编译规范化 Prompt（禁止 LLM 直接编最终提示词）
参数 `project_id` + `shot_id`。按固定规则编译出带 `@CHAR001 @LOC001` 锚点的 prompt，返回 `prompt` + `image_urls` + `audio_urls`，供 `generate_video` 直接调用。全部镜头编译后自动 → `quality_check`。

`image_urls` 装配顺序（自动，Agent 只需照搬）：**上一镜头尾帧（首帧引用，仅当 `continuity=true`）** → **空间坐标关系资产图**（`attach_spatial_map`，若存在）→ 人物/场景/道具参考图。`audio_urls` 来自 `attach_audio` 附加的音频资产（若存在）。

`continuity`（可选布尔）由智能体**逐镜头抉择**：本镜头与上一镜头时间/动作连续（如连续动作分镜）→ `true`，工具校验上一镜头已 `record_shot_frame` 并把尾帧放 `image_urls[0]`；跨场景、跨剧情段、硬切 → 省略或 `false`，不引入尾帧。设 `true` 但上一镜头未记录尾帧会被拒绝。

空间规划强制后，`compile_prompt` 先做**空间一致性硬门校验**（LOC 界内/不叠放、人物/道具不悬空、机位界内、运动不穿墙），不一致直接抛错拒绝；校验通过则把坐标/朝向翻译成自然语言方位短语注入 prompt，例如：

```
@CHAR001 …（站在 @LOC001 西南角，面向东）
@LOC001 …（位于平面图 (50,40)，朝向北）
Camera: tracking, 24fps（机位在 @LOC001 西南方 28 米）
```

### record_qc —— 视频审核 Gate（质检硬阻塞）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id / shot_id | 是 | 定位镜头（须 `prompted` 或 `qc_fail`） |
| character_score | 是 | 人物一致度 0–100 |
| scene_score | 是 | 场景一致度 0–100 |
| action_score | 是 | 动作一致度 0–100 |
| threshold | 否 | 通过阈值，默认 85 |
| note | 否 | 备注 / 重生成建议 |
| reviewer | 否 | 审核人（员工代号或姓名，记录是谁审的） |

任一分数 < 阈值 → `qc_fail`（返回「必须重生成」）；全部镜头通过后 → `final_edit`。

### record_shot_result —— 记录成片（final_edit 阶段）
参数 `project_id` + `shot_id` + `video_path`。要求镜头已 `qc_pass`，否则拒绝。全部镜头 `final` 后 `completed=true`。

### record_shot_frame —— 记录成片尾帧（视频生成后，video_generation/quality_check/final_edit 阶段）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id / shot_id | 是 | 定位镜头（须 `final` 状态，即已 `record_shot_result`） |
| last_frame | 是 | 本镜头成片**最后一帧图片路径**（Agent 用 shell 跑 ffmpeg 抽取，如 `ffmpeg -sseof -1 -i shot.mp4 -update 1 last_frame.jpg`） |

该尾帧会在**下一镜头** `compile_prompt(continuity=true)` 时被作为首帧引用（`image_urls[0]`），实现片段连贯。**只有需要衔接的镜头才需记录**——跨场景/跨剧情段可跳过本动作。

> **顺序生成**：需要连贯的镜头须**逐个**生成——`compile_prompt(Shot N, continuity=…) → generate_video → record_qc → record_shot_result → record_shot_frame`，再做 `Shot N+1`。`record_qc` / `record_shot_result` / `record_shot_frame` 因此在 `video_generation / quality_check / final_edit` 阶段均可用（阶段只是进度指示，真正硬门是镜头自身状态）。

### attach_audio —— 附加音频资产（storyboard / video_generation 阶段）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id / shot_id | 是 | 定位镜头 |
| audio | 是 | 音频资产路径（`text_to_speech` 生成的本地路径 / URL） |

分镜中 `dialogue=true` 的镜头**必须**在 `compile_prompt` 前调用本动作，否则 `compile_prompt` 拒绝（口型 + 音频一致性）。附加后 `compile_prompt` 会把它作为 `audio_urls` 返回，供 `generate_video` 使用。

### attach_spatial_map —— 附加空间坐标关系资产图（set_floorplan 之后任意阶段）
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| image | 是 | 空间坐标关系资产图路径（如平面图俯视图 `generate_image` 生成） |

附加后 `compile_prompt` 会把该图并入每个镜头的 `image_urls`（在参考图之前），作为空间布局参考。

### status —— 查看状态 + 下一步
参数仅 `project_id`。返回阶段、各镜头状态、已锁定资产数、审核结果，以及代码算出的 `next_action`。

## 完整流程示例

```
# 1. 建项目
cinematic_director(action="create_project", project_id="snow-forest",
                   name="雪林逃亡", logline="一家三口在暴雪中逃亡求生",
                   style="realistic movie scene", ratio="16:9")

# 2. 写剧本（补全剧情 story；镜头必须带 spatial 空间站位）
cinematic_director(action="write_script", project_id="snow-forest",
                   scenes=[{"id":"Scene001","location":"雪林","time":"黄昏",
                            "function":"建立危机感","duration":10}],
                   shots=[{"id":"Shot001","scene_id":"Scene001","shot_size":"大全景",
                           "action":"一家三口逃亡","emotion":"恐惧","sound":"风雪声",
                           "asset_refs":["CHAR001","CHAR002","LOC001"],
                           "spatial": {
                             "camera": {"position":{"x":30,"y":20}, "target":{"x":50,"y":40}},
                             "characters": [
                               {"asset_id":"CHAR001","position":{"x":50,"y":40},"facing":90},
                               {"asset_id":"CHAR002","position":{"x":45,"y":36},"facing":0}
                             ]}}],
                   story={"plot":"一家三口在暴雪中逃亡求生，最终在雪林小屋反击脱险",
                          "characters":[{"id":"CHAR001","name":"爸爸","prompt":"45 岁男人，黑色羽绒服"},
                                        {"id":"CHAR002","name":"妈妈","prompt":"40 岁女人，深灰大衣"}],
                          "environments":[{"id":"LOC001","name":"雪林","prompt":"黄昏暴雪的雪林，冷色月光"}],
                          "spatial":"雪林在平面图东北部，人物从东侧入画向西逃亡",
                          "worldview":"末日废土+暴雪求生，冷色主调"})

# 3. 剧本审核。可选：先询问用户是否审核剧情——
#    需要 → request_user_review 呈现剧情 → 等用户确认 → approve_script(approved=true)
#    不需要 → 直接 review_script
cinematic_director(action="review_script", project_id="snow-forest", score=90)

# 3'. 若用户选择审核（示例）：
# cinematic_director(action="request_user_review", project_id="snow-forest")  # 呈现剧情给用户
# ... 用户确认后 ...
# cinematic_director(action="approve_script", project_id="snow-forest", approved=true)

# 4. 空间规划（强制）：声明全局平面图 → 进入 world_building
cinematic_director(action="set_floorplan", project_id="snow-forest", width=100, length=80)

# 5. 生成参考图并添加资产（先世界 LOC/PROP，再角色 CHAR；LOC 必须带坐标）
generate_image(prompt="黄昏暴雪的雪林，冷色月光", aspect_ratio="1:1")
cinematic_director(action="add_asset", project_id="snow-forest", kind="LOC", id="LOC001",
                   name="雪林", appearance="黄昏暴雪的雪林，冷色月光",
                   reference_image="<场景参考图路径>",
                   position={"x":50,"y":40}, footprint={"width":20,"depth":16})
generate_image(prompt="45岁男人，黑色羽绒服，正/侧/背三视图合成图，写实", aspect_ratio="1:1")
# 声线参考：用 text_to_speech 生成角色声线样本，作为 voice（口型 + 音色一致）
cinematic_director(action="add_asset", project_id="snow-forest", kind="CHAR", id="CHAR001",
                   name="爸爸", appearance="45 岁男人，黑色羽绒服，胡茬严肃",
                   reference_image="<角色三视图合成图路径>", three_view=true,
                   voice="<声线参考路径>")
# ... 为每个资产重复 add_asset ...

# 6. 资产审核 + 锁定
cinematic_director(action="review_assets", project_id="snow-forest", score=90)
cinematic_director(action="lock_assets", project_id="snow-forest")

# 7. 分镜 + 编译 Prompt（compile_prompt 先做空间一致性硬门校验）
cinematic_director(action="plan_shot", project_id="snow-forest", shot_id="Shot001",
                   movement="handheld tracking shot", fps=24, lighting="cold cinematic lighting")
cinematic_director(action="compile_prompt", project_id="snow-forest", shot_id="Shot001")
# → { prompt: "@CHAR001 ... @LOC001 ...\nCamera: ...", image_urls: [...] }
# prompt 中已注入方位短语，如「@CHAR001 …（站在 @LOC001 中央，面向东）」

# 8. 生成视频（image_urls / audio_urls 直接照搬 compile_prompt 的返回）
generate_video(prompt="<上一步的 prompt>", image_urls=["<上一步的 image_urls>"],
               audio_urls=["<上一步的 audio_urls>"], ratio="16:9", duration=10)

# 9. 视频审核（结合视觉能力读视频首帧与参考图对比后打分）
cinematic_director(action="record_qc", project_id="snow-forest", shot_id="Shot001",
                   character_score=95, scene_score=90, action_score=88)

# 10. 记录成片（qc_pass 才被接受）
cinematic_director(action="record_shot_result", project_id="snow-forest",
                   shot_id="Shot001", video_path="<生成的视频路径>")

# 10'. 抽取成片尾帧并记录（下一镜头首帧连贯硬门——不记录则下一镜头 compile_prompt 被拒）
# shell: ffmpeg -sseof -1 -i <生成的视频路径> -update 1 shot001_last_frame.jpg
cinematic_director(action="record_shot_frame", project_id="snow-forest",
                   shot_id="Shot001", last_frame="<shot001_last_frame.jpg 路径>")

# 11. 下一镜头：连续动作 → continuity=true，工具把 Shot001 尾帧作为 image_urls[0] 首帧引用
cinematic_director(action="compile_prompt", project_id="snow-forest", shot_id="Shot002", continuity=true)
# → image_urls[0] = Shot001 尾帧，实现片段连贯；再 generate_video → record_qc → record_shot_result → record_shot_frame …
# 跨场景/跨剧情/硬切则省略 continuity（或 continuity=false），不引入上一镜头尾帧
```

## 项目记忆（分目录）

项目状态持久化在 `workspace/cinematic/<project_id>/`：

```
project.json                  # 索引：stage/completed/scenes/时间戳
bible.json                    # 导演圣经
story.json                    # 剧情补充层（write_script 的 story）
script.md                     # 剧本资产文档（write_script 生成的 Markdown，供用户查阅剧情）
map.json                      # 平面图（set_floorplan 后存在，含 map_image 空间坐标关系图）
characters/CHAR001.json       # 角色资产
locations/LOC001.json         # 场景资产
props/PROP001.json            # 道具资产
shots/Shot001.json            # 分镜 + 摄影参数 + prompt + image_urls/audio_urls + video_path + last_frame/audio
videos/                       # 成片目录
reviews/                      # script_review.json / asset_review.json / shot_<id>_review.json
```

## 注意事项

- 资产 ID 是**全片一致性锚点**：一旦锁定，后续镜头一律复用同一 ID，不重新描述人物/场景，避免漂移。
- 质检/审核分数由 Agent 结合视觉能力（`screenshot`/`read_file` 读视频首帧与资产参考图对比）判定后填入；本工具只做记录 + 硬阻塞，不做自动判分。
- **首尾帧连贯（可选）**：只有需要衔接的镜头才 `record_shot_frame`，并在下一镜头 `compile_prompt(continuity=true)` 时复用尾帧作为首帧。若某镜头 QC 不通过需重生成，其尾帧会改变 → 重新抽取并 `record_shot_frame`，下游依赖它的镜头再重新 `compile_prompt`（首帧引用随之更新）。跨场景/跨剧情/硬切无需此步。
- 如需**分工协作**，可用 `spawn` 让剧本/资产/QA 子代理产出内容（起草剧本、生成参考图、打分），再由导演用本工具落库——子代理因 `_scopes` 限制无法直接改项目状态，天然满足权限隔离。
