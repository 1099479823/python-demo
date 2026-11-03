# -*- coding: utf-8 -*-
"""查泰国(52)当前库存：qf 及候选 service，另外看 qf 在热门国家的库存"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import config as C
from sms_factory import make_sms_client
from tiger_sms import TigerSMS

cli = make_sms_client()
print("余额:", cli.balance())

print("\n=== 泰国 52 各 service ===")
for svc in ["qf", "xhs", "xiaohongshu", "rednote", "redbook", "xh"]:
    try:
        d = cli.prices(svc, "52")
        ok = isinstance(d, dict) and "52" in d and svc in d["52"]
        print(f"  {svc:14s} {'有货' if ok else '无'}  {json.dumps(d, ensure_ascii=False)[:220]}")
    except Exception as e:
        print(f"  {svc:14s} 异常 {e}")

print("\n=== qf 在其他国家的泰国号码情况（抽几个常见国家）===")
for cid in ["52", "6", "16", "86", "36", "117", "10"]:
    try:
        d = cli.prices("qf", cid)
        if isinstance(d, dict) and cid in d and "qf" in d[cid]:
            v = d[cid]["qf"]
            print(f"  country={cid:>3}  count={v.get('count')}  price={v.get('price')}")
    except Exception as e:
        print(f"  country={cid} 异常 {e}")
