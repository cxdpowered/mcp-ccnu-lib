"""登录状态机（纯 HTTP CAS SSO）。

流程：getSysSet 取配置 → GET {CAS}/static/sso/login?redirectUrl={VUE} 跟随重定向
  · 落到 CAS 登录表单(200) → 未登录：抓验证码返回 NEED_CHALLENGE
  · 直接跳回 VUESERVICE 带 ?token=<JWT>（CASTGC 有效时的静默 SSO）→ 已登录
submit_challenge：POST CAS 表单 → 跟随 ticket → 拿 JWT → auth/cas 换 app token。

"会话独占"仍用 session.pending：有挂起 challenge 时新动作返回 CHALLENGE_PENDING。
登录态保活靠持久化 cookie（CASTGC）支持静默重登 + 缓存的 app token。
"""
from __future__ import annotations

import base64
import re
import time

import httpx

from . import api
from .config import settings
from .db import Database
from .http_client import Session, manager

CAPTCHA_URL = "https://account.ccnu.edu.cn/cas/captcha.jpg"
_ERROR_HINTS = ["用户名或密码", "密码错误", "认证信息无效", "认证失败",
                "验证码错误", "验证码不正确", "无效的验证码", "账号被锁定"]


# ---------- 系统配置 / 签名密钥 ----------
async def ensure_syscfg(sess: Session) -> None:
    if sess.realkey and sess.syscfg:
        return
    data = await api.get_sys_set(sess.client)
    sess.syscfg = data
    if data.get("hmac") == 1 and data.get("hmacKey"):
        sess.realkey = api.derive_realkey(data["hmacKey"])


def _cas_urls(sess: Session) -> tuple[str, str]:
    vc = (sess.syscfg or {}).get("vueConfig") or {}
    return vc.get("CASSSERVICE", ""), vc.get("VUESERVICE", "")


# ---------- CAS 链跟随 ----------
def _parse_params(url: str) -> dict[str, str]:
    q = url.split("?", 1)[1] if "?" in url else ""
    q = q.split("#")[0]
    out: dict[str, str] = {}
    for kv in q.split("&"):
        if "=" in kv:
            k, v = kv.split("=", 1)
            out.setdefault(k, v)
    return out


def _parse_cas_form(page_url: str, body: str) -> dict:
    def g(pat):
        m = re.search(pat, body)
        return m.group(1) if m else None
    action = g(r'<form[^>]*action="([^"]+)"')
    return {
        "cas_login_url": str(httpx.URL(page_url).join(action)) if action else page_url,
        "lt": g(r'name="lt" value="([^"]+)"'),
        "execution": g(r'name="execution" value="([^"]+)"'),
    }


async def _sso_walk(sess: Session) -> tuple[str, dict]:
    """GET sso/login 跟随重定向。返回 ('token', params) / ('form', ctx) / ('unknown', {})。"""
    cas, vue = _cas_urls(sess)
    if not cas or not vue:
        return "unknown", {}
    resp = await sess.client.get(f"{cas}/static/sso/login?redirectUrl={vue}")
    for _ in range(10):
        loc = resp.headers.get("location")
        if not loc:
            body = resp.text
            if 'name="execution"' in body:
                return "form", _parse_cas_form(str(resp.url), body)
            return "unknown", {}
        nxt = str(httpx.URL(str(resp.url)).join(loc))
        if "jsq-v" in nxt:
            return "token", _parse_params(nxt)
        resp = await sess.client.get(nxt)
    return "unknown", {}


async def _follow_to_token(sess: Session, resp: httpx.Response) -> dict | None:
    """从一个 302 响应继续跟随，直到回到 VUESERVICE 抓到 token 参数。"""
    for _ in range(10):
        loc = resp.headers.get("location")
        if not loc:
            return None
        nxt = str(httpx.URL(str(resp.url)).join(loc))
        if "jsq-v" in nxt:
            return _parse_params(nxt)
        resp = await sess.client.get(nxt)
    return None


async def _exchange(db: Database, sess: Session, params: dict) -> dict:
    """用 CAS 回跳的 JWT 换 app token，成功即标记登录。"""
    cas_token = params.get("token")
    if not cas_token:
        return {"ok": False, "code": "NO_CAS_TOKEN", "message": "SSO 未返回 token"}
    app_token = await api.auth_cas(sess.client, cas_token, params)
    if not app_token:
        return {"ok": False, "code": "AUTH_EXCHANGE_FAILED", "message": "app token 换取失败"}
    sess.token = app_token
    return await _mark_success(db, sess)


async def _mark_success(db: Database, sess: Session) -> dict:
    sess.pending = None
    sess.login_ctx = None
    db.set_login_status(sess.user_key, "logged_in")
    db.update_session(sess.user_key, status="logged_in", logged_in=True, error=None)
    await manager.save(sess.user_key)
    return {"ok": True, "user_key": sess.user_key, "status": "logged_in",
            "logged_in": True, "message": "登录成功"}


