# tbox-openai-adapter

> A multi-format API adapter that wraps TBox intelligent agents / workflows as OpenAI and Anthropic standard interfaces.

External clients call this service using either the OpenAI or Anthropic API
format. The adapter translates the requests, forwards them to TBox, and converts
the responses back to the corresponding wire format.

```
Client  ──(OpenAI API)────▶  tbox-openai-adapter  ──(TBox API)──▶  TBox
        ──(Anthropic API)──▶                        ◀──────────────
        ◀──────────────────
```

## Endpoints

| Format | Base URL | Description |
|---|---|---|
| OpenAI format | `http://localhost:2233/openai` | Compatible with OpenAI API spec |
| Anthropic format | `http://localhost:2233/anthropic` | Compatible with Anthropic Claude API spec |
| Legacy (backward compat) | `http://localhost:2233` | Same as OpenAI format, kept for old clients |

---

## Project layout

```
tbox-openai-adapter/
├── app/
│   ├── main.py                    # FastAPI app factory + lifespan
│   ├── core/
│   │   └── config.py              # Settings loaded from .env
│   ├── routes/
│   │   ├── models.py              # GET  /v1/models
│   │   ├── chat.py                # POST /v1/chat/completions  (OpenAI format)
│   │   ├── anthropic.py           # POST /v1/messages          (Anthropic format)
│   │   ├── conversations.py       # Conversation management CRUD
│   │   └── files.py               # File upload / retrieval
│   ├── services/
│   │   ├── tbox_client.py         # Low-level TBox HTTP client (httpx)
│   │   ├── chat_adapter.py        # OpenAI <-> TBox translation logic
│   │   ├── anthropic_adapter.py   # Anthropic <-> TBox translation logic
│   │   └── file_service.py        # File upload helper (v1 skeleton)
│   ├── stores/
│   │   └── session_store.py       # In-memory user -> conversationId map
│   ├── schemas/
│   │   ├── openai.py              # Pydantic models for OpenAI shapes
│   │   ├── anthropic.py           # Pydantic models for Anthropic shapes
│   │   └── tbox.py                # Pydantic models for TBox shapes
│   └── utils/
│       ├── sse.py                 # SSE encoding / line-parsing helpers
│       └── errors.py              # Custom exceptions + error response builder
├── tests/
│   ├── conftest.py
│   ├── test_models.py
│   └── test_chat.py
├── doc/                           # TBox API reference docs (Chinese)
├── .env.example
├── pytest.ini
└── requirements.txt
```

---

## Quick start

### 1. Clone & install dependencies

```bash
git clone <repo-url>
cd tbox-openai-adapter

python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set TBOX_APP_ID and TBOX_TOKEN
```

Required variables:

| Variable | Description |
|---|---|
| `TBOX_APP_ID` | Your TBox application ID |
| `TBOX_TOKEN` | Your TBox Bearer token |
| `TBOX_BASE_URL` | TBox API base URL (default: `https://api.tbox.cn`) |
| `TBOX_TIMEOUT` | Request timeout in seconds (default: `60`) |
| `ADAPTER_MODEL_ID` | Model ID advertised to clients (default: `tbox-codex`) |
| `ADAPTER_DEFAULT_USER` | Default user name when `user` field is omitted (default: `default-user`) |
| `HOST` | Bind address (default: `0.0.0.0`) |
| `PORT` | Listen port (default: `2233`) |
| `DEBUG` | Enable debug logging & hot reload (default: `false`) |

### 3. Run the server

```bash
# Production-like
uvicorn app.main:app --host 0.0.0.0 --port 2233

# Development with hot reload
DEBUG=true python -m app.main
```

The API docs are available at:
- Swagger UI: `http://localhost:2233/docs`
- ReDoc: `http://localhost:2233/redoc`

---

## API reference

### OpenAI format endpoints (`/openai` prefix)

### `GET /openai/v1/models`

Returns the list of available models.

```bash
curl http://localhost:2233/openai/v1/models
```

Example response:

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

### `POST /openai/v1/chat/completions` — Non-streaming

```bash
curl http://localhost:2233/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tbox-codex",
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ],
    "stream": false,
    "user": "alice"
  }'
```

Example response:

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
        "content": "2+2 equals 4."
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

> **Note:** Token counts are always 0 in v1 — TBox does not expose per-request
> token usage in this flow.

---

### `POST /openai/v1/chat/completions` — Streaming (SSE)

```bash
curl http://localhost:2233/openai/v1/chat/completions \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "tbox-codex",
    "messages": [
      {"role": "user", "content": "Tell me a short story."}
    ],
    "stream": true,
    "user": "alice"
  }'
```

Example SSE output:

```
data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{"role":"assistant","content":""},"finish_reason":null}]}

data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{"content":"Once upon a time..."},"finish_reason":null}]}

data: {"id":"chatcmpl-xyz","object":"chat.completion.chunk","created":1714000000,"model":"tbox-codex","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}

data: [DONE]
```

---

### `POST /openai/v1/conversations`

Create a new TBox conversation for the configured `appId`.

```bash
curl -X POST http://localhost:2233/openai/v1/conversations
```

Example response:

```json
{
  "code": 0,
  "msg": "success",
  "data": "conv_abc123"
}
```

---

