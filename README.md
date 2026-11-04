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

### Holiday Pass (2026 season)

Management opens `.holidaymanage` (or `.holidayorders`) and clicks **Open management
panel** for a private purchase queue. Select an order to approve/reject it or mark
an approved reward delivered. Open orders and history have pagination. Existing
forum-based purchases are also visible here; new shop requests create no forum
posts. Staff can see recent purchase statuses under **My Holiday Pass**.

Configured bot owners also see a **Grant Snowflakes** member picker in that panel.
Choose a current staff member, enter an amount and reason. Grants increase lifetime
earnings and spendable balance, are recorded in the ledger, and cannot be applied
twice from the same submission.

`.snowflakeleaderboard` (alias `.snowboard`) posts a ranking for Snowflakes earned
in the current two-hour round. Rounds reset at 00:00, 02:00, 04:00, etc. in
America/Phoenix; grants and challenge approvals both count. Pass levels and wallet
balances never reset. The latest posted board refreshes automatically each minute
and has a manual Refresh button. Earlier boards can be refreshed manually.

Run `.holidaysetup` as a configured bot owner. Use the role pickers for eligible
staff and management reviewers, select a **Forum Channel**, add challenges, and
post the staff panel in the current channel. Use a staff-only forum with required
tags disabled; the bot needs View Channel, Send Messages, Send Messages in Threads,
Embed Links, Read Message History, and permission to create forum posts. Participants
need to see and reply to their posts and attach files. Forum submissions are not
private from other members who can view that forum.

Staff use **My Holiday Pass**, **Challenges & proof**, and **Holiday shop**.
Challenge selection creates a forum submission; the member uploads image/video
attachments there, then selects **Submit proof for review**. The bot checks up to
100 recent messages, accepts up to five files of at most 25 MB each, and compares
file hashes against prior submissions. This detects identical reuploads, not
altered/cropped or misleading evidence: management still verifies authenticity.
Links alone are not evidence uploads in this version. A rejected claim consumes
that challenge/reset slot; management should review carefully.

Reviewers use persistent **Approve** / **Reject** buttons in the forum post.
They cannot approve their own proof or purchases. Approvals cannot award twice.
The shop uses the supplied 15 items and prices, with Autumn/Christmas boxes
restricted to November/December. Purchase approval rechecks funds and deducts the
spendable balance; **Mark delivered** tracks manual management fulfilment. Gold,
cosmetics, role grants, temporary role colors, raffle entries, and level rewards
are delivered by management, not automatically granted in-game. The panel marks
Level 20 reward eligibility at 2,000 lifetime Snowflakes.

Lifetime earned Snowflakes determine level (100 per level); purchases never lower
that total. The event runs November 2 through December 31, 2026, America/Phoenix.
Daily reset periods start at local midnight, weekly periods every seven days from
November 2. Secret missions have owner-configured deadlines and rewards up to 500.
Submitted proofs can be reviewed after event close; new claims and purchase
approvals are closed then. November balances carry into December unchanged.

All balances, review states, challenges, proof hashes, and thread IDs persist in
`holiday_pass.json` under the existing `DATA_DIR` volume using atomic replacement.
Keep this data private and back it up. Panels/review controls survive restarts.
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

