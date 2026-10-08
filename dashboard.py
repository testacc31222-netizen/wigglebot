"""Wigglesworth web dashboard — premium dark UI, all features.

Runs beside the bot on Railway. Guarded by DASHBOARD_KEY (?key=...).
"""
from __future__ import annotations

import os
import threading
import time

_STARTED = time.time()

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
    "verify_channel_id": ("channel", "Verification channel"),
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
    "link_allowed_channels": ("channellist", "Link-safe channels"),
    "lobby_channel_id": ("voice", "Join-to-create VC"),
    "chat_channel_id": ("channel", "Bot chat channel (blank = anywhere)"),
    "chat_enabled": ("bool", "Chatbot replies"),
    "bot_mood": ("mood", "Chatbot mood"),
    "autoreact": ("bool", "Auto reactions"),
    "qotd_channel_id": ("channel", "Question-of-the-day channel"),
    "abuse_role_id": ("role", "Abuse ping role (.abuse pings this)"),
    "abuse_channel_id": ("channel", "Abuse alert channel (blank = where used)"),
    "join_threshold_count": ("int", "Raid: joins to trigger"),
    "join_threshold_seconds": ("int", "Raid: within seconds"),
    "raid_action": ("action", "Raid response"),
    "lockdown_duration_minutes": ("int", "Auto-unlock minutes (0 = manual)"),
    "new_account_age_days": ("int", "Flag accounts younger than (days)"),
    "timeout_duration_minutes": ("int", "Mute length minutes"),
}

