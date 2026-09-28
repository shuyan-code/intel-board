import csv
from contextlib import closing
import io
import json
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import app


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.tmp.name) / "board.db"
        self.db = app.connect()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.db.close()
        self.tmp.cleanup()

    def request(self, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.base + path, body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8-sig")

    def test_capture_triage_verify_digest_and_backup(self):
        status, body = self.request("/api/items", {"type": "intel", "title": "Claude API update", "summary": "New API pricing", "url": "https://example.org/news", "platform": "Example", "author": "maintainer"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["added"], 1)
        rows = json.loads(self.request("/api/items?q=Claude")[1])
        self.assertEqual(len(rows), 1)
        item_id = rows[0]["id"]
        self.request(f"/api/items/{item_id}/triage", {"workflow": "saved", "note": "Checked source"})
        self.request(f"/api/items/{item_id}/verify", {"verified": 1})
        saved = json.loads(self.request("/api/items?workflow=saved&verified=1")[1])
        self.assertEqual(saved[0]["note"], "Checked source")
        digest = self.request("/api/digest.md?days=7")[1]
        self.assertIn("Claude API update", digest)
        self.assertIn("https://example.org/news", digest)
        backup = list(csv.DictReader(io.StringIO(self.request("/api/export.csv")[1])))
        self.assertEqual(backup[0]["workflow"], "saved")
        self.assertEqual(backup[0]["note"], "Checked source")

    def test_watch_rule_filters_exclusions_and_demo(self):
        app.import_records(self.db, [
            {"type": "intel", "title": "Claude API release", "platform": "GitHub"},
            {"type": "intel", "title": "Claude API rumor", "platform": "GitHub"},
            {"type": "intel", "title": "Claude API demo", "platform": "GitHub", "sample": True},
        ])
        rule_id = json.loads(self.request("/api/rules", {"name": "API", "include_terms": "Claude, Gemini", "exclude_terms": "rumor", "platform": "GitHub"})[1])["id"]
        hits = json.loads(self.request(f"/api/rules/{rule_id}/items")[1])
        self.assertEqual([row["title"] for row in hits], ["Claude API release"])

    def test_invalid_input_returns_clear_error(self):
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/api/items/999/triage", {"workflow": "bogus"})
        self.assertEqual(context.exception.code, 400)
        with self.assertRaises(urllib.error.HTTPError) as context:
            self.request("/api/digest.md?days=none")
        self.assertEqual(context.exception.code, 400)

    def test_rule_can_find_older_record_beyond_first_thousand(self):
        app.import_records(self.db, [{"type": "intel", "title": "Key strategic update", "platform": "GitHub", "published_at": "2020-01-01"}])
        app.import_records(self.db, [{"type": "intel", "title": f"Other update {number}", "platform": "GitHub"} for number in range(1100)])
        rule_id = json.loads(self.request("/api/rules", {"name": "Key", "include_terms": "strategic"})[1])["id"]
        hits = json.loads(self.request(f"/api/rules/{rule_id}/items")[1])
        self.assertEqual([row["title"] for row in hits], ["Key strategic update"])


class MigrationTests(unittest.TestCase):
    def test_old_database_gets_workflow_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            app.DB_PATH = Path(folder) / "old.db"
            old = sqlite3.connect(app.DB_PATH)
            old.executescript(app.SCHEMA)
            old.close()
            with closing(app.connect()) as db:
                columns = {row[1] for row in db.execute("PRAGMA table_info(items)")}
                self.assertTrue({"workflow", "note", "reviewed_at"} <= columns)


if __name__ == "__main__":
    unittest.main()
