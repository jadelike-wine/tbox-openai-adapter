# tbox-openai-adapter

> 一个多格式 API 适配器，将 TBox 智能体 / 工作流包装为 OpenAI 和 Anthropic 标准接口。

外部客户端可以用 OpenAI 或 Anthropic 的标准 API 格式调用本服务。适配器负责在两者之间进行协议转换：将请求翻译后转发给 TBox，再将 TBox 的响应转换回对应格式返回给客户端。

```
Client  ──(OpenAI API)────▶  tbox-openai-adapter  ──(TBox API)──▶  TBox
        ──(Anthropic API)──▶                        ◀──────────────
        ◀──────────────────
```

## 接入端点

| 格式 | 接入端点 | 说明 |
|---|---|---|
| OpenAI 格式 | `http://localhost:2233/openai` | 兼容 OpenAI API 规范 |
| Anthropic 格式 | `http://localhost:2233/anthropic` | 兼容 Anthropic Claude API 规范 |
| 旧版（向后兼容） | `http://localhost:2233` | 等同于 OpenAI 格式，保留以兼容老客户端 |

---

## 项目结构

```
tbox-openai-adapter/
├── app/
│   ├── main.py                    # FastAPI 应用工厂 + lifespan
│   ├── core/
│   │   └── config.py              # 从 .env 加载的配置
│   ├── routes/
│   │   ├── models.py              # GET  /v1/models
│   │   ├── chat.py                # POST /v1/chat/completions  (OpenAI 格式)
│   │   ├── anthropic.py           # POST /v1/messages          (Anthropic 格式)
│   │   ├── conversations.py       # 会话管理 CRUD 接口
│   │   └── files.py               # 文件上传 / 查询接口
│   ├── services/
│   │   ├── tbox_client.py         # 底层 TBox HTTP 客户端（httpx）
│   │   ├── chat_adapter.py        # OpenAI <-> TBox 翻译逻辑
│   │   ├── anthropic_adapter.py   # Anthropic <-> TBox 翻译逻辑
│   │   └── file_service.py        # 文件上传辅助（v1 骨架）
│   ├── stores/
│   │   └── session_store.py       # 内存会话存储（user -> conversationId）
│   ├── schemas/
│   │   ├── openai.py              # OpenAI 格式的 Pydantic 模型
│   │   ├── anthropic.py           # Anthropic 格式的 Pydantic 模型
│   │   └── tbox.py                # TBox 格式的 Pydantic 模型
│   └── utils/
│       ├── sse.py                 # SSE 编解码 / 行解析工具
│       └── errors.py              # 自定义异常 + 错误响应构建器
├── tests/
│   ├── conftest.py
│   ├── test_models.py
│   └── test_chat.py
├── doc/                           # TBox API 参考文档
├── .env.example
├── pytest.ini
└── requirements.txt
```

---

## 快速开始

### 1. 克隆仓库并安装依赖

```bash
git clone <repo-url>
cd tbox-openai-adapter

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env — 至少需要设置 TBOX_APP_ID 和 TBOX_TOKEN
```

配置项说明：

| 变量名 | 说明 | 默认值 |
|---|---|---|
| `TBOX_APP_ID` | TBox 应用 ID | **必填** |
| `TBOX_TOKEN` | TBox Bearer 认证 Token | **必填** |
| `TBOX_BASE_URL` | TBox API 基础 URL | `https://api.tbox.cn` |
| `TBOX_TIMEOUT` | 请求超时时间（秒） | `60` |
| `ADAPTER_MODEL_ID` | 对外暴露给客户端的模型 ID | `tbox-codex` |
| `ADAPTER_DEFAULT_USER` | 请求未携带 user 字段时的默认用户名 | `default-user` |
| `HOST` | 服务绑定地址 | `0.0.0.0` |
| `PORT` | 服务监听端口 | `2233` |
| `DEBUG` | 启用 Debug 日志和热重载 | `false` |

### 3. 启动服务

