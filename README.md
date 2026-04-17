# openai_api_to_claude

一个 Anthropic-to-OpenAI 协议适配代理。

它不是简单把几个 Python 包拼在一起，而是实现了一套可运行的兼容层方案：让原本要求 Anthropic `/v1/messages` 接口形状的客户端，可以接到只提供 OpenAI `chat/completions` 风格接口的后端上，并尽量保留工具调用、流式输出、错误结构和 token 统计语义。

换句话说，这个项目解决的是“协议不兼容”问题，而不是“怎么起一个 FastAPI 服务”问题。

## 这个项目解决了什么问题

很多客户端、代理或开发工具默认假设上游是 Claude / Anthropic 风格接口，例如：

- 请求入口是 `POST /v1/messages`
- 输入采用 `system + messages + tools + tool_choice`
- 工具调用使用 `tool_use` / `tool_result`
- 流式输出遵循 Anthropic 的 SSE 事件格式
- 错误返回结构和 stop reason 也遵循 Anthropic 习惯

但大量本地模型网关、私有推理服务或第三方兼容层，只提供 OpenAI `chat/completions` 形状接口。

这时会出现一个真实的集成断层：

- 客户端会说 Anthropic
- 上游后端只会说 OpenAI
- 两边都“差不多”，但实际字段、事件流和工具语义并不兼容

`openai_api_to_claude` 的作用，就是在这两者之间补上这层翻译器。

## 方案核心

本项目实现的是一层运行中的协议桥接：

1. 接收 Anthropic 风格请求
2. 将其转换为 OpenAI `chat/completions` 请求
3. 转发到指定上游模型接口
4. 再把上游返回结果转换回 Anthropic 兼容结构
5. 对流式场景额外做 SSE 事件重组

它的价值不在于依赖本身，而在于这些转换规则和边界处理：

- `system` 字段转成 OpenAI `system` message
- `messages` 内容块转成 OpenAI message 数组
- `tool_use` / `tool_result` 与 OpenAI `tool_calls` / `tool` message 互转
- 上游错误码映射成 Anthropic 风格 error type
- 上游非流式结果可回放成 Anthropic 流式事件
- 提供 `count_tokens` 兼容入口，维持客户端工作流完整性

## 为什么这不是“套壳”

这个项目虽然基于 `FastAPI`、`Uvicorn` 和 `httpx`，但真正创造的新东西是：

- 一套 Anthropic <-> OpenAI 的消息结构映射逻辑
- 一套工具调用语义对齐逻辑
- 一套流式输出事件重组逻辑
- 一套上游错误到 Anthropic 错误模型的映射逻辑
- 一套适合 Claude 类客户端接入 OpenAI 兼容后端的工程化落地方式

依赖库负责的是 HTTP 服务、ASGI 启动和异步请求。

这个项目本身负责的是协议翻译、语义保真和兼容性修补。

## 适合的使用场景

- 让依赖 Claude API 形状的客户端接到 OpenAI 兼容后端
- 在本地模型网关前放一层 Anthropic 兼容适配器
- 给 Claude Code 类工具提供一个可对接的中间层
- 把自建模型服务包装成更接近 Claude 客户端预期的接口
- 在不改客户端代码的前提下，替换后端模型提供方

## 当前能力

- 提供 `GET /` 和 `GET /health` 健康检查入口
- 提供 `POST /v1/messages` Anthropic 兼容主入口
- 提供 `POST /v1/messages/count_tokens` 兼容入口
- 支持 `system` 字段转换
- 支持多轮 `messages` 转换
- 支持 `tools` 和 `tool_choice` 映射
- 支持 `tool_use` / `tool_result` 转换
- 支持流式和非流式响应
- 支持上游非流式结果伪流式回放
- 支持基础超时控制和并发限制
- 支持上游 Bearer Token

## 架构概览

```mermaid
flowchart LR
    A[Anthropic client] --> B[/v1/messages]
    B --> C[Request translator]
    C --> D[OpenAI chat.completions payload]
    D --> E[Upstream model gateway]
    E --> F[Response translator]
    F --> G[Anthropic compatible response]
```

## 技术栈

- `fastapi`：提供 HTTP API
- `uvicorn`：ASGI 服务启动器
- `httpx`：异步转发上游请求

这里的技术栈只是承载层，不是项目的主要创新点。

## 接口

- `GET /`
- `GET /health`
- `POST /v1/messages/count_tokens`
- `POST /v1/messages`

## 安装

Linux / macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Windows:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 配置

复制 `.env.example` 为 `.env`，或直接设置环境变量。

关键变量：

- `OPENAI_API`
  上游 OpenAI 兼容接口地址，默认 `http://127.0.0.1:8000/v1/chat/completions`

- `UPSTREAM_MODEL`
  转发时使用的默认模型名

- `UPSTREAM_API_KEY`
  如果上游要求 Bearer Token，就填这里

