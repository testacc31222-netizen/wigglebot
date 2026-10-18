"""Wigglesworth design system — one visual language for every embed and card.

Every command response, leaderboard, profile card, notification and alert is
built from these helpers so the whole bot always looks like one product:

* a single palette (Wigglesworth violet + semantic accents),
* one progress-bar style (emoji blocks),
* one footer/timestamp convention,
* purpose-built card builders for rank, leaderboard, message stats, level-ups.

This module must stay dependency-free (discord.py only) and must never import
the bot module — bot.py imports *this*.
"""
from __future__ import annotations

from datetime import datetime, timezone

import discord

# ---------------------------------------------------------------------------
# palette — the Wigglesworth identity
# ---------------------------------------------------------------------------
PALETTE = {
    "brand": 0x7B6CF6,    # violet — primary identity
    "info": 0x5AA9FF,     # sky blue — neutral information
    "success": 0x3DD68C,  # mint — confirmations / wins
    "warning": 0xF2B33D,  # amber — caution / pending
    "error": 0xF65C78,    # rose — failures / denials
    "xp": 0x9D7BFF,       # leveling accent
    "msg": 0x4FC3F7,      # message statistics
    "gold": 0xF5C451,     # podium / giveaways / trivia
    "muted": 0x4E5058,    # empty states
}

# legacy "kind" aliases (bot.E + security alerts) mapped onto the palette
_KINDS = {
    "raid": "error", "night": "brand", "day": "warning", "win": "gold",
    "good": "success", "bad": "error",
    "brand": "brand", "info": "info", "success": "success",
    "warning": "warning", "error": "error", "xp": "xp",
    "msg": "msg", "gold": "gold", "muted": "muted",
}

FOOTER = "Wigglesworth"
DIVIDER = "━━━━━━━━━━━━━━"

# bar themes: (filled, empty)
_BARS = {
    "xp": ("🟪", "⬛"),
    "msg": ("🟦", "⬛"),
    "vote": ("🟩", "⬛"),
    "gold": ("🟨", "⬛"),
}

GLYPHS = {"ok": "✅", "err": "❌", "warn": "⚠️", "info": "ℹ️", "tip": "💡"}


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def color(kind: str = "info") -> discord.Color:
    """Resolve a legacy kind or palette key to a discord.Color."""
    key = _KINDS.get(kind, kind)
    return discord.Color(PALETTE.get(key, PALETTE["info"]))


def now() -> datetime:
    return datetime.now(timezone.utc)


