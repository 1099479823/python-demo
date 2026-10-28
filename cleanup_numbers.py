# -*- coding: utf-8 -*-
"""等过最小存活时间(120s)后取消对照测试取到的号"""
import sys, io, time
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import requests
import config as C

S = requests.Session()
S.trust_env = False
S.proxies = {"http": None, "https": None}


def call(action, **params):
    q = {"action": action, "api_key": C.TIGER_API_KEY}
    q.update(params)
    r = S.get(C.TIGER_BASE, params=q, timeout=40)
    return (r.text or "").strip()[:300]


ids = ["9999990009836336309", "9999990009836336385", "9999990009836336457"]
print("等待 130 秒（平台要求最小存活 120s）...")
time.sleep(130)

for i in ids:
    print(f"cancel {i} ->", call("setStatus", id=i, status=8))
    time.sleep(1)

print("\n余额:", call("getBalance"))
print("active:", call("getActiveActivations")[:300])
