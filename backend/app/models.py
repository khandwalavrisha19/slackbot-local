from typing import Optional

from pydantic import BaseModel, Field, field_validator

from app.constants import MAX_QUESTION_LEN, MAX_CHANNEL_IDS
from app.utils import _validate_date, _validate_team_id, _validate_channel_id


class ChatRequest(BaseModel):
    team_id:    str           = Field(..., min_length=1, max_length=20)
    channel_id: str           = Field(..., min_length=1, max_length=20)
    question:   str           = Field(..., min_length=1, max_length=MAX_QUESTION_LEN)
    from_date:  Optional[str] = Field(None)
    to_date:    Optional[str] = Field(None)
    user_id:    Optional[str] = Field(None, max_length=20)
    top_k:      int           = Field(10, ge=1, le=12)

    @field_validator("team_id")
    @classmethod
    def validate_team(cls, v): return _validate_team_id(v)

    @field_validator("channel_id")
    @classmethod
    def validate_channel(cls, v): return _validate_channel_id(v)

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, v): return _validate_date(v)

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question cannot be blank")
        return v


class MultiChatRequest(BaseModel):
    team_id:     str           = Field(..., min_length=1, max_length=20)
    channel_ids: list[str]     = Field(..., min_length=1, max_length=MAX_CHANNEL_IDS)
    question:    str           = Field(..., min_length=1, max_length=MAX_QUESTION_LEN)
    from_date:   Optional[str] = Field(None)
    to_date:     Optional[str] = Field(None)
    user_id:     Optional[str] = Field(None, max_length=20)
    top_k:       int           = Field(10, ge=1, le=20)

    @field_validator("team_id")
    @classmethod
    def validate_team(cls, v): return _validate_team_id(v)

    @field_validator("channel_ids")
    @classmethod
    def validate_channels(cls, v: list[str]) -> list[str]:
        if not v:
            raise ValueError("channel_ids must not be empty")
        if len(v) > MAX_CHANNEL_IDS:
            raise ValueError(f"Too many channels — max {MAX_CHANNEL_IDS}")
        return [_validate_channel_id(c) for c in v]

    @field_validator("from_date", "to_date")
    @classmethod
    def validate_date(cls, v): return _validate_date(v)

    @field_validator("question")
    @classmethod
    def validate_question(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question cannot be blank")
        return v