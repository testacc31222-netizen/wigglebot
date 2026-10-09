"""Wigglesworth security expansion: anti-nuke, permission monitor, anti-phishing,
incident management, diagnostics, emergency response.

Uses existing infrastructure only: JSON file stores (same pattern as xp/afk),
track() analytics events (kind="sec"), send_log() alerts, get/update_config().
No new dependencies. No Discord commands added.
"""
from __future__ import annotations

import json
import re
import time
import uuid
from collections import defaultdict, deque
from urllib.parse import urlparse

import discord

bm = None  # bot module ref, set by setup()

INCIDENTS_FILE = None          # set in setup() from bm.DATA_DIR
INCIDENTS_CAP_PER_GUILD = 300
NOTES_CAP = 50

incidents_data: dict = {}

_windows: dict = defaultdict(deque)   # (gid, action) -> deque[timestamps]
_last_alert: dict = {}                # (gid, action) -> ts (dedupe cooldown)
_rep_cache: dict = {}                 # domain -> (ts, verdict, detail)

# ---------------------------------------------------------------- stores

def setup(bot_module) -> None:
    """Called once from bot.on_ready. Registers listeners, inits stores."""
    global bm, INCIDENTS_FILE, incidents_data
    bm = bot_module
    INCIDENTS_FILE = bm.DATA_DIR / "incidents.json"
    incidents_data = _load_incidents()
    bot = bm.bot
    if bot is None or getattr(bot, "_sec_ready", False):
        return
    bot._sec_ready = True
    bot.add_listener(_on_channel_delete, "on_guild_channel_delete")
    bot.add_listener(_on_channel_create, "on_guild_channel_create")
    bot.add_listener(_on_role_delete, "on_guild_role_delete")
    bot.add_listener(_on_role_create, "on_guild_role_create")
    bot.add_listener(_on_role_update, "on_guild_role_update")
    bot.add_listener(_on_member_ban_ev, "on_member_ban")
    bot.add_listener(_on_member_unban_ev, "on_member_unban")
    bot.add_listener(_on_member_remove_ev, "on_member_remove")
    bot.add_listener(_on_webhooks_update, "on_webhooks_update")
    bot.add_listener(_on_guild_update, "on_guild_update")
    bot.add_listener(_on_member_update_ev, "on_member_update")


