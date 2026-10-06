#!/usr/bin/env python3
"""本地冒烟测试：对运行中的 A/B 两节点验证完整交换流程与边界行为。

用法（需先启动两个节点，见 README「本地开发」）：
    python3 scripts/smoke_test.py [--a http://127.0.0.1:18001] [--b http://127.0.0.1:18002] [--secret testsecret]
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import os
import sys
import time
import urllib.parse

import httpx

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILED = []


def check(name: str, cond: bool, extra: str = "") -> None:
    mark = "ok " if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f" — {extra}" if extra and not cond else ""))
    if not cond:
        FAILED.append(name)


def token_of(link: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(link).query)["token"][0]


def wait_state(base: str, ex: str, token: str, pred, desc: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = httpx.get(f"{base}/api/exchanges/{ex}/state", params={"token": token})
        last = r.json()
        if pred(last):
            return last
        time.sleep(0.4)
    raise TimeoutError(f"{base} exchange {ex} 未在 {timeout}s 内满足 {desc}，最后状态: {last}")


def wait_phase(base: str, ex: str, token: str, phase: str, timeout: float = 30.0) -> dict:
    return wait_state(base, ex, token, lambda s: s["phase"] == phase, f"phase={phase}", timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="http://127.0.0.1:18001")
    ap.add_argument("--b", default="http://127.0.0.1:18002")
    ap.add_argument("--secret", default="testsecret")
    args = ap.parse_args()
    A, B, KEY = args.a, args.b, {"X-Node-Key": args.secret}

    a_file = os.urandom(2048)
    b_file = os.urandom(4096)
    a_sha = hashlib.sha256(a_file).hexdigest()
    b_sha = hashlib.sha256(b_file).hexdigest()

    print("== 1. 创建交换 ==")
    d = httpx.post(f"{A}/api/exchanges").json()
    ex, a_tok, b_tok = d["exchange_id"], token_of(d["a_link"]), token_of(d["b_link"])
    check("创建交换并返回双方随机链接", bool(ex and a_tok and b_tok and a_tok != b_tok))

    print("== 2. 提交约束 ==")
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": a_tok}, files={"file": ("x", b"")})
    check("0 字节拒绝", r.status_code == 400)
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": a_tok}, files={"file": ("x", os.urandom(65537))})
    check("64KiB+1 拒绝", r.status_code == 400)
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": "wrong"}, files={"file": ("x", a_file)})
    check("错误 token 404", r.status_code == 404)
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": a_tok}, files={"file": ("a.bin", a_file)})
    check("A 提交成功", r.status_code == 200 and r.json()["sha256"] == a_sha)
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": a_tok}, files={"file": ("a.bin", a_file)})
    check("A 同内容重复提交返回原回执", r.status_code == 200 and r.json()["repeated"] is True)
    r = httpx.post(f"{A}/api/exchanges/{ex}/submit", params={"token": a_tok}, files={"file": ("a.bin", b"different")})
    check("A 异内容 409", r.status_code == 409)

    print("== 3. 决议前下载限制 ==")
    r = httpx.get(f"{A}/api/exchanges/{ex}/download", params={"token": a_tok, "which": "peer"})
    check("A 决议前下载对方文件 403", r.status_code == 403)

    print("== 4. B 提交 ==")
    # B 的交换记录由 A 异步注册，先等注册完成（页面场景下表现为稍候自动刷新）
    wait_state(A, ex, a_tok, lambda s: s["peer_registered"], "peer_registered", timeout=15)
    r = httpx.post(f"{B}/api/exchanges/{ex}/submit", params={"token": b_tok}, files={"file": ("b.bin", b_file)})
    check("B 提交成功", r.status_code == 200 and r.json()["sha256"] == b_sha)
    r = httpx.post(f"{B}/api/exchanges/{ex}/submit", params={"token": b_tok}, files={"file": ("b.bin", b"different")})
    check("B 异内容 409", r.status_code == 409)

    print("== 5. 等待协议收敛（B 若开启演练会先 503 一次）==")
    sa = wait_phase(A, ex, a_tok, "complete")
    sb = wait_phase(B, ex, b_tok, "complete")
    check("A 完成且拿到 B 摘要", sa["peer"]["sha256"] == b_sha)
    check("B 完成且拿到 A 摘要", sb["peer"]["sha256"] == a_sha)
    check("双方决议时间一致", sa["resolution_created_at"] == sb["resolution_created_at"])

    print("== 6. 决议后下载 ==")
    r = httpx.get(f"{A}/api/exchanges/{ex}/download", params={"token": a_tok, "which": "peer"})
    check("A 下载到 B 文件且内容一致", r.status_code == 200 and r.content == b_file)
    r = httpx.get(f"{B}/api/exchanges/{ex}/download", params={"token": b_tok, "which": "peer"})
    check("B 下载到 A 文件且内容一致", r.status_code == 200 and r.content == a_file)

    print("== 7. B 决议接口幂等（模拟 A 并发重试）==")
    body = {
        "a_sha256": a_sha,
        "b_sha256": b_sha,
        "resolution_created_at": sa["resolution_created_at"],
        "a_file": __import__("base64").b64encode(a_file).decode(),
        "a_filename": "a.bin",
    }
    with concurrent.futures.ThreadPoolExecutor(4) as pool:
        codes = list(pool.map(lambda _: httpx.post(f"{B}/internal/exchanges/{ex}/resolution", json=body, headers=KEY).status_code, range(4)))
    check("并发重放同一决议全部 200", codes == [200, 200, 200, 200], str(codes))
    bad = dict(body, resolution_created_at="2099-01-01T00:00:00+00:00")
    r = httpx.post(f"{B}/internal/exchanges/{ex}/resolution", json=bad, headers=KEY)
    check("异内容决议 409", r.status_code == 409)
    r = httpx.post(f"{B}/internal/exchanges/{ex}/resolution", json=body, headers={"X-Node-Key": "nope"})
    check("错误节点密钥 401", r.status_code == 401)

    print()
    if FAILED:
        print(f"失败 {len(FAILED)} 项: {FAILED}")
        return 1
    print("全部通过 ✔")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
