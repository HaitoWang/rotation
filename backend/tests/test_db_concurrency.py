import gc
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app.webui import db


class DatabaseConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.original_path = db.DB_PATH
        self.temp_dir = tempfile.TemporaryDirectory()
        db.DB_PATH = Path(self.temp_dir.name) / "webui.db"
        db.init_db()

    def tearDown(self):
        try:
            con = getattr(db._db_local, "connection", None)
            if con is not None:
                con.close()
        except Exception:
            pass
        db.DB_PATH = self.original_path
        gc.collect()
        self.temp_dir.cleanup()

    def test_one_hundred_concurrent_database_clients(self):
        def exercise(worker_id: int) -> None:
            run_id = f"stress-{worker_id}"
            settings = db.get_mail_settings()
            self.assertIn("mail_source", settings)
            db.create_run(run_id, f"worker-{worker_id}@example.com", "")
            db.finish_run(run_id, "done")

        with ThreadPoolExecutor(max_workers=100) as executor:
            list(executor.map(exercise, range(100)))

        con = db._conn()
        completed = con.execute(
            "SELECT COUNT(*) FROM runs WHERE status='done'"
        ).fetchone()[0]
        self.assertEqual(completed, 100)


if __name__ == "__main__":
    unittest.main()
