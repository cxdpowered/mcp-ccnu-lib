# mcp-ccnu-lib

华中师范大学图书馆**空间预约 MCP 服务**。它把"登录图书馆 → 查座位 → 约/取消 → 暂离/回座/退座"这一整套网页操作，封装成一组可被 LLM 调用的 MCP 工具。

技术上：Playwright 持久化浏览器负责登录与保活，登录后直连后端 JSON API（HMAC 签名）完成查询与预约。

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
| 保存图书馆登录态、账号、Playwright profile | ❌ | ✅ |
| 处理验证码 / 短信挑战 | ❌ | ✅ |
| 查座位、提交预约、取消、暂离/回座/退座 | ❌ | ✅ |

一句话：**小青团决策，本服务执行**。高风险动作（约/取消/退座等）由小青团做确认与预授权，本服务只负责把动作真实落到网站上。

---

## 2. 部署（Docker，推荐）

镜像基于 Playwright 官方 Python 镜像（已内置 Chromium 与系统依赖），本地 `docker compose` 一键构建运行。

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
        ├── profile/           # Playwright persistent context
        ├── cookies.json       # 会话 cookie，跨重启 SSO 保活
        └── screenshots/       # 验证码截图
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
HEADLESS=true
DEFAULT_USER_KEY=default

# 真实空间预约 SPA 入口。URL 里的 # 是 hash 路由的一部分，不是注释 —— 必须加引号
CCNU_BASE_URL="https://kjyy.ccnu.edu.cn/jsq-v/#/main/home"

# 验证码 / 短信人工输入超时秒数
CHALLENGE_TTL_SECONDS=240

# 可选：default 测试用户的默认账号（留空则用 save_account 工具传入）
CCNU_DEFAULT_USERNAME=你的学号
CCNU_DEFAULT_PASSWORD=你的密码
```

> `docker-compose.yml` 会用 `environment` 覆盖 `DATA_DIR`、`HEADLESS`、`MCP_*`、`CCNU_BASE_URL` 等非机密项（避免 env_file 对带 `#` 的 URL 处理不一致），所以 `.env` 里真正必须填的只有账号。其余项按上面模板写齐即可，互不冲突。

### 2.4 启动

```bash
docker compose up -d --build

docker compose logs -f          # 看启动日志
docker compose ps               # 确认容器 healthy
```

服务监听 `http://<服务器IP>:8010/mcp`（传输 streamable-http）。

`data/` 已挂载为 volume，重启容器免重新登录。升级代码后重新 `docker compose up -d --build` 即可，登录态保留。

> ⚠️ **Playwright 版本一致**：`pyproject.toml` 的 `playwright==1.60.0` 必须与 `Dockerfile` 的镜像 tag `v1.60.0-noble` 完全一致，升级时两处同步改，否则浏览器二进制对不上会崩。

### 2.5 本地开发（不走 Docker）

