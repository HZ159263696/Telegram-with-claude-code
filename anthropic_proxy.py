#!/usr/bin/env python3
"""
Anthropic-to-OpenAI Translation Proxy

Sits between Claude Code CLI and upstream providers:
  Claude Code CLI (Anthropic Messages API)
    → anthropic_proxy.py (port 4001, translates format)
      → 智谱 / 百炼 (OpenAI-compatible endpoints, direct)

Routes by model alias (CLI_MODEL_ALIAS from bridge.py).
API keys are read from ~/.claude/telegram_api_keys.json.
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time as _time
import urllib.request
from http.server import HTTPServer, BaseHTTPRequestHandler

# 本进程只访问国内厂商端点；彻底忽略系统/VPN 代理，避免 VPN 关闭后仍指向
# 127.0.0.1 失效端口，或国内请求被错误送往境外代理。
for _proxy_var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                   "ALL_PROXY", "all_proxy"):
    os.environ.pop(_proxy_var, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"
urllib.request.install_opener(
    urllib.request.build_opener(urllib.request.ProxyHandler({}))
)

PROXY_PORT = 4001
API_KEYS_FILE = os.path.expanduser("~/.claude/telegram_api_keys.json")

# CLI model alias → (api_base_url, real_model_name, provider_key)
_MODEL_ROUTES = {
    "claude-3-5-sonnet-20241022": ("https://api.deepseek.com",                         "deepseek-v4-flash", "deepseek"),
    "claude-3-opus-20240229":     ("https://api.deepseek.com",                         "deepseek-v4-pro",   "deepseek"),
    "claude-3-sonnet-20240229":  ("https://open.bigmodel.cn/api/paas/v4",              "glm-4-plus",    "zhipu"),
    "claude-3-haiku-20240307":   ("https://open.bigmodel.cn/api/paas/v4",              "glm-4-flash",   "zhipu"),
    "claude-3-5-sonnet-latest":  ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-max",      "bailian"),
    "claude-3-opus-latest":      ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-plus",     "bailian"),
}


def _load_api_keys():
    if os.path.exists(API_KEYS_FILE):
        try:
            return json.load(open(API_KEYS_FILE))
        except Exception:
            pass
    return {}


def _zhipu_jwt(api_key):
    id_, secret = api_key.split(".", 1)
    def b64(s):
        return base64.urlsafe_b64encode(s if isinstance(s, bytes) else s.encode()).rstrip(b"=").decode()
    header  = b64(json.dumps({"alg": "HS256", "sign_type": "SIGN"}, separators=(",", ":")))
    ts      = int(_time.time() * 1000)
    payload = b64(json.dumps({"api_key": id_, "exp": ts + 3600000, "timestamp": ts}, separators=(",", ":")))
    sig     = hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()
    return f"{header}.{payload}.{b64(sig)}"


def _resolve_route(model):
    """Return (endpoint_url, real_model, auth_header) for a CLI model alias, or None if unknown."""
    route = _MODEL_ROUTES.get(model)
    if not route:
        return None
    base_url, real_model, provider = route
    keys = _load_api_keys()
    if provider == "zhipu":
        api_key = keys.get("zhipu", "")
        token = _zhipu_jwt(api_key) if api_key and "." in api_key else api_key
        auth = f"Bearer {token}"
    else:
        api_key = keys.get(provider, "")
        auth = f"Bearer {api_key}"
    return f"{base_url}/chat/completions", real_model, auth


def anthropic_to_openai(body):
    """Convert Anthropic Messages API request to OpenAI Chat Completions format."""
    messages = []

    # System message
    system = body.get("system")
    if system:
        if isinstance(system, list):
            system_text = "\n".join(
                b.get("text", "") for b in system if b.get("type") == "text"
            )
        else:
            system_text = system
        if system_text:
            messages.append({"role": "system", "content": system_text})

    # Convert messages
    for msg in body.get("messages", []):
        role = msg["role"]
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
        elif isinstance(content, list):
            # Handle content blocks
            parts = []
            tool_calls = []
            tool_results = []

            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    parts.append(block.get("text", ""))
                elif btype == "tool_use":
                    tool_calls.append({
                        "id": block.get("id", ""),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    })
                elif btype == "tool_result":
                    result_content = block.get("content", "")
                    if isinstance(result_content, list):
                        result_content = "\n".join(
                            b.get("text", str(b))
                            for b in result_content
                        )
                    elif not isinstance(result_content, str):
                        result_content = str(result_content)
                    tool_results.append({
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id", ""),
                        "content": result_content,
                    })
                elif btype == "image":
                    # Pass image as text description (most models can't handle images)
                    parts.append("[Image]")

            if role == "assistant":
                msg_out = {"role": "assistant"}
                if parts:
                    msg_out["content"] = "\n".join(parts)
                if tool_calls:
                    msg_out["tool_calls"] = tool_calls
                    if "content" not in msg_out:
                        msg_out["content"] = None
                messages.append(msg_out)
            elif role == "user":
                if tool_results:
                    # First add any text content as user message
                    if parts:
                        messages.append({"role": "user", "content": "\n".join(parts)})
                    # Then add tool results
                    messages.extend(tool_results)
                else:
                    messages.append({"role": "user", "content": "\n".join(parts) if parts else ""})

    # Convert tools
    tools = None
    if body.get("tools"):
        tools = []
        for tool in body["tools"]:
            tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

    openai_body = {
        "model": body.get("model", ""),
        "messages": messages,
        "max_tokens": body.get("max_tokens", 4096),
    }

    if tools:
        openai_body["tools"] = tools
        # Allow model to choose whether to use tools
        openai_body["tool_choice"] = "auto"

    if body.get("temperature") is not None:
        openai_body["temperature"] = body["temperature"]

    if body.get("stream"):
        openai_body["stream"] = True

    return openai_body


def openai_to_anthropic(result, model):
    """Convert OpenAI Chat Completions response to Anthropic Messages API format."""
    choice = result.get("choices", [{}])[0]
    message = choice.get("message", {})

    content = []
    text = message.get("content")
    if text:
        content.append({"type": "text", "text": text})

    # Convert tool calls
    for tc in message.get("tool_calls", []):
        func = tc.get("function", {})
        try:
            args = json.loads(func.get("arguments", "{}"))
        except (json.JSONDecodeError, TypeError):
            args = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": func.get("name", ""),
            "input": args,
        })

    if not content:
        content.append({"type": "text", "text": ""})

    # Map finish_reason to stop_reason
    finish_reason = choice.get("finish_reason", "end_turn")
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    stop_reason = stop_reason_map.get(finish_reason, "end_turn")

    usage = result.get("usage", {})

    return {
        "id": result.get("id", "msg_proxy"),
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
    }


def openai_stream_to_anthropic_stream(openai_body, model, endpoint_url=None, auth_header=None):
    """Send streaming request to upstream provider and yield Anthropic SSE events."""
    data = json.dumps(openai_body).encode()
    headers = {"Content-Type": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header
    req = urllib.request.Request(
        endpoint_url,
        data=data,
        headers=headers,
    )

    # Yield message_start
    yield {
        "type": "message_start",
        "message": {
            "id": "msg_proxy_stream",
            "type": "message",
            "role": "assistant",
            "content": [],
            "model": model,
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }

    # Yield content_block_start for first text block
    yield {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "text", "text": ""},
    }

    block_index = 0
    tool_index = 0
    current_tool_calls = {}  # id -> accumulated args
    final_finish_reason = None

    with urllib.request.urlopen(req, timeout=120) as resp:
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").strip()
            if not line.startswith("data: "):
                continue
            payload = line[6:]
            if payload == "[DONE]":
                break
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue

            delta = chunk.get("choices", [{}])[0].get("delta", {})
            finish_reason = chunk.get("choices", [{}])[0].get("finish_reason")

            # Text delta
            text = delta.get("content")
            if text:
                yield {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                }

            # Tool call deltas
            for tc_delta in delta.get("tool_calls", []):
                tc_idx = tc_delta.get("index", tool_index)
                tc_id = tc_delta.get("id")
                func = tc_delta.get("function", {})

                if tc_id and tc_id not in current_tool_calls:
                    # New tool call - close previous text block, start tool block
                    if block_index == 0:
                        yield {"type": "content_block_stop", "index": 0}

                    block_index += 1
                    current_tool_calls[tc_id] = {
                        "name": func.get("name", ""),
                        "args": "",
                    }
                    yield {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc_id,
                            "name": func.get("name", ""),
                            "input": {},
                        },
                    }

                # Accumulate arguments
                if func.get("arguments"):
                    for tid, tdata in current_tool_calls.items():
                        if tc_id and tid == tc_id:
                            yield {
                                "type": "content_block_delta",
                                "index": block_index,
                                "delta": {
                                    "type": "input_json_delta",
                                    "partial_json": func["arguments"],
                                },
                            }
                            break

            if finish_reason:
                final_finish_reason = finish_reason
                break

    # Close open blocks
    yield {"type": "content_block_stop", "index": block_index}

    # message_delta with final stop_reason
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
    }
    stop_reason = stop_reason_map.get(final_finish_reason, "end_turn")
    yield {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": 0},
    }
    yield {"type": "message_stop"}


class ProxyHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        model = body.get("model", "unknown")
        is_stream = body.get("stream", False)

        # Resolve route: CLI alias → real endpoint + real model
        route = _resolve_route(model)
        if route:
            endpoint_url, real_model, auth_header = route
        else:
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "type": "error",
                "error": {"type": "api_error", "message": f"Unknown model alias: {model}"},
            }).encode())
            return

        print(f"[proxy] {model} → {real_model} stream={is_stream} messages={len(body.get('messages', []))}")

        try:
            openai_body = anthropic_to_openai(body)
            openai_body["model"] = real_model

            if is_stream:
                self._handle_stream(openai_body, model, endpoint_url, auth_header)
            else:
                self._handle_sync(openai_body, model, endpoint_url, auth_header)
        except urllib.error.HTTPError as e:
            # Forward the actual HTTP status code (e.g., 429 rate limit) instead of always 500
            err_body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            print(f"[proxy] HTTP {e.code}: {err_body[:200]}")
            # Map HTTP codes to Anthropic error types
            err_type_map = {429: "rate_limit_error", 401: "authentication_error", 403: "permission_error"}
            err_type = err_type_map.get(e.code, "api_error")
            error_resp = {
                "type": "error",
                "error": {"type": err_type, "message": f"HTTP Error {e.code}: {e.reason}"},
            }
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(error_resp).encode())
        except Exception as e:
            print(f"[proxy] error: {e}")
            error_resp = {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            }
            self.send_response(500)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(error_resp).encode())

    def _handle_sync(self, openai_body, model, endpoint_url, auth_header):
        """Non-streaming request."""
        openai_body["stream"] = False
        data = json.dumps(openai_body).encode()
        headers = {"Content-Type": "application/json"}
        if auth_header:
            headers["Authorization"] = auth_header
        req = urllib.request.Request(endpoint_url, data=data, headers=headers)
        with urllib.request.urlopen(req, timeout=120) as r:
            result = json.loads(r.read())

        anthropic_resp = openai_to_anthropic(result, model)
        out = json.dumps(anthropic_resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _handle_stream(self, openai_body, model, endpoint_url, auth_header):
        """Streaming request - convert OpenAI SSE to Anthropic SSE."""
        openai_body["stream"] = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()

            for event in openai_stream_to_anthropic_stream(openai_body, model, endpoint_url, auth_header):
                line = f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
                self.wfile.write(line.encode())
                self.wfile.flush()
        except urllib.error.HTTPError as e:
            err_body = e.read().decode(errors="replace") if hasattr(e, "read") else ""
            print(f"[proxy] stream HTTP {e.code}: {err_body[:200]}")
            err_type_map = {429: "rate_limit_error", 401: "authentication_error", 403: "permission_error"}
            err_type = err_type_map.get(e.code, "api_error")
            error_resp = {"type": "error", "error": {"type": err_type, "message": f"HTTP Error {e.code}: {e.reason}"}}
            # If headers not yet sent, send proper status code
            try:
                self.send_response(e.code)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(error_resp).encode())
            except Exception:
                # Headers already sent (streaming started), send as SSE error event
                self.wfile.write(f"event: error\ndata: {json.dumps(error_resp)}\n\n".encode())
                self.wfile.flush()
        except Exception as e:
            print(f"[proxy] stream error: {e}")
            error_event = {
                "type": "error",
                "error": {"type": "api_error", "message": str(e)},
            }
            try:
                self.wfile.write(f"event: error\ndata: {json.dumps(error_event)}\n\n".encode())
                self.wfile.flush()
            except Exception:
                pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Anthropic-to-OpenAI Proxy OK")

    def log_message(self, format, *args):
        pass  # Suppress default logging


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else PROXY_PORT
    print(f"[anthropic_proxy] Listening on port {port}, routing to provider endpoints")
    HTTPServer.allow_reuse_address = True
    HTTPServer(("0.0.0.0", port), ProxyHandler).serve_forever()