def _load_incidents() -> dict:
    try:
        if INCIDENTS_FILE and INCIDENTS_FILE.exists():
            d = json.loads(INCIDENTS_FILE.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_incidents() -> None:
    if INCIDENTS_FILE is None:
        return  # setup() hasn't run (e.g. offline import); keep in memory
    try:
        by_guild: dict = defaultdict(list)
        for inc in incidents_data.values():
            by_guild[inc.get("guild")].append(inc["id"])
        for gid, ids in by_guild.items():
            if len(ids) > INCIDENTS_CAP_PER_GUILD:
                ordered = sorted(ids, key=lambda i: incidents_data[i].get("created", 0))
                for drop in ordered[:-INCIDENTS_CAP_PER_GUILD]:
                    incidents_data.pop(drop, None)
        INCIDENTS_FILE.write_text(json.dumps(incidents_data, indent=2), encoding="utf-8")
    except OSError:
        pass


def track_sec(guild_id, event: str, **kw) -> None:
    try:
        bm.track(guild_id, "sec", action=event, **kw)
    except Exception:
        pass


async def sec_alert(guild, text: str, embed=None) -> bool:
    """Security alerts prefer sec_alert_channel, fall back to send_log routing.
    Returns True if the dedicated channel was used."""
    try:
        cfg = bm.get_config(guild.id)
        cid = cfg.get("sec_alert_channel")
        if cid:
            ch = guild.get_channel(int(cid))
            if ch is not None:
                try:
                    if embed is not None:
                        await ch.send(text, embed=embed)
                    else:
                        await ch.send(text)
                    return True
                except Exception:
                    pass
    except Exception:
        pass
    try:
        await bm.send_log(guild, text, embed=embed)
    except Exception:
        pass
    return False


def _new_id() -> str:
    for _ in range(10):
        cid = "INC-" + uuid.uuid4().hex[:6].upper()
        if cid not in incidents_data:
            return cid
    return "INC-" + uuid.uuid4().hex[:12].upper()


# ---------------------------------------------------------------- incidents

def open_incident(guild_id, itype: str, severity: str, summary: str,
                  actor=None, target=None, evidence=None) -> dict:
    inc = {
        "id": _new_id(), "guild": str(guild_id), "type": itype,
        "severity": severity if severity in ("critical", "warning", "info") else "info",
        "created": time.time(), "summary": summary[:300],
        "actor": actor, "target": target,
        "evidence": list(evidence or [])[:20],
        "timeline": [{"t": time.time(), "text": "Incident opened: " + summary[:200]}],
        "attempted": [], "completed": [], "failed": [],
        "status": "open", "notes": [], "resolved_at": None,
    }
    incidents_data[inc["id"]] = inc
    _save_incidents()
    track_sec(guild_id, "incident", iid=inc["id"], itype=itype, severity=inc["severity"])
    return inc


def get_incident(iid: str, guild_id) -> dict | None:
    inc = incidents_data.get((iid or "").strip().upper())
    if inc and inc.get("guild") == str(guild_id):
        return inc
    return None


def incident_timeline(inc: dict, text: str) -> None:
    inc.setdefault("timeline", []).append({"t": time.time(), "text": text[:300]})
    inc["timeline"] = inc["timeline"][-100:]
    _save_incidents()


def incident_note(iid: str, guild_id, text: str) -> bool:
    inc = get_incident(iid, guild_id)
    text = (text or "").strip()[:500]
    if not inc or not text:
        return False
    inc.setdefault("notes", []).append({"t": time.time(), "text": text})
    inc["notes"] = inc["notes"][-NOTES_CAP:]
    incident_timeline(inc, "Staff note added.")
    return True


def incident_status(iid: str, guild_id, status: str) -> bool:
    inc = get_incident(iid, guild_id)
    if not inc or status not in ("open", "reviewed", "resolved"):
        return False
    inc["status"] = status
    if status == "resolved":
        inc["resolved_at"] = time.time()
    else:
        inc["resolved_at"] = None
    incident_timeline(inc, f"Status → {status}.")
    return True


def guild_incidents(guild_id, status=None, limit=60):
    out = [i for i in incidents_data.values() if i.get("guild") == str(guild_id)]
    if status:
        out = [i for i in out if i.get("status") == status]
    out.sort(key=lambda i: i.get("created", 0), reverse=True)
    return out[:limit]


def record_attempt(inc: dict, action: str, ok: bool, detail: str = "") -> None:
    inc.setdefault("attempted", []).append(action)
    (inc.setdefault("completed", []) if ok else inc.setdefault("failed", [])).append(
        action + (f" ({detail})" if detail else ""))
    incident_timeline(inc, f"{'✓' if ok else '✗'} {action}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------- audit + trust

def _audit_action(name: str):
    try:
        return getattr(discord.AuditLogAction, name, None)
    except Exception:
        return None


async def audit_actor(guild, action_name: str, target_id=None, window_s: int = 45):
    """Best-effort actor attribution. Returns (actor_dict|None, note)."""
    act = _audit_action(action_name)
    if act is None:
        return None, "audit action unsupported by this discord.py"
    try:
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None, "no audit-log permission"
        async for entry in guild.audit_logs(limit=8, action=act):
            try:
                age = (discord.utils.utcnow() - entry.created_at).total_seconds()
            except Exception:
                age = 0
            if age > window_s:
                continue
            if target_id and getattr(entry.target, "id", None) != target_id:
                continue
            u = entry.user
            if u is None:
                continue
            return {"id": str(u.id), "name": str(u)}, f"audit match ({age:.0f}s old)"
    except (discord.Forbidden, discord.HTTPException) as exc:
        return None, f"audit log unavailable ({exc})"
    except Exception as exc:
        return None, f"audit lookup failed ({exc})"
    return None, "no matching audit entry"


def is_exempt(guild, cfg: dict, user_id) -> tuple[bool, str]:
    """Trusted actors skip automated response (but events are still recorded)."""
    try:
        if str(user_id) == str(guild.owner_id):
            return True, "server owner"
    except Exception:
        pass
    if str(user_id) in {str(x) for x in (cfg.get("whitelisted_user_ids", []) or [])}:
        return True, "whitelisted user"
    try:
        m = guild.get_member(int(user_id))
        if m is not None:
            allowed = {str(x) for x in (cfg.get("whitelisted_role_ids", []) or [])}
            if any(str(r.id) in allowed for r in m.roles):
                return True, "whitelisted role"
    except Exception:
        pass
    return False, ""


# ---------------------------------------------------------------- anti-nuke

NUKE_ACTIONS = {
    "chandel": ("Channel deleted", "antinuke_chandel"),
    "chancr": ("Channel created", "antinuke_chancr"),
    "roledel": ("Role deleted", "antinuke_roledel"),
    "rolecr": ("Role created", "antinuke_rolecr"),
    "ban": ("Member banned", "antinuke_ban"),
    "kick": ("Member kicked", "antinuke_kick"),
    "webhook": ("Webhook change", "antinuke_webhook"),
    "permchange": ("Permission change", "antinuke_perm"),
}

CRITICAL_NUKE = {"chandel", "roledel", "ban"}


async def burst(guild, action: str, target_desc: str, audit_action: str | None = None,
                target_id=None) -> None:
    """Record one destructive event; open an incident if it bursts past threshold."""
    gid = guild.id
    try:
        cfg = bm.get_config(gid)
    except Exception:
        return
    if not cfg.get("antinuke_enabled"):
        return
    label, cfgkey = NUKE_ACTIONS[action]
    try:
        window = max(10, int(cfg.get("antinuke_window", 60)))
    except (ValueError, TypeError):
        window = 60
    try:
        threshold = max(2, int(cfg.get(cfgkey, 3)))
    except (ValueError, TypeError):
        threshold = 3
    now = time.time()
    dq = _windows[(str(gid), action)]
    dq.append(now)
    while dq and now - dq[0] > window:
        dq.popleft()
    track_sec(gid, "nuke", action=action, target=target_desc[:120])
    if len(dq) < threshold:
        return
    if now - _last_alert.get((str(gid), action), 0) < window:
        return  # one incident per action per window
    _last_alert[(str(gid), action)] = now

    actor, note = (await audit_actor(guild, audit_action, target_id)) if audit_action else (None, "no audit lookup")
    actor_id = actor["id"] if actor else None
    # never act on our own actions
    try:
        if actor_id and bm.bot and actor_id == str(bm.bot.user.id):
            return
    except Exception:
        pass
    sev = "critical" if action in CRITICAL_NUKE else "warning"
    ev = [f"{len(dq)}× {label.lower()} within {window}s (threshold {threshold}): {target_desc[:160]}",
          f"Actor: {actor['name']} ({actor_id}) — {note}" if actor else f"Actor unknown — {note}"]
    inc = open_incident(gid, "nuke", sev,
                        f"Possible nuke: {len(dq)}× {label.lower()} in {window}s",
                        actor=actor, target=target_desc[:160], evidence=ev)
    # alert staff (security channel preferred, log channel w/ fallbacks)
    try:
        em = bm.E(f"🚨 Possible nuke: {label}",
                  f"{len(dq)}× in the last {window}s (threshold {threshold}).\n"
                  f"Target: {target_desc[:160]}\n"
                  f"Suspect: {(actor['name'] + ' (`' + actor_id + '`)') if actor else 'unknown'} — {note}\n"
                  f"Incident: `{inc['id']}`", kind="bad")
        await sec_alert(guild, f"🚨 {label} burst — incident `{inc['id']}`", embed=em)
    except Exception as exc:
        record_attempt(inc, "alert staff", False, str(exc)[:120])
    else:
        record_attempt(inc, "alert staff", True)
    # automatic response: quarantine only, never auto-ban
    if actor_id:
        exempt, why = is_exempt(guild, cfg, actor_id)
        if exempt:
            incident_timeline(inc, f"Trusted actor ({why}) — no automated response.")
        elif cfg.get("sec_auto_strip") and int(cfg.get("antinuke_sensitivity", 2) or 2) >= 2:
            removed, failed = await quarantine(guild, actor_id, "Anti-nuke quarantine")
            if removed:
                record_attempt(inc, "quarantine suspect", True, f"removed {len(removed)} role(s)")
            else:
                record_attempt(inc, "quarantine suspect", False, failed or "nothing removable")
        else:
            incident_timeline(inc, "Auto-quarantine off — manual review required.")
    else:
        incident_timeline(inc, "No confirmed actor — manual review required.")


async def quarantine(guild, user_id, reason: str):
    """Strip removable roles from a suspect (reversible, recorded). Returns (removed_ids, fail_note)."""
    removed, fail = [], ""
    try:
        member = guild.get_member(int(user_id))
        me = guild.me
        if member is None:
            return removed, "member not found"
        if member.id == guild.owner_id:
            return removed, "refusing: server owner"
        if bm.bot and member.id == bm.bot.user.id:
            return removed, "refusing: self"
        if member.top_role >= me.top_role:
            return removed, "hierarchy: suspect outranks bot"
        roles = [r for r in member.roles if not r.is_default() and r < me.top_role]
        if not roles:
            return removed, "no removable roles"
        await member.remove_roles(*roles, reason=reason)
        removed = [str(r.id) for r in roles]
    except discord.Forbidden:
        fail = "missing Manage Roles permission"
    except discord.HTTPException as exc:
        fail = f"Discord error: {exc}"[:120]
    except Exception as exc:
        fail = f"unexpected: {exc}"[:120]
    return removed, fail


async def _on_channel_delete(channel) -> None:
    try:
        await burst(channel.guild, "chandel", f"#{channel.name}", "channel_delete", channel.id)
    except Exception:
        pass


async def _on_channel_create(channel) -> None:
    try:
        await burst(channel.guild, "chancr", f"#{channel.name}", "channel_create", channel.id)
    except Exception:
        pass


async def _on_role_delete(role) -> None:
    try:
        await burst(role.guild, "roledel", f"@{role.name}", "role_delete", role.id)
    except Exception:
        pass


async def _on_role_create(role) -> None:
    try:
        await burst(role.guild, "rolecr", f"@{role.name}", "role_create", role.id)
    except Exception:
        pass


async def _on_member_ban_ev(guild, user) -> None:
    try:
        await burst(guild, "ban", f"{user} (`{getattr(user, 'id', '?')}`)", "ban",
                    getattr(user, "id", None))
    except Exception:
        pass


async def _on_member_unban_ev(guild, user) -> None:
    try:
        track_sec(guild.id, "unban", target=f"{user} (`{getattr(user, 'id', '?')}`)")
    except Exception:
        pass


async def _on_member_remove_ev(member) -> None:
    # kicks surface as removes; confirm via audit log (only when watching)
    try:
        cfg = bm.get_config(member.guild.id)
        if not cfg.get("antinuke_enabled"):
            return
        actor, note = await audit_actor(member.guild, "kick", member.id)
        if actor is None:
            return  # plain leave, nothing to do
        try:
            if actor["id"] == str(bm.bot.user.id):
                return
        except Exception:
            pass
        await burst(member.guild, "kick", f"{member} (`{member.id}`) by {actor['name']}",
                    None, None)
        # attribute the burst to the kicker for the latest incident
        for inc in guild_incidents(member.guild.id, status="open", limit=5):
            if inc.get("type") == "nuke" and not inc.get("actor"):
                inc["actor"] = actor
                inc["evidence"].append(f"Kicker from audit: {actor['name']} — {note}")
                incident_timeline(inc, f"Attributed to {actor['name']} ({note}).")
                break
    except Exception:
        pass


async def _on_webhooks_update(channel) -> None:
    try:
        guild = channel.guild
        cfg = bm.get_config(guild.id)
        if not (cfg.get("antinuke_enabled") or cfg.get("permwatch_enabled")):
            return
        actor, note = await audit_actor(guild, "webhook_create")
        kind = "create"
        if actor is None:
            actor, note2 = await audit_actor(guild, "webhook_delete")
            if actor is not None:
                kind, note = "delete", note2
        desc = f"webhook {kind} in #{channel.name}"
        if actor:
            desc += f" by {actor['name']}"
        track_sec(guild.id, "webhook", kind=kind, channel=getattr(channel, "name", "?"),
                  actor=(actor or {}).get("id"))
        if cfg.get("antinuke_enabled"):
            await burst(guild, "webhook", desc, None, None)
        elif cfg.get("permwatch_enabled"):
            open_incident(guild.id, "perm", "info", f"Webhook {kind} in #{channel.name}",
                          actor=actor, target=f"#{channel.name}",
                          evidence=[f"{desc} — {note}"])
    except Exception:
        pass


# ---------------------------------------------------------------- permission monitor

DANGEROUS_PERMS = ("administrator", "manage_guild", "manage_roles", "manage_channels",
                   "manage_webhooks", "manage_messages", "mention_everyone",
                   "kick_members", "ban_members")


def perm_diff(before, after):
    """Return [(perm, old_bool, new_bool)] for dangerous perms that changed."""
    out = []
    try:
        bp, ap = before.permissions, after.permissions
    except Exception:
        return out
    for p in DANGEROUS_PERMS:
        try:
            b, a = bool(getattr(bp, p, False)), bool(getattr(ap, p, False))
        except Exception:
            continue
        if b != a:
            out.append((p, b, a))
    return out


def _perm_suspicious(changes, admin_only: bool) -> tuple[bool, str]:
    grants = [p for p, b, a in changes if a and not b]
    if not grants:
        return False, "only removals"
    if "administrator" in grants:
        return True, "administrator granted"
    if admin_only and not any(p in ("administrator", "manage_guild", "manage_roles") for p in grants):
        return False, "non-admin change"
    return True, "sensitive grant: " + ", ".join(grants)


async def _on_role_update(before, after) -> None:
    try:
        guild = after.guild
        cfg = bm.get_config(guild.id)
        changes = perm_diff(before, after)
        if changes:
            track_sec(guild.id, "perm", target=f"role:{after.name}",
                      changes=[f"{p} {b}->{a}" for p, b, a in changes])
        if cfg.get("permwatch_enabled") and changes:
            sus, why = _perm_suspicious(changes, bool(cfg.get("permwatch_admin_only", True)))
            if sus:
                actor, note = await audit_actor(guild, "role_update", after.id)
                inc = open_incident(
                    guild.id, "perm", "warning" if "administrator" not in why else "critical",
                    f"Role @{after.name}: {why}",
                    actor=actor, target=f"role:{after.name}",
                    evidence=[f"{p}: {b} → {a}" for p, b, a in changes] +
                             [f"Actor: {(actor or {}).get('name', 'unknown')} — {note}"])
                try:
                    em = bm.E("⚠️ Sensitive permission change",
                              f"Role **{after.name}**: {why}\n"
                              f"By: {(actor or {}).get('name', 'unknown')} — {note}\n"
                              f"Incident: `{inc['id']}`", kind="bad")
                    await sec_alert(guild, f"⚠️ perm change @{after.name} — `{inc['id']}`", embed=em)
                    record_attempt(inc, "alert staff", True)
                except Exception as exc:
                    record_attempt(inc, "alert staff", False, str(exc)[:120])
        if cfg.get("antinuke_enabled") and changes:
            await burst(guild, "permchange", f"@{after.name}: " +
                        ", ".join(f"{p} {b}->{a}" for p, b, a in changes), None, None)
    except Exception:
        pass


async def _on_guild_update(before, after) -> None:
    try:
        guild = after
        cfg = bm.get_config(guild.id)
        if not cfg.get("permwatch_enabled"):
            return
        watched = (("verification_level", "verification level"),
                   ("mfa_level", "2FA requirement"), ("default_notifications", "default notifications"),
                   ("explicit_content_filter", "explicit media filter"),
                   ("system_channel", "system channel"), ("rules_channel", "rules channel"))
        changes = []
        for attr, label in watched:
            try:
                b, a = getattr(before, attr, None), getattr(after, attr, None)
                if b != a:
                    changes.append(f"{label}: {b} → {a}")
            except Exception:
                continue
        if not changes:
            return
        track_sec(guild.id, "perm", target="server config", changes=changes)
        actor, note = await audit_actor(guild, "guild_update")
        open_incident(guild.id, "perm", "warning",
                      "Sensitive server configuration changed",
                      actor=actor, target="server config",
                      evidence=changes + [f"Actor: {(actor or {}).get('name', 'unknown')} — {note}"])
    except Exception:
        pass


async def _on_member_update_ev(before, after) -> None:
    # watch the bot's own role position (hierarchy attacks break everything else)
    try:
        if bm.bot is None or after.id != bm.bot.user.id:
            return
        guild = after.guild
        br = {r.id for r in getattr(before, "roles", [])}
        ar = {r.id for r in getattr(after, "roles", [])}
        if br != ar:
            track_sec(guild.id, "perm", target="bot roles")
            open_incident(guild.id, "perm", "critical",
                          "The bot's own roles were changed",
                          target="bot roles",
                          evidence=["role set changed — verify hierarchy still allows moderation"])
    except Exception:
        pass


async def _on_member_update_ev(before, after) -> None:
    # watch the bot's own role position (hierarchy attacks break everything else)
    try:
        if bm.bot is None or after.id != bm.bot.user.id:
            return
        guild = after.guild
        br = {r.id for r in getattr(before, "roles", [])}
        ar = {r.id for r in getattr(after, "roles", [])}
        if br != ar:
            track_sec(guild.id, "perm", target="bot roles")
            open_incident(guild.id, "perm", "critical",
                          "The bot's own roles were changed",
                          target="bot roles",
                          evidence=["role set changed — verify hierarchy still allows moderation"])
    except Exception:
        pass


# ---------------------------------------------------------------- anti-phishing

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)

# historically-abused lure patterns (substring match on the full host).
# Conservative: only exact scam shapes, never generic words.
KNOWN_BAD_SUBSTRINGS = (
    "discorduwu", "discordnitro", "nitro-gift", "free-nitro", "nitrogg",
    "discord-gift", "steamgift", "discrod", "dicsord", "discorcl",
    "steamcommunnity", "steamcomunity",
)

LURE_WORDS = ("nitro", "gift", "free", "airdrop", "crypto", "wallet",
              "steam", "verify", "login", "reward", "premium")

SAFE_ROOTS = ("discord.com", "discord.gg", "discordapp.com", "dis.gd",
              "discord.media", "discordactivities.com", "youtube.com", "youtu.be",
              "twitch.tv", "twitter.com", "x.com", "reddit.com", "github.com",
              "google.com", "tenor.com", "giphy.com", "spotify.com")

SHORTENERS = ("bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "rb.gy",
              "shorturl.at", "ow.ly", "buff.ly", "rebrand.ly")

CONFUSE_MAP = str.maketrans({
    "а": "a", "е": "e", "і": "i", "і".upper(): "I", "о": "o", "р": "p",
    "ѕ": "s", "х": "x", "с": "c", "к": "k", "м": "m", "н": "h", "т": "t",
    "ԁ": "d", "ԛ": "q", "ԝ": "w", "һ": "h", "ρ": "p", "ο": "o", "ι": "i",
    "κ": "k", "ν": "v", "χ": "x", "μ": "u", "０": "0", "１": "1", "３": "e",
    "５": "s", "７": "t", "！": "!", "．": ".", "，": ",",
})


def _fold_host(host: str) -> str:
    h = (host or "").lower()
    try:
        import unicodedata as _ud
        h = _ud.normalize("NFKC", h)
    except Exception:
        pass
    return h.translate(CONFUSE_MAP).replace("rn", "m").replace("vv", "w").replace("cl", "d")


def _registrable(host: str) -> str:
    parts = host.strip().lower().rstrip(".").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _lev(a: str, b: str) -> int:
    if abs(len(a) - len(b)) > 2:
        return 99
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


HIGH_VALUE = ("discord", "discordapp", "steamcommunity", "nitro")


def analyze_url(url: str, trusted: list, blocked: list):
    """Pure verdict. Returns (verdict, reason) with verdict in ok/suspicious/malicious."""
    try:
        host = (urlparse(url).hostname or "").lower().rstrip(".")
    except Exception:
        return "ok", "unparseable"
    if not host:
        return "ok", "no host"
    low_trusted = {str(d).lower().strip().lstrip(".") for d in (trusted or []) if str(d).strip()}
    if any(host == t or host.endswith("." + t) for t in low_trusted):
        return "ok", "trusted domain"
    if any(host == s or host.endswith("." + s) for s in SAFE_ROOTS):
        if host.isascii():
            return "ok", "official domain"
        return "suspicious", f"lookalike of official domain ({host})"
    for b in (blocked or []):
        b = str(b).lower().strip().lstrip(".")
        if b and (host == b or host.endswith("." + b)):
            return "malicious", f"matches blocked domain {b}"
    folded = _fold_host(host)
    for bad in KNOWN_BAD_SUBSTRINGS:
        if bad in folded:
            return "malicious", f"known scam pattern ({bad})"
    reg = _registrable(folded)
    if reg.startswith("xn--"):
        lures = [w for w in LURE_WORDS if w in folded]
        if lures:
            return "suspicious", f"punycode host with lure words ({', '.join(lures)})"
    if reg in SAFE_ROOTS:
        # folded to an official domain, but the raw host didn't match it:
        # a pure-ASCII official domain already returned above, so this is a spoof
        return "suspicious", f"lookalike of official domain ({host})"
    base = reg.split(".")[0] if "." in reg else reg
    for hv in HIGH_VALUE:
        if base != hv and _lev(base, hv) <= 2:
            return "suspicious", f"lookalike of {hv} ({host})"
    if reg.startswith(("discord-", "discord_")) or ".discord-" in reg or reg.endswith("-discord"):
        return "suspicious", f"discord lookalike outside official domains ({host})"
    if host in SHORTENERS or _registrable(host) in SHORTENERS:
        path = (urlparse(url).path or "").lower()
        if any(w in path for w in ("nitro", "gift", "free", "steam", "airdrop")):
            return "suspicious", "shortened link with lure keywords"
    return "ok", "no signals"


async def reputation_check(domain: str):
    """Optional external reputation. Returns (verdict, detail) or (None, reason-disabled). Never raises."""
    import os as _os
    if not _os.environ.get("PHISH_REP_URL"):
        return None, "not configured"
    now = time.time()
    hit = _rep_cache.get(domain)
    if hit and now - hit[0] < 86400:
        return hit[1], hit[2]
    import aiohttp as _aio
    url = _os.environ["PHISH_REP_URL"].replace("{domain}", domain)
    headers = {}
    if _os.environ.get("PHISH_REP_KEY"):
        headers["Authorization"] = "Bearer " + _os.environ["PHISH_REP_KEY"]
    try:
        async with _aio.ClientSession(timeout=_aio.ClientTimeout(total=8)) as sess:
            async with sess.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise OSError(f"HTTP {resp.status}")
                data = await resp.json()
    except Exception as exc:
        _rep_cache[domain] = (now, "unknown", f"reputation service unavailable ({exc})")
        return "unknown", f"reputation service unavailable ({exc})"
    verdict = "ok"
    try:
        if isinstance(data, dict):
            for k in ("malicious", "unsafe", "threat", "blocked"):
                if data.get(k):
                    verdict = "malicious"
                    break
    except Exception:
        pass
    _rep_cache[domain] = (now, verdict, "reputation service verdict")
    return verdict, "reputation service verdict"


async def phish_reason(message, cfg) -> str:
    """Separate intelligence layer. Returns a deletion reason or '' — never fetches the link."""
    if not cfg.get("phish_enabled"):
        return ""
    content = message.content or ""
    urls = URL_RE.findall(content)
    if not urls:
        return ""
    trusted = cfg.get("phish_trusted", []) or []
    blocked = cfg.get("phish_blocked", []) or []
    for url in urls[:5]:
        verdict, reason = analyze_url(url, trusted, blocked)
        if verdict == "ok" and cfg.get("phish_rep_enabled"):
            try:
                host = (urlparse(url).hostname or "").lower()
                rep, detail = await reputation_check(host)
                if rep == "malicious":
                    verdict, reason = "malicious", f"threat intel: {detail}"
                elif rep == "unknown":
                    track_sec(message.guild.id, "phish_rep_down", domain=host)
            except Exception:
                pass
        if verdict in ("malicious", "suspicious"):
            track_sec(message.guild.id, "phish", domain=(urlparse(url).hostname or "?")[:120],
                      snippet=content[:160], verdict=verdict)
            open_incident(message.guild.id, "phish", "warning" if verdict == "suspicious" else "critical",
                          f"{verdict} link blocked: {reason}",
                          actor={"id": str(message.author.id), "name": str(message.author)},
                          target=f"#{message.channel.name}",
                          evidence=[f"domain: {(urlparse(url).hostname or '?')[:120]}",
                                    f"reason: {reason}",
                                    f"message: {content[:200]}"])
            return f"that link looks malicious ({reason})"
    return ""


# ---------------------------------------------------------------- diagnostics

def diagnose(guild, cfg) -> list:
    """Evidence-based health checks. Returns [{sev,title,why,feature,fix,anchor}]."""
    out = []

    def add(sev, title, why, feature, fix, anchor=""):
        out.append({"sev": sev, "title": title, "why": why,
                    "feature": feature, "fix": fix, "anchor": anchor})

    me = getattr(guild, "me", None)
    perms = getattr(me, "guild_permissions", None) if me is not None else None
    if me is None or perms is None:
        add("info", "Bot presence unverifiable",
            "The bot object is unavailable, so permission-dependent checks were skipped. "
            "Nothing was flagged as broken without evidence.",
            "posture", "If the bot is offline, restart it; otherwise ignore this.",
            "")

    def has(perm: str) -> bool | None:
        if perms is None:
            return None
        try:
            return bool(getattr(perms, perm, False))
        except Exception:
            return None

    # --- permissions the bot actually needs for enabled features
    needs = []
    if cfg.get("raid_action", "ban") != "none":
        needs.append(("ban_members" if cfg.get("raid_action") == "ban"
                      else "kick_members" if cfg.get("raid_action") == "kick"
                      else "moderate_members", f"raid response ({cfg.get('raid_action')})"))
    if cfg.get("antinuke_enabled"):
        needs.append(("manage_channels", "anti-nuke channel monitoring"))
        needs.append(("view_audit_log", "anti-nuke actor attribution"))
    if cfg.get("permwatch_enabled"):
        needs.append(("view_audit_log", "permission-change attribution"))
    if cfg.get("verify_enabled"):
        needs.append(("manage_roles", "verification role"))
    if cfg.get("sec_auto_strip"):
        needs.append(("manage_roles", "suspect quarantine"))
    for perm, feature in needs:
        h = has(perm)
        if h is False:
            add("critical", f"Missing permission: {perm.replace('_', ' ')}",
                "The bot tried nothing — this check reads the live permission set, "
                "and Discord reports it as denied.",
                feature, f"Server Settings → Roles → give the bot role {perm.replace('_', ' ')}.",
                "raid" if "raid" in feature or "nuke" in feature else "verify")

    # --- role hierarchy (only what can be verified live)
    try:
        if me is not None and me.top_role:
            top = me.top_role
            vrole = guild.get_role(int(cfg.get("verified_role_id") or 0)) if cfg.get("verified_role_id") else None
            if vrole is not None and vrole >= top:
                add("critical", "Verified role sits above the bot",
                    f"@{vrole.name} outranks the bot, so verification role-grants fail.",
                    "verification", "Drag the bot role above it in Server Settings → Roles.",
                    "verify")
            try:
                rr_menus = (bm.rr_data.get(str(guild.id), []) if hasattr(bm, "rr_data") else [])
            except Exception:
                rr_menus = []
            above = set()
            for m in rr_menus:
                for rid in (m.get("roles", []) or []):
                    try:
                        r = guild.get_role(int(rid))
                        if r is not None and r >= top:
                            above.add(r.name)
                    except Exception:
                        continue
            if above:
                add("warning", "Self-serve roles above the bot",
                    f"{len(above)} menu role(s) outrank the bot and cannot be assigned: "
                    + ", ".join(sorted(above))[:160],
                    "reaction roles", "Drag the bot role above them, or remove them from menus.",
                    "roles")
    except Exception:
        pass

    # --- channels referenced in config
    for key, label in (("log_channel_id", "Log channel"), ("verify_channel_id", "Verification channel"),
                       ("abuse_channel_id", "Abuse alert channel"),
                       ("welcome_channel_id", "Welcome channel"),
                       ("goodbye_channel_id", "Goodbye channel"),
                       ("sec_alert_channel", "Security alert channel")):
        cid = cfg.get(key)
        if not cid:
            continue
        try:
            ch = guild.get_channel(int(cid))
        except (ValueError, TypeError):
            ch = None
        if ch is None:
            add("warning", f"{label} points nowhere",
                f"Configured ID {cid} does not resolve to a channel — it was likely deleted.",
                "logging" if "Log" in label or "alert" in label else "misc",
                "Re-pick the channel in the panel; stale IDs are ignored safely.",
                "logs" if "Log" in label or "alert" in label else "verify")
        elif label in ("Log channel", "Security alert channel"):
            try:
                if not ch.permissions_for(guild.me).send_messages:
                    add("warning", f"Cannot write to {label.lower()}",
                        f"#{ch.name} exists but the bot cannot send messages there.",
                        "logging", f"Fix #{ch.name} permissions or pick another channel.",
                        "logs")
            except Exception:
                pass

    # --- invalid thresholds (evidence: unparseable values)
    for key, lo, hi, label in (("join_threshold_count", 2, 100, "raid join threshold"),
                               ("join_threshold_seconds", 5, 3600, "raid time window"),
                               ("antinuke_window", 10, 3600, "anti-nuke window")):
        try:
            v = int(cfg.get(key))
            if not (lo <= v <= hi):
                add("warning", f"Odd threshold: {label} = {v}",
                    f"Outside the sane range {lo}–{hi}; detection may never fire or fire constantly.",
                    "thresholds", f"Set it between {lo} and {hi}.", "raid")
        except (ValueError, TypeError):
            add("critical", f"Broken threshold: {label}",
                f"Value {cfg.get(key)!r} is not a number — detection for it is disabled.",
                "thresholds", "Re-save a numeric value in the panel.", "raid")

    # --- disabled protections: informational only, never alarming
    off = []
    if not cfg.get("antinuke_enabled"):
        off.append("anti-nuke")
    if not cfg.get("permwatch_enabled"):
        off.append("permission monitor")
    if not cfg.get("phish_enabled"):
        off.append("anti-phishing")
    if cfg.get("raid_action", "ban") == "none":
        off.append("raid response")
    if off:
        add("info", "Protections currently off: " + ", ".join(off),
            "These were found disabled in config. Nothing is broken — this is FYI.",
            "posture", "Enable them from their pages if you want coverage.",
            "raid")
    if cfg.get("phish_rep_enabled"):
        import os as _os
        if not _os.environ.get("PHISH_REP_URL"):
            add("warning", "Reputation checks unavailable",
                "Enabled in config but no PHISH_REP_URL is set — links are allowed unless "
                "locally flagged (fail-safe), and each miss is tracked.",
                "anti-phishing", "Set PHISH_REP_URL (+ optional PHISH_REP_KEY) on the host, or turn it off.",
                "automod")

    order = {"critical": 0, "warning": 1, "info": 2}
    return sorted(out, key=lambda f: order.get(f["sev"], 3))


# ---------------------------------------------------------------- emergency

def snapshot_config(cfg: dict) -> dict:
    return {"join_threshold_count": cfg.get("join_threshold_count"),
            "raid_action": cfg.get("raid_action")}


def snapshot_channels(guild, channel_ids=None) -> dict:
    """Capture @everyone overwrites (+ verification level) so recovery can restore them."""
    snap = {"channels": {}, "verification": None, "config": snapshot_config(bm.get_config(guild.id))}
    try:
        snap["verification"] = int(guild.verification_level.value) if hasattr(
            guild.verification_level, "value") else int(guild.verification_level)
    except Exception:
        pass
    everyone = guild.default_role
    for ch in guild.text_channels:
        if channel_ids is not None and ch.id not in set(channel_ids):
            continue
        try:
            ow = ch.overwrites_for(everyone)
            snap["channels"][str(ch.id)] = {
                "name": ch.name,
                "allow": ow._allow.value if hasattr(ow, "_allow") else int(ow.pair()[0].value),
                "deny": ow._deny.value if hasattr(ow, "_deny") else int(ow.pair()[1].value),
            }
        except Exception as exc:
            snap["channels"][str(ch.id)] = {"name": getattr(ch, "name", "?"),
                                            "error": str(exc)[:120]}
    return snap


async def lockdown_subset(guild, channel_ids, reason: str) -> tuple[int, list]:
    """Deny @everyone SendMessages in the given channels only. Returns (locked, failed_names)."""
    from discord import PermissionOverwrite
    everyone = guild.default_role
    locked, failed = 0, []
    for ch in guild.text_channels:
        if channel_ids is not None and ch.id not in set(channel_ids):
            continue
        try:
            ow = ch.overwrites_for(everyone)
            if ow.send_messages is False:
                continue
            ow.send_messages = False
            await ch.set_permissions(everyone, overwrite=ow, reason=f"Emergency lockdown: {reason}")
            locked += 1
        except discord.Forbidden:
            failed.append(getattr(ch, "name", str(ch.id)))
        except Exception:
            failed.append(getattr(ch, "name", str(ch.id)))
    return locked, failed


async def restore_snapshot(guild, snap: dict) -> tuple[list, list]:
    """Best-effort restore. Only touches channels present in the snapshot.
    Returns (restored_names, [(name, reason)])."""
    from discord import PermissionOverwrite
    everyone = guild.default_role
    ok, failed = [], []
    for cid, rec in (snap.get("channels") or {}).items():
        try:
            cid = int(cid)
        except (ValueError, TypeError):
            failed.append((str(cid), "bad channel id in snapshot"))
            continue
        ch = guild.get_channel(cid)
        if ch is None:
            failed.append((rec.get("name", cid), "channel no longer exists"))
            continue
        if "error" in rec:
            failed.append((rec.get("name", cid), "no pre-lockdown state captured"))
            continue
        try:
            ow = PermissionOverwrite.from_pair(
                discord.Permissions(rec.get("allow", 0)),
                discord.Permissions(rec.get("deny", 0)))
            await ch.set_permissions(everyone, overwrite=ow, reason="Emergency recovery restore")
            ok.append(rec.get("name", str(cid)))
        except discord.Forbidden:
            failed.append((rec.get("name", str(cid)), "missing Manage Channels"))
        except Exception as exc:
            failed.append((rec.get("name", str(cid)), str(exc)[:100]))
    if snap.get("verification") is not None:
        try:
            await guild.edit(verification_level=discord.VerificationLevel(snap["verification"]),
                             reason="Emergency recovery restore")
            ok.append("verification level")
        except Exception as exc:
            failed.append(("verification level", str(exc)[:100]))
    for k, v in (snap.get("config") or {}).items():
        try:
            bm.update_config(guild.id, **{k: v})
            ok.append(f"config:{k}")
        except Exception as exc:
            failed.append((f"config:{k}", str(exc)[:100]))
    return ok, failed
