"""The watcher loop.

Runs forever. Each tick:
  1. Build a set of (venue_id, day, party_size) probes from active watches,
     coalescing watches that share a probe so we make one HTTP call.
  2. For each probe, call Resy /4/find.
  3. Distribute the resulting slots to each watch that needs them, applying
     time-window and day-of-week filters.
  4. Dedup against the seen-slot store, then send Discord alerts.

We respect each watch's poll_seconds independently — a watch only re-polls
when poll_seconds have elapsed since its last_polled_at.
"""
from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import date as Date, datetime, timedelta, timezone
from typing import TYPE_CHECKING

from app.config import Settings
from app.resy import RateLimited, ResyClient, ResyError, Slot
from app.storage import Store, Watch

if TYPE_CHECKING:
    import discord

log = logging.getLogger("watcher")


@dataclass(frozen=True)
class Probe:
    """A unique (venue, day, party) combination to query Resy for."""
    venue_id: int
    day: Date
    party_size: int


class WatcherLoop:
    def __init__(self, settings: Settings, store: Store, bot: "discord.Client") -> None:
        self._settings = settings
        self._store = store
        self._bot = bot
        self._stop = asyncio.Event()
        self._resy = ResyClient(
            api_key=settings.resy_api_key,
            user_agent=settings.user_agent,
            timeout=settings.http_timeout_seconds,
        )
        # Last time we hit any endpoint, for global rate-limit pacing
        self._last_request_at: float = 0.0
        self._min_request_gap_s: float = 0.25  # max ~4 req/sec across all watches
        self._backoff_until: datetime | None = None

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        # Wait for bot to finish connecting before sending alerts
        await self._bot.wait_until_ready()
        log.info("watcher loop started")

        prune_counter = 0
        try:
            while not self._stop.is_set():
                try:
                    await self._tick()
                except Exception:
                    log.exception("tick failed")

                # Prune old seen-slot records every ~30 minutes
                prune_counter += 1
                if prune_counter >= 360:  # 360 ticks * ~5s = ~30 min
                    prune_counter = 0
                    try:
                        removed = await self._store.prune_old_seen()
                        if removed:
                            log.info("pruned %d old seen-slot records", removed)
                    except Exception:
                        log.exception("prune failed")

                # Sleep ~5s between ticks; per-watch poll_seconds is enforced inside.
                # This way a 30s watch fires every 30s, a 60s watch every 60s, etc.,
                # and we don't spin tighter than 5s.
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    pass
        finally:
            await self._resy.aclose()
            log.info("watcher loop stopped")

    # ---------- tick logic ----------

    async def _tick(self) -> None:
        # Honor global backoff if we've been rate limited
        if self._backoff_until and datetime.now(timezone.utc) < self._backoff_until:
            return

        watches = self._store.list_active_watches()
        if not watches:
            return

        # Find watches that are due to poll
        now = datetime.now(timezone.utc)
        due: list[Watch] = []
        for w in watches:
            if not w.last_polled_at:
                due.append(w)
                continue
            try:
                last = datetime.fromisoformat(w.last_polled_at)
                if last.tzinfo is None:
                    last = last.replace(tzinfo=timezone.utc)
            except ValueError:
                due.append(w)
                continue
            if (now - last).total_seconds() >= w.poll_seconds:
                due.append(w)

        if not due:
            return

        # Build probes — one per (venue, day, party) across all due watches
        probes: dict[Probe, list[Watch]] = defaultdict(list)
        today = Date.today()
        for w in due:
            try:
                d_start = Date.fromisoformat(w.date_start)
                d_end = Date.fromisoformat(w.date_end)
            except ValueError:
                log.warning("watch %s has bad dates, skipping", w.id)
                continue
            d = max(d_start, today)
            while d <= d_end:
                if not w.days_of_week or d.weekday() in w.days_of_week:
                    probes[Probe(w.venue_id, d, w.party_size)].append(w)
                d += timedelta(days=1)

        log.debug("tick: %d due watches, %d probes", len(due), len(probes))

        for probe, watch_list in probes.items():
            if self._stop.is_set():
                break
            await self._pace()
            try:
                slots = await self._resy.find_slots(
                    venue_id=probe.venue_id,
                    day=probe.day,
                    party_size=probe.party_size,
                )
            except RateLimited:
                log.warning("rate limited; backing off 60s")
                self._backoff_until = datetime.now(timezone.utc) + timedelta(seconds=60)
                return
            except ResyError as e:
                log.warning("probe %s failed: %s", probe, e)
                continue

            # Mark each watch as polled even if no slots, so we space out next call
            poll_ts = datetime.now(timezone.utc).isoformat()
            for w in watch_list:
                w.last_polled_at = poll_ts

            if slots:
                for w in watch_list:
                    matching = [s for s in slots if w.matches_time(s.time_hhmm)]
                    if not matching:
                        continue
                    new_slots = [
                        s for s in matching
                        if not self._store.is_seen(
                            w.id, s.token, self._settings.alert_cooldown_minutes,
                        )
                    ]
                    if new_slots:
                        await self._send_alert(w, probe.day, new_slots)
                        await self._store.mark_seen(w.id, [s.token for s in new_slots])
                        w.last_alerted_at = poll_ts

            # Persist last_polled_at updates (and any last_alerted_at)
            for w in watch_list:
                await self._store.update_watch(w)

    async def _pace(self) -> None:
        """Light global pacing so we don't burst Resy with simultaneous calls."""
        loop = asyncio.get_running_loop()
        now = loop.time()
        elapsed = now - self._last_request_at
        if elapsed < self._min_request_gap_s:
            await asyncio.sleep(self._min_request_gap_s - elapsed)
        self._last_request_at = loop.time()

    # ---------- alerting ----------

    async def _send_alert(self, watch: Watch, day: Date, slots: list[Slot]) -> None:
        import discord

        channel = self._bot.get_channel(watch.channel_id)
        if channel is None:
            try:
                channel = await self._bot.fetch_channel(watch.channel_id)
            except discord.DiscordException as e:
                log.warning("can't fetch channel %s: %s", watch.channel_id, e)
                return

        # Sort by time
        slots = sorted(slots, key=lambda s: s.time_hhmm)
        times_str = ", ".join(
            f"{s.time_hhmm}" + (f" ({s.table_type})" if s.table_type else "")
            for s in slots[:10]
        )
        if len(slots) > 10:
            times_str += f", +{len(slots) - 10} more"

        booking_url = self._build_booking_url(watch, day)

        embed = discord.Embed(
            title=f"🍽️  {watch.venue_name}",
            description=(
                f"**{len(slots)}** slot{'s' if len(slots) != 1 else ''} open "
                f"on **{day.strftime('%a %b %-d')}** for **{watch.party_size}**\n"
                f"{times_str}"
            ),
            color=0x00C2A8,
            url=booking_url,
        )
        embed.add_field(name="Book it", value=f"[Open on Resy]({booking_url})", inline=False)
        embed.set_footer(text=f"watch {watch.id} • <@{watch.user_id}>")

        try:
            await channel.send(content=f"<@{watch.user_id}>", embed=embed)
        except discord.DiscordException as e:
            log.warning("alert send failed for watch %s: %s", watch.id, e)

    @staticmethod
    def _build_booking_url(watch: Watch, day: Date) -> str:
        # Resy URLs look like:
        #   https://resy.com/cities/ny/venues/<slug>?date=YYYY-MM-DD&seats=N
        # If we don't have a slug, fall back to the search URL.
        if watch.venue_slug:
            return (
                f"https://resy.com/cities/ny/venues/{watch.venue_slug}"
                f"?date={day.isoformat()}&seats={watch.party_size}"
            )
        return f"https://resy.com/?query={watch.venue_name.replace(' ', '+')}"