- `TIMEOUT`
  上游请求超时秒数

- `MAX_CONCURRENT`
  并发上限，用于限制同时转发的请求数

- `DEFAULT_MAX_TOKENS`
  当请求未明确给出时使用的默认输出上限

- `USE_UPSTREAM_STREAM`
  是否直接使用上游流式输出
  `1` 表示开启，`0` 表示关闭

- `ENABLE_TOOL_SYSTEM_HINT`
  是否在存在 tools 时，给上游补一条 tool-calling system hint

## 启动

直接启动：

```bash
uvicorn openaitoclaude:app --host 0.0.0.0 --port 4000 --workers 1
```

Windows 下可直接运行：

```bat
start.bat
```

Linux / macOS 下可直接运行：

```bash
chmod +x start.sh
./start.sh
```

## 工作流程

1. 客户端向 `/v1/messages` 发送 Anthropic 风格请求
2. 代理校验 `messages`、`model`、`stream` 等基本字段
3. 把 `system`、`messages`、`tools`、`tool_choice` 转成 OpenAI payload
4. 把请求转发到 `OPENAI_API`
5. 如果上游返回流式数据，代理重组成 Anthropic SSE 事件
6. 如果上游只支持非流式，代理也可以回放成 Anthropic 风格流式响应
7. 把最终结果返回给 Anthropic 风格客户端

## 最小请求示例

健康检查：

```bash
curl http://127.0.0.1:4000/health
```

最小消息请求：

```bash
curl -sS http://127.0.0.1:4000/v1/messages \
  -H 'content-type: application/json' \
  -d '{
    "model": "claude-compatible",
    "max_tokens": 256,
    "messages": [
      {
        "role": "user",
        "content": "Say hello in one sentence."
      }
    ]
  }'
```

带工具定义的请求示例：

```bash
curl -sS http://127.0.0.1:4000/v1/messages \
  -H 'content-type: application/json' \
  -d '{
    "model": "claude-compatible",
    "max_tokens": 256,
    "tools": [
      {
        "name": "get_weather",
        "description": "Get current weather",
        "input_schema": {
          "type": "object",
          "properties": {
            "city": { "type": "string" }
          },
          "required": ["city"]
        }
      }
    ],
    "messages": [
      {
        "role": "user",
        "content": "What is the weather in Taipei?"
      }
    ]
  }'
```

## 一个最小价值示例

原本：

- 客户端只能对 Anthropic `/v1/messages` 说话
- 你的模型网关只开放 OpenAI `chat/completions`
- 双方无法直接互通

接入这个代理后：

- 客户端继续按 Anthropic 协议发请求
- 代理负责转换成 OpenAI 兼容格式
- 上游模型照常处理
- 结果再被翻回 Anthropic 兼容结构

这让“Claude 风格客户端接 OpenAI 风格后端”成为可运行方案。

## 已包含的项目文件

- `openaitoclaude.py`：主程序，包含请求转换、响应转换、流式处理和错误映射
- `start.bat`：Windows 循环拉起脚本
- `start.sh`：Linux / macOS 循环拉起脚本
- `requirements.txt`：运行依赖
- `Dockerfile`：最小容器化部署文件
- `.env.example`：环境变量样板
- `.gitignore`：基础忽略规则

## Docker 启动

构建镜像：

```bash
docker build -t openai-api-to-claude .
```

运行容器：

```bash
docker run --rm -p 4000:4000 \
  -e OPENAI_API=http://host.docker.internal:8000/v1/chat/completions \
  -e UPSTREAM_MODEL=gemma \
  openai-api-to-claude
```

如果宿主机不是 Docker Desktop 环境，需要把 `host.docker.internal` 换成你上游网关实际可达的地址。

## 当前边界

- 当前上游目标是 OpenAI `chat/completions` 兼容接口，不是 Responses API
- 某些更复杂的多模态块类型目前会被忽略，而不是完整映射
- 这是协议适配层，不是完整的鉴权网关、审计网关或配额系统
- 如果要公网暴露，建议放到反向代理后并补鉴权

## 后续可扩展方向

- 支持 OpenAI Responses API 作为上游
- 补充更完整的 image / document / thinking 块映射
- 增加 API key 校验和租户隔离
- 增加请求日志脱敏和审计功能
- 增加 Docker 化部署与 systemd 服务模板
- 增加更严格的兼容性测试样例

## 总结

`openai_api_to_claude` 借助成熟 Python 组件承载服务，但它本身的核心价值是新方案：把原本不兼容的 Anthropic 客户端协议和 OpenAI 兼容后端协议接起来，并且尽量保持工具调用、流式输出和错误语义的一致性。

这不是简单封装库，而是一个明确解决协议兼容问题的适配层实现。

## License

本仓库使用 `MIT` 许可证，详见 [LICENSE](LICENSE)。
