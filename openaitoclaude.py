import asyncio
import json
import logging
import math
import os
import sys
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse


# =========================
# Windows 兼容
# =========================
if sys.platform == "win32" and hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


# =========================
# 配置
# =========================
OPENAI_API = os.getenv("OPENAI_API", "http://127.0.0.1:8000/v1/chat/completions")
UPSTREAM_MODEL = os.getenv("UPSTREAM_MODEL", "gemma")
TIMEOUT = float(os.getenv("TIMEOUT", "120"))
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT", "1"))
DEFAULT_MAX_TOKENS = int(os.getenv("DEFAULT_MAX_TOKENS", "1024"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
UPSTREAM_API_KEY = os.getenv("UPSTREAM_API_KEY", "").strip()

# 若你的上游流式实现很差，可改成 0，然后对 Claude 流式请求做“伪流式回放”
USE_UPSTREAM_STREAM = os.getenv("USE_UPSTREAM_STREAM", "1") == "1"

ENABLE_TOOL_SYSTEM_HINT = os.getenv("ENABLE_TOOL_SYSTEM_HINT", "1") == "1"
TOOL_SYSTEM_HINT = (
    "You may call tools when needed. "
    "When calling a tool, use the structured tool calling interface only. "
    "Do not wrap tool calls in markdown or XML."
)

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)

semaphore = asyncio.Semaphore(MAX_CONCURRENT)


# =========================
# 生命周期
# =========================
@asynccontextmanager
async def lifespan(app: FastAPI):
    limits = httpx.Limits(
        max_connections=MAX_CONCURRENT,
        max_keepalive_connections=MAX_CONCURRENT,
    )
    app.state.http = httpx.AsyncClient(timeout=TIMEOUT, limits=limits)
    yield
    await app.state.http.aclose()


app = FastAPI(lifespan=lifespan)


# =========================
# 基础工具
# =========================
def anthropic_error(status_code: int, error_type: str, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={
            "type": "error",
            "error": {
                "type": error_type,
                "message": message,
            },
        },
    )


def map_upstream_error_type(status_code: int) -> str:
    if status_code == 400:
        return "invalid_request_error"
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 404:
        return "not_found_error"
    if status_code == 429:
        return "rate_limit_error"
    return "api_error"


def safe_json_dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def estimate_tokens_from_text(text: str) -> int:
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def sse_pack(event: str, data: Dict[str, Any]) -> str:
    return f"event: {event}\ndata: {safe_json_dumps(data)}\n\n"


def upstream_headers() -> Dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if UPSTREAM_API_KEY:
        headers["Authorization"] = f"Bearer {UPSTREAM_API_KEY}"
    return headers


def text_from_content_blocks(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        if content.get("type") == "text":
            return str(content.get("text", ""))
        if isinstance(content.get("text"), str):
            return content["text"]
        return ""

    if isinstance(content, list):
        parts: List[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "\n".join([p for p in parts if p])

    return ""


def stringify_tool_result_content(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, dict):
        return safe_json_dumps(content)

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                parts.append(safe_json_dumps(item))
        return "\n".join([p for p in parts if p])

    return str(content)


def normalize_tool_input(raw_args: Any) -> Dict[str, Any]:
    if raw_args is None:
        return {}

    if isinstance(raw_args, dict):
        return raw_args

    if isinstance(raw_args, str):
        s = raw_args.strip()
        if not s:
            return {}
        try:
            parsed = json.loads(s)
            if isinstance(parsed, dict):
                return parsed
            return {"value": parsed}
        except Exception:
            return {"raw": raw_args}

    return {"value": raw_args}


def flush_text_message(target: List[Dict[str, Any]], role: str, text_parts: List[str]) -> None:
    text = "\n".join([p for p in text_parts if isinstance(p, str) and p != ""])
    if text:
        target.append({"role": role, "content": text})
    text_parts.clear()


# =========================
# Anthropic -> OpenAI
# =========================
def anthropic_system_to_openai_messages(system_field: Any) -> List[Dict[str, Any]]:
    if system_field is None:
        return []

    system_text = text_from_content_blocks(system_field)
    if not system_text.strip():
        return []

    return [{"role": "system", "content": system_text}]


def anthropic_tools_to_openai_tools(tools: Any) -> List[Dict[str, Any]]:
    if not isinstance(tools, list):
        return []

    out: List[Dict[str, Any]] = []

    for tool in tools:
        if not isinstance(tool, dict):
            continue

        name = tool.get("name")
        if not name:
            continue

        fn: Dict[str, Any] = {
            "name": name,
            "parameters": tool.get("input_schema") or {
                "type": "object",
                "properties": {},
            },
        }

        description = tool.get("description")
        if description:
            fn["description"] = description

        if "strict" in tool:
            fn["strict"] = bool(tool["strict"])

        out.append({
            "type": "function",
            "function": fn,
        })

    return out


def anthropic_tool_choice_to_openai(tool_choice: Any) -> Optional[Any]:
    if tool_choice is None:
        return None

    if isinstance(tool_choice, str):
        if tool_choice in {"auto", "none", "required"}:
            return tool_choice
        return None

    if not isinstance(tool_choice, dict):
        return None

    choice_type = tool_choice.get("type")

    if choice_type == "auto":
        return "auto"
    if choice_type == "none":
        return "none"
    if choice_type in {"any", "required"}:
        return "required"
    if choice_type == "tool":
        name = tool_choice.get("name")
        if name:
            return {
                "type": "function",
                "function": {"name": name},
            }

    return None


def anthropic_messages_to_openai_messages(messages: Any) -> List[Dict[str, Any]]:
    if not isinstance(messages, list):
        return []

    out: List[Dict[str, Any]] = []

    for msg in messages:
        if not isinstance(msg, dict):
            continue

        role = msg.get("role")
        content = msg.get("content", "")

        if role not in {"user", "assistant", "system"}:
            continue

        if isinstance(content, str):
            if content != "":
                out.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        if role == "user":
            text_parts: List[str] = []

            for block in content:
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")

                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))

                elif block_type == "tool_result":
                    flush_text_message(out, "user", text_parts)

                    tool_call_id = block.get("tool_use_id")
                    if not tool_call_id:
                        text_parts.append(stringify_tool_result_content(block.get("content")))
                        continue

                    tool_content = stringify_tool_result_content(block.get("content"))
                    if block.get("is_error") is True:
                        tool_content = f"[tool_result_error]\n{tool_content}"

                    out.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": tool_content,
                    })

                else:
                    # 忽略 image/document/thinking 等暂不支持块，避免误伤 Claude Code 基本流程
                    continue

            flush_text_message(out, "user", text_parts)
            continue

        if role == "assistant":
            text_parts: List[str] = []
            tool_calls: List[Dict[str, Any]] = []

            for block in content:
                if not isinstance(block, dict):
                    continue

                block_type = block.get("type")

                if block_type == "text":
                    text_parts.append(str(block.get("text", "")))

                elif block_type == "tool_use":
                    tool_id = block.get("id") or f"call_{uuid.uuid4().hex[:24]}"
                    tool_name = block.get("name") or "tool"
                    tool_input = block.get("input", {})
                    if not isinstance(tool_input, dict):
                        tool_input = {"value": tool_input}

                    tool_calls.append({
                        "id": tool_id,
                        "type": "function",
                        "function": {
                            "name": tool_name,
                            "arguments": safe_json_dumps(tool_input),
                        },
                    })

                else:
                    continue

            if text_parts or tool_calls:
                msg_out: Dict[str, Any] = {
                    "role": "assistant",
                    "content": "\n".join([p for p in text_parts if p]),
                }
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                out.append(msg_out)

            continue

        if role == "system":
            system_text = text_from_content_blocks(content)
            if system_text:
                out.append({"role": "system", "content": system_text})

    return out