# ---------- 对外：start_login ----------
def _pending_challenge_response(row: dict) -> dict:
    image_base64 = None
    screenshot_path = row.get("screenshot_path")
    if screenshot_path:
        try:
            raw = open(screenshot_path, "rb").read()
            mime = "image/png" if str(screenshot_path).lower().endswith(".png") else "image/jpeg"
            image_base64 = f"data:{mime};base64," + base64.b64encode(raw).decode()
        except OSError:
            image_base64 = None
    res = {
        "ok": False,
        "code": "NEED_CHALLENGE",
        "challenge_id": row["challenge_id"],
        "challenge_type": row.get("type") or "captcha",
        "prompt": row.get("prompt") or "请输入图形验证码",
        "expires_at": row.get("expires_at"),
    }
    if image_base64:
        res["image_base64"] = image_base64
        res["screenshot_path"] = screenshot_path
    return res


async def start_login(db: Database, user_key: str) -> dict:
    sess = await manager.get_session(user_key)
    db.expire_stale_challenges(user_key)
    if sess.pending:
        row = db.get_pending_challenge(user_key, sess.pending["challenge_id"])
        if row and int(row.get("expires_at") or 0) >= int(time.time()):
            return _pending_challenge_response(row)
        sess.pending = None
        sess.login_ctx = None

    account = db.get_account(user_key)
    if not account or not account.get("username"):
        return {"ok": False, "code": "NO_ACCOUNT", "message": "未保存账号，请先 save_account"}

    async with sess.lock:
        try:
            await ensure_syscfg(sess)
        except Exception as e:
            return {"ok": False, "code": "SYSCFG_FAILED", "message": f"取系统配置失败：{e}"}

        try:
            kind, ctx = await _sso_walk(sess)
        except Exception as e:
            return {"ok": False, "code": "SSO_FAILED", "message": f"SSO 链失败：{e}"}

        if kind == "token":  # CASTGC 有效，静默登录
            return await _exchange(db, sess, ctx)
        if kind != "form":
            return {"ok": False, "code": "AUTH_UNSETTLED",
                    "message": "SSO 链未落到登录表单也未拿到 token，请重试"}

        # 落到 CAS 登录表单 → 抓验证码，转人工
        sess.login_ctx = ctx
        try:
            cap = await sess.client.get(CAPTCHA_URL)
            image_bytes = cap.content
        except Exception:
            image_bytes = None
        from .challenges import create_challenge
        res = create_challenge(db, user_key, "captcha", "请输入图形验证码",
                               image_bytes=image_bytes, image_mime="image/jpeg")
        sess.pending = {"challenge_id": res["challenge_id"], "type": "captcha"}
        return res


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
                "message": "挂起的会话已丢失（可能超时或重启），请重新 start_login"}

    async with sess.lock:
        ctx = sess.login_ctx or {}
        account = db.get_account(user_key) or {}
        form = {
            "username": account.get("username", ""),
            "password": account.get("password", ""),
            "captcha": answer,
            "lt": ctx.get("lt", ""),
            "execution": ctx.get("execution", "e1s1"),
            "_eventId": "submit",
            "submit": "登录",
        }
        try:
            resp = await sess.client.post(
                ctx.get("cas_login_url", ""), data=form,
                headers={"Content-Type": "application/x-www-form-urlencoded",
                         "Referer": ctx.get("cas_login_url", "")},
            )
        except Exception as e:
            return {"ok": False, "code": "SUBMIT_FAILED", "message": str(e)}

        db.set_challenge_status(challenge_id, "resolved")

        # 仍停在登录表单 = 登录失败（验证码/密码错），验证码已刷新需重来
        if resp.status_code == 200 and 'name="execution"' in resp.text:
            sess.pending = None
            sess.login_ctx = None
            err = next((h for h in _ERROR_HINTS if h in resp.text), "认证失败")
            db.update_session(user_key, status="login_failed", error=err)
            return {"ok": False, "code": "LOGIN_FAILED",
                    "message": f"登录失败：{err}（验证码已刷新，请重新 start_login）"}

        params = await _follow_to_token(sess, resp)
        if not params:
            sess.pending = None
            sess.login_ctx = None
            return {"ok": False, "code": "AUTH_UNSETTLED",
                    "message": "提交后未拿到 token，请重新 start_login"}
        return await _exchange(db, sess, params)


# ---------- 对外：get_login_status（实地探测） ----------
async def get_login_status(db: Database, user_key: str) -> dict:
    account = db.get_account(user_key)
    if not account:
        return {"ok": True, "user_key": user_key, "logged_in": False,
                "status": "no_account", "needs_challenge": False, "message": "未保存账号"}
    sess = await manager.get_session(user_key)
    async with sess.lock:
        try:
            await ensure_syscfg(sess)
            kind, ctx = await _sso_walk(sess)
        except Exception as e:
            db.update_session(user_key, status="error", error=str(e))
            return {"ok": False, "user_key": user_key, "logged_in": False,
                    "status": "error", "message": str(e)}
        logged = False
        if kind == "token":
            try:
                await _exchange(db, sess, ctx)
                logged = True
            except Exception:
                logged = False
        status = "logged_in" if logged else "logged_out"
        db.set_login_status(user_key, status)
        db.update_session(user_key, status=status, logged_in=logged)
        return {"ok": True, "user_key": user_key, "logged_in": logged,
                "status": status, "needs_challenge": False,
                "message": "当前登录态可用" if logged else "登录态已失效，请 start_login"}
