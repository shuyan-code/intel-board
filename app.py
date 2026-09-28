"""Local source-attributed intelligence board; Python standard library only."""
from __future__ import annotations

import csv
import hashlib
import html
import io
import ipaddress
import json
import os
import re
import socket
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("INTEL_DB", str(ROOT / "intel.db")))
TYPES = {"message", "link", "intel", "deal"}
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
 id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE NOT NULL,
 type TEXT NOT NULL, title TEXT NOT NULL, summary TEXT NOT NULL DEFAULT '',
 url TEXT NOT NULL DEFAULT '', platform TEXT NOT NULL DEFAULT '',
 author TEXT NOT NULL DEFAULT '', published_at TEXT NOT NULL DEFAULT '',
 category TEXT NOT NULL DEFAULT '', confidence TEXT NOT NULL DEFAULT 'unverified',
 verified INTEGER NOT NULL DEFAULT 0, tags TEXT NOT NULL DEFAULT '',
 sample INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_items_date ON items(published_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
CREATE TABLE IF NOT EXISTS sources (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, url TEXT UNIQUE NOT NULL,
 kind TEXT NOT NULL, last_checked TEXT NOT NULL DEFAULT '',
 last_error TEXT NOT NULL DEFAULT ''
);
"""

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    return db

def clean(value, limit=2000):
    return str(value or "").strip()[:limit]

def normalize(record):
    item = {key: clean(record.get(key), 4000 if key == "summary" else 500)
            for key in ("type", "title", "summary", "url", "platform", "author", "published_at", "category", "confidence", "tags")}
    item["type"] = item["type"].lower()
    if item["type"] not in TYPES or not item["title"]:
        raise ValueError("type 必须为 message/link/intel/deal，title 不能为空")
    if item["url"] and urllib.parse.urlparse(item["url"]).scheme not in {"http", "https"}:
        raise ValueError("url 只支持 http/https")
    item["verified"] = int(str(record.get("verified", "")).lower() in {"1", "true", "yes", "是"})
    item["sample"] = int(bool(record.get("sample", False)))
    item["confidence"] = item["confidence"] or "unverified"
    identity = item["url"].rstrip("/").lower() if item["url"] else "|".join((item["platform"], item["type"], item["title"])) .lower()
    item["fingerprint"] = hashlib.sha256(identity.encode()).hexdigest()
    item["created_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return item

def insert(db, record):
    item = normalize(record)
    fields = list(item)
    cursor = db.execute(
        f"INSERT OR IGNORE INTO items ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",
        [item[field] for field in fields],
    )
    return cursor.rowcount == 1

def import_records(db, records):
    added = skipped = errors = 0
    for record in records:
        try:
            if insert(db, record): added += 1
            else: skipped += 1
        except (ValueError, AttributeError, TypeError):
            errors += 1
    db.commit()
    return {"added": added, "skipped": skipped, "errors": errors}

def search(db, params):
    clauses, values = [], []
    for field in ("type", "platform", "category"):
        value = clean(params.get(field, [""])[0], 100)
        if value:
            clauses.append(f"{field} = ?")
            values.append(value)
    verified = params.get("verified", [""])[0]
    if verified in {"0", "1"}:
        clauses.append("verified = ?")
        values.append(int(verified))
    query = clean(params.get("q", [""])[0], 100)
    if query:
        clauses.append("(title LIKE ? OR summary LIKE ? OR author LIKE ? OR tags LIKE ?)")
        values.extend([f"%{query}%"] * 4)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [dict(row) for row in db.execute("SELECT * FROM items" + where + " ORDER BY published_at DESC, id DESC LIMIT 200", values)]

def summary(db):
    counts = {r["type"]: r["n"] for r in db.execute("SELECT type, count(*) n FROM items GROUP BY type")}
    authors = [dict(r) for r in db.execute("SELECT author, platform, count(*) n, sum(type='intel') intel_count, sum(type='deal') deal_count FROM items WHERE author != '' GROUP BY author, platform ORDER BY n DESC LIMIT 30")]
    platforms = [dict(r) for r in db.execute("SELECT platform, count(*) n FROM items GROUP BY platform ORDER BY n DESC LIMIT 30")]
    return {"counts": counts, "authors": authors, "platforms": platforms}

def valid_source(url):
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("公开来源需要 HTTPS 地址")
    host = parsed.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith((".local", ".internal")):
        raise ValueError("不允许本地网络地址")
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("来源域名无法解析") from exc
    if not addresses or any(not ipaddress.ip_address(result[4][0]).is_global for result in addresses):
        raise ValueError("不允许内网或保留地址")
    return url

class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        valid_source(newurl)
        return super().redirect_request(request, fp, code, msg, headers, newurl)

OPEN = urllib.request.build_opener(SafeRedirect)

def fetch_source(db, source):
    url = valid_source(source["url"])
    parsed = urllib.parse.urlparse(url)
    github_match = re.fullmatch(r"/([^/]+)/([^/]+)/?", parsed.path)
    if parsed.hostname == "github.com" and github_match:
        owner, repo = github_match.groups()
        api = f"https://api.github.com/repos/{owner}/{repo}/issues?state=all&per_page=50"
        req = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json", "User-Agent": "intel-board/1.0"})
        with OPEN.open(req, timeout=15) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000: raise ValueError("来源响应过大")
        data = json.loads(payload)
        records = [{"type": "intel", "title": row.get("title"), "summary": clean(row.get("body"), 1500),
                    "url": row.get("html_url"), "platform": "GitHub", "author": (row.get("user") or {}).get("login"),
                    "published_at": row.get("created_at"), "category": "PR" if "pull_request" in row else "Issue"}
                   for row in data if isinstance(row, dict)]
    else:
        req = urllib.request.Request(url, headers={"User-Agent": "intel-board/1.0"})
        with OPEN.open(req, timeout=15) as response:
            payload = response.read(2_000_001)
        if len(payload) > 2_000_000: raise ValueError("来源响应过大")
        root = ET.fromstring(payload)
        nodes = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
        records = []
        for node in nodes[:100]:
            def val(*names):
                for name in names:
                    child = node.find(name)
                    if child is not None:
                        return child.get("href", "") or "".join(child.itertext()).strip()
                return ""
            records.append({"type": "link", "title": val("title", "{http://www.w3.org/2005/Atom}title"),
                            "summary": val("description", "{http://www.w3.org/2005/Atom}summary"),
                            "url": val("link", "{http://www.w3.org/2005/Atom}link"),
                            "platform": source["name"], "author": val("author", "{http://www.w3.org/2005/Atom}author"),
                            "published_at": val("pubDate", "{http://www.w3.org/2005/Atom}updated")})
    result = import_records(db, records)
    db.execute("UPDATE sources SET last_checked=?, last_error='' WHERE id=?", (datetime.now(timezone.utc).isoformat(timespec="seconds"), source["id"]))
    db.commit()
    return result

def seed(db):
    if db.execute("SELECT count(*) FROM items").fetchone()[0]: return
    import_records(db, [
        {"type":"intel","title":"示例：公开项目 PR 更新","summary":"演示情报。请点击来源并核对原文后再标记已核实。","platform":"GitHub","author":"demo-maintainer","published_at":"2026-09-28","category":"开发动态","sample":True},
        {"type":"deal","title":"示例：开发者 API 试用额度","summary":"演示优惠。真实额度、资格和有效期需从服务官网确认。","platform":"官网","author":"demo-provider","published_at":"2026-09-28","category":"免费额度","sample":True},
        {"type":"message","title":"示例：社区讨论摘要","summary":"这是导入聊天记录后的展示样式；本项目不包含任何私人聊天记录。","platform":"社区","author":"demo-user","published_at":"2026-09-28","category":"社区观察","sample":True},
    ])

PAGE = """<!doctype html><html lang=zh-CN><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'><title>Intel Board</title>
<style>body{margin:0;background:#170c20;color:#eee;font:16px system-ui,'Microsoft YaHei',sans-serif}header{padding:24px 4%;background:#2b1032;border-bottom:1px solid #543255}h1{margin:0;color:#d9b7ff}header p{color:#bcaec4}main{max-width:1400px;margin:auto;padding:24px}nav{display:flex;gap:10px;flex-wrap:wrap}.tab,button{background:#503056;border:1px solid #805687;color:white;border-radius:8px;padding:10px 16px;cursor:pointer}.tab.active,button:hover{background:#8d4fac}section{display:none}section.active{display:block}.stats,.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px}.card{background:#26182d;border:1px solid #49324f;border-radius:10px;padding:18px;margin:12px 0}.stats .card strong{font-size:30px;color:#47d9e8}input,select{background:#2f2037;color:white;border:1px solid #71577b;border-radius:6px;padding:10px;margin:4px}input[type=text]{min-width:180px}a{color:#69c8ef}small,.muted{color:#aa9bb2}.badge{border-radius:4px;background:#61416b;padding:3px 7px;margin-right:7px}.sample{background:#785422}.ok{background:#216b59}table{width:100%;border-collapse:collapse}td,th{padding:10px;border-bottom:1px solid #60476a;text-align:left}p{line-height:1.55}label{display:inline-block}.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}</style>
<header><h1>📡 Intel Board</h1><p>本地情报与优惠看板 · 每条信息保留来源、时间和核验状态</p></header><main><nav><button class=tab data-tab=overview>总览</button><button class=tab data-tab=intel>情报</button><button class=tab data-tab=deals>优惠</button><button class=tab data-tab=authors>作者画像</button><button class=tab data-tab=sources>数据源与导入</button></nav>
<section id=overview><div id=stats class=stats></div><div class=card><h2>全部记录</h2><div class=row><input id=q type=text placeholder='搜索标题、摘要、作者'><select id=type><option value=''>全部类型</option><option>message</option><option>link</option><option>intel</option><option>deal</option></select><select id=verified><option value=''>全部核验状态</option><option value=1>已核实</option><option value=0>待核实</option></select><button onclick=loadItems()>筛选</button></div><div id=items></div></div></section>
<section id=intel><h2>近期情报</h2><div id=intelItems></div></section><section id=deals><h2>免费额度与优惠</h2><div id=dealItems></div></section><section id=authors><h2>作者贡献</h2><div class=card><table><thead><tr><th>作者</th><th>平台</th><th>记录</th><th>情报</th><th>优惠</th></tr></thead><tbody id=authorRows></tbody></table></div></section>
<section id=sources><h2>导入与采集</h2><div class=card><h3>手工添加</h3><div class=row><select id=newType><option value=intel>情报</option><option value=deal>优惠</option><option value=link>链接</option><option value=message>消息</option></select><input id=newTitle type=text placeholder='标题'><input id=newUrl type=text placeholder='原始来源 HTTPS 链接'><input id=newPlatform type=text placeholder='平台'><input id=newAuthor type=text placeholder='作者'></div><input id=newSummary type=text placeholder='摘要' style='width:75%'><button onclick=addItem()>保存</button></div><div class=card><h3>导入 CSV / JSON</h3><p>支持 type、title、summary、url、platform、author、published_at、category、confidence、verified、tags 字段。</p><input id=file type=file accept='.csv,.json'><button onclick=upload()>导入</button></div><div class=card><h3>添加公开来源</h3><input id=sourceName type=text placeholder='名称'><input id=sourceUrl type=text placeholder='HTTPS RSS 或 GitHub 仓库地址' style='min-width:330px'><button onclick=addSource()>添加</button><div id=sourceList></div></div><p id=status></p></section></main>
<script>const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));async function api(path,opts){const r=await fetch(path,opts);const j=await r.json();if(!r.ok)throw Error(j.error||r.status);return j}function show(tab){document.querySelectorAll('.tab').forEach(x=>x.classList.toggle('active',x.dataset.tab===tab));document.querySelectorAll('section').forEach(x=>x.classList.toggle('active',x.id===tab))}document.querySelectorAll('.tab').forEach(x=>x.onclick=()=>show(x.dataset.tab));function cards(rows){return rows.map(x=>`<article class=card><div><span class=badge>${esc(x.type)}</span>${x.sample?'<span class="badge sample">演示数据</span>':''}<span class="badge ${x.verified?'ok':''}">${x.verified?'已核实':'待核实'}</span><small>${esc(x.published_at)} · ${esc(x.platform)} · ${esc(x.author)}</small></div><h3>${x.url?`<a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.title)}</a>`:esc(x.title)}</h3><p>${esc(x.summary)}</p><small>${esc(x.category)} ${esc(x.tags)}</small><p><button onclick="verifyItem(${x.id},${x.verified?0:1})">${x.verified?'撤销核实':'标记已核实'}</button></p></article>`).join('')||'<p class=muted>暂无记录</p>'}async function loadItems(){let p=new URLSearchParams({q:$('q').value,type:$('type').value,verified:$('verified').value});$('items').innerHTML=cards(await api('/api/items?'+p));}async function refresh(){let s=await api('/api/summary');$('stats').innerHTML=['message','link','intel','deal'].map(t=>`<div class=card><strong>${s.counts[t]||0}</strong><p>${t}</p></div>`).join('');$('authorRows').innerHTML=s.authors.map(a=>`<tr><td>${esc(a.author)}</td><td>${esc(a.platform)}</td><td>${a.n}</td><td>${a.intel_count}</td><td>${a.deal_count}</td></tr>`).join('');$('intelItems').innerHTML=cards(await api('/api/items?type=intel'));$('dealItems').innerHTML=cards(await api('/api/items?type=deal'));await loadItems();let sources=await api('/api/sources');$('sourceList').innerHTML=sources.map(x=>`<div class=card><b>${esc(x.name)}</b> <a href="${esc(x.url)}" target="_blank" rel="noopener noreferrer">${esc(x.url)}</a><p><small>上次采集：${esc(x.last_checked||'未采集')} ${esc(x.last_error)}</small></p><button onclick="runSource(${x.id})">采集</button></div>`).join('')}async function verifyItem(id,value){await api('/api/items/'+id+'/verify',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({verified:value})});await refresh()}async function addItem(){try{let row={type:$('newType').value,title:$('newTitle').value,url:$('newUrl').value,platform:$('newPlatform').value,author:$('newAuthor').value,summary:$('newSummary').value,published_at:new Date().toISOString()};$('status').textContent=JSON.stringify(await api('/api/items',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(row)}));await refresh()}catch(e){$('status').textContent=e.message}}async function upload(){try{let f=$('file').files[0];if(!f)throw Error('请选择文件');let r=await api('/api/import?name='+encodeURIComponent(f.name),{method:'POST',body:await f.text()});$('status').textContent=JSON.stringify(r);await refresh()}catch(e){$('status').textContent=e.message}}async function addSource(){try{await api('/api/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name:$('sourceName').value,url:$('sourceUrl').value})});await refresh()}catch(e){$('status').textContent=e.message}}async function runSource(id){$('status').textContent='正在采集…';try{$('status').textContent=JSON.stringify(await api('/api/sources/'+id+'/fetch',{method:'POST'}));await refresh()}catch(e){$('status').textContent=e.message}}show('overview');refresh().catch(e=>$('status').textContent=e.message);</script></html>"""

class Handler(BaseHTTPRequestHandler):
    def respond(self, code, data, content_type="application/json; charset=utf-8"):
        payload = data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        with connect() as db:
            if parsed.path == "/": return self.respond(200, PAGE, "text/html; charset=utf-8")
            if parsed.path == "/api/items": return self.respond(200, search(db, urllib.parse.parse_qs(parsed.query)))
            if parsed.path == "/api/summary": return self.respond(200, summary(db))
            if parsed.path == "/api/sources": return self.respond(200, [dict(r) for r in db.execute("SELECT * FROM sources ORDER BY id DESC")])
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 5_000_000: raise ValueError("上传文件不得超过 5 MB")
            body = self.rfile.read(length)
            with connect() as db:
                if parsed.path == "/api/items":
                    data = json.loads(body)
                    result = import_records(db, [data])
                    if result["errors"]: raise ValueError("记录字段不合法")
                    return self.respond(200, result)
                item_match = re.fullmatch(r"/api/items/(\d+)/verify", parsed.path)
                if item_match:
                    data = json.loads(body)
                    if data.get("verified") not in (0, 1): raise ValueError("verified 只能为 0 或 1")
                    cursor = db.execute("UPDATE items SET verified=? WHERE id=?", (data["verified"], int(item_match.group(1))))
                    db.commit()
                    if not cursor.rowcount: return self.respond(404, {"error": "记录不存在"})
                    return self.respond(200, {"ok": True})
                if parsed.path == "/api/import":
                    name = urllib.parse.parse_qs(parsed.query).get("name", [""])[0].lower()
                    content = body.decode("utf-8-sig")
                    if name.endswith(".csv"): records = csv.DictReader(io.StringIO(content))
                    elif name.endswith(".json"):
                        records = json.loads(content)
                        if not isinstance(records, list): raise ValueError("JSON 必须为对象数组")
                    else: raise ValueError("仅支持 CSV 或 JSON")
                    return self.respond(200, import_records(db, records))
                if parsed.path == "/api/sources":
                    data = json.loads(body)
                    url = valid_source(clean(data.get("url"), 1000))
                    name = clean(data.get("name"), 100) or urllib.parse.urlparse(url).hostname
                    kind = "github" if urllib.parse.urlparse(url).hostname == "github.com" else "feed"
                    db.execute("INSERT OR IGNORE INTO sources(name,url,kind) VALUES(?,?,?)", (name,url,kind))
                    db.commit()
                    return self.respond(200, {"ok": True})
                match = re.fullmatch(r"/api/sources/(\d+)/fetch", parsed.path)
                if match:
                    source = db.execute("SELECT * FROM sources WHERE id=?", (int(match.group(1)),)).fetchone()
                    if not source: return self.respond(404, {"error": "来源不存在"})
                    try: result = fetch_source(db, source)
                    except Exception as exc:
                        db.execute("UPDATE sources SET last_error=? WHERE id=?", (clean(exc, 250), source["id"]))
                        db.commit()
                        raise
                    return self.respond(200, result)
            self.respond(404, {"error": "Not found"})
        except (ValueError, UnicodeError, json.JSONDecodeError, sqlite3.Error, urllib.error.URLError, ET.ParseError) as exc:
            self.respond(400, {"error": str(exc)})

if __name__ == "__main__":
    with connect() as database: seed(database)
    port = int(os.environ.get("INTEL_PORT", "8770"))
    print(f"Intel Board: http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
