"""Stable task request contracts shared by HTTP adapters and application services."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

MAX_TASK_CONCURRENCY = 200
DEFAULT_REGISTER_RETRY_TIMES = 1


class RegisterTaskRequest(BaseModel):
    platform: str
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = Field(default=1, ge=1)
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    register_retry_times: int = DEFAULT_REGISTER_RETRY_TIMES
    register_delay_seconds: float = 0
    proxy: Optional[str] = None
    executor_type: str = "protocol"
    captcha_solver: str = "yescaptcha"
    extra: dict = Field(default_factory=dict)


class BackfillRtTaskRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    only_missing_rt: bool = True
    allow_login: bool = True
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    delay_seconds: float = Field(default=5, ge=0)
    sms_max_phone_attempts: int = Field(default=3, ge=0, le=20)
    proxy: Optional[str] = None


class BackfillAccountIdTaskRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    only_missing_account_id: bool = True
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    delay_seconds: float = Field(default=3, ge=0)
    proxy: Optional[str] = None


class Bind2faTaskRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    all_filtered: bool = False
    email: str = ""
    status: str = ""
    plus_status: str = ""
    only_missing_2fa: bool = True
    allow_login: bool = True
    concurrency: int = Field(default=1, ge=1, le=MAX_TASK_CONCURRENCY)
    delay_seconds: float = Field(default=5, ge=0)
    proxy: Optional[str] = None
