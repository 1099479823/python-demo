# -*- coding: utf-8 -*-
"""
依次登录多个比特浏览器窗口（串行：开一台 → 登录 → 关一台 → 再开下一台）

设计要点（针对几十上百台的长时间批跑）:
    * 子进程隔离：单台崩溃/卡死不影响后面的窗口
    * 每台独立结果 results/NN_窗口名_id.json（含 cookie，可直接复用）
    * 每台独立日志 logs/NN_id.log，出问题能复盘
    * --resume 断点续跑：跳过已成功的窗口
    * --timeout 单台超时，超时自动关窗止损
    * --min-balance 余额熔断
    * --shuffle 打乱顺序，避免同代理商相邻 IP 段被关联
    * 汇总 batch_summary.json（逐台状态 + 余额变化）

用法:
    # 先小批量试水
    python xhs_batch.py --limit 10 --resume --sleep 15 --min-balance 0.5

    # 正式跑 100 台（分批执行，每批 20 台）
    python xhs_batch.py --limit 20 --resume --sleep 15 --min-balance 0.5

    # 指定区间 / 指定 ID
    python xhs_batch.py --start 91 --end 100
    python xhs_batch.py --ids id1,id2,id3

    # 不耗号码，只验证发码+滑块
    python xhs_batch.py --limit 2 --dry-run
"""
from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import requests

import config as C
from tiger_sms import TigerSMS

PY = sys.executable
RESULT_DIR = Path("results")
LOG_DIR = Path("logs")
SUMMARY = Path("batch_summary.json")


# ------------------------------------------------------------------ 窗口
def list_browsers() -> list[dict]:
    """
    按配置的分组（BIT_BROWSER_GROUP）分页拉取窗口：
    先 /group/list 把分组名换成 groupId，再 /browser/list 分页
    （page 从 0 开始；pageSize 上限 100，超出仍只返回 100 条）。
    """
    import bit_client
    items = bit_client.list_browsers(group_name=C.BROWSER_GROUP or None)
    return [b for b in items if not b.get("isDelete") and not b.get("isRemove")]


def seq_of(b: dict) -> int:
    n = "".join(ch for ch in (b.get("name") or "") if ch.isdigit())
    return int(n) if n else 0


def parse_seq_spec(spec: str) -> set[int]:
    """'1-20,35,40-45' -> {1..20, 35, 40..45}"""
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


def pick(args) -> list[dict]:
    items = list_browsers()

    if args.ids:
        want = [x.strip() for x in args.ids.split(",") if x.strip()]
        order = {w: i for i, w in enumerate(want)}
        return sorted([b for b in items if b["id"] in want], key=lambda b: order[b["id"]])

    if args.prefix:
        items = [b for b in items if args.prefix in (b.get("name") or "")]
    items.sort(key=seq_of)

    if args.exclude:
        ex = parse_seq_spec(args.exclude)
        before = len(items)
        items = [b for b in items if seq_of(b) not in ex]
        print(f"排除已登录窗口 {before - len(items)} 台（序号 {sorted(ex)}）")

    if args.start:
        items = [b for b in items if seq_of(b) >= args.start]
    if args.end:
        items = [b for b in items if seq_of(b) <= args.end]
    # 注意：这里不截断 limit，交给 main 在 resume 过滤之后再截断，
    # 否则 --limit 20 --resume 每轮都只看前 20 台，永远推进不了。

    if args.shuffle:
        random.Random(args.seed).shuffle(items)
    return items


def close_browser(bid: str) -> None:
    try:
        requests.post(f"{C.BIT_API}/browser/close", json={"id": bid}, timeout=60)
    except Exception:
        pass


