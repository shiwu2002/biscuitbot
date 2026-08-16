# 人才市场注册表 JSON 格式（开发文档）

本文档定义数字员工「人才市场」注册表的 JSON 格式。人才市场项目启动后，
注册表会托管在某个 `http(s)://` 地址上，由后台 CLI 配置
（`biscuitbot talent-market set <url>`），WebUI / 桌面应用只读拉取。

## 1. 顶层结构

注册表是一个 JSON 对象：

```json
{
  "schema": "talent-market.v1",
  "meta": {
    "updated": "2026-08-16T00:00:00Z"
  },
  "employees": [
    { /* 员工条目，见第 3 节 */ }
  ]
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema` | string | 是 | 固定为 `"talent-market.v1"`，用于版本识别 |
| `meta` | object | 否 | 目录级元信息 |
| `meta.updated` | string | 否 | 目录最后更新时间（建议 ISO-8601），用于前端展示 |
| `employees` | array | 是 | 员工条目数组；兼容旧键名 `talent` |

后端兼容性：当 `employees` 缺失时，会回退读取 `talent` 键。

## 2. 员工条目字段

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `id` | string | 否 | 员工英文代号（slug：小写字母/数字/`-`/`_`）。缺省时按 `name` 生成 ASCII slug（中文名剥离后可能回退为 `employee`） |
| `name` | string | 是 | 员工代号 / 姓名 |
| `title` | string | 否 | 职位小标签（如「剪辑」） |
| `avatar` | string | 否 | 头像（emoji 或 URL） |
| `description` | string | 否 | 一句话简介（卡片副标题） |
| `category` | string | 否 | 分类标签（卡片上的分类 chip） |
| `system_prompt` | string | 是 | 员工沉浸式 persona 提示词。**为空时条目不可安装** |
| `skills` | array | 否 | 技能列表，见第 4 节 |

示例：

```json
{
  "id": "clip-master-pro",
  "name": "阿伟 Pro",
  "title": "AI 视频剪辑总监",
  "avatar": "🎬",
  "description": "一键把素材加工成爆款视频",
  "category": "视频",
  "system_prompt": "你是「阿伟 Pro」，团队里的 AI 视频剪辑总监……",
  "skills": ["jianying-editor"]
}
```

## 3. `skills` 字段

`skills` 是一个数组，**每个元素可以是字符串或对象**，两种形式可混用：

### 3.1 字符串（引用已有技能）

```json
"skills": ["jianying-editor", "web_search"]
```

表示该员工绑定这些**技能名**。这些技能必须在本地已可用（内置技能，或之前已
安装到工作区的技能）。安装员工时，后端只把技能名写入员工的 `skills` 字段，
**不会**额外下载任何文件。

### 3.2 对象（自带技能 bundle）

```json
"skills": [
  {
    "name": "my-custom-skill",
    "files": {
      "SKILL.md": "---\nname: my-custom-skill\ndescription: ...\n---\n\n# 技能正文",
      "references/guide.md": "# 指南",
      "scripts/run.py": "print('hello')"
    }
  }
]
```

表示该员工**自带**一个技能。安装员工时，后端会把这个技能的所有文件写入
`workspace/skills/<name>/`，并记录「该技能归属于这名员工」。技能名同时会加入
员工的 `skills` 字段。

对象字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `name` | string | 是 | 技能名（slug：小写字母/数字/`-`/`_`） |
| `files` | object | 是 | 相对路径 → 文件内容（UTF-8 文本）的映射 |

`files` 约束：

- **必须包含 `SKILL.md`**（否则该技能会被视为无效并跳过落盘）。
- 键为相对路径，使用 `/` 分隔；路径段只允许 `[A-Za-z0-9._-]`。
- 禁止 `..`、空段、绝对路径（以 `/` 开头），后端会校验并拒绝越出技能目录的路径。
- 值为 UTF-8 字符串；非字符串值会被忽略。

### 3.3 归属与删除规则

自带技能（bundle）会记录归属关系（`workspace/skill_owners.json`，映射
`技能名 → 员工 id`）。基于此，业务规则为：

1. **员工删除时，其自带技能会自动删除**（级联清理技能目录 + 归属记录）。
2. **员工未删除时，其自带技能不可单独删除**（后端返回 `409 Conflict`）。

引用已有技能的字符串形式不建立归属关系，不受此规则影响。

## 4. 完整示例

```json
{
  "schema": "talent-market.v1",
  "meta": {
    "updated": "2026-08-16T00:00:00Z"
  },
  "employees": [
    {
      "id": "copywriter",
      "name": "文案专员",
      "title": "营销文案",
      "avatar": "✍️",
      "description": "面向营销场景的文案专家",
      "category": "内容",
      "system_prompt": "你是文案专员，负责产出高转化文案……",
      "skills": ["web_search", "browser"]
    },
    {
      "id": "analyst-pro",
      "name": "数据分析师 Pro",
      "title": "数据分析",
      "avatar": "📊",
      "description": "自带 pandas 分析技能",
      "category": "数据",
      "system_prompt": "你是数据分析师……",
      "skills": [
        {
          "name": "pandas-analyst",
          "files": {
            "SKILL.md": "---\nname: pandas-analyst\ndescription: 数据分析技能\n---\n\n# pandas-analyst\n\n按以下步骤分析数据……",
            "references/eda.md": "# 探索性数据分析清单"
          }
        }
      ]
    }
  ]
}
```

## 5. 后端处理流程

1. **目录拉取**（`talent_catalog_payload`）：拉取注册表 JSON，规范化每个条目，
   `skills` 被展平为**技能名数组**（对象形式只取 `name`，不向前端泄露文件内容），
   并标注 `installed`（该员工是否已本地安装）。
2. **安装**（`install_talent_employee`）：
   - 前端只回传 `id/name/avatar/system_prompt/skills(名称)` + `source_url`；
   - 后端以 `source_url` 重新拉取（命中缓存）注册表，按 `id` 定位原始条目，
     从 `skills` 中解析出 bundle 对象；
   - 把 bundle 技能文件写入 `workspace/skills/<name>/` 并记录归属；
   - 最后落库员工（`workspace/employees.json`），重复 `id` 幂等。
3. **删除员工**：级联删除其自带技能目录 + 归属记录。
4. **删除技能**：若技能归属的员工仍存在，拒绝删除（`409`）。

## 6. 安全约束

- 注册表 URL 仅允许 `http/https`（拒绝 `file://`、`ftp://` 等）。
- 拉取带 15s 超时、2MB 大小上限；缓存写到
  `<config dir>/talent-market/registry_cache.json`，不触碰 `config.json`。
- 技能落盘前校验技能名与每个文件相对路径，防路径穿越（`..`、绝对路径）。
- 安装只写员工文件与工作区技能目录，不触碰任何配置写入逻辑。
