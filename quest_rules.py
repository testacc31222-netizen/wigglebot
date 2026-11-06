"""Shared rules for proof-free timed quests and the strict fun-command whitelist."""
import re


def timed_play_quest(item):
    # Match the requested custom quest without changing moderation or 20-minute tasks.
    name = re.sub(r"[^a-z0-9]+", " ", item.get("name", item.get("title", "")).lower()).strip()
    return bool(re.fullmatch(r"(?:play (?:for )?30 (?:minutes|mins)(?: quest)?|play wigglesworth for 30 (?:minutes|mins)(?: quest)?)", name))


def explicitly_whitelisted(member, config):
    return (not getattr(member, "bot", False) and
            (member.id in config.get("whitelisted_user_ids", []) or
             any(role.id in config.get("whitelisted_role_ids", []) for role in member.roles)))
