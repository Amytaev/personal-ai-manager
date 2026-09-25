"""Wishlist persistence (TZ v4 §16 / §26).

A flat local JSON file rather than a SQLite table - the wishlist is
small, entirely user-edited via Telegram commands, and independent of
any agent's run-to-run state, so it doesn't belong in agent_runs or
valorant_store. The VALORANT agent (Phase 6) will read this file to
match storefront items against it; it doesn't write to it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


class WishlistStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write([])

    def _read(self) -> list[str]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            logger.warning("Wishlist file missing or corrupt, treating as empty: %s", self.path)
            return []
        if isinstance(raw, dict):
            return list(raw.get("skins", []))
        if isinstance(raw, list):
            return list(raw)
        return []

    def _write(self, skins: list[str]) -> None:
        self.path.write_text(
            json.dumps({"skins": skins}, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def list(self) -> list[str]:
        return self._read()

    def add(self, skin: str) -> bool:
        """Adds ``skin`` (case-insensitive dedup). Returns True if it
        was actually added, False if it was blank or already present."""
        normalized = skin.strip()
        if not normalized:
            return False
        skins = self._read()
        if any(existing.lower() == normalized.lower() for existing in skins):
            return False
        skins.append(normalized)
        self._write(skins)
        return True

    def remove(self, skin: str) -> bool:
        """Returns True if something was actually removed."""
        target = skin.strip().lower()
        skins = self._read()
        remaining = [existing for existing in skins if existing.lower() != target]
        if len(remaining) == len(skins):
            return False
        self._write(remaining)
        return True