def build_openai_payload_from_anthropic(body: Dict[str, Any], stream: bool) -> Dict[str, Any]:
    openai_messages: List[Dict[str, Any]] = []

    openai_messages.extend(anthropic_system_to_openai_messages(body.get("system")))

    if ENABLE_TOOL_SYSTEM_HINT and isinstance(body.get("tools"), list) and body.get("tools"):
        openai_messages.append({
            "role": "system",
            "content": TOOL_SYSTEM_HINT,
        })

    openai_messages.extend(anthropic_messages_to_openai_messages(body.get("messages", [])))

    payload: Dict[str, Any] = {
        "model": UPSTREAM_MODEL,
        "messages": openai_messages,
        "max_tokens": int(body.get("max_tokens", DEFAULT_MAX_TOKENS)),
        "stream": stream,
    }

    for key in ("temperature", "top_p", "frequency_penalty", "presence_penalty", "seed"):
        if key in body:
            payload[key] = body[key]

    stop_sequences = body.get("stop_sequences")
    if stop_sequences:
        payload["stop"] = stop_sequences

    openai_tools = anthropic_tools_to_openai_tools(body.get("tools"))
    if openai_tools:
        payload["tools"] = openai_tools
        tool_choice = anthropic_tool_choice_to_openai(body.get("tool_choice"))
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice

    return payload


