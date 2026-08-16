# cinematic_director

AI 导演工作流编排工具。把「导演圣经 → 剧本 → 剧本审核 → 世界观/角色资产 → 资产审核 → 资产锁定 → 分镜 → 视频生成 → 视频审核 → 成片」固化为**代码强约束的 9 阶段状态机**：阶段顺序、三级审核 Gate、数据冻结都由本工具硬校验，非法操作被拒绝，解决 AI 视频人物/场景/光影漂移与流程偏移问题。

## 何时使用

用户想拍短剧、把小说/故事/剧本改编成视频、做 AI 影视/分镜/预告片生产时使用。本工具**不生成视频/图片/音频**——它只做编排与管控；视频/图片/配音分别交给 `generate_video` / `generate_image` / `text_to_speech`。

## 9 阶段状态机

项目 `stage`：

```
init → script_analysis → world_building → character_design → asset_lock
     → storyboard → video_generation → quality_check → final_edit
```

全部镜头 `final` 后 `completed=true`。镜头 `shot.status`：`pending → prompted → qc_pass/qc_fail → final`。

**硬门（代码强制，不可绕过）：**

1. **三级审核 Gate**：`review_script`（剧本审核）不过 → 不能进入 world_building；`review_assets`（资产审核）不过 → 不能进入 asset_lock；`record_qc`（视频审核）任一分数 < 阈值（默认 85）→ 镜头 `qc_fail`，`record_shot_result` 拒绝推进。
2. **资产优先 + 参考图强制**：`add_asset` 必须有 `reference_image`；`lock_assets` 校验脚本引用的每个资产都已添加且有参考图。
3. **数据冻结**：`review_script` 通过后 `write_script` 拒绝改剧本；`lock_assets` 后 `add_asset` 拒绝改资产；资产一旦创建**无「编辑/删除」动作**，角色定义天然不可变。
4. **权限隔离**：本工具 `_scopes={"core"}`，只有主 Agent（导演）能改项目状态；`spawn` 出的子 Agent 无法调用本工具，只能用 `generate_image`/`read_file`/视觉能力**产出内容后回报**，由导演落库。

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

### write_script —— 写场景 + 分镜（叙事层）→ stage=script_analysis
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| scenes | 是 | 场景列表：`{id, location, time, function, duration}` |
| shots | 是 | 分镜列表：`{id, scene_id, shot_size, camera, lens, action, emotion, sound, asset_refs}` |

约束：每个 shot 的 `scene_id` 必须存在；`asset_refs` 必须含至少一个 `CHAR###` 与一个 `LOC###`。

### review_script —— 剧本审核 Gate → 通过后 stage=world_building
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| score | 是 | 审核分数 0–100 |
| passed | 否 | 是否通过（`False` 一票否决） |
| threshold | 否 | 通过阈值，默认 85 |
| note | 否 | 备注 / 修改建议 |
| reviewer | 否 | 审核人（员工代号或姓名，记录是谁审的） |

### add_asset —— 锁定单个资产
| 参数 | 必填 | 说明 |
|------|------|------|
| project_id | 是 | 项目 ID |
| kind | 是 | `CHAR`/`LOC`/`PROP` |
| id | 是 | 资产 ID，如 `CHAR001`（前缀+3 位数字） |
| name | 是 | 资产名 |
| appearance | 是 | **稳定外观短语**（prompt 直接引用的、保持一致的关键描述） |
| reference_image | 是 | 参考图路径（`generate_image` 生成的本地路径） |

阶段约束：`LOC`/`PROP` 只能在 `world_building` 添加（全部齐备后自动 → `character_design`）；`CHAR` 只能在 `character_design` 添加。

### review_assets —— 资产审核 Gate → 通过后 stage=asset_lock
参数同 `review_script`（`score`/`passed`/`threshold`/`note`/`reviewer`）。要求所有 `CHAR###` 引用资产已添加。

### lock_assets —— 校验资产完备并锁定 → stage=storyboard（资产冻结）
参数仅 `project_id`。校验脚本引用的每个资产都已添加且有参考图，通过后锁定并推进。

### plan_shot —— 补摄影参数（storyboard 阶段）→ 全部齐备后自动 stage=video_generation
`project_id` + `shot_id` + 可选 `camera/lens/fps/movement/depth/lighting`。

