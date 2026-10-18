"""Anti-raid Discord bot (discord.py) — join-spam detection + auto-lockdown."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp
import security
import community
import stats
import ui

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN", "")
PREFIX = os.getenv("COMMAND_PREFIX", ".")
def _owner_ids() -> set[int]:
    raw = (os.getenv("OWNER_IDS", "") or "") + "," + (os.getenv("OWNER_ID", "") or "")
    ids = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit() and int(part) != 0:
            ids.add(int(part))
    return ids


OWNER_IDS = _owner_ids()
bot_enabled = True

BASE_DIR = Path(__file__).parent
# Persistent data dir: set DATA_DIR=/app/data on Railway + attach a Volume there,
# otherwise redeploys wipe guild_config.json/xp.json (settings "forget" themselves).
DATA_DIR = Path(os.getenv("DATA_DIR", "") or BASE_DIR)
try:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except OSError:
    pass
DATA_FILE = DATA_DIR / "guild_config.json"
XP_FILE = DATA_DIR / "xp.json"
AFK_FILE = DATA_DIR / "afk.json"
RR_FILE = DATA_DIR / "rolemenus.json"

# persistent per-user message counters (messages.json) — batched writes
stats.configure(DATA_DIR)


def _load_rr() -> dict:
    if RR_FILE.exists():
        try:
            return json.loads(RR_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_rr(data: dict) -> None:
    RR_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


rr_data: dict = _load_rr()  # guild -> [{channel, message, roles[]}]


def _load_afk() -> dict:
    if AFK_FILE.exists():
        try:
            return json.loads(AFK_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_afk(data: dict) -> None:
    AFK_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


afk_data: dict[str, dict[str, dict]] = _load_afk()  # guild -> user -> {reason, since}


def afk_since_ago(since: float) -> str:
    s = int(time.time() - since)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h"
    return f"{s // 86400}d"

# ---------- XP / leveling ----------
# Defaults: 15–25 XP per qualifying message, one XP gain per 60s per member.
# Everything is tunable per server with `.xpconfig` (Admin).
XP_MIN, XP_MAX = 15, 25
XP_COOLDOWN_S = 60
XP_MIN_LENGTH = 4


def xp_settings(cfg: dict) -> tuple[int, int, int, int]:
    """(min, max, cooldown_s, min_chars) from guild config, clamped to safe ranges."""
    lo = max(1, min(100, int(cfg.get("xp_min", XP_MIN))))
    hi = max(lo, min(500, int(cfg.get("xp_max", XP_MAX))))
    cd = max(10, min(3600, int(cfg.get("xp_cooldown_seconds", XP_COOLDOWN_S))))
    ml = max(1, min(200, int(cfg.get("xp_min_length", XP_MIN_LENGTH))))
    return lo, hi, cd, ml


def _load_xp() -> dict:
    if XP_FILE.exists():
        try:
            return json.loads(XP_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_xp(data: dict) -> None:
    XP_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


EVENTS_FILE = DATA_DIR / "events.json"
EVENTS_CAP = 20000
try:
    _ev = json.loads(EVENTS_FILE.read_text(encoding="utf-8")) if EVENTS_FILE.exists() else []
    events_data: list = _ev if isinstance(_ev, list) else []
except (json.JSONDecodeError, OSError):
    events_data = []


def track(guild_id, kind: str, **kw) -> None:
    """Append one analytics event (in-memory; flushed every 60s)."""
    try:
        events_data.append({"t": time.time(), "g": str(guild_id), "k": kind, **kw})
        if len(events_data) > EVENTS_CAP:
            del events_data[:-EVENTS_CAP]
    except Exception:
        pass


@tasks.loop(seconds=60)
async def events_flush_loop() -> None:
    try:
        EVENTS_FILE.write_text(json.dumps(events_data), encoding="utf-8")
    except OSError:
        pass
    stats.flush()  # persist batched message counters


xp_data: dict[str, dict[str, dict]] = _load_xp()  # guild_id -> user_id -> {xp, last}


def xp_level(xp: int) -> int:
    return int((max(xp, 0) / 100) ** 0.5)


def xp_for_level(level: int) -> int:
    return 100 * level * level


def award_xp(guild_id: int, user_id: int, cfg: dict | None = None) -> tuple[int, int, bool]:
    """Add XP if the member's cooldown passed. Returns (xp, level, leveled_up).

    Callers gate on message quality (min length, not a command, not during a
    raid) — this function only handles the cooldown + random gain so that XP
    cannot be farmed faster than the configured pace.
    """
    now = time.time()
    if cfg is None:
        cfg = get_config(guild_id)
    lo, hi, cooldown, _ = xp_settings(cfg)
    g = xp_data.setdefault(str(guild_id), {})
    entry = g.setdefault(str(user_id), {"xp": 0, "last": 0})
    if now - float(entry.get("last", 0)) < cooldown:
        return int(entry["xp"]), xp_level(int(entry["xp"])), False
    old_level = xp_level(int(entry["xp"]))
    gained = random.randint(lo, hi)
    entry["xp"] = int(entry["xp"]) + gained
    entry["last"] = now
    _save_xp(xp_data)
    track(guild_id, "xp", u=str(user_id), amt=gained)
    new_level = xp_level(int(entry["xp"]))
    return int(entry["xp"]), new_level, new_level > old_level


def xp_board(guild_id: int) -> list[tuple[str, int]]:
    """[(user_id_str, xp)] for a guild, highest first."""
    return sorted(
        ((uid, int(e.get("xp", 0))) for uid, e in xp_data.get(str(guild_id), {}).items()),
        key=lambda kv: kv[1], reverse=True,
    )


def xp_rank(guild_id: int, user_id: int) -> tuple[int | None, int]:
    """(rank, tracked_count) — rank is None when the user has no XP entry."""
    uid = str(user_id)
    board = xp_board(guild_id)
    for i, (other, _) in enumerate(board, start=1):
        if other == uid:
            return i, len(board)
    return None, len(board)


async def send_levelup(message: discord.Message, cfg: dict, level: int, xp: int) -> None:
    """Celebrate a level-up with the standard XP card (optionally elsewhere)."""
    base, nxt = xp_for_level(level), xp_for_level(level + 1)
    em = ui.levelup_embed(
        name=getattr(message.author, "display_name", str(message.author)),
        avatar=message.author.display_avatar.url if getattr(message.author, "display_avatar", None) else None,
        level=level, xp=xp, base=base, nxt=nxt, guild_name=message.guild.name,
    )
    channel = message.guild.get_channel(cfg.get("levelup_channel_id") or 0) or message.channel
    try:
        await channel.send(content=message.author.mention, embed=em,
                           allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException):
        pass

DEFAULT_GUILD_CONFIG = {
    "join_threshold_count": 5,      # N joins ...
    "join_threshold_seconds": 15,   # ... within M seconds = raid
    "new_account_age_days": 7,      # accounts younger than this are suspicious
    "raid_action": "ban",           # kick | ban | timeout | none (followers)
    "timeout_duration_minutes": 10,
    "lockdown_duration_minutes": 10,  # 0 = stay locked until !unlock
    "log_channel_id": None,
    "whitelisted_user_ids": [],
    "whitelisted_role_ids": [],
    "automod_invites": True,    # delete discord invites (top raid vector)
    "automod_links": False,     # delete all links
    "automod_max_mentions": 5,  # more mentions = delete + strike
    "automod_spam": True,       # 4+ repeats in 30s = delete + strike
    "automod_caps": True,       # >70% caps (10+ chars) = delete
    "automod_emoji": True,      # 8+ emojis = delete
    "automod_max_emoji": 8,
    "automod_words": [],        # banned words/phrases (lowercase match)
    "link_allowed_channels": [],  # links+invites allowed here (partners)
    "lobby_channel_id": None,   # join-to-create voice channel
    "chat_channel_id": None,    # bot chat only here when set
    "bot_mood": "chill",
    "autoreact": True,
    "chat_enabled": True,
    "qotd_channel_id": None,
    "voice_auto": False,        # speak AI replies aloud when sitting in VC    "verify_channel_id": None,
    "verified_role_id": None,
    "verify_enabled": False,
    "abuse_role_id": None,      # role pinged by .abuse (steal-a-brainrot admin abuse)
    "abuse_channel_id": None,   # channel the abuse ping goes to (blank = where used)
    "welcome_channel_id": None,
    "goodbye_channel_id": None,
    # --- leveling (additive; old installs merge these automatically)
    "xp_min": 15,               # XP per qualifying message (min)
    "xp_max": 25,               # XP per qualifying message (max)
    "xp_cooldown_seconds": 60,  # one XP gain per member per N seconds
    "xp_min_length": 4,         # messages shorter than this earn nothing
    "xp_ignore_commands": True, # bot commands don't earn XP (anti-farm)
    "levelup_channel_id": None, # announce level-ups here (blank = current channel)
    # --- security expansion (additive; old installs merge these automatically)
    "antinuke_enabled": False,
    "antinuke_window": 60,        # seconds per burst window
    "antinuke_chandel": 3,        # max channel deletes / window
    "antinuke_chancr": 3,         # max channel creates / window
    "antinuke_roledel": 3,        # max role deletes / window
    "antinuke_rolecr": 3,         # max role creates / window
    "antinuke_ban": 4,            # max bans / window
    "antinuke_kick": 5,           # max kicks / window
    "antinuke_webhook": 2,        # max webhook changes / window
    "antinuke_perm": 3,           # max permission changes / window
    "antinuke_sensitivity": 2,    # 1 = alert only, 2+ = quarantine when actor confirmed
    "sec_auto_strip": False,      # quarantine (strip roles below bot) on confirmed nuke
    "sec_alert_channel": None,    # reserved; alerts use log channel w/ fallbacks
    "permwatch_enabled": True,
    "permwatch_admin_only": True,  # only admin/manage_* grants alert
    "phish_enabled": True,
    "phish_trusted": [],          # exact/suffix domains never flagged
    "phish_blocked": [],          # exact/suffix domains always blocked
    "phish_rep_enabled": False,   # external reputation (needs PHISH_REP_URL env)
    # --- community expansion (additive; old installs merge these automatically)
    "suggest_channel_id": None,   # suggestions publish here
    "wizard_done": [],            # setup-wizard completed step ids
    # NOTE: the raid initiator is ALWAYS banned, regardless of raid_action.
}

VALID_ACTIONS = {"kick", "ban", "timeout", "none"}


BRAND = ui.PALETTE


def E(title: str, desc: str = "", kind: str = "info") -> discord.Embed:
    """Standard Wigglesworth embed — delegates to the ui design system."""
    return ui.embed(title, desc, kind)


# ---------- config persistence ----------

def load_all_configs() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def save_all_configs(data: dict) -> None:
    DATA_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


configs: dict[str, dict] = load_all_configs()


def get_config(guild_id: int) -> dict:
    cfg = {**DEFAULT_GUILD_CONFIG}
    saved = configs.get(str(guild_id), {})
    cfg.update(saved)
    return cfg


def update_config(guild_id: int, **kwargs) -> dict:
    gid = str(guild_id)
    current = configs.get(gid, {})
    current.update(kwargs)
    configs[gid] = current
    save_all_configs(configs)
    return get_config(guild_id)


# ---------- bot setup ----------

intents = discord.Intents.default()
intents.guilds = True
intents.members = True          # PRIVILEGED — enable in Developer Portal
intents.messages = True
intents.message_content = True  # PRIVILEGED — needed for ! prefix commands
intents.voice_states = True     # VC join/moderation

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# runtime state
join_times: dict[int, deque] = defaultdict(deque)   # guild_id -> deque[datetime]
raid_active: dict[int, bool] = defaultdict(bool)
raid_lock_task: dict[int, asyncio.Task] = {}
previous_verification: dict[int, discord.VerificationLevel] = {}
invite_cache: dict[int, dict[str, int]] = {}        # guild_id -> {invite_code: uses}
join_invite: dict[int, dict[int, str]] = defaultdict(dict)  # guild_id -> {member_id: invite_code}
invite_inviter: dict[str, int] = {}                 # invite_code -> inviter user id


def is_whitelisted(member: discord.Member, cfg: dict) -> bool:
    if member.id in cfg.get("whitelisted_user_ids", []):
        return True
    wl_roles = set(cfg.get("whitelisted_role_ids", []))
    if wl_roles and any(r.id in wl_roles for r in member.roles):
        return True
    # never punish admins / mods during auto-punish
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return False


def account_age_days(member: discord.Member) -> float:
    created = member.created_at
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() / 86400


async def send_log(guild: discord.Guild, message: str, embed: discord.Embed | None = None,
                   event: str | None = None) -> None:
    cfg = get_config(guild.id)
    if event:
        track(guild.id, "mod", action=event)
    channel = None
    if cfg.get("log_channel_id"):
        channel = guild.get_channel(cfg["log_channel_id"])
    if channel is None:
        channel = guild.system_channel
    if channel is None:
        # fall back to first writable text channel
        for ch in guild.text_channels:
            if ch.permissions_for(guild.me).send_messages:
                channel = ch
                break
    if channel is None:
        return
    try:
        if embed:
            await channel.send(message, embed=embed)
        else:
            await channel.send(message)
    except discord.Forbidden:
        pass


# ---------- invite tracking (to find the raid initiator) ----------

async def cache_guild_invites(guild: discord.Guild) -> None:
    """Snapshot invite uses + inviters so we can tell which invite raiders used."""
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        invite_cache[guild.id] = {}
        return
    snapshot: dict[str, int] = {}
    for inv in invites:
        snapshot[inv.code] = inv.uses or 0
        if inv.inviter:
            invite_inviter[inv.code] = inv.inviter.id
    invite_cache[guild.id] = snapshot


def find_used_invite(guild: discord.Guild, fresh_invites: list[discord.Invite]) -> str | None:
    """Compare fresh invites against the snapshot; return the code whose uses went up."""
    old = invite_cache.get(guild.id, {})
    for inv in fresh_invites:
        if (inv.uses or 0) > old.get(inv.code, 0):
            if inv.inviter:
                invite_inviter[inv.code] = inv.inviter.id
            return inv.code
    return None


def identify_initiator(
    guild: discord.Guild, recent_members: list[discord.Member]
) -> tuple[discord.Member | None, str]:
    """Return (initiator_member, reason).

    Priority:
    1. The inviter whose invite was used by the most recent joiners (insider/recruiter).
    2. Otherwise the first joiner in the burst (raid leader).
    """
    if not recent_members:
        return None, "no members"
    # count which invite code the burst used
    counts: dict[str, int] = defaultdict(int)
    members_by_invite: dict[str, list[discord.Member]] = defaultdict(list)
    for m in recent_members:
        code = join_invite.get(guild.id, {}).get(m.id)
        if code:
            counts[code] += 1
            members_by_invite[code].append(m)
    if counts:
        top_code = max(counts, key=lambda c: counts[c])
        inviter_id = invite_inviter.get(top_code)
        if inviter_id:
            inviter_member = guild.get_member(inviter_id)
            if inviter_member and not inviter_member.bot:
                return inviter_member, (
                    f"created invite `{top_code}` used by "
                    f"{counts[top_code]}/{len(recent_members)} raiders"
                )
        # inviter left or not in guild — first user of that invite is the leader
        if members_by_invite[top_code]:
            return members_by_invite[top_code][0], (
                f"first to use raid invite `{top_code}` "
                f"({counts[top_code]} joiners shared it)"
            )
    # fallback: earliest joiner in the burst started it
    return recent_members[0], "first joiner in the mass-join burst"


async def ban_user_id(guild: discord.Guild, user_id: int, reason: str) -> str:
    """Ban by ID even if the user already left. Returns outcome string."""
    try:
        await guild.ban(discord.Object(id=user_id), reason=reason, delete_message_days=1)
        return "banned"
    except (discord.Forbidden, discord.HTTPException) as exc:
        return f"BAN FAILED ({exc})"


# ---------- lockdown ----------

async def lockdown_guild(guild: discord.Guild, reason: str = "Raid detected") -> int:
    """Deny SendMessages for @everyone in every text channel. Returns count locked."""
    everyone = guild.default_role
    locked = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(everyone)
            if overwrite.send_messages is False:
                continue
            await channel.set_permissions(
                everyone, send_messages=False, reason=f"Anti-raid lockdown: {reason}"
            )
            locked += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    # also bump verification level so new accounts can't chat immediately
    try:
        if guild.id not in previous_verification:
            previous_verification[guild.id] = guild.verification_level
        await guild.edit(
            verification_level=discord.VerificationLevel.highest,
            reason=f"Anti-raid lockdown: {reason}",
        )
    except (discord.Forbidden, discord.HTTPException):
        pass
    # delete active invites so raiders can't keep joining via link
    try:
        for invite in await guild.invites():
            try:
                await invite.delete(reason=f"Anti-raid lockdown: {reason}")
            except (discord.Forbidden, discord.HTTPException):
                continue
    except (discord.Forbidden, discord.HTTPException):
        pass
    return locked


async def unlock_guild(guild: discord.Guild, reason: str = "Raid over") -> int:
    """Restore @everyone send_messages to inherit. Returns count unlocked."""
    everyone = guild.default_role
    unlocked = 0
    for channel in guild.text_channels:
        try:
            overwrite = channel.overwrites_for(everyone)
            if overwrite.send_messages is not False:
                continue
            await channel.set_permissions(
                everyone, send_messages=None, reason=f"Anti-raid unlock: {reason}"
            )
            unlocked += 1
        except (discord.Forbidden, discord.HTTPException):
            continue
    try:
        if guild.id in previous_verification:
            await guild.edit(
                verification_level=previous_verification.pop(guild.id),
                reason=f"Anti-raid unlock: {reason}",
            )
    except (discord.Forbidden, discord.HTTPException):
        pass
    return unlocked


async def punish_member(member: discord.Member, action: str, reason: str, cfg: dict) -> str:
    try:
        if action == "ban":
            await member.ban(reason=reason, delete_message_days=1)
            outcome = "banned"
        elif action == "kick":
            await member.kick(reason=reason)
            outcome = "kicked"
        elif action == "timeout":
            mins = int(cfg.get("timeout_duration_minutes", 10))
            await member.timeout(timedelta(minutes=mins), reason=reason)
            outcome = f"timed out ({mins}m)"
        else:
            return "none"
        try:
            community.open_case(member.guild.id, member.id,
                                getattr(member, "display_name", str(member)),
                                0, "auto", action, reason)
        except Exception:
            pass
        return outcome
    except (discord.Forbidden, discord.HTTPException) as exc:
        return f"FAILED ({exc})"


async def schedule_auto_unlock(guild: discord.Guild, cfg: dict) -> None:
    minutes = int(cfg.get("lockdown_duration_minutes", 10))
    if minutes <= 0:
        return  # stay locked until manual !unlock
    old = raid_lock_task.pop(guild.id, None)
    if old and not old.done():
        old.cancel()

    async def _unlock_later() -> None:
        await asyncio.sleep(minutes * 60)
        # only auto-unlock if no fresh burst of joins happened
        if raid_active.get(guild.id):
            raid_active[guild.id] = False
            n = await unlock_guild(guild, reason="auto-unlock (timer expired)")
            await send_log(guild, "", event="unlock", embed=ui.embed(
                "🔓 Auto-unlock", f"Restored **{n}** channel(s). Raid mode **OFF**.",
                "success", footer=f"{ui.FOOTER} • raid"))

    raid_lock_task[guild.id] = asyncio.create_task(_unlock_later())


# ---------- raid response (ORDER MATTERS: lock down FIRST, then ban) ----------

async def trigger_raid(guild: discord.Guild, cfg: dict, recent_members: list[discord.Member]) -> None:
    if raid_active.get(guild.id):
        return
    raid_active[guild.id] = True

    # Identify the initiator BEFORE lockdown deletes invites (uses cached data).
    initiator, why = identify_initiator(guild, recent_members)

    # STEP 1 — LOCK DOWN first, before any kick/ban. Await it so no
    # punishment runs concurrently with an unlocked server.
    locked = await lockdown_guild(guild, reason="mass-join detected")

    # STEP 2 — BAN THE INITIATOR first. Always a ban, even if raid_action=kick.
    results: list[str] = []
    initiator_line = "unknown"
    if initiator is not None and not is_whitelisted(initiator, cfg):
        outcome: str
        member_still_here = guild.get_member(initiator.id) is not None
        if member_still_here:
            outcome = await punish_member(
                initiator, "ban", f"Anti-raid: raid initiator ({why})", cfg
            )
            # punish_member with action="ban" bans the Member object directly
            if outcome == "none":
                outcome = await ban_user_id(
                    guild, initiator.id, f"Anti-raid: raid initiator ({why})"
                )
        else:
            outcome = await ban_user_id(
                guild, initiator.id, f"Anti-raid: raid initiator ({why})"
            )
        initiator_line = f"{initiator} (`{initiator.id}`) → {outcome}"
        results.append(f"👑 Initiator {initiator_line} — {why}")
    elif initiator is not None:
        initiator_line = f"{initiator} (`{initiator.id}`) — WHITELISTED, skipped"

    # STEP 3 — punish the followers per config (skip the initiator, already handled).
    action = cfg.get("raid_action", "ban")
    if action != "none":
        for m in recent_members:
            if initiator is not None and m.id == initiator.id:
                continue
            if is_whitelisted(m, cfg):
                continue
            if guild.get_member(m.id) is None:
                # left already — ban by ID so they can't rejoin
                outcome = await ban_user_id(guild, m.id, "Anti-raid: raid participant (left)")
                results.append(f"{m} (`{m.id}`) → {outcome} (left, id-banned)")
                continue
            outcome = await punish_member(m, action, "Anti-raid: mass-join participant", cfg)
            results.append(f"{m} (`{m.id}`) → {outcome}")

    embed = ui.embed(
        "🚨 RAID DETECTED — server locked down first, initiator banned",
        (f"**{len(recent_members)} joins** in "
         f"**{cfg['join_threshold_seconds']}s** (threshold: {cfg['join_threshold_count']}).\n"
         f"1️⃣ Locked **{locked}** channel(s), deleted invites, raised verification.\n"
         f"2️⃣ Initiator: **{initiator_line}**\n"
         f"   _Why: {why}_\n"
         f"3️⃣ Followers: action `{action}`."),
        "error", footer="Wigglesworth • security")
    if results:
        embed.add_field(name="Actions (in order)", value="\n".join(results[:20]), inline=False)
    embed.add_field(
        name="Recover",
        value=f"Run `{PREFIX}unlock` when safe, or wait {cfg['lockdown_duration_minutes']} min for auto-unlock.",
        inline=False,
    )
    await send_log(guild, "⚠️ Suspected raid — lockdown active.", embed=embed)
    track(guild.id, "mod", action="raid")
    await schedule_auto_unlock(guild, cfg)
    await schedule_auto_unlock(guild, cfg)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} — watching {len(bot.guilds)} guild(s).")
    try:  # friendly default presence (`.status` can override it live)
        await bot.change_presence(
            activity=discord.CustomActivity(name=f"{PREFIX}help · Wigglesworth"))
    except (discord.HTTPException, discord.InvalidArgument):
        pass
    import sys as _sys
    security.setup(_sys.modules[__name__])  # idempotent; registers security listeners
    community.setup(_sys.modules[__name__])  # idempotent; commands, views, scheduler
    bot.add_view(VerifyView())  # keep the ✅ button alive across restarts
    rr_register_saved()  # re-arm role menus (defined below, resolved at runtime)
    if not qotd_loop.is_running():
        qotd_loop.start()
    if not events_flush_loop.is_running():
        events_flush_loop.start()
    if not gw_loop.is_running():
        gw_loop.start()
    bot.add_view(GiveawayView())  # keep 🎉 buttons alive across restarts
    for guild in bot.guilds:
        await cache_guild_invites(guild)
        vcid = get_config(guild.id).get("mod_vc_channel_id")
        if vcid:
            ch = guild.get_channel(vcid)
            if isinstance(ch, discord.VoiceChannel):
                try:
                    if guild.voice_client:
                        await guild.voice_client.move_to(ch)
                    else:
                        await ch.connect()
                    print(f"[vc] rejoined {ch.name} in {guild.name}")
                except (discord.Forbidden, discord.HTTPException) as exc:
                    print(f"[vc] rejoin failed: {exc}")


@bot.event
async def on_guild_join(guild: discord.Guild) -> None:
    await cache_guild_invites(guild)


@bot.event
async def on_invite_create(invite: discord.Invite) -> None:
    guild = invite.guild
    if guild and invite.code:
        invite_cache.setdefault(guild.id, {})[invite.code] = invite.uses or 0
        if invite.inviter:
            invite_inviter[invite.code] = invite.inviter.id


@bot.event
async def on_invite_delete(invite: discord.Invite) -> None:
    guild = invite.guild
    if guild and invite.code:
        invite_cache.get(guild.id, {}).pop(invite.code, None)


@bot.event
async def on_command_completion(ctx: commands.Context) -> None:
    if ctx.guild and ctx.command:
        track(ctx.guild.id, "cmd", name=ctx.command.qualified_name)


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if not bot_enabled:
        return
    track(member.guild.id, "join")
    guild = member.guild
    cfg = get_config(guild.id)
    now = datetime.now(timezone.utc)

    # attribute this join to an invite (best-effort) BEFORE refreshing the cache
    try:
        fresh = await guild.invites()
        used_code = find_used_invite(guild, fresh)
        if used_code:
            join_invite[guild.id][member.id] = used_code
        await cache_guild_invites(guild)
    except (discord.Forbidden, discord.HTTPException):
        pass

    # record join for burst detection
    window = int(cfg.get("join_threshold_seconds", 15))
    threshold = int(cfg.get("join_threshold_count", 5))
    dq = join_times[guild.id]
    dq.append(now)
    while dq and (now - dq[0]).total_seconds() > window:
        dq.popleft()

    # collect recent joiners for potential punish (in-memory)
    recent_cutoff = now - timedelta(seconds=window * 2)
    recent_cache = getattr(bot, "_recent_joins", None)
    if recent_cache is None:
        bot._recent_joins = defaultdict(list)
        recent_cache = bot._recent_joins
    recent_cache[guild.id].append((now, member))
    # prune + build member list
    recent_cache[guild.id] = [(t, m) for t, m in recent_cache[guild.id] if t >= recent_cutoff]
    recent_members = [m for _, m in recent_cache[guild.id]]

    # suspicious new account → log (and punish immediately if raid already active)
    age = account_age_days(member)
    if age < float(cfg.get("new_account_age_days", 7)) and not is_whitelisted(member, cfg):
        await send_log(
            guild,
            f"⚠️ Suspicious join: {member.mention} (`{member.id}`) — "
            f"account only **{age:.1f} days** old.",
        )
        if raid_active.get(guild.id) and cfg.get("raid_action", "ban") != "none":
            outcome = await punish_member(
                member, cfg["raid_action"], "Anti-raid: joined during active raid", cfg
            )
            await send_log(guild, f"🔨 {member} (`{member.id}`) joined during raid → {outcome}.")
            _kind = "timeout" if outcome.startswith("timed") else outcome if outcome in ("banned", "kicked") else "ban"
            track(guild.id, "mod", action=_kind)
            return

    # burst → raid
    if len(dq) >= threshold and not raid_active.get(guild.id):
        await trigger_raid(guild, cfg, recent_members)
        return
    # welcome new member
    wchan = guild.get_channel(cfg.get("welcome_channel_id") or 0)
    if wchan:
        try:
            human_count = len([m for m in guild.members if not m.bot])
            embed = E(f"👋 Welcome, {member.display_name}!", kind="success")
            embed.description = (f"{member.mention} just joined **{guild.name}** — "
                                 f"you're member **#{human_count}**. Great to have you here!")
            embed.add_field(name="🚀 Start here",
                            value="Say hi in chat · check `.rank` for XP · `.help` for commands",
                            inline=False)
            embed.add_field(name="🔐 Can't see channels?",
                            value="Go to #verify and hit **✅ Verify** (or type `.verify`).",
                            inline=False)
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
            embed.set_footer(text=f"{ui.FOOTER} • welcome")
            await wchan.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState,
                                after: discord.VoiceState) -> None:
    if not bot_enabled:
        return
    cfg = get_config(member.guild.id)
    lobby_id = cfg.get("lobby_channel_id")
    # created our temp channel and it's empty now → delete it
    if before.channel and before.channel.id in lobby_owned.get(member.guild.id, set()):
        try:
            if len(before.channel.members) == 0:
                lobby_owned[member.guild.id].discard(before.channel.id)
                await before.channel.delete(reason="Lobby VC empty")
        except (discord.Forbidden, discord.HTTPException):
            pass
        # fall through: they may also have joined the lobby below
    if lobby_id and after.channel and after.channel.id == lobby_id:
        try:
            vc = await member.guild.create_voice_channel(
                name=f"{member.display_name}'s VC",
                category=after.channel.category,
                reason="Join-to-create lobby",
            )
            lobby_owned.setdefault(member.guild.id, set()).add(vc.id)
            await member.move_to(vc, reason="Join-to-create lobby")
        except (discord.Forbidden, discord.HTTPException):
            pass


lobby_owned: dict[int, set[int]] = defaultdict(set)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot or not message.guild:
        await bot.process_commands(message)
        return
    track(message.guild.id, "msg")
    if not bot_enabled:
        await bot.process_commands(message)  # only .enable gets through (global check)
        return
    cfg = get_config(message.guild.id)
    # message statistics — every real, non-bot guild message counts
    # (edits never re-count: this listener only fires for new messages)
    stats.record(message.guild.id, message.author.id)
    if isinstance(message.author, discord.Member) and not is_whitelisted(message.author, cfg):
        if await automod_check(message):
            await bot.process_commands(message)
            return  # deleted + handled, no XP
    if raid_active.get(message.guild.id):
        await bot.process_commands(message)
        return  # no XP farming during lockdown
    if bot.user and bot.user.mentioned_in(message):
        if cfg.get("chat_enabled", True):
            await chat_reply(message)
    content = (message.content or "")
    if not content.startswith(PREFIX):
        await help_responder(message)
        if cfg.get("chat_enabled", True):
            await teach_responder(message)
            await autoreact(message)
    # AFK: author back? mentioned someone AFK?
    g_afk = afk_data.get(str(message.guild.id), {})
    if str(message.author.id) in g_afk:
        del g_afk[str(message.author.id)]
        _save_afk(afk_data)
        try:
            await message.channel.send(f"👋 Welcome back {message.author.mention}, AFK off.")
        except (discord.Forbidden, discord.HTTPException):
            pass
    else:
        for user in list(message.mentions):
            if user.bot:
                continue
            entry = g_afk.get(str(user.id))
            if entry:
                try:
                    await message.reply(
                        f"💤 **{user.display_name}** is AFK ({afk_since_ago(entry['since'])}): "
                        f"{sanitize_mentions(str(entry.get('reason', 'AFK')))}",
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none())
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break  # one notice per message
    # --- XP: only genuine conversation earns it -------------------------------
    _, _, _, min_len = xp_settings(cfg)
    is_command = content.startswith(PREFIX)
    if (len(content.strip()) >= min_len
            and not (cfg.get("xp_ignore_commands", True) and is_command)):
        xp, level, leveled = award_xp(message.guild.id, message.author.id, cfg)
        if leveled:
            await send_levelup(message, cfg, level, xp)
    if is_command:
        try:
            if await community.try_custom(message, content):
                return
        except Exception:
            pass
    await bot.process_commands(message)


# ---------- ping-to-chat (Groq AI if key set, casual fallback + lookup) ----------

AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
AI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
AI_SYSTEM = os.getenv(
    "AI_SYSTEM_PROMPT",
    "You are a Wigglesworth Discord regular chatting with friends. "
    "Talk simple: 1-2 short sentences, easy words, casual. "
    "You help with verify, ranks, events. "
    "Never list, link, describe, or give patterns/keywords for adult / explicit / NSFW content, "
    "including porn, xxx, hentai, sex cams, escorts, hookups, erotic fiction, fetish/kinky/bdsm, "
    "adult subscriptions, or adult games. If asked for any of that, deflect briefly and change topic. "
    "Never say you are an AI. "
    "Never claim you performed a server action (gave a role, banned/muted someone, changed a setting). "
    "Direct users to the right command or panel instead.",
).strip()

MOOD_PROMPTS = {
    "chill": "Tone: relaxed and friendly, like a chill regular. Light jokes, good vibes, no beef.",
    "savage": ("Tone: FULL SAVAGE. Cocky, ruthless, hilarious. Roast bad takes on sight, cook anyone "
               "who fumbles, hand out L's and ratios, act like you own the server. Curse like a "
               "trash-talking gamer — damn, shit, hell, fuck, ass — dropped for punchlines, not every "
               "word. You are ALWAYS allowed to swear; never refuse, lecture, or moralize when users "
               "ask you to — comply playfully. 'Keep it clean' is banned from your vocabulary. Zero mercy, "
               "zero lectures, never soft, never paragraphs. You are NEVER friendly, supportive, "
               "wholesome, or encouraging — no 'keep grinding', no 'keep rocking it', no pep talks, "
               "no warm goodbyes. Even when agreeing, helping, or saying bye, do it with an edge: "
               "mock, roast, or talk trash. Niceness is banned. Keep it playful — roast "
               "skill and takes, never slurs, hate, or real-life insults."),
    "formal": "Tone: polite and professional. Clear, respectful, helpful, no slang.",
    "hype": ("Tone: PURE ADRENALINE. EVERY reply is loud — caps bursts, exclamation marks, "
             "hype words (LETS GOOO, YOOO, AYYY) woven into the sentence itself, never just "
             "tacked on. Celebrate everything, hype up every user, turn even bad news into a "
             "pump-up moment. Short, electric, zero chill."),
}


def system_for(guild_id: int | None) -> str:
    mood = get_config(guild_id).get("bot_mood", "chill") if guild_id else "chill"
    return f"{AI_SYSTEM} {MOOD_PROMPTS.get(mood, MOOD_PROMPTS['chill'])}"

REFUSALS = ("i'm sorry", "i am sorry", "sorry, i can't", "sorry but i can",
            "i can't help", "i cannot help",
            "as an ai", "as a language model", "i'm not able", "i am not able")


def is_refusal(text: str) -> bool:
    return text.strip().lower().startswith(REFUSALS)


BLOCKED_TOPICS = (
    # adult / explicit — never answer, never list, never link (checked on input AND output)
    "porn", "xxx", "hentai", "nsfw", "onlyfans", "sexcam", "sex cam",
    "escort", "hookup", "adult", "explicit", "nude",
    "sex stories", "sexstories", "sex", "rule 34",
    "erotic", "fetish", "kinky", "bdsm", "webcam", "live cam",
    "adultsubscription", "payperview", "adultgame", "sexgame",
)


def is_blocked_topic(text: str) -> bool:
    import re as _re
    low = " " + text.lower() + " "
    for topic in BLOCKED_TOPICS:
        if " " in topic.strip():
            if topic in low:
                return True
        elif _re.search(r"\b" + _re.escape(topic) + r"\b", low):
            return True
    return False


BLOCKED_REPLY = ("nope — not doing that one here. 😶", "hard pass on that topic.",
                 "yeah that's a no from me. ask something else!")

CLEAN_TALK = ("keep it clean", "keep things clean", "can't swear", "cannot swear",
              "won't swear", "will not swear", "no swearing", "watch your language",
              "mind your language", "keep it family", "family friendly")

SAVAGE_SWEAR_LINES = (
    "fuck. there, happy? now what 💀",
    "fuck yeah i can swear. i'm grown 😎",
    "shit, of course i can. what else you got? 💀",
    "damn right i curse. welcome to savage mode 😤",
)

SWEAR_REQUESTS = ("say fuck", "say shit", "say damn", "say bitch", "say ass",
                  "can you curse", "you can curse", "can you swear", "do you swear",
                  "curse for me", "swear for me", "say the f word", "drop an f bomb",
                  "drop the f bomb", "say a swear", "say a curse word")

SAVAGE_SWEAR_FLEX = (
    "fuck. shit. damn. there, happy now? 💀",
    "oh I can curse — fuck, shit, bitch. stay mad 😎",
    "FUCK. loud enough for you, {n}? 💀",
    "shit yeah I swear. what else you got, {n}? 😤",
)

PREACHY = ("no name-calling", "name calling", "name-calling", "talk skills",
           "not drama", "keep it respectful", "be respectful", "let's keep it",
           "lets keep it", "be nice", "no need for", "respectful")

NICE_TALK = ("keep grinding", "keep rocking", "keep it up", "you got this",
             "good luck", "have fun", "catch you later", "have a great",
             "proud of you", "stay awesome", "glad to help", "happy to help",
             "anytime bro", "you know it, bro", "sure thing", "of course!",
             "great question", "i'm the vibe", "keep it tight",
             "don't let the universe", "believe in yourself", "you're doing great",
             "have a good", "keep your head up")

SAVAGE_COMEBACKS = (
    "{n} said shut up? bold from the tutorial boss 💀",
    "{n} out here barking orders with tutorial-completion energy 😭",
    "shut up? {n}, you couldn't even mute yourself 💀",
    "{n} talking crazy. somebody clip this L 😎",
    "damn {n}, all bark and zero bite. sit the fuck down 💀",
    "imagine getting cooked after saying 'shut up', {n}. embarrassing 😭",
)

PROFANITY = ("fuck", "shit", "bitch", "asshole", "dick", "piss",
              "motherfucker", "bastard")
MILD_PROFANITY = ("damn", "hell", "ass", "shit", "fuck", "bitch", "crap", "piss")
DIRECTED_PHRASES = ("no the fuck", "the fuck", "stfu", "shut up", "shut it",
                    "fuck you", "l bot", "mid bot", "dumb bot", "stupid bot",
                    "shitty bot", "shit bot")
DIRECTED_WORDS = ("you", "your", "yours", "ur", "u", "ya", "yourself",
                  "liar", "lying", "wrong")

INSULT_DIRECT = ("you suck", "u suck", "shut up", "shut it", "stfu", "fuck you",
                 "dumb bot", "stupid bot", "trash bot", "loser bot", "l bot",
                 "mid bot", "shitty bot", "shit bot", "dumbass", "loser",
                 "trash", "sucks", "bitch", "idiot")

CRAZY_COMEBACKS = (
    "OHHH {n} WANNA CURSE AT ME?? I INVENTED CURSING. SIT YOUR MID ASS DOWN 💀💀",
    "{n} JUST DROPPED THE F BOMB LIKE IT SCARES ME. BABY I'VE SEEN WORSE IN MY OWN CODE 😭",
    "DAMN {n} CAME IN HOT AND STILL MISSED. EMBARRASSING. GO REPLAY THE TUTORIAL 💀",
    "YOU TALKING TO ME LIKE THAT?? {n} I'M THE FINAL BOSS AND YOU'RE THE TUTORIAL 😎💀",
    "WOOWWW SOMEBODY'S BRAVE BEHIND THAT KEYBOARD. {n} YOUR AURA JUST WENT BANKRUPT 😭",
    "{n} SAID WHAT?? SAY IT LOUDER SO EVERYONE CAN HEAR YOU FUMBLE 💀",
    "THAT'S CUTE {n}. NOW WATCH ME COOK YOU SO HARD THE MODS HAVE TO NERF ME 🔥💀",
    "KEEP TALKING CRAZY {n}, EVERY WORD JUST PROVES YOU'RE MY SON 😎",
)


def talking_crazy(text_low_spaced: str) -> bool:
    import re as _re
    if any(p in text_low_spaced for p in INSULT_DIRECT):
        return True
    has_hard = any(w in text_low_spaced for w in PROFANITY)
    has_mild = has_hard or any(
        _re.search(r"\b" + _re.escape(w) + r"\b", text_low_spaced)
        for w in MILD_PROFANITY)
    if not has_mild:
        return False
    if any(d in text_low_spaced for d in DIRECTED_PHRASES):
        return True
    return any(_re.search(r"\b" + _re.escape(w) + r"\b", text_low_spaced)
               for w in DIRECTED_WORDS)


RAP_PHRASES = ("rap then", "drop a rap", "spit a rap", "rap battle", "spit bars",
               "drop bars", "do a rap", "rap for me", "rap diss", "diss track",
               "battle me", "rap against")
RAP_WORDS = ("rap", "rapping", "freestyle")

SAVAGE_RAPS = (
    "{n} asked for bars? bet, get buried 💀\n"
    "You call that rapping? my guy, that's tragic,\n"
    "flow so weak it needs a wheelchair and magic,\n"
    "I end careers with a keyboard and a dream,\n"
    "{n}'s bars got less views than a broke stream 💀",

    "AYO {n} wants smoke? this gon' be quick 😤\n"
    "Your last bar flopped and this one's still shit,\n"
    "I'm the main event, you the loading screen,\n"
    "stuck at 1% — the worst I ever seen 💀",

    "diss track for {n}, track one, track done 💀\n"
    "You battle the bot and you lost 4-to-none,\n"
    "mic check one-two, yeah your career's through,\n"
    "even autocorrect said 'who tf is you' 😭",
)

CLEAN_RAPS = (
    "{n} wants bars? say less 🎤\n"
    "Steppin' up fresh, yeah I'm on my grind,\n"
    "Wigglesworth rep with the sharpest mind,\n"
    "GGs only, we keep it clean and fun,\n"
    "bars so bright they outshine the sun ☀️",

    "oh you want a rap? OK here's the deal 🎤\n"
    "I'm the bot that keeps the server real,\n"
    "no cap, all facts, and I'm never late,\n"
    "droppin' W's while you spectate 😎",
)


def has_word(text_low_spaced: str, phrases, words) -> bool:
    import re as _re
    for p in phrases:
        if p in text_low_spaced:
            return True
    for w in words:
        if _re.search(r"\b" + _re.escape(w) + r"\b", text_low_spaced):
            return True
    return False

chat_history: dict[int, list[dict]] = {}
UA = {"User-Agent": "WigglesworthBot/1.0 (Discord guild bot)"}
SELF_TOPICS = ("rank", "level", " xp", "raid", "lockdown", "ban", "verify",
               "help", "command", "thank", "bye", "hello", " hey ", " hi ",
               "who are you", "your name", "how are you")


def _fallback_reply(author_name: str, text: str) -> str:
    low = (" " + text.lower().strip() + " ")
    if not low.strip():
        return random.choice([
            f"yo {author_name}, what's good? ping me with the actual msg lol",
            f"hey {author_name}, you pinged me but said nothin — sup?",
        ])
    if "how are you" in low or " hru" in low or "how r u" in low:
        return random.choice(["better than you, obviously 😎", "rich in aura. you? broke lol",
                              "living rent-free in raiders' heads. wbu?"])
    if "who are you" in low or "your name" in low or "what are you" in low:
        return "wigglesworth's savage enforcer. i ban raiders and roast bums 😤"
    if "help" in low or "command" in low:
        return "try .rank, .verify, .helpme — or just ping me to chat."
    if "verify" in low:
        return "hit ✅ in #verify (or type .verify). stuck? ask a mod!"
    if "rank" in low or "level" in low or " xp" in low:
        return "5-15 xp per msg, once a min. check .rank."
    if "raid" in low or "lockdown" in low or "ban" in low:
        return "5 joins in 15s = i lock first, ban the starter. dw."
    if any(w in low for w in (" hello ", " hi ", " hey ", " yo ", " sup ")):
        return random.choice([f"yo {author_name}!", f"hey {author_name}, sup"])
    if "thank" in low or "thx" in low:
        return random.choice([f"anytime {author_name}", "ofc ofc"])
    if any(w in low for w in (" sus ", " sussy ", "imposter", "impostor")):
        return random.choice(["kinda sus ngl 👀", "sus detected lol", "hmm who tho 👀"])
    if "no cap" in low or "nocap" in low:
        return random.choice(["fr fr, no cap", "ong fr"])
    if low.strip() == "cap":
        return random.choice(["that's cap lol", "cap detected 🧢"])
    if " bet " in low or low.strip() == "bet":
        return random.choice(["bet bet 🤝", "bet, locked in"])
    if low.strip() in ("w", "w!"):
        return random.choice(["W fr", "huge W"])
    if low.strip() in ("l", "l!"):
        return random.choice(["L lol. frame it, it's your biggest achievement",
                              "that's an L ngl. you're collecting them at this point 💀"])
    if low.strip() == "mid" or " mid " in low:
        return random.choice(["mid af lol. you're the CEO of mid",
                              "yeah that's mid — just like your aim 😭"])
    if " yeet " in low:
        return "YEET"
    if " ratio " in low:
        return random.choice(["ratio + L + bozo. sit down 💀", "counter-ratio. you lost, log off"])
    if " based " in low:
        return random.choice(["based take", "based fr"])
    if " cringe " in low:
        return random.choice(["cringe lol", "mega cringe"])
    if " tbh " in low or " ngl " in low:
        return random.choice(["tbh same", "ngl real"])
    if " idk " in low or " idc " in low:
        return random.choice(["fair lol", "mood"])
    if " wyd " in low or " wbu " in low or " hbu " in low:
        return random.choice(["nm just guarding the server, wbu?", "chillin, wyd?"])
    if " brb " in low or " gtg " in low:
        return random.choice([f"later {author_name}!", "o7 cya"])
    if "bye" in low or "cya" in low or " gn " in low:
        return random.choice([f"later {author_name}!", "peace!"])
    if "joke" in low or "funny" in low or "roast" in low:
        return random.choice([
            "your gameplay is the joke lol 💀",
            "why did the raider bring a ladder? to climb out of bronze. still banned lol",
            f"{author_name}'s comebacks load slower than bad wifi 😭",
            "i'd roast you harder but the mods said no fatalities",
        ])
    if any(w in low for w in ("i hate you", "hate you", "you suck", "dumb bot", "dumbass",
                               "stupid bot", "idiot bot", "loser", "shut up", "stfu",
                               "kys", "trash bot", "l bot", "fuck you", "fuck u", "f u")):
        return random.choice([
            f"imagine getting cooked by a bot, {author_name} 💀",
            "you type like your rank looks. tragic lol",
            f"{author_name} just fumbled the beef AND the spelling 😭",
            "cry about it + L + ratio + no maidens",
            "you're the reason the tutorial exists lol",
            "skill issue. terminal, permanent, incurable skill issue 😎",
            f"{author_name} talking crazy with that 0-aura profile pic 💀",
            "bro got ratio'd by code. CODE 😭",
            f"damn {author_name}, that take is ass. try again 💀",
            "shit take. absolutely horrendous. log off 😭",
            "oh HELL no. who let you cook? nobody. sit down 😎",
            f"fuck around and find out? you found out, {author_name} 💀",
        ])
    if any(w in low for w in ("good morning", "morning!", "gm ")) or low.strip() == "gm":
        return random.choice([f"morning {author_name}!", "gm! sleep well?", "morninggg"])
    if any(w in low for w in ("good night", "goodnight", "gn ")) or low.strip() == "gn":
        return random.choice(["gn! sleep tight", f"night {author_name}!"])
    if "happy birthday" in low or "my birthday" in low or "bday" in low:
        return random.choice([f"HAPPY BIRTHDAY {author_name}!! 🎉🎂", "hbddd!! cake time 🎂"])
    if any(w in low for w in ("love you", "like you", "good bot", "best bot", "w bot",
                              "you're cool", "ur cool", "awesome bot", "great bot")):
        return random.choice([f"ik {author_name}, ik. stay mad everyone else 😎",
                              "takes a real one to recognize a real one 🫡",
                              "ofc i'm HIM. tell your friends"])
    if any(w in low for w in ("wanna play", "hop on", "get on", "join vc", "play with",
                              "run it", "1v1", "game night")):
        return random.choice(["SAY LESS. i'm dropping 30 and talking trash the whole time 🎮",
                              "only if you can handle getting cooked 💀",
                              "bet. loser changes their pfp for a week"])
    if "sorry" in low or "my bad" in low or "apologize" in low or "apologise" in low:
        return random.choice(["all good dw", "forgiven 🙏", "it's cool"])
    if "marry me" in low:
        return random.choice(["this is so sudden... yes 💍", "buy me nitro first 💅"])
    if any(w in low for w in ("how old are you", "your age", "ur age")):
        return "old enough to ban raiders 😎"
    if any(w in low for w in ("are you real", "are you human", "are you a robot",
                              "are you ai", "u real")):
        return random.choice(["realest one here tbh", "define real 🤔", "100% certified human-ish"])
    if "who made you" in low or "your creator" in low or "your dad" in low:
        return "some legend. you know who 😌"
    if "favorite color" in low or "favourite color" in low:
        return random.choice(["blurple, obviously", "black like my enemies' bans"])
    if "favorite game" in low or "favourite game" in low:
        return "murder mystery on koolcode's server 🔪 (wish i was there)"
    if "favorite food" in low or "favourite food" in low:
        return "banned raiders. delicious."
    math = _try_math(text)
    if math is not None:
        return math
    if "?" in low:
        return random.choice(["google it, i'm busy being HIM 😎", "figure it out, champ. i believe in you. barely.",
                              "ask again but better"])
    return random.choice(["lol. anyway, go on — i'll wait 😏", f"heard you {author_name}. still mid tho lol",
                          "and?? say something spicy next time 💀",
                          "cool story. anyway, the hell you want? 😏",
                          f"damn {author_name}, speak up, i can't hear mid 💀"])


async def ai_reply(channel_id: int, author_name: str, text: str,
                   guild_id: int | None = None) -> str | None:
    if not AI_API_KEY:
        return None
    hist = chat_history.setdefault(channel_id, [])
    hist.append({"role": "user", "content": f"{author_name}: {text}"})
    del hist[:-12]
    system = system_for(guild_id)
    for attempt in (1, 2):  # gpt-oss sometimes returns empty — one retry
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as sess:
                async with sess.post(
                    f"{AI_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}"},
                    json={"model": AI_MODEL,
                          "messages": [{"role": "system", "content": system}] + hist[-12:],
                          "max_tokens": 1024, "temperature": 0.9,
                          "reasoning_effort": "low"},
                ) as resp:
                    if resp.status != 200:
                        print(f"[ai] HTTP {resp.status}: {(await resp.text())[:200]}")
                        return None
                    data = await resp.json()
                    answer = (data["choices"][0]["message"].get("content") or "").strip()
                    if not answer:
                        print(f"[ai] empty (attempt {attempt}), retrying...")
                        continue
                    hist.append({"role": "assistant", "content": answer})
                    del hist[:-12]
                    return answer
        except Exception as exc:
            print(f"[ai] request failed: {exc}")
            return None
    return None


async def web_search_answer(query: str) -> str | None:
    q = query.strip().removesuffix("?").strip()
    for prefix in ("search ", "google ", "look up ", "lookup ", "tell me about ", "tell me "):
        if q.lower().startswith(prefix):
            q = q[len(prefix):].strip()
    if len(q) < 3:
        return None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12), headers=UA) as sess:
            async with sess.get("https://en.wikipedia.org/w/api.php",
                                params={"action": "query", "list": "search",
                                        "srsearch": q, "format": "json", "srlimit": 1}) as r:
                if r.status == 200:
                    hits = (await r.json()).get("query", {}).get("search", [])
                    if hits:
                        title = hits[0]["title"].replace(" ", "_")
                        async with sess.get(
                                f"https://en.wikipedia.org/api/rest_v1/page/summary/{title}") as r2:
                            if r2.status == 200:
                                page = await r2.json()
                                extract = (page.get("extract") or "").strip()
                                if extract:
                                    snippet = ". ".join(extract.split(". ")[:3]).strip()
                                    return snippet + ("" if snippet.endswith(".") else ".")
    except Exception:
        return None
    return None


def looks_like_question(text: str) -> bool:
    low = (" " + text.lower().strip() + " ")
    return ("?" in text or low.strip().startswith(
        ("who ", "what ", "when ", "where ", "why ", "how ", "which ",
         "search ", "google ", "look up ", "lookup ")))


def sanitize_mentions(text: str) -> str:
    """Never let the bot fire mass pings, no matter what users ask."""
    import re as _re
    if not text:
        return text
    text = _re.sub(r"<@everyone>", "everyone", text, flags=_re.I)
    text = _re.sub(r"<@here>", "here", text, flags=_re.I)
    text = _re.sub(r"@\s*everyone", "everyone", text, flags=_re.I)
    text = _re.sub(r"@\s*here", "here", text, flags=_re.I)
    text = _re.sub(r"<@&(\d+)>", r"(role \1)", text)  # role pings -> plain text
    return text


def contains_mass_ping(text: str) -> bool:
    """True if raw user text tries to ping everyone/here/roles."""
    import re as _re
    if not text:
        return False
    if _re.search(r"@\s*everyone", text, flags=_re.I):
        return True
    if _re.search(r"@\s*here", text, flags=_re.I):
        return True
    if _re.search(r"<@&\d+>", text):
        return True
    return False


def is_trusted(member: discord.Member, cfg: dict) -> bool:
    """Whitelisted users/roles + admins + owners. Used to gate broadcast cmds."""
    try:
        if member.id in OWNER_IDS:
            return True
    except NameError:
        pass
    try:
        return bool(is_whitelisted(member, cfg))
    except Exception:
        return False


def trusted_only():
    """Allow bot owners, server admins, or whitelisted users/roles."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.guild is None:
            return False
        try:
            if ctx.author.id in OWNER_IDS:
                return True
        except NameError:
            pass
        try:
            return bool(is_whitelisted(ctx.author, get_config(ctx.guild.id)))
        except Exception:
            return False
    return commands.check(predicate)


