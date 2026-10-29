# -*- coding: utf-8 -*-
"""快速找出代理可用的窗口"""
import asyncio, sys, io
import requests
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
API = "http://127.0.0.1:54345"

CANDIDATES = ["9c718bb09ec84e169cd1902ebdc0dfcd", "eb1b02e167a84b21a31adeff6cbf9279",
              "34653ef1f48944d3a3936c1636a9bd59", "25f3e4f713d447dd996d922338af3fe9",
              "a1e3102eabf341a9befb09b50fdf7703", "2f4ed09e130d422da21d1cff770022ca"]


async def test(pw, name, bid):
    try:
        r = requests.post(f"{API}/browser/open", json={"id": bid}, timeout=90).json()
        if not r.get("success"):
            print(f"  {name}: 开窗失败 {r.get('msg')}")
            return False
        b = await pw.chromium.connect_over_cdp(r["data"]["ws"])
        ctx = b.contexts[0]
        page = await ctx.new_page()
        try:
            await page.goto("https://www.rednote.com/login", wait_until="domcontentloaded", timeout=45000)
            ok = await page.evaluate("() => !!document.querySelector('input[placeholder=\"Enter phone number\"]')")
            print(f"  {name}: {'可用' if ok else '页面异常'} {page.url[:60]}")
            return ok
        finally:
            await page.close()
            await b.close()
            requests.post(f"{API}/browser/close", json={"id": bid}, timeout=30)
    except Exception as e:
        print(f"  {name}: 异常 {str(e)[:60]}")
        try:
            requests.post(f"{API}/browser/close", json={"id": bid}, timeout=30)
        except Exception:
            pass
        return False


async def main():
    lst = requests.post(f"{API}/browser/list", json={"page": 0, "pageSize": 500}, timeout=30).json()["data"]["list"]
    names = {b["id"]: b["name"] for b in lst}
    async with async_playwright() as pw:
        for bid in CANDIDATES:
            if bid not in names:
                continue
            if await test(pw, names[bid], bid):
                print("\n可用窗口:", names[bid], bid)
                return


asyncio.run(main())
