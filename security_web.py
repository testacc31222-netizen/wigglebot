"""Wigglesworth security dashboard: pages + /api/sec routes.

Uses existing dashboard helpers (cards, fields, escaping) and the generic
/api/config route for all security settings — no duplicate systems.
"""
from __future__ import annotations

import re
import time

import dashboard as D
import security as S

DOMAIN_RE = re.compile(r"^(?=.{1,253}$)(?!-)[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}$")


def _rel(ts) -> str:
    try:
        s = int(time.time() - float(ts or 0))
    except (ValueError, TypeError):
        return "—"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _sec_events(guild_id, action=None, limit=50):
    b = D._bot()
    evs = [e for e in getattr(b, "events_data", []) if e.get("g") == str(guild_id)
           and e.get("k") == "sec"]
    if action:
        evs = [e for e in evs if e.get("action") == action]
    evs.sort(key=lambda e: e.get("t", 0), reverse=True)
    return evs[:limit]


def _sev_pill(sev: str) -> str:
    cls = "on" if sev == "info" else ("warn" if sev == "warning" else "off")
    dot = "●" if sev == "critical" else ("●" if sev == "warning" else "○")
    return f"<span class='statpill {cls}'>{dot} {D._esc(sev.upper())}</span>"


def _status_pill(st: str) -> str:
    cls = "off" if st == "open" else ("warn" if st == "reviewed" else "on")
    return f"<span class='statpill {cls}'>{D._esc(st.upper())}</span>"


# ---------------------------------------------------------------- anti-nuke page

def page_antinuke(guild, cfg, urlkey):
    gid = guild.id
    open_n = [i for i in S.guild_incidents(gid, limit=200) if i.get("status") != "resolved"
              and i.get("type") == "nuke"]
    recent = [i for i in S.guild_incidents(gid, limit=200) if i.get("type") == "nuke"][:8]
    if recent:
        rows = "".join(
            f"<div class=evrow><span class='dot {'bad' if i.get('severity') == 'critical' else 'warn'}'></span>"
            f"<span class=t>{D._esc(i.get('summary', i['id']))}</span>"
            f"<span class=when>{_rel(i.get('created', 0))}</span></div>" for i in recent)
    else:
        rows = ("<div class=empty><b>No nuke incidents</b>"
                "<p>Bursts past your thresholds will open incidents here.</p></div>")
    return (
        D._card("Anti-nuke status", "Burst detection for destructive actions.",
                f"<div class=row><label>Protection<small>Master switch.</small></label>"
                f"<span class='statpill {'on' if cfg.get('antinuke_enabled') else 'off'}'>"
                f"{'● ARMED' if cfg.get('antinuke_enabled') else '○ OFF'}</span></div>"
                f"<div class=row><label>Open nuke incidents<small>Need review.</small></label>"
                f"<span class=pill>{len(open_n)}</span></div>", f"c-{gid}-nukestatus",
                pill=("ARMED", "on") if cfg.get("antinuke_enabled") else ("OFF", "off"))
        + D._card("Detection thresholds", "Max actions per time window before an incident opens.",
                  "".join(D._field(k, D.SCHEMA[k][0], D.SCHEMA[k][1], guild, cfg, urlkey)
                          for k in ("antinuke_enabled", "antinuke_window", "antinuke_sensitivity",
                                    "antinuke_chandel", "antinuke_chancr", "antinuke_roledel",
                                    "antinuke_rolecr", "antinuke_ban", "antinuke_kick",
                                    "antinuke_webhook", "antinuke_perm")),
                  f"c-{gid}-nukethresh")
        + D._card("Automatic response", "Quarantine strips roles below the bot. Never auto-bans.",
                  D._field("sec_auto_strip", *D.SCHEMA["sec_auto_strip"], guild, cfg, urlkey)
                  + D._field("sec_alert_channel", *D.SCHEMA["sec_alert_channel"], guild, cfg, urlkey)
                  + "<p><small>Trusted = server owner + your Raid whitelist. "
                    "The bot itself is always ignored. "
                    f"<a href='#c-{gid}-whitelist'>Manage whitelist →</a></small></p>",
                  f"c-{gid}-nukeresp")
        + D._card("Recent nuke incidents", "Latest bursts, newest first.", rows, f"c-{gid}-nukerecent"))