def wait_all_closed(bid: str, timeout: float = 30) -> None:
    """等窗口真的关掉，避免下一台抢内存被拒绝启动"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            b = next((x for x in list_browsers() if x["id"] == bid), None)
            if not b or not b.get("pid"):
                return
        except Exception:
            return
        time.sleep(1.5)


# ------------------------------------------------------------------ 余额
def balance() -> float:
    try:
        cli = TigerSMS(C.TIGER_API_KEY, base=C.TIGER_BASE, proxy=C.TIGER_PROXY)
        return float(cli.balance())
    except Exception as e:
        print(f"  (余额查询失败: {e})")
        return -1.0


# ------------------------------------------------------------------ 断点
def load_done() -> set[str]:
    if not SUMMARY.exists():
        return set()
    try:
        d = json.loads(SUMMARY.read_text(encoding="utf-8"))
        return {r["id"] for r in d.get("results", []) if r.get("ok")}
    except Exception:
        return set()


# ------------------------------------------------------------------ 单台
def run_one(b: dict, args) -> dict:
    seq = seq_of(b)
    tag = f"{seq:03d}_{b['id'][:8]}"
    out = RESULT_DIR / f"{tag}.json"
    logf = LOG_DIR / f"{tag}.log"

    cmd = [PY, "xhs_login.py",
           "--browser-id", b["id"],
           "--out", str(out),
           "--wait-seconds", str(args.wait_seconds),
           "--max-numbers", str(args.max_numbers)]
    if args.dry_run:
        cmd.append("--dry-run")
    if args.scan_logged:
        cmd.append("--scan-only")

    t0 = time.time()
    res = {"seq": seq, "name": b.get("name"), "id": b["id"],
           "ok": False, "reason": "", "file": str(out), "log": str(logf)}

    try:
        with open(logf, "w", encoding="utf-8") as f:
            proc = subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT,
                                  text=True, timeout=args.timeout)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        res["reason"] = f"超时({args.timeout}s)"
        res["seconds"] = round(time.time() - t0, 1)
        close_browser(b["id"])          # 卡死兜底关窗
        return res
    except Exception as e:
        res["reason"] = f"启动失败:{e}"
        res["seconds"] = round(time.time() - t0, 1)
        return res

    res["seconds"] = round(time.time() - t0, 1)
    res["returncode"] = rc

    if out.exists():
        try:
            d = json.loads(out.read_text(encoding="utf-8"))
            res["ok"] = bool(d.get("ok"))
            res["reason"] = d.get("reason") or d.get("note") or ""
            res["message"] = d.get("message") or ""
            res["last_success"] = d.get("last_success")
            res["phone"] = d.get("phone")
            res["local"] = d.get("local")
            res["url"] = d.get("url")
        except Exception:
            res["reason"] = "结果文件解析失败"
    if not res["ok"] and not res["reason"]:
        tail = []
        try:
            tail = logf.read_text(encoding="utf-8", errors="replace").strip().splitlines()[-3:]
        except Exception:
            pass
        res["reason"] = (" / ".join(tail))[:200] or f"退出码 {rc}"

    close_browser(b["id"])              # 脚本异常退出时窗口可能还开着
    wait_all_closed(b["id"])
    return res


# ------------------------------------------------------------------ 汇总
def save_summary(results: list[dict], total: int, bal0: float) -> None:
    SUMMARY.write_text(json.dumps({
        "total": total,
        "done": len(results),
        "ok": sum(1 for x in results if x["ok"]),
        "balance_start": bal0,
        "balance_now": balance(),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }, ensure_ascii=False, indent=2), encoding="utf-8")


# ------------------------------------------------------------------ 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="依次登录多个窗口")
    ap.add_argument("--ids", default="", help="逗号分隔窗口 ID（按给定顺序执行）")
    ap.add_argument("--prefix", default="小红书测试", help="按名称筛选")
    ap.add_argument("--start", type=int, default=0, help="名称序号下限")
    ap.add_argument("--end", type=int, default=0, help="名称序号上限")
    ap.add_argument("--exclude", default="",
                    help="排除的窗口序号，支持区间：1-20,35,40-45（已登录的直接排除）")
    ap.add_argument("--limit", type=int, default=10, help="最多跑几台（0=不限）")
    ap.add_argument("--resume", action="store_true", help="跳过 batch_summary 里已成功的窗口")
    ap.add_argument("--scan-logged", action="store_true",
                    help="只扫描登录态并登记（已登录的导 cookie、未登录的直接跳过），不花号码")
    ap.add_argument("--shuffle", action="store_true", help="打乱执行顺序")
    ap.add_argument("--seed", type=int, default=20261028)
    ap.add_argument("--retry", type=int, default=0, help="单台失败重试次数")
    ap.add_argument("--sleep", type=float, default=12.0, help="两台之间的间隔秒数")
    ap.add_argument("--timeout", type=int, default=480, help="单台超时秒数")
    ap.add_argument("--min-balance", type=float, default=0.0, help="余额低于该值停止")
    ap.add_argument("--wait-seconds", type=int, default=C.CODE_WAIT_SECONDS)
    ap.add_argument("--max-numbers", type=int, default=C.MAX_NUMBERS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    RESULT_DIR.mkdir(exist_ok=True)
    LOG_DIR.mkdir(exist_ok=True)

    targets = pick(args)
    if not targets:
        print("没有匹配的窗口，检查 --prefix / --ids")
        return 2

    if args.resume:
        done = load_done()
        before = len(targets)
        targets = [b for b in targets if b["id"] not in done]
        print(f"断点续跑：跳过已完成 {before - len(targets)} 台，剩 {len(targets)} 台")
        if not targets:
            print("全部已完成")
            return 0

    # 先续跑去重，再截断 limit —— 这样每轮都往后推进
    if args.limit:
        targets = targets[: args.limit]

    bal0 = balance()
    print(f"共 {len(targets)} 个窗口，余额 {bal0:.4f}，单台超时 {args.timeout}s")
    print("=" * 62)

    results: list[dict] = []
    for i, b in enumerate(targets, 1):
        # 余额熔断
        if args.min_balance > 0:
            bal = balance()
            if 0 <= bal < args.min_balance:
                print(f"\n余额 {bal:.4f} < {args.min_balance}，停止（已完成 {len(results)} 台）")
                break

        print(f"\n[{i}/{len(targets)}] {b.get('name')}  {b['id']}")
        r = None
        for attempt in range(args.retry + 1):
            if attempt:
                print(f"  重试 {attempt}/{args.retry}")
                time.sleep(5)
            r = run_one(b, args)
            if r["ok"]:
                break
        results.append(r)
        print(f"  -> {'✅ 成功' if r['ok'] else '❌ 失败'}  {r['reason']}  ({r['seconds']}s)")

        save_summary(results, len(targets), bal0)

        # 两国都拿不到号：全局性问题，继续跑只是白等，直接停整个批次
        if not r["ok"] and (r.get("returncode") == 30 or r.get("reason") == "no_numbers"):
            tip = r.get("message") or "当前时段无号码可用（泰国与美国均取不到号或收不到验证码）"
            print("\n" + "!" * 62)
            print(tip)
            print(f"已停止：完成 {len(results)}/{len(targets)} 台；等号码补货后重跑同一命令，"
                  f"--resume 会自动跳过已成功的窗口。")
            print("!" * 62)
            break

        if i < len(targets):
            time.sleep(args.sleep)

    okn = sum(1 for r in results if r["ok"])
    bal1 = balance()
    print("\n" + "=" * 62)
    print(f"完成: {okn}/{len(results)} 成功")
    print(f"余额: {bal0:.4f} -> {bal1:.4f}  (花费 {bal0 - bal1:.4f})")
    print(f"明细: {SUMMARY.resolve()}")
    print(f"逐台结果: {RESULT_DIR.resolve()}   日志: {LOG_DIR.resolve()}")
    return 0 if okn == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
