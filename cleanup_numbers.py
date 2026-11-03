# -*- coding: utf-8 -*-
"""等过最小存活时间(120s)后取消对照测试取到的号"""
import sys, io, time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from sms_factory import make_sms_client

cli = make_sms_client()          # 按 SMS_PROVIDER 自动选 tiger / hero

ids = ["9999990009836336309", "9999990009836336385", "9999990009836336457"]
print("等待 130 秒（平台要求最小存活 120s）...")
time.sleep(130)

for i in ids:
    print(f"cancel {i} ->", cli.cancel(i))
    time.sleep(1)

print("\n余额:", cli.balance())
print("active:", cli.active_activations())