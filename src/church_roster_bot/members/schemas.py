import re

from pydantic import BaseModel, Field, field_validator


class VolunteerCreate(BaseModel):
    name: str = Field(min_length=1, max_length=160)
    phone_e164: str
    role_ids: set[int] = Field(min_length=1)
    invite_opt_in: bool = False

    @field_validator("phone_e164")
    @classmethod
    def validate_phone(cls, value: str) -> str:
        if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
            raise ValueError("phone must use E.164 format")
        return value
