# -*- coding: utf-8 -*-
"""全局配置：优先读环境变量，其次读同目录 .env"""
from __future__ import annotations
import os
from pathlib import Path

ROOT = Path(__file__).parent


def _load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if v[:1] in ('"', "'"):
            q = v[0]
            end = v.find(q, 1)
            v = v[1:end] if end > 0 else v[1:]
        else:
            # 去掉行内注释： VALUE   # 说明
            v = v.split("#", 1)[0].strip()
        os.environ.setdefault(k, v)


_load_env()

# ---------- 接码平台 ----------
TIGER_BASE = os.getenv("TIGER_BASE", "https://api.tiger-sms.com/stubs/handler_api.php")
TIGER_API_KEY = os.getenv("TIGER_SMS_KEY", "")
# 接码平台默认直连（系统代理往往连不通），需要时填 http://127.0.0.1:7890 之类
TIGER_PROXY = os.getenv("TIGER_PROXY", "") or None

# 小红书在接码平台的 service 代码（不同平台上架代码不同，按顺序自动探测）
SERVICE_CANDIDATES = [s.strip() for s in os.getenv(
    "TIGER_SERVICE_CANDIDATES", "xhs,xiaohongshu,rednote,redbook,xh").split(",") if s.strip()]
SERVICE = os.getenv("TIGER_SERVICE", "xhs")

# 国家: sms-activate 兼容编号表，泰国 = 52，美国 = 187
COUNTRY_ID = os.getenv("TIGER_COUNTRY_ID", "52")
COUNTRY_CODE = os.getenv("TIGER_COUNTRY_CODE", "66")   # 国际电话前缀
COUNTRY_ID_US = os.getenv("TIGER_COUNTRY_ID_US", "187")
COUNTRY_CODE_US = os.getenv("TIGER_COUNTRY_CODE_US", "1")
# 美国号单价上限（超过这个价的 provider 不买）
MAX_PRICE_US = os.getenv("TIGER_MAX_PRICE_US", "0.05")

# 国家备用顺序：运行时按“可用号码数”自动排序（号多的先用），接不到再换下一个
COUNTRY_LIST = [
    {"label": "Thailand", "cn": "泰国", "id": COUNTRY_ID, "code": COUNTRY_CODE,
     "max_price": None},
    {"label": "United States", "cn": "美国", "id": COUNTRY_ID_US, "code": COUNTRY_CODE_US,
     "max_price": MAX_PRICE_US},
]
# 每个国家最多连续失败几个号就换下一个国家（取号无货 / 收不到码 / 发码被拒 都算失败）
COUNTRY_SWITCH_AFTER = int(os.getenv("COUNTRY_SWITCH_AFTER", "3"))

# ---------- 比特浏览器 ----------
BIT_API = os.getenv("BIT_API", "http://127.0.0.1:54345")
BROWSER_ID = os.getenv("BIT_BROWSER_ID", "")
BROWSER_GROUP = os.getenv("BIT_BROWSER_GROUP", "")

# ---------- 站点 ----------
LOGIN_URL = os.getenv("XHS_LOGIN_URL", "https://www.rednote.com/login")
API_HOST = os.getenv("XHS_API_HOST", "webapi.rednote.com")

# ---------- 发帖 ----------
# 发帖配置表路径；不配则默认用桌面上的 测试.xlsx
POST_EXCEL = os.getenv("POST_EXCEL") or str(Path.home() / "Desktop" / "测试.xlsx")
# 每条发完后的间隔秒
POST_SLEEP = float(os.getenv("POST_SLEEP", "10"))

# ---------- 行为参数 ----------
CODE_WAIT_SECONDS = int(os.getenv("CODE_WAIT_SECONDS", "90"))   # 单号等码时长
NUMBER_ATTEMPTS = int(os.getenv("NUMBER_ATTEMPTS", "4"))        # 取号重试次数
NUMBER_RETRY_INTERVAL = float(os.getenv("NUMBER_RETRY_INTERVAL", "5"))  # 每次间隔秒
MAX_NUMBERS = int(os.getenv("MAX_NUMBERS", "6"))                # 换号上限
POLL_INTERVAL = float(os.getenv("POLL_INTERVAL", "5"))
CAPTCHA_RETRIES = int(os.getenv("CAPTCHA_RETRIES", "2"))
# 滑块缺口定位：OpenCV 内容匹配（xhs_captcha_cv.py），旧 scipy 方案已移除
# 区分度 margin 低于此值直接换题，不做无意义拖动（省一次风控计数）
CAPTCHA_MIN_MARGIN = float(os.getenv("CAPTCHA_MIN_MARGIN", "0.05"))
# 换题前的随机停顿（秒），避免机械节奏被判定为脚本
CAPTCHA_REFRESH_DELAY_MIN = float(os.getenv("CAPTCHA_REFRESH_DELAY_MIN", "3"))
CAPTCHA_REFRESH_DELAY_MAX = float(os.getenv("CAPTCHA_REFRESH_DELAY_MAX", "5"))
HEADLESS_DELAY = float(os.getenv("HEADLESS_DELAY", "0"))
ASSETS_DIR = ROOT / "captcha_assets"

# ---------- 选择器（实测于 2026-10 / rednote.com 登录页） ----------
SEL = {
    "country_box": "div.country-code-select",
    "country_search": 'input[placeholder="Search country/region"]',
    "country_item": 'div.country-item:has-text("Thailand")',
    "phone": 'input[placeholder="Enter phone number"]',
    "code_btn": "span.code-button",
    "code_input": 'input[placeholder="Enter verification code"]',
    "submit": "button.submit",
    "agree_dialog_btn": 'button:has-text("Agree and continue")',
    "login_entry": 'text=Log in with phone number',
}
