"""阶段 2：查询与预约工具（直连 kjyy API）。

每个工具先确保登录态（有 token 直接用，否则尝试 SSO/提示登录），再调 api.*。
"""
from __future__ import annotations

import json
import random
from typing import Any

from . import api, login
from .browser import Session, manager
from .db import Database

# 后端 status → 需求枚举
_STATUS = {
    "RESERVE": "reserved",
    "USING": "in_use", "USE": "in_use", "SIGN": "in_use", "SIGNED": "in_use",
    "AWAY": "away", "LEAVE": "away",
    "END": "ended", "FINISH": "ended", "OVER": "ended", "COMPLETE": "ended",
    "CANCEL": "cancelled",
    "BREAK": "violation_risk", "VIOLATION": "violation_risk",
    "ILLEGAL": "violation_risk", "BREACH": "violation_risk",
}
_ACTIVE = {"reserved", "waiting_sign_in", "in_use", "away", "violation_risk"}


def _map_status(s: str | None) -> str:
    return _STATUS.get((s or "").upper(), "unknown")


def _fmt_make(rec: dict) -> dict:
    loc = rec.get("location") or ""
    path = loc.split("|") if loc else [rec.get("buildName"), rec.get("floorName"),
                                       rec.get("roomName")]
    return {
        "reservation_id": rec.get("id"),
        "status": _map_status(rec.get("status")),
        "raw_status": rec.get("status"),
        "seat_no": rec.get("seatLabel"),
        "path": [p for p in path if p],
        "date": rec.get("makeDateStr"),
        "start_time": rec.get("makeBeginStr"),
        "end_time": rec.get("makeEndStr"),
        "receipt": rec.get("receipt"),
        "raw_text": rec.get("message"),
    }


# ---------- 登录态保障 ----------
async def _require_login(db: Database, uk: str) -> tuple[Session | None, dict | None]:
    sess = await manager.get_session(uk)
    if await login._has_token(sess.page):
        return sess, None
    st = await login.get_login_status(db, uk)  # 尝试 SSO 复登
    if st.get("logged_in"):
        return manager.peek(uk), None
    return None, {"ok": False, "code": "NEED_LOGIN",
                  "message": "未登录或登录态失效，请先 start_login", "user_key": uk}


def _check_time(start: str, end: str) -> dict | None:
    try:
        b, e = api.hm_to_min(start), api.hm_to_min(end)
    except Exception:
        return {"ok": False, "code": "BAD_TIME", "message": "时间格式应为 HH:MM"}
    if b >= e:
        return {"ok": False, "code": "BAD_TIME", "message": "start_time 必须早于 end_time"}
    return None


# ---------- 1. 区域可用分布 ----------
async def get_availability_distribution(
    db: Database, uk: str, date: str, start_time: str, end_time: str,
    library: str | None = None, area_filter: str | None = None,
) -> dict:
    if err := _check_time(start_time, end_time):
        return err
    sess, err = await _require_login(db, uk)
    if err:
        return err
    begin, end = api.hm_to_min(start_time), api.hm_to_min(end_time)

    bfd = await api.building_floor_date(sess)
    buildings = bfd.get("buildings", [])
    if library:
        buildings = [b for b in buildings if library in b.get("name", "")]

    locations: list[dict] = []
    for b in buildings:
        page = 1
        while True:
            rd = await api.find_room_duration(sess, b["id"], date, begin,
                                              end_min=end, page=page)
            for r in rd.get("pageList", []):
                if area_filter and area_filter not in r.get("name", ""):
                    continue
                locations.append({
                    "location_id": r["id"],
                    "path": [r.get("buildingName"), r.get("floorName"), r.get("name")],
                    "total": r.get("seatTotal"),
                    "available": r.get("seatFree"),
                    "max_minute": r.get("maxMinute"),
                })
            if not rd.get("next") or page >= rd.get("totalPage", 1):
                break
            page += 1

    locations.sort(key=lambda x: -(x.get("available") or 0))
    return {"ok": True, "date": date, "start_time": start_time,
            "end_time": end_time, "locations": locations}


# ---------- 2. 具体可用座位 ----------
async def list_available_seats(
    db: Database, uk: str, date: str, start_time: str, end_time: str,
    location_id: str | None = None, area_filter: str | None = None,
    limit: int = 50,
) -> dict:
    if err := _check_time(start_time, end_time):
        return err
    if not location_id:
        return {"ok": False, "code": "NEED_LOCATION",
                "message": "请传 location_id（先调 get_availability_distribution 取区域）"}
    sess, err = await _require_login(db, uk)
    if err:
        return err
    begin, end = api.hm_to_min(start_time), api.hm_to_min(end_time)

    raw = await api.free_seats(sess, location_id, date, begin, end)
    items = list(raw.values()) if isinstance(raw, dict) else (raw or [])
    seats = []
    for it in items:
        label = it.get("label")
        if area_filter and area_filter not in (label or ""):
            continue
        seats.append({
            "seat_id": it.get("id"),
            "seat_no": label,
            "name": it.get("name"),
            "location_id": location_id,
            "available": it.get("status") == "FREE",
        })
        if len(seats) >= limit:
            break
    return {"ok": True, "date": date, "location_id": location_id, "seats": seats}


