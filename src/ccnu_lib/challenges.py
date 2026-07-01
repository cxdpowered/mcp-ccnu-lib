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


# 在页面内把 <img> 画到 canvas 取 PNG dataURL。验证码图与 CAS 登录页同源
# (account.ccnu.edu.cn)，canvas 不会被跨域污染。比 Playwright 的 loc.screenshot()
# 稳得多——后者要等元素稳定/动画帧，headless 下会卡满 30s 超时退化整页。
_CANVAS_GRAB = """
async (sel) => {
  const img = document.querySelector(sel);
  if (!img) return {err: 'not_found'};
  if (!(img.complete && img.naturalWidth > 0)) {
    await new Promise(r => { img.onload = img.onerror = () => r();
                             setTimeout(r, 3000); });
  }
  if (!img.naturalWidth) return {err: 'not_loaded'};
  try {
    const c = document.createElement('canvas');
    c.width = img.naturalWidth; c.height = img.naturalHeight;
    c.getContext('2d').drawImage(img, 0, 0);
    return {dataUrl: c.toDataURL('image/png')};
  } catch (e) { return {err: String(e)}; }
}
"""


async def _shot_captcha(page: Page, image_selector: str | None, path: str) -> bytes:
    """只取验证码图片本身（不是整页）。

    先在页面内用 canvas 抓 <img> 像素（最稳）；失败再退化到短超时的元素截图；
    再不行才整页，并打印告警便于校正 selector。
    """
    if image_selector:
        # 1) canvas 抓像素（同源，最稳）
        try:
            loc = page.locator(image_selector).first
            await loc.wait_for(state="visible", timeout=5000)
            r = await page.evaluate(_CANVAS_GRAB, image_selector)
            if isinstance(r, dict) and r.get("dataUrl"):
                png = base64.b64decode(r["dataUrl"].split(",", 1)[1])
                with open(path, "wb") as f:
                    f.write(png)
                return png
            print(f"[challenge] canvas 抓验证码失败({image_selector})：{r}")
        except Exception as e:
            print(f"[challenge] canvas 抓验证码异常({image_selector})：{e}")
        # 2) 元素截图，短超时避免卡死
        try:
            return await page.locator(image_selector).first.screenshot(
                path=path, timeout=5000)
        except Exception as e:
            print(f"[challenge] 验证码元素截图失败({image_selector})，退化整页：{e}")
    return await page.screenshot(path=path)


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
        png = await _shot_captcha(page, image_selector, screenshot_path)
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
