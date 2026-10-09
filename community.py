"""Wigglesworth community expansion: custom commands, mod cases, suggestions,
announcements, temp roles, reminders, polls, applications, events, backups, wizard.

Same architecture as security.py: JSON file stores in DATA_DIR, one scheduler
loop, persistent views re-registered on ready, dashboard via community_web.py.
No new dependencies.
"""
from __future__ import annotations

import json
import random
import time
import uuid
from collections import defaultdict

import discord
from discord.ext import tasks

bm = None  # bot module ref, set by setup()
_bot = None  # commands.Bot instance, set by setup()

FILES = {}
DATA = {}
DIRTY = set()
_cooldowns = {}  # (gid, scope, uid) -> ts
RESERVED = set()


def _path(name):
    return bm.DATA_DIR / name


def _load(name):
    try:
        p = _path(name)
        if p.exists():
            d = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(d, (dict, list)):
                return d
    except (json.JSONDecodeError, OSError):
        pass
    return [] if name in ("temproles.json", "reminders.json") else {}


def _save(name):
    try:
        path = FILES.get(name)
        if path is None:
            return  # setup() not yet run; data held in memory
        path.write_text(json.dumps(DATA[name], indent=2), encoding="utf-8")
        DIRTY.discard(name)
    except OSError:
        DIRTY.add(name)


def save_all():
    for name in list(DATA):
        _save(name)


def store(name, default):
    """Get a persistent store container, creating it if setup() never ran."""
    cur = DATA.get(name)
    if not isinstance(cur, type(default)):
        cur = default
        DATA[name] = cur
    return cur


def new_id(prefix=""):
    return f"{prefix}{int(time.time() * 1000):x}{random.randint(0, 255):02x}"


def new_case_id():
    return f"CASE-{uuid.uuid4().hex[:6].upper()}"


# ---------- shared helpers ----------

def has_perm(member: discord.Member, level: str) -> bool:
    """level: everyone|mod|admin|owner. Reuses Discord permissions + OWNER_IDS."""
    if level in (None, "", "everyone"):
        return True
    if member.id in bm.OWNER_IDS:
        return True
    perms = member.guild_permissions
    if level == "owner":
        return False
    if level == "admin":
        return bool(perms.administrator or perms.manage_guild)
    if level == "mod":  # noqa: SIM114
        return bool(perms.administrator or perms.manage_guild or perms.manage_messages
                    or perms.kick_members or perms.ban_members or perms.moderate_members
                    or perms.manage_channels)
    return True


def check_cooldown(gid, scope: str, uid, secs: int) -> int:
    """Returns seconds remaining (0 = ok, and records the hit)."""
    if secs <= 0:
        return 0
    now = time.time()
    k = (str(gid), scope, str(uid))
    last = _cooldowns.get(k, 0)
    if now - last < secs:
        return int(secs - (now - last))
    _cooldowns[k] = now
    return 0


def render_vars(text: str, member: discord.Member, guild: discord.Guild, channel) -> str:
    now = time.localtime()
    subs = {
        "{user}": member.display_name,
        "{mention}": member.mention,
        "{server}": guild.name,
        "{channel}": getattr(channel, "name", "here"),
        "{date}": time.strftime("%Y-%m-%d", now),
        "{time}": time.strftime("%H:%M", now),
        "{membercount}": str(len([m for m in guild.members if not m.bot])),
    }
    for k, v in subs.items():
        text = text.replace(k, v)
    return text


def guild_cfg(gid):
    return bm.get_config(gid)


def audit(action: str, gid, **kw) -> None:
    """Tamper-evident trail for sensitive admin actions (uses existing track())."""
    try:
        bm.track(gid, "mod", action=action, **kw)
    except Exception:
        pass


# ---------- scheduler: announcements, temp roles, reminders, event reminders ----------

@tasks.loop(seconds=30)
async def sched_loop() -> None:
    try:
        await _run_due()
    except Exception as exc:
        print(f"[community] sched_loop failed: {exc}")


async def _run_due() -> None:
    now = time.time()
    # announcements due
    for gid, items in list(DATA.get("announcements.json", {}).items()):
        for ann in items:
            if ann.get("status") == "scheduled" and ann.get("send_at", 0) <= now:
                await _send_announcement(gid, ann)
                await _sleep_small()
    # temp roles expired
    for tr in list(DATA.get("temproles.json", [])):
        if tr.get("status") == "active" and tr.get("expires_at", 0) <= now:
            await _expire_temprole(tr)
            await _sleep_small()
    # reminders due
    for r in list(DATA.get("reminders.json", [])):
        if r.get("status") == "active" and r.get("next_run", 0) <= now:
            await _send_reminder(r)
            await _sleep_small()
    # event reminders + auto-start
    for gid, items in list(DATA.get("eventsched.json", {}).items()):
        for ev in items:
            if ev.get("status") != "active":
                continue
            start = ev.get("starts_at", 0)
            for mins in sorted(ev.get("remind_mins", [60]), reverse=True):
                key = f"rem{mins}"
                if (ev.get("reminded", {}) or {}).get(key) is not True and start - mins * 60 <= now < start:
                    await _send_event_notice(ev, mins)
                    ev.setdefault("reminded", {})[key] = True
                    _save("eventsched.json")
                    await _sleep_small()
            if start <= now and not ev.get("started"):
                ev["started"] = True
                _save("eventsched.json")
                await _send_event_notice(ev, 0)
                await _sleep_small()
    if DIRTY:
        save_all()


async def _sleep_small():
    try:
        import asyncio as _aio
        await _aio.sleep(1)
    except Exception:
        pass


def _get_guild(gid):
    try:
        return _bot.get_guild(int(gid))
    except (ValueError, TypeError):
        return None