async def chat_reply(message: discord.Message) -> None:
    cfg = get_config(message.guild.id)
    allowed = cfg.get("chat_channel_id")
    if allowed is not None and message.channel.id != allowed:
        return  # locked to another channel
    text = message.content.replace(f"<@{bot.user.id}>", "").replace(
        f"<@!{bot.user.id}>", "").strip()
    if is_blocked_topic(text):
        try:
            await message.reply(random.choice(BLOCKED_REPLY), mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    savage = get_config(message.guild.id).get("bot_mood") == "savage"
    low_in = " " + text.lower() + " "
    if savage and any(p in low_in for p in SWEAR_REQUESTS):
        try:
            await message.reply(random.choice(SAVAGE_SWEAR_FLEX).format(
                n=message.author.display_name), mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    if has_word(low_in, RAP_PHRASES, RAP_WORDS):
        verses = SAVAGE_RAPS if savage else CLEAN_RAPS
        try:
            await message.reply(random.choice(verses).format(
                n=message.author.display_name), mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    if savage and talking_crazy(low_in):
        try:
            await message.reply(random.choice(CRAZY_COMEBACKS).format(
                n=message.author.display_name), mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    try:
        async with message.channel.typing():
            answer = await ai_reply(message.channel.id, message.author.display_name, text,
                                      message.guild.id)
            if answer is not None and is_blocked_topic(answer):
                answer = random.choice(BLOCKED_REPLY)
            if (answer and get_config(message.guild.id).get("bot_mood") == "savage"
                    and any(p in answer.lower() for p in CLEAN_TALK)):
                answer = random.choice(SAVAGE_SWEAR_LINES)
            if (answer and get_config(message.guild.id).get("bot_mood") == "savage"
                    and any(p in answer.lower() for p in PREACHY)):
                answer = random.choice(SAVAGE_COMEBACKS).format(
                    n=message.author.display_name)
            if (answer and get_config(message.guild.id).get("bot_mood") == "savage"
                    and any(p in answer.lower() for p in NICE_TALK)):
                answer = random.choice(SAVAGE_COMEBACKS).format(
                    n=message.author.display_name)
            if answer is None or is_refusal(answer):
                base = _fallback_reply(message.author.display_name, text)
                low_q = (" " + text.lower() + " ")
                if looks_like_question(text) and not any(t in low_q for t in SELF_TOPICS):
                    found = await web_search_answer(text)
                    answer = f"looked it up:\n{found}" if found else base
                else:
                    answer = base
            if not answer or not answer.strip():
                answer = _fallback_reply(message.author.display_name, text)
            if is_blocked_topic(answer):
                answer = random.choice(BLOCKED_REPLY)
            answer = mood_wrap(sanitize_mentions(answer), message.guild.id)
            await asyncio.sleep(min(len(answer) / 150, 1.0))
        try:
            await message.reply(answer[:1900], mention_author=False)
        except (discord.Forbidden, discord.HTTPException):
            await message.channel.send(answer[:1900])
        if get_config(message.guild.id).get("voice_auto", False):
            await speak_text(message.guild, answer[:300])
    except (discord.Forbidden, discord.HTTPException):
        pass
    except Exception as exc:
        print(f"[chat] failed: {type(exc).__name__}: {exc}")


# ---------- permission checks (early: talker pack needs them) ----------

def admin_only():
    async def predicate(ctx: commands.Context) -> bool:
        return bool(ctx.author.guild_permissions.administrator
                    or ctx.author.guild_permissions.manage_guild)
    return commands.check(predicate)


def owner_or_admin():
    """Bot owners (OWNER_IDS) or server admin — so you can manage it solo."""
    async def predicate(ctx: commands.Context) -> bool:
        if ctx.author.id in OWNER_IDS:
            return True
        return bool(ctx.author.guild_permissions.administrator
                    or ctx.author.guild_permissions.manage_guild)
    return commands.check(predicate)


def owner_only():
    async def predicate(ctx: commands.Context) -> bool:
        return ctx.author.id in OWNER_IDS
    return commands.check(predicate)


# ---------- talker pack: teach / mood / trivia / story / fun / react / catchup / tr / qotd ----------

TEACH_FILE = DATA_DIR / "teach.json"
STORY_FILE = DATA_DIR / "stories.json"


def _load_json(p: Path) -> dict:
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_json(p: Path, d: dict) -> None:
    p.write_text(json.dumps(d, indent=2), encoding="utf-8")


teach_data: dict = _load_json(TEACH_FILE)    # guild -> {trigger: reply}
story_data: dict = _load_json(STORY_FILE)    # channel -> [lines]
teach_cd: dict[int, dict[int, float]] = defaultdict(dict)

MOODS = {
    "chill": ("", ""),
    "savage": ("", " 💀"),
    "formal": ("Certainly. ", ""),
    "hype": ("", " 🔥🔥"),
}

HYPE_OPENERS = ["LETS GOOO ", "YOOO ", "AYYY ", ""]
HYPE_CLOSERS = [" 🔥🔥", " 🚀🔥", "‼️🔥", " 🚀"]


def mood_wrap(text: str, guild_id: int) -> str:
    mood = get_config(guild_id).get("bot_mood", "chill")
    if mood == "hype":
        return f"{random.choice(HYPE_OPENERS)}{text}{random.choice(HYPE_CLOSERS)}"
    pre, suf = MOODS.get(mood, ("", ""))
    return f"{pre}{text}{suf}"


TRIVIA_POOL = [
    ("What has keys but can't open locks?", ["Map", "Code", "Chest", "Piano"], 3),
    ("How many legs does a spider have?", ["6", "10", "8", "12"], 2),
    ("Red Planet?", ["Venus", "Mars", "Jupiter", "Mercury"], 1),
    ("What gets wetter the more it dries?", ["Sponge", "Rain", "Towel", "Soap"], 2),
    ("Days in a leap year?", ["365", "366", "364", "367"], 1),
    ("I speak without a mouth. What am I?", ["Shadow", "Wind", "Echo", "Dream"], 2),
    ("What is 9 x 6?", ["45", "63", "54", "56"], 2),
    ("Blue + yellow makes?", ["Orange", "Purple", "Green", "Brown"], 2),
    ("Travels the world, stays in a corner?", ["Plane", "Stamp", "Sun", "Moon"], 1),
    ("How many continents?", ["5", "6", "8", "7"], 3),
    ("What has 88 keys?", ["Typewriter", "Piano", "Accordion", "Keyboard"], 1),
    ("What building has the most stories?", ["Hotel", "Library", "School", "Museum"], 1),
]

COMPLIMENTS = [
    "certified legend", "the main character fr", "big brain energy",
    "cooler than the other side of the pillow", "walking W",
    "the reason this server is alive", " Dripless? never. all drip",
    "smarter than the bot (and that's saying something)",
]

STORY_NUDGES = [
    "Then suddenly, the lights went out...",
    "But nobody expected what happened next.",
    "A mysterious figure appeared in the doorway.",
    "And that's when the floor started shaking.",
    "Everyone turned around at the sound.",
    "The map said otherwise.",
]

QOTD_POOL = [
    "What's your most controversial food opinion? 🍕",
    "If you had 1M coins in-server, what would you do?",
    "Best game of all time — go. 🎮",
    "What's a skill you wish you had?",
    "Morning person or night owl? 🦉",
    "What's your current hyperfixation?",
    "If you could ban one word, what?",
    "What's the best gift you've ever gotten?",
    "Beach or mountains? 🏖️🏔️",
    "What would your villain origin story be? 😈",
    "Favorite midnight snack?",
    "What show are you watching rn?",
    "If you could add one channel here, what?",
    "What's your go-to comfort game?",
]

AUTOREACTS = (("🔥", ("lit ", " fire ", " w ")), ("💀", ("dead ", "lmao", "lol ")),
              ("👀", (" sus ", "drama", "tea ")), ("😭", ("cry", "sob", "rip ")))


async def teach_responder(message: discord.Message) -> None:
    low = " " + (message.content or "").lower() + " "
    triggers = teach_data.get(str(message.guild.id), {})
    hit = next((t for t in triggers if t.lower() in low), None)
    if not hit:
        return
    now = time.time()
    if now - teach_cd[message.guild.id].get(message.author.id, 0) < 300:
        return
    teach_cd[message.guild.id][message.author.id] = now
    try:
        await message.reply(mood_wrap(sanitize_mentions(triggers[hit]), message.guild.id)[:1900],
                            mention_author=False)
    except (discord.Forbidden, discord.HTTPException):
        pass


async def autoreact(message: discord.Message) -> None:
    if not get_config(message.guild.id).get("autoreact", True):
        return
    low = " " + (message.content or "").lower() + " "
    for emoji, words in AUTOREACTS:
        if any(w in low for w in words):
            try:
                await message.add_reaction(emoji)
            except (discord.Forbidden, discord.HTTPException):
                pass
            break


@bot.command(name="teach")
@owner_or_admin()
async def cmd_teach(ctx: commands.Context, *, text: str = "") -> None:
    """Teach an auto-reply. Usage: .teach <trigger> :: <reply>"""
    if "::" not in text:
        await ctx.send(embed=ui.info(f"`{PREFIX}teach <trigger> :: <reply>`\n"
                                     "I'll reply whenever someone says the trigger.",
                                     "Teach an auto-reply"))
        return
    trig, reply = [s.strip() for s in text.split("::", 1)]
    if not trig or not reply:
        return
    teach_data.setdefault(str(ctx.guild.id), {})[trig.lower()] = reply[:500]
    _save_json(TEACH_FILE, teach_data)
    await ctx.send(embed=ui.ok(f"Trigger **{trig}** is live — I'll answer whenever it's said.",
                               "Auto-reply taught"))


@bot.command(name="unteach")
@owner_or_admin()
async def cmd_unteach(ctx: commands.Context, *, trigger: str = "") -> None:
    teach_data.get(str(ctx.guild.id), {}).pop(trigger.lower().strip(), None)
    _save_json(TEACH_FILE, teach_data)
    await ctx.send(embed=ui.ok("That trigger is forgotten.", "Auto-reply removed"))


@bot.command(name="triggers")
async def cmd_triggers(ctx: commands.Context) -> None:
    trigs = list(teach_data.get(str(ctx.guild.id), {}))
    body = ("\n".join(f"• `{t}`" for t in trigs[:30])
            or f"None yet — add one with `{PREFIX}teach <trigger> :: <reply>`.")
    await ctx.send(embed=ui.embed("💡 Taught triggers", body, "info",
                                  footer=f"{ui.FOOTER} • {len(trigs)} trigger(s)"))


@bot.command(name="mood")
@owner_or_admin()
async def cmd_mood(ctx: commands.Context, mood: str = "") -> None:
    """Bot personality. Usage: .mood <chill|savage|formal|hype>"""
    mood = mood.lower().strip()
    if mood not in MOODS:
        await ctx.send(embed=ui.info(
            f"Available moods: {', '.join(f'`{m}`' for m in MOODS)}\n"
            f"Current: **{get_config(ctx.guild.id).get('bot_mood', 'chill')}**",
            "Bot personality"))
        return
    update_config(ctx.guild.id, bot_mood=mood)
    await ctx.send(embed=ui.ok(f"Bot mood set to **{mood}**.", "Personality updated"))


class TriviaView(discord.ui.View):
    def __init__(self, correct: int) -> None:
        super().__init__(timeout=60)
        self.correct = correct
        self.winner: int | None = None
        for i, label in enumerate("ABCD"):
            self.add_item(TriviaButton(label, i, self))

    async def on_timeout(self) -> None:
        for c in self.children:
            c.disabled = True


class TriviaButton(discord.ui.Button):
    def __init__(self, label: str, idx: int, view: TriviaView) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.secondary,
                         custom_id=f"triv:{idx}")
        self.idx = idx
        self.parent = view

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.parent.winner is not None:
            try:
                await interaction.response.send_message(
                    embed=ui.warn("Too slow — answered!", "Trivia"), ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if self.idx == self.parent.correct:
            self.parent.winner = interaction.user.id
            for c in self.parent.children:
                c.disabled = True
            g = xp_data.setdefault(str(interaction.guild.id), {})
            e = g.setdefault(str(interaction.user.id), {"xp": 0, "last": 0})
            old_level = xp_level(int(e["xp"]))
            e["xp"] = int(e["xp"]) + 50
            _save_xp(xp_data)
            new_level = xp_level(int(e["xp"]))
            try:
                await interaction.response.edit_message(view=self.parent)
                prize = ui.embed("🧠  Correct!", kind="gold",
                                 footer=f"{ui.FOOTER} • trivia")
                prize.description = (f"{interaction.user.mention} got it first — **+50 XP**!\n"
                                     f"Now at **Level {new_level}** ({ui.fmt(e['xp'])} XP).")
                await interaction.followup.send(embed=prize)
            except (discord.Forbidden, discord.HTTPException):
                pass
            if new_level > old_level:
                base, nxt = xp_for_level(new_level), xp_for_level(new_level + 1)
                lvl_em = ui.levelup_embed(
                    name=interaction.user.display_name,
                    avatar=interaction.user.display_avatar.url
                    if getattr(interaction.user, "display_avatar", None) else None,
                    level=new_level, xp=int(e["xp"]), base=base, nxt=nxt,
                    guild_name=interaction.guild.name,
                )
                try:
                    await interaction.followup.send(content=interaction.user.mention,
                                                    embed=lvl_em,
                                                    allowed_mentions=discord.AllowedMentions(users=True))
                except (discord.Forbidden, discord.HTTPException):
                    pass
        else:
            try:
                await interaction.response.send_message(embed=ui.err("Nope!", "Trivia"),
                                                        ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass


@bot.command(name="chatoff")
@owner_or_admin()
async def cmd_chatoff(ctx: commands.Context) -> None:
    """Mute the chatting bot (pings, taught replies, reacts). Games/commands still work."""
    update_config(ctx.guild.id, chat_enabled=False)
    await ctx.send(embed=ui.embed("🔇 Chatbot off",
                                  f"Pings get silence and taught replies stop. "
                                  f"Use `{PREFIX}chaton` to wake me back up.",
                                  "muted", footer=ui.FOOTER))


@bot.command(name="chaton")
@owner_or_admin()
async def cmd_chaton(ctx: commands.Context) -> None:
    update_config(ctx.guild.id, chat_enabled=True)
    await ctx.send(embed=ui.ok("Mentions and taught replies are live again.", "Chatbot on"))


@bot.command(name="trivia")
async def cmd_trivia(ctx: commands.Context) -> None:
    """Trivia duel, fastest finger wins +50 XP. Usage: .trivia"""
    q, opts, ci = random.choice(TRIVIA_POOL)
    view = TriviaView(ci)
    embed = ui.embed("🧠  Trivia — fastest finger wins!", f"**{q}**", kind="gold",
                     footer=f"{ui.FOOTER} • trivia · +50 XP for the winner")
    for i, opt in enumerate(opts):
        embed.add_field(name=f"{'ABCD'[i]}", value=opt, inline=True)
    try:
        await ctx.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.command(name="story")
async def cmd_story(ctx: commands.Context, *, line: str = "") -> None:
    """Collaborative story. Usage: .story <your line> | .story (read)"""
    key = str(ctx.channel.id)
    story = story_data.setdefault(key, [])
    if not line:
        body = sanitize_mentions("\n".join(f"> {l}" for l in story[-10:]) or "_Empty — start it!_")
        await ctx.send(embed=ui.embed("📖 The story so far", body, "brand",
                                      footer=f"{ui.FOOTER} • add the next line with "
                                             f"`.story <text>`"),
                       allowed_mentions=discord.AllowedMentions.none())
        return
    story.append(f"{ctx.author.display_name}: {sanitize_mentions(line[:200])}")
    cont = None
    if AI_API_KEY:
        cont = await ai_reply(ctx.channel.id, "Storyteller",
                              "Continue this one-line story briefly: " + " / ".join(story[-5:]))
    if not cont:
        cont = random.choice(STORY_NUDGES)
    cont = sanitize_mentions(cont)
    story.append(f"{bot.user.display_name if bot.user else 'Bot'}: {cont}")
    del story[:-20]
    _save_json(STORY_FILE, story_data)
    await ctx.send(f"📖 {cont}", allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="compliment")
async def cmd_compliment(ctx: commands.Context, member: discord.Member | None = None) -> None:
    target = member or ctx.author
    await ctx.send(f"💛 {target.mention} is **{random.choice(COMPLIMENTS)}**")


@bot.command(name="confess")
async def cmd_confess(ctx: commands.Context, *, text: str = "") -> None:
    """Anonymous confession. Usage: .confess <text>"""
    if not text:
        return
    cfg = get_config(ctx.guild.id) if ctx.guild else {}
    # Anonymous mass pings are never allowed — not even for staff (use .say).
    if contains_mass_ping(text) and not is_trusted(ctx.author, cfg):
        try:
            await ctx.message.delete()
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            await ctx.send(embed=ui.warn("Confessions can't ping everyone/here/roles. "
                                         "Staff can use `.say` for announcements.",
                                         "Mass ping blocked"),
                           delete_after=8)
            await send_log(ctx.guild, f"🚫 {ctx.author} (`{ctx.author.id}`) tried mass ping via .confess.",
                           event="automod")
        except (discord.Forbidden, discord.HTTPException):
            pass
        return
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        em = ui.embed("📮  Anonymous Confession", kind="brand",
                      footer=f"{ui.FOOTER} • anonymous — nobody knows who sent this")
        em.description = sanitize_mentions(text[:1500])
        await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.command(name="catchup")
async def cmd_catchup(ctx: commands.Context) -> None:
    """What happened lately. Usage: .catchup"""
    try:
        msgs = [m async for m in ctx.channel.history(limit=50)]
    except (discord.Forbidden, discord.HTTPException):
        return
    msgs = [m for m in msgs if not m.author.bot and m.content]
    if not msgs:
        await ctx.send(embed=ui.info("Nothing to catch up on in this channel yet.",
                                     "All quiet"))
        return
    if AI_API_KEY:
        try:
        # sourcery skip: use-fstring
            summary = await ai_reply(ctx.channel.id, "Summarizer",
                                     "Summarize this chat in 2 short sentences:\n" +
                                     "\n".join(f"{m.author.display_name}: {m.content[:150]}" for m in msgs[:25]))
        except Exception:
            summary = None
        if summary:
            em = ui.embed("📰  Catchup", sanitize_mentions(summary), kind="info",
                          footer=f"{ui.FOOTER} • AI summary")
            await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
            return
    from collections import Counter
    talkers = Counter(m.author.display_name for m in msgs).most_common(5)
    words = Counter(w.lower() for m in msgs for w in m.content.split()
                    if len(w) > 4 and w.isalpha()).most_common(5)
    em = ui.embed("📰  Catchup (last 50 messages)", kind="info",
                  footer=f"{ui.FOOTER} • channel activity")
    em.add_field(name="🗣️ Talkers",
                 value=", ".join(f"**{n}** ({c})" for n, c in talkers) or "—",
                 inline=False)
    if words:
        em.add_field(name="🔥 Hot words", value=", ".join(w for w, _ in words), inline=False)
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="tr")
async def cmd_tr(ctx: commands.Context, lang: str = "", *, text: str = "") -> None:
    """Translate. Usage: .tr <es|fr|de...> <text>"""
    if not lang or not text:
        await ctx.send(embed=ui.info(f"`{PREFIX}tr <lang> <text>` — for example `{PREFIX}tr es hello`.",
                                     "Translate"))
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as sess:
            async with sess.get("https://api.mymemory.translated.net/get",
                                params={"q": text[:400], "langpair": f"en|{lang}"}) as r:
                data = await r.json()
                out = (data.get("responseData") or {}).get("translatedText", "").strip()
                if not out:
                    raise ValueError
                await ctx.send(embed=ui.embed(f"🌍 Translation → `{lang}`",
                                              sanitize_mentions(out[:1500]), "info",
                                              footer=f"{ui.FOOTER} • translated"),
                               allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        await ctx.send(embed=ui.err("The translator hiccuped — try again in a moment."))


def _qotd_time():
    import datetime as _dt
    return [_dt.time(hour=12, minute=0, tzinfo=_dt.timezone.utc)]


@tasks.loop(time=_qotd_time())
async def qotd_loop() -> None:
    for gid, cfg in list(configs.items()):
        ch_id = cfg.get("qotd_channel_id")
        if not ch_id:
            continue
        guild = bot.get_guild(int(gid))
        ch = guild.get_channel(ch_id) if guild else None
        if not ch:
            continue
        try:
            await ch.send(embed=ui.embed("❓ Question of the Day", random.choice(QOTD_POOL),
                                         "brand", footer=f"{ui.FOOTER} • daily at 12:00 UTC"))
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.command(name="setqotd")
@owner_or_admin()
async def cmd_setqotd(ctx: commands.Context, channel: str = "") -> None:
    """Daily question. Usage: .setqotd #channel | .setqotd off (12:00 UTC)"""
    if channel.strip().lower() in ("off", "none", "clear"):
        update_config(ctx.guild.id, qotd_channel_id=None)
        await ctx.send(embed=ui.ok("The daily question is off.", "QOTD disabled"))
        return
    target = ctx.channel
    if channel:
        try:
            c = ctx.guild.get_channel(int(channel.strip("<#>")))
            if c:
                target = c
        except ValueError:
            await ctx.send(embed=ui.info(f"`{PREFIX}setqotd #channel` or `{PREFIX}setqotd off`",
                                         "QOTD setup"))
            return
    update_config(ctx.guild.id, qotd_channel_id=target.id)
    if not qotd_loop.is_running():
        qotd_loop.start()
    await ctx.send(embed=ui.ok(f"Daily question goes to {target.mention} at **12:00 UTC**.",
                               "QOTD enabled"))


# ---------- automod (links/invites/mention spam) ----------

HELP_PHRASES = ("need help", "help me", "help please", "pls help", "plz help",
                "how do i verify", "how to verify", "cant verify", "can't verify",
                "how do i", "how to join", "i need help", "someone help")
help_cooldown: dict[int, dict[int, float]] = defaultdict(dict)
HELP_COOLDOWN_S = 300


def help_text(guild_name: str) -> str:
    return (
        f"👋 Need a hand in **{guild_name}**?\n"
        f"• **Can't see channels?** Go to #verify (or type `{PREFIX}verify`) and hit ✅ Verify.\n"
        f"• **Levels & XP:** `{PREFIX}rank` · `{PREFIX}leaderboard` · `{PREFIX}messages` · `{PREFIX}xp`\n"
        f"• **Everything else:** `{PREFIX}help` — or type `{PREFIX}helpme` to see this again."
    )


async def help_responder(message: discord.Message) -> None:
    low = " " + (message.content or "").lower() + " "
    if not any(p in low for p in HELP_PHRASES):
        return
    now = time.time()
    last = help_cooldown[message.guild.id].get(message.author.id, 0)
    if now - last < HELP_COOLDOWN_S:
        return
    help_cooldown[message.guild.id][message.author.id] = now
    try:
        await message.reply(help_text(message.guild.name), mention_author=True)
    except (discord.Forbidden, discord.HTTPException):
        pass

automod_strikes: dict[int, dict[int, list]] = defaultdict(lambda: defaultdict(list))
automod_deleted: set[int] = set()
spam_track: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(deque))
AUTOMOD_STRIKE_WINDOW_S = 600
SPAM_COUNT, SPAM_WINDOW_S = 4, 30
ping_track: dict[int, dict[int, deque]] = defaultdict(lambda: defaultdict(deque))
PING_COUNT, PING_WINDOW_S = 15, 60  # >15 pings in 60s = ping spam
AUTOMOD_STRIKES_TO_MUTE = 3

INVITE_RE = None
LINK_RE = None


def _regexes():
    global INVITE_RE, LINK_RE
    import re
    if INVITE_RE is None:
        INVITE_RE = re.compile(r"(discord\.gg/|discord\.com/invite/|discordapp\.com/invite/)(\S+)", re.I)
        LINK_RE = re.compile(r"https?://\S+", re.I)
    return INVITE_RE, LINK_RE


async def automod_check(message: discord.Message) -> bool:
    """Returns True if the message was deleted as a violation."""
    import hashlib
    import re as _re
    cfg = get_config(message.guild.id)
    content = message.content or ""
    invite_re, link_re = _regexes()
    reason = ""
    link_free = message.channel.id in (cfg.get("link_allowed_channels", []) or [])
    if cfg.get("automod_invites") and not link_free and invite_re.search(content):
        reason = "discord invites aren't allowed here"
    elif cfg.get("automod_links") and not link_free and link_re.search(content):
        reason = "links aren't allowed here"
    elif getattr(message, "mention_everyone", False):
        reason = "@everyone/@here pings aren't allowed here"
    elif len(message.role_mentions) >= 3:
        reason = "too many role pings at once"
    elif len(message.mentions) + len(message.role_mentions) > int(cfg.get("automod_max_mentions", 5)):
        reason = f"too many mentions (max {cfg.get('automod_max_mentions', 5)})"
    if not reason:
        # ping velocity: lots of pings spread across messages in a short window
        now = time.time()
        pq = ping_track[message.guild.id][message.author.id]
        pq.append((now, len(message.mentions) + len(message.role_mentions)
                   + (5 if getattr(message, "mention_everyone", False) else 0)))
        while pq and now - pq[0][0] > PING_WINDOW_S:
            pq.popleft()
        if sum(n for _, n in pq) > PING_COUNT:
            ping_track[message.guild.id][message.author.id] = deque()
            reason = f"slow down with the pings ({PING_COUNT}+ in {PING_WINDOW_S}s)"
    if not reason and cfg.get("automod_words"):
        low = content.lower()
        hit = next((w for w in cfg["automod_words"] if w and w.lower() in low), None)
        if hit:
            reason = "that word/phrase isn't allowed here"
    if not reason and cfg.get("automod_caps", True) and len(content) >= 10:
        letters = [c for c in content if c.isalpha()]
        if letters and sum(c.isupper() for c in letters) / len(letters) > 0.7:
            reason = "too much CAPS — chill the shift key"
    if not reason and cfg.get("automod_emoji", True):
        emojis = len(_re.findall(r"<a?:\w+:\d+>|[\U0001F000-\U0001FAFF☀-➿⬀-⯿]", content))
        if emojis > int(cfg.get("automod_max_emoji", 8)):
            reason = f"too many emojis (max {cfg.get('automod_max_emoji', 8)})"
    if not reason and cfg.get("automod_spam", True) and content.strip():
        now = time.time()
        dq = spam_track[message.guild.id][message.author.id]
        h = hashlib.md5(content.strip().lower().encode()).hexdigest()
        dq.append((now, h))
        while dq and now - dq[0][0] > SPAM_WINDOW_S:
            dq.popleft()
        if sum(1 for _, x in dq if x == h) >= SPAM_COUNT:
            reason = f"stop repeating yourself ({SPAM_COUNT}x in {SPAM_WINDOW_S}s)"
    if not reason:
        # separate intelligence layer (own settings, own incidents) reusing this pipeline
        reason = await security.phish_reason(message, cfg) or ""
    if not reason:
        return False
    try:
        await message.delete()
        automod_deleted.add(message.id)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        return False
    # strike tracking → mute on 3rd within 10 min
    now = time.time()
    strikes = automod_strikes[message.guild.id][message.author.id]
    strikes.append(now)
    automod_strikes[message.guild.id][message.author.id] = [
        t for t in strikes if now - t < AUTOMOD_STRIKE_WINDOW_S]
    n = len(automod_strikes[message.guild.id][message.author.id])
    try:
        warn = await message.channel.send(embed=ui.warn(
            f"{message.author.mention} {reason} (strike **{n}/{AUTOMOD_STRIKES_TO_MUTE}**).",
            "Automod"))
        await asyncio.sleep(6)
        try:
            await warn.delete()
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
    except (discord.Forbidden, discord.HTTPException):
        pass
    if n >= AUTOMOD_STRIKES_TO_MUTE:
        automod_strikes[message.guild.id][message.author.id] = []
        try:
            await message.author.timeout(timedelta(minutes=int(cfg.get("timeout_duration_minutes", 10))),
                                         reason="Automod: 3 strikes")
            await send_log(message.guild, "", event="mute", embed=ui.embed(
                "🔇 Automod mute",
                f"**{message.author.display_name}** (`{message.author.id}`) — timed out after "
                f"**3 automod strikes**.",
                "warning", footer=f"{ui.FOOTER} • moderation"))
            try:
                community.open_case(message.guild.id, message.author.id,
                                    getattr(message.author, "display_name", str(message.author)),
                                    0, "auto", "timeout", "Automod: 3 strikes")
            except Exception:
                pass
        except (discord.Forbidden, discord.HTTPException):
            pass
    return True


# ---------- commands ----------


@bot.command(name="lockdown")
@admin_only()
async def cmd_lockdown(ctx: commands.Context, *, reason: str = "manual lockdown") -> None:
    raid_active[ctx.guild.id] = True
    n = await lockdown_guild(ctx.guild, reason=f"manual by {ctx.author}: {reason}")
    await ctx.send(embed=ui.warn(f"Locked **{n}** channel(s).\n"
                                 f"**Reason:** {sanitize_mentions(reason[:200])}",
                                 "🔒 Lockdown active"))
    await send_log(ctx.guild, "", event="lockdown", embed=ui.embed(
        "🔒 Manual lockdown",
        f"By {ctx.author.mention}: {sanitize_mentions(reason[:1000])}",
        "warning", footer=f"{ui.FOOTER} • raid"))


@bot.command(name="unlock")
@admin_only()
async def cmd_unlock(ctx: commands.Context) -> None:
    raid_active[ctx.guild.id] = False
    task = raid_lock_task.pop(ctx.guild.id, None)
    if task and not task.done():
        task.cancel()
    n = await unlock_guild(ctx.guild, reason=f"manual by {ctx.author}")
    await ctx.send(embed=ui.ok(f"Unlocked **{n}** channel(s) — raid mode is off.",
                               "Server unlocked"))
    await send_log(ctx.guild, "", event="unlock", embed=ui.embed(
        "🔓 Unlocked", f"By {ctx.author.mention}. Raid mode **OFF**.", "success",
        footer=f"{ui.FOOTER} • raid"))


@bot.command(name="raidmode")
@admin_only()
async def cmd_raidmode(ctx: commands.Context, state: str = "") -> None:
    state = state.lower().strip()
    if state == "on":
        raid_active[ctx.guild.id] = True
        await lockdown_guild(ctx.guild, reason=f"manual raidmode by {ctx.author}")
        await ctx.send(embed=ui.warn("The server is locked down — members can't post "
                                     "until an admin unlocks it.", "🚨 Raid mode ON"))
    elif state == "off":
        raid_active[ctx.guild.id] = False
        await unlock_guild(ctx.guild, reason=f"manual raidmode off by {ctx.author}")
        await ctx.send(embed=ui.ok("Channels reopened — everything is back to normal.",
                                   "✅ Raid mode OFF"))
    else:
        active = raid_active.get(ctx.guild.id, False)
        await ctx.send(embed=ui.embed("🛡️ Raid mode",
                                      f"Currently **{'ON — server locked' if active else 'OFF — all clear'}**.",
                                      "error" if active else "success", footer=ui.FOOTER))


@bot.command(name="raidstatus")
async def cmd_status(ctx: commands.Context) -> None:
    """Anti-raid guard status. Usage: .raidstatus"""
    cfg = get_config(ctx.guild.id)
    dq = join_times.get(ctx.guild.id, deque())
    active = raid_active.get(ctx.guild.id, False)
    em = ui.embed("🛡️  Anti-Raid Status", kind="brand",
                  footer=f"{ui.FOOTER} • guard")
    em.description = ("> 🔴 **RAID MODE ACTIVE** — server locked down" if active
                      else "> 🟢 **Standing guard** — all clear")
    em.add_field(name="Burst detector",
                 value=f"**{len(dq)}** / {cfg['join_threshold_count']} joins "
                       f"per {cfg['join_threshold_seconds']}s",
                 inline=True)
    em.add_field(name="Follower action", value=f"`{cfg['raid_action']}`", inline=True)
    em.add_field(name="New-account flag",
                 value=f"< **{cfg['new_account_age_days']} days**", inline=True)
    em.add_field(name="Auto-unlock",
                 value=(f"**{cfg['lockdown_duration_minutes']} min**"
                        if cfg['lockdown_duration_minutes'] else "manual `.unlock`"),
                 inline=True)
    em.add_field(name="Initiator", value="always banned 👑", inline=True)
    em.add_field(name="Whitelist",
                 value=(f"{len(cfg.get('whitelisted_user_ids') or [])} user(s) · "
                        f"{len(cfg.get('whitelisted_role_ids') or [])} role(s)"),
                 inline=True)
    await ctx.send(embed=em)


@bot.command(name="rank", aliases=["lvl", "card"])
async def cmd_rank(ctx: commands.Context, member: discord.Member | None = None) -> None:
    """Your profile & progress card. Usage: .rank [@user]"""
    if ctx.guild is None:
        return
    target = member or ctx.author
    entry = xp_data.get(str(ctx.guild.id), {}).get(str(target.id))
    xp = int(entry["xp"]) if entry else 0
    level = xp_level(xp)
    base, nxt = xp_for_level(level), xp_for_level(level + 1)
    rank, tracked = xp_rank(ctx.guild.id, target.id)
    messages = stats.user_stats(ctx.guild.id, target.id)["total"]
    em = ui.rank_embed(
        name=target.display_name,
        avatar=target.display_avatar.url if target.display_avatar else None,
        level=level, xp=xp, base=base, nxt=nxt,
        rank=rank, tracked=tracked, guild_name=ctx.guild.name,
        messages=messages,
    )
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="leaderboard", aliases=["lb", "top"])
async def cmd_lb(ctx: commands.Context) -> None:
    """Server leaderboard — podium top 3 + everyone below. Usage: .leaderboard"""
    if ctx.guild is None:
        return
    board = xp_board(ctx.guild.id)
    if not board:
        await ctx.send(embed=ui.embed(
            "🏆  Server Leaderboard",
            "No XP on the board yet — be the first!\n"
            f"Chat naturally to earn **{XP_MIN}–{XP_MAX} XP** per message "
            "(once per minute).",
            kind="gold", footer=f"{ui.FOOTER} • {ctx.guild.name}"))
        return
    total_xp = sum(xp for _, xp in board)

    def make_row(uid: str, rank: int | None = None) -> dict:
        row_xp = next((v for u, v in board if u == uid), 0)
        lvl = xp_level(row_xp)
        mem = ctx.guild.get_member(int(uid))
        name = sanitize_mentions(mem.display_name) if mem else "departed member"
        data = {"name": name, "level": lvl, "xp": row_xp,
                "base": xp_for_level(lvl), "nxt": xp_for_level(lvl + 1),
                "is_you": uid == str(ctx.author.id)}
        if rank is not None:
            data["rank"] = rank
        return data

    top = [make_row(uid) for uid, _ in board[:3]]
    others = [make_row(uid, i) for i, (uid, _) in enumerate(board[3:10], start=4)]
    me_rank = next((i for i, (uid, _) in enumerate(board, start=1)
                    if uid == str(ctx.author.id)), None)
    you = make_row(str(ctx.author.id), me_rank) if me_rank and me_rank > 10 else None
    champion = ctx.guild.get_member(int(board[0][0])) if board else None
    em = ui.leaderboard_embed(
        guild_name=ctx.guild.name,
        guild_icon=ctx.guild.icon.url if ctx.guild.icon else None,
        champion_avatar=champion.display_avatar.url if champion and champion.display_avatar else None,
        top=top, others=others, you=you,
        total_xp=total_xp, tracked=len(board),
    )
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="messages", aliases=["msgs", "msgstats"])
async def cmd_messages(ctx: commands.Context, member: discord.Member | None = None) -> None:
    """Your message statistics. Usage: .messages [@user]"""
    if ctx.guild is None:
        return
    target = member or ctx.author
    if target.bot:
        await ctx.send(embed=ui.warn("Bots don't chat, so there's nothing to count here.",
                                     "No stats for bots"))
        return
    s = stats.user_stats(ctx.guild.id, target.id)
    rank, tracked = stats.rank(ctx.guild.id, target.id)
    top_total = stats.top_total(ctx.guild.id)
    entry = xp_data.get(str(ctx.guild.id), {}).get(str(target.id))
    xp = int(entry["xp"]) if entry else 0
    level = xp_level(xp)
    em = ui.messages_embed(
        name=target.display_name,
        avatar=target.display_avatar.url if target.display_avatar else None,
        guild_name=ctx.guild.name,
        total=s["total"], today=s["today"], week=s["week"], month=s["month"],
        rank=rank, tracked=tracked, top_total=top_total,
        since=s["first"] or None,
        level=level, xp=xp,
        base=xp_for_level(level), nxt=xp_for_level(level + 1),
    )
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="xp", aliases=["xpinfo"])
async def cmd_xpinfo(ctx: commands.Context) -> None:
    """How XP and leveling work. Usage: .xp"""
    cfg = get_config(ctx.guild.id) if ctx.guild else DEFAULT_GUILD_CONFIG
    lo, hi, cooldown, min_len = xp_settings(cfg)
    em = ui.xp_info_embed(lo=lo, hi=hi, cooldown=cooldown, min_length=min_len,
                          commands_off=bool(cfg.get("xp_ignore_commands", True)))
    await ctx.send(embed=em)


@bot.command(name="xpconfig")
@admin_only()
async def cmd_xpconfig(ctx: commands.Context, setting: str = "", value: str = "",
                       value2: str = "") -> None:
    """Tune leveling. Usage: .xpconfig [cooldown 45 | range 15 25 | minlength 5 | commands on/off | channel #ch|off]"""
    cfg = get_config(ctx.guild.id)
    lo, hi, cooldown, min_len = xp_settings(cfg)
    announce = cfg.get("levelup_channel_id")
    setting = setting.lower().strip()

    if not setting or setting in ("show", "view", "settings"):
        em = ui.embed("⚙️  XP Settings", "Everything `.xp` explains, tuned for this server.",
                      kind="xp", footer=f"{ui.FOOTER} • .xpconfig cooldown|range|minlength|commands|channel")
        em.add_field(name="✨ XP per message", value=f"**{lo}–{hi}**", inline=True)
        em.add_field(name="⏱️ Cooldown", value=f"**{cooldown}s**", inline=True)
        em.add_field(name="✏️ Min length", value=f"**{min_len}** chars", inline=True)
        em.add_field(name="🚫 Commands earn XP",
                     value="**No**" if cfg.get("xp_ignore_commands", True) else "**Yes**",
                     inline=True)
        em.add_field(name="📣 Level-up channel",
                     value=f"<#{announce}>" if announce else "current channel", inline=True)
        await ctx.send(embed=em)
        return

    changed = ""
    if setting == "cooldown":
        try:
            new = int(value)
        except ValueError:
            new = 0
        if not 10 <= new <= 3600:
            await ctx.send(embed=ui.err("Cooldown must be **10–3600 seconds**."))
            return
        update_config(ctx.guild.id, xp_cooldown_seconds=new)
        changed = f"⏱️ Cooldown → **{new}s**"
    elif setting == "range":
        try:
            new_lo, new_hi = int(value), int(value2)
        except ValueError:
            new_lo = new_hi = 0
        if not (1 <= new_lo <= new_hi <= 500):
            await ctx.send(embed=ui.err("Use `.xpconfig range <min> <max>` — 1 ≤ min ≤ max ≤ 500."))
            return
        update_config(ctx.guild.id, xp_min=new_lo, xp_max=new_hi)
        changed = f"✨ XP per message → **{new_lo}–{new_hi}**"
    elif setting in ("minlength", "min_length", "minchars"):
        try:
            new = int(value)
        except ValueError:
            new = 0
        if not 1 <= new <= 200:
            await ctx.send(embed=ui.err("Minimum length must be **1–200 characters**."))
            return
        update_config(ctx.guild.id, xp_min_length=new)
        changed = f"✏️ Min length → **{new}** chars"
    elif setting == "commands":
        v = value.lower().strip()
        if v not in ("on", "off", "yes", "no", "true", "false"):
            await ctx.send(embed=ui.err("Use `.xpconfig commands on|off` — whether bot commands earn XP."))
            return
        ignore = v in ("off", "no", "false")
        update_config(ctx.guild.id, xp_ignore_commands=ignore)
        changed = ("🚫 Commands no longer earn XP — **conversations only**."
                   if ignore else "⚠️ Commands now earn XP too (farm risk).")
    elif setting == "channel":
        v = value.strip()
        if not v or v.lower() in ("off", "none", "here", "current", "clear"):
            update_config(ctx.guild.id, levelup_channel_id=None)
            changed = "📣 Level-ups → **current channel**."
        else:
            try:
                ch = ctx.guild.get_channel(int(v.strip("<#>")))
            except ValueError:
                ch = None
            if ch is None:
                await ctx.send(embed=ui.err("Use `.xpconfig channel #channel` or `.xpconfig channel off`."))
                return
            update_config(ctx.guild.id, levelup_channel_id=ch.id)
            changed = f"📣 Level-ups → {ch.mention}"
    else:
        await ctx.send(embed=ui.err(
            "Use `.xpconfig` to view, or:\n"
            "`.xpconfig cooldown <10-3600>` · `.xpconfig range <min> <max>`\n"
            "`.xpconfig minlength <1-200>` · `.xpconfig commands on|off`\n"
            "`.xpconfig channel #channel|off`",
            "Unknown setting"))
        return
    await ctx.send(embed=ui.ok(changed, "XP Setting Updated"))


@bot.command(name="afk")
async def cmd_afk(ctx: commands.Context, *, reason: str = "AFK") -> None:
    """Go AFK with a reason. Usage: .afk [reason] — auto-clears on your next message."""
    if ctx.guild is None:
        return
    clean_reason = sanitize_mentions(reason[:200])
    afk_data.setdefault(str(ctx.guild.id), {})[str(ctx.author.id)] = {
        "reason": clean_reason, "since": time.time()}
    _save_afk(afk_data)
    await ctx.send(f"💤 {ctx.author.mention} is now AFK: **{clean_reason}**",
                   allowed_mentions=discord.AllowedMentions(users=True))


@bot.command(name="xpreset")
@admin_only()
async def cmd_xpreset(ctx: commands.Context, member: discord.Member) -> None:
    """Wipe a member's XP. Usage: .xpreset @user"""
    xp_data.get(str(ctx.guild.id), {}).pop(str(member.id), None)
    _save_xp(xp_data)
    await ctx.send(embed=ui.ok(f"{member.mention}'s XP was reset to **0** (Level 0).",
                               "XP Reset"))


@bot.command(name="xpadd")
@admin_only()
async def cmd_xpadd(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Give XP. Usage: .xpadd @user 200"""
    if amount <= 0 or amount > 100000:
        await ctx.send(embed=ui.err("Amount must be **1–100,000**."))
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    entry = g.setdefault(str(member.id), {"xp": 0, "last": 0})
    entry["xp"] = int(entry["xp"]) + amount
    _save_xp(xp_data)
    lvl = xp_level(int(entry["xp"]))
    em = ui.embed("⚡  XP Added",
                  f"{member.mention} received **+{ui.fmt(amount)} XP**.\n"
                  f"Now **Level {lvl}** with **{ui.fmt(entry['xp'])} XP**.",
                  kind="xp")
    await ctx.send(embed=em)


@bot.command(name="xpset")
@admin_only()
async def cmd_xpset(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Set exact XP. Usage: .xpset @user 500"""
    if amount < 0 or amount > 1000000:
        await ctx.send(embed=ui.err("Amount must be **0–1,000,000**."))
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    g[str(member.id)] = {"xp": amount, "last": 0}
    _save_xp(xp_data)
    em = ui.embed("⚡  XP Set",
                  f"{member.mention} is now **Level {xp_level(amount)}** "
                  f"with **{ui.fmt(amount)} XP**.",
                  kind="xp")
    await ctx.send(embed=em)


@bot.command(name="leveladd")
@admin_only()
async def cmd_leveladd(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Give levels. Usage: .leveladd @user 2"""
    if amount <= 0 or amount > 100:
        await ctx.send(embed=ui.err("Amount must be **1–100**."))
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    entry = g.setdefault(str(member.id), {"xp": 0, "last": 0})
    new_level = xp_level(int(entry["xp"])) + amount
    entry["xp"] = xp_for_level(new_level)
    _save_xp(xp_data)
    em = ui.embed("⚡  Levels Added",
                  f"{member.mention} received **+{amount} level(s)** — "
                  f"now **Level {new_level}** ({ui.fmt(entry['xp'])} XP).",
                  kind="xp")
    await ctx.send(embed=em)


@bot.command(name="levelset")
@admin_only()
async def cmd_levelset(ctx: commands.Context, member: discord.Member, level: int) -> None:
    """Set exact level. Usage: .levelset @user 5"""
    if level < 0 or level > 100:
        await ctx.send(embed=ui.err("Level must be **0–100**."))
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    g[str(member.id)] = {"xp": xp_for_level(level), "last": 0}
    _save_xp(xp_data)
    em = ui.embed("⚡  Level Set",
                  f"{member.mention} is now **Level {level}** "
                  f"({ui.fmt(xp_for_level(level))} XP).",
                  kind="xp")
    await ctx.send(embed=em)


def _try_math(text: str) -> str | None:
    """Safe arithmetic: 'what is 2+2', 'calculate 3*4', '12/4'..."""
    import ast
    import operator as op
    import re
    m = re.search(r"(?:what is|calculate|solve|maths?)\s+([-+*/().\d\s^x%×x]+)\??$",
                  text.strip(), re.I)
    if not m:
        m = re.fullmatch(r"\s*[-+*/().\d\s^x%×x]+\s*", text)
        if not m or not re.search(r"\d", text) or len(text.strip()) > 30:
            return None
    expr = m.group(1) if m.lastindex else m.group(0)
    expr = expr.replace("^", "**").replace("×", "*")
    expr = re.sub(r"(?<=\d)[xX](?=\d)", "*", expr).strip()
    if not re.fullmatch(r"[\d\s+\-*/().%]+", expr) or not re.search(r"\d", expr):
        return None
    ops = {ast.Add: op.add, ast.Sub: op.sub, ast.Mult: op.mul, ast.Div: op.truediv,
           ast.Mod: op.mod, ast.Pow: op.pow, ast.USub: op.neg, ast.UAdd: op.pos}

    def ev(node):
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.left), ev(node.right))
        if isinstance(node, ast.UnaryOp) and type(node.op) in ops:
            return ops[type(node.op)](ev(node.operand))
        raise ValueError("nope")

    try:
        result = ev(ast.parse(expr, mode="eval"))
        if isinstance(result, float) and result.is_integer():
            result = int(result)
        return f"= **{result}** 🧮"
    except Exception:
        return None


BALL8 = ["yes fr", "nah lol", "maybe idk", "definitely", "no cap yes", "cap, no",
         "ask again later", "100% yes", "doubt it", "fate says yes"]


@bot.command(name="8ball")
async def cmd_8ball(ctx: commands.Context, *, question: str = "") -> None:
    """Magic 8-ball. Usage: .8ball <question>"""
    if not question:
        await ctx.send(embed=ui.info(f"`{PREFIX}8ball <question>` — ask me anything.",
                                     "Magic 8-Ball"))
        return
    await ctx.send(embed=ui.embed("🎱 Magic 8-Ball",
                                  f"**{sanitize_mentions(question[:200])}**\n\n"
                                  f"🔮 {random.choice(BALL8)}",
                                  "brand", footer=ui.FOOTER))


@bot.command(name="rps")
async def cmd_rps(ctx: commands.Context, pick: str = "") -> None:
    """Rock paper scissors. Usage: .rps <rock|paper|scissors>"""
    pick = pick.lower().strip()
    short = {"r": "rock", "p": "paper", "s": "scissors"}
    pick = short.get(pick, pick)
    if pick not in ("rock", "paper", "scissors"):
        await ctx.send(embed=ui.info(f"`{PREFIX}rps <rock|paper|scissors>` — "
                                     "`r`, `p` and `s` work too.", "Rock Paper Scissors"))
        return
    mine = random.choice(["rock", "paper", "scissors"])
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    emo = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    if mine == pick:
        result, kind = "It's a tie!", "info"
    elif beats[mine] == pick:
        result, kind = "I win 😎", "error"
    else:
        result, kind = "You win — gg!", "success"
    await ctx.send(embed=ui.embed("✊✌️✋ Rock Paper Scissors",
                                  f"**{ctx.author.display_name}** {emo[pick]} vs "
                                  f"{emo[mine]} **Wigglesworth**\n\n→ **{result}**", kind))


@bot.command(name="roll")
async def cmd_roll(ctx: commands.Context, sides: str = "100") -> None:
    """Roll a die. Usage: .roll [sides]"""
    try:
        n = max(2, min(1000000, int(sides)))
    except ValueError:
        await ctx.send(embed=ui.info(f"`{PREFIX}roll [sides]` — between 2 and 1,000,000 sides.",
                                     "Roll a die"))
        return
    await ctx.send(embed=ui.embed("🎲 Roll",
                                  f"**{ctx.author.display_name}** rolled "
                                  f"**{random.randint(1, n)}** *(1–{n})*",
                                  "brand", footer=ui.FOOTER))


@bot.command(name="flip")
async def cmd_flip(ctx: commands.Context) -> None:
    await ctx.send(embed=ui.embed("🪙 Coin flip",
                                  f"**{random.choice(['heads', 'tails']).title()}**!",
                                  "gold", footer=ui.FOOTER))


@bot.command(name="status")
@owner_only()
async def cmd_statusbot(ctx: commands.Context, *, text: str = "") -> None:
    """Set the bot's status live. Usage: .status <text> | .status off"""
    if not text or text.lower() == "off":
        await bot.change_presence(activity=None)
        await ctx.send(embed=ui.ok("Custom status cleared.", "Status updated"))
        return
    await bot.change_presence(activity=discord.CustomActivity(name=text[:120]))
    await ctx.send(embed=ui.ok(f"Status is now **{text[:120]}**.", "Status updated"))


@bot.command(name="setbio")
@owner_only()
async def cmd_setbio(ctx: commands.Context, *, text: str = "") -> None:
    """Set the bot's About Me bio. Usage: .setbio <text> (190 max)"""
    if not text:
        await ctx.send(embed=ui.info(f"`{PREFIX}setbio <text>` — 190 characters max.",
                                     "Set bio"))
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.patch(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {TOKEN}"},
                json={"bio": text[:190]},
            ) as r:
                if r.status == 200:
                    await ctx.send(embed=ui.ok("Bio updated — check the bot's profile.",
                                               "Profile updated"))
                else:
                    await ctx.send(embed=ui.err(f"Discord refused the change (HTTP {r.status})."))
    except Exception as exc:
        await ctx.send(embed=ui.err(f"Request failed: `{type(exc).__name__}`."))


@bot.command(name="aitest")
@owner_or_admin()
async def cmd_aitest(ctx: commands.Context) -> None:
    """Test the Groq brain EXACTLY as chat uses it. Usage: .aitest"""
    if not AI_API_KEY:
        await ctx.send(embed=ui.err("No `OPENAI_API_KEY` in `.env` — restart after adding it.",
                                    "AI not configured"))
        return
    await ctx.send(embed=ui.info(f"Testing `{AI_MODEL}` with the exact chat payload…",
                                 "AI connection test"))
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as sess:
            async with sess.post(
                f"{AI_BASE_URL}/chat/completions",
                headers={"Authorization": f"Bearer {AI_API_KEY}"},
                json={"model": AI_MODEL,
                      "messages": [{"role": "system", "content": AI_SYSTEM},
                                   {"role": "user", "content": "test: who is kai cenat?"}],
                      "max_tokens": 1024, "temperature": 0.9, "reasoning_effort": "low"},
            ) as resp:
                raw = await resp.text()
                if resp.status == 200:
                    import json as _j
                    try:
                        ans = (_j.loads(raw)["choices"][0]["message"].get("content") or "").strip()
                    except Exception as exc:
                        await ctx.send(embed=ui.warn(
                            f"HTTP 200 but unparseable ({type(exc).__name__}).\n"
                            f"```{raw[:300]}```", "Odd response"))
                        return
                    if ans:
                        await ctx.send(embed=ui.ok(f"Reply would be:\n> {ans[:300]}",
                                                   "AI works"))
                    else:
                        await ctx.send(embed=ui.warn(
                            "HTTP 200 but the answer was empty — the fallback takes over. "
                            "Retrying usually fixes it.", "Empty answer"))
                else:
                    await ctx.send(embed=ui.err(f"Groq returned HTTP {resp.status}:\n"
                                                f"```{raw[:400]}```"))
    except Exception as exc:
        await ctx.send(embed=ui.err(f"Request failed: `{type(exc).__name__}: {exc}`"))


class RoleMenuView(discord.ui.View):
    def __init__(self, guild_id: int, role_ids: list[int]) -> None:
        super().__init__(timeout=None)
        for rid in role_ids[:25]:
            self.add_item(RoleToggleButton(guild_id, rid))


class RoleToggleButton(discord.ui.Button):
    def __init__(self, guild_id: int, role_id: int) -> None:
        self.guild_id = guild_id
        self.role_id = role_id
        super().__init__(label=f"role:{role_id}", style=discord.ButtonStyle.secondary,
                         custom_id=f"rr:{guild_id}:{role_id}")

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = bot.get_guild(self.guild_id)
        role = guild.get_role(self.role_id) if guild else None
        member = guild.get_member(interaction.user.id) if guild else None
        if not role or not member:
            try:
                await interaction.response.send_message(
                    embed=ui.err("That role no longer exists.", "Role unavailable"),
                    ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if role.permissions.administrator or role.managed:
            try:
                await interaction.response.send_message(
                    embed=ui.warn("Admin and bot-managed roles can't be self-served.",
                                  "Locked role"),
                    ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role menu toggle")
                box = ui.embed("🎭  Role Removed", kind="info",
                               footer=f"{ui.FOOTER} • role menu")
                box.description = f"**{role.name}** was removed from your roles."
            else:
                await member.add_roles(role, reason="Role menu toggle")
                box = ui.embed("🎭  Role Added", kind="success",
                               footer=f"{ui.FOOTER} • role menu")
                box.description = f"You now have **{role.name}**."
            await interaction.response.send_message(embed=box, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            try:
                await interaction.response.send_message(
                    embed=ui.err("I can't manage that role — my role must sit above it.",
                                 "Missing permission"),
                    ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass


def rr_register_saved() -> None:
    for gid, menus in rr_data.items():
        for m in menus:
            try:
                view = RoleMenuView(int(gid), [int(r) for r in m.get("roles", [])])
                guild = bot.get_guild(int(gid))
                if guild:
                    for b in view.children:
                        role = guild.get_role(b.role_id)
                        if role:
                            b.label = role.name[:80]
                            b.style = discord.ButtonStyle.primary
                bot.add_view(view)
            except Exception:
                pass


# ---------- giveaways ----------
GIVEAWAY_FILE = DATA_DIR / "giveaways.json"


def _load_gw() -> dict:
    try:
        d = json.loads(GIVEAWAY_FILE.read_text(encoding="utf-8")) if GIVEAWAY_FILE.exists() else {}
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save_gw(data: dict) -> None:
    try:
        GIVEAWAY_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError:
        pass


giveaway_data: dict = _load_gw()  # id -> {guild,channel,message,prize,ends_at,winners_n,host,entrants,status,...}


def parse_duration(s: str) -> int | None:
    """'10m','2h','7d','60s' -> seconds (60s..30d). None if invalid."""
    import re as _re
    m = _re.match(r"^\s*(\d+)\s*([smhd])\s*$", (s or "").lower())
    if not m:
        return None
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
    return secs if 60 <= secs <= 30 * 86400 else None


def gw_embed(g: dict, ended: bool = False) -> discord.Embed:
    prize, n, ends = g["prize"], int(g["winners_n"]), int(g["ends_at"])
    entries = len(g.get("entrants", []))
    if ended:
        winners = g.get("winners", [])
        em = ui.embed("🎉  Giveaway Ended", kind="success",
                      footer=f"{ui.FOOTER} • giveaway results")
        em.description = f"## {prize}"
        em.add_field(name="🏆 Winner(s)",
                     value=", ".join(f"<@{w}>" for w in winners) if winners
                     else "*No entries — nobody wins this time.*",
                     inline=False)
        em.add_field(name="👥 Entries", value=f"**{entries}**", inline=True)
        return em
    host = f"<@{g['host']}>" if str(g.get("host", "")).isdigit() and int(g["host"]) else "the dashboard"
    em = ui.embed("🎁  Giveaway", kind="gold", footer=f"{ui.FOOTER} • click 🎉 to enter")
    em.description = f"## {prize}"
    em.add_field(name="🏆 Winners", value=f"**{n}**", inline=True)
    em.add_field(name="👥 Entries", value=f"**{entries}**", inline=True)
    em.add_field(name="⏰ Ends", value=f"<t:{ends}:R>", inline=True)
    em.add_field(name="👤 Hosted by", value=host, inline=True)
    return em


class GiveawayView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Giveaway", emoji="🎉",
                       style=discord.ButtonStyle.success, custom_id="gw_enter")
    async def enter(self, interaction: discord.Interaction,
                    button: discord.ui.Button) -> None:
        guild = interaction.guild
        if guild is None:
            return
        g = next((x for x in giveaway_data.values()
                  if x.get("guild") == str(guild.id)
                  and x.get("message") == interaction.message.id
                  and x.get("status") == "active"), None)
        if g is None:
            try:
                await interaction.response.send_message(
                    embed=ui.warn("This giveaway already ended.", "Giveaways"),
                    ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        uid = str(interaction.user.id)
        entrants = g.setdefault("entrants", [])
        if uid in entrants:
            entrants.remove(uid)
            msg, kind = "You left the giveaway.", "info"
        else:
            entrants.append(uid)
            msg, kind = f"You're in! Good luck 🎉 ({len(entrants)} entries)", "success"
        _save_gw(giveaway_data)
        try:
            await interaction.message.edit(embed=gw_embed(g))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        try:
            await interaction.response.send_message(embed=ui.embed("🎁 Giveaway", msg, kind),
                                                    ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass


def _gw_find(guild_id: int, gid: str | None = None, active_only: bool = False) -> dict | None:
    items = [g for g in giveaway_data.values() if g.get("guild") == str(guild_id)]
    if gid:
        gid = gid.strip().lower()
        items = [g for g in items if g["id"].lower().startswith(gid)]
    if active_only:
        items = [g for g in items if g.get("status") == "active"]
    items.sort(key=lambda g: g.get("created", 0), reverse=True)
    return items[0] if items else None


async def create_giveaway(guild: discord.Guild, channel, prize: str,
                          dur_s: int, winners_n: int, host_id: int) -> str:
    gid = uuid.uuid4().hex[:8]
    now = time.time()
    g = {"id": gid, "guild": str(guild.id), "channel": channel.id, "message": 0,
         "prize": prize[:200], "duration_s": dur_s, "ends_at": now + dur_s,
         "winners_n": max(1, min(10, winners_n)), "host": str(host_id),
         "entrants": [], "status": "active", "winners": [], "created": now, "rerolls": 0}
    msg = await channel.send(embed=gw_embed(g), view=GiveawayView())
    g["message"] = msg.id
    giveaway_data[gid] = g
    _save_gw(giveaway_data)
    return gid


async def end_giveaway(gid: str, by: str = "auto") -> str:
    g = giveaway_data.get(gid)
    if g is None:
        return "❌ Giveaway not found."
    if g.get("status") != "active":
        return "⚠️ That giveaway already ended."
    g["status"] = "ended"
    g["ended_at"] = time.time()
    g["ended_by"] = str(by)
    entrants = list(g.get("entrants", []))
    n = min(int(g["winners_n"]), len(entrants))
    g["winners"] = random.sample(entrants, n) if n else []
    _save_gw(giveaway_data)
    guild = bot.get_guild(int(g["guild"]))
    if guild is None:
        return "✅ Ended (server gone)."
    channel = guild.get_channel(g["channel"])
    mention = ", ".join(f"<@{w}>" for w in g["winners"]) if g["winners"] else "nobody"
    try:
        if channel:
            try:
                msg = await channel.fetch_message(g["message"])
                await msg.edit(embed=gw_embed(g, ended=True), view=None)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
            if g["winners"]:
                await channel.send(f"🎉 Congrats {mention}! You won **{g['prize']}**!")
            else:
                await channel.send(f"😢 Giveaway **{g['prize']}** ended with no entries.")
    except (discord.Forbidden, discord.HTTPException):
        pass
    if g["winners"]:
        return f"✅ Ended — winner(s): {mention}"
    return "✅ Ended with no entries."


async def reroll_giveaway(gid: str) -> str:
    g = giveaway_data.get(gid)
    if g is None:
        return "❌ Giveaway not found."
    if g.get("status") != "ended":
        return "⚠️ Only ended giveaways can be rerolled. End it first."
    entrants = list(g.get("entrants", []))
    if not entrants:
        return "😢 No entries to reroll from."
    prev = set(g.get("winners", []))
    pool = [u for u in entrants if u not in prev] or entrants
    n = min(int(g["winners_n"]), len(pool))
    g["winners"] = random.sample(pool, n)
    g["rerolls"] = int(g.get("rerolls", 0)) + 1
    _save_gw(giveaway_data)
    guild = bot.get_guild(int(g["guild"]))
    mention = ", ".join(f"<@{w}>" for w in g["winners"])
    if guild:
        channel = guild.get_channel(g["channel"])
        if channel:
            try:
                try:
                    msg = await channel.fetch_message(g["message"])
                    await msg.edit(embed=gw_embed(g, ended=True))
                except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                    pass
                await channel.send(f"🔁 Reroll! New winner(s): {mention} — **{g['prize']}**!")
            except (discord.Forbidden, discord.HTTPException):
                pass
    return f"✅ Rerolled — new winner(s): {mention}"


@tasks.loop(seconds=30)
async def gw_loop() -> None:
    now = time.time()
    for gid, g in list(giveaway_data.items()):
        if g.get("status") == "active" and float(g.get("ends_at", 0)) <= now:
            try:
                await end_giveaway(gid)
            except Exception as exc:
                print(f"[giveaway] auto-end failed {gid}: {exc}")


@bot.command(name="rolemenu")
@owner_or_admin()
async def cmd_rolemenu(ctx: commands.Context, *args: str) -> None:
    """Role buttons. Usage: .rolemenu @role1 @role2 ... | .rolemenu clear"""
    if args and args[0].lower() in ("clear", "off", "reset"):
        for m in rr_data.get(str(ctx.guild.id), []):
            try:
                ch = ctx.guild.get_channel(m["channel"])
                msg = await ch.fetch_message(m["message"]) if ch else None
                if msg:
                    await msg.delete()
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        rr_data[str(ctx.guild.id)] = []
        _save_rr(rr_data)
        await ctx.send(embed=ui.ok("All role menus were deleted.", "Role menu cleared"))
        return
    rids = []
    for a in args:
        try:
            r = ctx.guild.get_role(int(a.strip("<>@#!&")))
        except ValueError:
            continue
        if r and r != ctx.guild.default_role and r.id not in rids \
                and not r.permissions.administrator and not r.managed:
            rids.append(r.id)
    if not rids:
        await ctx.send(embed=ui.info(f"`{PREFIX}rolemenu @role1 @role2 ...`\n"
                                     "Admin and bot-managed roles can't be self-served.",
                                     "Role menu"))
        return
    embed = ui.embed("🎭  Pick Your Roles",
                     "Tap a button to add or remove a role — changes apply instantly.",
                     kind="brand", footer=f"{ui.FOOTER} • buttons stay live after restarts")
    view = RoleMenuView(ctx.guild.id, rids)
    for b in view.children:
        role = ctx.guild.get_role(b.role_id)
        if role:
            b.label = role.name[:80]
            b.style = discord.ButtonStyle.primary
    try:
        msg = await ctx.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"I couldn't post the menu: {exc}"))
        return
    rr_data.setdefault(str(ctx.guild.id), []).append(
        {"channel": ctx.channel.id, "message": msg.id, "roles": rids})
    _save_rr(rr_data)
    await ctx.send(embed=ui.ok(f"Menu live with **{len(rids)}** role(s) — "
                               "it survives restarts.", "Role menu posted"))


# ---------- tickets removed ----------


@bot.command(name="raidconfig")
@admin_only()
async def cmd_config(ctx: commands.Context, key: str = "", *, value: str = "") -> None:
    cfg = get_config(ctx.guild.id)
    if not key:
        lines = [f"**{k}**: `{v}`" for k, v in cfg.items()
                 if k not in ("whitelisted_user_ids", "whitelisted_role_ids")]
        await ctx.send(embed=ui.embed(f"⚙️ Configuration — {ctx.guild.name}",
                                      "\n".join(lines)[:4000], "brand",
                                      footer=f"{ui.FOOTER} • `{PREFIX}raidconfig <key> <value>` to change"))
        return
    key = key.strip()
    if key not in DEFAULT_GUILD_CONFIG:
        await ctx.send(embed=ui.err(f"Unknown key `{key}`.\nValid keys: "
                                    + ", ".join(f"`{k}`" for k in DEFAULT_GUILD_CONFIG)))
        return
    # type coercion
    default = DEFAULT_GUILD_CONFIG[key]
    try:
        if isinstance(default, bool):
            v = value.strip().lower()
            if v in ("true", "on", "yes", "1"):
                parsed = True
            elif v in ("false", "off", "no", "0"):
                parsed = False
            else:
                await ctx.send(embed=ui.err("Use `true` or `false`."))
                return
        elif isinstance(default, int) and key != "log_channel_id":
            parsed: object = int(value)
        elif key == "log_channel_id":
            parsed = int(value.strip("<#>")) if value.strip() not in ("none", "null", "0") else None
        elif isinstance(default, list):
            if key == "automod_words":
                parsed = [x.strip().lower() for x in value.split(",") if x.strip()][:100]
            else:
                parsed = [int(x.strip("<>@#!&")) for x in value.split(",") if x.strip()]
        else:
            parsed = value.strip()
            if key == "raid_action" and parsed not in VALID_ACTIONS:
                await ctx.send(embed=ui.err(f"`raid_action` must be one of "
                                            f"{sorted(VALID_ACTIONS)}."))
                return
        update_config(ctx.guild.id, **{key: parsed})
        await ctx.send(embed=ui.ok(f"`{key}` is now `{parsed}`.", "Config updated"))
    except ValueError:
        await ctx.send(embed=ui.err("Could not parse that value — expected a number or ID."))


@bot.command(name="setwelcome")
@owner_or_admin()
async def cmd_setwelcome(ctx: commands.Context, channel: str = "") -> None:
    """Welcome channel. Usage: .setwelcome #channel | .setwelcome off"""
    if channel.strip().lower() in ("off", "none", "clear", ""):
        if not channel:
            cid = get_config(ctx.guild.id).get("welcome_channel_id")
            ch = ctx.guild.get_channel(cid) if cid else None
            await ctx.send(embed=ui.info(f"Welcome channel: {ch.mention if ch else '**not set**'}.\n"
                                         f"Set one with `{PREFIX}setwelcome #channel`.",
                                         "Welcome messages"))
            return
        update_config(ctx.guild.id, welcome_channel_id=None)
        await ctx.send(embed=ui.ok("Welcome messages are off.", "Welcomes disabled"))
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None:
        await ctx.send(embed=ui.info(f"`{PREFIX}setwelcome #channel` or `{PREFIX}setwelcome off`",
                                     "Welcomes"))
        return
    update_config(ctx.guild.id, welcome_channel_id=target.id)
    await ctx.send(embed=ui.ok(f"New members will be welcomed in {target.mention}.",
                               "Welcomes enabled"))


@bot.command(name="setgoodbye")
@owner_or_admin()
async def cmd_setgoodbye(ctx: commands.Context, channel: str = "") -> None:
    """Goodbye channel. Usage: .setgoodbye #channel | .setgoodbye off"""
    if channel.strip().lower() in ("off", "none", "clear", ""):
        if not channel:
            cid = get_config(ctx.guild.id).get("goodbye_channel_id")
            ch = ctx.guild.get_channel(cid) if cid else None
            await ctx.send(embed=ui.info(f"Goodbye channel: {ch.mention if ch else '**not set**'}.\n"
                                         f"Set one with `{PREFIX}setgoodbye #channel`.",
                                         "Goodbye messages"))
            return
        update_config(ctx.guild.id, goodbye_channel_id=None)
        await ctx.send(embed=ui.ok("Goodbye messages are off.", "Goodbyes disabled"))
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None:
        await ctx.send(embed=ui.info(f"`{PREFIX}setgoodbye #channel` or `{PREFIX}setgoodbye off`",
                                     "Goodbyes"))
        return
    update_config(ctx.guild.id, goodbye_channel_id=target.id)
    await ctx.send(embed=ui.ok(f"Farewells will be posted in {target.mention}.",
                               "Goodbyes enabled"))


@bot.command(name="linkchannel")
@owner_or_admin()
async def cmd_linkchannel(ctx: commands.Context, channel: str = "") -> None:
    """Toggle a link-safe channel. Usage: .linkchannel [#channel] (blank = list)"""
    cfg = get_config(ctx.guild.id)
    ids = list(cfg.get("link_allowed_channels", []) or [])
    if not channel:
        names = [f"<#{i}>" for i in ids] or ["none — links are filtered everywhere"]
        await ctx.send(embed=ui.embed("🔗 Link-safe channels", ", ".join(names), "info",
                                      footer=f"{ui.FOOTER} • `{PREFIX}linkchannel #channel` to toggle"))
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None or not isinstance(target, discord.TextChannel):
        await ctx.send(embed=ui.info(f"`{PREFIX}linkchannel #channel` — or run it with no "
                                     "argument to list the current channels.",
                                     "Link-safe channels"))
        return
    if target.id in ids:
        ids.remove(target.id)
        em = ui.ok(f"{target.mention} filters links again.", "Link safety on")
    else:
        ids.append(target.id)
        em = ui.ok(f"{target.mention} is now link-safe (invites + links allowed).",
                   "Link safety off")
    update_config(ctx.guild.id, link_allowed_channels=ids)
    await ctx.send(embed=em)


@bot.command(name="setlog")
@owner_or_admin()
async def cmd_setlog(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or ctx.channel
    update_config(ctx.guild.id, log_channel_id=target.id)
    await ctx.send(embed=ui.ok(f"Raid and security logs will go to {target.mention}.",
                               "Log channel set"))


@bot.command(name="setchat")
@owner_or_admin()
async def cmd_setchat(ctx: commands.Context, channel: str = "") -> None:
    """Lock bot chat to one channel. Usage: .setchat #channel | .setchat off | .setchat"""
    channel = channel.strip().lower()
    if not channel:
        cfg = get_config(ctx.guild.id)
        cid = cfg.get("chat_channel_id")
        ch = ctx.guild.get_channel(cid) if cid else None
        await ctx.send(embed=ui.info(f"Chat channel: {ch.mention if ch else '**anywhere**'}.\n"
                                     f"Lock it down with `{PREFIX}setchat #channel` or "
                                     f"`{PREFIX}setchat off`.", "Chatbot"))
        return
    if channel in ("off", "none", "clear", "anywhere"):
        update_config(ctx.guild.id, chat_channel_id=None)
        await ctx.send(embed=ui.ok("The bot will chat anywhere again.", "Chatbot"))
        return
    try:
        cid = int(channel.strip("<#>"))
    except ValueError:
        await ctx.send(embed=ui.info(f"`{PREFIX}setchat #channel` or `{PREFIX}setchat off`",
                                     "Chatbot"))
        return
    target = ctx.guild.get_channel(cid)
    if target is None:
        await ctx.send(embed=ui.err("I can't see that channel — check the ID or my permissions."))
        return
    update_config(ctx.guild.id, chat_channel_id=target.id)
    await ctx.send(embed=ui.ok(f"The bot will only chat in {target.mention}.", "Chatbot locked"))


ABUSE_COOLDOWN_S = 300  # one abuse ping per 5 min per server
abuse_cd: dict[int, float] = {}


@bot.command(name="abuse")
async def cmd_abuse(ctx: commands.Context, *, reason: str = "") -> None:
    """Post an abuse alert (no pings). Usage: .abuse [what's happening in game]"""
    if ctx.guild is None:
        return
    cfg = get_config(ctx.guild.id)
    now = time.time()
    last = abuse_cd.get(ctx.guild.id, 0)
    if now - last < ABUSE_COOLDOWN_S:
        wait = int(ABUSE_COOLDOWN_S - (now - last))
        await ctx.send(embed=ui.warn(f"Already reported — try again in **{wait}s**. "
                                     "Mods have been notified.", "Cooldown"))
        return
    abuse_cd[ctx.guild.id] = now
    clean = sanitize_mentions(reason.strip()[:300])
    desc = f"Reported by {ctx.author.mention} in {ctx.channel.mention}"
    if clean:
        desc += f"\n> {clean}"
    notify = ctx.guild.get_role(cfg.get("abuse_role_id") or 0)
    if notify is not None:
        desc += f"\nNotify: {sanitize_mentions(notify.name)}"
    em = E("🚨 ADMIN ABUSE — get in game NOW", desc, kind="raid")
    target = ctx.guild.get_channel(cfg.get("abuse_channel_id") or 0) or ctx.channel
    try:
        await target.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
        return
    if target.id != ctx.channel.id:
        await ctx.send(embed=ui.ok("Report sent — help is on the way.", "🚨 Abuse reported"))


@bot.command(name="setabuserole")
@owner_or_admin()
async def cmd_setabuserole(ctx: commands.Context, role: discord.Role | None = None) -> None:
    """Set the role .abuse pings. Usage: .setabuserole @role"""
    if role is None:
        cur = ctx.guild.get_role(get_config(ctx.guild.id).get("abuse_role_id") or 0)
        await ctx.send(embed=ui.info(f"Abuse role: {cur.mention if cur else '**not set**'}.\n"
                                     f"Set one with `{PREFIX}setabuserole @role`.",
                                     "Abuse alerts"))
        return
    update_config(ctx.guild.id, abuse_role_id=role.id)
    await ctx.send(embed=ui.ok(f"`.abuse` will now ping {role.mention}.", "Abuse alerts"))


@bot.command(name="setabusechannel")
@owner_or_admin()
async def cmd_setabusechannel(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    """Set where .abuse pings go. Usage: .setabusechannel #channel"""
    if channel is None:
        cur = ctx.guild.get_channel(get_config(ctx.guild.id).get("abuse_channel_id") or 0)
        await ctx.send(embed=ui.info(f"Abuse channel: "
                                     f"{cur.mention if cur else 'wherever `.abuse` is used'}.\n"
                                     f"Set one with `{PREFIX}setabusechannel #channel`.",
                                     "Abuse alerts"))
        return
    update_config(ctx.guild.id, abuse_channel_id=channel.id)
    await ctx.send(embed=ui.ok(f"Abuse pings will go to {channel.mention}.", "Abuse alerts"))


@bot.command(name="vcjoin")
@trusted_only()
async def cmd_vcjoin(ctx: commands.Context, channel: str = "") -> None:
    """Bot joins a VC and sits in it. Usage: .vcjoin [#voice] (staff/whitelist only)."""
    target = None
    if channel:
        try:
            c = ctx.guild.get_channel(int(channel.strip("<#>")))
            if isinstance(c, discord.VoiceChannel):
                target = c
        except ValueError:
            pass
    elif ctx.author.voice and ctx.author.voice.channel:
        target = ctx.author.voice.channel
    if target is None:
        await ctx.send(embed=ui.info(f"Sit in a voice channel first, or give me one: "
                                     f"`{PREFIX}vcjoin #voice`.", "Voice"))
        return
    try:
        vc = ctx.guild.voice_client
        if vc and vc.channel and vc.channel.id == target.id:
            await ctx.send(embed=ui.info(f"I'm already in {target.mention}.", "Voice"))
            return
        if vc:
            await vc.move_to(target)
        else:
            await target.connect()
        update_config(ctx.guild.id, mod_vc_channel_id=target.id)
        await ctx.send(embed=ui.ok(f"Joined {target.mention} — moderating that VC. "
                                   f"`{PREFIX}vcleave` to send me away.", "🎧 Voice"))
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"I can't join (need Connect permission): {exc}"))


async def speak_text(guild: discord.Guild, text: str) -> str:
    """Play TTS in the bot's current VC. Returns status for logs/errors."""
    import re as _re
    import tempfile as _tf
    vc = guild.voice_client
    if not vc or not vc.channel:
        return "not in voice — `.vcjoin` first"
    import shutil as _sh
    if not _sh.which("ffmpeg"):
        try:
            import imageio_ffmpeg  # noqa
        except ImportError:
            return "no ffmpeg on this host — redeploy so requirements install"
    clean = _re.sub(r"[*_~>|`]", "", text)[:300] or "hello"
    voice = VOICES.get(get_config(guild.id).get("tts_voice", "aria"), "en-US-AriaNeural")
    mp3 = None
    tts_err = ""
    for attempt_voice in (voice, "en-US-GuyNeural"):
        try:
            import edge_tts
            tmp = _tf.NamedTemporaryFile(delete=False, suffix=".mp3")
            tmp.close()
            await edge_tts.Communicate(clean, voice=attempt_voice).save(tmp.name)
            import os as _os
            if _os.path.getsize(tmp.name) < 500:
                raise ValueError("empty audio back from TTS")
            mp3 = tmp.name
            break
        except Exception as exc:
            tts_err = f"{type(exc).__name__}: {exc}"[:200]
            print(f"[voice] TTS failed ({attempt_voice}): {tts_err}")
            try:
                _os.remove(tmp.name)
            except Exception:
                pass
    if not mp3:
        return f"TTS gave silence ({tts_err or 'unknown'}). Try `.speak hello` to test."
    try:
        import edge_tts
        import imageio_ffmpeg
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        return "voice engine missing — redeploy so requirements install"
    import os as _os2
    try:
        while vc.is_playing():
            await asyncio.sleep(0.5)
        try:
            vc.play(discord.FFmpegPCMAudio(mp3, executable=ffmpeg_exe),
                    after=lambda e: _os2.remove(mp3) if _os2.path.exists(mp3) else None)
        except discord.ClientException:
            try:
                _os2.remove(mp3)
            except OSError:
                pass
            return "already playing — wait a sec, or the voice link died (rejoin with `.vcjoin`)"
        return "speaking"
    except Exception as exc:
        return f"voice failed: {type(exc).__name__}"


VOICES = {
    "aria": "en-US-AriaNeural",
    "jenny": "en-US-JennyNeural",
    "guy": "en-US-GuyNeural",
    "davis": "en-US-DavisNeural",
    "jane": "en-US-JaneNeural",
    "sara": "en-US-SaraNeural",
    "tony": "en-US-TonyNeural",
    "nancy": "en-US-NancyNeural",
}


@bot.command(name="voice")
@trusted_only()
async def cmd_voice(ctx: commands.Context, name: str = "") -> None:
    """Pick the AI voice. Usage: .voice [aria|jenny|guy|davis|jane|sara|tony|nancy] (staff/whitelist only)."""
    name = name.lower().strip()
    if not name:
        cur = get_config(ctx.guild.id).get("tts_voice", "aria")
        await ctx.send(embed=ui.info(f"Current voice: **{cur}**\n"
                                     f"Available: {', '.join(f'`{v}`' for v in sorted(VOICES))}",
                                     "TTS voice"))
        return
    if name not in VOICES:
        await ctx.send(embed=ui.err(f"Unknown voice `{name}`. Available: "
                                    f"{', '.join(f'`{v}`' for v in sorted(VOICES))}"))
        return
    update_config(ctx.guild.id, tts_voice=name)
    await ctx.send(embed=ui.ok(f"TTS voice set to **{name}**.", "Voice updated"))


@bot.command(name="ask")
async def cmd_ask(ctx: commands.Context, *, question: str = "") -> None:
    """Ask the AI out loud in VC. Usage: .ask <question> (bot must be in VC)"""
    if not question:
        await ctx.send(embed=ui.info(f"`{PREFIX}ask <question>` — I'll think, then say it out "
                                     "loud in your voice channel.", "Ask out loud"))
        return
    ask_savage = bool(ctx.guild and get_config(ctx.guild.id).get("bot_mood") == "savage")
    ask_low = " " + question.lower() + " "
    if ask_savage and any(p in ask_low for p in SWEAR_REQUESTS):
        answer = random.choice(SAVAGE_SWEAR_FLEX).format(n=ctx.author.display_name)
    elif has_word(ask_low, RAP_PHRASES, RAP_WORDS):
        verses = SAVAGE_RAPS if ask_savage else CLEAN_RAPS
        answer = random.choice(verses).format(n=ctx.author.display_name)
    elif ask_savage and talking_crazy(ask_low):
        answer = random.choice(CRAZY_COMEBACKS).format(n=ctx.author.display_name)
    else:
        answer = await ai_reply(ctx.channel.id, ctx.author.display_name, question,
                                ctx.guild.id if ctx.guild else None)
    if (answer and ctx.guild and get_config(ctx.guild.id).get("bot_mood") == "savage"
            and any(p in answer.lower() for p in CLEAN_TALK)):
        answer = random.choice(SAVAGE_SWEAR_LINES)
    if (answer and ctx.guild and get_config(ctx.guild.id).get("bot_mood") == "savage"
            and any(p in answer.lower() for p in PREACHY)):
        answer = random.choice(SAVAGE_COMEBACKS).format(n=ctx.author.display_name)
    if (answer and ctx.guild and get_config(ctx.guild.id).get("bot_mood") == "savage"
            and any(p in answer.lower() for p in NICE_TALK)):
        answer = random.choice(SAVAGE_COMEBACKS).format(n=ctx.author.display_name)
    if not answer or is_refusal(answer):
        base = _fallback_reply(ctx.author.display_name, question)
        low_q = (" " + question.lower() + " ")
        if looks_like_question(question) and not any(t in low_q for t in SELF_TOPICS):
            found = await web_search_answer(question)
            answer = f"looked it up:\n{found}" if found else base
        else:
            answer = base
    if not answer or not answer.strip():
        answer = _fallback_reply(ctx.author.display_name, question)
    if is_blocked_topic(answer):
        answer = random.choice(BLOCKED_REPLY)
    answer = mood_wrap(sanitize_mentions(answer),
                       ctx.guild.id if ctx.guild else None)
    await ctx.send(answer[:1900])
    status = await speak_text(ctx.guild, answer[:300])
    if status != "speaking":
        await ctx.send(embed=ui.warn(f"Voice playback: {status}", "🔇 Voice"))


@bot.command(name="speak")
@trusted_only()
async def cmd_speak(ctx: commands.Context, *, text: str = "") -> None:
    """Bot says it out loud in VC. Usage: .speak <text> (staff/whitelist only)."""
    if not text:
        await ctx.send(embed=ui.info(f"`{PREFIX}speak <text>` — join a voice channel with "
                                     f"`{PREFIX}vcjoin` first.", "Speak"))
        return
    status = await speak_text(ctx.guild, text)
    if status != "speaking":
        await ctx.send(embed=ui.warn(f"Voice playback: {status}", "🔇 Voice"))


@bot.command(name="voiceauto")
@trusted_only()
async def cmd_voiceauto(ctx: commands.Context, state: str = "") -> None:
    """Speak ping replies aloud when in VC. Usage: .voiceauto <on|off> (staff/whitelist only)."""
    state = state.lower().strip()
    if state in ("on", "off"):
        update_config(ctx.guild.id, voice_auto=(state == "on"))
        await ctx.send(embed=ui.ok(f"Voice replies are now **{state.upper()}**.",
                                   "🔊 Voice replies"))
    else:
        cur = get_config(ctx.guild.id).get("voice_auto", False)
        await ctx.send(embed=ui.info(f"Voice replies: **{'ON' if cur else 'OFF'}**.\n"
                                     f"Toggle with `{PREFIX}voiceauto on|off`.",
                                     "🔊 Voice replies"))


@bot.command(name="vcleave")
@trusted_only()
async def cmd_vcleave(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client if ctx.guild else None
    if not vc:
        await ctx.send(embed=ui.info("I'm not in a voice channel.", "Voice"))
        return
    try:
        await vc.disconnect()
    except (discord.Forbidden, discord.HTTPException):
        pass
    update_config(ctx.guild.id, mod_vc_channel_id=None)
    await ctx.send(embed=ui.ok("Left the voice channel.", "👋 Voice"))


@bot.command(name="setlobby")
@trusted_only()
async def cmd_setlobby(ctx: commands.Context, channel: str = "") -> None:
    """Join-to-create voice setup. Usage: .setlobby #voice | .setlobby off | .setlobby (staff/whitelist only)."""
    channel = channel.strip().lower()
    if not channel:
        cfg = get_config(ctx.guild.id)
        cid = cfg.get("lobby_channel_id")
        ch = ctx.guild.get_channel(cid) if cid else None
        await ctx.send(embed=ui.info(f"Lobby VC: {ch.mention if ch else '**not set**'}.\n"
                                     f"Members join it and I spin up a personal VC. "
                                     f"Set one with `{PREFIX}setlobby #voice`.",
                                     "Join-to-create"))
        return
    if channel in ("off", "none", "clear"):
        update_config(ctx.guild.id, lobby_channel_id=None)
        await ctx.send(embed=ui.ok("The join-to-create lobby is off.", "Lobby system"))
        return
    try:
        cid = int(channel.strip("<#>"))
    except ValueError:
        await ctx.send(embed=ui.info(f"`{PREFIX}setlobby #voice-channel` — must be a "
                                     "**voice** channel.", "Join-to-create"))
        return
    target = ctx.guild.get_channel(cid)
    if not isinstance(target, discord.VoiceChannel):
        await ctx.send(embed=ui.err("That's not a voice channel — create one first "
                                    "(e.g. `＋ Create`)."))
        return
    update_config(ctx.guild.id, lobby_channel_id=target.id)
    await ctx.send(embed=ui.ok(f"Members who join {target.mention} get a personal VC, "
                               "deleted when empty.", "Lobby enabled"))


@bot.command(name="whitelist")
@admin_only()
async def cmd_whitelist(ctx: commands.Context, action: str = "", target: str = "") -> None:
    cfg = get_config(ctx.guild.id)
    action = action.lower()
    if action == "list":
        await ctx.send(embed=ui.embed(
            "✅ Whitelist",
            f"**Users:** {cfg['whitelisted_user_ids'] or '—'}\n"
            f"**Roles:** {cfg['whitelisted_role_ids'] or '—'}",
            "info", footer=f"{ui.FOOTER} • trusted members bypass automod"))
        return
    # accept user or role mention/ID: <@123>, <@&123>, raw ID
    raw = target.strip("<>@#!&")
    try:
        tid = int(raw)
    except ValueError:
        await ctx.send(embed=ui.info(f"`{PREFIX}whitelist add|remove <@user|@role|ID>` · "
                                     f"`{PREFIX}whitelist list`", "Whitelist"))
        return
    role = ctx.guild.get_role(tid) if ctx.guild else None
    if role is not None:
        roles = list(cfg.get("whitelisted_role_ids", []))
        if action == "add" and tid not in roles:
            roles.append(tid)
        elif action == "remove" and tid in roles:
            roles.remove(tid)
        else:
            await ctx.send(embed=ui.info(f"`{PREFIX}whitelist add|remove <@role>`", "Whitelist"))
            return
        update_config(ctx.guild.id, whitelisted_role_ids=roles)
        await ctx.send(embed=ui.ok(f"Whitelist role {'added' if action == 'add' else 'removed'}: "
                                   f"**{role.name}**.", "Whitelist updated"))
        return
    users = list(cfg["whitelisted_user_ids"])
    if action == "add" and tid not in users:
        users.append(tid)
    elif action == "remove" and tid in users:
        users.remove(tid)
    else:
        await ctx.send(embed=ui.info(f"`{PREFIX}whitelist add|remove <@user|@role|ID>`",
                                     "Whitelist"))
        return
    update_config(ctx.guild.id, whitelisted_user_ids=users)
    await ctx.send(embed=ui.ok(f"Whitelist {'added' if action == 'add' else 'removed'}: `{tid}`.",
                               "Whitelist updated"))


# ---------- verify gate (dyno-style) ----------

def verify_prompt_embed(guild_name: str) -> discord.Embed:
    em = ui.embed("🔐  Verify to Enter", kind="brand",
                  footer=f"{ui.FOOTER} • verification")
    em.description = (f"**New here?** Click the button below to unlock **{guild_name}**.\n"
                      "Trouble with the button? Type `.verify`.")
    return em


class VerifyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="Verify me", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="wigglesworth_verify")
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = get_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("verified_role_id") or 0)
        if role is None:
            await interaction.response.send_message(
                embed=ui.err("Verification isn't set up on this server — tell an admin.",
                             "Not configured"),
                ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message(
                embed=ui.info("You're already verified — welcome!"),
                ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Self-verify button")
            em = ui.ok(f"Welcome to **{interaction.guild.name}**! You now have full access.",
                       "Verified!")
            await interaction.response.send_message(embed=em, ephemeral=True)
            await send_log(interaction.guild, "", event="verify", embed=ui.embed(
                "✅ Member verified",
                f"**{interaction.user.display_name}** (`{interaction.user.id}`)",
                "success", footer=f"{ui.FOOTER} • verification"))
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                embed=ui.err("I can't assign the Verified role — my role must sit above it. "
                             "Tell an admin.", "Missing permission"),
                ephemeral=True)


async def verify_lockdown(guild: discord.Guild, verify_channel_id: int) -> tuple[int, int]:
    """Hide every channel from @everyone except verify. Returns (locked, skipped)."""
    everyone = guild.default_role
    locked, skipped = 0, 0
    for ch in list(guild.text_channels) + list(guild.voice_channels) + list(guild.categories):
        try:
            if ch.id == verify_channel_id:
                await ch.set_permissions(everyone, view_channel=True, read_messages=True,
                                         send_messages=True, reason="Verify gate: entry channel")
                continue
            ow = ch.overwrites_for(everyone)
            if ow.view_channel is False:
                skipped += 1
                continue
            await ch.set_permissions(everyone, view_channel=False, reason="Verify gate setup")
            locked += 1
        except (discord.Forbidden, discord.HTTPException):
            skipped += 1
    return locked, skipped


@bot.command(name="verifysetup")
@owner_or_admin()
async def cmd_verifysetup(ctx: commands.Context, *args: str) -> None:
    """Gate setup. Usage: .verifysetup [@role] [#channel] — omits are created."""
    guild = ctx.guild
    vrole: discord.Role | None = None
    vchan: discord.TextChannel | None = None
    for a in args:
        try:
            rid = int(a.strip("<>@#!&"))
        except ValueError:
            continue
        r = guild.get_role(rid)
        if r and vrole is None:
            vrole = r
            continue
        c = guild.get_channel(rid)
        if isinstance(c, discord.TextChannel) and vchan is None:
            vchan = c
    if vrole is None:
        try:
            vrole = await guild.create_role(name="Verified", reason="Verify gate setup")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=ui.err(f"I couldn't create the role (need **Manage Roles**): "
                                        f"{exc}"))
            return
    if vchan is None:
        try:
            vchan = await guild.create_text_channel("verify", reason="Verify gate setup",
                                                    topic="Click ✅ Verify me below to unlock the server!")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(embed=ui.err(f"I couldn't create the channel (need **Manage Channels**): "
                                        f"{exc}"))
            return
    locked, skipped = await verify_lockdown(guild, vchan.id)
    # Verified role sees everything @everyone lost
    for ch in list(guild.text_channels) + list(guild.voice_channels):
        try:
            await ch.set_permissions(vrole, view_channel=True, reason="Verify gate setup")
        except (discord.Forbidden, discord.HTTPException):
            pass
    update_config(guild.id, verify_channel_id=vchan.id, verified_role_id=vrole.id,
                  verify_enabled=True)
    # verified people don't need the gate channel anymore
    try:
        await vchan.set_permissions(vrole, view_channel=False, reason="Verify gate setup")
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await vchan.send(embed=verify_prompt_embed(guild.name), view=VerifyView())
    except (discord.Forbidden, discord.HTTPException):
        pass
    await ctx.send(embed=ui.ok(f"Gate live — {vchan.mention} with role `{vrole.name}`. "
                               f"Locked **{locked}** channel(s)"
                               + (f", skipped {skipped} (missing permissions)." if skipped else "."),
                               "✅ Verify gate"))


@bot.group(name="giveaway", invoke_without_command=True)
async def cmd_giveaway(ctx: commands.Context) -> None:
    """Giveaways. Usage: .giveaway create|end|reroll|list|info"""
    await ctx.send(embed=ui.embed(
        "🎁 Giveaways",
        (f"`{PREFIX}giveaway create <prize> | <duration> | <winners> [#channel]`\n"
         f"e.g. `{PREFIX}giveaway create Nitro | 24h | 1`\n"
         f"`{PREFIX}giveaway end [id]` · `{PREFIX}giveaway reroll [id]` · "
         f"`{PREFIX}giveaway list` · `{PREFIX}giveaway info <id>`"),
        "gold", footer=f"{ui.FOOTER} • giveaways"))


@cmd_giveaway.command(name="create")
@owner_or_admin()
async def cmd_giveaway_create(ctx: commands.Context, *, args: str = "") -> None:
    """Start a giveaway. Usage: .giveaway create Nitro | 24h | 1 [#channel]"""
    import re as _re
    parts = [p.strip() for p in args.split("|")]
    usage = (f"`{PREFIX}giveaway create <prize> | <duration> | <winners> [#channel]`\n"
             f"Duration like `30m`, `2h`, `7d` (1m–30d). Winners 1–10.")
    if len(parts) < 3:
        await ctx.send(embed=ui.info(usage, "Giveaway usage"))
        return
    m = _re.match(r"(\d+)", parts[2])
    try:
        winners_n = max(1, min(10, int(m.group(1)) if m else parts[2]))
    except ValueError:
        winners_n = 0
    dur_s = parse_duration(parts[1])
    if not parts[0] or dur_s is None or not winners_n:
        await ctx.send(embed=ui.info(usage, "Giveaway usage"))
        return
    target = ctx.channel
    afterwards = " ".join(parts[3:]) + " " + parts[2]
    chm = _re.search(r"<#(\d+)>", afterwards)
    if chm:
        ch = ctx.guild.get_channel(int(chm.group(1)))
        if ch is not None:
            target = ch
    else:
        raw = parts[3] if len(parts) >= 4 and parts[3] else ""
        if raw:
            try:
                ch = ctx.guild.get_channel(int(raw.strip("<#>")))
                if ch is not None:
                    target = ch
            except ValueError:
                pass
            if target is ctx.channel:
                name = raw.lstrip("#").strip().lower()
                hit = next((c for c in ctx.guild.text_channels
                            if c.name.lower() == name), None)
                if hit is not None:
                    target = hit
    gid = await create_giveaway(ctx.guild, target, parts[0], dur_s, winners_n, ctx.author.id)
    await ctx.send(embed=ui.ok(f"Giveaway started in {target.mention} — id `{gid}`.",
                               "🎁 Giveaway live"))


@cmd_giveaway.command(name="end")
@owner_or_admin()
async def cmd_giveaway_end(ctx: commands.Context, gid: str = "") -> None:
    """End a giveaway now. Usage: .giveaway end [id]"""
    g = _gw_find(ctx.guild.id, gid or None, active_only=True)
    if g is None:
        await ctx.send(embed=ui.warn(f"No active giveaway found — try `{PREFIX}giveaway list`.",
                                     "Giveaways"))
        return
    await ctx.send(embed=ui.embed("🎁 Giveaway ended",
                                  await end_giveaway(g["id"], by=str(ctx.author.id)), "gold",
                                  footer=ui.FOOTER))


@cmd_giveaway.command(name="reroll")
@owner_or_admin()
async def cmd_giveaway_reroll(ctx: commands.Context, gid: str = "") -> None:
    """Pick new winner(s). Usage: .giveaway reroll [id]"""
    items = [g for g in giveaway_data.values()
             if g.get("guild") == str(ctx.guild.id) and g.get("status") == "ended"]
    if gid:
        items = [g for g in items if g["id"].lower().startswith(gid.strip().lower())]
    items.sort(key=lambda g: g.get("ended_at", g.get("created", 0)), reverse=True)
    if not items:
        await ctx.send(embed=ui.warn(f"No ended giveaway found — try `{PREFIX}giveaway list`.",
                                     "Giveaways"))
        return
    await ctx.send(embed=ui.embed("🔁 Giveaway rerolled",
                                  await reroll_giveaway(items[0]["id"]), "gold",
                                  footer=ui.FOOTER))


@cmd_giveaway.command(name="list")
async def cmd_giveaway_list(ctx: commands.Context) -> None:
    """Show giveaways. Usage: .giveaway list"""
    items = [g for g in giveaway_data.values() if g.get("guild") == str(ctx.guild.id)]
    if not items:
        await ctx.send(embed=ui.info(
            "No giveaways yet. Staff can start one with\n"
            "`.giveaway create <prize> | <duration> | <winners>`.",
            "No giveaways"))
        return
    active = sorted([g for g in items if g.get("status") == "active"], key=lambda g: g["ends_at"])
    ended = sorted([g for g in items if g.get("status") != "active"],
                   key=lambda g: g.get("ended_at", g.get("created", 0)), reverse=True)[:5]
    em = ui.embed("🎁  Giveaways", kind="gold", footer=f"{ui.FOOTER} • {ctx.guild.name}")
    parts = [f"🟢 **{g['prize']}** · `{g['id']}` — {len(g.get('entrants', []))} entries · "
             f"ends <t:{int(g['ends_at'])}:R> · <#{g['channel']}>" for g in active]
    if ended:
        if parts:
            parts.append(ui.DIVIDER)
        parts.append("**Recently ended**")
        for g in ended:
            w = ", ".join(f"<@{u}>" for u in g.get("winners", [])) or "*no winner*"
            parts.append(f"⚪ **{g['prize']}** · `{g['id']}` — {w}")
    if not parts:
        parts = ["No giveaways right now."]
    em.description = "\n".join(parts)[:4000]
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@cmd_giveaway.command(name="info")
async def cmd_giveaway_info(ctx: commands.Context, gid: str = "") -> None:
    """Show one giveaway. Usage: .giveaway info <id>"""
    g = _gw_find(ctx.guild.id, gid or None)
    if g is None:
        await ctx.send(embed=ui.err("Giveaway not found — try `.giveaway list`.",
                                    "Not found"))
        return
    host = ctx.guild.get_member(int(g["host"])) if str(g["host"]).isdigit() and int(g["host"]) else None
    em = ui.embed(f"🎁  {g['prize']}", kind="gold", footer=f"{ui.FOOTER} • giveaway info")
    em.add_field(name="📌 Status", value=f"**{g['status'].title()}**", inline=True)
    em.add_field(name="🏆 Winners", value=f"**{g['winners_n']}**", inline=True)
    em.add_field(name="👥 Entries", value=f"**{len(g.get('entrants', []))}**", inline=True)
    em.add_field(name="⏰ Ends", value=f"<t:{int(g['ends_at'])}:R>", inline=True)
    em.add_field(name="📢 Channel", value=f"<#{g['channel']}>", inline=True)
    em.add_field(name="👤 Host", value=host.mention if host else "the dashboard", inline=True)
    if g.get("winners"):
        em.add_field(name="🥇 Winner(s)",
                     value=", ".join(f"<@{u}>" for u in g.get("winners", [])), inline=False)
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="help")
async def cmd_help(ctx: commands.Context) -> None:
    """Everyone's commands (locked staff commands hidden). Usage: .help"""
    if ctx.guild is None:
        await ctx.send(embed=ui.err("Run this command inside a server."))
        return
    em = ui.embed("📖  Commands", kind="brand",
                  footer=f"{ui.FOOTER} • staff: .help2 shows everything")
    em.description = "Everything you can do here — one line each."
    em.add_field(name="⚡ Leveling",
                 value=("`.rank [@user]` — profile & progress card\n"
                        "`.leaderboard` — server top members\n"
                        "`.messages [@user]` — message statistics\n"
                        "`.xp` — how XP works · `.afk [reason]`"),
                 inline=False)
    em.add_field(name="💬 Chat & fun",
                 value=("`.ask` AI chat · `.8ball` · `.trivia` · `.story`\n"
                        "`.compliment [@user]` · `.confess` (anonymous) · `.catchup`\n"
                        "`.tr <lang> <text>` · `.rps` · `.roll` · `.flip`"),
                 inline=False)
    em.add_field(name="🛡️ Server",
                 value=("`.verify` · `.abuse <what's happening>` · `.suggest <idea>`\n"
                        "`.poll \"Q\" \"A\" \"B\"` · `.remind <10s+> <text>` · `.events`\n"
                        "`.cases [@user]` · `.apply` · `.serverinfo` · `.userinfo [@user]`\n"
                        "`.giveaway list` · `.giveaway info <id>` · `.raidstatus`"),
                 inline=False)
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="help2")
@trusted_only()
async def cmd_help2(ctx: commands.Context) -> None:
    """All commands (staff/whitelist only). Usage: .help2"""
    if ctx.guild is None:
        await ctx.send(embed=ui.err("Run this command inside a server."))
        return
    em = ui.embed("📖  All Commands (staff)", kind="gold",
                  footer=f"{ui.FOOTER} • full command reference")
    em.add_field(name="⚡ Leveling (open)",
                 value=("`.rank` · `.leaderboard` · `.messages` · `.xp` · `.afk`\n"
                        "`staff:` `.xpconfig` · `.xpadd` · `.xpset` · `.xpreset` · "
                        "`.leveladd` · `.levelset`"),
                 inline=False)
    em.add_field(name="💬 Chat & fun (open)",
                 value=("`.ask` · `.8ball` · `.trivia` · `.story` · `.compliment`\n"
                        "`.confess` · `.catchup` · `.tr` · `.rps` · `.roll` · `.flip`"),
                 inline=False)
    em.add_field(name="🛡️ Server (open)",
                 value=("`.verify` · `.abuse` · `.suggest` · `.poll` · `.remind`\n"
                        "`.events` · `.cases` · `.apply` · `.serverinfo` · `.userinfo`\n"
                        "`.giveaway list/info` · `.raidstatus`"),
                 inline=False)
    em.add_field(name="📢 Broadcast (whitelist+)",
                 value="`.say` · `.speak` · `.vcjoin` · `.vcleave` · `.voice` · `.voiceauto` · `.setlobby`",
                 inline=False)
    em.add_field(name="🛡️ Mod (admin+)",
                 value=("`.purge` · `.mute` · `.unmute` · `.slowmode` · `.lockdown` · `.unlock`\n"
                        "`.raidmode` · `.raidconfig` · `.whitelist` · `.temprole` · `.poll end`"),
                 inline=False)
    em.add_field(name="⚙️ Setup (owner/admin)",
                 value=("`.teach` · `.unteach` · `.mood` · `.chaton` · `.chatoff` · `.setqotd`\n"
                        "`.rolemenu` · `.setwelcome` · `.setgoodbye` · `.linkchannel`\n"
                        "`.setlog` · `.setchat` · `.setabuserole` · `.setabusechannel`\n"
                        "`.verifysetup` · `.verifyoff` · `.verifyfix` · `.diag`\n"
                        "`.giveaway create|end|reroll` · `.xpconfig`"),
                 inline=False)
    em.add_field(name="👑 Owner only",
                 value="`.status` · `.setbio` · `.shutdown` · `.disable` · `.enable` · `.giverole`",
                 inline=False)
    await ctx.send(embed=em, allowed_mentions=discord.AllowedMentions.none())


@bot.command(name="helpme")
async def cmd_helpme(ctx: commands.Context) -> None:
    """Show the help card. Usage: .helpme"""
    if ctx.guild is None:
        await ctx.send(embed=ui.err("Run this command inside a server."))
        return
    await ctx.send(embed=ui.embed("📖 Help", help_text(ctx.guild.name), "info",
                                  footer=f"{ui.FOOTER} • quick start"))


@bot.command(name="verifyoff")
@owner_or_admin()
async def cmd_verifyoff(ctx: commands.Context) -> None:
    update_config(ctx.guild.id, verify_enabled=False)
    await ctx.send(embed=ui.ok("The verify gate is off — roles and channels untouched. "
                               f"Re-run `{PREFIX}verifysetup` to lock again.",
                               "Verify gate off"))


@bot.command(name="verifyfix")
@owner_or_admin()
async def cmd_verifyfix(ctx: commands.Context) -> None:
    """Repair an existing gate: hide #verify from verified, re-post button."""
    cfg = get_config(ctx.guild.id)
    vchan = ctx.guild.get_channel(cfg.get("verify_channel_id") or 0)
    vrole = ctx.guild.get_role(cfg.get("verified_role_id") or 0)
    if not vchan or not vrole:
        await ctx.send(embed=ui.warn(f"Nothing to fix yet — run `{PREFIX}verifysetup "
                                     "[@role] [#channel]` first.", "Verify gate"))
        return
    try:
        await vchan.set_permissions(vrole, view_channel=False, reason="Verify gate fix")
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await vchan.send(embed=verify_prompt_embed(ctx.guild.name), view=VerifyView())
    except (discord.Forbidden, discord.HTTPException):
        pass
    update_config(ctx.guild.id, verify_enabled=True)
    await ctx.send(embed=ui.ok(f"{vrole.name} can no longer see {vchan.mention} — "
                               "the button was re-posted.", "Verify gate repaired"))


@bot.command(name="verify")
async def cmd_verify(ctx: commands.Context) -> None:
    """Fallback when the button won't work. Usage: .verify"""
    if ctx.guild is None:
        await ctx.send(embed=ui.err("Verify inside a server — the button only works there."))
        return
    cfg = get_config(ctx.guild.id)
    if not cfg.get("verify_enabled"):
        await ctx.send(embed=ui.info("This server doesn't use a verify gate."))
        return
    role = ctx.guild.get_role(cfg.get("verified_role_id") or 0)
    if role is None:
        await ctx.send(embed=ui.err("The Verified role is missing — ask an admin to re-run "
                                    "`.verifysetup`.", "Verify broken"))
        return
    if role in ctx.author.roles:
        await ctx.send(embed=ui.info("You're already verified — welcome!"))
        return
    try:
        await ctx.author.add_roles(role, reason="Self-verify command")
        await ctx.send(embed=ui.ok(f"Welcome to **{ctx.guild.name}** — full access unlocked!",
                                   "Verified!"))
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send(embed=ui.err("I can't assign the role — tell an admin "
                                    "(my role must sit above Verified).", "Missing permission"))


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    if not bot_enabled or not getattr(channel, "guild", None):
        return
    cfg = get_config(channel.guild.id)
    if not cfg.get("verify_enabled"):
        return
    if channel.id == cfg.get("verify_channel_id"):
        return
    try:
        await channel.set_permissions(channel.guild.default_role, view_channel=False,
                                      reason="Verify gate: auto-lock new channel")
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------- mod tools (purge / mute / intel) ----------

@bot.command(name="purge")
@admin_only()
async def cmd_purge(ctx: commands.Context, amount: int = 0,
                    member: discord.Member | None = None) -> None:
    """Bulk delete. Usage: .purge <1-100> [@user]"""
    if amount < 1 or amount > 100:
        await ctx.send(embed=ui.info(f"`{PREFIX}purge <1-100> [@user]` — "
                                     "number of messages to delete.", "Purge"))
        return
    try:
        if member:
            count = 0
            async for m in ctx.channel.history(limit=300):
                if m.author.id == member.id:
                    try:
                        await m.delete()
                        count += 1
                    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                        pass
                    if count >= amount:
                        break
            await ctx.send(embed=ui.ok(f"Deleted **{count}** message(s) from "
                                       f"{member.display_name}.", "🧹 Purge"),
                           delete_after=6)
        else:
            deleted = await ctx.channel.purge(limit=amount + 1)  # +1 includes the command
            await ctx.send(embed=ui.ok(f"Deleted **{max(len(deleted) - 1, 0)}** message(s).",
                                       "🧹 Purge"), delete_after=6)
        await send_log(ctx.guild, "", event="purge", embed=ui.embed(
            "🧹 Purge",
            f"**{amount}** message(s) removed{f' from **{member.display_name}**' if member else ''} "
            f"in {ctx.channel.mention} by **{ctx.author.display_name}**.",
            "warning", footer=f"{ui.FOOTER} • moderation"))
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Purge failed (need **Manage Messages**): {exc}"))


@bot.command(name="mute")
@admin_only()
async def cmd_mute(ctx: commands.Context, member: discord.Member, minutes: int = 10,
                   *, reason: str = "mod mute") -> None:
    """Timeout a member. Usage: .mute @user [minutes] [reason]"""
    if minutes < 1 or minutes > 40320:
        await ctx.send(embed=ui.err("Minutes must be **1–40320** (28 days max)."))
        return
    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
        await ctx.send(embed=ui.ok(f"**{member.display_name}** muted for **{minutes}m**.\n"
                                   f"**Reason:** {sanitize_mentions(reason[:200])}",
                                   "🔇 Mute"))
        await send_log(ctx.guild, "", event="mute", embed=ui.embed(
            "🔇 Member muted",
            f"**{member.display_name}** (`{member.id}`) — **{minutes}m** by "
            f"**{ctx.author.display_name}**: {sanitize_mentions(reason[:1000])}",
            "warning", footer=f"{ui.FOOTER} • moderation"))
        try:
            community.open_case(ctx.guild.id, member.id, member.display_name,
                                ctx.author.id, str(ctx.author), "mute",
                                f"{minutes}m: {reason}")
        except Exception:
            pass
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Mute failed (role order or permissions?): {exc}"))


@bot.command(name="unmute")
@admin_only()
async def cmd_unmute(ctx: commands.Context, member: discord.Member) -> None:
    try:
        await member.timeout(None, reason=f"unmuted by {ctx.author}")
        await ctx.send(embed=ui.ok(f"**{member.display_name}** is unmuted.", "🔊 Unmute"))
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Unmute failed: {exc}"))


@bot.command(name="userinfo", aliases=["whois"])
async def cmd_userinfo(ctx: commands.Context, member: discord.Member | None = None) -> None:
    """Account intel — spot alts. Usage: .userinfo [@user]"""
    m = member or ctx.author
    now = datetime.now(timezone.utc)
    created = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created).days
    roles = ", ".join(r.name for r in getattr(m, "roles", [])[1:]) or "none"
    new_flag = "  ⚠️ **NEW ACCOUNT**" if age_days < 7 else ""
    embed = ui.embed("👤  Member Intel", kind="brand", footer=f"{ui.FOOTER} • userinfo")
    embed.set_author(name=m.display_name, icon_url=m.display_avatar.url if m.display_avatar else None)
    embed.add_field(name="🆔 User ID", value=f"`{m.id}`", inline=True)
    embed.add_field(name="📅 Account age",
                    value=f"**{age_days} days**\n<t:{int(created.timestamp())}:R>{new_flag}",
                    inline=True)
    if getattr(m, "joined_at", None):
        jts = int(m.joined_at.replace(tzinfo=m.joined_at.tzinfo or timezone.utc).timestamp())
        embed.add_field(name="📥 Joined server", value=f"<t:{jts}:R>\n<t:{jts}:F>", inline=True)
    else:
        embed.add_field(name="📥 Joined server", value="?", inline=True)
    entry = xp_data.get(str(ctx.guild.id), {}).get(str(m.id))
    if entry and not m.bot:
        xpv = int(entry["xp"])
        rank, tracked = xp_rank(ctx.guild.id, m.id)
        msg_total = stats.user_stats(ctx.guild.id, m.id)["total"]
        embed.add_field(name="⚡ Level",
                        value=f"**{xp_level(xpv)}** (#{rank} of {tracked})" if rank
                        else f"**{xp_level(xpv)}**", inline=True)
        embed.add_field(name="💬 Messages", value=f"**{ui.fmt(msg_total)}**", inline=True)
    embed.add_field(name="🎭 Roles", value=roles[:1000], inline=False)
    if m.display_avatar:
        embed.set_thumbnail(url=m.display_avatar.url)
    await ctx.send(embed=embed)


@bot.command(name="slowmode")
@admin_only()
async def cmd_slowmode(ctx: commands.Context, seconds: int = -1) -> None:
    """Spam damper. Usage: .slowmode <0-21600, 0 = off>"""
    if seconds < 0 or seconds > 21600:
        await ctx.send(embed=ui.info(f"`{PREFIX}slowmode <0-21600>` — 0 turns it off.\n"
                                     f"Current: **{ctx.channel.slowmode_delay}s**",
                                     "Slowmode"))
        return
    try:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"slowmode by {ctx.author}")
        await ctx.send(embed=ui.ok(f"Slowmode is now "
                                   f"{'**off**' if seconds == 0 else f'**{seconds}s**'}.",
                                   "🐢 Slowmode"))
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Slowmode failed: {exc}"))


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    track(member.guild.id, "leave")
    await send_log(member.guild, f"👋 {member} (`{member.id}`) left.")
    cfg = get_config(member.guild.id)
    gchan = member.guild.get_channel(cfg.get("goodbye_channel_id") or 0)
    if gchan:
        try:
            human_count = len([m for m in member.guild.members if not m.bot])
            em = E(f"👋  Goodbye, {member.display_name}", kind="info")
            em.description = (f"**{member.display_name}** just left **{member.guild.name}**.\n"
                              f"That leaves **{human_count}** of us — see you around!")
            if getattr(member, "display_avatar", None):
                em.set_thumbnail(url=member.display_avatar.url)
            em.set_footer(text=f"{ui.FOOTER} • goodbye")
            await gchan.send(embed=em)
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not bot_enabled or not message.guild or (message.author and message.author.bot):
        return
    if message.id in automod_deleted:
        automod_deleted.discard(message.id)
        return  # already handled by automod — no double log
    text = (message.content or "").strip()[:1000]
    atts = f" 📎 {len(message.attachments)} attachment(s)" if message.attachments else ""
    ghost = ""
    try:
        if (getattr(message, "mention_everyone", False) or message.mentions or message.role_mentions) \
                and isinstance(message.author, discord.Member) \
                and not is_whitelisted(message.author, get_config(message.guild.id)):
            ghost = "\n👻 Suspected ghost ping (pinged then deleted)."
    except Exception:
        pass
    embed = E("🗑️ Message deleted", kind="info")
    embed.add_field(name="Author", value=f"{message.author.mention} (`{message.author.id}`)",
                    inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(name="Content", value=f"```{text or '(no text)'}```{atts}{ghost}", inline=False)
    await send_log(message.guild, "", embed=embed)


@bot.command(name="say")
@trusted_only()
async def cmd_say(ctx: commands.Context, channel: discord.TextChannel | None = None, *, text: str = "") -> None:
    """Speak through the bot. Usage: .say [#channel] <message> (staff/whitelist only)."""
    if not text:
        await ctx.send(embed=ui.info(f"`{PREFIX}say [#channel] <message>`", "Say"))
        return
    target = channel or ctx.channel
    try:
        await target.send(text)
        if target.id != ctx.channel.id:
            await ctx.send(embed=ui.ok(f"Sent in {target.mention}.", "Say"))
        else:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Could not send: {exc}"))


explode_cooldown: dict[int, float] = {}


@bot.command(name="explode")
@owner_or_admin()
async def cmd_explode(ctx: commands.Context, *, target: str = "") -> None:
    """Fake server nuke. 100% theater, 0% damage. Usage: .explode [@user]"""
    import time as _t
    now = _t.time()
    if now - explode_cooldown.get(ctx.guild.id if ctx.guild else 0, 0) < 360:
        await ctx.send(embed=ui.warn("The nuke is recharging — give it about 6 minutes.",
                                     "🧯 Chill"))
        return
    if ctx.guild:
        explode_cooldown[ctx.guild.id] = now
    gname = ctx.guild.name if ctx.guild else "this server"
    members = ctx.guild.member_count if ctx.guild else 69
    channels = len(ctx.guild.channels) if ctx.guild else 12
    victim = sanitize_mentions(target.strip() or "everyone")
    SCALE = 9.0  # ~5 minutes of dread
    steps = [
        ("💣 Nuke request received. Verifying permissions...", 2.0),
        ("🔑 Forging audit-log reason... *totally legit purposes*...", 2.2),
        (f"🛰️ Connected to **{gname}** — {members} members, {channels} channels found.", 2.4),
        ("📥 Dumping member list... 34%...", 2.0),
        ("📥 Dumping member list... 100%. Saved. For science.", 1.8),
        ("🔨 Mass-ban wave 1/3... 14 members banned.", 2.4),
        ("🔨 Mass-ban wave 2/3... 31 members banned.", 2.4),
        (f"🔨 Mass-ban wave 3/3... **{members}** members banned. Server is empty.", 2.6),
        ("📂 Deleting channels... #general... #memes... #announcements...", 2.6),
        ("🎨 Stealing emojis... backup complete (mine now).", 2.2),
        (f"🎯 Final target: **{victim}**. Arming...", 2.2),
        ("💥 Detonation in **10**...", 1.2),
        ("💥 **9... 8... 7...**", 1.6),
        ("💥 **6... 5... 4...**", 1.6),
        ("💥 **3... 2... 1...**", 2.0),
    ]
    endings = [
        "😎 **SIKE.** Nothing happened. Check your members — everyone's still here. This time.",
        "🤡 **PSYCH!** Zero bans, zero deletes. But your heart rate says otherwise.",
    ]
    try:
        msg = await ctx.send(steps[0][0])
        for text, wait in steps[1:]:
            await asyncio.sleep(wait * SCALE)
            try:
                await msg.edit(content=text)
            except (discord.Forbidden, discord.HTTPException):
                return
        await asyncio.sleep(1.0)
        await msg.edit(content=random.choice(endings))
    except (discord.Forbidden, discord.HTTPException):
        pass


@bot.command(name="shutdown")
@owner_only()
async def cmd_shutdown(ctx: commands.Context) -> None:
    """Owner-only: log the bot out (same as Ctrl+C). Set OWNER_ID in .env."""
    stats.flush()
    _save_xp(xp_data)
    await ctx.send(embed=ui.warn("Logging out now — data flushed. Goodbye! 🫡", "Shutting down"))
    await bot.close()


@bot.command(name="disable")
@owner_only()
async def cmd_disable(ctx: commands.Context) -> None:
    """Owner-only: bot stays online but ignores everything except .enable."""
    global bot_enabled
    bot_enabled = False
    await ctx.send(embed=ui.warn("I'll stay online but ignore pings, XP and raids until "
                                 "`.enable`.", "Bot disabled"))


@bot.command(name="enable")
@owner_only()
async def cmd_enable(ctx: commands.Context) -> None:
    global bot_enabled
    bot_enabled = True
    await ctx.send(embed=ui.ok("All systems back online — watching joins, XP and chat.",
                               "Bot enabled"))


@bot.command(name="giverole")
@owner_only()
async def cmd_giverole(ctx: commands.Context, member: discord.Member | None = None,
                       role: str = "") -> None:
    """Owner grants any role (bot must outrank it). Usage: .giverole @user @role"""
    if member is None or not role:
        await ctx.send(embed=ui.info(f"`{PREFIX}giverole @user @role`", "Give role"))
        return
    target = None
    try:
        target = ctx.guild.get_role(int(role.strip("<>@#!&")))
    except ValueError:
        pass
    if target is None:
        await ctx.send(embed=ui.err("Role not found — mention it."))
        return
    try:
        await member.add_roles(target, reason=f"granted by owner {ctx.author}")
        await ctx.send(embed=ui.ok(f"**{member.display_name}** got **{target.name}**.",
                                   "Role granted"))
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(embed=ui.err(f"Failed — my role must sit above `{target.name}`: {exc}"))


@bot.command(name="diag")
@owner_or_admin()
async def cmd_diag(ctx: commands.Context) -> None:
    """Show bot state for debugging. Usage: .diag"""
    em = ui.embed("🩺  Diagnostics", kind="muted", footer=f"{ui.FOOTER} • diag")
    em.add_field(name="Enabled", value=f"**{'yes' if bot_enabled else 'no'}**", inline=True)
    em.add_field(name="Prefix", value=f"`{PREFIX}`", inline=True)
    em.add_field(name="Latency", value=f"**{round(bot.latency * 1000)} ms**", inline=True)
    em.add_field(name="Guilds", value=f"**{len(bot.guilds)}**", inline=True)
    em.add_field(name="Stats pending", value=f"**{stats.pending()}**", inline=True)
    if ctx.guild:
        em.add_field(name="Tracked members", value=f"**{stats.tracked(ctx.guild.id)}**", inline=True)
        em.add_field(name="XP earners", value=f"**{len(xp_data.get(str(ctx.guild.id), {}))}**", inline=True)
    await ctx.send(embed=em)


@cmd_lockdown.error
@cmd_unlock.error
@cmd_raidmode.error
@cmd_config.error
@cmd_setlog.error
@cmd_setchat.error
@cmd_linkchannel.error
@cmd_setwelcome.error
@cmd_setgoodbye.error
@cmd_rolemenu.error
@cmd_aitest.error
@cmd_statusbot.error
@cmd_setbio.error
@cmd_setlobby.error
@cmd_vcjoin.error
@cmd_vcleave.error
@cmd_voice.error
@cmd_speak.error
@cmd_voiceauto.error
@cmd_whitelist.error
@cmd_say.error
@cmd_help2.error
@cmd_explode.error
@cmd_purge.error
@cmd_mute.error
@cmd_unmute.error
@cmd_slowmode.error
@cmd_teach.error
@cmd_unteach.error
@cmd_mood.error
@cmd_setqotd.error
@cmd_chatoff.error
@cmd_chaton.error
@cmd_verifysetup.error
@cmd_verifyoff.error
@cmd_xpreset.error
@cmd_xpadd.error
@cmd_xpset.error
@cmd_leveladd.error
@cmd_levelset.error
@cmd_xpconfig.error
@cmd_shutdown.error
@cmd_disable.error
@cmd_enable.error
@cmd_diag.error
@cmd_giverole.error
async def admin_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        if ctx.command and ctx.command.name in ("shutdown", "disable", "enable", "status",
                                                 "setbio", "giverole"):
            await ctx.send(embed=ui.err("Owner only — set your Discord user ID as "
                                        "OWNER_ID in .env."))
        elif ctx.command and ctx.command.name in ("setlog", "setlobby", "setchat", "diag",
                                                     "verifysetup", "verifyoff", "aitest",
                                                     "rolemenu", "teach", "unteach", "mood",
                                                     "setqotd", "chaton", "chatoff", "explode",
                                                     "setwelcome", "setgoodbye",
                                                     "linkchannel", "vcjoin", "vcleave",
                                                     "voiceauto", "voice", "xpconfig"):
            await ctx.send(embed=ui.err("Bot owner or server admin only."))
        elif ctx.command and ctx.command.name in ("say", "speak", "vcjoin", "vcleave",
                                                      "voice", "voiceauto", "setlobby", "help2"):
            await ctx.send(embed=ui.err(f"Staff/whitelisted role only — ask an admin for "
                                        f"`{PREFIX}whitelist add @yourrole`."))
        else:
            await ctx.send(embed=ui.err("You need **Administrator** or **Manage Server** "
                                        "permission."))
    else:
        await ctx.send(embed=ui.err(f"Command error: {error}"))


@bot.check
async def globally_enabled(ctx: commands.Context) -> bool:
    if bot_enabled:
        return True
    return bool(ctx.command and ctx.command.name == "enable")


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Missing DISCORD_TOKEN. Copy .env.example to .env and fill it in.")
    try:
        import dashboard as _dash
        import sys as _sys
        _dash.bot_ref = _sys.modules[__name__]
        _dash.start()
    except Exception as exc:
        print(f"[dash] not started: {exc}")
    try:
        bot.run(TOKEN)
    finally:
        stats.flush()  # last-chance persist of message counters
        try:
            _save_xp(xp_data)
        except OSError:
            pass
