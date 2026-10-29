# -*- coding: utf-8 -*-
"""检查拼图块内容在元素内的偏移：是否导致 target_pz 系统性偏大"""
import sys, io, pathlib, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import numpy as np
from PIL import Image

ASSETS = pathlib.Path("captcha_assets")

print("piece 原图尺寸 与 alpha 内容框（是否存在左侧偏移）\n")
print(f"{'文件':<22} {'原图':<10} {'内容框':<20} {'左偏移':<8} {'上偏移':<8} {'内容占比'}")
print("-" * 88)

rows = []
for p in sorted(ASSETS.glob("piece_*.png"))[:12]:
    im = Image.open(p).convert("RGBA")
    a = np.asarray(im)[:, :, 3]
    m = a > 120
    if not m.any():
        continue
    ys, xs = np.where(m)
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    w, h = im.size
    cw, ch = x1 - x0 + 1, y1 - y0 + 1
    rows.append((p.name, w, h, x0, y0, cw, ch))
    print(f"{p.name[:20]:<22} {w}x{h:<6} ({x0},{y0})-({x1},{y1})   "
          f"{x0:<8} {y0:<8} {cw/w*100:.0f}%x{ch/h*100:.0f}%")

if rows:
    xs0 = [r[3] for r in rows]
    print(f"\n左偏移统计: min={min(xs0)} max={max(xs0)} 平均={sum(xs0)/len(xs0):.1f}px")
    sizes = {(r[1]) for r in rows}
    print(f"原图宽度种类: {sizes}")
    # 换算到 CSS：舞台 352 / 原图 560
    if min(xs0) > 0:
        css = [x * 352 / 560 for x in xs0]
        print(f"换算到 CSS 的左侧偏移: {[round(c,1) for c in css]}")
        print(f"\n=> 若代码未扣掉这个偏移，拼图块会整体右移约 {sum(css)/len(css):.1f}px（CSS）")
