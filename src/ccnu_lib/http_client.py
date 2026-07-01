"""HTTP 会话管理：每个 user_key 一个 httpx.AsyncClient（cookie jar）+ 锁 + 挂起态。

纯 HTTP，无浏览器。登录态保活载体从 Playwright profile 换成持久化的
cookie jar（关键是 CASTGC 这类无过期 session cookie → 支持静默 SSO 重登）
外加 app token，一起存 {user_dir}/session.json。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

from .config import settings


@dataclass
class Session:
    user_key: str
    client: httpx.AsyncClient
    token: Optional[str] = None            # app token（= SPA 的 jsq_p-token）
    realkey: Optional[str] = None          # HMAC 真实密钥（getSysSet 解出）
    syscfg: Optional[dict] = None          # getSysSet 返回（含 vueConfig）
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 挂起中的 challenge：{"challenge_id":..., "type":...}
    pending: Optional[dict[str, Any]] = None
    # CAS 登录中转态：{"cas_login_url":..., "lt":..., "execution":...}
    login_ctx: Optional[dict[str, Any]] = None


class HttpManager:
    """进程级单例，持有各用户的 HTTP 会话。"""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._create_lock = asyncio.Lock()

    async def stop(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.client.aclose()
            except Exception:
                pass
        self._sessions.clear()

    def _new_client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=20, verify=False, follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0", "loginType": "PC"},
        )

    async def get_session(self, user_key: str) -> Session:
        async with self._create_lock:
            sess = self._sessions.get(user_key)
            if sess is not None:
                return sess
            client = self._new_client()
            sess = Session(user_key=user_key, client=client)
            self._restore(user_key, sess)
            self._sessions[user_key] = sess
            return sess

    # ---------- 持久化（cookie + token）----------
    def _state_path(self, user_key: str):
        return settings.user_dir(user_key) / "session.json"

    def _restore(self, user_key: str, sess: Session) -> None:
        p = self._state_path(user_key)
        if not p.exists():
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            sess.token = data.get("token")
            for c in data.get("cookies", []):
                sess.client.cookies.set(c["name"], c["value"],
                                        domain=c.get("domain", ""), path=c.get("path", "/"))
        except Exception:
            pass

    async def save(self, user_key: str) -> None:
        sess = self._sessions.get(user_key)
        if sess is None:
            return
        try:
            cookies = []
            for c in sess.client.cookies.jar:
                cookies.append({"name": c.name, "value": c.value,
                                "domain": c.domain, "path": c.path})
            p = self._state_path(user_key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"token": sess.token, "cookies": cookies},
                                    ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def peek(self, user_key: str) -> Session | None:
        return self._sessions.get(user_key)


# 进程级单例
manager = HttpManager()
