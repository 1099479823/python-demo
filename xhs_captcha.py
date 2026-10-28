# -*- coding: utf-8 -*-
"""
小红书 / RedNote 滑动拼图验证（redcaptcha verifyType=217）自动处理

实测 DOM（rednote.com 登录页）:
    [class*=slider-stage]                        拼图舞台，CSS 352x211（原图 560x336）
      img[class*=slider-background]              *_hole.png，带洞背景
      [class*=slider-puzzle]                     *_piece.png，拼图块，translate3d 定位
    [class*=slider-track]                        拖动条，宽 352
      button[data-captcha-role=slider-handle]    滑块按钮 56x44
      [data-captcha-role=slider-refresh]         换一换

题库特征（实测 4 组）:
    背景图上存在两块「同形状暗色拼图块」，一块是目标洞，一块是干扰影子。
    已验证案例中目标洞 = y 坐标更小的那块（x 也更大）。
    因此默认策略 top，失败后依次切换 right / inner 并自动记忆成功策略。

实测映射: 拼图块位移 = 滑块位移 * k，k 随缩放变化（0.80~0.93），运行时标定。
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image
from scipy import ndimage

log = logging.getLogger("captcha")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

STRATEGIES = ("top", "right", "inner")
STRATEGY_FILE = "strategy.json"


# ------------------------------------------------------------------ 资源
def _download(url: str, dst: Path) -> Path:
    """urllib 兜底下载（重试 + 绕开系统代理，避免代理 SSL EOF）"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.rednote.com/"})
            with opener.open(req, timeout=45) as r:
                dst.write_bytes(r.read())
            return dst
        except Exception as e:
            last = e
            time.sleep(1.0)
    raise last if last else RuntimeError("download failed")


async def _fetch_asset(page, url: str, dst: Path) -> Path:
    """优先走浏览器上下文请求（带 cookie/referer，最稳），失败再退回 urllib"""
    try:
        resp = await page.context.request.get(url, timeout=45000,
                                              headers={"Referer": "https://www.rednote.com/"})
        if resp.ok:
            body = await resp.body()
            if body and len(body) > 1000:
                dst.write_bytes(body)
                return dst
    except Exception as e:
        log.debug("context.request 下载失败: %s", e)
    return _download(url, dst)


def _piece_mask(piece_path: Path) -> np.ndarray:
    pa = np.asarray(Image.open(piece_path).convert("RGBA"))
    m = pa[:, :, 3] > 120
    if m.sum() < 100:
        return np.ones(pa.shape[:2], bool)
    ys, xs = np.where(m)
    return m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


# ------------------------------------------------------------------ 缺口
def find_holes(bg_path: Path, piece_path: Optional[Path] = None) -> list[dict]:
    """返回所有疑似「洞/影子」的候选块（暗色、近似方形、与拼图块尺寸接近）"""
    im = Image.open(bg_path).convert("RGB")
    a = np.asarray(im).astype(np.float32)
    lum = a.mean(axis=2)
    thr = max(52.0, float(np.percentile(lum, 12)))
    mask = lum < thr
    mask = ndimage.binary_opening(mask, np.ones((3, 3)))
    lab, n = ndimage.label(mask)

    pw = ph = None
    if piece_path and Path(piece_path).exists():
        ph, pw = _piece_mask(Path(piece_path)).shape

    cands = []
    for i in range(1, n + 1):
        ys, xs = np.where(lab == i)
        area = len(xs)
        if area < 1200:
            continue
        w = int(xs.max() - xs.min() + 1)
        h = int(ys.max() - ys.min() + 1)
        if not (45 <= w <= 200 and 45 <= h <= 200):
            continue
        if not (0.78 < w / h < 1.30):          # 拼图块近似方形
            continue
        if pw and ph:                           # 与拼图块尺寸比对（宽松）
            if not (0.62 < w / pw < 1.55 and 0.62 < h / ph < 1.55):
                continue
        cands.append({"x0": int(xs.min()), "y0": int(ys.min()), "x1": int(xs.max()),
                      "y1": int(ys.max()), "w": w, "h": h, "area": area,
                      "mask": (lab[ys.min():ys.max() + 1, xs.min():xs.max() + 1] == i)})
    cands.sort(key=lambda c: -c["area"])
    return cands


