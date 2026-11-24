"""Wigglesworth creator rewards system.

Existing Content Creators post a video link in the submissions channel. The bot
checks what it can automatically (duplicate links, video exists, duration,
public status, channel ownership) and either approves it or sends it to staff
review. Approved videos award Creator Credits, which can be redeemed for
in-game rewards fulfilled by staff.

Storage lives in DATA_DIR/creator.json so redeploys do not wipe progress.
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import aiohttp
import discord

BUILD = "2026-11-23.3"  # bump on every push; printed at startup

bm = None
_bot = None

DATA_FILE = None
DATA = {}
_DECIDING: set[str] = set()

PREFIX = "."
HTTP_TIMEOUT = 12
SITE_DATA_URL = os.getenv(
    "CREATOR_SITE_DATA_URL",
    "https://raw.githubusercontent.com/testacc31222-netizen/"
    "wigglesworth-moderator-apps/main/data.live.json").strip()

# ---------- reward catalog (launch set) ----------
# All amounts are Creator Credits. Only the rewards staff approved for launch are
# listed here; add more entries later without touching the rest of the system.
REWARD_CATALOG = [
    {"id": "cash", "name": "In-game cash bundle", "price": 250, "kind": "In-game",
     "desc": "A bundle of in-game cash handed over by staff."},
    {"id": "wheel", "name": "Cosmetic crate — wheel spin", "price": 400, "kind": "Cosmetic",
     "desc": "Spin the wheel for a random cosmetic. Odds are posted in the shop."},
    {"id": "doublexp", "name": "Double XP weekend (whole server)", "price": 1500,
     "kind": "Community",
     "desc": "Staff turns on double XP for the whole server for one weekend."},
]

# Cosmetic wheel weights. Keep these public in the shop embed; total must be 100.
WHEEL_PRIZES = [
    {"tier": "Common", "chance": 60, "label": "common cosmetic"},
    {"tier": "Rare", "chance": 30, "label": "rare cosmetic"},
    {"tier": "Limited", "chance": 10, "label": "limited cosmetic"},
]

TIERS = [
    {"id": "creator", "name": "Creator", "videos": 0, "multiplier": 1.0,
     "perk": "Creator commands, credits, shop, leaderboard."},
    {"id": "plus", "name": "Creator Plus", "videos": 10, "multiplier": 1.25,
     "perk": "1.25x credits and priority review."},
    {"id": "partner", "name": "Creator Partner", "videos": 25, "multiplier": 1.5,
     "perk": "1.5x credits, custom title eligibility, event collabs."},
]

# Weekly missions. Bonus credits are awarded once per user per week.
MISSIONS = [
    {"videos": 1, "bonus": 25, "label": "Post 1 approved video"},
    {"videos": 3, "bonus": 100, "label": "Post 3 approved videos"},
    {"videos": 5, "bonus": 200, "label": "Post 5 approved videos"},
]

BASE_CREDITS = 50
LONG_VIDEO_SECONDS = 300
LONG_VIDEO_BONUS = 10

# Monthly in-game gold paid to every Content Creator. Staff deliver it in-game;
# the bot only tracks who is owed what and pings the owner.
MONTHLY_GOLD = 500

DEFAULTS = {
    "role_id": 0,
    "owner_id": 0,
    "submissions_channel": 0,
    "review_channel": 0,
    "approved_channel": 0,
    "min_seconds": 10,
    "min_seconds_custom": False,
    "auto_approve": True,
    "require_linked_channel": True,
    "base_credits": BASE_CREDITS,
    "allowance_enabled": True,
    "allowance_gold": MONTHLY_GOLD,
}

VIDEO_HOSTS = {
    "youtube": ("youtube.com", "youtu.be", "youtube-nocookie.com"),
    "tiktok": ("tiktok.com",),
    "twitch": ("twitch.tv",),
    "streamable": ("streamable.com",),
    "x": ("x.com", "twitter.com"),
    "medal": ("medal.tv",),
    "instagram": ("instagram.com",),
    "kick": ("kick.com",),
    "reddit": ("reddit.com", "redd.it"),
}
OEMBED = {
    "youtube": "https://www.youtube.com/oembed",
    "tiktok": "https://www.tiktok.com/oembed",
    "twitch": "https://twitch.tv/oembed",
    "streamable": "https://api.streamable.com/oembed.json",
    "x": "https://publish.twitter.com/oembed",
    "reddit": "https://www.reddit.com/oembed",
}

URL_RE = re.compile(r"https?://[^\s<>()\[\]{}\"']+")


# ---------- persistence ----------

def _path():
    global DATA_FILE
    if DATA_FILE is None:
        DATA_FILE = (bm.DATA_DIR if bm is not None else _fallback_dir()) / "creator.json"
    return DATA_FILE


def _fallback_dir():
    from pathlib import Path
    return Path(os.getenv("DATA_DIR", "") or Path(__file__).parent)


def load() -> dict:
    global DATA
    p = _path()
    try:
        if p.exists():
            raw = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                DATA = raw
    except (json.JSONDecodeError, OSError):
        pass
    DATA.setdefault("guilds", {})
    return DATA


def save() -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(DATA, indent=2), encoding="utf-8")
        tmp.replace(p)
    except OSError as exc:
        print(f"[creator] save failed: {type(exc).__name__}: {exc}")


def gdata(guild_id) -> dict:
    gid = str(guild_id)
    if not DATA.get("guilds"):
        load()
    g = DATA["guilds"].setdefault(gid, {})
    g.setdefault("settings", dict(DEFAULTS))
    g.setdefault("creators", {})
    g.setdefault("submissions", {})
    g.setdefault("credits", {})
    g.setdefault("ledger", [])
    g.setdefault("redemptions", {})
    g.setdefault("missions", {})
    g.setdefault("allowances", {})
    g.setdefault("allowance_history", [])
    g.setdefault("withdrawals", {})
    g.setdefault("by_withdraw_message", {})
    g.setdefault("pending_withdrawals", {})
    for k, v in DEFAULTS.items():
        g["settings"].setdefault(k, v)
    # Older saves were created when the default minimum was 60s, then 15s. Skits and
    # shorts are the norm here, so move an untouched old default to the current one.
    if not g["settings"].get("min_seconds_custom") and g["settings"].get("min_seconds") in (60, 15):
        g["settings"]["min_seconds"] = DEFAULTS["min_seconds"]
    return g


def new_id(prefix="VC-") -> str:
    return prefix + uuid.uuid4().hex[:6].upper()


def now_ts() -> float:
    return time.time()


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def week_key(ts: float | None = None) -> str:
    d = datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc)
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


# ---------- links ----------

def extract_url(text: str) -> str | None:
    m = URL_RE.search(text or "")
    if not m:
        return None
    return m.group(0).rstrip(".,;:!?)>]")


def platform_for(url: str) -> str | None:
    try:
        host = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    except ValueError:
        return None
    host = host[4:] if host.startswith("www.") else host
    for name, domains in VIDEO_HOSTS.items():
        if any(host == d or host.endswith("." + d) for d in domains):
            return name
    return None


def youtube_id(url: str) -> str | None:
    try:
        p = urlparse(url)
    except ValueError:
        return None
    host = p.netloc.lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    if "youtube" in host:
        if p.path.startswith("/shorts/") or p.path.startswith("/live/") or p.path.startswith("/embed/"):
            parts = [x for x in p.path.split("/") if x]
            return parts[1] if len(parts) > 1 else None
        q = dict(parse_qsl(p.query))
        return q.get("v") or None
    return None


def normalize_url(url: str) -> str:
    """Canonical form for duplicate detection and stable storage."""
    platform = platform_for(url)
    if platform == "youtube":
        vid = youtube_id(url)
        if vid:
            return f"https://www.youtube.com/watch?v={vid}"
    try:
        p = urlparse(url)
    except ValueError:
        return url
    host = p.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = p.path.rstrip("/") or "/"
    keep = ("v", "id")
    query = [(k, v) for k, v in parse_qsl(p.query) if k in keep]
    return urlunparse((p.scheme or "https", host, path, "", urlencode(query), ""))


def is_short(url: str) -> bool:
    """Short-form vertical video (counts at a lower weight)."""
    low = (url or "").lower()
    return any(x in low for x in ("/shorts/", "tiktok.com", "/reel/", "clips.twitch.tv"))


# ---------- checks ----------

async def _get_json(session, url: str):
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT)) as r:
            if r.status != 200:
                return None
            ctype = (r.headers.get("content-type") or "").lower()
            if "json" not in ctype and "javascript" not in ctype:
                return None
            return await r.json(content_type=None)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError):
        return None


def parse_iso_duration(value: str | None) -> int | None:
    if not value:
        return None
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if not m:
        return None
    h, mi, s = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + s


def _names_match(a: str, b: str) -> bool:
    def norm(v):
        v = re.sub(r"[^a-z0-9]+", " ", (v or "").lower()).strip()
        return v
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) >= 2 or (len(ta) == 1 and ta <= tb) or (len(tb) == 1 and tb <= ta)


def channel_handle(url: str) -> str:
    """@handle / channel slug from a profile URL."""
    try:
        p = urlparse(url)
    except ValueError:
        return ""
    parts = [x for x in p.path.split("/") if x]
    if not parts:
        return ""
    return parts[0].lstrip("@").lower()


def channel_match(video: dict, creator: dict | None) -> bool | None:
    """Compare the video's channel with the creator's linked profile."""
    creator = creator or {}
    linked_url = str(creator.get("channel") or "")
    linked_name = str(creator.get("channel_name") or "")
    author = str(video.get("author") or "")
    author_url = str(video.get("author_url") or "")
    platform = str(video.get("platform") or "")
    if author_url and linked_url and platform_for(linked_url) == platform:
        a, b = channel_handle(author_url), channel_handle(linked_url)
        if a and b:
            return a == b
    if linked_name and author:
        return _names_match(author, linked_name)
    if linked_url and author:
        return _names_match(author, channel_handle(linked_url))
    return None


async def inspect_video(url: str, creator: dict | None, settings: dict,
                        session=None) -> dict:
    """Return what we could verify. verdict is 'approve' or 'review'."""
    own_session = session is None
    platform = platform_for(url)
    result = {
        "platform": platform or "other",
        "url": normalize_url(url),
        "original_url": url,
        "title": "",
        "author": "",
        "thumbnail": "",
        "duration_s": None,
        "views": None,
        "public": None,
        "channel_match": None,
        "verified": False,
        "reasons": [],
        "warnings": [],
        "verdict": "review",
    }
    if platform is None:
        result["reasons"].append("Not a supported video platform — staff check needed.")
        return result
    if platform == "medal" or platform == "kick" or platform == "instagram":
        result["warnings"].append(f"{platform} has no public metadata check — staff check needed.")
        result["reasons"].append("Platform cannot be auto-verified.")
        return result
    if session is None:
        session = aiohttp.ClientSession()
    try:
        endpoint = OEMBED.get(platform)
        if endpoint:
            data = await _get_json(session, f"{endpoint}?format=json&url={quote(url, safe='')}")
            if isinstance(data, dict) and (data.get("title") or data.get("author_name")):
                result["verified"] = True
                result["title"] = str(data.get("title") or "")[:200]
                result["author"] = str(data.get("author_name") or "")[:120]
                result["author_url"] = str(data.get("author_url") or "")[:400]
                result["thumbnail"] = str(data.get("thumbnail_url") or "")
            else:
                result["reasons"].append("Video link did not resolve — it may be private, deleted, or a bad link.")
        else:
            result["reasons"].append("No public metadata for this platform.")

        if platform == "youtube":
            await _youtube_enrich(session, result, url)

        if result["verified"]:
            if platform == "youtube" and result.get("channel_title_raw"):
                result["author"] = str(result["channel_title_raw"])
            result["channel_match"] = channel_match(result, creator)
            if result["channel_match"] is None and settings.get("require_linked_channel", True):
                result["reasons"].append("Creator has not linked their channel yet (`.creator link <url>`).")

        if result["duration_s"] is not None:
            limit = int(settings.get("min_seconds", 10))
            if result["duration_s"] < limit:
                result["reasons"].append(
                    f"Video is only {result['duration_s']}s — under the {limit}s minimum, staff check needed.")
        if result["public"] is False:
            result["reasons"].append("Video is not public — staff check needed.")
        if result["channel_match"] is False:
            result["reasons"].append("Video channel does not match the creator's linked channel.")

        if result["verified"] and not result["reasons"]:
            result["verdict"] = "approve"
    finally:
        if own_session:
            await session.close()
    return result


async def _youtube_enrich(session, result: dict, url: str) -> None:
    key = os.getenv("YOUTUBE_API_KEY", "").strip()
    vid = youtube_id(url)
    if not key or not vid:
        return
    api = ("https://www.googleapis.com/youtube/v3/videos?part=snippet,contentDetails,"
           f"statistics,status&id={vid}&key={key}")
    data = await _get_json(session, api)
    items = (data or {}).get("items") or []
    if not items:
        result["reasons"].append("YouTube API could not find the video.")
        return
    item = items[0]
    snippet = item.get("snippet") or {}
    details = item.get("contentDetails") or {}
    stats = item.get("statistics") or {}
    status = item.get("status") or {}
    result["title"] = str(snippet.get("title") or result["title"])[:200]
    result["author"] = str(snippet.get("channelTitle") or result["author"])[:120]
    result["channel_title_raw"] = str(snippet.get("channelTitle") or "")
    result["duration_s"] = parse_iso_duration(details.get("duration"))
    try:
        result["views"] = int(stats.get("viewCount") or 0)
    except (TypeError, ValueError):
        result["views"] = None
    privacy = str(status.get("privacyStatus") or "").lower()
    if privacy:
        result["public"] = privacy == "public"
    result["verified"] = True


# ---------- credits / tiers ----------

def tier_for(approved_count: int) -> dict:
    tier = TIERS[0]
    for t in TIERS:
        if approved_count >= t["videos"]:
            tier = t
    return tier


def next_tier(approved_count: int) -> dict | None:
    for t in TIERS:
        if approved_count < t["videos"]:
            return t
    return None


def month_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or now_ts(), tz=timezone.utc).strftime("%Y-%m")


def approved_count(g: dict, user_id, month: str | None = None) -> int:
    n = 0
    for s in g["submissions"].values():
        if str(s.get("user_id")) != str(user_id) or s.get("status") != "approved":
            continue
        if month and month_key(float(s.get("decided_at") or 0)) != month:
            continue
        n += 1
    return n


def credits(g: dict, user_id) -> int:
    try:
        return int(g["credits"].get(str(user_id), 0))
    except (TypeError, ValueError):
        return 0


def add_credits(g: dict, user_id, amount: int, reason: str, by=None) -> int:
    uid = str(user_id)
    g["credits"][uid] = max(0, credits(g, uid) + int(amount))
    g["ledger"].insert(0, {
        "t": iso_now(), "user": uid, "amount": int(amount),
        "reason": reason[:200], "by": str(by or ""),
    })
    del g["ledger"][500:]
    return g["credits"][uid]


# ---------- monthly gold allowance ----------

def allowance_gold(g: dict) -> int:
    try:
        return max(0, int(g["settings"].get("allowance_gold", MONTHLY_GOLD)))
    except (TypeError, ValueError):
        return MONTHLY_GOLD


def allowance_recipients(guild, g: dict) -> list[int]:
    """Current CC-role holders; fall back to known creators if the cache is empty."""
    role = creator_role(guild, g)
    if role is not None:
        members = [m for m in (getattr(role, "members", []) or [])
                   if not getattr(m, "bot", False)]
        if members:
            return sorted({int(m.id) for m in members})
    ids: set[int] = set()
    for uid in g.get("creators", {}):
        if str(uid).isdigit():
            ids.add(int(uid))
    for uid in g.get("credits", {}):
        if str(uid).isdigit():
            ids.add(int(uid))
    for sub in g.get("submissions", {}).values():
        uid = str(sub.get("user_id") or "")
        if uid.isdigit():
            ids.add(int(uid))
    if guild is not None:
        # Don't carry a debt for someone who has left the server.
        ids = {uid for uid in ids if guild.get_member(uid) is not None}
    return sorted(ids)


def queue_allowances(guild, g: dict, month: str | None = None) -> list[dict]:
    """Create this month's allowance records once. Safe to call repeatedly."""
    month = month or month_key()
    if not g["settings"].get("allowance_enabled", True):
        return []
    gold = allowance_gold(g)
    new: list[dict] = []
    for uid in allowance_recipients(guild, g):
        rec = g["allowances"].get(str(uid)) or {}
        if rec.get("month") == month and rec.get("status") in ("pending", "paid"):
            continue
        if rec and rec.get("month") != month and rec.get("status") == "pending":
            # Keep an unpaid older month visible in history instead of erasing it.
            history = g.setdefault("allowance_history", [])
            history.insert(0, dict(rec))
            del history[200:]
        rec = {
            "user_id": str(uid), "month": month, "gold": gold,
            "status": "pending", "created_at": iso_now(),
        }
        g["allowances"][str(uid)] = rec
        new.append(rec)
    if new:
        save()
    return new


