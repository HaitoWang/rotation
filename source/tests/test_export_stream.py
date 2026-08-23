import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock


from webui import db
from webui.app import ExportRegisteredReq, api_export_registered_download


async def _read_stream(response) -> bytes:
    output = bytearray()
    async for chunk in response.body_iterator:
        output.extend(chunk.encode("utf-8") if isinstance(chunk, str) else chunk)
    return bytes(output)


class ExportStreamTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db_path = Path(self.temp_dir.name) / "test.db"
        self.path_patch = mock.patch.object(db, "DB_PATH", self.db_path)
        self.path_patch.start()
        db._db_local.connection = None
        db._db_local.path = ""
        db.init_db()
        con = db._new_connection()
        con.executemany(
            "INSERT INTO registered "
            "(email, password, access_token, totp_secret, extra_json, created_at, deleted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, NULL)",
            [
                ("first@example.com", "pw-1", "at-1", "tfa-1", '{"large":"ignored"}', 3),
                ("second@example.com", "pw-2", "at-2", "tfa-2", '{"large":"ignored"}', 2),
                ("third@example.com", "pw-3", "at-3", "tfa-3", '{"large":"ignored"}', 1),
            ],
        )
        con.commit()
        con.close()

    def tearDown(self):
        con = getattr(db._db_local, "connection", None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
        db._db_local.connection = None
        db._db_local.path = ""
        self.path_patch.stop()
        self.temp_dir.cleanup()

    def test_streams_text_rows_in_table_order(self):
        response = api_export_registered_download(
            ExportRegisteredReq(format="email_pw_2fa", all=True)
        )

        payload = asyncio.run(_read_stream(response)).decode("utf-8")

        self.assertEqual(
            payload.splitlines(),
            [
                "first@example.com----pw-1----tfa-1",
                "second@example.com----pw-2----tfa-2",
                "third@example.com----pw-3----tfa-3",
            ],
        )
        self.assertEqual(response.headers["x-export-count"], "3")
        self.assertEqual(response.headers["x-accel-buffering"], "no")

    def test_selected_stream_uses_only_existing_active_rows(self):
        con = db._new_connection()
        con.execute(
            "UPDATE registered SET deleted_at=99 WHERE email='third@example.com'"
        )
        con.commit()
        con.close()

        response = api_export_registered_download(
            ExportRegisteredReq(
                format="at",
                emails=["third@example.com", "second@example.com", "missing@example.com"],
            )
        )
        payload = asyncio.run(_read_stream(response)).decode("utf-8")

        self.assertEqual(payload, "at-2")
        self.assertEqual(response.headers["x-export-count"], "1")

    def test_limit_and_soft_delete_apply_only_after_complete_stream(self):
        response = api_export_registered_download(
            ExportRegisteredReq(format="email_pw_2fa", all=True, limit=2, soft_delete=True)
        )

        # The rows remain available until the response iterator reaches EOF.
        con = db._new_connection()
        self.assertEqual(con.execute(
            "SELECT count(*) FROM registered WHERE deleted_at IS NULL"
        ).fetchone()[0], 3)
        con.close()

        payload = asyncio.run(_read_stream(response)).decode("utf-8")
        self.assertEqual(payload.splitlines(), [
            "first@example.com----pw-1----tfa-1",
            "second@example.com----pw-2----tfa-2",
        ])
        self.assertEqual(response.headers["x-export-count"], "2")

        con = db._new_connection()
        self.assertEqual(con.execute(
            "SELECT count(*) FROM registered WHERE deleted_at IS NULL"
        ).fetchone()[0], 1)
        self.assertIsNone(con.execute(
            "SELECT deleted_at FROM registered WHERE email='third@example.com'"
        ).fetchone()[0])
        con.close()

    def test_mailbox_export_join_uses_indexable_normalized_email(self):
        con = db._new_connection()
        con.execute(
            "INSERT INTO outlook_accounts "
            "(email, password, client_id, refresh_token, kind, status) "
            "VALUES (?, ?, ?, ?, 'outlook', 'done')",
            ("first@example.com", "mail-pw", "client-1", "mail-rt-1"),
        )
        con.commit()
        con.close()

        queries = []
        real_new_connection = db._new_connection

        def traced_connection():
            connection = real_new_connection()
            connection.set_trace_callback(queries.append)
            return connection

        with mock.patch.object(db, "_new_connection", side_effect=traced_connection):
            rows = list(db.iter_registered_export_rows("mailbox_credentials", limit=1))

        self.assertEqual(rows[0]["mail_client_id"], "client-1")
        select = next(query for query in queries if query.startswith("SELECT r.*"))
        self.assertIn("ON o.email=r.email", select)
        self.assertNotIn("lower(o.email)", select)

    def test_frontend_keeps_export_controls_and_shared_payload(self):
        frontend = Path(__file__).resolve().parents[1] / "webui" / "frontend" / "src"
        page = (frontend / "views" / "Registered.vue").read_text(encoding="utf-8")
        form = (frontend / "stores" / "form.js").read_text(encoding="utf-8")
        for marker in ("exportLimit", "exportSoftDelete", "soft_delete", "limit", "filter"):
            self.assertIn(marker, page)
        self.assertIn("exportLimit: 0", form)
        self.assertIn("exportSoftDelete: false", form)


if __name__ == "__main__":
    unittest.main()
