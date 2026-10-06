"""Wigglesworth web dashboard — premium dark UI, all features.

Runs beside the bot on Railway. Guarded by DASHBOARD_KEY (?key=...).
"""
from __future__ import annotations

import os
import threading

DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
PORT = int(os.environ.get("PORT", "8080") or 8080)

bot_ref = None  # set by bot.py to the bot module


def _bot():
    return bot_ref


def _disc():
    b = _bot()
    return getattr(b, "bot", None) if b else None


def _check(key: str) -> bool:
    return bool(DASHBOARD_KEY) and key == DASHBOARD_KEY


# key -> (kind, label) kinds: bool int channel role mood
SCHEMA: dict[str, tuple[str, str]] = {
    "verify_enabled": ("bool", "Verification gate"),
    "verified_role_id": ("role", "Verified role"),
    "log_channel_id": ("channel", "Log channel"),
    "welcome_channel_id": ("channel", "Welcome channel"),
    "goodbye_channel_id": ("channel", "Goodbye channel"),
    "automod_invites": ("bool", "Delete Discord invites"),
    "automod_links": ("bool", "Delete all links"),
    "automod_max_mentions": ("int", "Max mentions per message"),
    "automod_spam": ("bool", "Repeat-spam filter (4x/30s)"),
    "automod_caps": ("bool", "CAPS filter (>70%)"),
    "automod_emoji": ("bool", "Emoji-spam filter"),
    "automod_max_emoji": ("int", "Max emojis per message"),
    "automod_words": ("words", "Banned words (comma separated)"),
    "lobby_channel_id": ("channel", "Join-to-create VC"),
    "chat_channel_id": ("channel", "Bot chat channel (blank = anywhere)"),
    "chat_enabled": ("bool", "Chatbot replies"),
    "bot_mood": ("mood", "Chatbot mood"),
    "autoreact": ("bool", "Auto reactions"),
    "qotd_channel_id": ("channel", "Question-of-the-day channel"),
    "join_threshold_count": ("int", "Raid: joins to trigger"),
    "join_threshold_seconds": ("int", "Raid: within seconds"),
    "raid_action": ("action", "Raid response"),
    "lockdown_duration_minutes": ("int", "Auto-unlock minutes (0 = manual)"),
    "new_account_age_days": ("int", "Flag accounts younger than (days)"),
    "timeout_duration_minutes": ("int", "Mute length minutes"),
}

