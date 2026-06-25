"""把预约 SPA 的所有 JS 下载到本地，供离线 grep 签名算法。

先确保登录（否则只会落到 CAS 登录页，加载不到 Vue bundle），
再收集并下载 kjyy 同源的 .js 到 scripts/_probe_out/js/。

    python scripts/dump_signing.py
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ccnu_lib import login                         # noqa: E402
from ccnu_lib.browser import manager               # noqa: E402
from ccnu_lib.config import settings               # noqa: E402
from ccnu_lib.db import Database                    # noqa: E402

OUT = Path(__file__).parent / "_probe_out" / "js"


async def ask(p: str) -> str:
    return (await asyncio.to_thread(input, p)).strip()


async def ensure_login(db: Database, uk: str) -> bool:
    st = await login.get_login_status(db, uk)
    if st.get("logged_in"):
        return True
    res = await login.start_login(db, uk)
    while res.get("code") == "NEED_CHALLENGE":
        print(f"[challenge] {res['challenge_type']} {res.get('prompt','')}")
        if res.get("screenshot_path"):
            try:
                if sys.platform == "win32":
                    os.startfile(res["screenshot_path"])  # type: ignore
            except Exception:
                pass
            print("  验证码图:", res["screenshot_path"])
        ans = await ask("  输入验证码: ")
        res = await login.submit_challenge(db, uk, res["challenge_id"], ans)
    return bool(res.get("logged_in"))


async def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    uk = settings.default_user_key
    db = Database(settings.db_path)
    if not db.get_account(uk) and settings.default_username:
        db.upsert_account(uk, settings.default_username, settings.default_password,
                          None, str(settings.profile_dir(uk)))

    if not await ensure_login(db, uk):
        print("✗ 未登录")
        await manager.stop()
        return

    s = manager.peek(uk)
    page = s.page
    await page.goto(settings.base_url, wait_until="domcontentloaded")
    await asyncio.sleep(5)

    urls = await page.evaluate(
        """() => {
            const out = new Set();
            document.querySelectorAll('script[src]').forEach(s=>out.add(s.src));
            document.querySelectorAll('link[href]').forEach(l=>{
                if (l.href.endsWith('.js')) out.add(l.href);
            });
            performance.getEntriesByType('resource').forEach(e=>{
                if (e.name.endsWith('.js')) out.add(e.name);
            });
            return [...out].filter(u => u.includes('kjyy.ccnu.edu.cn'));
        }"""
    )
    print(f"[dump] kjyy 同源 JS {len(urls)} 个，逐个下载...")

    manifest = []
    for i, u in enumerate(urls):
        try:
            text = await page.evaluate(
                "async (u) => { const r = await fetch(u); return await r.text(); }", u
            )
        except Exception as e:
            print(f"  [{i}] 失败 {u} -> {e}")
            continue
        name = re.sub(r"[^A-Za-z0-9._-]", "_", u.split("/")[-1].split("?")[0].split(";")[0]) or f"chunk{i}"
        fp = OUT / f"{i:02d}_{name}"
        fp.write_text(text, encoding="utf-8")
        manifest.append((fp.name, len(text), u))
        print(f"  [{i}] {len(text):>8} bytes  {fp.name}")

    (OUT / "_manifest.txt").write_text(
        "\n".join(f"{n}\t{ln}\t{u}" for n, ln, u in manifest), encoding="utf-8"
    )
    print(f"\n[dump] 全部存到 {OUT}")
    await manager.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
