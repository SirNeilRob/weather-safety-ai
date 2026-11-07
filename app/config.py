"""Runtime configuration.

Everything is environment driven so the same code runs on a laptop with no
network, on a phone hotspot, and in the demo hall. Nothing here ever blocks on
a network call: bad or missing values fall back to safe offline defaults.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

TRUE_VALUES = {"1", "true", "yes", "on", "y"}


def _flag(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in TRUE_VALUES


def _number(name: str, default: float, cast=float):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return cast(raw)
    except (TypeError, ValueError):
        return default


def _csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_env_file(path: str | os.PathLike[str]) -> None:
    """Minimal .env loader. Real environment variables always win."""
    candidate = Path(path)
    if not candidate.is_file():
        return
    for raw_line in candidate.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


@dataclass(frozen=True)
class Settings:
    """Runtime configuration.

    ``forced_offline`` short-circuits every network decision. It exists so the
    offline path can be demonstrated on stage without unplugging anything.
    """

    app_name: str = "sharkbyte-backend"
    db_path: Path = Path("data/sharkbyte.db")

    # --- Swarm (remote) processor -------------------------------------------
    swarm_base_url: str | None = None
    swarm_api_key: str | None = None
    swarm_endpoint: str = "/v1/chat/completions"
    swarm_model: str = "swarm-default"
    swarm_timeout: float = 20.0
    swarm_max_retries: int = 2
    swarm_sync_endpoint: str | None = None

    # --- Connectivity probing ----------------------------------------------
    probe_urls: tuple[str, ...] = ("https://1.1.1.1", "https://www.gstatic.com/generate_204")
    probe_interval: float = 5.0
    probe_timeout: float = 3.0
    offline_after_failures: int = 2
    online_after_successes: int = 1
    probe_on_startup: bool = True

    # --- Local (offline) processor -----------------------------------------
    local_engine: str = "builtin"  # "builtin" | "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "llama3.2"
    ollama_timeout: float = 120.0

    # --- Alert pipeline -----------------------------------------------------
    # Which agent stages may be handed to Swarm when online. Empty (default)
    # keeps the whole pipeline local and deterministic. Set to "communication"
    # to let Swarm rewrite the delivery copy while classification and safety
    # stay local.
    alert_cloud_stages: tuple[str, ...] = ()

    # --- Queue / workers ----------------------------------------------------
    worker_count: int = 2
    max_attempts: int = 3
    job_timeout: float = 60.0
    worker_idle_poll: float = 1.0

    # --- Sync ---------------------------------------------------------------
    sync_interval: float = 10.0
    sync_batch: int = 20

    # --- Resilience ---------------------------------------------------------
    breaker_threshold: int = 3
    breaker_cooldown: float = 30.0

    # --- Demo / overrides ---------------------------------------------------
    forced_offline: bool = False

    host: str = "0.0.0.0"
    port: int = 8000

    @classmethod
    def load(cls, env_file: str | os.PathLike[str] | None = ".env") -> "Settings":
        if env_file:
            load_env_file(env_file)

        base_url = os.getenv("SWARM_BASE_URL") or None
        sync_endpoint = os.getenv("SWARM_SYNC_ENDPOINT") or None
        api_key = os.getenv("SWARM_API_KEY") or None

        return cls(
            app_name=os.getenv("APP_NAME", cls.app_name),
            db_path=Path(os.getenv("DB_PATH", str(cls.db_path))),
            swarm_base_url=base_url.rstrip("/") if base_url else None,
            swarm_api_key=api_key,
            swarm_endpoint=os.getenv("SWARM_ENDPOINT", cls.swarm_endpoint),
            swarm_model=os.getenv("SWARM_MODEL", cls.swarm_model),
            swarm_timeout=_number("SWARM_TIMEOUT", cls.swarm_timeout),
            swarm_max_retries=int(_number("SWARM_MAX_RETRIES", cls.swarm_max_retries, int)),
            swarm_sync_endpoint=sync_endpoint,
            probe_urls=_csv("PROBE_URLS", cls.probe_urls),
            probe_interval=_number("PROBE_INTERVAL", cls.probe_interval),
            probe_timeout=_number("PROBE_TIMEOUT", cls.probe_timeout),
            offline_after_failures=int(
                _number("OFFLINE_AFTER_FAILURES", cls.offline_after_failures, int)
            ),
            online_after_successes=int(
                _number("ONLINE_AFTER_SUCCESSES", cls.online_after_successes, int)
            ),
            probe_on_startup=_flag("PROBE_ON_STARTUP", cls.probe_on_startup),
            local_engine=os.getenv("LOCAL_ENGINE", cls.local_engine).strip().lower(),
            ollama_url=os.getenv("OLLAMA_URL", cls.ollama_url).rstrip("/"),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            ollama_timeout=_number("OLLAMA_TIMEOUT", cls.ollama_timeout),
            alert_cloud_stages=_csv("ALERT_CLOUD_STAGES", cls.alert_cloud_stages),
            worker_count=max(1, int(_number("WORKER_COUNT", cls.worker_count, int))),
            max_attempts=max(1, int(_number("MAX_ATTEMPTS", cls.max_attempts, int))),
            job_timeout=_number("JOB_TIMEOUT", cls.job_timeout),
            worker_idle_poll=_number("WORKER_IDLE_POLL", cls.worker_idle_poll),
            sync_interval=_number("SYNC_INTERVAL", cls.sync_interval),
            sync_batch=max(1, int(_number("SYNC_BATCH", cls.sync_batch, int))),
            breaker_threshold=max(1, int(_number("BREAKER_THRESHOLD", cls.breaker_threshold, int))),
            breaker_cooldown=_number("BREAKER_COOLDOWN", cls.breaker_cooldown),
            forced_offline=_flag("FORCED_OFFLINE", cls.forced_offline),
            host=os.getenv("HOST", cls.host),
            port=int(_number("PORT", cls.port, int)),
        )

    @property
    def swarm_configured(self) -> bool:
        return bool(self.swarm_base_url)

    @property
    def swarm_can_sync(self) -> bool:
        return bool(self.swarm_base_url and self.swarm_sync_endpoint)
