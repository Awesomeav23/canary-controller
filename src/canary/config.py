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
        )
