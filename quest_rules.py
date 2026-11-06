"""Shared rules for proof-free timed quests and the strict fun-command whitelist."""
import re


def timed_play_quest(item):
    # Recognize the saved community play quest and its existing claim snapshots.
    if item.get('challenge') == 'community-play' or item.get('name', item.get('title', '')).strip().casefold() == 'winter warm-up':
        return True
    name = re.sub(r"[^a-z0-9]+", " ", item.get("name", item.get("title", "")).lower()).strip()
    return bool(re.fullmatch(r"(?:play (?:for )?30 (?:minutes|mins)(?: quest)?|play wigglesworth for 30 (?:minutes|mins)(?: quest)?)", name))


def explicitly_whitelisted(member, config):
    return (not getattr(member, "bot", False) and
            (member.id in config.get("whitelisted_user_ids", []) or
             any(role.id in config.get("whitelisted_role_ids", []) for role in member.roles)))
