"""FastAPI 路由：参与链接页面 / 提交 / 状态 / 下载 + 节点间内部接口。"""
from __future__ import annotations

import asyncio
import os
import secrets
from contextlib import asynccontextmanager
from urllib.parse import quote

import httpx
from fastapi import Body, FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import HTMLResponse, Response

from . import config, db, pages, protocol


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    print(
        f"[node] role={config.ROLE} peer={config.PEER_URL} "
        f"drill_503={config.DRILL_FAIL_FIRST_RESOLUTION} db={db.DB_PATH}"
    )
    task = asyncio.create_task(protocol.worker_loop())
    yield
    task.cancel()


app = FastAPI(title=f"冻结交换节点 {config.ROLE}", lifespan=lifespan)


# ---------------------------------------------------------------- 工具

def _own_token(row) -> str:
    return row["token_a"] if config.ROLE == "A" else row["token_b"]


def _load_party_exchange(ex_id: str, token: str):
    """凭参与链接 token 取交换；失败一律 404（不泄露存在性）。"""
    row = db.get_exchange(ex_id)
    if row is None or not token or not secrets.compare_digest(token, _own_token(row)):
        raise HTTPException(status_code=404, detail="交换不存在或链接无效")
    return row


def _require_node_key(x_node_key: str | None) -> None:
    if not x_node_key or not secrets.compare_digest(x_node_key, config.NODE_SECRET):
        raise HTTPException(status_code=401, detail="节点认证失败")


def _compute_phase(row):
    if config.ROLE == "A":
        if row["a_sha256"] is None:
            return "awaiting_own", "等待本方提交"
        if row["resolution_created_at"] is None:
            return "awaiting_peer", "等待对方提交"
        if row["status"] != "COMPLETE":
            return "pending_sync", "待同步"
        return "complete", "完成"
    else:
        if row["b_sha256"] is None:
            return "awaiting_own", "等待本方提交"
        if row["resolution_created_at"] is None:
            return "awaiting_peer", "等待 A 决议"
        if not row["receipt_acked"]:
            return "pending_sync", "待同步"
        return "complete", "完成"


def _build_state(row) -> dict:
    phase, phase_text = _compute_phase(row)
    if config.ROLE == "A":
        me = {
            "submitted": row["a_sha256"] is not None,
            "sha256": row["a_sha256"],
            "filename": row["a_filename"],
            "submitted_at": row["a_submitted_at"],
        }
        peer = {
            "sha256": row["b_sha256"],
            "filename": row["b_filename"],
            "downloadable": row["b_file"] is not None,
        }
    else:
        me = {
            "submitted": row["b_sha256"] is not None,
            "sha256": row["b_sha256"],
            "filename": row["b_filename"],
            "submitted_at": row["b_submitted_at"],
        }
        peer = {
            "sha256": row["a_sha256"],
            "filename": row["a_filename"],
            "downloadable": row["a_file"] is not None,
        }
    return {
        "exchange_id": row["id"],
        "role": config.ROLE,
        "phase": phase,
        "phase_text": phase_text,
        "me": me,
        "peer": peer,
        "resolution_created_at": row["resolution_created_at"],
        "peer_registered": bool(row["b_registered"]),
        "drill_enabled": config.DRILL_FAIL_FIRST_RESOLUTION,
    }


def _clean_filename(name: str | None, default: str) -> str:
    name = os.path.basename(name or "")[:128]
    return name or default


# ---------------------------------------------------------------- 页面

@app.get("/", response_class=HTMLResponse)
def home():
    return pages.home_page()


@app.get("/e/{ex_id}", response_class=HTMLResponse)
def exchange_page(ex_id: str, token: str = Query("")):
    _load_party_exchange(ex_id, token)
    return pages.exchange_page(ex_id)


# ---------------------------------------------------------------- 参与方 API

@app.post("/api/exchanges")
def create_exchange():
    """仅 A 创建交换，产生双方各一条随机参与链接。"""
    if config.ROLE != "A":
        raise HTTPException(status_code=405, detail="只有 A 节点可以创建交换")
    ex_id = secrets.token_hex(8)
    token_a = secrets.token_urlsafe(24)
    token_b = secrets.token_urlsafe(24)
    with db.tx() as conn:
        conn.execute(
            "INSERT INTO exchanges (id, token_a, token_b, status, created_at) VALUES (?,?,?,?,?)",
            (ex_id, token_a, token_b, "OPEN", protocol.now_iso()),
        )
    print(f"[A] exchange {ex_id}: 已创建")
    return {
        "exchange_id": ex_id,
        "a_link": f"{config.PUBLIC_BASE}/e/{ex_id}?token={token_a}",
        "b_link": f"{config.PEER_PUBLIC_BASE}/e/{ex_id}?token={token_b}",
    }