# ---------------------------------------------------------------- perm watch page

def page_permwatch(guild, cfg, urlkey):
    evs = _sec_events(guild.id, limit=40)
    perms = [e for e in evs if e.get("action") == "perm"][:15]
    if perms:
        rows = "".join(
            f"<div class=evrow><span class=dot></span>"
            f"<span class=t>{D._esc(str(e.get('target', '?')))} — "
            f"{D._esc(', '.join(e.get('changes', []) or [])[:140])}</span>"
            f"<span class=when>{_rel(e.get('t', 0))}</span></div>" for e in perms)
    else:
        rows = ("<div class=empty><b>No permission changes tracked</b>"
                "<p>Role updates with dangerous permission diffs will appear here.</p></div>")
    return (
        D._card("Permission monitor", "Watches dangerous permission grants.",
                D._field("permwatch_enabled", *D.SCHEMA["permwatch_enabled"], guild, cfg, urlkey)
                + D._field("permwatch_admin_only", *D.SCHEMA["permwatch_admin_only"], guild, cfg, urlkey),
                f"c-{guild.id}-permconf",
                pill=("ON", "on") if cfg.get("permwatch_enabled") else ("OFF", "off"))
        + D._card("Recent permission events", "Newest first. Full detail lives in incidents.",
                  rows, f"c-{guild.id}-permevents"))


# ---------------------------------------------------------------- diagnostics page

def page_diag(guild, cfg, urlkey):
    findings = S.diagnose(guild, cfg)
    groups = [("critical", "Critical — fix these first"),
              ("warning", "Warnings — should fix"),
              ("info", "Informational")]
    out = ""
    for sev, title in groups:
        items = [f for f in findings if f["sev"] == sev]
        if not items:
            rows = "<p><small>None. Good.</small></p>"
        else:
            rows = "".join(
                f"<div class=row><label>{D._esc(f['title'])}<small>{D._esc(f['why'])} "
                f"Fix: {D._esc(f['fix'])}</small></label>"
                + (f"<a href='{D._esc(f['anchor'])}'><button type=button class=dim>Open setting</button></a>"
                   if f.get("anchor") else "<span class=pill>manual</span>")
                + "</div>" for f in items)
        dot = "bad" if sev == "critical" else ("warn" if sev == "warning" else "")
        out += D._card(title, f"{len(items)} finding(s).",
                       rows, f"c-{guild.id}-diag-{sev}",
                       pill=(str(len(items)), "off" if sev == "critical" and items else
                             ("warn" if sev == "warning" and items else "on")))
    return out


# ---------------------------------------------------------------- incidents pages

def _incident_row(guild, inc, urlkey):
    actor = (inc.get("actor") or {}).get("name", "unknown") if inc.get("actor") else "unknown"
    return (f"<div class=row><label><code>{inc['id']}</code> {D._esc(inc.get('summary', ''))}<small>"
            f"{D._esc(inc.get('type', ''))} · by {D._esc(actor)} · {_rel(inc.get('created', 0))}</small></label>"
            f"<span>{_sev_pill(inc.get('severity', 'info'))} {_status_pill(inc.get('status', 'open'))} "
            f"<a href='/?key={urlkey}&incident={inc['id']}#s{guild.id}-incidents'>"
            f"<button type=button class=dim>View</button></a></span></div>")


def phishing_card(guild, cfg, urlkey):
    """Anti-phishing settings + recent hits, embedded in the Automod page."""
    gid = guild.id
    rep = "not configured"
    try:
        import os as _os
        if _os.environ.get("PHISH_REP_URL"):
            rep = "configured" if cfg.get("phish_rep_enabled") else "configured (off)"
    except Exception:
        pass
    hits = [e for e in _sec_events(gid, limit=60) if e.get("action") == "phish"][:8]
    if hits:
        rows = ""
        for e in hits:
            dom = str(e.get("domain", "?"))[:80]
            rows += (f"<div class=row><label>{D._esc(dom)}"
                     f"<small>{D._esc(str(e.get('verdict', '')))} · {_rel(e.get('t', 0))}</small></label>"
                     f"<form method=post action='/api/sec?key={urlkey}'>"
                     f"<input type=hidden name=guild value={gid}>"
                     f"<input type=hidden name=action value='trust_domain'>"
                     f"<input type=hidden name=domain value='{D._esc(dom)}'>"
                     f"<button class=dim>Trust domain</button></form></div>")
    else:
        rows = ("<div class=empty><b>No flagged links</b>"
                "<p>Blocked or suspicious links will show here for review.</p></div>")
    return D._card(
        "Phishing intelligence", "Separate layer: known-bad + lookalike + optional reputation.",
        "".join(D._field(k, D.SCHEMA[k][0], D.SCHEMA[k][1], guild, cfg, urlkey)
                for k in ("phish_enabled", "phish_trusted", "phish_blocked", "phish_rep_enabled"))
        + f"<div class=row><label>Reputation service<small>External threat intel.</small></label>"
        f"<span class=pill>{D._esc(rep)}</span></div>"
        f"<h3>Recent hits</h3>{rows}", f"c-{gid}-phish",
        pill=("ON", "on") if cfg.get("phish_enabled") else ("OFF", "off"))


