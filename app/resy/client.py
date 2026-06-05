"""Resy API client — read-only operations only.

We never call /3/details or /3/book. The point of this app is to *notify*
the user when a slot opens up; the user does the actual booking themselves
via the Resy web/app. Keeps us clean re: ToS and the NYC reservation-resale law.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as Date
from typing import Any

import httpx

log = logging.getLogger("resy")

BASE_URL = "https://api.resy.com"


@dataclass(frozen=True)
class Venue:
    id: int
    name: str
    city: str | None
    neighborhood: str | None
    url_slug: str | None  # used for building booking links


@dataclass(frozen=True)
class Slot:
    """A single available reservation time slot."""
    token: str          # opaque slot identifier from Resy
    start_time: str     # "2026-05-12 19:30:00" — Resy's format, local to venue
    end_time: str | None
    table_type: str | None   # "Dining Room", "Bar", "Patio", etc.
    min_party: int | None
    max_party: int | None

    @property
    def time_hhmm(self) -> str:
        """Just the HH:MM portion for time-window matching."""
        # start_time looks like "2026-05-12 19:30:00"
        try:
            return self.start_time.split(" ", 1)[1][:5]
        except (IndexError, AttributeError):
            return ""

    @property
    def date_yyyy_mm_dd(self) -> str:
        try:
            return self.start_time.split(" ", 1)[0]
        except (IndexError, AttributeError):
            return ""


class ResyError(Exception):
    pass


class RateLimited(ResyError):
    pass


class ResyClient:
    def __init__(self, api_key: str, user_agent: str, timeout: float = 15.0) -> None:
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=timeout,
            headers={
                "Authorization": f'ResyAPI api_key="{api_key}"',
                "User-Agent": user_agent,
                "Accept": "application/json, text/plain, */*",
                "Origin": "https://resy.com",
                "Referer": "https://resy.com/",
                "X-Origin": "https://resy.com",
            },
            http2=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def search_venues(self, query: str, limit: int = 10) -> list[Venue]:
        """Search for venues by name. Used to resolve 'carbone' -> venue_id."""
        # The Resy search endpoint is a POST with a JSON body.
        url = f"{BASE_URL}/3/venuesearch/search"
        payload = {
            "query": query,
            "per_page": limit,
            "page": 1,
            "types": ["venue"],
        }
        try:
            r = await self._client.post(url, json=payload)
        except httpx.HTTPError as e:
            raise ResyError(f"network error: {e}") from e

        if r.status_code == 429:
            raise RateLimited("venuesearch rate limited")
        if r.status_code >= 400:
            raise ResyError(f"venuesearch {r.status_code}: {r.text[:200]}")

        data = r.json()
        hits = (
            data.get("search", {}).get("hits")
            or data.get("hits", {}).get("hits")
            or []
        )
        out: list[Venue] = []
        for h in hits:
            src = h.get("_source", h)
            try:
                vid = int(src.get("id", {}).get("resy") or src.get("id") or 0)
            except (TypeError, ValueError):
                vid = 0
            if not vid:
                continue
            out.append(
                Venue(
                    id=vid,
                    name=src.get("name", "?"),
                    city=(src.get("location") or {}).get("city")
                        or src.get("locality"),
                    neighborhood=(src.get("location") or {}).get("neighborhood")
                        or src.get("neighborhood"),
                    url_slug=src.get("url_slug") or src.get("slug"),
                )
            )
        return out

    async def find_slots(
        self,
        venue_id: int,
        day: Date,
        party_size: int,
        lat: float = 0.0,
        lng: float = 0.0,
    ) -> list[Slot]:
        """Find available slots for a venue on a specific day for a party size.

        This is the workhorse endpoint. No authentication required.
        """
        url = f"{BASE_URL}/4/find"
        params = {
            "venue_id": str(venue_id),
            "day": day.isoformat(),
            "party_size": str(party_size),
            "lat": str(lat),
            "long": str(lng),
        }
        try:
            r = await self._client.get(url, params=params)
        except httpx.HTTPError as e:
            raise ResyError(f"network error: {e}") from e

        if r.status_code == 429:
            raise RateLimited("find rate limited")
        if r.status_code >= 400:
            raise ResyError(f"find {r.status_code}: {r.text[:200]}")

        data = r.json()
        # Response shape: results.venues[0].slots[]
        venues = (data.get("results") or {}).get("venues") or []
        if not venues:
            return []

        slots_raw = venues[0].get("slots") or []
        out: list[Slot] = []
        for s in slots_raw:
            cfg = s.get("config") or {}
            d = s.get("date") or {}
            size = s.get("size") or {}
            token = cfg.get("token")
            start = d.get("start")
            if not token or not start:
                continue
            out.append(
                Slot(
                    token=token,
                    start_time=start,
                    end_time=d.get("end"),
                    table_type=cfg.get("type"),
                    min_party=size.get("min"),
                    max_party=size.get("max"),
                )
            )
        return out