@app.get("/api/exchanges")
def list_exchanges():
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM exchanges ORDER BY created_at DESC, id").fetchall()
    finally:
        conn.close()
    items = []
    for row in rows:
        phase, phase_text = _compute_phase(row)
        item = {
            "exchange_id": row["id"],
            "phase": phase,
            "phase_text": phase_text,
            "created_at": row["created_at"],
            "resolution_created_at": row["resolution_created_at"],
            "a_sha256": row["a_sha256"],
            "b_sha256": row["b_sha256"],
        }
        if config.ROLE == "A":
            item["a_link"] = f"{config.PUBLIC_BASE}/e/{row['id']}?token={row['token_a']}"
            item["b_link"] = f"{config.PEER_PUBLIC_BASE}/e/{row['id']}?token={row['token_b']}"
        else:
            item["b_link"] = f"{config.PUBLIC_BASE}/e/{row['id']}?token={row['token_b']}"
        items.append(item)
    return {"role": config.ROLE, "exchanges": items}


@app.get("/api/exchanges/{ex_id}/state")
def exchange_state(ex_id: str, token: str = Query("")):
    row = _load_party_exchange(ex_id, token)
    return _build_state(row)


@app.post("/api/exchanges/{ex_id}/submit")
async def submit_file(ex_id: str, file: UploadFile = File(...), token: str = Query("")):
    """提交并冻结本方文件：同内容幂等返回原回执，异内容 409。"""
    data = await file.read(config.MAX_FILE_SIZE + 1)
    if not (config.MIN_FILE_SIZE <= len(data) <= config.MAX_FILE_SIZE):
        raise HTTPException(
            status_code=400,
            detail=f"文件大小须在 {config.MIN_FILE_SIZE}～{config.MAX_FILE_SIZE} 字节之间",
        )
    digest = protocol.sha256_hex(data)
    filename = _clean_filename(file.filename, "file.bin")
    side = "a" if config.ROLE == "A" else "b"
    now = protocol.now_iso()
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
        if row is None or not token or not secrets.compare_digest(token, _own_token(row)):
            raise HTTPException(status_code=404, detail="交换不存在或链接无效")
        existing = row[f"{side}_sha256"]
        if existing is not None:
            if existing == digest and row[f"{side}_file"] == data:
                return {
                    "exchange_id": ex_id,
                    "side": config.ROLE,
                    "sha256": existing,
                    "submitted_at": row[f"{side}_submitted_at"],
                    "repeated": True,
                }
            raise HTTPException(status_code=409, detail="已冻结不同内容，摘要不可修改")
        if config.ROLE == "A":
            conn.execute(
                "UPDATE exchanges SET a_sha256=?, a_file=?, a_filename=?, a_submitted_at=? WHERE id=?",
                (digest, data, filename, now, ex_id),
            )
        else:
            new_status = "SUBMITTED" if row["status"] == "OPEN" else row["status"]
            conn.execute(
                "UPDATE exchanges SET b_sha256=?, b_file=?, b_filename=?, b_submitted_at=?, status=? WHERE id=?",
                (digest, data, filename, now, new_status, ex_id),
            )
    print(f"[{config.ROLE}] exchange {ex_id}: 本方文件已冻结 sha256={digest[:16]}…")
    return {
        "exchange_id": ex_id,
        "side": config.ROLE,
        "sha256": digest,
        "submitted_at": now,
        "repeated": False,
    }


