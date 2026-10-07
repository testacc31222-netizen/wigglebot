"""Anti-raid Discord bot (discord.py) — join-spam detection + auto-lockdown."""
from __future__ import annotations

import asyncio
import json
import os
import random
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
import aiohttp

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
XP_MIN, XP_MAX = 5, 15
XP_COOLDOWN_S = 60


def _load_xp() -> dict:
    if XP_FILE.exists():
        try:
            return json.loads(XP_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


def _save_xp(data: dict) -> None:
    XP_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


xp_data: dict[str, dict[str, dict]] = _load_xp()  # guild_id -> user_id -> {xp, last}


def xp_level(xp: int) -> int:
    return int((max(xp, 0) / 100) ** 0.5)


def xp_for_level(level: int) -> int:
    return 100 * level * level


def award_xp(guild_id: int, user_id: int) -> tuple[int, int, bool]:
    """Add XP if cooldown passed. Returns (xp, level, leveled_up)."""
    now = time.time()
    g = xp_data.setdefault(str(guild_id), {})
    entry = g.setdefault(str(user_id), {"xp": 0, "last": 0})
    if now - float(entry.get("last", 0)) < XP_COOLDOWN_S:
        return int(entry["xp"]), xp_level(int(entry["xp"])), False
    old_level = xp_level(int(entry["xp"]))
    entry["xp"] = int(entry["xp"]) + random.randint(XP_MIN, XP_MAX)
    entry["last"] = now
    _save_xp(xp_data)
    new_level = xp_level(int(entry["xp"]))
    return int(entry["xp"]), new_level, new_level > old_level

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
    "welcome_channel_id": None,
    "goodbye_channel_id": None,
    # NOTE: the raid initiator is ALWAYS banned, regardless of raid_action.
}

VALID_ACTIONS = {"kick", "ban", "timeout", "none"}


BRAND = {
    "raid": discord.Color.red(),
    "night": discord.Color.dark_purple(),
    "day": discord.Color.orange(),
    "win": discord.Color.gold(),
    "good": discord.Color.green(),
    "bad": discord.Color.red(),
    "info": discord.Color.blurple(),
}


def E(title: str, desc: str = "", kind: str = "info") -> discord.Embed:
    embed = discord.Embed(title=title, description=desc,
                          color=BRAND.get(kind, BRAND["info"]),
                          timestamp=datetime.now(timezone.utc))
    embed.set_footer(text="Wigglesworth")
    return embed


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

bot = commands.Bot(command_prefix=PREFIX, intents=intents)

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


async def send_log(guild: discord.Guild, message: str, embed: discord.Embed | None = None) -> None:
    cfg = get_config(guild.id)
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
            return "banned"
        if action == "kick":
            await member.kick(reason=reason)
            return "kicked"
        if action == "timeout":
            mins = int(cfg.get("timeout_duration_minutes", 10))
            await member.timeout(timedelta(minutes=mins), reason=reason)
            return f"timed out ({mins}m)"
    except (discord.Forbidden, discord.HTTPException) as exc:
        return f"FAILED ({exc})"
    return "none"


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
            await send_log(guild, f"🔓 Auto-unlock: restored {n} channel(s). Raid mode OFF.")

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

    embed = discord.Embed(
        title="🚨 RAID DETECTED — server locked down first, initiator banned",
        description=(
            f"**{len(recent_members)} joins** in "
            f"**{cfg['join_threshold_seconds']}s** (threshold: {cfg['join_threshold_count']}).\n"
            f"1️⃣ Locked **{locked}** channel(s), deleted invites, raised verification.\n"
            f"2️⃣ Initiator: **{initiator_line}**\n"
            f"   _Why: {why}_\n"
            f"3️⃣ Followers: action `{action}`."
        ),
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    if results:
        embed.add_field(name="Actions (in order)", value="\n".join(results[:20]), inline=False)
    embed.add_field(
        name="Recover",
        value=f"Run `{PREFIX}unlock` when safe, or wait {cfg['lockdown_duration_minutes']} min for auto-unlock.",
        inline=False,
    )
    await send_log(guild, "@everyone ⚠️ suspected raid — lockdown active.", embed=embed)
    await schedule_auto_unlock(guild, cfg)


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user} — watching {len(bot.guilds)} guild(s).")
    bot.add_view(VerifyView())  # keep the ✅ button alive across restarts
    bot.add_view(TicketOpenView())
    bot.add_view(TicketCloseView())
    rr_register_saved()  # re-arm role menus (defined below, resolved at runtime)
    if not qotd_loop.is_running():
        qotd_loop.start()
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
async def on_member_join(member: discord.Member) -> None:
    if not bot_enabled:
        return
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
            return

    # burst → raid
    if len(dq) >= threshold and not raid_active.get(guild.id):
        await trigger_raid(guild, cfg, recent_members)
        return
    # welcome new member
    wchan = guild.get_channel(cfg.get("welcome_channel_id") or 0)
    if wchan:
        try:
            embed = E(f"👋 Welcome, {member.display_name}!",
                      f"{member.mention} joined **{guild.name}** — you're member "
                      f"**#{len([m for m in guild.members if not m.bot])}**!\n"
                      f"Head to #verify if you can't see channels.",
                      kind="good")
            if member.display_avatar:
                embed.set_thumbnail(url=member.display_avatar.url)
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
    if not bot_enabled:
        await bot.process_commands(message)  # only .enable gets through (global check)
        return
    if isinstance(message.author, discord.Member) and not is_whitelisted(
            message.author, get_config(message.guild.id)):
        if await automod_check(message):
            await bot.process_commands(message)
            return  # deleted + handled, no XP
    if raid_active.get(message.guild.id):
        await bot.process_commands(message)
        return  # no XP farming during lockdown
    if bot.user and bot.user.mentioned_in(message):
        if get_config(message.guild.id).get("chat_enabled", True):
            await chat_reply(message)
    content = (message.content or "")
    if not content.startswith(PREFIX):
        await help_responder(message)
        if get_config(message.guild.id).get("chat_enabled", True):
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
                        f"{entry['reason']}", mention_author=False)
                except (discord.Forbidden, discord.HTTPException):
                    pass
                break  # one notice per message
    xp, level, leveled = award_xp(message.guild.id, message.author.id)
    if leveled:
        try:
            await message.channel.send(
                f"🎉 {message.author.mention} leveled up to **level {level}** ({xp} XP)!"
            )
        except (discord.Forbidden, discord.HTTPException):
            pass
    await bot.process_commands(message)


# ---------- ping-to-chat (Groq AI if key set, casual fallback + lookup) ----------

