"""Wigglesworth community dashboard: /api/community routes + pages.

Mirrors security_web.py: same auth (shared key), same guild scoping
(disc.get_guild), same rate limiting, same redirect-back UX.
"""
from __future__ import annotations

import json
import time

import dashboard as D
import community as C


def _ok_redirect(key, anchor):
    from flask import redirect
    return redirect(f"/?key={key}#{anchor}")


def register(app, deps):
    import asyncio as _aio
    from flask import request, redirect, Response
    _check = deps["check"]
    _get_bot = deps["bot"]
    _get_disc = deps["disc"]

    def _rl(key, endpoint):
        return D._ratelimit(key, endpoint)

    @app.post("/api/community")
    def api_community():
        key = request.args.get("key", "")
        if not _check(key):
            return "no", 401
        if not _rl(key, "community"):
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

        def _run(coro):
            # synchronous: blocks the request thread until the coro finishes
            # (same pattern as api_mod; Flask runs each request in its own thread)
            fut = _aio.run_coroutine_threadsafe(coro, disc.loop)
            return fut.result(timeout=30)

        F = request.form.get
        # ---------- custom commands ----------
        if action == "cc_save":
            name = (F("name", "") or "").strip().lower()
            newname = (F("newname", "") or "").strip().lower() or name
            err = C.validate_cmd_name(newname) if newname != name else C.validate_cmd_name(name)
            live = {c.name.lower() for c in b.bot.commands}
            live.update(a.lower() for c in b.bot.commands for a in getattr(c, "aliases", []))
            if not err and newname in live:
                err = f"`{newname}` is already a bot command."
            try:
                cooldown = max(0, min(3600, int(F("cooldown", 0) or 0)))
            except ValueError:
                cooldown = 0
            perm = F("perm", "everyone")
            if perm not in ("everyone", "mod", "admin", "owner"):
                perm = "everyone"
            cmds = C.custom_cmds(gid)
            if not err:
                if newname != name and name in cmds:
                    del cmds[name]
                cmds[newname] = {
                    "name": newname,
                    "response": (F("response", "") or "")[:2000],
                    "embed": bool(F("embed")),
                    "title": (F("title", "") or "")[:256],
                    "color": (F("color", "") or "5865F2")[:6],
                    "perm": perm, "cooldown": cooldown,
                    "enabled": bool(F("enabled")),
                }
                C._save("customcmds.json")
                C.audit("cc_save", gid, name=newname)
            return redirect(f"/?key={key}#s{gid}-custom")
        if action == "cc_delete":
            cmds = C.custom_cmds(gid)
            name = (F("name", "") or "").strip().lower()
            if name in cmds:
                del cmds[name]
                C._save("customcmds.json")
                C.audit("cc_delete", gid, name=name)
            return redirect(f"/?key={key}#s{gid}-custom")
        # ---------- cases ----------
        if action == "case_note":
            c = C.find_case(gid, F("id", ""))
            text = (F("text", "") or "")[:500]
            if c and text.strip():
                c.setdefault("notes", []).append(
                    {"t": time.time(), "by": "dashboard", "text": text.strip()})
                c["updated"] = time.time()
                C._save("cases.json")
            return redirect(f"/?key={key}&case={F('id', '')}#s{gid}-cases")
        if action == "case_reason":
            c = C.find_case(gid, F("id", ""))
            if c:
                c["reason"] = (F("reason", "") or "")[:500]
                c["updated"] = time.time()
                C._save("cases.json")
                C.audit("case_reason", gid, id=c["id"])
            return redirect(f"/?key={key}&case={F('id', '')}#s{gid}-cases")
        if action == "case_status":
            c = C.find_case(gid, F("id", ""))
            if c and F("to", "") in ("open", "actioned", "appealed", "closed"):
                c["status"] = F("to")
                c["updated"] = time.time()
                C._save("cases.json")
            return redirect(f"/?key={key}&case={F('id', '')}#s{gid}-cases")
        # ---------- suggestions ----------
        if action == "sug_status":
            subs = C.DATA.get("suggestions.json", {}).get(str(gid), {})
            s = subs.get(F("id", ""))
            if s and F("to", "") in C.SUG_STATUS:
                s["status"] = F("to")
                C._save("suggestions.json")
                try:
                    if s.get("message") and s.get("channel"):
                        ch = g.get_channel(s["channel"])
                        if ch:
                            msg = _run(ch.fetch_message(s["message"]))
                            _run(msg.edit(embed=C.sug_embed(g, s)))
                except Exception:
                    pass
            return redirect(f"/?key={key}#s{gid}-suggest")
        if action == "sug_respond":
            subs = C.DATA.get("suggestions.json", {}).get(str(gid), {})
            s = subs.get(F("id", ""))
            if s:
                s["response"] = (F("text", "") or "")[:1000]
                C._save("suggestions.json")
            return redirect(f"/?key={key}#s{gid}-suggest")
        # ---------- announcements ----------
        if action in ("ann_create", "ann_update"):
            try:
                ch_id = int(F("channel", 0) or 0)
            except ValueError:
                ch_id = 0
            ch = g.get_channel(ch_id)
            items = C.store("announcements.json", {}).setdefault(str(gid), [])
            try:
                send_at = int(time.mktime(time.strptime(
                    (F("send_at", "") or "").strip(), "%Y-%m-%dT%H:%M")))
            except ValueError:
                send_at = 0
            recurs = {"": 0, "daily": 86400, "weekly": 604800}.get(F("recurs", ""), 0)
            body = (F("body", "") or "")[:2000]
            if action == "ann_create" and ch is not None and body.strip() and send_at > time.time():
                items.append({"id": C.new_id("N"), "channel_id": ch.id,
                              "title": (F("title", "") or "")[:256], "body": body,
                              "embed": bool(F("embed")), "send_at": send_at,
                              "recurs_s": recurs, "status": "scheduled",
                              "sent_count": 0, "last_error": "", "created": time.time()})
                C._save("announcements.json")
            elif action == "ann_update":
                a = next((x for x in items if x["id"] == F("id", "")), None)
                if a and a.get("status") == "scheduled":
                    if ch is not None:
                        a["channel_id"] = ch.id
                    if body.strip():
                        a["body"] = body
                    a["title"] = (F("title", "") or "")[:256]
                    a["embed"] = bool(F("embed"))
                    if send_at > time.time():
                        a["send_at"] = send_at
                    a["recurs_s"] = recurs
                    C._save("announcements.json")
            return redirect(f"/?key={key}#s{gid}-announce")
        if action == "ann_dup":
            items = C.store("announcements.json", {}).setdefault(str(gid), [])
            a = next((x for x in items if x["id"] == F("id", "")), None)
            if a:
                items.append({**a, "id": C.new_id("N"), "status": "scheduled",
                              "send_at": time.time() + 3600, "sent_count": 0, "last_error": ""})
                C._save("announcements.json")
            return redirect(f"/?key={key}#s{gid}-announce")
        if action in ("ann_cancel", "ann_delete", "ann_reschedule"):
            items = C.DATA.get("announcements.json", {}).get(str(gid), [])
            a = next((x for x in items if x["id"] == F("id", "")), None)
            if a:
                if action == "ann_delete":
                    items.remove(a)
                elif action == "ann_cancel" and a.get("status") == "scheduled":
                    a["status"] = "cancelled"
                elif action == "ann_reschedule" and a.get("status") in ("scheduled", "failed"):
                    try:
                        send_at = int(time.mktime(time.strptime(
                            (F("send_at", "") or "").strip(), "%Y-%m-%dT%H:%M")))
                    except ValueError:
                        send_at = 0
                    if send_at > time.time():
                        a["send_at"] = send_at
                        a["status"] = "scheduled"
                        a["last_error"] = ""
                C._save("announcements.json")
            return redirect(f"/?key={key}#s{gid}-announce")
        # ---------- temp roles ----------
        if action == "tr_assign":
            try:
                member = g.get_member(int(F("user_id", 0) or 0))
                role = g.get_role(int(F("role_id", 0) or 0))
            except ValueError:
                member, role = None, None
            dur_s = b.parse_duration((F("dur", "") or "").strip())
            err = C._validate_role_grant(g, g.me, member, role)
            if member is not None and role is not None and not err and dur_s:
                try:
                    _run(member.add_roles(role, reason="dashboard temp role"))
                    tr = {"id": C.new_id("T"), "guild": str(gid),
                          "user_id": str(member.id), "role_id": str(role.id),
                          "reason": (F("reason", "") or "")[:200] or "dashboard",
                          "by_id": "dashboard",
                          "expires_at": time.time() + dur_s, "status": "active",
                          "created": time.time(), "result": ""}
                    C.store("temproles.json", []).append(tr)
                    C._save("temproles.json")
                    C.audit("temprole_assign", gid, user=str(member.id), role=str(role.id))
                except Exception:
                    pass
            return redirect(f"/?key={key}#s{gid}-temprole")
        if action in ("tr_extend", "tr_cancel", "tr_end"):
            try:
                extra = int(F("extra", 3600) or 3600)
            except ValueError:
                extra = 3600
            hit = next((t for t in C.DATA.get("temproles.json", [])
                        if t.get("guild") == str(gid) and t.get("id") == F("id", "")
                        and t.get("status") == "active"), None)
            if hit:
                if action == "tr_extend":
                    hit["expires_at"] = hit.get("expires_at", time.time()) + max(60, min(extra, 30 * 86400))
                else:
                    # expire-now: the scheduler removes the role within ~30s and
                    # records success/failure. Never call Discord inline here.
                    hit["expires_at"] = time.time() - 1
                C._save("temproles.json")
            return redirect(f"/?key={key}#s{gid}-temprole")
        # ---------- backups ----------
        if action == "backup_create":
            name = (F("name", "") or "").strip()[:60] or time.strftime("Backup %Y-%m-%d %H:%M")
            snaps = C.store("backups.json", {}).setdefault(str(gid), [])
            snap = C.snapshot_backup(gid)
            snaps.append({"id": C.new_id("B"), "name": name, "created": time.time(),
                          "by": "dashboard", **snap})
            C.DATA["backups.json"][str(gid)] = snaps[-10:]
            C._save("backups.json")
            C.audit("backup_create", gid, name=name)
            return redirect(f"/?key={key}#s{gid}-backups")
        if action == "backup_delete":
            snaps = C.DATA.get("backups.json", {}).get(str(gid), [])
            hit = next((s for s in snaps if s["id"] == F("id", "")), None)
            if hit:
                snaps.remove(hit)
                C._save("backups.json")
            return redirect(f"/?key={key}#s{gid}-backups")
        if action == "backup_restore":
            snaps = C.DATA.get("backups.json", {}).get(str(gid), [])
            hit = next((s for s in snaps if s["id"] == F("id", "")), None)
            if hit:
                C.restore_backup(gid, hit)
            return redirect(f"/?key={key}#s{gid}-backups")
        # ---------- wizard ----------
        if action == "wizard_done":
            steps = set(b.get_config(gid).get("wizard_done", []) or [])
            step = (F("step", "") or "")[:40]
            if step:
                if F("done", ""):
                    steps.add(step)
                else:
                    steps.discard(step)
                b.update_config(gid, wizard_done=sorted(steps))
            return redirect(f"/?key={key}#s{gid}-wizard")
        if action == "testmsg":
            try:
                ch = g.get_channel(int(F("channel", 0) or 0))
            except ValueError:
                ch = None
            if ch is not None:
                try:
                    import discord as _d
                    _run(ch.send(embed=_d.Embed(
                        title="✅ Wigglesworth test message",
                        description="If you can read this, the bot can write here.",
                        color=_d.Color.green())))
                except Exception:
                    pass
            return redirect(f"/?key={key}#s{gid}-wizard")
        # ---------- applications ----------
        if action in ("app_form_save", "app_form_delete", "app_form_toggle"):
            forms = C.app_forms(gid)
            if action == "app_form_save":
                fid = F("id", "") or C.new_id("F")
                fields = []
                for i in range(5):
                    lab = (F(f"flabel{i}", "") or "").strip()[:45]
                    if lab:
                        try:
                            ml = max(50, min(int(F(f"maxlen{i}", 500) or 500), 1000))
                        except ValueError:
                            ml = 500
                        fields.append({"label": lab, "required": bool(F(f"freq{i}")),
                                       "maxlen": ml})
                if not fields:
                    return redirect(f"/?key={key}#s{gid}-apply")
                try:
                    rch = g.get_channel(int(F("review_channel", 0) or 0))
                except ValueError:
                    rch = None
                data = {"id": fid, "name": (F("name", "") or "")[:60] or "Application",
                        "desc": (F("desc", "") or "")[:500],
                        "review_channel": rch.id if rch else 0,
                        "open": bool(F("open")), "fields": fields}
                old = next((x for x in forms if x["id"] == fid), None)
                if old is not None:
                    forms[forms.index(old)] = data
                else:
                    forms.append(data)
                C._save("applications.json")
            elif action == "app_form_delete":
                hit = next((x for x in forms if x["id"] == F("id", "")), None)
                if hit:
                    forms.remove(hit)
                    C._save("applications.json")
            elif action == "app_form_toggle":
                hit = next((x for x in forms if x["id"] == F("id", "")), None)
                if hit:
                    hit["open"] = not hit.get("open", True)
                    C._save("applications.json")
            return redirect(f"/?key={key}#s{gid}-apply")
        if action == "app_note":
            subs = C.app_subs(gid)
            s = subs.get(F("id", ""))
            if s:
                notes = s.setdefault("notes", [])
                text = (F("text", "") or "")[:500]
                if text.strip():
                    notes.append({"t": time.time(), "by": "dashboard", "text": text.strip()})
                    C._save("applications.json")
            return redirect(f"/?key={key}&appsub={F('id', '')}#s{gid}-apply")
        if action == "app_decide":
            subs = C.app_subs(gid)
            s = subs.get(F("id", ""))
            if s and F("to", "") in ("approved", "rejected", "info", "pending"):
                s["status"] = F("to")
                s["decided_at"] = time.time()
                C._save("applications.json")
                C.audit("app_decide", gid, id=s["id"], to=F("to"))
            return redirect(f"/?key={key}&appsub={F('id', '')}#s{gid}-apply")
        # ---------- events ----------
        if action in ("event_save", "event_delete", "event_cancel"):
            items = C.ev_list(gid)
            if action == "event_save":
                eid = F("id", "") or C.new_id("E")
                try:
                    starts_at = int(time.mktime(time.strptime(
                        (F("starts_at", "") or "").strip(), "%Y-%m-%dT%H:%M")))
                except ValueError:
                    starts_at = 0
                try:
                    ch = g.get_channel(int(F("channel", 0) or 0))
                except ValueError:
                    ch = None
                try:
                    limit = max(0, min(1000, int(F("limit", 0) or 0)))
                except ValueError:
                    limit = 0
                try:
                    rms = sorted({int(x) for x in (F("remind", "") or "").replace(",", " ").split()
                                  if x.isdigit()} - {0})[:5] or [60]
                except ValueError:
                    rms = [60]
                title = (F("title", "") or "")[:200]
                if title and ch is not None and starts_at > 0:
                    data = {"id": eid, "title": title, "desc": (F("desc", "") or "")[:2000],
                            "channel_id": ch.id, "starts_at": starts_at,
                            "remind_mins": rms, "limit": limit,
                            "status": "active", "message": 0, "rsvps": {},
                            "reminded": {}, "created": time.time()}
                    old = next((x for x in items if x["id"] == eid), None)
                    if old is not None:
                        data["rsvps"] = old.get("rsvps", {})
                        data["message"] = old.get("message", 0)
                        data["reminded"] = old.get("reminded", {})
                        data["created"] = old.get("created", time.time())
                        items[items.index(old)] = data
                    else:
                        items.append(data)
                    C._save("eventsched.json")
            else:
                ev = next((x for x in items if x["id"] == F("id", "")), None)
                if ev:
                    if action == "event_delete":
                        items.remove(ev)
                    elif ev.get("status") == "active":
                        ev["status"] = "cancelled"
                    C._save("eventsched.json")
            return redirect(f"/?key={key}#s{gid}-events")
        if action == "event_publish":
            ev = next((x for x in C.ev_list(gid) if x["id"] == F("id", "")), None)
            if ev and ev.get("status") == "active":
                ch = g.get_channel(ev.get("channel_id") or 0)
                if ch is not None:
                    try:
                        msg = _run(ch.send(embed=C.ev_embed(ev), view=C.EventView(ev["id"])))
                        ev["message"] = msg.id
                        C._save("eventsched.json")
                    except Exception:
                        pass
            return redirect(f"/?key={key}#s{gid}-events")
        return redirect(f"/?key={key}")

    @app.get("/api/community/download")
    def api_community_download():
        key = request.args.get("key", "")
        if not _check(key):
            return "no", 401
        if not D._ratelimit(key, "community-dl"):
            return "slow down", 429
        disc = _get_disc()
        if not disc:
            return "no server", 404
        try:
            gid = int(request.args.get("guild", 0))
        except (ValueError, TypeError):
            return "bad guild", 400
        g = disc.get_guild(gid)
        if not g:
            return "no server", 404
        if request.args.get("type") != "backup":
            return "bad type", 400
        snaps = C.DATA.get("backups.json", {}).get(str(gid), [])
        hit = next((s for s in snaps if s["id"] == request.args.get("id", "")), None)
        if not hit:
            return "not found", 404
        body = json.dumps(hit, indent=2)
        return Response(body, mimetype="application/json",
                        headers={"Content-Disposition":
                                 f"attachment; filename=wigglesworth-backup-{hit['id']}.json"})


