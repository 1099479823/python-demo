# -*- coding: utf-8 -*-
"""多窗口各跑一题，统计新策略的真实通过率（窗口可用才计分）"""
import asyncio, sys, io, random, logging, time
import requests
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

import config as C
import xhs_captcha as CAP

API = "http://127.0.0.1:54345"
WINDOWS = sys.argv[1:] or ["a1e3102eabf341a9befb09b50fdf7703",
                           "2f4ed09e130d422da21d1cff770022ca",
                           "25f3e4f713d447dd996d922338af3fe9"]


async def one(pw, bid):
    name = bid[:8]
    try:
        r = requests.post(f"{API}/browser/open", json={"id": bid}, timeout=120).json()
        if not r.get("success"):
            print(f"[{name}] 开窗失败: {r.get('msg')}")
            return None
    except Exception as e:
        print(f"[{name}] 开窗异常: {str(e)[:60]}")
        return None

    b = await pw.chromium.connect_over_cdp(r["data"]["ws"])
    ctx = b.contexts[0]
    page = await ctx.new_page()
    try:
        await page.goto("https://www.rednote.com/login", wait_until="domcontentloaded", timeout=90000)
        t0 = time.time()
        while time.time() - t0 < 90:
            st = await page.evaluate("""() => ({c: !!document.querySelector('div.country-code-select'),
                                                p: !!document.querySelector('input[placeholder*=\"phone number\"]')})""")
            if st["c"] and st["p"]:
                break
            await page.wait_for_timeout(1500)
        else:
            print(f"[{name}] 页面 90s 未就绪（代理慢）")
            return None

        await page.click("div.country-code-select", timeout=15000)
        await page.wait_for_timeout(900)
        await page.fill('input[placeholder="Search country/region"]', "Thailand", timeout=8000)
        await page.wait_for_timeout(800)
        await page.click('div.country-item:has-text("Thailand")', timeout=8000)
        num = "9" + "".join(random.choice("0123456789") for _ in range(8))
        await page.fill('input[placeholder*="phone number"]', num)
        await page.click("span.code-button", timeout=10000)

        if not await CAP.wait_captcha(page, timeout=25):
            print(f"[{name}] 未出现滑块")
            return None

        ok = await CAP.solve_captcha(page, C.ASSETS_DIR, retries=2)
        print(f"[{name}] 结果: {'通过' if ok else '未通过'}")
        return ok
    except Exception as e:
        print(f"[{name}] 异常: {str(e)[:70]}")
        return None
    finally:
        try:
            await page.close()
            await b.close()
        except Exception:
            pass
        requests.post(f"{API}/browser/close", json={"id": bid}, timeout=30)


async def main():
    results = []
    async with async_playwright() as pw:
        for bid in WINDOWS:
            print(f"\n----- 窗口 {bid[:8]} -----")
            r = await one(pw, bid)
            results.append((bid, r))
            await asyncio.sleep(5)
    ok = [1 for _, r in results if r is True]
    fail = [1 for _, r in results if r is False]
    skip = [1 for _, r in results if r is None]
    print(f"\n=== 汇总: 通过 {len(ok)} / 失败 {len(fail)} / 无法测 {len(skip)} ===")
    for bid, r in results:
        print(f"  {bid[:8]}  {'通过' if r else ('未通过' if r is False else '窗口不可用')}")


asyncio.run(main())
