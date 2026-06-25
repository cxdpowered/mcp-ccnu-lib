"""kjyy.ccnu.edu.cn/jsq 后端 API 直连客户端。

用已登录会话的 context.request 发起 POST（自动带 rem_JSESSIONID cookie）。
若后端还要求 token 头，会从页面 localStorage 尽力取出附加。
所有接口返回统一信封 {status, code, message, data}，本模块解包返回 data。
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from .browser import Session
from .config import settings

JSQ = "https://kjyy.ccnu.edu.cn/jsq/static"


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


# ---------- 底层调用 ----------
# 在已登录页面内同源 fetch：自动带 rem_JSESSIONID cookie；token 从 localStorage
# 尽力取出附加为 header。比独立 APIRequestContext 更稳（后者在 Win 上会崩驱动）。
# 复刻 SPA axios 拦截器的请求签名（见 app.*.js）：
#   msg = "seat::" + uuid + "::" + ms + "::" + METHOD
#   key = AES-CBC-decrypt(systemInfo.hmacKey, key="server_date_time", iv="client_date_time")
#   X-hmac-request-key = HMAC-SHA256(msg, key).hex
# 用页面已加载的 window.CryptoJS 现算，密钥运行时从 sessionStorage 解出，不硬编码。
_JS_CALL = """
async ({url, body}) => {
  function uuid(){
    const t=[]; for(let e=0;e<36;e++) t[e]="0123456789abcdef".substr(Math.floor(16*Math.random()),1);
    t[14]="4"; t[19]="0123456789abcdef".substr(3&t[19]|8,1); t[8]=t[13]=t[18]=t[23]="-";
    return t.join("");
  }
  const token = sessionStorage.getItem('jsq_p-token') || '';
  const headers = {'Content-Type':'application/json', 'token':token, 'loginType':'PC'};
  try {
    const A = JSON.parse(sessionStorage.getItem('jsq_p-systemInfo') || '{}');
    if (A && A.hmac == 1) {
      const CJ = window.CryptoJS;
      if (!CJ) return {signError: 'window.CryptoJS 不可用'};
      const id = uuid(), date = Date.now();
      const msg = "seat::" + id + "::" + date + "::POST";
      const dec = CJ.AES.decrypt(A.hmacKey, CJ.enc.Utf8.parse("server_date_time"),
                   {iv: CJ.enc.Utf8.parse("client_date_time"),
                    mode: CJ.mode.CBC, padding: CJ.pad.Pkcs7});
      const realKey = CJ.enc.Utf8.stringify(dec).toString();
      const sig = CJ.HmacSHA256(msg, realKey).toString();
      headers['X-request-id']=id; headers['X-request-date']=String(date);
      headers['X-hmac-request-key']=sig;
    }
  } catch(e) { return {signError: String(e)}; }
  let resp, text;
  try {
    resp = await fetch(url, {method:'POST', credentials:'include',
                            headers, body: JSON.stringify(body||{})});
    text = await resp.text();
  } catch(e) { return {fetchError: String(e)}; }
  let js=null; try { js=JSON.parse(text); } catch(e){}
  return {httpStatus: resp.status, text, js, hadToken: !!token};
}
"""


async def call(session: Session, path: str, *, data: Any = None,
               params: dict | None = None) -> Any:
    """页面内 POST {JSQ}/frontApi/{path}，解包返回 data。path 形如 'res/buildingFloorDate'。"""
    # 确保页面在 kjyy 同源，否则 fetch 带不上 cookie
    if "kjyy.ccnu.edu.cn" not in (session.page.url or ""):
        await session.page.goto(settings.base_url, wait_until="domcontentloaded")
    url = f"{JSQ}/frontApi/{path}"
    if params:
        url += "?" + urlencode(params)
    r = await session.page.evaluate(_JS_CALL,
                                    {"url": url, "body": data if data is not None else {}})
    if r.get("signError"):
        raise ApiError("SIGN", r["signError"], path)
    if r.get("fetchError"):
        raise ApiError("FETCH", r["fetchError"], path)
    js = r.get("js")
    if js is None:
        raise ApiError(r.get("httpStatus"), f"非 JSON 响应: {(r.get('text') or '')[:200]}", path)
    if not js.get("status"):
        hint = "" if r.get("hadToken") else "（sessionStorage 无 jsq_p-token，可能未登录）"
        raise ApiError(js.get("code"), f"{js.get('message', '失败')}{hint}", path)
    return js.get("data")


# ---------- 接口封装 ----------
async def get_user_info(s: Session) -> dict:
    return await call(s, "user/getUserInfo")


async def building_floor_date(s: Session) -> dict:
    return await call(s, "res/buildingFloorDate")


async def find_room_duration(s: Session, building_id: str, date: str,
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


async def free_seats(s: Session, room_id: str, date: str,
                     begin_min: int, end_min: int) -> dict:
    body = {"beginMinute": begin_min, "endMinute": end_min, "minMinute": 0}
    return await call(s, f"res/freeSeatIdsDuration/{room_id}/{date}", data=body)


async def query_seat_layout(s: Session, room_id: str) -> Any:
    return await call(s, f"res/querySeatLayout/{room_id}/0")


async def free_book(s: Session, seat_id: str, date: str,
                    begin_min: int, end_min: int) -> dict:
    return await call(
        s, f"make/freeBook/{seat_id}/{date}/{begin_min}/{end_min}",
        params={"capToken": "capToken"},
    )


async def current_use_make(s: Session) -> Any:
    return await call(s, "user/currentUseMake")


async def last_make(s: Session) -> Any:
    return await call(s, "user/lastMake")


async def make_life(s: Session, make_id: str) -> Any:
    return await call(s, f"user/makeLife/{make_id}")


async def cancel(s: Session, make_id: str) -> Any:
    return await call(s, f"make/cancel/{make_id}")


# 使用中状态的动作（均无需 id、body 空，服务端按当前使用中的预约操作）
async def make_leave(s: Session) -> Any:
    """暂离。"""
    return await call(s, "make/leave")


async def make_check_in(s: Session) -> Any:
    """签到 / 回座（checkIn 兼任两者，成功提示"返回成功"）。"""
    return await call(s, "make/checkIn", params={"qrMd5": "PC"})


async def make_stop(s: Session) -> Any:
    """提前结束 / 退座（成功提示"结束使用成功"）。"""
    return await call(s, "make/stop")


async def find_common_seat(s: Session) -> Any:
    """网站常用/收藏座位。"""
    return await call(s, "res/findCommonSeat")
