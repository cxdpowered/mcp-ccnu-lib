# mcp-ccnu-lib

华中师范大学图书馆**空间预约 MCP 服务**。它把"登录图书馆 → 查座位 → 约/取消 → 暂离/回座/退座"这一整套网页操作，封装成一组可被 LLM 调用的 MCP 工具。

技术上：**纯 HTTP 实现，无浏览器**——直接复刻 CAS SSO 登录链拿到会话，再直连后端 JSON API（Python 现算 HMAC 签名）完成查询与预约。登录态靠持久化 cookie（CASTGC）静默续登保活。

**关键约束**

- 真实预约系统：`https://kjyy.ccnu.edu.cn/jsq-v/`（Vue SPA，统一身份认证 CAS 登录）。
- **仅校园网内可访问** —— 部署机器必须在校园网内，否则页面不可达。
- 验证码 / 短信验证走**人工输入**（截图 base64 回传给用户看），不做自动识别。

---

## 1. 职责边界

本服务只是**预约执行器**，不理解自然语言、不主动提醒用户、不做定时调度。那些由调用方（小青团）负责。

| | 小青团（MCP client） | mcp-ccnu-lib（本服务） |
|---|---|---|
| 理解用户意图、管理身份映射 | ✅ | ❌ |
| 保存预约计划 / 偏好 / 提醒策略 | ✅ | ❌ |
| 定时触发预约、签到前提醒、超时自动退座 | ✅ | ❌ |
| 判断哪些动作需要确认 / 预授权 | ✅ | ❌ |
| 保存图书馆登录态（cookie+token）、账号 | ❌ | ✅ |
| 处理验证码 / 短信挑战 | ❌ | ✅ |
| 查座位、提交预约、取消、暂离/回座/退座 | ❌ | ✅ |

一句话：**小青团决策，本服务执行**。高风险动作（约/取消/退座等）由小青团做确认与预授权，本服务只负责把动作真实落到网站上。

---

## 2. 部署（Docker，推荐）

镜像基于轻量 `python:3.12-slim`（纯 HTTP，无 Chromium，体积小），本地 `docker compose` 一键构建运行。

### 2.1 服务器目录建议

把本仓库克隆到服务器，`.env` 与 `data/` 都落在仓库目录里，便于备份与迁移：

```text
ccnu-library-mcp/              # 克隆下来的本仓库
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── src/                       # 服务代码
├── .env                       # 你创建：账号等机密（不入库）
└── data/                      # 首次启动自动生成，持久化登录态
    ├── ccnu_lib.sqlite        # accounts / sessions / challenges
    └── users/<user_key>/
        ├── session.json       # cookie(含 CASTGC) + app token，跨重启静默 SSO 保活
        └── screenshots/       # 验证码图
```

### 2.2 初始化

```bash
git clone <本仓库地址> ccnu-library-mcp
cd ccnu-library-mcp

cp .env.example .env
# 按下方模板编辑 .env（账号可选；也可启动后用 save_account 工具传入）
```

### 2.3 生产 .env 最小模板

```bash
MCP_HOST=0.0.0.0
MCP_PORT=8010
DATA_DIR=/data
DEFAULT_USER_KEY=default

# 验证码人工输入超时秒数
CHALLENGE_TTL_SECONDS=240

# 可选：default 测试用户的默认账号（留空则用 save_account 工具传入）
CCNU_DEFAULT_USERNAME=你的学号
CCNU_DEFAULT_PASSWORD=你的密码
```

> `docker-compose.yml` 会用 `environment` 覆盖 `DATA_DIR`、`MCP_*` 等非机密项，所以 `.env` 里真正必须填的只有账号。登录入口（CAS/VUE 地址）运行时从后端 `getSysSet` 取，无需在 `.env` 配。

### 2.4 启动

```bash
docker compose up -d --build

docker compose logs -f          # 看启动日志
docker compose ps               # 确认容器 healthy
```

服务监听 `http://<服务器IP>:8010/mcp`（传输 streamable-http）。

`data/` 已挂载为 volume，重启容器免重新登录（session.json 里的 CASTGC 支持静默 SSO）。升级代码后重新 `docker compose up -d --build` 即可，登录态保留。

