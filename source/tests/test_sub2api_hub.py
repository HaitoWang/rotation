import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from webui import db, exporter
from webui.team_rotation import TeamRotationController


def _jwt(payload: dict) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    return f"{header}.{body}.signature"


class Sub2ApiHubPayloadTests(unittest.TestCase):
    def setUp(self):
        self.access_token = _jwt({
            "exp": 1787632800,
            "sub": "user-from-sub",
            "client_id": "client-from-token",
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "personal-account",
                "chatgpt_user_id": "chatgpt-user",
                "chatgpt_plan_type": "free",
                "organization_id": "org-1",
            },
        })

    def test_team_workspace_model_and_concurrency_are_written_to_account(self):
        account = exporter.build_sub2api_payload(
            {
                "email": "user@example.com",
                "access_token": self.access_token,
                "refresh_token": "refresh-token",
                "id_token": "id-token",
                "account_id": "team-workspace",
                "plan_type": "team",
                "notes": "Team 轮转",
            },
            [12],
            default_model="gpt-5.6-sol",
            supported_models=["gpt-image-2", "gpt-5.4", "gpt-5.4", "custom-model"],
            concurrency=3,
        )

        credentials = account["credentials"]
        self.assertEqual(credentials["chatgpt_account_id"], "team-workspace")
        self.assertEqual(credentials["chatgpt_user_id"], "chatgpt-user")
        self.assertEqual(credentials["plan_type"], "team")
        self.assertEqual(credentials["model_mapping"], {
            "gpt-image-2": "gpt-image-2",
            "gpt-5.4": "gpt-5.4",
            "custom-model": "custom-model",
            "gpt-5.6-sol": "gpt-5.6-sol",
            "codex-main": "gpt-5.6-sol",
        })
        self.assertEqual(credentials["email"], "user@example.com")
        self.assertTrue(credentials["expires_at"].endswith("Z"))
        self.assertEqual(account["group_ids"], [12])
        self.assertEqual(account["concurrency"], 3)
        self.assertEqual(account["priority"], 0)
        self.assertEqual(account["rate_multiplier"], 1)
        self.assertIsNone(account["proxy_id"])

    def test_export_uses_batch_endpoint_and_stable_idempotency_header(self):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"accounts": [{"id": "hub-account-1"}]}
        response.text = ""
        cffi = mock.Mock()
        cffi.post.return_value = response
        cfg = {
            "sub2api_url": "https://hub.example.com/",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
            "sub2api_default_model": "gpt-5.4",
            "sub2api_models": ["gpt-image-2", "gpt-5.4", "gpt-5.6-terra"],
            "sub2api_concurrency": "3",
            "sub2api_timeout": "30",
        }
        cred = {
            "email": "user@example.com",
            "access_token": self.access_token,
            "refresh_token": "refresh-token",
            "account_id": "team-workspace",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            first = exporter.export_to_sub2api(cred, cfg)
            second = exporter.export_to_sub2api(cred, cfg)

        self.assertTrue(first["ok"])
        self.assertEqual(first["account_id"], "hub-account-1")
        self.assertEqual(first["idempotency_key"], second["idempotency_key"])
        url, = cffi.post.call_args_list[0].args
        kwargs = cffi.post.call_args_list[0].kwargs
        self.assertEqual(url, "https://hub.example.com/api/v1/admin/accounts/batch")
        self.assertEqual(kwargs["headers"]["x-api-key"], "admin-key")
        self.assertEqual(kwargs["headers"]["Idempotency-Key"], first["idempotency_key"])
        self.assertEqual(len(kwargs["json"]["accounts"]), 1)
        self.assertEqual(
            kwargs["json"]["accounts"][0]["credentials"]["model_mapping"],
            {
                "gpt-image-2": "gpt-image-2",
                "gpt-5.4": "gpt-5.4",
                "gpt-5.6-terra": "gpt-5.6-terra",
                "codex-main": "gpt-5.4",
            },
        )

    def test_default_supported_models_match_hub_configuration(self):
        self.assertEqual(exporter.parse_sub2api_models(None), [
            "gpt-image-2",
            "gpt-5.3-codex",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.5",
            "gpt-5.6",
            "gpt-5.6-sol",
            "gpt-5.6-luna",
            "gpt-5.6-terra",
            "codex-auto-review",
        ])

    def test_group_query_keeps_only_active_openai_groups(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "message": "success",
            "data": [
                {"id": 12, "name": "openai-main", "platform": "openai", "status": "active"},
                {"id": 13, "name": "disabled", "platform": "openai", "status": "disabled"},
                {"id": 14, "name": "anthropic", "platform": "anthropic", "status": "active"},
            ],
        }
        response.text = ""
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_groups({
                "sub2api_url": "https://hub.example.com/",
                "sub2api_api_key": "admin-key",
                "sub2api_timeout": "30",
            })

        self.assertEqual(result["groups"], [{
            "id": 12,
            "name": "openai-main",
            "platform": "openai",
            "status": "active",
        }])
        url, = cffi.get.call_args.args
        self.assertEqual(url, "https://hub.example.com/api/v1/admin/groups/all?platform=openai")
        self.assertEqual(cffi.get.call_args.kwargs["headers"]["x-api-key"], "admin-key")


class TeamRotationHubStateTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path_patch = mock.patch.object(
            db, "DB_PATH", Path(self.temp_dir.name) / "webui.db"
        )
        self.path_patch.start()
        db._db_local.connection = None
        db._db_local.path = ""
        db.init_db()
        db.save_registered({
            "email": "child@example.com",
            "access_token": "web-access-token",
            "refresh_token": "refresh-token",
        })
        self.mother = db.create_team_mother({
            "name": "Team A",
            "workspace_id": "team-workspace",
            "access_token": "mother-access-token",
            "enabled": True,
        })
        self.assignment = db.claim_team_rotation_candidate(self.mother["id"])
        db.update_team_rotation_member(
            self.assignment["id"], status="active", member_id="member-1"
        )
        db.save_export_config({
            "sub2api_enabled": "1",
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
            "sub2api_default_model": "gpt-5.4",
            "sub2api_models": ["gpt-image-2", "gpt-5.4", "custom-model"],
            "sub2api_concurrency": "3",
        })

        public_cfg = db.get_export_config()
        internal_cfg = db.get_export_internal_config()["sub2api"]
        expected_models = ["gpt-image-2", "gpt-5.4", "custom-model"]
        self.assertEqual(public_cfg["sub2api_models"], expected_models)
        self.assertEqual(internal_cfg["sub2api_models"], expected_models)

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

    def test_successful_hub_push_is_persisted_with_team_workspace(self):
        controller = TeamRotationController(service_factory=mock.Mock())
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        with mock.patch.object(
            exporter,
            "run_exports",
            return_value={"sub2api": {"ok": True, "account_id": "hub-1"}},
        ) as run_exports:
            controller._push_assignment_to_hub(self.mother, assignment)

        pushed_cred = run_exports.call_args.args[0]
        self.assertEqual(pushed_cred["account_id"], "team-workspace")
        self.assertEqual(pushed_cred["plan_type"], "team")
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_status"], "success")
        self.assertIsNotNone(updated["hub_pushed_at"])
        self.assertEqual(updated["hub_error"], "")


if __name__ == "__main__":
    unittest.main()
