# -*- coding: utf-8 -*-
"""
滑块拖拽离线自检：不需要浏览器、不需要接码，纯本地模拟。

用真实历史题目图（captcha_assets/bg_*.png + piece_*.png）在内存里搭一个假页面：
  - 假 mouse.move 会把「滑块位移 × k」写进 translate3d（k 可指定，可加取整量化）
  - 假 evaluate 应答 read_geometry / puzzle_x / 背景图 naturalWidth / 弹窗检测
  - 真跑 xhs_captcha._solve_once（cv2 定位 + 两点最小二乘标定 + 拟人轨迹）

输出：落点误差、轨迹统计（事件数/时长/停顿/纵向漂移），
并与旧版「单点 24px 标定」做同题对比，直观看到 k 误差被放大多少。

    python check_captcha_drag.py                        # 默认 k=0.87，量化 1px
    python check_captcha_drag.py --k 1.10 --quant 0.1
    python check_captcha_drag.py --matrix               # 扫多个 k 出总表
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).parent
ASSETS = ROOT / "captcha_assets"

STAGE = {"x": 24.0, "y": 46.0, "w": 352.0, "h": 211.0}
HANDLE = {"x": 20.0, "y": 292.0, "w": 56.0, "h": 44.0}
TRACK = {"x": 20.0, "y": 292.0, "w": 352.0, "h": 44.0}
PUZZLE_W = 58.0
NAT = (560, 336)
PIECE_NAT = (58, 58)      # 假页面里 piece 元素与原图 1:1（元素 58px 宽）


# ------------------------------------------------------------------ 假浏览器
class _Resp:
    def __init__(self, body: bytes):
        self.ok = True
        self._body = body

    async def body(self) -> bytes:
        return self._body


class _Request:
    def __init__(self, page):
        self._page = page

    async def get(self, url, timeout=None, headers=None):
        f = self._page.url_map.get(url)
        if not f:
            raise RuntimeError(f"unknown url {url}")
        return _Resp(Path(f).read_bytes())


class _Ctx:
    def __init__(self, page):
        self.request = _Request(page)


class _Mouse:
    def __init__(self, page):
        self._p = page

    async def move(self, x, y, steps=1):
        self._p.on_move(float(x), float(y))

    async def down(self):
        self._p.down_at()

    async def up(self):
        self._p.up_at()


class FakePage:
    """
    k：真实映射系数；quant：translate3d 取整粒度（1.0 = 只认整数 px）；
    warp：映射非线性程度（k 随拖动距离漂移），用来考标定残差自检和纠正回路
    """

    def __init__(self, bg: Path, piece: Path, k: float = 0.87, quant: float = 1.0,
                 warp: float = 0.0):
        self.k, self.quant, self.warp = float(k), float(quant), float(warp)
        self.pz0 = 0.0
        self.pz = 0.0
        self.down = False
        self.down_x = 0.0
        self.mouse = _Mouse(self)
        self.context = _Ctx(self)
        self.url_map = {"http://fixture/bg.png": bg, "http://fixture/piece.png": piece}
        self.events: list[tuple[float, float, float]] = []   # (t_ms, x, y)
        self.waits: list[int] = []
        self.t = 0.0
        self.down_t = 0.0
        self.up_t = 0.0
        self.max_pz = STAGE["w"] - PUZZLE_W

    # -- 鼠标模型 ------------------------------------------------------
    def _k_at(self, offset: float) -> float:
        return self.k * (1.0 + self.warp * offset / 300.0)

    def on_move(self, x: float, y: float):
        self.events.append((self.t, x, y))
        if not self.down:
            return
        offset = x - self.down_x
        if self.warp:      # 非线性映射：分段按当前 k 积分
            prev = getattr(self, "_acc", self.pz0)
            prev_off = getattr(self, "_acc_off", 0.0)
            # 用中点 k 做增量，模拟「k 随位置漂移」
            k_mid = self._k_at((prev_off + offset) / 2.0)
            self.pz = prev + (offset - prev_off) * k_mid
            self._acc, self._acc_off = self.pz, offset
        else:
            self.pz = self.pz0 + offset * self.k
        raw = max(0.0, min(self.max_pz, self.pz))
        self.pz = round(raw / self.quant) * self.quant if self.quant else raw

    def down_at(self):
        self.down = True
        self.down_t = self.t
        self.down_x = self.events[-1][1] if self.events else 0.0
        self._acc, self._acc_off = self.pz0, 0.0

    def up_at(self):
        self.down = False
        self.up_t = self.t

    # -- DOM 模型 ------------------------------------------------------
    def _geometry(self):
        return {"stage": dict(STAGE),
                "puzzle": {"x": STAGE["x"] + self.pz, "y": STAGE["y"], "w": PUZZLE_W, "h": 58.0},
                "handle": dict(HANDLE), "track": dict(TRACK), "puzzleX": self.pz,
                "bgUrl": "http://fixture/bg.png", "pieceUrl": "http://fixture/piece.png",
                "bgNat": list(NAT), "text": ""}

    async def evaluate(self, js: str):
        if "bgUrl" in js:                       # read_geometry（内含 naturalWidth，必须先判）
            return self._geometry()
        if "naturalWidth" in js:
            return NAT[0]
        if "captcha-modal" in js:
            return False
        if "translate3d" in js:
            return self.pz
        if "slider-handle" in js and "getBoundingClientRect" in js:
            return dict(HANDLE)
        raise RuntimeError(f"unexpected evaluate: {js[:60]}")

    async def wait_for_timeout(self, ms):
        self.waits.append(int(ms))
        self.t += float(ms)

    async def click(self, sel, timeout=None):
        return None


# ------------------------------------------------------------------ 轨迹统计
def stats(events, waits, down_t: float, up_t: float) -> dict:
    """只统计按住到松手这一段（拖拽本身），排除等图/等弹窗的固定开销"""
    drag = [(t, x, y) for t, x, y in events if down_t <= t <= up_t]
    xs = [e[1] for e in drag]
    ys = [e[2] for e in drag]
    speeds = []
    for i in range(1, len(drag)):
        dt = max(drag[i][0] - drag[i - 1][0], 1.0)
        speeds.append(abs(drag[i][1] - drag[i - 1][1]) / dt)   # px/ms
    mid_pause = 0
    for i in range(1, len(drag)):
        gap = drag[i][0] - drag[i - 1][0]
        if 30 <= gap <= 200:
            mid_pause += 1
    peak_at = 0.0
    flat = 0.0
    if speeds:
        peak_i = max(range(len(speeds)), key=lambda i: speeds[i])
        peak_at = (peak_i + 1) / len(speeds) * 100
        med = sorted(speeds)[len(speeds) // 2]
        flat = sum(1 for v in speeds if abs(v - med) <= 0.15 * med) / len(speeds) * 100
    return {"events": len(drag), "drag_ms": int(up_t - down_t),
            "y_drift": round(max(ys) - min(ys), 2) if ys else 0.0,
            "pauses": mid_pause,
            "peak_at": round(peak_at), "flat": round(flat)}


# ------------------------------------------------------------------ 旧算法对照
async def old_engine_error(page: FakePage, target_pz: float) -> float:
    """复刻旧逻辑：单点拖 24px 读 k，再按 k 一次性拖到位"""
    sx = HANDLE["x"] + HANDLE["w"] / 2
    sy = HANDLE["y"] + HANDLE["h"] / 2
    await page.mouse.move(sx, sy)
    await page.mouse.down()
    await page.mouse.move(sx + 24, sy, steps=8)
    k_est = (page.pz - page.pz0) / 24.0
    await page.mouse.move(sx + target_pz / max(k_est, 1e-6), sy, steps=8)
    await page.mouse.up()
    return abs(page.pz - target_pz)


# ------------------------------------------------------------------ 主流程
def load_pair(sample: str) -> tuple[Path, Path]:
    bg, pc = ASSETS / f"bg_{sample}.png", ASSETS / f"piece_{sample}.png"
    if not bg.exists() or not pc.exists():
        raise SystemExit(f"样本不存在: {sample}")
    return bg, pc


def expected_target(sample: str, strategy: str = "top") -> float | None:
    """
    该题的正确落点：gap x0 × 舞台缩放，**再扣掉拼图块内容左侧的透明边**。

    题库里 piece 图 α 内容固定从 x=13 开始，元素左侧有 13px 透明带；
    只把元素左边缘对齐洞口会让图形整体偏右，所以基准必须与
    xhs_captcha._solve_once 里同一套算法保持一致。
    """
    import xhs_captcha_cv as C
    import xhs_captcha as cap
    g = C.find_gap(ASSETS / f"bg_{sample}.png", ASSETS / f"piece_{sample}.png", strategy)
    if not g:
        return None
    scale = STAGE["w"] / NAT[0]
    pc = ASSETS / f"piece_{sample}.png"
    off_nat = cap.piece_content_x0(pc)
    off_css = off_nat * (PUZZLE_W / PIECE_NAT[0]) if PIECE_NAT[0] else 0.0
    return max(0.0, min(g["x0"] * scale - off_css, STAGE["w"] - PUZZLE_W))


async def main_async(args) -> int:
    import logging
    import xhs_captcha as cap

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING,
                        format="%(levelname)s %(message)s")
    samples = args.sample if isinstance(args.sample, list) else [args.sample]
    k_list = [0.80, 0.87, 0.95, 1.05, 1.20, 1.37] if args.matrix else [args.k]

    rows = []
    for s in samples:
        bg, pc = load_pair(s)
        tgt = expected_target(s)
        if tgt is None:
            print(f"{s}: cv2 判不出缺口，跳过")
            continue
        for k in k_list:
            # ---- 新版：cv2 定位 + 两点最小二乘 + 拟人轨迹
            page = FakePage(bg, pc, k=k, quant=args.quant, warp=args.warp)
            with tempfile.TemporaryDirectory() as td:
                await cap._solve_once(page, Path(td), "top")
            err_new = abs(page.pz - tgt)
            st = stats(page.events, page.waits, page.down_t, page.up_t)

            # ---- 旧版：单点 24px 标定 + 匀速小步
            page2 = FakePage(bg, pc, k=k, quant=args.quant, warp=args.warp)
            err_old = await old_engine_error(page2, tgt)
            rows.append((s, k, tgt, page.pz, err_new, err_old, st))

    hdr = (f"{'样本':10s} {'k真值':>5s} {'目标pz':>7s} {'落点pz':>7s} {'新误差':>7s} "
           f"{'旧误差':>7s} {'事件':>4s} {'拖拽ms':>6s} {'y漂移':>6s} {'停顿':>4s} "
           f"{'峰速%':>5s} {'匀速%':>5s}")
    print(hdr)
    print("-" * 84)
    for s, k, tgt, pz, en, eo, st in rows:
        print(f"{s:10s} {k:5.2f} {tgt:7.1f} {pz:7.1f} {en:7.2f} {eo:7.2f} "
              f"{st['events']:4d} {st['drag_ms']:6d} {st['y_drift']:6.2f} {st['pauses']:4d} "
              f"{st['peak_at']:5d} {st['flat']:5d}")

    en = sorted(r[4] for r in rows)
    eo = sorted(r[5] for r in rows)
    tol = args.tol
    print(f"\n新引擎：中位误差 {en[len(en) // 2]:.2f}px，最大 {max(en):.2f}px，"
          f"≤{tol}px 命中 {sum(1 for x in en if x <= tol)}/{len(en)}")
    print(f"旧引擎：中位误差 {eo[len(eo) // 2]:.2f}px，最大 {max(eo):.2f}px，"
          f"≤{tol}px 命中 {sum(1 for x in eo if x <= tol)}/{len(eo)}")
    worst = max(rows, key=lambda r: r[4])
    if worst[4] > tol:
        print(f"注意：最差 {worst[0]} k={worst[1]} 误差 {worst[4]:.2f}px")
    return 0 if all(x <= tol for x in en) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", default="89245792", nargs="+")
    ap.add_argument("--k", type=float, default=0.87)
    ap.add_argument("--quant", type=float, default=1.0, help="translate3d 取整粒度，1.0=整数px")
    ap.add_argument("--tol", type=float, default=2.5, help="判定通过的最大落点误差(px)")
    ap.add_argument("--warp", type=float, default=0.0, help="映射非线性程度（0.3=远端 k 漂 30%%）")
    ap.add_argument("--matrix", action="store_true", help="扫 6 个 k 值")
    ap.add_argument("--verbose", action="store_true")
    parsed = ap.parse_args()
    sys.exit(asyncio.run(main_async(parsed)))
