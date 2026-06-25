"""运行配置：从环境变量 / .env 读取。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    mcp_host: str = os.getenv("MCP_HOST", "0.0.0.0")
    mcp_port: int = int(os.getenv("MCP_PORT", "8010"))
    data_dir: Path = Path(os.getenv("DATA_DIR", "./data")).resolve()
    headless: bool = _bool("HEADLESS", True)
    default_user_key: str = os.getenv("DEFAULT_USER_KEY", "default")
    base_url: str = os.getenv("CCNU_BASE_URL", "https://kjyy.ccnu.edu.cn/jsq-v/#/main/home")
    challenge_ttl_seconds: int = int(os.getenv("CHALLENGE_TTL_SECONDS", "240"))
    default_username: str | None = os.getenv("CCNU_DEFAULT_USERNAME") or None
    default_password: str | None = os.getenv("CCNU_DEFAULT_PASSWORD") or None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "ccnu_lib.sqlite"

    def user_dir(self, user_key: str) -> Path:
        return self.data_dir / "users" / user_key

    def profile_dir(self, user_key: str) -> Path:
        return self.user_dir(user_key) / "profile"

    def screenshots_dir(self, user_key: str) -> Path:
        return self.user_dir(user_key) / "screenshots"


settings = Settings()
