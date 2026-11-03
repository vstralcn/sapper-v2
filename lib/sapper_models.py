"""Model profile transport and an HTTP protocol adapter; Claude keeps the tool loop."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROTOCOLS = {"anthropic": "Anthropic Messages", "responses": "OpenAI Responses", "chat_completions": "OpenAI Chat Completions"}
ALIASES = {"response": "responses", "completion": "chat_completions", "completions": "chat_completions"}
MAX_BODY = 32 * 1024 * 1024


class ModelError(ValueError):
    pass


def protocol(profile):
    value = profile.get("protocol") or "anthropic"
    value = ALIASES.get(value, value)
    if value not in PROTOCOLS:
        raise ModelError("不支持的模型协议")
    return value


def validate(profile, require_model=True):
    p = dict(profile)
    p["protocol"] = protocol(p)
    base = p.get("base_url", "")
    if not isinstance(base, str):
        raise ModelError("API 地址必须为文本")
    p["base_url"] = base.strip().rstrip("/")
    if base:
        u = urllib.parse.urlsplit(base)
        if u.scheme not in ("http", "https") or not u.hostname or u.username or u.password or u.query or u.fragment:
            raise ModelError("API 地址须为无凭据、查询参数或片段的 HTTP(S) 地址")
        try:
            u.port
        except ValueError as e:
            raise ModelError("API 端口不正确") from e
    for key, default, low, high in (("timeout_seconds", 120, 5, 600), ("max_output_tokens", 0, 0, 131072)):
        value = p.get(key, default)
        if isinstance(value, bool):
            raise ModelError(f"{key} 必须为整数")
        try:
            value = int(value)
        except (ValueError, TypeError) as e:
            raise ModelError(f"{key} 必须为整数") from e
        if not low <= value <= high:
            raise ModelError(f"{key} 必须在 {low}–{high} 之间")
        p[key] = value
    p["token_field"] = p.get("token_field") or "max_completion_tokens"
    if p["token_field"] not in ("max_tokens", "max_completion_tokens"):
        raise ModelError("Chat Completions token 参数不正确")
    if require_model and not str(p.get("model", "")).strip():
        raise ModelError("请填写或选择模型 ID")
    if p["protocol"] != "anthropic" and p.get("engine_prefix"):
        raise ModelError("Responses／Chat Completions 使用原 Claude Code 协议适配；请清空 CLI 包装器")
    return p


def resolved(profile):
    p = validate(profile)
    if p["protocol"] == "anthropic":
        p["base_url"] = p["base_url"] or os.environ.get("ANTHROPIC_BASE_URL") or "https://api.anthropic.com"
        p["api_key"] = p.get("api_key") or os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_API_KEY", "")
    else:
        p["base_url"] = p["base_url"] or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        p["api_key"] = p.get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    return validate(p)


def endpoint(profile, resource):
    base = profile["base_url"].rstrip("/")
    for suffix in ("/chat/completions", "/messages/count_tokens", "/responses", "/messages", "/models"):
        if base.endswith(suffix):
            base = base[:-len(suffix)]
            break
    if (profile["protocol"] == "anthropic" and not base.endswith("/v1")) or not urllib.parse.urlsplit(base).path.rstrip("/"):
        base += "/v1"
    return base + "/" + resource


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward a profile credential to a redirected host.


def request(profile, resource, body=None, timeout=None):
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    key = profile.get("api_key", "")
    if key:
        headers["Authorization"] = "Bearer " + key
    if profile["protocol"] == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
        if key:
            headers["x-api-key"] = key
    req = urllib.request.Request(endpoint(profile, resource), data=json.dumps(body).encode() if body is not None else None, headers=headers)
    try:
        return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout or profile["timeout_seconds"])
    except urllib.error.HTTPError as e:
        # Do not reflect provider bodies: they can echo credentials or prompts.
        e.close()
        raise ModelError(f"上游返回 HTTP {e.code}；请检查协议、API 地址、密钥、模型权限或限流状态") from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise ModelError("无法连接模型服务或请求超时；请检查 API 地址、网络和超时设置") from None


def read_json_response(response):
    raw = response.read(MAX_BODY + 1)
    if len(raw) > MAX_BODY:
        raise ModelError("模型响应过大")
    try:
        result = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ModelError("上游没有返回有效 JSON；请检查 API 地址和协议") from None
    if not isinstance(result, dict) or result.get("error"):
        raise ModelError("模型服务返回错误或响应格式不正确")
    return result


def list_models(profile):
    p = resolved({**profile, "model": profile.get("model") or "discovery"})
    found = {}
    resource = "models"
    for _ in range(10):
        with request(p, resource, timeout=min(p["timeout_seconds"], 30)) as r:
            obj = read_json_response(r)
        if not isinstance(obj.get("data"), list):
            raise ModelError("模型列表缺少 data 数组；仍可手动填写模型 ID")
        for item in obj["data"]:
            if isinstance(item, dict) and isinstance(item.get("id"), str):
                found[item["id"]] = {"id": item["id"], "name": item.get("display_name") or item["id"]}
        if p["protocol"] != "anthropic" or not obj.get("has_more"):
            return {"models": sorted(found.values(), key=lambda m: m["id"]), "truncated": False}
        last = obj.get("last_id")
        if not isinstance(last, str):
            raise ModelError("模型分页响应缺少 last_id")
        resource = "models?" + urllib.parse.urlencode({"after_id": last, "limit": 100})
    return {"models": sorted(found.values(), key=lambda m: m["id"]), "truncated": True}


def blocks(content):
    return [{"type": "text", "text": content}] if isinstance(content, str) else content or []


def text(content):
    return "\n".join(b.get("text", "") for b in blocks(content) if b.get("type") == "text")


def media(block, responses=False):
    kind = block.get("type")
    if kind == "text":
        return {"type": "input_text" if responses else "text", "text": block.get("text", "")}
    if kind == "image":
        src = block.get("source", {})
        if src.get("type") == "base64":
            url = f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"
        elif src.get("type") == "url":
            url = src.get("url", "")
        else:
            raise ModelError("所选协议暂不支持此图片来源；请使用 URL 或 base64")
        return {"type": "input_image", "image_url": url} if responses else {"type": "image_url", "image_url": {"url": url}}
    if kind == "document":
        src = block.get("source", {})
        if src.get("type") == "text":
            return {"type": "input_text" if responses else "text", "text": src.get("data", "")}
        if src.get("type") == "base64":
            url = f"data:{src.get('media_type', 'application/pdf')};base64,{src.get('data', '')}"
            file = {"filename": block.get("title") or "attachment.pdf", "file_data": url}
            return {"type": "input_file", **file} if responses else {"type": "file", "file": file}
        if src.get("type") == "url" and responses:
            return {"type": "input_file", "file_url": src["url"]}
        raise ModelError("所选协议暂不支持此文档来源；请先使用原工具读取文件")
    if kind in ("thinking", "redacted_thinking"):
        return None  # Vendor thought signatures are not transferable.
    if kind == "tool_reference":
        return {"type": "input_text" if responses else "text", "text": json.dumps(block)}
    raise ModelError(f"所选协议暂不支持消息块 {str(kind)[:60]}；可切换 Anthropic Messages")


def to_openai(body, profile, reasoning=None):
    responses = profile["protocol"] == "responses"
    messages = []
    inserted_thoughts = set()
    system = text(body.get("system", ""))
    if system:
        messages.append({"role": "system", "content": system})
    for msg in body.get("messages", []):
        role = msg.get("role")
        if role not in ("assistant", "user", "system", "developer"):
            raise ModelError("消息角色不正确")
        parts, calls = [], []
        def flush():
            if not parts:
                return
            contents = list(parts)
            if responses and role == "assistant":
                contents = [{"type": "output_text", "text": b["text"], "annotations": []} for b in parts if b["type"] == "input_text"]
                if len(contents) != len(parts):
                    raise ModelError("不支持 assistant 图片／文件消息")
            messages.append({"role": role, "content": contents})
            parts.clear()
        for b in blocks(msg.get("content")):
            kind = b.get("type")
            if kind == "tool_use":
                if role != "assistant":
                    raise ModelError("tool_use 必须来自 assistant")
                call = {"id": b["id"], "type": "function", "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}))}}
                if responses:
                    flush()
                    if reasoning and b["id"] in reasoning:
                        for thought in reasoning[b["id"]]:
                            identity = json.dumps(thought, sort_keys=True)
                            if identity not in inserted_thoughts:
                                messages.append(thought)
                                inserted_thoughts.add(identity)
                    messages.append({"type": "function_call", "call_id": b["id"], **call["function"]})
                else:
                    calls.append(call)
            elif kind == "tool_result":
                if role != "user":
                    raise ModelError("tool_result 必须来自 user")
                flush()
                result = blocks(b.get("content", ""))
                summary = text(result)
                if b.get("is_error"):
                    summary = "Tool error: " + summary
                if responses:
                    output = [media(x, True) for x in result]
                    messages.append({"type": "function_call_output", "call_id": b["tool_use_id"], "output": output or summary})
                else:
                    messages.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": summary})
                    images = [media(x) for x in result if x.get("type") != "text"]
                    if images:
                        messages.append({"role": "user", "content": images})
            else:
                converted = media(b, responses)
                if converted:
                    parts.append(converted)
        if calls:
            messages.append({"role": role, "content": list(parts) or None, "tool_calls": calls})
        else:
            flush()
    cap = body.get("max_tokens", 4096)
    if profile["max_output_tokens"]:
        cap = min(cap, profile["max_output_tokens"])
    result = {"model": body.get("model") or profile["model"], "stream": bool(body.get("stream")),
              "input" if responses else "messages": messages,
              "max_output_tokens" if responses else profile["token_field"]: cap}
    if responses:
        result.update(store=False, include=["reasoning.encrypted_content"])
    for key in ("temperature", "top_p"):
        if key in body:
            result[key] = body[key]
    if body.get("stop_sequences"):
        if responses:
            raise ModelError("Responses 不支持 stop_sequences；请选择兼容协议")
        result["stop"] = body["stop_sequences"]
    tools = []
    for tool in body.get("tools", []):
        if not tool.get("name") or not isinstance(tool.get("input_schema"), dict):
            raise ModelError("所选协议需要标准函数工具；供应商专用工具请使用 Anthropic Messages")
        f = {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool["input_schema"], "strict": False}
        tools.append({"type": "function", **f} if responses else {"type": "function", "function": f})
    if tools:
        result["tools"] = tools
        choice = body.get("tool_choice", {"type": "auto"})
        name = choice.get("type", "auto")
        if name == "tool":
            result["tool_choice"] = {"type": "function", "name": choice["name"]} if responses else {"type": "function", "function": {"name": choice["name"]}}
        else:
            result["tool_choice"] = {"any": "required"}.get(name, name)
        if choice.get("disable_parallel_tool_use"):
            result["parallel_tool_calls"] = False
    return result


def from_openai(obj, profile, reasoning=None):
    content = []
    stop = "end_turn"
    if profile["protocol"] == "responses":
        output = obj.get("output", [])
        thoughts = [item for item in output if item.get("type") == "reasoning"]
        for item in output:
            if item.get("type") == "message":
                for part in item.get("content", []):
                    value = part.get("text") or part.get("refusal")
                    if value:
                        content.append({"type": "text", "text": value})
            elif item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id")
                content.append(tool_block(call_id, item["name"], item.get("arguments", "{}")))
                if reasoning is not None and thoughts:
                    reasoning[call_id] = thoughts
                    while len(reasoning) > 1024:
                        reasoning.pop(next(iter(reasoning)))
        usage = obj.get("usage") or {}
        if obj.get("status") == "incomplete":
            if (obj.get("incomplete_details") or {}).get("reason") == "max_output_tokens":
                stop = "max_tokens"
            else:
                raise ModelError("Responses 返回未完成结果")
        if obj.get("status") in ("failed", "cancelled"):
            raise ModelError("Responses 生成失败或已取消")
        tokens = {"input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0)}
    else:
        choices = obj.get("choices") or []
        if not choices:
            raise ModelError("Chat Completions 未返回 choices")
        msg = choices[0].get("message") or {}
        value = msg.get("content") or msg.get("refusal")
        if isinstance(value, str) and value:
            content.append({"type": "text", "text": value})
        for call in msg.get("tool_calls", []):
            f = call["function"]
            content.append(tool_block(call["id"], f["name"], f.get("arguments", "{}")))
        if choices[0].get("finish_reason") == "length":
            stop = "max_tokens"
        if choices[0].get("finish_reason") == "content_filter":
            stop = "refusal"
        usage = obj.get("usage") or {}
        tokens = {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)}
    if any(b["type"] == "tool_use" for b in content) and stop != "max_tokens":
        stop = "tool_use"
    if not content and stop == "end_turn":
        raise ModelError("模型未返回文本或工具调用")
    return {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": profile["model"],
            "content": content, "stop_reason": stop, "stop_sequence": None, "usage": tokens}


def tool_block(call_id, name, arguments):
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else arguments
    except ValueError:
        raise ModelError("模型返回了不完整的工具参数 JSON，未执行该工具") from None
    if not isinstance(args, dict):
        raise ModelError("工具参数必须为 JSON 对象，未执行该工具")
    return {"type": "tool_use", "id": call_id, "name": name, "input": args}


def test_connection(profile):
    p = resolved(profile)
    body = {"model": p["model"], "max_tokens": 256, "messages": [{"role": "user", "content": "Reply with OK only."}]}
    started = time.monotonic()
    if p["protocol"] == "anthropic":
        with request(p, "messages", body) as r:
            obj = read_json_response(r)
        answer = text(obj.get("content"))
    else:
        with request(p, "responses" if p["protocol"] == "responses" else "chat/completions", to_openai(body, p)) as r:
            obj = from_openai(read_json_response(r), p)
        answer = text(obj["content"])
    if not answer:
        raise ModelError("已连接但模型未返回文本，不能判为生成测试通过")
    return {"ok": True, "protocol": p["protocol"], "model": p["model"], "latency_ms": round((time.monotonic() - started) * 1000), "reply": answer[:400],
            "message": "文本生成测试通过；工具能力请以实际 agent 任务为准"}


def sse_events(response):
    data = []
    size = 0
    for raw in response:
        size += len(raw)
        if size > MAX_BODY:
            raise ModelError("模型流响应过大")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                payload = "\n".join(data)
                if payload == "[DONE]":
                    return
                yield json.loads(payload)
                data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
    if data and "\n".join(data) != "[DONE]":
        yield json.loads("\n".join(data))


class BridgeServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, profile, token, fingerprint="", usage_path=None):
        self.profile, self.token, self.fingerprint = resolved(profile), token, fingerprint
        self.reasoning = {}
        self.reasoning_lock = threading.Lock()
        self.usage_path = Path(usage_path) if usage_path else None
        self.usage_lock = threading.Lock()
        super().__init__(("127.0.0.1", 0), BridgeHandler)

    def record_usage(self, entry):
        """Append one billing record per model call; tokens only, never prompt text."""
        if not self.usage_path:
            return
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **entry}
        line = json.dumps(entry, ensure_ascii=False) + "\n"
        try:
            with self.usage_lock:
                fd = os.open(self.usage_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                with os.fdopen(fd, "a") as f:
                    f.write(line)
        except OSError:
            pass  # Accounting must never break the tool loop.


class BridgeHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log prompts, keys or provider responses.

    def reply(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def authorized(self):
        provided = self.headers.get("Authorization", "").removeprefix("Bearer ") or self.headers.get("x-api-key", "")
        host = self.headers.get("Host", "")
        return host == f"127.0.0.1:{self.server.server_port}" and secrets.compare_digest(provided, self.server.token)

    def do_GET(self):
        if not self.authorized():
            return self.reply(401, {"error": "unauthorized"})
        if self.path == "/health":
            return self.reply(200, {"fingerprint": self.server.fingerprint, "protocol": self.server.profile["protocol"],
                                    "model": self.server.profile["model"], "pid": os.getpid()})
        return self.reply(404, {"error": "not_found"})

    def event(self, obj):
        self.wfile.write(("event: " + obj["type"] + "\ndata: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode())
        self.wfile.flush()

    def note_usage(self, model, obj, stream):
        usage = obj.get("usage") or {}
        self.server.record_usage({
            "model": model, "protocol": self.server.profile["protocol"], "stream": bool(stream), "status": "ok",
            "input_tokens": usage.get("input_tokens", 0), "output_tokens": usage.get("output_tokens", 0),
            "stop_reason": obj.get("stop_reason"),
            "duration_ms": int((time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)})

    def do_POST(self):
        streaming = False
        self.started = time.monotonic()
        try:
            if not self.authorized():
                return self.reply(401, {"error": {"type": "authentication_error", "message": "unauthorized"}})
            if self.path == "/shutdown":
                self.reply(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            path = urllib.parse.urlsplit(self.path).path
            if path not in ("/v1/messages", "/v1/messages/count_tokens"):
                return self.reply(404, {"error": {"type": "not_found_error", "message": "unsupported endpoint"}})
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= MAX_BODY:
                raise ModelError("模型请求为空或过大")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ModelError("模型请求必须为 JSON 对象")
            if path.endswith("/count_tokens"):
                # OpenAI-compatible providers do not expose the Anthropic tokenizer.
                # Used only as a conservative context estimate; never billed usage.
                return self.reply(200, {"input_tokens": math.ceil(len(json.dumps(body, ensure_ascii=False).encode()) / 3)})
            p = self.server.profile
            with self.server.reasoning_lock:
                outgoing = to_openai(body, p, self.server.reasoning)
            resource = "responses" if p["protocol"] == "responses" else "chat/completions"
            with request(p, resource, outgoing) as upstream:
                if not body.get("stream") or "text/event-stream" not in upstream.headers.get("Content-Type", ""):
                    response = read_json_response(upstream)
                    with self.server.reasoning_lock:
                        obj = from_openai(response, {**p, "model": outgoing["model"]}, self.server.reasoning)
                    if not body.get("stream"):
                        self.note_usage(outgoing["model"], obj, False)
                        return self.reply(200, obj)
                    self.begin_stream(outgoing["model"])
                    streaming = True
                    return self.finish_stream(obj, False)
                self.begin_stream(outgoing["model"])
                streaming = True
                self.relay(upstream, p)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ModelError, ValueError, KeyError, TypeError, OSError) as e:
            message = str(e) if isinstance(e, ModelError) else "模型响应格式异常或连接中断"
            self.server.record_usage({
                "model": self.server.profile["model"], "protocol": self.server.profile["protocol"],
                "stream": bool(streaming), "status": "error", "input_tokens": 0, "output_tokens": 0,
                "error": message[:200],
                "duration_ms": int((time.monotonic() - getattr(self, "started", time.monotonic())) * 1000)})
            error = {"type": "error", "error": {"type": "api_error", "message": message}}
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                if streaming:
                    self.event(error)
                else:
                    self.reply(502 if not isinstance(e, ModelError) else 400, error)

    def begin_stream(self, model):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.event({"type": "message_start", "message": {"id": "msg_" + uuid.uuid4().hex, "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 0, "output_tokens": 0}}})
        self.text_started = False

    def text_delta(self, value):
        if not value:
            return
        if not self.text_started:
            self.event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}})
            self.text_started = True
        self.event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": value}})

    def finish_stream(self, obj, text_sent):
        if not text_sent:
            for b in obj["content"]:
                if b["type"] == "text":
                    self.text_delta(b["text"])
        index = 0
        if self.text_started:
            self.event({"type": "content_block_stop", "index": 0})
            index = 1
        for b in obj["content"]:
            if b["type"] != "tool_use":
                continue
            self.event({"type": "content_block_start", "index": index, "content_block": {**b, "input": {}}})
            self.event({"type": "content_block_delta", "index": index, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}})
            self.event({"type": "content_block_stop", "index": index})
            index += 1
        self.event({"type": "message_delta", "delta": {"stop_reason": obj["stop_reason"], "stop_sequence": None}, "usage": obj["usage"]})
        self.event({"type": "message_stop"})

    def relay(self, upstream, profile):
        calls, chunks, usage, final, finish = {}, [], {}, None, None
        for event in sse_events(upstream):
            if event.get("error") or event.get("type") in ("error", "response.failed"):
                raise ModelError("模型流返回错误，未执行未完成的工具调用")
            kind = event.get("type")
            if profile["protocol"] == "responses":
                if kind in ("response.output_text.delta", "response.refusal.delta"):
                    delta = event.get("delta", "")
                    chunks.append(delta)
                    self.text_delta(delta)
                elif kind in ("response.completed", "response.incomplete"):
                    final = event["response"]
                    break
                else:
                    # Long thinking/function argument generation keeps the client alive.
                    self.event({"type": "ping"})
            else:
                if event.get("usage"):
                    usage = event["usage"]
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0]
                finish = choice.get("finish_reason") or finish
                delta = choice.get("delta") or {}
                value = delta.get("content") or delta.get("refusal") or ""
                if value:
                    chunks.append(value)
                    self.text_delta(value)
                for call in delta.get("tool_calls", []):
                    c = calls.setdefault(call["index"], {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                    if call.get("id"):
                        c["id"] = call["id"]
                    for key in ("name", "arguments"):
                        c["function"][key] += (call.get("function") or {}).get(key) or ""
                if not value:
                    self.event({"type": "ping"})
        if profile["protocol"] == "chat_completions":
            if not finish:
                raise ModelError("模型流提前断开，未执行未完成的工具调用")
            final = {"choices": [{"message": {"content": "".join(chunks), "tool_calls": list(calls.values())}, "finish_reason": finish}], "usage": usage}
        if final is None:
            raise ModelError("Responses 流缺少最终完成事件，未执行未完成的工具调用")
        with self.server.reasoning_lock:
            obj = from_openai(final, profile, self.server.reasoning)
        self.note_usage(profile["model"], obj, True)
        self.finish_stream(obj, bool(chunks))


def private_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4))
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(obj, f)
    os.replace(tmp, path)


def ensure_bridge(state, profile):
    """Detached transport survives Web/controller exit, including native fleet up."""
    profile = resolved(profile)
    transport = {k: profile.get(k) for k in ("protocol", "base_url", "api_key", "model", "timeout_seconds", "max_output_tokens", "token_field")}
    transport["adapter_version"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    fingerprint = hashlib.sha256(json.dumps(transport, sort_keys=True).encode()).hexdigest()[:24]
    folder = Path(state) / "model-bridges" / fingerprint
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (folder / "startup.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        ready = folder / "ready.json"
        def healthy():
            try:
                data = json.loads(ready.read_text())
                if not isinstance(data["port"], int) or not 0 < data["port"] < 65536:
                    return None
                req = urllib.request.Request(f"http://127.0.0.1:{data['port']}/health", headers={"Authorization": "Bearer " + data["token"]})
                with urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect).open(req, timeout=1) as r:
                    if json.load(r).get("fingerprint") == fingerprint:
                        return data
            except (OSError, ValueError, KeyError):
                pass
            return None
        if existing := healthy():
            return existing
        ready.unlink(missing_ok=True)
        launch = folder / "launch.json"
        private_json(launch, {"profile": profile, "token": secrets.token_urlsafe(32), "fingerprint": fingerprint})
        process = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "serve", str(launch)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(60):
            if result := healthy():
                return result
            if process.poll() is not None:
                break
            time.sleep(0.05)
        process.terminate()
        launch.unlink(missing_ok=True)
        raise ModelError("无法启动本机模型协议适配，请检查执行机端口和私有目录权限")


def apply_profile(env, profile, state):
    """Modify only this child's environment; never global process configuration."""
    if not profile:
        return
    p = validate(profile)
    env["SAPPER_MODEL"] = p["model"]
    # Claude settings.env otherwise overrides a host-selected provider/credential.
    # This documented host-routing flag leaves tools/hooks/project settings loaded.
    if p["protocol"] != "anthropic" or p["base_url"] or p.get("api_key"):
        env["CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST"] = "1"
    if p["protocol"] == "anthropic":
        if p["base_url"]:
            env["ANTHROPIC_BASE_URL"] = endpoint(p, "messages").removesuffix("/v1/messages")
        if p.get("api_key"):
            env["ANTHROPIC_AUTH_TOKEN"] = p["api_key"]
            env.pop("ANTHROPIC_API_KEY", None)
        if p.get("engine_prefix"):
            env["SAPPER_BIN"] = p["engine_prefix"]
    else:
        b = ensure_bridge(state, p)
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{b['port']}"
        env["ANTHROPIC_AUTH_TOKEN"] = b["token"]
        for key in ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY"):
            env.pop(key, None)
        env["ANTHROPIC_MODEL"] = p["model"]
        for tier in ("HAIKU", "SONNET", "OPUS"):
            env["ANTHROPIC_DEFAULT_" + tier + "_MODEL"] = p["model"]


