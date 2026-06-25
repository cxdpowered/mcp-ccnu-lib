"""抓一次真实 frontApi 请求的请求头 + 打印 session/localStorage，定位 token 怎么传。

复用已登录 profile。打开首页会自动触发 getUserInfo 等接口，捕获其请求头。
自动运行 ~8 秒后退出。

    python scripts/probe_headers.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ccnu_lib.config import settings              # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

SEEN: set[str] = set()


async def main() -> None:
    profile = settings.profile_dir(settings.default_user_key)
    cookies_file = settings.user_dir(settings.default_user_key) / "cookies.json"

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), headless=False,
            viewport={"width": 1280, "height": 900}, locale="zh-CN",
        )
        if cookies_file.exists():
            try:
                await ctx.add_cookies(json.loads(cookies_file.read_text(encoding="utf-8")))
            except Exception:
                pass
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        async def log_req(req) -> None:
            if "frontApi" not in req.url or req.url in SEEN:
                return
            SEEN.add(req.url)
            try:
                h = await req.all_headers()
            except Exception:
                return
            print(f"\n[frontApi] {req.method} {req.url}")
            for k, v in h.items():
                if k.lower() == "cookie":
                    print(f"    {k}: {v[:60]}...(len={len(v)})")
                else:
                    print(f"    {k}: {v}")

        page.on("request", lambda r: asyncio.create_task(log_req(r)))

        print(f"[probe] 打开 {settings.base_url}")
        await page.goto(settings.base_url, wait_until="domcontentloaded")
        await asyncio.sleep(8)

        print("\n=== sessionStorage ===")
        try:
            ss = await page.evaluate("() => Object.fromEntries(Object.entries(sessionStorage))")
            for k, v in ss.items():
                print(f"  {k} = {str(v)[:120]}")
        except Exception as e:
            print("  读取失败:", e)
        print("\n=== localStorage ===")
        try:
            ls = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
            for k, v in ls.items():
                print(f"  {k} = {str(v)[:120]}")
        except Exception as e:
            print("  读取失败:", e)

        await ctx.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
