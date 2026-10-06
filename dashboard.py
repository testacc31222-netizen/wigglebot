"""Wigglesworth web dashboard — every feature, Carl-style.

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
    "lobby_channel_id": ("channel", "Join-to-create VC"),
    "chat_channel_id": ("channel", "Bot chat channel (blank = anywhere)"),
    "chat_enabled": ("bool", "Chatbot replies"),
    "bot_mood": ("mood", "Chatbot mood"),
    "autoreact": ("bool", "Auto reactions"),
    "qotd_channel_id": ("channel", "Question-of-the-day channel"),
}

SECTIONS = [
    ("🛡️ Verification", ["verify_enabled", "verified_role_id"]),
    ("🎭 Reaction roles", []),
    ("👋 Welcome", ["welcome_channel_id", "goodbye_channel_id"]),
    ("📝 Logging", ["log_channel_id"]),
    ("🤖 Automod", ["automod_invites", "automod_links", "automod_max_mentions"]),
    ("🔊 Voice lobby", ["lobby_channel_id"]),
    ("💬 Chatbot", ["chat_enabled", "chat_channel_id", "bot_mood", "autoreact"]),
    ("❓ Daily question", ["qotd_channel_id"]),
]

CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#08080f;color:#f1f1f6;font-family:'Segoe UI',system-ui,sans-serif;min-height:100vh}
.bg{position:fixed;inset:0;z-index:-1;
background:radial-gradient(600px 400px at 15% 0%,rgba(190,242,100,.13),transparent 60%),
radial-gradient(700px 500px at 85% 10%,rgba(124,58,237,.35),transparent 60%),
radial-gradient(800px 600px at 50% 100%,rgba(76,29,149,.5),transparent 65%),#08080f}
.nav{position:sticky;top:0;z-index:5;backdrop-filter:blur(12px);background:rgba(8,8,15,.75);
border-bottom:1px solid #23233a}
.navin{max-width:1060px;margin:0 auto;padding:12px 16px;display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.logo{font-weight:900;font-size:17px}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin-left:8px}
.tabs a{color:#c9c9de;text-decoration:none;font-size:13px;padding:7px 14px;border-radius:20px;background:#17171f}
.tabs a:hover{background:#fff;color:#111}
.wrap{max-width:1060px;margin:0 auto;padding:18px 16px 40px}
.hero h1{font-size:30px;font-weight:800;margin:14px 0 2px}
.hero h1 span{color:#8f8f9e}.hero p{color:#8f8f9e;margin-bottom:14px}
.statgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:14px 0}
.stat{background:#101018;border:1px solid #23233a;border-radius:16px;padding:16px}
.stat .n{font-size:26px;font-weight:800}.stat .l{color:#8f8f9e;font-size:12px;margin-top:4px}
.stat .bar{height:8px;border-radius:6px;background:#23233a;margin-top:10px;overflow:hidden}
.stat .bar i{display:block;height:100%;background:linear-gradient(90deg,#7c3aed,#22d3ee);border-radius:6px}
.grid2{display:grid;grid-template-columns:1.2fr .8fr;gap:12px}
@media(max-width:760px){.grid2{grid-template-columns:1fr}}
.card{background:#101018;border:1px solid #23233a;border-radius:16px;padding:16px;margin:12px 0}
.card h2{font-size:17px;margin-bottom:2px}.card h3{margin:14px 0 6px;font-size:13px;color:#a5b4fc;text-transform:uppercase;letter-spacing:.5px}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 0;border-top:1px solid #1d1d2c}
.row:first-of-type{border-top:0}.row label{font-size:14px}.row small{display:block;color:#8f8f9e;font-size:12px}
button,select{background:#fff;color:#111;border:0;border-radius:20px;padding:8px 16px;font-weight:700;cursor:pointer;font-size:13px}
button:hover{filter:brightness(.92)}button.danger{background:#f43f5e;color:#fff}
button.ok{background:#22c55e;color:#06240f}button.dim{background:#26263a;color:#eee}
select,input[type=text]{background:#17171f;color:#eee;border:1px solid #2c2c44;border-radius:8px;padding:8px}
.badge{display:inline-block;border-radius:20px;padding:2px 12px;font-size:12px;font-weight:800}
.on{background:rgba(34,197,94,.15);color:#4ade80}.off{background:rgba(244,63,94,.15);color:#fda4af}
.pill{display:inline-block;background:#1d1d2e;border-radius:12px;padding:3px 10px;margin:2px;font-size:13px}
.lb{display:flex;align-items:center;gap:10px;margin:8px 0}
.lb .who{width:130px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;font-size:13px}
.lb .track{flex:1;height:10px;background:#23233a;border-radius:6px;overflow:hidden}
.lb .fill{height:100%;background:linear-gradient(90deg,#a3e635,#7c3aed);border-radius:6px}
.lb .xp{font-size:12px;color:#8f8f9e;min-width:70px;text-align:right}
form{margin:0}.footer{text-align:center;color:#55556e;font-size:12px;padding:20px}
"""

