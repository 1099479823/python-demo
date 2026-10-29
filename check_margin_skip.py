# -*- coding: utf-8 -*-
"""统计新阈值下有多少题会被「直接换题」而不是硬拖"""
import sys, io, pathlib, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import xhs_captcha_cv as CV

ASSETS = pathlib.Path("captcha_assets")
stems = sorted({m.group(1) for p in ASSETS.glob("bg_*.png")
                if (m := re.match(r"bg_(\w+)\.png", p.name))})

rows = []
for stem in stems:
    bg, pc = ASSETS / f"bg_{stem}.png", ASSETS / f"piece_{stem}.png"
    if not pc.exists():
        continue
    try:
        g = CV.find_gap(bg, pc, "top")
    except Exception:
        continue
    if g:
        rows.append((stem, g["score"], g["margin"]))

skip = [r for r in rows if r[2] < 0.05]
print(f"有效样本 {len(rows)} 组")
print(f"margin < 0.05（会直接换题、不硬拖）: {len(skip)} 组  ({len(skip)/len(rows)*100:.1f}%)")
for s in skip:
    print(f"   {s[0]}  score={s[1]:.3f}  margin={s[2]:.3f}")

ms = sorted(r[2] for r in rows)
print(f"\nmargin 分布: min={ms[0]:.3f}  p10={ms[int(len(ms)*0.1)]:.3f}  中位={ms[len(ms)//2]:.3f}  max={ms[-1]:.3f}")