### `GET /openai/v1/conversations`

List conversations created via the TBox OpenAPI or SDK.

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `userId` | string (optional) | Filter by user ID |
| `source` | string (optional) | Filter by channel: `AGENT_SDK` \| `OPENAPI` \| `IOT_SDK` |
| `pageNum` | int (default: `1`) | Page number, starts from 1 |
| `pageSize` | int (default: `10`, max: `50`) | Items per page |
| `sortOrder` | string (default: `DESC`) | Sort by creation time: `ASC` or `DESC` |

```bash
curl "http://localhost:2233/openai/v1/conversations?userId=alice&pageSize=5"
```

---

### `GET /openai/v1/conversations/{conversation_id}/messages`

List messages (Q&A rounds) in a specific conversation.

Path parameters:

| Parameter | Type | Description |
|---|---|---|
| `conversation_id` | string | The conversation ID |

Query parameters:

| Parameter | Type | Description |
|---|---|---|
| `pageNum` | int (default: `1`) | Page number, starts from 1 |
| `pageSize` | int (default: `10`, max: `50`) | Items per page |
| `sortOrder` | string (default: `DESC`) | Sort by creation time: `ASC` or `DESC` |

```bash
curl "http://localhost:2233/openai/v1/conversations/conv_abc123/messages?pageSize=20"
```

---

### `POST /openai/v1/files`

Upload a file to TBox for use in multimodal conversations or knowledge bases.

- Supported types: text, image, audio, video, document, etc.
- File validity: 3 months; expired files are automatically deleted by TBox.
- Returns a `fileId` that can be passed in the `files` field of chat requests.

```bash
curl http://localhost:2233/openai/v1/files \
  -F "file=@/path/to/document.pdf"
```

Example response:

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

Retrieve details for a previously uploaded file.

```bash
curl http://localhost:2233/openai/v1/files/file_xyz789
```

Returns file name, type, size (bytes), and creation time.

---

### Anthropic format endpoints (`/anthropic` prefix)

---

### `POST /anthropic/v1/messages` — Non-streaming

```bash
curl http://localhost:2233/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "tbox-codex",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "What is 2+2?"}
    ]
  }'
```

Example response:

```json
{
  "id": "msg_abc123",
  "type": "message",
  "role": "assistant",
  "content": [
    {
      "type": "text",
      "text": "2+2 equals 4."
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

### `POST /anthropic/v1/messages` — Streaming (SSE)

```bash
curl http://localhost:2233/anthropic/v1/messages \
  -H "Content-Type: application/json" \
  -N \
  -d '{
    "model": "tbox-codex",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Tell me a short story."}
    ],
    "stream": true
  }'
```

SSE output follows the Anthropic streaming event protocol:

```
event: message_start
data: {"type":"message_start","message":{...}}

event: content_block_start
data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}

event: content_block_delta
data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Once upon a time..."}}

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

## Session management

The adapter maps each `user` value to a TBox `conversationId`:

- First message from a `user` → new TBox conversation
- Subsequent messages → same `conversationId` (multi-turn)
- To reset the session, clear it via the store or restart the service

This mapping lives in process memory (v1).  It is lost on restart.
See [Future work](#future-work) for persistence options.

---

## Fields accepted but ignored in v1

The following request fields are parsed (to avoid client errors) but not
forwarded to TBox:

**OpenAI format:**

| Field | Reason |
|---|---|
| `temperature` | TBox manages sampling internally |
| `top_p` | Same as above |
| `max_tokens` | TBox controls response length |
| `n` | Always 1 choice |
| `stop` | Not supported by TBox in v1 |
| `presence_penalty` | Not applicable |
| `frequency_penalty` | Not applicable |
| `logit_bias` | Not applicable |
| `tools` / `tool_choice` | Function calling not in v1 |
| `response_format` | JSON mode not in v1 |

**Anthropic format:**

| Field | Reason |
|---|---|
| `temperature` | TBox manages sampling internally |
| `top_p` | Same as above |
| `top_k` | Same as above |
| `stop_sequences` | Not supported by TBox in v1 |
| `tools` / `tool_choice` | Tool use not in v1 |

---

## Running tests

```bash
pytest -v
```

Tests run entirely offline — TBox HTTP calls are mocked.

---

## Future work

Planned improvements for v2+:

1. **Persistent session store** — swap `session_store.py` for a Redis or
   SQLite backend without changing any callers.
2. **Multi-tenant auth** — verify an `Authorization: Bearer <token>` header
   per tenant before forwarding to TBox.
3. **File attachments** — wire `file_service.py` into the chat flow; accept
   `multipart/form-data` uploads.
4. **Token count passthrough** — surface TBox `meta` event token data in the
   `usage` field.
5. **Function calling / tools** — forward OpenAI tool specs to TBox skill
   invocation API.
6. **Thinking / CoT transparency** — optionally stream `thinking` events as
   a separate delta type.
7. **Rate limiting** — add a middleware layer (e.g. `slowapi`).
8. **Production logging** — structured JSON logs with a platform sink
   (e.g. Datadog, Loki).
9. **Multiple model IDs** — map different `model` values to different TBox
   `appId` configurations.
10. **OpenAI-compatible error codes** — return precise error `code` values
    for well-known TBox error states.
