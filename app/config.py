"""Process configuration, read once from the environment (and an optional .env file)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env reader: KEY=VALUE lines, # comments; real env vars win."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Ring Partner API. Defaults point at the local ring-sandbox emulator.
    ring_base_url: str = os.environ.get("RING_BASE_URL", "http://127.0.0.1:8787")
    ring_token: str = os.environ.get("RING_TOKEN", "sandbox-token")
    ring_sandbox: bool = _bool("RING_SANDBOX", True)
    webhook_signing_key: str = os.environ.get("WEBHOOK_SIGNING_KEY", "heatwatch-demo-key")
    public_webhook_url: str = os.environ.get(
        "PUBLIC_WEBHOOK_URL", "http://127.0.0.1:8000/webhooks/ring"
    )
    # Reject deliveries whose signed meta.time is older than this (replay defense).
    webhook_max_age_s: float = float(os.environ.get("WEBHOOK_MAX_AGE_S", "300"))

    # How often the rules are evaluated (real seconds).
    tick_s: float = float(os.environ.get("HEATWATCH_TICK_S", "0.25"))

    # Optional access code for the family app. Empty = open (local demo only).
    access_code: str = os.environ.get("HEATWATCH_ACCESS_CODE", "")

    # Amazon Bedrock (optional). Without credentials HeatWatch uses fixed templates.
    bedrock_enabled: bool = _bool("BEDROCK_ENABLED", True)
    aws_region: str = os.environ.get("AWS_REGION", "us-east-1")
    bedrock_model_id: str = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-opus-5-5")

    data_dir: Path = Path(os.environ.get("HEATWATCH_DATA_DIR", str(ROOT / "data")))
    scenarios_dir: Path = ROOT / "scenarios"
    web_dir: Path = ROOT / "web"


CONFIG = Config()
