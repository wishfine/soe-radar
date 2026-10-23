from __future__ import annotations

import os
from importlib.resources import files
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, model_validator

from .schemas import SourceSpec


class ProviderConfig(BaseModel):
    name: str
    protocol: str = Field(pattern="^(chat_completions|responses|codex_cli)$")
    enabled: bool = True
    base_url: str = ""
    models: list[str]
    free_models: list[str] = Field(default_factory=list)
    api_key_env: str = ""
    key_required: bool = True
    billing: str = Field(default="paid", pattern="^(free|paid|subscription)$")
    input_usd_per_million: float = Field(default=0, ge=0)
    output_usd_per_million: float = Field(default=0, ge=0)


class LLMConfig(BaseModel):
    allow_paid: bool = False
    monthly_budget_usd: float = Field(default=0, ge=0)
    daily_calls: int = Field(default=100, ge=0)
    max_attempts: int = Field(default=3, ge=1, le=3)
    timeout: int = Field(default=90, ge=1, le=600)
    max_output_tokens: int = Field(default=3000, ge=100, le=16000)
    providers: list[ProviderConfig] = Field(default_factory=list)


class EmployerConfig(BaseModel):
    name: str
    parent: str = ""
    proof_url: str
    aliases: list[str] = Field(default_factory=list)
    industry: str = "综合"

    @model_validator(mode="after")
    def check_proof(self):
        from .urls import public_url

        public_url(self.proof_url)
        return self


class Settings(BaseModel):
    timezone: str = "Asia/Shanghai"
    data_dir: Path = Path("data")
    schedule_hours: float = Field(default=6, gt=0)
    source_pages: int = Field(default=5, ge=1, le=100)
    details_per_source: int = Field(default=50, ge=1, le=1000)
    request_delay: float = Field(default=1, ge=0)
    http_timeout: int = Field(default=25, ge=1, le=180)
    allow_fake_ip_dns: bool = False
    browser_enabled: bool = False
    browser_channel: str | None = None
    jina_enabled: bool = False
    tavily_enabled: bool = False
    tavily_daily_limit: int = Field(default=20, ge=0, le=20)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    employers: list[EmployerConfig] = Field(default_factory=list)
    sources: list[SourceSpec] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_sources(self):
        ids = [x.id for x in self.sources]
        if len(ids) != len(set(ids)):
            raise ValueError("来源 id 不得重复")
        from zoneinfo import ZoneInfo

        ZoneInfo(self.timezone)
        return self


def config_path() -> Path:
    return Path(os.environ.get("SOE_RADAR_CONFIG", "config/local.yaml")).resolve()


def load_settings(path: Path | None = None) -> Settings:
    path = path or config_path()
    _load_env(Path.cwd() / ".env")
    defaults = yaml.safe_load(files("soe_radar").joinpath("defaults.yaml").read_text())
    if path.exists():
        override = yaml.safe_load(path.read_text()) or {}
        defaults = merge(defaults, override)
    cfg = Settings.model_validate(defaults)
    # Paths are relative to the working directory; CLI and README explicitly document this.
    cfg.data_dir = cfg.data_dir.resolve()
    return cfg


def merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if key in ("sources", "providers") and isinstance(value, list) and value:
            identity = "id" if key == "sources" else "name"
            entries = {e[identity]: dict(e) for e in result.get(key, [])}
            for entry in value:
                entries[entry[identity]] = merge(entries.get(entry[identity], {}), entry)
            result[key] = list(entries.values())
            continue
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else value
        )
    return result


def _load_env(path: Path) -> None:
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.strip().removeprefix("export ").split("=", 1)
                if key.strip().replace("_", "").isalnum():
                    os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
