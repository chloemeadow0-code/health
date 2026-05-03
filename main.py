import os
import asyncio
import uvicorn
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse
from supabase import create_client, Client

# ── Supabase ───────────────────────────────────────────────
supabase: Client = create_client(
    os.environ["SUPABASE_URL"],
    os.environ["SUPABASE_KEY"]
)

RETAIN_DAYS = 2

# ── 自动清理：每次查询前删除超过 RETAIN_DAYS 天的数据 ──
def _cleanup():
    try:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=RETAIN_DAYS)).isoformat()
        supabase.table("health_data").lt("recorded_at", cutoff).delete().execute()
    except:
        pass

# ── FastMCP ────────────────────────────────────────────────
mcp = FastMCP("HealthNode")

@mcp.tool()
async def get_health_data(days: int = 3) -> str:
    """
    【健康数据查询】从 Supabase 读取 HC 同步的健康数据（步数、睡眠等）。
    可指定查询最近几天，默认 3 天。
    """
    try:
        await asyncio.to_thread(_cleanup)

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

        # 统计心率/血氧
        hr_list, spo2_list = [], []
        for r in res.data:
            dt = r.get("data_type", "")
            v = r.get("value", "")
            if dt == "heart_rate":
                try: hr_list.append(int(v))
                except: pass
            elif dt == "blood_oxygen":
                try: spo2_list.append(int(v))
                except: pass

        lines = [f"📊 【近{days}天健康数据报告】:"]
        for r in res.data:
            time_str = r.get("recorded_at", "")[:16]
            data_type = r.get("data_type", "")
            value = r.get("value", "")

            if data_type == "steps":
                lines.append(f"  [{time_str}] 🏃 步数: {value}步")
            elif data_type == "sleep":
                hours = round(float(value) / 3600, 1)
                lines.append(f"  [{time_str}] 💤 睡眠: {hours}小时")
            elif data_type == "resting_heart_rate":
                lines.append(f"  [{time_str}] 💓 静息心率: {value}bpm")
            elif data_type in ("heart_rate", "blood_oxygen"):
                continue
            else:
                lines.append(f"  [{time_str}] {data_type}: {value}")

        if hr_list:
            lines.append(f"  🫀 心率: 平均{sum(hr_list)//len(hr_list)}bpm | 最高{max(hr_list)}bpm | 最低{min(hr_list)}bpm (共{len(hr_list)}条)")
        if spo2_list:
            lines.append(f"  🩸 血氧: 平均{sum(spo2_list)//len(spo2_list)}% | 最高{max(spo2_list)}% | 最低{min(spo2_list)}% (共{len(spo2_list)}条)")

        return "\n".join(lines)
    except Exception as e:
        return f"❌ 查询健康数据失败: {e}"


# ── SSE Transport + Starlette ──────────────────────────────
sse = SseServerTransport("/messages")

async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await mcp._mcp_server.run(
            streams[0], streams[1],
            mcp._mcp_server.create_initialization_options()
        )

async def handle_messages(request: Request):
    await sse.handle_post_message(request.scope, request.receive, request._send)

async def health(request: Request):
    return JSONResponse({"status": "ok", "server": "health-mcp"})

app = Starlette(
    routes=[
        Route("/",        health),
        Route("/sse",     handle_sse),
        Route("/messages", handle_messages, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
