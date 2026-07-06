"""Pydantic DTO для API (docs/05-api-contracts). Роли/поля согласованы с docs/04."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


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
    # ADR-0012: доп. команды, в которые пользователь добавляется при создании.
    extra_team_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def _normalize_extra_team_ids(self) -> CreateUserRequest:
        """Нормализация ``extra_team_ids`` (docs/05 §4, ADR-0012): только
        положительные, дедуп, исключить дубль домашней команды (``team_id`` уже
        добавляется home-членством). Порядок стабильный (первое вхождение)."""
        seen: set[int] = set()
        cleaned: list[int] = []
        for tid in self.extra_team_ids:
            if tid <= 0 or tid == self.team_id or tid in seen:
                continue
            seen.add(tid)
            cleaned.append(tid)
        self.extra_team_ids = cleaned
        return self


class UpdateUserRequest(BaseModel):
    team_id: int | None = None
    display_name: str | None = Field(None, max_length=100)


class AddMembershipRequest(BaseModel):
    """Тело ``POST /api/admin/users/{id}/teams`` (доп. членство, ADR-0012)."""

    team_id: int


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

    @field_validator("label", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            trimmed = value.strip()
            return trimmed or None
        return value  # не-строка → отклонит core-валидация pydantic


class UpdateNumberRequest(BaseModel):
    """Тело ``PATCH /api/numbers/{id}`` — редактирование никнейма (docs/05 §6).

    Presence-семантика различается роутером по наличию ключа ``label`` в body
    (``"label" in body``). Здесь только нормализация значения: trim (``mode=before``,
    поэтому ``max_length=100`` проверяется после strip → превышение = pydantic
    ``validation_error``), пустое/пробельное → ``None`` (затирание никнейма)."""

    label: str | None = Field(None, max_length=100)

    @field_validator("label", mode="before")
    @classmethod
    def _normalize_label(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, str):
            trimmed = value.strip()
            return trimmed or None
        return value  # не-строка → отклонит core-валидация pydantic