# ============================ pages ============================

def _vars_help():
    return ("<p><small>Variables: {user} name · {mention} ping · {server} · {channel} · "
            "{date} · {time} · {membercount}</small></p>")


def page_custom(guild, cfg, urlkey):
    gid = guild.id
    cmds = C.custom_cmds(gid)
    rows = ""
    for name in sorted(cmds):
        c = cmds[name]
        rows += (
            f"<div class=row><label><code>{D._esc('.' + name)}</code>"
            f"<small>{D._esc((c.get('title') or c.get('response', ''))[:90])} · "
            f"{D._esc(c.get('perm', 'everyone'))} · cd {int(c.get('cooldown', 0) or 0)}s · "
            f"{'on' if c.get('enabled', True) else 'off'}{' · embed' if c.get('embed') else ''}</small></label>"
            f"<span><details><summary><button type=button class=dim>Edit</button></summary>"
            f"<form method=post action='/api/community?key={urlkey}'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='cc_save'>"
            f"<input type=hidden name=name value='{D._esc(name)}'>"
            f"<div class=row><label>Rename<small>2–24 chars: a-z 0-9 - _</small></label>"
            f"<input type=text name=newname value='{D._esc(name)}' size=16></div>"
            f"<div class=row><label>Response<small>Supports variables below.</small></label>"
            f"<input type=text name=response value='{D._esc(c.get('response', ''))}' size=30></div>"
            f"<div class=row><label>Send as embed</label>"
            f"<input type=checkbox name=embed value=1{' checked' if c.get('embed') else ''}></div>"
            f"<div class=row><label>Embed title</label>"
            f"<input type=text name=title value='{D._esc(c.get('title', ''))}' size=24></div>"
            f"<div class=row><label>Color hex</label>"
            f"<input type=text name=color value='{D._esc(c.get('color', '5865F2'))}' size=8></div>"
            f"<div class=row><label>Permission</label>"
            f"<select name=perm>{''.join(f'<option value={p}{' selected' if c.get('perm', 'everyone') == p else ''}>{p}</option>' for p in ('everyone', 'mod', 'admin', 'owner'))}</select></div>"
            f"<div class=row><label>Cooldown secs</label>"
            f"<input type=text name=cooldown value='{int(c.get('cooldown', 0) or 0)}' size=6></div>"
            f"<div class=row><label>Enabled</label>"
            f"<input type=checkbox name=enabled value=1{' checked' if c.get('enabled', True) else ''}></div>"
            f"<div class=row><label>Save</label><button class=primary>Save</button></div>"
            f"</form>"
            f"<form method=post action='/api/community?key={urlkey}' data-confirm='Delete .{D._esc(name)}?'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='cc_delete'>"
            f"<input type=hidden name=name value='{D._esc(name)}'>"
            f"<button class=danger>Delete</button></form>"
            f"</details></span></div>")
    if not rows:
        rows = ("<div class=empty><b>No custom commands</b>"
                "<p>Build one below — it works the moment you save.</p></div>")
    form = (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='cc_save'>"
        f"<div class=row><label>Trigger<small>Typed after the prefix, e.g. rules.</small></label>"
        f"<input type=text name=name placeholder='rules' size=16 required></div>"
        f"<div class=row><label>Response<small>Supports variables below.</small></label>"
        f"<input type=text name=response placeholder='Hello {{user}}!' size=30></div>"
        f"<div class=row><label>Embed + options</label><span>"
        f"<label class=pill><input type=checkbox name=embed value=1> embed</label> "
        f"<input type=text name=title placeholder='Embed title' size=16> "
        f"<input type=text name=color placeholder='5865F2' size=8></span></div>"
        f"<div class=row><label>Permission</label>"
        f"<select name=perm><option>everyone</option><option>mod</option>"
        f"<option>admin</option><option>owner</option></select></div>"
        f"<div class=row><label>Cooldown / enabled</label><span>"
        f"<input type=text name=cooldown value='0' size=5> secs "
        f"<label class=pill><input type=checkbox name=enabled value=1 checked> enabled</label></span></div>"
        f"<div class=row><label>Create</label><button class=primary>➕ Create command</button></div>"
        f"</form>")
    return (D._card("Custom commands", f"{len(cmds)} command(s). Conflicts with built-ins are blocked.",
                    rows, f"c-{gid}-cclist")
            + D._card("New custom command", "No code needed.",
                      form + _vars_help()
                      + "<div class=row><label>Embed preview<small>Approximate styling.</small></label>"
                      + "<div class=embedprev id='cc-prev'><b>Title shows here</b><br>Response shows here</div></div>",
                      f"c-{gid}-ccnew"))


