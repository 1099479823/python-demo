# -*- coding: utf-8 -*-
"""对代表性样本画候选框：绿=最终选中，橙=其他候选，标注内容分"""
import sys, io, pathlib
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import xhs_captcha_cv as CV

ASSETS = pathlib.Path("captcha_assets")
OUT = pathlib.Path("probe")

CASES = [
    ("37345136", "高分高margin"),
    ("74616695", "低margin"),
    ("12157047", "两引擎分歧大"),
    ("37830107", "最低内容分"),
    ("55666425", "中等分歧"),
]

for stem, tag in CASES:
    bg, pc = ASSETS / f"bg_{stem}.png", ASSETS / f"piece_{stem}.png"
    if not bg.exists() or not pc.exists():
        print(f"{stem}: 样本缺失")
        continue
    dst = OUT / f"gap_{stem}.png"
    best = CV.debug_draw(bg, pc, dst, strategy="top")
    print(f"{stem} [{tag}] -> 选中 x0={best.get('x0')} score={best.get('score')} "
          f"margin={best.get('margin')} scale={best.get('scale')} 图: {dst}")