def page_incidents(guild, cfg, urlkey):
    import flask as _flask
    gid = guild.id
    iid = (_flask.request.args.get("incident", "") or "").strip().upper()
    if iid:
        inc = S.get_incident(iid, gid)
        if inc is None:
            return D._card("Incident", "Not found.",
                           "<p><small>No incident with that ID on this server.</small></p>",
                           f"c-{gid}-incdetail")
        tl = "".join(
            f"<div class=evrow><span class=dot></span><span class=t>{D._esc(e.get('text', ''))}</span>"
            f"<span class=when>{_rel(e.get('t', 0))}</span></div>"
            for e in inc.get("timeline", [])[-30:])
        ev = "".join(f"<div class=row><label>Evidence<small>{D._esc(x)}</small></label></div>"
                     for x in inc.get("evidence", [])[:20])
        atts = "".join(f"<div class=row><label>{D._esc(a)}<small>attempted</small></label></div>"
                       for a in inc.get("attempted", [])[-15:])
        done = "".join(f"<div class=row><label>{D._esc(a)}<small>completed</small></label></div>"
                       for a in inc.get("completed", [])[-15:])
        failed = "".join(f"<div class=row><label>{D._esc(a)}<small>failed</small></label></div>"
                         for a in inc.get("failed", [])[-15:])
        notes = "".join(
            f"<div class=row><label>{D._esc(n.get('text', ''))}<small>{_rel(n.get('t', 0))}</small></label></div>"
            for n in inc.get("notes", [])[-20:])
        actor = inc.get("actor") or {}
        snap = inc.get("snapshot")
        ctl = (f"<form method=post action='/api/sec?key={urlkey}'>"
               f"<input type=hidden name=guild value={gid}>"
               f"<input type=hidden name=action value='incident_note'>"
               f"<input type=hidden name=id value='{inc['id']}'>"
               f"<input type=text name=text placeholder='Internal note…' size=30 maxlength=500>"
               f"<button>Note</button></form>"
               f"<form method=post action='/api/sec?key={urlkey}'>"
               f"<input type=hidden name=guild value={gid}>"
               f"<input type=hidden name=action value='incident_status'>"
               f"<input type=hidden name=id value='{inc['id']}'>"
               f"<input type=hidden name=to value='reviewed'>"
               f"<button class=dim>Mark reviewed</button></form>"
               f"<form method=post action='/api/sec?key={urlkey}' "
               f"data-confirm='Mark this incident resolved?'>"
               f"<input type=hidden name=guild value={gid}>"
               f"<input type=hidden name=action value='incident_status'>"
               f"<input type=hidden name=id value='{inc['id']}'>"
               f"<input type=hidden name=to value='resolved'>"
               f"<button class=ok>Resolve</button></form>")
        if snap and not inc.get("restored"):
            ctl += (f"<form method=post action='/api/sec?key={urlkey}' "
                    f"data-confirm='Restore pre-lockdown permissions and settings from this incident? "
                    f"Only channels in the snapshot are touched.'>"
                    f"<input type=hidden name=guild value={gid}>"
                    f"<input type=hidden name=action value='emergency_restore'>"
                    f"<input type=hidden name=id value='{inc['id']}'>"
                    f"<button>↩ Restore snapshot</button></form>")
        return D._card(f"{inc['id']} — {D._esc(inc.get('type', ''))}",
                       f"{D._esc(inc.get('summary', ''))} · {_rel(inc.get('created', 0))}",
                       f"<div class=row><label>Status<small>Workflow state.</small></label>"
                       f"<span>{_sev_pill(inc.get('severity', 'info'))} "
                       f"{_status_pill(inc.get('status', 'open'))}</span></div>"
                       f"<div class=row><label>Actor<small>From audit log when available.</small></label>"
                       f"<span class=pill>{D._esc((actor.get('name') or 'unknown')[:40])}</span></div>"
                       f"<div class=row><label>Target<small>What was affected.</small></label>"
                       f"<span class=pill>{D._esc(str(inc.get('target') or '—'))[:60]}</span></div>"
                       f"<h3>Evidence</h3>{ev or '<p><small>—</small></p>'}"
                       f"<h3>Timeline</h3>{tl or '<p><small>—</small></p>'}"
                       f"<h3>Response attempts</h3>{atts or '<p><small>None yet.</small></p>'}"
                       f"<h3>Completed</h3>{done or '<p><small>—</small></p>'}"
                       f"<h3>Failed</h3>{failed or '<p><small>—</small></p>'}"
                       f"<h3>Notes</h3>{notes or '<p><small>No notes.</small></p>'}"
                       f"<h3>Respond</h3><div class=row><label>Actions<small>Logged to the timeline.</small></label>"
                       f"<span>{ctl}</span></div>",
                       f"c-{gid}-incdetail")
    open_ = S.guild_incidents(gid, status="open", limit=40)
    others = [i for i in S.guild_incidents(gid, limit=60) if i.get("status") != "open"][:20]
    body = "".join(_incident_row(guild, i, urlkey) for i in open_) or \
        "<div class=empty><b>No open incidents</b><p>Detections will open incidents here automatically.</p></div>"
    if others:
        body += "<h3>Recently resolved / reviewed</h3>" + "".join(_incident_row(guild, i, urlkey) for i in others)
    return D._card("Incidents", f"{len(open_)} open.", body, f"c-{gid}-inclist")