def allowance_month(g: dict, month: str | None = None) -> list[dict]:
    month = month or month_key()
    return [r for r in g["allowances"].values() if r.get("month") == month]


def mark_allowance_paid(g: dict, user_id, by=None, month: str | None = None) -> bool:
    month = month or month_key()
    rec = g["allowances"].get(str(user_id)) or {}
    if rec.get("month") != month or rec.get("status") == "paid":
        return False
    rec["status"] = "paid"
    rec["paid_at"] = iso_now()
    rec["paid_by"] = str(by or "")
    save()
    return True


def mark_all_allowances_paid(g: dict, by=None, month: str | None = None) -> int:
    month = month or month_key()
    changed = 0
    for rec in allowance_month(g, month):
        if rec.get("status") == "paid":
            continue
        rec["status"] = "paid"
        rec["paid_at"] = iso_now()
        rec["paid_by"] = str(by or "")
        changed += 1
    if changed:
        save()
    return changed


def _missions_for(g: dict, user_id, decided_ts: float, commit: bool) -> tuple[int, list[str]]:
    """Preview (commit=False) or apply (commit=True) this video's weekly missions."""
    wk = week_key(decided_ts)
    uid = str(user_id)
    if commit:
        wk_map = g["missions"].setdefault(wk, {})
        state = wk_map.setdefault(uid, {"count": 0, "awarded": []})
    else:
        state = (g["missions"].get(wk) or {}).get(uid, {"count": 0, "awarded": []})
    upcoming = int(state.get("count") or 0) + 1
    awarded = list(state.get("awarded") or [])
    newly = [m for m in MISSIONS if upcoming >= m["videos"] and m["videos"] not in awarded]
    if commit:
        state["count"] = upcoming
        state["awarded"] = awarded + [m["videos"] for m in newly]
        # Keep only the last 12 weeks of mission state.
        for old in sorted(g["missions"])[:-12]:
            if old != wk:
                g["missions"].pop(old, None)
    return sum(m["bonus"] for m in newly), [m["label"] for m in newly]


def mission_bonus(g: dict, user_id, decided_ts: float) -> tuple[int, list[str]]:
    return _missions_for(g, user_id, decided_ts, commit=True)


def credits_for(g: dict, user_id, duration_s: int | None, decided_ts=None,
                commit: bool = False) -> dict:
    settings = g["settings"]
    base = int(settings.get("base_credits", BASE_CREDITS))
    bonus = 0
    notes = [f"Base {base}"]
    if duration_s and duration_s >= LONG_VIDEO_SECONDS:
        bonus += LONG_VIDEO_BONUS
        notes.append(f"+{LONG_VIDEO_BONUS} long video")
    tier = tier_for(approved_count(g, user_id))
    subtotal = int((base + bonus) * float(tier["multiplier"]) + 0.5)
    if tier["multiplier"] > 1:
        notes.append(f"x{tier['multiplier']} {tier['name']}")
    mb, labels = _missions_for(g, user_id, decided_ts or now_ts(), commit=commit)
    for label in labels:
        notes.append(f"+{next(m['bonus'] for m in MISSIONS if m['label'] == label)} mission: {label}")
    total = subtotal + mb
    return {"total": total, "base": subtotal, "mission": mb, "tier": tier, "notes": notes}


# ---------- eligibility ----------

def duplicate_of(g: dict, url: str, user_id=None) -> str | None:
    target = normalize_url(url)
    for sid, s in g["submissions"].items():
        if s.get("status") == "rejected":
            continue
        if s.get("url") == target:
            return sid
    return None


# ---------- formatting ----------

