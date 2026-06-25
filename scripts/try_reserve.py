"""阶段 2 验证：默认只读（分布+座位+当前预约），加 --book 才真下单+取消。

    python scripts/try_reserve.py              # 只读
    python scripts/try_reserve.py --book       # 真预约一个明天的座位再取消（测试闭环）
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("HEADLESS", "false")

from ccnu_lib import login, reservations          # noqa: E402
from ccnu_lib.browser import manager               # noqa: E402
from ccnu_lib.config import settings               # noqa: E402
from ccnu_lib.db import Database                    # noqa: E402


async def ask(p: str) -> str:
    return (await asyncio.to_thread(input, p)).strip()


async def ensure_login(db: Database, uk: str) -> bool:
    st = await login.get_login_status(db, uk)
    if st.get("logged_in"):
        return True
    res = await login.start_login(db, uk)
    while res.get("code") == "NEED_CHALLENGE":
        if res.get("screenshot_path"):
            try:
                if sys.platform == "win32":
                    os.startfile(res["screenshot_path"])  # type: ignore
            except Exception:
                pass
            print("  验证码图:", res["screenshot_path"])
        ans = await ask(f"  [{res['challenge_type']}] 输入: ")
        res = await login.submit_challenge(db, uk, res["challenge_id"], ans)
    return bool(res.get("logged_in"))


async def main() -> None:
    book = "--book" in sys.argv
    uk = settings.default_user_key
    db = Database(settings.db_path)
    if not db.get_account(uk) and settings.default_username:
        db.upsert_account(uk, settings.default_username, settings.default_password,
                          None, str(settings.profile_dir(uk)))
    if not await ensure_login(db, uk):
        print("✗ 未登录")
        await manager.stop()
        return

    day = (date.today() + timedelta(days=1)).isoformat()
    print(f"\n=== 当前预约 ===")
    print(await reservations.get_current_reservation(db, uk))

    print(f"\n=== 区域分布 {day} 08:00-12:00（前5）===")
    dist = await reservations.get_availability_distribution(db, uk, day, "08:00", "12:00")
    locs = dist.get("locations", [])
    for l in locs[:5]:
        print(f"  {l['path']}  空闲 {l['available']}/{l['total']}  id={l['location_id']}")

    if locs:
        loc = next((l for l in locs if (l.get("available") or 0) > 0), locs[0])
        print(f"\n=== 座位 {loc['path'][-1]} ===")
        seats = await reservations.list_available_seats(
            db, uk, day, "08:00", "12:00", loc["location_id"], limit=8)
        for s in seats.get("seats", []):
            print(f"  {s['seat_no']}  id={s['seat_id']}")

        if book:
            print("\n=== 真下单（first_available）===")
            r = await reservations.reserve_seat(
                db, uk, day, "08:00", "12:00",
                location_id=loc["location_id"], strategy="first_available")
            print("预约结果:", r)
            if r.get("ok"):
                print("\n=== 取消刚才的预约 ===")
                print(await reservations.cancel_reservation(db, uk, r["reservation_id"]))

    await asyncio.sleep(1)
    await manager.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