async def _send_announcement(gid, ann) -> None:
    ann["status"] = "sending"
    _save("announcements.json")
    guild = _get_guild(gid)
    if guild is None:
        ann["status"] = "failed"
        ann["last_error"] = "bot no longer in server"
        _save("announcements.json")
        return
    ch = guild.get_channel(ann.get("channel_id") or 0)
    if ch is None:
        ann["status"] = "failed"
        ann["last_error"] = "channel not found"
        _save("announcements.json")
        return
    try:
        if ann.get("embed"):
            em = discord.Embed(title=(ann.get("title") or "")[:256],
                               description=(ann.get("body") or "")[:4000],
                               color=int(ann.get("color", "0x5865F2"), 16))
            await ch.send(embed=em)
        else:
            await ch.send((ann.get("body") or "")[:2000] or "(empty)")
        if ann.get("recurs_s"):
            ann["send_at"] = time.time() + int(ann["recurs_s"])
            ann["status"] = "scheduled"
            ann["last_error"] = ""
            ann["sent_count"] = int(ann.get("sent_count", 0)) + 1
        else:
            ann["status"] = "sent"
            ann["sent_count"] = int(ann.get("sent_count", 0)) + 1
            ann["last_error"] = ""
    except (discord.Forbidden, discord.HTTPException) as exc:
        ann["status"] = "failed"
        ann["last_error"] = str(exc)[:200]
    _save("announcements.json")


async def _expire_temprole(tr) -> None:
    tr["status"] = "expiring"
    _save("temproles.json")
    guild = _get_guild(tr.get("guild"))
    ok, detail = False, ""
    if guild is None:
        detail = "bot no longer in server"
    else:
        member = guild.get_member(int(tr.get("user_id", 0)))
        role = guild.get_role(int(tr.get("role_id", 0)))
        if member is None:
            detail = "member left the server"
            ok = True  # nothing to remove
        elif role is None:
            detail = "role was deleted"
            ok = True
        else:
            try:
                await member.remove_roles(role, reason="temp role expired")
                ok, detail = True, f"removed {role.name}"
            except (discord.Forbidden, discord.HTTPException) as exc:
                detail = str(exc)[:200]
    tr["status"] = "expired" if ok else "failed"
    tr["result"] = detail
    _save("temproles.json")
    try:
        if ok and guild is not None:
            audit(tr.get("guild"), "temprole_expired",
                  user=str(tr.get("user_id")), role=str(tr.get("role_id")))
    except Exception:
        pass


async def _send_reminder(r) -> None:
    r["status"] = "sending"
    _save("reminders.json")
    guild = _get_guild(r.get("guild"))
    detail = ""
    ok = False
    if guild is not None:
        ch = guild.get_channel(r.get("channel_id") or 0)
        if ch is not None:
            try:
                await ch.send(f"⏰ <@{r['user_id']}> {r.get('text', '')[:1500]}")
                ok = True
            except (discord.Forbidden, discord.HTTPException) as exc:
                detail = str(exc)[:200]
        else:
            detail = "channel not found"
    else:
        detail = "bot no longer in server"
    if r.get("interval_s"):
        r["next_run"] = time.time() + int(r["interval_s"])
        r["status"] = "active" if ok else "failed"
        r["last_error"] = "" if ok else detail
    else:
        r["status"] = "sent" if ok else "failed"
        r["last_error"] = "" if ok else detail
    _save("reminders.json")


async def _send_event_notice(ev, mins: int) -> None:
    guild = _get_guild(ev.get("guild"))
    if guild is None:
        return
    ch = guild.get_channel(ev.get("channel_id") or 0)
    if ch is None:
        return
    going = [u for u, v in (ev.get("rsvps", {}) or {}).items() if v == "going"]
    mentions = " ".join(f"<@{u}>" for u in going[:20])
    when = "starting NOW" if mins == 0 else f"starting in {mins}m"
    try:
        await ch.send(f"📅 **{ev.get('title', 'Event')[:200]}** {when}! {mentions}".strip()[:2000])
    except (discord.Forbidden, discord.HTTPException):
        pass


# ---------- 1. custom commands ----------

def custom_cmds(gid):
    return store("customcmds.json", {}).setdefault(str(gid), {})


def validate_cmd_name(name: str):
    """Returns error string or '' if usable."""
    import re as _re
    n = (name or "").strip().lower()
    if not _re.fullmatch(r"[a-z0-9_-]{2,24}", n):
        return "Use 2–24 chars: lowercase letters, numbers, - or _."
    if n in RESERVED:
        return f"`{n}` is already a bot command."
    return ""


def build_response(cmd: dict, member: discord.Member, guild: discord.Guild, channel):
    text = render_vars(cmd.get("response", ""), member, guild, channel)
    if not cmd.get("embed"):
        return text[:2000] or "(empty response)", None
    try:
        color = int(str(cmd.get("color", "5865F2")).strip().lstrip("#"), 16)
    except ValueError:
        color = 0x5865F2
    em = discord.Embed(title=render_vars(cmd.get("title", "") or "", member, guild, channel)[:256],
                       description=text[:4000],
                       color=color)
    return None, em


async def try_custom(message: discord.Message, content: str) -> bool:
    """Run a matching custom command. Returns True if handled."""
    prefix = bm.PREFIX
    if not content.startswith(prefix):
        return False
    first = content[len(prefix):].split(None, 1)[0].lower() if content[len(prefix):].strip() else ""
    if not first:
        return False
    cmds = DATA.get("customcmds.json", {}).get(str(message.guild.id), {})
    cmd = cmds.get(first)
    if not cmd or not cmd.get("enabled", True):
        return False
    if not has_perm(message.author, cmd.get("perm", "everyone")):
        return False
    left = check_cooldown(message.guild.id, f"cc:{first}", message.author.id,
                          int(cmd.get("cooldown", 0) or 0))
    if left:
        try:
            await message.channel.send(f"⏳ `{first}` on cooldown ({left}s).", delete_after=6)
        except (discord.Forbidden, discord.HTTPException):
            pass
        return True
    text, em = build_response(cmd, message.author, message.guild, message.channel)
    try:
        if em is not None:
            await message.channel.send(embed=em)
        else:
            await message.channel.send(text)
    except (discord.Forbidden, discord.HTTPException):
        return True
    try:
        bm.track(message.guild.id, "cmd", name=first)
    except Exception:
        pass
    return True