def page_cases(guild, cfg, urlkey):
    from flask import request as _rq
    gid = guild.id
    f_user = (_rq.args.get("case_user", "") or "").strip()
    f_action = (_rq.args.get("case_action", "") or "").strip()
    f_status = (_rq.args.get("case_status", "") or "").strip()
    f_detail = (_rq.args.get("case", "") or "").strip()
    cases = C.DATA.get("cases.json", {}).get(str(gid), [])
    if f_detail:
        c = C.find_case(gid, f_detail)
        if c is None:
            body = "<div class=empty><b>Case not found</b></div>"
        else:
            m = guild.get_member(int(c.get("target_id", 0))) if str(c.get("target_id", "")).isdigit() else None
            notes = "".join(f"<div class=row><label>{D._esc(n.get('by', ''))}"
                            f"<small>{D._esc(n.get('text', ''))}</small></label></div>"
                            for n in c.get("notes", [])[-10:]) or "<p><small>No notes.</small></p>"
            body = (
                f"<div class=row><label>User<small>Target.</small></label>"
                f"<span class=pill>{D._esc(m.display_name) if m else 'left server'} "
                f"(`{D._esc(c.get('target_id', ''))}`)</span></div>"
                f"<div class=row><label>Action / moderator</label>"
                f"<span class=pill>{D._esc(c.get('action', ''))} · by {D._esc(c.get('mod_name', ''))}</span></div>"
                f"<div class=row><label>Date</label>"
                f"<span class=pill>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(c.get('created', 0)))}</span></div>"
                f"<div class=row><label>Status</label><span>"
                + "".join(f"<form method=post action='/api/community?key={urlkey}' style='display:inline'>"
                          f"<input type=hidden name=guild value={gid}>"
                          f"<input type=hidden name=action value='case_status'>"
                          f"<input type=hidden name=id value='{c['id']}'>"
                          f"<input type=hidden name=to value='{s}'>"
                          f"<button class={'primary' if c.get('status') == s else 'dim'}>{s}</button></form> "
                          for s in ("open", "actioned", "appealed", "closed"))
                + "</span></div>"
                + f"<form method=post action='/api/community?key={urlkey}'>"
                f"<input type=hidden name=guild value={gid}>"
                f"<input type=hidden name=action value='case_reason'>"
                f"<input type=hidden name=id value='{c['id']}'>"
                f"<div class=row><label>Reason<small>Editable.</small></label>"
                f"<span><input type=text name=reason value='{D._esc(c.get('reason', ''))}' size=30> "
                f"<button>Save reason</button></span></div></form>"
                + (f"<div class=row><label>Evidence</label><span class=pill>{D._esc(c.get('evidence', ''))}</span></div>"
                   if c.get("evidence") else "")
                + (f"<div class=row><label>Appeal</label><span class=pill>{D._esc(c.get('appeal', ''))}</span></div>"
                   if c.get("appeal") else "")
                + f"<h3>Notes</h3>{notes}"
                f"<form method=post action='/api/community?key={urlkey}'>"
                f"<input type=hidden name=guild value={gid}>"
                f"<input type=hidden name=action value='case_note'>"
                f"<input type=hidden name=id value='{c['id']}'>"
                f"<div class=row><label>Add note<small>Staff-only.</small></label>"
                f"<span><input type=text name=text size=30 maxlength=500> <button>Note</button></span></div></form>")
        return D._card(f"Case {D._esc(f_detail.upper())}", "Detail view.",
                       body + f"<p><a href='/?key={urlkey}#s{gid}-cases'>← Back to cases</a></p>",
                       f"c-{gid}-casedetail")
    items = sorted(cases, key=lambda c: c.get("created", 0), reverse=True)
    if f_user:
        items = [c for c in items if f_user.lower() in (c.get("target_name", "") or "").lower()
                 or f_user == str(c.get("target_id"))]
    if f_action:
        items = [c for c in items if c.get("action") == f_action]
    if f_status:
        items = [c for c in items if c.get("status") == f_status]
    actions = sorted({c.get("action", "?") for c in cases})
    filt = (
        f"<form method=get action='/'>"
        f"<input type=hidden name=key value='{urlkey}'>"
        f"<div class=row><label>Filters<small>Search history.</small></label><span>"
        f"<input type=text name=case_user placeholder='user or ID' value='{D._esc(f_user)}' size=12> "
        f"<select name=case_action><option value=''>all actions</option>"
        + "".join(f"<option value='{a}'{' selected' if f_action == a else ''}>{a}</option>" for a in actions)
        + f"</select> <select name=case_status><option value=''>all statuses</option>"
        + "".join(f"<option value='{s}'{' selected' if f_status == s else ''}>{s}</option>"
                  for s in ("open", "actioned", "appealed", "closed"))
        + f"</select> <button>Filter</button> <a href='/?key={urlkey}#s{gid}-cases'>"
        f"<button type=button class=dim>Clear</button></a></span></div></form>")
    rows = ""
    for c in items[:100]:
        m = guild.get_member(int(c.get("target_id", 0))) if str(c.get("target_id", "")).isdigit() else None
        rows += (
            f"<div class=row><label><code>{c['id']}</code> "
            f"{D._esc(m.display_name) if m else 'left server'} — {D._esc(c.get('action', ''))}"
            f"<small>{D._esc((c.get('reason') or '')[:90])} · {c.get('status', 'open')} · "
            f"{time.strftime('%Y-%m-%d', time.gmtime(c.get('created', 0)))}</small></label>"
            f"<a href='/?key={urlkey}&case={c['id']}#s{gid}-cases'>"
            f"<button type=button class=dim>View</button></a></div>")
    if not rows:
        rows = ("<div class=empty><b>No cases match</b>"
                "<p>Cases appear automatically when staff moderate.</p></div>")
    return (D._card("Moderation cases", f"{len(items)} shown · {len(cases)} total. "
                                        " distinct from Logs: these are per-user records.",
                    filt + rows, f"c-{gid}-cases"))


