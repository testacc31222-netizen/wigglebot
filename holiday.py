"""Holiday Pass panels, forum evidence, management reviews and shop orders."""
from __future__ import annotations
import hashlib
import asyncio
import uuid
from datetime import datetime, timezone
import discord
from discord.ext import commands
import ui
from holiday_store import HolidayStore, LedgerError, SHOP, PHOENIX
from holiday_defaults import MILESTONES, ANNOUNCEMENT, ANNOUNCEMENT_CHANNEL, ANNOUNCEMENT_GUILD, ANNOUNCEMENT_ROLE


def setup(bm):
    if bm.bot.get_command('holidaysetup'):
        return
    ledger = HolidayStore(bm.DATA_DIR / 'holiday_pass.json')
    now = lambda: datetime.now(timezone.utc)

    def owner(uid):
        return uid in bm.OWNER_IDS

    def staff(member):
        return any(r.id in ledger.guild(member.guild.id)['staff_roles'] for r in member.roles)

    def management(member):
        return owner(member.id) or any(r.id in ledger.guild(member.guild.id)['review_roles'] for r in member.roles)

    async def reply(i, text, error=False):
        em = ui.err(text) if error else ui.info(text)
        if i.response.is_done():
            await i.followup.send(embed=em, ephemeral=True)
        else:
            await i.response.send_message(embed=em, ephemeral=True)

    def config(gid, **kw):
        ledger.change(gid, lambda g: g.update(kw))

    def lookup(i):
        g = ledger.guild(i.guild_id)
        for kind in ('claims', 'orders'):
            for key, item in g[kind].items():
                if item.get('thread') == i.channel_id:
                    return kind, key, item
        raise LedgerError('This is not a Holiday Pass submission post.')

    async def open_post(i, title, description, kind, key):
        forum = i.guild.get_channel(ledger.guild(i.guild_id)['forum'] or 0)
        if not isinstance(forum, discord.ForumChannel):
            raise LedgerError('An owner must select a proof forum in holidaysetup first.')
        result = await forum.create_thread(name=title[:100], embed=ui.embed(title, description, 'brand'), view=ReviewPanel(), allowed_mentions=discord.AllowedMentions.none())
        ledger.change(i.guild_id, lambda g: g[kind][key].update(thread=result.thread.id))
        return result.thread

    class Personal(discord.ui.View):
        def __init__(self, uid):
            super().__init__(timeout=300)
            self.uid = uid

        async def interaction_check(self, i):
            if i.user.id != self.uid:
                await reply(i, 'Open your own Holiday Pass panel.', True)
                return False
            return True

    class ReviewPanel(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Submit proof for review', style=discord.ButtonStyle.primary, custom_id='holiday:submit:v1')
        async def submit(self, i, button):
            await i.response.defer(ephemeral=True)
            try:
                kind, key, item = lookup(i)
                if kind != 'claims' or item['user'] != i.user.id or not staff(i.user):
                    raise LedgerError('Only the staff member who opened this challenge post can submit it.')
                if item['status'] != 'draft':
                    raise LedgerError('This submission has already been sent for review.')
                hashes = []
                # Hash actual files: changing a filename or reuploading identical
                # bytes does not allow the same evidence to earn XP again.
                async for message in i.channel.history(limit=100):
                    if message.author.id != i.user.id:
                        continue
                    for a in message.attachments:
                        if not (a.content_type or '').startswith(('image/', 'video/')):
                            continue
                        if a.size > 25 * 1024 * 1024:
                            raise LedgerError('Use screenshots or video clips under 25 MB each.')
                        if len(hashes) >= 5:
                            raise LedgerError('Use at most five evidence files in a submission.')
                        hashes.append(hashlib.sha256(await a.read()).hexdigest())
                ledger.submit(i.guild_id, key, list(set(hashes)), now())
                await i.channel.send(embed=ui.info(f'<@{i.user.id}> submitted **{item["title"]}** for management review.\nReward: **{item["points"]} Snowflakes**. Approval has not been granted yet.'), allowed_mentions=discord.AllowedMentions.none())
                await reply(i, 'Your proof is awaiting management review.')
            except (LedgerError, discord.HTTPException, OSError) as e:
                await reply(i, str(e), True)

        async def decide(self, i, action):
            if not management(i.user):
                return await reply(i, 'Management reviewers only.', True)
            await i.response.defer(ephemeral=True)
            try:
                kind, key, item = lookup(i)
                if kind == 'claims':
                    if action == 'delivered':
                        raise LedgerError('Delivery applies to shop purchases only.')
                    result = ledger.review_claim(i.guild_id, key, i.user.id, action == 'approved')
                    message = f'Challenge **{result["title"]}**: **{result["status"]}**.'
                    if result['status'] == 'approved':
                        message += f'\n**{result["points"]} Snowflakes** awarded to <@{result["user"]}>.'
                else:
                    result = ledger.review_order(i.guild_id, key, i.user.id, action, now())
                    message = f'Purchase **{result["item"]}**: **{result["status"]}**.'
                    if action == 'approved':
                        message += f'\n**{result["cost"]} Snowflakes** deducted. Management must deliver the reward, then select **Mark delivered**.'
                await i.channel.send(embed=ui.embed('Holiday Pass · Review recorded', message + f'\nReviewed by <@{i.user.id}>.', 'success' if action != 'rejected' else 'warning'), allowed_mentions=discord.AllowedMentions.none())
                await reply(i, 'Review saved.')
            except (LedgerError, discord.HTTPException, OSError) as e:
                await reply(i, f'{e}\nCheck the post and progress before retrying.', True)

        @discord.ui.button(label='Approve', style=discord.ButtonStyle.success, custom_id='holiday:approve:v1')
        async def approve(self, i, button):
            await self.decide(i, 'approved')

        @discord.ui.button(label='Reject', style=discord.ButtonStyle.danger, custom_id='holiday:reject:v1')
        async def reject(self, i, button):
            await self.decide(i, 'rejected')

        @discord.ui.button(label='Mark delivered', custom_id='holiday:delivered:v1')
        async def delivered(self, i, button):
            await self.decide(i, 'delivered')

    class ChallengePicker(Personal):
        def __init__(self, uid, gid):
            super().__init__(uid)
            g = ledger.guild(gid)
            options = [discord.SelectOption(label=c['name'][:100], value=cid, description=f'{c["points"]} Snowflakes · {c["cadence"]}') for cid, c in g['challenges'].items() if c.get('enabled', True) and (not c.get('expires') or c['expires'] > now().timestamp())]
            picker = discord.ui.Select(placeholder='Choose a challenge', options=options[:25])
            picker.callback = self.choose
            self.add_item(picker)
            self.picker = picker

        async def choose(self, i):
            if not staff(i.user):
                return await reply(i, 'This pass is for configured staff roles.', True)
            await i.response.defer(ephemeral=True)
            key = None
            try:
                key = ledger.create_claim(i.guild_id, i.user.id, self.picker.values[0], now())
                claim = ledger.guild(i.guild_id)['claims'][key]
                thread = await open_post(i, f'Proof · {i.user.display_name} · {claim["title"]}', f'**Staff member:** <@{i.user.id}>\n**Challenge:** {claim["title"]}\n**Reward:** {claim["points"]} Snowflakes\n**Task:** {claim.get("instructions", "Follow the challenge announcement.")}\n\nUpload screenshot/video evidence as a reply in this post, then click **Submit proof for review**. Only management can approve it.', 'claims', key)
                await reply(i, f'Your proof post is ready: {thread.mention}\nUpload your evidence there, then submit it for review.')
            except (LedgerError, discord.HTTPException, OSError) as e:
                if key and not ledger.guild(i.guild_id)['claims'][key].get('thread'):
                    ledger.change(i.guild_id, lambda g: g['claims'].pop(key, None))
                await reply(i, str(e), True)

    class ShopPicker(Personal):
        def __init__(self, uid):
            super().__init__(uid)
            month = now().astimezone(PHOENIX).month
            self.picker = discord.ui.Select(placeholder='Choose a reward', options=[discord.SelectOption(label=name, value=item, description=f'{cost:,} Snowflakes · management delivery') for item, name, cost, rotation in SHOP if rotation in (0, month)])
            self.picker.callback = self.choose
            self.add_item(self.picker)

        async def choose(self, i):
            if not staff(i.user):
                return await reply(i, 'This shop is for staff participants.', True)
            await i.response.defer(ephemeral=True)
            key = None
            try:
                key = ledger.request_order(i.guild_id, i.user.id, self.picker.values[0], now())
                order = ledger.guild(i.guild_id)['orders'][key]
                thread = await open_post(i, f'Purchase · {i.user.display_name} · {order["item"]}', f'**Staff member:** <@{i.user.id}>\n**Reward:** {order["item"]}\n**Cost:** {order["cost"]} Snowflakes\n\nAwaiting management approval. No balance has been deducted yet. Purchases are final once approved; management delivers the reward separately.', 'orders', key)
                await reply(i, f'Purchase request created: {thread.mention}')
            except (LedgerError, discord.HTTPException, OSError) as e:
                if key and not ledger.guild(i.guild_id)['orders'][key].get('thread'):
                    ledger.change(i.guild_id, lambda g: g['orders'].pop(key, None))
                await reply(i, str(e), True)

    class HolidayPanel(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        async def interaction_check(self, i):
            if not i.guild or not staff(i.user):
                await reply(i, 'This optional Holiday Pass is for configured staff roles.', True)
                return False
            return True

        @discord.ui.button(label='My Holiday Pass', style=discord.ButtonStyle.primary, custom_id='holiday:progress:v1')
        async def progress(self, i, button):
            g = ledger.guild(i.guild_id)
            a = g['accounts'].get(str(i.user.id), {'earned': 0, 'balance': 0})
            level = a['earned'] // 100
            em = ui.embed('Holiday Pass · Your progress', f'**Level {level}**\n{ui.bar((a["earned"] % 100) / 100, 10)} {a["earned"] % 100}/100 to the next level', 'brand')
            em.add_field(name='Lifetime Snowflakes', value=f'{a["earned"]:,}')
            em.add_field(name='Spendable balance', value=f'{a["balance"]:,}')
            em.add_field(name='Level 20 reward', value='Eligible — contact management to claim.' if level >= 20 else f'{max(0, 2000-a["earned"]):,} more Snowflakes needed.', inline=False)
            em.add_field(name='Milestone rewards · management delivery', value='\n'.join(f'{"Unlocked" if level >= lvl else "Level " + str(lvl)} · {reward}' for lvl, reward in MILESTONES), inline=False)
            em.add_field(name='Season', value=f'November 2 – December 31, {g["year"]} · America/Phoenix\nSpending does not lower your level. November progress carries into December.', inline=False)
            recent = [f'[ {c.get("title", c.get("item", "Submission"))} · {c["status"]} ](https://discord.com/channels/{i.guild_id}/{c["thread"]})' for kind in ('claims', 'orders') for c in g[kind].values() if c['user'] == i.user.id and c.get('thread')]
            if recent:
                em.add_field(name='Recent submissions & purchases', value='\n'.join(recent[-5:])[:1024], inline=False)
            await i.response.send_message(embed=em, ephemeral=True)

        @discord.ui.button(label='Challenges & proof', custom_id='holiday:challenges:v1')
        async def challenges(self, i, button):
            if not ledger.active(ledger.guild(i.guild_id), now()):
                return await reply(i, 'The event is closed.', True)
            view = ChallengePicker(i.user.id, i.guild_id)
            if not view.picker.options:
                return await reply(i, 'Management has not added any active challenges yet.')
            await i.response.send_message(embed=ui.info('Choose a challenge to open your forum submission. Daily/weekly resets follow the event start in America/Phoenix. Each challenge can be claimed once per reset. Upload original evidence; management verifies it before awarding Snowflakes.', 'Holiday challenges'), view=view, ephemeral=True)

        @discord.ui.button(label='Holiday shop', custom_id='holiday:shop:v1')
        async def shop(self, i, button):
            if not ledger.active(ledger.guild(i.guild_id), now()):
                return await reply(i, 'The Holiday Shop is closed.', True)
            await i.response.send_message(embed=ui.info('Choose an item to request a purchase in the proof forum. Management checks your balance, approves the purchase, and delivers the reward. Purchases are final once approved. Spending never reduces your pass level.', 'Holiday shop'), view=ShopPicker(i.user.id), ephemeral=True)

    class ChallengeModal(discord.ui.Modal, title='Add a Holiday challenge'):
        name = discord.ui.TextInput(label='Challenge name', max_length=80)
        instructions = discord.ui.TextInput(label='What to do and what proof is required', style=discord.TextStyle.paragraph, max_length=1000)
        points = discord.ui.TextInput(label='Snowflakes awarded (1–500)', max_length=3)
        cadence = discord.ui.TextInput(label='Reset: daily, weekly, or secret', max_length=6)
        hours = discord.ui.TextInput(label='Secret mission duration in hours (1–744)', required=False, max_length=3)

        async def on_submit(self, i):
            if not owner(i.user.id):
                return await reply(i, 'Bot owners only.', True)
            try:
                points = int(self.points.value)
                cadence = self.cadence.value.strip().lower()
                if not 1 <= points <= 500 or cadence not in ('daily', 'weekly', 'secret'):
                    raise ValueError()
                duration = int(self.hours.value) if cadence == 'secret' else 0
                if cadence == 'secret' and not 1 <= duration <= 744:
                    raise ValueError()
                def add(g):
                    if sum(c.get('enabled', True) for c in g['challenges'].values()) >= 25:
                        raise LedgerError('Disable a challenge first; at most 25 can be active.')
                    g['challenges'][uuid.uuid4().hex[:8]] = {'name': self.name.value.strip(), 'instructions': self.instructions.value.strip(), 'points': points, 'cadence': cadence, 'enabled': True, 'expires': now().timestamp() + duration*3600 if duration else None}
                ledger.change(i.guild_id, add)
                await reply(i, 'Challenge added. It is now available in the staff panel.')
            except (ValueError, OSError) as e:
                await reply(i, str(e) if isinstance(e, LedgerError) else 'Use 1–500 Snowflakes, daily/weekly/secret, and 1–744 hours for a secret mission.', True)

    class SetupPanel(Personal):
        async def interaction_check(self, i):
            return await super().interaction_check(i) and owner(i.user.id)

        @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Staff roles eligible to participate', min_values=1, max_values=25, row=0)
        async def staff_roles(self, i, select):
            if any(r.is_default() for r in select.values):
                return await reply(i, 'Choose staff roles rather than @everyone.', True)
            config(i.guild_id, staff_roles=[r.id for r in select.values])
            await reply(i, 'Eligible staff roles saved.')

        @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Management roles allowed to review', min_values=1, max_values=25, row=1)
        async def reviewers(self, i, select):
            if any(r.is_default() for r in select.values):
                return await reply(i, 'Choose management roles rather than @everyone.', True)
            config(i.guild_id, review_roles=[r.id for r in select.values])
            await reply(i, 'Management reviewer roles saved. Nobody can approve their own submissions.')

        @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder='Choose the proof submissions forum', channel_types=[discord.ChannelType.forum], row=2)
        async def forum(self, i, select):
            config(i.guild_id, forum=select.values[0].id)
            await reply(i, 'Proof forum saved. Ensure staff can view and reply there, and the bot can create posts, send messages, and read history.')

        @discord.ui.button(label='Add challenge', style=discord.ButtonStyle.primary, row=3)
        async def add(self, i, button):
            await i.response.send_modal(ChallengeModal())

        @discord.ui.button(label='Manage challenges', row=3)
        async def manage(self, i, button):
            options = [discord.SelectOption(label=c['name'][:100], value=cid, description='Disable this challenge') for cid, c in ledger.guild(i.guild_id)['challenges'].items() if c.get('enabled', True)]
            if not options:
                return await reply(i, 'No active challenges.')
            view = Personal(i.user.id)
            select = discord.ui.Select(placeholder='Choose a challenge to disable', options=options[:25])
            async def disable(interaction):
                if not owner(interaction.user.id):
                    return await reply(interaction, 'Bot owners only.', True)
                ledger.change(interaction.guild_id, lambda g: g['challenges'][select.values[0]].update(enabled=False))
                await interaction.response.edit_message(embed=ui.info('Challenge disabled. Existing submitted proofs keep their snapshotted reward.'), view=None)
            select.callback = disable
            view.add_item(select)
            await i.response.send_message(embed=ui.info('Disable a task here; add a new task to change its reward or reset. Existing claim history stays intact.'), view=view, ephemeral=True)

        @discord.ui.button(label='Post staff panel here', style=discord.ButtonStyle.success, row=4)
        async def post(self, i, button):
            g = ledger.guild(i.guild_id)
            forum = i.guild.get_channel(g['forum'] or 0)
            if not g['staff_roles'] or not g['review_roles'] or not isinstance(forum, discord.ForumChannel):
                return await reply(i, 'Select staff roles, management roles, and a proof forum first.', True)
            if forum.flags.require_tag:
                return await reply(i, 'Turn off “Require people to select tags” on the proof forum before posting the panel.', True)
            await i.response.defer(ephemeral=True)
            try:
                await i.channel.send(embed=ui.embed('Wigglesworth · Staff Holiday Pass', f'**November 2 – December 31, {g["year"]}**\n\nComplete challenges, upload proof in your forum post, and earn Snowflakes after management approval.\n\n**100 Snowflakes = 1 level**\nSpend Snowflakes in the shop without losing pass levels. Progress carries into December.\n\nParticipation is optional and does not affect your staff position. All rewards are delivered by management.', 'brand'), view=HolidayPanel())
                await reply(i, 'Staff panel posted in this channel. Its buttons and forum review buttons work after restarts.')
            except discord.HTTPException:
                await reply(i, 'Could not post the panel. Check the bot’s channel permissions.', True)

        @discord.ui.button(label='Post Holiday announcement', row=4)
        async def announcement(self, i, button):
            if i.guild_id != ANNOUNCEMENT_GUILD:
                return await reply(i, 'This announcement is configured for the Wigglesworth server.', True)
            if ledger.guild(i.guild_id).get('announcement_message') or ledger.guild(i.guild_id).get('announcement_attempted'):
                return await reply(i, 'The Holiday announcement has already been posted. Check the announcements channel.', True)
            channel = i.guild.get_channel(ANNOUNCEMENT_CHANNEL)
            role = i.guild.get_role(ANNOUNCEMENT_ROLE)
            if not isinstance(channel, discord.TextChannel) or not role:
                return await reply(i, 'The configured announcement channel or ping role is unavailable.', True)
            await i.response.defer(ephemeral=True)
            try:
                config(i.guild_id, announcement_attempted=True)
                message = await channel.send(ANNOUNCEMENT, allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]))
                config(i.guild_id, announcement_message=message.id)
                await reply(i, f'Announcement sent: {message.jump_url}')
            except (discord.HTTPException, OSError):
                await reply(i, 'Could not confirm announcement delivery. Check the channel before trying again.', True)

    @bm.bot.command(name='holidaysetup')
    @commands.guild_only()
    async def holidaysetup(ctx):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        g = ledger.guild(ctx.guild.id)
        em = ui.embed('Holiday Pass · Setup', 'Choose eligible staff, management reviewers, and a proof forum. Add challenges, then post the staff panel in this channel.\n\nSettings save as you select them. Forum posts are visible to everyone with access to that forum; use a staff-only forum.\n\nSeason: November 2 – December 31, 2026 · America/Phoenix. Daily resets at midnight; weekly resets every seven days from November 2.', 'brand')
        em.add_field(name='Current configuration', value=f'Staff roles: {len(g["staff_roles"])} · Reviewer roles: {len(g["review_roles"])}\nForum: ' + (f'<#{g["forum"]}>' if g['forum'] else 'Not selected'))
        await ctx.send(embed=em, view=SetupPanel(ctx.author.id), allowed_mentions=discord.AllowedMentions.none())

    bm.bot.add_view(HolidayPanel())
    bm.bot.add_view(ReviewPanel())

    async def send_requested_announcement():
        """One explicitly requested announcement, never repeated on restarts."""
        await bm.bot.wait_until_ready()
        guild = bm.bot.get_guild(ANNOUNCEMENT_GUILD)
        if guild is None:
            return
        g = ledger.guild(guild.id)
        if g.get('announcement_message') or g.get('announcement_attempted'):
            return
        channel = guild.get_channel(ANNOUNCEMENT_CHANNEL)
        role = guild.get_role(ANNOUNCEMENT_ROLE)
        if not isinstance(channel, discord.TextChannel) or role is None:
            print('[holiday] announcement destination unavailable; use setup after checking channel/role')
            return
        try:
            config(guild.id, announcement_attempted=True)
            message = await channel.send(ANNOUNCEMENT, allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role]))
            config(guild.id, announcement_message=message.id)
            print(f'[holiday] requested announcement posted message={message.id}')
        except (discord.HTTPException, OSError) as exc:
            print(f'[holiday] announcement not confirmed: {type(exc).__name__}; inspect channel before manual retry')

    bm.bot._holiday_announcement_task = asyncio.create_task(send_requested_announcement())


