"""原生 JavaScript 页面：首页（创建/列表）与交换参与页。"""
from __future__ import annotations

import json

from . import config

STYLE = """
body { font-family: system-ui, -apple-system, sans-serif; max-width: 880px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }
h1 { font-size: 1.4rem; }
h2 { font-size: 1.05rem; margin: 0 0 .5rem; }
.card { border: 1px solid #ddd; border-radius: 8px; padding: 1rem; margin: 1rem 0; }
.badge { display: inline-block; padding: .2rem .7rem; border-radius: 999px; font-weight: 600; font-size: .9rem; }
.badge.waiting { background: #fff3cd; color: #856404; }
.badge.pending { background: #cce5ff; color: #004085; }
.badge.complete { background: #d4edda; color: #155724; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; word-break: break-all; }
.hint { color: #666; font-size: .85rem; }
button { padding: .4rem 1rem; border-radius: 6px; border: 1px solid #888; background: #f5f5f5; cursor: pointer; }
button:hover { background: #e9e9e9; }
a { color: #0b5ed7; }
"""

HOME_JS = """
const IS_A = __IS_A__;
function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function badge(phase, text) {
  const cls = phase === 'complete' ? 'complete' : (phase === 'pending_sync' ? 'pending' : 'waiting');
  return '<span class="badge ' + cls + '">' + esc(text) + '</span>';
}
function short(s) { return s ? s.slice(0, 16) + '…' : '—'; }
async function load() {
  const r = await fetch('/api/exchanges');
  const data = await r.json();
  const el = document.getElementById('list');
  if (!data.exchanges.length) { el.innerHTML = '<p class="hint">暂无交换。</p>'; return; }
  let html = '';
  for (const ex of data.exchanges) {
    html += '<div class="card">' +
      '<div><b>交换</b> <span class="mono">' + esc(ex.exchange_id) + '</span> ' + badge(ex.phase, ex.phase_text) + '</div>' +
      '<div>A 摘要：<span class="mono">' + esc(short(ex.a_sha256)) + '</span>　B 摘要：<span class="mono">' + esc(short(ex.b_sha256)) + '</span></div>' +
      (IS_A
        ? '<div>本方（A）链接：<a href="' + esc(ex.a_link) + '">' + esc(ex.a_link) + '</a></div>' +
          '<div>B 方链接（分发给 B 组）：<a href="' + esc(ex.b_link) + '">' + esc(ex.b_link) + '</a></div>'
        : '<div>参与链接：<a href="' + esc(ex.b_link) + '">' + esc(ex.b_link) + '</a></div>') +
      '</div>';
  }
  el.innerHTML = html;
}
if (IS_A) {
  document.getElementById('createBtn').onclick = async function () {
    const r = await fetch('/api/exchanges', { method: 'POST' });
    const d = await r.json();
    document.getElementById('created').innerHTML =
      '<div class="card"><b>已创建交换 ' + esc(d.exchange_id) + '</b><br>' +
      'A 方链接：<a href="' + esc(d.a_link) + '">' + esc(d.a_link) + '</a><br>' +
      'B 方链接（分发给 B 组）：<a href="' + esc(d.b_link) + '">' + esc(d.b_link) + '</a></div>';
    load();
  };
}
load();
setInterval(load, 3000);
"""

