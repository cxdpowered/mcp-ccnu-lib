"""登录状态机：竞速等待 + 挂起式 challenge。

跨 MCP 调用的"会话独占"用 session.pending 标志实现：
有 pending challenge 时，新动作直接拒绝，必须先 submit_challenge 或等其过期。
"""
from __future__ import annotations

import asyncio

from playwright.async_api import Page

from . import selectors as S
from .browser import Session, manager
from .challenges import create_challenge
from .config import settings
from .db import Database


# ---------- 页面判定 ----------
async def _visible(page: Page, selector: str) -> bool:
    try:
        loc = page.locator(selector).first
        return await loc.count() > 0 and await loc.is_visible()
    except Exception:
        return False


async def _has_token(page: Page) -> bool:
    """SPA 把会话 token 存进 sessionStorage.jsq_p-token，这才是 API 可用的真凭证。"""
    try:
        tok = await page.evaluate("() => sessionStorage.getItem('jsq_p-token')")
        return bool(tok)
    except Exception:
        return False


async def is_logged_in(page: Page) -> bool:
    url = page.url or ""
    # 在 CAS 登录页（account.ccnu.edu.cn / /cas/login）一定是未登录
    if any(m in url for m in S.LOGIN_URL_MARKERS):
        return False
    if await _visible(page, S.LOGIN_PAGE_MARKERS):
        return False
    # 回到预约域（kjyy）且 SPA 已取得会话 token → 真正可用
    if S.LOGGED_IN_HOST in url:
        return await _has_token(page)
    return False


async def _wait_settled(page: Page, seconds: float = 20.0) -> str:
    """等 CAS 重定向/SSO 链稳定，返回 'logged_in' 或 'login_form'。

    关键：不能一看到 kjyy 域就判已登录——未登录时会先闪一下 kjyy 再跳回 CAS。
    要求 kjyy 域 URL 稳定约 1s 且无密码框，才算真登录。
    """
    for _ in range(int(seconds / 0.5)):
        if await _visible(page, S.PASSWORD_INPUT):
            return "login_form"  # 渲染出密码框 = 确定要登录
        if S.LOGGED_IN_HOST in (page.url or "") and await _has_token(page):
            return "logged_in"   # 回到 kjyy 且 SPA 已存好 token
        await asyncio.sleep(0.5)
    if await _visible(page, S.PASSWORD_INPUT):
        return "login_form"
    return "timeout"


async def _error_text(page: Page) -> str | None:
    try:
        body = await page.inner_text("body")
    except Exception:
        return None
    for hint in S.ERROR_HINTS:
        if hint in body:
            return hint
    return None


# ---------- 提交后竞速判定 ----------
async def _evaluate_after_submit(db: Database, sess: Session) -> dict:
    """点击登录后，竞速等待：成功 / 短信页 / 错误，最多约 12s。"""
    page = sess.page
    for _ in range(24):  # 24 * 0.5s
        if await is_logged_in(page):
            return await _mark_success(db, sess)
        if await _visible(page, S.SMS_INPUT):
            return await _make_sms_challenge(db, sess)
        if await _visible(page, S.SMS_SEND_BUTTON) and not await _visible(page, S.SMS_INPUT):
            return await _make_confirm_send_sms(db, sess)
        err = await _error_text(page)
        if err:
            sess.pending = None  # 失败：清挂起，允许重新 start_login
            db.update_session(sess.user_key, status="login_failed", error=err)
            return {"ok": False, "code": "LOGIN_FAILED",
                    "message": f"登录失败：{err}（验证码已刷新，请重新 start_login）"}
        await asyncio.sleep(0.5)
    # 既没成功也没明确失败：交人工兜底
    res = await create_challenge(
        db, page, sess.user_key, "manual_login",
        "自动登录未能确认结果，请在浏览器中手动完成登录后再调用 get_login_status",
    )
    sess.pending = {"challenge_id": res["challenge_id"], "type": "manual_login"}
    return res


async def _mark_success(db: Database, sess: Session) -> dict:
    sess.pending = None
    db.set_login_status(sess.user_key, "logged_in")
    db.update_session(sess.user_key, status="logged_in", logged_in=True, error=None)
    await manager.save_cookies(sess.user_key)  # 存盘以便跨进程免登录
    return {"ok": True, "user_key": sess.user_key, "status": "logged_in",
            "logged_in": True, "message": "登录成功"}


async def _make_sms_challenge(db: Database, sess: Session) -> dict:
    phone_hint = None
    try:
        loc = sess.page.locator(S.PHONE_HINT_TEXT).first
        if await loc.count() > 0:
            phone_hint = (await loc.inner_text()).strip()
    except Exception:
        pass
    res = await create_challenge(
        db, sess.page, sess.user_key, "sms",
        "因 IP 变化等需要短信验证码，请输入收到的短信验证码",
        phone_hint=phone_hint,
    )
    sess.pending = {"challenge_id": res["challenge_id"], "type": "sms"}
    return res


async def _make_confirm_send_sms(db: Database, sess: Session) -> dict:
    res = await create_challenge(
        db, sess.page, sess.user_key, "confirm_send_sms",
        "需要短信验证码。提交任意值以确认发送短信（避免误触发风控）",
    )
    sess.pending = {"challenge_id": res["challenge_id"], "type": "confirm_send_sms"}
    return res


