"""
MiMo Reasoning Content Proxy v1.3
==================================
v1.3: 当缓存未命中时，剥离 assistant 消息的 tool_calls（降级为纯文本），
     避免 400 错误。MiMo 只对有 tool_calls 的 assistant 消息要求 reasoning_content。
"""

import hashlib
import json
import logging
import time
from collections import OrderedDict
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

# ─── 配置 ──────────────────────────────────────────────────────
MIMO_API_BASE = "https://token-plan-cn.xiaomimimo.com/v1"
LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 8899
CACHE_MAX_SIZE = 2000
CACHE_TTL = 7200

log = logging.getLogger("mimo-proxy")

# ─── 缓存 ──────────────────────────────────────────────────────
_cache: OrderedDict[str, tuple[str, float]] = OrderedDict()
_tool_call_index: dict[str, str] = {}
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=30),
            follow_redirects=True,
        )
    return _http_client


def _msg_hash(msg: dict) -> str:
    content = msg.get("content") or ""
    tool_calls = json.dumps(msg.get("tool_calls") or [], sort_keys=True, ensure_ascii=False)
    raw = f"{content}||{tool_calls}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _extract_tool_call_ids(msg: dict) -> list[str]:
    return [tc.get("id", "") for tc in msg.get("tool_calls") or [] if tc.get("id")]


def _cache_get(key: str) -> str | None:
    if key in _cache:
        val, ts = _cache[key]
        if time.time() - ts < CACHE_TTL:
            _cache.move_to_end(key)
            return val
        del _cache[key]
    return None


def _cache_set(key: str, value: str):
    if key in _cache:
        del _cache[key]
    _cache[key] = (value, time.time())
    while len(_cache) > CACHE_MAX_SIZE:
        _cache.popitem(last=False)


def _cache_set_with_index(key: str, value: str, tool_call_ids: list[str]):
    _cache_set(key, value)
    for tid in tool_call_ids:
        _tool_call_index[tid] = value


def _find_by_tool_call_ids(msg: dict) -> str | None:
    for tid in _extract_tool_call_ids(msg):
        if tid in _tool_call_index:
            return _tool_call_index[tid]
    return None


# ─── 核心逻辑 ──────────────────────────────────────────────────

def inject_reasoning(messages: list[dict]) -> tuple[int, int]:
    """
    处理 assistant 消息：
    1. 有缓存 → 注入 reasoning_content
    2. 无缓存 → 剥离 tool_calls（降级为纯文本，避免 400）
    
    返回 (注入数, 降级数)
    """
    injected = 0
    degraded = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        if not msg.get("tool_calls"):
            continue
        if msg.get("reasoning_content"):
            continue

        # 尝试查找缓存
        h = _msg_hash(msg)
        cached = _cache_get(h)
        if not cached:
            cached = _find_by_tool_call_ids(msg)

        if cached:
            # ✅ 有缓存，注入
            msg["reasoning_content"] = cached
            injected += 1
            log.info("✅ Injected reasoning_content into msg[%d] [%s] (%d chars)", i, h[:8], len(cached))
        else:
            # ⚠️ 无缓存，降级：剥离 tool_calls，避免 400
            tc_ids = _extract_tool_call_ids(msg)
            log.warning("⚠️  No cache for msg[%d] [%s] tool_call_ids=%s → degrading to plain text",
                        i, h[:8], tc_ids)

            # 将 tool_calls 信息转为文本摘要附加到 content 中
            original_content = msg.get("content") or ""
            tc_summary = []
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function", {})
                tc_summary.append(f"[Called {fn.get('name', '?')}]")

            if tc_summary:
                msg["content"] = original_content + " " + " ".join(tc_summary)

            # 移除 tool_calls（这样 MiMo 就不会要求 reasoning_content）
            del msg["tool_calls"]
            degraded += 1

    return injected, degraded


def cache_reasoning_from_message(msg: dict):
    rc = msg.get("reasoning_content")
    if rc and msg.get("tool_calls"):
        h = _msg_hash(msg)
        tc_ids = _extract_tool_call_ids(msg)
        _cache_set_with_index(h, rc, tc_ids)
        log.info("📦 Cached reasoning [%s] (%d chars) tc_ids=%s", h[:8], len(rc), tc_ids)


# ─── SSE 流式处理 ──────────────────────────────────────────────

def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode("utf-8")