def serve(launch):
    launch = Path(launch)
    cfg = json.loads(launch.read_text())
    launch.unlink()
    server = BridgeServer(cfg["profile"], cfg["token"], cfg["fingerprint"],
                          usage_path=launch.parent / "usage.jsonl")
    private_json(launch.parent / "ready.json", {"port": server.server_port, "token": cfg["token"], "pid": os.getpid()})
    try:
        server.serve_forever()
    finally:
        server.server_close()
        (launch.parent / "ready.json").unlink(missing_ok=True)


def bridge_operation(state, identity=None):
    """Inspect/stop only authenticated adapters belonging to this Web state."""
    root = Path(state) / "model-bridges"
    result = []
    for file in root.glob("*/ready.json"):
        if identity is not None and file.parent.name != identity:
            continue
        try:
            data = json.loads(file.read_text())
            if type(data["port"]) is not int or not 0 < data["port"] < 65536:
                continue
            base = f"http://127.0.0.1:{data['port']}"
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
            headers = {"Authorization": "Bearer " + data["token"]}
            with opener.open(urllib.request.Request(base + "/health", headers=headers), timeout=1) as r:
                health = json.load(r)
            if health.get("fingerprint") != file.parent.name:
                continue
            if identity is not None:
                with opener.open(urllib.request.Request(base + "/shutdown", data=b"{}", headers=headers), timeout=2) as r:
                    result = json.load(r)
                for _ in range(40):
                    if not file.exists():
                        break
                    time.sleep(0.05)
                return result
            result.append({"id": file.parent.name, **{k: health[k] for k in ("protocol", "model", "pid")}})
        except (OSError, ValueError, KeyError):
            continue
    if identity is not None:
        raise ModelError("协议适配服务未运行或已停止")
    return result


