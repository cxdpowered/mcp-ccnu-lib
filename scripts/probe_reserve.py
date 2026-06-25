"""阶段 2 接口探测：抓预约流程的后端 API（XHR/fetch）。

复用已登录的 profile（先跑过 try_login.py 登录成功）。headful 打开预约首页，
你手动点：选馆/楼层/区域 → 看座位 → （可选）点预约/取消。脚本把所有
API 请求与响应记录到 scripts/_probe_out/api_calls.json，据此还原接口。

运行：
    python scripts/probe_reserve.py
点完流程后回到终端按 Ctrl+C 结束。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("HEADLESS", "false")

from ccnu_lib.config import settings          # noqa: E402
from playwright.async_api import async_playwright  # noqa: E402

OUT = Path(__file__).parent / "_probe_out"
RECORDS: list[dict] = []

# 只关心这些域/路径的接口，过滤掉静态资源
INTEREST = ("kjyy.ccnu.edu.cn", "/api", "/rem", "jsq")
SKIP_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".woff",
            ".woff2", ".ttf", ".ico", ".map")


def _interesting(url: str, rtype: str) -> bool:
    if rtype not in ("xhr", "fetch"):
        return False
    if any(url.lower().endswith(e) for e in SKIP_EXT):
        return False
    return any(k in url for k in INTEREST)


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    profile = settings.profile_dir(settings.default_user_key)
    cookies_file = settings.user_dir(settings.default_user_key) / "cookies.json"

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        if cookies_file.exists():
            try:
                await ctx.add_cookies(json.loads(cookies_file.read_text(encoding="utf-8")))
            except Exception:
                pass

        async def on_response(resp) -> None:
            req = resp.request
            if not _interesting(req.url, req.resource_type):
                return
            rec: dict = {
                "time": datetime.now().strftime("%H:%M:%S"),
                "method": req.method,
                "url": req.url,
                "status": resp.status,
                "post_data": req.post_data,
            }
            try:
                body = await resp.text()
                rec["response"] = body[:4000]
            except Exception as e:
                rec["response_error"] = str(e)
            RECORDS.append(rec)
            star = "★ " if ("/make/" in req.url or "/user/" in req.url) else "  "
            print(f"{star}[{rec['method']}] {resp.status} {req.url}")
            if req.post_data:
                print(f"       payload: {req.post_data[:300]}")

        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        page.on("response", lambda r: asyncio.create_task(on_response(r)))

        print(f"[probe] 打开 {settings.base_url}")
        await page.goto(settings.base_url, wait_until="domcontentloaded")
        print("[probe] 请在浏览器手动点：选馆/楼层/区域 → 看座位 → 可选预约/取消。")
        print("[probe] 每个动作的 API 会实时打印。完成后按 Ctrl+C 结束。\n")

        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            out = OUT / "api_calls.json"
            out.write_text(json.dumps(RECORDS, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            print(f"\n[probe] 共抓到 {len(RECORDS)} 条接口，写入 {out}")
            await ctx.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
