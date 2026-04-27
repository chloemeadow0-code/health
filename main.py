import asyncio
import json
import os
from datetime import datetime, timedelta
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse
from supabase import create_client, Client

app = FastAPI()

# Supabase 初始化
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"]
)

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


# ── MCP 消息处理核心 ───────────────────────────────────────
async def handle_message(body: dict) -> dict:
    method = body.get("method")
    msg_id = body.get("id")

    # initialize
    if method == "initialize":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "health-mcp", "version": "1.0.0"}
            }
        }

    # tools/list
    if method == "tools/list":
        return {
            "jsonrpc": "2.0", "id": msg_id,
            "result": {"tools": TOOLS}
        }

    # tools/call
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

    return {
        "jsonrpc": "2.0", "id": msg_id,
        "error": {"code": -32601, "message": f"Unknown method: {method}"}
    }


# ── SSE 端点（rikkahub 用这个）─────────────────────────────
@app.get("/sse")
async def sse_endpoint(request: Request):
    async def event_stream() -> AsyncGenerator[str, None]:
        # 告知客户端 POST 消息的地址
        yield "event: endpoint\ndata: /messages\n\n"

        # 保持连接，定时心跳
        while True:
            if await request.is_disconnected():
                break
            await asyncio.sleep(15)
            yield ": ping\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        }
    )


# ── HTTP POST 消息端点 ─────────────────────────────────────
@app.post("/messages")
async def messages_endpoint(request: Request):
    body = await request.json()
    response = await handle_message(body)
    return JSONResponse(response)


# ── 健康检查 ───────────────────────────────────────────────
@app.get("/")
async def health():
    return {"status": "ok", "server": "health-mcp"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
