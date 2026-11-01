# -*- coding: utf-8 -*-
"""
比特浏览器分组 / 窗口列表查询

按需求：
    1. POST /group/list  {page, pageSize, all:true}  -> 拿到所有分组的 id + 名称
    2. 用配置里的分组名称匹配出 groupId
    3. POST /browser/list {page, pageSize, groupId}  -> 分页拉该分组下的窗口
       注意：page 从 0 开始；pageSize 最大 100，传更大的值也只返回 100 条

字段注意：/group/list 返回的分组名字段是 **groupName**（不是 name）。
"""
from __future__ import annotations

import logging

import requests

import config as C

log = logging.getLogger("bit")

MAX_PAGE_SIZE = 100          # 接口硬上限


def _post(path: str, payload: dict, timeout: int = 60) -> dict:
    try:
        r = requests.post(f"{C.BIT_API}{path}", json=payload, timeout=timeout)
        j = r.json()
    except Exception as e:
        raise RuntimeError(f"请求 {path} 失败: {e}")
    if not j.get("success"):
        raise RuntimeError(f"{path} 返回失败: {j.get('msg')}")
    return j.get("data") or {}


# ------------------------------------------------------------------ 分组
def bit_groups(page_size: int = 100) -> list[dict]:
    """拉取所有分组（自动翻页）"""
    out: list[dict] = []
    page = 0
    while True:
        data = _post("/group/list", {"page": page, "pageSize": min(page_size, MAX_PAGE_SIZE),
                                     "all": True})
        batch = data.get("list") or []
        out.extend(batch)
        total = int(data.get("totalNum") or 0)
        if len(batch) < page_size or len(out) >= total:
            break
        page += 1
    return out


def group_name_of(g: dict) -> str:
    """兼容 groupName / name 两种字段"""
    return (g.get("groupName") or g.get("name") or "").strip()


def resolve_group_id(name: str | None, groups: list[dict] | None = None) -> tuple[str | None, str]:
    """
    按分组名称找 groupId。返回 (id, 说明)。

    匹配顺序：完全相等 -> 名称包含（唯一命中才用）-> 失败
    name 为空时返回 (None, "未配置分组")，调用方按全量窗口处理。
    """
    want = (name or "").strip()
    if not want:
        return None, "未配置分组（按全量窗口处理）"

    groups = groups if groups is not None else bit_groups()
    names = [group_name_of(g) for g in groups]

    for g in groups:
        if group_name_of(g) == want:
            return g["id"], f"分组精确匹配: {want}"

    hits = [g for g in groups if want in group_name_of(g)]
    if len(hits) == 1:
        return hits[0]["id"], f"分组模糊匹配: {want} -> {group_name_of(hits[0])}"
    if len(hits) > 1:
        raise RuntimeError(f"分组名 {want!r} 匹配到多个: {[group_name_of(g) for g in hits]}，请写全名")

    raise RuntimeError(f"找不到分组 {want!r}；现有分组: {names}")


def describe_groups(groups: list[dict] | None = None) -> str:
    groups = groups if groups is not None else bit_groups()
    return "\n".join(f"  {group_name_of(g)}  (id={g['id']}, 窗口数={g.get('browserCount')})"
                     for g in groups) or "  （无分组）"


# ------------------------------------------------------------------ 窗口列表
def list_browsers(group_name: str | None = None, page_size: int = MAX_PAGE_SIZE,
                  group_id: str | None = None, **filters) -> list[dict]:
    """
    分页拉取窗口列表。

    group_name: 分组名称（None/空 -> 全量）；也可直接传 group_id
    filters:    name / remark / seq / minSeq / maxSeq / sort / ownedByMe / opened
    """
    gid = group_id
    if gid is None and group_name:
        gid, how = resolve_group_id(group_name)
        log.info("%s (groupId=%s)", how, gid)

    size = max(1, min(page_size, MAX_PAGE_SIZE))
    out: list[dict] = []
    page = 0
    while True:
        payload = {"page": page, "pageSize": size}
        if gid:
            payload["groupId"] = gid
        payload.update({k: v for k, v in filters.items() if v is not None})

        data = _post("/browser/list", payload)
        batch = data.get("list") or []
        out.extend(batch)
        total = int(data.get("totalNum") or 0)
        if len(batch) < size or len(out) >= total:
            break
        page += 1
        if page > 1000:            # 兜底，避免异常数据导致死循环
            log.warning("窗口列表翻页超过 1000 页，提前结束")
            break

    log.info("窗口列表: 分组=%s 共 %d 个（翻页 %d 次）", gid or "全量", len(out), page + 1)
    return out


def list_browser_ids(group_name: str | None = None, **filters) -> list[str]:
    return [b["id"] for b in list_browsers(group_name=group_name, **filters)]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    gs = bit_groups()
    print("分组列表:")
    print(describe_groups(gs))
    print(f"\n配置的分组名: {C.BROWSER_GROUP!r}")
    if C.BROWSER_GROUP:
        gid, how = resolve_group_id(C.BROWSER_GROUP, gs)
        print(f"解析结果: {how} -> {gid}")
        bs = list_browsers(group_id=gid)
        print(f"该分组窗口数: {len(bs)}")