```bash
pip install -e .
python -m playwright install chromium      # 本地首次需要；Docker 镜像已内置
cp .env.example .env                        # 可设 HEADLESS=false 看浏览器
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

所有工具都接受可选 `user_key`。**小青团应传 `user_key = person_id`**，这样一个人的 QQ / 微信 / CLI 共用同一份图书馆登录态与偏好。不传则用 `default`（仅测试）。每个 `user_key` 独立保存 profile、cookies、账号。

### 3.2 challenge 交互（关键机制）

challenge 不是登录专属，而是贯穿所有工具的**通用中断模型**：任何动作执行到一半遇到图形验证码 / 短信时，工具**不阻塞**，而是挂起当前浏览器页面并返回：

```json
{
  "ok": false, "code": "NEED_CHALLENGE",
  "challenge_id": "ch_xxx", "challenge_type": "captcha",
  "prompt": "请输入图形验证码",
  "image_base64": "data:image/png;base64,....",   // captcha 才有
  "phone_hint": "尾号1234",                         // sms 才有
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
| `reserve_seat` | date, start_time, end_time, seat_id?, location_id?, strategy? | 提交预约 | ✅ |
| `cancel_reservation` | reservation_id? | 取消未开始预约（缺省取当前有效） | ✅ |
| `get_current_reservation` | — | 当前预约 + 状态 + 暂离详情 | |
| `start_temporary_leave` | reservation_id? | 暂离 | ✅ |
| `return_from_temporary_leave` | reservation_id? | 回座 | ✅ |
| `end_reservation_early` | reservation_id? | 提前结束/退座（区别于取消未开始） | ✅ |
| `get_site_favorite_locations` | — | 网站常用/收藏座位 | |

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

查询当前预约及状态（优先返回活跃预约，否则返回最近一条）。

**成功（无预约）**：`{ "ok": true, "status": "none", "message": "当前无预约" }`
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
`status` 枚举：`none` / `reserved` / `waiting_sign_in` / `in_use` / `away` / `ended` / `cancelled` / `violation_risk` / `unknown`。`away_*` 仅暂离/使用中时出现。
**错误码**：`NEED_LOGIN`

#### `get_site_favorite_locations`

返回网站里的常用/收藏座位（原始结构）。**成功**：`{ "ok": true, "favorites": ... }`；**错误码**：`NEED_LOGIN`

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

> 这三个工具与 `cancel_reservation` 都接受可选 `reservation_id`，但当前实现以"当前预约"为准操作，`reservation_id` 主要用于调用方自查比对。

---

### 典型调用顺序

```text
get_availability_distribution → 选区域 location_id
list_available_seats(location_id) → 选 seat_id
reserve_seat(seat_id 或 location_id+strategy)
get_current_reservation（轮询状态 / 暂离提醒）
end_reservation_early / cancel_reservation
```

---

## 5. 环境变量（.env）

| 变量 | 默认 | 说明 |
|---|---|---|
| `MCP_HOST` / `MCP_PORT` | 0.0.0.0 / 8010 | 服务监听 |
| `DATA_DIR` | ./data | 数据目录（容器内为 `/data`） |
| `HEADLESS` | true | 容器内 true；本地调试可 false 看浏览器 |
| `DEFAULT_USER_KEY` | default | 不传 user_key 时的兜底键 |
| `CCNU_BASE_URL` | kjyy 预约首页 | 含 `#` hash 路由，**.env 里要加引号** |
| `CHALLENGE_TTL_SECONDS` | 240 | 验证码人工输入超时 |
| `CCNU_DEFAULT_USERNAME` / `_PASSWORD` | — | 可选：default 用户默认账号 |

---

## 6. 数据与安全

- 第一版按**个人自用**实现：账号密码明文保存，无加密 / 审计 / 权限系统。
- `.env`、`data/`、`scripts/_probe_out/` 已在 `.gitignore` 与 `.dockerignore`，**不入库、不进镜像**。
- 同一 `user_key` 同一时刻只允许一个活动浏览器任务，避免两个定时任务同时操作同一登录态。
- 数据目录结构见 [§2.1](#21-服务器目录建议)。

---

## 7. 已知边界

- 仅校园网内可用；登录态可保活一段时间，偶发图形验证码、有时需短信验证。
- 签到 / 暂离 / 回座物理上由进出馆闸机自动触发；网页手动按钮（暂离 / 回座 / 退座）已接入，但**服务端是否允许手动操作取决于当前状态**，在馆"使用中"时才能真机确认。
- 续约官方要求到现场预约终端刷卡，网页端无入口，本服务不提供。
- 违约红线：每月 3 次违约进黑名单一周。自动取消 / 退座的预授权与确认由小青团控制，**本服务只执行，不判断**。

---

## 8. 验证脚本（scripts/）

| 脚本 | 用途 |
|---|---|
| `try_login.py` | 直连登录闭环（headful，存验证码图） |
| `try_api.py` | 验证后端 API 鉴权（getUserInfo / 区域 / 座位） |
| `try_reserve.py` | 查询链路；`--book` 测真下单 + 取消 |
| `probe_reserve.py` | 抓预约流程的后端接口 |
| `probe_headers.py` / `dump_signing.py` | 抓请求头 / 还原 HMAC 签名算法 |
| `smoke_mcp.py` | 通过 MCP 协议冒烟（连服务、列工具、跑只读流程） |

---

## 9. 免责声明

- 本项目仅供**个人学习与自用**，用于自动化操作本人在华中师范大学图书馆的合法预约权限，不提供任何绕过学校系统规则的能力（不做验证码自动识别、不做虚假签到、不做位置伪造）。
- 使用者须对自己的账号行为负责，遵守学校图书馆的预约与违约规则。因使用本项目导致的违约、封禁或其他后果，由使用者自行承担，作者不承担任何责任。
- 本项目与华中师范大学及其图书馆无任何隶属或合作关系，所涉站点与接口的所有权归校方所有。学校系统若调整，本项目可能随时失效。

## 10. 许可证

[MIT](LICENSE) © 2026 mcp-ccnu-lib contributors
