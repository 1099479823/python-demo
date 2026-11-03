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
from sms_factory import current_service, make_sms_client
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
            if (await page.locator(C.SEL["phone"]).count()
                    and await page.locator(C.SEL["country_box"]).count()):
                return                      # 表单（含区号选择器）已就绪
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
    if not await page.locator(C.SEL["country_box"]).count():
        log.warning("区号选择器不在当前页面，重新打开登录页")
        await open_login(page)
    if not await page.locator(C.SEL["country_box"]).count():
        log.error("登录页仍未渲染区号选择器，无法切区号")
        return ""
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
    判断是否**真的**登录成功。

    判据（按优先级）：
      1. 登录表单（手机号/验证码输入框）消失且 URL 离开 login  -> 已登录
      2. 表单消失但 URL 仍带 login（登录后的过渡页/引导页）：
         - 引导页文本命中 -> 已登录
         - 浏览器 cookie 有 web_session -> 已登录（账号真的登上了，页面在哪无所谓）
      3. 表单还在 -> 未登录

    表单消失是硬信号：登录成功后小红书必然跳离表单页。
    cookie 只做辅助（防「URL 带 login 但不渲染表单」的白屏误判）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            has_phone = (await page.locator(C.SEL["phone"]).count()) > 0
            has_code = (await page.locator(C.SEL["code_input"]).count()) > 0
            in_login_url = "login" in page.url

            if not has_phone and not has_code:
                if not in_login_url:
                    if ctx is not None:
                        ok, info = await has_login_cookie(ctx)
                        log.info("页面已离站，浏览器 cookie: %s", describe_cookies(info))
                    return True
                # URL 还挂 login 但表单没了：过渡页 / 引导页
                if await on_onboarding(page):
                    return True
                if ctx is not None:
                    ok, info = await has_login_cookie(ctx)
                    if ok:
                        log.info("表单已消失且有 web_session，判定已登录: %s",
                                 describe_cookies(info))
                        return True
        except Exception:
            pass
        await page.wait_for_timeout(1200)
    return False


async def verify_login(page: Page, ctx=None, timeout: float = 25.0) -> tuple[bool, dict]:
    """
    严格校验登录结果，返回 (是否成功, 详情)。

    三个条件同时满足才算成功：
      1. 页面离开 /login（或进入引导页）
      2. 浏览器 cookie 里有 web_session
      3. 详情里带上 cookie 摘要，便于排查
    """
    page_ok = await logged_in(page, timeout=timeout, ctx=ctx)
    if not page_ok:
        return False, {"page_ok": False, "reason": "页面仍在登录页"}

    cookie_ok, info = False, {}
    for _ in range(8):                      # cookie 写入可能滞后 1~2 秒
        cookie_ok, info = await has_login_cookie(ctx) if ctx is not None else (False, {})
        if cookie_ok or ctx is None:
            break
        await page.wait_for_timeout(1000)

    if ctx is not None and not cookie_ok:
        return False, {"page_ok": True, "cookie_ok": False,
                       "reason": "页面离站但浏览器没有 web_session，登录未真正完成"}

    return True, {"page_ok": True, "cookie_ok": cookie_ok,
                  "cookie": describe_cookies(info), "url": page.url}


