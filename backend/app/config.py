from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    storage_dir: Path = ROOT / "storage"
    database_path: Path | None = None
    worker_token: str = "change-me-worker-token"
    git_webhook_secret: str = "change-me-git-secret"
    tracker_webhook_secret: str = "change-me-tracker-secret"
    public_url: str = "http://localhost:8000"
    tracker_webhook_public_url: str = ""
    telegram_credentials_file: Path | None = None
    auto_start: bool = True

    model_config = SettingsConfigDict(env_prefix="ORCHESTRATOR_", env_file=".env", extra="ignore")

    @property
    def db_path(self) -> Path:
        return self.database_path or self.storage_dir / "orchestrator.db"

    @property
    def tracker_webhook_url(self) -> str:
        base_url = self.tracker_webhook_public_url or self.public_url
        return f"{base_url.rstrip('/')}/api/webhooks/tracker"


settings = Settings()
settings.storage_dir.mkdir(parents=True, exist_ok=True)
(settings.storage_dir / "avatars").mkdir(exist_ok=True)