BASE = ("<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Wigglesworth Panel</title><style>" + CSS + "</style></head>"
        "<body><div class=bg></div>"
        "<div class=nav><div class=navin><span class=logo>🛡️ Wigglesworth</span>"
        "<span class=tabs><a href='#overview'>Overview</a><a href='#verify'>Verify</a>"
        "<a href='#roles'>Roles</a><a href='#automod'>Automod</a>"
        "<a href='#chat'>Chat</a><a href='#logs'>Logs</a></span></div></div>"
        "<div class=wrap>%%BODY%%</div>"
        "<div class=footer>Wigglesworth Bot · keep your ?key= secret</div></body></html>")


def _hero(guilds):
    members = sum(len([m for m in g.members if not getattr(m, "bot", False)]) for g in guilds)
    return (f"<div class=hero id=overview><h1>Welcome back, <span>boss</span></h1>"
            f"<p>{len(guilds)} server(s) under protection.</p></div>"
            f"<div class=statgrid>"
            f"<div class=stat><div class=n>{members}</div><div class=l>Members watched</div>"
            f"<div class=bar><i style='width:100%'></i></div></div>"
            f"<div class=stat><div class=n>{len(guilds)}</div><div class=l>Servers</div>"
            f"<div class=bar><i style='width:{min(len(guilds) * 20, 100)}%'></i></div></div>"
            f"</div>")


def _topboard(b, guild):
    xp = getattr(b, "xp_data", {}).get(str(guild.id), {})
    board = sorted(((int(v.get("xp", 0)), uid) for uid, v in xp.items()), reverse=True)[:5]
    if not board:
        return ""
    top = max(board[0][0], 1)
    rows = "".join(
        f"<div class=lb><span class=who>{(guild.get_member(int(uid)).display_name if guild.get_member(int(uid)) else '—')}</span>"
        f"<span class=track><span class=fill style='width:{int(xp_ * 100 / top)}%'></span></span>"
        f"<span class=xp>{xp_:,} XP</span></div>" for xp_, uid in board)
    return f"<div class=card><h2>🏆 XP Leaders</h2>{rows}</div>"


SECTION_IDS = {"🛡️ Verification": "verify", "🎭 Reaction roles": "roles",
               "🤖 Automod": "automod", "💬 Chatbot": "chat", "📝 Logging": "logs"}


def _page(body: str, keyform: str = "") -> str:
    return BASE.replace("%%BODY%%", (keyform + body if keyform else body))


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
    cur = cfg.get(key)
    if kind == "bool":
        state = bool(cur)
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
                f"<input type=text name=value value='{cur or ''}' size=6>"
                f"<button>Save</button></form></div>")
    if kind == "role":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{_role_opts(guild, cur)}</select>"
                f"<button>Save</button></form></div>")
    if kind == "channel":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{_chan_opts(guild, cur)}</select>"
                f"<button>Save</button></form></div>")
    if kind == "mood":
        opts = "".join(f"<option{' selected' if cur == m else ''}>{m}</option>"
                        for m in ("chill", "savage", "formal", "hype"))
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{opts}</select>"
                f"<button>Save</button></form></div>")
    return ""


def _guild_card(guild, cfg, urlkey):
    parts = [f"<div class=card><h2>🛡️ {guild.name}</h2>"]
    for title, keys in SECTIONS:
        anchor = SECTION_IDS.get(title, "")
        parts.append(f"<h3 id='{anchor}'>{title}</h3>" if anchor else f"<h3>{title}</h3>")
        for key in keys:
            kind, label = SCHEMA[key]
            parts.append(_field(key, kind, label, guild, cfg, urlkey))
        if title == "🎭 Reaction roles":
            parts.append(_rolemenu_block(guild, urlkey))
    parts.append("</div>")
    return "".join(parts)


def _rolemenu_block(guild, urlkey):
    b = _bot()
    menus = b.rr_data.get(str(guild.id), [])
    rows = []
    for i, m in enumerate(menus):
        ch = guild.get_channel(m.get("channel") or 0)
        rows.append(
            f"<div class=row><label>#{ch.name if ch else m.get('channel')} "
            f"· {len(m.get('roles', []))} roles</label>"
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
        boards = "".join(_topboard(b, g) for g in glist)
        return _page(_hero(glist)
                     + "".join(_guild_card(g, b.get_config(g.id), key) for g in glist)
                     + boards)

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
        else:
            val = raw[:50]
        b.update_config(gid, **{name: val})
        return redirect(f"/?key={key}")

    @app.post("/api/verify")
    def api_verify():
        if not _check(request.args.get("key", "")):
            return "no", 401
        b = _bot()
        gid = int(request.form["guild"])
        if request.form.get("action") == "toggle":
            cfg = b.get_config(gid)
            b.update_config(gid, verify_enabled=not bool(cfg.get("verify_enabled")))
        return redirect(f"/?key={request.args.get('key', '')}")

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