def fmt_int(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"


def platform_label(p: str | None) -> str:
    return {"youtube": "YouTube", "tiktok": "TikTok", "twitch": "Twitch",
            "streamable": "Streamable", "x": "X", "medal": "Medal",
            "instagram": "Instagram", "kick": "Kick", "reddit": "Reddit"}.get(
                (p or "").lower(), (p or "Other").title())


def progress_embed(guild, g: dict, member: discord.Member) -> discord.Embed:
    total = approved_count(g, member.id)
    month = approved_count(g, member.id, month_key())
    tier = tier_for(total)
    nxt = next_tier(total)
    uid = member.id
    creator = g["creators"].get(str(uid), {})
    lines = [
        f"**Tier:** {tier['name']}  ·  {tier['perk']}",
        f"**Credits:** {fmt_int(credits(g, uid))}",
        f"**Videos:** {total} all-time  ·  {month} this month",
    ]
    if nxt:
        lines.append(f"**Next tier:** {nxt['name']} at {nxt['videos']} videos ({nxt['videos'] - total} to go)")
    if creator.get("channel"):
        lines.append(f"**Linked channel:** {creator.get('channel')}")
    else:
        lines.append("**Linked channel:** not set — use `.creator link <your channel url>`")
    month_now = month_key()
    allowance = g.get("allowances", {}).get(str(uid)) or {}
    if allowance.get("month") == month_now:
        state = "paid ✅" if allowance.get("status") == "paid" else "queued for the owner ⏳"
        lines.append(f"**Monthly gold:** {fmt_int(allowance.get('gold'))} Gold — {state}")
        lines.append(f"**Gold available:** {fmt_int(pending_gold(g, uid))} · "
                     f"**Deducted:** {fmt_int(deducted_gold(g, uid))}")
    else:
        lines.append(f"**Monthly gold:** {fmt_int(allowance_gold(g))} Gold each month")
    wk = week_key()
    state = g["missions"].get(wk, {}).get(str(uid), {})
    mlines = []
    for m in MISSIONS:
        done = "✅" if m["videos"] in state.get("awarded", []) else f"{min(state.get('count', 0), m['videos'])}/{m['videos']}"
        mlines.append(f"{done} — {m['label']} (+{m['bonus']} credits)")
    em = bm.ui.embed(f"🎬 Creator progress — {member.display_name}", "\n".join(lines), "info",
                     footer="Wigglesworth • creator rewards")
    em.add_field(name="This week's missions", value="\n".join(mlines)[:1024], inline=False)
    return em


def submission_embed(guild, sub: dict, review: bool = True) -> discord.Embed:
    status = str(sub.get("status") or "pending").replace("_", " ").title()
    color = "success" if sub.get("status") == "approved" else (
        "error" if sub.get("status") == "rejected" else "warning")
    em = bm.ui.embed(
        f"🎬 {sub.get('title') or 'Creator submission'}",
        f"[{platform_label(sub.get('platform'))} video]({sub.get('url')})",
        color,
        footer=f"Wigglesworth • creator rewards • {sub.get('id')}")
    em.add_field(name="Creator", value=f"<@{sub['user_id']}>", inline=True)
    em.add_field(name="Status", value=f"**{status}**", inline=True)
    em.add_field(name="Video ID", value=f"`{sub.get('id')}`", inline=True)
    if sub.get("author"):
        em.add_field(name="Found channel", value=str(sub["author"])[:100], inline=True)
    checks = sub.get("checks") or {}
    detail = []
    if checks.get("duration_s"):
        detail.append(f"Duration {checks['duration_s'] // 60}m {checks['duration_s'] % 60:02d}s")
    if checks.get("views") is not None:
        detail.append(f"{fmt_int(checks['views'])} views")
    if checks.get("channel_match") is True:
        detail.append("channel matches ✅")
    elif checks.get("channel_match") is False:
        detail.append("channel mismatch ⚠️")
    if checks.get("public") is True:
        detail.append("public ✅")
    elif checks.get("public") is False:
        detail.append("not public ⚠️")
    reasons = sub.get("reasons") or []
    if reasons:
        em.add_field(name="Needs a human look", value="\n".join(f"• {r}" for r in reasons)[:1024], inline=False)
    if detail:
        em.add_field(name="Automatic checks", value=" · ".join(detail)[:1024], inline=False)
    if sub.get("staff_note"):
        em.add_field(name="Staff note", value=str(sub["staff_note"])[:1024], inline=False)
    if sub.get("credits"):
        em.add_field(name="Credits awarded", value=fmt_int(sub["credits"]), inline=True)
    if sub.get("decided_by"):
        em.add_field(name="Reviewed by", value=f"<@{sub['decided_by']}>", inline=True)
    if sub.get("original_url") and sub.get("original_url") != sub.get("url"):
        em.add_field(name="Original link", value=sub["original_url"][:200], inline=False)
    return em


def showcase_content(sub: dict) -> str:
    """Plain message (no embed) so Discord shows the video player preview."""
    title = str(sub.get("title") or "Creator video").strip()[:200]
    url = str(sub.get("url") or sub.get("original_url") or "")
    credits = fmt_int(sub.get("credits"))
    return (f"✅ **{title}**\n"
            f"{url}\n"
            f"<@{sub['user_id']}> · **+{credits} credits** · `.creator progress`")


def approved_embed(guild, sub: dict) -> discord.Embed:
    em = submission_embed(guild, sub, review=False)
    em.title = f"✅ Approved — {sub.get('title') or 'Creator video'}"
    lines = [
        f"Creator: <@{sub['user_id']}>",
        f"Credits awarded: **{fmt_int(sub.get('credits'))}**",
        f"Reviewed by: <@{sub.get('decided_by')}>",
        f"Date: {str(sub.get('decided_at_iso') or '')[:10]}",
    ]
    em.description = f"{em.description}\n\n" + "\n".join(lines)
    return em


# ---------- staff ----------

def is_staff(member: discord.Member) -> bool:
    if bm is not None and member.id in getattr(bm, "OWNER_IDS", set()):
        return True
    p = member.guild_permissions
    return bool(p.administrator or p.manage_guild or p.manage_messages)


def can_fulfill(member: discord.Member, guild=None, g: dict | None = None) -> bool:
    """Staff, bot owners, or the configured reward owner may deliver rewards."""
    if member is None:
        return False
    if is_staff(member):
        return True
    return int(getattr(member, "id", 0) or 0) in owner_ids(guild, g)


def staff_only():
    async def predicate(ctx):
        if ctx.guild is None:
            return False
        if is_staff(ctx.author):
            return True
        raise discord.CheckFailure("staff only")
    return predicate


def creator_role(guild: discord.Guild, g: dict) -> discord.Role | None:
    rid = int(g["settings"].get("role_id") or 0)
    if rid:
        role = guild.get_role(rid)
        if role:
            return role
    for name in ("Content Creator", "Content Creators", "Creator", "CC"):
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role
    return None


def has_creator_role(member: discord.Member, g: dict) -> bool:
    role = creator_role(member.guild, g)
    if role is None:
        return True  # not configured yet: don't lock people out
    return role in member.roles


async def _channel(guild: discord.Guild, key: str) -> discord.TextChannel | None:
    g = gdata(guild.id)
    cid = int(g["settings"].get(key) or 0)
    ch = guild.get_channel(cid) if cid else None
    return ch if isinstance(ch, discord.TextChannel) else None


async def _submission_channel(guild, sub: dict):
    """Where the original post lives: a channel normally, a thread in forums."""
    cid = int(sub.get("submission_channel_id") or 0)
    if cid:
        ch = guild.get_channel(cid) or guild.get_thread(cid)
        if ch is None:
            # Forum posts archive after a while and drop out of the cache.
            fetch = getattr(guild, "fetch_channel", None)
            if fetch:
                try:
                    ch = await fetch(cid)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    ch = None
        if ch is not None:
            return ch
    return await _channel(guild, "submissions_channel")


async def _send(channel, embed=None, view=None, content=None):
    try:
        return await channel.send(content=content, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"[creator] send failed: {type(exc).__name__}: {exc}")
        return None


def owner_ids(guild=None, g: dict | None = None) -> list[int]:
    """Reward owner: per-guild setting, then env, then bot owners, then server owner."""
    ids: set[int] = set()

    def _add(raw):
        for part in str(raw or "").replace(";", ",").split(","):
            part = part.strip()
            if part.isdigit():
                ids.add(int(part))

    if g is not None:
        _add(g["settings"].get("owner_id"))
    if not ids:
        _add(os.getenv("CREATOR_OWNER_ID", ""))
    if not ids:
        for raw in getattr(bm, "OWNER_IDS", set()) or ():
            _add(raw)
    if not ids and guild is not None and getattr(guild, "owner_id", None):
        ids.add(int(guild.owner_id))
    return sorted(ids)


async def _notify_owner(guild, content: str = "", embed=None, view=None,
                        channel_key: str = "review_channel"):
    """Ping the owner(s) where staff will see it; DM them if no channel is set."""
    g = gdata(guild.id)
    ids = owner_ids(guild, g)
    mentions = " ".join(f"<@{uid}>" for uid in ids)
    text = " ".join(part for part in (mentions, content) if part).strip()
    allowed = (discord.AllowedMentions(users=[discord.Object(id=uid) for uid in ids])
               if ids else discord.AllowedMentions.none())
    channel = await _channel(guild, channel_key) if channel_key else None
    if channel is not None:
        try:
            return await channel.send(content=text or None, embed=embed, view=view,
                                      allowed_mentions=allowed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            print(f"[creator] owner ping failed in #{getattr(channel, 'name', '?')}: "
                  f"{type(exc).__name__}: {exc}")
    for uid in ids:
        member = guild.get_member(uid) if guild is not None else None
        if member is None:
            continue
        try:
            sent = await member.send(content=content or "Creator reward needs you.",
                                     embed=embed, view=view)
            return sent
        except (discord.Forbidden, discord.HTTPException):
            continue
    return None


async def notify_allowances(guild, g: dict, records: list[dict]):
    """One owner message per batch of newly queued monthly allowances."""
    if not records:
        return None
    month = records[0].get("month") or month_key()
    month_label = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
    total = sum(int(r.get("gold") or 0) for r in records)
    lines = [f"<@{r['user_id']}> — **{fmt_int(r.get('gold'))} Gold**" for r in records[:20]]
    if len(records) > 20:
        lines.append(f"…and {len(records) - 20} more")
    em = bm.ui.embed(
        f"Monthly creator allowance — {month_label}",
        f"**{len(records)}** creator(s) · **{fmt_int(total)} Gold** total\n\n"
        + "\n".join(lines),
        "gold", footer="Wigglesworth • creator rewards • .creator allowance")
    content = ("Monthly creator gold is ready to deliver. "
               "Mark it with `.creator allowance paid @user` or `.creator allowance paid all`.")
    return await _notify_owner(guild, content, embed=em)


async def _allowance_tick(bot) -> None:
    for guild in bot.guilds:
        g = gdata(guild.id)
        new = queue_allowances(guild, g)
        if new:
            await notify_allowances(guild, g, new)


async def _allowance_loop() -> None:
    # Let the site sync and role cache settle before the first ping.
    await asyncio.sleep(90)
    while True:
        try:
            if _bot is not None and _bot.is_ready():
                await _allowance_tick(_bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let the monthly task die
            print(f"[creator] allowance check failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(6 * 60 * 60)


# ---------- gold withdrawals ----------

def withdrawals_for(g: dict, user_id, month: str | None = None) -> list[dict]:
    """A creator's withdrawal requests, newest first."""
    month = month or month_key()
    rows = [w for w in g.get("withdrawals", {}).values()
            if str(w.get("user_id")) == str(user_id) and w.get("month") == month]
    return sorted(rows, key=lambda w: w.get("requested_at") or "", reverse=True)


def latest_pending_withdrawal(g: dict, user_id) -> dict | None:
    for w in withdrawals_for(g, user_id):
        if w.get("status") == "pending":
            return w
    return None


def pending_gold(g: dict, user_id) -> int:
    """Gold still available this month after pending and accepted withdrawals."""
    uid = str(user_id)
    rec = g.get("allowances", {}).get(uid) or {}
    if rec.get("month") != month_key() or rec.get("status") == "paid":
        return 0
    total = max(0, int(rec.get("gold") or 0))
    reserved = sum(
        int(w.get("deducted_amount") or w.get("amount") or 0)
        if w.get("status") == "fulfilled" else int(w.get("amount") or 0)
        for w in g.get("withdrawals", {}).values()
        if str(w.get("user_id")) == uid
        and w.get("month") == month_key()
        and w.get("status") in ("pending", "fulfilled")
    )
    return max(0, total - reserved)


def deducted_gold(g: dict, user_id) -> int:
    """Gold already deducted in Discord for the amount received in-game."""
    uid = str(user_id)
    return sum(int(w.get("deducted_amount") or 0)
               for w in g.get("withdrawals", {}).values()
               if str(w.get("user_id")) == uid
               and w.get("month") == month_key()
               and w.get("status") == "fulfilled")


def find_withdrawal(g: dict, wid: str) -> dict | None:
    return g.get("withdrawals", {}).get((wid or "").strip().upper())


def find_withdrawal_for_message(g: dict, message) -> dict | None:
    mid = str(getattr(message, "id", "") or "")
    wid = (g.get("by_withdraw_message") or {}).get(mid)
    w = g.get("withdrawals", {}).get(wid or "")
    if w:
        return w
    if mid:
        for cand in g.get("withdrawals", {}).values():
            if str(cand.get("message_id") or "") == mid:
                return cand
    for e in (getattr(message, "embeds", None) or []):
        for text in (getattr(e, "description", "") or "",
                     getattr(getattr(e, "footer", None), "text", "") or ""):
            found = re.search(r"\bGW-[0-9A-F]{6}\b", str(text))
            if found:
                return g.get("withdrawals", {}).get(found.group(0))
    return None


def _withdraw_embed(w: dict, title: str, color: str = "gold") -> discord.Embed:
    return bm.ui.embed(
        title,
        f"Creator: <@{w.get('user_id')}>\nAmount: **{fmt_int(w.get('amount'))} Gold**\n"
        f"Request: `{w.get('id')}`\nStatus: **{w.get('status')}**\n\n"
        "Deliver the gold in-game, then press **Accept**. "
        "**Deny** returns it to the creator's balance. "
        "**Modify** changes the amount.",
        color, footer="Wigglesworth • creator rewards • .creator withdraw")


def _creator_withdraw_embed(w: dict) -> discord.Embed:
    return bm.ui.embed(
        "🪙 Your gold withdrawal request",
        f"Amount: **{fmt_int(w.get('amount'))} Gold**\n"
        f"Request: `{w.get('id')}`\nStatus: **{w.get('status')}**\n\n"
        "You can **Change amount** or **Cancel request** until the owner accepts. "
        "Once accepted, the amount is locked and deducted from your balance.",
        "gold", footer="Wigglesworth • creator rewards • .creator withdrawgold")


async def request_gold_withdrawal(guild, g: dict, member, amount) -> dict:
    """Reserve part of this month's gold and ping the owner for approval."""
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Amount must be a number."}
    if amount <= 0:
        return {"ok": False, "error": "Amount must be greater than 0."}
    uid = str(getattr(member, "id", "") or "")
    if not uid or not uid.isdigit():
        return {"ok": False, "error": "Could not identify the creator."}
    if not has_creator_role(member, g) and not can_fulfill(member, guild, g):
        return {"ok": False, "error": "Only Content Creators can withdraw gold."}
    available = pending_gold(g, uid)
    if available <= 0:
        return {"ok": False, "error":
                "No pending gold this month. Ask staff to queue allowances first."}
    if amount > available:
        return {"ok": False, "error":
                f"You only have **{fmt_int(available)} Gold** available to withdraw."}

    wid = new_id("GW-")
    w = {"id": wid, "user_id": uid, "amount": amount, "status": "pending",
         "month": month_key(), "requested_at": iso_now(),
         "owner_notified": False}
    g["withdrawals"][wid] = w
    save()

    mention = getattr(member, "mention", f"<@{uid}>")
    msg = await _notify_owner(
        guild,
        f"{mention} requested a gold withdrawal — {fmt_int(amount)} Gold. "
        f"Use `.creator withdraw accept {wid}` if buttons do not work.",
        embed=_withdraw_embed(w, "🪙 Gold withdrawal request"),
        view=GoldWithdrawalView())
    if msg is not None:
        w["message_id"] = msg.id
        w["owner_notified"] = True
        g.setdefault("by_withdraw_message", {})[str(msg.id)] = wid
    save()
    return {"ok": True, "wid": wid, "amount": amount,
            "owner_notified": bool(w["owner_notified"])}


async def _dm_creator(guild, uid, text: str) -> None:
    if guild is None:
        return
    member = guild.get_member(int(uid)) if str(uid).isdigit() else None
    if member is None:
        return
    try:
        await member.send(text)
    except (discord.Forbidden, discord.HTTPException, AttributeError):
        pass


async def fulfill_gold_withdrawal(guild, g: dict, w: dict | None, by=None) -> dict:
    if not w:
        return {"ok": False, "error": "Withdrawal not found."}
    if w.get("status") != "pending":
        return {"ok": False, "error": f"Already {w.get('status')}."}
    amount = int(w.get("amount") or 0)
    w["status"] = "fulfilled"
    w["deducted_amount"] = amount
    w["handled_by"] = str(getattr(by, "id", by) or "")
    w["handled_at"] = iso_now()
    rec = g.get("allowances", {}).get(str(w.get("user_id"))) or {}
    if rec:
        rec["deducted"] = deducted_gold(g, w.get("user_id"))
        rec["remaining"] = pending_gold(g, w.get("user_id"))
    save()
    remaining = pending_gold(g, w.get("user_id"))
    if remaining == 0:
        mark_allowance_paid(g, w.get("user_id"), by=w["handled_by"])
    await _dm_creator(guild, w.get("user_id"),
                      f"Your withdrawal was accepted. **{fmt_int(amount)} Gold** has been "
                      f"deducted from your Discord balance for the amount delivered in-game. "
                      f"Remaining this month: **{fmt_int(remaining)} Gold**. "
                      f"(Request `{w.get('id')}`)")
    return {"ok": True, "withdrawal": w}


async def cancel_gold_withdrawal(guild, g: dict, w: dict | None, by=None) -> dict:
    if not w:
        return {"ok": False, "error": "Withdrawal not found."}
    if w.get("status") != "pending":
        return {"ok": False, "error": f"Already {w.get('status')}."}
    w["status"] = "cancelled"
    w["handled_by"] = str(getattr(by, "id", by) or "")
    w["handled_at"] = iso_now()
    save()
    await _dm_creator(guild, w.get("user_id"),
                      f"Your gold withdrawal `{w.get('id')}` was cancelled. "
                      f"The gold is available to request again.")
    return {"ok": True, "withdrawal": w}


async def modify_gold_withdrawal(guild, g: dict, w: dict | None, amount, by=None) -> dict:
    if not w:
        return {"ok": False, "error": "Withdrawal not found."}
    if w.get("status") != "pending":
        return {"ok": False, "error": f"Already {w.get('status')}."}
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return {"ok": False, "error": "Amount must be a number."}
    if amount <= 0:
        return {"ok": False, "error": "Amount must be greater than 0."}
    other = sum(
        int(x.get("amount") or 0)
        for x in g.get("withdrawals", {}).values()
        if x is not w and str(x.get("user_id")) == str(w.get("user_id"))
        and x.get("month") == w.get("month")
        and x.get("status") in ("pending", "fulfilled")
    )
    rec = g.get("allowances", {}).get(str(w.get("user_id"))) or {}
    cap = max(0, int(rec.get("gold") or 0) - other)
    if amount > cap:
        return {"ok": False, "error": f"Maximum for this creator is **{fmt_int(cap)} Gold**."}
    w["amount"] = amount
    w["modified_by"] = str(getattr(by, "id", by) or "")
    w["modified_at"] = iso_now()
    save()
    await _dm_creator(guild, w.get("user_id"),
                      f"Your gold withdrawal `{w.get('id')}` was updated to "
                      f"**{fmt_int(amount)} Gold**.")
    return {"ok": True, "withdrawal": w}


class GoldWithdrawalView(discord.ui.View):
    """Owner controls on the withdrawal card posted to #creator-review."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _guard(self, interaction) -> tuple[dict | None, bool]:
        g = gdata(interaction.guild_id)
        if not can_fulfill(interaction.user, interaction.guild, g):
            await interaction.response.send_message(
                "Only staff or the reward owner can do that.", ephemeral=True)
            return g, False
        w = find_withdrawal_for_message(g, interaction.message)
        if not w:
            await interaction.response.send_message(
                "Withdrawal not found. Use `.creator withdraw list`.", ephemeral=True)
            return g, False
        if w.get("status") != "pending":
            await interaction.response.send_message(
                f"Already **{w.get('status')}**.", ephemeral=True)
            return g, False
        return g, True

    async def _close_card(self, interaction) -> None:
        try:
            await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success,
                       custom_id="creator:withdraw:accept")
    async def accept_btn(self, interaction: discord.Interaction, _):
        g, ok = await self._guard(interaction)
        if not ok:
            return
        await interaction.response.defer(ephemeral=True)
        w = find_withdrawal_for_message(g, interaction.message)
        res = await fulfill_gold_withdrawal(interaction.guild, g, w, interaction.user)
        if not res.get("ok"):
            await interaction.followup.send(res["error"], ephemeral=True)
            return
        await self._close_card(interaction)
        await interaction.followup.send(f"Accepted `{w['id']}` ✅", ephemeral=True)

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger,
                       custom_id="creator:withdraw:cancel")
    async def cancel_btn(self, interaction: discord.Interaction, _):
        g, ok = await self._guard(interaction)
        if not ok:
            return
        await interaction.response.defer(ephemeral=True)
        w = find_withdrawal_for_message(g, interaction.message)
        res = await cancel_gold_withdrawal(interaction.guild, g, w, interaction.user)
        if not res.get("ok"):
            await interaction.followup.send(res["error"], ephemeral=True)
            return
        await self._close_card(interaction)
        await interaction.followup.send(f"Cancelled `{w['id']}`", ephemeral=True)

    @discord.ui.button(label="Modify", style=discord.ButtonStyle.secondary,
                       custom_id="creator:withdraw:modify")
    async def modify_btn(self, interaction: discord.Interaction, _):
        g, ok = await self._guard(interaction)
        if not ok:
            return
        w = find_withdrawal_for_message(g, interaction.message)
        await interaction.response.send_modal(WithdrawAmountModal(w["id"]))


class WithdrawAmountModal(discord.ui.Modal, title="Gold withdrawal"):
    """Type the amount in-game, either as a new request or an edit."""

    amount = discord.ui.TextInput(label="Amount of Gold", placeholder="e.g. 250",
                                  min_length=1, max_length=7, required=True)

    def __init__(self, wid: str | None = None) -> None:
        super().__init__()
        self.wid = wid

    async def on_submit(self, interaction: discord.Interaction) -> None:
        g = gdata(interaction.guild_id)
        digits = re.sub(r"\D", "", str(self.amount.value))
        if not digits:
            await interaction.response.send_message("Enter a number of Gold.", ephemeral=True)
            return
        value = int(digits)

        if self.wid is None:
            res = await request_gold_withdrawal(interaction.guild, g,
                                                interaction.user, value)
            if not res.get("ok"):
                await interaction.response.send_message(res["error"], ephemeral=True)
                return
            w = find_withdrawal(g, res["wid"])
            if getattr(interaction.message, "id", None):
                g.setdefault("by_withdraw_message", {})[str(interaction.message.id)] = w["id"]
                w["creator_message_id"] = interaction.message.id
                save()
            await interaction.response.send_message(
                f"Requested **{fmt_int(value)} Gold** — `{w['id']}`. "
                "Change or cancel below until the owner accepts.", ephemeral=True)
            try:
                await interaction.message.edit(embed=_creator_withdraw_embed(w),
                                               view=CreatorWithdrawalView())
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass
            return

        w = find_withdrawal(g, self.wid)
        if not w or w.get("status") != "pending":
            await interaction.response.send_message(
                "That request is no longer pending — the amount is locked.", ephemeral=True)
            return
        res = await modify_gold_withdrawal(interaction.guild, g, w, value,
                                           interaction.user)
        if not res.get("ok"):
            await interaction.response.send_message(res["error"], ephemeral=True)
            return
        await interaction.response.send_message(
            f"Updated `{w['id']}` to **{fmt_int(w['amount'])} Gold**.", ephemeral=True)
        try:
            is_owner = can_fulfill(interaction.user, interaction.guild, g)
            embed = (_withdraw_embed(w, "🪙 Gold withdrawal request") if is_owner
                     else _creator_withdraw_embed(w))
            await interaction.message.edit(embed=embed)
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass


class WithdrawStartView(discord.ui.View):
    """One button opens the amount box — no command syntax needed."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Withdraw gold", style=discord.ButtonStyle.primary,
                       emoji="🪙", custom_id="creator:withdraw:start")
    async def start_btn(self, interaction: discord.Interaction, _):
        await interaction.response.send_modal(WithdrawAmountModal())


class CreatorWithdrawalView(discord.ui.View):
    """Creator controls on their own request card until the owner accepts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _guard(self, interaction) -> tuple[dict | None, dict | None]:
        g = gdata(interaction.guild_id)
        w = find_withdrawal_for_message(g, interaction.message)
        if not w or str(w.get("user_id")) != str(interaction.user.id):
            await interaction.response.send_message(
                "That isn't your withdrawal request.", ephemeral=True)
            return g, None
        if w.get("status") != "pending":
            await interaction.response.send_message(
                f"Already **{w['status']}** — the amount is now locked.", ephemeral=True)
            return g, None
        return g, w

    @discord.ui.button(label="Change amount", style=discord.ButtonStyle.primary,
                       custom_id="creator:withdraw:change")
    async def change_btn(self, interaction: discord.Interaction, _):
        g, w = await self._guard(interaction)
        if w is None:
            return
        await interaction.response.send_modal(WithdrawAmountModal(w["id"]))

    @discord.ui.button(label="Cancel request", style=discord.ButtonStyle.danger,
                       custom_id="creator:withdraw:withdraw")
    async def cancel_btn(self, interaction: discord.Interaction, _):
        g, w = await self._guard(interaction)
        if w is None:
            return
        res = await cancel_gold_withdrawal(interaction.guild, g, w, interaction.user)
        if not res.get("ok"):
            await interaction.response.send_message(res["error"], ephemeral=True)
            return
        await interaction.response.send_message(
            f"Cancelled `{w['id']}`. Your gold is available to request again.", ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass


class WithdrawConfirmView(discord.ui.View):
    """Two-step confirm so a stray number can't submit a gold request."""

    def __init__(self) -> None:
        super().__init__(timeout=300)

    def _pending(self, interaction) -> dict | None:
        g = gdata(interaction.guild_id)
        return (g.get("pending_withdrawals") or {}).get(str(interaction.user.id))

    @discord.ui.button(label="Confirm withdrawal", style=discord.ButtonStyle.success,
                       custom_id="creator:withdraw:confirm")
    async def confirm_btn(self, interaction: discord.Interaction, _):
        uid = str(interaction.user.id)
        g = gdata(interaction.guild_id)
        pending = (g.get("pending_withdrawals") or {}).get(uid)
        if not pending or now_ts() - float(pending.get("at") or 0) > 300:
            g.get("pending_withdrawals", {}).pop(uid, None)
            await interaction.response.send_message(
                "That confirmation expired. Run `.creator withdrawgold <amount>` again.",
                ephemeral=True)
            return
        g["pending_withdrawals"].pop(uid, None)
        save()
        res = await request_gold_withdrawal(interaction.guild, g, interaction.user,
                                            pending.get("amount"))
        if res.get("ok"):
            w = find_withdrawal(g, res["wid"])
            if w is not None and getattr(interaction.message, "id", None):
                g.setdefault("by_withdraw_message", {})[str(interaction.message.id)] = res["wid"]
                w["creator_message_id"] = interaction.message.id
                save()
            try:
                await interaction.message.edit(embed=_creator_withdraw_embed(w),
                                               view=CreatorWithdrawalView())
            except (discord.Forbidden, discord.HTTPException, AttributeError):
                pass
        await interaction.response.send_message(
            (f"Submitted `{res['wid']}` for **{fmt_int(res['amount'])} Gold**. "
             "Use the buttons above to change the amount or cancel before the owner accepts."
             if res.get("ok") else res.get("error") or "Could not submit that request."),
            ephemeral=True)
        if not res.get("ok"):
            try:
                await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary,
                       custom_id="creator:withdraw:deny")
    async def cancel_btn(self, interaction: discord.Interaction, _):
        uid = str(interaction.user.id)
        g = gdata(interaction.guild_id)
        g.get("pending_withdrawals", {}).pop(uid, None)
        save()
        await interaction.response.send_message("Withdrawal cancelled.", ephemeral=True)
        try:
            await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ---------- review views ----------

def find_redemption(g: dict, rid: str) -> dict | None:
    return g.get("redemptions", {}).get((rid or "").strip().upper())


async def fulfill_redemption(guild, g: dict, red: dict | None, by=None) -> dict:
    if not red:
        return {"ok": False, "error": "Redemption not found."}
    if red.get("status") != "requested":
        return {"ok": False, "error": f"Already {red.get('status')}."}
    red["status"] = "fulfilled"
    red["fulfilled_by"] = str(getattr(by, "id", by) or "")
    red["fulfilled_at"] = iso_now()
    save()
    user = None
    if guild is not None:
        try:
            user = guild.get_member(int(red.get("user_id") or 0))
        except (TypeError, ValueError):
            user = None
    if user is not None:
        try:
            await user.send(f"Your reward **{red.get('name')}** was delivered. "
                            f"Check in-game. (Request `{red.get('id')}`)")
        except (discord.Forbidden, discord.HTTPException):
            pass
    return {"ok": True, "redemption": red}


async def refund_redemption(guild, g: dict, red: dict | None, by=None) -> dict:
    if not red:
        return {"ok": False, "error": "Redemption not found."}
    if red.get("status") != "requested":
        return {"ok": False, "error": f"Already {red.get('status')}."}
    red["status"] = "refunded"
    red["refunded_by"] = str(getattr(by, "id", by) or "")
    red["refunded_at"] = iso_now()
    balance = add_credits(g, red.get("user_id"), int(red.get("price") or 0),
                          f"refund {red.get('name')} ({red.get('id')})", by=by)
    save()
    return {"ok": True, "balance": balance, "redemption": red}


class ReviewView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _decide(self, interaction: discord.Interaction, approve: bool):
        if not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        g = gdata(interaction.guild_id)
        sid = (g.get("by_review_message") or {}).get(str(interaction.message.id))
        if not sid and interaction.message.embeds:
            footer = interaction.message.embeds[0].footer.text or ""
            m = re.search(r"(VC-[A-Z0-9]{6})", footer)
            sid = m.group(1) if m else None
        sub = g["submissions"].get(sid or "")
        if not sub:
            await interaction.response.send_message("Submission not found — it may be old.", ephemeral=True)
            return
        if sub.get("status") not in ("needs_review", "pending"):
            await interaction.response.send_message(
                f"Already **{sub.get('status')}**.", ephemeral=True)
            return
        if sid in _DECIDING:
            await interaction.response.send_message("Someone is already handling this one.", ephemeral=True)
            return
        _DECIDING.add(sid)
        try:
            await interaction.response.defer()
            if approve:
                await approve_submission(interaction.guild, sub, interaction.user, source="button")
            else:
                await reject_submission(interaction.guild, sub, interaction.user,
                                        "Rejected in staff review.", source="button")
            try:
                await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await interaction.followup.send(
                ("Approved ✅" if approve else "Rejected") + f" — `{sub['id']}`", ephemeral=True)
        finally:
            _DECIDING.discard(sid)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, custom_id="creator:approve")
    async def approve_btn(self, interaction: discord.Interaction, _):
        await self._decide(interaction, True)

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, custom_id="creator:reject")
    async def reject_btn(self, interaction: discord.Interaction, _):
        await self._decide(interaction, False)