def page_suggest(guild, cfg, urlkey):
    from flask import request as _rq
    gid = guild.id
    f_status = (_rq.args.get("sug_status", "") or "").strip()
    f_sort = (_rq.args.get("sug_sort", "top") or "").strip()
    f_q = (_rq.args.get("sug_q", "") or "").strip().lower()
    subs = C.DATA.get("suggestions.json", {}).get(str(gid), {})
    items = list(subs.values())
    if f_status in C.SUG_STATUS:
        items = [s for s in items if s.get("status") == f_status]
    if f_q:
        items = [s for s in items if f_q in (s.get("text", "") or "").lower()]
    scored = [(len(s.get("up", [])) - len(s.get("down", [])), s.get("created", 0), s) for s in items]
    if f_sort == "new":
        scored.sort(key=lambda t: t[1], reverse=True)
    elif f_sort == "old":
        scored.sort(key=lambda t: t[1])
    else:
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    ch = guild.get_channel(cfg.get("suggest_channel_id") or 0)
    filt = (
        f"<form method=get action='/'>"
        f"<input type=hidden name=key value='{urlkey}'>"
        f"<div class=row><label>Filters</label><span>"
        f"<select name=sug_status><option value=''>all statuses</option>"
        + "".join(f"<option value='{s}'{' selected' if f_status == s else ''}>{s}</option>"
                  for s in C.SUG_STATUS)
        + f"</select> <select name=sug_sort>"
        f"<option value='top'{' selected' if f_sort != 'new' and f_sort != 'old' else ''}>most popular</option>"
        f"<option value='new'{' selected' if f_sort == 'new' else ''}>newest</option>"
        f"<option value='old'{' selected' if f_sort == 'old' else ''}>oldest</option></select> "
        f"<input type=text name=sug_q placeholder='search' value='{D._esc(_rq.args.get('sug_q', ''))}' size=12> "
        f"<button>Filter</button></span></div></form>")
    setup = (
        f"<form method=post action='/api/config?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=key value='suggest_channel_id'>"
        f"<div class=row><label>Publish channel<small>New suggestions post here.</small></label>"
        f"<span><select name=value>{D._chan_opts(guild, cfg.get('suggest_channel_id'))}</select> "
        f"<button>Save</button></span></div></form>")
    rows = ""
    for score, _, s in scored[:100]:
        rows += (
            f"<div class=row><label><code>{s['id']}</code> {D._esc(s.get('text', '')[:120])}"
            f"<small>{score:+d} ({len(s.get('up', []))}👍/{len(s.get('down', []))}👎) · "
            f"{D._esc(s.get('author_name', ''))} · {s.get('status', 'pending')}</small></label>"
            f"<span><details><summary><button type=button class=dim>Review</button></summary>"
            f"<form method=post action='/api/community?key={urlkey}'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='sug_status'>"
            f"<input type=hidden name=id value='{s['id']}'>"
            f"<div class=row><label>Status</label><span><select name=to>"
            + "".join(f"<option value='{st}'{' selected' if s.get('status') == st else ''}>{st}</option>"
                      for st in C.SUG_STATUS)
            + f"</select> <button>Set</button></span></div></form>"
            f"<form method=post action='/api/community?key={urlkey}'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='sug_respond'>"
            f"<input type=hidden name=id value='{s['id']}'>"
            f"<div class=row><label>Staff response</label>"
            f"<span><input type=text name=text value='{D._esc(s.get('response', ''))}' size=24> "
            f"<button>Send</button></span></div></form>"
            f"</details></span></div>")
    if not rows:
        rows = ("<div class=empty><b>No suggestions yet</b>"
                "<p>Members can run `.suggest &lt;idea&gt;` in Discord.</p></div>")
    return (D._card("Suggestions",
                    f"Publish channel: {'#' + ch.name if ch else 'not set'} · "
                    "members vote with 👍👎 (one vote each).",
                    setup + filt + rows, f"c-{gid}-suggest"))


