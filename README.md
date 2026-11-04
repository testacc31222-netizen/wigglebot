# Wigglesworth Bot (security-first server bot)

Built for the Wigglesworth Discord server. Same engine as our guard bot,
fresh identity + data.

## Security (the point of this bot)
- **Anti-raid**: 5 joins in 15s → lock all channels first, ban the initiator,
  punish followers, auto-unlock. Invite used is tracked to find the recruiter.
- **Automod**: invites deleted by default, optional link filter, mention-spam
  strikes → auto-timeout on 3rd.
- **Lockdown**: `.lockdown` / `.unlock` / `.raidmode on|off` (Admin).
- **Whitelist**: `.whitelist add @user` — never auto-punished.
- **Owner kill-switch**: `.shutdown` / `.disable` / `.enable` (OWNER_ID only).

## Fun / community
- XP levels: `.rank`, `.leaderboard` (+ Admin `.xpadd/.xpset/.leveladd/.levelset`)
- Voice lobby: `.setlobby #voice` for temp VCs
- `.say`, `.diag`, `.raidstatus`

## Staff resignation and update announcements

Configured bot owners (`OWNER_ID` / `OWNER_IDS`) manage these features. The
existing moderation whitelist and administrator permissions do not grant update
publishing access.

- `.staffquitsetup` opens the owner-only setup UI: pick roles, pick a text channel, then **Save and post panel**. The posted **Leave staff team** button survives bot restarts and opens a private confirmation for each staff member.
- `.staffquitroles @Staff @Moderator` is an optional shortcut to replace the list of roles removed on resignation.
- `.staffquitroles` shows that list; `.staffquitoff` disables resignations.
- Staff use `.staffquit` (or `.resign`), review the roles, then choose **Leave staff team** or **Cancel** within two minutes. Only the requesting member can confirm.
- The bot needs **Manage Roles** and a highest role above the departing member and all configured roles. Server owners cannot resign through role automation.
- `.updateowner add @Member`, `.updateowner remove @Member`, `.updateowner list` manage the separate per-server publishing whitelist. Configured bot owners can also publish.
- `.updatesetup discord #updates @DiscordUpdates` sets the Discord update channel/ping role.
- `.updatesetup wigglesworth #game-updates @GameUpdates` sets the game update channel/ping role.
- `.updatepost` opens the category chooser, message editor, and private preview. **Publish update** sends the category heading, message, and assigned role ping at the end. Only the configured role can be pinged; mentions in the message cannot ping other users/roles or everyone.
- Update messages support Discord Markdown and are limited to 1,800 characters. The bot needs channel access and permission to mention the assigned role (or the role must be mentionable).

Settings use the existing persistent guild configuration. Confirmations and
previews expire after two minutes and are not resumed after a restart. No live
roles or announcements are changed by installing these features.

Shared embed cards retain their fields, avatars, footers, colors, and timestamps;
decorative emoji in headings/field labels are replaced by clean text, with one
segment-bar style across progress displays. Discord controls the card geometry,
font, and button appearance.

## Setup (separate from the other bot!)
1. Developer Portal → **new application** (don't reuse the old token) →
   Bot → copy token, enable **Server Members + Message Content** intents.
2. Invite with the same permission set (Kick/Ban/Timeout, Manage
   Channels/Roles/Guild, Send Messages).
3. Run:
```powershell
cd C:\Users\testa\wigglesworth-bot
Copy-Item .env.example .env
notepad .env   # DISCORD_TOKEN=new token, OWNER_ID=you
pip install -r requirements.txt
python bot.py
```
Keep this and the old bot in **separate** PowerShell windows (one process each).