def find_redemption_for_message(g: dict, message) -> dict | None:
    """Locate the order a delivery card belongs to, even after a bot restart.

    Older cards only had a ``by_redeem_message`` entry; newer ones also store
    ``message_id`` on the redemption and print the request id in the embed.
    Try all three so the Fulfilled / Refund buttons never dead-end.
    """
    mid = str(getattr(message, "id", "") or "")
    rid = (g.get("by_redeem_message") or {}).get(mid)
    red = g.get("redemptions", {}).get(rid or "")
    if red:
        return red
    if mid:
        for cand in g.get("redemptions", {}).values():
            if str(cand.get("message_id") or "") == mid:
                return cand
    text = " ".join(str(getattr(e, "description", "") or "")
                    for e in (getattr(message, "embeds", None) or []))
    found = re.search(r"\bRD-[0-9A-F]{6}\b", text)
    if found:
        return g.get("redemptions", {}).get(found.group(0))
    # Footer carries the request id on some older cards.
    for e in (getattr(message, "embeds", None) or []):
        footer = getattr(getattr(e, "footer", None), "text", "") or ""
        found = re.search(r"\bRD-[0-9A-F]{6}\b", str(footer))
        if found:
            return g.get("redemptions", {}).get(found.group(0))
    return None


class RedeemView(discord.ui.View):
    """Staff view for fulfilling/refunding a shop purchase delivery card."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _finish(self, interaction: discord.Interaction, fulfill: bool):
        g = gdata(interaction.guild_id)
        if not can_fulfill(interaction.user, interaction.guild, g):
            await interaction.response.send_message(
                "Only staff or the reward owner can do that.", ephemeral=True)
            return
        red = find_redemption_for_message(g, interaction.message)
        if not red:
            await interaction.response.send_message(
                "Order not found. Use `.creator orders` then "
                "`.creator deliver <id>` / `.creator refund <id>`.", ephemeral=True)
            return
        if red.get("status") != "requested":
            await interaction.response.send_message(
                f"Already **{red.get('status')}**.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        res = (await fulfill_redemption(interaction.guild, g, red, interaction.user)
               if fulfill else
               await refund_redemption(interaction.guild, g, red, interaction.user))
        if not res.get("ok"):
            await interaction.followup.send(res.get("error") or "Could not update the order.",
                                            ephemeral=True)
            return
        try:
            await interaction.message.edit(view=None)
        except (discord.Forbidden, discord.HTTPException):
            pass
        await interaction.followup.send(
            f"{'Fulfilled ✅' if fulfill else 'Refunded'} `{red['id']}`", ephemeral=True)

    @discord.ui.button(label="Fulfilled", style=discord.ButtonStyle.success, custom_id="creator:redeem:done")
    async def done_btn(self, interaction: discord.Interaction, _):
        await self._finish(interaction, True)

    @discord.ui.button(label="Refund", style=discord.ButtonStyle.secondary, custom_id="creator:redeem:refund")
    async def refund_btn(self, interaction: discord.Interaction, _):
        await self._finish(interaction, False)


#
class ShopBuyView(discord.ui.View):
    """Persistent buy buttons for the creator shop, visible to everyone."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Buy: In-game cash bundle — 250 credits",
        style=discord.ButtonStyle.primary,
        custom_id="creator:shop:buy:cash",
    )
    async def cash_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_item(interaction, "cash")

    @discord.ui.button(
        label="Buy: Cosmetic crate — 400 credits",
        style=discord.ButtonStyle.primary,
        custom_id="creator:shop:buy:wheel",
    )
    async def wheel_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_item(interaction, "wheel")

    @discord.ui.button(
        label="Buy: Double XP weekend — 1500 credits",
        style=discord.ButtonStyle.primary,
        custom_id="creator:shop:buy:doublexp",
    )
    async def doublexp_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._buy_item(interaction, "doublexp")

    async def _buy_item(self, interaction: discord.Interaction, key: str) -> None:
        g = gdata(interaction.guild_id)
        member = interaction.user
        if member is None or not hasattr(member, "id") or getattr(member, "bot", False):
            await interaction.response.send_message(
                "This can only be used by server members.", ephemeral=True)
            return

        item = find_item(key)
        if not item:
            await interaction.response.send_message("Unknown reward.", ephemeral=True)
            return

        bal = credits(g, member.id)
        if bal < item["price"]:
            await interaction.response.send_message(
                f"Not enough credits ({fmt_int(bal)}/{item['price']}).", ephemeral=True)
            return

        rid = new_id("RD-")
        remaining = add_credits(g, member.id, -item["price"],
                                f"buy {item['name']} ({rid})")
        red = {"id": rid, "user_id": str(member.id), "item": item["id"],
               "name": item["name"], "price": item["price"], "status": "requested",
               "remaining_after": remaining, "owner_notified": False,
               "created_at": now_ts(), "created_at_iso": iso_now()}
        if item["id"] == "wheel":
            prize = roll_wheel()
            red["wheel_tier"] = prize["tier"]
            red["wheel_prize"] = prize["label"]
        g["redemptions"][rid] = red

        # Build delivery embed for the owner
        extra = ""
        if red.get("wheel_tier"):
            extra = f"\nWheel roll: **{red['wheel_tier']}** — grant a {red['wheel_prize']}."

        em = bm.ui.embed(
            f"🛍️ New purchase — {item['name']}",
            f"Buyer: {member.mention}\nItem: **{item['name']}**\n"
            f"Cost: **{fmt_int(item['price'])} credits**\n"
            f"Balance left: **{fmt_int(remaining)} credits**\n"
            f"Request: `{rid}`{extra}\n\n"
            "Deliver it in-game, then press **Delivered**. "
            "Use **Refund** to return the credits.",
            "warning", footer="Wigglesworth • creator rewards • owner action")

        # Ping the owner and remember which card belongs to which order. Without
        # this mapping the Delivered/Refund buttons report "Redemption not found".
        msg = await _notify_owner(
            interaction.guild,
            f"New creator purchase from {member.mention} needs delivery.",
            embed=em,
            view=RedeemView())
        if msg is not None:
            red["message_id"] = msg.id
            red["owner_notified"] = True
            g.setdefault("by_redeem_message", {})[str(msg.id)] = rid
        save()

        # Confirm to the buyer in the channel (visible to all)
        await interaction.response.send_message(
            f"Bought **{item['name']}** for {fmt_int(item['price'])} credits — "
            f"request `{rid}`. Balance left: {fmt_int(remaining)} credits. "
            "Staff will follow up to deliver it.", mention_author=False)


