"""最小 MCP client 冒烟测试：连 streamable-http 服务，列工具，走只读流程。

先在另一个终端启动服务：
    python -m ccnu_lib.server

再运行本脚本：
    python scripts/smoke_mcp.py

会：list_tools → get_login_status →（必要时 start_login + 输验证码）→
get_availability_distribution → get_current_reservation。全程经 MCP 协议，
验证工具在协议层可用。不下单。
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

# 让本地连接绕过系统代理（否则 HTTP_PROXY 会把 127.0.0.1 也走代理 → 502）
for _k in ("NO_PROXY", "no_proxy"):
    os.environ[_k] = (os.environ.get(_k, "") + ",127.0.0.1,localhost").strip(",")

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

URL = os.getenv("MCP_URL", "http://127.0.0.1:8010/mcp")
SHOT = Path(__file__).parent / "_probe_out" / "captcha.png"


def unwrap(result) -> dict:
    """把 CallToolResult 解成 dict。优先解析 text content 的 JSON。"""
    for c in getattr(result, "content", []) or []:
        if getattr(c, "type", None) == "text":
            try:
                return json.loads(c.text)
            except Exception:
                return {"text": c.text}
    sc = getattr(result, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc)
    return {}


async def ask(p: str) -> str:
    return (await asyncio.to_thread(input, p)).strip()


def show_captcha(res: dict) -> None:
    img = res.get("image_base64") or ""
    if img.startswith("data:"):
        SHOT.parent.mkdir(parents=True, exist_ok=True)
        SHOT.write_bytes(base64.b64decode(img.split(",", 1)[1]))
        print(f"  验证码图已存: {SHOT}")
        try:
            if sys.platform == "win32":
                os.startfile(SHOT)  # type: ignore
        except Exception:
            pass
    elif res.get("screenshot_path"):
        print("  验证码图:", res["screenshot_path"])


async def main() -> None:
    async with streamablehttp_client(URL) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"✅ 已连接，{len(names)} 个工具：")
            print("  " + ", ".join(names))

            async def call(name, args=None):
                return unwrap(await session.call_tool(name, args or {}))

            print("\n--- get_login_status ---")
            st = await call("get_login_status")
            print(" ", st)

            if not st.get("logged_in"):
                print("\n--- start_login ---")
                res = await call("start_login")
                while res.get("code") == "NEED_CHALLENGE":
                    print(f"  [{res.get('challenge_type')}] {res.get('prompt','')}")
                    show_captcha(res)
                    ans = await ask("  输入验证码/短信码: ")
                    res = await call("submit_challenge",
                                     {"challenge_id": res["challenge_id"], "answer": ans})
                print(" ", res)
                if not res.get("logged_in"):
                    print("✗ 登录未成功，停止")
                    return

            day = (date.today() + timedelta(days=1)).isoformat()
            print(f"\n--- get_availability_distribution {day} 08:00-12:00 ---")
            dist = await call("get_availability_distribution",
                              {"date": day, "start_time": "08:00", "end_time": "12:00"})
            locs = dist.get("locations", []) if isinstance(dist, dict) else []
            for l in locs[:5]:
                print(f"  {l.get('path')}  空闲 {l.get('available')}/{l.get('total')}")

            print("\n--- get_current_reservation ---")
            print(" ", await call("get_current_reservation"))

            print("\n✅ MCP 协议层冒烟通过。")


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
