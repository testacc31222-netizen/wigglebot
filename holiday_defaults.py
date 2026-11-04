"""Approved event content and the explicitly requested one-time announcement."""
CHALLENGES = {
    'santa-helper': ('Santa’s Helper', 'Help 3 players in-game.', 30, 'daily'),
    'snow-patrol': ('Snow Patrol', 'Actively moderate for 30 minutes.', 40, 'daily'),
    'holiday-spirit': ('Holiday Spirit', 'Welcome 3 new community members.', 25, 'daily'),
    'holiday-watch': ('Holiday Watch', 'Submit a valid moderator report.', 35, 'daily'),
    'winter-guardian': ('Winter Guardian', 'Be active in-game for 5 days.', 200, 'weekly'),
    'holiday-host': ('Holiday Host', 'Host or help with a community event.', 175, 'weekly'),
    'festive-teamwork': ('Festive Teamwork', 'Complete a challenge with another staff member.', 100, 'weekly'),
    'holiday-memories': ('Holiday Memories', 'Take a group photo with 4 other staff members.', 150, 'weekly'),
}
MILESTONES = [(1, 'Holiday Rookie Role'), (3, '500 Gold'), (5, 'Holiday Staff Reward · items of your choice'), (8, '1,000 Gold'), (10, 'Exclusive Holiday Cosmetic'), (15, 'Winter Legend Title'), (20, 'Grand Holiday Reward')]
COMMUNITY_CHALLENGES = {
    'community-play': ('Winter Warm-up', 'Play Wigglesworth for 20 minutes. Submit screenshots showing the start and end of your session.', 30, 'daily'),
    'community-help': ('Helping Hand', 'Help one player with a genuine gameplay question or task. Submit evidence of the help; do not stage requests.', 25, 'daily'),
    'community-photo': ('Holiday Snapshot', 'Take an original screenshot of a fun in-game moment. Submit a new screenshot each reset.', 20, 'daily'),
    'community-team': ('Winter Duo', 'Play together with another community member for 15 minutes. Submit start/end screenshots showing both players.', 35, 'daily'),
    'community-days': ('Winter Regular', 'Play on three different days this week. Submit dated screenshots from each day.', 150, 'weekly'),
    'community-event': ('Event Explorer', 'Participate in a community event. Submit proof of participation; you do not need to host it.', 150, 'weekly'),
    'community-group': ('Festive Friends', 'Take an in-game group photo with two other willing community members. Everyone may submit their own distinct photo.', 100, 'weekly'),
    'community-creative': ('Holiday Creator', 'Create an original Wigglesworth holiday drawing, short clip, or screenshot story. Submit your creation and evidence it is yours.', 125, 'weekly'),
}


def eligible(member, config):
    if getattr(member, 'bot', False):
        return False
    return config.get('participation', 'roles') == 'community' or any(r.id in config['staff_roles'] for r in member.roles)


def visible_challenge(cid, challenge, config):
    if config.get('participation', 'roles') != 'community':
        return not cid.startswith('community-')
    return cid not in CHALLENGES and challenge.get('audience') != 'staff'
ANNOUNCEMENT_CHANNEL = 1566961707622994012
ANNOUNCEMENT_GUILD = 1551097539804008580
ANNOUNCEMENT_ROLE = 1551589273264984114
ANNOUNCEMENT = """# Wigglesworth Staff Holiday Pass
**November 2 – December 31 · Optional for all staff**

Complete challenges, earn **Snowflakes**, and unlock rewards. **100 Snowflakes = 1 level.**

## Daily challenges
• **Santa’s Helper:** Help 3 players in-game — **30**
• **Snow Patrol:** Moderate for 30 minutes — **40**
• **Holiday Spirit:** Welcome 3 new members — **25**
• **Holiday Watch:** Submit a valid moderator report — **35**

## Weekly challenges
• **Winter Guardian:** Be active in-game on 5 days — **200**
• **Holiday Host:** Host or help with an event — **175**
• **Festive Teamwork:** Complete a challenge with another staff member — **100**
• **Holiday Memories:** Take a group photo with 4 other staff — **150**

## Level rewards
**1** · Holiday Rookie Role
**3** · 500 Gold
**5** · Holiday Staff Reward — items of your choice
**8** · 1,000 Gold
**10** · Exclusive Holiday Cosmetic
**15** · Winter Legend Title
**20** · Grand Holiday Reward

## How to join
Use the **Holiday Pass panel** to choose a challenge and open a submission in <#1566963036236025956>. Upload your proof, then select **Submit proof for review**. Management approval awards your Snowflakes.

Each challenge can be claimed once per reset. Reused or misleading proof is not accepted. Spend Snowflakes in the **Holiday Shop** without lowering your level; management delivers rewards.

Progress carries into December. Watch for Christmas surprises and **secret missions worth up to 500 Snowflakes**. The event and shop close **December 31**.

**Happy holidays, Wigglesworth staff!**

<@&1551589273264984114>"""