# ---------- decisions ----------

async def approve_submission(guild, sub: dict, staff: discord.abc.User, source="command",
                             note: str = "") -> dict:
    g = gdata(guild.id)
    if sub.get("status") in ("approved", "rejected"):
        return {"ok": False, "error": f"Already {sub['status']}."}
    amount = credits_for(g, sub["user_id"], (sub.get("checks") or {}).get("duration_s"),
                         commit=True)
    sub["status"] = "approved"
    sub["decided_by"] = str(staff.id)
    sub["decided_at"] = now_ts()
    sub["decided_at_iso"] = iso_now()
    sub["source"] = source
    if note:
        sub["staff_note"] = note[:1000]
    sub["credits"] = amount["total"]
    add_credits(g, sub["user_id"], amount["total"],
                f"approved video {sub['id']} {sub.get('title', '')}", by=staff.id)
    if amount["mission"]:
        sub["mission_bonus"] = amount["mission"]
        sub["mission_notes"] = amount["notes"]
    approved_ch = await _channel(guild, "approved_channel")
    if approved_ch:
        msg = await _send(approved_ch, content=showcase_content(sub))
        if msg:
            sub["approved_message_id"] = msg.id
    # Update the original submission message with a receipt.
    sub_ch = await _submission_channel(guild, sub)
    if sub_ch and sub.get("submission_message_id"):
        try:
            original = await sub_ch.fetch_message(int(sub["submission_message_id"]))
            await original.add_reaction("✅")
            await original.reply(
                f"Approved ✅ **+{fmt_int(amount['total'])} Creator Credits** — "
                f"check `.creator progress`.", mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
    save()
    return {"ok": True, "credits": amount["total"], "notes": amount["notes"]}


async def reject_submission(guild, sub: dict, staff: discord.abc.User, reason: str,
                            source="command") -> dict:
    g = gdata(guild.id)
    if sub.get("status") in ("approved", "rejected"):
        return {"ok": False, "error": f"Already {sub['status']}."}
    sub["status"] = "rejected"
    sub["decided_by"] = str(staff.id)
    sub["decided_at"] = now_ts()
    sub["decided_at_iso"] = iso_now()
    sub["staff_note"] = (reason or "Rejected.")[:1000]
    sub["source"] = source
    sub_ch = await _submission_channel(guild, sub)
    if sub_ch and sub.get("submission_message_id"):
        try:
            original = await sub_ch.fetch_message(int(sub["submission_message_id"]))
            await original.add_reaction("❌")
            await original.reply(
                f"Not counted — {sub['staff_note']}", mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
    save()
    return {"ok": True}


# ---------- message pipeline ----------

async def handle_message(message: discord.Message) -> bool:
    """Handle a creator submission. Returns True when the message was consumed."""
    if message.guild is None or message.author.bot:
        return False
    g = gdata(message.guild.id)
    sub_ch = int(g["settings"].get("submissions_channel") or 0)
    if not sub_ch:
        return False
    channel = message.channel
    # Forum channels deliver each post as a thread whose parent is the forum.
    in_forum_post = getattr(channel, "parent_id", None) == sub_ch
    if channel.id != sub_ch and not in_forum_post:
        return False
    content = (message.content or "").strip()
    if content.startswith(PREFIX):
        return False
    if not content:
        return False
    url = extract_url(content)
    if not url:
        return False
    if not has_creator_role(message.author, g):
        await _reply_temp(message, "Only Content Creators can submit here. "
                                  "Apply for the creator role on the applications site first.")
        return True
    await process_submission(message.guild, message.author, url, message, g)
    return True


async def _reply_temp(message: discord.Message, text: str, seconds: int = 12) -> None:
    try:
        sent = await message.reply(text, mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        return

    async def _cleanup():
        await asyncio.sleep(seconds)
        try:
            await sent.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
    asyncio.create_task(_cleanup())


async def process_submission(guild, author: discord.Member, url: str,
                             message: discord.Message | None = None,
                             g: dict | None = None) -> dict:
    g = g or gdata(guild.id)
    platform = platform_for(url)
    if not platform:
        if message:
            await _reply_temp(message, "That link is not a supported video platform. "
                                      "Post a YouTube, TikTok, Twitch, Streamable, X, or Reddit video.")
        return {"ok": False, "error": "unsupported platform"}

    dup = duplicate_of(g, url)
    if dup:
        if message:
            await _reply_temp(message, f"That video was already submitted (`{dup}`).")
        return {"ok": False, "error": "duplicate", "id": dup}

    sid = new_id()
    creator = g["creators"].get(str(author.id), {})
    sub = {
        "id": sid,
        "user_id": str(author.id),
        "url": normalize_url(url),
        "original_url": url,
        "platform": platform,
        "status": "checking",
        "created_at": now_ts(),
        "created_at_iso": iso_now(),
        "submission_message_id": getattr(message, "id", None),
        "submission_channel_id": getattr(message.channel, "id", None),
        "checks": {},
        "reasons": [],
    }
    g["submissions"][sid] = sub
    save()

    if message:
        try:
            await message.add_reaction("⏳")
        except (discord.Forbidden, discord.HTTPException):
            pass

    try:
        checks = await inspect_video(url, creator, g["settings"])
    except Exception as exc:  # noqa: BLE001 - never lose a submission to a check bug
        print(f"[creator] check failed: {type(exc).__name__}: {exc}")
        checks = {"platform": platform, "url": normalize_url(url), "original_url": url,
                  "verified": False, "reasons": ["Automatic check failed — staff look needed."],
                  "warnings": [], "verdict": "review"}
    sub["checks"] = checks
    sub["reasons"] = checks.get("reasons") or []
    sub["title"] = checks.get("title") or ""
    sub["author"] = checks.get("author") or checks.get("channel_title_raw") or ""
    sub["thumbnail"] = checks.get("thumbnail") or ""
    if message:
        try:
            await message.remove_reaction("⏳", guild.me)
        except (discord.Forbidden, discord.HTTPException):
            pass

    auto = bool(g["settings"].get("auto_approve", True)) and checks.get("verdict") == "approve"
    if auto:
        result = await approve_submission(guild, sub, guild.me, source="auto")
        sub = g["submissions"][sid]
        if message:
            try:
                await message.add_reaction("✅")
            except (discord.Forbidden, discord.HTTPException):
                pass
        return {"ok": True, "auto": True, "id": sid, "credits": result.get("credits", 0)}

    sub["status"] = "needs_review"
    review_ch = await _channel(guild, "review_channel")
    if review_ch:
        msg = await _send(review_ch, submission_embed(guild, sub), ReviewView())
        if msg:
            sub["review_message_id"] = msg.id
            g.setdefault("by_review_message", {})[str(msg.id)] = sid
    elif message:
        await _reply_temp(message, "Saved for staff review, but no review channel is set yet. "
                                  "Staff: `.creator reviewchannel #channel`.")
    if message:
        try:
            await message.add_reaction("🕵️")
        except (discord.Forbidden, discord.HTTPException):
            pass
    save()
    return {"ok": True, "auto": False, "id": sid}


# ---------- shop / redeeming ----------

def shop_embed(g: dict | None = None, member=None) -> discord.Embed:
    lines = []
    if g is not None and member is not None:
        lines.append(f"**Your balance:** {fmt_int(credits(g, member.id))} credits\n")
    for i, item in enumerate(REWARD_CATALOG, 1):
        lines.append(f"`{i}` **{item['name']}** — {fmt_int(item['price'])} credits\n"
                     f"*{item['desc']}*")
    em = bm.ui.embed("🛍️ Creator shop", "\n\n".join(lines), "info",
                     footer="Wigglesworth • creator rewards • .creator shop")
    em.add_field(name="How to buy",
                 value="`.creator buy 1` (or `.creator buy cash`). Credits are taken "
                       "immediately and the owner is pinged to deliver in-game.",
                 inline=False)
    odds = " · ".join(f"{p['tier']} {p['chance']}%" for p in WHEEL_PRIZES)
    em.add_field(name="Wheel odds", value=odds + "\nEvery spin is a cosmetic crate.", inline=False)
    em.add_field(name="Double XP weekends",
                 value="Buy any time; staff schedules and announces the next weekend.",
                 inline=False)
    return em


def roll_wheel(rng=None) -> dict:
    """Pick a cosmetic tier using the published weights."""
    rng = rng or random
    roll = rng.uniform(0, 100)
    upto = 0
    for prize in WHEEL_PRIZES:
        upto += prize["chance"]
        if roll < upto:
            return prize
    return WHEEL_PRIZES[-1]


def find_item(key: str) -> dict | None:
    k = (key or "").strip().lower()
    if not k:
        return None
    if k.isdigit():
        idx = int(k) - 1
        return REWARD_CATALOG[idx] if 0 <= idx < len(REWARD_CATALOG) else None
    for item in REWARD_CATALOG:
        if item["id"] == k or item["name"].lower() == k:
            return item
    for item in REWARD_CATALOG:
        if k in item["id"] or k in item["name"].lower():
            return item
    return None


async def redeem(guild, member: discord.Member, key: str) -> dict:
    g = gdata(guild.id)
    item = find_item(key)
    if not item:
        return {"ok": False, "error": "Unknown reward. Use `.creator shop`."}
    bal = credits(g, member.id)
    if bal < item["price"]:
        return {"ok": False, "error": f"Not enough credits ({fmt_int(bal)}/{item['price']}). "
                                      "Post another video or buy a cheaper reward."}
    for red in g["redemptions"].values():
        if (str(red.get("user_id")) == str(member.id)
                and red.get("item") == item["id"] and red.get("status") == "requested"):
            return {"ok": False, "error": "You already have that reward pending. "
                                          "Staff will deliver it soon."}
    rid = "RD-" + uuid.uuid4().hex[:6].upper()
    remaining = add_credits(g, member.id, -item["price"],
                            f"buy {item['name']} ({rid})")
    red = {"id": rid, "user_id": str(member.id), "item": item["id"],
           "name": item["name"], "price": item["price"], "status": "requested",
           "remaining_after": remaining, "owner_notified": False,
           "created_at": now_ts(), "created_at_iso": iso_now()}
    if item["id"] == "wheel":
        prize = roll_wheel()
        red["wheel_tier"] = prize["tier"]
        red["wheel_prize"] = prize["label"]
    g["redemptions"][rid] = red

    extra = ""
    if red.get("wheel_tier"):
        extra = f"\nWheel roll: **{red['wheel_tier']}** — grant a {red['wheel_prize']}."
    em = bm.ui.embed(
        f"🎁 New purchase — {item['name']}",
        f"Buyer: {member.mention}\nItem: **{item['name']}**\n"
        f"Cost: **{fmt_int(item['price'])} credits**\n"
        f"Balance left: **{fmt_int(remaining)} credits**\n"
        f"Request: `{rid}`{extra}\n\n"
        "Deliver it in-game, then press **Delivered**. "
        "Use **Refund** to return the credits.",
        "warning", footer="Wigglesworth • creator rewards • owner action")
    msg = await _notify_owner(
        guild, f"New creator purchase from {member.mention} needs delivery.",
        embed=em, view=RedeemView())
    if msg is not None:
        red["message_id"] = msg.id
        red["owner_notified"] = True
        g.setdefault("by_redeem_message", {})[str(msg.id)] = rid
    save()
    return {"ok": True, "id": rid, "item": item, "wheel": red.get("wheel_tier"),
            "balance": remaining, "owner_notified": red["owner_notified"]}


def leaderboard_embed(guild, g: dict) -> discord.Embed:
    month = month_key()
    counts = {}
    for s in g["submissions"].values():
        if s.get("status") != "approved" or month_key(float(s.get("decided_at") or 0)) != month:
            continue
        uid = str(s.get("user_id"))
        counts[uid] = counts.get(uid, 0) + 1
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    if not rows:
        body = "No approved videos this month yet. Be the first."
    else:
        medals = ["🥇", "🥈", "🥉"]
        body = "\n".join(
            f"{medals[i] if i < 3 else f'{i + 1}.'} <@{uid}> — **{n}** video{'s' if n != 1 else ''}"
            for i, (uid, n) in enumerate(rows))
    return bm.ui.embed(f"🏆 Creator leaderboard — {datetime.now(timezone.utc).strftime('%B %Y')}",
                       body, "info", footer="Wigglesworth • creator rewards")


# ---------- site sync ----------
# The applications site already stores the approved creator's Discord User ID
# and channel link. Pull those once so creators do not have to link manually.
_SYNCED = False


async def sync_site(bot, force: bool = False) -> dict:
    global _SYNCED
    if _SYNCED and not force:
        return {"ok": True, "skipped": True, "linked": 0}
    if not SITE_DATA_URL:
        return {"ok": False, "error": "No site data URL configured."}
    session = aiohttp.ClientSession()
    try:
        data = await _get_json(session, SITE_DATA_URL)
        if not isinstance(data, dict):
            return {"ok": False, "error": "Could not read the applications site data."}
        apps = [a for a in (data.get("submissions") or [])
                if a.get("type") == "creator" and a.get("status") == "approved"]
        linked = 0
        for guild in bot.guilds:
            g = gdata(guild.id)
            for app in apps:
                channel = str(app.get("channel") or "").strip()
                if not channel.startswith("http"):
                    continue
                uid = re.sub(r"\D", "", str(app.get("discordId") or ""))
                if not uid:
                    uid = await _lookup_member_id(guild, str(app.get("discord") or ""))
                if not uid:
                    continue
                doc = g["creators"].setdefault(uid, {})
                if doc.get("channel"):
                    continue
                doc["channel"] = channel[:400]
                doc["channel_name"] = channel_handle(channel)
                doc["linked_at"] = iso_now()
                doc["source"] = "site"
                doc["platform"] = str(app.get("platform") or "")
                linked += 1
        save()
        _SYNCED = True
        return {"ok": True, "linked": linked, "approved": len(apps)}
    finally:
        await session.close()


async def _lookup_member_id(guild, username: str) -> str:
    query = (username or "").strip().lstrip("@").split("#")[0]
    if not query:
        return ""
    member = guild.get_member_named(query)
    if member:
        return str(member.id)
    try:
        found = await guild.query_members(query=query, limit=10)
    except (discord.Forbidden, discord.HTTPException):
        return ""
    want = query.lower()
    exact = [m for m in found
             if m.name.lower() == want or (m.nick or "").lower() == want
             or (m.global_name or "").lower() == want]
    return str(exact[0].id) if len(exact) == 1 else ""


# ---------- command router ----------

HELP = """**Creator commands**
`.creator progress` — your credits, tier, monthly gold, and weekly missions
`.creator link <channel url>` — link the channel you post on
`.creator shop` — rewards and prices (Buy buttons work too)
`.creator buy <1-3>` — spend credits on a reward (owner gets pinged)
`.creator withdrawgold` — button + text box for the gold you want in-game
`.creator withdrawgold status|modify <amount>|cancel` — change or cancel before acceptance
`.creator leaderboard` — top creators this month
`.creator submit <video link>` — submit without posting in the channel

**Staff**
`.creator submitchannel #channel` — where creators post videos
`.creator reviewchannel #channel` — where purchases and unclear videos go
`.creator approvedchannel #channel` — where approved videos are showcased
`.creator setrole @role` — which role may submit and gets monthly gold
`.creator owner @user` — who gets pinged to deliver purchases and gold
`.creator config` — show the current setup
`.creator setmin <seconds>` — shortest video that auto-approves (default 10)
`.creator allowance` — this month's 500 Gold status
`.creator allowance queue` — queue this month now and ping the owner
`.creator allowance paid @user` — mark gold delivered (or `paid all`)
`.creator allowance amount <gold>` — change the monthly amount
`.creator allowance on|off` — toggle the monthly allowance
`.creator orders` — pending and recent shop purchases
`.creator withdraw list` — gold withdrawal requests
`.creator withdraw accept <id>` — accept a gold withdrawal
`.creator withdraw cancel <id>` — cancel a gold withdrawal
`.creator withdraw modify <id> <amount>` — change a gold withdrawal
`.creator deliver <id>` — mark a purchase delivered
`.creator refund <id>` — refund a purchase and return credits
`.creator sync` — pull approved creator channels from the site
`.creator submissions` — latest submissions
`.creator approve <id>` — approve and award credits
`.creator reject <id> [reason]` — reject a submission
`.creator grant @user <credits>` — manual credit adjustment"""


async def cmd_creator(ctx, *, args: str = "") -> None:
    if ctx.guild is None:
        return
    parts = (args or "").strip().split()
    sub = parts[0].lower() if parts else ""
    rest = (args or "").strip()[len(sub):].strip() if sub else ""
    g = gdata(ctx.guild.id)

    if sub in ("", "help"):
        em = bm.ui.embed("🎬 Creator rewards", HELP, "info",
                         footer="Wigglesworth • creator rewards")
        await ctx.reply(embed=em, mention_author=False)
        return

    if sub == "progress":
        if not isinstance(ctx.author, discord.Member):
            return
        await ctx.reply(embed=progress_embed(ctx.guild, g, ctx.author), mention_author=False)
        return

    if sub == "link":
        if not rest:
            await ctx.reply("Usage: `.creator link https://youtube.com/@yourchannel`", mention_author=False)
            return
        url = extract_url(rest)
        if not url or not url.startswith("http"):
            await ctx.reply("Send a full channel URL.", mention_author=False)
            return
        creator = g["creators"].setdefault(str(ctx.author.id), {})
        creator["channel"] = url[:400]
        creator["linked_at"] = iso_now()
        creator["channel_name"] = ""
        save()
        await ctx.reply(f"Linked <{url}> to your creator profile.", mention_author=False)
        return

    if sub == "submit":
        url = extract_url(rest)
        if not url:
            await ctx.reply("Usage: `.creator submit <video link>`", mention_author=False)
            return
        if not has_creator_role(ctx.author, g):
            await ctx.reply("Only Content Creators can submit videos.", mention_author=False)
            return
        res = await process_submission(ctx.guild, ctx.author, url, None, g)
        if not res.get("ok"):
            await ctx.reply(f"Not submitted: {res.get('error')}", mention_author=False)
        elif res.get("auto"):
            await ctx.reply(f"Approved automatically ✅ `{res['id']}` — "
                            f"+{fmt_int(res.get('credits'))} credits.", mention_author=False)
        else:
            await ctx.reply(f"Sent to staff review 🕵️ `{res['id']}`", mention_author=False)
        return

    if sub == "shop":
        await ctx.reply(embed=shop_embed(g, ctx.author), view=ShopBuyView(), mention_author=False)
        return

    if sub == "withdrawgold":
        tail = rest.split()
        action = tail[0].lower() if tail else ""
        uid = str(ctx.author.id)
        available = pending_gold(g, uid)
        if action in ("status", "mine"):
            mine = withdrawals_for(g, uid)
            if not mine:
                await ctx.reply(
                    f"You have **{fmt_int(available)} Gold** available. "
                    "Run `.creator withdrawgold` to request some.", mention_author=False)
                return
            lines = [f"`{w['id']}` — **{fmt_int(w.get('amount'))} Gold** — {w.get('status')}"
                     for w in mine[:10]]
            lines.append(f"**Available:** {fmt_int(available)} Gold")
            await ctx.reply(embed=bm.ui.embed("🪙 Your gold withdrawals", "\n".join(lines),
                                              "gold"), mention_author=False)
            return
        if action in ("modify", "change"):
            w = latest_pending_withdrawal(g, uid)
            if w is None:
                await ctx.reply("You have no pending request to change.", mention_author=False)
                return
            try:
                amount = int(tail[1])
            except (IndexError, ValueError):
                await ctx.reply("Usage: `.creator withdrawgold modify <amount>`",
                                mention_author=False)
                return
            res = await modify_gold_withdrawal(ctx.guild, g, w, amount, ctx.author)
            await ctx.reply(res.get("error") or
                            f"Updated `{w['id']}` to **{fmt_int(w['amount'])} Gold**.",
                            mention_author=False)
            return
        if action in ("cancel", "deny"):
            w = latest_pending_withdrawal(g, uid)
            if w is None:
                await ctx.reply("You have no pending request to cancel.", mention_author=False)
                return
            res = await cancel_gold_withdrawal(ctx.guild, g, w, ctx.author)
            await ctx.reply(res.get("error") or
                            f"Cancelled `{w['id']}`. Your gold is available again.",
                            mention_author=False)
            return
        if not rest:
            if available <= 0:
                await ctx.reply(
                    "You have no gold available this month. Ask staff to queue allowances.",
                    mention_author=False)
                return
            await ctx.reply(
                f"You have **{fmt_int(available)} Gold** available. Press the button and "
                "type how much you want in-game. Nothing is deducted until the owner accepts.",
                view=WithdrawStartView(), mention_author=False)
            return
        try:
            amount = int(tail[0])
        except ValueError:
            await ctx.reply("Amount must be a number, or use the button.",
                            mention_author=False)
            return
        if amount <= 0:
            await ctx.reply("Amount must be greater than 0.", mention_author=False)
            return
        if available <= 0:
            await ctx.reply(
                "You have no gold available this month. Ask staff to queue allowances.",
                mention_author=False)
            return
        if amount > available:
            await ctx.reply(
                f"You only have **{fmt_int(available)} Gold** available to withdraw.",
                mention_author=False)
            return
        g.setdefault("pending_withdrawals", {})[uid] = {
            "amount": amount, "at": now_ts()}
        save()
        await ctx.reply(
            f"Confirm: withdraw **{fmt_int(amount)} Gold** from your pending allowance? "
            "The owner must still accept before anything is delivered or deducted.",
            view=WithdrawConfirmView(), mention_author=False)
        return

    if sub == "withdraw":
        if not can_fulfill(ctx.author, ctx.guild, g):
            await ctx.reply("Only staff or the reward owner can manage withdrawals.",
                            mention_author=False)
            return
        action = parts[1].lower() if len(parts) > 1 else "list"
        if action == "list":
            rows = sorted(g.get("withdrawals", {}).values(),
                          key=lambda w: w.get("requested_at") or "", reverse=True)[:10]
            if not rows:
                await ctx.reply("No gold withdrawal requests yet.", mention_author=False)
                return
            lines = [f"`{w.get('id')}` <@{w.get('user_id')}> — "
                     f"**{fmt_int(w.get('amount'))} Gold** — {w.get('status')}"
                     for w in rows]
            em = bm.ui.embed("🪙 Gold withdrawal requests", "\n".join(lines), "info",
                             footer="Wigglesworth • creator rewards • .creator withdraw")
            await ctx.reply(embed=em, mention_author=False)
            return
        wid = parts[2].upper() if len(parts) > 2 else ""
        w = find_withdrawal(g, wid)
        if w is None:
            await ctx.reply("Unknown request id. Use `.creator withdraw list`.",
                            mention_author=False)
            return
        if action == "accept":
            res = await fulfill_gold_withdrawal(ctx.guild, g, w, ctx.author)
        elif action == "cancel":
            res = await cancel_gold_withdrawal(ctx.guild, g, w, ctx.author)
        elif action == "modify":
            try:
                new_amount = int(parts[3])
            except (IndexError, ValueError):
                await ctx.reply("Usage: `.creator withdraw modify <id> <amount>`",
                                mention_author=False)
                return
            res = await modify_gold_withdrawal(ctx.guild, g, w, new_amount, ctx.author)
        else:
            await ctx.reply("Use `list`, `accept <id>`, `cancel <id>` or "
                            "`modify <id> <amount>`.", mention_author=False)
            return
        verb = {"accept": "Accepted", "cancel": "Cancelled",
                "modify": "Modified"}.get(action, action.title())
        await ctx.reply(res.get("error") or
                        f"{verb} `{wid}` — "
                        f"**{fmt_int(w.get('amount'))} Gold**.",
                        mention_author=False)
        return

    if sub in ("redeem", "buy"):
        if not isinstance(ctx.author, discord.Member):
            return
        if not rest:
            await ctx.reply("Usage: `.creator buy <number>` — see `.creator shop`.",
                            mention_author=False)
            return
        if not has_creator_role(ctx.author, g) and not is_staff(ctx.author):
            await ctx.reply("Only Content Creators can buy rewards.", mention_author=False)
            return
        res = await redeem(ctx.guild, ctx.author, rest.split()[0])
        if res.get("ok"):
            wheel = f" Wheel rolled: **{res['wheel']}**." if res.get("wheel") else ""
            ping = ("Owner has been pinged to deliver it."
                    if res.get("owner_notified") else
                    "Staff will follow up to deliver it.")
            await ctx.reply(
                f"Bought **{res['item']['name']}** for {fmt_int(res['item']['price'])} credits "
                f"— request `{res['id']}`.{wheel}\n"
                f"**Balance left:** {fmt_int(res.get('balance'))} credits. {ping}",
                mention_author=False)
        else:
            await ctx.reply(res.get("error") or "Could not buy that reward.",
                            mention_author=False)
        return

    if sub == "leaderboard":
        await ctx.reply(embed=leaderboard_embed(ctx.guild, g), mention_author=False)
        return

    if sub in ("allowance", "allowances"):
        tail = rest.split()
        action = tail[0].lower() if tail else ""
        month = month_key()
        gold = allowance_gold(g)
        enabled = bool(g["settings"].get("allowance_enabled", True))
        staff = can_fulfill(ctx.author, ctx.guild, g)
        if action in ("", "status", "mine"):
            uid = str(ctx.author.id)
            rec = g.get("allowances", {}).get(uid) or {}
            mine = withdrawals_for(g, uid, month)
            waiting = [w for w in mine if w.get("status") == "pending"]
            delivered = [w for w in mine if w.get("status") == "fulfilled"]
            if rec.get("month") != month:
                state = "not queued yet"
            elif rec.get("status") == "paid":
                state = "paid ✅"
            else:
                state = "queued for the owner ⏳"
            lines = [
                f"**Your monthly gold:** {fmt_int(rec.get('gold') or gold)} Gold — {state}",
                f"**Available to withdraw:** {fmt_int(pending_gold(g, uid))} Gold",
                f"**Pending request:** "
                + (f"`{waiting[0]['id']}` — {fmt_int(waiting[0].get('amount'))} Gold "
                   "(change or cancel with `.creator withdrawgold modify|cancel`)"
                   if waiting else "none"),
                f"**Delivered this month:** "
                + (f"{fmt_int(sum(int(w.get('deducted_amount') or w.get('amount') or 0) for w in delivered))} Gold"
                   if delivered else "none"),
            ]
            if staff:
                records = allowance_month(g, month)
                pending = [r for r in records if r.get("status") == "pending"]
                paid = [r for r in records if r.get("status") == "paid"]
                lines += [
                    "", f"**All creators:** {fmt_int(gold)} Gold each · "
                    f"{'on ✅' if enabled else 'off ❌'} · "
                    f"{len(pending)} pending · {len(paid)} paid",
                ]
                if pending:
                    names = ", ".join(f"<@{r['user_id']}>" for r in pending[:15])
                    lines.append(f"**Waiting on owner:** {names}"
                                 + (f" +{len(pending) - 15} more" if len(pending) > 15 else ""))
            lines.append("Withdrawing is optional: `.creator withdrawgold <amount>`. "
                         "Nothing is deducted until the owner accepts.")
            em = bm.ui.embed("💰 Monthly creator gold", "\n".join(lines), "gold",
                             footer=f"Wigglesworth • creator rewards • {BUILD}")
            await ctx.reply(embed=em, mention_author=False)
            return
        if not staff:
            await ctx.reply("Staff only. Try `.creator allowance` to see your own gold.",
                            mention_author=False)
            return
        if action == "queue":
            new = queue_allowances(ctx.guild, g, month)
            if not new:
                why = "The allowance is off." if not enabled else "Everyone is already queued or paid this month."
                await ctx.reply(f"Nothing to queue. {why}", mention_author=False)
                return
            await notify_allowances(ctx.guild, g, new)
            await ctx.reply(f"Queued **{len(new)}** creator(s) for {fmt_int(gold)} Gold "
                            "each and pinged the owner.", mention_author=False)
            return
        if action == "paid":
            if len(tail) > 1 and tail[1].lower() == "all":
                changed = mark_all_allowances_paid(g, ctx.author.id, month)
                await ctx.reply(f"Marked **{changed}** allowance(s) paid for this month.",
                                mention_author=False)
                return
            targets = ctx.message.mentions if ctx.message else []
            if not targets and len(tail) > 1:
                uid = re.sub(r"\D", "", tail[1])
                member = ctx.guild.get_member(int(uid)) if uid else None
                targets = [member] if member else []
            if not targets:
                await ctx.reply("Usage: `.creator allowance paid @user` or "
                                "`.creator allowance paid all`", mention_author=False)
                return
            changed = sum(1 for user in targets
                          if mark_allowance_paid(g, user.id, ctx.author.id, month))
            await ctx.reply(f"Marked **{changed}** allowance(s) paid.", mention_author=False)
            return
        if action == "amount":
            digits = re.sub(r"\D", "", rest[len(tail[0]):])
            if not digits:
                await ctx.reply("Usage: `.creator allowance amount 500`", mention_author=False)
                return
            amount = max(0, min(int(digits), 1_000_000))
            g["settings"]["allowance_gold"] = amount
            save()
            await ctx.reply(f"Monthly gold is now **{fmt_int(amount)}**. "
                            "It applies to the next queued month.", mention_author=False)
            return
        if action in ("on", "off"):
            g["settings"]["allowance_enabled"] = action == "on"
            save()
            if action == "on":
                new = queue_allowances(ctx.guild, g, month)
                if new:
                    await notify_allowances(ctx.guild, g, new)
                    await ctx.reply(f"Monthly gold is **on** — queued {len(new)} creator(s) "
                                    "and pinged the owner.", mention_author=False)
                    return
            await ctx.reply(f"Monthly gold is **{action}**.", mention_author=False)
            return
        await ctx.reply("Usage: `.creator allowance [status|queue|paid @user|paid all|"
                        "amount 500|on|off]`", mention_author=False)
        return

    # ---- staff-only below ----
    if not is_staff(ctx.author):
        await ctx.reply("Staff only. Try `.creator progress` or `.creator shop`.", mention_author=False)
        return

    if sub in ("submitchannel", "reviewchannel", "approvedchannel"):
        m = re.search(r"<#(\d+)>", rest)
        ch = ctx.message.channel_mentions[0] if ctx.message.channel_mentions else None
        cid = int(m.group(1)) if m else (ch.id if ch else 0)
        if not cid:
            await ctx.reply(f"Usage: `.creator {sub} #channel`", mention_author=False)
            return
        key = {"submitchannel": "submissions_channel", "reviewchannel": "review_channel",
               "approvedchannel": "approved_channel"}[sub]
        g["settings"][key] = cid
        save()
        await ctx.reply(f"Set **{sub}** to <#{cid}>.", mention_author=False)
        return

    if sub in ("config", "settings", "setup"):
        s = g["settings"]

        def _ch(key):
            cid = int(s.get(key) or 0)
            return f"<#{cid}>" if cid else "❌ not set"

        subch = ctx.guild.get_channel(int(s.get("submissions_channel") or 0))
        kind = ""
        if subch is not None:
            kind = " (forum ✅)" if type(subch).__name__ == "ForumChannel" else " (text channel)"
        role = ctx.guild.get_role(int(s.get("role_id") or 0))
        lines = [
            f"**Submissions:** {_ch('submissions_channel')}{kind}",
            f"**Review:** {_ch('review_channel')}",
            f"**Showcase:** {_ch('approved_channel')}",
            f"**Creator role:** {role.mention if role else '❌ not set'}",
            f"**Reward owner:** {('<@' + str(s.get('owner_id')) + '>') if s.get('owner_id') else 'OWNER_IDS / server owner'}",
            f"**Minimum length:** {s.get('min_seconds', 10)}s",
            f"**Auto-approve:** {'on' if s.get('auto_approve', True) else 'off'}",
            f"**Monthly gold:** {fmt_int(allowance_gold(g))} Gold — "
            f"{'on' if s.get('allowance_enabled', True) else 'off'}",
        ]
        missing = [n for n, k in (("submitchannel", "submissions_channel"),
                                  ("reviewchannel", "review_channel"),
                                  ("approvedchannel", "approved_channel"),
                                  ("setrole", "role_id")) if not s.get(k)]
        if missing:
            lines.append("**Still needed:** " + " · ".join(f"`.creator {m}`" for m in missing))
        em = bm.ui.embed("⚙️ Creator setup", "\n".join(lines), "info",
                         footer=f"Wigglesworth • creator rewards • {BUILD}")
        await ctx.reply(embed=em, mention_author=False)
        return

    if sub == "setrole":
        role = ctx.message.role_mentions[0] if ctx.message.role_mentions else None
        if role is None and rest.isdigit():
            role = ctx.guild.get_role(int(rest))
        if role is None:
            await ctx.reply("Usage: `.creator setrole @Content Creator`", mention_author=False)
            return
        g["settings"]["role_id"] = role.id
        save()
        await ctx.reply(f"Creator role set to **{role.name}**.", mention_author=False)
        return

    if sub == "owner":
        if rest.lower() in ("clear", "none", "0"):
            g["settings"]["owner_id"] = 0
            save()
            await ctx.reply("Reward owner cleared — purchases ping OWNER_IDS / the server owner.",
                            mention_author=False)
            return
        member = (ctx.message.mentions[0] if ctx.message and ctx.message.mentions
                  else None)
        if member is None and rest.isdigit():
            member = ctx.guild.get_member(int(rest))
        if member is None:
            current = owner_ids(ctx.guild, g)
            who = ", ".join(f"<@{uid}>" for uid in current) or "not set"
            await ctx.reply(f"Reward owner: {who}. Set one with `.creator owner @user`.",
                            mention_author=False)
            return
        g["settings"]["owner_id"] = member.id
        save()
        await ctx.reply(f"Shop purchases and monthly gold will ping {member.mention}.",
                        mention_author=False)
        return

    if sub == "setmin":
        if not rest.isdigit():
            await ctx.reply("Usage: `.creator setmin <seconds>` (default 10)", mention_author=False)
            return
        secs = max(1, min(int(rest), 3600))
        g["settings"]["min_seconds"] = secs
        g["settings"]["min_seconds_custom"] = True
        save()
        await ctx.reply(f"Videos must now be at least **{secs}s** to auto-approve. "
                        "Shorter ones go to staff review.", mention_author=False)
        return

    if sub in ("orders", "purchases"):
        rows = sorted(g["redemptions"].values(),
                      key=lambda r: r.get("created_at") or 0, reverse=True)[:10]
        if not rows:
            await ctx.reply("No shop purchases yet.", mention_author=False)
            return
        lines = [f"`{r.get('id')}` <@{r.get('user_id')}> — **{r.get('name')}** — "
                 f"{r.get('status')} — {fmt_int(r.get('price'))} credits"
                 for r in rows]
        em = bm.ui.embed("🛍️ Creator orders", "\n".join(lines), "info",
                         footer="Wigglesworth • creator rewards • .creator orders")
        await ctx.reply(embed=em, mention_author=False)
        return

    if sub in ("deliver", "refund"):
        rid = rest.split()[0].upper() if rest else ""
        red = find_redemption(g, rid)
        if red is None:
            await ctx.reply("Unknown order id. Use `.creator orders`.", mention_author=False)
            return
        if sub == "deliver":
            res = await fulfill_redemption(ctx.guild, g, red, ctx.author)
        else:
            res = await refund_redemption(ctx.guild, g, red, ctx.author)
        if res.get("ok"):
            if sub == "deliver":
                await ctx.reply(f"Delivered `{rid}` ✅", mention_author=False)
            else:
                await ctx.reply(f"Refunded `{rid}` — buyer now has "
                                f"{fmt_int(res.get('balance'))} credits.", mention_author=False)
        else:
            await ctx.reply(res.get("error") or "Could not update the order.",
                            mention_author=False)
        return

    if sub == "sync":
        res = await sync_site(ctx.bot, force=True)
        if res.get("ok"):
            await ctx.reply(f"Site sync complete — linked {res.get('linked', 0)} creator channel(s) "
                            f"from {res.get('approved', 0)} approved application(s).", mention_author=False)
        else:
            await ctx.reply(f"Site sync failed: {res.get('error')}. "
                            "Creators can still use `.creator link <url>`.", mention_author=False)
        return

    if sub == "submissions":
        rows = sorted(g["submissions"].values(), key=lambda s: s.get("created_at") or 0, reverse=True)[:10]
        if not rows:
            await ctx.reply("No submissions yet.", mention_author=False)
            return
        lines = [f"`{s['id']}` <@{s['user_id']}> — **{s.get('status')}** — {str(s.get('title') or s.get('url'))[:60]}"
                 for s in rows]
        em = bm.ui.embed("🎬 Latest creator submissions", "\n".join(lines), "info",
                         footer="Wigglesworth • creator rewards")
        await ctx.reply(embed=em, mention_author=False)
        return

    if sub in ("approve", "reject"):
        sid = rest.split()[0].upper() if rest else ""
        target = g["submissions"].get(sid)
        if not target:
            await ctx.reply("Unknown submission id. Use `.creator submissions`.", mention_author=False)
            return
        if sub == "approve":
            res = await approve_submission(ctx.guild, target, ctx.author, source="command")
            if res.get("ok"):
                await ctx.reply(f"Approved `{sid}` — +{fmt_int(res.get('credits'))} credits.",
                                mention_author=False)
            else:
                await ctx.reply(res.get("error") or "Could not approve.", mention_author=False)
        else:
            reason = rest[len(sid):].strip() or "Rejected by staff."
            res = await reject_submission(ctx.guild, target, ctx.author, reason, source="command")
            if res.get("ok"):
                await ctx.reply(f"Rejected `{sid}`.", mention_author=False)
            else:
                await ctx.reply(res.get("error") or "Could not reject.", mention_author=False)
        return

    if sub == "grant":
        if not ctx.message.mentions or len(parts) < 3:
            await ctx.reply("Usage: `.creator grant @user 100`", mention_author=False)
            return
        user = ctx.message.mentions[0]
        try:
            amount = int(parts[-1])
        except ValueError:
            await ctx.reply("Credits must be a number.", mention_author=False)
            return
        bal = add_credits(g, user.id, amount, "staff adjustment", by=ctx.author.id)
        save()
        await ctx.reply(f"{user.mention} now has {fmt_int(bal)} credits.", mention_author=False)
        return

    await ctx.reply(f"Unknown subcommand. Use `.creator help`. (build {BUILD})",
                    mention_author=False)


# ---------- setup ----------

def setup(bot_module) -> None:
    global bm, _bot, PREFIX
    bm = bot_module
    _bot = bm.bot
    PREFIX = getattr(bm, "PREFIX", ".") or "."
    if getattr(_bot, "_creator_ready", False):
        return
    _bot._creator_ready = True
    load()
    _bot.add_view(ReviewView())
    _bot.add_view(RedeemView())
    _bot.add_view(ShopBuyView())
    _bot.add_view(GoldWithdrawalView())
    _bot.add_view(CreatorWithdrawalView())
    _bot.add_view(WithdrawStartView())
    try:
        if not getattr(_bot, "_creator_allowance_task", None):
            _bot._creator_allowance_task = asyncio.create_task(_allowance_loop())
    except RuntimeError:
        pass
    print(f"[creator] loaded build {BUILD} (shop buttons, owner pings, monthly gold, withdrawals)")
    b = _bot

    async def _router(ctx, *, args: str = ""):
        await cmd_creator(ctx, args=args)
    _router.__name__ = "cmd_creator"
    b.command(name="creator")(_router)
