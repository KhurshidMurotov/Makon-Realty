from __future__ import annotations

from typing import Any

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

