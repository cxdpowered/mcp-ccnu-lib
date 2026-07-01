"""kjyy.ccnu.edu.cn/jsq 后端 API 直连客户端（纯 HTTP，无浏览器）。

用已登录会话的 httpx.AsyncClient 发 POST：自动带 rem_JSESSIONID cookie，
附加 header token（app token）+ HMAC 签名三件套。返回统一信封
{status,code,message,data}，call() 解包返回 data，失败抛 ApiError。

鉴权/签名细节见 login.py 与项目 memory `ccnu-api-hmac-signing`：
  msg = "seat::" + uuid + "::" + ms + "::POST"
  realKey = AES-CBC-decrypt(hmacKey, key="server_date_time", iv="client_date_time")
  X-hmac-request-key = HMAC-SHA256(msg, realKey).hex
realKey 由 getSysSet 返回的 hmacKey 运行时解出（当前实测解得 "Lib2025ccnu"）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from .http_client import Session

# 接口根（config.js: BASEURL）。frontApi/public 都挂在 {F}/static/ 下。
F = "https://kjyy.ccnu.edu.cn/jsq"
JSQ = F + "/static"


class ApiError(Exception):
    def __init__(self, code: Any, message: str, path: str):
        super().__init__(f"[{code}] {message} ({path})")
        self.code = code
        self.message = message
        self.path = path


# ---------- 时间换算 ----------
def hm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def min_to_hm(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


# ---------- 签名 ----------
def derive_realkey(hmac_key_b64: str) -> str:
    """AES-CBC 解出真实 HMAC 密钥（固定 key/iv，见模块 docstring）。"""
    ct = base64.b64decode(hmac_key_b64)
    d = Cipher(algorithms.AES(b"server_date_time"),
               modes.CBC(b"client_date_time")).decryptor()
    pt = d.update(ct) + d.finalize()
    return pt[:-pt[-1]].decode("utf-8")  # 去 PKCS7 padding


def sign_headers(token: str, realkey: str | None) -> dict[str, str]:
    h = {"Content-Type": "application/json", "loginType": "PC", "token": token or ""}
    if realkey:
        uid = str(uuid.uuid4())
        date = str(int(time.time() * 1000))
        msg = f"seat::{uid}::{date}::POST"
        h["X-request-id"] = uid
        h["X-request-date"] = date
        h["X-hmac-request-key"] = hmac.new(
            realkey.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return h


# ---------- public（免签名）----------
async def get_sys_set(client) -> dict:
    """系统配置：含 hmac/hmacKey 与 vueConfig(CASSSERVICE/VUESERVICE)。public，免签名。"""
    r = await client.post(JSQ + "/public/cg/getSysSet/PC", json={},
                          headers={"Content-Type": "application/json", "loginType": "PC"})
    js = r.json()
    if not js.get("status"):
        raise ApiError(js.get("code"), js.get("message", "getSysSet 失败"), "public/cg/getSysSet/PC")
    return js.get("data") or {}


async def auth_cas(client, cas_token: str, params: dict) -> str | None:
    """用 CAS 回跳的 JWT 换取 app token（=SPA 的 jsq_p-token）。public，免签名。"""
    r = await client.post(JSQ + f"/public/auth/cas/{cas_token}", json=params,
                          headers={"Content-Type": "application/json", "loginType": "PC"})
    js = r.json()
    if not js.get("status"):
        raise ApiError(js.get("code"), js.get("message", "auth/cas 失败"), "public/auth/cas")
    d = js.get("data")
    return d.get("token") if isinstance(d, dict) else None


# ---------- 底层调用（frontApi，需签名）----------
async def call(session: "Session", path: str, *, data: Any = None,
               params: dict | None = None) -> Any:
    """POST {JSQ}/frontApi/{path}，签名 + 解包。path 形如 'res/buildingFloorDate'。"""
    url = f"{JSQ}/frontApi/{path}"
    if params:
        url += "?" + urlencode(params)
    headers = sign_headers(session.token or "", session.realkey)
    try:
        r = await session.client.post(url, json=data if data is not None else {},
                                      headers=headers)
    except Exception as e:
        raise ApiError("FETCH", str(e), path)
    try:
        js = r.json()
    except Exception:
        raise ApiError(r.status_code, f"非 JSON 响应: {r.text[:200]}", path)
    if not js.get("status"):
        hint = "" if session.token else "（无 app token，可能未登录）"
        raise ApiError(js.get("code"), f"{js.get('message', '失败')}{hint}", path)
    return js.get("data")


# ---------- 接口封装 ----------
async def get_user_info(s: "Session") -> dict:
    return await call(s, "user/getUserInfo")


async def building_floor_date(s: "Session") -> dict:
    return await call(s, "res/buildingFloorDate")


async def find_room_duration(s: "Session", building_id: str, date: str,
                             begin_min: int, *, end_min: int = 0,
                             floor_id: str | int = 0, page: int = 1,
                             page_size: int = 50) -> dict:
    body = {
        "beginMinute": begin_min, "endMinute": end_min, "minMinute": 0,
        "floorId": floor_id, "currentPage": page, "pageSize": page_size,
        "power": False, "roomType": False, "windows": False,
        "sortField": "", "sortType": "",
    }
    return await call(s, f"res/findRoomDuration/{building_id}/{date}", data=body)


async def free_seats(s: "Session", room_id: str, date: str,
                     begin_min: int, end_min: int) -> dict:
    body = {"beginMinute": begin_min, "endMinute": end_min, "minMinute": 0}
    return await call(s, f"res/freeSeatIdsDuration/{room_id}/{date}", data=body)


async def query_seat_layout(s: "Session", room_id: str) -> Any:
    return await call(s, f"res/querySeatLayout/{room_id}/0")


async def free_book(s: "Session", seat_id: str, date: str,
                    begin_min: int, end_min: int) -> dict:
    return await call(
        s, f"make/freeBook/{seat_id}/{date}/{begin_min}/{end_min}",
        params={"capToken": "capToken"},
    )


async def current_use_make(s: "Session") -> Any:
    return await call(s, "user/currentUseMake")


async def last_make(s: "Session") -> Any:
    return await call(s, "user/lastMake")


async def make_life(s: "Session", make_id: str) -> Any:
    return await call(s, f"user/makeLife/{make_id}")


async def cancel(s: "Session", make_id: str) -> Any:
    return await call(s, f"make/cancel/{make_id}")


# 使用中状态动作（均无需 id、body 空，服务端按当前使用中的预约操作）
async def make_leave(s: "Session") -> Any:
    return await call(s, "make/leave")


async def make_check_in(s: "Session") -> Any:
    return await call(s, "make/checkIn", params={"qrMd5": "PC"})


async def make_stop(s: "Session") -> Any:
    return await call(s, "make/stop")


async def find_common_seat(s: "Session") -> Any:
    return await call(s, "res/findCommonSeat")


async def breach(s: "Session", page: int = 1, page_size: int = 20) -> Any:
    return await call(s, f"user/breach/{page}/{page_size}")


async def history(s: "Session", page: int = 1, page_size: int = 20) -> Any:
    return await call(s, f"user/history/{page}/{page_size}")


async def door_log(s: "Session", date: str) -> Any:
    return await call(s, f"user/doorLog/{date}")