@app.get("/api/exchanges/{ex_id}/download")
def download_file(ex_id: str, which: str = Query("peer"), token: str = Query("")):
    row = _load_party_exchange(ex_id, token)
    if which == "own":
        side = "a" if config.ROLE == "A" else "b"
        data, filename = row[f"{side}_file"], row[f"{side}_filename"] or "file.bin"
        if data is None:
            raise HTTPException(status_code=404, detail="尚未提交本方文件")
    elif which == "peer":
        if row["resolution_created_at"] is None:
            raise HTTPException(status_code=403, detail="决议达成前不得下载对方文件")
        peer_side = "b" if config.ROLE == "A" else "a"
        data = row[f"{peer_side}_file"]
        filename = row[f"{peer_side}_filename"] or "peer.bin"
        if data is None:
            raise HTTPException(status_code=409, detail="待同步：尚未收到并核对对方文件")
    else:
        raise HTTPException(status_code=400, detail="which 须为 own 或 peer")
    return Response(
        content=bytes(data),
        media_type="application/octet-stream",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@app.post("/api/exchanges/{ex_id}/retry")
async def retry_now(ex_id: str, token: str = Query("")):
    """手动触发一轮同步（与后台重试并发也安全：对端事务幂等）。"""
    _load_party_exchange(ex_id, token)
    async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
        if config.ROLE == "A":
            await protocol.a_attempt_register(client, ex_id)
            await protocol.a_attempt_resolve(client, ex_id)
            await protocol.a_attempt_deliver(client, ex_id)
        else:
            await protocol.b_attempt_push_receipt(client, ex_id)
    return _build_state(db.get_exchange(ex_id))


# ---------------------------------------------------------------- 节点间接口

@app.post("/internal/exchanges")
def internal_register(body: dict = Body(...), x_node_key: str | None = Header(None)):
    """B 侧：接受 A 的交换注册（幂等）。"""
    _require_node_key(x_node_key)
    if config.ROLE != "B":
        raise HTTPException(status_code=404, detail="not found")
    ex_id = str(body.get("id") or "")
    token_b = str(body.get("token_b") or "")
    if not ex_id or not token_b:
        raise HTTPException(status_code=400, detail="缺少 id 或 token_b")
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
        if row is not None:
            if secrets.compare_digest(row["token_b"], token_b):
                return {"ok": True, "repeated": True}
            raise HTTPException(status_code=409, detail="交换编号冲突")
        conn.execute(
            "INSERT INTO exchanges (id, token_b, status, created_at) VALUES (?,?,?,?)",
            (ex_id, token_b, "OPEN", protocol.now_iso()),
        )
    return {"ok": True, "repeated": False}


@app.get("/internal/exchanges/{ex_id}/digest")
def internal_digest(ex_id: str, x_node_key: str | None = Header(None)):
    """B 侧：向 A 提供 B 的已冻结摘要。"""
    _require_node_key(x_node_key)
    if config.ROLE != "B":
        raise HTTPException(status_code=404, detail="not found")
    row = db.get_exchange(ex_id)
    if row is None:
        raise HTTPException(status_code=404, detail="未知交换")
    return {"b_sha256": row["b_sha256"], "b_submitted_at": row["b_submitted_at"]}


@app.post("/internal/exchanges/{ex_id}/resolution")
def internal_resolution(ex_id: str, body: dict = Body(...), x_node_key: str | None = Header(None)):
    """B 侧：在事务中核对并保存决议与 A 文件；重复同内容重返回执，异内容拒绝。"""
    _require_node_key(x_node_key)
    if config.ROLE != "B":
        raise HTTPException(status_code=404, detail="not found")
    try:
        a_sha256 = str(body["a_sha256"])
        b_sha256 = str(body["b_sha256"])
        resolution_created_at = str(body["resolution_created_at"])
        a_file = protocol.b64d(str(body["a_file"]))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="决议格式错误")
    a_filename = _clean_filename(body.get("a_filename"), "a-file.bin")
    if not (config.MIN_FILE_SIZE <= len(a_file) <= config.MAX_FILE_SIZE):
        raise HTTPException(status_code=422, detail="A 文件大小超限")
    if protocol.sha256_hex(a_file) != a_sha256:
        raise HTTPException(status_code=422, detail="A 文件与其摘要不符")

    drill_now = False
    with db.tx() as conn:
        row = conn.execute("SELECT * FROM exchanges WHERE id=?", (ex_id,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="未知交换")
        if row["resolution_created_at"] is not None:
            same = (
                row["a_sha256"] == a_sha256
                and row["b_sha256"] == b_sha256
                and row["resolution_created_at"] == resolution_created_at
                and row["a_file"] == a_file
            )
            if not same:
                raise HTTPException(status_code=409, detail="与已持久化决议冲突")
        else:
            if row["b_sha256"] is None:
                raise HTTPException(status_code=409, detail="B 尚未提交文件")
            if row["b_sha256"] != b_sha256:
                raise HTTPException(status_code=409, detail="决议摘要与本地冻结摘要不符")
            drill_now = config.DRILL_FAIL_FIRST_RESOLUTION and not row["drill_fired"]
            conn.execute(
                """UPDATE exchanges
                   SET a_sha256=?, a_file=?, a_filename=?, resolution_created_at=?,
                       status='RESOLVED', drill_fired=MAX(drill_fired, ?)
                   WHERE id=?""",
                (a_sha256, a_file, a_filename, resolution_created_at,
                 1 if drill_now else 0, ex_id),
            )
            print(f"[B] exchange {ex_id}: 决议与 A 文件已核对并持久化")
        receipt = {
            "b_sha256": row["b_sha256"],
            "b_file": protocol.b64e(row["b_file"]),
            "b_filename": row["b_filename"],
            "resolution_created_at": resolution_created_at,
        }
    if drill_now:
        # 演练：事务已提交，但回执“丢失”——A 的重试将命中上面的幂等分支。
        print(f"[drill] exchange {ex_id}: 决议已持久化，按演练开关返回 503（回执丢失）")
        raise HTTPException(status_code=503, detail="drill: receipt lost after resolution persisted")
    return receipt


@app.post("/internal/exchanges/{ex_id}/receipt")
def internal_receipt(ex_id: str, body: dict = Body(...), x_node_key: str | None = Header(None)):
    """A 侧：接收 B 主动推送的回执（与决议响应路径同等幂等）。"""
    _require_node_key(x_node_key)
    if config.ROLE != "A":
        raise HTTPException(status_code=404, detail="not found")
    try:
        b_sha256 = str(body["b_sha256"])
        b_file = protocol.b64d(str(body["b_file"]))
    except (KeyError, ValueError):
        raise HTTPException(status_code=422, detail="回执格式错误")
    try:
        result = protocol.a_process_receipt(ex_id, b_sha256, b_file, body.get("b_filename"))
    except protocol.ProtocolError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    return {"ok": True, "result": result}
