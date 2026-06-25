"""challenge（验证码 / 短信）创建与回传。截图转 base64 给调用方展示。"""
from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timedelta, timezone

from playwright.async_api import Page

from .config import settings
from .db import Database

_TZ = timezone(timedelta(hours=8))


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, _TZ).isoformat()


async def create_challenge(
    db: Database,
    page: Page,
    user_key: str,
    ctype: str,
    prompt: str,
    *,
    image_selector: str | None = None,
    phone_hint: str | None = None,
) -> dict:
    """生成一条 pending challenge 并返回 NEED_CHALLENGE 结构。

    ctype: captcha | sms | confirm_send_sms | manual_login
    """
    challenge_id = "ch_" + uuid.uuid4().hex[:12]
    expires_at = int(time.time()) + settings.challenge_ttl_seconds
    screenshot_path = None
    image_base64 = None

    if ctype == "captcha":
        shot_dir = settings.screenshots_dir(user_key)
        shot_dir.mkdir(parents=True, exist_ok=True)
        screenshot_path = str(shot_dir / f"{challenge_id}.png")
        try:
            target = page.locator(image_selector).first if image_selector else None
            if target and await target.count() > 0:
                png = await target.screenshot(path=screenshot_path)
            else:  # 退化为整页截图，至少让用户看得到
                png = await page.screenshot(path=screenshot_path)
            image_base64 = "data:image/png;base64," + base64.b64encode(png).decode()
        except Exception:
            png = await page.screenshot(path=screenshot_path)
            image_base64 = "data:image/png;base64," + base64.b64encode(png).decode()

    db.create_challenge(challenge_id, user_key, ctype, prompt, screenshot_path, expires_at)

    result = {
        "ok": False,
        "code": "NEED_CHALLENGE",
        "challenge_id": challenge_id,
        "challenge_type": ctype,
        "prompt": prompt,
        "expires_at": _iso(expires_at),
    }
    if image_base64:
        result["image_base64"] = image_base64
        result["screenshot_path"] = screenshot_path
    if phone_hint:
        result["phone_hint"] = phone_hint
    return result