# ---------------------------------------------------------------- emergency page

def page_emergency(guild, cfg, urlkey):
    gid = guild.id
    armed = cfg.get("raid_action", "ban") != "none"
    open_crit = [i for i in S.guild_incidents(gid, status="open", limit=200)
                 if i.get("severity") == "critical"]
    open_all = [i for i in S.guild_incidents(gid, status="open", limit=200)]
    locked_hint = ""
    try:
        ev = guild.default_role
        for ch in guild.text_channels[:50]:
            try:
                if ch.overwrites_for(ev).send_messages is False:
                    locked_hint = "at least one channel is currently locked"
                    break
            except Exception:
                continue
    except Exception:
        pass
    status = (
        f"<div class=row><label>Raid response<small>Automatic punishment.</small></label>"
        f"<span class='statpill {'on' if armed else 'off'}'>{'ARMED' if armed else 'OFF'}</span></div>"
        f"<div class=row><label>Anti-nuke<small>Burst detection.</small></label>"
        f"<span class='statpill {'on' if cfg.get('antinuke_enabled') else 'off'}'>"
        f"{'ARMED' if cfg.get('antinuke_enabled') else 'OFF'}</span></div>"
        f"<div class=row><label>Channels<small>Lockdown footprint.</small></label>"
        f"<span class=pill>{locked_hint or 'all writable'}</span></div>"
        f"<div class=row><label>Open critical incidents<small>Need action.</small></label>"
        f"<span class=pill>{len(open_crit)} critical / {len(open_all)} open</span></div>")
    boxes = "".join(
        f"<label class=pill><input type=checkbox name=channels value={c.id} checked> #{D._esc(c.name)}</label>"
        for c in guild.text_channels[:30])
    lockform = (
        f"<form method=post action='/api/sec?key={urlkey}' "
        f"data-confirm='Lock the selected channels? @everyone loses Send Messages until you restore. Trusted roles with explicit allows keep access.'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='emergency_lockdown'>"
        f"<input type=text name=reason placeholder='Reason (shown in audit log)' size=24 maxlength=120>"
        f"<div style='max-height:220px;overflow-y:auto;display:flex;flex-wrap:wrap;gap:6px;padding:6px 0'>{boxes}</div>"
        f"<button class=danger>🔒 Emergency lockdown</button></form>"
        f"<form method=post action='/api/sec?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='emergency_unlock'>"
        f"<button class=ok>🔓 Unlock (no restore)</button></form>")
    heighten = (
        f"<form method=post action='/api/sec?key={urlkey}' "
        f"data-confirm='Heighten sensitivity? Join threshold drops to 2 until you stand down.'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='heighten'>"
        f"<button class=dim>⬆ Heighten sensitivity</button></form>"
        f"<form method=post action='/api/sec?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='standdown'>"
        f"<button class=dim>⬇ Stand down</button></form>")
    if open_all:
        inclist = "".join(_incident_row(guild, i, urlkey) for i in open_all[:10])
    else:
        inclist = "<p><small>No active incidents. Quiet — good.</small></p>"
    return (
        D._card("Protection status", "Live posture. Read-only.",
                status, f"c-{gid}-emstatus",
                pill=("ALERT", "off") if open_crit else ("WATCH", "warn") if open_all else ("CALM", "on"))
        + D._card("Emergency lockdown", "Snapshot first, lock second, restore after.",
                  lockform, f"c-{gid}-emlock", "lockcard")
        + D._card("Join sensitivity", "Temporarily tighten raid detection.",
                  heighten, f"c-{gid}-emheight")
        + D._card("Active incidents", "Newest first.", inclist, f"c-{gid}-eminc"))


