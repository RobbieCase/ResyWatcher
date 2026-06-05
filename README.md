# ResyWatcher

bot that checks resy for reservation openings and sends a ping on discord when something matching your criteria becomes available.

## features

- search for restaurants
- check current availability
- create reservation watches
- get discord notifications when a matching slot opens
- pause, resume, or remove watches

## commands

- `/search query:<text>` — search for a restaurant
- `/find venue:<name|id> party_size:N date:YYYY-MM-DD` — check availability
- `/watch ...` — create a watch
- `/list` — view active watches
- `/pause watch_id:<id>`
- `/resume watch_id:<id>`
- `/stop watch_id:<id>`

## example

```text
/watch venue:Carbone party_size:2 date:2026-05-15..2026-05-30 time_window:19:00-21:30
