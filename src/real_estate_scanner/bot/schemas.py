from __future__ import annotations

import json
from typing import Any

from aiogram.utils.web_app import WebAppInitData, safe_parse_webapp_init_data
from pydantic import BaseModel, ConfigDict, Field


class FilterSchema(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str | None = Field(default=None, max_length=200)
    type: str = Field(pattern=r"^(rent|sale)$")
    rooms: list[int] = Field(default_factory=list, min_length=0)

    price_min: int | None = Field(default=None, ge=0)
    price_max: int | None = Field(default=None, ge=0)

    area_min: float | None = Field(default=None, ge=0)
    area_max: float | None = Field(default=None, ge=0)

    region: str = Field(min_length=1, max_length=128)
    cities: list[str] = Field(default_factory=list, min_length=1)

    # Used for optional extra fields from frontend.
    additional_params: dict[str, Any] = Field(default_factory=dict)


def verify_webapp_init_data(
    *,
    bot_token: str,
    init_data_raw: str,
    expected_user_id: int,
) -> WebAppInitData:
    """
    Проверяет подпись и валидность initData Telegram WebApp.

    Используем безопасный парсер aiogram:
    - он валидирует подпись
    - кидает ValueError при проблемах
    """
    parsed = safe_parse_webapp_init_data(token=bot_token, init_data=init_data_raw)
    user = getattr(parsed, "user", None)
    if not user or user.id != expected_user_id:
        raise ValueError("WebApp initData user mismatch")
    return parsed


def parse_payload_json(raw: str) -> dict[str, Any]:
    """
    Парсит JSON строку из web_app_data.
    """
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("payload must be an object")
    return parsed