```bash
# 生产模式
uvicorn app.main:app --host 0.0.0.0 --port 2233

# 开发模式（支持热重载）
DEBUG=true python -m app.main
```

服务启动后可访问在线 API 文档：
- Swagger UI：`http://localhost:2233/docs`
- ReDoc：`http://localhost:2233/redoc`

---

## API 参考

### OpenAI 格式接口（`/openai` 前缀）

---

### `GET /openai/v1/models`

返回可用模型列表。

```bash
curl http://localhost:2233/openai/v1/models
```

响应示例：

```json
{
  "object": "list",
  "data": [
    {
      "id": "tbox-codex",
      "object": "model",
      "created": 1714000000,
      "owned_by": "tbox"
    }
  ]
}
```

---

### `POST /openai/v1/chat/completions` — 非流式

```bash
curl http://localhost:2233/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tbox-codex",
    "messages": [
      {"role": "user", "content": "2+2 等于多少？"}
    ],
    "stream": false,
    "user": "alice"
  }'
```

响应示例：

```json
{
  "id": "chatcmpl-abc123",
  "object": "chat.completion",
  "created": 1714000000,
  "model": "tbox-codex",
  "choices": [
    {
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "2+2 等于 4。"
      },
      "finish_reason": "stop"
    }
  ],
  "usage": {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0
  }
}
```

> **注意：** v1 版本中 Token 用量始终为 0 — TBox 在此流程中不返回每次请求的 Token 消耗数据。

---

### `POST /openai/v1/chat/completions` — 流式（SSE）

```bash
curl http://localhost:2233/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "tbox-codex",
    "messages": [
      {"role": "user", "content": "给我讲个短故事。"}
    ],
    "stream": true,
    "user": "alice"
  }'
```

SSE 输出示例：

```
data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{"content":"从前有一座山..."},"finish_reason":null}]}

data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

### `POST /openai/v1/conversations`

为当前配置的 `appId` 创建一个新的 TBox 会话。

```bash
curl -X POST http://localhost:2233/openai/v1/conversations
```

响应示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": "conv_abc123"
}
```

---

### `GET /openai/v1/conversations`

查询通过 TBox OpenAPI 或 SDK 创建的会话列表。

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `userId` | string（可选） | 按用户 ID 筛选 |
| `source` | string（可选） | 按渠道筛选：`AGENT_SDK` \| `OPENAPI` \| `IOT_SDK` |
| `pageNum` | int（默认 `1`） | 页码，从 1 开始 |
| `pageSize` | int（默认 `10`，最大 `50`） | 每页条数 |
| `sortOrder` | string（默认 `DESC`） | 按创建时间排序：`ASC` 或 `DESC` |

```bash
curl "http://localhost:2233/openai/v1/conversations?userId=alice&pageSize=5"
```

---

### `GET /openai/v1/conversations/{conversation_id}/messages`

查询指定会话中的消息列表（问答轮次）。

路径参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `conversation_id` | string | 会话 ID |

查询参数：

| 参数 | 类型 | 说明 |
|---|---|---|
| `pageNum` | int（默认 `1`） | 页码，从 1 开始 |
| `pageSize` | int（默认 `10`，最大 `50`） | 每页条数 |
| `sortOrder` | string（默认 `DESC`） | 按创建时间排序：`ASC` 或 `DESC` |

```bash
curl "http://localhost:2233/openai/v1/conversations/conv_abc123/messages?pageSize=20"
```

---

### `POST /openai/v1/files`

上传文件到 TBox，用于多模态对话或知识库。

- 支持类型：文本、图片、音频、视频、文档等
- 文件有效期：3 个月，过期后 TBox 自动删除
- 返回的 `fileId` 可在聊天请求的 `files` 字段中使用

```bash
curl http://localhost:2233/openai/v1/files \
  -F "file=@/path/to/document.pdf"
```

响应示例：

```json
{
  "code": 0,
  "msg": "success",
  "data": {
    "fileId": "file_xyz789"
  }
}
```

---

### `GET /openai/v1/files/{file_id}`

查询已上传文件的详细信息。