# ---------- 2. moderation cases ----------

def open_case(guild_id, target_id, target_name, mod_id, mod_name,
              action: str, reason: str = "", evidence: str = "") -> dict:
    cases = store("cases.json", {}).setdefault(str(guild_id), [])
    case = {"id": new_case_id(), "target_id": str(target_id),
            "target_name": (target_name or "")[:100],
            "mod_id": str(mod_id), "mod_name": (mod_name or "")[:100],
            "action": action, "reason": (reason or "")[:500],
            "evidence": (evidence or "")[:500], "status": "open",
            "appeal": "", "notes": [], "created": time.time(), "updated": time.time()}
    cases.append(case)
    DATA["cases.json"][str(guild_id)] = cases[-500:]
    _save("cases.json")
    try:
        bm.track(guild_id, "mod", action=f"case:{action}")
    except Exception:
        pass
    return case


def find_case(guild_id, cid: str):
    cid = (cid or "").strip().upper()
    for c in store("cases.json", {}).get(str(guild_id), []):
        if c["id"].upper() == cid or c["id"].upper().startswith(cid):
            return c
    return None


def user_cases(guild_id, user_id):
    return [c for c in store("cases.json", {}).get(str(guild_id), [])
            if str(c.get("target_id")) == str(user_id)]


# ---------- 3. suggestions ----------

SUG_STATUS = ("pending", "review", "accepted", "progress", "rejected")


def open_suggestion(guild_id, author_id, author_name, text: str):
    subs = store("suggestions.json", {}).setdefault(str(guild_id), {})
    sid = new_id("S")
    subs[sid] = {"id": sid, "author_id": str(author_id),
                 "author_name": (author_name or "")[:100],
                 "text": text[:1000], "status": "pending", "response": "",
                 "up": [], "down": [], "message": 0, "channel": 0,
                 "created": time.time()}
    _save("suggestions.json")
    return subs[sid]


def sug_embed(guild, sub: dict):
    score = len(sub.get("up", [])) - len(sub.get("down", []))
    em = discord.Embed(title=f"💡 Suggestion ({sub['id']})",
                       description=sub.get("text", "")[:1000],
                       color=discord.Color.blurple())
    em.add_field(name="Status", value=sub.get("status", "pending").title(), inline=True)
    em.add_field(name="Score", value=f"{score:+d} (👍 {len(sub.get('up', []))} / 👎 {len(sub.get('down', []))})",
                 inline=True)
    em.add_field(name="By", value=f"<@{sub.get('author_id')}>", inline=True)
    if sub.get("response"):
        em.add_field(name="Staff response", value=sub["response"][:1000], inline=False)
    em.set_footer(text="Wigglesworth suggestions · one vote per person")
    return em


