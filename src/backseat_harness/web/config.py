"""Web config (scaffold step 3.2)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WebConfig:
    """Resolved web server settings."""

    host: str = "127.0.0.1"
    port: int = 8090
    workspace: Path = Path(".")