def fmt(n) -> str:
    """1234 -> '1,234' (safe for junk input)."""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def short(n) -> str:
    """Compact number for tight spots: 1.2K / 3.4M."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return str(n)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        v = f"{n / 1000:.1f}"
        return (v.removesuffix(".0") + "K")
    v = f"{n / 1_000_000:.1f}"
    return (v.removesuffix(".0") + "M")


def clamp01(pct) -> float:
    try:
        return max(0.0, min(1.0, float(pct)))
    except (TypeError, ValueError):
        return 0.0


def pct_text(pct) -> str:
    return f"{round(clamp01(pct) * 100)}%"


def bar(pct, length: int = 10, kind: str = "xp") -> str:
    """Emoji progress bar. pct may be 0..1 or (filled, total) ratio float."""
    pct = clamp01(pct)
    fill, empty = _BARS.get(kind, _BARS["xp"])
    filled = round(pct * length)
    if pct > 0 and filled == 0:
        filled = 1
    return fill * filled + empty * (length - filled)


def progress(xp: int, base: int, nxt: int) -> tuple[float, int, int]:
    """(fraction, xp_into_level, xp_needed_for_level)."""
    span = max(nxt - base, 1)
    into = max(int(xp) - base, 0)
    return clamp01(into / span), into, span


def cut(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# embed factory + quick response embeds
# ---------------------------------------------------------------------------
def embed(title: str = "", desc: str = "", kind: str = "info", *,
          footer: str | None = None, timestamp: bool = True) -> discord.Embed:
    """Standard Wigglesworth embed: palette color, footer, UTC timestamp."""
    e = discord.Embed(title=title or None, description=desc or None,
                      color=color(kind))
    if timestamp:
        e.timestamp = now()
    if footer is None:
        footer = FOOTER
    if footer:
        e.set_footer(text=footer[:2048])
    return e


def ok(desc: str, title: str = "Success") -> discord.Embed:
    return embed(f"{GLYPHS['ok']} {title}", desc, "success")


def err(desc: str, title: str = "Something went wrong") -> discord.Embed:
    return embed(f"{GLYPHS['err']} {title}", desc, "error")


def warn(desc: str, title: str = "Heads up") -> discord.Embed:
    return embed(f"{GLYPHS['warn']} {title}", desc, "warning")


def info(desc: str, title: str = "Note") -> discord.Embed:
    return embed(f"{GLYPHS['info']} {title}", desc, "info")


def tip(desc: str, title: str = "Tip") -> discord.Embed:
    return embed(f"{GLYPHS['tip']} {title}", desc, "info")


# ---------------------------------------------------------------------------
# personal cards
# ---------------------------------------------------------------------------
def rank_embed(*, name: str, avatar: str | None, level: int, xp: int,
               base: int, nxt: int, rank: int | None, tracked: int,
               guild_name: str, messages: int | None = None) -> discord.Embed:
    """The profile / rank card shown by `.rank`."""
    frac, into, needed = progress(xp, base, nxt)
    e = embed(f"⚡ Level {level}", kind="xp",
              footer=f"{FOOTER} • Rank Card • {guild_name}")
    if avatar:
        e.set_author(name=cut(name, 220), icon_url=avatar)
        e.set_thumbnail(url=avatar)
    else:
        e.set_author(name=cut(name, 220))
    e.description = (
        f"{bar(frac, 12, 'xp')} **{pct_text(frac)}**\n"
        f"`{fmt(into)} / {fmt(needed)} XP` — **{fmt(max(nxt - xp, 0))} XP** to Level {level + 1}"
    )
    e.add_field(name="🏆 Server Rank",
                value=f"**#{rank}** of {fmt(tracked)}" if rank else "*Unranked*",
                inline=True)
    e.add_field(name="✨ Total XP", value=f"**{fmt(xp)}**", inline=True)
    if messages:
        e.add_field(name="💬 Messages", value=f"**{fmt(messages)}**", inline=True)
    else:
        e.add_field(name="🎯 Next Level", value=f"Level {level + 1}", inline=True)
    return e


def levelup_embed(*, name: str, avatar: str | None, level: int, xp: int,
                  base: int, nxt: int, guild_name: str) -> discord.Embed:
    """Level-up celebration card."""
    frac, into, needed = progress(xp, base, nxt)
    e = embed("🎉  Level Up!", kind="xp",
              footer=f"{FOOTER} • keep chatting to earn XP")
    if avatar:
        e.set_thumbnail(url=avatar)
    e.description = (
        f"**{cut(name, 80)}** just reached **Level {level}**!\n"
        f"{bar(frac, 12, 'xp')} **{pct_text(frac)}** to Level {level + 1}"
    )
    e.add_field(name="✨ Total XP", value=f"**{fmt(xp)}**", inline=True)
    e.add_field(name="🎯 Next Level",
                value=f"**{fmt(max(nxt - xp, 0))} XP** to go", inline=True)
    return e


def messages_embed(*, name: str, avatar: str | None, guild_name: str,
                   total: int, today: int, week: int, month: int,
                   rank: int | None, tracked: int, top_total: int,
                   since: float | None = None,
                   level: int | None = None, xp: int = 0,
                   base: int = 0, nxt: int = 0) -> discord.Embed:
    """The `.messages` statistics card."""
    e = embed("📊  Message Statistics", kind="msg",
              footer=f"{FOOTER} • {guild_name}")
    if avatar:
        e.set_author(name=cut(name, 220), icon_url=avatar)
        e.set_thumbnail(url=avatar)
    else:
        e.set_author(name=cut(name, 220))
    e.description = "Your activity in this server — updated live."
    e.add_field(name="📨 Total", value=f"**{fmt(total)}**", inline=True)
    e.add_field(name="📅 Today", value=f"**{fmt(today)}**", inline=True)
    e.add_field(name="📆 Last 7 days", value=f"**{fmt(week)}**", inline=True)
    e.add_field(name="🗓️ Last 30 days", value=f"**{fmt(month)}**", inline=True)

    if total > 0 and top_total > 0:
        share = total / top_total
        share_line = f"{bar(share, 10, 'msg')} {pct_text(share)} of #1"
    else:
        share_line = "Send a message to get on the board!"
    e.add_field(name="🏆 Message Rank",
                value=(f"**#{rank}** of {fmt(tracked)} tracked\n{share_line}"
                       if rank else share_line),
                inline=False)

    if level is not None and nxt > base:
        frac, into, needed = progress(xp, base, nxt)
        e.add_field(
            name="⚡ Level Progress",
            value=(f"**Level {level}** · {fmt(xp)} XP total\n"
                   f"{bar(frac, 10, 'xp')} {pct_text(frac)} — "
                   f"**{fmt(max(nxt - xp, 0))} XP** to Level {level + 1}"),
            inline=False)

    if since:
        stamp = datetime.fromtimestamp(float(since), tz=timezone.utc)
        e.set_footer(text=f"{FOOTER} • tracking since {stamp.strftime('%b %d, %Y')} (days in UTC)")
    return e


# ---------------------------------------------------------------------------
# leaderboard
# ---------------------------------------------------------------------------
def leaderboard_embed(*, guild_name: str, guild_icon: str | None,
                      top: list[dict], others: list[dict],
                      you: dict | None, total_xp: int, tracked: int,
                      champion_avatar: str | None = None) -> discord.Embed:
    """Server leaderboard: medal podium for the top 3, compact list below.

    top:    up to 3 dicts -> name, level, xp, base, nxt, is_you
    others: dicts -> rank, name, level, xp, is_you
    you:    dict with rank/name/level/xp when the requester is off the list
    """
    medals = ("🥇", "🥈", "🥉")
    e = embed(kind="gold", footer=f"{FOOTER} • {guild_name}")
    e.title = "🏆  Server Leaderboard"
    if guild_icon:
        e.set_author(name=cut(guild_name, 220), icon_url=guild_icon)
    else:
        e.set_author(name=cut(guild_name, 220))
    if champion_avatar:
        e.set_thumbnail(url=champion_avatar)

    parts: list[str] = []
    for i, row in enumerate(top[:3]):
        frac, _, _ = progress(row["xp"], row["base"], row["nxt"])
        you_mark = "  ⬅ *you*" if row.get("is_you") else ""
        parts.append(
            f"{medals[i]} **{cut(row['name'], 24)}** · `LVL {row['level']}`{you_mark}\n"
            f"{bar(frac, 10, 'xp')} **{fmt(row['xp'])} XP**"
        )
    if others:
        parts.append(DIVIDER)
        for row in others:
            you_mark = " ⬅ *you*" if row.get("is_you") else ""
            tag = f"**#{row['rank']:02d}**" if row.get("is_you") else f"`#{row['rank']:02d}`"
            parts.append(
                f"{tag} **{cut(row['name'], 20)}** · Lvl {row['level']} · {fmt(row['xp'])} XP{you_mark}"
            )
    if you is not None:
        parts.append(DIVIDER)
        parts.append(
            f"⬅ **You** — `#{you['rank']:02d}` · Lvl {you['level']} · {fmt(you['xp'])} XP"
        )
    e.description = "\n".join(parts)[:4096]
    e.set_footer(text=(f"{FOOTER} • {fmt(tracked)} members tracked · "
                       f"{short(total_xp)} total XP"))
    return e


# ---------------------------------------------------------------------------
# XP explainer
# ---------------------------------------------------------------------------
def xp_info_embed(*, lo: int, hi: int, cooldown: int, min_length: int,
                  commands_off: bool) -> discord.Embed:
    e = embed("⚡  How XP & Levels Work", kind="xp",
              footer=f"{FOOTER} • leveling guide")
    avg = (lo + hi) / 2
    per_hour = int(3600 / max(cooldown, 1) * avg)
    e.description = (
        "Chat naturally and you'll gain XP automatically — no commands needed.\n"
        f"{DIVIDER}"
    )
    e.add_field(
        name="✨ Earning XP",
        value=(f"• **{lo}–{hi} XP** per qualifying message\n"
               f"• One XP gain every **{cooldown}s**\n"
               f"• ~**{fmt(per_hour)} XP/hour** if you keep talking\n"
               f"• Bonus **+50 XP** for winning `.trivia`"),
        inline=False)
    qualify = (f"• Messages with at least **{min_length} characters**\n"
               "• Bots and webhooks never earn XP\n"
               "• No XP during raid lockdowns\n"
               "• Spam, repeats and spammy pings earn nothing")
    if commands_off:
        qualify += "\n• Bot commands don't earn XP — real talk does"
    e.add_field(name="✅ Qualifying messages", value=qualify, inline=False)
    e.add_field(
        name="🎯 Levels",
        value=("Level **n** needs **100 × n²** total XP:\n"
               "`Lvl 1 = 100` · `Lvl 5 = 2,500` · `Lvl 10 = 10,000` · `Lvl 20 = 40,000`"),
        inline=False)
    e.add_field(
        name="📌 Handy commands",
        value=("`.rank` — your profile & progress card\n"
               "`.leaderboard` — server top members\n"
               "`.messages` — your message statistics"),
        inline=False)
    return e