def shape_iou(piece_mask: np.ndarray, cand_mask: np.ndarray) -> float:
    """把候选块蒙版缩放到拼图块尺寸后算 IoU（形状匹配）"""
    h, w = piece_mask.shape
    m = Image.fromarray((cand_mask.astype(np.uint8) * 255)).resize((w, h), Image.BILINEAR)
    cm = np.asarray(m) > 127
    inter = np.logical_and(piece_mask, cm).sum()
    union = np.logical_or(piece_mask, cm).sum()
    return float(inter / union) if union else 0.0


def find_pairs(cands: list[dict]) -> list[tuple[dict, dict]]:
    """题库里「目标洞 + 干扰影子」通常成对且尺寸几乎一致"""
    pairs = []
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            if abs(a["area"] - b["area"]) / max(a["area"], b["area"]) > 0.13:
                continue
            if abs(a["w"] - b["w"]) > 6 or abs(a["h"] - b["h"]) > 6:
                continue
            pairs.append((a, b))
    return pairs


def pick_hole(cands: list[dict], strategy: str, bg_size: tuple[int, int],
              piece_mask: Optional[np.ndarray] = None) -> Optional[dict]:
    """
    选目标洞：
      1. 若拿到拼图块蒙版，用形状 IoU 排序（洞的形状与拼图块一致，影子不是）
      2. 再按位置策略做 tie-break
    """
    if not cands:
        return None
    bw, bh = bg_size

    def inner(c):
        return c["x0"] > 6 and c["y0"] > 6 and c["x1"] < bw - 6 and c["y1"] < bh - 6

    scored = []
    for c in cands:
        iou = shape_iou(piece_mask, c["mask"]) if piece_mask is not None else 0.0
        scored.append((iou, c))
    # 主排序：IoU 降序；同分时按策略
    if strategy == "right":
        scored.sort(key=lambda t: (-round(t[0], 2), -t[1]["x0"], t[1]["y0"]))
    elif strategy == "inner":
        scored.sort(key=lambda t: (-round(t[0], 2), 0 if inner(t[1]) else 1, t[1]["y0"]))
    else:
        scored.sort(key=lambda t: (-round(t[0], 2), t[1]["y0"], -t[1]["x0"]))
    log.debug("候选打分: %s", [(round(s, 3), c["x0"], c["y0"]) for s, c in scored])
    return scored[0][1]


# ------------------------------------------------------------------ 几何
async def read_geometry(page) -> dict:
    return await page.evaluate(r"""() => {
        const q = s => document.querySelector(s);
        const g = e => { if (!e) return null; const r = e.getBoundingClientRect();
            return {x:r.x, y:r.y, w:r.width, h:r.height}; };
        const puzzle = q('[class*=slider-puzzle]');
        const bg = q('img[class*=slider-background]');
        const pc = q('[class*=slider-puzzle] img');
        let pzX = 0;
        if (puzzle) { const m = puzzle.style.transform.match(/translate3d\(\s*(-?[\d.]+)px/); if (m) pzX = parseFloat(m[1]); }
        return {stage: g(q('[class*=slider-stage]')), puzzle: g(puzzle), handle: g(q('[data-captcha-role="slider-handle"]')),
                track: g(q('[class*=slider-track]')), puzzleX: pzX,
                bgUrl: bg ? bg.src : null, pieceUrl: pc ? pc.src : null,
                bgNat: bg ? [bg.naturalWidth, bg.naturalHeight] : [0, 0],
                text: (q('[class*=slider-track-text]')?.innerText || '').slice(0, 60)};
    }""")


async def puzzle_x(page) -> Optional[float]:
    return await page.evaluate(r"""() => {
        const p = document.querySelector('[class*=slider-puzzle]');
        if (!p) return null;
        const m = p.style.transform.match(/translate3d\(\s*(-?[\d.]+)px/);
        return m ? parseFloat(m[1]) : null;
    }""")


