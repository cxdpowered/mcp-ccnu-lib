"""页面 selector 与判定信号集中配置。

登录已由 probe 实测（CAS 统一认证 account.ccnu.edu.cn/cas/login）：
账号/密码/验证码框 id 干净可用，验证码图为 captcha.jpg。
短信验证（异地登录触发）结构尚未实测，保留猜测值，待真实触发后再校正。
"""
from __future__ import annotations

# --- CAS 登录页（account.ccnu.edu.cn/cas/login）实测值 ---
USERNAME_INPUT = "#username"
PASSWORD_INPUT = "#password"
CAPTCHA_INPUT = "#captcha"
CAPTCHA_IMAGE = "img[src*='captcha']"          # 实际 https://account.ccnu.edu.cn/cas/captcha.jpg
LOGIN_BUTTON = "input[name='submit'], button:has-text('登录')"

# 登录失败提示：用足够具体的短语，避免命中页面上的"验证码/账号"等标签文字
ERROR_HINTS = [
    "用户名或密码", "密码错误", "认证信息无效", "认证失败",
    "验证码错误", "验证码不正确", "无效的验证码",
]

# --- 短信验证（异地/新设备登录可能触发，结构待实测） ---
SMS_INPUT = "input[name='smsCode'], input[placeholder*='短信'], input[placeholder*='手机验证码']"
SMS_SEND_BUTTON = "button:has-text('发送'), button:has-text('获取验证码')"
PHONE_HINT_TEXT = "text=/尾号|\\d{3}\\*{4}\\d{4}/"

# --- 登录态判定 ---
# 登录页在 account.ccnu.edu.cn；认证成功后跳回 kjyy.ccnu.edu.cn 的 SPA 主页。
LOGIN_URL_MARKERS = ["account.ccnu.edu.cn", "/cas/login"]   # 命中即处于登录页 → 未登录
LOGGED_IN_HOST = "kjyy.ccnu.edu.cn"                          # 回到此域 + 无密码框 → 已登录
LOGGED_IN_URL_HINTS = ["#/main"]                            # SPA 主路由（辅助判定）
LOGGED_IN_ELEMENTS = "text=退出, .user-name, .avatar, text=个人中心"

# 登录页存在标志：密码框可见即说明在登录页
LOGIN_PAGE_MARKERS = "input[type='password']"
