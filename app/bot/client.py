"""Discord bot — slash commands for managing watches."""
from __future__ import annotations

import logging
import re
from datetime import date as Date, datetime, timedelta
from typing import Optional

import discord
from discord import app_commands

from app.config import Settings
from app.resy import RateLimited, ResyClient, ResyError
from app.storage import Store, Watch

log = logging.getLogger("bot")

DAY_MAP = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]

TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def _parse_time(s: str) -> str | None:
    s = s.strip()
    m = TIME_RE.match(s)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def _parse_days(s: str) -> list[int] | None:
    """Parse 'fri,sat' or 'mon-thu' or '' (= all days)."""
    s = s.strip().lower()
    if not s:
        return []
    out: set[int] = set()
    for chunk in s.split(","):
        chunk = chunk.strip()
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            ai, bi = DAY_MAP.get(a.strip()), DAY_MAP.get(b.strip())
            if ai is None or bi is None:
                return None
            i = ai
            while True:
                out.add(i)
                if i == bi:
                    break
                i = (i + 1) % 7
        else:
            v = DAY_MAP.get(chunk)
            if v is None:
                return None
            out.add(v)
    return sorted(out)


def _parse_date_range(s: str) -> tuple[Date, Date] | None:
    """Parse '2026-05-12' (single day) or '2026-05-12..2026-05-20' (range)."""
    s = s.strip()
    try:
        if ".." in s:
            a, b = s.split("..", 1)
            d1 = Date.fromisoformat(a.strip())
            d2 = Date.fromisoformat(b.strip())
            if d2 < d1:
                return None
            return d1, d2
        d = Date.fromisoformat(s)
        return d, d
    except ValueError:
        return None


def _format_watch(w: Watch) -> str:
    days = (
        ",".join(DAY_NAMES[i] for i in w.days_of_week)
        if w.days_of_week else "any day"
    )
    status = "⏸ paused" if w.paused else "▶ active"
    return (
        f"`{w.id}` {status} • **{w.venue_name}** • party {w.party_size}\n"
        f"   dates `{w.date_start}..{w.date_end}` • times `{w.time_start}-{w.time_end}` "
        f"• {days} • every {w.poll_seconds}s"
    )