# ------------------------------------------------------------------ 拖动
async def _drag_rest(page, sx: float, sy: float, start: float, target: float) -> None:
    cur = start
    while cur < target - 1:
        remain = target - cur
        step = max(1.0, min(remain, random.uniform(2.5, 9.0) * (1 + remain / max(target, 1))))
        cur = min(target, cur + step)
        await page.mouse.move(sx + cur, sy + random.uniform(-1.0, 1.0), steps=1)
        await page.wait_for_timeout(max(8, int(step * random.uniform(1.8, 3.2))))


# ------------------------------------------------------------------ 单次求解
async def _solve_once(page, assets_dir: Path, strategy: str, log_=log) -> bool:
    # 等背景图真正加载出来（题库可能加载几秒）
    for _ in range(25):
        nw = await page.evaluate(
            "() => { const b=document.querySelector('img[class*=slider-background]');"
            " return b ? (b.naturalWidth||0) : 0; }")
        if nw:
            break
        await page.wait_for_timeout(600)
    else:
        log_.error("背景图始终未加载")
        return False
    await page.wait_for_timeout(600)

    geo = await read_geometry(page)
    if not geo.get("bgUrl") or not geo.get("pieceUrl"):
        log_.error("拼图资源缺失")
        return False

    stem = str(abs(hash(geo["bgUrl"])))[:8]
    bg = await _fetch_asset(page, geo["bgUrl"], assets_dir / f"bg_{stem}.png")
    pc = await _fetch_asset(page, geo["pieceUrl"], assets_dir / f"piece_{stem}.png")

    bw, bh = geo["bgNat"]
    bw = bw or 560
    bh = bh or 336
    cands = find_holes(bg, pc)
    pmask = _piece_mask(pc) if pc.exists() else None
    hole = pick_hole(cands, strategy, (bw, bh), pmask)
    if not hole:
        log_.error("未找到缺口候选（策略 %s，候选 %d 个）", strategy, len(cands))
        return False

    log_.info("策略=%s 候选=%d 选用 x0=%d y0=%d %dx%d", strategy, len(cands),
              hole["x0"], hole["y0"], hole["w"], hole["h"])

    stage_w = geo["stage"]["w"]
    pz_w = geo["puzzle"]["w"]
    handle = geo["handle"]
    track_w = geo["track"]["w"]
    handle_w = handle["w"]
    pz0 = geo["puzzleX"] or 0.0

    scale = stage_w / bw
    target_css = hole["x0"] * scale
    max_pz = max(stage_w - pz_w, 1.0)
    max_h = max(track_w - handle_w, 1.0)
    target_pz = max(0.0, min(target_css - pz0, max_pz))

    # ---- 标定 k：先拖 24px 读实际位移
    sx = handle["x"] + handle_w / 2
    sy = handle["y"] + handle["h"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(120, 280))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(60, 150))
    await page.mouse.move(sx + 24, sy, steps=8)
    await page.wait_for_timeout(160)
    x1 = await puzzle_x(page)
    if x1 is None or abs(x1 - pz0) < 1.5:
        # 位移没读到，多拖一点再读
        await page.mouse.move(sx + 40, sy, steps=6)
        await page.wait_for_timeout(200)
        x1 = await puzzle_x(page)
        if x1 is not None and abs(x1 - pz0) >= 1.5:
            k = (x1 - pz0) / 40.0
            moved0 = 40.0
        else:
            k = max_pz / max_h
            moved0 = 24.0
    else:
        k = (x1 - pz0) / 24.0
        moved0 = 24.0
    k = max(0.4, min(k, 2.5))

    moved = moved0
    cur_px = x1 if x1 is not None else pz0 + moved0 * k
    err_pz = target_pz - (cur_px - pz0)
    need_h = min(max_h, max(moved0, moved0 + err_pz / k))
    await _drag_rest(page, sx, sy, moved0, need_h)
    moved = need_h
    await page.wait_for_timeout(220)
    fx = await puzzle_x(page)

    # 兜底：拼图块根本没动（常见于页面有浮层/事件丢失），松开重拖一次
    if fx is not None and abs(fx - pz0) < 5 and target_pz > 20:
        log_.warning("拖动未生效(pz=%.1f)，重试一次", fx)
        await page.mouse.up()
        await page.wait_for_timeout(900)
        hb2 = await page.evaluate(r"""() => { const h=document.querySelector('[data-captcha-role="slider-handle"]');
            const r=h.getBoundingClientRect(); return {x:r.x,y:r.y,w:r.width,h:r.height}; }""")
        sx = hb2["x"] + hb2["w"] / 2
        sy = hb2["y"] + hb2["h"] / 2
        await page.mouse.move(sx, sy)
        await page.wait_for_timeout(180)
        await page.mouse.down()
        await page.wait_for_timeout(120)
        await _drag_rest(page, sx, sy, 0.0, min(max_h, target_pz / k))
        moved = min(max_h, target_pz / k)
        await page.wait_for_timeout(220)
        fx = await puzzle_x(page)

    # 只有明显偏差才做一次小幅修正（±10px 内不动）
    if fx is not None:
        err2 = target_pz - (fx - pz0)
        if abs(err2) > 10:
            delta = max(-40.0, min(40.0, err2 / k))
            log_.info("末次修正: 误差 %.1fpx -> 移动 %.1fpx", err2, delta)
            await _drag_rest(page, sx, sy, moved, moved + delta)
            moved += delta
            await page.wait_for_timeout(200)
            fx = await puzzle_x(page)

    log_.info("targetCss=%.1f pz0=%.2f targetPz=%.1f k=%.3f 总拖动 %.1fpx 当前pz=%.1f",
              target_css, pz0, target_pz, k, moved, (fx or 0))
    await page.wait_for_timeout(random.randint(180, 400))
    await page.mouse.up()
    await page.wait_for_timeout(3200)

    ok = not await page.evaluate("() => !!document.querySelector('[class*=captcha-modal]')")
    log_.info("滑块结果: %s", "通过" if ok else "未通过")
    return ok


