import asyncio
import os
import uuid
import json
from datetime import datetime, timedelta
from typing import AsyncGenerator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from supabase import create_client, Client

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Supabase ───────────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"]
)

# ── SSE Session 管理 ───────────────────────────────────────
sessions: dict[str, asyncio.Queue] = {}

# ── MCP 工具定义 ──────────────────────────────────────────
TOOLS = [
    {
        "name": "get_health_data",
        "description": "从 Supabase 读取 HC 同步的健康数据（步数、睡眠等）",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "查询最近几天的数据",
                    "default": 3
                }
            }
        }
    }
]

# ── 工具实现 ───────────────────────────────────────────────
async def get_health_data(days: int = 3) -> str:
    try:
        now_bj = datetime.utcnow() + timedelta(hours=8)
        since = (now_bj - timedelta(days=days)).isoformat()

        def _query():
            return (
                supabase.table("health_data")
                .select("*")
                .gte("recorded_at", since)
                .order("recorded_at", desc=True)
                .execute()
            )

        res = await asyncio.to_thread(_query)

        if not res or not res.data:
            return f"📊 近{days}天暂无健康数据记录。"

        lines = [f"📊 【近{days}天健康数据】:"]
        for r in res.data:
            time_str = r.get("recorded_at", "")[:16]
            data_type = r.get("data_type", "")
            value = r.get("value", "")

            if data_type == "steps":
                lines.append(f"  [{time_str}] 🏃 步数: {value}步")
            elif data_type == "sleep":
                hours = round(float(value) / 3600, 1)
                lines.append(f"  [{time_str}] 💤 睡眠: {hours}小时")
            else:
                lines.append(f"  [{time_str}] {data_type}: {value}")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询健康数据失败: {e}"


# ── MCP 消息处理 ───────────────────────────────────────────
async def handle_message(body: dict) -> dict | None:
    method = body.get("method")
    msg_id = body.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "health-mcp", "version": "1.0.0"}
            }
        }

    if method in ("notifications/initialized", "initialized"):
        return None

    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {"tools": TOOLS}
        }

    if method == "tools/call":
        tool_name = body["params"]["name"]
        arguments = body["params"].get("arguments", {})

        if tool_name == "get_health_data":
            result_text = await get_health_data(**arguments)
            return {
                "jsonrpc": "2.0", "id": msg_id,
                "result": {
                    "content": [{"type": "text", "text": result_text}]
                }
            }

        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown tool: {tool_name}"}
        }

    if msg_id is not None:
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "error": {"code": -32601, "message": f"Unknown method: {method}"}
        }

    return None


# ── Streamable HTTP 端点（新协议，rikkahub 用这个）─────────
@app.post("/mcp")
@app.post("/messages")
async def streamable_http(request: Request):
    """Streamable HTTP transport - 直接 POST，直接返回结果"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}},
            status_code=400
        )

    # 支持批量请求
    if isinstance(body, list):
        results = []
        for item in body:
            r = await handle_message(item)
            if r is not None:
                results.append(r)
        return JSONResponse(results)

    response = await handle_message(body)
    if response is None:
        return Response(status_code=202)

    # 检查客户端是否接受 SSE 流式响应
    accept = request.headers.get("accept", "")
    if "text/event-stream" in accept:
        async def stream():
            yield f"event: message\ndata: {json.dumps(response)}\n\n"
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return JSONResponse(response)


# ── SSE 端点（旧协议备用）────────────────────────────────
@app.get("/sse")
async def sse_endpoint(request: Request):
    session_id = str(uuid.uuid4())
    queue: asyncio.Queue = asyncio.Queue()
    sessions[session_id] = queue
    base_url = str(request.base_url).rstrip("/")

    async def event_stream() -> AsyncGenerator[str, None]:
        yield f"event: endpoint\ndata: {base_url}/messages?sessionId={session_id}\n\n"
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"event: message\ndata: {json.dumps(message)}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            sessions.pop(session_id, None)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"}
    )

@app.post("/messages")
async def sse_messages(request: Request):
    session_id = request.query_params.get("sessionId")
    body = await request.json()
    response = await handle_message(body)
    if session_id and session_id in sessions:
        if response is not None:
            await sessions[session_id].put(response)
        return Response(status_code=202)
    if response is not None:
        return JSONResponse(response)
    return Response(status_code=202)


# ── 健康检查 ───────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "server": "health-mcp", "sessions": len(sessions)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