### 2.5 本地开发（不走 Docker）

```bash
pip install -e .                            # httpx + cryptography + mcp，无浏览器
cp .env.example .env
python -m ccnu_lib.server                   # 监听 0.0.0.0:8010/mcp
```

> 本地用 MCP client 连 `127.0.0.1` 时若设了系统代理，需 `NO_PROXY=127.0.0.1,localhost`，否则 502。

---

## 3. MCP 接入（小青团侧）

传输为 **streamable-http**，端点 `http://<host>:8010/mcp`。

```json
{
  "mcpServers": {
    "ccnu_library": {
      "transport": "http",
      "url": "http://ccnu-library-mcp:8010/mcp",
      "tool_prefix": "ccnu_",
      "high_risk_tools": [
        "save_account", "reserve_seat", "cancel_reservation",
        "start_temporary_leave", "return_from_temporary_leave",
        "end_reservation_early"
      ]
    }
  }
}
```

### 3.1 user_key（多用户隔离）

所有工具都接受可选 `user_key`。**小青团应传 `user_key = person_id`**，这样一个人的 QQ / 微信 / CLI 共用同一份图书馆登录态与偏好。不传则用 `default`（仅测试）。每个 `user_key` 独立保存 session.json（cookie+token）与账号。

### 3.2 challenge 交互（关键机制）

challenge 不是登录专属，而是贯穿所有工具的**通用中断模型**：任何动作执行到一半遇到图形验证码时，工具**不阻塞**，而是挂起当前登录流程并返回：

```json
{
  "ok": false, "code": "NEED_CHALLENGE",
  "challenge_id": "ch_xxx", "challenge_type": "captcha",
  "prompt": "请输入图形验证码",
  "image_base64": "data:image/jpeg;base64,....",   // captcha 才有（CAS 验证码 jpg）
  "phone_hint": "尾号1234",                         // sms 才有（纯 HTTP 下短信暂未实现）
  "expires_at": "2026-06-25T12:30:00+08:00"
}
```

小青团应：把 `image_base64` 原样发给用户看 → 收集用户输入 → 调 `submit_challenge(challenge_id, answer)` 续跑同一个流程，直到 `logged_in:true` 或原动作完成。

- `challenge_type` 取值：`captcha`（图形）/ `sms`（短信）/ `confirm_send_sms`（提交任意值确认发短信）/ `manual_login`。
- challenge 有超时（默认 240s），过期需重新发起原动作。
- 同一 `user_key` 同一时刻只应有一个挂起 challenge；存在挂起时再发起新动作会返回 `CHALLENGE_PENDING`。
- 内存中的挂起会话在进程重启后丢失，所有 `pending` challenge 视为失效，需重新发起。

---

## 4. 工具 API（全量）

### 4.1 通用约定

