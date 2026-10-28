# -*- coding: utf-8 -*-
"""
Tiger SMS 接码平台客户端（sms-activate 兼容协议）
文档来源: tiger-sms-api.postman_collection.json
    GET https://api.tiger-sms.com/stubs/handler_api.php?action=...

核心接口:
    getNumber  -> ACCESS_NUMBER:<id>:<phone>
    getStatus  -> STATUS_WAIT_CODE / STATUS_OK:<code> / STATUS_WAIT_RETRY:<code>
    setStatus  -> 状态: 1=已就绪 3=重发 6=完成 8=取消
"""
from __future__ import annotations

import re
import time
import logging
from dataclasses import dataclass
from typing import Optional

import requests

log = logging.getLogger("tiger")

BASE = "https://api.tiger-sms.com/stubs/handler_api.php"


class TigerError(RuntimeError):
    """接码平台返回了错误状态"""


@dataclass
class Activation:
    activation_id: str
    phone: str          # 平台原始号码，通常带国家码
    raw: str = ""

    def id_dict(self) -> dict:
        """可 JSON 序列化的摘要（写结果文件用）"""
        return {"id": self.activation_id, "phone": self.phone}

    def local_number(self, country_code: str = "66") -> str:
        """去掉国家码前缀，得到填进输入框的本地号码"""
        return strip_country_prefix(self.phone, country_code)


def strip_country_prefix(phone: str, country_code: str = "66") -> str:
    """把 66xxxxxxxx 变成 xxxxxxxx；不带前缀时原样返回"""
    digits = re.sub(r"\D", "", phone or "")
    cc = re.sub(r"\D", "", country_code or "")
    if cc and digits.startswith(cc) and len(digits) > len(cc) + 6:
        return digits[len(cc):]
    return digits


class TigerSMS:
    def __init__(self, api_key: str, base: str = BASE, timeout: int = 30, proxy: str | None = None):
        if not api_key:
            raise ValueError("api_key 不能为空")
        self.api_key = api_key
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        # 关键：默认绕开系统代理（Clash/V2Ray 会对该域名 SSL EOF），有需要再显式指定
        self.session.trust_env = False
        if proxy:
            self.session.proxies = {"http": proxy, "https": proxy}
        else:
            self.session.proxies = {"http": None, "https": None}

    # ---------- 底层 ----------
    def _get(self, action: str, **params) -> str:
        q = {"action": action, "api_key": self.api_key}
        q.update({k: v for k, v in params.items() if v is not None})
        r = self.session.get(self.base, params=q, timeout=self.timeout)
        text = (r.text or "").strip()
        if r.status_code == 401:
            raise TigerError("BAD_KEY: api_key 无效")
        log.debug("GET %s %s -> %s %s", action, params, r.status_code, text[:200])
        return text

    # ---------- 查询 ----------
    def balance(self) -> str:
        t = self._get("getBalance")
        return t.split(":", 1)[1] if t.startswith("ACCESS_BALANCE:") else t

    def prices(self, service: str, country: str) -> dict:
        t = self._get("getPricesV3", service=service, country=country)
        try:
            import json
            return json.loads(t)
        except Exception:
            return {"raw": t}

    def active_activations(self) -> dict:
        t = self._get("getActiveActivations")
        try:
            import json
            return json.loads(t)
        except Exception:
            return {"raw": t}

    # ---------- 取号 ----------
    def get_number(self, service: str, country: str = "52",
                   max_price: str | float | None = None) -> Activation:
        """
        返回 Activation；NO_NUMBERS / NO_BALANCE / WRONG_MAX_PRICE 等会抛 TigerError
        max_price: 单价上限，只下发该价格以内的号（如美国限制 0.05）
        """
        params = {"service": service, "country": country}
        if max_price not in (None, "", 0, "0"):
            params["maxPrice"] = str(max_price)
        t = self._get("getNumber", **params)
        if t.startswith("ACCESS_NUMBER:"):
            _, aid, phone = t.split(":", 2)
            return Activation(activation_id=aid.strip(), phone=phone.strip(), raw=t)
        raise TigerError(f"取号失败: {t}")

    def get_number_retry(self, service: str, country: str = "52",
                         attempts: int = 5, interval: float = 3.0,
                         max_price: str | float | None = None) -> Activation:
        last = ""
        for i in range(1, attempts + 1):
            try:
                return self.get_number(service, country, max_price=max_price)
            except TigerError as e:
                last = str(e)
                log.warning("取号第 %d/%d 次失败: %s", i, attempts, last)
                if "NO_BALANCE" in last or "BAD_KEY" in last:
                    raise
                time.sleep(interval)
        raise TigerError(f"连续取号失败: {last}")

    # ---------- 状态 ----------
    def status(self, activation_id: str) -> str:
        return self._get("getStatus", id=activation_id)

    def status_v2(self, activation_id: str) -> dict:
        t = self._get("getStatusV2", id=activation_id)
        try:
            import json
            return json.loads(t)
        except Exception:
            return {"raw": t}

    def set_status(self, activation_id: str, status: int) -> str:
        """status: 1 就绪 / 3 重发 / 6 完成 / 8 取消"""
        return self._get("setStatus", id=activation_id, status=status)

    def mark_ready(self, activation_id: str) -> str:
        return self.set_status(activation_id, 1)

    def complete(self, activation_id: str) -> str:
        return self.set_status(activation_id, 6)

    def cancel(self, activation_id: str) -> str:
        return self.set_status(activation_id, 8)

    # ---------- 等码 ----------
    @staticmethod
    def parse_code(status_text: str) -> Optional[str]:
        """STATUS_OK:852508 -> 852508"""
        if not status_text:
            return None
        m = re.match(r"STATUS_OK[:\s]*(\d{4,8})", status_text.strip())
        return m.group(1) if m else None

    def wait_code(self, activation_id: str, timeout: float = 90.0, interval: float = 5.0) -> Optional[str]:
        """
        轮询等码，默认 90 秒。
        返回验证码；超时返回 None（调用方负责 cancel + 换号）。
        """
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            try:
                st = self.status(activation_id)
            except Exception as e:      # 网络抖动不终止
                log.warning("getStatus 异常: %s", e)
                time.sleep(interval)
                continue
            if st != last:
                log.info("status = %s", st)
                last = st
            code = self.parse_code(st)
            if code:
                return code
            if st == "STATUS_CANCEL" or st == "NO_ACTIVATION":
                return None
            time.sleep(interval)
        log.warning("等待 %ss 未收到验证码", timeout)
        return None

    # ---------- 探测 ----------
    def probe_services(self, candidates, country: str = "52") -> list:
        """用 getPricesV3 探测哪些 service 代码在小红书/该国家下可用"""
        ok = []
        for s in candidates:
            try:
                d = self.prices(s, country)
                if isinstance(d, dict) and str(country) in d and s in d[str(country)]:
                    ok.append(s)
                    log.info("service 可用: %s %s", s, d[str(country)][s])
            except Exception as e:
                log.debug("probe %s 失败: %s", s, e)
        return ok


if __name__ == "__main__":
    import os
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    key = os.getenv("TIGER_SMS_KEY", "")
    cli = TigerSMS(key)
    print("balance:", cli.balance())
