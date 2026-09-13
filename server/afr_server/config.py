"""运行配置。全部可用环境变量覆盖，默认值面向"本机单用户"。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def _project_root() -> Path:
    """仓库根目录。server/afr_server/config.py -> 上溯三层。"""

    return Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    db_path: Path
    seed_on_startup: bool
    use_online_model: bool
    openai_model: str

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    root = _project_root()
    default_db = root / "data" / "afr.db"
    db_path = Path(os.environ.get("AFR_DB_PATH", default_db))

    settings = Settings(
        host=os.environ.get("AFR_HOST", "127.0.0.1"),
        port=int(os.environ.get("AFR_PORT", "7710")),
        db_path=db_path,
        seed_on_startup=_flag("AFR_SEED_ON_STARTUP", True),
        use_online_model=_flag("AFR_USE_ONLINE_MODEL", False),
        openai_model=os.environ.get("AFR_OPENAI_MODEL", "gpt-5-mini"),
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings


def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}