def _ann_form(guild, urlkey, a=None):
    a = a or {}
    gid = guild.id
    chans = "".join(f"<option value={c.id}{' selected' if a.get('channel_id') == c.id else ''}>"
                    f"#{D._esc(c.name)}</option>" for c in guild.text_channels[:30])
    dt = ""
    try:
        dt = time.strftime("%Y-%m-%dT%H:%M", time.localtime(a.get("send_at", 0))) if a.get("send_at") else ""
    except (ValueError, OSError, OverflowError):
        dt = ""
    recurs = int(a.get("recurs_s", 0) or 0)
    return (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='{'ann_update' if a.get('id') else 'ann_create'}'>"
        + (f"<input type=hidden name=id value='{a['id']}'>" if a.get("id") else "")
        + f"<div class=row><label>Title<small>Embed title (blank = plain message).</small></label>"
        f"<input type=text name=title value='{D._esc(a.get('title', ''))}' size=28></div>"
        f"<div class=row><label>Message<small>Up to 2000 chars.</small></label>"
        f"<input type=text name=body value='{D._esc(a.get('body', ''))}' size=40 required></div>"
        f"<div class=row><label>Channel</label>"
        f"<select name=channel>{chans}</select></div>"
        f"<div class=row><label>Send at (UTC)<small>Format: YYYY-MM-DDTHH:MM.</small></label>"
        f"<input type=text name=send_at value='{dt}' placeholder='2026-12-25T18:00' size=18></div>"
        f"<div class=row><label>Embed + repeat</label><span>"
        f"<label class=pill><input type=checkbox name=embed value=1{' checked' if a.get('embed') else ''}> embed</label> "
        f"<select name=recurs>"
        f"<option value=''{'' if recurs else ' selected'}>once</option>"
        f"<option value='daily'{' selected' if recurs == 86400 else ''}>daily</option>"
        f"<option value='weekly'{' selected' if recurs == 604800 else ''}>weekly</option>"
        f"</select></span></div>"
        f"<div class=row><label>Schedule</label>"
        f"<button class=primary>{'Save changes' if a.get('id') else '➕ Schedule'}</button></div>"
        f"</form>")


def page_announce(guild, cfg, urlkey):
    gid = guild.id
    items = sorted(C.DATA.get("announcements.json", {}).get(str(gid), []),
                   key=lambda a: (a.get("status") != "scheduled", a.get("send_at", 0)))
    groups = {}
    for a in items:
        groups.setdefault(a.get("status", "scheduled"), []).append(a)
    out = ""
    for st in ("scheduled", "sent", "failed", "cancelled"):
        rows = ""
        for a in groups.get(st, [])[:50]:
            ch = guild.get_channel(a.get("channel_id") or 0)
            when = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(a.get("send_at", 0)))
            sub = (f"{'#' + ch.name if ch else '?'} · {when}"
                   + (f" · 🔁 every {int(a['recurs_s'] // 86400) or 1}d" if a.get("recurs_s") else "")
                   + (f" · sent {a.get('sent_count', 0)}×" if a.get("sent_count") else "")
                   + (f" · ⚠ {a.get('last_error', '')[:80]}" if a.get("last_error") else ""))
            rows += (
                f"<div class=row><label>{D._esc((a.get('title') or a.get('body', ''))[:90])}"
                f"<small>{D._esc(sub)}</small></label>"
                f"<span><details><summary><button type=button class=dim>Manage</button></summary>"
                + _ann_form(guild, urlkey, a)
                + f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
                f"data-confirm='Duplicate this announcement?'>"
                f"<input type=hidden name=guild value={gid}>"
                f"<input type=hidden name=action value='ann_dup'>"
                f"<input type=hidden name=id value='{a['id']}'>"
                f"<button class=dim>Duplicate</button></form> "
                + (f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
                   f"data-confirm='Cancel this announcement?'>"
                   f"<input type=hidden name=guild value={gid}>"
                   f"<input type=hidden name=action value='ann_cancel'>"
                   f"<input type=hidden name=id value='{a['id']}'>"
                   f"<button class=danger>Cancel</button></form> " if st == "scheduled" else "")
                + (f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
                   f"data-confirm='Delete this record?'>"
                   f"<input type=hidden name=guild value={gid}>"
                   f"<input type=hidden name=action value='ann_delete'>"
                   f"<input type=hidden name=id value='{a['id']}'>"
                   f"<button class=danger>Delete</button></form>" if st != "scheduled" else "")
                + (f"<form method=post action='/api/community?key={urlkey}' style='display:inline'>"
                   f"<input type=hidden name=guild value={gid}>"
                   f"<input type=hidden name=action value='ann_reschedule'>"
                   f"<input type=hidden name=id value='{a['id']}'>"
                   f"<input type=text name=send_at placeholder='YYYY-MM-DDTHH:MM' size=16> "
                   f"<button>Reschedule</button></form>" if st in ("scheduled", "failed") else "")
                + f"</details></span></div>")
        out += D._card(st.title(), f"{len(groups.get(st, []))} announcement(s).",
                       rows or "<p><small>None.</small></p>", f"c-{gid}-ann-{st}")
    out += D._card("New announcement", "Drafts post exactly once unless repeating. Times are UTC.",
                   _ann_form(guild, urlkey), f"c-{gid}-annnew")
    return out


