# 3. JWT 鉴权机制

## 3.1 概述

平台调用智能体所有接口时，通过 `Authorization` 请求头传递 JWT Token，不再明文传递 API Key。智能体端使用与平台共享的签名密钥验证 JWT 的有效性，从中提取用户身份和 API Key 信息。

**算法**：HMAC-SHA256（HS256），签名密钥为字符串经 UTF-8 编码的字节。

**适用范围**：所有业务接口（对话、流式对话、会话历史、清除会话、文档上传）均需验证 JWT。健康检测和元信息接口不需要 JWT 验证（仍使用注册时配置的 `auth_header`，如已配置）。

## 3.2 JWT Payload 结构

```json
{
  "sub": "user_1234567890123456789",
  "iss": "hczk-platform",
  "iat": 1781593200,
  "exp": 1781596800,
  "role": 1,
  "userId": "1234567890123456789"
}
```

**字段说明**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sub` | string | 是 | 主体标识，格式为 `user_{username}` |
| `iss` | string | 是 | 签发者，固定为 `hczk-platform` |
| `iat` | number | 是 | 签发时间（Unix 时间戳，秒级） |
| `exp` | number | 是 | 过期时间（Unix 时间戳，秒级），默认1小时 |
| `role` | integer | 是 | 用户角色：0=管理员，1=普通用户 |
| `userId` | string | 是 | 用户 ID（雪花ID），用于用户隔离和知识库查找 |

> **注意**：平台调用智能体时使用的是**智能体调用 JWT**（通过 `generateAgentToken` 生成），其 payload 结构与登录 JWT 不同：

**智能体调用 JWT Payload**

```json
{
  "sub": "user_1234567890123456789",
  "iss": "hczk-platform",
  "iat": 1781593200,
  "exp": 1781596800,
  "userId": "1234567890123456789",
  "scope": "chat",
  "apiKey": "sk-hczk-xxx"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `sub` | string | 是 | 主体标识，格式为 `user_{userId}` |
| `iss` | string | 是 | 签发者，固定为 `hczk-platform` |
| `iat` | number | 是 | 签发时间（Unix 时间戳，秒级） |
| `exp` | number | 是 | 过期时间（Unix 时间戳，秒级），默认1小时 |
| `userId` | string | 是 | 用户 ID（雪花ID），用于用户隔离和知识库查找 |
| `scope` | string | 是 | 权限范围，当前固定为 `chat` |
| `apiKey` | string | 否 | 平台 API Key（访问知识库和模型时使用），试用场景为空 |

> **v6.0.0 变更**：智能体调用 JWT 中用户标识 claim 从 `user_id` 修正为 `userId`，与登录 JWT 保持一致，便于 JwtAuthenticationFilter 统一解析。

## 3.3 请求头格式

```
Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdW...省略...xxx
```

> 注意：JWT 方式始终带有 `Bearer ` 前缀。

## 3.4 共享密钥配置

平台和智能体端必须配置相同的 HMAC-SHA256 签名密钥：

**平台端配置**

| 环境 | 路径 | 说明 |
|------|------|------|
| 开发环境 | `application-dev.yaml` → `jwt.secret` | 默认值为 `hczk-ai-platform-secret-key-2024-very-long-and-secure` |
| 生产环境 | 环境变量 `JWT_AGENT_SHARED_SECRET` | 若未设置则回退到 `JWT_SECRET` |

**智能体端配置**

| 环境 | 配置方式 | 示例值 |
|------|---------|--------|
| 开发环境 | 硬编码或配置文件 | `hczk-ai-platform-secret-key-2024-very-long-and-secure` |
| 生产环境 | 环境变量 `JWT_AGENT_SHARED_SECRET` | 与平台保持一致 |

**相关配置项**

| 配置项 | 默认值 | 说明 |
|------|--------|------|
| `jwt.agent.shared-secret` | 复用 `jwt.secret` | 智能体调用 JWT 的签名密钥 |
| `jwt.agent.expiration` | `3600000`（1 小时） | Token 过期时间（毫秒） |
| `jwt.agent.issuer` | `hczk-platform` | JWT 签发者标识 |

## 3.5 平台端 JWT 识别机制

平台的 `JwtAuthenticationFilter` 支持识别两种 JWT Token：

1. **登录 JWT**：使用 `jwt.secret` 签名，包含 `role` 和 `userId` claim，验证成功后赋予 `ROLE_ADMIN` 或 `ROLE_USER` 权限
2. **智能体调用 JWT（agentToken）**：使用 `jwt.agent.shared-secret` 签名，包含 `userId` 和 `scope` claim，无 `role` 信息，按 `ROLE_USER` 处理

验证流程：先尝试用 `jwt.secret` 验证（登录 JWT），失败后尝试用 `jwt.agent.shared-secret` 验证（agentToken）。任一验证成功即可提取 `userId` 建立认证上下文。

## 3.6 智能体端验证流程

```
1. 从 Authorization 请求头提取 Bearer Token
   - 检查格式：必须以 "Bearer " 开头
   - 截取 "Bearer " 之后的部分作为 JWT Token

2. 使用共享密钥验证 JWT 签名
   - 算法：HS256
   - 密钥：共享密钥字符串的 UTF-8 字节

3. 检查 iss（签发者）== "hczk-platform"
   - 防止其他来源的 JWT 被误接受

4. 检查 exp（过期时间）未过期
   - 建议允许 30 秒时钟偏差

5. 从 payload 中提取 user_id 和 apiKey
   - user_id 用于用户隔离和知识库查找（必填）
   - apiKey 用于调用平台服务（可空）

6. 使用 user_id 做用户隔离，并根据 user_id 查找用户的知识库集合
7. 使用 apiKey 调用平台知识库/模型服务（如需要）
```

**验证失败时的响应**

| 失败原因 | HTTP 状态码 | 响应体示例 |
|---------|------------|-----------|
| 缺少认证信息 | 401 | `{"error": "缺少认证信息"}` |
| JWT 签名无效 | 401 | `{"error": "JWT 验证失败: 签名无效"}` |
| 签发者不匹配 | 401 | `{"error": "JWT 验证失败: 无法识别的签发者"}` |
| Token 已过期 | 401 | `{"error": "JWT 验证失败: Token 已过期"}` |

## 3.6 试用场景

管理后台试用智能体时，平台从 JWT 认证上下文中获取当前登录用户的真实 ID，生成包含真实用户信息的 JWT Token：

- `user_id` 为当前登录用户的真实雪花ID（如 `U1A2B3C4D5E6F7G8`）
- `apiKey` 为空（不包含该字段）
- `sub` 为 `"user_{真实用户ID}"`

```json
{
  "sub": "user_U1A2B3C4D5E6F7G8",
  "iss": "hczk-platform",
  "user_id": "U1A2B3C4D5E6F7G8",
  "scope": "chat"
}
```

> **v6.0.0 变更**：试用场景不再使用 `user_id: "trial"`，而是传递真实用户 ID，确保试用时也能正确检索该用户的知识库集合。

## 3.7 代码示例

**Java 生成（平台端，JJWT 0.12.x）**

```java
// 登录 JWT（含 userId claim）
SecretKey key = Keys.hmacShaKeyFor(secret.getBytes(StandardCharsets.UTF_8));
String token = Jwts.builder()
    .subject(username)
    .claim("role", role)
    .claim("userId", userId)
    .issuedAt(new Date())
    .expiration(new Date(System.currentTimeMillis() + expiration))
    .signWith(key)
    .compact();

// 智能体调用 JWT
SecretKey agentKey = Keys.hmacShaKeyFor(agentSharedSecret.getBytes(StandardCharsets.UTF_8));
String agentToken = Jwts.builder()
    .subject("user_" + userId)
    .issuer("hczk-platform")
    .issuedAt(new Date())
    .expiration(new Date(System.currentTimeMillis() + 3600000))
    .claim("user_id", userId)
    .claim("scope", "chat")
    .claim("apiKey", apiKey)  // 可选
    .signWith(agentKey)
    .compact();
```

**Node.js 验证（智能体端，jsonwebtoken）**

```js
const jwt = require('jsonwebtoken');
const payload = jwt.verify(token, SHARED_SECRET, {
    algorithms: ['HS256'],
    issuer: 'hczk-platform',
    clockTolerance: 30
});
```

**Python 验证（智能体端，PyJWT）**

```python
import jwt
payload = jwt.decode(token, SHARED_SECRET, algorithms=['HS256'],
                     issuer='hczk-platform', leeway=30)
```
