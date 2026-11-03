# -*- coding: utf-8 -*-
"""
接码平台统一入口：按配置的 SMS_PROVIDER 返回对应客户端实例。

    SMS_PROVIDER=tiger  -> TigerSMS（默认，走 TIGER_* 配置）
    SMS_PROVIDER=hero   -> HeroSMS（走 HERO_* 配置）

所有构造点请改用 make_sms_client()，不要直接 new TigerSMS/HeroSMS。
"""
from __future__ import annotations

import logging

import config as C
from tiger_sms import TigerSMS

log = logging.getLogger("sms")

_SUPPORTED = ("tiger", "hero")


def make_sms_client(provider: str | None = None):
    """
    按配置创建接码客户端。provider 显式传值可覆盖配置（测试用）。

    返回对象接口（duck typing，两平台一致）：
        get_number(service, country, max_price)   -> Activation
        get_number_retry(...)
        status(aid) / wait_code(aid, timeout, interval)
        mark_ready(aid) / complete(aid) / cancel(aid)
        cancel_with_retry(aid, created_at)
        balance() / prices(service, country) / active_activations()
        probe_services(candidates, country)
    """
    p = (provider or C.SMS_PROVIDER or "tiger").strip().lower()
    if p not in _SUPPORTED:
        raise ValueError(
            f"SMS_PROVIDER={p!r} 不支持，可选: {', '.join(_SUPPORTED)}"
            "（在 .env 里配置 SMS_PROVIDER=tiger|hero）")

    proxy = getattr(C, "SMS_PROXY", None) or getattr(C, "TIGER_PROXY", None)

    if p == "hero":
        from hero_sms import HeroSMS
        if not getattr(C, "HERO_API_KEY", ""):
            raise ValueError("SMS_PROVIDER=hero 但 HERO_API_KEY 为空，请在 .env 里配置")
        cli = HeroSMS(C.HERO_API_KEY, base=getattr(C, "HERO_BASE", "https://hero-sms.com/stubs/handler_api.php"),
                      proxy=proxy)
        log.info("接码平台: hero (%s)", cli.base)
        return cli

    # tiger
    if not getattr(C, "TIGER_API_KEY", ""):
        raise ValueError("SMS_PROVIDER=tiger 但 TIGER_API_KEY 为空，请在 .env 里配置")
    cli = TigerSMS(C.TIGER_API_KEY, base=getattr(C, "TIGER_BASE", ""), proxy=proxy)
    log.info("接码平台: tiger (%s)", cli.base)
    return cli


def current_service(provider: str | None = None) -> str:
    """
    当前平台的 service 代码：
    hero 平台优先 HERO_SERVICE，其次 SERVICE / TIGER_SERVICE。
    """
    p = (provider or C.SMS_PROVIDER or "tiger").strip().lower()
    if p == "hero":
        # hero 优先 HERO_SERVICE，其次通用 SERVICE，再其次 tiger 的 TIGER_SERVICE（两平台常用同一代码）
        return (getattr(C, "HERO_SERVICE", "") or getattr(C, "SERVICE", "")
                or getattr(C, "TIGER_SERVICE", "") or "")
    return getattr(C, "SERVICE", "") or getattr(C, "TIGER_SERVICE", "")


if __name__ == "__main__":
    import sys
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")
    cli = make_sms_client()
    print("provider:", C.SMS_PROVIDER)
    print("balance :", cli.balance())
    print("service :", current_service())