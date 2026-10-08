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
/* ===== Wigglesworth control-center theme ===== */
:root{--bg:#07070d;--bg2:#0b0b14;--card:#11111c;--line:#22222f;--line2:#2c2c44;
--txt:#f2f2f8;--mut:#8b8b9e;--mut2:#55556e;--acc:#7c3aed;--accsoft:rgba(124,58,237,.14);
--grn:#4ade80;--red:#f87171}
body{background:var(--bg)}
.app{display:flex;min-height:100vh}
.sidebar{width:248px;flex:0 0 248px;position:sticky;top:0;height:100vh;overflow-y:auto;
background:#0a0a12;border-right:1px solid var(--line);padding:20px 14px;display:flex;flex-direction:column;gap:4px}
.sbrand{display:flex;align-items:center;gap:10px;padding:4px 8px 16px}
.sbrand .orb{width:32px;height:32px;border-radius:50%;flex:0 0 32px;
background:conic-gradient(from 40deg,#a3e635,#7c3aed,#22d3ee,#a3e635)}
.sbrand b{font-size:15px;display:block}.sbrand small{color:var(--mut);font-size:11px;display:block}
.snavlabel{font-size:10px;letter-spacing:1.2px;color:var(--mut2);padding:12px 10px 4px;font-weight:800}
.snav{display:flex;align-items:center;gap:10px;color:#b9b9cf;text-decoration:none;font-size:13.5px;
padding:9px 12px;border-radius:10px;border:1px solid transparent}
.snav .ic{width:20px;text-align:center}
.snav:hover{background:#14141f;color:#fff}
.snav.active{background:var(--accsoft);border-color:rgba(124,58,237,.35);color:#fff}
.servers{margin-top:auto;border-top:1px solid var(--line);padding-top:12px;display:flex;flex-direction:column;gap:6px}
.srv{display:flex;align-items:center;gap:9px;padding:8px 10px;border-radius:10px;color:#cfcfe0;text-decoration:none;font-size:13px}
.srv:hover{background:#14141f}
.srv img,.srv .noav{width:28px;height:28px;border-radius:50%;flex:0 0 28px}
.srv .noav{background:linear-gradient(135deg,#7c3aed,#22d3ee);display:flex;align-items:center;justify-content:center;font-weight:800;color:#fff}
.main{flex:1;min-width:0;padding:0 28px 48px;max-width:1180px}
.topbar2{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;padding:14px 0;
background:linear-gradient(var(--bg) 78%,transparent)}
.topbar2 .crumb{font-size:12px;color:var(--mut2)}
.topbar2 h1{font-size:19px;font-weight:800}
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
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.gridstats{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px;margin:0 0 14px;
box-shadow:0 18px 50px rgba(0,0,0,.45)}
.card h2{font-size:15px;margin-bottom:4px;display:flex;justify-content:space-between;align-items:center}
.card .sub{color:var(--mut);font-size:12px;margin-bottom:10px}
.card h3{margin:16px 0 6px;font-size:11px;color:#a5b4fc;text-transform:uppercase;letter-spacing:.8px}
.stat{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:16px}
.stat .lab{font-size:10.5px;letter-spacing:1px;color:var(--mut);font-weight:800;display:flex;gap:6px;align-items:center}
.stat .num{font-size:30px;font-weight:800;letter-spacing:-.5px;margin:6px 0 2px}
.stat .tr{font-size:12px;color:var(--mut)}
.stat .tr.up{color:var(--grn)}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid #1b1b27}
.row:first-of-type{border-top:0}.row label{font-size:13.5px}.row small{display:block;color:var(--mut);font-size:12px;font-weight:400}
.row form{display:flex;align-items:center;gap:8px;flex-wrap:wrap;justify-content:flex-end}
button,select{background:#fff;color:#111;border:0;border-radius:20px;padding:8px 16px;font-weight:700;cursor:pointer;font-size:13px;transition:transform .15s,box-shadow .15s}
button:hover{transform:translateY(-1px);box-shadow:0 6px 18px rgba(124,58,237,.25)}
button:disabled{opacity:.6;cursor:wait;transform:none}
button.danger{background:rgba(244,63,94,.14);color:#fda4af;border:1px solid rgba(244,63,94,.4)}
button.ok{background:rgba(34,197,94,.14);color:#4ade80;border:1px solid rgba(34,197,94,.4)}
button.dim{background:#1d1d2e;color:#cfcfe0}
button.primary{background:linear-gradient(135deg,#7c3aed,#6d28d9);color:#fff}
select,input[type=text]{background:#0e0e17;color:#eee;border:1px solid var(--line2);border-radius:10px;padding:9px 11px;font-size:13px;max-width:100%}
select:focus,input[type=text]:focus{outline:none;border-color:var(--acc)}
button.sw{position:relative;width:46px;height:26px;border-radius:20px;padding:0;background:#2a2a3d;border:1px solid var(--line2)}
button.sw .knob{position:absolute;top:2px;left:2px;width:20px;height:20px;border-radius:50%;background:#8b8b9e;transition:left .18s,background .18s}
button.sw.on{background:rgba(124,58,237,.5);border-color:var(--acc)}
button.sw.on .knob{left:22px;background:#fff}
.pill{display:inline-flex;align-items:center;gap:6px;background:#1d1d2e;border:1px solid var(--line);border-radius:12px;padding:4px 11px;margin:2px;font-size:12.5px}
.pill input{accent-color:#7c3aed}
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
function toast(msg, kind) {
  var box = document.getElementById('toasts');
  if (!box) return;
  var t = document.createElement('div');
  t.className = 'toast' + (kind ? ' ' + kind : '');
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(function() { t.remove(); }, 2600);
}
document.addEventListener('submit', function(e) {
  var f = e.target;
  if (!f || f.tagName !== 'FORM' || (f.method || '').toLowerCase() !== 'post') return;
  e.preventDefault();
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
          path.indexOf('/api/rolemenu') === 0) {
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
        return;
      }
      toast('Saved', 'ok');
      if (btn) {
        btn.textContent = 'Saved';
        setTimeout(function() {
          if (btn.classList.contains('sw')) return;
          btn.innerHTML = orig; btn.disabled = false;
        }, 1200);
      }
    })
    .catch(function() {
      toast('Save failed — is the bot online?', 'err');
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
</script>
'''


NAV = [
    ("OVERVIEW", [("🏠", "Overview", "overview")]),
    ("SERVER", [("🛡️", "Verify", "verify"), ("🎭", "Roles", "roles"),
               ("🚨", "Raid", "raid"), ("🚩", "Abuse", "abuse"),
               ("🤖", "Automod", "automod"), ("💬", "Chat", "chat"),
               ("📝", "Logs", "logs")]),
]


def _sidebar(g0, guilds):
    links = []
    for label, items in NAV:
        links.append(f"<div class=snavlabel>{label}</div>")
        for ic, name, anchor in items:
            href = f"#s{g0.id}-{anchor}" if g0 else f"#{anchor}"
            links.append(f"<a class=snav data-spy href='{href}'><span class=ic>{ic}</span>{name}</a>")
    srvs = []
    for g in guilds:
        icon = f"<img src='{g.icon.url}'>" if getattr(g, "icon", None) else \
            f"<span class=noav>{_esc((g.name or '?')[:1])}</span>"
        srvs.append(f"<a class=srv href='#srv-{g.id}'>{icon}<span>{_esc(g.name)}</span></a>")
    return ("<aside class=sidebar><div class=sbrand><span class=orb></span>"
            "<span><b>Wigglesworth</b><small>Discord Bot</small></span></div>"
            + "".join(links) +
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


def _field(key, kind, label, guild, cfg, urlkey):
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


def _card(title, sub, inner, card_id="", extra_cls=""):
    import re as _re
    plain = _re.sub(r"<[^>]+>", " ", inner)
    search = _esc((title + " " + sub + " " + plain)[:900])
    return (f"<div class='card {extra_cls}' id='{card_id}' data-search='{search}'>"
            f"<h2>{title}</h2>" + (f"<div class=sub>{sub}</div>" if sub else "") + inner + "</div>")


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


def _guild_block(guild, cfg, urlkey):
    gid = guild.id

    def F(keys):
        return "".join(_field(k, SCHEMA[k][0], SCHEMA[k][1], guild, cfg, urlkey) for k in keys)

    parts = [f"<section class=page id='srv-{gid}'><div class=pagehead><h2>{_esc(guild.name)}</h2>"
             f"<p>{len(guild.members)} members · {len(guild.text_channels)} text channels</p></div></section>"]
    parts.append(_page_sec(gid, "verify", "Verify", "Gate new members and hand out the verified role.",
        _card("Verification gate", "Who gets in and what they receive.",
              F(["verify_enabled", "verified_role_id", "verify_channel_id"]), f"c-{gid}-verify")))
    parts.append(_page_sec(gid, "roles", "Roles", "Self-serve role menus members opt into.",
        _card("Reaction roles", "Menus post as embeds with toggle buttons.",
              _rolemenu_block(guild, urlkey), f"c-{gid}-roles")))
    parts.append(_page_sec(gid, "raid", "Raid", "Join-spike detection and automatic response.",
        _card("Raid guard", "Thresholds and what the bot does when they trip.",
              F(["join_threshold_count", "join_threshold_seconds", "raid_action",
                 "lockdown_duration_minutes", "new_account_age_days",
                 "timeout_duration_minutes"]), f"c-{gid}-raid")))
    parts.append(_page_sec(gid, "abuse", "Abuse", "Pings, lockdowns and manual moderation.",
        _card("Abuse ping", "Where .abuse alerts go and who they notify.",
              F(["abuse_role_id", "abuse_channel_id"]), f"c-{gid}-abuse")
        + _card("Lockdown", "Freeze every text channel instantly. Big red button energy.",
                _lockdown_block(guild, urlkey), f"c-{gid}-lockdown", "lockcard")
        + _card("Whitelist", "These users bypass raid actions.",
                _whitelist_block(guild, cfg, urlkey), f"c-{gid}-whitelist")
        + _card("Mod actions", "Bulk delete, slowmode and voice control.",
                _mod_block(guild, urlkey), f"c-{gid}-mod")))
    parts.append(_page_sec(gid, "automod", "Automod", "Automatic message filtering.",
        _card("Automod", "Tuned per category. Switches save instantly.",
              _automod_groups(guild, cfg, urlkey), f"c-{gid}-automod")))
    parts.append(_page_sec(gid, "chat", "Chat", "Talkative features and voice.",
        _card("Chatbot", "Replies, mood and reactions.",
              F(["chat_enabled", "chat_channel_id", "bot_mood", "autoreact"]), f"c-{gid}-chat")
        + _card("Daily question", "One prompt a day to spark chat.",
                F(["qotd_channel_id"]), f"c-{gid}-qotd")
        + _card("Voice", "Join-to-create lobbies and where the bot sits.",
                F(["lobby_channel_id"]) + _voice_block(guild, cfg, urlkey), f"c-{gid}-voice")))
    parts.append(_page_sec(gid, "logs", "Logs", "Where the bot reports what it does.",
        _card("Logging", "Raid hits, mod actions and joins land here.",
              F(["log_channel_id"]), f"c-{gid}-logs")))
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
    chans = "".join(f"<option value={c.id}>#{_esc(c.name)}</option>" for c in guild.text_channels[:25])
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
            f"<form method=post action='/api/rolemenu?key={urlkey}'>"
            f"<input type=hidden name=guild value={guild.id}>"
            f"<input type=hidden name=action value='delete'>"
            f"<input type=hidden name=idx value={i}>"
            f"<button type=button class='dim menu-edit' data-idx={i} "
            f"data-channel={m.get('channel')} data-roles='{rids}'>Edit</button>"
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
    return (("".join(rows) or "<p><small>No menus yet.</small></p>")
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


def _overview(b, guilds, g0, cfg):
    members = [m for m in g0.members if not getattr(m, "bot", False)]
    xp = getattr(b, "xp_data", {}).get(str(g0.id), {})
    total_xp = sum(int(v.get("xp", 0)) for v in xp.values() if isinstance(v, dict))
    earners = sum(1 for v in xp.values() if isinstance(v, dict) and int(v.get("xp", 0)) > 0)
    flags = [bool(cfg.get("verify_enabled")), bool(cfg.get("automod_invites")),
             bool(cfg.get("automod_links")), bool(cfg.get("automod_spam")),
             bool(cfg.get("automod_caps")), bool(cfg.get("automod_emoji")),
             bool(cfg.get("chat_enabled", True)), bool(cfg.get("autoreact", True)),
             cfg.get("raid_action", "ban") != "none", bool(cfg.get("qotd_channel_id"))]
    on = sum(1 for f in flags if f)
    health_items = [("Verification", bool(cfg.get("verify_enabled"))),
                    ("Invite filter", bool(cfg.get("automod_invites"))),
                    ("Chatbot", bool(cfg.get("chat_enabled", True))),
                    ("Automod", any(cfg.get(k) for k in ("automod_links", "automod_spam",
                                                         "automod_caps", "automod_emoji"))),
                    ("Raid protection", cfg.get("raid_action", "ban") != "none")]
    hon = sum(1 for _, s in health_items if s)
    top = sorted(((int(v.get("xp", 0)), uid) for uid, v in xp.items()
                  if isinstance(v, dict)), reverse=True)[:1]
    topname = ""
    if top:
        m = g0.get_member(int(top[0][1]))
        topname = f" · top: {(m.display_name if m else '—')}"
    stats = "".join([
        _stat("👥", "SERVER MEMBERS", f"{len(members)}", f"{len(g0.members) - len(members)} bots"),
        _stat("✨", "TOTAL XP", f"{total_xp:,}", f"across {earners} earners{topname}"),
        _stat("🏆", "XP EARNERS", f"{earners}", f"of {len(members)} members"),
        _stat("🧩", "ACTIVE FEATURES", f"{on}/10", "tracked systems on"),
        _stat("💚", "SERVER HEALTH", f"{hon * 20}%", f"{hon}/5 systems on"),
    ])
    return (f"<section class=page id='s{g0.id}-overview'><div class=pagehead>"
            f"<h2>Welcome back, boss</h2><p>Here's what's happening across your server.</p>"
            f"<span class=pill>🛡️ All systems nominal</span></div>"
            f"<div class=gridstats>{stats}</div>"
            f"<div class=grid3>{_health(health_items)}{_leaders(b, g0)}{_weekheat(b, guilds)}</div>"
            f"</section>")


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
        body = _overview(b, glist, g0, b.get_config(g0.id))
        for g in glist:
            body += _guild_block(g, b.get_config(g.id), key)
        disc = _disc()
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
