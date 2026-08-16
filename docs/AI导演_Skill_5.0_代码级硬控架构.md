# AI导演 Skill 5.0 代码级硬控架构设计

## 核心理念

纯 Markdown Skill 只能提供认知约束，无法保证智能体严格执行影视生产流程。

商业级 AI 导演数字员工应该采用：

> Skill 定义能力，代码控制流程，数据记录状态，审核机制保证质量。

## 总体架构

    AI导演大脑(LLM)

            |

    Skill规则层

            |

    Workflow Engine

            |

    剧本Agent / 资产Agent / 分镜Agent / 视频Agent / QA Agent

            |

    状态数据库 + 资产库 + 审核系统

## 一、工作流状态机硬控

导演流程：

    INIT
    ↓
    SCRIPT_ANALYSIS
    ↓
    WORLD_BUILDING
    ↓
    CHARACTER_DESIGN
    ↓
    ASSET_LOCK
    ↓
    STORYBOARD
    ↓
    VIDEO_GENERATION
    ↓
    QUALITY_CHECK
    ↓
    FINAL_EDIT

代码必须阻止跳跃执行。

例如：

``` python
if asset_lock == False:
    raise WorkflowError("禁止生成视频，请先完成资产锁定")
```

## 二、结构化输出约束

所有 Agent 必须输出 Schema 数据。

示例：

``` json
{
 "project":"雪林逃亡",
 "characters":[
  {
   "id":"CHAR001",
   "name":"爸爸"
  }
 ],
 "scenes":[
  {
   "id":"SC001",
   "location":"雪林",
   "duration":10
  }
 ]
}
```

## 三、资产锁定系统

建立 Asset Registry：

    CHAR001

    角色:
    爸爸

    服装:
    黑色羽绒服

    face_hash:
    abc123

    status:
    locked

未锁定资产禁止进入视频生成阶段。

## 四、Prompt 编译器

禁止 LLM 直接生成最终视频提示词。

流程：

    镜头数据
    +
    资产ID
    +
    摄影参数

    ↓

    Prompt Compiler

    ↓

    视频模型Prompt

## 五、质量审核 Gate

阶段审核：

-   剧本审核
-   资产审核
-   视频审核

低于质量阈值：

    重新生成

## 六、Agent权限隔离

剧本 Agent：

-   读取项目资料
-   写入剧本

禁止：

-   修改资产

资产 Agent：

-   创建资产

禁止：

-   修改剧情

视频 Agent：

-   读取资产
-   生成视频

禁止：

-   修改角色定义

## 七、项目记忆

目录：

    project/

    ├── bible.json
    ├── characters/
    ├── locations/
    ├── props/
    ├── shots/
    ├── videos/
    └── reviews/

## 八、推荐技术架构

    FastAPI

    +

    LangGraph

    +

    PostgreSQL

    +

    Vector Database

    +

    Object Storage

    +

    Workflow Engine

## 九、最终结构

    AI_Director/

    ├── skill.md
    ├── workflow.py
    ├── state.py
    ├── schemas.py
    ├── validators.py

    ├── agents/
    │   ├── script.py
    │   ├── asset.py
    │   ├── storyboard.py
    │   ├── video.py
    │   └── editor.py

    ├── memory/
    │   └── project_db.py

    └── prompts/
        ├── director.yaml
        ├── camera.yaml
        └── seedance.yaml

## 总结

AI导演 Skill 不应该只是：

> 会写视频提示词的聊天机器人

而应该成为：

> 拥有流程状态、资产管理、权限控制、质量审核和长期记忆的影视生产操作系统。

核心原则：

> LLM负责思考，代码负责纪律。