def build_bot(settings: Settings, store: Store) -> discord.Client:
    intents = discord.Intents.default()
    # We don't need message content; slash commands work without it.
    bot = discord.Client(intents=intents)
    tree = app_commands.CommandTree(bot)

    # Shared Resy client for command-time lookups (separate from watcher's)
    resy = ResyClient(
        api_key=settings.resy_api_key,
        user_agent=settings.user_agent,
        timeout=settings.http_timeout_seconds,
    )

    @bot.event
    async def on_ready() -> None:
        log.info("bot logged in as %s (id=%s)", bot.user, bot.user.id if bot.user else "?")
        try:
            if settings.discord_guild_id:
                guild = discord.Object(id=settings.discord_guild_id)
                tree.copy_global_to(guild=guild)
                synced = await tree.sync(guild=guild)
                log.info("synced %d commands to guild %s", len(synced), settings.discord_guild_id)
            else:
                synced = await tree.sync()
                log.info("synced %d global commands (may take up to 1h to appear)", len(synced))
        except discord.DiscordException:
            log.exception("command sync failed")

    @bot.event
    async def on_close() -> None:
        await resy.aclose()

    # ---------- /watch ----------

    @tree.command(name="watch", description="Watch a Resy venue for open reservations")
    @app_commands.describe(
        venue="Venue name (e.g. 'Carbone') or numeric venue_id",
        party_size="Party size",
        date="Single date YYYY-MM-DD or range YYYY-MM-DD..YYYY-MM-DD",
        time_window="Time window HH:MM-HH:MM (e.g. 19:00-21:30)",
        days="Days of week, e.g. 'fri,sat' or 'mon-thu' (blank = any day)",
        poll_seconds="How often to poll, in seconds (default 30, min 10)",
    )
    async def watch_cmd(
        interaction: discord.Interaction,
        venue: str,
        party_size: app_commands.Range[int, 1, 20],
        date: str,
        time_window: str,
        days: Optional[str] = "",
        poll_seconds: app_commands.Range[int, 10, 3600] = settings.default_poll_seconds,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=False)

        # Parse date
        dr = _parse_date_range(date)
        if not dr:
            await interaction.followup.send(
                "❌ Bad date. Use `YYYY-MM-DD` or `YYYY-MM-DD..YYYY-MM-DD`.",
                ephemeral=True,
            )
            return
        d_start, d_end = dr
        if d_end < Date.today():
            await interaction.followup.send("❌ That date is in the past.", ephemeral=True)
            return

        # Parse time window
        if "-" not in time_window:
            await interaction.followup.send(
                "❌ Bad time window. Use `HH:MM-HH:MM`.", ephemeral=True,
            )
            return
        t1_raw, t2_raw = time_window.split("-", 1)
        t1, t2 = _parse_time(t1_raw), _parse_time(t2_raw)
        if not t1 or not t2 or t1 > t2:
            await interaction.followup.send(
                "❌ Bad time window. Use `HH:MM-HH:MM` 24-hour, e.g. `18:30-21:00`.",
                ephemeral=True,
            )
            return

        # Parse days
        dow = _parse_days(days or "")
        if dow is None:
            await interaction.followup.send(
                "❌ Bad days. Use e.g. `fri,sat` or `mon-thu`, or leave blank.",
                ephemeral=True,
            )
            return

        # Resolve venue
        venue_id: int | None = None
        venue_name = venue
        venue_slug: str | None = None
        if venue.strip().isdigit():
            venue_id = int(venue.strip())
            # Best-effort: probe to confirm and try to grab a name from the response later
            venue_name = f"Venue #{venue_id}"
        else:
            try:
                hits = await resy.search_venues(venue, limit=5)
            except RateLimited:
                await interaction.followup.send(
                    "⏳ Resy is rate-limiting us. Try again in a minute.", ephemeral=True,
                )
                return
            except ResyError as e:
                await interaction.followup.send(f"❌ Resy error: {e}", ephemeral=True)
                return
            if not hits:
                await interaction.followup.send(
                    f"❌ No venues found for `{venue}`. Try a more specific name "
                    "or pass a numeric venue_id.",
                    ephemeral=True,
                )
                return
            top = hits[0]
            venue_id = top.id
            venue_name = top.name
            venue_slug = top.url_slug

        assert venue_id is not None
        w = Watch.make(
            user_id=interaction.user.id,
            channel_id=interaction.channel_id or 0,
            venue_id=venue_id,
            venue_name=venue_name,
            venue_slug=venue_slug,
            party_size=int(party_size),
            date_start=d_start.isoformat(),
            date_end=d_end.isoformat(),
            time_start=t1,
            time_end=t2,
            days_of_week=dow,
            poll_seconds=int(poll_seconds),
        )
        await store.add_watch(w)

        await interaction.followup.send(
            "✅ Watching for slots:\n" + _format_watch(w),
        )

    # ---------- /list ----------

    @tree.command(name="list", description="List your active watches")
    async def list_cmd(interaction: discord.Interaction) -> None:
        watches = store.list_watches_for_user(interaction.user.id)
        if not watches:
            await interaction.response.send_message(
                "You have no watches. Create one with `/watch`.", ephemeral=True,
            )
            return
        body = "\n\n".join(_format_watch(w) for w in watches)
        await interaction.response.send_message(
            f"**Your watches ({len(watches)}):**\n\n{body}",
            ephemeral=True,
        )

    # ---------- /stop ----------

    @tree.command(name="stop", description="Delete a watch by ID")
    @app_commands.describe(watch_id="The 8-char watch id from /list")
    async def stop_cmd(interaction: discord.Interaction, watch_id: str) -> None:
        w = store.get_watch(watch_id.strip())
        if not w or w.user_id != interaction.user.id:
            await interaction.response.send_message(
                "❌ No watch with that ID under your account.", ephemeral=True,
            )
            return
        await store.remove_watch(w.id)
        await interaction.response.send_message(
            f"🛑 Stopped watch `{w.id}` for **{w.venue_name}**.", ephemeral=True,
        )

    # ---------- /pause + /resume ----------

    @tree.command(name="pause", description="Pause a watch (keeps it but stops polling)")
    @app_commands.describe(watch_id="The 8-char watch id from /list")
    async def pause_cmd(interaction: discord.Interaction, watch_id: str) -> None:
        w = store.get_watch(watch_id.strip())
        if not w or w.user_id != interaction.user.id:
            await interaction.response.send_message("❌ Not found.", ephemeral=True)
            return
        await store.set_paused(w.id, True)
        await interaction.response.send_message(f"⏸ Paused `{w.id}`.", ephemeral=True)

    @tree.command(name="resume", description="Resume a paused watch")
    @app_commands.describe(watch_id="The 8-char watch id from /list")
    async def resume_cmd(interaction: discord.Interaction, watch_id: str) -> None:
        w = store.get_watch(watch_id.strip())
        if not w or w.user_id != interaction.user.id:
            await interaction.response.send_message("❌ Not found.", ephemeral=True)
            return
        await store.set_paused(w.id, False)
        await interaction.response.send_message(f"▶ Resumed `{w.id}`.", ephemeral=True)

    # ---------- /find (one-shot lookup) ----------

    @tree.command(name="find", description="One-shot: check current openings for a venue")
    @app_commands.describe(
        venue="Venue name or numeric ID",
        party_size="Party size",
        date="Date YYYY-MM-DD",
    )
    async def find_cmd(
        interaction: discord.Interaction,
        venue: str,
        party_size: app_commands.Range[int, 1, 20],
        date: str,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            d = Date.fromisoformat(date.strip())
        except ValueError:
            await interaction.followup.send("❌ Bad date.", ephemeral=True)
            return

        venue_id: int
        venue_name: str
        if venue.strip().isdigit():
            venue_id = int(venue.strip())
            venue_name = f"Venue #{venue_id}"
        else:
            try:
                hits = await resy.search_venues(venue, limit=1)
            except ResyError as e:
                await interaction.followup.send(f"❌ {e}", ephemeral=True)
                return
            if not hits:
                await interaction.followup.send("❌ No venues found.", ephemeral=True)
                return
            venue_id = hits[0].id
            venue_name = hits[0].name

        try:
            slots = await resy.find_slots(venue_id, d, int(party_size))
        except ResyError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return

        if not slots:
            await interaction.followup.send(
                f"No openings at **{venue_name}** on {d} for {party_size}.",
                ephemeral=True,
            )
            return

        slots = sorted(slots, key=lambda s: s.time_hhmm)
        lines = [
            f"• **{s.time_hhmm}**" + (f" — {s.table_type}" if s.table_type else "")
            for s in slots[:25]
        ]
        more = f"\n…and {len(slots) - 25} more" if len(slots) > 25 else ""
        await interaction.followup.send(
            f"**{venue_name}** — {d} — party {party_size}\n" + "\n".join(lines) + more,
            ephemeral=True,
        )

    # ---------- /search (find venue IDs) ----------

    @tree.command(name="search", description="Search for a venue by name to get its ID")
    @app_commands.describe(query="Search text, e.g. 'carbone'")
    async def search_cmd(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            hits = await resy.search_venues(query, limit=8)
        except ResyError as e:
            await interaction.followup.send(f"❌ {e}", ephemeral=True)
            return
        if not hits:
            await interaction.followup.send("No matches.", ephemeral=True)
            return
        lines = []
        for v in hits:
            loc = ", ".join(x for x in (v.neighborhood, v.city) if x)
            lines.append(f"• `{v.id}` — **{v.name}**" + (f" ({loc})" if loc else ""))
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    return bot