# ---------------------------------------------------------------- overview strip

def overview_security(guild, cfg):
    open_ = S.guild_incidents(guild.id, status="open", limit=200)
    crit = sum(1 for i in open_ if i.get("severity") == "critical")
    armed = cfg.get("raid_action", "ban") != "none" and bool(cfg.get("antinuke_enabled"))
    return (
        f"<div class=row><label>Protection<small>Raid response + anti-nuke.</small></label>"
        f"<span class='statpill {'on' if armed else 'off'}'>{'ARMED' if armed else 'PARTIAL'}</span></div>"
        f"<div class=row><label>Open incidents<small>Across all security systems.</small></label>"
        f"<span class=pill>{crit} critical / {len(open_)} open</span></div>"
        f"<div class=row><label>Review<small>Full timeline and response.</small></label>"
        f"<a href='#s{guild.id}-incidents'><button type=button class=dim>Open incidents</button></a></div>")


# ---------------------------------------------------------------- routes

def register(app, deps):
    import asyncio as _aio
    from flask import request, redirect
    _check = deps["check"]
    _get_bot = deps["bot"]
    _get_disc = deps["disc"]

    def _rl(key, endpoint):
        return D._ratelimit(key, endpoint)

    @app.post("/api/sec")
    def api_sec():
        key = request.args.get("key", "")
        if not _check(key):
            return "no", 401
        if not _rl(key, "sec"):
            return "slow down", 429
        b = _get_bot()
        disc = _get_disc()
        if not disc:
            return redirect(f"/?key={key}")
        try:
            gid = int(request.form["guild"])
        except (ValueError, TypeError, KeyError):
            return redirect(f"/?key={key}")
        g = disc.get_guild(gid)
        if not g:
            return redirect(f"/?key={key}")
        action = request.form.get("action", "")

        async def _run(coro):
            fut = _aio.run_coroutine_threadsafe(coro, disc.loop)
            return fut.result(timeout=30)

        if action == "incident_note":
            inc = S.get_incident(request.form.get("id", ""), gid)
            if inc:
                text = (request.form.get("text", "") or "")[:500]
                if text.strip():
                    S.incident_note(inc["id"], gid, text.strip())
            return redirect(f"/?key={key}&incident={request.form.get('id', '')}#s{gid}-incidents")
        if action == "incident_status":
            inc = S.get_incident(request.form.get("id", ""), gid)
            to = request.form.get("to", "")
            if inc and to in ("reviewed", "resolved", "open"):
                S.incident_status(inc["id"], gid, to)
            return redirect(f"/?key={key}&incident={request.form.get('id', '')}#s{gid}-incidents")
        if action == "trust_domain":
            dom = (request.form.get("domain", "") or "").lower().strip().lstrip(".")
            if DOMAIN_RE.match(dom):
                cfg = b.get_config(gid)
                trusted = [str(d) for d in (cfg.get("phish_trusted", []) or [])]
                if dom not in trusted:
                    trusted.append(dom)
                    b.update_config(gid, phish_trusted=trusted)
                    S.track_sec(gid, "config", key="phish_trusted")
            return redirect(f"/?key={key}#s{gid}-automod")
        if action == "emergency_lockdown":
            try:
                wanted = {int(x) for x in request.form.getlist("channels") if str(x).isdigit()}
            except (ValueError, TypeError):
                wanted = set()
            valid = {c.id for c in g.text_channels}
            scope = sorted(wanted & valid) or None  # None = all
            reason = (request.form.get("reason", "") or "")[:120] or "emergency dashboard lockdown"
            snap = S.snapshot_channels(g, scope)
            inc = S.open_incident(gid, "emergency", "critical",
                                  f"Emergency lockdown ({'all channels' if scope is None else f'{len(scope)} channels'})",
                                  target="channels", evidence=[f"reason: {reason}"])
            inc["snapshot"] = snap
            S.incident_timeline(inc, "Pre-lockdown snapshot captured.")
            try:
                if scope is None:
                    n = _run(b.lockdown_guild(g, reason=f"emergency: {reason}"))
                    S.record_attempt(inc, "lockdown all channels", True, f"{n} locked")
                else:
                    n, failed = _run(S.lockdown_subset(g, scope, reason))
                    S.record_attempt(inc, f"lockdown {len(scope)} channels",
                                     not failed, f"{n} locked" + (f"; failed: {', '.join(failed)}" if failed else ""))
                    for f_ in failed:
                        S.record_attempt(inc, f"lock {f_}", False, "Discord refused")
            except Exception as exc:
                S.record_attempt(inc, "lockdown", False, str(exc)[:120])
            return redirect(f"/?key={key}#s{gid}-emergency")
        if action == "emergency_unlock":
            try:
                n = _run(b.unlock_guild(g, reason="emergency dashboard unlock"))
                S.track_sec(gid, "emergency", action="unlock", channels=n)
            except Exception as exc:
                S.track_sec(gid, "emergency", action="unlock_failed", detail=str(exc)[:120])
            return redirect(f"/?key={key}#s{gid}-emergency")
        if action == "emergency_restore":
            inc = S.get_incident(request.form.get("id", ""), gid)
            if inc and inc.get("snapshot") and not inc.get("restored"):
                try:
                    ok_, failed = _run(S.restore_snapshot(g, inc["snapshot"]))
                    for o in ok_:
                        S.record_attempt(inc, f"restore {o}", True)
                    for f_, why in failed:
                        S.record_attempt(inc, f"restore {f_}", False, why)
                    inc["restored"] = True
                    S.incident_timeline(inc, f"Restore finished: {len(ok_)} ok, {len(failed)} need manual attention.")
                    S._save_incidents()
                except Exception as exc:
                    S.record_attempt(inc, "restore", False, str(exc)[:120])
            return redirect(f"/?key={key}&incident={request.form.get('id', '')}#s{gid}-incidents")
        if action == "heighten":
            cfg = b.get_config(gid)
            inc = S.open_incident(gid, "emergency", "warning", "Sensitivity heightened (threshold → 2)",
                                  evidence=["previous join_threshold_count: "
                                            f"{cfg.get('join_threshold_count')}"])
            inc["snapshot"] = {"channels": {}, "verification": None,
                               "config": S.snapshot_config(cfg)}
            b.update_config(gid, join_threshold_count=2)
            S.record_attempt(inc, "heighten", True)
            return redirect(f"/?key={key}#s{gid}-emergency")
        if action == "standdown":
            cands = [i for i in S.guild_incidents(gid, limit=50)
                     if i.get("type") == "emergency" and i.get("snapshot", {}).get("config")
                     and not i.get("restored")]
            if cands:
                inc = cands[0]
                for k, v in (inc["snapshot"].get("config") or {}).items():
                    try:
                        b.update_config(gid, **{k: v})
                        S.record_attempt(inc, f"restore config:{k}", True)
                    except Exception as exc:
                        S.record_attempt(inc, f"restore config:{k}", False, str(exc)[:100])
                inc["restored"] = True
                S.incident_timeline(inc, "Stood down — config restored.")
                S._save_incidents()
            return redirect(f"/?key={key}#s{gid}-emergency")
        return redirect(f"/?key={key}")
