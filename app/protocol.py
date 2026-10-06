"""交换协议核心：状态机推进 + 后台自动重试。

协议时序：
  1. A 创建交换（含双方随机参与 token），并把交换注册到 B；
  2. 双方各自凭链接向本节点提交文件（提交即冻结，同内容幂等、异内容 409）；
  3. A 从 B 取得 B 的冻结摘要，先在本地事务中持久化不可撤销决议
     （双方摘要 + 决议时间），再把决议和 A 文件发给 B；
  4. B 在事务中核对本地 B 摘要与收到的 A 文件摘要，保存 A 文件和决议，
     回执带 B 文件及摘要（重复消息同内容重返回执、异内容拒绝）；
  5. A 核验回执（摘要必须等于已持久化决议中的 B 摘要）并持久化 B 文件后，
     才把交换标为完成；
  6. B 也会把回执主动推给 A，直到 A 确认为止——两端各自重试，
     任一路径成功都能收敛，且幂等。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
from datetime import datetime, timezone

import httpx

from . import config, db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def b64e(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def b64d(text: str) -> bytes:
    return base64.b64decode(text.encode("ascii"), validate=True)


def node_headers() -> dict:
    return {"X-Node-Key": config.NODE_SECRET}


class ProtocolError(Exception):
    """对端消息与本地已持久化状态不一致。"""


# ---------------------------------------------------------------- A 侧

def a_process_receipt(ex_id: str, b_sha256: str, b_file: bytes, b_filename) -> str:
    """A 处理 B 的回执：核验 -> 持久化 B 文件 -> 标为完成（同一事务）。

    返回 'stored' / 'repeated'；不一致抛 ProtocolError。
    """
    if sha256_hex(b_file) != b_sha256:
        raise ProtocolError("回执文件与其摘要不符")
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
        if row is None:
            raise ProtocolError("未知交换")
        if row["status"] == "COMPLETE":
            if row["b_sha256"] == b_sha256 and row["b_file"] == b_file:
                return "repeated"
            raise ProtocolError("交换已完成，收到冲突回执")
        if row["status"] != "RESOLVED" or row["resolution_created_at"] is None:
            raise ProtocolError("本地尚无已持久化决议")
        if row["b_sha256"] != b_sha256:
            raise ProtocolError("回执摘要与已持久化决议不符")
        conn.execute(
            "UPDATE exchanges SET b_file=?, b_filename=?, status='COMPLETE' WHERE id=?",
            (b_file, b_filename, ex_id),
        )
        print(f"[A] exchange {ex_id}: B 文件已核验并持久化，交换完成")
        return "stored"


async def a_attempt_register(client: httpx.AsyncClient, ex_id: str) -> bool:
    row = db.get_exchange(ex_id)
    if row is None or row["b_registered"]:
        return True
    try:
        resp = await client.post(
            f"{config.PEER_URL}/internal/exchanges",
            json={"id": ex_id, "token_b": row["token_b"]},
            headers=node_headers(),
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    with db.tx() as conn:
        conn.execute("UPDATE exchanges SET b_registered=1 WHERE id=?", (ex_id,))
    print(f"[A] exchange {ex_id}: 已注册到 B")
    return True


def a_mark_unregistered(ex_id: str) -> None:
    """对端已不认识该交换（如对端数据被重置）时，重新走注册流程。"""
    with db.tx() as conn:
        conn.execute("UPDATE exchanges SET b_registered=0 WHERE id=?", (ex_id,))


async def a_attempt_resolve(client: httpx.AsyncClient, ex_id: str) -> bool:
    """从 B 取得已冻结摘要，并先持久化不可撤销决议。"""
    row = db.get_exchange(ex_id)
    if (
        row is None
        or row["status"] != "OPEN"
        or row["a_sha256"] is None
        or not row["b_registered"]
    ):
        return False
    try:
        resp = await client.get(
            f"{config.PEER_URL}/internal/exchanges/{ex_id}/digest",
            headers=node_headers(),
        )
    except httpx.HTTPError:
        return False
    if resp.status_code == 404:
        a_mark_unregistered(ex_id)
        return False
    if resp.status_code != 200:
        return False
    b_sha256 = resp.json().get("b_sha256")
    if not b_sha256:
        return False  # B 尚未提交
    with db.tx() as conn:
        cur = conn.execute(
            "SELECT status, resolution_created_at, a_sha256 FROM exchanges WHERE id=?",
            (ex_id,),
        ).fetchone()
        if (
            cur is None
            or cur["status"] != "OPEN"
            or cur["resolution_created_at"] is not None
            or cur["a_sha256"] is None
        ):
            return False  # 已被并发重试处理
        # 关键顺序：决议先持久化，之后只允许按此决议重发。
        conn.execute(
            "UPDATE exchanges SET b_sha256=?, resolution_created_at=?, status='RESOLVED' WHERE id=?",
            (b_sha256, now_iso(), ex_id),
        )
    print(f"[A] exchange {ex_id}: 决议已持久化 (b_sha256={b_sha256[:16]}…)")
    return True


async def a_attempt_deliver(client: httpx.AsyncClient, ex_id: str) -> bool:
    """把已持久化的决议和 A 文件发给 B，并处理回执。"""
    row = db.get_exchange(ex_id)
    if row is None:
        return False
    if row["status"] == "COMPLETE":
        return True
    if row["status"] != "RESOLVED":
        return False
    body = {
        "a_sha256": row["a_sha256"],
        "b_sha256": row["b_sha256"],
        "resolution_created_at": row["resolution_created_at"],
        "a_file": b64e(row["a_file"]),
        "a_filename": row["a_filename"],
    }
    try:
        resp = await client.post(
            f"{config.PEER_URL}/internal/exchanges/{ex_id}/resolution",
            json=body,
            headers=node_headers(),
        )
    except httpx.HTTPError:
        return False
    if resp.status_code == 404:
        a_mark_unregistered(ex_id)
        return False
    if resp.status_code != 200:
        return False  # 409/503 等：留给下一轮重试
    rc = resp.json()
    if rc.get("resolution_created_at") != row["resolution_created_at"]:
        print(f"[A] exchange {ex_id}: 回执决议时间与本地不一致，忽略")
        return False
    try:
        a_process_receipt(ex_id, rc["b_sha256"], b64d(rc["b_file"]), rc.get("b_filename"))
    except (ProtocolError, KeyError, ValueError) as exc:
        print(f"[A] exchange {ex_id}: 回执核验失败: {exc}")
        return False
    return True


# ---------------------------------------------------------------- B 侧

async def b_attempt_push_receipt(client: httpx.AsyncClient, ex_id: str) -> bool:
    """B 把回执主动推给 A，直到 A 确认（A 侧幂等）。"""
    row = db.get_exchange(ex_id)
    if row is None or row["resolution_created_at"] is None or row["receipt_acked"]:
        return True
    body = {
        "b_sha256": row["b_sha256"],
        "b_file": b64e(row["b_file"]),
        "b_filename": row["b_filename"],
    }
    try:
        resp = await client.post(
            f"{config.PEER_URL}/internal/exchanges/{ex_id}/receipt",
            json=body,
            headers=node_headers(),
        )
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    with db.tx() as conn:
        conn.execute(
            "UPDATE exchanges SET receipt_acked=1, status='COMPLETE' WHERE id=?",
            (ex_id,),
        )
    print(f"[B] exchange {ex_id}: 回执已被 A 确认，交换完成")
    return True


# ---------------------------------------------------------------- 后台重试

async def worker_tick(client: httpx.AsyncClient) -> None:
    if config.ROLE == "A":
        for ex_id in db.list_ids("b_registered = 0"):
            await a_attempt_register(client, ex_id)
        for ex_id in db.list_ids("status = 'OPEN' AND a_file IS NOT NULL AND b_registered = 1"):
            await a_attempt_resolve(client, ex_id)
        for ex_id in db.list_ids("status = 'RESOLVED'"):
            await a_attempt_deliver(client, ex_id)
    else:
        for ex_id in db.list_ids("resolution_created_at IS NOT NULL AND receipt_acked = 0"):
            await b_attempt_push_receipt(client, ex_id)


async def worker_loop() -> None:
    """重启后自动恢复：所有未确认的决议都会被持续重试。"""
    await asyncio.sleep(0.5)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        while True:
            try:
                await worker_tick(client)
            except Exception as exc:  # 保持重试循环存活
                print(f"[worker] tick 异常: {exc!r}")
            await asyncio.sleep(config.WORKER_INTERVAL)
