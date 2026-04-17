# openai_api_to_claude

一个用 `FastAPI + Uvicorn + httpx` 写的轻量代理，把 Anthropic `/v1/messages` 风格请求转换成 OpenAI `chat/completions` 风格请求，再把响应映射回 Claude 兼容格式。

适合的用途：

- 让依赖 Claude API 形状的客户端接到 OpenAI 兼容后端
- 在本地模型网关前放一层 Anthropic 兼容适配器
- 给 Claude Code 类工具提供一个可对接的中间层

## 技术栈

- `fastapi`：提供 HTTP API
- `uvicorn`：ASGI 服务启动器
- `httpx`：异步转发上游请求

## 当前接口

- `GET /`
- `GET /health`
- `POST /v1/messages/count_tokens`
- `POST /v1/messages`

## 安装

```bash
python -m venv .venv
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

- `OPENAI_API`：上游 OpenAI 兼容接口地址
- `UPSTREAM_MODEL`：转发时默认使用的模型名
- `UPSTREAM_API_KEY`：如果上游要求 Bearer Token，就填这里
- `TIMEOUT`：上游请求超时秒数
- `MAX_CONCURRENT`：并发上限
- `DEFAULT_MAX_TOKENS`：默认最大输出 token
- `USE_UPSTREAM_STREAM`：是否直接使用上游流式输出，`1` 为开启
- `ENABLE_TOOL_SYSTEM_HINT`：是否给上游补一条 tool calling system hint

## 启动

开发启动：

```bash
uvicorn openaitoclaude:app --host 0.0.0.0 --port 4000 --workers 1
```

Windows 可直接运行：

```bat
start.bat
```

## 工作方式

1. 接收 Anthropic `/v1/messages` 请求
2. 把 `system`、`messages`、`tools`、`tool_choice` 映射成 OpenAI 格式
3. 转发到 `OPENAI_API`
4. 把上游返回的文本、tool calls、usage 和 stop reason 转回 Anthropic 兼容结构
5. 支持非流式和 SSE 流式响应

## 已包含的项目文件

- `openaitoclaude.py`：主程序
- `start.bat`：Windows 循环拉起脚本
- `requirements.txt`：运行依赖
- `.env.example`：环境变量样板
- `.gitignore`：基础忽略规则

## 注意事项

- 当前上游目标是 OpenAI `chat/completions` 兼容接口，不是 Responses API
- 如果你的上游流式实现不稳定，可以把 `USE_UPSTREAM_STREAM=0`
- 这个项目当前没有鉴权层；如需公网暴露，建议放到反向代理后并加认证
