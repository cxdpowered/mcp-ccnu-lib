"""FastMCP 服务入口。第一阶段：账号 + 登录 + challenge。"""
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from . import login, reservations
from .config import settings
from .db import Database

mcp = FastMCP("ccnu_library", host=settings.mcp_host, port=settings.mcp_port)
db = Database(settings.db_path)


def _uk(user_key: str | None) -> str:
    return user_key or settings.default_user_key


@mcp.tool()
async def save_account(
    user_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
    phone_hint: str | None = None,
    login_now: bool = False,
) -> dict:
    """保存图书馆账号密码（明文，个人自用）。已存在则覆盖。"""
    uk = _uk(user_key)
    username = username or settings.default_username
    password = password or settings.default_password
    if not username or not password:
        return {"ok": False, "code": "MISSING_CREDENTIALS",
                "message": "需要 username/password（或在 .env 配 default 账号）"}
    db.upsert_account(uk, username, password, phone_hint,
                      str(settings.profile_dir(uk)))
    if login_now:
        return await login.start_login(db, uk)
    return {"ok": True, "user_key": uk, "message": "账号已保存"}


@mcp.tool()
async def get_login_status(user_key: str | None = None) -> dict:
    """实地探测当前登录态（打开预约页看是否被打回登录页）。"""
    return await login.get_login_status(db, _uk(user_key))


@mcp.tool()
async def start_login(
    user_key: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict:
    """发起登录。遇验证码/短信返回 NEED_CHALLENGE，用 submit_challenge 续跑。"""
    uk = _uk(user_key)
    if username and password:
        db.upsert_account(uk, username, password, None, str(settings.profile_dir(uk)))
    elif not db.get_account(uk) and settings.default_username:
        db.upsert_account(uk, settings.default_username, settings.default_password,
                          None, str(settings.profile_dir(uk)))
    return await login.start_login(db, uk)


@mcp.tool()
async def submit_challenge(challenge_id: str, answer: str,
                           user_key: str | None = None) -> dict:
    """提交验证码/短信码，继续被挂起的登录流程。"""
    return await login.submit_challenge(db, _uk(user_key), challenge_id, answer)


# ---------- 阶段 2：查询与预约 ----------
@mcp.tool()
async def get_availability_distribution(
    date: str, start_time: str, end_time: str,
    user_key: str | None = None, library: str | None = None,
    area_filter: str | None = None,
) -> dict:
    """查询各区域可用座位分布（date=YYYY-MM-DD, 时间=HH:MM）。"""
    return await reservations.get_availability_distribution(
        db, _uk(user_key), date, start_time, end_time, library, area_filter)


@mcp.tool()
async def list_available_seats(
    date: str, start_time: str, end_time: str, location_id: str,
    user_key: str | None = None, area_filter: str | None = None,
    limit: int = 50,
) -> dict:
    """列出某区域(location_id)指定时段的具体可用座位。"""
    return await reservations.list_available_seats(
        db, _uk(user_key), date, start_time, end_time, location_id, area_filter, limit)


@mcp.tool()
async def reserve_seat(
    date: str, start_time: str, end_time: str,
    user_key: str | None = None, seat_id: str | None = None,
    location_id: str | None = None, strategy: str | None = None,
) -> dict:
    """提交预约。strategy: exact_seat/favorite_first/first_available/random_available。
    高风险操作，调用方需自行确认。"""
    return await reservations.reserve_seat(
        db, _uk(user_key), date, start_time, end_time, seat_id, location_id, strategy)


@mcp.tool()
async def cancel_reservation(reservation_id: str | None = None,
                             user_key: str | None = None) -> dict:
    """取消预约（不传 reservation_id 则取消当前有效预约）。高风险操作。"""
    return await reservations.cancel_reservation(db, _uk(user_key), reservation_id)


@mcp.tool()
async def get_current_reservation(user_key: str | None = None) -> dict:
    """查询当前预约及状态（none/reserved/in_use/away/ended/cancelled/...），含暂离详情。"""
    return await reservations.get_current_reservation(db, _uk(user_key))


# ---------- 阶段 3：暂离 / 回座 / 提前退座 ----------
@mcp.tool()
async def start_temporary_leave(user_key: str | None = None,
                                reservation_id: str | None = None) -> dict:
    """暂离（离座计时）。高风险操作。"""
    return await reservations.start_temporary_leave(db, _uk(user_key), reservation_id)


@mcp.tool()
async def return_from_temporary_leave(user_key: str | None = None,
                                      reservation_id: str | None = None) -> dict:
    """回座（暂离后返回继续履约）。高风险操作。"""
    return await reservations.return_from_temporary_leave(db, _uk(user_key), reservation_id)


@mcp.tool()
async def end_reservation_early(user_key: str | None = None,
                                reservation_id: str | None = None) -> dict:
    """提前结束/退座（主动放弃使用中的座位，区别于取消未开始预约）。高风险操作。"""
    return await reservations.end_reservation_early(db, _uk(user_key), reservation_id)


@mcp.tool()
async def get_site_favorite_locations(user_key: str | None = None) -> dict:
    """网站里的常用/收藏座位。"""
    return await reservations.get_site_favorite_locations(db, _uk(user_key))


@mcp.tool()
async def get_seat_layout(location_id: str, user_key: str | None = None) -> dict:
    """透传网页版某区域(location_id)的座位平面布局原始数据，用于渲染座位图/辅助选座。"""
    return await reservations.get_seat_layout(db, _uk(user_key), location_id)


@mcp.tool()
async def get_door_log(date: str, user_key: str | None = None) -> dict:
    """某天(yyyy-MM-dd)闸机进出记录。物理暂离不改预约状态，判断是否在馆看这里
    (in_building=最后一条是否为入)。events: [{time,gate,direction(in/out)}]。"""
    return await reservations.get_door_log(db, _uk(user_key), date)


@mcp.tool()
async def get_violation_records(
    user_key: str | None = None, page: int = 1, page_size: int = 20,
) -> dict:
    """违约记录（爽约/超时未签到等），分页。records 为映射后的预约记录列表。"""
    return await reservations.get_violation_records(db, _uk(user_key), page, page_size)


@mcp.tool()
async def get_reservation_history(
    user_key: str | None = None, page: int = 1, page_size: int = 20,
) -> dict:
    """历史预约记录（已结束/取消/违约的所有过往预约），分页。"""
    return await reservations.get_reservation_history(db, _uk(user_key), page, page_size)


def main() -> None:
    # FastMCP 自带事件循环；浏览器在首个工具调用时惰性启动（get_session 内 await start）。
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
