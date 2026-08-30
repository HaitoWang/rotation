from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator


class AccountInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=320)
    kind: str = Field(default="outlook", min_length=1, max_length=64)
    password: str = ""
    client_id: str = ""
    refresh_token: str = ""
    relay_url: str = ""

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        value = value.lower()
        if "@" not in value:
            raise ValueError("email 格式无效")
        return value


class AccountImportRequest(BaseModel):
    accounts: List[AccountInput] = Field(min_length=1, max_length=10000)


class AccountTextImportRequest(BaseModel):
    text: str = Field(min_length=1)
    kind: str = ""


class AccountResponse(AccountInput):
    pooled: bool = True
    status: str
    claimed_at: Optional[Any] = None
    finished_at: Optional[Any] = None
    fail_reason: Optional[str] = None

    @classmethod
    def from_model(cls, account: Any) -> "AccountResponse":
        return cls(
            email=account.email,
            kind=account.kind,
            pooled=bool(account.pooled),
            password=account.password,
            client_id=account.client_id,
            refresh_token=account.refresh_token,
            relay_url=account.relay_url,
            status=account.status,
            claimed_at=account.claimed_at,
            finished_at=account.finished_at,
            fail_reason=account.fail_reason,
        )


class RegisterRequest(BaseModel):
    email: Optional[str] = None
    kind: Optional[str] = None
    options: Dict[str, Any] = Field(default_factory=dict)


class RegisterResponse(BaseModel):
    run_id: str
    email: str


class RunResponse(BaseModel):
    run_id: str
    email: str
    status: Literal["queued", "running", "done", "failed"]
    options: Dict[str, Any]
    result: Dict[str, Any]
    log_path: str = ""
    started_at: Optional[Any] = None
    finished_at: Optional[Any] = None
    error: Optional[str] = None
    error_category: Optional[str] = None

    @classmethod
    def from_model(cls, run: Any) -> "RunResponse":
        return cls(
            run_id=run.run_id,
            email=run.email,
            status=run.status,
            options=run.options or {},
            result=run.result or {},
            log_path=run.log_path or "",
            started_at=run.started_at,
            finished_at=run.finished_at,
            error=run.error,
            error_category=run.error_category,
        )


class TeamMotherInput(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    session: str = Field(min_length=1, max_length=20000)
    workspace_id: str = Field(default="", max_length=200)
    enabled: bool = True
    join_mode: str = "invite_accept"
    preferred_seat_type: str = "standard"


class TeamMotherPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    session: Optional[str] = Field(default=None, max_length=20000)
    workspace_id: Optional[str] = Field(default=None, min_length=1, max_length=200)
    access_token: Optional[str] = None
    cookie_header: Optional[str] = None
    owner_user_id: Optional[str] = None
    enabled: Optional[bool] = None
    join_mode: Optional[str] = None
    preferred_seat_type: Optional[str] = None
