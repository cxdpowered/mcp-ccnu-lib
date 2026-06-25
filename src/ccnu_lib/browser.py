"""浏览器会话管理：每个 user_key 一个 persistent context + 锁 + 挂起态。"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    async_playwright,
)

from .config import settings


@dataclass
class Session:
    user_key: str
    context: BrowserContext
    page: Page
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # 挂起中的 challenge：{"challenge_id":..., "type":..., "resume": <协程工厂>}
    pending: Optional[dict[str, Any]] = None


class BrowserManager:
    """进程级单例，持有 Playwright 与各用户会话。"""

    def __init__(self) -> None:
        self._pw: Playwright | None = None
        self._sessions: dict[str, Session] = {}
        self._create_lock = asyncio.Lock()

    async def start(self) -> None:
        if self._pw is None:
            self._pw = await async_playwright().start()

    async def stop(self) -> None:
        for s in list(self._sessions.values()):
            try:
                await s.context.close()
            except Exception:
                pass
        self._sessions.clear()
        if self._pw is not None:
            await self._pw.stop()
            self._pw = None

    async def get_session(self, user_key: str) -> Session:
        """取或建该用户的 persistent context。profile 落盘即登录态保活载体。"""
        async with self._create_lock:
            sess = self._sessions.get(user_key)
            if sess is not None and not sess.page.is_closed():
                return sess  # 复用存活的会话（含其 profile 登录态）
            await self.start()
            assert self._pw is not None

            profile_dir = settings.profile_dir(user_key)
            profile_dir.mkdir(parents=True, exist_ok=True)
            settings.screenshots_dir(user_key).mkdir(parents=True, exist_ok=True)

            context = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=settings.headless,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            # 回灌上次保存的会话 cookie（含 CASTGC 这类无过期的 session cookie，
            # persistent profile 默认不保存它们，必须手动恢复才能跨进程免登录）
            await self._restore_cookies(user_key, context)
            page = context.pages[0] if context.pages else await context.new_page()
            sess = Session(user_key=user_key, context=context, page=page)
            self._sessions[user_key] = sess
            return sess

    def _cookies_path(self, user_key: str):
        return settings.user_dir(user_key) / "cookies.json"

    async def _restore_cookies(self, user_key: str, context: BrowserContext) -> None:
        p = self._cookies_path(user_key)
        if not p.exists():
            return
        try:
            cookies = json.loads(p.read_text(encoding="utf-8"))
            if cookies:
                await context.add_cookies(cookies)
        except Exception:
            pass

    async def save_cookies(self, user_key: str) -> None:
        sess = self._sessions.get(user_key)
        if sess is None:
            return
        try:
            cookies = await sess.context.cookies()
            p = self._cookies_path(user_key)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cookies, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def peek(self, user_key: str) -> Session | None:
        return self._sessions.get(user_key)


# 进程级单例
manager = BrowserManager()