# ------------------------------------------------------------------ 对外
async def captcha_alive(page) -> bool:
    """滑块弹窗是否已出现（先看容器，图可能还在加载）"""
    return await page.evaluate(
        "() => !!document.querySelector('[class*=slider-stage],[class*=captcha-modal]')")


async def wait_captcha(page, timeout: float = 20.0) -> bool:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if await captcha_alive(page):
            return True
        await page.wait_for_timeout(600)
    return False


def _load_best_strategy(assets_dir: Path) -> str:
    try:
        d = json.loads((assets_dir / STRATEGY_FILE).read_text(encoding="utf-8"))
        s = d.get("strategy")
        if s in STRATEGIES:
            return s
    except Exception:
        pass
    return STRATEGIES[0]


def _save_best_strategy(assets_dir: Path, strategy: str) -> None:
    p = assets_dir / STRATEGY_FILE
    try:
        d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        d["strategy"] = strategy
        d["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


async def solve_captcha(page, assets_dir: Path, retries: int = 6, log_=log) -> bool:
    """滑块出现后自动求解；失败则换一换并切换策略"""
    assets_dir.mkdir(parents=True, exist_ok=True)
    best = _load_best_strategy(assets_dir)
    order = [best] + [s for s in STRATEGIES if s != best]

    for i in range(1, retries + 1):
        if not await wait_captcha(page, timeout=20):
            log_.info("未出现滑块，视为无需验证")
            return True
        strategy = order[(i - 1) % len(order)]
        log_.info("滑块第 %d/%d 次，策略 %s", i, retries, strategy)
        if await _solve_once(page, assets_dir, strategy, log_):
            _save_best_strategy(assets_dir, strategy)
            return True
        try:
            await page.click('[data-captcha-role="slider-refresh"]', timeout=3000)
            await page.wait_for_timeout(1800)
        except Exception:
            log_.warning("刷新题目失败，等待自动重载")
            await page.wait_for_timeout(2500)
    return False