```bash
curl http://localhost:2233/openai/v1/files/file_xyz789
```

返回文件名、类型、大小（字节）和创建时间。

---

### Anthropic 格式接口（`/anthropic` 前缀）

---

### `POST /anthropic/v1/messages` — 非流式

```bash
curl http://localhost:2233/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tbox-codex",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "2+2 等于多少？"}
    ]
  }'
```

响应示例：

```json
{
  "id": "msg_abc123",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "2+2 等于 4。"
    }
  ],
  "model": "tbox-codex",
  "stop_reason": "end_turn",
  "stop_sequence": null,
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0
  }
}
```

---

### `POST /anthropic/v1/messages` — 流式（SSE）

```bash
curl http://localhost:2233/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "tbox-codex",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "给我讲个短故事。"}
    ],
    "stream": true
  }'
```

SSE 输出遵循 Anthropic 流式事件协议：

```
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"从前有一座山..."}}

event: content_block_stop
data: {"type":"content_block_stop","index":0}

event: message_delta
data: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"input_tokens":0,"output_tokens":0}}

event: message_stop
data: {"type":"message_stop"}
```

---

### `GET /health`

```bash
curl http://localhost:2233/health
# {"status": "ok"}
```

---

## 会话管理

适配器将每个 `user` 值映射到一个 TBox `conversationId`：

- 某个 `user` 的首条消息 → 创建新的 TBox 对话
- 后续消息 → 复用同一 `conversationId`，实现多轮连续对话
- 如需重置会话，可清空存储或重启服务

此映射关系存储在进程内存中（v1），服务重启后会丢失。  
持久化方案请参见[未来计划](#未来计划)。

---

## v1 中接受但忽略的字段

以下请求字段会被正常解析（避免客户端报错），但不会转发给 TBox：

**OpenAI 格式：**

| 字段 | 原因 |
|---|---|
| `temperature` | TBox 内部管理采样参数 |
| `top_p` | 同上 |
| `max_tokens` | TBox 控制响应长度 |
| `n` | 始终只返回 1 个 choice |
| `stop` | v1 中 TBox 不支持 |
| `presence_penalty` | 不适用 |
| `frequency_penalty` | 不适用 |
| `logit_bias` | 不适用 |
| `tools` / `tool_choice` | v1 不支持 Function Calling |
| `response_format` | v1 不支持 JSON 模式 |

**Anthropic 格式：**

| 字段 | 原因 |
|---|---|
| `temperature` | TBox 内部管理采样参数 |
| `top_p` | 同上 |
| `top_k` | 同上 |
| `stop_sequences` | v1 中 TBox 不支持 |
| `tools` / `tool_choice` | v1 不支持工具调用 |

---

## 运行测试

```bash
pytest -v
```

测试完全离线运行 — TBox 的 HTTP 调用均已被 mock。

---

## 未来计划

v2+ 版本规划中的改进：

1. **持久化会话存储** — 将 `session_store.py` 替换为 Redis 或 SQLite 后端，无需修改调用方。
2. **多租户认证** — 在转发请求到 TBox 之前，对每个租户验证 `Authorization: Bearer <token>` 请求头。
3. **文件附件** — 将 `file_service.py` 接入聊天流程，支持 `multipart/form-data` 文件上传。
4. **Token 用量透传** — 将 TBox `meta` 事件中的 Token 数据填充到响应的 `usage` 字段。
5. **Function Calling / 工具调用** — 将 OpenAI tool specs 转发至 TBox 技能调用 API。
6. **思维链（CoT）透明化** — 可选地将 `thinking` 事件作为独立的 delta 类型进行流式输出。
7. **限流** — 添加限流中间件（例如 `slowapi`）。
8. **生产级日志** — 输出结构化 JSON 日志并对接平台日志系统（例如 Datadog、Loki）。
9. **多模型 ID 映射** — 将不同的 `model` 值映射到不同的 TBox `appId` 配置。
10. **OpenAI 兼容错误码** — 针对已知的 TBox 错误状态，返回精确的 OpenAI 错误 `code` 值。
