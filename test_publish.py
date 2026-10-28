# -*- coding: utf-8 -*-
"""用已登录的窗口直接验证发帖模块（图片/视频）"""
import asyncio, json, sys, io, pathlib
import requests
from playwright.async_api import async_playwright

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
import config as C
import xhs_publish as P
from xhs_login import bit_open, bit_close

BID = sys.argv[1] if len(sys.argv) > 1 else "0a0e67d658d54002ad80fd90780f2812"
FILE = sys.argv[2] if len(sys.argv) > 2 else r"D:\素材\图片\· 薄雾 自然森林 春天 小径 4K风景壁纸_小鱼素材.jpg"
TITLE = sys.argv[3] if len(sys.argv) > 3 else "测试标题"
CONTENT = sys.argv[4] if len(sys.argv) > 4 else "这是一条测试内容"


async def main():
    opened = bit_open(BID)
    print("window:", opened.get("name"), opened["ws"])
    try:
        async with async_playwright() as p:
            b = await p.chromium.connect_over_cdp(opened["ws"])
            ctx = b.contexts[0]
            page = await ctx.new_page()

            logged = await P.is_logged_in(page)
            print("creator 登录态:", logged, "| url:", page.url)
            if not logged:
                print("未登录，跳过发帖测试")
                return 2

            r = await P.publish_post(page, FILE, TITLE, CONTENT, ["#测试"])
            print("\n结果:", json.dumps(r, ensure_ascii=False, indent=2))
            await page.screenshot(path=str(pathlib.Path("probe") / "publish_result.png"))
            await page.close()
            await b.close()
            return 0 if r.get("ok") else 1
    finally:
        bit_close(BID)


sys.exit(asyncio.run(main()))
