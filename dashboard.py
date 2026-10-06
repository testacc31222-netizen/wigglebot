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
*{box-sizing:border-box}body{margin:0;background:#0f1117;color:#e6e9f5;
font-family:'Segoe UI',system-ui,sans-serif}
.top{background:linear-gradient(135deg,#6d28d9,#2563eb);padding:26px 20px;text-align:center}
.top h1{margin:0;font-size:26px}.top p{margin:6px 0 0;opacity:.85}
.wrap{max-width:900px;margin:0 auto;padding:16px}
.card{background:#1a1d2e;border:1px solid #2b3050;border-radius:14px;padding:16px;margin:14px 0}
.card h2{margin:0 0 4px;font-size:19px}.card h3{margin:14px 0 6px;font-size:15px;color:#a5b4fc}
.row{display:flex;align-items:center;justify-content:space-between;gap:10px;
padding:9px 0;border-top:1px solid #262b45}
.row:first-of-type{border-top:0}
.row label{font-size:14px}.row small{display:block;color:#8b93b8;font-size:12px}
button,select{background:#4f46e5;color:#fff;border:0;border-radius:8px;padding:8px 14px;
font-weight:700;cursor:pointer;font-size:13px}
button:hover{filter:brightness(1.15)}button.danger{background:#dc2645}
button.ok{background:#16a34a}button.dim{background:#3a4060}
select,input[type=text]{background:#0f1117;color:#eee;border:1px solid #3a4060}
.badge{display:inline-block;border-radius:20px;padding:2px 12px;font-size:12px;font-weight:700}
.on{background:#14532d;color:#86efac}.off{background:#3a2030;color:#fda4af}
.pill{display:inline-block;background:#2b3050;border-radius:12px;padding:3px 10px;margin:2px;font-size:13px}
.guildhead{display:flex;align-items:center;gap:10px}
.dot{width:12px;height:12px;border-radius:50%;background:#22c55e}
form{margin:0}
.footer{text-align:center;color:#5b6285;font-size:12px;padding:20px}
"""

BASE = ("<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Wigglesworth Panel</title><style>" + CSS + "</style></head>"
        "<body><div class=top><h1>🛡️ Wigglesworth Panel</h1>"
        "<p>Every feature. No commands needed.</p></div>"
        "<div class=wrap>%%BODY%%</div>"
        "<div class=footer>Wigglesworth Bot · keep your ?key= secret</div></body></html>")


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
    parts = [f"<div class=card><div class=guildhead><span class=dot></span>"
             f"<h2>{guild.name}</h2></div>"]
    for title, keys in SECTIONS:
        parts.append(f"<h3>{title}</h3>")
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
        cards = "".join(_guild_card(g, b.get_config(g.id), key) for g in guilds())
        return _page(cards or "No servers yet.")

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
