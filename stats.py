"""Wigglesworth message statistics — persistent, batched per-user counters.

Tracks how many messages every member sends, per server, forever:

    messages.json
    {
      "v": 1,
      "guilds": {
        "<guild_id>": {
          "<user_id>": {
            "total": 1234,             # all-time messages
            "first": 1790819120.0,     # first counted message (unix ts)
            "last":  1791099796.0,     # most recent counted message
            "days":  {"2026-10-17": 42, ...}   # rolling ~45 UTC day buckets
          }
        }
      }
    }

Design notes:
* Writes are batched: record() only touches memory and marks the store dirty.
  bot.py calls flush() on its 60s maintenance loop (and early when a burst of
  records piles up, and on shutdown), so a busy server still persists quickly
  without hammering the disk on every single message.
* Writes are atomic-ish (temp file + replace) so a crash mid-write can't
  corrupt the store.
* Day buckets older than KEEP_DAYS are pruned lazily (only when a user's first
  message of a new day lands), keeping the file lean.
* No discord.py dependency — this module is pure storage.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

STORE_VERSION = 1
KEEP_DAYS = 45            # rolling day buckets kept per user
FLUSH_AFTER_RECORDS = 200  # safety valve: flush early during message bursts

_store: dict = {"v": STORE_VERSION, "guilds": {}}
_file: Path | None = None
_dirty = False
_pending = 0


# ---------------------------------------------------------------------------
# setup / persistence
# ---------------------------------------------------------------------------
def configure(data_dir) -> None:
    """Point the store at `<data_dir>/messages.json` and load it."""
    global _file
    _file = Path(data_dir) / "messages.json"
    _load()


def _load() -> None:
    global _store
    if _file is None or not _file.exists():
        return
    try:
        data = json.loads(_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(data, dict):
        return
    guilds = data.get("guilds")
    if isinstance(guilds, dict):
        _store = {"v": STORE_VERSION, "guilds": guilds}
    else:
        # tolerate a bare {guild: {user: record}} shape from manual edits
        _store = {"v": STORE_VERSION, "guilds": {
            str(g): {str(u): _norm(r) for u, r in users.items()}
            for g, users in data.items() if isinstance(users, dict)
        }}


def _norm(rec: dict) -> dict:
    """Coerce one user record into the canonical shape."""
    total = int(rec.get("total", 0) or 0)
    first = float(rec.get("first", 0) or 0)
    last = float(rec.get("last", 0) or 0)
    days = {str(k): int(v) for k, v in (rec.get("days") or {}).items()
            if str(v).lstrip("-").isdigit() and int(v) > 0}
    return {"total": max(total, 0), "first": first, "last": last, "days": days}


def flush() -> bool:
    """Persist to disk if there are unsaved changes. Returns True if written."""
    global _dirty, _pending
    if not _dirty or _file is None:
        return False
    try:
        _file.parent.mkdir(parents=True, exist_ok=True)
        tmp = _file.with_name(_file.name + ".tmp")
        tmp.write_text(json.dumps(_store, separators=(",", ":")), encoding="utf-8")
        tmp.replace(_file)
        _dirty = False
        _pending = 0
        return True
    except OSError:
        return False


def pending() -> int:
    """Unsaved record count (used by .diag)."""
    return _pending


# ---------------------------------------------------------------------------
# recording
# ---------------------------------------------------------------------------
def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def record(guild_id: int, user_id: int) -> None:
    """Count one message for a user (call only for real, non-bot messages)."""
    global _dirty, _pending
    try:
        now = time.time()
        guilds = _store.setdefault("guilds", {})
        users = guilds.setdefault(str(guild_id), {})
        uid = str(user_id)
        rec = users.get(uid)
        if not isinstance(rec, dict):
            rec = {"total": 0, "first": now, "last": now, "days": {}}
            users[uid] = rec
        day = _today()
        days = rec.get("days")
        if not isinstance(days, dict):
            days = {}
            rec["days"] = days
        if day not in days:
            # first message of a new UTC day -> prune buckets we no longer need
            cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
            rec["days"] = days = {k: v for k, v in days.items() if k >= cutoff}
        days[day] = int(days.get(day, 0)) + 1
        rec["total"] = int(rec.get("total", 0)) + 1
        rec["first"] = float(rec.get("first", 0) or now)
        rec["last"] = now
        _dirty = True
        _pending += 1
        if _pending >= FLUSH_AFTER_RECORDS:
            flush()
    except Exception:
        pass  # stats must never break message handling


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------
def _users(guild_id: int) -> dict:
    return _store.get("guilds", {}).get(str(guild_id), {}) or {}


def _sum_days(days: dict, span: int) -> int:
    today = datetime.now(timezone.utc).date()
    return sum(int(days.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0))
               for i in range(span))


def user_stats(guild_id: int, user_id: int) -> dict:
    """{total, today, week, month, first, last} for one user (zeros if new)."""
    rec = _users(guild_id).get(str(user_id))
    if not isinstance(rec, dict):
        return {"total": 0, "today": 0, "week": 0, "month": 0, "first": 0.0, "last": 0.0}
    days = rec.get("days") if isinstance(rec.get("days"), dict) else {}
    return {
        "total": int(rec.get("total", 0)),
        "today": int(days.get(_today(), 0)),
        "week": _sum_days(days, 7),
        "month": _sum_days(days, 30),
        "first": float(rec.get("first", 0) or 0),
        "last": float(rec.get("last", 0) or 0),
    }


def board(guild_id: int, limit: int | None = None) -> list[tuple[str, int]]:
    """[(user_id_str, total)] sorted by total desc (optional limit)."""
    rows = sorted((_users(guild_id)).items(),
                  key=lambda kv: (-int((kv[1] or {}).get("total", 0)), str(kv[0])))
    out = [(uid, int((rec or {}).get("total", 0))) for uid, rec in rows]
    return out[:limit] if limit else out


def rank(guild_id: int, user_id: int) -> tuple[int | None, int]:
    """(rank, tracked_count). rank is None when the user has no tracked messages."""
    uid = str(user_id)
    found = None
    for i, (other, total) in enumerate(board(guild_id), start=1):
        if other == uid:
            found = i
            break
    return found, tracked(guild_id)


def tracked(guild_id: int) -> int:
    return len(_users(guild_id))


def top_total(guild_id: int) -> int:
    rows = board(guild_id, limit=1)
    return rows[0][1] if rows else 0