# =========================
# OpenAI -> Anthropic 非流式
# =========================
def extract_openai_text_content(content: Any) -> str:
    if content is None:
        return ""

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                if item.get("type") in {"text", "output_text"} and isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif isinstance(item.get("text"), str):
                    parts.append(item["text"])
        return "".join(parts)

    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]

    return str(content)


def openai_tool_call_to_anthropic_block(tool_call: Dict[str, Any], index: int) -> Dict[str, Any]:
    function_obj = tool_call.get("function") or {}
    tool_id = tool_call.get("id") or f"toolu_{uuid.uuid4().hex[:24]}"
    name = function_obj.get("name") or tool_call.get("name") or f"tool_{index}"

    raw_args = function_obj.get("arguments")
    if raw_args is None:
        raw_args = tool_call.get("arguments")

    input_obj = normalize_tool_input(raw_args)

    return {
        "type": "tool_use",
        "id": tool_id,
        "name": name,
        "input": input_obj,
    }


def map_openai_finish_reason_to_anthropic(finish_reason: Optional[str], has_tool_calls: bool) -> str:
    if has_tool_calls or finish_reason in {"tool_calls", "function_call"}:
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    return "end_turn"


def build_anthropic_response_from_openai(result: Dict[str, Any], requested_model: str) -> Dict[str, Any]:
    choices = result.get("choices") or []
    choice = choices[0] if choices else {}

    finish_reason = choice.get("finish_reason")
    message = choice.get("message") or {}

    text = extract_openai_text_content(message.get("content"))
    raw_tool_calls = message.get("tool_calls") or []

    content_blocks: List[Dict[str, Any]] = []

    if text != "":
        content_blocks.append({
            "type": "text",
            "text": text,
        })

    for idx, tool_call in enumerate(raw_tool_calls):
        if isinstance(tool_call, dict):
            content_blocks.append(openai_tool_call_to_anthropic_block(tool_call, idx))

    if not content_blocks:
        content_blocks = [{
            "type": "text",
            "text": "",
        }]

    usage_raw = result.get("usage") or {}
    input_tokens = int(usage_raw.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage_raw.get("completion_tokens", 0) or 0)

    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": requested_model or "claude-proxy",
        "content": content_blocks,
        "stop_reason": map_openai_finish_reason_to_anthropic(
            finish_reason=finish_reason,
            has_tool_calls=bool(raw_tool_calls),
        ),
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


