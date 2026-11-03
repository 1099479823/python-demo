# -*- coding: utf-8 -*-
"""
Hero SMS 接码平台客户端（sms-activate 兼容协议）
文档来源: api-cn.json（当前工作区）
    GET https://hero-sms.com/stubs/handler_api.php?action=...
    REST: https://hero-sms.com/api/v1（Authorization: ApiKey {token}）

协议与 tiger_sms 完全一致（同样兼容 sms-activate）：
    getNumber  -> ACCESS_NUMBER:<id>:<phone>（V2 返回 JSON）
    getStatus  -> STATUS_WAIT_CODE / STATUS_OK:<code>
    setStatus  -> 状态: 1=已就绪 3=重发 6=完成 8=取消
    getBalance -> ACCESS_BALANCE:<余额>

差异点（已适配）：
    - 库存查询：tiger 用 getPricesV3（{cid: {service: ...}}），hero 兼容器用 getPrices；
      这里做了两种格式的解析归一化，country_stock 无需改。
    - 服务代码：hero 有自己的服务清单（getServicesList），探测逻辑已兼容。

用法:
    from hero_sms import HeroSMS
    cli = HeroSMS("你的key")
"""
from __future__ import annotations

import json
import logging

from tiger_sms import TigerSMS

log = logging.getLogger("hero")

HERO_BASE = "https://hero-sms.com/stubs/handler_api.php"


class HeroSMS(TigerSMS):
    """Hero SMS 客户端，协议同 TigerSMS，价格/服务清单接口做了兼容"""

    def __init__(self, api_key: str, base: str = HERO_BASE, timeout: int = 30,
                 proxy: str | None = None):
        super().__init__(api_key, base=base, timeout=timeout, proxy=proxy)

    # ---------- 状态：hero 只认 3/6/8，没有 tiger 的 1（就绪）----------
    def mark_ready(self, activation_id: str) -> str:
        """
        hero 的 setStatus 枚举只有 3（请求重发）/6（完成）/8（取消），
        没有 sms-activate 的 1（已就绪）。这里直接跳过，返回标记，避免刷错误日志。
        取号后 hero 本身已处于待短信状态，不需要额外置位。
        """
        return "ACCESS_READY_SKIPPED"

    # ---------- 价格/库存（hero 用 getPrices，兼容解析成 V3 结构）----------
    def prices(self, service: str, country: str) -> dict:
        """
        查某服务某国家的价格库存，返回统一结构 {cid: {service: {count, price...}}}。
        getPricesV3 不可用时回落到 getPrices（hero 兼容器），再不行返回 {"raw": ...}。
        """
        # 1) 先试 getPricesV3（若 hero 兼容器支持，直接就是 V3 结构）
        try:
            t = self._get("getPricesV3", service=service, country=country)
            j = json.loads(t)
            if isinstance(j, dict) and str(country) in j:
                return j
        except Exception:
            pass

        # 2) 回落 getPrices（sms-activate 经典接口）
        try:
            t = self._get("getPrices", service=service, country=country)
            j = json.loads(t)
            if isinstance(j, dict):
                # 经典格式: {"<service>": {"<country_id>": {"price": "...", "count": n}}}
                cc = str(country)
                svc_block = j.get(service) or j.get(str(service))
                if isinstance(svc_block, dict) and cc in svc_block:
                    info = svc_block[cc]
                    return {cc: {service: {"count": int(info.get("count") or 0),
                                           "price": str(info.get("price") or ""),
                                           "providers": {}}}}
                # 另一种可能: {"<country_id>": {"<service>": {...}}}
                if cc in j:
                    return j
                return {"raw": t}
        except Exception as e:
            log.debug("getPrices 失败: %s", e)
        return {"raw": t}

    # ---------- 服务清单（探测用）----------
    def list_services(self) -> list[dict]:
        """getServicesList -> [{"code": "...", "name": "..."}]；失败返回 []"""
        try:
            j = json.loads(self._get("getServicesList"))
            if isinstance(j, dict) and isinstance(j.get("services"), list):
                return j["services"]
            if isinstance(j, list):
                return j
        except Exception as e:
            log.debug("getServicesList 失败: %s", e)
        return []

    def probe_services(self, candidates, country: str = "52") -> list:
        """hero 下优先用服务清单模糊匹配，找不到再用价格接口逐个试"""
        # 1) 服务清单关键词匹配
        try:
            svcs = self.list_services()
            if svcs:
                hit = []
                kws = ("xhs", "red", "xiaohongshu", "rednote", "hbook", "redbook")
                for s in svcs:
                    code = str(s.get("code") or "")
                    name = str(s.get("name") or "")
                    blob = (code + " " + name).lower()
                    if any(k in blob for k in kws):
                        hit.append(code)
                if hit:
                    log.info("hero 服务清单匹配: %s", hit)
                    return sorted(set(hit))
                log.warning("hero 服务清单里没找到小红书相关，返回全部候选前 20 个供参考")
                return [s.get("code") for s in svcs][:20]
        except Exception:
            pass
        # 2) 传统逐服务探测
        return super().probe_services(candidates, country)


if __name__ == "__main__":
    import os
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = os.getenv("HERO_API_KEY", "")
    cli = HeroSMS(key)
    print("balance:", cli.balance())
    if "xhs" in (os.getenv("PROBE", "") or ""):
        print("services:", cli.probe_services(["xhs", "qf", "rednote", "redbook", "xh"]))