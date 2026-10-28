# -*- coding: utf-8 -*-
"""
RedNote 发帖模块（图片 / 视频）

实测结构（creator.rednote.com/publish/publish）:
    tab          .creator-tab             「上传视频」/「上传图文」
    上传         input[type=file]         视频 accept=mp4,mov... ；图文 accept=jpg,jpeg,png,webp
                 注意：Playwright set_input_files 走 CDP 时单文件限 50MB，
                 这里用原生 DOM.setFileInputFiles 传本地路径，大视频也能传
    标题         input[placeholder*="标题"]  最多 20 字
    正文         div.tiptap.ProseMirror     最多 1000 字，话题写成 #标签 回车确认
    话题按钮     button.contentBtn.topic-btn
    发布         div.btn-wrapper 文本「发布笔记」
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger("publish")

PUBLISH_URL = "https://creator.rednote.com/publish/publish"

VIDEO_EXT = {".mp4", ".mov", ".flv", ".f4v", ".mkv", ".rm", ".rmvb", ".m4v", ".mpg", ".mpeg", ".ts"}
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"}

SEL_TAB_VIDEO = '.creator-tab:has-text("上传视频")'
SEL_TAB_IMAGE = '.creator-tab:has-text("上传图文")'
SEL_TITLE = 'input[placeholder*="标题"]'
SEL_EDITOR = '[contenteditable="true"]'
SEL_PUBLISH_BTN = 'div.btn-wrapper:has-text("发布笔记"), button:has-text("发布笔记")'

TITLE_MAX = 20
CONTENT_MAX = 1000


def kind_of(path: str) -> str:
    """按后缀判断发什么：video / image / unknown"""
    ext = Path(path).suffix.lower()
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    return "unknown"


# ------------------------------------------------------------------ 上传
async def _set_files_cdp(page, selector: str, paths: list[str]) -> None:
    """
    用 CDP 原生 DOM.setFileInputFiles 投递文件。
    Playwright 的 set_input_files 经 CDP 传输有 50MB 上限，视频会报
    'Cannot transfer files larger than 50Mb to a browser not co-located with the server'。
    """
    cdp = await page.context.new_cdp_session(page)
    doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
    node = await cdp.send("DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector})
    nid = node.get("nodeId")
    if not nid:
        raise RuntimeError(f"找不到文件输入框: {selector}")
    await cdp.send("DOM.setFileInputFiles", {"files": paths, "nodeId": nid})
    await cdp.detach()


async def _wait_tabs(page, timeout=45) -> bool:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        if await page.evaluate("() => document.querySelectorAll('.creator-tab').length"):
            return True
        await page.wait_for_timeout(800)
    return False


async def _click_tab(page, text: str) -> None:
    """tab 上有 span.title 拦截 pointer events，用 JS 点击"""
    ok = await page.evaluate("""(txt) => {
        const t = [...document.querySelectorAll('.creator-tab')].find(e => (e.innerText||'').includes(txt));
        if (t) { t.click(); return true; }
        return false;
    }""", text)
    if not ok:
        raise RuntimeError(f"找不到 tab: {text}")


async def _wait_editor(page, timeout=60) -> bool:
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        n = await page.evaluate(
            "() => (document.querySelector('input[placeholder*=\"标题\"]') ? 1 : 0)"
            " + (document.querySelector('[contenteditable=\"true\"]') ? 1 : 0)")
        if n >= 2:
            return True
        await page.wait_for_timeout(800)
    return False


async def _wait_upload_done(page, kind: str, timeout: int) -> bool:
    """
    等上传完成。

    踩过的坑：图文模式下页面文本里没有“上传中 xx%”，如果只靠文本判断会立刻返回 True，
    结果点发布时被拒（toast：图片上传中，请稍后）。
    所以图文要额外确认：缩略图已出现 + 页面文本连续静默若干秒。
    """
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    last = ""
    quiet = 0.0
    while loop.time() < end:
        st = await page.evaluate(r"""() => {
            const t = document.body.innerText || '';
            const m = t.match(/(\d{1,3})\s*%/);
            // 图文：图片缩略图
            const thumbs = document.querySelectorAll(
                '[class*=img-item], [class*=image-item], [class*=pic-item], [class*=upload-item] img, .img-container img').length;
            return {pct: m ? parseInt(m[1]) : -1,
                    uploading: /上传中|正在上传|处理中|请稍后/.test(t),
                    thumbs,
                    txt: t.slice(0, 120).replace(/\n/g, ' / ')};
        }""")
        if st["txt"] != last:
            if st["uploading"] or st["pct"] >= 0:
                log.info("上传中: %s%% (缩略图 %d)", st["pct"] if st["pct"] >= 0 else "-", st["thumbs"])
            last = st["txt"]
            quiet = 0.0
        else:
            quiet += 1.5

        if not st["uploading"] and st["pct"] < 0:
            if kind == "video":
                await page.wait_for_timeout(1500)
                return True
            # 图文：缩略图在 + 文本静默 4.5 秒才算稳
            if quiet >= 4.5:
                return True
        await page.wait_for_timeout(1500)
    log.warning("上传等待超时(%ss)，仍尝试继续", timeout)
    return False


# ------------------------------------------------------------------ 内容
async def _fill_title(page, title: str) -> None:
    t = (title or "").strip()
    if not t:
        return
    if len(t) > TITLE_MAX:
        log.warning("标题超过 %d 字，截断", TITLE_MAX)
        t = t[:TITLE_MAX]
    loc = page.locator(SEL_TITLE).first
    await loc.click()
    await page.wait_for_timeout(200)
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Backspace")
    await loc.type(t, delay=40)
    await page.wait_for_timeout(300)


async def _fill_content(page, content: str, tags: Iterable[str]) -> None:
    body = (content or "").strip()
    if len(body) > CONTENT_MAX - 50:
        body = body[: CONTENT_MAX - 50]
    ed = page.locator(SEL_EDITOR).first
    await ed.click()
    await page.wait_for_timeout(300)

    if body:
        # 逐字输入，模拟真人（tiptap 对 paste 有时不触发变更）
        await page.keyboard.type(body, delay=25)
        await page.wait_for_timeout(300)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(200)

    for tag in tags:
        t = str(tag).strip().lstrip("#")
        if not t:
            continue
        await page.keyboard.type(f"#{t}", delay=80)
        # 等联想出现再回车确认成 #标签[话题]#
        await page.wait_for_timeout(900)
        await page.keyboard.press("Enter")
        await page.wait_for_timeout(400)
    await page.wait_for_timeout(300)


# ------------------------------------------------------------------ 结果
ERROR_HINTS = ("账号异常", "禁止发布", "被封禁", "存在风险", "发布失败", "审核不通过",
               "操作过于频繁", "请稍后再试", "内容违规", "无法发布")
SUCCESS_HINTS = ("发布成功", "已发布", "笔记发布成功")


async def _read_notice(page) -> str:
    """读页面上的提示语（toast 选择器不固定，改为全文关键词抓取）"""
    try:
        return await page.evaluate(r"""() => {
            const hits = [];
            const kw = /上传中|请稍后|发布成功|失败|异常|违规|审核|禁止|风险|频繁/;
            document.querySelectorAll('div,span,p').forEach(e => {
                if (e.childElementCount > 0) return;
                const t = (e.innerText || '').trim();
                if (!t || t.length > 40 || !kw.test(t)) return;
                const r = e.getBoundingClientRect();
                if (r.width > 40 && r.height > 12) hits.push(t);
            });
            return [...new Set(hits)].join(' | ');
        }""")
    except Exception:
        return ""


async def _click_publish_button(page) -> str:
    """
    点击底部「发布」按钮，三级策略：

    1) 宿主属性就绪判断：<xhs-publish-btn submit-disabled="false" submit-loading="false">
       —— 比“等页面文本静默”准得多，这是组件自己暴露的状态
    2) 通过组件挂在宿主上的 _sr（ShadowRoot 引用）精确取到「发布」按钮并 click()
       —— 该元素 shadowRoot 属性为 null（closed），但组件把引用存成了 _sr，
          实测 ownKeys = ['_sr','_app','_props','_onPublish','_onSave']
    3) 兜底：按宿主相对坐标点击（0.62, 0.60）

    注意：侧边栏那个「发布笔记」（div.btn-wrapper）是新建发布入口，不能当提交按钮。
    """
    # ---- 1. 等按钮就绪
    ready = False
    for i in range(120):
        st = await page.evaluate(r"""() => {
            const c = document.querySelector('xhs-publish-btn');
            if (!c) return null;
            return {disabled: c.getAttribute('submit-disabled'),
                    loading: c.getAttribute('submit-loading'),
                    text: c.getAttribute('submit-text')};
        }""")
        if st and st.get("disabled") == "false" and st.get("loading") != "true":
            ready = True
            if i:
                log.info("发布按钮就绪（等了 %ds）", i)
            break
        if i and i % 15 == 0:
            log.info("等待发布按钮就绪: disabled=%s loading=%s (%ds)",
                     (st or {}).get("disabled"), (st or {}).get("loading"), i)
        await page.wait_for_timeout(1000)
    if not ready:
        log.warning("发布按钮 120 秒内未就绪，仍尝试点击")

    # ---- 2. 走 _sr 精确点击
    r = await page.evaluate(r"""() => {
        const c = document.querySelector('xhs-publish-btn');
        if (!c) return {ok: false, reason: '宿主不存在'};
        const sr = c._sr;
        if (!sr) return {ok: false, reason: '无 _sr'};
        const btns = [...sr.querySelectorAll('button')];
        const btn = btns.find(b => (b.textContent || '').includes('发布'));
        if (!btn) return {ok: false, reason: '未找到发布按钮',
                          all: btns.map(b => (b.textContent || '').trim())};
        if (btn.disabled) return {ok: false, reason: '按钮 disabled'};
        btn.focus && btn.focus();
        btn.click();
        return {ok: true, txt: (btn.textContent || '').trim()};
    }""")
    if r.get("ok"):
        log.info("通过 _sr 点击发布按钮(%s)", r.get("txt"))
        return "sr-click"
    log.warning("_sr 点击失败(%s)，改用坐标点击", r.get("reason"))

    # ---- 3. 兜底坐标点击
    host = page.locator("xhs-publish-btn").first
    if await host.count():
        try:
            await host.scroll_into_view_if_needed(timeout=5000)
        except Exception:
            pass
        box = await host.bounding_box()
        if box and box["width"] > 100:
            x = box["x"] + box["width"] * 0.62
            y = box["y"] + box["height"] * 0.60
            log.info("坐标点击发布按钮: (%.0f, %.0f)", x, y)
            await page.mouse.move(x, y)
            await page.wait_for_timeout(150)
            await page.mouse.click(x, y)
            return "coord-click"

    # ---- 4. 最后兜底：普通 DOM 文本
    clicked = await page.evaluate("""() => {
        const els = [...document.querySelectorAll('button,div,span,a')];
        const b = els.find(e => {
            const t = (e.innerText || '').trim();
            return (t === '发布' || t === '发布笔记') && !e.closest('.menu-container');
        });
        if (b) { b.click(); return true; }
        return false;
    }""")
    return "dom-click" if clicked else ""


async def _submit(page, timeout: int = 150) -> dict:
    """
    点发布并判定结果。

    实测成功形态：
      1) 出现「发布成功」提示
      2) URL 离开发布页
    实测失败/未就绪：
      toast「图片上传中，请稍后」-> 需要等一会再点一次（最常见）
    """
    before_url = page.url
    loop = asyncio.get_event_loop()
    end = loop.time() + timeout
    clicks = 0
    notice = ""

    while loop.time() < end:
        how = await _click_publish_button(page)
        clicks += 1
        if not how:
            return {"ok": False, "reason": "找不到发布按钮"}
        log.info("第 %d 次点击发布(%s)，等待结果 ...", clicks, how)

        # 单次点击后观察 12 秒
        for _ in range(8):
            await page.wait_for_timeout(1500)
            notice = await _read_notice(page)

            if any(k in notice for k in SUCCESS_HINTS):
                return {"ok": True, "reason": "发布成功", "notice": notice, "click": how}
            if any(k in notice for k in ("账号异常", "禁止发布", "被封禁", "存在风险", "账号受限")):
                return {"ok": False, "reason": f"账号异常: {notice}", "click": how}
            if any(k in notice for k in ("内容违规", "审核不通过")):
                return {"ok": False, "reason": f"内容被拒: {notice}", "click": how}
            if "上传中" in notice or "请稍后" in notice:
                log.info("提示上传未完成(%s)，等 5 秒后重试点击", notice)
                break                       # 跳出内层，外层重新点击
            if page.url != before_url and "publish/publish" not in page.url:
                return {"ok": True, "reason": f"已跳转 {page.url}", "notice": notice, "click": how}
            # 二次确认弹窗
            for sel in ['button:has-text("确认发布")', 'button:has-text("确定")']:
                try:
                    loc = page.locator(sel).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=3000)
                        break
                except Exception:
                    pass

        await page.wait_for_timeout(5000)
        if clicks >= 6:
            break

    try:
        await page.screenshot(path="publish_timeout.png")
    except Exception:
        pass
    return {"ok": False, "reason": f"发布结果未知（点击 {clicks} 次，超时）{notice}".strip(),
            "url": page.url}


# ------------------------------------------------------------------ 主函数
async def publish_post(page, file_path: str, title: str = "", content: str = "",
                       tags: Iterable[str] = (), upload_timeout: int = 900) -> dict:
    """
    在已登录的窗口里发一条笔记。返回 {ok, reason, kind, ...}

    调用方负责：打开窗口、检查登录、失败后重新登录。
    """
    kind = kind_of(file_path)
    if kind == "unknown":
        return {"ok": False, "reason": f"不支持的文件后缀: {Path(file_path).suffix}"}
    p = Path(file_path)
    if not p.exists():
        return {"ok": False, "reason": f"文件不存在: {file_path}"}
    size_mb = p.stat().st_size / 1024 / 1024
    log.info("发帖类型=%s 文件=%s (%.1fMB) 标题=%r 标签=%s", kind, p.name, size_mb, title, list(tags))

    await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=90000)
    if not await _wait_tabs(page):
        return {"ok": False, "reason": f"发布页未加载: {page.url}"}
    if "login" in page.url:
        return {"ok": False, "reason": "未登录", "need_login": True}

    if kind == "image":
        await _click_tab(page, "上传图文")
        await page.wait_for_timeout(1500)
        sel = 'input[type=file][accept*="jpg"], input[type=file][accept*="png"]'
    else:
        sel = 'input[type=file][accept*="mp4"], input[type=file]'

    await _set_files_cdp(page, sel, [str(p.resolve())])
    log.info("文件已投递，等待编辑器 ...")

    if not await _wait_editor(page, timeout=90):
        return {"ok": False, "reason": "编辑器未出现"}

    done = await _wait_upload_done(page, kind, timeout=upload_timeout)
    if not done:
        log.warning("上传可能未完成，继续尝试填写")

    await _fill_title(page, title)
    await _fill_content(page, content, tags)
    await page.wait_for_timeout(1000)

    res = await _submit(page)
    res.update({"kind": kind, "file": str(p), "size_mb": round(size_mb, 1),
                "title": title, "tags": list(tags)})
    return res


# ------------------------------------------------------------------ 登录态
async def is_logged_in(page) -> bool:
    """creator 平台能否直接进发布页（页面渲染较慢，要等 tab 出现）"""
    try:
        await page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        if "login" in page.url:
            return False
        if await _wait_tabs(page, timeout=25):
            return True
        # 再兜底看一眼 URL 和页面文案
        if "login" in page.url:
            return False
        txt = (await page.inner_text("body"))[:200]
        log.warning("发布页无 tab，页面文案: %s", txt.replace("\n", " / "))
        return False
    except Exception as e:
        log.warning("登录态检查异常: %s", e)
        return False


async def logout(ctx) -> None:
    """清掉 rednote 域 cookie，等效退出登录"""
    try:
        await ctx.clear_cookies()
        log.info("已清空 cookie（退出登录）")
    except Exception as e:
        log.warning("清 cookie 失败: %s", e)
