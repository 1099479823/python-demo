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


print("TIGER_BASE      =", C.TIGER_BASE)
print("TIGER_API_KEY   =", mask(C.TIGER_API_KEY), f"(len={len(C.TIGER_API_KEY)})")
print("TIGER_SERVICE   =", C.SERVICE)
print("SERVICE候选     =", C.SERVICE_CANDIDATES)
print("COUNTRY_ID      =", C.COUNTRY_ID)
print("COUNTRY_CODE    =", C.COUNTRY_CODE)
print("BIT_API         =", C.BIT_API)
print("BIT_BROWSER_ID  =", C.BROWSER_ID or "(空，将走 --name-prefix)")
print("LOGIN_URL       =", C.LOGIN_URL)
print("CODE_WAIT_SECONDS =", C.CODE_WAIT_SECONDS)
print("MAX_NUMBERS     =", C.MAX_NUMBERS)
print("CAPTCHA_RETRIES =", C.CAPTCHA_RETRIES)
print("COUNTRY_ID_US   =", C.COUNTRY_ID_US, "| COUNTRY_CODE_US =", C.COUNTRY_CODE_US)
print("POST_EXCEL      =", C.POST_EXCEL,
      "(存在)" if __import__("pathlib").Path(C.POST_EXCEL).exists() else "(不存在!)")
print("POST_SLEEP      =", C.POST_SLEEP)