# ---------- 对外：start_login ----------
async def start_login(db: Database, user_key: str) -> dict:
    sess = await manager.get_session(user_key)
    db.expire_stale_challenges(user_key)
    if sess.pending:
        return {"ok": False, "code": "CHALLENGE_PENDING",
                "message": "存在待处理的 challenge，请先 submit_challenge",
                "challenge_id": sess.pending["challenge_id"]}

    account = db.get_account(user_key)
    if not account or not account.get("username"):
        return {"ok": False, "code": "NO_ACCOUNT", "message": "未保存账号，请先 save_account"}

    async with sess.lock:
        page = sess.page
        try:
            await page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            db.update_session(user_key, status="error", error=str(e))
            return {"ok": False, "code": "NAV_FAILED", "message": f"打开页面失败：{e}"}

        # 等重定向链/CAS SSO 跳转 settle，再决定登录还是已登录
        state = await _wait_settled(page)
        if state == "logged_in":
            return await _mark_success(db, sess)
        if state == "timeout":
            return {"ok": False, "code": "AUTH_UNSETTLED",
                    "message": "页面停在 kjyy 但未取得会话 token，请重试 start_login"}

        # 填账号密码（填到一半若 SSO 突然完成跳走，回查登录态）
        try:
            await page.fill(S.USERNAME_INPUT, account["username"], timeout=8000)
            await page.fill(S.PASSWORD_INPUT, account["password"], timeout=8000)
        except Exception as e:
            if await is_logged_in(page):
                return await _mark_success(db, sess)
            return {"ok": False, "code": "LOGIN_FORM_NOT_FOUND",
                    "message": f"未定位到登录表单：{e}"}

        # 验证码：有时有有时无，动态检测
        if await _visible(page, S.CAPTCHA_INPUT):
            res = await create_challenge(
                db, page, user_key, "captcha", "请输入图形验证码",
                image_selector=S.CAPTCHA_IMAGE,
            )
            sess.pending = {"challenge_id": res["challenge_id"], "type": "captcha"}
            return res

        # 无验证码，直接登录
        await _click_login(page)
        return await _evaluate_after_submit(db, sess)


async def _click_login(page: Page) -> None:
    try:
        await page.click(S.LOGIN_BUTTON, timeout=8000)
    except Exception:
        await page.keyboard.press("Enter")


# ---------- 对外：submit_challenge ----------
async def submit_challenge(db: Database, user_key: str, challenge_id: str, answer: str) -> dict:
    sess = manager.peek(user_key)
    row = db.get_challenge(challenge_id)
    if not row:
        return {"ok": False, "code": "CHALLENGE_NOT_FOUND", "message": "challenge 不存在"}
    if row["status"] != "pending":
        return {"ok": False, "code": "CHALLENGE_NOT_PENDING",
                "message": f"challenge 状态为 {row['status']}"}
    if sess is None or sess.pending is None or sess.pending["challenge_id"] != challenge_id:
        db.set_challenge_status(challenge_id, "expired")
        return {"ok": False, "code": "SESSION_LOST",
                "message": "挂起的浏览器会话已丢失（可能超时或重启），请重新 start_login"}

    async with sess.lock:
        page = sess.page
        ctype = sess.pending["type"]
        try:
            if ctype == "captcha":
                await page.fill(S.CAPTCHA_INPUT, answer, timeout=8000)
                await _click_login(page)
            elif ctype == "sms":
                await page.fill(S.SMS_INPUT, answer, timeout=8000)
                await _click_login(page)
            elif ctype == "confirm_send_sms":
                await page.click(S.SMS_SEND_BUTTON, timeout=8000)
                await page.wait_for_timeout(1500)
                db.set_challenge_status(challenge_id, "resolved")
                return await _make_sms_challenge(db, sess)
            else:  # manual_login
                if await is_logged_in(page):
                    db.set_challenge_status(challenge_id, "resolved")
                    return await _mark_success(db, sess)
                return {"ok": False, "code": "STILL_NOT_LOGGED_IN",
                        "message": "仍未登录，请继续手动操作"}
        except Exception as e:
            return {"ok": False, "code": "SUBMIT_FAILED", "message": str(e)}

        db.set_challenge_status(challenge_id, "resolved")
        return await _evaluate_after_submit(db, sess)


# ---------- 对外：get_login_status（实地探测） ----------
async def get_login_status(db: Database, user_key: str) -> dict:
    account = db.get_account(user_key)
    if not account:
        return {"ok": True, "user_key": user_key, "logged_in": False,
                "status": "no_account", "needs_challenge": False,
                "message": "未保存账号"}
    sess = await manager.get_session(user_key)
    async with sess.lock:
        page = sess.page
        try:
            await page.goto(settings.base_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            db.update_session(user_key, status="error", error=str(e))
            return {"ok": False, "user_key": user_key, "logged_in": False,
                    "status": "error", "message": str(e)}
        logged = await _wait_settled(page) == "logged_in"
        status = "logged_in" if logged else "logged_out"
        db.set_login_status(user_key, status)
        db.update_session(user_key, status=status, logged_in=logged)
        if logged:
            await manager.save_cookies(user_key)  # 刷新存盘的会话 cookie
        return {"ok": True, "user_key": user_key, "logged_in": logged,
                "status": status, "needs_challenge": False,
                "message": "当前登录态可用" if logged else "登录态已失效，请 start_login"}