# =========================
# Anthropic SSE 生成
# =========================
async def anthropic_stream_from_openai_stream(
    upstream_resp: httpx.Response,
    requested_model: str,
) -> Any:
    """
    将上游 OpenAI-style SSE 转成 Anthropic SSE
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    input_tokens = 0
    output_tokens = 0

    next_content_index = 0

    text_block_index: Optional[int] = None
    text_block_open = False
    text_accum: List[str] = []

    # key: openai tool_calls[].index
    tool_states: Dict[int, Dict[str, Any]] = {}

    final_finish_reason: Optional[str] = None

    def ensure_text_block_started() -> List[str]:
        nonlocal text_block_index, text_block_open, next_content_index
        events: List[str] = []
        if text_block_index is None:
            text_block_index = next_content_index
            next_content_index += 1
        if not text_block_open:
            text_block_open = True
            events.append(sse_pack("content_block_start", {
                "type": "content_block_start",
                "index": text_block_index,
                "content_block": {
                    "type": "text",
                    "text": "",
                },
            }))
        return events

    def close_text_block_if_open() -> List[str]:
        nonlocal text_block_open
        events: List[str] = []
        if text_block_open and text_block_index is not None:
            text_block_open = False
            events.append(sse_pack("content_block_stop", {
                "type": "content_block_stop",
                "index": text_block_index,
            }))
        return events

    def ensure_tool_state(tool_idx: int) -> Dict[str, Any]:
        nonlocal next_content_index
        if tool_idx not in tool_states:
            tool_states[tool_idx] = {
                "content_index": next_content_index,
                "id": None,
                "name": "",
                "started": False,
                "pending_args": [],
                "total_args_text": "",
            }
            next_content_index += 1
        return tool_states[tool_idx]

    def maybe_start_tool_block(state: Dict[str, Any]) -> List[str]:
        events: List[str] = []
        if not state["started"]:
            tool_id = state["id"] or f"toolu_{uuid.uuid4().hex[:24]}"
            state["id"] = tool_id

            tool_name = state["name"] if state["name"] else "tool"
            state["started"] = True

            events.append(sse_pack("content_block_start", {
                "type": "content_block_start",
                "index": state["content_index"],
                "content_block": {
                    "type": "tool_use",
                    "id": tool_id,
                    "name": tool_name,
                    "input": {},
                },
            }))

            if state["pending_args"]:
                for piece in state["pending_args"]:
                    if piece:
                        events.append(sse_pack("content_block_delta", {
                            "type": "content_block_delta",
                            "index": state["content_index"],
                            "delta": {
                                "type": "input_json_delta",
                                "partial_json": piece,
                            },
                        }))
                state["pending_args"].clear()

        return events

    try:
        # message_start
        yield sse_pack("message_start", {
            "type": "message_start",
            "message": {
                "id": message_id,
                "type": "message",
                "role": "assistant",
                "model": requested_model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {
                    "input_tokens": input_tokens,
                    "output_tokens": 0,
                },
            },
        })

        async for raw_line in upstream_resp.aiter_lines():
            if raw_line is None:
                continue

            line = raw_line.strip()
            if not line:
                continue

            # 兼容标准 SSE: 只处理 data 行
            if not line.startswith("data:"):
                continue

            payload_str = line[5:].strip()

            if not payload_str:
                continue

            if payload_str == "[DONE]":
                break

            try:
                chunk = json.loads(payload_str)
            except Exception:
                logging.warning("[UPSTREAM NON-JSON CHUNK] %s", payload_str[:500])
                continue

            usage = chunk.get("usage") or {}
            if usage:
                input_tokens = int(usage.get("prompt_tokens", input_tokens) or input_tokens)
                output_tokens = int(usage.get("completion_tokens", output_tokens) or output_tokens)

            choices = chunk.get("choices") or []
            if not choices:
                continue

            choice = choices[0] or {}
            delta = choice.get("delta") or {}
            finish_reason = choice.get("finish_reason")
            if finish_reason:
                final_finish_reason = finish_reason

            # 文本增量
            delta_content = delta.get("content")
            if isinstance(delta_content, str) and delta_content:
                for ev in close_text_block_if_open() if False else []:
                    yield ev  # 保留结构，不走这分支

                for ev in ensure_text_block_started():
                    yield ev

                text_accum.append(delta_content)
                output_tokens = max(output_tokens, estimate_tokens_from_text("".join(text_accum)))

                yield sse_pack("content_block_delta", {
                    "type": "content_block_delta",
                    "index": text_block_index,
                    "delta": {
                        "type": "text_delta",
                        "text": delta_content,
                    },
                })

            # 工具调用增量
            delta_tool_calls = delta.get("tool_calls") or []
            if isinstance(delta_tool_calls, list) and delta_tool_calls:
                # 一旦开始工具块，先关闭文本块
                for ev in close_text_block_if_open():
                    yield ev

                for item in delta_tool_calls:
                    if not isinstance(item, dict):
                        continue

                    tool_idx = int(item.get("index", 0))
                    state = ensure_tool_state(tool_idx)

                    if item.get("id") and not state["id"]:
                        state["id"] = item["id"]

                    function_obj = item.get("function") or {}

                    # 某些实现会把 name 分片返回
                    name_piece = function_obj.get("name")
                    if isinstance(name_piece, str) and name_piece:
                        state["name"] += name_piece

                    args_piece = function_obj.get("arguments")
                    if isinstance(args_piece, str) and args_piece:
                        state["total_args_text"] += args_piece
                        if state["started"]:
                            yield sse_pack("content_block_delta", {
                                "type": "content_block_delta",
                                "index": state["content_index"],
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": args_piece,
                                },
                            })
                        else:
                            state["pending_args"].append(args_piece)

                    # 只要看到了工具调用痕迹，就尽快发 start
                    for ev in maybe_start_tool_block(state):
                        yield ev

                    output_tokens = max(
                        output_tokens,
                        estimate_tokens_from_text(
                            "".join(text_accum) + "".join(
                                [s["total_args_text"] for s in tool_states.values()]
                            )
                        ),
                    )

        # 收尾：关闭文本块
        for ev in close_text_block_if_open():
            yield ev

        # 收尾：关闭工具块
        for _, state in sorted(tool_states.items(), key=lambda kv: kv[1]["content_index"]):
            for ev in maybe_start_tool_block(state):
                yield ev

            if state["started"]:
                yield sse_pack("content_block_stop", {
                    "type": "content_block_stop",
                    "index": state["content_index"],
                })

        has_tool_calls = bool(tool_states)
        stop_reason = map_openai_finish_reason_to_anthropic(final_finish_reason, has_tool_calls)

        yield sse_pack("message_delta", {
            "type": "message_delta",
            "delta": {
                "stop_reason": stop_reason,
                "stop_sequence": None,
            },
            "usage": {
                "output_tokens": int(output_tokens),
            },
        })

        yield sse_pack("message_stop", {
            "type": "message_stop",
        })

    except Exception as e:
        logging.exception("stream convert failed")
        yield sse_pack("error", {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": f"stream conversion failed: {e}",
            },
        })
    finally:
        await upstream_resp.aclose()


async def anthropic_stream_from_openai_json(
    result: Dict[str, Any],
    requested_model: str,
) -> Any:
    """
    上游非流式时，给 Claude 伪装成 Anthropic SSE
    """
    message_id = f"msg_{uuid.uuid4().hex[:24]}"
    converted = build_anthropic_response_from_openai(result, requested_model)

    yield sse_pack("message_start", {
        "type": "message_start",
        "message": {
            "id": message_id,
            "type": "message",
            "role": "assistant",
            "model": requested_model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {
                "input_tokens": int(converted.get("usage", {}).get("input_tokens", 0)),
                "output_tokens": 0,
            },
        },
    })

    for idx, block in enumerate(converted.get("content", [])):
        if block.get("type") == "text":
            text = block.get("text", "")
            yield sse_pack("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {"type": "text", "text": ""},
            })
            if text:
                yield sse_pack("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {"type": "text_delta", "text": text},
                })
            yield sse_pack("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

        elif block.get("type") == "tool_use":
            yield sse_pack("content_block_start", {
                "type": "content_block_start",
                "index": idx,
                "content_block": {
                    "type": "tool_use",
                    "id": block.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
                    "name": block.get("name") or "tool",
                    "input": {},
                },
            })

            input_json = safe_json_dumps(block.get("input", {}))
            if input_json:
                yield sse_pack("content_block_delta", {
                    "type": "content_block_delta",
                    "index": idx,
                    "delta": {
                        "type": "input_json_delta",
                        "partial_json": input_json,
                    },
                })

            yield sse_pack("content_block_stop", {
                "type": "content_block_stop",
                "index": idx,
            })

    yield sse_pack("message_delta", {
        "type": "message_delta",
        "delta": {
            "stop_reason": converted.get("stop_reason"),
            "stop_sequence": None,
        },
        "usage": {
            "output_tokens": int(converted.get("usage", {}).get("output_tokens", 0)),
        },
    })

    yield sse_pack("message_stop", {
        "type": "message_stop",
    })


# =========================
# 计数
# =========================
def estimate_input_tokens_heuristic(body: Dict[str, Any]) -> int:
    payload = {
        "system": body.get("system"),
        "messages": body.get("messages"),
        "tools": body.get("tools"),
    }
    text = safe_json_dumps(payload)
    return max(1, math.ceil(len(text) / 4))


# =========================
# 中间件
# =========================
@app.middleware("http")
async def timeout_middleware(request: Request, call_next):
    try:
        return await asyncio.wait_for(call_next(request), timeout=TIMEOUT)
    except asyncio.TimeoutError:
        return anthropic_error(504, "api_error", "Request timeout")


# =========================
# 路由
# =========================
@app.api_route("/", methods=["GET", "HEAD"])
async def root_probe():
    return Response(status_code=200)


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    try:
        body = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Body must be valid JSON")

    if not isinstance(body, dict):
        return anthropic_error(400, "invalid_request_error", "Request body must be a JSON object")

    return {
        "input_tokens": estimate_input_tokens_heuristic(body),
    }


@app.post("/v1/messages")
async def claude_messages_proxy(request: Request):
    try:
        body = await request.json()
    except Exception:
        return anthropic_error(400, "invalid_request_error", "Body must be valid JSON")

    if not isinstance(body, dict):
        return anthropic_error(400, "invalid_request_error", "Request body must be a JSON object")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return anthropic_error(400, "invalid_request_error", "messages must be a non-empty list")

    requested_model = str(body.get("model") or "claude-proxy")
    want_stream = bool(body.get("stream"))

    # 记录 Claude Code 请求头，便于排障
    logging.info("[HEADERS] anthropic-version=%s anthropic-beta=%s x-request-id=%s",
                 request.headers.get("anthropic-version"),
                 request.headers.get("anthropic-beta"),
                 request.headers.get("x-request-id"))

    try:
        openai_payload = build_openai_payload_from_anthropic(
            body=body,
            stream=(want_stream and USE_UPSTREAM_STREAM),
        )
    except Exception as e:
        logging.exception("payload build failed")
        return anthropic_error(500, "api_error", f"Failed to build upstream payload: {e}")

    logging.info(
        "[REQ] anthropic_model=%s upstream_model=%s stream=%s upstream_stream=%s messages=%s tools=%s",
        requested_model,
        UPSTREAM_MODEL,
        want_stream,
        openai_payload.get("stream"),
        len(openai_payload.get("messages", [])),
        len(openai_payload.get("tools", [])) if isinstance(openai_payload.get("tools"), list) else 0,
    )

    async with semaphore:
        # =========================
        # Claude 要流式
        # =========================
        if want_stream:
            # 方案 A：真正透传上游流式
            if USE_UPSTREAM_STREAM:
                try:
                    upstream_req = request.app.state.http.build_request(
                        "POST",
                        OPENAI_API,
                        headers=upstream_headers(),
                        json=openai_payload,
                    )
                    upstream_resp = await request.app.state.http.send(upstream_req, stream=True)
                except httpx.TimeoutException:
                    return anthropic_error(504, "api_error", "Upstream timeout")
                except httpx.RequestError as e:
                    return anthropic_error(502, "api_error", f"Upstream request failed: {e}")

                if upstream_resp.status_code >= 400:
                    try:
                        err_text = (await upstream_resp.aread()).decode("utf-8", errors="ignore")
                    finally:
                        await upstream_resp.aclose()

                    return anthropic_error(
                        upstream_resp.status_code if upstream_resp.status_code < 600 else 502,
                        map_upstream_error_type(upstream_resp.status_code),
                        f"Upstream error: {err_text[:4000]}",
                    )

                return StreamingResponse(
                    anthropic_stream_from_openai_stream(upstream_resp, requested_model),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            # 方案 B：上游非流式，给 Claude 做伪流式
            try:
                resp = await request.app.state.http.post(
                    OPENAI_API,
                    headers=upstream_headers(),
                    json={**openai_payload, "stream": False},
                )
            except httpx.TimeoutException:
                return anthropic_error(504, "api_error", "Upstream timeout")
            except httpx.RequestError as e:
                return anthropic_error(502, "api_error", f"Upstream request failed: {e}")

            if resp.status_code >= 400:
                try:
                    err_json = resp.json()
                    detail = err_json.get("error") or err_json
                    detail_text = detail if isinstance(detail, str) else safe_json_dumps(detail)
                except Exception:
                    detail_text = resp.text

                return anthropic_error(
                    resp.status_code if resp.status_code < 600 else 502,
                    map_upstream_error_type(resp.status_code),
                    f"Upstream error: {detail_text[:4000]}",
                )

            try:
                result = resp.json()
            except Exception:
                return anthropic_error(502, "api_error", "Upstream returned non-JSON response")

            return StreamingResponse(
                anthropic_stream_from_openai_json(result, requested_model),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        # =========================
        # Claude 要非流式
        # =========================
        try:
            resp = await request.app.state.http.post(
                OPENAI_API,
                headers=upstream_headers(),
                json={**openai_payload, "stream": False},
            )
        except httpx.TimeoutException:
            return anthropic_error(504, "api_error", "Upstream timeout")
        except httpx.RequestError as e:
            return anthropic_error(502, "api_error", f"Upstream request failed: {e}")

    if resp.status_code >= 400:
        try:
            err_json = resp.json()
            detail = err_json.get("error") or err_json
            detail_text = detail if isinstance(detail, str) else safe_json_dumps(detail)
        except Exception:
            detail_text = resp.text

        return anthropic_error(
            resp.status_code if resp.status_code < 600 else 502,
            map_upstream_error_type(resp.status_code),
            f"Upstream error: {detail_text[:4000]}",
        )

    try:
        result = resp.json()
    except Exception:
        return anthropic_error(502, "api_error", "Upstream returned non-JSON response")

    try:
        anthropic_resp = build_anthropic_response_from_openai(result, requested_model)
    except Exception as e:
        logging.exception("response convert failed")
        return anthropic_error(500, "api_error", f"Failed to convert upstream response: {e}")

    logging.info(
        "[RESP] stop_reason=%s input_tokens=%s output_tokens=%s",
        anthropic_resp.get("stop_reason"),
        anthropic_resp.get("usage", {}).get("input_tokens", 0),
        anthropic_resp.get("usage", {}).get("output_tokens", 0),
    )

    return JSONResponse(content=anthropic_resp)