SECTIONS = [
    ("🛡️ Verification", ["verify_enabled", "verified_role_id"]),
    ("🎭 Reaction roles", []),
    ("🚨 Raid guard", ["join_threshold_count", "join_threshold_seconds", "raid_action",
                       "lockdown_duration_minutes", "new_account_age_days",
                       "timeout_duration_minutes"]),
    ("🔐 Lockdown", []),
    ("📋 Whitelist", []),
    ("🧹 Mod actions", []),
    ("👋 Welcome", ["welcome_channel_id", "goodbye_channel_id"]),
    ("📝 Logging", ["log_channel_id"]),
    ("🤖 Automod", ["automod_invites", "automod_links", "automod_max_mentions",
                    "automod_spam", "automod_caps", "automod_emoji", "automod_max_emoji",
                    "automod_words"]),
    ("🔊 Voice lobby", ["lobby_channel_id"]),
    ("💬 Chatbot", ["chat_enabled", "chat_channel_id", "bot_mood", "autoreact"]),
    ("❓ Daily question", ["qotd_channel_id"]),
]

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0b0b14;color:#f2f2f8;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.bg{position:fixed;inset:0;z-index:-1;animation:bgdrift 24s ease-in-out infinite alternate;
background:radial-gradient(700px 420px at 12% -5%,rgba(190,242,100,.16),transparent 60%),
radial-gradient(760px 520px at 88% 8%,rgba(124,58,237,.4),transparent 62%),
radial-gradient(900px 640px at 50% 108%,rgba(76,29,149,.55),transparent 65%),#0b0b14}
@keyframes bgdrift{from{transform:scale(1)}to{transform:scale(1.06)}}
.shell{max-width:1120px;margin:22px auto;padding:0 18px 40px}
.panel{background:#0a0a12;border:1px solid #23232f;border-radius:26px;padding:26px 26px 8px;
box-shadow:0 30px 80px rgba(0,0,0,.55);animation:rise .5s ease both}
@keyframes rise{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
.topbar{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.brand{display:flex;align-items:center;gap:9px;font-weight:900;font-size:17px}
.brand .orb{width:26px;height:26px;border-radius:50%;
background:conic-gradient(from 40deg,#a3e635,#7c3aed,#22d3ee,#a3e635);animation:spin 9s linear infinite}
@keyframes spin{to{transform:rotate(360deg)}}
.pills{display:flex;gap:7px;flex-wrap:wrap;margin-left:6px}
.pills a{color:#b9b9cf;text-decoration:none;font-size:13px;padding:7px 15px;border-radius:20px;background:#17171f;transition:all .18s}
.pills a:hover{background:#fff;color:#111;transform:translateY(-1px)}
.pills a.hot{background:#fff;color:#111}
.hero{display:flex;justify-content:space-between;align-items:flex-end;gap:14px;flex-wrap:wrap;margin:20px 2px 4px}
.hero h1{font-size:31px;font-weight:800;letter-spacing:-.5px}
.hero h1 span{color:#8b8b9e}.hero p{color:#8b8b9e;margin-top:4px;font-size:14px}
.range{background:#17171f;border-radius:20px;padding:4px;display:flex;gap:4px}
.range span{font-size:12px;color:#8b8b9e;padding:6px 14px;border-radius:16px}
.range span.on{background:#26263a;color:#fff}
.bignum{font-size:15px;color:#c9c9de}.bignum b{font-size:38px;color:#fff;letter-spacing:-1px}
.bignum small{color:#22d3ee;font-size:13px;font-weight:700}
.avail{font-size:13px;color:#8b8b9e;margin:8px 0 12px}.avail b{color:#fff}
.btnrow{display:flex;gap:8px;flex-wrap:wrap}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-top:16px}
@media(max-width:900px){.grid3{grid-template-columns:1fr}}
.card{background:#121218;border:1px solid #22222f;border-radius:18px;padding:18px;margin:0;
animation:rise .55s ease both}
.card:nth-child(2){animation-delay:.07s}.card:nth-child(3){animation-delay:.14s}
.card h2{font-size:15px;margin-bottom:12px;display:flex;justify-content:space-between;align-items:center}
.card h2 .x{color:#55556a;font-size:14px}
.card h3{margin:16px 0 6px;font-size:12px;color:#a5b4fc;text-transform:uppercase;letter-spacing:.6px}
.legend{display:flex;gap:14px;font-size:12px;color:#8b8b9e;margin-bottom:8px}
.dot{width:9px;height:9px;border-radius:3px;display:inline-block;margin-right:5px}
.dot.g{background:#a3e635}.dot.p{background:#7c3aed}.dot.gr{background:#55556a}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid #1b1b27}
.row:first-of-type{border-top:0}.row label{font-size:14px}.row small{display:block;color:#8b8b9e;font-size:12px}
button,select{background:#fff;color:#111;border:0;border-radius:20px;padding:8px 16px;font-weight:700;cursor:pointer;font-size:13px;transition:transform .15s}
button:hover{transform:translateY(-1px)}button.danger{background:#f43f5e;color:#fff}
button.ok{background:#22c55e;color:#06240f}
select,input[type=text]{background:#17171f;color:#eee;border:1px solid #2c2c44;border-radius:8px;padding:8px}
.badge{display:inline-block;border-radius:20px;padding:2px 12px;font-size:12px;font-weight:800}
.on{background:rgba(34,197,94,.15);color:#4ade80}.off{background:rgba(244,63,94,.15);color:#fda4af}
.pill{display:inline-block;background:#1d1d2e;border-radius:12px;padding:3px 10px;margin:2px;font-size:13px}
.sec{margin-top:18px}
.sechead{font-size:20px;font-weight:800;margin:6px 2px 0}
.lb{display:flex;align-items:center;gap:10px;margin:9px 0}
.lb .who{width:120px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.lb .track{flex:1;height:9px;background:#23232f;border-radius:6px;overflow:hidden}
.lb .fill{height:100%;background:linear-gradient(90deg,#a3e635,#7c3aed);border-radius:6px;
animation:fill 1s ease both}
@keyframes fill{from{width:0}}
.lb .xp{font-size:12px;color:#8b8b9e;min-width:74px;text-align:right}
.txn{display:flex;align-items:center;gap:10px;padding:9px 0;border-top:1px solid #1b1b27;font-size:13px}
.txn:first-of-type{border-top:0}.txn .t{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.txn .tag{font-size:11px;background:#1d1d2e;border-radius:12px;padding:2px 10px;color:#a5b4fc}
.txn .amt{font-weight:800}.pos{color:#4ade80}.neg{color:#f87171}
.heat{display:grid;grid-template-columns:repeat(7,1fr);gap:5px;margin-top:4px}
.heat i{aspect-ratio:1.4;border-radius:5px;background:#23232f;animation:pop .4s ease both}
@keyframes pop{from{opacity:0;transform:scale(.6)}to{opacity:1;transform:none}}
.heat i.l1{background:#2e1065}.heat i.l2{background:#5b21b6}.heat i.l3{background:#7c3aed}
.heat i.l4{background:#a855f7}.heat i.l5{background:#c084fc}
form{margin:0}.footer{text-align:center;color:#55556e;font-size:12px;padding:22px}
"""

BASE = ("<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Wigglesworth Panel</title><style>" + CSS + "</style></head>"
        "<body><div class=bg></div><div class=shell><div class=panel>"
        "<div class=topbar><span class=brand><span class=orb></span>Wigglesworth</span>"
        "<span class=pills><a href='#overview'>Overview</a>"
        "<a href='#verify'>Verify</a>"
        "<a href='#roles'>Roles</a><a href='#raid'>Raid</a><a href='#automod'>Automod</a>"
        "<a href='#chat'>Chat</a><a href='#logs'>Logs</a></span></div>"
        "%%BODY%%"
        "</div><div class=footer>Wigglesworth Bot · keep your ?key= secret</div></div></body></html>")


def _page(body: str) -> str:
    return BASE.replace("%%BODY%%", body)


def _role_opts(guild, current):
    out = ["<option value=''>— none —</option>"]
    for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if r.is_default() or r.managed:
            continue
        sel = " selected" if current == r.id else ""
        out.append(f"<option value={r.id}{sel}>{r.name}</option>")
    return "".join(out)


def _chan_opts(guild, current, voice=False):
    out = ["<option value=''>— none —</option>"]
    chans = guild.voice_channels if voice else guild.text_channels
    for c in chans[:30]:
        sel = " selected" if current == c.id else ""
        out.append(f"<option value={c.id}{sel}>#{c.name}</option>")
    return "".join(out)


def _field(key, kind, label, guild, cfg, urlkey):
    if kind == "bool":
        state = bool(cfg.get(key))
        return (f"<div class=row><label>{label}<br><span class='badge {('on' if state else 'off')}'>"
                f"{'ON' if state else 'OFF'}</span></label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=hidden name=value value={'0' if state else '1'}>"
                f"<button>{'Turn off' if state else 'Turn on'}</button></form></div>")
    if kind == "int":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=text name=value value='{cfg.get(key) or ''}' size=6>"
                f"<button>Save</button></form></div>")
    if kind == "words":
        cur = ", ".join(cfg.get(key) or [])
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=text name=value value='{cur}' size=24>"
                f"<button>Save</button></form></div>")
    if kind == "role":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{_role_opts(guild, cfg.get(key))}</select>"
                f"<button>Save</button></form></div>")
    if kind == "channel":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{_chan_opts(guild, cfg.get(key))}</select>"
                f"<button>Save</button></form></div>")
    if kind == "mood":
        cur = cfg.get(key)
        opts = "".join(f"<option{' selected' if cur == m else ''}>{m}</option>"
                        for m in ("chill", "savage", "formal", "hype"))
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{opts}</select>"
                f"<button>Save</button></form></div>")
    if kind == "action":
        cur = cfg.get(key)
        opts = "".join(f"<option{' selected' if cur == a else ''}>{a}</option>"
                        for a in ("kick", "ban", "timeout", "none"))
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{opts}</select>"
                f"<button>Save</button></form></div>")
    return ""


SECTION_IDS = {"🛡️ Verification": "verify", "🎭 Reaction roles": "roles",
               "🚨 Raid guard": "raid", "🔐 Lockdown": "raid",
               "🤖 Automod": "automod", "💬 Chatbot": "chat",
               "📝 Logging": "logs", "🧹 Mod actions": "logs",
               "📋 Whitelist": "raid"}


def _section(title, anchor, inner):
    return (f"<div class=card id='{anchor}'><h2>{title}</h2>{inner}</div>" if inner else "")


def _guild_block(guild, cfg, urlkey):
    parts = [f"<h2 class=sechead style='font-size:22px;margin-top:20px'>{guild.name}</h2>"]
    for title, keys in SECTIONS:
        anchor = SECTION_IDS.get(title, "")
        inner = "".join(_field(k, SCHEMA[k][0], SCHEMA[k][1], guild, cfg, urlkey)
                        for k in keys)
        if title == "🎭 Reaction roles":
            inner += _rolemenu_block(guild, urlkey)
        elif title == "🔐 Lockdown":
            inner += _lockdown_block(guild, urlkey)
        elif title == "📋 Whitelist":
            inner += _whitelist_block(guild, cfg, urlkey)
        elif title == "🧹 Mod actions":
            inner += _mod_block(guild, urlkey)
        parts.append(f"<div class=card id='{anchor}'><h2>{title}</h2>{inner}</div>" if anchor
                     else f"<div class=card><h2>{title}</h2>{inner}</div>")
    return "".join(parts)


def _lockdown_block(guild, urlkey):
    return (f"<div class=row><label>Lock every text channel NOW</label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='lockdown'>"
            f"<button class=danger>🔒 LOCKDOWN</button></form></div>"
            f"<div class=row><label>Restore channels</label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='unlock'>"
            f"<button class=ok>🔓 Unlock</button></form></div>")


def _whitelist_block(guild, cfg, urlkey):
    users = cfg.get("whitelisted_user_ids", []) or []
    rows = "".join(
        f"<div class=row><label><code>{uid}</code></label>"
        f"<form method=post action='/api/whitelist?key={urlkey}'>"
        f"<input type=hidden name=guild value={guild.id}>"
        f"<input type=hidden name=action value='remove'>"
        f"<input type=hidden name=user_id value={uid}>"
        f"<button class=danger>Remove</button></form></div>" for uid in users)
    return ((rows or "<p><small>Empty — raiders beware.</small></p>")
            + f"<form method=post action='/api/whitelist?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='add'>"
            f"<input type=text name=user_id placeholder='Discord user ID' size=20>"
            f"<button>➕ Whitelist</button></form>")


def _mod_block(guild, urlkey):
    chans = "".join(f"<option value={c.id}>#{c.name}</option>" for c in guild.text_channels[:25])
    return (f"<div class=row><label>Bulk delete</label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='purge'>"
            f"<select name=channel>{chans}</select>"
            f"<input type=text name=count value='20' size=4>"
            f"<button class=danger>Purge</button></form></div>"
            f"<div class=row><label>Slowmode (0 = off)</label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='slowmode'>"
            f"<select name=channel>{chans}</select>"
            f"<input type=text name=count value='5' size=4>"
            f"<button>Set</button></form></div>")


def _rolemenu_block(guild, urlkey):
    b = _bot()
    menus = b.rr_data.get(str(guild.id), [])
    rows = []
    for i, m in enumerate(menus):
        ch = guild.get_channel(m.get("channel") or 0)
        rows.append(
            f"<div class=row><label>#{ch.name if ch else m.get('channel')} "
            f"<small>{len(m.get('roles', []))} roles</small></label>"
            f"<form method=post action='/api/rolemenu?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='delete'>"
            f"<input type=hidden name=idx value={i}>"
            f"<button class=danger>Delete</button></form></div>")
    roles = "".join(
        f"<label class=pill><input type=checkbox name=roles value={r.id}> {r.name}</label>"
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)[:25]
        if not r.is_default() and not r.managed)
    chans = "".join(f"<option value={c.id}>#{c.name}</option>" for c in guild.text_channels[:25])
    return (("".join(rows) or "<p><small>No menus yet.</small></p>")
            + f"<form method=post action='/api/rolemenu?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='add'>"
            f"<select name=channel>{chans}</select><br>{roles}<br>"
            f"<button>➕ New menu</button></form>")


def _hero(b, guilds):
    members = sum(len([m for m in g.members if not getattr(m, "bot", False)]) for g in guilds)
    xp_all = getattr(b, "xp_data", {})
    total_xp = sum(int(v.get("xp", 0)) for gd in xp_all.values() for v in gd.values()
                   if isinstance(v, dict))
    earners = sum(1 for gd in xp_all.values() for v in gd.values()
                  if isinstance(v, dict) and int(v.get("xp", 0)) > 0)
    return (f"<div class=hero id=overview><div><h1>Welcome back, <span>boss</span></h1>"
            f"<p>{len(guilds)} server(s) · all systems nominal.</p></div></div>"
            f"<div class=bignum>Total XP floating around<b><br>${total_xp:,}</b> "
            f"<small>+live</small></div>"
            f"<div class=avail>Members watched: <b>{members}</b> · XP earners: <b>{earners}</b></div>"
            f"<div class=btnrow><span class=pill>🛡️ Guard on</span>"
            f"<span class=pill>💬 Chat on</span></div>")


def _leaders(b, guild):
    xp = getattr(b, "xp_data", {}).get(str(guild.id), {})
    board = sorted(((int(v.get("xp", 0)), uid) for uid, v in xp.items()
                    if isinstance(v, dict)), reverse=True)[:5]
    if not board:
        return ""
    top = max(board[0][0], 1)
    rows = "".join(
        f"<div class=lb><span class=who>{(guild.get_member(int(uid)).display_name if guild.get_member(int(uid)) else '—')}</span>"
        f"<span class=track><span class=fill style='width:{int(x * 100 / top)}%'></span></span>"
        f"<span class=xp>{x:,} XP</span></div>" for x, uid in board)
    return (f"<div class=card><h2>🏆 XP Leaders <span class=x>···</span></h2>"
            f"<div class=legend><span><i class='dot g'></i>Top 5</span></div>{rows}</div>")


def _health(b, guild, cfg):
    v = bool(cfg.get("verify_enabled"))
    am = bool(cfg.get("automod_invites"))
    ch = bool(cfg.get("chat_enabled", True))
    rows = [
        ("Verification gate", "ON" if v else "OFF", v),
        ("Invite filter", "ON" if am else "OFF", am),
        ("Chatbot", "ON" if ch else "OFF", ch),
    ]
    body = "".join(
        f"<div class=txn><span class=t>{label}</span>"
        f"<span class='tag'>{state}</span>"
        f"<span class='amt {'pos' if good else 'neg'}'>●</span></div>"
        for label, state, good in rows)
    return (f"<div class=card><h2>📊 Server health <span class=x>···</span></h2>"
            f"<div class=legend><span><i class='dot g'></i>Live</span>"
            f"<span><i class='dot gr'></i>Off</span></div>{body}</div>")


def _weekheat(b, guilds):
    import datetime as _dt
    counts = [0] * 7
    total = 0
    for g in guilds:
        xp = getattr(b, "xp_data", {}).get(str(g.id), {})
        for v in xp.values():
            if not isinstance(v, dict):
                continue
            last = float(v.get("last", 0))
            if last > 0:
                counts[_dt.datetime.fromtimestamp(last, tz=_dt.timezone.utc).weekday()] += 1
                total += 1
    if total == 0:
        cells = "".join("<i></i>" for _ in range(42))
        sub = "No activity yet — chat to light it up"
    else:
        levels = [min(int(c * 6 / max(counts)), 6) if max(counts) else 0 for c in counts]
        cells = "".join(
            f"<i class='l{min(lv - r, 5) if lv - r > 0 else 0}' "
            f"style='animation-delay:{(ci * 6 + r) * .02:.2f}s'></i>"
            if (lv - r) > 0 else "<i></i>"
            for ci, lv in enumerate(levels) for r in range(5, -1, -1))
        best = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][counts.index(max(counts))]
        sub = f"Peak day: {best} · from real XP timestamps"
    days = "".join(f"<span>{d}</span>" for d in ("M", "T", "W", "T", "F", "S", "S"))
    grid = "<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:5px'>" + cells + "</div>"
    return (f"<div class=card><h2>🌐 Weekly rhythm <span class=x>↗</span></h2>"
            f"<div class=legend><span>{sub}</span></div>"
            f"<div style='display:grid;grid-template-columns:repeat(7,1fr);gap:5px;"
            f"text-align:center;font-size:11px;color:#8b8b9e;margin-bottom:4px'>{days}</div>"
            f"{grid}"
            f"<div class=legend style='margin-top:6px'><span>Less</span>"
            f"<span><i class='dot' style='background:#5b21b6'></i></span>"
            f"<span><i class='dot' style='background:#a855f7'></i></span><span>More</span></div></div>")


def create_app():
    try:
        from flask import Flask, request, redirect
    except ImportError:
        return None
    app = Flask(__name__)

    def guilds():
        b = _bot()
        disc = getattr(b, "bot", None) if b else None
        return disc.guilds if disc else []

    def _disc():
        b = _bot()
        return getattr(b, "bot", None) if b else None

    @app.get("/")
    def index():
        key = request.args.get("key", "")
        if not _check(key):
            return _page("<div class=card>🔑 Enter key: "
                         "<form><input type=text name=key>"
                         "<button>Open</button></form></div>"), 401
        b = _bot()
        glist = guilds()
        if not glist:
            return _page("No servers yet.")
        body = _hero(b, glist)
        body += "<div class=grid3>"
        g0 = glist[0]
        body += _health(b, g0, b.get_config(g0.id))
        body += _leaders(b, g0)
        body += _weekheat(b, glist)
        body += "</div>"
        for g in glist:
            body += _guild_block(g, b.get_config(g.id), key)
        return _page(body)

    @app.post("/api/config")
    def api_config():
        key = request.args.get("key", "")
        if not _check(key):
            return "no", 401
        b = _bot()
        gid = int(request.form["guild"])
        name = request.form.get("key", "")
        if name not in SCHEMA:
            return "bad key", 400
        kind, _ = SCHEMA[name]
        raw = request.form.get("value", "").strip()
        if kind == "bool":
            val = raw == "1"
        elif kind == "int":
            try:
                val = int(raw)
            except ValueError:
                return redirect(f"/?key={key}")
        elif kind in ("channel", "role"):
            val = int(raw) if raw.isdigit() else None
        elif kind == "words":
            val = [x.strip().lower() for x in raw.split(",") if x.strip()][:100]
        else:
            val = raw[:50]
        b.update_config(gid, **{name: val})
        return redirect(f"/?key={key}")

    @app.post("/api/rolemenu")
    def api_rolemenu():
        if not _check(request.args.get("key", "")):
            return "no", 401
        import asyncio as _aio
        b = _bot()
        gid = int(request.form["guild"])
        action = request.form.get("action")
        if action == "delete":
            menus = b.rr_data.get(str(gid), [])
            try:
                m = menus.pop(int(request.form.get("idx", -1)))
                disc = _disc()
                g = disc.get_guild(gid) if disc else None
                ch = g.get_channel(m["channel"]) if g else None
                if ch and disc:
                    fut = _aio.run_coroutine_threadsafe(ch.fetch_message(m["message"]), disc.loop)
                    try:
                        msg = fut.result(timeout=10)
                        fut2 = _aio.run_coroutine_threadsafe(msg.delete(), disc.loop)
                        fut2.result(timeout=10)
                    except Exception:
                        pass
            except (IndexError, ValueError):
                pass
            b._save_rr(b.rr_data)
        elif action == "add":
            rids = [int(r) for r in request.form.getlist("roles")][:25]
            ch_id = int(request.form.get("channel", 0))
            disc = _disc()
            if rids and ch_id and disc:
                fut = _aio.run_coroutine_threadsafe(_make_menu(b, gid, ch_id, rids), disc.loop)
                try:
                    fut.result(timeout=20)
                except Exception as e:
                    print("[dash] menu create failed:", e)
        return redirect(f"/?key={request.args.get('key', '')}")

    @app.post("/api/whitelist")
    def api_whitelist():
        if not _check(request.args.get("key", "")):
            return "no", 401
        b = _bot()
        gid = int(request.form["guild"])
        cfg = b.get_config(gid)
        users = list(cfg.get("whitelisted_user_ids", []) or [])
        if request.form.get("action") == "add":
            try:
                uid = int(request.form.get("user_id", "").strip("<>@#!& "))
            except ValueError:
                return redirect(f"/?key={request.args.get('key', '')}")
            if uid and uid not in users:
                users.append(uid)
        else:
            try:
                uid = int(request.form.get("user_id", 0))
            except ValueError:
                uid = 0
            if uid in users:
                users.remove(uid)
        b.update_config(gid, whitelisted_user_ids=users)
        return redirect(f"/?key={request.args.get('key', '')}")

    @app.post("/api/mod")
    def api_mod():
        if not _check(request.args.get("key", "")):
            return "no", 401
        import asyncio as _aio
        b = _bot()
        disc = _disc()
        gid = int(request.form["guild"])
        action = request.form.get("action")
        if not disc:
            return redirect(f"/?key={request.args.get('key', '')}")
        g = disc.get_guild(gid)
        if not g:
            return redirect(f"/?key={request.args.get('key', '')}")

        async def _do():
            if action == "lockdown":
                await b.lockdown_guild(g, reason="dashboard lockdown")
            elif action == "unlock":
                await b.unlock_guild(g, reason="dashboard unlock")
            elif action == "purge":
                ch = g.get_channel(int(request.form.get("channel", 0)))
                n = max(1, min(100, int(request.form.get("count", 20) or 20)))
                if ch:
                    try:
                        await ch.purge(limit=n)
                    except Exception:
                        pass
            elif action == "slowmode":
                ch = g.get_channel(int(request.form.get("channel", 0)))
                n = max(0, min(21600, int(request.form.get("count", 0) or 0)))
                if ch:
                    try:
                        await ch.edit(slowmode_delay=n, reason="dashboard slowmode")
                    except Exception:
                        pass

        try:
            fut = _aio.run_coroutine_threadsafe(_do(), disc.loop)
            fut.result(timeout=30)
        except Exception as e:
            print("[dash] mod action failed:", e)
        return redirect(f"/?key={request.args.get('key', '')}")

    return app


async def _make_menu(b, gid: int, channel_id: int, rids: list[int]):
    disc = getattr(b, "bot", b)
    g = disc.get_guild(gid)
    ch = g.get_channel(channel_id) if g else None
    if not ch:
        return
    import discord
    embed = discord.Embed(title="🎭 Pick your roles",
                          description="Tap a button to add/remove it.",
                          color=discord.Color.blurple())
    view = b.RoleMenuView(gid, rids)
    for btn in view.children:
        role = g.get_role(btn.role_id)
        if role:
            btn.label = role.name[:80]
            from discord import ButtonStyle
            btn.style = ButtonStyle.primary
    msg = await ch.send(embed=embed, view=view)
    menus = b.rr_data.setdefault(str(gid), [])
    menus.append({"channel": channel_id, "message": msg.id, "roles": rids})
    b._save_rr(b.rr_data)


def run_thread():
    if not DASHBOARD_KEY:
        print("[dash] No DASHBOARD_KEY — dashboard disabled")
        return
    app = create_app()
    if app is None:
        print("[dash] Flask not installed — dashboard off (pip install flask)")
        return
    app.run(host="0.0.0.0", port=PORT, threaded=True)


def start():
    t = threading.Thread(target=run_thread, daemon=True)
    t.start()
