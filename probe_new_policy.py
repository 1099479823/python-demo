# -*- coding: utf-8 -*-
"""一次打开窗口 → 等页面真正就绪 → 触发滑块 → 用新策略求解（全程不关窗）"""
import asyncio, sys, io, random, logging, time
import requests
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")

import config as C
import xhs_captcha as CAP

API = "http://127.0.0.1:54345"
BID = sys.argv[1] if len(sys.argv) > 1 else "34653ef1f48944d3a3936c1636a9bd59"
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 2


async def main():
    r = requests.post(f"{API}/browser/open", json={"id": BID}, timeout=120).json()
    print("开窗:", r.get("success"), r.get("msg", ""))
    if not r.get("success"):
        return

    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(r["data"]["ws"])
        ctx = b.contexts[0]
        page = await ctx.new_page()

        print("打开登录页，等页面真正渲染（最多 120s，不提前退出）...")
        try:
            await page.goto("https://www.rednote.com/login", wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            print("goto 异常:", str(e)[:80])

        t0 = time.time()
        ready = False
        while time.time() - t0 < 120:
            st = await page.evaluate(r"""() => ({
                url: location.href,
                country: !!document.querySelector('div.country-code-select'),
                phone: !!document.querySelector('input[placeholder*="phone number"]'),
                inputs: document.querySelectorAll('input').length
            })""")
            if st["country"] and st["phone"]:
                ready = True
                print(f"页面就绪，用时 {time.time()-t0:.0f}s | {st['url']}")
                break
            await page.wait_for_timeout(2000)
        if not ready:
            print(f"120s 仍未就绪，当前: {st}")
            await page.screenshot(path="probe/not_ready.png")
            return          # 不关窗口，保留现场

        # 切泰国 → 填假号 → 发码
        await page.click("div.country-code-select", timeout=15000)
        await page.wait_for_timeout(1000)
        await page.fill('input[placeholder="Search country/region"]', "Thailand", timeout=8000)
        await page.wait_for_timeout(900)
        await page.click('div.country-item:has-text("Thailand")', timeout=8000)
        print("区号:", (await page.inner_text("div.country-code-select")).strip())

        num = "9" + "".join(random.choice("0123456789") for _ in range(8))
        await page.fill('input[placeholder*="phone number"]', num)
        await page.wait_for_timeout(500)
        await page.click("span.code-button", timeout=10000)
        print(f"已点发码（假号 {num}），等滑块")

        if not await CAP.wait_captcha(page, timeout=25):
            print("未出现滑块（可能直接返回错误，或号码被判无效）")
            await page.screenshot(path="probe/no_slider.png")
            return          # 不关窗口

        # 用新策略求解
        print("\n" + "=" * 50)
        ok = await CAP.solve_captcha(page, C.ASSETS_DIR, retries=ROUNDS)
        print("=" * 50)
        print("滑块结果:", "通过" if ok else "未通过")
        await page.screenshot(path="probe/new_policy_result.png")
        print("窗口保持打开，未关闭。")

asyncio.run(main())
