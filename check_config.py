# -*- coding: utf-8 -*-
"""校验 config 是否正确加载 .env（密钥打码）"""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import config as C


def mask(s, keep=4):
    s = s or ""
    if len(s) <= keep * 2:
        return "*" * len(s)
    return f"{s[:keep]}{'*' * (len(s) - keep * 2)}{s[-keep:]}"


print("=== 接码平台 ===")
print("SMS_PROVIDER      =", C.SMS_PROVIDER, "(可选 tiger / hero)")
if C.SMS_PROVIDER == "hero":
    print("HERO_BASE         =", C.HERO_BASE)
    print("HERO_API_KEY      =", mask(C.HERO_API_KEY), f"(len={len(C.HERO_API_KEY)})")
    print("HERO_SERVICE      =", C.HERO_SERVICE or "(空，回退 SERVICE)")
else:
    print("TIGER_BASE        =", C.TIGER_BASE)
    print("TIGER_API_KEY     =", mask(C.TIGER_API_KEY), f"(len={len(C.TIGER_API_KEY)})")
print("SERVICE(生效)     =", C.SERVICE)
print("SERVICE候选       =", C.SERVICE_CANDIDATES)
print("SMS_PROXY         =", C.SMS_PROXY or "(直连)")
print()
print("=== 国家 ===")
print("COUNTRY_ID        =", C.COUNTRY_ID)
print("COUNTRY_CODE      =", C.COUNTRY_CODE)
print("COUNTRY_ID_US     =", C.COUNTRY_ID_US, "| COUNTRY_CODE_US =", C.COUNTRY_CODE_US)
print("COUNTRY_ORDER     =", C.COUNTRY_ORDER_RAW,
      "->", " -> ".join(c["cn"] for c in C.COUNTRY_LIST))
if C.COUNTRY_ORDER_INVALID:
    print("  ⚠ 无效项:", C.COUNTRY_ORDER_INVALID)
print()
print("=== 浏览器 ===")
print("BIT_API           =", C.BIT_API)
print("BIT_BROWSER_ID    =", C.BROWSER_ID or "(空，将走 --name-prefix)")
print("BIT_BROWSER_GROUP =", C.BROWSER_GROUP or "(全量)")
print("LOGIN_URL         =", C.LOGIN_URL)
print()
print("=== 行为 ===")
print("CODE_WAIT_SECONDS =", C.CODE_WAIT_SECONDS)
print("CODE_SUBMIT_TRIES =", getattr(C, "CODE_SUBMIT_TRIES", "-"))
print("CODE_GRACE_SECONDS =", getattr(C, "CODE_GRACE_SECONDS", "-"))
print("MAX_NUMBERS       =", C.MAX_NUMBERS)
print("CAPTCHA_RETRIES   =", C.CAPTCHA_RETRIES)
print("POST_EXCEL        =", C.POST_EXCEL,
      "(存在)" if __import__("pathlib").Path(C.POST_EXCEL).exists() else "(不存在!)")
print("POST_SLEEP        =", C.POST_SLEEP)
print("POST_ACCOUNT_RETRIES =", getattr(C, "POST_ACCOUNT_RETRIES", "-"))