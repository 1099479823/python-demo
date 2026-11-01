# -*- coding: utf-8 -*-
"""
小红书 / RedNote 手机号自动登录（泰国 +66 + Tiger SMS 接码 + 滑动拼图验证）

流程:
    1. 比特浏览器指纹窗口 -> rednote.com/login
    2. 区号切到泰国 +66
    3. Tiger SMS 取号（号码去掉 66 前缀）写入输入框
    4. 点 Get code -> 出现滑动拼图 -> 自动识别缺口并拖动通过
    5. 轮询接码平台取码，90 秒没到 -> setStatus(8) 取消旧号 -> 换号重来
    6. 收到验证码 -> 填码 -> 勾协议 -> 点 Log in -> 校验登录态

用法:
    set TIGER_SMS_KEY=xxxx
    python xhs_login.py --browser-id 9c718bb09ec84e169cd1902ebdc0dfcd
    python xhs_login.py --browser-id xxx --dry-run     # 只到发码+过滑块，不点登录
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
from playwright.async_api import async_playwright, Page

import config as C
import xhs_captcha as captcha
from tiger_sms import TigerSMS, TigerError, strip_country_prefix

log = logging.getLogger("xhs")

# send_code 响应轨迹（每次尝试前清空）
SEND_TRACE: list[dict] = []


async def _trace_response(resp):
    if "send_code" in resp.url:
        try:
            body = await resp.text()
        except Exception:
            body = ""
        # 注意：小红书 HTTP 200 也可能是业务失败（{"success":false,"code":-13002}）
        compact = body.replace(" ", "")
        ok = '"success":true' in compact
        SEND_TRACE.append({"status": resp.status, "ok": ok,
                           "url": resp.url.split("?")[0], "body": body[:160]})


def _send_succeeded(trace: list[dict]) -> bool:
    """HTTP 200 且业务 success=true 才算真的发出短信"""
    return any(t.get("status") == 200 and t.get("ok") for t in trace)


def _send_rejected(trace: list[dict]) -> str:
    """200 但业务失败时返回错误码，如 -13002"""
    for t in trace:
        if t.get("status") == 200 and not t.get("ok"):
            m = re.search(r'"code"\s*:\s*(-?\d+)', t.get("body") or "")
            return m.group(1) if m else "unknown"
    return ""


# ------------------------------------------------------------------ 比特浏览器
def bit_open(browser_id: str) -> dict:
    """打开窗口，返回整个 data（含 ws / name / seq / pid）"""
    r = requests.post(f"{C.BIT_API}/browser/open", json={"id": browser_id}, timeout=120)
    j = r.json()
    if not j.get("success"):
        raise RuntimeError(f"打开窗口失败: {j.get('msg')}")
    return j["data"]


def bit_close(browser_id: str) -> None:
    try:
        requests.post(f"{C.BIT_API}/browser/close", json={"id": browser_id}, timeout=60)
    except Exception as e:
        log.warning("关闭窗口异常: %s", e)


def bit_first_browser(name_prefix: str = "") -> str:
    """按配置的分组（BIT_BROWSER_GROUP）拉窗口，取第一个匹配的"""
    import bit_client
    try:
        items = bit_client.list_browsers(group_name=C.BROWSER_GROUP or None)
    except Exception as e:
        log.error("拉取窗口列表失败: %s", e)
        raise
    if name_prefix:
        items = [x for x in items if name_prefix in (x.get("name") or "")]
    if not items:
        raise RuntimeError(f"分组 {C.BROWSER_GROUP!r} 下没有可用窗口")
    return items[0]["id"]


# ------------------------------------------------------------------ 页面动作
async def open_login(page: Page) -> None:
    """打开登录页并等表单真正渲染（代理慢时页面骨架会空白几秒）"""
    for _ in range(3):
        try:
            await page.goto(C.LOGIN_URL, wait_until="domcontentloaded", timeout=90000)
        except Exception as e:
            log.warning("打开登录页失败: %s", e)
            await page.wait_for_timeout(2500)
            continue
        for _ in range(20):
            await page.wait_for_timeout(1000)
            if "login" not in page.url:
                return                      # 已登录，跳走了
            if await page.locator(C.SEL["phone"]).count():
                return                      # 表单已就绪
            try:
                if await page.locator(C.SEL["login_entry"]).count():
                    await page.click(C.SEL["login_entry"], timeout=3000)
            except Exception:
                pass
        log.warning("登录表单未渲染，重试 (url=%s)", page.url)
    raise RuntimeError(f"登录页不可用: {page.url}")


async def select_country(page: Page, label: str, expect_code: str) -> str:
    """
    把登录页区号切到指定国家，返回当前区号文本（如 +66 / +1）。
    label 用国家英文名（Thailand / United States），expect_code 是期望前缀。
    """
    await page.click(C.SEL["country_box"], timeout=15000)
    await page.wait_for_timeout(random.randint(700, 1200))
    try:
        await page.fill(C.SEL["country_search"], label, timeout=5000)
        await page.wait_for_timeout(random.randint(600, 1000))
    except Exception:
        log.debug("区号搜索框不可用，直接滚动查找")

    item = page.locator(f'div.country-item:has-text("{label}")').first
    await item.scroll_into_view_if_needed(timeout=8000)
    await item.click(timeout=8000)
    await page.wait_for_timeout(random.randint(700, 1200))
    cur = (await page.inner_text(C.SEL["country_box"])).strip()
    if expect_code and expect_code not in cur:
        log.warning("区号可能不正确：期望 %s，实际 %s", expect_code, cur)
    return cur


async def select_thailand(page: Page) -> str:
    """兼容旧调用：切泰国 +66"""
    return await select_country(page, "Thailand", "+66")


async def code_button_text(page: Page) -> str:
    try:
        return (await page.inner_text(C.SEL["code_btn"])).strip()
    except Exception:
        return ""


async def send_code(page: Page) -> None:
    await page.click(C.SEL["code_btn"], timeout=8000)


async def submit_login(page: Page) -> None:
    await page.click(C.SEL["submit"], timeout=8000)
    await page.wait_for_timeout(1500)
    # 未勾协议时会弹“Agree and continue”
    try:
        btn = page.locator(C.SEL["agree_dialog_btn"]).first
        if await btn.count() and await btn.is_visible():
            log.info("出现协议确认弹窗，点击 Agree and continue")
            await btn.click(timeout=4000)
            await page.wait_for_timeout(1200)
    except Exception:
        pass


ONBOARD_HINT = ("Select your gender", "Tell us a bit about yourself",
                "Welcome to rednote", "选个性别", "完善资料")


async def on_onboarding(page: Page) -> bool:
    """是否停在新用户完善资料页（此时账号其实已注册成功）"""
    try:
        txt = await page.inner_text("body")
    except Exception:
        return False
    return any(k in txt for k in ONBOARD_HINT)


async def handle_onboarding(page: Page, log=log, rounds: int = 4) -> None:
    """尽量跳过性别/兴趣引导，把页面带到 feed"""
    for _ in range(rounds):
        if not await on_onboarding(page):
            break
        clicked = False
        # 选性别（新号必须选一次）
        for sel in ['text=Male', 'text=Female', 'div:has-text("Male")', 'div:has-text("Female")']:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    await page.wait_for_timeout(600)
                    clicked = True
                    break
            except Exception:
                pass
        # Continue / Skip / 下一步
        for sel in ['button:has-text("Continue")', 'button:has-text("Skip")',
                    'text=Skip', 'text=Continue', 'text=Next']:
            try:
                loc = page.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    log.info("引导页点击: %s", sel)
                    await page.wait_for_timeout(1800)
                    clicked = True
                    break
            except Exception:
                pass
        if not clicked:
            break
    # 选完引导后一般会跳首页
    for _ in range(10):
        if "login" not in page.url:
            return
        await page.wait_for_timeout(1000)


# ------------------------------------------------------------------ 登录态（读浏览器 cookie）
# 小红书登录后的关键 cookie（rednote.com / .rednote.com 域）
LOGIN_COOKIE_NAMES = ("web_session", "customerClientId", "id_token", "ets", "a1")


async def read_login_cookies(ctx) -> dict:
    """
    读**浏览器当前 cookie jar** 里的登录信息（不是外部导出/文件里的 cookie）。

    Playwright 的 ctx.cookies() 直接取自浏览器的 cookie 存储，
    包含该指纹环境已登录留下的 web_session 等，能真实反映"这个窗口有没有登录"。
    """
    out = {}
    try:
        cookies = await ctx.cookies()
    except Exception as e:
        log.warning("读取浏览器 cookie 失败: %s", e)
        return out
    for c in cookies:
        name = c.get("name")
        if name in LOGIN_COOKIE_NAMES and c.get("value"):
            dom = c.get("domain") or ""
            if "rednote.com" not in dom and "xiaohongshu.com" not in dom:
                continue
            out[name] = {
                "value": c["value"],
                "domain": dom,
                "expires": c.get("expires"),
                "length": len(c["value"]),
            }
    return out


async def has_login_cookie(ctx) -> tuple[bool, dict]:
    """浏览器里是否存在有效登录 cookie（web_session 是硬判据）"""
    info = await read_login_cookies(ctx)
    return bool(info.get("web_session")), info


def describe_cookies(info: dict) -> str:
    if not info:
        return "无登录 cookie"
    return ", ".join(f"{k}({v['length']}B)" for k, v in info.items())


async def logged_in(page: Page, timeout: float = 20.0, ctx=None) -> bool:
    """
    判断是否已登录：优先看浏览器 cookie，其次看页面状态。

    ctx 传入时会先查浏览器 cookie jar 里的 web_session —— 有就直接算登录，
    没有则再看 URL/引导页（新注册账号偶尔 cookie 写入略滞后）。
    """
    if ctx is not None:
        ok, info = await has_login_cookie(ctx)
        if ok:
            log.info("浏览器 cookie 显示已登录: %s", describe_cookies(info))
            return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if "login" not in page.url:
                return True
            if await on_onboarding(page):
                return True
            if ctx is not None:      # cookie 可能刚写入，轮询时再看一眼
                ok, info = await has_login_cookie(ctx)
                if ok:
                    log.info("浏览器 cookie 显示已登录: %s", describe_cookies(info))
                    return True
        except Exception:
            pass
        await page.wait_for_timeout(1200)
    return False


# ------------------------------------------------------------------ 成功记录
SUCCESS_LOG = Path("success_log.jsonl")


def window_seq(browser_id: str, name: str = "") -> str:
    """窗口序号：优先取名字里的数字（小红书测试21 -> 21）"""
    n = "".join(ch for ch in (name or "") if ch.isdigit())
    if n:
        return n
    return browser_id[:8]


def record_success(browser_id: str, name: str, phone: str = "", code: str = "") -> None:
    """每成功接码/登录一条，追加写入 success_log.jsonl"""
    try:
        row = {"time": time.strftime("%Y-%m-%d %H:%M:%S"),
               "browser_id": browser_id,
               "seq": window_seq(browser_id, name),
               "name": name,
               "phone": phone,
               "code": code}
        with SUCCESS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning("写成功记录失败: %s", e)


def last_success() -> dict | None:
    """读 success_log.jsonl 最后一条成功记录"""
    try:
        if not SUCCESS_LOG.exists():
            return None
        lines = [ln for ln in SUCCESS_LOG.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return json.loads(lines[-1]) if lines else None
    except Exception:
        return None


class NoNumbersError(RuntimeError):
    """连续多次取号都无库存"""

    def __init__(self, msg: str, last: dict | None = None):
        super().__init__(msg)
        self.last_success = last


# ------------------------------------------------------------------ 国家排序
def country_stock(cli, service: str, country: dict) -> int:
    """
    查某国家该 service 的可用号码数；查询失败返回 -1。
    若该国家设了 max_price，只统计价格 ≤ 上限的 provider 库存，
    否则“号多”会把买不起的高价号也算进去。
    """
    cid = country["id"]
    maxp = country.get("max_price")
    try:
        d = cli.prices(service, cid)
        if not (isinstance(d, dict) and cid in d and service in d[cid]):
            return -1
        info = d[cid][service]
        provs = info.get("providers") or {}
        if not maxp or not provs:
            return int(info.get("count") or 0)
        limit = float(maxp)
        total = 0
        for p in provs.values():
            prices = p.get("price") or []
            if prices and min(float(x) for x in prices) <= limit:
                total += int(p.get("count") or 0)
        return total
    except Exception as e:
        log.debug("库存查询失败 %s: %s", country.get("cn"), e)
    return -1


def order_countries(cli, service: str) -> list[dict]:
    """
    国家的尝试顺序 = COUNTRY_LIST 的配置顺序（泰国在前、美国在后），固定不变。

    库存只打日志做参考，不参与排序：泰国优先，泰国取不到号/收不到码才换美国。
    """
    ordered = list(C.COUNTRY_LIST)
    for c in ordered:
        n = country_stock(cli, service, c)
        cap = c.get("max_price")
        log.info("库存参考：%s(%s)%s -> %s",
                 c.get("cn"), c.get("id"), f" 限价 ${cap}" if cap else "",
                 "查询失败" if n < 0 else f"{n} 个")
    log.info("国家尝试顺序（固定）：%s",
             " -> ".join(f"{c.get('cn')}({c['id']})" for c in ordered))
    return ordered


async def acquire_number(cli, service: str, country_id: str,
                         attempts: int = 4, interval: float = 5.0,
                         max_price=None, country_cn: str = ""):
    """
    取号：没有库存时等 5 秒重试，连续 4 次都拿不到就抛 NoNumbersError
    max_price: 单价上限（美国限制 0.05），超价的号平台不会下发
    """
    last_err = ""
    for i in range(1, attempts + 1):
        try:
            act = cli.get_number(service, country_id, max_price=max_price)
            if i > 1:
                log.info("第 %d 次取号成功: %s", i, act.phone)
            return act
        except TigerError as e:
            last_err = str(e)
            if "NO_NUMBERS" not in last_err:
                raise                              # 余额不足/Key 错误等直接抛
            log.warning("%s第 %d/%d 次取号：暂无库存%s，%g 秒后重试",
                        f"{country_cn}" if country_cn else "", i, attempts,
                        f"（限价 ${max_price}）" if max_price else "", interval)
            if i < attempts:
                await asyncio.sleep(interval)

    last = last_success()
    if last:
        tip = (f"当前时段无号码可用（连续 {attempts} 次取号失败）；"
               f"上一个成功接码的窗口：第 {last.get('seq')} 号（{last.get('name')}，{last.get('time')}）")
    else:
        tip = f"当前时段无号码可用（连续 {attempts} 次取号失败）；暂无成功接码记录"
    log.error(tip)
    raise NoNumbersError(tip, last)


# ------------------------------------------------------------------ 单号尝试
async def demo_send_and_solve(page: Page, phone: str) -> dict:
    """自测模式：不取号、不登录，只验证「填号 + 发码 + 过滑块」是否正常"""
    await page.fill(C.SEL["phone"], "")
    await page.fill(C.SEL["phone"], phone)
    await page.wait_for_timeout(random.randint(400, 900))
    before = await code_button_text(page)
    await send_code(page)
    if not await captcha.wait_captcha(page, timeout=15):
        btn = await code_button_text(page)
        sent = btn != before and ("Resend" in btn or bool(re.search(r"\d+\s*s\b", btn or "")))
        return {"ok": sent, "reason": "no_captcha_already_sent" if sent else "no_captcha", "button": btn}
    ok = await captcha.solve_captcha(page, C.ASSETS_DIR, retries=C.CAPTCHA_RETRIES)
    await page.wait_for_timeout(3500)
    btn = await code_button_text(page)
    sent = btn != before and ("Resend" in btn or bool(re.search(r"\d+\s*s\b", btn or "")))
    return {"ok": ok and sent, "reason": "captcha_passed" if ok else "captcha_failed", "button": btn}


async def try_one_number(page: Page, cli: TigerSMS, service: str,
                         country: dict,
                         wait_seconds: int, dry_run: bool = False) -> dict:
    """
    一次完整尝试：取号 -> 填号 -> 发码 -> 过滑块 -> 等码
    country: {"label": "Thailand", "id": "52", "code": "66"}
    """
    trace = SEND_TRACE
    trace.clear()
    cid, ccode = country["id"], country["code"]
    label = country.get("label", "Thailand")

    # 取号：无库存时 5 秒重试，连续 4 次失败 -> NoNumbersError（带上一台成功窗口序号）
    act = await acquire_number(cli, service, cid,
                              attempts=C.NUMBER_ATTEMPTS, interval=C.NUMBER_RETRY_INTERVAL,
                              max_price=country.get("max_price"),
                              country_cn=country.get("cn", ""))
    t_acq = time.time()          # 记下取号时刻，退号时要算平台的最小存活时间
    local = strip_country_prefix(act.phone, ccode)
    log.info("取号 %s/%s  原始=%s  填写=%s  id=%s",
             country.get("cn", label), service, act.phone, local, act.activation_id)

    try:
        cli.mark_ready(act.activation_id)
    except Exception as e:
        log.debug("mark_ready 失败: %s", e)

    await page.fill(C.SEL["phone"], "")
    await page.fill(C.SEL["phone"], local)
    await page.wait_for_timeout(random.randint(400, 900))

    btn = await code_button_text(page)
    if "Resend" in btn or re.search(r"\d+\s*s", btn or ""):
        # 还在倒计时，刷新页面重新开始
        log.info("发码按钮处于倒计时(%s)，重新加载页面", btn)
        await open_login(page)
        await select_country(page, label, ccode)
        await page.fill(C.SEL["phone"], local)
        await page.wait_for_timeout(600)

    await send_code(page)
    t_send = time.time()      # 等码 90s 预算从「点击发送验证码」这一刻开始算

    # 快速判定：-13002「发送验证码失败」立刻换号，不再等滑块/等码
    for _ in range(6):
        await page.wait_for_timeout(600)
        if _send_rejected(trace):
            break
    rej = _send_rejected(trace)
    if rej:
        log.warning("发送验证码失败(code=%s)，立即换号", rej)
        ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
        if not ok_c:
            log.warning("退号失败(可能仍在计费): %s", resp_c)
        return {"ok": False, "reason": f"send_code_rejected:{rej}",
                "activation": act.id_dict(), "trace": trace}

    log.info("发码响应: %s", trace[-3:] or "无请求")

    # 出现滑块则处理；通过后前端自动重发 send_code
    if await captcha.wait_captcha(page, timeout=20):
        log.info("检测到滑块，开始处理")
        ok = await captcha.solve_captcha(page, C.ASSETS_DIR, retries=C.CAPTCHA_RETRIES)
        if not ok:
            log.warning("滑块未通过，取消该号换下一个")
            ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
            if not ok_c:
                log.warning("退号失败(可能仍在计费): %s", resp_c)
            return {"ok": False, "reason": "captcha_failed", "activation": act.id_dict()}
        await page.wait_for_timeout(1500)

    # 确认发码真的成功：按钮进入倒计时，或 send_code 业务成功
    sent = False
    for _ in range(10):
        await page.wait_for_timeout(1000)
        btn = await code_button_text(page)
        if "Resend" in btn or re.search(r"\d+\s*s\b", btn or ""):
            sent = True
            break
        if _send_succeeded(trace):
            sent = True
            break
        if _send_rejected(trace):
            break        # 业务已拒绝（如 -13002），不用再等

    # 滑块过了但码没发出去：再点一次（此时不用再过验证）
    if not sent and not _send_rejected(trace):
        btn = await code_button_text(page)
        if btn and "Resend" not in btn:
            log.info("滑块已通过但未发码（按钮=%s），重新点击获取验证码", btn)
            try:
                await page.click(C.SEL["code_btn"], timeout=8000)
            except Exception as e:
                log.warning("重发点击失败: %s", e)
            for _ in range(10):
                await page.wait_for_timeout(1000)
                btn = await code_button_text(page)
                if "Resend" in btn or re.search(r"\d+\s*s\b", btn or ""):
                    sent = True
                    break
                if _send_succeeded(trace):
                    sent = True
                    break
                if _send_rejected(trace):
                    break

    if not sent:
        rej = _send_rejected(trace)
        log.warning("发码未成功(按钮=%s, 业务码=%s, 响应=%s)，换号重试",
                    await code_button_text(page), rej or "-", trace[-2:])
        ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
        if not ok_c:
            log.warning("退号失败(可能仍在计费): %s", resp_c)
        return {"ok": False,
                "reason": f"send_code_rejected:{rej}" if rej else "send_code_failed",
                "activation": act.id_dict(), "trace": trace}
    log.info("短信已发出，开始等码 (响应序列 %s)", [t["status"] for t in trace])

    # 等码预算：从「点击发送验证码」起算 90 秒（滑块耗时也算在内）。
    # 剩余不足 15 秒就直接换号，免得刚点完码就超时白等。
    elapsed = time.time() - t_send
    remain = wait_seconds - elapsed
    log.info("等码预算: 总 %ss - 发码/滑块已用 %.0fs = 剩余 %.0fs", wait_seconds, elapsed, remain)
    if remain < 15:
        log.warning("发码+滑块已用 %.0fs，剩余不足 15s，直接退号换下一个号码", elapsed)
        ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
        if not ok_c:
            log.warning("退号失败(可能仍在计费): %s", resp_c)
        return {"ok": False, "reason": "no_sms_budget", "activation": act.id_dict()}

    code = cli.wait_code(act.activation_id, timeout=remain, interval=C.POLL_INTERVAL)
    if not code:
        log.warning("从发码起 %ss 内未收到验证码，退号 %s 并换下一个号码",
                    wait_seconds, act.activation_id)
        ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
        if not ok_c:
            log.warning("退号失败(可能仍在计费): %s", resp_c)
        return {"ok": False, "reason": "no_sms", "activation": act.id_dict()}

    log.info("收到验证码: %s", code)
    if dry_run:
        log.info("dry-run：跳过填码登录")
        return {"ok": True, "activation": act.id_dict(), "phone": act.phone, "code": code, "dry_run": True}

    await page.fill(C.SEL["code_input"], code)
    await page.wait_for_timeout(random.randint(500, 1100))
    await submit_login(page)
    await page.wait_for_timeout(3000)

    if await logged_in(page, timeout=25, ctx=ctx):
        try:
            cli.complete(act.activation_id)
        except Exception:
            pass
        await handle_onboarding(page)
        return {"ok": True, "activation": act.id_dict(), "phone": act.phone, "code": code}

    # 失败留现场，便于排查（协议弹窗 / 验证码错误 / 二次风控）
    try:
        stamp = time.strftime("%H%M%S")
        await page.screenshot(path=str(Path(f"login_fail_{stamp}.png")))
        body = (await page.inner_text("body"))[:300].replace("\n", " / ")
        log.warning("登录失败页面: %s", body)
    except Exception:
        pass
    return {"ok": False, "reason": "login_failed", "activation": act.id_dict(), "phone": act.phone, "code": code}


# ------------------------------------------------------------------ 主流程
async def run(args) -> int:
    browser_id = args.browser_id or (C.BROWSER_ID or bit_first_browser(args.name_prefix))
    log.info("使用窗口: %s", browser_id)

    if args.mock_phone:
        opened = bit_open(browser_id)
        ws = opened["ws"]
        win_name = opened.get("name") or ""
        try:
            async with async_playwright() as p:
                browser = await p.chromium.connect_over_cdp(ws)
                ctx = browser.contexts[0]
                page = await ctx.new_page()
                cur = await select_thailand(page)
                log.info("区号已切换: %s", cur)
                r = await demo_send_and_solve(page, args.mock_phone)
                log.info("自测结果: %s", r)
                await page.screenshot(path=str(Path("probe") / "selftest.png"))
                await page.close()
                await browser.close()
                return 0 if r.get("ok") else 1
        finally:
            bit_close(browser_id)

    if not C.TIGER_API_KEY:
        log.error("缺少 TIGER_SMS_KEY，先设置环境变量或在 .env 里填写")
        return 2

    cli = TigerSMS(C.TIGER_API_KEY, base=C.TIGER_BASE, proxy=C.TIGER_PROXY)
    try:
        log.info("接码平台余额: %s", cli.balance())
    except Exception as e:
        log.warning("查询余额失败: %s", e)

    service = args.service or C.SERVICE
    if args.probe_service:
        ok = cli.probe_services(C.SERVICE_CANDIDATES, C.COUNTRY_ID)
        log.info("可用 service: %s", ok)
        if not ok:
            log.error("候选 service 在该国家都无货，检查 COUNTRY_ID / SERVICE")
            return 3
        service = ok[0]

    opened = bit_open(browser_id)
    ws = opened["ws"]
    win_name = opened.get("name") or ""
    result = {"ok": False}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0]
            page = await ctx.new_page()
            page.on("response", lambda r: asyncio.ensure_future(_trace_response(r)))
            await open_login(page)

            # 浏览器 cookie（实时读 cookie jar，不是导出文件）
            cookie_ok, cookie_info = await has_login_cookie(ctx)
            page_ok = "login" not in page.url
            log.info("登录态 | 浏览器 cookie: %s | 页面: %s",
                     describe_cookies(cookie_info), "已进站" if page_ok else "停在登录页")

            # 判定以「页面能进站」为准：cookie 可能残留但已被服务端踢掉（实测会 401）
            if page_ok:
                await handle_onboarding(page)
                cookies = await ctx.cookies()
                result = {"ok": True, "url": page.url,
                          "note": "窗口已登录",
                          "login_cookie": describe_cookies(cookie_info),
                          "cookies": {c["name"]: c["value"] for c in cookies
                                      if c["domain"].endswith("rednote.com")}}
                log.info("窗口已登录: %s", page.url)
                await page.close()
                await browser.close()
                bit_close(browser_id)
                Path(args.out).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({k: v for k, v in result.items() if k != "cookies"},
                                 ensure_ascii=False, indent=2))
                return 0

            if cookie_ok:
                log.warning("浏览器里有 web_session，但页面被弹回登录页 -> cookie 已失效，需重新登录")

            # 只扫描登录态：没登录就直接返回，不取号不花钱
            if args.scan_only:
                res = {"ok": False, "reason": "not_logged_in", "url": page.url,
                       "cookie_present": cookie_ok,
                       "cookie_stale": cookie_ok,
                       "login_cookie": describe_cookies(cookie_info),
                       "window_seq": window_seq(browser_id, win_name)}
                out = Path(args.out)
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(json.dumps(res, ensure_ascii=False, indent=2), encoding="utf-8")
                log.info("扫描：未登录 %s（cookie: %s）", page.url, describe_cookies(cookie_info))
                return 1

            # 多国家回退：按当前可用号码数动态排序（号多的先用），接不到再换下一个
            success = False
            countries = order_countries(cli, service)
            if getattr(args, "country", ""):
                _picked = [c for c in countries
                           if (args.country == "th" and c["code"] == C.COUNTRY_CODE)
                           or (args.country == "us" and c["code"] == C.COUNTRY_CODE_US)]
                if _picked:
                    countries = _picked
            for country in countries:
                if success:
                    break
                cur = await select_country(page, country["label"], country["code"])
                if country["code"] not in cur:
                    log.error("%s 区号切换失败，当前: %s", country.get("cn"), cur)
                    continue
                log.info("== 使用国家 %s（区号 %s / 平台ID %s）==",
                         country.get("cn"), cur, country["id"])

                # 每国固定试 N 个号（默认 3）：等码 90s 超时 → 换下一个号
                # 该国 N 个号全失败 → 换下一个国家；两国都失败 → 当前时段无手机号可用
                for attempt in range(1, args.max_numbers + 1):
                    log.info("=== %s 第 %d/%d 个号码 ===", country.get("cn"), attempt, args.max_numbers)

                    # 上一轮可能已经成功（引导页/首页），先确认一下，别浪费号码
                    if attempt > 1 and ("login" not in page.url or await on_onboarding(page)):
                        await handle_onboarding(page)
                        cookies = await ctx.cookies()
                        result = {"ok": True, "url": page.url,
                                  "phone": (result or {}).get("phone"),
                                  "note": "上一轮已进入登录态",
                                  "cookies": {c["name"]: c["value"] for c in cookies
                                              if c["domain"].endswith("rednote.com")}}
                        log.info("检测到已登录，结束: %s", page.url)
                        success = True
                        break

                    try:
                        r = await try_one_number(page, cli, service, country,
                                                 args.wait_seconds, dry_run=args.dry_run)
                    except NoNumbersError as e:
                        last = e.last_success or {}
                        result = {"ok": False, "reason": "no_numbers", "message": str(e),
                                  "last_success": last or None,
                                  "window_seq": window_seq(browser_id, win_name)}
                        log.warning("%s 无库存，换下一个国家: %s", country.get("cn"), e)
                        break
                    except TigerError as e:
                        log.error("接码平台错误: %s", e)
                        if "NO_BALANCE" in str(e) or "BAD_KEY" in str(e):
                            result = {"ok": False, "reason": "platform_error", "message": str(e)}
                            break
                        time.sleep(3)
                        continue
                    except Exception as e:
                        log.exception("本次尝试异常: %s", e)
                        r = {"ok": False, "reason": f"exception:{e}"}

                    if r.get("ok"):
                        phone = r.get("phone") or ""
                        cookies = await ctx.cookies()
                        result = {
                            "ok": True,
                            "phone": phone,
                            "local": strip_country_prefix(phone, country["code"]),
                            "country": country.get("cn"),
                            "activation_id": (r.get("activation") or {}).get("id"),
                            "code": r.get("code"),
                            "dry_run": bool(r.get("dry_run")),
                            "url": page.url,
                            "window_seq": window_seq(browser_id, win_name),
                            "cookies": {c["name"]: c["value"] for c in cookies
                                        if c["domain"].endswith("rednote.com")},
                        }
                        record_success(browser_id, win_name, phone, r.get("code") or "")
                        log.info("%s 登录成功: %s", country.get("cn"), page.url)
                        success = True
                        break

                    log.warning("%s 第 %d/%d 个号失败: %s",
                                country.get("cn"), attempt, args.max_numbers, r.get("reason"))

                    # 二次判定：可能只是判定时机问题
                    if await logged_in(page, timeout=5, ctx=ctx):
                        phone = r.get("phone") or ""
                        cookies = await ctx.cookies()
                        result = {
                            "ok": True,
                            "phone": phone,
                            "local": strip_country_prefix(phone, country["code"]),
                            "country": country.get("cn"),
                            "activation_id": (r.get("activation") or {}).get("id"),
                            "code": r.get("code"),
                            "url": page.url,
                            "window_seq": window_seq(browser_id, win_name),
                            "cookies": {c["name"]: c["value"] for c in cookies
                                        if c["domain"].endswith("rednote.com")},
                            "note": "二次判定为登录成功",
                        }
                        record_success(browser_id, win_name, phone, r.get("code") or "")
                        log.info("二次判定登录成功: %s", page.url)
                        success = True
                        break

                    # 失败后回到干净状态（保持当前国家）
                    try:
                        await open_login(page)
                        await select_country(page, country["label"], country["code"])
                    except Exception as e:
                        log.warning("重置页面失败: %s", e)

            if not success:
                if result.get("reason") != "platform_error":
                    result = {"ok": False, "reason": "no_numbers",
                              "message": "当前时段无号码可用（泰国与美国均取不到号或收不到验证码）",
                              "window_seq": window_seq(browser_id, win_name)}
                log.error(result.get("message") or "当前时段无号码可用")

            await page.close()
            await browser.close()
    finally:
        bit_close(browser_id)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cookies"}, ensure_ascii=False, indent=2))
    log.info("结果已写入 %s", out.resolve())

    if result.get("ok"):
        return 0
    # 号码不足 / 平台异常 用独立退出码，便于批量脚本区分是否继续
    return {"no_numbers": 30, "platform_error": 31}.get(result.get("reason"), 1)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="小红书/RedNote 泰国号自动登录")
    ap.add_argument("--browser-id", default="", help="比特浏览器窗口 ID")
    ap.add_argument("--name-prefix", default="", help="按窗口名筛选，如 小红书测试")
    ap.add_argument("--service", default="", help="接码平台 service 代码，默认取配置")
    ap.add_argument("--probe-service", action="store_true", help="先用价格接口探测可用 service")
    ap.add_argument("--wait-seconds", type=int, default=C.CODE_WAIT_SECONDS, help="拿到号码后等码上限秒数（默认 90）")
    ap.add_argument("--max-numbers", type=int, default=C.MAX_NUMBERS, help="每个国家最多试几个号（默认 3）")
    ap.add_argument("--dry-run", action="store_true", help="只做发码+滑块，不登录")
    ap.add_argument("--mock-phone", default="", help="自测用：固定手机号，不调接码平台")
    ap.add_argument("--scan-only", action="store_true",
                    help="只检查窗口是否已登录：已登录则导出 cookie，未登录直接返回，不取号")
    ap.add_argument("--out", default="login_result.json", help="结果文件路径（批量时每个窗口一份）")
    ap.add_argument("--country", choices=["", "th", "us"], default="",
                    help="只跑指定国家：th=泰国66 / us=美国1，不填则泰国失败自动换美国")
    ap.add_argument("-v", "--verbose", action="store_true")
    return ap


def main() -> int:
    args = build_parser().parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)-8s %(message)s",
        datefmt="%H:%M:%S",
    )
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        log.warning("用户中断")
        return 130


if __name__ == "__main__":
    sys.exit(main())