# ---------- 3. 提交预约 ----------
async def reserve_seat(
    db: Database, uk: str, date: str, start_time: str, end_time: str,
    seat_id: str | None = None, location_id: str | None = None,
    strategy: str | None = None,
) -> dict:
    if err := _check_time(start_time, end_time):
        return err
    sess, err = await _require_login(db, uk)
    if err:
        return err
    begin, end = api.hm_to_min(start_time), api.hm_to_min(end_time)
    strategy = strategy or ("exact_seat" if seat_id else "first_available")

    # 选座
    if strategy != "exact_seat" or not seat_id:
        if not location_id:
            return {"ok": False, "code": "NEED_LOCATION",
                    "message": "非 exact_seat 策略需要 location_id"}
        raw = await api.free_seats(sess, location_id, date, begin, end)
        free = [it for it in (raw.values() if isinstance(raw, dict) else raw)
                if it.get("status") == "FREE"]
        if not free:
            return {"ok": False, "code": "NO_SEAT", "message": "该区域该时段无空闲座位"}
        if strategy == "random_available":
            seat_id = random.choice(free)["id"]
        elif strategy == "favorite_first":
            seat_id = await _pick_favorite(sess, free) or free[0]["id"]
        else:  # first_available
            seat_id = free[0]["id"]

    try:
        rec = await api.free_book(sess, seat_id, date, begin, end)
    except api.ApiError as e:
        return {"ok": False, "code": "RESERVE_FAILED", "message": str(e)}
    out = _fmt_make(rec)
    out["ok"] = True
    return out


async def _pick_favorite(sess: Session, free: list[dict]) -> str | None:
    # 用户收藏座位（getUserInfo.likeSeats，按使用次数 num 排序）与空闲集求交
    try:
        info = await api.get_user_info(sess)
        likes = json.loads(info.get("likeSeats") or "[]")
        likes.sort(key=lambda x: -(x.get("num") or 0))
        free_ids = {it["id"] for it in free}
        for lk in likes:
            if lk.get("seatId") in free_ids:
                return lk["seatId"]
    except Exception:
        pass
    return None


# ---------- 4. 取消预约 ----------
async def cancel_reservation(db: Database, uk: str,
                             reservation_id: str | None = None) -> dict:
    sess, err = await _require_login(db, uk)
    if err:
        return err
    if not reservation_id:
        makes = await api.last_make(sess)
        active = next((m for m in (makes or [])
                       if _map_status(m.get("status")) in _ACTIVE), None)
        if not active:
            return {"ok": False, "code": "NO_ACTIVE", "message": "无可取消的预约"}
        reservation_id = active["id"]
    try:
        await api.cancel(sess, reservation_id)
    except api.ApiError as e:
        return {"ok": False, "code": "CANCEL_FAILED", "message": str(e)}
    return {"ok": True, "reservation_id": reservation_id, "status": "cancelled"}


# ---------- 5. 当前预约状态 ----------
async def get_current_reservation(db: Database, uk: str) -> dict:
    sess, err = await _require_login(db, uk)
    if err:
        return err
    makes = await api.last_make(sess)
    if not makes:
        return {"ok": True, "status": "none", "message": "当前无预约"}
    active = next((m for m in makes if _map_status(m.get("status")) in _ACTIVE), None)
    rec = active or makes[0]
    out = _fmt_make(rec)
    out["ok"] = True
    if not active:
        out["status"] = "none" if out["status"] in ("cancelled", "ended") else out["status"]
    # 暂离/使用中详情：暴露所有 away* 字段 + 实际起止，供小青团判断与提醒
    away = {k: v for k, v in rec.items() if "away" in k.lower() and v not in ("", None)}
    if away:
        out["away_raw"] = away
        out["away_deadline"] = (rec.get("awayDeadline") or rec.get("awayEndTime")
                                or rec.get("awayBackTime") or None)
    out["actual_begin"] = rec.get("actualBegin") or None
    out["actual_end"] = rec.get("actualEnd") if rec.get("actualEnd") not in (-1, "") else None
    return out


# ---------- 阶段 3：暂离 / 回座 / 提前退座 ----------
async def _simple_action(db: Database, uk: str, fn, ok_status: str,
                         ok_msg: str, fail_code: str) -> dict:
    sess, err = await _require_login(db, uk)
    if err:
        return err
    try:
        data = await fn(sess)
    except api.ApiError as e:
        return {"ok": False, "code": fail_code, "message": str(e)}
    out = {"ok": True, "status": ok_status,
           "message": data if isinstance(data, str) else ok_msg}
    if isinstance(data, dict):
        out["raw"] = data
    return out


async def start_temporary_leave(db: Database, uk: str,
                                reservation_id: str | None = None) -> dict:
    res = await _simple_action(db, uk, api.make_leave, "away", "已暂离",
                               "LEAVE_FAILED")
    if res.get("ok"):  # 补一份 away_deadline 给调用方
        cur = await get_current_reservation(db, uk)
        for k in ("away_deadline", "away_raw"):
            if cur.get(k) is not None:
                res[k] = cur[k]
    return res


async def return_from_temporary_leave(db: Database, uk: str,
                                      reservation_id: str | None = None) -> dict:
    return await _simple_action(db, uk, api.make_check_in, "in_use", "已回座",
                                "RETURN_FAILED")


async def end_reservation_early(db: Database, uk: str,
                                reservation_id: str | None = None) -> dict:
    return await _simple_action(db, uk, api.make_stop, "ended", "已提前结束/退座",
                                "END_FAILED")


# ---------- 网站常用位置 ----------
async def get_site_favorite_locations(db: Database, uk: str) -> dict:
    sess, err = await _require_login(db, uk)
    if err:
        return err
    data = await api.find_common_seat(sess)
    return {"ok": True, "favorites": data}