SECTIONS = [
    ("🛡️ Verification", ["verify_enabled", "verified_role_id", "verify_channel_id"]),
    ("🎭 Reaction roles", []),
    ("🚨 Raid guard", ["join_threshold_count", "join_threshold_seconds", "raid_action",
                       "lockdown_duration_minutes", "new_account_age_days",
                       "timeout_duration_minutes"]),
    ("🚨 Abuse ping", ["abuse_role_id", "abuse_channel_id"]),
    ("🔐 Lockdown", []),
    ("📋 Whitelist", []),
    ("🧹 Mod actions", []),
    ("👋 Welcome", ["welcome_channel_id", "goodbye_channel_id"]),
    ("📝 Logging", ["log_channel_id"]),
    ("🤖 Automod", ["automod_invites", "automod_links", "automod_max_mentions",
                    "automod_spam", "automod_caps", "automod_emoji", "automod_max_emoji",
                    "automod_words", "link_allowed_channels"]),
    ("🔊 Voice lobby", ["lobby_channel_id"]),
    ("🎧 Voice presence", []),
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
background:#1B2224;border:1px solid #283033}
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
.dot.g{background:#3fb96f}.dot.p{background:#27C4CC}.dot.gr{background:#55556a}
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
.lb .fill{height:100%;background:#27C4CC;border-radius:6px;
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
.heat i.l1{background:#134E4A}.heat i.l2{background:#0F766E}.heat i.l3{background:#14B8A6}
.heat i.l4{background:#2DD4BF}.heat i.l5{background:#5EEAD4}
form{margin:0}.footer{text-align:center;color:#55556e;font-size:12px;padding:22px}
/* ===== Wigglesworth control-center theme ===== */
:root{--bg:#0B0F10;--bg2:#0D1112;--card:#101516;--line:#283033;--line2:#283033;
--txt:#E6EAEB;--mut:#A0ADB2;--mut2:#718087;--acc:#27C4CC;--accsoft:rgba(39,196,204,.10);
--grn:#4ade80;--red:#f87171}
body{background:var(--bg)}
.app{display:flex;min-height:100vh}
.sidebar{width:212px;flex:0 0 212px;position:sticky;top:0;height:100vh;overflow-y:auto;
background:#0D1112;border-right:1px solid var(--line);padding:16px 10px;display:flex;flex-direction:column;gap:2px}
.sbrand{display:flex;align-items:center;gap:10px;padding:4px 8px 14px}
.sbrand .orb{width:32px;height:32px;border-radius:50%;flex:0 0 32px;
background:#1B2224;border:1px solid #283033}
.sbrand b{font-size:14px;display:block}.sbrand small{color:var(--mut);font-size:11px;display:block}
.snavlabel{font-size:10px;letter-spacing:1.2px;color:var(--mut2);padding:10px 10px 3px;font-weight:800}
.snav{display:flex;align-items:center;gap:9px;color:#A0ADB2;text-decoration:none;font-size:13px;
padding:7px 10px;border-radius:8px;border-left:2px solid transparent}
.snav .ic{width:18px;text-align:center}
.snav:hover{background:#141A1C;color:#fff}
.snav.active{background:var(--accsoft);border-left-color:var(--acc);color:#fff}
.servers{margin-top:auto;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:6px}
.srv{display:flex;align-items:center;gap:9px;padding:7px 10px;border-radius:8px;color:#cfcfe0;text-decoration:none;font-size:12.5px}
.srv:hover{background:#141A1C}
.srv img,.srv .noav{width:26px;height:26px;border-radius:50%;flex:0 0 26px}
.srv .noav{background:#1B2224;border:1px solid var(--line);display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff}
.main{flex:1;min-width:0;padding:0 24px 40px;max-width:1180px}
.topbar2{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;padding:12px 0;
background:var(--bg)}
.topbar2 .crumb{font-size:12px;color:var(--mut2)}
.topbar2 h1{font-size:17px;font-weight:700}
.tspace{flex:1}
.iconbtn{width:36px;height:36px;border-radius:10px;background:var(--card);border:1px solid var(--line);
color:#cfcfe0;font-size:15px;cursor:pointer;display:flex;align-items:center;justify-content:center}
.iconbtn:hover{border-color:var(--acc);color:#fff}
.searchbox{background:var(--card);border:1px solid var(--line);border-radius:10px;color:#eee;
padding:9px 12px;font-size:13px;width:210px}
.searchbox:focus{outline:none;border-color:var(--acc)}
.statusdot{display:flex;align-items:center;gap:7px;font-size:12px;color:var(--mut);
background:var(--card);border:1px solid var(--line);border-radius:20px;padding:7px 13px}
.statusdot i{width:8px;height:8px;border-radius:50%;background:var(--grn);box-shadow:0 0 8px var(--grn)}
.statusdot.down i{background:var(--red);box-shadow:0 0 8px var(--red)}
.burger{display:none}
.page{scroll-margin-top:76px}
.pagehead{display:flex;align-items:baseline;gap:12px;margin:26px 2px 12px;flex-wrap:wrap}
.pagehead h2{font-size:22px;font-weight:800}
.pagehead p{color:var(--mut);font-size:13px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.gridstats{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 12px}
.card h2{font-size:14px;font-weight:700;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.card .sub{color:var(--mut);font-size:12px;margin-bottom:8px}
.card h3{margin:14px 0 4px;font-size:11px;color:var(--mut);text-transform:uppercase;letter-spacing:.8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}
.stat .lab{font-size:10.5px;letter-spacing:1px;color:var(--mut);font-weight:700;display:flex;gap:6px;align-items:center}
.stat .num{font-size:24px;font-weight:700;letter-spacing:-.5px;margin:4px 0 2px}
.stat .tr{font-size:12px;color:var(--mut)}
.stat .tr.up{color:var(--grn)}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.row label{font-size:13px;color:var(--txt)}.row small{display:block;color:var(--mut);font-size:12px;font-weight:400}
.row form{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
button,select{background:#1B2224;color:var(--txt);border:1px solid var(--line);border-radius:8px;padding:7px 14px;font-weight:600;cursor:pointer;font-size:13px;transition:background .12s,border-color .12s}
button:hover{border-color:var(--acc)}
button:disabled{opacity:.6;cursor:wait;transform:none}
button.danger{background:rgba(248,113,113,.10);color:#f87171;border:1px solid rgba(248,113,113,.4)}
button.ok{background:rgba(74,222,128,.10);color:#4ade80;border:1px solid rgba(74,222,128,.35)}
button.dim{background:#1B2224;color:#cfcfe0}
button.primary{background:var(--acc);border-color:var(--acc);color:#06292b}
button.primary:hover{background:#2bd6dd}
select,input[type=text]{background:#0B0F10;color:#eee;border:1px solid var(--line2);border-radius:8px;padding:8px 10px;font-size:13px;max-width:100%}
select:focus,input[type=text]:focus{outline:none;border-color:var(--acc)}
button.sw{position:relative;width:42px;height:24px;border-radius:20px;padding:0;background:#2a3437;border:1px solid var(--line2);flex:0 0 auto}
button.sw .knob{position:absolute;top:2px;left:2px;width:18px;height:18px;border-radius:50%;background:#8b8b9e;transition:left .15s,background .15s}
button.sw.on{background:rgba(39,196,204,.35);border-color:var(--acc)}
button.sw.on .knob{left:20px;background:#fff}
.pill{display:inline-flex;align-items:center;gap:6px;background:#1B2224;border:1px solid var(--line);border-radius:8px;padding:3px 10px;margin:2px;font-size:12.5px}
.pill input{accent-color:var(--acc)}
.lockcard{border-color:rgba(244,63,94,.45)!important;background:linear-gradient(rgba(244,63,94,.06),transparent 60%),var(--card)}
.av{width:30px;height:30px;border-radius:50%;flex:0 0 30px}
.rank{width:24px;color:var(--mut);font-weight:800;font-size:13px;text-align:center}
#toasts{position:fixed;right:18px;bottom:18px;z-index:99;display:flex;flex-direction:column;gap:8px}
.toast{background:#171724;border:1px solid var(--line2);border-left:3px solid var(--acc);border-radius:12px;
padding:11px 16px;font-size:13px;box-shadow:0 14px 40px rgba(0,0,0,.5);animation:tin .25s ease}
.toast.ok{border-left-color:var(--grn)}.toast.err{border-left-color:var(--red)}
@keyframes tin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
@media(max-width:1100px){.gridstats{grid-template-columns:repeat(3,1fr)}}
@media(max-width:900px){
.grid2,.grid3{grid-template-columns:1fr}.gridstats{grid-template-columns:repeat(2,1fr)}
.sidebar{position:fixed;left:0;z-index:50;transform:translateX(-105%);transition:transform .22s}
.sidebar.open{transform:none;box-shadow:30px 0 80px rgba(0,0,0,.6)}
.burger{display:flex}.searchbox{display:none}.main{padding:0 14px 40px}.row{flex-direction:column;align-items:stretch}
.row form{justify-content:flex-start}}
.statpill{font-size:10.5px;font-weight:800;letter-spacing:.8px;border-radius:20px;padding:3px 12px;
background:rgba(34,197,94,.14);color:#4ade80;border:1px solid rgba(34,197,94,.35)}
.statpill.off{background:rgba(244,63,94,.12);color:#fda4af;border-color:rgba(244,63,94,.35)}
.statpill.warn{background:rgba(251,191,36,.12);color:#fbbf24;border-color:rgba(251,191,36,.35)}
.row.dirty label::after{content:' ●';color:#fbbf24;font-size:11px}
.empty{border:1px dashed var(--line2);border-radius:12px;padding:22px;text-align:center;color:var(--mut)}
.empty b{color:var(--txt);display:block;margin-bottom:4px}
.empty p{font-size:12.5px}
.modalback{position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:18px}
.modal{background:#151522;border:1px solid var(--line2);border-radius:16px;padding:22px;max-width:380px;width:100%;
box-shadow:0 30px 80px rgba(0,0,0,.6);animation:tin .2s ease}
.modal h3{font-size:15px;margin-bottom:8px}
.modal p{font-size:13px;color:var(--mut);margin-bottom:16px}
.modal .mrow{display:flex;gap:10px;justify-content:flex-end}
button:focus-visible,select:focus-visible,input:focus-visible,a:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
code{background:#1d1d2e;border-radius:6px;padding:1px 7px;font-size:12px}
.pill.hot{background:var(--accsoft);color:var(--acc);border-color:var(--acc)}
.sbrand .dlogo{width:32px;height:32px;border-radius:8px;flex:0 0 32px;background:#161C1E;
border:1px solid var(--line);display:flex;align-items:center;justify-content:center}
/* calm-theme overrides for legacy rules */
.lb{margin:7px 0}
.lb .track{height:6px;background:#1B2224;border-radius:4px}
.lb .fill{background:var(--acc);border-radius:4px;animation:none}
.lb .xp{color:var(--mut)}
.heat i{background:#1B2224;border-radius:4px;animation:none}
.heat i.l1{background:#134E4A}.heat i.l2{background:#0F766E}.heat i.l3{background:#14B8A6}
.heat i.l4{background:#2DD4BF}.heat i.l5{background:#5EEAD4}
.txn{border-top-color:var(--line)}
.txn .tag{background:#1B2224;color:var(--mut)}
.lockcard{border-color:rgba(248,113,113,.4)!important;background:var(--card)}
.toast{background:#101516;box-shadow:none}
.modal{background:#101516;box-shadow:none}
.statpill{background:rgba(74,222,128,.10);color:#4ade80;border-color:rgba(74,222,128,.3)}
.statpill.off{background:rgba(248,113,113,.10);color:#f87171;border-color:rgba(248,113,113,.3)}
.empty{border-color:var(--line)}
/* overview + charts */
.crumb{font-size:12px;color:var(--mut2);margin-bottom:2px}
.ovhead{display:flex;align-items:flex-end;gap:12px;flex-wrap:wrap;margin:2px 2px 12px}
.ovhead h1{font-size:20px;font-weight:700}
.ovhead p{color:var(--mut);font-size:13px}
.ovhead .sp{flex:1}
.updated{font-size:12px;color:var(--mut2)}
.rangebar{display:flex;gap:6px;flex-wrap:wrap;align-items:center}
.rangebar a{text-decoration:none}
.chart{width:100%;height:auto;display:block}
.chart .grid{stroke:#1E2629;stroke-width:1}
.chart .line{fill:none;stroke:var(--acc);stroke-width:2}
.chart .area{fill:rgba(39,196,204,.08);stroke:none}
.chart .alab{fill:#718087;font-size:10px}
.evrow{display:flex;align-items:center;gap:10px;padding:8px 0;border-top:1px solid var(--line);font-size:13px}
.evrow:first-of-type{border-top:0}
.evrow .dot{width:7px;height:7px;border-radius:50%;background:var(--acc);flex:0 0 7px}
.evrow .dot.warn{background:#fbbf24}.evrow .dot.bad{background:var(--red)}
.evrow .t{flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.evrow .when{color:var(--mut2);font-size:12px;white-space:nowrap}
"""

BASE = ("<!doctype html><html><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Wigglesworth Panel</title><style>" + CSS + "</style></head>"
        "<body><div class=app>%%SIDEBAR%%<div class=main>%%TOPBAR%%"
        "%%BODY%%"
        "<div class=footer>Wigglesworth Bot · keep your ?key= secret</div>"
        "</div></div><div id=toasts></div></body></html>")


def _page(body: str, sidebar: str = "", topbar: str = "") -> str:
    html = BASE.replace("%%BODY%%", body).replace("%%SIDEBAR%%", sidebar)
    html = html.replace("%%TOPBAR%%", topbar)
    return html.replace("</body>", JS2 + "</body>")




JS2 = '''
<script>
function toast(msg, kind, onTap) {
  var box = document.getElementById('toasts');
  if (!box) return null;
  var t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  if (onTap) {
    t.style.cursor = 'pointer';
    t.addEventListener('click', function() { t.remove(); onTap(); });
  }
  box.appendChild(t);
  setTimeout(function() { t.remove(); }, 3200);
  return t;
}
function clearDirty(f) {
  var row = f.closest ? f.closest('.row') : null;
  if (row) row.classList.remove('dirty');
}
document.addEventListener('input', function(e) {
  var f = e.target && e.target.closest ? e.target.closest('form[method=post]') : null;
  if (!f) return;
  var row = f.closest('.row');
  if (row) row.classList.add('dirty');
});
document.addEventListener('change', function(e) {
  var f = e.target && e.target.closest ? e.target.closest('form[method=post]') : null;
  if (!f) return;
  var row = f.closest('.row');
  if (row) row.classList.add('dirty');
});
document.addEventListener('submit', function(e) {
  var f = e.target;
  if (!f || f.tagName !== 'FORM' || (f.method || '').toLowerCase() !== 'post') return;
  e.preventDefault();
  if (f.getAttribute('data-confirm') && !f.dataset.confirmed) { openConfirm(f); return; }
  delete f.dataset.confirmed;
  var btn = f.querySelector('button[type=submit],button:not([type])');
  if (!btn) btn = f.querySelector('button');
  var isSwitch = btn && btn.classList.contains('sw');
  var orig = btn ? btn.innerHTML : '';
  if (btn) { btn.disabled = true; }
  fetch(f.action, {method: 'POST', body: new FormData(f), credentials: 'same-origin'})
    .then(function(r) { if (!r.ok) throw new Error('HTTP ' + r.status); })
    .then(function() {
      var path = '';
      try { path = new URL(f.action, location.origin).pathname; }
      catch (err) { path = f.getAttribute('action') || ''; }
      if (path.indexOf('/api/mod') === 0 || path.indexOf('/api/whitelist') === 0 ||
          path.indexOf('/api/rolemenu') === 0 || path.indexOf('/api/giveaway') === 0) {
        var card = f.closest('.card');
        if (card && card.id) { try { location.hash = card.id; } catch (err2) {} }
        location.reload();
        return;
      }
      if (isSwitch) {
        var on = btn.classList.contains('on');
        btn.classList.toggle('on', !on);
        var val = f.querySelector('input[name=value][type=hidden]');
        if (val) val.value = on ? '0' : '1';
        var badge = f.parentElement ? f.parentElement.querySelector('.badge') : null;
        if (badge) {
          badge.textContent = on ? 'OFF' : 'ON';
          badge.className = 'badge ' + (on ? 'off' : 'on');
        }
        btn.disabled = false;
        toast(on ? 'Turned off' : 'Turned on', 'ok');
        clearDirty(f);
        return;
      }
      toast('Saved', 'ok');
      clearDirty(f);
      if (btn) {
        btn.textContent = 'Saved';
        setTimeout(function() {
          if (btn.classList.contains('sw')) return;
          btn.innerHTML = orig; btn.disabled = false;
        }, 1200);
      }
    })
    .catch(function() {
      clearDirty(f);
      toast('Unable to save settings. Tap to retry.', 'err', function() {
        if (f.isConnected) f.requestSubmit();
      });
      if (btn) {
        if (isSwitch) { btn.disabled = false; }
        else { btn.textContent = 'Retry'; btn.disabled = false; }
      }
    });
});
document.addEventListener('click', function(e) {
  var b = e.target.closest && e.target.closest('.burger');
  if (b) {
    var sb = document.querySelector('.sidebar');
    if (sb) sb.classList.toggle('open');
  }
  var nb = e.target.closest && e.target.closest('.snav');
  if (nb) {
    var sb2 = document.querySelector('.sidebar');
    if (sb2 && window.innerWidth <= 900) sb2.classList.remove('open');
  }
  var eb = e.target.closest && e.target.closest('.menu-edit');
  if (eb) {
    var card = eb.closest('.card');
    var form = card ? card.querySelector('form.rolemenu-form') : null;
    if (!form) return;
    var act = form.querySelector('input[name=action]');
    var idx = form.querySelector('input[name=idx]');
    if (act) act.value = 'edit';
    if (idx) idx.value = eb.getAttribute('data-idx') || '';
    var want = (eb.getAttribute('data-roles') || '').split(',').filter(Boolean);
    form.querySelectorAll('input[name=roles]').forEach(function(cb) {
      cb.checked = want.indexOf(cb.value) !== -1;
    });
    var ch = form.querySelector('select[name=channel]');
    if (ch) ch.value = eb.getAttribute('data-channel') || ch.value;
    var sub = form.querySelector('button[type=submit],button:not([type])') || form.querySelector('button');
    if (sub) sub.textContent = 'Save edits';
    form.scrollIntoView({behavior: 'smooth', block: 'center'});
    toast('Editing menu — tweak roles, then Save edits');
  }
});
(function() {
  var links = Array.prototype.slice.call(document.querySelectorAll('.snav[data-spy]'));
  if (!links.length || !('IntersectionObserver' in window)) return;
  var map = {};
  links.forEach(function(a) { map[a.getAttribute('href').slice(1)] = a; });
  var obs = new IntersectionObserver(function(es) {
    es.forEach(function(en) {
      if (en.isIntersecting && map[en.target.id]) {
        links.forEach(function(a) { a.classList.remove('active'); });
        map[en.target.id].classList.add('active');
      }
    });
  }, {rootMargin: '-30% 0px -60% 0px'});
  Object.keys(map).forEach(function(id) {
    var el = document.getElementById(id);
    if (el) obs.observe(el);
  });
})();
(function() {
  var box = document.getElementById('dashsearch');
  if (!box) return;
  box.addEventListener('input', function() {
    var q = box.value.trim().toLowerCase();
    document.querySelectorAll('.card[data-search]').forEach(function(card) {
      if (!q) { card.style.display = ''; return; }
      var hit = (card.getAttribute('data-search') || '').toLowerCase().indexOf(q) !== -1;
      card.style.display = hit ? '' : 'none';
    });
  });
  box.addEventListener('keydown', function(ev) {
    if (ev.key === 'Enter') {
      ev.preventDefault();
      var first = null;
      document.querySelectorAll('.card[data-search]').forEach(function(card) {
        if (!first && card.style.display !== 'none') first = card;
      });
      if (first) first.scrollIntoView({behavior: 'smooth', block: 'start'});
    }
  });
})();
function openConfirm(f) {
  var old = document.querySelector('.modalback');
  if (old) old.remove();
  var back = document.createElement('div');
  back.className = 'modalback';
  var msg = f.getAttribute('data-confirm') || 'Are you sure?';
  back.innerHTML = '<div class=modal><h3>Please confirm</h3><p></p>'
    + '<div class=mrow><button type=button class=dim>Cancel</button>'
    + '<button type=button class=danger>Confirm</button></div></div>';
  back.querySelector('p').textContent = msg;
  var btns = back.querySelectorAll('button');
  btns[0].addEventListener('click', function() { back.remove(); });
  back.addEventListener('click', function(ev) { if (ev.target === back) back.remove(); });
  btns[1].addEventListener('click', function() {
    back.remove();
    f.dataset.confirmed = '1';
    f.requestSubmit();
  });
  document.body.appendChild(back);
  btns[1].focus();
}
document.addEventListener('click', function(e) {
  var db = e.target.closest && e.target.closest('.menu-dup');
  if (!db) return;
  var card = db.closest('.card');
  var form = card ? card.querySelector('form.rolemenu-form') : null;
  if (!form) return;
  var act = form.querySelector('input[name=action]');
  var idx = form.querySelector('input[name=idx]');
  if (act) act.value = 'duplicate';
  if (idx) idx.value = db.getAttribute('data-idx') || '';
  form.requestSubmit();
});
</script>
'''


NAV = [
    ("OVERVIEW", [("🏠", "Dashboard", "overview")]),
    ("MODERATION", [("🤖", "Automod", "automod"), ("🚩", "Abuse Protection", "abuse"),
                   ("🚨", "Raid Protection", "raid"), ("📝", "Logs", "logs")]),
    ("SERVER", [("🛡️", "Verify", "verify"), ("🎭", "Roles", "roles"),
               ("💬", "Chat", "chat")]),
    ("COMMUNITY", [("🎁", "Giveaways", "giveaways")]),
    ("ANALYTICS", [("📊", "Analytics", "analytics")]),
    ("BOT", [("⚙️", "Settings", "settings")]),
]


def _sidebar(g0, guilds):
    links = []
    for label, items in NAV:
        links.append(f"<div class=snavlabel>{label}</div>")
        for ic, name, anchor in items:
            if anchor == "settings":
                href = "#settings"
            else:
                href = f"#s{g0.id}-{anchor}" if g0 else f"#{anchor}"
            links.append(f"<a class=snav data-spy href='{href}'><span class=ic>{ic}</span>{name}</a>")
    srvs = []
    for g in guilds:
        icon = f"<img src='{g.icon.url}'>" if getattr(g, "icon", None) else \
            f"<span class=noav>{_esc((g.name or '?')[:1])}</span>"
        srvs.append(f"<a class=srv href='#srv-{g.id}'>{icon}<span>{_esc(g.name)}</span></a>")
    return ("<aside class=sidebar><div class=sbrand><span class=dlogo>"
            "<svg viewBox='0 0 24 24' width='20' height='20' fill='#fff'>"
            "<path d='M20.317 4.3698a19.7913 19.7913 0 00-4.8851-1.5152.0741.0741 0 "
            "00-.0785.0371c-.211.3753-.4447.8648-.6083 1.2495-1.8447-.2762-3.68-.2762-5.4868 "
            "0-.1636-.3933-.4058-.8742-.6177-1.2495a.077.077 0 00-.0785-.037 19.7363 19.7363 0 "
            "00-4.8852 1.515.0699.0699 0 00-.0321.0277C.5334 9.0458-.319 13.5799.0992 "
            "18.0578a.0824.0824 0 00.0312.0561c2.0528 1.5076 4.0414 2.4228 5.9929 "
            "3.0294a.0777.0777 0 00.0842-.0276c.4616-.6304.8731-1.2952 1.226-1.9942a.076.076 "
            "0 00-.0416-.1057c-.6528-.2476-1.2743-.5495-1.8722-.8923a.077.077 "
            "0 01-.0076-.1277c.1258-.0943.2517-.1923.3718-.2914a.0743.0743 0 "
            "01.0776-.0105c3.9278 1.7933 8.18 1.7933 12.0614 0a.0739.0739 0 "
            "01.0785.0095c.1202.099.246.1981.3728.2924a.077.077 0 01-.0066.1276 12.2986 "
            "12.2986 0 01-1.873.8914.0766.0766 0 00-.0407.1067c.3604.698.7719 1.3628 "
            "1.225 1.9932a.076.076 0 00.0842.0286c1.961-.6067 3.9495-1.5219 "
            "6.0023-3.0294a.077.077 0 00.0313-.0552c.5004-5.177-.8382-9.6739-3.5485-13.6604a.061.061 "
            "0 00-.0312-.0286zM8.02 15.3312c-1.183 0-2.1569-1.0857-2.1569-2.419 "
            "0-1.3332.9555-2.4189 2.157-2.4189 1.2108 0 2.176 1.0952 2.1568 2.419 0 "
            "1.3332-.9555 2.4189-2.1569 2.4189zm7.9748 0c-1.183 0-2.1569-1.0857-2.1569-2.419 "
            "0-1.3332.9554-2.4189 2.1569-2.4189 1.2108 0 2.176 1.0952 2.1568 2.419 0 "
            "1.3332-.946 2.4189-2.1568 2.4189Z'/></svg></span>"
            "<span><b>Wigglesworth</b><small>Discord Bot</small></span></div>"
            + "".join(links) +
            "<div class=snavlabel>SHORTCUTS</div>"
            "<a class=snav href='https://heh.wwiggles.org/dashboard/#overview' target=_blank rel=noopener>"
            "<span class=ic>🌐</span>WiggleGate</a>"
            "<a class=snav href='https://railway.com/project/9cbe96a0-7d1c-4b4f-b649-7cab2ee543b8/service/0fe7242b-c24e-4247-9025-28997af0c339?environmentId=dfdf3ff0-190e-4dc7-befa-4e62d5a2fbb6' target=_blank rel=noopener>"
            "<span class=ic>🚂</span>Railway Service</a>"
            "<div class=servers><div class=snavlabel>Servers</div>" + "".join(srvs) + "</div></aside>")


def _topbar(connected, n_guilds):
    dot = "" if connected else " down"
    txt = f"Connected · {n_guilds} server{'s' if n_guilds != 1 else ''}" if connected else "Bot offline"
    return ("<div class=topbar2><button class='iconbtn burger' aria-label=menu>☰</button>"
            "<div><div class=crumb>Wigglesworth / Panel</div><h1>Dashboard</h1></div>"
            "<div class=tspace></div>"
            "<input id=dashsearch class=searchbox placeholder='Search settings…'>"
            f"<span class='statusdot{dot}'><i></i>{txt}</span></div>")


def _role_opts(guild, current):
    out = ["<option value=''>— none —</option>"]
    for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if r.is_default() or r.managed:
            continue
        sel = " selected" if current == r.id else ""
        out.append(f"<option value={r.id}{sel}>{_esc(r.name)}</option>")
    return "".join(out)


def _chan_opts(guild, current, voice=False):
    out = ["<option value=''>— none —</option>"]
    chans = guild.voice_channels if voice else guild.text_channels
    for c in chans[:30]:
        sel = " selected" if current == c.id else ""
        out.append(f"<option value={c.id}{sel}>#{_esc(c.name)}</option>")
    return "".join(out)


def _esc(s) -> str:
    import html as _h
    return _h.escape(str(s if s is not None else ""), quote=True)


DESCRIPTIONS = {
    "verify_enabled": "New members must verify before chatting.",
    "verified_role_id": "Role granted after verification.",
    "verify_channel_id": "Channel where users verify.",
    "join_threshold_count": "Joins that trigger raid mode.",
    "join_threshold_seconds": "Time window for counting joins.",
    "raid_action": "What happens to raiders. The initiator is always banned.",
    "lockdown_duration_minutes": "Auto-unlock delay. 0 = unlock manually.",
    "new_account_age_days": "Flag accounts younger than this.",
    "timeout_duration_minutes": "Mute length for timeouts.",
    "log_channel_id": "Raid hits and mod actions land here.",
    "automod_invites": "Delete Discord invite links.",
    "automod_links": "Delete all links server-wide.",
    "automod_max_mentions": "Max @mentions per message.",
    "automod_spam": "Repeated messages trigger the filter.",
    "automod_caps": "Deletes mostly-CAPS messages.",
    "automod_emoji": "Deletes emoji-spam messages.",
    "automod_max_emoji": "Max emojis per message.",
    "automod_words": "Comma-separated banned words.",
    "link_allowed_channels": "Links stay allowed in these channels.",
    "lobby_channel_id": "Join to create a temporary VC.",
    "chat_channel_id": "Blank = the bot chats anywhere.",
    "chat_enabled": "Let members chat with the bot.",
    "bot_mood": "Bot personality. Takes effect instantly.",
    "autoreact": "React to messages automatically.",
    "qotd_channel_id": "Daily question posts here.",
    "abuse_role_id": "Role pinged by .abuse.",
    "abuse_channel_id": "Blank = wherever .abuse is used.",
}


def _field(key, kind, label, guild, cfg, urlkey):
    _desc = DESCRIPTIONS.get(key, "")
    if _desc:
        label = label + f"<small>{_desc}</small>"
    if kind == "bool":
        state = bool(cfg.get(key))
        return (f"<div class=row><label>{label}<br><span class='badge {('on' if state else 'off')}'>"
                f"{'ON' if state else 'OFF'}</span></label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=hidden name=value value={'0' if state else '1'}>"
                f"<button class='sw {('on' if state else '')}' aria-label='toggle {label}' title='toggle {label}'><span class=knob></span></button></form></div>")
    if kind == "int":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=text name=value value='{_esc(cfg.get(key) or '')}' size=6>"
                f"<button>Save</button></form></div>")
    if kind == "words":
        cur = ", ".join(cfg.get(key) or [])
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<input type=text name=value value='{_esc(cur)}' size=24>"
                f"<button>Save</button></form></div>")
    if kind == "channellist":
        cur = set(cfg.get(key) or [])
        boxes = "".join(
            f"<label class=pill><input type=checkbox name=value value={c.id}"
            f"{' checked' if c.id in cur else ''}> #{_esc(c.name)}</label>"
            for c in guild.text_channels[:30])
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"{boxes}<br><button>Save</button></form></div>")
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
    if kind == "voice":
        return (f"<div class=row><label>{label}</label>"
                f"<form method=post action='/api/config?key={urlkey}'>"
                f"<input type=hidden name=guild value={guild.id}>"
                f"<input type=hidden name=key value={key}>"
                f"<select name=value>{_chan_opts(guild, cfg.get(key), voice=True)}</select>"
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
               "🚨 Raid guard": "raid", "🚨 Abuse ping": "abuse", "🔐 Lockdown": "raid",
               "🤖 Automod": "automod", "💬 Chatbot": "chat",
               "📝 Logging": "logs", "🧹 Mod actions": "logs",
               "📋 Whitelist": "raid"}


def _section(title, anchor, inner):
    return (f"<div class=card id='{anchor}'><h2>{title}</h2>{inner}</div>" if inner else "")


def _card(title, sub, inner, card_id="", extra_cls="", pill=None):
    import re as _re
    plain = _re.sub(r"<[^>]+>", " ", inner)
    search = _esc((title + " " + sub + " " + plain)[:900])
    head = f"<h2>{title}"
    if pill:
        head += f"<span class='statpill {pill[1]}'>{pill[0]}</span>"
    head += "</h2>"
    return (f"<div class='card {extra_cls}' id='{card_id}' data-search='{search}'>"
            + head + (f"<div class=sub>{sub}</div>" if sub else "") + inner + "</div>")


def _status(on, partial=False):
    if on:
        return ("ACTIVE", "on")
    if partial:
        return ("PARTIAL", "warn")
    return ("OFF", "off")


def _page_sec(gid, anchor, title, sub, cards_html):
    return (f"<section class=page id='s{gid}-{anchor}'><div class=pagehead><h2>{title}</h2>"
            f"<p>{sub}</p></div>{cards_html}</section>")


def _automod_groups(guild, cfg, urlkey):
    def F(keys):
        return "".join(_field(k, SCHEMA[k][0], SCHEMA[k][1], guild, cfg, urlkey) for k in keys)
    return ("<h3>Invite protection</h3>" + F(["automod_invites"])
            + "<h3>Link protection</h3>" + F(["automod_links", "link_allowed_channels"])
            + "<h3>Spam protection</h3>" + F(["automod_spam", "automod_max_mentions",
                                              "automod_emoji", "automod_max_emoji"])
            + "<h3>Content filter</h3>" + F(["automod_caps", "automod_words"]))


def _gw_name(guild, uid):
    m = guild.get_member(int(uid)) if str(uid).isdigit() else None
    return _esc(m.display_name) if m else f"ID {uid}"


def _giveaways_block(guild, urlkey):
    b = _bot()
    items = [g for g in getattr(b, "giveaway_data", {}).values()
             if g.get("guild") == str(guild.id)]
    active = sorted([g for g in items if g.get("status") == "active"],
                    key=lambda g: g.get("ends_at", 0))
    ended = sorted([g for g in items if g.get("status") != "active"],
                   key=lambda g: g.get("ended_at", g.get("created", 0)), reverse=True)[:10]
    n_entries = sum(len(g.get("entrants", [])) for g in items)
    n_winners = sum(len(g.get("winners", [])) for g in items)
    stats = ("<div class=gridstats>"
             + _stat("🟢", "ACTIVE GIVEAWAYS", str(len(active)), f"of {len(items)} total")
             + _stat("🎁", "TOTAL GIVEAWAYS", str(len(items)), "all time")
             + _stat("👥", "TOTAL ENTRIES", f"{n_entries:,}", "all giveaways")
             + _stat("🏆", "WINNERS", str(n_winners), "crowned so far") + "</div>")
    chans = "".join(f"<option value={c.id}>#{_esc(c.name)}</option>" for c in guild.text_channels[:25])
    dur_opts = "".join(f"<option value={s}>{lab}</option>" for s, lab in
                       ((900, "15 minutes"), (3600, "1 hour"), (21600, "6 hours"),
                        (86400, "24 hours"), (259200, "3 days"), (604800, "7 days")))
    create = _card("New giveaway", "Posts an embed with an Enter button.",
        f"<form method=post action='/api/giveaway?key={urlkey}'>"
        f"<input type=hidden name=guild value={guild.id}>"
        f"<input type=hidden name=action value='create'>"
        f"<div class=row><label>Prize<small>What the winners get.</small></label>"
        f"<input type=text name=prize placeholder='Nitro' size=20></div>"
        f"<div class=row><label>Duration<small>How long entries stay open.</small></label>"
        f"<select name=duration>{dur_opts}</select></div>"
        f"<div class=row><label>Winners<small>How many winners (1–10).</small></label>"
        f"<input type=text name=winners value='1' size=4></div>"
        f"<div class=row><label>Channel<small>Where the giveaway posts.</small></label>"
        f"<select name=channel>{chans}</select></div>"
        f"<div class=row><label>Ready?<small>Entries open immediately.</small></label>"
        f"<button class=primary>🎁 Create Giveaway</button></div></form>", f"c-{guild.id}-gwnew")
    cards = ""
    for g in active:
        import time as _t
        left = max(0, int(g["ends_at"] - _t.time()))
        h, rem = divmod(left, 3600)
        when = f"{h}h {rem // 60}m" if h else f"{rem // 60}m {rem % 60}s"
        ch = guild.get_channel(g["channel"])
        link = (f"<a href='https://discord.com/channels/{guild.id}/{g['channel']}/{g['message']}' "
                f"target=_blank rel=noopener><button type=button class=dim>View</button></a>"
                if g.get("message") else "")
        cards += _card(f"🎁 {_esc(g['prize'])}", f"Ends in {when} · id {g['id']}",
            f"<div class=row><label>Winners<small>Slots.</small></label><span class=pill>{g['winners_n']}</span></div>"
            f"<div class=row><label>Entries<small>Button entries.</small></label><span class=pill>{len(g.get('entrants', []))}</span></div>"
            f"<div class=row><label>Channel<small>Posted here.</small></label><span class=pill>#{_esc(ch.name) if ch else g['channel']}</span></div>"
            f"<div class=row><label>Status<small>Live now.</small></label><span class='statpill on'>● Active</span></div>"
            f"<div class=row><label>Actions<small>Open or end early.</small></label>"
            f"<span>{link}"
            f"<form method=post action='/api/giveaway?key={urlkey}' style='display:inline' data-confirm='End this giveaway now and pick winners?'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='end'>"
            f"<input type=hidden name=id value='{g['id']}'><button class=danger>End</button></form>"
            f"</span></div>", f"c-{guild.id}-gw-{g['id']}", pill=("ACTIVE", "on"))
    for g in ended:
        winners = ", ".join(_gw_name(guild, u) for u in g.get("winners", [])) or "no winner"
        cards += _card(f"🎁 {_esc(g['prize'])}", f"id {g['id']}",
            f"<div class=row><label>Entries<small>Total button entries.</small></label><span class=pill>{len(g.get('entrants', []))}</span></div>"
            f"<div class=row><label>Winner(s)<small>Picked randomly.</small></label><span class=pill>{winners}</span></div>"
            f"<div class=row><label>Status<small>Finished.</small></label><span class='statpill off'>● Ended</span></div>"
            f"<div class=row><label>Actions<small>Pick new winner(s).</small></label>"
            f"<form method=post action='/api/giveaway?key={urlkey}' data-confirm='Reroll and pick new winner(s)?'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='reroll'>"
            f"<input type=hidden name=id value='{g['id']}'><button class=dim>Reroll</button></form></div>",
            f"c-{guild.id}-gw-{g['id']}", pill=("ENDED", "off"))
    if not items:
        cards += ("<div class=empty><b>No giveaways yet</b>"
                  "<p>Create one above — entries open the moment it posts.</p></div>")
    return stats + create + cards


ANALYTIC_METRICS = (("messages", "Messages"), ("commands", "Commands"), ("xp", "XP"),
                      ("mod", "Moderation"), ("joins", "Joins"))
MOD_ACTIONS = ("ban", "kick", "timeout", "mute", "purge", "lockdown", "unlock",
               "verify", "automod", "raid")


def _evs(guild_id):
    b = _bot()
    return [e for e in getattr(b, "events_data", []) if e.get("g") == str(guild_id)]


def _ev_count(evs, kind, action=None):
    if action is not None:
        return sum(1 for e in evs if e.get("k") == kind and e.get("action") == action)
    return sum(1 for e in evs if e.get("k") == kind)


def _delta(cur_n, has_prev, prev_n):
    if not has_prev:
        return "no prior data"
    if prev_n == 0:
        return "new" if cur_n else "—"
    return f"{(cur_n - prev_n) / prev_n * 100:+.1f}% vs prior"


def _analytics_block(guild, urlkey, hours, metric):
    import time as _t
    import datetime as _dt
    from collections import Counter as _Counter
    hours = hours if hours in (6, 12, 24, 168, 720, 2160) else 168
    days = hours / 24
    now = _t.time()
    start = now - hours * 3600
    evs = _evs(guild.id)
    cur = [e for e in evs if e.get("t", 0) >= start]
    prev = [e for e in evs if e.get("t", 0) < start]
    has_prev = bool(prev)
    humans = [m for m in guild.members if not getattr(m, "bot", False)]
    msgs = _ev_count(cur, "msg")
    joins = _ev_count(cur, "join")
    leaves = _ev_count(cur, "leave")
    mods = _ev_count(cur, "mod")
    cmds = _ev_count(cur, "cmd")
    xp_earned = sum(int(e.get("amt", 0)) for e in cur if e.get("k") == "xp")
    xp_prev = sum(int(e.get("amt", 0)) for e in prev if e.get("k") == "xp")
    rng = _range_pills(urlkey, guild.id, hours, "analytics")
    mkey = metric if metric in ("messages", "commands", "xp", "mod", "joins") else "messages"
    mets = "".join(
        f"<a href='/?key={urlkey}&range={_rlabel(hours)}&metric={m}#s{guild.id}-analytics' style='text-decoration:none'>"
        f"<span class='pill{' hot' if m == mkey else ''}'>{lab}</span></a>"
        for m, lab in ANALYTIC_METRICS)
    head = (f"<div class=row><label>Range<small>Period for every number below.</small></label>"
            f"<span>{rng}</span></div>"
            f"<div class=row><label>Chart metric<small>What the activity chart shows.</small></label>"
            f"<span>{mets}</span></div>")
    out = _card("Range & metric", "Applies to this whole page.", head, f"c-{guild.id}-anrange")
    if not cur and not has_prev:
        out += ("<div class=empty><b>No activity tracked yet</b>"
                "<p>Tracking started with this update — numbers appear as messages, joins, "
                "commands and mod actions happen.</p></div>")
        return out
    out += ("<div class=gridstats>"
            + _stat("👥", "MEMBERS", str(len(humans)), f"{len(humans)} humans · {len(guild.members) - len(humans)} bots")
            + _stat("💬", "MESSAGES", f"{msgs:,}", _delta(msgs, has_prev, _ev_count(prev, "msg")))
            + _stat("📥", "NEW MEMBERS", str(joins), _delta(joins, has_prev, _ev_count(prev, "join")))
            + _stat("📤", "LEAVES", str(leaves), _delta(leaves, has_prev, _ev_count(prev, "leave")))
            + _stat("🛡️", "MOD ACTIONS", str(mods), _delta(mods, has_prev, _ev_count(prev, "mod")))
            + _stat("✨", "XP EARNED", f"{xp_earned:,}", _delta(xp_earned, has_prev, xp_prev))
            + _stat("⌨️", "COMMANDS USED", str(cmds), _delta(cmds, has_prev, _ev_count(prev, "cmd")))
            + "</div>")
    hourly = hours <= 48
    if hourly:
        nbuckets, span = min(int(hours), 24), 3600
    else:
        nbuckets, span = min(max(int(hours // 24), 2), 30), 86400

    def _hit(e):
        k = e.get("k")
        if mkey == "xp":
            return k == "xp"
        if mkey == "messages":
            return k == "msg"
        if mkey == "commands":
            return k == "cmd"
        if mkey == "mod":
            return k == "mod"
        return k in ("join", "leave")

    data = _bucketize(cur, start, span, nbuckets, _hit)
    labs = _bucket_labels(start, span, nbuckets, hourly)
    unit = "hour" if hourly else "day"
    if sum(data):
        chart = _linechart(data, labs)
    else:
        chart = ("<div class=empty><b>Nothing in this range</b>"
                 "<p>Tracking needs history first — data appears as activity happens.</p></div>")
    out += _card("Activity", f"{dict(ANALYTIC_METRICS)[mkey]} per {unit}.",
                 chart, f"c-{guild.id}-anchart")
    net = joins - leaves
    out += ("<div class=grid2>"
            + _card("Members", "Growth from tracked joins and leaves.",
                    f"<div class=row><label>Current<small>Humans right now.</small></label><span class=pill>{len(humans)}</span></div>"
                    f"<div class=row><label>New<small>Joined in range.</small></label><span class=pill>+{joins}</span></div>"
                    f"<div class=row><label>Lost<small>Left in range.</small></label><span class=pill>−{leaves}</span></div>"
                    f"<div class=row><label>Net growth<small>New minus lost.</small></label><span class=pill>{net:+d}</span></div>",
                    f"c-{guild.id}-anmem"))
    modrows = "".join(
        f"<div class=row><label style='text-transform:capitalize'>{a}<small>Tracked {a} actions.</small></label>"
        f"<span class=pill>{_ev_count(cur, 'mod', a)}</span></div>"
        for a in MOD_ACTIONS if _ev_count(cur, "mod", a))
    out += _card("Moderation", "Actions the bot took in range.",
                 modrows or "<p><small>No moderation actions in this range.</small></p>",
                 f"c-{guild.id}-anmod") + "</div>"
    top = _Counter(e.get("name", "?") for e in cur if e.get("k") == "cmd").most_common(8)
    if top:
        mxc = top[0][1]
        crows = "".join(
            f"<div class=lb><span class=who>{_esc('.' + n)}</span>"
            f"<span class=track><span class=fill style='width:{int(c * 100 / mxc)}%'></span></span>"
            f"<span class=xp>{c:,}</span></div>" for n, c in top)
    else:
        crows = "<p><small>No commands used in this range.</small></p>"
    b = _bot()
    xpmap = getattr(b, "xp_data", {}).get(str(guild.id), {})
    earners = sum(1 for v in xpmap.values() if isinstance(v, dict) and int(v.get("xp", 0)) > 0)
    board = sorted(((int(v.get("xp", 0)), uid) for uid, v in xpmap.items() if isinstance(v, dict)),
                   reverse=True)[:5]
    if board:
        xtop = board[0][0] or 1
        xrows = ""
        for x, uid in board:
            m = guild.get_member(int(uid))
            xrows += (f"<div class=lb><span class=who>{_esc(m.display_name) if m else '—'}</span>"
                      f"<span class=track><span class=fill style='width:{int(x * 100 / xtop)}%'></span></span>"
                      f"<span class=xp>{x:,}</span></div>")
    else:
        xrows = "<p><small>No XP on the board yet.</small></p>"
    out += ("<div class=grid2>"
            + _card("Top commands", "Most-used bot commands in range.", crows, f"c-{guild.id}-ancmd")
            + _card("XP", "Earned in range, plus all-time board.",
                    f"<div class=row><label>Earned<small>In this range.</small></label><span class=pill>{xp_earned:,}</span></div>"
                    f"<div class=row><label>Earners<small>All-time holders.</small></label><span class=pill>{earners}</span></div>"
                    f"<div class=row><label>Avg / day<small>Earned ÷ days.</small></label><span class=pill>{xp_earned / days:.1f}</span></div>"
                    + xrows, f"c-{guild.id}-anxp") + "</div>")
    return out


def _guild_block(guild, cfg, urlkey, hours=168, metric="messages"):
    gid = guild.id

    def F(keys):
        return "".join(_field(k, SCHEMA[k][0], SCHEMA[k][1], guild, cfg, urlkey) for k in keys)

    parts = [f"<section class=page id='srv-{gid}'><div class=pagehead><h2>{_esc(guild.name)}</h2>"
             f"<p>{len(guild.members)} members · {len(guild.text_channels)} text channels</p></div></section>"]
    am_on = sum(1 for k in ("automod_invites", "automod_links", "automod_spam",
                            "automod_caps", "automod_emoji") if cfg.get(k))
    parts.append(_page_sec(gid, "automod", "Automod", "Automatic message filtering.",
        _card("Automod", "Tuned per category. Switches save instantly.",
              _automod_groups(guild, cfg, urlkey), f"c-{gid}-automod",
              pill=(f"{am_on}/5 ON", "on" if am_on else "off"))))
    _gr = getattr(guild, "get_role", None)
    abuse_role = _gr(cfg.get("abuse_role_id") or 0) if _gr else None
    abuse_armed = cfg.get("raid_action", "ban") != "none"
    parts.append(_page_sec(gid, "abuse", "Abuse", "Security control center.",
        _card("Abuse protection", "Alert role, channel and linked systems.",
              F(["abuse_role_id", "abuse_channel_id"]), f"c-{gid}-abuse",
              pill=_status(bool(abuse_role) and abuse_armed, bool(abuse_role) or abuse_armed))
        + _card("Linked systems", "Detection, lockdown and whitelist live under Raid.",
                f"<div class=row><label>Raid detection & thresholds"
                f"<small>Join spikes, account age, responses</small></label>"
                f"<a href='#c-{gid}-raid'><button type=button class=dim>Open</button></a></div>"
                f"<div class=row><label>Lockdown & whitelist"
                f"<small>Freeze channels, exempt users</small></label>"
                f"<a href='#c-{gid}-lockdown'><button type=button class=dim>Open</button></a></div>",
                f"c-{gid}-linked")))
    parts.append(_page_sec(gid, "raid", "Raid", "Detection, response and manual controls.",
        _card("Detection", "What counts as a raid.",
              "<h3>Thresholds</h3>"
              + F(["join_threshold_count", "join_threshold_seconds", "new_account_age_days"]),
              f"c-{gid}-raid", pill=("ARMED", "on") if abuse_armed else ("OFF", "off"))
        + _card("Response", "What the bot does when detection trips.",
                "<h3>Actions</h3>"
                + F(["raid_action", "timeout_duration_minutes", "lockdown_duration_minutes"]),
                f"c-{gid}-response")
        + _card("Lockdown", "Freeze every text channel instantly. Big red button energy.",
                _lockdown_block(guild, urlkey), f"c-{gid}-lockdown", "lockcard")
        + _card("Whitelist", "These users bypass raid actions.",
                _whitelist_block(guild, cfg, urlkey), f"c-{gid}-whitelist")
        + _card("Mod actions", "Bulk delete, slowmode and voice control.",
                _mod_block(guild, urlkey), f"c-{gid}-mod")))
    parts.append(_page_sec(gid, "logs", "Logs", "Where the bot reports what it does.",
        _card("Logging", "Raid hits, lockdowns and mod actions post here. "
                         "Falls back to the system channel when unset.",
              F(["log_channel_id"]), f"c-{gid}-logs",
              pill=("SET", "on") if guild.get_channel(cfg.get("log_channel_id") or 0) else ("UNSET", "off"))))
    v_on = bool(cfg.get("verify_enabled"))
    parts.append(_page_sec(gid, "verify", "Verify", "Gate new members and hand out the verified role.",
        _card("Verification gate", "Who gets in and what they receive. "
                                   "Status mirrors the live setting.",
              F(["verify_enabled", "verified_role_id", "verify_channel_id"]), f"c-{gid}-verify",
              pill=("ON", "on") if v_on else ("OFF", "off"))))
    parts.append(_page_sec(gid, "roles", "Roles", "Self-serve role menus members opt into.",
        _card("Reaction roles", "Menus post as embeds with toggle buttons.",
              _rolemenu_block(guild, urlkey), f"c-{gid}-roles")))
    parts.append(_page_sec(gid, "chat", "Chat", "Talkative features and voice.",
        _card("Chatbot", "Replies, mood and reactions. Mood applies instantly.",
              F(["chat_enabled", "chat_channel_id", "bot_mood", "autoreact"]), f"c-{gid}-chat",
              pill=(str(cfg.get("bot_mood", "chill")).upper(),
                    "on" if cfg.get("chat_enabled", True) else "off"))
        + _card("Daily question", "One prompt a day to spark chat.",
                F(["qotd_channel_id"]), f"c-{gid}-qotd")
        + _card("Voice", "Join-to-create lobbies and where the bot sits.",
                F(["lobby_channel_id"]) + _voice_block(guild, cfg, urlkey), f"c-{gid}-voice")))
    parts.append(_page_sec(gid, "giveaways", "Giveaways", "Create and manage giveaways across your server.",
        _giveaways_block(guild, urlkey)))
    parts.append(_page_sec(gid, "analytics", "Server Analytics", "Understand what's happening across your server.",
        _analytics_block(guild, urlkey, hours, metric)))
    return "".join(parts)


def _lockdown_block(guild, urlkey):
    return (f"<div class=row><label>Lock every text channel NOW</label>"
            f"<form method=post action='/api/mod?key={urlkey}' data-confirm='Lock every text channel now? Members stay muted until you unlock.'>"
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
        f"<form method=post action='/api/whitelist?key={urlkey}' data-confirm='Remove this user from the whitelist?'>"
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
    chans = "".join(f"<option value={c.id}>#{_esc(c.name)}</option>" for c in guild.text_channels[:25])
    return (f"<div class=row><label>Bulk delete</label>"
            f"<form method=post action='/api/mod?key={urlkey}' data-confirm='Bulk-delete these messages? This cannot be undone.'>"
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


def _voice_block(guild, cfg, urlkey):
    b = _bot()
    disc = getattr(b, "bot", None) if b else None
    vc = disc.get_guild(guild.id).voice_client if disc and disc.get_guild(guild.id) else None
    current = f"#{vc.channel.name}" if vc and vc.channel else "not in voice"
    vcs = "".join(f"<option value={c.id}>🔊 {c.name}</option>"
                  for c in guild.voice_channels[:25])
    return (f"<div class=row><label>Now: <b>{current}</b></label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='vcleave'>"
            f"<button class=dim>Leave</button></form></div>"
            f"<div class=row><label>Join a VC</label>"
            f"<form method=post action='/api/mod?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='vcjoin'>"
            f"<select name=channel>{vcs}</select>"
            f"<button>Join</button></form></div>")


def _rolemenu_block(guild, urlkey):
    b = _bot()
    menus = b.rr_data.get(str(guild.id), [])
    rows = []
    for i, m in enumerate(menus):
        ch = guild.get_channel(m.get("channel") or 0)
        rids = ",".join(str(r) for r in m.get("roles", []))
        rows.append(
            f"<div class=row><label>#{ch.name if ch else m.get('channel')} "
            f"<small>{len(m.get('roles', []))} roles</small></label>"
            f"<form method=post action='/api/rolemenu?key={urlkey}' data-confirm='Delete this menu? Its Discord message goes too.'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='delete'>"
            f"<input type=hidden name=idx value={i}>"
            f"<button type=button class='dim menu-edit' data-idx={i} "
            f"data-channel={m.get('channel')} data-roles='{rids}'>Edit</button>"
            f"<button type=button class='dim menu-dup' data-idx={i}>Duplicate</button>"
            f"<button class=danger>Delete</button></form></div>")
    boxes = []
    for r in sorted(guild.roles, key=lambda r: r.position, reverse=True):
        if r.is_default() or r.managed:
            continue
        if r.permissions.administrator:
            boxes.append(
                f"<label class=pill title='Admin roles cannot be self-served'>"
                f"<input type=checkbox disabled> {_esc(r.name)} (ADMIN)</label>")
        else:
            boxes.append(
                f"<label class=pill><input type=checkbox name=roles value={r.id}> "
                f"{_esc(r.name)}</label>")
    roles = "".join(boxes)
    chans = "".join(f"<option value={c.id}>#{_esc(c.name)}</option>" for c in guild.text_channels[:25])
    return (("".join(rows) or "<div class=empty><b>No reaction menus</b>"
             "<p>You haven't created one yet — tick roles below and hit New menu.</p></div>")
            + f"<form class=rolemenu-form method=post action='/api/rolemenu?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='add'>"
            f"<input type=hidden name=idx value='-1'>"
            f"<select name=channel>{chans}</select><br>"
            f"<div style='max-height:220px;overflow-y:auto;display:flex;flex-wrap:wrap;gap:6px;"
            f"padding:6px 0'>{roles}</div>"
            f"<br><small>Max 25 roles per menu (Discord limit) — make another menu for the rest.</small><br>"
            f"<button>➕ New menu</button></form>")


def _stat(ic, label, num, sub):
    return (f"<div class=stat><div class=lab><span>{ic}</span>{label}</div>"
            f"<div class=num>{num}</div><div class=tr>{sub}</div></div>")


def _parse_range(args):
    r = (args.get("range", "") or "").strip().lower()
    mapping = {"6h": 6, "12h": 12, "24h": 24, "7d": 168, "30d": 720, "90d": 2160}
    if r in mapping:
        return r, mapping[r]
    try:
        d = int(args.get("days", 7))
    except (ValueError, TypeError):
        d = 7
    return {1: ("24h", 24), 7: ("7d", 168), 30: ("30d", 720),
            90: ("90d", 2160)}.get(d, ("7d", 168))


def _range_pills(urlkey, gid, curhours, anchor):
    out = []
    for label, h in (("6h", 6), ("12h", 12), ("24h", 24), ("7d", 168), ("30d", 720)):
        hot = " hot" if h == curhours else ""
        out.append(f"<a href='/?key={urlkey}&range={label}#s{gid}-{anchor}'>"
                   f"<span class='pill{hot}'>{label}</span></a>")
    return "".join(out)


def _rlabel(hours):
    return {6: "6h", 12: "12h", 24: "24h", 168: "7d", 720: "30d", 2160: "90d"}.get(hours, "7d")


def _rel(ts):
    import time as _t
    try:
        s = int(_t.time() - float(ts or 0))
    except (ValueError, TypeError):
        return "—"
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60}m ago"
    if s < 86400:
        return f"{s // 3600}h ago"
    return f"{s // 86400}d ago"


def _linechart(data, labels):
    w, h, pad = 600, 170, 34
    n = len(data)
    mx = max(data) if data else 0
    if mx <= 0:
        mx = 1
    def X(i):
        return pad + i * (w - 2 * pad) / max(n - 1, 1)
    def Y(v):
        return h - pad - (v / mx) * (h - 2 * pad - 16)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(data))
    grid = ""
    for f in (0, 0.5, 1.0):
        y = h - pad - f * (h - 2 * pad - 16)
        grid += (f"<line x1={pad} y1={y:.1f} x2={w - pad} y2={y:.1f} class=grid/>"
                 f"<text x=2 y={y + 3:.1f} class=alab>{mx * f:,.0f}</text>")
    xl = ""
    step = max(1, n // 6)
    for i in range(0, n, step):
        xl += f"<text x={X(i):.1f} y={h - 8} text-anchor=middle class=alab>{labels[i]}</text>"
    area = f"{pad},{h - pad} " + pts + f" {w - pad},{h - pad}"
    return (f"<svg class=chart viewBox='0 0 {w} {h}' role=img>"
            f"{grid}<polygon points='{area}' class=area/>"
            f"<polyline points='{pts}' class=line/>{xl}</svg>")


def _bucketize(evs, start, span, nbuckets, pred):
    data = [0] * nbuckets
    for e in evs:
        if pred(e):
            i = min(int((e.get("t", start) - start) // span), nbuckets - 1)
            if i >= 0:
                data[i] += e.get("amt", 1) if e.get("k") == "xp" else 1
    return data


def _bucket_labels(start, span, nbuckets, hourly):
    import datetime as _dt
    out = []
    for i in range(nbuckets):
        ts = start + (i + 0.5) * span
        d = _dt.datetime.fromtimestamp(ts)
        out.append(d.strftime("%H:00") if hourly else d.strftime("%a" if span < 7 * 86400 else "%m/%d"))
    return out


def _recent_events(guild, limit=6):
    evs = [e for e in _evs(guild.id) if e.get("k") == "mod"]
    evs.sort(key=lambda e: e.get("t", 0), reverse=True)
    rows = ""
    for e in evs[:limit]:
        a = e.get("action", "?")
        dot = "bad" if a in ("ban", "kick", "raid") else ("warn" if a in ("timeout", "mute") else "")
        rows += (f"<div class=evrow><span class='dot {dot}'></span>"
                 f"<span class=t style='text-transform:capitalize'>{_esc(a)}</span>"
                 f"<span class=when>{_rel(e.get('t', 0))}</span></div>")
    if not rows:
        return ("<div class=empty><b>No moderation events yet</b>"
                "<p>Bans, mutes and lockdowns will appear here once tracked.</p></div>")
    return rows


def _overview(b, guilds, g0, cfg, urlkey, hours):
    import time as _t
    import datetime as _dt
    now = _t.time()
    updated = _dt.datetime.fromtimestamp(now).strftime("%H:%M")
    members = [m for m in g0.members if not getattr(m, "bot", False)]
    nbots = len(g0.members) - len(members)
    evs = _evs(g0.id)
    day = [e for e in evs if e.get("t", 0) >= now - 86400]
    msgs = _ev_count(day, "msg")
    joins = _ev_count(day, "join")
    mods = _ev_count(day, "mod")
    cmds = _ev_count(day, "cmd")
    xp = getattr(b, "xp_data", {}).get(str(g0.id), {})
    total_xp = sum(int(v.get("xp", 0)) for v in xp.values() if isinstance(v, dict))
    earners = sum(1 for v in xp.values() if isinstance(v, dict) and int(v.get("xp", 0)) > 0)
    top = sorted(((int(v.get("xp", 0)), uid) for uid, v in xp.items()
                  if isinstance(v, dict)), reverse=True)[:1]
    topname = ""
    if top:
        m = g0.get_member(int(top[0][1]))
        topname = _esc(m.display_name) if m else "—"
    hourly = hours <= 48
    if hourly:
        nbuckets, span = min(int(hours), 24), 3600
    else:
        nbuckets, span = min(max(int(hours // 24), 2), 30), 86400
    start = now - nbuckets * span
    data = _bucketize(evs, start, span, nbuckets, lambda e: e.get("k") == "msg")
    labs = _bucket_labels(start, span, nbuckets, hourly)
    stats = "".join([
        _stat("👥", "SERVER MEMBERS", f"{len(members)}", f"{nbots} bots"),
        _stat("💬", "MESSAGES", f"{msgs:,}", "last 24 hours"),
        _stat("📥", "NEW MEMBERS", f"{joins}", "last 24 hours"),
        _stat("🛡️", "MOD ACTIONS", f"{mods}", "last 24 hours"),
    ])
    left = (
        f"<div class=row><label>Total XP<small>All-time across the server.</small></label>"
        f"<span class=pill>{total_xp:,}</span></div>"
        f"<div class=row><label>XP earners<small>Members holding XP.</small></label>"
        f"<span class=pill>{earners}</span></div>"
        f"<div class=row><label>Top member<small>Highest XP right now.</small></label>"
        f"<span class=pill>{topname or '—'}</span></div>"
        f"<div class=row><label>Commands used<small>Bot commands, last 24 hours.</small></label>"
        f"<span class=pill>{cmds}</span></div>")
    prot = cfg.get("raid_action", "ban") != "none"
    am_on = sum(1 for k in ("automod_invites", "automod_links", "automod_spam",
                            "automod_caps", "automod_emoji") if cfg.get(k))
    right = (
        f"<div class=row><label>Raid protection<small>Join-spike response.</small></label>"
        f"<span class='statpill {'on' if prot else 'off'}'>{'ON' if prot else 'OFF'}</span></div>"
        f"<div class=row><label>Automod filters<small>Content filters enabled.</small></label>"
        f"<span class=pill>{am_on}/5 on</span></div>"
        f"<div class=row><label>Verification<small>New-member gate.</small></label>"
        f"<span class='statpill {'on' if cfg.get('verify_enabled') else 'off'}'>"
        f"{'ON' if cfg.get('verify_enabled') else 'OFF'}</span></div>"
        f"<h3>Recent events</h3>{_recent_events(g0)}")
    return (f"<section class=page id='s{g0.id}-overview'>"
            f"<div class=crumb>Wigglesworth / {_esc(g0.name)} / Overview</div>"
            f"<div class=ovhead><div><h1>Overview</h1>"
            f"<p>Monitor your server activity, bot status, and moderation at a glance.</p></div>"
            f"<div class=sp></div><span class=updated>Updated {updated}</span>"
            f"<button class=dim onclick='location.reload()'>↻ Refresh</button></div>"
            f"<div class=gridstats>{stats}</div>"
            + _card("Server activity",
                    "Messages over the selected range.",
                    f"<div class=rangebar>{_range_pills(urlkey, g0.id, hours, 'overview')}</div>"
                    f"{_linechart(data, labs)}"
                    if sum(data) else
                    f"<div class=rangebar>{_range_pills(urlkey, g0.id, hours, 'overview')}</div>"
                    f"<div class=empty><b>No activity in this range</b>"
                    f"<p>Tracking needs history first — numbers appear as messages happen.</p></div>",
                    f"c-{g0.id}-ovchart")
            + f"<div class=grid2>"
            + _card("Server", "Activity and account statistics.", left, f"c-{g0.id}-ovserver")
            + _card("Moderation", "Protection status and recent events.", right, f"c-{g0.id}-ovmod")
            + "</div></section>")


def _settings_page(b, disc):
    up = int(time.time() - _STARTED)
    d, rem = divmod(up, 86400)
    h, rem = divmod(rem, 3600)
    m, _s = divmod(rem, 60)
    uptime = (f"{d}d " if d else "") + (f"{h}h " if h or d else "") + f"{m}m"
    lat = f"{int(disc.latency * 1000)} ms" if disc and getattr(disc, "latency", None) else "—"
    try:
        import discord as _d
        dver = getattr(_d, "__version__", "?")
    except ImportError:
        dver = "?"
    datafile = str(getattr(b, "DATA_FILE", "?"))
    dexists = bool(datafile) and os.path.exists(datafile)
    ncfg = len(getattr(b, "configs", {}) or {})
    persistent = bool(os.environ.get("DATA_DIR"))
    ng = len(disc.guilds) if disc else 0

    def _row(label, desc, val):
        return (f"<div class=row><label>{label}<small>{desc}</small></label>"
                f"<span class=pill>{val}</span></div>")

    status = _card("Bot status", "Live connection info. Read-only.",
        _row("Connection", "Discord gateway state.",
             "● Connected" if disc else "○ Offline")
        + _row("Latency", "Gateway heartbeat round-trip.", _esc(lat))
        + _row("Servers", "Guilds this process serves.", str(ng))
        + _row("Prefix", "Command prefix for chat commands.", _esc(getattr(b, "PREFIX", ".")))
        + _row("Uptime", "Time since this process started.", _esc(uptime))
        + _row("discord.py", "Library version.", _esc(dver)), "c-settings-status")
    persist = _card("Data & persistence", "Where settings live.",
        _row("Config file", "Per-server settings JSON.", f"<code>{_esc(datafile)}</code>")
        + _row("File present", "Whether the file exists on disk.",
               "Yes" if dexists else "Missing")
        + _row("Servers stored", "Guilds with saved settings.", str(ncfg))
        + _row("Persistent volume", "DATA_DIR set + volume attached?",
               "Yes" if persistent else "No — redeploys wipe settings")
        + ("" if persistent else "<p><small>Set DATA_DIR=/app/data and attach a Railway volume, "
                                 "or panel changes forget themselves on redeploy.</small></p>"),
        "c-settings-data")
    return (f"<section class=page id='settings'><div class=pagehead><h2>Settings</h2>"
            f"<p>Bot-level info. Read-only.</p></div>{status}{persist}</section>")


def _hero_unused(b, guilds):
    return ""


def _leaders(b, guild):
    xp = getattr(b, "xp_data", {}).get(str(guild.id), {})
    board = sorted(((int(v.get("xp", 0)), uid) for uid, v in xp.items()
                    if isinstance(v, dict)), reverse=True)[:5]
    if not board:
        return ("<div class=card><h2>🏆 XP Leaders</h2>"
                "<div class=sub>No XP yet — chat to light it up.</div></div>")
    top = max(board[0][0], 1)
    rows = ""
    for i, (x, uid) in enumerate(board, 1):
        m = guild.get_member(int(uid))
        name = _esc(m.display_name) if m else "—"
        av = (m.display_avatar.url if m and getattr(m, "display_avatar", None)
              else "https://cdn.discordapp.com/embed/avatars/0.png")
        rows += (f"<div class=lb><span class=rank>#{i}</span>"
                 f"<img class=av src='{av}' loading=lazy>"
                 f"<span class=who>{name}</span>"
                 f"<span class=track><span class=fill style='width:{int(x * 100 / top)}%'></span></span>"
                 f"<span class=xp>{x:,} XP</span></div>")
    return ("<div class=card><h2>🏆 XP Leaders</h2>"
            "<div class=sub>Top 5 by XP</div>" + rows + "</div>")


def _health(items):
    body = "".join(
        f"<div class=txn><span class=t>{label}</span>"
        f"<span class='badge {'on' if good else 'off'}'>{'ON' if good else 'OFF'}</span></div>"
        for label, good in items)
    return ("<div class=card><h2>💚 Server health</h2>"
            "<div class=sub>Live feature states</div>" + body + "</div>")


def _health_legacy(b, guild, cfg):
    return _health([("Verification gate", bool(cfg.get("verify_enabled"))),
                    ("Invite filter", bool(cfg.get("automod_invites"))),
                    ("Chatbot", bool(cfg.get("chat_enabled", True)))])


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
        from flask import Flask, request, redirect, make_response
    except ImportError:
        return None
    app = Flask(__name__)

    @app.after_request
    def _nocache(resp):
        resp.headers["Cache-Control"] = "no-store, max-age=0"
        return resp

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
        g0 = glist[0]
        rlabel, hours = _parse_range(request.args)
        metric = request.args.get("metric", "messages")
        body = _overview(b, glist, g0, b.get_config(g0.id), key, hours)
        for g in glist:
            body += _guild_block(g, b.get_config(g.id), key, hours, metric)
        disc = _disc()
        body += _settings_page(b, disc)
        return _page(body, _sidebar(g0, glist), _topbar(bool(disc), len(glist)))

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
        elif kind in ("channel", "role", "voice"):
            val = int(raw) if raw.isdigit() else None
        elif kind == "words":
            val = [x.strip().lower() for x in raw.split(",") if x.strip()][:100]
        elif kind == "channellist":
            val = [int(x) for x in request.form.getlist("value") if x.isdigit()][:50]
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
        elif action == "edit":
            try:
                idx = int(request.form.get("idx", -1))
            except (ValueError, TypeError):
                idx = -1
            menus = b.rr_data.get(str(gid), [])
            if 0 <= idx < len(menus):
                old = menus[idx]
                disc = _disc()
                g = disc.get_guild(gid) if disc else None
                ch = g.get_channel(old["channel"]) if g else None
                if ch and disc:
                    fut = _aio.run_coroutine_threadsafe(ch.fetch_message(old["message"]), disc.loop)
                    try:
                        msg = fut.result(timeout=10)
                        fut2 = _aio.run_coroutine_threadsafe(msg.delete(), disc.loop)
                        fut2.result(timeout=10)
                    except Exception:
                        pass
                    menus.pop(idx)
                    b._save_rr(b.rr_data)
                rids = [int(r) for r in request.form.getlist("roles")][:25]
                try:
                    ch_id = int(request.form.get("channel", 0) or 0)
                except (ValueError, TypeError):
                    ch_id = 0
                if rids and ch_id and disc and g:
                    before = len(b.rr_data.get(str(gid), []))
                    fut = _aio.run_coroutine_threadsafe(_make_menu(b, gid, ch_id, rids), disc.loop)
                    try:
                        fut.result(timeout=20)
                        menus = b.rr_data.get(str(gid), [])
                        if len(menus) > before:
                            menus.insert(min(idx, len(menus)), menus.pop())
                            b._save_rr(b.rr_data)
                    except Exception as e:
                        print("[dash] menu edit failed:", e)
        elif action == "duplicate":
            try:
                idx = int(request.form.get("idx", -1))
            except (ValueError, TypeError):
                idx = -1
            menus = b.rr_data.get(str(gid), [])
            if 0 <= idx < len(menus):
                src = menus[idx]
                disc = _disc()
                if disc:
                    fut = _aio.run_coroutine_threadsafe(
                        _make_menu(b, gid, src["channel"], list(src.get("roles", []))), disc.loop)
                    try:
                        fut.result(timeout=20)
                    except Exception as e:
                        print("[dash] menu duplicate failed:", e)
        return redirect(f"/?key={request.args.get('key', '')}")

    @app.post("/api/giveaway")
    def api_giveaway():
        import asyncio as _aio
        key = request.args.get("key", "")
        if not _check(key):
            return "no", 401
        b = _bot()
        disc = _disc()
        if not disc:
            return redirect(f"/?key={key}")
        try:
            gid = int(request.form["guild"])
        except (ValueError, TypeError, KeyError):
            return redirect(f"/?key={key}")
        g = disc.get_guild(gid)
        if not g:
            return redirect(f"/?key={key}")
        action = request.form.get("action")
        if action == "create":
            prize = (request.form.get("prize") or "").strip()[:200]
            try:
                dur_s = int(request.form.get("duration", 0))
            except (ValueError, TypeError):
                dur_s = 0
            try:
                winners_n = max(1, min(10, int(request.form.get("winners", 1))))
            except (ValueError, TypeError):
                winners_n = 1
            try:
                ch_id = int(request.form.get("channel", 0) or 0)
            except (ValueError, TypeError):
                ch_id = 0
            ch = g.get_channel(ch_id)
            if prize and ch and 60 <= dur_s <= 30 * 86400:
                fut = _aio.run_coroutine_threadsafe(
                    b.create_giveaway(g, ch, prize, dur_s, winners_n, 0), disc.loop)
                try:
                    fut.result(timeout=20)
                except Exception as e:
                    print("[dash] giveaway create failed:", e)
        elif action == "end":
            g = b.giveaway_data.get(request.form.get("id", ""))
            if g is None or g.get("guild") != str(gid):
                return redirect(f"/?key={key}")
            fut = _aio.run_coroutine_threadsafe(
                b.end_giveaway(g["id"], by="dashboard"), disc.loop)
            try:
                fut.result(timeout=20)
            except Exception as e:
                print("[dash] giveaway end failed:", e)
        elif action == "reroll":
            g = b.giveaway_data.get(request.form.get("id", ""))
            if g is None or g.get("guild") != str(gid):
                return redirect(f"/?key={key}")
            fut = _aio.run_coroutine_threadsafe(
                b.reroll_giveaway(g["id"]), disc.loop)
            try:
                fut.result(timeout=20)
            except Exception as e:
                print("[dash] giveaway reroll failed:", e)
        return redirect(f"/?key={key}")

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
            def _cid() -> int:
                try:
                    return int(request.form.get("channel", 0))
                except (ValueError, TypeError):
                    return 0

            if action == "lockdown":
                await b.lockdown_guild(g, reason="dashboard lockdown")
            elif action == "unlock":
                await b.unlock_guild(g, reason="dashboard unlock")
            elif action == "purge":
                ch = g.get_channel(_cid())
                n = max(1, min(100, int(request.form.get("count", 20) or 20)))
                if ch:
                    try:
                        await ch.purge(limit=n)
                    except Exception:
                        pass
            elif action == "slowmode":
                ch = g.get_channel(_cid())
                n = max(0, min(21600, int(request.form.get("count", 0) or 0)))
                if ch:
                    try:
                        await ch.edit(slowmode_delay=n, reason="dashboard slowmode")
                    except Exception:
                        pass
            elif action == "vcjoin":
                ch = g.get_channel(_cid())
                if ch:
                    try:
                        vc = g.voice_client
                        if vc:
                            await vc.move_to(ch)
                        else:
                            await ch.connect()
                        b.update_config(gid, mod_vc_channel_id=ch.id)
                    except Exception as e:
                        print("[dash] vcjoin failed:", e)
            elif action == "vcleave":
                try:
                    if g.voice_client:
                        await g.voice_client.disconnect()
                    b.update_config(gid, mod_vc_channel_id=None)
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