EXCHANGE_JS = """
const EX_ID = __EX_ID__;
const ROLE = __ROLE__;
const token = new URLSearchParams(location.search).get('token') || '';

function esc(s) { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; }
function badge(phase, text) {
  const cls = phase === 'complete' ? 'complete' : (phase === 'pending_sync' ? 'pending' : 'waiting');
  return '<span class="badge ' + cls + '">' + esc(text) + '</span>';
}

async function refresh() {
  const r = await fetch('/api/exchanges/' + EX_ID + '/state?token=' + encodeURIComponent(token));
  if (!r.ok) {
    document.getElementById('top').innerHTML =
      '<p class="hint">链接无效或交换不存在（若刚创建，对端可能尚未同步，稍候自动刷新）。</p>';
    return;
  }
  render(await r.json());
}

function render(s) {
  let html = '<p>状态：' + badge(s.phase, s.phase_text) + '</p>';
  if (s.resolution_created_at) {
    html += '<p>决议时间（双方一致）：<span class="mono">' + esc(s.resolution_created_at) + '</span></p>';
  }
  if (s.drill_enabled) {
    html += '<p class="hint">演练开关已开启：B 首次持久化决议后将返回 503（模拟回执丢失）。</p>';
  }
  if (ROLE === 'A' && !s.peer_registered) {
    html += '<p class="hint">等待 B 节点上线注册…</p>';
  }
  document.getElementById('top').innerHTML = html;

  const meCard = document.getElementById('meCard');
  if (s.me.submitted) {
    meCard.innerHTML = '<h2>本方文件（已冻结）</h2>' +
      '<p>SHA-256：<span class="mono">' + esc(s.me.sha256) + '</span></p>' +
      '<p>文件名：' + esc(s.me.filename || '') + '　提交时间：' + esc(s.me.submitted_at || '') + '</p>' +
      '<p><a href="/api/exchanges/' + EX_ID + '/download?which=own&token=' + encodeURIComponent(token) + '">下载本方文件</a></p>' +
      '<p class="hint">内容与摘要已不可修改；重复提交相同内容将返回同一回执，提交不同内容将被拒绝（409）。</p>';
  } else {
    meCard.innerHTML = '<h2>提交本方文件</h2>' +
      '<p class="hint">选择 1～65536 字节（1–64 KiB）的文件。提交后内容与 SHA-256 摘要不可修改。</p>' +
      '<input type="file" id="fileInput"> <button id="submitBtn">提交并冻结</button>' +
      '<p id="submitMsg" class="hint"></p>';
    document.getElementById('submitBtn').onclick = submitFile;
  }

  let ph = '<h2>对方文件</h2>';
  if (s.peer.sha256) {
    ph += '<p>SHA-256：<span class="mono">' + esc(s.peer.sha256) + '</span></p>';
    if (s.peer.downloadable) {
      ph += '<p><a href="/api/exchanges/' + EX_ID + '/download?which=peer&token=' + encodeURIComponent(token) + '">下载对方文件' +
            (s.peer.filename ? '（' + esc(s.peer.filename) + '）' : '') + '</a></p>';
    } else {
      ph += '<p class="hint">待同步：本节点尚未收到并核对对方文件，下载未开放。</p>';
    }
  } else {
    ph += '<p class="hint">决议达成前不可下载对方文件。</p>';
  }
  document.getElementById('peerCard').innerHTML = ph;
}

async function submitFile() {
  const input = document.getElementById('fileInput');
  const msg = document.getElementById('submitMsg');
  if (!input.files.length) { msg.textContent = '请先选择文件'; return; }
  const f = input.files[0];
  if (f.size < 1 || f.size > 65536) { msg.textContent = '文件须为 1～65536 字节'; return; }
  const fd = new FormData();
  fd.append('file', f);
  const r = await fetch('/api/exchanges/' + EX_ID + '/submit?token=' + encodeURIComponent(token), { method: 'POST', body: fd });
  if (r.ok) {
    const d = await r.json();
    msg.textContent = d.repeated ? '已提交过相同内容，返回原回执。' : '已提交并冻结。';
  } else if (r.status === 409) {
    msg.textContent = '冲突：已冻结不同内容（409）。';
  } else {
    msg.textContent = '提交失败：HTTP ' + r.status;
  }
  refresh();
}

document.getElementById('retryBtn').onclick = async function () {
  const msg = document.getElementById('retryMsg');
  msg.textContent = '重试中…';
  try {
    await fetch('/api/exchanges/' + EX_ID + '/retry?token=' + encodeURIComponent(token), { method: 'POST' });
    msg.textContent = '';
  } catch (e) { msg.textContent = '重试失败'; }
  refresh();
};

refresh();
setInterval(refresh, 2000);
"""


def home_page() -> str:
    is_a = config.ROLE == "A"
    create = '<button id="createBtn">创建新交换</button>' if is_a else ""
    note = "" if is_a else '<p class="hint">本节点（B）不创建交换；请使用 A 方分发的参与链接。</p>'
    js = HOME_JS.replace("__IS_A__", "true" if is_a else "false")
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>节点 {config.ROLE} · 冻结文件交换</title><style>{STYLE}</style></head><body>"
        f"<h1>节点 {config.ROLE} · 冻结文件交换</h1>"
        f"{create}{note}<div id='created'></div>"
        "<h2>交换列表</h2><div id='list'>加载中…</div>"
        f"<script>{js}</script></body></html>"
    )


def exchange_page(ex_id: str) -> str:
    js = (
        EXCHANGE_JS.replace("__EX_ID__", json.dumps(ex_id))
        .replace("__ROLE__", json.dumps(config.ROLE))
    )
    return (
        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
        f"<title>交换 {ex_id} · 节点 {config.ROLE}</title><style>{STYLE}</style></head><body>"
        f"<h1>节点 {config.ROLE} · 交换 <span class='mono'>{ex_id}</span></h1>"
        "<div id='top'>加载中…</div>"
        "<div class='card' id='meCard'></div>"
        "<div class='card' id='peerCard'></div>"
        "<div class='card'><button id='retryBtn'>立即重试同步</button> "
        f"<span id='retryMsg' class='hint'></span>"
        f"<p class='hint'>后台每 {config.WORKER_INTERVAL:g}s 自动重试；节点重启后也会自动恢复未确认的决议。</p></div>"
        "<p><a href='/'>← 返回首页</a></p>"
        f"<script>{js}</script></body></html>"
    )
