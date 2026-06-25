"""阶段 0 探测脚本 —— 在校园网机器上有界面运行，摸清登录页真实结构。

用途：
1. 打开真实空间预约 SPA，停下来等你手动完成登录（含验证码/短信）。
2. 每隔几秒把当前页面的 URL、可见 input、button、img 列表打印出来，
   据此回填 src/ccnu_lib/selectors.py。
3. 验证 persistent profile 能否保活登录态：第二次运行若直接进主页即说明保活成功。

运行：
    pip install playwright python-dotenv
    python -m playwright install chromium
    python scripts/probe.py

输出落在 scripts/_probe_out/ 。带界面（headful），方便你手动操作。
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from playwright.async_api import Page, async_playwright

load_dotenv()

BASE_URL = os.getenv("CCNU_BASE_URL", "https://kjyy.ccnu.edu.cn/jsq-v/#/main/home")
OUT = Path(__file__).parent / "_probe_out"
PROFILE = OUT / "profile"


async def dump(page: Page, tag: str) -> dict:
    """抓取当前页面关键可交互元素。"""
    info = {"tag": tag, "time": datetime.now().isoformat(), "url": page.url}
    try:
        info["inputs"] = await page.eval_on_selector_all(
            "input",
            "els => els.map(e => ({name:e.name, id:e.id, type:e.type,"
            " placeholder:e.placeholder, visible: e.offsetParent !== null}))",
        )
        info["buttons"] = await page.eval_on_selector_all(
            "button, input[type=submit], a[role=button]",
            "els => els.map(e => ({text:(e.innerText||e.value||'').trim(),"
            " id:e.id, type:e.type}))",
        )
        info["images"] = await page.eval_on_selector_all(
            "img",
            "els => els.map(e => ({src:e.src, id:e.id, cls:e.className,"
            " w:e.naturalWidth, h:e.naturalHeight}))",
        )
    except Exception as e:
        info["error"] = str(e)
    return info


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    PROFILE.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE),
            headless=False,
            viewport={"width": 1280, "height": 900},
            locale="zh-CN",
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
        print(f"[probe] 打开 {BASE_URL}")
        await page.goto(BASE_URL, wait_until="domcontentloaded")

        print("[probe] 请在浏览器里手动完成登录（账号/密码/验证码/短信）。")
        print("[probe] 每 5 秒抓一次页面结构。完成后回到这里按 Ctrl+C 结束。")

        i = 0
        try:
            while True:
                rec = await dump(page, f"t{i}")
                records.append(rec)
                shot = OUT / f"step_{i:02d}.png"
                try:
                    await page.screenshot(path=str(shot))
                except Exception:
                    pass
                vis_inputs = [x for x in rec.get("inputs", []) if x.get("visible")]
                print(f"\n=== step {i}  url={rec['url']}")
                print(f"  可见 inputs: {json.dumps(vis_inputs, ensure_ascii=False)}")
                print(f"  buttons   : {json.dumps(rec.get('buttons', []), ensure_ascii=False)}")
                imgs = [x for x in rec.get('images', []) if x.get('w', 0) < 300]
                if imgs:
                    print(f"  小图(疑似验证码): {json.dumps(imgs, ensure_ascii=False)}")
                i += 1
                await asyncio.sleep(5)
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            (OUT / "records.json").write_text(
                json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"\n[probe] 记录已写入 {OUT/'records.json'}，截图在 {OUT}")
            await ctx.close()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