### compile_prompt —— 编译规范化 Prompt（禁止 LLM 直接编最终提示词）
参数 `project_id` + `shot_id`。按固定规则编译出带 `@CHAR001 @LOC001` 锚点的 prompt，返回 `prompt` + `image_urls`，供 `generate_video` 直接调用。全部镜头编译后自动 → `quality_check`。

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

### status —— 查看状态 + 下一步
参数仅 `project_id`。返回阶段、各镜头状态、已锁定资产数、审核结果，以及代码算出的 `next_action`。

## 完整流程示例

```
# 1. 建项目
cinematic_director(action="create_project", project_id="snow-forest",
                   name="雪林逃亡", logline="一家三口在暴雪中逃亡求生",
                   style="realistic movie scene", ratio="16:9")

# 2. 写剧本
cinematic_director(action="write_script", project_id="snow-forest",
                   scenes=[{"id":"Scene001","location":"雪林","time":"黄昏","function":"建立危机感","duration":10}],
                   shots=[{"id":"Shot001","scene_id":"Scene001","shot_size":"大全景",
                           "action":"一家三口逃亡","emotion":"恐惧","sound":"风雪声",
                           "asset_refs":["CHAR001","CHAR002","LOC001"]}])

# 3. 剧本审核（不过则回写剧本再审）
cinematic_director(action="review_script", project_id="snow-forest", score=90)

# 4. 生成参考图并添加资产（先世界 LOC/PROP，再角色 CHAR）
generate_image(prompt="黄昏暴雪的雪林，冷色月光", aspect_ratio="1:1")
cinematic_director(action="add_asset", project_id="snow-forest", kind="LOC", id="LOC001",
                   name="雪林", appearance="黄昏暴雪的雪林，冷色月光",
                   reference_image="<场景参考图路径>")
generate_image(prompt="45岁男人，黑色羽绒服，三视图，写实", aspect_ratio="1:1")
cinematic_director(action="add_asset", project_id="snow-forest", kind="CHAR", id="CHAR001",
                   name="爸爸", appearance="45 岁男人，黑色羽绒服，胡茬严肃",
                   reference_image="<角色参考图路径>")
# ... 为每个资产重复 add_asset ...

# 5. 资产审核 + 锁定
cinematic_director(action="review_assets", project_id="snow-forest", score=90)
cinematic_director(action="lock_assets", project_id="snow-forest")

# 6. 分镜 + 编译 Prompt
cinematic_director(action="plan_shot", project_id="snow-forest", shot_id="Shot001",
                   movement="handheld tracking shot", fps=24, lighting="cold cinematic lighting")
cinematic_director(action="compile_prompt", project_id="snow-forest", shot_id="Shot001")
# → { prompt: "@CHAR001 ... @LOC001 ...\nCamera: ...", image_urls: [...] }

# 7. 生成视频
generate_video(prompt="<上一步的 prompt>", image_urls=["<上一步的 image_urls>"], ratio="16:9", duration=10)

# 8. 视频审核（结合视觉能力读视频首帧与参考图对比后打分）
cinematic_director(action="record_qc", project_id="snow-forest", shot_id="Shot001",
                   character_score=95, scene_score=90, action_score=88)

# 9. 记录成片（qc_pass 才被接受）
cinematic_director(action="record_shot_result", project_id="snow-forest",
                   shot_id="Shot001", video_path="<生成的视频路径>")
```

## 项目记忆（分目录）

项目状态持久化在 `workspace/cinematic/<project_id>/`：

```
project.json                  # 索引：stage/completed/scenes/时间戳
bible.json                    # 导演圣经
characters/CHAR001.json       # 角色资产
locations/LOC001.json         # 场景资产
props/PROP001.json            # 道具资产
shots/Shot001.json            # 分镜 + 摄影参数 + prompt + 成片路径
videos/                       # 成片目录
reviews/                      # script_review.json / asset_review.json / shot_<id>_review.json
```

## 注意事项

- 资产 ID 是**全片一致性锚点**：一旦锁定，后续镜头一律复用同一 ID，不重新描述人物/场景，避免漂移。
- 质检/审核分数由 Agent 结合视觉能力（`screenshot`/`read_file` 读视频首帧与资产参考图对比）判定后填入；本工具只做记录 + 硬阻塞，不做自动判分。
- 如需**分工协作**，可用 `spawn` 让剧本/资产/QA 子代理产出内容（起草剧本、生成参考图、打分），再由导演用本工具落库——子代理因 `_scopes` 限制无法直接改项目状态，天然满足权限隔离。