def temprole_card(guild, cfg, urlkey):
    gid = guild.id
    act = sorted([t for t in C.DATA.get("temproles.json", [])
                  if t.get("guild") == str(gid) and t.get("status") == "active"],
                 key=lambda t: t.get("expires_at", 0))
    rows = ""
    for t in act[:30]:
        m = guild.get_member(int(t.get("user_id", 0)))
        r = guild.get_role(int(t.get("role_id", 0)))
        left = max(0, int(t.get("expires_at", 0) - time.time()))
        h, rem = divmod(left, 3600)
        rows += (
            f"<div class=row><label>{D._esc(m.display_name) if m else 'left server'} → "
            f"<b>{D._esc(r.name) if r else '?'}</b>"
            f"<small>expires in {h}h {rem // 60}m (`{t['id']}`) · {D._esc(t.get('reason', ''))}</small></label>"
            f"<span><form method=post action='/api/community?key={urlkey}' style='display:inline'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='tr_extend'>"
            f"<input type=hidden name=id value='{t['id']}'>"
            f"<input type=text name=extra value='3600' size=6 title='extra seconds'>"
            f"<button class=dim>+ time</button></form> "
            f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
            f"data-confirm='Remove this role now?'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='tr_end'>"
            f"<input type=hidden name=id value='{t['id']}'>"
            f"<button class=danger>End now</button></form></span></div>")
    if not rows:
        rows = ("<div class=empty><b>No active temp roles</b>"
                "<p>Assign one below or with `.temprole @user @role 24h`.</p></div>")
    roles = "".join(
        f"<option value={r.id}>{D._esc(r.name)}</option>"
        for r in sorted(guild.roles, key=lambda r: r.position, reverse=True)
        if not r.is_default() and not r.managed)
    form = (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='tr_assign'>"
        f"<div class=row><label>User ID<small>Right-click user → Copy ID.</small></label>"
        f"<input type=text name=user_id size=20 required></div>"
        f"<div class=row><label>Role</label><select name=role_id>{roles}</select></div>"
        f"<div class=row><label>Duration<small>e.g. 30m, 2h, 7d (1m–30d).</small></label>"
        f"<input type=text name=dur placeholder='24h' size=10 required></div>"
        f"<div class=row><label>Reason</label>"
        f"<input type=text name=reason size=28 maxlength=200></div>"
        f"<div class=row><label>Assign</label><button class=primary>⏳ Assign temp role</button></div>"
        f"</form>")
    return (D._card("Temporary roles", f"{len(act)} active. Auto-removed on expiry, survives restarts.",
                    rows, f"c-{gid}-trlist")
            + D._card("Assign temp role", "Bot role must sit above the target role.",
                      form, f"c-{gid}-trnew"))


def page_backups(guild, cfg, urlkey):
    from flask import request as _rq
    gid = guild.id
    snaps = sorted(C.DATA.get("backups.json", {}).get(str(gid), []),
                   key=lambda s: s.get("created", 0), reverse=True)
    preview_id = (_rq.args.get("preview", "") or "").strip()
    preview = ""
    if preview_id:
        hit = next((s for s in snaps if s["id"] == preview_id), None)
        if hit is None:
            preview = "<div class=empty><b>Backup not found</b></div>"
        else:
            current = C.snapshot_backup(gid)
            allkeys = sorted(set(current["config"]) | set((hit.get("config") or {})))
            diffrows = ""
            for k in allkeys:
                old, new = current["config"].get(k, "—"), (hit.get("config") or {}).get(k, "—")
                if json.dumps(old, sort_keys=True, default=str) != json.dumps(new, sort_keys=True, default=str):
                    diffrows += (
                        f"<div class=row><label><code>{D._esc(k)}</code>"
                        f"<small>current → backup</small></label>"
                        f"<span class=pill>{D._esc(str(old)[:60])} → {D._esc(str(new)[:60])}</span></div>")
            ccc, ccn = current["customcmds"], hit.get("customcmds") or {}
            if set(ccc) != set(ccn):
                diffrows += (
                    f"<div class=row><label><code>custom_commands</code>"
                    f"<small>trigger sets differ</small></label>"
                    f"<span class=pill>{len(ccc)} now → {len(ccn)} in backup</span></div>")
            preview = D._card(f"Restore preview: {D._esc(hit.get('name', ''))}",
                              "Only differing settings are shown. Unknown keys are skipped, never applied.",
                              diffrows or "<p><small>No differences — restore would change nothing.</small></p>"
                              + f"<form method=post action='/api/community?key={urlkey}' "
                              f"data-confirm='Restore this backup? Current settings it replaces are overwritten.'>"
                              f"<input type=hidden name=guild value={gid}>"
                              f"<input type=hidden name=action value='backup_restore'>"
                              f"<input type=hidden name=id value='{hit['id']}'>"
                              f"<div class=row><label>Restore<small>Applies the snapshot.</small></label>"
                              f"<button class=danger>Restore backup</button></div></form>",
                              f"c-{gid}-bakpreview")
    rows = ""
    for s in snaps:
        rows += (
            f"<div class=row><label>{D._esc(s.get('name', ''))}"
            f"<small>{time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime(s.get('created', 0)))} · "
            f"{len(s.get('config', {}))} settings · {len((s.get('customcmds') or {}))} commands</small></label>"
            f"<span><a href='/?key={urlkey}&preview={s['id']}#s{gid}-backups'>"
            f"<button type=button class=dim>Preview</button></a> "
            f"<a href='/api/community/download?type=backup&id={s['id']}&guild={gid}&key={urlkey}'>"
            f"<button type=button class=dim>Download</button></a> "
            f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
            f"data-confirm='Delete this backup?'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='backup_delete'>"
            f"<input type=hidden name=id value='{s['id']}'>"
            f"<button class=danger>Delete</button></form></span></div>")
    if not rows:
        rows = ("<div class=empty><b>No backups yet</b>"
                "<p>Snapshots cover bot configuration + custom commands (never Discord messages).</p></div>")
    form = (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='backup_create'>"
        f"<div class=row><label>Name</label>"
        f"<span><input type=text name=name placeholder='Before big changes' size=24 maxlength=60> "
        f"<button class=primary>💾 Snapshot now</button></span></div></form>")
    return (D._card("Configuration backups", f"{len(snaps)} stored (max 10). Per-server isolated.",
                    form + rows, f"c-{gid}-backups") + preview)


