# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

华中师范大学图书馆**空间预约的 MCP 执行器**。被上游"小青团"QQ 机器人当作工具调用：本服务只负责执行（登录、查座、约/取消、暂离/回座/退座、违约/历史/闸机记录），**不做 NLU、不做调度、不做提醒**——那些在调用方侧。**纯 HTTP 实现，无浏览器**（历史上用过 Playwright，2026-07 已全量重构为 httpx 直连 CAS + 后端 API）。详尽的对外 API 契约、错误码、部署说明都在 `README.md`，改动工具签名/返回结构时必须同步更新它。

关键约束：
- 真实系统 `https://kjyy.ccnu.edu.cn/jsq-v/`（Vue SPA，CAS 统一认证），**仅校园网内可访问**，部署机必须在校园网。
- 验证码/短信走**人工输入**（challenge 机制），**不做 OCR/自动打码**——这是产品决策，不要主动加。

## 常用命令

```bash
# 本地开发（不走 Docker）
pip install -e .                            # 装 httpx + cryptography + mcp，无浏览器
python -m ccnu_lib.server                   # 启动，监听 0.0.0.0:8010/mcp (streamable-http)

# Docker（生产推荐）— 轻量 python:slim 镜像，无 Chromium
docker compose up -d --build

# 冒烟测试（先在另一终端启动服务，再跑）— 走真实 MCP 协议，只读不下单
python scripts/smoke_mcp.py
```

无单元测试（`pyproject.toml` 列了 pytest 但仓库无 test 文件）。注意 `scripts/` 下的老验证/probe 脚本（try_login/try_api/try_reserve/probe*）是 **Playwright 时代**的，重构后已失效，仅作接口反推的历史参考；真机验证改用临时脚本走 `login.start_login`/`reservations.*`。

**Windows 本地连 127.0.0.1 的坑**：必须设 `NO_PROXY=127.0.0.1,localhost`，否则系统代理把本地请求也转走 → 502。（`WindowsProactorEventLoopPolicy` 那条已随 Playwright 移除，纯 HTTP 不需要。）

## 架构（关键在于两条数据通路 + challenge 中断模型）

工具调用入口在 `server.py`（`@mcp.tool()` 装饰，17 个工具，全部接受可选 `user_key` 做多用户隔离，缺省 `default`）。每个工具是薄壳，转发到 `login.py` 或 `reservations.py`。

**核心设计：纯 HTTP。登录复刻 CAS SSO 重定向链，查询/预约直连后端 JSON API 并在 Python 里现算 HMAC 签名。**

1. **`http_client.py`** — 进程级单例 `HttpManager`，每个 `user_key` 一个 `httpx.AsyncClient`（cookie jar）。登录态保活载体 = 持久化的 cookie（关键是无过期的 `CASTGC`，支持静默 SSO 重登）+ 缓存的 app token，一起存 `{user_dir}/session.json`；建会话时回灌。`Session` 持有 `client`/`token`/`realkey`/`syscfg`/`asyncio.Lock`/`pending`/`login_ctx`。

2. **`login.py`** — 纯 HTTP CAS 状态机。`ensure_syscfg` 打 `getSysSet` 取配置(含 CASSSERVICE/VUESERVICE/hmacKey)并解出 `realkey`。`_sso_walk` GET `{CAS}/static/sso/login?redirectUrl={VUE}` 手动跟随重定向：落到 CAS 登录表单(200,含 `execution`)=未登录→抓 `captcha.jpg` 返回 challenge；直接跳回 VUESERVICE 带 `?token=<JWT>`（CASTGC 有效时）=静默登录。`submit_challenge` POST CAS 表单(username/password/captcha/lt/execution)→跟随 ticket→拿 JWT→`auth/cas` 换 **app token**。**判定真登录=拿到 app token**，不是 URL。

3. **`api.py`** — kjyy 后端 JSON 客户端，`httpx` 直连。鉴权三件套：cookie `rem_JSESSIONID` + header `token`(app token) + **HMAC 签名 header**（`hmac==1` 强校验）。签名在 Python 现算：`realkey = AES-CBC-decrypt(hmacKey, key="server_date_time", iv="client_date_time")`（实测解得 `Lib2025ccnu`），`X-hmac-request-key = HMAC-SHA256("seat::uuid::ms::POST", realkey)`。`public/*`(getSysSet、auth/cas) 免签名；`frontApi/*` 需签名。信封 `{status,code,message,data}`，`call()` 解包返回 `data`，失败抛 `ApiError`。时间换算：分钟数 = `H*60+M`（07:30=450，步长 30，约 07:30–22:00）。

4. **`reservations.py`** — 业务逻辑。每个工具先 `_require_login`（有 app token 直接用+确保 realkey 就绪，否则尝试静默 SSO 复登，仍失败返回 `NEED_LOGIN`），再调 `api.*`，最后把后端原始记录经 `_fmt_make` / `_map_status` 映射成需求枚举返回。

5. **`challenges.py` + `db.py`** — challenge 是**贯穿所有工具的通用中断模型**：遇验证码时工具不阻塞，把 httpx 直接 GET 到的 `captcha.jpg` 字节转 base64 存 SQLite 并返回 `NEED_CHALLENGE`，调用方收集用户输入后 `submit_challenge` 续跑。会话独占靠 `Session.pending` 实现（有挂起时新动作返回 `CHALLENGE_PENDING`）。`db.py` 是同步 SQLite（accounts/sessions/challenges 三表，账号密码明文存——个人自用）。**进程重启后内存里的 `pending`/`login_ctx` 丢失，挂起 challenge 失效需重发起**（但 app token 与 cookie 已落盘 session.json，重启后仍可静默续登）。

> 短信验证（异地登录风控）在纯 HTTP 下尚未实现：`submit_challenge` 目前只处理 captcha，CAS 若返回短信页会当作 `AUTH_UNSETTLED`/失败。真机触发后再补。

## 改动时注意

- 接口/HMAC 签名细节（已反推的完整接口清单、签名算法、CAS 登录链）记在项目 memory `ccnu-api-hmac-signing`；学校系统调整可能使其失效，改登录/API 前先核对真机。
- 登录链的可变量（CASSSERVICE=`.../rem`、VUESERVICE、hmacKey→realkey）全部运行时从 `getSysSet` 取，零硬编码；学校若改 CAS 部署或 hmacKey，`getSysSet` 会反映，一般无需改代码。指纹字段(visitorId)实测非必填。
- 暂离/回座**是过闸机自动触发的**（出馆自动暂离、回馆自动回座，且物理暂离不实时改预约状态），判断在馆/暂离用 `get_door_log` 轮询，不看预约状态；`make/leave`·`checkIn` 工具仅网页按钮镜像。提前退座 `make/stop` 已真机验证。
- `.env` 真正必填的只有账号（`CCNU_DEFAULT_USERNAME/PASSWORD`）。`CCNU_BASE_URL` 已基本不用（登录走 getSysSet 的 VUESERVICE），保留仅为兼容。