- **`user_key`**：所有工具都接受可选 `user_key`，不传则用 `default`（见 [§3.1](#31-user_key多用户隔离)）。下文各工具不再重复列出此参数。
- **时间**：`date` = `YYYY-MM-DD`，`start_time` / `end_time` = `HH:MM`（步长 30 分钟，范围约 07:30–22:00，`start_time` 必须早于 `end_time`）。
- **返回**：所有工具返回一个对象，必含 `ok`（bool）。失败时 `ok:false` 且含 `code`（机器可判）+ `message`（人读）。下文"错误码"列的是该工具特有的 `code`。
- **登录前置**：除登录类工具外，所有工具内部会先确保登录态（有 token 直接用，否则尝试 SSO 复登）；仍未登录则返回 `NEED_LOGIN`，调用方应转去 `start_login`。

### 4.2 速查表

| 工具 | 主要参数 | 说明 | 高风险 |
|---|---|---|---|
| `save_account` | username, password, phone_hint?, login_now? | 保存账号（明文，个人自用），覆盖旧值 | ✅ |
| `get_login_status` | — | 实地探测登录态（含自动 SSO 复登） | |
| `start_login` | username?, password? | 发起登录，可能返回 `NEED_CHALLENGE` | |
| `submit_challenge` | challenge_id, answer | 提交验证码/短信，续跑挂起流程 | |
| `get_availability_distribution` | date, start_time, end_time, library?, area_filter? | 各区域可用座位分布（按空闲降序） | |
| `list_available_seats` | date, start_time, end_time, **location_id**, area_filter?, limit? | 某区域具体可用座位 | |
| `get_seat_layout` | **location_id** | 区域座位平面图（网页版布局透传 + 扁平座位表） | |
| `reserve_seat` | date, start_time, end_time, seat_id?, location_id?, strategy? | 提交预约 | ✅ |
| `cancel_reservation` | reservation_id? | 取消未开始预约（缺省取当前有效） | ✅ |
| `get_current_reservation` | — | 当前预约 + 状态 + 暂离详情 | |
| `start_temporary_leave` | reservation_id? | 暂离（网页按钮镜像；正常暂离过闸机自动，见下） | ✅ |
| `return_from_temporary_leave` | reservation_id? | 回座（同上，正常回座过闸机自动） | ✅ |
| `end_reservation_early` | reservation_id? | 提前结束/退座（区别于取消未开始） | ✅ |
| `get_site_favorite_locations` | — | 网站常用/收藏座位 | |
| `get_violation_records` | page?, page_size? | 违约记录（爽约/暂离超时等），分页 | |
| `get_reservation_history` | page?, page_size? | 历史预约记录（过往全部），分页 | |
| `get_door_log` | **date** | 某天闸机进出记录（可靠判断在馆/暂离） | |

---

### 登录类

#### `save_account`

保存图书馆账号密码（明文，个人自用），同 `user_key` 已存在则覆盖。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `username` | str | 否¹ | `.env` 的 `CCNU_DEFAULT_USERNAME` | 学号 |
| `password` | str | 否¹ | `.env` 的 `CCNU_DEFAULT_PASSWORD` | 密码 |
| `phone_hint` | str | 否 | — | 手机号尾号提示，仅留存 |
| `login_now` | bool | 否 | `false` | 为 `true` 时保存后立即触发 `start_login`，返回其结果 |

> ¹ `username`/`password` 入参缺省时回落到 `.env` 默认账号；两边都没有则报错。

**成功**：`{ "ok": true, "user_key": "...", "message": "账号已保存" }`（`login_now:true` 时返回 `start_login` 的结果）
**错误码**：`MISSING_CREDENTIALS`（无账号且无 .env 默认）

#### `get_login_status`

打开预约页实地探测登录态（被打回 CAS 即未登录），并刷新落盘 cookie。

**成功**：
```json
{ "ok": true, "user_key": "...", "logged_in": true,
  "status": "logged_in", "needs_challenge": false, "message": "当前登录态可用" }
```
`status` 取值：`no_account` / `logged_in` / `logged_out` / `error`。

#### `start_login`

发起登录。可带账号密码（会先保存）。遇验证码/短信时**不阻塞**，返回 `NEED_CHALLENGE` 挂起。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `username` | str | 否 | 传入则先保存再登录 |
| `password` | str | 否 | 同上 |

**成功**：`{ "ok": true, "user_key": "...", "status": "logged_in", "logged_in": true, "message": "登录成功" }`
**挂起（需人工）**：
```json
{ "ok": false, "code": "NEED_CHALLENGE",
  "challenge_id": "ch_xxx", "challenge_type": "captcha",
  "prompt": "请输入图形验证码",
  "image_base64": "data:image/png;base64,....",
  "phone_hint": "尾号1234",
  "expires_at": "2026-06-25T12:30:00+08:00" }
```
`challenge_type`：`captcha` / `sms` / `confirm_send_sms` / `manual_login`（见 [§3.2](#32-challenge-交互关键机制)）。
**错误码**：`CHALLENGE_PENDING`（已有挂起 challenge）、`NO_ACCOUNT`、`NAV_FAILED`、`AUTH_UNSETTLED`、`LOGIN_FORM_NOT_FOUND`、`LOGIN_FAILED`（账号/验证码错，验证码已刷新需重发起）

#### `submit_challenge`

提交验证码/短信码，续跑被挂起的登录流程。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `challenge_id` | str | 是 | `NEED_CHALLENGE` 返回的 id |
| `answer` | str | 是 | 用户输入；`confirm_send_sms` 类型传任意值表示确认发短信 |

**成功**：返回登录成功结构（同 `start_login` 成功），或**下一个** `NEED_CHALLENGE`（如确认发短信后转入 `sms`）。
**错误码**：`CHALLENGE_NOT_FOUND`、`CHALLENGE_NOT_PENDING`、`SESSION_LOST`（挂起会话超时/重启丢失，需重发起）、`SUBMIT_FAILED`、`STILL_NOT_LOGGED_IN`（manual_login 仍未完成）、`LOGIN_FAILED`

---

### 查询类

#### `get_availability_distribution`

返回各区域可用座位分布，按 `available` 降序。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `date` / `start_time` / `end_time` | str | 是 | 见通用约定 |
| `library` | str | 否 | 按建筑名包含过滤 |
| `area_filter` | str | 否 | 按区域名包含过滤 |

**成功**：
```json
{ "ok": true, "date": "2026-06-25", "start_time": "08:00", "end_time": "12:00",
  "locations": [
    { "location_id": "...", "path": ["逸夫图书馆","2F","安静区"],
      "total": 120, "available": 34, "max_minute": 240 }
  ] }
```
**错误码**：`BAD_TIME`、`NEED_LOGIN`

#### `list_available_seats`

列出某区域指定时段的具体可用座位。

| 参数 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `date` / `start_time` / `end_time` | str | 是 | — | 见通用约定 |
| `location_id` | str | **是** | — | 由 `get_availability_distribution` 取得 |
| `area_filter` | str | 否 | — | 按座位 label 包含过滤 |
| `limit` | int | 否 | 50 | 返回上限 |

**成功**：
```json
{ "ok": true, "date": "2026-06-25", "location_id": "...",
  "seats": [ { "seat_id": "...", "seat_no": "312", "name": "...",
              "location_id": "...", "available": true } ] }
```
**错误码**：`BAD_TIME`、`NEED_LOCATION`、`NEED_LOGIN`

#### `get_current_reservation`

查询当前**有效**预约及状态。只在存在活跃预约（`reserved` / `waiting_sign_in` / `in_use` / `away` / `violation_risk`）时返回座位明细；否则返回干净的 `none`，**不会回显已取消/已结束那条的座位信息**——调用方判断"有没有座位"一律看 `status`，不要看 `seat_no`。

**成功（无有效预约）**：`{ "ok": true, "status": "none", "message": "当前无有效预约", "last_raw_status": "CANCEL" }`（`last_raw_status` 仅排查用，为最近一条记录的后端原始状态，可能不存在）
**成功（有预约）**：
```json
{ "ok": true, "reservation_id": "...", "status": "away", "raw_status": "AWAY",
  "seat_no": "312", "path": ["逸夫图书馆","2F","安静区"],
  "date": "2026-06-25", "start_time": "08:00", "end_time": "12:00",
  "receipt": "...", "raw_text": "...",
  "away_deadline": "2026-06-25T10:15:00+08:00",
  "away_raw": { "...": "..." },
  "actual_begin": "...", "actual_end": null }
```
`status` 枚举：`none` / `reserved` / `waiting_sign_in` / `in_use` / `away` / `ended` / `cancelled` / `violation` / `violation_risk` / `unknown`。`away_*` 仅暂离/使用中时出现。
- `ended`：正常用完 / 主动结束 / 管理员改判完成（后端 raw `STOP`）。
- `violation`：**已发生的违约**（后端 raw `MISS` 爽约未签到 / `LEAVE_EARLY` 暂离超时未归）。终态，不算活跃预约。
- `violation_risk`：进行中的违约风险（raw `BREAK`/`VIOLATION` 等），仍算活跃。

**错误码**：`NEED_LOGIN`

#### `get_site_favorite_locations`

返回网站里的常用/收藏座位（原始结构，含 `roomId`/`label`/`buildName`/`floorName`/`roomName` 等）。**成功**：`{ "ok": true, "favorites": [...] }`；**错误码**：`NEED_LOGIN`

#### `get_seat_layout`

透传网页版某区域的座位平面布局（Fabric.js 画布数据），并附一份扁平座位表方便辅助选座。参数 `location_id`（必填，取自 `get_availability_distribution`）。

```json
{ "ok": true, "location_id": "1995338594917990400", "layout_version": "15",
  "seat_count": 172,
  "seats": [ { "seat_id": "2003722571020177408", "seat_no": "N1167",
              "name": "1行1列", "direction": "top", "power": true,
              "x": 33.0, "y": 148.0 } ],
  "layout": { "version": "5.2.1", "objects": [...], "backgroundImage": {...} } }
```

- `seats`：从画布抽出的扁平座位表（`seat_id`/`seat_no`/`name` 行列/`direction`/`power` 是否有电源/`x,y` 画布坐标）——小青团可直接据此辅助选座，无需解析画布。
- `layout`：网页版原始 Fabric 画布对象（已剥掉后端尾部 `_updVersion_N` 标记并解析成对象），需要在前端渲染座位图时用；纯文字场景用 `seats` 即可。
- 注意：该布局是**静态座位表**，不含实时空闲状态；查空闲仍用 `list_available_seats`（可与 `seats` 按 `seat_id` 关联）。

**错误码**：`NEED_LOCATION`、`NEED_LOGIN`

#### `get_violation_records` / `get_reservation_history`

分页拉取违约记录 / 历史预约记录。参数 `page`（默认 1）、`page_size`（默认 20）。

```json
{ "ok": true, "page": 1, "page_size": 20, "total": 15, "count": 15,
  "records": [ { /* 同 get_current_reservation 的记录结构：reservation_id/status/raw_status/seat_no/path/date/start_time/end_time/receipt/raw_text */ } ] }
```

- `total` = 后端总条数（用于分页），`count` = 本页返回条数。
- 每条 `record` 复用统一映射：违约记录里 `raw_text` 常带原因（如"用户暂离超时未归, 预约释放"），`status` 映射为 `violation`；历史里正常完成的为 `ended`、取消的为 `cancelled`。
- 小青团可用 `get_violation_records` 的 `total` 做违约次数提醒。

**错误码**：`NEED_LOGIN`

#### `get_door_log`

某天(`date`=`yyyy-MM-dd`，必填)的闸机进出记录。

```json
{ "ok": true, "date": "2026-07-01", "count": 2, "in_building": false,
  "events": [ { "time": "2026-07-01 15:17:14", "gate": "南湖二楼中出", "direction": "out", "raw_direction": 1 },
              { "time": "2026-07-01 15:12:44", "gate": "南湖二楼右入", "direction": "in", "raw_direction": 0 } ] }
```

- `events` 按时间**倒序**（首条最新）；`direction`：`in`(进=`raw_direction 0`) / `out`(出=`1`)。
- `in_building`：由首条方向推断当前是否在馆（`true`=在馆）。
- **为何需要**：实测**物理暂离（走闸机出馆）不会实时改预约状态**——`get_current_reservation` 仍是 `in_use`、`awayTimeM` 仍 0（系统疑似回馆才结算离馆时长）。所以小青团判断"用户在不在馆/是否暂离"要靠这个接口，不能只看预约状态。

**错误码**：`NEED_LOGIN`

---

### 预约类（高风险）

#### `reserve_seat`

提交预约。`strategy` 缺省：传了 `seat_id` 则 `exact_seat`，否则 `first_available`。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `date` / `start_time` / `end_time` | str | 是 | 见通用约定 |
| `seat_id` | str | 否 | `exact_seat` 必填 |
| `location_id` | str | 否 | 非 `exact_seat` 策略必填（用于自动选座） |
| `strategy` | str | 否 | 见下 |

`strategy` 取值：
- `exact_seat` —— 必须约指定 `seat_id`
- `first_available` —— 区域内第一个可用（需 `location_id`，默认）
- `random_available` —— 可用中随机（需 `location_id`）
- `favorite_first` —— 收藏座位优先，无收藏命中则退回第一个可用（需 `location_id`）

**成功**：
```json
{ "ok": true, "reservation_id": "...", "status": "reserved", "raw_status": "RESERVE",
  "seat_no": "312", "path": ["逸夫图书馆","2F","安静区"],
  "date": "2026-06-25", "start_time": "08:00", "end_time": "12:00",
  "receipt": "...", "raw_text": "..." }
```
**错误码**：`BAD_TIME`、`NEED_LOGIN`、`NEED_LOCATION`、`NO_SEAT`（该时段无空闲）、`RESERVE_FAILED`

#### `cancel_reservation`

取消预约。不传 `reservation_id` 时自动取当前活跃预约。

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `reservation_id` | str | 否 | 缺省取当前活跃预约 |

**成功**：`{ "ok": true, "reservation_id": "...", "status": "cancelled" }`
**错误码**：`NEED_LOGIN`、`NO_ACTIVE`（无可取消）、`CANCEL_FAILED`

#### `start_temporary_leave` / `return_from_temporary_leave` / `end_reservation_early`

在馆状态操作，均对**当前预约**生效。

| 工具 | 成功 `status` | 成功附加 | 错误码 |
|---|---|---|---|
| `start_temporary_leave` | `away` | `away_deadline` / `away_raw`（暂离截止） | `NEED_LOGIN`、`LEAVE_FAILED` |
| `return_from_temporary_leave` | `in_use` | — | `NEED_LOGIN`、`RETURN_FAILED` |
| `end_reservation_early` | `ended` | — | `NEED_LOGIN`、`END_FAILED` |

成功结构：`{ "ok": true, "status": "away", "message": "...", "raw": {...} }`（`raw` 为后端原始返回，若有）。

> **重要（真机确认）**：**暂离 / 回座 实际是过闸机自动触发的**——走出馆=自动暂离、走回馆=自动回座，用户根本不点按钮。因此：
> - `start_temporary_leave` / `return_from_temporary_leave` 只是网页手动按钮的镜像，**不是暂离/回座的正常路径**，小青团一般不用调；且物理暂离**不会实时改预约状态**（`get_current_reservation` 仍显示 `in_use`、`awayTimeM` 仍 0）。
> - **要判断用户是否暂离/已回座，用 `get_door_log` 轮询**（最后一条 `out`=已出馆暂离，`in`=在馆），而不是看预约状态。
> - `end_reservation_early`（`make/stop` 提前退座）**已真机验证可用**：在馆外 `in_use` 时调用会正常结束（终态 `ended`，不产生违约）。
>
> 三个工具与 `cancel_reservation` 都接受可选 `reservation_id`，但当前实现以"当前预约"为准操作，`reservation_id` 主要用于调用方自查比对。

---

### 对接交互流程（小青团侧，已真机实测 2026-06-30）

下面是把本服务真正驱动起来的完整交互方式。两条闭环都已在校园网真机跑通（登录含图形验证码、预约 / 取消 / 重订）。

#### A. 登录闭环（带 challenge 的通用模式）

登录态会过期；任何业务工具内部会先尝试静默 SSO 复登，复登失败才返回 `NEED_LOGIN`，此时转登录闭环：

```text
start_login
  └─ ok:true                      → 已登录，继续业务
  └─ NEED_CHALLENGE(captcha/sms)  → 把 image_base64 发给用户看，收用户输入
        └─ submit_challenge(challenge_id, answer)
              └─ ok:true                → 登录成功
              └─ NEED_CHALLENGE(...)    → 还有下一步挑战（如确认发短信→输短信码），循环
              └─ LOGIN_FAILED           → 账号/验证码错；验证码已刷新，需重新 start_login
```

要点（实测结论）：
- **挑战图就是验证码那一小张**（已裁剪，非整页），`image_base64` 原样转发用户即可。
- 同一 `user_key` 同一时刻只允许一个挂起 challenge；存在挂起时其它动作返回 `CHALLENGE_PENDING`。
- 挂起态在内存，**服务进程重启即失效**，需重新发起；challenge 也有 `CHALLENGE_TTL_SECONDS` 超时。
- 刚登录过、cookie 新鲜时，下次通常能静默 SSO 免验证码。

#### B. 预约 / 取消闭环

```text
get_availability_distribution(date,start,end, library?/area_filter?)  → 选区域 location_id
list_available_seats(location_id)                                     → 选 seat_id（或交给 strategy 自动选）
reserve_seat(seat_id 或 location_id+strategy)                         → 返回 reservation_id / seat_no / receipt
get_current_reservation()                                             → 轮询状态、读签到窗口、暂离提醒
cancel_reservation()           # 不传 id 自动取当前活跃预约（已实测）
  或 end_reservation_early()    # 使用中提前退座
```

关键字段语义（供小青团做提醒/判断）：
- `reserve_seat` / `get_current_reservation` 成功结构里的 **`raw_text` 携带签到窗口**，如 `"请在 07:30 至 08:30 之间完成签到"`——小青团据此设签到提醒。
- 判断"当前有没有座位"**只看 `status`**：`none` 即无有效预约（此时不应读 `seat_no`/`receipt`，那些字段已被清掉）。
- `seat_no` 形如 `N1143`（馆区前缀 + 4 位），`receipt` 形如 `3533-0072-N1143`，每次预约都会变。
- 取消后该预约 `raw_status` 变 `CANCEL`，`get_current_reservation` 即回 `status:none`。

---

## 5. 环境变量（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | 0.0.0.0 / 8010 | 服务监听 |
| `DATA_DIR` | ./data | 数据目录（容器内为 `/data`） |
| `DEFAULT_USER_KEY` | default | 不传 user_key 时的兜底键 |
| `CHALLENGE_TTL_SECONDS` | 240 | 验证码人工输入超时 |
| `CCNU_DEFAULT_USERNAME` / `_PASSWORD` | — | 可选：default 用户默认账号 |

---

## 6. 数据与安全

- 第一版按**个人自用**实现：账号密码明文保存，无加密 / 审计 / 权限系统。
- `.env`、`data/`、`scripts/_probe_out/` 已在 `.gitignore` 与 `.dockerignore`，**不入库、不进镜像**。
- 同一 `user_key` 同一时刻只允许一个活动任务（`Session.lock` 串行化），避免两个定时任务同时操作同一登录态。
- 数据目录结构见 [§2.1](#21-服务器目录建议)。

---

## 7. 已知边界

- 仅校园网内可用；登录态靠持久化 cookie(CASTGC) 静默 SSO 保活，失效时重新触发图形验证码登录。异地登录风控的短信验证在纯 HTTP 下**尚未实现**（真机触发后再补）。
- 签到 / 暂离 / 回座物理上由进出馆闸机自动触发（真机确认）：进馆自动签到、出馆自动暂离、再进馆自动回座，用户不点按钮。且**物理暂离不实时改预约状态**——判断在馆/暂离用 `get_door_log` 轮询，别看 `get_current_reservation` 状态。网页手动按钮的镜像 `start_temporary_leave` / `return_from_temporary_leave` 仅备用，正常流程用不到；`end_reservation_early`（提前退座）已真机验证可用。
- 续约官方要求到现场预约终端刷卡，网页端无入口，本服务不提供。
- 违约红线：每月 3 次违约进黑名单一周。自动取消 / 退座的预授权与确认由小青团控制，**本服务只执行，不判断**。

---

## 8. 验证脚本（scripts/）

| 脚本 | 用途 |
|---|---|
| `smoke_mcp.py` | 通过 MCP 协议冒烟（连服务、列工具、跑只读流程） |
| `try_login.py` / `try_api.py` / `try_reserve.py` / `probe_*.py` / `dump_signing.py` | ⚠️ **Playwright 时代**的登录/鉴权/接口反推脚本，纯 HTTP 重构后已失效，仅作历史参考 |

> 重构后真机验证改用临时脚本直接调 `login.start_login` / `submit_challenge` / `reservations.*`（走文件握手输验证码）。

---

## 9. 免责声明

- 本项目仅供**个人学习与自用**，用于自动化操作本人在华中师范大学图书馆的合法预约权限，不提供任何绕过学校系统规则的能力（不做验证码自动识别、不做虚假签到、不做位置伪造）。
- 使用者须对自己的账号行为负责，遵守学校图书馆的预约与违约规则。因使用本项目导致的违约、封禁或其他后果，由使用者自行承担，作者不承担任何责任。
- 本项目与华中师范大学及其图书馆无任何隶属或合作关系，所涉站点与接口的所有权归校方所有。学校系统若调整，本项目可能随时失效。

## 10. 许可证

[MIT](LICENSE) © 2026 mcp-ccnu-lib contributors
