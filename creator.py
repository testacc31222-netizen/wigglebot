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
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlunparse

import aiohttp
import discord

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

# ---------- reward catalog (edit these values after staff approval) ----------
# All amounts are Creator Credits. The shop is intentionally data-driven so the
# catalog can change without touching code.
REWARD_CATALOG = [
    {"id": "cash", "name": "In-game cash bundle", "price": 250, "kind": "In-game",
     "desc": "A bundle of in-game currency delivered by staff."},
    {"id": "crate", "name": "Cosmetic crate", "price": 400, "kind": "Cosmetic",
     "desc": "One cosmetic crate unlocked on your account."},
    {"id": "title", "name": "Custom in-game title", "price": 600, "kind": "Cosmetic",
     "desc": "A custom title above your name for 30 days (staff approved)."},
    {"id": "early", "name": "Early access pass", "price": 900, "kind": "Access",
     "desc": "Play the next update up to 7 days before public launch."},
    {"id": "billboard", "name": "Hub billboard", "price": 1200, "kind": "Feature",
     "desc": "Your channel promoted on an in-game billboard for 7 days."},
    {"id": "adminvideo", "name": "Admin Menu video", "price": 1500, "kind": "Feature",
     "desc": "Your video featured inside the in-game Admin Menu."},
    {"id": "custom", "name": "Custom creator request", "price": 2000, "kind": "Custom",
     "desc": "Work with staff on a small custom cosmetic or NPC idea."},
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

DEFAULTS = {
    "role_id": 0,
    "submissions_channel": 0,
    "review_channel": 0,
    "approved_channel": 0,
    "min_seconds": 60,
    "auto_approve": True,
    "require_linked_channel": True,
    "base_credits": BASE_CREDITS,
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
    for k, v in DEFAULTS.items():
        g["settings"].setdefault(k, v)
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

        if result["duration_s"] is not None and result["duration_s"] < int(settings.get("min_seconds", 60)):
            result["reasons"].append(
                f"Video is shorter than {settings.get('min_seconds', 60)}s — staff check needed.")
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


async def _send(channel, embed=None, view=None, content=None):
    try:
        return await channel.send(content=content, embed=embed, view=view,
                                  allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException) as exc:
        print(f"[creator] send failed: {type(exc).__name__}: {exc}")
        return None


# ---------- review views ----------

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


class RedeemView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _finish(self, interaction: discord.Interaction, fulfill: bool):
        if not isinstance(interaction.user, discord.Member) or not is_staff(interaction.user):
            await interaction.response.send_message("Staff only.", ephemeral=True)
            return
        g = gdata(interaction.guild_id)
        rid = (g.get("by_redeem_message") or {}).get(str(interaction.message.id))
        red = g["redemptions"].get(rid or "")
        if not red:
            await interaction.response.send_message("Redemption not found.", ephemeral=True)
            return
        if red.get("status") != "requested":
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        await interaction.response.defer()
        if fulfill:
            red["status"] = "fulfilled"
            red["fulfilled_by"] = str(interaction.user.id)
            red["fulfilled_at"] = iso_now()
            try:
                await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            user = interaction.guild.get_member(int(red["user_id"]))
            if user:
                try:
                    await user.send(f"Your reward **{red.get('name')}** was delivered. "
                                    f"Check in-game. (Request `{red.get('id')}`)")
                except (discord.Forbidden, discord.HTTPException):
                    pass
            await interaction.followup.send(f"Fulfilled `{red['id']}` ✅", ephemeral=True)
        else:
            red["status"] = "refunded"
            add_credits(g, red["user_id"], int(red.get("price") or 0),
                        f"refund {red.get('name')} ({red.get('id')})", by=interaction.user.id)
            try:
                await interaction.message.edit(view=None)
            except (discord.Forbidden, discord.HTTPException):
                pass
            await interaction.followup.send(f"Refunded `{red['id']}`", ephemeral=True)
        save()

    @discord.ui.button(label="Fulfilled", style=discord.ButtonStyle.success, custom_id="creator:redeem:done")
    async def done_btn(self, interaction: discord.Interaction, _):
        await self._finish(interaction, True)

    @discord.ui.button(label="Refund", style=discord.ButtonStyle.secondary, custom_id="creator:redeem:refund")
    async def refund_btn(self, interaction: discord.Interaction, _):
        await self._finish(interaction, False)


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
        msg = await _send(approved_ch, approved_embed(guild, sub))
        if msg:
            sub["approved_message_id"] = msg.id
    # Update the original submission message with a receipt.
    sub_ch = await _channel(guild, "submissions_channel")
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
    sub_ch = await _channel(guild, "submissions_channel")
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
    if not sub_ch or message.channel.id != sub_ch:
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


async def _reply_temp(message: discord.Message, text: str) -> None:
    try:
        sent = await message.reply(text, mention_author=False,
                                   allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        return
    await asyncio.sleep(12)
    try:
        await sent.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass


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

def shop_embed() -> discord.Embed:
    lines = [f"**{item['name']}** — {item['price']} credits\n*{item['desc']}*"
             for item in REWARD_CATALOG]
    em = bm.ui.embed("🛍️ Creator shop", "\n\n".join(lines), "info",
                     footer="Wigglesworth • creator rewards • .creator redeem <item>")
    em.add_field(name="How it works",
                 value="Redeem with `.creator redeem <id>`. Staff deliver in-game, then mark it fulfilled.",
                 inline=False)
    return em


def find_item(key: str) -> dict | None:
    k = (key or "").strip().lower()
    if not k:
        return None
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
        return {"ok": False, "error": f"Not enough credits ({fmt_int(bal)}/{item['price']})."}
    for red in g["redemptions"].values():
        if (str(red.get("user_id")) == str(member.id)
                and red.get("item") == item["id"] and red.get("status") == "requested"):
            return {"ok": False, "error": "You already have that reward pending."}
    rid = "RD-" + uuid.uuid4().hex[:6].upper()
    red = {"id": rid, "user_id": str(member.id), "item": item["id"],
           "name": item["name"], "price": item["price"], "status": "requested",
           "created_at": now_ts(), "created_at_iso": iso_now()}
    g["redemptions"][rid] = red
    add_credits(g, member.id, -item["price"], f"redeem {item['name']} ({rid})")
    review_ch = await _channel(guild, "review_channel")
    if review_ch:
        em = bm.ui.embed(f"🎁 Redemption — {item['name']}",
                         f"Creator: {member.mention}\nItem: **{item['name']}**\n"
                         f"Cost: **{item['price']} credits**\nRequest: `{rid}`",
                         "warning", footer="Wigglesworth • creator rewards")
        msg = await _send(review_ch, em, RedeemView())
        if msg:
            red["message_id"] = msg.id
            g.setdefault("by_redeem_message", {})[str(msg.id)] = rid
    save()
    return {"ok": True, "id": rid, "item": item}


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
`.creator progress` — your credits, tier, and weekly missions
`.creator link <channel url>` — link the channel you post on
`.creator shop` — rewards you can redeem
`.creator redeem <item>` — spend credits on a reward
`.creator leaderboard` — top creators this month
`.creator submit <video link>` — submit without posting in the channel

**Staff**
`.creator submitchannel #channel` — where creators post videos
`.creator reviewchannel #channel` — where unclear videos go
`.creator approvedchannel #channel` — where approved videos are showcased
`.creator setrole @role` — which role may submit
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
        await ctx.reply(embed=shop_embed(), mention_author=False)
        return

    if sub == "redeem":
        if not isinstance(ctx.author, discord.Member):
            return
        res = await redeem(ctx.guild, ctx.author, rest)
        if res.get("ok"):
            await ctx.reply(f"Redeemed **{res['item']['name']}** — request `{res['id']}`. "
                            f"Staff will deliver it in-game.", mention_author=False)
        else:
            await ctx.reply(res.get("error") or "Could not redeem.", mention_author=False)
        return

    if sub == "leaderboard":
        await ctx.reply(embed=leaderboard_embed(ctx.guild, g), mention_author=False)
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

    await ctx.reply("Unknown subcommand. Use `.creator help`.", mention_author=False)


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
    b = _bot

    async def _router(ctx, *, args: str = ""):
        await cmd_creator(ctx, args=args)
    _router.__name__ = "cmd_creator"
    b.command(name="creator")(_router)
