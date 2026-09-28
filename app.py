"""Local source-attributed intelligence board; Python standard library only."""
from __future__ import annotations

import csv
import email.utils
import hashlib
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
from datetime import datetime, timedelta, timezone
from contextlib import closing
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
CREATE TABLE IF NOT EXISTS watch_rules (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL,
 include_terms TEXT NOT NULL, exclude_terms TEXT NOT NULL DEFAULT '',
 platform TEXT NOT NULL DEFAULT '', enabled INTEGER NOT NULL DEFAULT 1
);
"""

def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
    for name, definition in {
        "workflow": "TEXT NOT NULL DEFAULT 'inbox'",
        "note": "TEXT NOT NULL DEFAULT ''",
        "reviewed_at": "TEXT NOT NULL DEFAULT ''",
    }.items():
        if name not in columns:
            db.execute(f"ALTER TABLE items ADD COLUMN {name} {definition}")
    db.commit()
    return db

def clean(value, limit=2000):
    return str(value or "").strip()[:limit]

def normalized_date(value):
    raw = clean(value, 100)
    if not raw:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            return datetime.now(timezone.utc).isoformat(timespec="seconds")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds")

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
    item["published_at"] = normalized_date(item["published_at"])
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
    workflow = clean(params.get("workflow", [""])[0], 20)
    if workflow in {"inbox", "saved", "archived"}:
        clauses.append("workflow = ?")
        values.append(workflow)
    if params.get("sample", [""])[0] == "0":
        clauses.append("sample = 0")
    query = clean(params.get("q", [""])[0], 100)
    if query:
        clauses.append("(title LIKE ? OR summary LIKE ? OR author LIKE ? OR tags LIKE ?)")
        values.extend([f"%{query}%"] * 4)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [dict(row) for row in db.execute("SELECT * FROM items" + where + " ORDER BY published_at DESC, id DESC LIMIT 200", values)]

def summary(db):
    counts = {r["type"]: r["n"] for r in db.execute("SELECT type, count(*) n FROM items WHERE sample=0 GROUP BY type")}
    workflow = {r["workflow"]: r["n"] for r in db.execute("SELECT workflow, count(*) n FROM items WHERE sample=0 GROUP BY workflow")}
    authors = [dict(r) for r in db.execute("SELECT author, platform, count(*) n, sum(type='intel') intel_count, sum(type='deal') deal_count FROM items WHERE author != '' AND sample=0 GROUP BY author, platform ORDER BY n DESC LIMIT 30")]
    platforms = [dict(r) for r in db.execute("SELECT platform, count(*) n FROM items GROUP BY platform ORDER BY n DESC LIMIT 30")]
    return {"counts": counts, "workflow": workflow, "authors": authors, "platforms": platforms}

def rule_matches(item, rule):
    text = " ".join(clean(item.get(k)).casefold() for k in ("title", "summary", "tags"))
    include = [part.casefold() for part in rule["include_terms"].split(",") if part.strip()]
    exclude = [part.casefold() for part in rule["exclude_terms"].split(",") if part.strip()]
    return bool(include) and any(term.strip() in text for term in include) and not any(term.strip() in text for term in exclude) and (not rule["platform"] or rule["platform"] == item["platform"])

def rule_hits(db, rule_id):
    rule = db.execute("SELECT * FROM watch_rules WHERE id=?", (rule_id,)).fetchone()
    if rule is None:
        raise ValueError("规则不存在")
    hits = []
    for row in db.execute("SELECT * FROM items WHERE sample=0 AND workflow!='archived' ORDER BY published_at DESC, id DESC"):
        item = dict(row)
        if rule_matches(item, rule):
            hits.append(item)
            if len(hits) == 200:
                break
    return hits

def digest(db, days=7):
    days = max(1, min(int(days), 90))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    rows = db.execute("SELECT * FROM items WHERE sample=0 AND workflow='saved' AND verified=1 AND published_at>=? ORDER BY published_at DESC LIMIT 50", (cutoff,)).fetchall()
    lines = [f"# Intel Board · 近 {days} 天已核实简报", "", "只包含人工收藏并标记为已核实的记录。请在分享前再次核对来源。", ""]
    for row in rows:
        title = row["title"].replace("[", "\\[").replace("]", "\\]")
        lines.extend([f"## {title}", "", f"- 类型：{row['type']} · 来源：{row['platform']} · 作者：{row['author']}", f"- 发布时间：{row['published_at']}", f"- 原文：{row['url'] or '未提供'}", f"- 摘要：{row['summary']}", f"- 复核备注：{row['note'] or '无'}", ""])
    if not rows:
        lines.extend(["当前没有符合条件的内容。先收藏记录并核实，再导出简报。", ""])
    return "\n".join(lines)

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
    proxy_range = ipaddress.ip_network("198.18.0.0/15")
    if not addresses or any(not (ipaddress.ip_address(result[4][0]).is_global or ipaddress.ip_address(result[4][0]) in proxy_range) for result in addresses):
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

PAGE = (ROOT / 'index.html').read_text(encoding='utf-8')

class Handler(BaseHTTPRequestHandler):
    def respond(self, code, data, content_type="application/json; charset=utf-8"):
        payload = data if isinstance(data, bytes) else data.encode() if isinstance(data, str) else json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)
        with closing(connect()) as db:
            if parsed.path == "/": return self.respond(200, PAGE, "text/html; charset=utf-8")
            if parsed.path == "/api/items": return self.respond(200, search(db, query))
            if parsed.path == "/api/summary": return self.respond(200, summary(db))
            if parsed.path == "/api/sources": return self.respond(200, [dict(r) for r in db.execute("SELECT * FROM sources ORDER BY id DESC")])
            if parsed.path == "/api/rules": return self.respond(200, [dict(r) for r in db.execute("SELECT * FROM watch_rules ORDER BY id DESC")])
            rule_match = re.fullmatch(r"/api/rules/(\d+)/items", parsed.path)
            if rule_match:
                try: return self.respond(200, rule_hits(db, int(rule_match.group(1))))
                except ValueError as exc: return self.respond(404, {"error": str(exc)})
            if parsed.path == "/api/export.csv":
                output = io.StringIO()
                fields = [r[1] for r in db.execute("PRAGMA table_info(items)") if r[1] != "fingerprint"]
                writer = csv.DictWriter(output, fieldnames=fields)
                writer.writeheader()
                writer.writerows({field: row[field] for field in fields} for row in db.execute("SELECT * FROM items ORDER BY id"))
                return self.respond(200, "\ufeff" + output.getvalue(), "text/csv; charset=utf-8")
            if parsed.path == "/api/digest.md":
                try: return self.respond(200, digest(db, query.get("days", ["7"])[0]), "text/markdown; charset=utf-8")
                except ValueError: return self.respond(400, {"error": "days 必须是数字"})
        self.respond(404, {"error": "Not found"})

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 5_000_000: raise ValueError("上传文件不得超过 5 MB")
            body = self.rfile.read(length)
            with closing(connect()) as db:
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
                triage_match = re.fullmatch(r"/api/items/(\d+)/triage", parsed.path)
                if triage_match:
                    data = json.loads(body)
                    workflow = data.get("workflow")
                    if workflow not in {"inbox", "saved", "archived"}: raise ValueError("workflow 只能为 inbox/saved/archived")
                    note = clean(data.get("note"), 1000)
                    cursor = db.execute("UPDATE items SET workflow=?, note=?, reviewed_at=? WHERE id=?", (workflow, note, datetime.now(timezone.utc).isoformat(timespec="seconds"), int(triage_match.group(1))))
                    db.commit()
                    if not cursor.rowcount: return self.respond(404, {"error": "记录不存在"})
                    return self.respond(200, {"ok": True})
                if parsed.path == "/api/rules":
                    data = json.loads(body)
                    name = clean(data.get("name"), 100)
                    include = clean(data.get("include_terms"), 300)
                    if not name or not any(part.strip() for part in include.split(",")):
                        raise ValueError("请填写规则名称和至少一个关键词")
                    cursor = db.execute("INSERT INTO watch_rules(name,include_terms,exclude_terms,platform) VALUES(?,?,?,?)", (name, include, clean(data.get("exclude_terms"), 300), clean(data.get("platform"), 100)))
                    db.commit()
                    return self.respond(200, {"id": cursor.lastrowid})
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
    import argparse
    parser = argparse.ArgumentParser(description="Local intelligence board")
    parser.add_argument("--demo", action="store_true", help="Load clearly labeled sample records")
    parser.add_argument("--fetch-all", action="store_true", help="Refresh all configured public sources, then exit")
    args = parser.parse_args()
    with closing(connect()) as database:
        if args.demo: seed(database)
        if args.fetch_all:
            for source in database.execute("SELECT * FROM sources ORDER BY id").fetchall():
                try:
                    print(f"{source['name']}: {fetch_source(database, source)}", flush=True)
                except Exception as exc:
                    database.execute("UPDATE sources SET last_error=? WHERE id=?", (clean(exc, 250), source["id"]))
                    database.commit()
                    print(f"{source['name']}: ERROR {exc}", flush=True)
            raise SystemExit(0)
    port = int(os.environ.get("INTEL_PORT", "8770"))
    print(f"Intel Board: http://127.0.0.1:{port}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()
