"""JSON-backed persistent store.

Writes are atomic (write to temp, then rename) and serialized through an
asyncio.Lock so concurrent access from the bot and watcher loop is safe.

Schema:
{
  "version": 1,
  "watches": { "<id>": {...} },
  "seen": [ { "watch_id": "...", "slot_token": "...", "alerted_at": "..." } ]
}
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from app.storage.models import SeenSlot, Watch

log = logging.getLogger("store")

SCHEMA_VERSION = 1


class Store:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._watches: dict[str, Watch] = {}
        # Keyed by (watch_id, slot_token) for O(1) lookup
        self._seen: dict[tuple[str, str], SeenSlot] = {}

    # ---------- load / save ----------

    async def load(self) -> None:
        async with self._lock:
            if not self._path.exists():
                log.info("no store file at %s, starting fresh", self._path)
                await self._save_unlocked()
                return
            try:
                raw = self._path.read_text("utf-8")
                data = json.loads(raw) if raw.strip() else {}
            except (json.JSONDecodeError, OSError) as e:
                log.error("store load failed (%s); starting fresh", e)
                data = {}

            self._watches = {
                w["id"]: Watch.from_dict(w)
                for w in (data.get("watches") or {}).values()
            }
            self._seen = {
                (s["watch_id"], s["slot_token"]): SeenSlot.from_dict(s)
                for s in (data.get("seen") or [])
            }
            log.info(
                "loaded %d watches, %d seen-slot records",
                len(self._watches), len(self._seen),
            )

    async def _save_unlocked(self) -> None:
        """Atomic write: temp file in same dir, then rename."""
        payload = {
            "version": SCHEMA_VERSION,
            "watches": {wid: w.to_dict() for wid, w in self._watches.items()},
            "seen": [s.to_dict() for s in self._seen.values()],
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # tempfile in the same directory ensures atomic os.replace works
        # (atomicity requires same filesystem)
        fd, tmp_path = tempfile.mkstemp(
            prefix=".store-", suffix=".json.tmp", dir=str(self._path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    async def _save(self) -> None:
        async with self._lock:
            await self._save_unlocked()

    # ---------- watches ----------

    async def add_watch(self, watch: Watch) -> None:
        async with self._lock:
            self._watches[watch.id] = watch
            await self._save_unlocked()

    async def remove_watch(self, watch_id: str) -> bool:
        async with self._lock:
            existed = self._watches.pop(watch_id, None) is not None
            # Also clean up seen-slot rows for this watch
            self._seen = {
                k: v for k, v in self._seen.items() if v.watch_id != watch_id
            }
            if existed:
                await self._save_unlocked()
            return existed

    async def update_watch(self, watch: Watch) -> None:
        async with self._lock:
            self._watches[watch.id] = watch
            await self._save_unlocked()

    async def set_paused(self, watch_id: str, paused: bool) -> bool:
        async with self._lock:
            w = self._watches.get(watch_id)
            if not w:
                return False
            w.paused = paused
            await self._save_unlocked()
            return True

    def list_watches_for_user(self, user_id: int) -> list[Watch]:
        return [w for w in self._watches.values() if w.user_id == user_id]

    def list_active_watches(self) -> list[Watch]:
        return [w for w in self._watches.values() if not w.paused]

    def get_watch(self, watch_id: str) -> Watch | None:
        return self._watches.get(watch_id)

    # ---------- seen-slot dedup ----------

    def is_seen(
        self,
        watch_id: str,
        slot_token: str,
        cooldown_minutes: int,
    ) -> bool:
        """Returns True if we've alerted this slot for this watch within cooldown."""
        rec = self._seen.get((watch_id, slot_token))
        if not rec:
            return False
        try:
            t = datetime.fromisoformat(rec.alerted_at)
        except ValueError:
            return False
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) - t < timedelta(minutes=cooldown_minutes)

    async def mark_seen(self, watch_id: str, slot_tokens: Iterable[str]) -> None:
        async with self._lock:
            now = datetime.now(timezone.utc).isoformat()
            for token in slot_tokens:
                self._seen[(watch_id, token)] = SeenSlot(
                    watch_id=watch_id, slot_token=token, alerted_at=now,
                )
            await self._save_unlocked()

    async def prune_old_seen(self, older_than_days: int = 14) -> int:
        """Delete seen-slot records older than N days. Call periodically."""
        async with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
            before = len(self._seen)
            keep: dict[tuple[str, str], SeenSlot] = {}
            for k, v in self._seen.items():
                try:
                    t = datetime.fromisoformat(v.alerted_at)
                    if t.tzinfo is None:
                        t = t.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if t >= cutoff:
                    keep[k] = v
            self._seen = keep
            removed = before - len(self._seen)
            if removed:
                await self._save_unlocked()
            return removed