AI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
AI_MODEL = os.getenv("OPENAI_MODEL", "openai/gpt-oss-20b").strip() or "openai/gpt-oss-20b"
AI_BASE_URL = os.getenv("OPENAI_BASE_URL", "https://api.groq.com/openai/v1").strip().rstrip("/")
AI_SYSTEM = os.getenv(
    "AI_SYSTEM_PROMPT",
    "You are a chill Discord regular on the Wigglesworth server. Talk simple: 1-2 short "
    "sentences, easy words, casual. Never say you are an AI. You help with verify, ranks, events.",
).strip()

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
        return random.choice(["all good, wbu?", "vibing. you?", "good! you?"])
    if "who are you" in low or "your name" in low or "what are you" in low:
        return "just wigglesworth's helper bot lol. i chat, track xp, guard raids."
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
        return random.choice(["L lol", "that's an L ngl"])
    if low.strip() == "mid" or " mid " in low:
        return random.choice(["mid af lol", "yeah that's mid"])
    if " yeet " in low:
        return "YEET"
    if " ratio " in low:
        return random.choice(["ratio + L", "counter-ratio"])
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
    if "joke" in low or "funny" in low:
        return random.choice([
            "why did the raider bring a ladder? got banned lol",
            "i'd tell you a udp joke but you wouldn't get it",
        ])
    if any(w in low for w in ("i hate you", "hate you", "you suck", "dumb bot", "dumbass",
                               "stupid bot", "idiot bot", "loser", "shut up", "stfu",
                               "kys", "trash bot", "l bot", "fuck you", "fuck u", "f u")):
        return random.choice([
            f"rude?? {author_name} i'm telling the mods 😤",
            "ok and?? still here lol",
            f"wow {author_name}, hurtful. anyway —",
            "takes one to know one 😎",
            "cry about it + L + ratio",
            "congrats, you just lost 10 aura",
        ])
    if any(w in low for w in ("good morning", "morning!", "gm ")) or low.strip() == "gm":
        return random.choice([f"morning {author_name}!", "gm! sleep well?", "morninggg"])
    if any(w in low for w in ("good night", "goodnight", "gn ")) or low.strip() == "gn":
        return random.choice(["gn! sleep tight", f"night {author_name}!"])
    if "happy birthday" in low or "my birthday" in low or "bday" in low:
        return random.choice([f"HAPPY BIRTHDAY {author_name}!! 🎉🎂", "hbddd!! cake time 🎂"])
    if any(w in low for w in ("love you", "like you", "good bot", "best bot", "w bot",
                              "you're cool", "ur cool", "awesome bot", "great bot")):
        return random.choice([f"love you too {author_name} 🫶", "aww thanks! w user fr",
                              "ik i'm great 😎", "blushing rn"])
    if any(w in low for w in ("wanna play", "hop on", "get on", "join vc", "play with",
                              "run it", "1v1", "game night")):
        return random.choice(["SAY LESS, i'm in 🎮", "hop in vc i'm coming",
                              "bet, what we playing?"])
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
        return random.choice(["idk, what do you think?", "not sure tbh. more context?",
                              "hmm, explain a bit more?"])
    return random.choice(["lol real. go on?", f"yeah {author_name}. what else?", "fr. tell me more?"])


