import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Settings:
    database_url: str = "mysql+pymysql://canary:canary@127.0.0.1:3307/canary"
    prometheus_url: str = "http://127.0.0.1:9095"
    router: str = "log"  # "log" (no cluster), "replicas", or "gateway"
    namespace: str = "default"
    tick_seconds: float = 5.0
    default_policy_path: str = field(default=str(REPO_ROOT / "policies" / "default.yaml"))
    # When set, POST/PUT/DELETE need "Authorization: Bearer <token>". Reads stay open.
    api_token: str = ""
    slack_webhook_url: str = ""
    webhook_urls: tuple[str, ...] = ()  # generic JSON webhooks

    @classmethod
    def from_env(cls) -> "Settings":
        d = cls()
        return cls(
            database_url=os.getenv("CANARY_DATABASE_URL", d.database_url),
            prometheus_url=os.getenv("CANARY_PROMETHEUS_URL", d.prometheus_url),
            router=os.getenv("CANARY_ROUTER", d.router),
            namespace=os.getenv("CANARY_NAMESPACE", d.namespace),
            tick_seconds=float(os.getenv("CANARY_TICK_SECONDS", d.tick_seconds)),
            default_policy_path=os.getenv("CANARY_DEFAULT_POLICY", d.default_policy_path),
            api_token=os.getenv("CANARY_API_TOKEN", ""),
            slack_webhook_url=os.getenv("CANARY_SLACK_WEBHOOK_URL", ""),
            webhook_urls=tuple(
                u.strip() for u in os.getenv("CANARY_WEBHOOK_URLS", "").split(",") if u.strip()
            ),
        )

    def __repr__(self) -> str:  # never log secrets (tokens, webhook URLs)
        return f"Settings(router={self.router!r}, namespace={self.namespace!r}, ...)"
