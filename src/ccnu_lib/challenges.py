"""challenge（验证码 / 短信）创建与回传。验证码图 base64 给调用方展示。

纯 HTTP：验证码图片是 httpx 直接 GET 到的 jpg 字节，不再用浏览器截图。
"""
from __future__ import annotations

import base64
import time
import uuid
from datetime import datetime, timedelta, timezone

from .config import settings
from .db import Database

_TZ = timezone(timedelta(hours=8))


def _iso(ts: int) -> str:
    return datetime.fromtimestamp(ts, _TZ).isoformat()


def create_challenge(
    db: Database,
    user_key: str,
    ctype: str,
    prompt: str,
    *,
    image_bytes: bytes | None = None,
    image_mime: str = "image/jpeg",
    phone_hint: str | None = None,
) -> dict:
    """生成一条 pending challenge 并返回 NEED_CHALLENGE 结构。

    ctype: captcha | sms | confirm_send_sms | manual_login
    """
    challenge_id = "ch_" + uuid.uuid4().hex[:12]
    expires_at = int(time.time()) + settings.challenge_ttl_seconds
    screenshot_path = None
    image_base64 = None

    if image_bytes:
        shot_dir = settings.screenshots_dir(user_key)
        shot_dir.mkdir(parents=True, exist_ok=True)
        ext = "png" if image_mime == "image/png" else "jpg"
        screenshot_path = str(shot_dir / f"{challenge_id}.{ext}")
        with open(screenshot_path, "wb") as f:
            f.write(image_bytes)
        image_base64 = f"data:{image_mime};base64," + base64.b64encode(image_bytes).decode()

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
