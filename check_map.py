# -*- coding: utf-8 -*-
"""干跑：Excel 每行 -> 对应窗口，只验证绑定关系，不开窗不发帖不花钱"""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path

import xhs_post as M
import xhs_publish as P

rows = M.read_excel(M.DEFAULT_EXCEL)
print(f"Excel 共 {len(rows)} 行\n")

ok_all = True
for r in rows:
    seq = r["seq"]
    bid = M.find_browser_id(seq)
    kind = P.kind_of(r["path"])
    exists = Path(r["path"]).exists()
    size = round(Path(r["path"]).stat().st_size / 1024 / 1024, 1) if exists else 0
    status = "OK" if (bid and exists and kind != "unknown") else "FAIL"
    if status == "FAIL":
        ok_all = False
    print(f"[{status}] id={seq}")
    print(f"       窗口   : {('小红书测试' + str(seq)) if bid else '未找到'}  id={bid}")
    print(f"       类型   : {kind}")
    print(f"       文件   : {Path(r['path']).name}  {'存在' if exists else '缺失'}  {size}MB")
    print(f"       标题   : {r['title']!r}")
    print(f"       正文   : {r['content']!r}")
    print(f"       标签   : {r['tags']}")
    print()

print("全部绑定可解析" if ok_all else "存在无法解析的行")
