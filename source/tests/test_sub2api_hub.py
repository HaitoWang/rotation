import base64
import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from webui import db, exporter, registrar
from webui.team_rotation import (
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

        self.assertEqual(result["classification"], "rate_limited")
        self.assertIn("/api/v1/admin/accounts/101", cffi.get.call_args.args[0])

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
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["hub_status"], "success")
        self.assertIsNotNone(updated["hub_pushed_at"])
        self.assertEqual(updated["hub_error"], "")

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

    def test_hub_rate_limited_member_is_removed_without_balance_probe(self):
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
            return_value={"classification": "rate_limited", "error": "账号限流"},
        ):
            controller._process_mother(self.mother)

        service.remove_member.assert_called_once_with(self.mother, "member-1")
        service.check_quota.assert_not_called()
        updated = db.find_team_rotation_member(
            self.mother["id"], "child@example.com"
        )
        self.assertEqual(updated["status"], "exhausted")

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
                side_effect=lambda _mother, child: {
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
            mother_request.assert_called_once()
            self.assertTrue(
                mother_request.call_args.args[2].endswith(
                    "/settings/auto_accept_requests"
                )
            )
            self.assertEqual(mother_request.call_args.kwargs["json_body"], {"value": True})
            self.assertEqual(child_request.call_count, 2)
            for call in child_request.call_args_list:
                self.assertTrue(call.args[1].endswith("/invites/request"))
                self.assertNotIn("account_id", call.kwargs)
                self.assertFalse(call.kwargs["include_cookies"])
                self.assertFalse(call.kwargs["include_session_id"])
                self.assertTrue(call.kwargs["json_content_type"])
            updated = db.get_team_mother(self.mother["id"])
            self.assertTrue(updated["auto_accept_configured"])
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

        def check_status(_cfg, account_id):
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
