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

实测映射: 拼图块位移 = 滑块位移 * k，k 随缩放变化（0.80~1.37），运行时两点最小二乘标定。
拖动: 最小加加速度速度曲线 + 步抖动 + 微停顿 + 过冲回拉，落点误差 >2px 才修正。
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import random
import time
import urllib.request
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image

log = logging.getLogger("captcha")

try:  # config 提供 CAPTCHA_* 参数（.env 会被 config 先加载）
    import config as C
except Exception:
    C = None


def _cfg_get(name, default):
    """安全读配置（config 不可用时走默认值）"""
    try:
        return getattr(C, name, default) if C is not None else default
    except Exception:
        return default

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


def piece_content_x0(piece_path: Path) -> float:
    """
    拼图块图形相对图片左边缘的偏移（原图像素）。

    实测：题库里 piece 图宽 88~94px，但 α 内容**一律从 x=13 开始**，
    即元素左侧有 13px 透明边。若按「元素左边缘 = 洞口左边界」算位移，
    图形会整体偏右 13px（视觉上大约超出框 1/6~1/4），必须扣掉。
    """
    try:
        pa = np.asarray(Image.open(piece_path).convert("RGBA"))
        a = pa[:, :, 3] > 120
        if not a.any():
            return 0.0
        return float(np.where(a)[1].min())
    except Exception:
        return 0.0


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
                pieceNat: pc ? [pc.naturalWidth, pc.naturalHeight] : [0, 0],
                text: (q('[class*=slider-track-text]')?.innerText || '').slice(0, 60)};
    }""")


async def puzzle_x(page) -> Optional[float]:
    return await page.evaluate(r"""() => {
        const p = document.querySelector('[class*=slider-puzzle]');
        if (!p) return null;
        const m = p.style.transform.match(/translate3d\(\s*(-?[\d.]+)px/);
        return m ? parseFloat(m[1]) : null;
    }""")


# ------------------------------------------------------------------ 拟人轨迹
def _min_jerk(t: float) -> float:
    """最小加加速度（minimum jerk）位移曲线：起止加速度为 0，人手拖动的经典模型"""
    return t * t * t * (10.0 + t * (-15.0 + 6.0 * t))


def _human_steps(distance: float, rng=None) -> list[tuple[float, float, int]]:
    """
    把一段位移拆成拟人步序列 [(dx, dy, dt_ms), ...]，dx 累加正好等于 distance。

    三个特征缺一个都容易被判脚本：
      * 速度曲线偏斜（加速快、收尾慢），不是匀速也不是对称加速
      * 步间隔抖动 + 2~3 次微停顿（人手换气/找位）
      * y 轴 ±1.6px 低频抖动（手腕不是直线电机）
    """
    rng = rng or random
    if distance <= 0.4:
        return []
    n = int(max(10, min(56, distance / 2.8 + rng.randint(3, 9))))
    skew = rng.uniform(0.80, 1.15)
    total_ms = min(1500.0, max(220.0, 120.0 + distance * 2.5)) * rng.uniform(0.85, 1.15)
    pauses = set(rng.sample(range(2, n - 1), k=min(3, max(1, n // 10)))) if n > 4 else set()

    out, prev_x = [], 0.0
    phase = rng.uniform(0.0, 6.28)
    for i in range(1, n + 1):
        t = (i / n) ** skew
        x = distance * _min_jerk(min(1.0, t))
        dx = max(0.0, x - prev_x)
        prev_x = x
        dt = total_ms / n * rng.uniform(0.70, 1.35)
        if i in pauses:
            dt += rng.uniform(30.0, 140.0)
        dy = 0.55 * math.sin(phase + i * 0.55) + rng.gauss(0.0, 0.35)
        out.append((dx, max(-1.6, min(1.6, dy)), int(max(6.0, dt))))
    if out:  # 残差补到最后一步，避免收尾跳变
        out[-1] = (out[-1][0] + max(0.0, distance - prev_x), out[-1][1], out[-1][2])
    return out


async def _drag_to(page, sx: float, sy: float, from_x: float, to_x: float, rng=None) -> float:
    """沿拟人轨迹把指针从 from_x 移到 to_x，返回到位后的偏移"""
    dist = to_x - from_x
    if abs(dist) < 0.3:
        return from_x
    sign = 1.0 if dist > 0 else -1.0
    cur = from_x
    for dx, dy, dt in _human_steps(abs(dist), rng):
        cur += sign * dx
        await page.mouse.move(sx + cur, sy + dy, steps=1)
        await page.wait_for_timeout(dt)
    return cur


async def _human_drag(page, sx: float, sy: float, start: float, target: float, rng=None) -> float:
    """
    一次完整拖动：主轨迹 -> 可能的过冲回拉 -> 收尾手抖找位。
    真人会冲过头再退回来，匀速直线才是脚本特征。
    """
    rng = rng or random
    dist = target - start
    os_px = 0.0
    if abs(dist) > 40 and rng.random() < 0.45:
        os_px = rng.uniform(2.0, 6.0) * (1.0 if dist > 0 else -1.0)

    cur = await _drag_to(page, sx, sy, start, target + os_px, rng)
    if abs(os_px) > 0.1:
        await page.wait_for_timeout(rng.randint(40, 110))
        segs = rng.randint(2, 4)
        back = abs(os_px)
        sign = -1.0 if os_px > 0 else 1.0
        for i in range(segs):                    # 回拉总位移严格等于 os_px
            take = back if i == segs - 1 else back * rng.uniform(0.45, 0.65)
            cur = await _drag_to(page, sx, sy, cur, cur + sign * take, rng)
            await page.wait_for_timeout(rng.randint(18, 60))
        cur = await _drag_to(page, sx, sy, cur, target, rng)
    for _ in range(rng.randint(1, 3)):                      # 收尾微抖
        await page.mouse.move(sx + cur + rng.uniform(-0.9, 0.9),
                              sy + rng.uniform(-0.8, 0.8), steps=1)
        await page.wait_for_timeout(rng.randint(12, 40))
    await page.mouse.move(sx + cur, sy, steps=1)            # 回正后松手
    return cur


async def _calibrate_k(page, sx: float, sy: float, pz0: float,
                       probes=(18.0, 46.0), log_=log):
    """
    多点标定映射系数 k（拼图位移 / 滑块位移）。

    旧做法只读一个 24px 点，translate3d 的取整误差会整个进 k，长距离时放大；
    这里拖两个距离分别读实际位移，做过原点最小二乘，并用残差自检：
    拟合残差 >25% 或 k 越界就判「标定不可靠」，由调用方按舞台宽度兜底。
    返回 (k|None, 已拖滑块位移, 最后一次读到的拼图位移)。
    """
    pts, cur, pz_last = [], 0.0, None
    for m in probes:
        cur = await _drag_to(page, sx, sy, cur, m)
        await page.wait_for_timeout(random.randint(50, 120))
        pz = await puzzle_x(page)
        if pz is None:
            continue
        pz_last = pz
        dz = pz - pz0
        if dz > 0.5:                     # 0.5px 以下视为还没生效，不进拟合
            pts.append((m, dz))
    if not pts:
        return None, cur, pz_last

    if len(pts) == 1:
        k = pts[0][1] / pts[0][0]
        resid = 0.0
    else:
        den = sum(m * m for m, _ in pts)
        k = sum(m * d for m, d in pts) / den if den else 0.0
        resid = max(abs(d - k * m) / max(d, 1e-6) for m, d in pts)
    k = float(k)
    log_.info("标定 k: %s -> k=%.3f 残差=%.0f%%",
              ", ".join(f"{m:.0f}px→{d:.1f}px" for m, d in pts), k, resid * 100)
    if not (0.4 <= k <= 2.5) or resid > 0.25:
        # 两点拟合不可信时，用最远那个点的单点估计兜底（比按舞台宽度猜准得多）
        k1 = pts[-1][1] / pts[-1][0]
        if 0.4 <= k1 <= 2.5 and resid <= 0.6:
            log_.warning("两点拟合不可靠(残差=%.0f%%)，改用单点估计 k=%.3f", resid * 100, k1)
            return float(k1), cur, pz_last
        log_.warning("标定失败(k=%.3f 残差=%.0f%%)，按舞台宽度兜底", k, resid * 100)
        return None, cur, pz_last
    return k, cur, pz_last


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

    # 缺口定位：OpenCV 内容匹配（旧 scipy 形状匹配方案已弃用）
    try:
        import xhs_captcha_cv as cvcap
        g = cvcap.find_gap(bg, pc, strategy)
    except Exception as e:
        log_.error("cv2 引擎异常: %s", e)
        return False
    if not g:
        log_.info("未定位到缺口（内容分过低或候选为空），换题")
        return False

    # 区分度太低就别拖了：拖一次失败会多消耗一次风控计数，直接换题更划算
    min_margin = float(_cfg_get("CAPTCHA_MIN_MARGIN", 0.05))
    if g["margin"] < min_margin:
        log_.info("区分度不足(score=%.3f margin=%.3f < %.2f)，放弃本题直接换题",
                  g["score"], g["margin"], min_margin)
        return False

    hole = {"x0": g["x0"], "y0": g["y0"], "w": g["w"], "h": g["h"]}
    n_cand = g["ncand"]
    log_.info("cv2 引擎: score=%.3f margin=%.3f scale=%.2f", g["score"], g["margin"], g["scale"])

    log_.info("策略=%s 候选=%d 选用 x0=%d y0=%d %dx%d", strategy, n_cand,
              hole["x0"], hole["y0"], hole["w"], hole["h"])

    stage_w = geo["stage"]["w"]
    pz_w = geo["puzzle"]["w"]
    handle = geo["handle"]
    track_w = geo["track"]["w"]
    handle_w = handle["w"]
    pz0 = geo["puzzleX"] or 0.0

    scale = stage_w / bw
    # 拼图块图形在元素内左侧有透明边（实测固定 13px），不扣掉会整体右移
    piece_nat_w = (geo.get("pieceNat") or [0, 0])[0] or pz_w
    off_nat = piece_content_x0(pc)
    piece_off_css = off_nat * (pz_w / piece_nat_w) if piece_nat_w else 0.0
    target_css = hole["x0"] * scale - piece_off_css
    max_pz = max(stage_w - pz_w, 1.0)
    max_h = max(track_w - handle_w, 1.0)
    target_pz = max(0.0, min(target_css - pz0, max_pz))
    if piece_off_css:
        log_.info("拼图内容左偏移 %.0fpx(原图) -> %.1fpx(CSS)，已从目标位移中扣除",
                  off_nat, piece_off_css)

    # ---- 标定 k：两点最小二乘（替代原来的单点 24px）
    sx = handle["x"] + handle_w / 2
    sy = handle["y"] + handle["h"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(120, 320))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(70, 180))

    # 近距离目标只探一个点（拖两次反而显得犹豫），远了才做两点拟合
    probes = ((round(max(4.0, target_pz * 0.40), 1),) if target_pz < 40 else
              (round(max(6.0, target_pz * 0.18), 1), round(max(10.0, target_pz * 0.45), 1)))
    k, moved, cur_pz = await _calibrate_k(page, sx, sy, pz0, probes=probes, log_=log_)
    if k is None:
        # 注意：滑块已经被探针拖走了 moved，不能假装从 0 开始
        k = max_pz / max_h
        cur_pz = cur_pz if cur_pz is not None else pz0
    err_pz = target_pz - (cur_pz - pz0)
    need_h = max(0.0, min(max_h, moved + err_pz / k))
    moved = await _human_drag(page, sx, sy, moved, need_h)
    await page.wait_for_timeout(random.randint(140, 260))
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
        moved = await _human_drag(page, sx, sy, 0.0, min(max_h, target_pz / k))
        await page.wait_for_timeout(220)
        fx = await puzzle_x(page)

    # 偏差 >2px 就修一次（真人也会小幅回拉找位），最多两轮
    for _ in range(2):
        fx = await puzzle_x(page)
        if fx is None:
            break
        err2 = target_pz - (fx - pz0)
        if abs(err2) <= 2.0:
            break
        delta = max(-30.0, min(30.0, err2 / k))
        log_.info("末次修正: 误差 %.1fpx -> 移动 %.1fpx", err2, delta)
        moved = await _human_drag(page, sx, sy, moved, max(0.0, min(max_h, moved + delta)))
        await page.wait_for_timeout(random.randint(120, 240))

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


async def solve_captcha(page, assets_dir: Path, retries: int = None, log_=log) -> bool:
    """
    滑块出现后自动求解。

    风控经验（实测）：30 秒内连换 3 题 + 连拖 3 次，轨迹特征高度雷同，
    会被判定为脚本，后面分数再高也一律不过。所以：
      * 同一弹窗最多试 CAPTCHA_RETRIES 次（默认 2）
      * 每次换题前停顿 3~5 秒，避免机械节奏
      * 仍然不过就返回 False，由调用方换窗口重来（而不是原地死磕）
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    if retries is None:
        retries = _cfg_get("CAPTCHA_RETRIES", 2)
    best = _load_best_strategy(assets_dir)
    order = [best] + [s for s in STRATEGIES if s != best]
    lo = float(_cfg_get("CAPTCHA_REFRESH_DELAY_MIN", 3.0))
    hi = float(_cfg_get("CAPTCHA_REFRESH_DELAY_MAX", 5.0))

    for i in range(1, retries + 1):
        if not await wait_captcha(page, timeout=20):
            log_.info("未出现滑块，视为无需验证")
            return True
        strategy = order[(i - 1) % len(order)]
        log_.info("滑块第 %d/%d 次，策略 %s", i, retries, strategy)
        if await _solve_once(page, assets_dir, strategy, log_):
            _save_best_strategy(assets_dir, strategy)
            return True

        if i >= retries:
            break
        # 换题前随机停顿，避免高频操作被风控
        pause = random.uniform(lo, hi)
        log_.info("换题前等待 %.1fs（降低风控触发概率）", pause)
        await page.wait_for_timeout(int(pause * 1000))
        try:
            await page.click('[data-captcha-role="slider-refresh"]', timeout=3000)
            await page.wait_for_timeout(random.randint(2200, 3600))
        except Exception:
            log_.warning("刷新题目失败，等待自动重载")
            await page.wait_for_timeout(2500)

    log_.warning("滑块 %d 次未通过，交给调用方换窗口（不要原地继续硬试）", retries)
    return False