def page_wizard(guild, cfg, urlkey):
    import security as _sec
    gid = guild.id
    done = set(cfg.get("wizard_done", []) or [])
    steps = []
    # 1. channels
    missing_ch = [label for key, label in (("log_channel_id", "Log channel"),
                                           ("welcome_channel_id", "Welcome channel"),
                                           ("verify_channel_id", "Verification channel"))
                  if not guild.get_channel(cfg.get(key) or 0)]
    steps.append({"id": "channels", "title": "Channels",
                  "desc": "Log, welcome and verification channels picked.",
                  "ok": not missing_ch,
                  "detail": "Missing: " + ", ".join(missing_ch) if missing_ch else "All three set.",
                  "anchor": f"c-{gid}-logs"})
    # 2. permissions via live diagnostics
    diags = _sec.diagnose(guild, cfg)
    crits = [d for d in diags if d["sev"] == "critical"]
    steps.append({"id": "perms", "title": "Bot permissions",
                  "desc": "The bot can actually do its jobs.",
                  "ok": not crits,
                  "detail": f"{len(crits)} critical finding(s)." if crits else "No critical findings.",
                  "anchor": f"c-{gid}-secdiag"})
    # 3. moderation defaults
    mod_ok = cfg.get("raid_action", "ban") != "none" or any(
        cfg.get(k) for k in ("automod_invites", "automod_links", "automod_spam",
                             "automod_caps", "automod_emoji"))
    steps.append({"id": "moderation", "title": "Moderation defaults",
                  "desc": "At least one protection is armed.",
                  "ok": bool(mod_ok),
                  "detail": "Raid response or an automod filter is on." if mod_ok else "Everything is off.",
                  "anchor": f"c-{gid}-raid"})
    # 4. verification
    v_ok = bool(cfg.get("verify_enabled") and guild.get_role(cfg.get("verified_role_id") or 0))
    steps.append({"id": "verify", "title": "Verification",
                  "desc": "Gate enabled with a valid role.",
                  "ok": v_ok,
                  "detail": "Enabled with role." if v_ok else "Enable it and pick a role.",
                  "anchor": f"c-{gid}-verify"})
    # 5. test message
    steps.append({"id": "testmsg", "title": "Test message",
                  "desc": "Prove the bot can write somewhere.",
                  "ok": "testmsg" in done,
                  "detail": "Send a test embed below.",
                  "anchor": f"c-{gid}-wizard"})
    rows = ""
    for s in steps:
        rows += (
            f"<div class=row><label>{D._esc(s['title'])}<small>{D._esc(s['desc'])} "
            f"{D._esc(s['detail'])}</small></label>"
            f"<span><form method=post action='/api/community?key={urlkey}' style='display:inline'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='wizard_done'>"
            f"<input type=hidden name=step value='{s['id']}'>"
            f"<input type=hidden name=done value=''>"
            f"<label class=pill title='Mark reviewed'><input type=checkbox "
            f"{'checked' if s['id'] in done else ''} onchange='this.form.done.value=this.checked?\"1\":\"\";this.form.submit()'> done</label>"
            f"</form> <a href='#{s['anchor']}'><button type=button class=dim>Open</button></a></span></div>")
    chans = "".join(f"<option value={c.id}>#{D._esc(c.name)}</option>" for c in guild.text_channels[:30])
    testform = (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='testmsg'>"
        f"<div class=row><label>Send test message<small>Harmless embed, proves write access.</small></label>"
        f"<span><select name=channel>{chans}</select> <button>Send test</button></span></div></form>")
    ndone = sum(1 for s in steps if s["id"] in done)
    return D._card("Setup wizard",
                   f"{ndone}/{len(steps)} steps marked done. Nothing here overwrites settings — "
                   "it only links, checks, and sends a test message you ask for.",
                   rows + testform, f"c-{gid}-wizard")


def _app_form_form(guild, urlkey, f=None):
    f = f or {}
    gid = guild.id
    chans = "".join(f"<option value={c.id}{' selected' if f.get('review_channel') == c.id else ''}>"
                    f"#{D._esc(c.name)}</option>" for c in guild.text_channels[:30])
    fields = ""
    for i in range(5):
        fld = (f.get("fields", []) + [{}] * 5)[i]
        fields += (
            f"<div class=row><label>Field {i + 1}</label><span>"
            f"<input type=text name=flabel{i} placeholder='Label' value='{D._esc(fld.get('label', ''))}' size=16> "
            f"<label class=pill><input type=checkbox name=freq{i} value=1"
            f"{' checked' if fld.get('required', True) else ''}> req</label> "
            f"<input type=text name=maxlen{i} value='{fld.get('maxlen', 500)}' size=5 title='max length'></span></div>")
    return (
        f"<form method=post action='/api/community?key={urlkey}'>"
        f"<input type=hidden name=guild value={gid}>"
        f"<input type=hidden name=action value='app_form_save'>"
        + (f"<input type=hidden name=id value='{f['id']}'>" if f.get("id") else "")
        + f"<div class=row><label>Name</label>"
        f"<input type=text name=name value='{D._esc(f.get('name', ''))}' size=24 maxlength=60 required></div>"
        f"<div class=row><label>Description</label>"
        f"<input type=text name=desc value='{D._esc(f.get('desc', ''))}' size=32 maxlength=500></div>"
        f"<div class=row><label>Review channel<small>Decisions post here.</small></label>"
        f"<select name=review_channel>{chans}</select></div>"
        + fields +
        f"<div class=row><label>Open<small>Accepting submissions.</small></label>"
        f"<input type=checkbox name=open value=1{' checked' if f.get('open', True) else ''}></div>"
        f"<div class=row><label>Save form</label><button class=primary>Save</button></div>"
        f"</form>")


def page_apps(guild, cfg, urlkey):
    from flask import request as _rq
    gid = guild.id
    forms = C.app_forms(gid)
    subs = C.app_subs(gid)
    f_detail = (_rq.args.get("appsub", "") or "").strip()
    f_status = (_rq.args.get("app_status", "") or "").strip()
    f_form = (_rq.args.get("app_form", "") or "").strip()
    if f_detail:
        s = subs.get(f_detail)
        if s is None or s.get("form_id") not in {f["id"] for f in forms} | {s.get("form_id")}:
            body = "<div class=empty><b>Submission not found</b></div>"
        else:
            form = next((x for x in forms if x["id"] == s.get("form_id")),
                        {"name": "Deleted form", "fields": []})
            m = guild.get_member(int(s.get("user_id", 0))) if str(s.get("user_id", "")).isdigit() else None
            answers = "".join(
                f"<div class=row><label>{D._esc((form.get('fields', []) + [{}] * 5)[i].get('label', f'Q{i + 1}'))}</label>"
                f"<span class=pill>{D._esc(a)[:300] or '—'}</span></div>"
                for i, a in sorted(((int(k), v) for k, v in (s.get("answers", {}) or {}).items()
                                           if k.isdigit()), key=lambda t: t[0]))
            notes = "".join(f"<div class=row><label>Note<small>{time.strftime('%Y-%m-%d', time.gmtime(n.get('t', 0)))}</small></label>"
                            f"<span class=pill>{D._esc(n.get('text', ''))}</span></div>"
                            for n in (s.get("notes", []) or [])[-10:]) or "<p><small>No notes.</small></p>"
            body = (
                f"<div class=row><label>Applicant</label>"
                f"<span class=pill>{D._esc(m.display_name) if m else 'left server'} "
                f"(`{D._esc(s.get('user_id', ''))}`)</span></div>"
                f"<div class=row><label>Status</label><span>"
                + "".join(f"<form method=post action='/api/community?key={urlkey}' style='display:inline'>"
                          f"<input type=hidden name=guild value={gid}>"
                          f"<input type=hidden name=action value='app_decide'>"
                          f"<input type=hidden name=id value='{s['id']}'>"
                          f"<input type=hidden name=to value='{st}'>"
                          f"<button class={'primary' if s.get('status') == st else 'dim'}>{st}</button></form> "
                          for st in ("pending", "approved", "rejected", "info"))
                + "</span></div>" + answers
                + f"<h3>Reviewer notes (private)</h3>{notes}"
                f"<form method=post action='/api/community?key={urlkey}'>"
                f"<input type=hidden name=guild value={gid}>"
                f"<input type=hidden name=action value='app_note'>"
                f"<input type=hidden name=id value='{s['id']}'>"
                f"<div class=row><label>Add note</label>"
                f"<span><input type=text name=text size=30 maxlength=500> <button>Note</button></span></div></form>")
        return D._card(f"Submission {D._esc(f_detail[:12])}", "Private review view.",
                       body + f"<p><a href='/?key={urlkey}#s{gid}-apply'>← Back to applications</a></p>",
                       f"c-{gid}-appsub")
    items = sorted(subs.values(), key=lambda s: s.get("created", 0), reverse=True)
    if f_status in ("pending", "approved", "rejected", "info"):
        items = [s for s in items if s.get("status") == f_status]
    if f_form:
        items = [s for s in items if s.get("form_id") == f_form]
    filt = (
        f"<form method=get action='/'>"
        f"<input type=hidden name=key value='{urlkey}'>"
        f"<div class=row><label>Filters</label><span>"
        f"<select name=app_form><option value=''>all forms</option>"
        + "".join(f"<option value='{f['id']}'{' selected' if f_form == f['id'] else ''}>"
                  f"{D._esc(f.get('name', ''))}</option>" for f in forms)
        + f"</select> <select name=app_status><option value=''>all statuses</option>"
        + "".join(f"<option value='{s}'{' selected' if f_status == s else ''}>{s}</option>"
                  for s in ("pending", "approved", "rejected", "info"))
        + f"</select> <button>Filter</button></span></div></form>")
    rows = ""
    for s in items[:100]:
        m = guild.get_member(int(s.get("user_id", 0))) if str(s.get("user_id", "")).isdigit() else None
        form = next((x for x in forms if x["id"] == s.get("form_id")), {})
        rows += (
            f"<div class=row><label><code>{s['id']}</code> "
            f"{D._esc(m.display_name) if m else 'left server'} → {D._esc(form.get('name', '?'))}"
            f"<small>{s.get('status', 'pending')} · "
            f"{time.strftime('%Y-%m-%d', time.gmtime(s.get('created', 0)))}</small></label>"
            f"<a href='/?key={urlkey}&appsub={s['id']}#s{gid}-apply'>"
            f"<button type=button class=dim>Review</button></a></div>")
    if not rows:
        rows = ("<div class=empty><b>No submissions</b>"
                "<p>Members apply with `.apply` in Discord.</p></div>")
    frows = ""
    for f in forms:
        n = sum(1 for s in subs.values() if s.get("form_id") == f["id"] and s.get("status") == "pending")
        frows += (
            f"<div class=row><label>{D._esc(f.get('name', ''))}"
            f"<small>{len(f.get('fields', []))} fields · {n} pending · "
            f"{'open' if f.get('open', True) else 'closed'}</small></label>"
            f"<span><details><summary><button type=button class=dim>Edit</button></summary>"
            + _app_form_form(guild, urlkey, f)
            + f"</details> <form method=post action='/api/community?key={urlkey}' style='display:inline'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='app_form_toggle'>"
            f"<input type=hidden name=id value='{f['id']}'>"
            f"<button class=dim>{'Close' if f.get('open', True) else 'Open'}</button></form> "
            f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
            f"data-confirm='Delete this form and its submissions?'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='app_form_delete'>"
            f"<input type=hidden name=id value='{f['id']}'>"
            f"<button class=danger>Delete</button></form></span></div>")
    return (D._card("Application forms", f"{len(forms)} form(s). Members apply with `.apply`.",
                    frows + f"<details><summary><button type=button class=primary>➕ New form</button></summary>"
                    + _app_form_form(guild, urlkey) + "</details>", f"c-{gid}-appforms")
            + D._card("Submissions", f"{len(items)} shown. Private to staff.",
                      filt + rows, f"c-{gid}-appsubs"))


