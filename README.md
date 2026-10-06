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
