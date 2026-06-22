# 12. 常见问题

**Q: 智能体如何调用平台工具？**
A: 平台在对话请求中传递 `available_skills`（工具组目录）、`tools_discovery_endpoint`（工具详情查询端点）、`tools_execution_endpoint`（工具执行端点）和 `tools_auth_token`（认证令牌）。智能体按需查询工具详情，然后使用 `tools_auth_token` 作为认证调用工具执行端点。

**Q: 调用工具时需要传 user_id 吗？**
A: **必须携带**。`user_id` 是 `knowledge_search` 等工具的必填参数（`required: ["query", "user_id"]`），智能体必须从对话请求的 `user_id` 字段获取并在 `arguments` 中携带。如果缺失，平台将返回错误：`"缺少必填参数: user_id，智能体必须在调用时携带"`。

**Q: 为什么不能让平台自动注入 user_id？**
A: 平台自动注入无法区分"谁在调用工具"和"知识库集合归属谁"。必须由智能体显式传递 `user_id`，这样：1) 调用者身份明确；2) 集合归属准确（平台用 `user_id` 拼接集合名 `kb_{user_id}_{collection_name}`）；3) 责任链清晰。

**Q: tools_auth_token 和 JWT Token 有什么区别？**
A: `tools_auth_token` 是平台在对话请求中传递的认证令牌，通常是用户的 API Key。智能体调用平台工具时应使用此令牌。JWT Token 是平台调用智能体时使用的认证令牌。两者可互换使用（都支持 API Key 认证），但建议工具调用时使用 `tools_auth_token`。

**Q: 不实现工具调用会影响对话吗？**
A: 不会。`available_skills`、`tools_discovery_endpoint`、`tools_execution_endpoint` 和 `tools_auth_token` 字段可忽略，不影响正常的对话功能。

**Q: 工具定义会频繁变化吗？**
A: 工具定义由平台管理员维护，变化频率低。平台对工具 API 做了 Redis 缓存（20-30 分钟随机 TTL），智能体端不需要频繁查询。

**Q: JWT Token 过期了怎么办？**
A: 平台每次调用智能体时都会生成新的 JWT Token，有效期1小时。智能体端只需验证 Token 有效性，无需处理刷新逻辑。

**Q: 智能体端如何获取共享密钥？**
A: 开发环境可直接使用与平台相同的密钥字符串；生产环境通过环境变量 `JWT_AGENT_SHARED_SECRET` 注入，与平台保持一致。

**Q: 试用场景和正式场景的 JWT 有什么区别？**
A: v6.0.0 起，试用场景传递真实用户ID（从JWT认证上下文获取），与正式场景一致。早期版本试用场景 `user_id` 为 `"trial"`，现已废弃。

**Q: 私有工具组如何访问？**
A: 管理员在管理后台将私有工具组绑定到指定用户。当该用户对应的智能体调用工具时，平台会校验绑定关系，有绑定记录且 `enabled=1` 时允许访问。

**Q: 检索结果中的溯源信息怎么用？**
A: 每个检索结果 chunk 的 `metadata` 包含 `pageNumber`（页码）、`chapter`（章节）、`sourceFilename`（源文件）等信息。智能体可在回复中引用出处，例如"根据《产品手册》第3页的描述..."，提升回复的可信度。

**Q: 智能体如何使用 CLI 工具？**
A: 智能体会收到系统 Skill "CLI工具市场"（name=cli-market），包含两个工具：1) `cli_tools_list` — 列出管理员已启用的 CLI 工具；2) `cli_tools_install` — 获取指定 CLI 工具的安装元数据（install_cmd、entry_point、commands）。智能体拿到元数据后，自行 `pip install` 并在本地执行 CLI 命令。平台不代为安装或执行。

**Q: 为什么 cli_tools_list 返回为空？**
A: 管理员需要在后台"CLI 工具市场"页面先手动同步注册表，然后启用需要的工具。新同步的工具默认未启用（is_enabled=false），`cli_tools_list` 只返回已启用且同步成功的工具。

**Q: 为什么检索一直走兜底回复？**
A: 最常见的原因是 `user_id` 未正确传递，导致 Milvus 集合名拼接错误。请确保：1) 调用工具时在 `arguments` 中携带 `user_id`（从对话请求的 `user_id` 字段获取）；2) 使用 `tools_auth_token` 作为认证令牌调用工具；3) `collection_name` 可选，默认为 `"default"`；4) 确认该用户确实有知识库数据（检查 Milvus 中是否存在 `kb_{user_id}_default` 集合）。
