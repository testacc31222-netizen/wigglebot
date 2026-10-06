"""Wigglesworth web dashboard (phase 1: verify + role menus).

Runs in a thread next to the bot on the same Railway service.
Protect with DASHBOARD_KEY env (?key=...). No key set = dashboard disabled.
"""
from __future__ import annotations

import asyncio
import os
import threading

DASHBOARD_KEY = os.environ.get("DASHBOARD_KEY", "")
PORT = int(os.environ.get("PORT", "8080") or 8080)

bot_ref = None  # set by bot.py


def _bot():
    return bot_ref


def _check(key: str) -> bool:
    return bool(DASHBOARD_KEY) and key == DASHBOARD_KEY


def _cfg(gid: int) -> dict:
    return _bot().get_config(gid)


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Wigglesworth Panel</title>
<style>
body{{background:#1a1b26;color:#eee;font-family:sans-serif;max-width:760px;margin:20px auto;padding:0 12px}}
.card{{background:#24283b;border-radius:10px;padding:14px;margin:12px 0}}
button,select{{background:#7aa2f7;color:#111;border:0;border-radius:6px;padding:8px 12px;margin:4px;cursor:pointer;font-weight:bold}}
button.danger{{background:#f7768e}}button.ok{{background:#9ece6a}}
input[type=text]{{padding:8px;border-radius:6px;border:1px solid #555;background:#111;color:#eee}}
.pill{{display:inline-block;background:#414868;border-radius:12px;padding:3px 10px;margin:2px;font-size:13px}}
a{{color:#7aa2f7}}
</style></head><body>
<h2>🛡️ Wigglesworth Panel</h2>
{keyform}
{body}
</body></html>"""


def run_thread():
    try:
        from flask import Flask, request, jsonify, redirect
    except ImportError:
        print("[dash] Flask not installed — dashboard off (pip install flask)")
        return
    if not DASHBOARD_KEY:
        print("[dash] No DASHBOARD_KEY — dashboard disabled")
        return
    app = Flask(__name__)

    def guilds():
        b = _bot()
        return b.guilds if b else []

    @app.get("/")
    def index():
        key = request.args.get("key", "")
        if not _check(key):
            return PAGE.format(keyform="<div class=card>🔑 Enter key: "
                               "<form><input type=text name=key>"
                               "<button>Open</button></form></div>", body=""), 401
        cards = []
        for g in guilds():
            cfg = _cfg(g.id)
            vrole = g.get_role(cfg.get("verified_role_id") or 0)
            vchan = g.get_channel(cfg.get("verify_channel_id") or 0)
            on = bool(cfg.get("verify_enabled"))
            opts = "".join(
                f"<option value={r.id}{' selected' if vrole and r.id == vrole.id else ''}>"
                f"{r.name}</option>" for r in sorted(g.roles, key=lambda r: r.position)
                if not r.is_default() and not r.managed)[:4000]
            menus = _bot().rr_data.get(str(g.id), [])
            mrows = []
            for i, m in enumerate(menus):
                ch = g.get_channel(m.get("channel") or 0)
                mrows.append(
                    f"<div>#{ch.name if ch else m.get('channel')} "
                    f"({len(m.get('roles', []))} roles) "
                    f"<form style=display:inline method=post action='/api/rolemenu?key={key}'>"
                    f"<input type=hidden name=guild value={g.id}>"
                    f"<input type=hidden name=action value='delete'>"
                    f"<input type=hidden name=idx value={i}>"
                    f"<button class=danger>Delete</button></form></div>")
            mlist = "".join(mrows) or "<i>No menus</i>"
            roles = "".join(
                f"<label class=pill><input type=checkbox name=roles value={r.id}> {r.name}</label>"
                for r in sorted(g.roles, key=lambda r: r.position, reverse=True)[:25]
                if not r.is_default() and not r.managed)
            chans = "".join(
                f"<option value={c.id}>{c.name}</option>" for c in g.text_channels[:25])
            cards.append(f"""<div class=card><h3>{g.name}</h3>
<p>Verify: <b>{'ON 🟢' if on else 'OFF 🔴'}</b> ·
Channel: {vchan.mention if vchan else '—'} · Role: {vrole.name if vrole else '—'}</p>
<form method=post action='/api/verify?key={key}'>
<input type=hidden name=guild value={g.id}>
<input type=hidden name=action value='toggle'>
<button>{'Disable' if on else 'Enable'}</button></form>
<form method=post action='/api/verify?key={key}'>
<input type=hidden name=guild value={g.id}>
<input type=hidden name=action value='role'>
<select name=role_id>{opts}</select>
<button>Set verified role</button></form>
<h4>Role menus</h4>{mlist}
<form method=post action='/api/rolemenu?key={key}'>
<input type=hidden name=guild value={g.id}>
<input type=hidden name=action value='add'>
<select name=channel>{chans}</select><br>{roles}<br>
<button>➕ New menu</button></form>
</div>""")
        return PAGE.format(keyform="",
                           body="".join(cards) or "<div class=card>Bot in no servers yet.</div>")

    @app.post("/api/verify")
    def api_verify():
        if not _check(request.args.get("key", "")):
            return "no", 401
        b = _bot()
        gid = int(request.form["guild"])
        action = request.form.get("action")
        if action == "toggle":
            cfg = _cfg(gid)
            b.update_config(gid, verify_enabled=not bool(cfg.get("verify_enabled")))
        elif action == "role":
            b.update_config(gid, verified_role_id=int(request.form["role_id"]))
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
                g = b.get_guild(gid)
                ch = g.get_channel(m["channel"]) if g else None
                if ch:
                    fut = _aio.run_coroutine_threadsafe(ch.fetch_message(m["message"]), b.loop)
                    try:
                        msg = fut.result(timeout=10)
                        fut2 = _aio.run_coroutine_threadsafe(msg.delete(), b.loop)
                        fut2.result(timeout=10)
                    except Exception:
                        pass
            except (IndexError, ValueError):
                pass
            b._save_rr(b.rr_data)
        elif action == "add":
            rids = [int(r) for r in request.form.getlist("roles")][:25]
            ch_id = int(request.form.get("channel", 0))
            if rids and ch_id:
                fut = _aio.run_coroutine_threadsafe(_make_menu(b, gid, ch_id, rids), b.loop)
                try:
                    fut.result(timeout=20)
                except Exception as e:
                    print("[dash] menu create failed:", e)
        return redirect(f"/?key={request.args.get('key', '')}")

    app.run(host="0.0.0.0", port=PORT, threaded=True)


async def _make_menu(b, gid: int, channel_id: int, rids: list[int]):
    g = b.get_guild(gid)
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


def start():
    t = threading.Thread(target=run_thread, daemon=True)
    t.start()
