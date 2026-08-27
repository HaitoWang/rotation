import base64
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from auth_flow import AuthFlow
from webui import db, exporter, registrar
from webui.team_rotation import (
    TeamApiError,
    TeamChildAuthInvalidError,
    TeamRotationController,
    TeamService,
)


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
                "name": "重授权-user@example.com",
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
        self.assertEqual(account["name"], "重授权-user@example.com")
        self.assertTrue(credentials["expires_at"].endswith("Z"))
        self.assertEqual(account["group_ids"], [12])
        self.assertEqual(account["concurrency"], 3)
        self.assertEqual(account["priority"], 0)
        self.assertEqual(account["rate_multiplier"], 1)
        self.assertIsNone(account["proxy_id"])
        self.assertEqual(account["extra"], {
            "codex_fingerprint_mode": "session",
            "openai_team_rotation_managed": True,
            "openai_team_workspace_id": "team-workspace",
            "openai_team_plan_type": "team",
        })

    def test_fingerprint_off_omits_extra(self):
        account = exporter.build_sub2api_payload(
            {
                "email": "user@example.com",
                "access_token": self.access_token,
            },
            [12],
            fingerprint_mode="off",
        )

        self.assertNotIn("extra", account)

    def test_advanced_team_payload_preserves_prolite_plan_and_seat_type(self):
        account = exporter.build_sub2api_payload(
            {
                "email": "advanced@example.com",
                "access_token": self.access_token,
                "refresh_token": "refresh-token",
                "account_id": "advanced-workspace",
                "plan_type": "self_serve_business_prolite",
                "seat_type": "advanced",
            },
            [12],
        )

        self.assertEqual(
            account["credentials"]["plan_type"],
            "self_serve_business_prolite",
        )
        self.assertEqual(
            account["extra"]["openai_team_plan_type"],
            "self_serve_business_prolite",
        )
        self.assertEqual(account["extra"]["openai_team_seat_type"], "advanced")

    def test_advanced_export_rejects_personal_oauth_before_hub_write(self):
        with mock.patch.object(
            exporter,
            "refresh_codex_token",
            return_value={"access_token": self.access_token},
        ), mock.patch.object(exporter, "export_to_sub2api") as upload:
            result = exporter.run_exports(
                {
                    "email": "advanced@example.com",
                    "refresh_token": "refresh-token",
                    "account_id": "advanced-workspace",
                    "plan_type": "self_serve_business_prolite",
                    "seat_type": "advanced",
                },
                sub2api_cfg={"enabled": True},
            )["sub2api"]

        self.assertFalse(result["ok"])
        self.assertIn("workspace 不匹配", result["error"])
        upload.assert_not_called()

    def test_advanced_export_accepts_matching_prolite_oauth(self):
        advanced_token = _jwt({
            "https://api.openai.com/auth": {
                "chatgpt_account_id": "advanced-workspace",
                "chatgpt_plan_type": "self_serve_business_prolite",
            },
        })
        with mock.patch.object(
            exporter,
            "refresh_codex_token",
            return_value={"access_token": advanced_token},
        ), mock.patch.object(
            exporter,
            "export_to_sub2api",
            return_value={"ok": True, "account_id": "advanced-hub"},
        ) as upload:
            result = exporter.run_exports(
                {
                    "email": "advanced@example.com",
                    "refresh_token": "refresh-token",
                    "account_id": "advanced-workspace",
                    "plan_type": "self_serve_business_prolite",
                    "seat_type": "advanced",
                },
                sub2api_cfg={"enabled": True},
            )["sub2api"]

        self.assertTrue(result["ok"])
        self.assertEqual(upload.call_args.args[0]["access_token"], advanced_token)

    def test_auth_flow_prefers_explicit_target_workspace(self):
        flow = AuthFlow.__new__(AuthFlow)
        flow._env_overrides = {
            "OAUTH_TARGET_WORKSPACE_ID": "advanced-workspace",
        }

        self.assertEqual(flow._extract_workspace_id(), "advanced-workspace")

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
            "sub2api_fingerprint_mode": "full",
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
        self.assertEqual(kwargs["json"]["accounts"][0]["extra"], {
            "codex_fingerprint_mode": "full",
        })
        self.assertEqual(
            kwargs["json"]["accounts"][0]["credentials"]["model_mapping"],
            {
                "gpt-image-2": "gpt-image-2",
                "gpt-5.4": "gpt-5.4",
                "gpt-5.6-terra": "gpt-5.6-terra",
                "codex-main": "gpt-5.4",
            },
        )

    def test_reauthorized_token_uses_a_new_idempotency_key(self):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"accounts": [{"id": "hub-account-1"}]}
        response.text = ""
        cffi = mock.Mock()
        cffi.post.return_value = response
        cfg = {
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
        }
        cred = {
            "email": "user@example.com",
            "access_token": self.access_token,
            "account_id": "team-workspace",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            before = exporter.export_to_sub2api(cred, cfg)
            after = exporter.export_to_sub2api(
                {**cred, "access_token": self.access_token + "-reauthorized"},
                cfg,
            )

        self.assertNotEqual(before["idempotency_key"], after["idempotency_key"])

    def test_existing_hub_account_is_updated_instead_of_created(self):
        update_response = mock.Mock(status_code=200, text="")
        recovery_response = mock.Mock(status_code=200, text="")
        cffi = mock.Mock()
        cffi.put.return_value = update_response
        cffi.post.return_value = recovery_response
        cfg = {
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
        }
        cred = {
            "email": "user@example.com",
            "access_token": self.access_token,
            "account_id": "team-workspace",
            "plan_type": "team",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.export_to_sub2api(
                cred, cfg, existing_account_id="101"
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["updated"])
        self.assertEqual(result["account_id"], "101")
        self.assertEqual(
            cffi.put.call_args.args[0],
            "https://hub.example.com/api/v1/admin/accounts/101",
        )
        self.assertEqual(
            cffi.put.call_args.kwargs["json"]["credentials"]["plan_type"],
            "team",
        )
        self.assertEqual(cffi.post.call_count, 1)
        self.assertFalse(any(
            call.args[0].endswith("/accounts/batch")
            for call in cffi.post.call_args_list
        ))
        self.assertTrue(cffi.post.call_args.args[0].endswith("/recover-state"))

    def test_managed_paused_hub_account_is_reactivated_after_update(self):
        update_response = mock.Mock(status_code=200, text="")
        action_response = mock.Mock(status_code=200, text="")
        cffi = mock.Mock()
        cffi.put.return_value = update_response
        cffi.post.return_value = action_response
        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.export_to_sub2api({
                "email": "user@example.com",
                "access_token": self.access_token,
                "account_id": "team-workspace",
                "plan_type": "team",
            }, {
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
                "sub2api_group_ids": "12",
            }, existing_account_id="101", reactivate_schedulable=True)

        self.assertTrue(result["ok"])
        self.assertEqual(cffi.post.call_count, 2)
        self.assertTrue(cffi.post.call_args_list[1].args[0].endswith("/schedulable"))
        self.assertEqual(
            cffi.post.call_args_list[1].kwargs["json"], {"schedulable": True}
        )

    def test_set_hub_account_schedulable_false(self):
        response = mock.Mock(status_code=200, text="")
        cffi = mock.Mock()
        cffi.post.return_value = response
        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.set_sub2api_account_schedulable({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, "101", False)

        self.assertTrue(result["ok"])
        self.assertFalse(result["schedulable"])
        self.assertTrue(cffi.post.call_args.args[0].endswith("/accounts/101/schedulable"))
        self.assertEqual(cffi.post.call_args.kwargs["json"], {"schedulable": False})

    def test_missing_existing_hub_account_falls_back_to_batch_create(self):
        missing_response = mock.Mock(status_code=404, text="not found")
        create_response = mock.Mock(status_code=201, text="")
        create_response.json.return_value = {
            "data": {"results": [{"success": True, "id": 202}]}
        }
        cffi = mock.Mock()
        cffi.put.return_value = missing_response
        cffi.post.return_value = create_response
        cfg = {
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.export_to_sub2api({
                "email": "user@example.com",
                "access_token": self.access_token,
                "account_id": "team-workspace",
                "plan_type": "team",
            }, cfg, existing_account_id="101")

        self.assertTrue(result["ok"])
        self.assertEqual(result["account_id"], "202")
        self.assertTrue(cffi.post.call_args.args[0].endswith("/accounts/batch"))

    def test_fingerprint_mode_uses_a_new_idempotency_key(self):
        response = mock.Mock(status_code=201)
        response.json.return_value = {"accounts": [{"id": "hub-account-1"}]}
        response.text = ""
        cffi = mock.Mock()
        cffi.post.return_value = response
        base_cfg = {
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
            "sub2api_group_ids": "12",
        }
        cred = {
            "email": "user@example.com",
            "access_token": self.access_token,
            "account_id": "team-workspace",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            session_result = exporter.export_to_sub2api(
                cred, {**base_cfg, "sub2api_fingerprint_mode": "session"}
            )
            full_result = exporter.export_to_sub2api(
                cred, {**base_cfg, "sub2api_fingerprint_mode": "full"}
            )

        self.assertNotEqual(
            session_result["idempotency_key"], full_result["idempotency_key"]
        )

    def test_run_exports_persists_rotated_refresh_token_before_upload(self):
        token_update = mock.Mock()
        cfg = {"enabled": True}
        with mock.patch.object(
            exporter,
            "refresh_codex_token",
            return_value={
                "access_token": self.access_token,
                "refresh_token": "rotated-refresh-token",
                "id_token": "rotated-id-token",
            },
        ), mock.patch.object(
            exporter,
            "export_to_sub2api",
            return_value={"ok": True},
        ):
            result = exporter.run_exports(
                {
                    "email": "user@example.com",
                    "refresh_token": "old-refresh-token",
                },
                sub2api_cfg=cfg,
                token_update_fn=token_update,
            )

        self.assertTrue(result["sub2api"]["ok"])
        token_update.assert_called_once_with({
            "email": "user@example.com",
            "refresh_token": "rotated-refresh-token",
            "id_token": "rotated-id-token",
        })

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

    def test_account_status_classifies_rate_limit_from_hub(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "id": 101,
                "status": "active",
                "schedulable": True,
                "rate_limit_reset_at": time.time() + 3600,
            },
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, 101)

        self.assertEqual(result["classification"], "short_rate_limited")
        self.assertIn("/api/v1/admin/accounts/101", cffi.get.call_args.args[0])

    def test_account_status_distinguishes_5h_and_7d_windows(self):
        response = mock.Mock(status_code=200)
        response.json.side_effect = [
            {
                "code": 0,
                "data": {
                    "id": 101,
                    "status": "active",
                    "schedulable": False,
                    "extra": {
                        "codex_5h_used_percent": 100,
                        "codex_5h_reset_at": "2099-01-01T01:00:00Z",
                        "codex_7d_used_percent": 12,
                    },
                },
            },
            {
                "code": 0,
                "data": {
                    "id": 102,
                    "status": "active",
                    "schedulable": False,
                    "extra": {
                        "codex_5h_used_percent": 100,
                        "codex_7d_used_percent": 100,
                    },
                },
            },
        ]
        cffi = mock.Mock()
        cffi.get.return_value = response
        cfg = {
            "sub2api_url": "https://hub.example.com",
            "sub2api_api_key": "admin-key",
        }

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            five_hour = exporter.get_sub2api_account_status(cfg, 101)
            seven_day = exporter.get_sub2api_account_status(cfg, 102)

        self.assertEqual(five_hour["classification"], "short_rate_limited")
        self.assertEqual(seven_day["classification"], "weekly_exhausted")

    def test_account_status_classifies_error_for_reauthorization(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "id": 101,
                "status": "error",
                "error_message": "access token expired",
            },
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, "101")

        self.assertEqual(result["classification"], "auth_required")
        self.assertEqual(result["error"], "access token expired")

    def test_admin_api_auth_failure_is_not_child_reauthorization(self):
        response = mock.Mock(status_code=401)
        response.json.return_value = {"message": "invalid admin key"}
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, 101)

        self.assertEqual(result["classification"], "hub_error")

    def test_disabled_hub_account_is_not_treated_as_auth_failure(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {"id": 101, "status": "active", "schedulable": False},
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, 101)

        self.assertEqual(result["classification"], "inactive")

    def test_stopped_scheduling_takes_priority_over_team_mismatch(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "id": 101,
                "status": "active",
                "schedulable": False,
                "credentials": {
                    "plan_type": "free",
                    "chatgpt_account_id": "personal-account",
                },
            },
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, 101, expected_workspace_id="team-workspace")

        self.assertEqual(result["classification"], "inactive")

    def test_account_status_detects_team_workspace_overwritten_to_free(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "id": 101,
                "status": "active",
                "schedulable": True,
                "credentials": {
                    "plan_type": "free",
                    "chatgpt_account_id": "personal-account",
                },
            },
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status({
                "sub2api_url": "https://hub.example.com",
                "sub2api_api_key": "admin-key",
            }, 101, expected_workspace_id="team-workspace")

        self.assertEqual(result["classification"], "team_mismatch")
        self.assertIn("plan_type=free", result["error"])

    def test_account_status_accepts_expected_advanced_plan(self):
        response = mock.Mock(status_code=200)
        response.json.return_value = {
            "code": 0,
            "data": {
                "id": 101,
                "status": "active",
                "schedulable": True,
                "credentials": {
                    "plan_type": "self_serve_business_prolite",
                    "chatgpt_account_id": "advanced-workspace",
                },
            },
        }
        cffi = mock.Mock()
        cffi.get.return_value = response

        with mock.patch.object(exporter, "_import_cffi", return_value=cffi):
            result = exporter.get_sub2api_account_status(
                {
                    "sub2api_url": "https://hub.example.com",
                    "sub2api_api_key": "admin-key",
                },
                101,
                expected_workspace_id="advanced-workspace",
                expected_plan_type="self_serve_business_prolite",
            )

        self.assertEqual(result["classification"], "healthy")


class TeamRotationHubStateTests(unittest.TestCase):
    def setUp(self):
        TeamService._invalidate_codex_token_cache("child@example.com")
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
            "session_token": "session-token",
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
            "sub2api_fingerprint_mode": "device",
        })

        public_cfg = db.get_export_config()
        internal_cfg = db.get_export_internal_config()["sub2api"]
        expected_models = ["gpt-image-2", "gpt-5.4", "custom-model"]
        self.assertEqual(public_cfg["sub2api_models"], expected_models)
        self.assertEqual(internal_cfg["sub2api_models"], expected_models)
        self.assertEqual(public_cfg["sub2api_fingerprint_mode"], "device")
        self.assertEqual(internal_cfg["sub2api_fingerprint_mode"], "device")

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
        self.assertIsNone(run_exports.call_args.kwargs["sub2api_account_id"])
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_status"], "success")
        self.assertIsNotNone(updated["hub_pushed_at"])
        self.assertEqual(updated["hub_error"], "")

    def test_advanced_reauthorization_selects_configured_workspace(self):
        db.update_team_mother(self.mother["id"], {
            "preferred_seat_type": "advanced",
        })
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        controller = TeamRotationController(service_factory=mock.Mock())
        with mock.patch.object(
            registrar,
            "reauthorize_registered_account",
            return_value={"ok": True, "account": db.get_registered("child@example.com")},
        ) as reauthorize:
            result = controller._reauthorize_assignment(
                mother, assignment, repush_hub=False
            )

        self.assertEqual(result, "success")
        self.assertEqual(
            reauthorize.call_args.kwargs["workspace_id"], "team-workspace"
        )

    def test_successful_hub_push_persists_account_id(self):
        controller = TeamRotationController(service_factory=mock.Mock())
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        with mock.patch.object(
            exporter,
            "run_exports",
            return_value={"sub2api": {"ok": True, "account_id": "101"}},
        ):
            controller._push_assignment_to_hub(self.mother, assignment)

        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_account_id"], "101")

    def test_hub_push_reuses_persisted_account_id(self):
        db.update_team_rotation_member(
            self.assignment["id"], hub_account_id="101", hub_status="pending"
        )
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        controller = TeamRotationController(service_factory=mock.Mock())
        with mock.patch.object(
            exporter,
            "run_exports",
            return_value={"sub2api": {"ok": True, "account_id": "101", "updated": True}},
        ) as run_exports:
            controller._push_assignment_to_hub(self.mother, assignment)

        self.assertEqual(run_exports.call_args.kwargs["sub2api_account_id"], "101")
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_account_id"], "101")

    def test_team_mismatch_repushes_same_hub_account_id(self):
        db.update_team_rotation_member(
            self.assignment["id"], hub_account_id="101", hub_status="success"
        )
        db.record_team_mother_check(
            self.mother["id"], entitled=1, in_use=1, remaining=0
        )
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = mock.Mock()
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "team_mismatch",
                "error": "plan_type=free",
            },
        ), mock.patch.object(controller, "_push_assignment_to_hub") as push:
            controller._process_mother(mother, force_team_refresh=False)

        push.assert_called_once()
        pushed_assignment = push.call_args.args[1]
        self.assertEqual(pushed_assignment["hub_account_id"], "101")

    def test_hub_5h_limited_member_is_removed_with_temporary_cooldown(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            hub_status="success", hub_account_id="101",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 1, "in_use": 1, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        service.remove_member.return_value = {"removed": True}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        order = []
        service.remove_member.side_effect = lambda *_args: (
            order.append("remove") or {"removed": True}
        )
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "short_rate_limited",
                "error": "账号 5h 限流",
                "reset_at": time.time() + 3600,
            },
        ), mock.patch.object(
            exporter,
            "set_sub2api_account_schedulable",
            side_effect=lambda *_args: (
                order.append("pause")
                or {"ok": True, "account_id": "101", "schedulable": False}
            ),
        ) as pause_hub:
            controller._process_mother(self.mother)

        service.remove_member.assert_called_once_with(self.mother, "member-1")
        service.check_quota.assert_not_called()
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "cooldown")
        pause_hub.assert_called_once()
        self.assertEqual(updated["hub_status"], "paused")
        self.assertEqual(order, ["pause", "remove"])
        history = db.list_team_rotation_member_history("child@example.com")
        self.assertFalse(history[0]["permanently_excluded"])
        self.assertGreater(history[0]["cooldown_until"], time.time())

    def test_advanced_seat_ignores_short_window_rate_limit(self):
        db.update_team_mother(self.mother["id"], {
            "preferred_seat_type": "advanced",
            "join_mode": "auto_accept_request",
        })
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            seat_type="advanced", hub_status="success", hub_account_id="101",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {
                "entitled": 1,
                "in_use": 1,
                "remaining_default": 0,
                "pools": {"advanced": {"paid": 1, "assigned": 1, "available": 0}},
            },
            "members": [{
                "id": "member-1",
                "email": "child@example.com",
                "seat_type": "prolite",
            }],
        }
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "short_rate_limited",
                "error": "错误标记成 5h 限流",
            },
        ):
            controller._process_mother(mother)

        service.remove_member.assert_not_called()
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "active")
        self.assertIn("5h", updated["error"])

    def test_advanced_weekly_exhaustion_cools_pair_until_reset(self):
        db.update_team_mother(self.mother["id"], {
            "preferred_seat_type": "advanced",
            "join_mode": "auto_accept_request",
        })
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            seat_type="advanced", hub_status="success", hub_account_id="101",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {
                "entitled": 1,
                "in_use": 1,
                "remaining_default": 0,
                "pools": {"advanced": {"paid": 1, "assigned": 1, "available": 0}},
            },
            "members": [{
                "id": "member-1",
                "email": "child@example.com",
                "seat_type": "prolite",
            }],
        }
        service.remove_member.return_value = {"removed": True}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        reset_at = time.time() + 7 * 86400
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "weekly_exhausted",
                "error": "高级席位周额度耗尽",
                "reset_at": reset_at,
            },
        ), mock.patch.object(
            exporter,
            "set_sub2api_account_schedulable",
            return_value={"ok": True, "account_id": "101", "schedulable": False},
        ):
            controller._process_mother(mother)

        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "cooldown")
        history = db.list_team_rotation_member_history("child@example.com")
        self.assertFalse(history[0]["permanently_excluded"])
        self.assertAlmostEqual(history[0]["cooldown_until"], reset_at, delta=1)

    def test_5h_member_is_not_removed_when_hub_pause_fails(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            hub_status="success", hub_account_id="101",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 1, "in_use": 1, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "short_rate_limited",
                "error": "账号 5h 限流",
                "reset_at": time.time() + 3600,
            },
        ), mock.patch.object(
            exporter,
            "set_sub2api_account_schedulable",
            side_effect=RuntimeError("hub unavailable"),
        ):
            controller._process_mother(self.mother)

        service.remove_member.assert_not_called()
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["hub_status"], "pause_failed")
        self.assertIn("本轮暂不移出 Team", updated["error"])

    def test_hub_7d_exhausted_member_is_permanently_excluded_for_mother(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            hub_status="success", hub_account_id="101",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 1, "in_use": 1, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        service.remove_member.return_value = {"removed": True}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={"classification": "weekly_exhausted", "error": "账号 7d 耗尽"},
        ), mock.patch.object(
            exporter,
            "set_sub2api_account_schedulable",
            return_value={"ok": True, "account_id": "101", "schedulable": False},
        ):
            controller._process_mother(self.mother)

        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "exhausted")
        history = db.list_team_rotation_member_history("child@example.com")
        self.assertTrue(history[0]["permanently_excluded"])
        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))

    def test_inactive_hub_member_is_removed_and_seat_is_released(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], status="active", member_id="member-1",
            hub_status="success", hub_account_id="101",
        )
        db.record_team_mother_check(
            self.mother["id"], entitled=2, in_use=1, remaining=1
        )
        service = mock.Mock()
        service.remove_member.return_value = {"removed": True}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={
                "classification": "inactive",
                "error": "Sub2API 账号不可调度 (status=active)",
            },
        ):
            controller._process_mother(
                mother,
                force_team_refresh=False,
            )

        service.remove_member.assert_called_once_with(mother, "member-1")
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "cooldown")
        updated_mother = db.get_team_mother(self.mother["id"])
        self.assertEqual(updated_mother["seats_remaining"], 2)
        self.assertEqual(updated_mother["seats_in_use"], 0)

    def test_reauthorized_hub_push_prefixes_display_name_only(self):
        controller = TeamRotationController(service_factory=mock.Mock())
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        with mock.patch.object(
            exporter,
            "run_exports",
            return_value={"sub2api": {"ok": True, "account_id": "hub-reauth"}},
        ) as run_exports:
            controller._push_assignment_to_hub(
                self.mother, assignment, reauthorized=True
            )

        pushed_cred = run_exports.call_args.args[0]
        self.assertEqual(pushed_cred["name"], "重授权-child@example.com")
        self.assertEqual(pushed_cred["email"], "child@example.com")

    def test_removed_account_can_be_recycled_by_another_mother(self):
        mother_b = db.create_team_mother({
            "name": "Team B",
            "workspace_id": "team-workspace-b",
            "access_token": "mother-b-access-token",
            "enabled": True,
        })
        db.update_team_rotation_member(
            self.assignment["id"],
            status="removed",
            member_id="member-a",
            primary_used_percent=100.0,
            secondary_used_percent=50.0,
            joined_at=100.0,
            last_checked_at=200.0,
            removed_at=300.0,
            error="额度达到 100%",
            hub_status="success",
            hub_pushed_at=150.0,
            hub_last_attempt_at=140.0,
            hub_error="old error",
        )
        db.record_team_rotation_removal(
            "child@example.com",
            self.mother["id"],
            reason="7d exhausted",
            removed_at=300.0,
            permanently_excluded=True,
        )

        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))
        self.assertIsNone(db.claim_team_rotation_candidate(self.mother["id"]))
        self.assertTrue(db.has_team_rotation_candidate(mother_b["id"]))

        claim = db.claim_team_rotation_candidate(mother_b["id"])
        self.assertTrue(claim["recycled"])
        self.assertEqual(claim["id"], self.assignment["id"])
        self.assertEqual(claim["previous_mother_id"], self.mother["id"])
        self.assertIsNone(db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        ))

        recycled = db.find_team_rotation_member(
            mother_b["id"], "child@example.com"
        )
        self.assertEqual(recycled["status"], "pending")
        self.assertIsNone(recycled["member_id"])
        self.assertIsNone(recycled["primary_used_percent"])
        self.assertIsNone(recycled["secondary_used_percent"])
        self.assertIsNone(recycled["joined_at"])
        self.assertIsNone(recycled["last_checked_at"])
        self.assertIsNone(recycled["removed_at"])
        self.assertEqual(recycled["error"], "")
        self.assertEqual(recycled["hub_status"], "pending")
        self.assertIsNone(recycled["hub_pushed_at"])
        self.assertIsNone(recycled["hub_last_attempt_at"])
        self.assertEqual(recycled["hub_error"], "")

    def test_member_cannot_cycle_back_to_a_mother_already_used(self):
        mother_b = db.create_team_mother({
            "name": "Team B",
            "workspace_id": "team-workspace-b-cycle",
            "access_token": "mother-b-access-token",
            "enabled": True,
        })
        db.record_team_rotation_join("child@example.com", self.mother["id"], joined_at=100.0)
        db.update_team_rotation_member(
            self.assignment["id"], status="removed", removed_at=200.0
        )
        db.record_team_rotation_removal(
            "child@example.com", self.mother["id"], reason="7d exhausted",
            removed_at=200.0, permanently_excluded=True,
        )

        claim_b = db.claim_team_rotation_candidate(mother_b["id"])
        self.assertIsNotNone(claim_b)
        self.assertEqual(claim_b["email"], "child@example.com")
        db.update_team_rotation_member(claim_b["id"], status="removed", removed_at=300.0)
        db.record_team_rotation_join("child@example.com", mother_b["id"], joined_at=250.0)
        db.record_team_rotation_removal(
            "child@example.com", mother_b["id"], reason="7d exhausted",
            removed_at=300.0, permanently_excluded=True,
        )

        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))
        self.assertIsNone(db.claim_team_rotation_candidate(self.mother["id"]))
        self.assertEqual(
            [item["mother_id"] for item in db.list_team_rotation_member_history("child@example.com")],
            [self.mother["id"], mother_b["id"]],
        )

    def test_5h_cooldown_blocks_same_mother_until_reset_only(self):
        mother_b = db.create_team_mother({
            "name": "Team B Cooldown",
            "workspace_id": "team-workspace-b-cooldown",
            "access_token": "mother-b-access-token",
            "enabled": True,
        })
        db.update_team_rotation_member(
            self.assignment["id"], status="cooldown", removed_at=time.time()
        )
        db.record_team_rotation_removal(
            "child@example.com",
            self.mother["id"],
            reason="5h rate limit",
            cooldown_until=time.time() + 3600,
        )

        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))
        self.assertTrue(db.has_team_rotation_candidate(mother_b["id"]))

        db.record_team_rotation_removal(
            "child@example.com",
            self.mother["id"],
            reason="5h reset",
            cooldown_until=time.time() - 1,
        )
        self.assertTrue(db.has_team_rotation_candidate(self.mother["id"]))

    def test_exhausted_account_joins_another_mother(self):
        mother_b_public = db.create_team_mother({
            "name": "Team B",
            "workspace_id": "team-workspace-b",
            "access_token": "mother-b-access-token",
            "enabled": True,
        })
        mother_b = db.get_team_mother(mother_b_public["id"], include_secret=True)
        db.update_team_rotation_member(
            self.assignment["id"],
            status="exhausted",
            removed_at=300.0,
            error="额度达到 100%",
            hub_status="success",
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 2, "in_use": 1, "remaining_default": 1},
            "members": [],
        }
        service.invite_and_accept.return_value = {"member_id": "member-b"}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )

        with mock.patch.object(controller, "_push_assignment_to_hub") as push:
            controller._process_mother(mother_b)

        service.invite_and_accept.assert_called_once()
        recycled = db.find_team_rotation_member(
            mother_b["id"], "child@example.com"
        )
        self.assertEqual(recycled["status"], "active")
        self.assertEqual(recycled["member_id"], "member-b")
        self.assertEqual(recycled["hub_status"], "pending")
        push.assert_called_once()

    def test_join_401_skips_to_next_account_without_reauthorizing(self):
        for created_at, email in (
            (100.0, "invalid@example.com"),
            (200.0, "healthy@example.com"),
        ):
            db.save_registered({
                "email": email,
                "access_token": f"access-{email}",
                "session_token": f"session-{email}",
                "refresh_token": f"refresh-{email}",
            })
            con = db._conn()
            con.execute(
                "UPDATE registered SET created_at=? WHERE email=?",
                (created_at, email),
            )
            con.commit()

        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 3, "in_use": 1, "remaining_default": 2},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        service.check_quota.return_value = {
            "status": "alive",
            "http_status": 200,
            "primary_used_percent": 10.0,
            "secondary_used_percent": 5.0,
            "error": "",
        }
        service.invite_and_accept.side_effect = [
            TeamChildAuthInvalidError("子号接受邀请失败: HTTP 401, token_invalidated"),
            {"member_id": "member-healthy"},
        ]
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )

        with mock.patch.object(controller, "_push_assignment_to_hub"), mock.patch(
            "webui.registrar.reauthorize_registered_account"
        ) as reauthorize:
            controller._process_mother(self.mother)

        self.assertEqual(service.invite_and_accept.call_count, 2)
        reauthorize.assert_not_called()
        invalid = db.find_team_rotation_member(
            self.mother["id"], "invalid@example.com"
        )
        healthy = db.find_team_rotation_member(
            self.mother["id"], "healthy@example.com"
        )
        self.assertEqual(invalid["status"], "auth_required")
        self.assertEqual(invalid["error"], "等待账号池手动重授权")
        self.assertEqual(healthy["status"], "active")
        self.assertEqual(healthy["member_id"], "member-healthy")

    def test_manual_reauthorization_returns_waiting_account_to_pool(self):
        db.update_team_rotation_member(
            self.assignment["id"],
            status="auth_required",
            error="等待账号池手动重授权",
        )

        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))
        self.assertTrue(db.release_team_rotation_auth_required("child@example.com"))
        self.assertIsNone(db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        ))
        self.assertTrue(db.has_team_rotation_candidate(self.mother["id"]))
        claim = db.claim_team_rotation_candidate(self.mother["id"])
        self.assertEqual(claim["email"], "child@example.com")
        self.assertFalse(claim["recycled"])

    def test_candidate_requires_complete_tokens_and_done_mailbox(self):
        db.save_registered({
            "email": "incomplete@example.com",
            "access_token": "access-incomplete",
            "refresh_token": "refresh-incomplete",
        })
        db.save_registered({
            "email": "staged@example.com",
            "access_token": "access-staged",
            "session_token": "session-staged",
            "refresh_token": "refresh-staged",
        })
        con = db._conn()
        con.execute(
            "INSERT INTO outlook_accounts "
            "(email, kind, status, imported_at) VALUES (?, 'icloud', 'available', ?)",
            ("staged@example.com", 100.0),
        )
        con.commit()

        self.assertFalse(db.has_team_rotation_candidate(self.mother["id"]))
        db.mark_done("staged@example.com")
        self.assertTrue(db.has_team_rotation_candidate(self.mother["id"]))
        claim = db.claim_team_rotation_candidate(self.mother["id"])
        self.assertEqual(claim["email"], "staged@example.com")

    def test_service_classifies_child_accept_401_as_auth_invalid(self):
        with mock.patch("webui.team_rotation.TeamApiClient") as client_cls:
            client_cls.return_value.request.return_value = (
                401,
                {"error": {"code": "token_invalidated"}},
            )
            service = TeamService()
            with mock.patch.object(
                service,
                "_mother_request",
                return_value=(200, {"success": True}),
            ):
                with self.assertRaises(TeamChildAuthInvalidError):
                    service.invite_and_accept(
                        self.mother,
                        {
                            "email": "invalid@example.com",
                            "access_token": "invalid-access",
                        },
                    )

    def test_join_is_not_confirmed_when_member_list_never_contains_child(self):
        service = TeamService()
        child = service._credentials_from_registered({
            "email": "missing@example.com",
            "access_token": "child-access",
            "user_id": "personal-user-id",
        })
        try:
            with mock.patch.object(
                service,
                "get_team_members",
                return_value={"members": [], "total_members": 0},
            ), mock.patch("webui.team_rotation.time.sleep"):
                with self.assertRaisesRegex(
                    TeamApiError, "Team 成员列表中未找到 missing@example.com"
                ):
                    service._confirm_joined_member(self.mother, child)
        finally:
            service.close()

    def test_auto_accept_mode_enables_once_then_only_requests_join(self):
        db.update_team_mother(self.mother["id"], {
            "email": "owner@example.com",
            "join_mode": "auto_accept_request",
            "auto_accept_configured": False,
        })
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = TeamService()
        try:
            with mock.patch.object(
                service,
                "_mother_request",
                return_value=(200, {"success": True}),
            ) as mother_request, mock.patch.object(
                service.client,
                "request",
                return_value=(200, {"success": True}),
            ) as child_request, mock.patch.object(
                service,
                "_confirm_joined_member",
                side_effect=lambda _mother, child, **_kwargs: {
                    "member_id": child.user_id or child.email,
                    "email": child.email,
                },
            ):
                first = service.invite_and_accept(mother, {
                    "email": "first@example.com",
                    "access_token": "first-access",
                    "user_id": "first-user",
                })
                second = service.invite_and_accept(mother, {
                    "email": "second@example.com",
                    "access_token": "second-access",
                    "user_id": "second-user",
                })

            self.assertEqual(first["member_id"], "first-user")
            self.assertEqual(second["member_id"], "second-user")
            settings_calls = [
                call for call in mother_request.call_args_list
                if call.args[2].endswith("/settings/auto_accept_requests")
            ]
            self.assertEqual(len(settings_calls), 1)
            self.assertEqual(settings_calls[0].kwargs["json_body"], {"value": True})
            self.assertEqual(
                len([
                    call for call in mother_request.call_args_list
                    if "/invites?" in call.args[2]
                ]),
                2,
            )
            self.assertEqual(child_request.call_count, 2)
            for call in child_request.call_args_list:
                self.assertTrue(call.args[1].endswith("/invites/request"))
                self.assertEqual(call.kwargs["account_id"], "")
                self.assertFalse(call.kwargs["include_cookies"])
                self.assertFalse(call.kwargs["include_session_id"])
                self.assertNotIn("json_content_type", call.kwargs)
            updated = db.get_team_mother(self.mother["id"])
            self.assertTrue(updated["auto_accept_configured"])
        finally:
            service.close()

    def test_advanced_mother_approves_pending_request_as_prolite(self):
        db.update_team_mother(self.mother["id"], {
            "email": "owner@example.com",
            "join_mode": "auto_accept_request",
            "preferred_seat_type": "advanced",
            "auto_accept_configured": True,
        })
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = TeamService()
        pending = {
            "id": "request-advanced-1",
            "email": "advanced@example.com",
            "role": "standard-user",
            "seat_type": "default",
        }
        try:
            with mock.patch.object(
                service.client, "request", return_value=(200, {"id": "request-advanced-1"})
            ) as child_request, mock.patch.object(
                service,
                "_find_pending_request",
                side_effect=[None, pending],
            ), mock.patch.object(
                service,
                "_mother_request",
                return_value=(200, {"ok": True}),
            ) as mother_request, mock.patch.object(
                service,
                "get_team_members",
                side_effect=[
                    {"members": [], "total_members": 0},
                    {"members": [{
                        "id": "advanced-user",
                        "email": "advanced@example.com",
                        "seat_type": "prolite",
                    }], "total_members": 1},
                ],
            ), mock.patch("webui.team_rotation.time.sleep"):
                result = service.invite_and_accept(mother, {
                    "email": "advanced@example.com",
                    "access_token": "advanced-access",
                    "user_id": "advanced-user",
                    "account_id": "personal-account",
                })

            self.assertEqual(result["member_id"], "advanced-user")
            self.assertEqual(result["seat_type"], "advanced")
            self.assertEqual(mother_request.call_args.args[1], "PATCH")
            self.assertTrue(
                mother_request.call_args.args[2].endswith(
                    "/invites/request-advanced-1"
                )
            )
            self.assertEqual(
                mother_request.call_args.kwargs["json_body"],
                {
                    "role": "standard-user",
                    "seat_type": "prolite",
                    "accept_request": True,
                },
            )
            self.assertEqual(child_request.call_args.kwargs["account_id"], "personal-account")
            self.assertTrue(child_request.call_args.kwargs["empty_body"])
            self.assertNotIn("json_body", child_request.call_args.kwargs)
        finally:
            service.close()

    def test_existing_default_member_is_switched_and_verified_as_prolite(self):
        db.update_team_mother(self.mother["id"], {
            "preferred_seat_type": "advanced",
        })
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = TeamService()
        try:
            with mock.patch.object(
                service, "_mother_request", return_value=(200, {"ok": True})
            ) as request, mock.patch.object(
                service,
                "get_team_members",
                return_value={"members": [{
                    "id": "member-1",
                    "email": "child@example.com",
                    "seat_type": "prolite",
                }], "total_members": 1},
            ), mock.patch("webui.team_rotation.time.sleep"):
                result = service._ensure_member_seat_type(
                    mother,
                    {"id": "member-1", "email": "child@example.com", "seat_type": "default"},
                    "prolite",
                )

            self.assertEqual(result["seat_type"], "prolite")
            self.assertTrue(request.call_args.args[2].endswith("/users/member-1/seat/update"))
            self.assertEqual(request.call_args.kwargs["json_body"]["seat_type"], "prolite")
            self.assertEqual(request.call_args.kwargs["json_body"]["operation"], "switch")
        finally:
            service.close()

    def test_advanced_mother_persists_official_seat_pools(self):
        mother = db.create_team_mother({
            "name": "Advanced Team",
            "email": "owner@example.com",
            "workspace_id": "advanced-workspace",
            "access_token": "owner-token",
            "preferred_seat_type": "advanced",
        })
        db.record_team_mother_check(
            mother["id"],
            entitled=1,
            in_use=1,
            remaining=3,
            capacity={
                "standard": {"paid": 0, "assigned": 0, "available": 0},
                "advanced": {"paid": 4, "assigned": 1, "available": 3},
            },
        )

        stored = db.get_team_mother(mother["id"])
        self.assertEqual(stored["preferred_seat_type"], "advanced")
        self.assertEqual(stored["join_mode"], "auto_accept_request")
        self.assertEqual(stored["seats_remaining"], 3)
        self.assertEqual(stored["seat_capacity"]["advanced"]["available"], 3)

    def test_advanced_mother_uses_prolite_capacity_for_refill_count(self):
        db.update_team_mother(self.mother["id"], {
            "preferred_seat_type": "advanced",
        })
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = TeamService()
        try:
            with mock.patch.object(
                service,
                "_mother_request",
                return_value=(200, {
                    "seats_entitled": 1,
                    "seats_in_use": 1,
                    "assigned": {"default": 1, "prolite": 2},
                    "seat_capacity": [
                        {"type": "default", "paid": 1, "available": 0},
                        {"type": "prolite", "paid": 6, "available": 4},
                    ],
                }),
            ):
                seats = service.get_team_seats(mother)

            self.assertEqual(seats["remaining_default"], 4)
            self.assertEqual(seats["remaining_standard"], 0)
            self.assertEqual(seats["remaining_advanced"], 4)
            self.assertEqual(seats["pools"]["advanced"]["assigned"], 2)
        finally:
            service.close()

    def test_auto_accept_mother_only_claims_matching_email_domain(self):
        db.update_team_mother(self.mother["id"], {
            "email": "owner@icloud.com",
            "join_mode": "auto_accept_request",
        })
        db.save_registered({
            "email": "older@gmail.com",
            "access_token": "gmail-access",
            "session_token": "gmail-session",
            "refresh_token": "gmail-refresh",
        })
        db.save_registered({
            "email": "matching@icloud.com",
            "access_token": "icloud-access",
            "session_token": "icloud-session",
            "refresh_token": "icloud-refresh",
        })

        claim = db.claim_team_rotation_candidate(self.mother["id"])

        self.assertIsNotNone(claim)
        self.assertEqual(claim["email"], "matching@icloud.com")

    def test_quota_refreshes_codex_token_before_usage_check(self):
        service = TeamService()
        try:
            usage = {
                "rate_limit": {
                    "primary_window": {"used_percent": 37},
                    "secondary_window": {"used_percent": 12},
                }
            }
            with mock.patch(
                "webui.exporter.refresh_codex_token",
                return_value={
                    "access_token": "fresh-codex-access",
                    "refresh_token": "fresh-codex-refresh",
                    "id_token": "fresh-id-token",
                },
            ) as refresh, mock.patch.object(
                db, "update_registered_codex_tokens", return_value=True
            ) as update, mock.patch.object(
                service.client, "request", return_value=(200, usage)
            ) as request:
                result = service.check_quota(
                    db.get_registered("child@example.com"),
                    "team-workspace",
                )
                second = service.check_quota(
                    db.get_registered("child@example.com"),
                    "team-workspace",
                )

            self.assertEqual(result["status"], "alive")
            self.assertEqual(result["primary_used_percent"], 37.0)
            self.assertEqual(second["status"], "alive")
            refresh.assert_called_once_with("refresh-token")
            update.assert_called_once_with(
                "child@example.com",
                refresh_token="fresh-codex-refresh",
                id_token="fresh-id-token",
            )
            credentials = request.call_args.args[2]
            self.assertEqual(credentials.access_token, "fresh-codex-access")
            self.assertEqual(credentials.cookie_header, "")
            self.assertEqual(request.call_args.kwargs["account_id"], "team-workspace")
        finally:
            service.close()

    def _quota_service(self, *quota_results):
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 2, "in_use": 2, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        service.check_quota.side_effect = list(quota_results)
        service.remove_member.return_value = {"removed": True}
        return service

    def test_hub_status_error_reauthorizes_and_repushes(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], hub_status="success", hub_account_id="101"
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 2, "in_use": 2, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        controller = TeamRotationController(service_factory=mock.Mock(return_value=service))

        def mark_hub_success(_mother, assignment, **_kwargs):
            db.update_team_rotation_member(
                assignment["id"], hub_status="success", hub_error="", hub_account_id="102"
            )

        with mock.patch(
            "webui.registrar.reauthorize_registered_account",
            return_value={"ok": True, "account": db.get_registered("child@example.com")},
        ) as reauthorize, mock.patch.object(
            controller, "_push_assignment_to_hub", side_effect=mark_hub_success
        ) as push, mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={"classification": "auth_required", "error": "access token expired"},
        ):
            controller._process_mother(self.mother)

        reauthorize.assert_called_once()
        push.assert_called_once()
        service.remove_member.assert_not_called()
        updated = db.find_team_rotation_member(self.mother["id"], "child@example.com")
        self.assertEqual(updated["status"], "active")
        self.assertEqual(updated["hub_status"], "success")
        self.assertEqual(updated["reauth_failure_count"], 0)

    def test_two_reauthorization_failures_remove_member_and_stop_retrying(self):
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        db.update_team_rotation_member(
            assignment["id"], hub_status="success", hub_account_id="101"
        )
        service = mock.Mock()
        service.get_team_detail.return_value = {
            "seats": {"entitled": 2, "in_use": 2, "remaining_default": 0},
            "members": [{"id": "member-1", "email": "child@example.com"}],
        }
        service.remove_member.return_value = {"removed": True}
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )

        with mock.patch.object(
            exporter,
            "get_sub2api_account_status",
            return_value={"classification": "auth_required", "error": "access token expired"},
        ), mock.patch(
            "webui.registrar.reauthorize_registered_account",
            return_value={"ok": False, "error": "HTTP 429 rate_limit_exceeded"},
        ) as reauthorize:
            controller._process_mother(self.mother)

            after_first = db.find_team_rotation_member(
                self.mother["id"], "child@example.com"
            )
            self.assertEqual(after_first["status"], "active")
            self.assertEqual(after_first["reauth_failure_count"], 1)
            service.remove_member.assert_not_called()

            controller._process_mother(self.mother)

            after_second = db.find_team_rotation_member(
                self.mother["id"], "child@example.com"
            )
            self.assertEqual(after_second["status"], "auth_required")
            self.assertEqual(after_second["reauth_failure_count"], 2)
            self.assertIsNotNone(after_second["removed_at"])
            service.remove_member.assert_called_once_with(self.mother, "member-1")
            self.assertEqual(reauthorize.call_count, 2)

            controller._process_mother(
                db.get_team_mother(self.mother["id"], include_secret=True),
                force_team_refresh=False,
            )
            self.assertEqual(reauthorize.call_count, 2)

    def test_invalid_hub_refresh_token_reauthorizes_and_retries_once(self):
        controller = TeamRotationController(service_factory=mock.Mock())
        assignment = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        invalid = {
            "sub2api": {
                "ok": False,
                "error": "HTTP 401 refresh_token_invalidated: Your session has ended",
            },
        }
        success = {"sub2api": {"ok": True, "account_id": "hub-2"}}

        with mock.patch.object(
            exporter,
            "run_exports",
            side_effect=[invalid, success],
        ) as run_exports, mock.patch.object(
            registrar,
            "reauthorize_registered_account",
            return_value={"ok": True, "account": db.get_registered("child@example.com")},
        ) as reauthorize:
            controller._push_assignment_to_hub(self.mother, assignment)

        self.assertEqual(run_exports.call_count, 2)
        reauthorize.assert_called_once()
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_status"], "success")

    def test_reauthorization_requests_new_codex_refresh_token(self):
        observed = {}

        def start_registration(_account, options, observer=None):
            observed.update(options)
            db.save_registered({
                **db.get_registered("child@example.com"),
                "access_token": "new-web-access-token",
                "session_token": "new-session-token",
                "refresh_token": "new-codex-refresh-token",
            })
            observer("reauth-run", "done", {})
            return "reauth-run"

        with mock.patch.object(db, "get_account", return_value={
            "email": "child@example.com",
            "password": "mail-password",
            "kind": "outlook",
        }), mock.patch.object(
            registrar,
            "start_registration",
            side_effect=start_registration,
        ), mock.patch.object(
            registrar,
            "wait_run_done",
            return_value=True,
        ), mock.patch.object(
            registrar,
            "remove_run_observer",
        ), mock.patch.object(
            registrar,
            "remove_run_queue",
        ):
            result = registrar.reauthorize_registered_account("child@example.com")

        self.assertTrue(result["ok"])
        self.assertTrue(observed["want_refresh_token"])

    def test_periodic_hub_status_check_with_no_seat_skips_team_detail_api(self):
        db.record_team_mother_check(
            self.mother["id"],
            entitled=2,
            in_use=2,
            remaining=0,
        )
        db.update_team_rotation_member(self.assignment["id"], hub_status="success")
        mother = db.get_team_mother(self.mother["id"], include_secret=True)
        service = mock.Mock()
        controller = TeamRotationController(service_factory=mock.Mock(return_value=service))

        controller._process_mother(mother, force_team_refresh=False)

        service.get_team_detail.assert_not_called()
        service.invite_and_accept.assert_not_called()
        service.check_quota.assert_not_called()

    def test_cached_seats_fill_all_slots_without_rescanning_team(self):
        db.record_team_mother_check(
            self.mother["id"], entitled=3, in_use=1, remaining=2
        )
        for index in (2, 3):
            db.save_registered({
                "email": f"child-{index}@example.com",
                "access_token": f"access-{index}",
                "session_token": f"session-{index}",
                "refresh_token": f"refresh-{index}",
            })
        service = mock.Mock()
        service.invite_and_accept.side_effect = [
            {"member_id": "member-2"},
            {"member_id": "member-3"},
        ]
        controller = TeamRotationController(
            service_factory=mock.Mock(return_value=service)
        )

        controller._process_mother(
            db.get_team_mother(self.mother["id"], include_secret=True),
            force_team_refresh=False,
        )

        service.get_team_detail.assert_not_called()
        self.assertEqual(service.invite_and_accept.call_count, 2)
        updated_mother = db.get_team_mother(self.mother["id"])
        self.assertEqual(updated_mother["seats_in_use"], 3)
        self.assertEqual(updated_mother["seats_remaining"], 0)

    def test_quota_concurrency_is_persisted_in_rotation_options(self):
        controller = TeamRotationController(service_factory=mock.Mock())
        saved = controller._save_options({
            "interval_seconds": 60,
            "quota_threshold": 100,
            "quota_concurrency": 12,
            "proxy": "",
        })
        self.assertEqual(saved["quota_concurrency"], 12)
        self.assertEqual(db.get_setting("team_rotation_quota_concurrency"), "12")

    def test_active_member_hub_status_checks_overlap(self):
        emails = ["child-2@example.com", "child-3@example.com"]
        now = time.time()
        for index, email in enumerate(emails, start=2):
            db.save_registered({
                "email": email,
                "access_token": f"web-access-{index}",
                "session_token": f"session-{index}",
                "refresh_token": f"refresh-{index}",
            })
        con = db._conn()
        for index, email in enumerate(emails, start=2):
            con.execute(
                "INSERT INTO team_rotation_members "
                "(mother_id, email, member_id, status, hub_status, hub_account_id, created_at, updated_at) "
                "VALUES (?, ?, ?, 'active', 'success', ?, ?, ?)",
                (self.mother["id"], email, f"member-{index}", str(100 + index), now, now),
            )
        con.commit()
        db.record_team_mother_check(
            self.mother["id"], entitled=3, in_use=3, remaining=0
        )
        db.update_team_rotation_member(
            self.assignment["id"], hub_status="success", hub_account_id="101"
        )

        barrier = threading.Barrier(3)
        lock = threading.Lock()
        observed = {"active": 0, "max_active": 0}

        def check_status(_cfg, account_id, **_kwargs):
            with lock:
                observed["active"] += 1
                observed["max_active"] = max(observed["max_active"], observed["active"])
            try:
                barrier.wait(timeout=3)
            finally:
                with lock:
                    observed["active"] -= 1
            return {"classification": "healthy", "error": ""}

        controller = TeamRotationController(service_factory=mock.Mock())
        controller._save_options({
            "interval_seconds": 60,
            "quota_concurrency": 3,
            "mother_concurrency": 1,
            "proxy": "",
        })

        with mock.patch.object(exporter, "get_sub2api_account_status", side_effect=check_status):
            controller._process_mother(
                db.get_team_mother(self.mother["id"], include_secret=True),
                force_team_refresh=False,
            )

        self.assertEqual(observed["max_active"], 3)


if __name__ == "__main__":
    unittest.main()
