# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目定位

华中师范大学图书馆**空间预约的 MCP 执行器**。被上游"小青团"QQ 机器人当作工具调用：本服务只负责浏览器自动化执行（登录、查座、约/取消、暂离/回座/退座），**不做 NLU、不做调度、不做提醒**——那些在调用方侧。详尽的对外 API 契约、错误码、部署说明都在 `README.md`，改动工具签名/返回结构时必须同步更新它。

关键约束：
- 真实系统 `https://kjyy.ccnu.edu.cn/jsq-v/`（Vue SPA，CAS 统一认证），**仅校园网内可访问**，部署机必须在校园网。
- 验证码/短信走**人工输入**（challenge 机制），**不做 OCR/自动打码**——这是产品决策，不要主动加。

## 常用命令

```bash
# 本地开发（不走 Docker）
pip install -e .
python -m playwright install chromium      # 首次需要；Docker 镜像已内置
python -m ccnu_lib.server                   # 启动，监听 0.0.0.0:8010/mcp (streamable-http)

# Docker（生产推荐）
docker compose up -d --build

# 冒烟测试（先在另一终端启动服务，再跑）— 走真实 MCP 协议，只读不下单
python scripts/smoke_mcp.py
```

无单元测试（`pyproject.toml` 列了 pytest 但仓库无 test 文件）。验证靠 `scripts/` 下的脚本（真机连校园网）：`try_login.py`（登录闭环）、`try_api.py`（API 鉴权）、`try_reserve.py [--book]`（查询；`--book` 测真下单+取消）。其余 probe 脚本用于反推接口/签名。

**Windows 本地连 127.0.0.1 的两个坑**：① 必须设 `NO_PROXY=127.0.0.1,localhost`，否则系统代理把本地请求也转走 → 502；② 客户端脚本需 `WindowsProactorEventLoopPolicy`（见 smoke_mcp.py），否则 Playwright 子进程崩。

**版本锁定**：`pyproject.toml` 的 `playwright==1.60.0` 必须与 `Dockerfile` 的镜像 tag `v1.60.0-noble` 完全一致，升级两处同步改，否则浏览器二进制对不上会崩。

## 架构（关键在于两条数据通路 + challenge 中断模型）

工具调用入口在 `server.py`（`@mcp.tool()` 装饰，17 个工具，全部接受可选 `user_key` 做多用户隔离，缺省 `default`）。每个工具是薄壳，转发到 `login.py` 或 `reservations.py`。

**核心设计：登录用浏览器，查询/预约直连后端 JSON API——但 API 调用也在浏览器页面里发起。**

1. **`browser.py`** — 进程级单例 `BrowserManager`，每个 `user_key` 一个 Playwright persistent context（profile 落盘 = 登录态保活载体）。注意：CAS 的 session cookie（如 CASTGC）persistent profile 默认不存，所以登录成功后手动 `save_cookies()` 写 `cookies.json`，建会话时 `_restore_cookies()` 回灌，实现跨进程免登录。`Session` 持有 `asyncio.Lock`（同 user 串行）和 `pending`（挂起的 challenge）。

2. **`login.py`** — 登录状态机。`start_login` 走 `base_url` → 等 CAS SSO 重定向链 settle（`_wait_settled`：不能一看到 kjyy 域就判已登录，未登录会先闪 kjyy 再跳回 CAS；要求 kjyy 域 URL 稳定**且** SPA 已把 token 存进 `sessionStorage.jsq_p-token`）→ 填表单 → `_evaluate_after_submit` 竞速判定成功/短信/错误。**登录是否真成功的唯一判据是 `sessionStorage.jsq_p-token` 存在**（`_has_token`），不是 URL。

3. **`api.py`** — kjyy 后端 JSON 客户端。**所有 API 调用都用 `page.evaluate` 在已登录的 kjyy 同源页面里 `fetch`**（模块顶部 docstring 说的 `context.request` 是旧实现的残留描述——实际看 `_JS_CALL`；独立 APIRequestContext 在 Windows 会崩驱动）。鉴权三件套：cookie `rem_JSESSIONID` + header `token`(取自 sessionStorage) + **HMAC 签名 header**（`systemInfo.hmac==1` 时强校验，用页面里的 `window.CryptoJS` 现算，密钥运行时从 sessionStorage 解出，零硬编码）。返回统一信封 `{status,code,message,data}`，`call()` 解包返回 `data`，失败抛 `ApiError`。时间换算：分钟数 = `H*60+M`（07:30=450，步长 30，范围约 07:30–22:00）。

4. **`reservations.py`** — 阶段 2/3 业务逻辑。每个工具先 `_require_login`（有 token 直接用，否则尝试 SSO 复登，仍失败返回 `NEED_LOGIN`），再调 `api.*`，最后把后端原始记录经 `_fmt_make` / `_map_status` 映射成需求枚举返回。

5. **`challenges.py` + `db.py`** — challenge 是**贯穿所有工具的通用中断模型**（不只登录）：遇验证码/短信时工具不阻塞，截图转 base64 存 SQLite 并返回 `NEED_CHALLENGE`，调用方收集用户输入后 `submit_challenge` 续跑挂起的流程。会话独占靠 `Session.pending` 实现：有挂起 challenge 时新动作返回 `CHALLENGE_PENDING`。`db.py` 是同步 SQLite（accounts/sessions/challenges 三表，账号密码明文存——个人自用）。**进程重启后内存里的 `pending` 会话丢失，所有挂起 challenge 失效需重发起。**

6. **`selectors.py`** — CAS 登录页 selector 与判定信号集中配置。登录框已 probe 实测；短信验证结构尚未真机触发，是猜测值，真实触发后需校正。

## 改动时注意

- 接口/HMAC 签名细节（已反推的完整接口清单、签名算法）记在项目 memory `ccnu-api-hmac-signing`；学校系统调整可能使其失效，改 API 前先核对真机。
- 阶段 3 三个"使用中"动作（暂离/回座/退座）服务端是否放行取决于当前在馆状态，只能在馆"使用中"真机验证。
- `CCNU_BASE_URL` 含 `#` hash 路由，`.env` 里**必须加引号**；`docker-compose.yml` 用 `environment` 覆盖非机密项规避 env_file 对 `#` 的处理不一致，`.env` 真正必填的只有账号。