class SuggestView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def _vote(self, interaction: discord.Interaction, side: str):
        guild = interaction.guild
        sub = next((s for s in DATA.get("suggestions.json", {}).get(str(guild.id), {}).values()
                    if s.get("message") == interaction.message.id), None)
        if sub is None:
            try:
                await interaction.response.send_message("Suggestion not found.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        uid = str(interaction.user.id)
        other = "down" if side == "up" else "up"
        if uid in sub.get(side, []):
            sub[side].remove(uid)
            msg = "Vote removed."
        else:
            if uid in sub.get(other, []):
                sub[other].remove(uid)
            sub.setdefault(side, []).append(uid)
            msg = "Vote counted!"
        _save("suggestions.json")
        try:
            await interaction.message.edit(embed=sug_embed(guild, sub))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass

    @discord.ui.button(label="Upvote", emoji="👍", style=discord.ButtonStyle.success,
                       custom_id="sug_up")
    async def up(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "up")

    @discord.ui.button(label="Downvote", emoji="👎", style=discord.ButtonStyle.danger,
                       custom_id="sug_down")
    async def down(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._vote(interaction, "down")


# ---------- polls (command-only, persistent buttons) ----------

def _polls():
    return DATA.setdefault("polls.json", {})


class PollView(discord.ui.View):
    def __init__(self, options) -> None:
        super().__init__(timeout=None)
        for idx, opt in enumerate(options[:6]):
            self.add_item(_PollButton(idx, opt[:80]))

    @staticmethod
    def for_message(message_id: int):
        p = _polls().get(str(message_id))
        if not p or p.get("status") != "open":
            return None
        return PollView(p["options"])


class _PollButton(discord.ui.Button):
    def __init__(self, idx: int, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.secondary,
                         custom_id=f"poll:{idx}")

    async def callback(self, interaction: discord.Interaction) -> None:
        p = _polls().get(str(interaction.message.id))
        if not p or p.get("status") != "open":
            try:
                await interaction.response.send_message("This poll is closed.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        uid = str(interaction.user.id)
        votes = p.setdefault("votes", {})
        if votes.get(uid) == self.custom_id.split(":")[1]:
            del votes[uid]
            msg = "Vote removed."
        else:
            votes[uid] = self.custom_id.split(":")[1]
            msg = "Vote counted!"
        _save("polls.json")
        try:
            await interaction.message.edit(embed=poll_embed(p))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass


def poll_embed(p: dict):
    counts = [0] * len(p["options"])
    for v in (p.get("votes") or {}).values():
        try:
            counts[int(v)] += 1
        except (ValueError, IndexError):
            pass
    total = sum(counts) or 1
    lines = [f"`{i + 1}.` {opt} — **{c}** ({c * 100 // total}%)"
             for i, (opt, c) in enumerate(zip(p["options"], counts))]
    em = discord.Embed(title=f"📊 {p['question'][:250]}",
                       description="\n".join(lines)[:4000],
                       color=discord.Color.blurple())
    em.set_footer(text=f"Wigglesworth poll · {sum(counts)} votes · tap to change your vote")
    return em


def register_views() -> None:
    """Re-arm persistent views after restarts (deterministic custom_ids)."""
    if _bot is None:
        return
    _bot.add_view(SuggestView())
    for p in _polls().values():
        if p.get("status") == "open" and p.get("message"):
            try:
                _bot.add_view(PollView(p["options"]))
            except Exception:
                pass
    for gid, pack in list(DATA.get("applications.json", {}).items()):
        for sid, sub in (pack.get("subs", {}) or {}).items():
            if sub.get("status") in ("pending", "info") and sub.get("message"):
                try:
                    _bot.add_view(ReviewView(sid))
                except Exception:
                    pass
    for gid, items in list(DATA.get("eventsched.json", {}).items()):
        for ev in items:
            if ev.get("status") == "active" and ev.get("message"):
                try:
                    _bot.add_view(EventView(ev["id"]))
                except Exception:
                    pass


# ---------- 6. backups ----------

BACKUPABLE_PREFIXES = ("automod_", "raid_", "verify", "verified_", "log_", "welcome_",
                       "goodbye_", "chat_", "bot_mood", "autoreact", "qotd_",
                       "abuse_", "lobby_", "antinuke_", "sec_", "permwatch_",
                       "phish_", "suggest_", "timeout_", "new_account_",
                       "lockdown_", "whitelisted_")


def snapshot_backup(gid):
    cfg = dict(guild_cfg(gid))
    cmds = custom_cmds(gid)
    return {"config": cfg, "customcmds": cmds}


def restore_backup(gid, snap: dict):
    """Apply a snapshot. Returns (applied_keys, skipped_keys). Unknown keys are skipped."""
    try:
        known = set(bm.DEFAULT_GUILD_CONFIG)
    except Exception:
        known = set()
    applied, skipped = [], []
    for k, v in (snap.get("config") or {}).items():
        if k in known or any(k.startswith(p) for p in BACKUPABLE_PREFIXES):
            try:
                bm.update_config(int(gid), **{k: v})
                applied.append(k)
            except Exception:
                skipped.append(k)
        else:
            skipped.append(k)
    cmds = snap.get("customcmds")
    if isinstance(cmds, dict):
        clean = {}
        for name, c in cmds.items():
            if isinstance(c, dict) and not validate_cmd_name(name):
                c = dict(c)
                c["name"] = name
                clean[name] = c
        store("customcmds.json", {})[str(gid)] = clean
        _save("customcmds.json")
        applied.append("custom_commands")
    else:
        skipped.append("custom_commands")
    audit("backup_restore", gid, keys=len(applied))
    return applied, skipped


# ---------- Discord commands ----------

async def _cmd_serverinfo(ctx) -> None:
    g = ctx.guild
    humans = [m for m in g.members if not m.bot]
    em = discord.Embed(title=f"🏠 {g.name}", color=discord.Color.blurple())
    em.add_field(name="ID", value=f"`{g.id}`", inline=True)
    em.add_field(name="Owner", value=f"<@{g.owner_id}>", inline=True)
    em.add_field(name="Created", value=f"<t:{int(g.created_at.timestamp())}:D>", inline=True)
    em.add_field(name="Members", value=f"{len(humans)} humans · {len(g.members) - len(humans)} bots",
                 inline=True)
    em.add_field(name="Channels", value=f"{len(g.text_channels)} text · {len(g.voice_channels)} voice",
                 inline=True)
    em.add_field(name="Roles", value=str(len(g.roles)), inline=True)
    if g.icon:
        em.set_thumbnail(url=g.icon.url)
    await ctx.send(embed=em)


async def _cmd_poll(ctx, args: str) -> None:
    import shlex
    try:
        parts = shlex.split(args)
    except ValueError:
        parts = []
    if len(parts) < 3:
        await ctx.send(f"Usage: `{bm.PREFIX}poll \"Question\" \"Option A\" \"Option B\" [\"C\"...]` (2–6 options)")
        return
    q, opts = parts[0][:250], [o[:80] for o in parts[1:7]]
    if len(opts) < 2:
        await ctx.send("Give at least 2 options.")
        return
    p = {"question": q, "options": opts, "votes": {}, "status": "open",
         "guild": str(ctx.guild.id), "channel": ctx.channel.id, "message": 0,
         "author": str(ctx.author.id), "created": time.time()}
    msg = await ctx.channel.send(embed=poll_embed(p), view=PollView(opts))
    p["message"] = msg.id
    _polls()[str(msg.id)] = p
    _save("polls.json")


async def _cmd_poll_end(ctx, args: str) -> None:
    if not has_perm(ctx.author, "mod"):
        await ctx.send("Mods only.")
        return
    ref = None
    if ctx.message.reference and ctx.message.reference.message_id:
        ref = str(ctx.message.reference.message_id)
    if not ref and args.strip().isdigit():
        ref = args.strip()
    p = _polls().get(ref or "")
    if not p or p.get("guild") != str(ctx.guild.id):
        await ctx.send(f"Reply to the poll with `{bm.PREFIX}poll end`, or pass its message ID.")
        return
    p["status"] = "closed"
    _save("polls.json")
    try:
        msg = await ctx.channel.fetch_message(int(ref))
        await msg.edit(embed=poll_embed(p), view=None)
    except (discord.Forbidden, discord.HTTPException, discord.NotFound):
        pass
    await ctx.send("📊 Poll closed — final results above.")


async def _cmd_remind(ctx, args: str) -> None:
    parts = (args or "").strip().split(None, 1)
    if not parts or parts[0].lower() in ("list",):
        mine = [r for r in DATA.get("reminders.json", [])
                if r.get("guild") == str(ctx.guild.id) and r.get("user_id") == str(ctx.author.id)
                and r.get("status") == "active"]
        if not mine:
            await ctx.send(f"No active reminders. Usage: `{bm.PREFIX}remind <10s+> <text>` or "
                           f"`{bm.PREFIX}remind every <dur> <text>`")
            return
        lines = [f"`{r['id']}` <t:{int(r['next_run'])}:R> — {(r.get('text') or '')[:80]}"
                 + (" 🔁" if r.get("interval_s") else "") for r in mine[:10]]
        await ctx.send("⏰ Your reminders:\n" + "\n".join(lines))
        return
    if parts[0].lower() == "cancel":
        rid = parts[1].strip() if len(parts) > 1 else ""
        hit = next((r for r in DATA.get("reminders.json", [])
                    if r.get("guild") == str(ctx.guild.id) and r.get("id") == rid
                    and r.get("status") == "active"), None)
        if hit is None or (hit.get("user_id") != str(ctx.author.id)
                           and not has_perm(ctx.author, "mod")):
            await ctx.send("Reminder not found (or not yours).")
            return
        hit["status"] = "cancelled"
        _save("reminders.json")
        await ctx.send("⏰ Reminder cancelled.")
        return
    rest = args or ""
    interval = 0
    if rest.lower().startswith("every "):
        rest = rest[6:].strip()
        dur_s, rest2 = _split_dur(rest)
        if dur_s is None or dur_s < 60:
            await ctx.send(f"Recurring minimum is 1m. Usage: `{bm.PREFIX}remind every <dur> <text>`")
            return
        interval = dur_s
        rest = rest2
    else:
        dur_s, rest2 = _split_dur(rest)
        if dur_s is None or dur_s < 10:
            await ctx.send(f"Usage: `{bm.PREFIX}remind <10s+> <text>` · `{bm.PREFIX}remind every <1m+> <text>` · "
                           f"`{bm.PREFIX}remind list|cancel <id>`")
            return
        rest = rest2
    if not rest.strip():
        await ctx.send("Reminder text can't be empty.")
        return
    r = {"id": new_id("R"), "guild": str(ctx.guild.id), "user_id": str(ctx.author.id),
         "channel_id": ctx.channel.id, "text": rest.strip()[:1500],
         "next_run": time.time() + dur_s, "interval_s": interval,
         "status": "active", "created": time.time(), "last_error": ""}
    store("reminders.json", []).append(r)
    _save("reminders.json")
    await ctx.send(f"⏰ Got it — I'll ping you <t:{int(r['next_run'])}:R>"
                   + (" (repeating)" if interval else ""))


def _split_dur(s: str):
    import re as _re
    m = _re.match(r"^\s*(\d+)\s*([smhd])\s+(.*)$", (s or ""), _re.DOTALL | _re.IGNORECASE)
    if not m:
        return None, s
    secs = int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2).lower()]
    return secs, m.group(3)


async def _cmd_suggest(ctx, text: str) -> None:
    text = (text or "").strip()
    if not text:
        await ctx.send(f"Usage: `{bm.PREFIX}suggest <your idea>` (max 1000 chars)")
        return
    left = check_cooldown(ctx.guild.id, "suggest", ctx.author.id, 60)
    if left:
        await ctx.send(f"⏳ Slow down ({left}s).")
        return
    cfg = guild_cfg(ctx.guild.id)
    ch = ctx.guild.get_channel(cfg.get("suggest_channel_id") or 0)
    sub = open_suggestion(ctx.guild.id, ctx.author.id, ctx.author.display_name, text[:1000])
    target = ch or ctx.channel
    try:
        msg = await target.send(embed=sug_embed(ctx.guild, sub), view=SuggestView())
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Couldn't post: {exc}")
        return
    sub["message"] = msg.id
    sub["channel"] = target.id
    _save("suggestions.json")
    if target.id != ctx.channel.id:
        await ctx.send(f"💡 Posted in {target.mention}!")


async def _cmd_temprole(ctx, args: str) -> None:
    import shlex
    try:
        parts = shlex.split(args or "")
    except ValueError:
        parts = []
    if not parts or parts[0].lower() == "list":
        act = [t for t in DATA.get("temproles.json", [])
               if t.get("guild") == str(ctx.guild.id) and t.get("status") == "active"]
        if not act:
            await ctx.send("No active temp roles.")
            return
        lines = []
        for t in sorted(act, key=lambda x: x.get("expires_at", 0))[:15]:
            m = ctx.guild.get_member(int(t.get("user_id", 0)))
            r = ctx.guild.get_role(int(t.get("role_id", 0)))
            lines.append(f"<@{t['user_id']}> → **{(r.name if r else '?')}** "
                         f"ends <t:{int(t.get('expires_at', 0))}:R> (`{t['id']}`)")
            _ = m
        await ctx.send("⏳ Active temp roles:\n" + "\n".join(lines))
        return
    if not has_perm(ctx.author, "admin"):
        await ctx.send("Admins only.")
        return
    if parts[0].lower() == "end" and len(parts) >= 3:
        hit = next((t for t in DATA.get("temproles.json", [])
                    if t.get("guild") == str(ctx.guild.id) and t.get("status") == "active"
                    and str(t.get("user_id")) == parts[1].strip("<>@!").split()[0]
                    and str(t.get("role_id")) == parts[2].strip("<>@!&")), None)
        if hit is None:
            await ctx.send("No matching active temp role.")
            return
        hit["expires_at"] = time.time() - 1
        _save("temproles.json")
        await ctx.send("⏳ Expiring now — the scheduler will remove it within a minute.")
        return
    if len(parts) < 3:
        await ctx.send(f"Usage: `{bm.PREFIX}temprole @user @role <dur> [reason]` · "
                       f"`{bm.PREFIX}temprole list` · `{bm.PREFIX}temprole end @user @role`")
        return
    try:
        member = ctx.guild.get_member(int(parts[0].strip("<>@!")))
        role = ctx.guild.get_role(int(parts[1].strip("<>@!&")))
    except ValueError:
        member, role = None, None
    dur_s = bm.parse_duration(parts[2]) if len(parts) > 2 else None
    reason = " ".join(parts[3:])[:200] or "temp role"
    err = _validate_role_grant(ctx.guild, ctx.author, member, role)
    if err or dur_s is None:
        await ctx.send(err or f"Bad duration. Use like `10m`, `2h`, `7d` (1m–30d).")
        return
    try:
        await member.add_roles(role, reason=f"temp role by {ctx.author}: {reason}")
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Failed: {exc}")
        return
    tr = {"id": new_id("T"), "guild": str(ctx.guild.id), "user_id": str(member.id),
          "role_id": str(role.id), "reason": reason, "by_id": str(ctx.author.id),
          "expires_at": time.time() + dur_s, "status": "active",
          "created": time.time(), "result": ""}
    store("temproles.json", []).append(tr)
    _save("temproles.json")
    audit("temprole_assign", ctx.guild.id, user=str(member.id), role=str(role.id))
    await ctx.send(f"⏳ **{member.display_name}** has **{role.name}** until <t:{int(tr['expires_at'])}:F>.")


def _validate_role_grant(guild, author, member, role):
    """Shared hierarchy/permission validation. Returns error string or ''."""
    me = guild.me
    if member is None:
        return "Member not found."
    if role is None:
        return "Role not found — mention it."
    if role.is_default():
        return "Can't assign @everyone."
    if getattr(role, "managed", False):
        return "That role is managed by an integration."
    if me is None:
        return "I can't see myself in this server."
    if role >= me.top_role:
        return f"My role must sit above `{role.name}`."
    try:
        if not me.guild_permissions.manage_roles:
            return "I need Manage Roles."
    except Exception:
        return "I need Manage Roles."
    if author.id not in bm.OWNER_IDS:
        try:
            atop = author.top_role
            if role >= atop and not author.guild_permissions.administrator:
                return f"`{role.name}` outranks you."
        except Exception:
            pass
    return ""


async def _cmd_events(ctx, args: str) -> None:
    now = time.time()
    upcoming = sorted(
        [e for e in ev_list(ctx.guild.id)
         if e.get("status") == "active" and e.get("starts_at", 0) > now],
        key=lambda e: e.get("starts_at", 0))[:10]
    if not upcoming:
        await ctx.send("No upcoming events. Check the dashboard to see past ones.")
        return
    lines = []
    for e in upcoming:
        going = sum(1 for v in (e.get("rsvps", {}) or {}).values() if v == "going")
        lines.append(f"**{e.get('title', '?')[:80]}** — <t:{int(e.get('starts_at', 0))}:F> · "
                     f"{going} going (`{e['id']}`)")
    await ctx.send("📅 Upcoming events:\n" + "\n".join(lines))


async def _cmd_cases(ctx, args: str) -> None:
    if not has_perm(ctx.author, "mod"):
        await ctx.send("Mods only.")
        return
    parts = (args or "").strip().split(None, 1)
    if not parts:
        await ctx.send(f"Usage: `{bm.PREFIX}cases @user` or `{bm.PREFIX}cases CASE-XXXXXX`")
        return
    q = parts[0].strip("<>@!")
    if q.upper().startswith("CASE-") or len(q) == 6:
        c = find_case(ctx.guild.id, q)
        if c is None:
            await ctx.send("Case not found.")
            return
        await ctx.send(embed=case_embed(ctx.guild, c))
        return
    try:
        member = ctx.guild.get_member(int(q)) or await ctx.guild.fetch_member(int(q))
    except (ValueError, discord.HTTPException, discord.NotFound):
        member = None
    if member is None:
        await ctx.send("Member not found.")
        return
    hist = sorted(user_cases(ctx.guild.id, member.id), key=lambda c: c.get("created", 0),
                  reverse=True)[:10]
    if not hist:
        await ctx.send(f"✅ **{member.display_name}** has a clean record.")
        return
    lines = [f"`{c['id']}` **{c.get('action', '?')}** — {c.get('reason', '—')[:80]} "
             f"({c.get('status', 'open')})" for c in hist]
    await ctx.send(f"📁 **{member.display_name}** — {len(user_cases(ctx.guild.id, member.id))} case(s):\n"
                   + "\n".join(lines))


def case_embed(guild, c: dict):
    m = guild.get_member(int(c.get("target_id", 0))) if str(c.get("target_id", "")).isdigit() else None
    em = discord.Embed(title=f"📁 {c['id']} — {c.get('action', '?').title()}",
                       description=(c.get("reason") or "—")[:1000],
                       color=discord.Color.orange())
    em.add_field(name="User", value=m.mention if m else f"`{c.get('target_id')}`", inline=True)
    em.add_field(name="Moderator", value=f"<@{c.get('mod_id')}>", inline=True)
    em.add_field(name="Status", value=c.get("status", "open").title(), inline=True)
    em.add_field(name="Date", value=f"<t:{int(c.get('created', 0))}:F>", inline=True)
    if c.get("evidence"):
        em.add_field(name="Evidence", value=c["evidence"][:500], inline=False)
    if c.get("appeal"):
        em.add_field(name="Appeal", value=c["appeal"][:500], inline=False)
    notes = c.get("notes", [])[-3:]
    if notes:
        em.add_field(name="Notes", value="\n".join(f"• {n[:200]}" for n in notes), inline=False)
    em.set_footer(text="Wigglesworth moderation")
    return em


async def _cmd_apply(ctx, args: str) -> None:
    forms = [f for f in app_forms(ctx.guild.id) if f.get("open", True)]
    if not forms:
        await ctx.send("No open applications right now.")
        return
    want = (args or "").strip().lower()
    if want:
        form = next((f for f in forms if f["id"].lower() == want or f.get("name", "").lower() == want), None)
        if form is None:
            await ctx.send("Form not found. Run `.apply` to see open forms.")
            return
        forms = [form]
    try:
        await ctx.send("📝 Pick a form to apply:", view=ApplyListView(forms),
                       ephemeral=False)
    except (discord.Forbidden, discord.HTTPException) as exc:
        await ctx.send(f"❌ Failed: {exc}")


def ev_list(gid):
    return store("eventsched.json", {}).setdefault(str(gid), [])


def setup(bot_module) -> None:
    """Register commands + views + scheduler. Idempotent; call from on_ready."""
    global bm, _bot
    bm = bot_module
    _bot = bm.bot
    if getattr(_bot, "_community_ready", False):
        return
    _bot._community_ready = True
    for name in ("customcmds.json", "cases.json", "suggestions.json",
                 "announcements.json", "temproles.json", "reminders.json",
                 "applications.json", "eventsched.json", "polls.json",
                 "backups.json"):
        FILES[name] = bm.DATA_DIR / name
        DATA[name] = _load(name)
    for cmd in list(_bot.commands):
        RESERVED.add(cmd.name.lower())
        for a in getattr(cmd, "aliases", []):
            RESERVED.add(a.lower())
    register_views()
    try:
        import asyncio as _aio
        _aio.get_running_loop()
    except RuntimeError:
        pass  # no running loop yet (tests); on_ready path starts it
    else:
        if not sched_loop.is_running():
            sched_loop.start()

    b = _bot
    b.command(name="serverinfo")(_cmd_serverinfo)

    async def _poll_router(ctx, *, args: str = ""):
        if (args or "").strip().lower() == "end" or (args or "").strip().lower().startswith("end "):
            rest = (args or "").strip()[3:].strip()
            await _cmd_poll_end(ctx, rest)
        else:
            await _cmd_poll(ctx, args)
    _poll_router.__name__ = "cmd_poll"
    b.command(name="poll")(_poll_router)

    async def _remind_router(ctx, *, args: str = ""):
        await _cmd_remind(ctx, args)
    _remind_router.__name__ = "cmd_remind"
    b.command(name="remind")(_remind_router)

    async def _suggest_router(ctx, *, text: str = ""):
        await _cmd_suggest(ctx, text)
    _suggest_router.__name__ = "cmd_suggest"
    b.command(name="suggest")(_suggest_router)

    async def _temprole_router(ctx, *, args: str = ""):
        await _cmd_temprole(ctx, args)
    _temprole_router.__name__ = "cmd_temprole"
    b.command(name="temprole")(_temprole_router)

    async def _events_router(ctx, *, args: str = ""):
        await _cmd_events(ctx, args)
    _events_router.__name__ = "cmd_events"
    b.command(name="events")(_events_router)

    async def _cases_router(ctx, *, args: str = ""):
        await _cmd_cases(ctx, args)
    _cases_router.__name__ = "cmd_cases"
    b.command(name="cases")(_cases_router)

    async def _apply_router(ctx, *, args: str = ""):
        await _cmd_apply(ctx, args)
    _apply_router.__name__ = "cmd_apply"
    b.command(name="apply")(_apply_router)


# ---------- 9. applications ----------

def app_forms(gid):
    return store("applications.json", {}).setdefault(str(gid), {}).setdefault("forms", [])


def app_subs(gid):
    return store("applications.json", {}).setdefault(str(gid), {}).setdefault("subs", {})


def app_embed(guild, form: dict, sub: dict):
    lines = [f"**{f.get('label', '?')}:** {(sub.get('answers', {}) or {}).get(str(i), '—')[:500]}"
             for i, f in enumerate(form.get("fields", []))]
    em = discord.Embed(title=f"📝 {form.get('name', 'Application')} — {sub['id']}",
                       description="\n".join(lines)[:4000] or "(no answers)",
                       color=discord.Color.blurple())
    em.add_field(name="Applicant", value=f"<@{sub.get('user_id')}>", inline=True)
    em.add_field(name="Status", value=sub.get("status", "pending").title(), inline=True)
    if sub.get("reviewer_note"):
        em.add_field(name="Reviewer note", value=sub["reviewer_note"][:1000], inline=False)
    em.set_footer(text="Wigglesworth applications")
    return em


class ApplyModal(discord.ui.Modal):
    def __init__(self, form: dict) -> None:
        super().__init__(title=f"Apply: {form.get('name', '')[:40]}", custom_id=f"capply:{form['id']}")
        self.form_id = form["id"]
        for i, f in enumerate(form.get("fields", [])[:5]):
            self.add_item(discord.ui.TextInput(
                label=f.get("label", f"Q{i + 1}")[:45],
                required=bool(f.get("required", True)),
                max_length=min(int(f.get("maxlen", 500) or 500), 1000),
                style=discord.TextStyle.paragraph,
                custom_id=f"f{i}"))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        form = next((x for x in app_forms(guild.id) if x["id"] == self.form_id and x.get("open", True)), None)
        if form is None:
            try:
                await interaction.response.send_message("That form is closed.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        answers = {str(i): c.value[:1000] for i, c in enumerate(self.children)}
        subs = app_subs(guild.id)
        sid = new_id("A")
        subs[sid] = {"id": sid, "form_id": form["id"], "user_id": str(interaction.user.id),
                     "user_name": interaction.user.display_name[:100], "answers": answers,
                     "status": "pending", "reviewer_note": "", "message": 0,
                     "created": time.time(), "decided_at": 0}
        _save("applications.json")
        ch = guild.get_channel(form.get("review_channel") or 0)
        if ch is not None:
            try:
                msg = await ch.send(embed=app_embed(guild, form, subs[sid]), view=ReviewView(sid))
                subs[sid]["message"] = msg.id
                _save("applications.json")
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            await interaction.response.send_message("✅ Application submitted! Staff will review it soon.",
                                                    ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass


class ApplyListView(discord.ui.View):
    def __init__(self, forms) -> None:
        super().__init__(timeout=None)
        for f in forms[:10]:
            self.add_item(_ApplyButton(f["id"], (f.get("name", "Apply"))[:80]))

    @staticmethod
    def for_guild(guild_id) -> "ApplyListView | None":
        forms = [f for f in app_forms(guild_id) if f.get("open", True)]
        return ApplyListView(forms) if forms else None


class _ApplyButton(discord.ui.Button):
    def __init__(self, form_id: str, label: str):
        super().__init__(label=label, style=discord.ButtonStyle.primary,
                         custom_id=f"capplybtn:{form_id}")
        self.form_id = form_id

    async def callback(self, interaction: discord.Interaction) -> None:
        form = next((x for x in app_forms(interaction.guild.id)
                     if x["id"] == self.form_id and x.get("open", True)), None)
        if form is None:
            try:
                await interaction.response.send_message("That form is closed.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        try:
            await interaction.response.send_modal(ApplyModal(form))
        except (discord.Forbidden, discord.HTTPException):
            pass


class ReviewView(discord.ui.View):
    def __init__(self, sub_id: str) -> None:
        super().__init__(timeout=None)
        for label, style, act in (("Approve", discord.ButtonStyle.success, "approve"),
                                  ("More info", discord.ButtonStyle.secondary, "info"),
                                  ("Reject", discord.ButtonStyle.danger, "reject")):
            self.add_item(_ReviewButton(sub_id, label, style, act))


class _ReviewButton(discord.ui.Button):
    def __init__(self, sub_id: str, label: str, style, act: str):
        super().__init__(label=label, style=style, custom_id=f"capp:{sub_id}:{act}")
        self.sub_id, self.act = sub_id, act

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if not has_perm(interaction.user, "admin"):
            try:
                await interaction.response.send_message("Staff only.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        subs = app_subs(guild.id)
        sub = subs.get(self.sub_id)
        if sub is None or sub.get("status") not in ("pending", "info"):
            try:
                await interaction.response.send_message("Already decided.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        form = next((x for x in app_forms(guild.id) if x["id"] == sub.get("form_id")), None)
        status = {"approve": "approved", "reject": "rejected", "info": "info"}[self.act]
        sub["status"] = status
        sub["decided_at"] = time.time()
        sub["decided_by"] = str(interaction.user.id)
        _save("applications.json")
        if form is not None:
            try:
                await interaction.message.edit(embed=app_embed(guild, form, sub), view=None)
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                pass
        member = guild.get_member(int(sub["user_id"]))
        if member is not None and self.act in ("approve", "reject"):
            try:
                await member.send(f"📝 Your application **{(form or {}).get('name', '')}** was **{status}**.")
            except (discord.Forbidden, discord.HTTPException):
                pass
        try:
            await interaction.response.send_message(f"Marked **{status}**.", ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass


# ---------- 10. events & RSVP ----------

def ev_embed(ev: dict):
    going = sum(1 for v in (ev.get("rsvps", {}) or {}).values() if v == "going")
    maybe = sum(1 for v in (ev.get("rsvps", {}) or {}).values() if v == "maybe")
    limit = int(ev.get("limit", 0) or 0)
    cap = f" / {limit}" if limit else ""
    wait = sum(1 for v in (ev.get("rsvps", {}) or {}).values() if v == "waitlist")
    em = discord.Embed(title=f"📅 {ev.get('title', 'Event')[:250]}",
                       description=(ev.get("desc", "") or "")[:2000] or "(no description)",
                       color=discord.Color.blurple())
    em.add_field(name="Starts", value=f"<t:{int(ev.get('starts_at', 0))}:F> (<t:{int(ev.get('starts_at', 0))}:R>)",
                 inline=False)
    em.add_field(name="Going", value=f"{going}{cap}", inline=True)
    em.add_field(name="Maybe", value=str(maybe), inline=True)
    if limit:
        em.add_field(name="Waitlist", value=str(wait), inline=True)
    em.set_footer(text="Wigglesworth events · tap to RSVP (one spot per person)")
    return em


class EventView(discord.ui.View):
    def __init__(self, ev_id: str) -> None:
        super().__init__(timeout=None)
        for label, style, choice in (("Going ✅", discord.ButtonStyle.success, "going"),
                                     ("Maybe 🤔", discord.ButtonStyle.secondary, "maybe"),
                                     ("Can't go ❌", discord.ButtonStyle.danger, "decline")):
            self.add_item(_RSVPButton(ev_id, label, style, choice))


class _RSVPButton(discord.ui.Button):
    def __init__(self, ev_id: str, label: str, style, choice: str):
        super().__init__(label=label, style=style, custom_id=f"evrsvp:{ev_id}:{choice}")
        self.ev_id, self.choice = ev_id, choice

    async def callback(self, interaction: discord.Interaction) -> None:
        ev = next((e for e in ev_list(interaction.guild.id)
                   if e["id"] == self.ev_id and e.get("status") == "active"), None)
        if ev is None:
            try:
                await interaction.response.send_message("Event is over or cancelled.", ephemeral=True)
            except (discord.Forbidden, discord.HTTPException):
                pass
            return
        uid = str(interaction.user.id)
        rsvps = ev.setdefault("rsvps", {})
        if self.choice == "decline":
            rsvps.pop(uid, None)
            msg = "RSVP removed."
        else:
            going = sum(1 for u, v in rsvps.items() if v == "going" and u != uid)
            limit = int(ev.get("limit", 0) or 0)
            if self.choice == "going" and limit and going >= limit:
                rsvps[uid] = "waitlist"
                msg = "Event is full — you're on the waitlist."
            else:
                rsvps[uid] = self.choice
                msg = f"Marked **{self.choice}**!"
        _save("eventsched.json")
        try:
            await interaction.message.edit(embed=ev_embed(ev))
        except (discord.Forbidden, discord.HTTPException, discord.NotFound):
            pass
        try:
            await interaction.response.send_message(msg, ephemeral=True)
        except (discord.Forbidden, discord.HTTPException):
            pass
