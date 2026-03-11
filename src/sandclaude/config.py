"""
Type-safe configuration via pydantic-settings.
All settings are configurable via environment variables or .env file.
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    environment: str = "production"
    anthropic_api_key: str = ""
    port: int = 3271
    data_dir: Path = Path("./data")
    task_timeout_s: int = 1800  # 30 minutes
    max_concurrent: int = 3
    host_cwd: str = ""  # host working directory for local repo mode
    host_data_dir: str = ""  # host-side path for data_dir (needed for DinD volume mounts)
    api_url: str = "http://localhost:3271"  # public URL for generic (non-Slack) webhook payloads
    allowed_domains: str = ""  # comma-separated domains allowed in agent phase
    task_retention_days: int = 30  # auto-delete completed tasks older than this (0 = keep forever)
    skip_network_isolation: bool = False  # only allowed in development/test environments
    git_token: str = ""  # optional, for cloning private repos (GitHub PAT or similar)
    github_token: str = ""  # optional, for PyGithub-based PR creation
    auth_tokens: str = ""  # optional extra bearer tokens (comma-separated)
    allowed_repo_base: str = ""  # comma-separated allowed base dirs for local repo mounts
    webhook_include_prompt: bool = False  # include prompt in webhooks

    @field_validator("task_timeout_s")
    @classmethod
    def validate_timeout(cls, v: int) -> int:
        if v < 10:
            raise ValueError("TASK_TIMEOUT_S must be at least 10 seconds")
        if v > 86400:
            raise ValueError("TASK_TIMEOUT_S must not exceed 86400 (24 hours)")
        return v

    @field_validator("max_concurrent")
    @classmethod
    def validate_max_concurrent(cls, v: int) -> int:
        if v < 1:
            raise ValueError("MAX_CONCURRENT must be at least 1")
        if v > 100:
            raise ValueError("MAX_CONCURRENT must not exceed 100")
        return v

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
