"""Configuration from TOML, environment, and explicit CLI overrides."""

import os
import tomllib
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field


class RankWeights(BaseModel):
    model_config = ConfigDict(extra="forbid")
    embedding_drift: float = Field(default=0.25, ge=0)
    semantic_drift: float = Field(default=0.25, ge=0)
    substantive_information: float = Field(default=0.20, ge=0)
    economic_relevance: float = Field(default=0.20, ge=0)
    non_boilerplate: float = Field(default=0.10, ge=0)
    domain_boost: float = Field(default=0.05, ge=0)
    section_boost: float = Field(default=0.03, ge=0)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    data_dir: Path = Path("data")
    sec_user_agent: str = ""
    sec_requests_per_second: float = Field(default=4, gt=0, le=5)
    sec_retries: int = Field(default=3, ge=0, le=10)
    sec_timeout: float = Field(default=30, gt=0)
    metadata_ttl_seconds: int = Field(default=3600, ge=0)
    min_paragraph_chars: int = Field(default=40, ge=1)
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    embedding_enabled: bool = True
    embedding_batch_size: int = Field(default=64, ge=1)
    alignment_top_k: int = Field(default=3, ge=1)
    alignment_ambiguity_margin: float = Field(default=0.04, ge=0, le=1)
    cosine_floor: float = Field(default=0.60, ge=0, le=1)
    lexical_match_floor: float = Field(default=0.55, ge=0, le=1)
    cosmetic_threshold: float = Field(default=0.985, ge=0, le=1)
    jev_enabled: bool = True
    jev_concurrency: int = Field(default=4, ge=1, le=16)
    top_n: int = Field(default=20, ge=1, le=500)
    weights: RankWeights = Field(default_factory=RankWeights)

    @classmethod
    def load(cls, config: Path | None = None) -> "Settings":
        load_dotenv()
        values = tomllib.loads(config.read_text()) if config else {}
        if os.environ.get("SEC_USER_AGENT"):
            values["sec_user_agent"] = os.environ["SEC_USER_AGENT"]
        if os.environ.get("RADAR_DATA_DIR"):
            values["data_dir"] = os.environ["RADAR_DATA_DIR"]
        return cls.model_validate(values)

    def public_dict(self) -> dict:
        return self.model_dump(mode="json", exclude={"sec_user_agent"})
