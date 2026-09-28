import tempfile
import unittest
from pathlib import Path

import app

class BoardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        app.DB_PATH = Path(self.tmp.name) / "test.db"
        self.db = app.connect()

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_import_deduplicates_and_searches(self):
        row = {"type":"intel","title":"Project fix","url":"https://example.org/fix","platform":"GitHub","author":"dev","verified":"yes"}
        self.assertEqual(app.import_records(self.db, [row, row]), {"added":1,"skipped":1,"errors":0})
        self.assertEqual(len(app.search(self.db, {"q":["Project"],"verified":["1"]})), 1)
        self.assertEqual(app.summary(self.db)["authors"][0]["intel_count"], 1)

    def test_rejects_invalid_records_and_local_sources(self):
        self.assertEqual(app.import_records(self.db, [{"type":"other","title":"x"}])["errors"], 1)
        with self.assertRaises(ValueError): app.valid_source("http://127.0.0.1/private")

if __name__ == "__main__": unittest.main()
