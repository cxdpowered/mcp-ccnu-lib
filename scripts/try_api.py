"""验证直连 API 鉴权是否可用（cookie / token 头）。

复用已登录 profile。先确保登录态，再依次调 getUserInfo / buildingFloorDate /
findRoomDuration / freeSeats，打印结果。全部 200 即说明可直接走 API。

运行：
    python scripts/try_api.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("HEADLESS", "false")

from ccnu_lib import api, login            # noqa: E402
from ccnu_lib.browser import manager       # noqa: E402
from ccnu_lib.config import settings       # noqa: E402
from ccnu_lib.db import Database           # noqa: E402


async def ask(p: str) -> str:
    return (await asyncio.to_thread(input, p)).strip()


async def ensure_login(db: Database, uk: str):
    st = await login.get_login_status(db, uk)
    if st.get("logged_in"):
        return True
    res = await login.start_login(db, uk)
    while res.get("code") == "NEED_CHALLENGE":
        print(f"[challenge] {res['challenge_type']} {res.get('prompt','')}")
        if res.get("screenshot_path"):
            print("  验证码图:", res["screenshot_path"])
            try:
                if sys.platform == "win32":
                    os.startfile(res["screenshot_path"])  # type: ignore
            except Exception:
                pass
        ans = await ask("  输入验证码: ")
        res = await login.submit_challenge(db, uk, res["challenge_id"], ans)
    return bool(res.get("logged_in"))


async def main() -> None:
    uk = settings.default_user_key
    db = Database(settings.db_path)
    if not db.get_account(uk) and settings.default_username:
        db.upsert_account(uk, settings.default_username, settings.default_password,
                          None, str(settings.profile_dir(uk)))

    if not await ensure_login(db, uk):
        print("✗ 未登录，先把登录跑通")
        await manager.stop()
        return
    s = manager.peek(uk)

    print("\n--- getUserInfo ---")
    try:
        u = await api.get_user_info(s)
        print(f"✅ {u.get('fullName')} / {u.get('flagName')} / 违约 {u.get('breachNum')} "
              f"/ 单次上限 {u.get('maxMinute')}分 / 步长 {u.get('stepMinute')}分")
    except Exception as e:
        print("✗ getUserInfo 失败:", e)
        await manager.stop()
        return

    print("\n--- buildingFloorDate ---")
    bfd = await api.building_floor_date(s)
    builds = bfd.get("buildings", [])
    for b in builds:
        print(f"  楼栋 {b['name']} ({b['id']}) 楼层数={len(b.get('floors', []))}")

    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if builds:
        b0 = builds[0]
        print(f"\n--- findRoomDuration {b0['name']} {tomorrow} 08:00起 ---")
        rd = await api.find_room_duration(s, b0["id"], tomorrow, api.hm_to_min("08:00"))
        rooms = rd.get("pageList", [])
        print(f"  区域数={rd.get('totalCount')}，前几个：")
        for r in rooms[:5]:
            print(f"    {r['name']} ({r['id']})  空闲 {r['seatFree']}/{r['seatTotal']}")
        if rooms:
            r0 = next((r for r in rooms if r["seatFree"] > 0), rooms[0])
            print(f"\n--- freeSeats {r0['name']} 08:00-12:00 ---")
            fs = await api.free_seats(s, r0["id"], tomorrow,
                                      api.hm_to_min("08:00"), api.hm_to_min("12:00"))
            items = list(fs.values()) if isinstance(fs, dict) else fs
            print(f"  空闲座位 {len(items)} 个，示例：",
                  [i["label"] for i in items[:8]])

    print("\n✅ API 直连可用，可实现阶段 2 工具。")
    await asyncio.sleep(1)
    await manager.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
