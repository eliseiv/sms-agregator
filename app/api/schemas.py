"""Pydantic DTO для API (docs/05-api-contracts). Роли/поля согласованы с docs/04."""

from __future__ import annotations

from pydantic import BaseModel, Field


class HealthResponse(BaseModel):
    status: str = "ok"
    service: str


# --- Auth -------------------------------------------------------------------


class LoginUsernameRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)


class LoginPasswordRequest(BaseModel):
    password: str = Field(..., min_length=1)
    csrf_token: str | None = None


class SetPasswordRequest(BaseModel):
    password: str = Field(..., min_length=8)
    password_confirm: str = Field(..., min_length=8)
    csrf_token: str | None = None


# --- Telegram SSO -----------------------------------------------------------


class TelegramAuthRequest(BaseModel):
    init_data: str = Field(..., min_length=1)


class TelegramAuthResponse(BaseModel):
    linked: bool
    redirect: str | None = None
    healed: bool | None = None
    logged_out: bool | None = None


# --- Admin: users -----------------------------------------------------------


class CreateUserRequest(BaseModel):
    username: str = Field(..., min_length=3, max_length=64)
    display_name: str | None = Field(None, max_length=100)
    team_id: int | None = None


class UpdateUserRequest(BaseModel):
    team_id: int | None = None
    display_name: str | None = Field(None, max_length=100)


# --- Admin: teams -----------------------------------------------------------


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class RenameTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)


class SetLeaderRequest(BaseModel):
    new_leader_user_id: int


# --- Numbers ----------------------------------------------------------------


class CreateNumberRequest(BaseModel):
    phone_number: str = Field(..., min_length=1)
    label: str | None = Field(None, max_length=100)
    team_id: int | None = None