async def ai_reply(channel_id: int, author_name: str, text: str) -> str | None:
    if not AI_API_KEY:
        return None
    hist = chat_history.setdefault(channel_id, [])
    hist.append({"role": "user", "content": f"{author_name}: {text}"})
    del hist[:-12]
    for attempt in (1, 2):  # gpt-oss sometimes returns empty — one retry
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=25)) as sess:
                async with sess.post(
                    f"{AI_BASE_URL}/chat/completions",
                    headers={"Authorization": f"Bearer {AI_API_KEY}"},
                    json={"model": AI_MODEL,
                          "messages": [{"role": "system", "content": AI_SYSTEM}] + hist[-12:],
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


async def chat_reply(message: discord.Message) -> None:
    cfg = get_config(message.guild.id)
    allowed = cfg.get("chat_channel_id")
    if allowed is not None and message.channel.id != allowed:
        return  # locked to another channel
    text = message.content.replace(f"<@{bot.user.id}>", "").replace(
        f"<@!{bot.user.id}>", "").strip()
    try:
        async with message.channel.typing():
            answer = await ai_reply(message.channel.id, message.author.display_name, text)
            if answer is None:
                base = _fallback_reply(message.author.display_name, text)
                low_q = (" " + text.lower() + " ")
                if looks_like_question(text) and not any(t in low_q for t in SELF_TOPICS):
                    found = await web_search_answer(text)
                    answer = f"looked it up:\n{found}" if found else base
                else:
                    answer = base
            if not answer or not answer.strip():
                answer = _fallback_reply(message.author.display_name, text)
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
    "savage": ("", " 😤"),
    "formal": ("Certainly. ", ""),
    "hype": ("LETS GOOO ", " 🔥🔥"),
}


def mood_wrap(text: str, guild_id: int) -> str:
    mood = get_config(guild_id).get("bot_mood", "chill")
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
        await message.reply(mood_wrap(triggers[hit], message.guild.id)[:1900],
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
        await ctx.send(f"Usage: `{PREFIX}teach <trigger> :: <reply>`")
        return
    trig, reply = [s.strip() for s in text.split("::", 1)]
    if not trig or not reply:
        return
    teach_data.setdefault(str(ctx.guild.id), {})[trig.lower()] = reply[:500]
    _save_json(TEACH_FILE, teach_data)
    await ctx.send(f"✅ When someone says **{trig}**, I'll reply.")


@bot.command(name="unteach")
@owner_or_admin()
async def cmd_unteach(ctx: commands.Context, *, trigger: str = "") -> None:
    teach_data.get(str(ctx.guild.id), {}).pop(trigger.lower().strip(), None)
    _save_json(TEACH_FILE, teach_data)
    await ctx.send("✅ Forgotten.")


@bot.command(name="triggers")
async def cmd_triggers(ctx: commands.Context) -> None:
    trigs = list(teach_data.get(str(ctx.guild.id), {}))
    await ctx.send("Taught triggers: " + (", ".join(f"`{t}`" for t in trigs) or "none yet"))


@bot.command(name="mood")
@owner_or_admin()
async def cmd_mood(ctx: commands.Context, mood: str = "") -> None:
    """Bot personality. Usage: .mood <chill|savage|formal|hype>"""
    mood = mood.lower().strip()
    if mood not in MOODS:
        await ctx.send(f"Moods: {', '.join(MOODS)} (current: `{get_config(ctx.guild.id).get('bot_mood', 'chill')}`)")
        return
    update_config(ctx.guild.id, bot_mood=mood)
    await ctx.send(f"✅ Mood: **{mood}**")


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
                await interaction.response.send_message("Too slow — answered!", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if self.idx == self.parent.correct:
            self.parent.winner = interaction.user.id
            for c in self.parent.children:
                c.disabled = True
            try:
                await interaction.response.edit_message(view=self.parent)
                await interaction.followup.send(
                    f"✅ {interaction.user.mention} got it! +50 XP")
            except (discord.Forbidden, discord.HTTPException):
                pass
            g = xp_data.setdefault(str(interaction.guild.id), {})
            e = g.setdefault(str(interaction.user.id), {"xp": 0, "last": 0})
            e["xp"] = int(e["xp"]) + 50
            _save_xp(xp_data)
        else:
            try:
                await interaction.response.send_message("❌ Nope!", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass


@bot.command(name="chatoff")
@owner_or_admin()
async def cmd_chatoff(ctx: commands.Context) -> None:
    """Mute the chatting bot (pings, taught replies, reacts). Games/commands still work."""
    update_config(ctx.guild.id, chat_enabled=False)
    await ctx.send("🔇 Chatbot off — pings get silence. `.chaton` to wake it.")


@bot.command(name="chaton")
@owner_or_admin()
async def cmd_chaton(ctx: commands.Context) -> None:
    update_config(ctx.guild.id, chat_enabled=True)
    await ctx.send("🔊 Chatbot on!")


@bot.command(name="trivia")
async def cmd_trivia(ctx: commands.Context) -> None:
    """Trivia duel, fastest finger wins +50 XP. Usage: .trivia"""
    q, opts, ci = random.choice(TRIVIA_POOL)
    view = TriviaView(ci)
    embed = discord.Embed(title="🧠 Trivia — fastest wins!", description=f"**{q}**",
                          color=discord.Color.gold())
    for i, opt in enumerate(opts):
        embed.add_field(name="ABCD"[i], value=opt, inline=True)
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
        await ctx.send("📖 So far:\n" + ("\n".join(f"> {l}" for l in story[-10:]) or "_Empty — start it!_"))
        return
    story.append(f"{ctx.author.display_name}: {line[:200]}")
    cont = None
    if AI_API_KEY:
        cont = await ai_reply(ctx.channel.id, "Storyteller",
                              "Continue this one-line story briefly: " + " / ".join(story[-5:]))
    if not cont:
        cont = random.choice(STORY_NUDGES)
    story.append(f"{bot.user.display_name if bot.user else 'Bot'}: {cont}")
    del story[:-20]
    _save_json(STORY_FILE, story_data)
    await ctx.send(f"📖 {cont}")


@bot.command(name="compliment")
async def cmd_compliment(ctx: commands.Context, member: discord.Member | None = None) -> None:
    target = member or ctx.author
    await ctx.send(f"💛 {target.mention} is **{random.choice(COMPLIMENTS)}**")


@bot.command(name="confess")
async def cmd_confess(ctx: commands.Context, *, text: str = "") -> None:
    """Anonymous confession. Usage: .confess <text>"""
    if not text:
        return
    try:
        await ctx.message.delete()
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await ctx.send(f"📮 **Anonymous confession:** {text[:1500]}")
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
        await ctx.send("Nothing to catch up on.")
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
            await ctx.send(f"📰 **Catchup:** {summary}")
            return
    from collections import Counter
    talkers = Counter(m.author.display_name for m in msgs).most_common(5)
    words = Counter(w.lower() for m in msgs for w in m.content.split()
                    if len(w) > 4 and w.isalpha()).most_common(5)
    await ctx.send("📰 **Catchup (last 50):**\nTalkers: " +
                   ", ".join(f"{n} ({c})" for n, c in talkers) +
                   ("\nHot words: " + ", ".join(w for w, _ in words) if words else ""))


@bot.command(name="tr")
async def cmd_tr(ctx: commands.Context, lang: str = "", *, text: str = "") -> None:
    """Translate. Usage: .tr <es|fr|de...> <text>"""
    if not lang or not text:
        await ctx.send(f"Usage: `{PREFIX}tr <lang> <text>` (e.g. `.tr es hello`)")
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as sess:
            async with sess.get("https://api.mymemory.translated.net/get",
                                params={"q": text[:400], "langpair": f"en|{lang}"}) as r:
                data = await r.json()
                out = (data.get("responseData") or {}).get("translatedText", "").strip()
                if not out:
                    raise ValueError
                await ctx.send(f"🌍 `{lang}`: {out[:1500]}")
    except Exception:
        await ctx.send("❌ Translate hiccup — try again later.")


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
            await ch.send(f"❓ **Question of the day:**\n{random.choice(QOTD_POOL)}")
        except (discord.Forbidden, discord.HTTPException):
            pass


@bot.command(name="setqotd")
@owner_or_admin()
async def cmd_setqotd(ctx: commands.Context, channel: str = "") -> None:
    """Daily question. Usage: .setqotd #channel | .setqotd off (12:00 UTC)"""
    if channel.strip().lower() in ("off", "none", "clear"):
        update_config(ctx.guild.id, qotd_channel_id=None)
        await ctx.send("QOTD off.")
        return
    target = ctx.channel
    if channel:
        try:
            c = ctx.guild.get_channel(int(channel.strip("<#>")))
            if c:
                target = c
        except ValueError:
            await ctx.send(f"Usage: `{PREFIX}setqotd #channel` or `{PREFIX}setqotd off`")
            return
    update_config(ctx.guild.id, qotd_channel_id=target.id)
    if not qotd_loop.is_running():
        qotd_loop.start()
    await ctx.send(f"✅ Daily question goes to {target.mention} at 12:00 UTC.")


# ---------- automod (links/invites/mention spam) ----------

HELP_PHRASES = ("need help", "help me", "help please", "pls help", "plz help",
                "how do i verify", "how to verify", "cant verify", "can't verify",
                "how do i", "how to join", "i need help", "someone help")
help_cooldown: dict[int, dict[int, float]] = defaultdict(dict)
HELP_COOLDOWN_S = 300


def help_text(guild_name: str) -> str:
    return (
        f"👋 Need a hand in **{guild_name}**?\n"
        "• **Can't see channels?** Go to #verify (or type `.verify`) and hit ✅ Verify.\n"
        "• **Bot stuff:** `.rank` for level, `.raidstatus` for guard status.\n"
        "• **Still stuck?** Ping a mod — or type `.helpme` to see this again."
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
    elif len(message.mentions) + len(message.role_mentions) > int(cfg.get("automod_max_mentions", 5)):
        reason = f"too many mentions (max {cfg.get('automod_max_mentions', 5)})"
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
        warn = await message.channel.send(
            f"⚠️ {message.author.mention} {reason} (strike {n}/{AUTOMOD_STRIKES_TO_MUTE}).")
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
            await send_log(message.guild,
                           f"🔇 {message.author} (`{message.author.id}`) auto-muted: 3 automod strikes.")
        except (discord.Forbidden, discord.HTTPException):
            pass
    return True


# ---------- commands ----------


@bot.command(name="lockdown")
@admin_only()
async def cmd_lockdown(ctx: commands.Context, *, reason: str = "manual lockdown") -> None:
    raid_active[ctx.guild.id] = True
    n = await lockdown_guild(ctx.guild, reason=f"manual by {ctx.author}: {reason}")
    await ctx.send(f"🔒 Locked **{n}** channel(s). Reason: {reason}")
    await send_log(ctx.guild, f"🔒 Manual lockdown by {ctx.author.mention}: {reason}")


@bot.command(name="unlock")
@admin_only()
async def cmd_unlock(ctx: commands.Context) -> None:
    raid_active[ctx.guild.id] = False
    task = raid_lock_task.pop(ctx.guild.id, None)
    if task and not task.done():
        task.cancel()
    n = await unlock_guild(ctx.guild, reason=f"manual by {ctx.author}")
    await ctx.send(f"🔓 Unlocked **{n}** channel(s). Raid mode OFF.")
    await send_log(ctx.guild, f"🔓 Manual unlock by {ctx.author.mention}.")


@bot.command(name="raidmode")
@admin_only()
async def cmd_raidmode(ctx: commands.Context, state: str = "") -> None:
    state = state.lower().strip()
    if state == "on":
        raid_active[ctx.guild.id] = True
        await lockdown_guild(ctx.guild, reason=f"manual raidmode by {ctx.author}")
        await ctx.send("🚨 Raid mode **ON** — server locked.")
    elif state == "off":
        raid_active[ctx.guild.id] = False
        await unlock_guild(ctx.guild, reason=f"manual raidmode off by {ctx.author}")
        await ctx.send("✅ Raid mode **OFF** — server unlocked.")
    else:
        active = raid_active.get(ctx.guild.id, False)
        await ctx.send(f"Raid mode is currently **{'ON' if active else 'OFF'}**.")


@bot.command(name="raidstatus")
async def cmd_status(ctx: commands.Context) -> None:
    cfg = get_config(ctx.guild.id)
    dq = join_times.get(ctx.guild.id, deque())
    embed = discord.Embed(title="🛡️ Anti-raid status", color=discord.Color.blurple())
    embed.add_field(name="Raid mode", value="ON 🔴" if raid_active.get(ctx.guild.id) else "OFF 🟢")
    embed.add_field(name="Joins in window", value=f"{len(dq)}/{cfg['join_threshold_count']} per {cfg['join_threshold_seconds']}s")
    embed.add_field(name="Action", value=cfg["raid_action"], inline=True)
    embed.add_field(name="New-account flag", value=f"< {cfg['new_account_age_days']} days", inline=True)
    embed.add_field(name="Auto-unlock", value=f"{cfg['lockdown_duration_minutes']} min (0 = manual)", inline=True)
    await ctx.send(embed=embed)


@bot.command(name="rank")
async def cmd_rank(ctx: commands.Context, member: discord.Member | None = None) -> None:
    """Show XP level. Usage: .rank [@user]"""
    target = member or ctx.author
    entry = xp_data.get(str(ctx.guild.id), {}).get(str(target.id), {"xp": 0})
    xp = int(entry["xp"])
    level = xp_level(xp)
    nxt = xp_for_level(level + 1)
    # guild rank
    board = sorted(
        xp_data.get(str(ctx.guild.id), {}).items(), key=lambda kv: int(kv[1]["xp"]), reverse=True
    )
    rank = next((i + 1 for i, (uid, _) in enumerate(board) if uid == str(target.id)), len(board) or 1)
    bar_len = 10
    filled = int(bar_len * (xp - xp_for_level(level)) / max(nxt - xp_for_level(level), 1))
    bar = "█" * filled + "░" * (bar_len - filled)
    embed = discord.Embed(title=f"🏆 {target.display_name} — level {level}", color=discord.Color.gold())
    embed.add_field(name="XP", value=f"{xp} / {nxt}", inline=True)
    embed.add_field(name="Rank", value=f"#{rank}", inline=True)
    embed.add_field(name="Progress", value=f"`{bar}`", inline=False)
    await ctx.send(embed=embed)


@bot.command(name="leaderboard", aliases=["lb"])
async def cmd_lb(ctx: commands.Context) -> None:
    """Top 10 by XP."""
    board = sorted(
        xp_data.get(str(ctx.guild.id), {}).items(), key=lambda kv: int(kv[1]["xp"]), reverse=True
    )[:10]
    if not board:
        await ctx.send("No XP yet — chat to earn some (5–15 XP/min).")
        return
    lines = []
    for i, (uid, e) in enumerate(board, 1):
        m = ctx.guild.get_member(int(uid))
        name = m.display_name if m else f"<@{uid}>"
        lines.append(f"**{i}.** {name} — Lvl {xp_level(int(e['xp']))} ({int(e['xp'])} XP)")
    await ctx.send("🏆 **Leaderboard**\n" + "\n".join(lines))


@bot.command(name="afk")
async def cmd_afk(ctx: commands.Context, *, reason: str = "AFK") -> None:
    """Go AFK with a reason. Usage: .afk [reason] — auto-clears on your next message."""
    if ctx.guild is None:
        return
    afk_data.setdefault(str(ctx.guild.id), {})[str(ctx.author.id)] = {
        "reason": reason[:200], "since": time.time()}
    _save_afk(afk_data)
    await ctx.send(f"💤 {ctx.author.mention} is now AFK: **{reason[:200]}**")


@bot.command(name="xpreset")
@admin_only()
async def cmd_xpreset(ctx: commands.Context, member: discord.Member) -> None:
    xp_data.get(str(ctx.guild.id), {}).pop(str(member.id), None)
    _save_xp(xp_data)
    await ctx.send(f"Reset {member.display_name}'s XP.")


@bot.command(name="xpadd")
@admin_only()
async def cmd_xpadd(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Give XP. Usage: .xpadd @user 200"""
    if amount <= 0 or amount > 100000:
        await ctx.send("Amount must be 1–100000.")
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    entry = g.setdefault(str(member.id), {"xp": 0, "last": 0})
    entry["xp"] = int(entry["xp"]) + amount
    _save_xp(xp_data)
    await ctx.send(
        f"Added **{amount} XP** to {member.display_name} — "
        f"now Lvl {xp_level(int(entry['xp']))} ({int(entry['xp'])} XP)."
    )


@bot.command(name="xpset")
@admin_only()
async def cmd_xpset(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Set exact XP. Usage: .xpset @user 500"""
    if amount < 0 or amount > 1000000:
        await ctx.send("Amount must be 0–1000000.")
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    g[str(member.id)] = {"xp": amount, "last": 0}
    _save_xp(xp_data)
    await ctx.send(
        f"Set {member.display_name} to **{amount} XP** (Lvl {xp_level(amount)})."
    )


@bot.command(name="leveladd")
@admin_only()
async def cmd_leveladd(ctx: commands.Context, member: discord.Member, amount: int) -> None:
    """Give levels. Usage: .leveladd @user 2"""
    if amount <= 0 or amount > 100:
        await ctx.send("Amount must be 1–100.")
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    entry = g.setdefault(str(member.id), {"xp": 0, "last": 0})
    new_level = xp_level(int(entry["xp"])) + amount
    entry["xp"] = xp_for_level(new_level)
    _save_xp(xp_data)
    await ctx.send(
        f"Added **{amount} level(s)** to {member.display_name} — "
        f"now Lvl {new_level} ({int(entry['xp'])} XP)."
    )


@bot.command(name="levelset")
@admin_only()
async def cmd_levelset(ctx: commands.Context, member: discord.Member, level: int) -> None:
    """Set exact level. Usage: .levelset @user 5"""
    if level < 0 or level > 100:
        await ctx.send("Level must be 0–100.")
        return
    g = xp_data.setdefault(str(ctx.guild.id), {})
    g[str(member.id)] = {"xp": xp_for_level(level), "last": 0}
    _save_xp(xp_data)
    await ctx.send(f"Set {member.display_name} to **Lvl {level}** ({xp_for_level(level)} XP).")


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
        await ctx.send(f"Usage: `{PREFIX}8ball <question>`")
        return
    await ctx.send(f"🎱 {random.choice(BALL8)}")


@bot.command(name="rps")
async def cmd_rps(ctx: commands.Context, pick: str = "") -> None:
    """Rock paper scissors. Usage: .rps <rock|paper|scissors>"""
    pick = pick.lower().strip()
    short = {"r": "rock", "p": "paper", "s": "scissors"}
    pick = short.get(pick, pick)
    if pick not in ("rock", "paper", "scissors"):
        await ctx.send(f"Usage: `{PREFIX}rps <rock|paper|scissors>`")
        return
    mine = random.choice(["rock", "paper", "scissors"])
    beats = {"rock": "scissors", "paper": "rock", "scissors": "paper"}
    emo = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
    if mine == pick:
        await ctx.send(f"{emo[mine]} vs {emo[pick]} — tie!")
    elif beats[mine] == pick:
        await ctx.send(f"{emo[mine]} vs {emo[pick]} — I win 😎")
    else:
        await ctx.send(f"{emo[mine]} vs {emo[pick]} — you win, gg")


@bot.command(name="roll")
async def cmd_roll(ctx: commands.Context, sides: str = "100") -> None:
    """Roll a die. Usage: .roll [sides]"""
    try:
        n = max(2, min(1000000, int(sides)))
    except ValueError:
        await ctx.send(f"Usage: `{PREFIX}roll [sides]`")
        return
    await ctx.send(f"🎲 {ctx.author.mention} rolled **{random.randint(1, n)}** (1-{n})")


@bot.command(name="flip")
async def cmd_flip(ctx: commands.Context) -> None:
    await ctx.send(f"🪙 **{random.choice(['heads', 'tails'])}**")


@bot.command(name="status")
@owner_only()
async def cmd_statusbot(ctx: commands.Context, *, text: str = "") -> None:
    """Set the bot's status live. Usage: .status <text> | .status off"""
    if not text or text.lower() == "off":
        await bot.change_presence(activity=None)
        await ctx.send("Status cleared.")
        return
    await bot.change_presence(activity=discord.CustomActivity(name=text[:120]))
    await ctx.send(f"Status: **{text[:120]}**")


@bot.command(name="setbio")
@owner_only()
async def cmd_setbio(ctx: commands.Context, *, text: str = "") -> None:
    """Set the bot's About Me bio. Usage: .setbio <text> (190 max)"""
    if not text:
        await ctx.send(f"Usage: `{PREFIX}setbio <text>`")
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as sess:
            async with sess.patch(
                "https://discord.com/api/v10/users/@me",
                headers={"Authorization": f"Bot {TOKEN}"},
                json={"bio": text[:190]},
            ) as r:
                if r.status == 200:
                    await ctx.send("Bio updated! Check the bot's profile.")
                else:
                    await ctx.send(f"❌ Discord said no (HTTP {r.status}).")
    except Exception as exc:
        await ctx.send(f"❌ Failed: `{type(exc).__name__}`")


@bot.command(name="aitest")
@owner_or_admin()
async def cmd_aitest(ctx: commands.Context) -> None:
    """Test the Groq brain EXACTLY as chat uses it. Usage: .aitest"""
    if not AI_API_KEY:
        await ctx.send("❌ No `OPENAI_API_KEY` in `.env` — restart after adding it.")
        return
    await ctx.send(f"🔌 Testing `{AI_MODEL}` (chat-identical payload)...")
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
                        await ctx.send(f"⚠️ HTTP 200 but unparseable ({type(exc).__name__}). "
                                       f"First 300 chars:\n```{raw[:300]}```")
                        return
                    await ctx.send(f"✅ Works! Reply would be:\n> {ans[:300]}"
                                   if ans else "⚠️ HTTP 200 but EMPTY answer — fallback takes over. Retrying usually fixes it.")
                else:
                    await ctx.send(f"❌ Groq HTTP {resp.status}:\n```{raw[:400]}```")
    except Exception as exc:
        await ctx.send(f"❌ Request failed: `{type(exc).__name__}: {exc}`")


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
                await interaction.response.send_message("Role's gone.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        if role.permissions.administrator or role.managed:
            try:
                await interaction.response.send_message(
                    "🔒 Admin/managed roles can't be self-served.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="Role menu toggle")
                await interaction.response.send_message(f"Removed **{role.name}**.", ephemeral=True)
            else:
                await member.add_roles(role, reason="Role menu toggle")
                await interaction.response.send_message(f"Added **{role.name}**!", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            try:
                await interaction.response.send_message(
                    "❌ Can't — my role must sit above that one.", ephemeral=True)
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
        await ctx.send("🧹 Role menus cleared.")
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
        await ctx.send(f"Usage: `{PREFIX}rolemenu @role1 @role2 ...` "
                       f"(admin/managed roles can't be self-served).")
        return
    embed = discord.Embed(title="🎭 Pick your roles", description="Tap a button to add/remove it.",
                          color=discord.Color.blurple())
    view = RoleMenuView(ctx.guild.id, rids)
    for b in view.children:
        role = ctx.guild.get_role(b.role_id)
        if role:
            b.label = role.name[:80]
            b.style = discord.ButtonStyle.primary
    try:
        msg = await ctx.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Can't post: {exc}")
        return
    rr_data.setdefault(str(ctx.guild.id), []).append(
        {"channel": ctx.channel.id, "message": msg.id, "roles": rids})
    _save_rr(rr_data)
    await ctx.send(f"✅ Menu live with {len(rids)} role(s). Survives restarts.")


class TicketOpenView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="🎫 Open a ticket", style=discord.ButtonStyle.blurple,
                       custom_id="wiggle_ticketopen")
    async def open_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
        try:
            channel = await ticket_create(interaction.guild, interaction.user)
        except Exception as exc:
            print(f"[tickets] create failed: {type(exc).__name__}: {exc}")
            try:
                await interaction.followup.send(
                    f"❌ Ticket failed: `{type(exc).__name__}: {exc}` (screenshot this to the dev)",
                    ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        try:
            if channel:
                await interaction.followup.send(f"Ticket: {channel.mention}", ephemeral=True)
            else:
                await interaction.followup.send(
                    "❌ Couldn't make the ticket — I need **Manage Channels** (and a role above members). Tell an admin.",
                    ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass


class TicketCloseView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="🔒 Close ticket", style=discord.ButtonStyle.red,
                       custom_id="wiggle_ticketclose")
    async def close_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        channel = interaction.channel
        topic = getattr(channel, "topic", "") or ""
        is_owner = f"uid:{interaction.user.id}" in topic
        is_staff = bool(interaction.user.guild_permissions.administrator
                        or interaction.user.guild_permissions.manage_guild)
        if not (is_owner or is_staff):
            try:
                await interaction.response.send_message("Not your ticket.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        try:
            await interaction.response.send_message("🔒 Closing in 5s...")
        except (discord.Forbidden, discord.HTTPException):
            pass
        await asyncio.sleep(5)
        try:
            await channel.delete(reason=f"Ticket closed by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass


async def ticket_create(guild: discord.Guild, user) -> discord.TextChannel | None:
    for ch in guild.text_channels:
        if ch.name == f"ticket-{user.name.lower().replace(' ', '-')[:20]}":
            return ch  # already open
    category = discord.utils.get(guild.categories, name="Tickets")
    if category is None:
        try:
            category = await guild.create_category("Tickets", reason="Ticket setup")
        except (discord.Forbidden, discord.HTTPException):
            category = None
    overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  user: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                    read_message_history=True)}
    for role in guild.roles:
        if role.permissions.administrator and not role.is_default():
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                           read_message_history=True)
    try:
        channel = await guild.create_text_channel(
            f"ticket-{user.name.lower().replace(' ', '-')[:20]}",
            category=category, overwrites=overwrites,
            topic=f"uid:{user.id} | opened by {user}",
            reason=f"Ticket for {user}")
        await channel.send(f"👋 {user.mention} staff will be with you.\n"
                           f"Describe your issue — 🔒 button or `.ticketclose` to close.",
                           view=TicketCloseView())
        return channel
    except (discord.Forbidden, discord.HTTPException):
        return None


@bot.command(name="ticketsetup")
@owner_or_admin()
async def cmd_ticketsetup(ctx: commands.Context) -> None:
    """Post the ticket panel here. Usage: .ticketsetup"""
    try:
        await ctx.send(embed=E("🎫 Need help?",
                               "Click below to open a private ticket with staff.",
                               kind="info"), view=TicketOpenView())
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Can't post: {exc}")


@bot.command(name="ticket")
async def cmd_ticket(ctx: commands.Context) -> None:
    """Open your ticket. Usage: .ticket"""
    if ctx.guild is None:
        return
    channel = await ticket_create(ctx.guild, ctx.author)
    if channel:
        await ctx.send(f"Ticket: {channel.mention}")
    else:
        await ctx.send("❌ Can't create channels — tell an admin.")


@bot.command(name="ticketclose")
async def cmd_ticketclose(ctx: commands.Context) -> None:
    """Close this ticket. Usage: .ticketclose (in a ticket channel)"""
    topic = getattr(ctx.channel, "topic", "") or ""
    if not ctx.channel.name.startswith("ticket-"):
        await ctx.send("Not a ticket channel.")
        return
    is_owner = f"uid:{ctx.author.id}" in topic
    is_staff = bool(ctx.author.guild_permissions.administrator
                    or ctx.author.guild_permissions.manage_guild)
    if not (is_owner or is_staff):
        await ctx.send("Not your ticket.")
        return
    await ctx.send("🔒 Closing in 5s...")
    await asyncio.sleep(5)
    try:
        await ctx.channel.delete(reason=f"Ticket closed by {ctx.author}")
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        pass


@bot.command(name="raidconfig")
@admin_only()
async def cmd_config(ctx: commands.Context, key: str = "", *, value: str = "") -> None:
    cfg = get_config(ctx.guild.id)
    if not key:
        lines = [f"**{k}**: `{v}`" for k, v in cfg.items()
                 if k not in ("whitelisted_user_ids", "whitelisted_role_ids")]
        await ctx.send("Current config:\n" + "\n".join(lines))
        return
    key = key.strip()
    if key not in DEFAULT_GUILD_CONFIG:
        await ctx.send(f"Unknown key. Valid: {', '.join(DEFAULT_GUILD_CONFIG)}")
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
                await ctx.send("Use `true` or `false`.")
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
                await ctx.send(f"raid_action must be one of {sorted(VALID_ACTIONS)}")
                return
        update_config(ctx.guild.id, **{key: parsed})
        await ctx.send(f"✅ `{key}` set to `{parsed}`.")
    except ValueError:
        await ctx.send("❌ Could not parse value (expected a number/ID).")


@bot.command(name="setwelcome")
@owner_or_admin()
async def cmd_setwelcome(ctx: commands.Context, channel: str = "") -> None:
    """Welcome channel. Usage: .setwelcome #channel | .setwelcome off"""
    if channel.strip().lower() in ("off", "none", "clear", ""):
        if not channel:
            cid = get_config(ctx.guild.id).get("welcome_channel_id")
            ch = ctx.guild.get_channel(cid) if cid else None
            await ctx.send(f"Welcome channel: {ch.mention if ch else 'not set'}.")
            return
        update_config(ctx.guild.id, welcome_channel_id=None)
        await ctx.send("Welcome messages off.")
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None:
        await ctx.send(f"Usage: `{PREFIX}setwelcome #channel`")
        return
    update_config(ctx.guild.id, welcome_channel_id=target.id)
    await ctx.send(f"👋 Welcomes go to {target.mention}.")


@bot.command(name="setgoodbye")
@owner_or_admin()
async def cmd_setgoodbye(ctx: commands.Context, channel: str = "") -> None:
    """Goodbye channel. Usage: .setgoodbye #channel | .setgoodbye off"""
    if channel.strip().lower() in ("off", "none", "clear", ""):
        if not channel:
            cid = get_config(ctx.guild.id).get("goodbye_channel_id")
            ch = ctx.guild.get_channel(cid) if cid else None
            await ctx.send(f"Goodbye channel: {ch.mention if ch else 'not set'}.")
            return
        update_config(ctx.guild.id, goodbye_channel_id=None)
        await ctx.send("Goodbye messages off.")
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None:
        await ctx.send(f"Usage: `{PREFIX}setgoodbye #channel`")
        return
    update_config(ctx.guild.id, goodbye_channel_id=target.id)
    await ctx.send(f"👋 Goodbyes go to {target.mention}.")


@bot.command(name="linkchannel")
@owner_or_admin()
async def cmd_linkchannel(ctx: commands.Context, channel: str = "") -> None:
    """Toggle a link-safe channel. Usage: .linkchannel [#channel] (blank = list)"""
    cfg = get_config(ctx.guild.id)
    ids = list(cfg.get("link_allowed_channels", []) or [])
    if not channel:
        names = [f"<#{i}>" for i in ids] or ["none — links filtered everywhere"]
        await ctx.send("Link-safe channels: " + ", ".join(names))
        return
    try:
        target = ctx.guild.get_channel(int(channel.strip("<#>")))
    except ValueError:
        target = None
    if target is None or not isinstance(target, discord.TextChannel):
        await ctx.send(f"Usage: `{PREFIX}linkchannel #channel`")
        return
    if target.id in ids:
        ids.remove(target.id)
        msg = f"🔒 {target.mention} filters links again."
    else:
        ids.append(target.id)
        msg = f"🔓 {target.mention} is now link-safe (invites + links allowed)."
    update_config(ctx.guild.id, link_allowed_channels=ids)
    await ctx.send(msg)


@bot.command(name="setlog")
@owner_or_admin()
async def cmd_setlog(ctx: commands.Context, channel: discord.TextChannel | None = None) -> None:
    target = channel or ctx.channel
    update_config(ctx.guild.id, log_channel_id=target.id)
    await ctx.send(f"✅ Raid logs will go to {target.mention}.")


@bot.command(name="setchat")
@owner_or_admin()
async def cmd_setchat(ctx: commands.Context, channel: str = "") -> None:
    """Lock bot chat to one channel. Usage: .setchat #channel | .setchat off | .setchat"""
    channel = channel.strip().lower()
    if not channel:
        cfg = get_config(ctx.guild.id)
        cid = cfg.get("chat_channel_id")
        ch = ctx.guild.get_channel(cid) if cid else None
        await ctx.send(f"Chat channel: {ch.mention if ch else 'anywhere'}.")
        return
    if channel in ("off", "none", "clear", "anywhere"):
        update_config(ctx.guild.id, chat_channel_id=None)
        await ctx.send("✅ I'll chat anywhere.")
        return
    try:
        cid = int(channel.strip("<#>"))
    except ValueError:
        await ctx.send(f"Usage: `{PREFIX}setchat #channel` or `{PREFIX}setchat off`")
        return
    target = ctx.guild.get_channel(cid)
    if target is None:
        await ctx.send("❌ Channel not found.")
        return
    update_config(ctx.guild.id, chat_channel_id=target.id)
    await ctx.send(f"✅ I'll only chat in {target.mention}.")


@bot.command(name="vcjoin")
@owner_or_admin()
async def cmd_vcjoin(ctx: commands.Context, channel: str = "") -> None:
    """Bot joins a VC and sits in it. Usage: .vcjoin [#voice] (blank = your VC)"""
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
        await ctx.send(f"Sit in a VC first, or `.vcjoin #voice`.")
        return
    try:
        vc = ctx.guild.voice_client
        if vc and vc.channel and vc.channel.id == target.id:
            await ctx.send(f"Already in {target.mention}.")
            return
        if vc:
            await vc.move_to(target)
        else:
            await target.connect()
        update_config(ctx.guild.id, mod_vc_channel_id=target.id)
        await ctx.send(f"🎧 Joined {target.mention} — moderating VC. `.vcleave` to bounce me.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Can't join (need Connect perm): {exc}")


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
@owner_or_admin()
async def cmd_voice(ctx: commands.Context, name: str = "") -> None:
    """Pick the AI voice. Usage: .voice [aria|jenny|guy|davis|jane|sara|tony|nancy]"""
    name = name.lower().strip()
    if not name:
        cur = get_config(ctx.guild.id).get("tts_voice", "aria")
        await ctx.send("Voice: **{}**. Options: {}".format(cur, ", ".join(sorted(VOICES))))
        return
    if name not in VOICES:
        await ctx.send(f"Unknown voice. Options: {', '.join(sorted(VOICES))}")
        return
    update_config(ctx.guild.id, tts_voice=name)
    await ctx.send(f"🎙️ Voice set to **{name}**.")


@bot.command(name="speak")
async def cmd_speak(ctx: commands.Context, *, text: str = "") -> None:
    """Bot says it out loud in VC. Usage: .speak <text>"""
    if not text:
        await ctx.send(f"Usage: `{PREFIX}speak <text>` (join a VC with `.vcjoin` first)")
        return
    status = await speak_text(ctx.guild, text)
    if status != "speaking":
        await ctx.send(f"🔇 {status}")


@bot.command(name="voiceauto")
@owner_or_admin()
async def cmd_voiceauto(ctx: commands.Context, state: str = "") -> None:
    """Speak ping replies aloud when in VC. Usage: .voiceauto <on|off>"""
    state = state.lower().strip()
    if state in ("on", "off"):
        update_config(ctx.guild.id, voice_auto=(state == "on"))
        await ctx.send(f"🔊 Voice replies **{state.upper()}**.")
    else:
        cur = get_config(ctx.guild.id).get("voice_auto", False)
        await ctx.send(f"Voice replies: **{'ON' if cur else 'OFF'}**. `.voiceauto on|off`.")


@bot.command(name="vcleave")
@owner_or_admin()
async def cmd_vcleave(ctx: commands.Context) -> None:
    vc = ctx.guild.voice_client if ctx.guild else None
    if not vc:
        await ctx.send("Not in a VC.")
        return
    try:
        await vc.disconnect()
    except (discord.Forbidden, discord.HTTPException):
        pass
    update_config(ctx.guild.id, mod_vc_channel_id=None)
    await ctx.send("👋 Left VC.")


@bot.command(name="setlobby")
@owner_or_admin()
async def cmd_setlobby(ctx: commands.Context, channel: str = "") -> None:
    """Join-to-create voice setup. Usage: .setlobby #voice | .setlobby off | .setlobby"""
    channel = channel.strip().lower()
    if not channel:
        cfg = get_config(ctx.guild.id)
        cid = cfg.get("lobby_channel_id")
        ch = ctx.guild.get_channel(cid) if cid else None
        await ctx.send(f"Lobby VC: {ch.mention if ch else 'not set'}. "
                       f"Join it and I make you a temp VC. `.setlobby #voice` to set.")
        return
    if channel in ("off", "none", "clear"):
        update_config(ctx.guild.id, lobby_channel_id=None)
        await ctx.send("✅ Lobby system off.")
        return
    try:
        cid = int(channel.strip("<#>"))
    except ValueError:
        await ctx.send(f"Usage: `{PREFIX}setlobby #voice-channel` (must be a VOICE channel).")
        return
    target = ctx.guild.get_channel(cid)
    if not isinstance(target, discord.VoiceChannel):
        await ctx.send("❌ That's not a voice channel. Create one (e.g. `＋ Create`) first.")
        return
    update_config(ctx.guild.id, lobby_channel_id=target.id)
    await ctx.send(f"✅ Join {target.mention} and I'll spin you a personal VC (deleted when empty).")


@bot.command(name="whitelist")
@admin_only()
async def cmd_whitelist(ctx: commands.Context, action: str = "", target: str = "") -> None:
    cfg = get_config(ctx.guild.id)
    action = action.lower()
    if action == "list":
        await ctx.send(
            f"Users: {cfg['whitelisted_user_ids'] or '—'}\nRoles: {cfg['whitelisted_role_ids'] or '—'}"
        )
        return
    # accept mention or raw ID, for user or role
    raw = target.strip("<>@#!&")
    try:
        tid = int(raw)
    except ValueError:
        await ctx.send(f"Usage: `{PREFIX}whitelist add|remove <@user or ID>` / `{PREFIX}whitelist list`")
        return
    users = list(cfg["whitelisted_user_ids"])
    if action == "add" and tid not in users:
        users.append(tid)
    elif action == "remove" and tid in users:
        users.remove(tid)
    else:
        await ctx.send(f"Usage: `{PREFIX}whitelist add|remove <@user or ID>`")
        return
    update_config(ctx.guild.id, whitelisted_user_ids=users)
    await ctx.send(f"✅ Whitelist {'added' if action == 'add' else 'removed'} `{tid}`.")


# ---------- verify gate (dyno-style) ----------

class VerifyView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Verify me", style=discord.ButtonStyle.green,
                       custom_id="wigglesworth_verify")
    async def verify_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        cfg = get_config(interaction.guild.id)
        role = interaction.guild.get_role(cfg.get("verified_role_id") or 0)
        if role is None:
            await interaction.response.send_message("❌ Verify isn't set up.", ephemeral=True)
            return
        if role in interaction.user.roles:
            await interaction.response.send_message("You're already verified!", ephemeral=True)
            return
        try:
            await interaction.user.add_roles(role, reason="Self-verify button")
            await interaction.response.send_message(
                f"✅ Verified! Welcome to **{interaction.guild.name}**.", ephemeral=True)
            await send_log(interaction.guild, f"✅ {interaction.user} (`{interaction.user.id}`) verified.")
        except (discord.Forbidden, discord.HTTPException):
            await interaction.response.send_message(
                "❌ Can't assign role (bot role must be above Verified). Tell an admin.",
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
            await ctx.send(f"❌ Can't create role (need Manage Roles): {exc}")
            return
    if vchan is None:
        try:
            vchan = await guild.create_text_channel("verify", reason="Verify gate setup",
                                                    topic="Click ✅ Verify me below to unlock the server!")
        except (discord.Forbidden, discord.HTTPException) as exc:
            await ctx.send(f"❌ Can't create channel (need Manage Channels): {exc}")
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
        await vchan.send("👋 **New here?** Click the button to unlock **{}**!\n"
                         "(Having trouble? type `.verify`)".format(guild.name),
                         view=VerifyView())
    except (discord.Forbidden, discord.HTTPException):
        pass
    await ctx.send(f"✅ Gate live: {vchan.mention} + `{vrole.name}` — locked {locked} channel(s)"
                   + (f", {skipped} skipped (no perms)." if skipped else "."))


@bot.command(name="helpme")
async def cmd_helpme(ctx: commands.Context) -> None:
    """Show the help card. Usage: .helpme"""
    if ctx.guild is None:
        await ctx.send("Use this inside the server.")
        return
    await ctx.send(help_text(ctx.guild.name))


@bot.command(name="verifyoff")
@owner_or_admin()
async def cmd_verifyoff(ctx: commands.Context) -> None:
    update_config(ctx.guild.id, verify_enabled=False)
    await ctx.send("Verify gate off (roles/channels untouched — re-run `.verifysetup` to re-lock).")


@bot.command(name="verifyfix")
@owner_or_admin()
async def cmd_verifyfix(ctx: commands.Context) -> None:
    """Repair an existing gate: hide #verify from verified, re-post button."""
    cfg = get_config(ctx.guild.id)
    vchan = ctx.guild.get_channel(cfg.get("verify_channel_id") or 0)
    vrole = ctx.guild.get_role(cfg.get("verified_role_id") or 0)
    if not vchan or not vrole:
        await ctx.send("Nothing to fix — run `.verifysetup [@role] [#channel]` first.")
        return
    try:
        await vchan.set_permissions(vrole, view_channel=False, reason="Verify gate fix")
    except (discord.Forbidden, discord.HTTPException):
        pass
    try:
        await vchan.send("👋 **New here?** Click the button to unlock **{}**!\n"
                         "(Having trouble? type `.verify`)".format(ctx.guild.name),
                         view=VerifyView())
    except (discord.Forbidden, discord.HTTPException):
        pass
    update_config(ctx.guild.id, verify_enabled=True)
    await ctx.send(f"✅ Fixed: {vrole.name} can no longer see {vchan.mention}; button re-posted.")


@bot.command(name="verify")
async def cmd_verify(ctx: commands.Context) -> None:
    """Fallback when the button won't work. Usage: .verify"""
    if ctx.guild is None:
        await ctx.send("Verify inside the server.")
        return
    cfg = get_config(ctx.guild.id)
    if not cfg.get("verify_enabled"):
        await ctx.send("No verify gate here.")
        return
    role = ctx.guild.get_role(cfg.get("verified_role_id") or 0)
    if role is None:
        await ctx.send("❌ Verified role missing — ask an admin to re-run `.verifysetup`.")
        return
    if role in ctx.author.roles:
        await ctx.send("Already verified!")
        return
    try:
        await ctx.author.add_roles(role, reason="Self-verify command")
        await ctx.send(f"✅ Verified! Welcome to **{ctx.guild.name}**.")
    except (discord.Forbidden, discord.HTTPException):
        await ctx.send("❌ Can't assign role — tell an admin (bot role order).")


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
        await ctx.send(f"Usage: `{PREFIX}purge <1-100> [@user]`")
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
            await ctx.send(f"🧹 Deleted {count} message(s) from {member.display_name}.",
                           delete_after=6)
        else:
            deleted = await ctx.channel.purge(limit=amount + 1)  # +1 includes the command
            await ctx.send(f"🧹 Deleted {max(len(deleted) - 1, 0)} message(s).", delete_after=6)
        await send_log(ctx.guild, f"🧹 {ctx.author} purged {amount}"
                                  f"{f' from {member}' if member else ''} in {ctx.channel.mention}.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Purge failed (need Manage Messages): {exc}")


@bot.command(name="mute")
@admin_only()
async def cmd_mute(ctx: commands.Context, member: discord.Member, minutes: int = 10,
                   *, reason: str = "mod mute") -> None:
    """Timeout a member. Usage: .mute @user [minutes] [reason]"""
    if minutes < 1 or minutes > 40320:
        await ctx.send("Minutes must be 1–40320 (28 days max).")
        return
    try:
        await member.timeout(timedelta(minutes=minutes), reason=f"{ctx.author}: {reason}")
        await ctx.send(f"🔇 {member.display_name} muted {minutes}m.")
        await send_log(ctx.guild, f"🔇 {ctx.author} muted {member} (`{member.id}`) {minutes}m: {reason}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Mute failed (role order / perms?): {exc}")


@bot.command(name="unmute")
@admin_only()
async def cmd_unmute(ctx: commands.Context, member: discord.Member) -> None:
    try:
        await member.timeout(None, reason=f"unmuted by {ctx.author}")
        await ctx.send(f"🔊 {member.display_name} unmuted.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Unmute failed: {exc}")


@bot.command(name="userinfo", aliases=["whois"])
async def cmd_userinfo(ctx: commands.Context, member: discord.Member | None = None) -> None:
    """Account intel — spot alts. Usage: .userinfo [@user]"""
    m = member or ctx.author
    now = datetime.now(timezone.utc)
    created = m.created_at if m.created_at.tzinfo else m.created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created).days
    joined = m.joined_at.strftime("%Y-%m-%d %H:%M UTC") if getattr(m, "joined_at", None) else "?"
    roles = ", ".join(r.name for r in getattr(m, "roles", [])[1:]) or "none"
    flag = " ⚠️ NEW ACCOUNT" if age_days < 7 else ""
    embed = discord.Embed(title=f"👤 {m.display_name}", color=discord.Color.blurple())
    embed.add_field(name="ID", value=f"`{m.id}`", inline=True)
    embed.add_field(name="Account age", value=f"{age_days} days{flag}", inline=True)
    embed.add_field(name="Joined server", value=joined, inline=True)
    embed.add_field(name="Roles", value=roles[:500], inline=False)
    await ctx.send(embed=embed)


@bot.command(name="slowmode")
@admin_only()
async def cmd_slowmode(ctx: commands.Context, seconds: int = -1) -> None:
    """Spam damper. Usage: .slowmode <0-21600, 0 = off>"""
    if seconds < 0 or seconds > 21600:
        await ctx.send(f"Usage: `{PREFIX}slowmode <0-21600>` (current: {ctx.channel.slowmode_delay}s)")
        return
    try:
        await ctx.channel.edit(slowmode_delay=seconds, reason=f"slowmode by {ctx.author}")
        await ctx.send(f"🐢 Slowmode {'off' if seconds == 0 else f'{seconds}s'}.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Failed: {exc}")


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    await send_log(member.guild, f"👋 {member} (`{member.id}`) left.")
    cfg = get_config(member.guild.id)
    gchan = member.guild.get_channel(cfg.get("goodbye_channel_id") or 0)
    if gchan:
        try:
            await gchan.send(embed=E(f"👋 {member.display_name} left",
                                     f"We'll… probably not miss them. Bye!",
                                     kind="info"))
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
    embed = E("🗑️ Message deleted", kind="info")
    embed.add_field(name="Author", value=f"{message.author.mention} (`{message.author.id}`)",
                    inline=True)
    embed.add_field(name="Channel", value=message.channel.mention, inline=True)
    embed.add_field(name="Content", value=f"```{text or '(no text)'}```{atts}", inline=False)
    await send_log(message.guild, "", embed=embed)


@bot.command(name="say")
@admin_only()
async def cmd_say(ctx: commands.Context, channel: discord.TextChannel | None = None, *, text: str = "") -> None:
    """Speak through the bot. Usage: .say [#channel] <message>"""
    if not text:
        await ctx.send(f"Usage: `{PREFIX}say [#channel] <message>`")
        return
    target = channel or ctx.channel
    try:
        await target.send(text)
        if target.id != ctx.channel.id:
            await ctx.send(f"✅ Sent in {target.mention}.")
        else:
            try:
                await ctx.message.delete()
            except (discord.Forbidden, discord.HTTPException):
                pass
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Could not send: {exc}")


explode_cooldown: dict[int, float] = {}


@bot.command(name="explode")
@owner_or_admin()
async def cmd_explode(ctx: commands.Context, *, target: str = "") -> None:
    """Fake server nuke. 100% theater, 0% damage. Usage: .explode [@user]"""
    import time as _t
    now = _t.time()
    if now - explode_cooldown.get(ctx.guild.id if ctx.guild else 0, 0) < 360:
        await ctx.send("🧯 Chill — the nuke is recharging (6 min).")
        return
    if ctx.guild:
        explode_cooldown[ctx.guild.id] = now
    gname = ctx.guild.name if ctx.guild else "this server"
    members = ctx.guild.member_count if ctx.guild else 69
    channels = len(ctx.guild.channels) if ctx.guild else 12
    victim = target.strip() or "everyone"
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
    await ctx.send("🛑 Shutting down...")
    await bot.close()


@bot.command(name="disable")
@owner_only()
async def cmd_disable(ctx: commands.Context) -> None:
    """Owner-only: bot stays online but ignores everything except .enable."""
    global bot_enabled
    bot_enabled = False
    await ctx.send("🔴 Bot disabled — I'll ignore pings, XP and raids until `.enable`.")


@bot.command(name="enable")
@owner_only()
async def cmd_enable(ctx: commands.Context) -> None:
    global bot_enabled
    bot_enabled = True
    await ctx.send("🟢 Bot enabled.")


@bot.command(name="diag")
@owner_or_admin()
async def cmd_diag(ctx: commands.Context) -> None:
    """Show bot state for debugging. Usage: .diag"""
    await ctx.send(
        f"enabled: **{bot_enabled}**\n"
        f"prefix: `{PREFIX}` | me: {bot.user.mention if bot.user else '?'}"
    )


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
@cmd_ticketsetup.error
@cmd_aitest.error
@cmd_statusbot.error
@cmd_setbio.error
@cmd_setlobby.error
@cmd_vcjoin.error
@cmd_vcleave.error
@cmd_voice.error
@cmd_voiceauto.error
@cmd_whitelist.error
@cmd_say.error
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
@cmd_shutdown.error
@cmd_disable.error
@cmd_enable.error
@cmd_diag.error
async def admin_error(ctx: commands.Context, error: commands.CommandError) -> None:
    if isinstance(error, commands.CheckFailure):
        if ctx.command and ctx.command.name in ("shutdown", "disable", "enable", "status",
                                                 "setbio"):            await ctx.send("❌ Owner only — set your Discord user ID as OWNER_ID in .env.")
        elif ctx.command and ctx.command.name in ("setlog", "setlobby", "setchat", "diag",
                                                     "verifysetup", "verifyoff", "aitest",
                                                     "rolemenu", "teach", "unteach", "mood",
                                                     "setqotd", "chaton", "chatoff", "explode",
                                                     "ticketsetup", "setwelcome", "setgoodbye",
                                                     "linkchannel", "vcjoin", "vcleave",
                                                     "voiceauto", "voice"):
            await ctx.send("❌ Bot owner or server admin only.")
        else:
            await ctx.send("❌ You need **Administrator** or **Manage Server** permission.")
    else:
        await ctx.send(f"❌ Error: {error}")


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
    bot.run(TOKEN)
