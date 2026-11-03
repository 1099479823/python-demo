# -*- coding: utf-8 -*-
"""
Excel 驱动的批量发帖

Excel 列：id | path | title | content | tag
    id       窗口序号（对应「小红书测试{id}」）
    path     视频或图片的本地路径（按后缀自动判断发视频还是图文）
    title    标题（≤20 字）
    content  正文（≤1000 字）
    tag      标签，多个用空格/逗号分隔，可带 # 也可不带

每个窗口的流程：
    开窗 -> 检查登录 -> 未登录则注册登录（复用 xhs_login）
         -> 按后缀发视频/图文 -> 成功关页面 / 失败(账号异常)则退出重登再发一次

用法:
    python xhs_post.py                          # 跑 Excel 里所有行
    python xhs_post.py --only 100,99            # 只发指定窗口序号
    python xhs_post.py --only 85,90-95          # 支持区间写法
    python xhs_post.py --skip-publish           # 只填内容不点发布（验证流程）
    python xhs_post.py --no-register            # 未登录就跳过，不自动注册
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import sys
import time
from pathlib import Path

import requests
from openpyxl import load_workbook
from playwright.async_api import async_playwright

import config as C
import xhs_login as XL
import xhs_publish as P
from sms_factory import current_service, make_sms_client
from tiger_sms import TigerSMS, strip_country_prefix
from xhs_login import (bit_close, bit_open, handle_onboarding, logged_in,
                       open_login, record_success, select_country,
                       try_one_number, window_seq)

log = logging.getLogger("post")

# 路径统一由 config 管理（.env 里的 POST_EXCEL，默认桌面/测试.xlsx）
DEFAULT_EXCEL = C.POST_EXCEL
RESULT_FILE = Path("post_results.json")

ACCOUNT_BLOCK_HINTS = ("您的账号处于禁言状态", "账号处于禁言", "已被禁言", "被禁言",
                        "已被封禁", "被封禁", "封号", "账号异常", "账号受限",
                        "禁止发布", "禁止发笔记", "禁止发帖", "禁止发表",
                        "违反社区规则", "违反社区公约", "社区规则",
                        "无法发布", "不能发布", "暂不支持发布",
                        "存在风险", "功能受限", "账号冻结")


# ------------------------------------------------------------------ Excel
def read_excel(path: str) -> list[dict]:
    wb = load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    header = [str(c).strip().lower() if c is not None else "" for c in rows[0]]
    idx = {name: header.index(name) for name in ("id", "path", "title", "content", "tag")
           if name in header}
    missing = {"id", "path"} - set(idx)
    if missing:
        raise RuntimeError(f"Excel 缺少列: {missing}，实际表头: {header}")

    out = []
    for r in rows[1:]:
        if r is None or all(c is None or str(c).strip() == "" for c in r):
            continue
        def cell(name):
            i = idx.get(name)
            if i is None or i >= len(r) or r[i] is None:
                return ""
            return str(r[i]).strip()
        rid = cell("id")
        if not rid:
            continue
        try:
            seq = int(float(re.sub(r"[^\d.]", "", rid)))
        except ValueError:
            continue
        tags = re.split(r"[,\s，、]+", cell("tag"))
        out.append({"seq": seq, "path": cell("path"), "title": cell("title"),
                    "content": cell("content"), "tags": [t for t in tags if t]})
    return out


# ------------------------------------------------------------------ 窗口
def find_browser_id(seq: int) -> str | None:
    """在配置的分组（BIT_BROWSER_GROUP）里按窗口名精确找 id"""
    import bit_client
    want = f"小红书测试{seq}"
    try:
        # 先用接口的 name 模糊过滤缩小分页范围，再精确比对
        items = bit_client.list_browsers(group_name=C.BROWSER_GROUP or None, name=want)
    except Exception as e:
        log.error("拉取窗口列表失败: %s", e)
        return None
    for b in items:
        if (b.get("name") or "").strip() == want:
            return b["id"]
    return None


# ------------------------------------------------------------------ 登录
async def ensure_login(page, ctx, cli: TigerSMS, browser_id: str, win_name: str,
                       args) -> tuple[bool, dict]:
    """确认这个窗口能进 creator 发布页；不能则走注册登录"""
    if await P.is_logged_in(page, ctx=ctx):
        log.info("窗口已登录")
        return True, {}

    if args.no_register:
        return False, {"reason": "未登录且 --no-register 不允许注册"}

    log.info("未登录，开始注册流程")
    await open_login(page)
    if "login" not in page.url:
        await handle_onboarding(page)
        return True, {}

    # 多国家回退：按当前可用号码数排序（号多的先用），接不到再换下一个
    for country in XL.order_countries(cli, current_service()):
        cn = country.get("cn", country["label"])
        cur = await select_country(page, country["label"], country["code"])
        if country["code"] not in cur:
            log.error("%s 区号切换失败: %s", cn, cur)
            continue
        log.info("== 使用国家 %s（区号 %s / 平台ID %s）==", cn, cur, country["id"])

        # 每国固定试 N 个号（默认 3）：等码 90s 超时 → 换号
        for n in range(1, args.max_numbers + 1):
            log.info("=== %s 第 %d/%d 个号码 ===", cn, n, args.max_numbers)
            try:
                r = await try_one_number(page, cli, current_service(), country, args.wait_seconds, ctx=ctx)
            except XL.NoNumbersError as e:
                log.warning("%s 无库存，换下一个国家: %s", cn, e)
                break
            except Exception as e:
                log.warning("%s 第 %d 个号异常: %s", cn, n, e)
                r = {"ok": False, "reason": f"exception:{e}"}

            if r.get("ok"):
                # try_one_number 内部已做严格校验（页面离站 + cookie）。
                # 登录后可能停在带 login 的过渡/引导 URL，这里只确认 cookie 落盘，
                # 不再用页面 URL 二次误杀；引导页能跳就跳，跳不动也不影响发帖。
                ok_c, info = await XL.has_login_cookie(ctx)
                for _ in range(4):
                    if ok_c:
                        break
                    await page.wait_for_timeout(1000)
                    ok_c, info = await XL.has_login_cookie(ctx)
                if ok_c:
                    await handle_onboarding(page)
                    record_success(browser_id, win_name, r.get("phone") or "", r.get("code") or "")
                    log.info("%s 注册登录成功: %s | %s", cn, page.url, XL.describe_cookies(info))
                    return True, r
                log.warning("接码成功但浏览器未写入 web_session: %s", XL.describe_cookies(info))
                r = {"ok": False, "reason": "login_verify_failed:no_cookie"}

            log.warning("%s 第 %d/%d 个号失败: %s", cn, n, args.max_numbers, r.get("reason"))
            try:
                await open_login(page)
                await select_country(page, country["label"], country["code"])
            except Exception as e:
                log.warning("重置登录页失败: %s", e)

    # 两国都拿不到号/收不到码：不是这一台的问题，继续跑没意义，直接停
    return False, {"reason": "当前时段无手机号可用（泰国与美国均取不到号或收不到验证码）",
                   "no_numbers": True}


# ------------------------------------------------------------------ 单行
async def run_row(pw, cli: TigerSMS, row: dict, args) -> dict:
    seq = row["seq"]
    res = {"seq": seq, "path": row["path"], "title": row["title"],
           "ok": False, "reason": "", "kind": P.kind_of(row["path"])}

    if not Path(row["path"]).exists():
        res["reason"] = f"文件不存在: {row['path']}"
        log.error(res["reason"])
        return res

    bid = find_browser_id(seq)
    if not bid:
        res["reason"] = f"找不到窗口 小红书测试{seq}"
        log.error(res["reason"])
        return res
    res["browser_id"] = bid

    opened = bit_open(bid)
    win_name = opened.get("name") or f"小红书测试{seq}"
    try:
        browser = await pw.chromium.connect_over_cdp(opened["ws"])
        ctx = browser.contexts[0]
        page = await ctx.new_page()
        # 注册发码响应监听（try_one_number 依赖它判断是否真的发出去了）
        page.on("response", lambda r: asyncio.ensure_future(XL._trace_response(r)))

        ok, info = await ensure_login(page, ctx, cli, bid, win_name, args)
        if not ok:
            res.update({"reason": info.get("reason") or "登录失败"})
            if info.get("no_numbers"):
                res["no_numbers"] = True
            return res

        if args.skip_publish:
            log.info("[skip-publish] 只验证到编辑态")
            res.update({"ok": True, "reason": "skip-publish"})
            return res

        # 账号被禁言/封禁 -> 退出换新号重试，直到能正常发布或达到上限
        max_retry = getattr(C, "POST_ACCOUNT_RETRIES", 6)
        retried = 0
        pr = None
        while True:
            pr = await P.publish_post(page, row["path"], row["title"],
                                      row["content"], row["tags"])
            res.update({"ok": pr["ok"], "reason": pr.get("reason", ""),
                        "account_retries": retried})
            log.info("发帖结果: %s | %s (第 %d 次尝试)",
                     "成功" if pr["ok"] else "失败", pr.get("reason"), retried + 1)

            if pr["ok"]:
                break
            if not any(k in (pr.get("reason") or "") for k in ACCOUNT_BLOCK_HINTS):
                break                        # 非账号问题（内容被拒/上传失败），不换号
            if retried >= max_retry:
                log.warning("账号连续被禁/封 %d 次，放弃本条", retried + 1)
                break

            log.warning("账号异常（%s），退出并注册新号重试 (%d/%d)",
                        pr.get("reason"), retried + 1, max_retry)
            retried += 1
            await P.logout(ctx)
            ok2, info2 = await ensure_login(page, ctx, cli, bid, win_name, args)
            if not ok2:
                res.update({"ok": False,
                            "reason": f"换号重登失败: {info2.get('reason')}（原账号异常: {pr.get('reason')}）",
                            "account_retries": retried})
                break
            await page.wait_for_timeout(1500)

        if retried:
            res["retried"] = True
            res["account_retries"] = retried
            if pr and pr.get("ok"):
                res["reason"] = f"换号成功(共试 {retried + 1} 个账号)后发布成功"
                log.info("换号重发成功: 共试 %d 个账号", retried + 1)
            elif pr:
                res["reason"] = f"连续 {retried + 1} 个账号均被禁/封: {pr.get('reason')}"
                log.warning("连续 %d 个账号均被禁/封: %s", retried + 1, pr.get("reason"))

        # 关闭发帖页面
        try:
            await page.close()
        except Exception:
            pass
        await browser.close()
    finally:
        bit_close(bid)
    return res


# ------------------------------------------------------------------ 主流程
async def main(args) -> int:
    rows = read_excel(args.excel)
    if args.only:
        want = XL.parse_seq_spec(args.only)      # 支持 85,86,90-95
        if not want:
            print(f"--only {args.only!r} 解析不到任何序号，示例：85,86,90-95")
            return 2
        before = len(rows)
        rows = [r for r in rows if r["seq"] in want]
        missing = sorted(want - {r["seq"] for r in rows})
        print(f"--only {args.only} -> Excel 命中 {len(rows)}/{before} 行"
              + (f"（Excel 里没有: {missing}）" if missing else ""))
    if not rows:
        print("Excel 没有可执行的行")
        return 2

    print(f"共 {len(rows)} 条待发：")
    for r in rows:
        print(f"  窗口{r['seq']:>4}  {P.kind_of(r['path']):6}  {Path(r['path']).name[:36]}  "
              f"标题={r['title']!r} 标签={r['tags']}")

    cli = make_sms_client()
    results = []
    async with async_playwright() as pw:
        for i, row in enumerate(rows, 1):
            log.info("=" * 20 + f" [{i}/{len(rows)}] 窗口{row['seq']} " + "=" * 20)
            t0 = time.time()
            try:
                r = await run_row(pw, cli, row, args)
            except Exception as e:
                log.exception("执行异常")
                r = {"seq": row["seq"], "ok": False, "reason": f"exception:{e}"}
            r["seconds"] = round(time.time() - t0, 1)
            results.append(r)
            RESULT_FILE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"  -> {'✅' if r['ok'] else '❌'} {r['reason']}  ({r['seconds']}s)")

            # 两国都无号：这是全局性问题，继续跑只是白等，直接停整个批次
            if r.get("no_numbers"):
                print("\n" + "!" * 62)
                print(r["reason"])
                print(f"已停止：完成 {len(results)}/{len(rows)} 条，剩下的等号码补货后再跑。")
                print("!" * 62)
                break

            if i < len(rows):
                await asyncio.sleep(args.sleep)

    okn = sum(1 for r in results if r["ok"])
    print(f"\n完成 {okn}/{len(results)} 成功，明细见 {RESULT_FILE.resolve()}")
    return 0 if okn == len(results) else 1


def build_parser():
    ap = argparse.ArgumentParser(description="Excel 驱动批量发帖")
    ap.add_argument("--excel", default=DEFAULT_EXCEL)
    ap.add_argument("--only", default="",
                    help="只发这些窗口序号，支持区间：85,86,90-95")
    ap.add_argument("--skip-publish", action="store_true", help="只填内容不点发布")
    ap.add_argument("--no-register", action="store_true", help="未登录不自动注册")
    ap.add_argument("--wait-seconds", type=int, default=C.CODE_WAIT_SECONDS)
    ap.add_argument("--max-numbers", type=int, default=C.MAX_NUMBERS)
    ap.add_argument("--sleep", type=float, default=C.POST_SLEEP, help="每条之间的间隔秒")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


if __name__ == "__main__":
    a = build_parser().parse_args()
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(name)-7s %(message)s",
                        datefmt="%H:%M:%S")
    sys.exit(asyncio.run(main(a)))