def usage_command(argv):
    """Print the billing ledger the bridges appended: tokens per model call."""
    state = os.environ.get("SAPPER_WEB_STATE", ".sapper-web")
    since, price_in, price_out, as_json = None, 0.0, 0.0, False
    it = iter(argv)
    for item in it:
        if item == "--state":
            state = next(it, state)
        elif item == "--since":
            value = next(it, "0")
            scale = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(value[-1:])
            since = float(value[:-1] or 0) * scale if scale else float(value)
        elif item == "--price-in":
            price_in = float(next(it, "0"))
        elif item == "--price-out":
            price_out = float(next(it, "0"))
        elif item == "--json":
            as_json = True
        else:
            raise SystemExit(f"usage: unknown option {item}")
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - since)) if since else ""
    rows, total = {}, {"requests": 0, "errors": 0, "input_tokens": 0, "output_tokens": 0, "duration_ms": 0}
    for file in sorted((Path(state) / "model-bridges").glob("*/usage.jsonl")):
        with contextlib.suppress(OSError):
            for line in file.read_text(errors="replace").splitlines():
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if cutoff and str(entry.get("at", "")) < cutoff:
                    continue
                key = (entry.get("model", "?"), entry.get("protocol", "?"))
                row = rows.setdefault(key, dict(total))
                for name in total:
                    row[name] += int(entry.get(name) or 0)
                row["errors"] += 1 if entry.get("status") == "error" else 0
                row["requests"] += 0 if entry.get("status") == "error" else 1
    for row in rows.values():
        for name in total:
            total[name] += row[name]
    if as_json:
        print(json.dumps({"models": [{"model": k[0], "protocol": k[1], **v} for k, v in rows.items()],
                          "total": total, "price_in": price_in, "price_out": price_out}, ensure_ascii=False, indent=1))
        return 0
    if not rows:
        print(f"{Path(state) / 'model-bridges'} 下没有 usage.jsonl 记录（协议适配器重启后开始记账）")
        return 0
    print(f"{'model':32s}{'protocol':18s}{'req':>6s}{'err':>5s}{'input':>13s}{'output':>11s}{'sec':>9s}")
    for (model, proto), row in sorted(rows.items(), key=lambda kv: -(kv[1]["input_tokens"] + kv[1]["output_tokens"])):
        print(f"{model[:31]:32s}{proto:18s}{row['requests']:6d}{row['errors']:5d}"
              f"{row['input_tokens']:13,d}{row['output_tokens']:11,d}{row['duration_ms'] / 1000:9.1f}")
    print(f"{'TOTAL':32s}{'':18s}{total['requests']:6d}{total['errors']:5d}"
          f"{total['input_tokens']:13,d}{total['output_tokens']:11,d}{total['duration_ms'] / 1000:9.1f}")
    if price_in or price_out:
        cost = total["input_tokens"] / 1e6 * price_in + total["output_tokens"] / 1e6 * price_out
        print(f"\ncost = ${cost:.4f}  (price_in={price_in}/M, price_out={price_out}/M)")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "serve":
        serve(sys.argv[2])
    elif sys.argv[1:2] == ["usage"]:
        raise SystemExit(usage_command(sys.argv[2:]))
    else:
        raise SystemExit("internal model bridge: serve <private-launch-file> | usage [--state DIR] [--since 30m]")