def page_events(guild, cfg, urlkey):
    gid = guild.id
    now = time.time()
    items = C.ev_list(gid)
    upcoming = sorted([e for e in items if e.get("status") == "active" and e.get("starts_at", 0) > now],
                      key=lambda e: e.get("starts_at", 0))
    past = sorted([e for e in items if not (e.get("status") == "active" and e.get("starts_at", 0) > now)],
                  key=lambda e: e.get("starts_at", 0), reverse=True)[:20]
    chans = "".join(f"<option value={c.id}>#{D._esc(c.name)}</option>" for c in guild.text_channels[:30])

    def evform(e=None):
        e = e or {}
        dt = ""
        try:
            dt = time.strftime("%Y-%m-%dT%H:%M", time.localtime(e.get("starts_at", 0))) if e.get("starts_at") else ""
        except (ValueError, OSError, OverflowError):
            dt = ""
        return (
            f"<form method=post action='/api/community?key={urlkey}'>"
            f"<input type=hidden name=guild value={gid}>"
            f"<input type=hidden name=action value='event_save'>"
            + (f"<input type=hidden name=id value='{e['id']}'>" if e.get("id") else "")
            + f"<div class=row><label>Title</label>"
            f"<input type=text name=title value='{D._esc(e.get('title', ''))}' size=28 maxlength=200 required></div>"
            f"<div class=row><label>Description</label>"
            f"<input type=text name=desc value='{D._esc(e.get('desc', ''))}' size=36 maxlength=2000></div>"
            f"<div class=row><label>Channel</label>"
            f"<select name=channel>{''.join(f'<option value={c.id}' + (' selected' if e.get('channel_id') == c.id else '') + f'>#{D._esc(c.name)}</option>' for c in guild.text_channels[:30])}</select></div>"
            f"<div class=row><label>Starts (UTC)<small>YYYY-MM-DDTHH:MM.</small></label>"
            f"<input type=text name=starts_at value='{dt}' placeholder='2026-12-25T18:00' size=18></div>"
            f"<div class=row><label>Cap / reminders<small>0 = no cap. Minutes before, comma separated.</small></label>"
            f"<span><input type=text name=limit value='{int(e.get('limit', 0) or 0)}' size=5> "
            f"<input type=text name=remind value='{D._esc(','.join(str(x) for x in e.get('remind_mins', [60])))}' size=10></span></div>"
            f"<div class=row><label>Save</label>"
            f"<button class=primary>{'Save changes' if e.get('id') else '➕ Create event'}</button></div>"
            f"</form>")

    def evrow(e):
        going = sum(1 for v in (e.get("rsvps", {}) or {}).values() if v == "going")
        ch = guild.get_channel(e.get("channel_id") or 0)
        ctl = ""
        if e.get("status") == "active" and e.get("starts_at", 0) > now:
            ctl = (f"<form method=post action='/api/community?key={urlkey}' style='display:inline'>"
                   f"<input type=hidden name=guild value={gid}>"
                   f"<input type=hidden name=action value='event_publish'>"
                   f"<input type=hidden name=id value='{e['id']}'>"
                   f"<button class=dim>Publish now</button></form> "
                   f"<form method=post action='/api/community?key={urlkey}' style='display:inline' "
                   f"data-confirm='Cancel this event?'>"
                   f"<input type=hidden name=guild value={gid}>"
                   f"<input type=hidden name=action value='event_cancel'>"
                   f"<input type=hidden name=id value='{e['id']}'>"
                   f"<button class=danger>Cancel</button></form> ")
        return (
            f"<div class=row><label>{D._esc(e.get('title', ''))}"
            f"<small><t:{int(e.get('starts_at', 0))}:F> · #{ch.name if ch else '?'} · "
            f"{going} going · {e.get('status', '')}</small></label>"
            f"<span>{ctl}<details><summary><button type=button class=dim>Edit</button></summary>"
            + evform(e) + "</details></span></div>")

    up = "".join(evrow(e) for e in upcoming) or ("<div class=empty><b>No upcoming events</b>"
                                                 "<p>Create one below.</p></div>")
    down = "".join(evrow(e) for e in past) or "<p><small>No past events.</small></p>"
    return (D._card("Upcoming events", f"{len(upcoming)} scheduled. RSVP via Discord buttons.",
                    up, f"c-{gid}-evup")
            + D._card("New event", "Times are UTC. Reminders + start notice post automatically.",
                      evform(), f"c-{gid}-evnew")
            + D._card("Past & cancelled", "Last 20.", down, f"c-{gid}-evpast"))