def parse_seq_spec(spec: str) -> set[int]:
    """
    解析窗口序号表达式：'1-20,35,40-45' -> {1..20, 35, 40..45}
    登录(--seqs) 和发帖(--only) 共用同一套写法。
    """
    out: set[int] = set()
    for part in (spec or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            try:
                out.update(range(int(a), int(b) + 1))
            except ValueError:
                pass
        else:
            try:
                out.add(int(part))
            except ValueError:
                pass
    return out


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
    if C.COUNTRY_ORDER_INVALID:
        log.warning("COUNTRY_ORDER 里有无效项，已忽略: %s", C.COUNTRY_ORDER_INVALID)
    log.info("国家尝试顺序（来自 COUNTRY_ORDER）：%s",
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


# ------------------------------------------------------------------ 用码登录
CODE_ERR_HINTS = ("验证码错误", "验证码已失效", "验证码不正确", "验证码已过期",
                  "code error", "invalid code", "验证码有误")


async def _code_form_ready(page) -> bool:
    """页面是否还停在可填验证码的状态"""
    try:
        return "login" in page.url and (await page.locator(C.SEL["code_input"]).count()) > 0
    except Exception:
        return False


async def _refund_or_mark(cli, act, t_acq, log=log) -> tuple[str, str]:
    """退号：成功=refunded（钱回来了），失败=wasted（短信已送达退不了）"""
    try:
        refunded, resp = cli.cancel_with_retry(act.activation_id, t_acq)
    except Exception as e:
        refunded, resp = False, f"exception:{e}"
    if refunded:
        log.info("该号未用上，已退号退款: %s", resp)
        return "refunded", str(resp)
    log.warning("该号未用上且退不回来: %s -> 记为浪费", str(resp)[:60])
    return "wasted", str(resp)


async def submit_code_until_success(page, ctx, code: str, country: dict, local: str,
                                    tries: int = None, log=log) -> tuple[bool, dict]:
    """
    用已收到的验证码登录，最多试 CODE_SUBMIT_TRIES 次。

    为什么必须重试：手机号一旦收到短信，接码平台就不允许取消/退款
    （setStatus(8) 会返回 SMS_RECEIVED）。所以校验码没填进去就换号，
    这个号的钱就白花了。常见失败原因是时序（页面还在跳转/被刷新），
    重填一次通常就能成功。
    """
    if tries is None:
        tries = getattr(C, "CODE_SUBMIT_TRIES", 3)
    last = {}
    submitted = 0          # 成功把码填进输入框并点了登录的次数
    for i in range(1, tries + 1):
        try:
            # ① 页面已经跑走了？先看是不是其实登录了
            if "login" not in page.url:
                ok, d = await verify_login(page, ctx=ctx, timeout=5)
                if ok:
                    return True, d
                log.warning("填码前页面已离站且未登录，拉回登录页重试 (%d/%d)", i, tries)
                await open_login(page)
                await select_country(page, country["label"], country["code"])

            # ② 页面被重置过 -> 手机号框空了，补填回去（不重发短信）
            if not await _code_form_ready(page):
                await open_login(page)
                await select_country(page, country["label"], country["code"])
            try:
                phone_now = await page.input_value(C.SEL["phone"])
            except Exception:
                phone_now = ""
            if not phone_now:
                await page.fill(C.SEL["phone"], local)
                await page.wait_for_timeout(random.randint(300, 700))

            # ③ 填码并提交
            await page.fill(C.SEL["code_input"], "")
            await page.fill(C.SEL["code_input"], code)
            await page.wait_for_timeout(random.randint(400, 1000))
            await submit_login(page)
            submitted += 1
            await page.wait_for_timeout(3500)

            ok, d = await verify_login(page, ctx=ctx, timeout=8)
            if ok:
                log.info("第 %d/%d 次提交验证码 %s -> 登录成功（%s）", i, tries, code, d.get("cookie"))
                d["submitted"] = submitted
                return True, d

            last = d
            notice = ""
            try:
                notice = (await page.inner_text("body"))[:400]
            except Exception:
                pass
            if any(k in notice for k in CODE_ERR_HINTS):
                log.warning("第 %d/%d 次提交，页面提示验证码错误，重试无意义", i, tries)
                return False, {"reason": "code_rejected", "notice": notice[:120],
                               "submitted": submitted, **(d or {})}
            log.warning("第 %d/%d 次提交未成功: %s，稍后重试", i, tries, d.get("reason"))
        except Exception as e:
            log.warning("第 %d/%d 次提交验证码异常: %s", i, tries, e)
            last = {"reason": f"exception:{e}"}
        await page.wait_for_timeout(1500)

    out = dict(last or {})
    out.setdefault("reason", "code_submit_failed")
    out["submitted"] = submitted
    return False, out


async def try_one_number(page: Page, cli: TigerSMS, service: str,
                         country: dict,
                         wait_seconds: int, dry_run: bool = False,
                         ctx=None) -> dict:
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
            # 滑块没过，但短信可能已经发出去了 —— 放弃就白花钱，先试着收码
            log.warning("滑块未通过，先确认是否有短信到达（有就直接用掉，不退号）")
            early = cli.wait_code(act.activation_id,
                                  timeout=C.CODE_GRACE_SECONDS, interval=C.POLL_INTERVAL)
            if early:
                log.info("滑块虽未通过但已收到验证码 %s，继续用它登录", early)
                ok_login, detail = await submit_code_until_success(
                    page, ctx, early, country, local, log=log)
                if ok_login:
                    try:
                        cli.complete(act.activation_id)
                    except Exception:
                        pass
                    await handle_onboarding(page)
                    return {"ok": True, "activation": act.id_dict(), "phone": act.phone,
                            "code": early, "cookie": detail.get("cookie"),
                            "usage": "used", "submitted": detail.get("submitted", 0)}
                usage, refund_resp = await _refund_or_mark(cli, act, t_acq, log)
                return {"ok": False, "reason": "captcha_failed_code_unused",
                        "activation": act.id_dict(), "code": early,
                        "usage": usage, "refund": str(refund_resp)[:80]}
            log.warning("滑块未通过且宽限期内无短信，退号换下一个")
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

    # 宽限期：短信常比预期晚到几秒，直接退号就白买了
    if not code:
        grace = getattr(C, "CODE_GRACE_SECONDS", 30)
        log.info("主预算 %.0fs 内没收到码，进入 %ss 宽限期继续等", remain, grace)
        code = cli.wait_code(act.activation_id, timeout=grace, interval=C.POLL_INTERVAL)

    # 退号前最后查一次状态，避免「刚超时 1 秒码就到了」被退掉
    if not code:
        try:
            st = cli.status(act.activation_id)
            code = TigerSMS.parse_code(st)
            if code:
                log.info("退号前最后检查发现验证码 %s，继续使用", code)
        except Exception as e:
            log.debug("退号前查询状态失败: %s", e)

    if not code:
        log.warning("从发码起共 %.0fs 仍未收到验证码，尝试退号 %s",
                    wait_seconds + getattr(C, "CODE_GRACE_SECONDS", 30), act.activation_id)
        ok_c, resp_c = cli.cancel_with_retry(act.activation_id, t_acq)
        if not ok_c and "SMS_RECEIVED" in str(resp_c):
            # 平台说短信已送达 -> 说明码确实发出去了，再等最后一轮
            log.warning("平台提示短信已送达(SMS_RECEIVED)，再等最后一轮 60s 抢救")
            code = cli.wait_code(act.activation_id, timeout=60, interval=C.POLL_INTERVAL)
            if code:
                log.info("抢救成功，拿到验证码 %s", code)
        if not code:
            if ok_c:
                log.info("已退号退款: %s", resp_c)
            else:
                log.warning("退号失败(可能仍在计费): %s", resp_c)
            return {"ok": False, "reason": "no_sms", "activation": act.id_dict(),
                    "usage": "refunded" if ok_c else "wasted"}

    log.info("收到验证码: %s", code)
    if dry_run:
        log.info("dry-run：跳过填码登录")
        return {"ok": True, "activation": act.id_dict(), "phone": act.phone, "code": code, "dry_run": True}

    # 号码已经收到短信 = 平台不允许退号，必须把这个码用掉，否则钱白花
    ok_login, detail = await submit_code_until_success(page, ctx, code, country, local, log=log)
    if ok_login:
        try:
            cli.complete(act.activation_id)
        except Exception:
            pass
        await handle_onboarding(page)
        return {"ok": True, "activation": act.id_dict(), "phone": act.phone,
                "code": code, "cookie": detail.get("cookie"),
                "usage": "used", "submitted": detail.get("submitted", 0)}

    # 多次提交仍失败：留现场便于排查
    try:
        stamp = time.strftime("%H%M%S")
        await page.screenshot(path=str(Path(f"login_fail_{stamp}.png")))
        body = (await page.inner_text("body"))[:300].replace("\n", " / ")
        log.warning("用码登录失败，页面: %s", body)
    except Exception:
        pass
    # 判定这个号到底有没有被用掉：
    #   退号成功(ACCESS_CANCEL)   -> refunded，钱回来了，不算浪费
    #   退号被拒(SMS_RECEIVED 等) -> wasted，短信已送达，钱退不回
    usage, refund_resp = await _refund_or_mark(cli, act, t_acq, log)

    return {"ok": False, "reason": f"code_login_failed:{detail.get('reason')}",
            "detail": detail, "activation": act.id_dict(),
            "phone": act.phone, "code": code,
            "usage": usage, "submitted": detail.get("submitted", 0),
            "refund": str(refund_resp)[:80]}


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

    if C.SMS_PROVIDER == "hero":
        if not C.HERO_API_KEY:
            log.error("SMS_PROVIDER=hero 但缺少 HERO_API_KEY，在 .env 里填写")
            return 2
    elif not C.TIGER_API_KEY:
        log.error("缺少 TIGER_SMS_KEY，先设置环境变量或在 .env 里填写")
        return 2

    cli = make_sms_client()
    try:
        log.info("接码平台余额: %s", cli.balance())
    except Exception as e:
        log.warning("查询余额失败: %s", e)

    service = args.service or current_service()
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
            _only = getattr(args, "country", "") or ""
            if _only:
                _picked = [c for c in countries if c.get("key") == _only]
                if _picked:
                    countries = _picked
                else:
                    log.warning("--country %s 不在当前配置的国家顺序里（%s），按全部国家跑",
                                _only, " -> ".join(c["key"] for c in countries))
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
                                                 args.wait_seconds, dry_run=args.dry_run,
                                                 ctx=ctx)
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
                            "usage": r.get("usage") or "used",
                            "submitted": r.get("submitted"),
                            "cookie": r.get("cookie"),
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

                    # 二次判定：本轮返回失败，但页面可能已经跳走（时序问题）。
                    # 必须走严格校验（页面离站 + 有 web_session），否则会被残留 cookie 骗过。
                    if await logged_in(page, timeout=5, ctx=ctx):
                        ok2, detail2 = await verify_login(page, ctx=ctx, timeout=3)
                        if not ok2:
                            log.warning("二次判定不通过，继续换号: %s", detail2.get("reason"))
                        else:
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
                                "cookie": detail2.get("cookie"),
                            }
                            record_success(browser_id, win_name, phone, r.get("code") or "")
                            log.info("二次判定登录成功: %s | %s", page.url, detail2.get("cookie"))
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
    ap.add_argument("--country", choices=[""] + sorted(C.COUNTRY_CATALOG.keys()), default="",
                    help="只跑指定国家（代号见 COUNTRY_CATALOG，如 th/us）；不填则按 COUNTRY_ORDER")
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