async def _stream_proxy(client: httpx.AsyncClient, url: str, headers: dict, body: dict):
    acc_content = ""
    acc_reasoning = ""
    acc_tool_calls: list[dict] = []

    try:
        async with client.stream("POST", url, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                error_body = await resp.aread()
                yield _sse(error_body.decode("utf-8", errors="replace"))
                return

            buffer = ""
            async for raw_chunk in resp.aiter_bytes():
                buffer += raw_chunk.decode("utf-8", errors="replace")

                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.rstrip("\r")

                    if line.startswith("data: "):
                        payload = line[6:].strip()

                        if payload == "[DONE]":
                            if acc_reasoning and (acc_content or acc_tool_calls):
                                synthetic = {
                                    "role": "assistant",
                                    "content": acc_content,
                                    "tool_calls": acc_tool_calls,
                                    "reasoning_content": acc_reasoning,
                                }
                                h = _msg_hash(synthetic)
                                tc_ids = _extract_tool_call_ids(synthetic)
                                _cache_set_with_index(h, acc_reasoning, tc_ids)
                                log.info("📦 Cached streaming reasoning [%s] (%d chars)", h[:8], len(acc_reasoning))
                            yield _sse("[DONE]")
                            continue

                        try:
                            chunk = json.loads(payload)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            rc = delta.get("reasoning_content")
                            if rc:
                                acc_reasoning += rc
                            c = delta.get("content")
                            if c:
                                acc_content += c
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                while len(acc_tool_calls) <= idx:
                                    acc_tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                if tc.get("id"):
                                    acc_tool_calls[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    acc_tool_calls[idx]["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    acc_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                        except (json.JSONDecodeError, IndexError, KeyError):
                            pass

                        yield _sse(payload)

                    elif line.strip() == "":
                        yield b"\n"
                    elif line.startswith(":"):
                        yield (line + "\n\n").encode("utf-8")
                    else:
                        yield (line + "\n").encode("utf-8")

    except Exception as e:
        log.error("❌ Stream error: %s", e, exc_info=True)
        yield _sse(json.dumps({"error": f"Proxy error: {e}"}))


# ─── HTTP 端点 ─────────────────────────────────────────────────

async def chat_completions(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    messages = body.get("messages", [])
    injected, degraded = inject_reasoning(messages)
    if injected or degraded:
        log.info("🔧 Injected=%d, Degraded=%d", injected, degraded)

    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth

    is_stream = body.get("stream", False)
    upstream = f"{MIMO_API_BASE}/chat/completions"
    client = _get_client()

    if is_stream:
        return StreamingResponse(
            _stream_proxy(client, upstream, headers, body),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )
    else:
        try:
            resp = await client.post(upstream, headers=headers, json=body)
            data = resp.json()
            if resp.status_code == 200:
                for choice in data.get("choices", []):
                    cache_reasoning_from_message(choice.get("message", {}))
            return JSONResponse(content=data, status_code=resp.status_code)
        except Exception as e:
            log.error("❌ Error: %s", e, exc_info=True)
            return JSONResponse({"error": str(e)}, status_code=500)


async def list_models(request: Request):
    headers = {}
    auth = request.headers.get("authorization")
    if auth:
        headers["authorization"] = auth
    client = _get_client()
    try:
        resp = await client.get(f"{MIMO_API_BASE}/models", headers=headers)
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=502)


async def root(request: Request):
    return JSONResponse({
        "status": "running",
        "service": "MiMo Reasoning Content Proxy v1.3",
        "cache_size": len(_cache),
        "tool_call_index_size": len(_tool_call_index),
        "upstream": MIMO_API_BASE,
    })


async def health(request: Request):
    return JSONResponse({"ok": True})


@asynccontextmanager
async def lifespan(app):
    global _http_client
    _http_client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=30), follow_redirects=True)
    log.info("🚀 httpx client initialized")
    yield
    if _http_client:
        await _http_client.aclose()


routes = [
    Route("/", root),
    Route("/health", health),
    Route("/v1/models", list_models),
    Route("/models", list_models),
    Route("/v1/chat/completions", chat_completions, methods=["POST"]),
    Route("/chat/completions", chat_completions, methods=["POST"]),
]

app = Starlette(routes=routes, lifespan=lifespan)

if __name__ == "__main__":
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s", datefmt="%H:%M:%S")
    log.info("🚀 MiMo Proxy v1.3 on %s:%d → %s", LISTEN_HOST, LISTEN_PORT, MIMO_API_BASE)
    uvicorn.run(app, host=LISTEN_HOST, port=LISTEN_PORT, log_level="info")
