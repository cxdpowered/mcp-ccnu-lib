"""本地登录闭环验证脚本（不经过 MCP，直接调 login 流程）。

- headful 启动，你能看着浏览器一步步走。
- 遇验证码：截图存盘并尝试自动弹开，终端输入答案即可。
- 跑通后终端打印 logged_in。

运行（校园网机器）：
    pip install -e .
    python -m playwright install chromium
    python scripts/try_login.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

# 让脚本未 pip install 也能 import；并强制有界面方便观察
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("HEADLESS", "false")

from ccnu_lib import login              # noqa: E402
from ccnu_lib.browser import manager    # noqa: E402
from ccnu_lib.config import settings    # noqa: E402
from ccnu_lib.db import Database        # noqa: E402


async def ask(prompt: str) -> str:
    return (await asyncio.to_thread(input, prompt)).strip()


def show_image(path: str | None) -> None:
    if not path or not Path(path).exists():
        return
    print(f"  验证码图片已存到: {path}")
    try:
        if sys.platform == "win32":
            os.startfile(path)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            os.system(f"open '{path}'")
        else:
            os.system(f"xdg-open '{path}' >/dev/null 2>&1 &")
    except Exception:
        pass


async def main() -> None:
    user_key = settings.default_user_key
    db = Database(settings.db_path)

    # 确保有账号（取 .env 里的 default 账号）
    if not db.get_account(user_key):
        if not (settings.default_username and settings.default_password):
            print("✗ 未保存账号，且 .env 没有 CCNU_DEFAULT_USERNAME/PASSWORD")
            return
        db.upsert_account(user_key, settings.default_username,
                          settings.default_password, None,
                          str(settings.profile_dir(user_key)))
        print(f"已写入 default 账号: {settings.default_username}")

    print(f"\n=== start_login(user_key={user_key}) ===")
    res = await login.start_login(db, user_key)

    # 循环处理 challenge
    while res.get("code") == "NEED_CHALLENGE":
        print(f"\n[challenge] 类型={res['challenge_type']}  {res.get('prompt','')}")
        if res.get("phone_hint"):
            print(f"  手机号: {res['phone_hint']}")
        show_image(res.get("screenshot_path"))
        answer = await ask("  请输入答案（直接回车放弃）: ")
        if not answer:
            print("已放弃。")
            break
        res = await login.submit_challenge(db, user_key, res["challenge_id"], answer)

    print("\n=== 结果 ===")
    print(res)
    if res.get("logged_in"):
        print("\n✅ 登录成功！profile 已保活，下次应可免登录。")
        print("（再跑一次本脚本，若直接 logged_in 不弹验证码，说明保活验证通过）")
    else:
        print("\n✗ 未登录，看上面的 code/message 排查。")

    await asyncio.sleep(2)
    await manager.stop()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    asyncio.run(main())
