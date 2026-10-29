# -*- coding: utf-8 -*-
"""
小红书 / RedNote 滑动拼图缺口定位 —— OpenCV 实现

与 xhs_captcha.py 里那套 scipy 手写流水线（亮度阈值 + ndimage.label + 自写 IoU）
相比，这里换成四个 cv2 原生算子，并且换掉了「按形状猜」的判据：

    1. 候选定位   cv2.morphologyEx + cv2.connectedComponentsWithStats
    2. 候选过滤   bbox 面积 / 边长 / 长宽比 / 与拼图块尺寸比 / 填充率
    3. 真假判别   cv2.matchTemplate(TM_CCOEFF_NORMED, mask=alpha)   ← 内容相关，不再靠形状
    4. 兜底 tie   top / right / inner 位置先验（与旧实现兼容）

为什么判据要换：题库里两块暗色块的**轮廓几乎完全一致**（旧实现 49 组里有 18 组
两个候选 IoU 都是 0.99，形状判不出来），但它们的**内部内容不同** —— 真缺口是被
压暗、模糊过的拼图块本体，干扰块是画面另一处的同形暗斑。所以真正可分的信号是
「拼图块内容 ↔ 缺口内内容的零均值归一化相关」，亮度差被 CCOEFF 归一化抵消。

用法::

    import xhs_captcha_cv as cvcap
    r = cvcap.find_gap("captcha_assets/bg_x.png", "captcha_assets/piece_x.png")
    # r = {"x0":327, "y0":99, "w":72, "h":72, "score":0.72, "margin":0.13, "engine":"cv2"}
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

STRATEGIES = ("top", "right", "inner")

# 候选过滤阈值（与旧实现对齐，便于横向对比）
MIN_AREA = 1200
SIDE_MIN, SIDE_MAX = 45, 200
ASPECT_LO, ASPECT_HI = 0.78, 1.30
SIZE_LO, SIZE_HI = 0.62, 1.55
MIN_FILL = 0.35          # 暗块填充率下限，滤掉暗色书架/窗帘这类大块

# 内容匹配搜索范围（拼图块 79~92px，缺口 65~78px，比例落在 0.70~1.20）
SCALES = tuple(round(s, 2) for s in np.arange(0.70, 1.21, 0.02))
BLUR_KSIZE = 5           # 缺口内是模糊过的内容，模板也要糊一下再比

# 内容信号下限：低于此分说明题库风格变了（例如缺口被画成纯黑剪影），
# find_gap 返回 None，由调用方回落旧版形状匹配。
MIN_CONTENT_SCORE = 0.35


# ---------------------------------------------------------------- 资源读取
def load_piece(path: str | Path) -> tuple[np.ndarray, np.ndarray]:
    """读拼图块，按 alpha 裁到内容框，返回 (灰度图, 0/255 蒙版)"""
    q = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
    if q is None:
        raise FileNotFoundError(path)
    if q.ndim == 3 and q.shape[2] == 4:
        alpha = q[:, :, 3]
        gray = cv2.cvtColor(q, cv2.COLOR_BGRA2GRAY)
    else:
        gray = q if q.ndim == 2 else cv2.cvtColor(q, cv2.COLOR_BGR2GRAY)
        alpha = np.full(gray.shape, 255, np.uint8)
    ys, xs = np.where(alpha > 120)
    if len(ys) == 0:
        return gray, np.full(gray.shape, 255, np.uint8)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return gray[y0:y1, x0:x1], (alpha[y0:y1, x0:x1] > 120).astype(np.uint8) * 255


def load_bg(path: str | Path) -> np.ndarray:
    b = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if b is None:
        raise FileNotFoundError(path)
    return cv2.cvtColor(b, cv2.COLOR_BGR2GRAY)


# ---------------------------------------------------------------- 候选暗块
def dark_mask(gray: np.ndarray) -> np.ndarray:
    """亮度阈值 + 开运算，得到「暗块」二值图"""
    lum = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float32)
    thr = max(52.0, float(np.percentile(lum, 12)))
    m = (lum < thr).astype(np.uint8)
    return cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))


def gap_candidates(gray: np.ndarray, piece_shape: Optional[tuple[int, int]] = None) -> list[dict]:
    """连通域候选：面积、边长、长宽比、与拼图块尺寸比、填充率"""
    m = dark_mask(gray)
    n, lab, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    ph = pw = None
    if piece_shape:
        ph, pw = piece_shape                      # (h, w)

    out = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i][:5])
        if area < MIN_AREA:
            continue
        if not (SIDE_MIN <= w <= SIDE_MAX and SIDE_MIN <= h <= SIDE_MAX):
            continue
        if not (ASPECT_LO < w / h < ASPECT_HI):
            continue
        fill = area / float(w * h)
        if fill < MIN_FILL:
            continue
        if pw and ph:
            if not (SIZE_LO < w / pw < SIZE_HI and SIZE_LO < h / ph < SIZE_HI):
                continue
        # 用该连通域自己的像素做形状蒙版（保留给调试/兜底用）
        mask = (lab[y:y + h, x:x + w] == i).astype(np.uint8) * 255
        out.append({"x0": x, "y0": y, "x1": x + w - 1, "y1": y + h - 1,
                    "w": w, "h": h, "area": area, "fill": round(fill, 3), "mask": mask})
    out.sort(key=lambda c: -c["area"])
    return out


# ---------------------------------------------------------------- 内容匹配
def content_score(gray: np.ndarray, pgray: np.ndarray, pmask: np.ndarray,
                  box: dict, scales=SCALES, pad: int = 12) -> tuple[float, float]:
    """
    在候选框邻域内做多尺度「带蒙版的零均值归一化相关」。
    返回 (最高分, 对应缩放比)。
    """
    gx0, gy0 = max(0, box["x0"] - pad), max(0, box["y0"] - pad)
    gx1 = min(gray.shape[1], box["x1"] + 1 + pad)
    gy1 = min(gray.shape[0], box["y1"] + 1 + pad)
    win = gray[gy0:gy1, gx0:gx1]
    if win.shape[0] < 8 or win.shape[1] < 8:
        return -1.0, 0.0

    best, best_s = -1.0, 0.0
    for s in scales:
        w = max(8, int(round(pgray.shape[1] * s)))
        h = max(8, int(round(pgray.shape[0] * s)))
        if h > win.shape[0] or w > win.shape[1]:
            continue
        t = cv2.resize(pgray, (w, h), interpolation=cv2.INTER_AREA)
        t = cv2.GaussianBlur(t, (BLUR_KSIZE, BLUR_KSIZE), 0)
        mm = cv2.resize(pmask, (w, h), interpolation=cv2.INTER_NEAREST)
        r = cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED, mask=mm)
        r = np.nan_to_num(r, nan=-1.0, posinf=-1.0, neginf=-1.0)
        v = float(r.max())
        if v > best:
            best, best_s = v, s
    return best, best_s


def _pick(scored: list[tuple[float, dict]], strategy: str, bg_size: tuple[int, int]) -> dict:
    """分数优先，近似平局（差 <0.02）时用位置先验 tie-break"""
    bw, bh = bg_size

    def inner(c):
        return c["x0"] > 6 and c["y0"] > 6 and c["x1"] < bw - 6 and c["y1"] < bh - 6

    top = max(s for s, _ in scored)
    band = [c for s, c in scored if s >= top - 0.02] or [c for _, c in scored]
    if strategy == "right":
        band.sort(key=lambda c: (-c["x0"], c["y0"]))
    elif strategy == "inner":
        band.sort(key=lambda c: (0 if inner(c) else 1, c["y0"]))
    else:
        band.sort(key=lambda c: (c["y0"], -c["x0"]))
    return band[0]


def find_gap(bg_path: str | Path, piece_path: str | Path, strategy: str = "top",
             min_score: float = MIN_CONTENT_SCORE) -> Optional[dict]:
    """
    定位缺口：返回 {x0,y0,w,h,score,margin,scale,engine}
    候选为空、或最高内容分 < min_score（内容信号失效）时返回 None，交给旧引擎兜底。
    """
    gray = load_bg(bg_path)
    pgray, pmask = load_piece(piece_path)
    cands = gap_candidates(gray, pgray.shape)
    if not cands:
        return None

    scored = []
    for c in cands:
        s, sc = content_score(gray, pgray, pmask, c)
        c["score"], c["scale"] = round(s, 4), sc
        scored.append((s, c))

    chosen = _pick(scored, strategy, (gray.shape[1], gray.shape[0]))
    if chosen["score"] < min_score:
        return None
    others = [s for s, c in scored if c is not chosen]
    margin = round(chosen["score"] - max(others), 4) if others else round(chosen["score"], 4)
    return {"x0": chosen["x0"], "y0": chosen["y0"], "w": chosen["w"], "h": chosen["h"],
            "score": chosen["score"], "margin": margin, "scale": float(chosen["scale"]),
            "ncand": len(cands), "engine": "cv2"}


# ---------------------------------------------------------------- 调试可视化
def debug_draw(bg_path: str | Path, piece_path: str | Path, out_path: str | Path,
               strategy: str = "top") -> dict:
    """画候选框 + 分数，落一张 PNG 便于人工核对"""
    bgr = cv2.imread(str(bg_path), cv2.IMREAD_COLOR)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    pgray, pmask = load_piece(piece_path)
    cands = gap_candidates(gray, pgray.shape)
    best = find_gap(bg_path, piece_path, strategy)
    for c in cands:
        s, _ = content_score(gray, pgray, pmask, c)
        hit = best and (c["x0"], c["y0"]) == (best["x0"], best["y0"])
        color = (0, 255, 0) if hit else (0, 165, 255)
        cv2.rectangle(bgr, (c["x0"], c["y0"]), (c["x1"], c["y1"]), color, 2)
        cv2.putText(bgr, f"{s:.2f}", (c["x0"], max(12, c["y0"] - 4)),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)
    cv2.imwrite(str(out_path), bgr)
    return best or {}


if __name__ == "__main__":  # 手工跑一组：python xhs_captcha_cv.py stem
    import sys
    stem = sys.argv[1] if len(sys.argv) > 1 else "89245792"
    root = Path(__file__).parent / "captcha_assets"
    bg, pc = root / f"bg_{stem}.png", root / f"piece_{stem}.png"
    print(find_gap(bg, pc))
    print(debug_draw(bg, pc, Path(os.getenv("TEMP", ".")) / f"gap_{stem}.png"))
