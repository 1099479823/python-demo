# -*- coding: utf-8 -*-
"""诊断：登录页到底渲染成什么样了（结构变了还是加载慢）"""
import asyncio, pathlib, sys, io, json
import requests
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
API = "http://127.0.0.1:54345"
OUT = pathlib.Path("probe")
BID = sys.argv[1] if len(sys.argv) > 1 else "34653ef1f48944d3a3936c1636a9bd59"


async def main():
    r = requests.post(f"{API}/browser/open", json={"id": BID}, timeout=90).json()
    print("open:", r.get("success"), r.get("msg", ""))
    if not r.get("success"):
        return
    async with async_playwright() as p:
        b = await p.chromium.connect_over_cdp(r["data"]["ws"])
        ctx = b.contexts[0]
        page = await ctx.new_page()
        page.on("response", lambda x: print("  ", x.status, x.url[:100])
                if any(k in x.url for k in ("/login", "zones", "send_code")) else None)

        try:
            await page.goto("https://www.rednote.com/login", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            print("goto 失败:", str(e)[:100])
            return

        for i in range(30):
            await page.wait_for_timeout(2000)
            st = await page.evaluate(r"""() => ({
                url: location.href,
                inputs: [...document.querySelectorAll('input')].map(e => ({
                    type: e.type, ph: e.placeholder || '', name: e.name || '',
                    vis: e.getBoundingClientRect().width > 0})),
                country: !!document.querySelector('div.country-code-select, span.country-code'),
                len: document.body.innerText.length,
                head: document.body.innerText.slice(0, 150).replace(/\n/g, ' / ')
            })""")
            if i % 3 == 0:
                print(f"  t={i*2}s len={st['len']} country={st['country']} inputs={len(st['inputs'])} | {st['head'][:90]}")
            if st["country"] or len(st["inputs"]) >= 2:
                print("  就绪，用时", (i + 1) * 2, "s")
                break

        await page.screenshot(path=str(OUT / "login_now.png"))
        print("\nurl:", page.url)
        print("inputs:", json.dumps(st["inputs"], ensure_ascii=False))
        print("body:", st["head"])
        await page.close()
        await b.close()

asyncio.run(main())
