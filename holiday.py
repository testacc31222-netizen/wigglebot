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
from holiday_defaults import COMMUNITY_CHALLENGES, eligible, visible_challenge
from quest_rules import timed_play_quest


def setup(bm):
    if bm.bot.get_command('holidaysetup'):
        return
    ledger = HolidayStore(bm.DATA_DIR / 'holiday_pass.json')
    now = lambda: datetime.now(timezone.utc)

    def owner(uid):
        return uid in bm.OWNER_IDS

    def staff(member):
        return eligible(member, ledger.guild(member.guild.id))

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

    def session_description(claim):
        ready = int(claim['created'] + 30 * 60)
        return (f'**Participant:** <@{claim["user"]}>\n'
                f'**Challenge:** {claim["title"]}\n'
                f'**Reward:** {claim["points"]} Snowflakes\n\n'
                f'Play for 30 minutes, then select **Finish 30-minute session**.\n'
                f'You can finish <t:{ready}:R> (<t:{ready}:t>).\n'
                'No screenshots or video required. This timer does not track gameplay; '
                'management still approves your reward.')

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
        tags = usable_tags(forum)
        item = ledger.guild(i.guild_id)[kind][key]
        result = await forum.create_thread(name=title[:100], embed=ui.embed(title, description, 'brand'), view=ReviewPanel(timed=kind == 'claims' and timed_play_quest(item)), applied_tags=tags, allowed_mentions=discord.AllowedMentions.none())
        ledger.change(i.guild_id, lambda g: g[kind][key].update(thread=result.thread.id))
        return result.thread

    def usable_tags(forum):
        if not forum.flags.require_tag:
            return []
        tags = [t for t in forum.available_tags if not t.moderated or forum.permissions_for(forum.guild.me).manage_threads]
        if not tags:
            raise LedgerError('The proof forum requires a tag, but no usable tags exist. Add a normal forum tag first.')
        return tags[:1]

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
        def __init__(self, timed=False):
            super().__init__(timeout=None)
            if timed:
                self.remove_item(self.submit)
            else:
                self.remove_item(self.finish_session)

        @discord.ui.button(label='Finish 30-minute session', style=discord.ButtonStyle.primary, custom_id='holiday:finish-session:v1')
        async def finish_session(self, i, button):
            await i.response.defer(ephemeral=True)
            try:
                kind, key, item = lookup(i)
                if kind != 'claims' or not timed_play_quest(item) or item['user'] != i.user.id or not staff(i.user):
                    raise LedgerError('Only the eligible participant can finish their own timed quest.')
                ledger.submit(i.guild_id, key, [], now())
                await reply(i, 'Session completed and sent to management for approval. No screenshots or video required.')
            except (LedgerError, discord.HTTPException, OSError) as e:
                await reply(i, str(e), True)

        @discord.ui.button(label='Submit proof for review', style=discord.ButtonStyle.primary, custom_id='holiday:submit:v1')
        async def submit(self, i, button):
            await i.response.defer(ephemeral=True)
            try:
                kind, key, item = lookup(i)
                if kind != 'claims' or item['user'] != i.user.id or not staff(i.user):
                    raise LedgerError('Only the participant who opened this challenge post can submit it.')
                if item['status'] != 'draft':
                    raise LedgerError('This submission has already been sent for review.')
                if timed_play_quest(item):
                    ledger.submit(i.guild_id, key, [], now())
                    return await reply(i, 'Session completed and sent to management. No proof upload is required.')
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
            options = [discord.SelectOption(label=c['name'][:100], value=cid, description=f'{c["points"]} Snowflakes · {c["cadence"]}') for cid, c in g['challenges'].items() if visible_challenge(cid, c, g) and c.get('enabled', True) and (not c.get('expires') or c['expires'] > now().timestamp())]
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
                if timed_play_quest(claim):
                    thread = await open_post(i, f'Session · {i.user.display_name} · {claim["title"]}', session_description(claim), 'claims', key)
                    await reply(i, f'Your 30-minute session has started: {thread.mention}\nPlay for 30 minutes, then click **Finish 30-minute session**. No uploads required. Management approves the reward.')
                else:
                    thread = await open_post(i, f'Proof · {i.user.display_name} · {claim["title"]}', f'**Participant:** <@{i.user.id}>\n**Challenge:** {claim["title"]}\n**Reward:** {claim["points"]} Snowflakes\n**Task:** {claim.get("instructions", "Follow the challenge announcement.")}\n\nUpload screenshot/video evidence as a reply in this post, then click **Submit proof for review**. Only management can approve it.', 'claims', key)
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
                return await reply(i, 'This shop is for eligible Holiday Pass participants.', True)
            await i.response.defer(ephemeral=True)
            key = None
            try:
                key = ledger.request_order(i.guild_id, i.user.id, self.picker.values[0], now())
                order = ledger.guild(i.guild_id)['orders'][key]
                await reply(i, f'Request **{key}** saved: **{order["item"]}** for **{order["cost"]} Snowflakes**.\nManagement will review it in their purchase panel. No Snowflakes have been deducted yet. Check My Holiday Pass for its status.')
            except (LedgerError, discord.HTTPException, OSError) as e:
                await reply(i, str(e), True)

    class HolidayPanel(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        async def interaction_check(self, i):
            if not i.guild or not staff(i.user):
                await reply(i, 'You are not eligible for this Holiday Pass. Ask an owner to check participation settings.', True)
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
            em.add_field(name='Milestone rewards · management delivery', value='\n'.join(f'{"Unlocked" if level >= lvl else "Level " + str(lvl)} · {reward.replace("Holiday Staff Reward", "Holiday Community Reward") if g.get("participation") == "community" else reward}' for lvl, reward in MILESTONES), inline=False)
            em.add_field(name='Season', value=f'November 2 – December 31, {g["year"]} · America/Phoenix\nSpending does not lower your level. November progress carries into December.', inline=False)
            recent = [f'[ {c.get("title", c.get("item", "Submission"))} · {c["status"]} ](https://discord.com/channels/{i.guild_id}/{c["thread"]})' for kind in ('claims', 'orders') for c in g[kind].values() if c['user'] == i.user.id and c.get('thread')]
            if recent:
                em.add_field(name='Recent submissions & purchases', value='\n'.join(recent[-5:])[:1024], inline=False)
            orders = [(key, o) for key, o in g['orders'].items() if o['user'] == i.user.id]
            if orders:
                em.add_field(name='My purchases', value='\n'.join(f'`{key}` · {o["item"]} · **{o["status"]}**' for key, o in orders[-5:])[:1024], inline=False)
            await i.response.send_message(embed=em, ephemeral=True)

        @discord.ui.button(label='Start a challenge', custom_id='holiday:challenges:v1')
        async def challenges(self, i, button):
            if not ledger.active(ledger.guild(i.guild_id), now()):
                return await reply(i, 'The event is closed.', True)
            view = ChallengePicker(i.user.id, i.guild_id)
            if not view.picker.options:
                return await reply(i, 'Management has not added any active challenges yet.')
            await i.response.send_message(embed=ui.info('Choose a challenge to open your forum post. The Play for 30 minutes quest uses a timer and needs no uploads. Other challenges require original evidence. Management approves rewards. Each challenge can be claimed once per reset.', 'Holiday challenges'), view=view, ephemeral=True)

        @discord.ui.button(label='Holiday shop', custom_id='holiday:shop:v1')
        async def shop(self, i, button):
            if not ledger.active(ledger.guild(i.guild_id), now()):
                return await reply(i, 'The Holiday Shop is closed.', True)
            await i.response.send_message(embed=ui.info('Choose an item to send a purchase request to management’s queue. No forum post is created. Management approves it and delivers the reward. Purchases are final once approved. Spending never reduces your pass level.', 'Holiday shop'), view=ShopPicker(i.user.id), ephemeral=True)

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
            try:
                usable_tags(forum)
            except LedgerError as e:
                return await reply(i, str(e), True)
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

    class QuickSetup(Personal):
        """Two role choices, one explicit save; advanced tools stay optional."""
        def __init__(self, uid, guild):
            super().__init__(uid)
            g = ledger.guild(guild.id)
            self.staff_ids = list(g['staff_roles'])
            self.participation = g.get('participation', 'roles')
            self.reviewer_ids = list(g['review_roles'])
            self.forum_id = g['forum'] or (1566963036236025956 if guild.id == ANNOUNCEMENT_GUILD else None)
            self.step = 0
            self.busy = False
            self.render()

        async def interaction_check(self, i):
            return await super().interaction_check(i) and owner(i.user.id)

        def summary(self):
            if self.step == 0:
                desc = '**Step 1 of 2 · Who can participate?**\nClick **Everyone in the community** or choose specific roles below.'
            elif self.step == 1:
                desc = '**Step 2 of 2 · Who approves proof?**\nChoose your management role below.'
            else:
                desc = '**Ready to start.**\nYour eight challenges, shop prices, and level rewards are already included.'
            em = ui.embed('Holiday Pass · Quick setup', desc, 'brand')
            if self.participation == 'community':
                em.add_field(name='Participants', value='Everyone in the community · new community challenges', inline=False)
            elif self.staff_ids:
                em.add_field(name='Participants', value=' '.join(f'<@&{rid}>' for rid in self.staff_ids)[:1024], inline=False)
            if self.reviewer_ids:
                em.add_field(name='Management', value=' '.join(f'<@&{rid}>' for rid in self.reviewer_ids)[:1024], inline=False)
            em.add_field(name='Proof forum', value=f'<#{self.forum_id}>' if self.forum_id else 'Choose a forum below', inline=False)
            if self.step == 2:
                em.add_field(name='Holiday panel', value='Will be posted in this channel. No new announcement ping.', inline=False)
            return em

        def render(self):
            self.clear_items()
            if self.step < 2:
                picker = discord.ui.RoleSelect(placeholder='Choose staff roles' if self.step == 0 else 'Choose management roles', min_values=1, max_values=25)
                async def choose(i):
                    if any(r.is_default() for r in picker.values):
                        return await reply(i, 'Choose a staff or management role, not @everyone.', True)
                    if self.step == 0:
                        self.participation = 'roles'
                        self.staff_ids = [r.id for r in picker.values]
                    else:
                        self.reviewer_ids = [r.id for r in picker.values]
                    self.step += 1
                    self.render()
                    await i.response.edit_message(embed=self.summary(), view=self)
                picker.callback = choose
                self.add_item(picker)
                if self.step == 0:
                    community = discord.ui.Button(label='Everyone in the community', style=discord.ButtonStyle.primary)
                    async def everyone(i):
                        self.participation = 'community'
                        self.step = 1
                        self.render()
                        await i.response.edit_message(embed=self.summary(), view=self)
                    community.callback = everyone
                    self.add_item(community)
            else:
                finish = discord.ui.Button(label='Start Holiday Pass', style=discord.ButtonStyle.success)
                finish.callback = self.finish
                self.add_item(finish)
                forum_picker = discord.ui.ChannelSelect(placeholder='Change proof forum (optional)', channel_types=[discord.ChannelType.forum])
                async def forum_choose(i):
                    self.forum_id = forum_picker.values[0].id
                    await i.response.edit_message(embed=self.summary(), view=self)
                forum_picker.callback = forum_choose
                self.add_item(forum_picker)
            if self.step > 0:
                back = discord.ui.Button(label='Back')
                async def previous(i):
                    self.step -= 1
                    self.render()
                    await i.response.edit_message(embed=self.summary(), view=self)
                back.callback = previous
                self.add_item(back)
            advanced = discord.ui.Button(label='Advanced settings')
            async def advanced_open(i):
                self.stop()
                await i.response.edit_message(embed=ui.info('Optional tools for changing challenges and forum settings. Existing progress is kept.', 'Holiday Pass · Advanced settings'), view=SetupPanel(self.uid))
            advanced.callback = advanced_open
            self.add_item(advanced)

        async def finish(self, i):
            if self.busy:
                return await reply(i, 'Setup is already being saved.')
            forum = i.guild.get_channel(self.forum_id or 0)
            if not isinstance(forum, discord.ForumChannel):
                return await reply(i, 'Choose a proof forum using the optional forum picker.', True)
            if (self.participation != 'community' and not self.staff_ids) or not self.reviewer_ids or any(i.guild.get_role(rid) is None for rid in self.reviewer_ids + (self.staff_ids if self.participation != 'community' else [])):
                return await reply(i, 'A selected role is missing. Go Back and select it again.', True)
            perms = forum.permissions_for(i.guild.me)
            panel_perms = i.channel.permissions_for(i.guild.me)
            if self.participation == 'community':
                public_forum = forum.permissions_for(i.guild.default_role)
                public_panel = i.channel.permissions_for(i.guild.default_role)
                if not (public_panel.view_channel and public_forum.view_channel and public_forum.send_messages_in_threads and public_forum.attach_files and public_forum.read_message_history):
                    return await reply(i, 'Choose a community-visible panel channel and proof forum. Members need View Channel, Read Message History, Send Messages in Threads, and Attach Files in the forum. The bot will not change channel permissions automatically.', True)
            if not (perms.view_channel and perms.send_messages and perms.send_messages_in_threads and perms.embed_links and perms.read_message_history and panel_perms.send_messages and panel_perms.embed_links):
                return await reply(i, 'Give the bot View Channel, Send Messages, Send Messages in Threads, Embed Links, and Read Message History in the proof forum, plus Send Messages and Embed Links here.', True)
            try:
                usable_tags(forum)
            except LedgerError as e:
                return await reply(i, str(e), True)
            self.busy = True
            await i.response.defer()
            old = ledger.guild(i.guild_id)
            try:
                def save(g):
                    g.update(staff_roles=self.staff_ids, review_roles=self.reviewer_ids, forum=self.forum_id, participation=self.participation)
                    if self.participation == 'community':
                        for cid, (name, task, points, cadence) in COMMUNITY_CHALLENGES.items():
                            g['challenges'].setdefault(cid, {'name': name, 'instructions': task, 'points': points, 'cadence': cadence, 'enabled': True, 'audience': 'community'})
                ledger.change(i.guild_id, save)
                title = 'Community Holiday Pass' if self.participation == 'community' else 'Holiday Pass'
                message = await i.channel.send(embed=ui.embed(title, 'Complete challenges. Upload proof. Earn rewards.\n\n**100 Snowflakes = 1 level.** Spending in the shop never lowers your level.\n\nUse the buttons below to get started. Participation is optional.', 'brand'), view=HolidayPanel())
            except (discord.HTTPException, OSError):
                config(i.guild_id, staff_roles=old['staff_roles'], review_roles=old['review_roles'], forum=old['forum'], participation=old.get('participation', 'roles'), challenges=old['challenges'])
                self.busy = False
                return await i.edit_original_response(embed=ui.err('Could not post the panel. Check channel permissions and try again.'), view=self)
            self.stop()
            await i.edit_original_response(embed=ui.ok(f'All set! [Open the staff panel]({message.jump_url}).\nParticipants choose a challenge and upload proof; management approves it in the forum.', 'Holiday Pass is ready'), view=None)

    @bm.bot.command(name='holidaysetup')
    @commands.guild_only()
    async def holidaysetup(ctx):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        view = QuickSetup(ctx.author.id, ctx.guild)
        await ctx.send(embed=view.summary(), view=view, allowed_mentions=discord.AllowedMentions.none())

    bm.bot.add_view(HolidayPanel())
    bm.bot.add_view(ReviewPanel())
    bm.bot.add_view(ReviewPanel(timed=True))
    import wigglesworth
    wigglesworth.setup(bm)
    import holiday_management
    def shop_surface():
        view = HolidayPanel()
        for child in list(view.children):
            if child.custom_id != 'holiday:shop:v1':
                view.remove_item(child)
        view.shop.label = 'Browse Holiday Shop'
        return view
    holiday_management.setup(bm, ledger, owner, management, staff, now, reply, shop_surface, HolidayPanel)

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

    async def refresh_quest_panels():
        # Wait for legacy panel setup so we edit its existing message, never repost it.
        await bm.bot.wait_until_ready()
        await bm.bot._holiday_proof_surface_task
        for gid in list(ledger.data):
            guild = bm.bot.get_guild(int(gid))
            if guild is None:
                continue
            g = ledger.guild(gid)
            panel_count = session_count = 0
            from holiday_management import matching_channel
            channel = matching_channel(guild.text_channels, 'holiday-pass')
            if channel is not None:
                try:
                    messages = {}
                    panel_id = g.get('requested_community_proof_panel')
                    if panel_id:
                        try:
                            messages[panel_id] = await channel.fetch_message(panel_id)
                        except discord.NotFound:
                            pass
                    async for message in channel.history(limit=100):
                        if message.author.id == bm.bot.user.id and any(
                            getattr(child, 'custom_id', '') == 'holiday:progress:v1'
                            for row in message.components for child in row.children
                        ):
                            messages[message.id] = message
                    for message in messages.values():
                        if message.author.id != bm.bot.user.id:
                            continue
                        title = 'Community Holiday Pass' if g.get('participation') == 'community' else 'Holiday Pass'
                        description = (
                            'Choose **Start a challenge** to begin.\n\n'
                            '**Play for 30 minutes:** start your session, play, then use '
                            '**Finish 30-minute session** in your post. No uploads required. '
                            'Management approves the reward.\n'
                            '**Other challenges:** upload the requested evidence and submit it for review.\n\n'
                            '**100 Snowflakes = 1 level.** Check **My Holiday Pass** for progress.\n\n'
                            '**New: .wigglesworth** gives you a fun honorary title. '
                            'Whitelisted members and roles only; no pings.')
                        await message.edit(embed=ui.embed(title, description, 'brand'),
                                           view=HolidayPanel(), allowed_mentions=discord.AllowedMentions.none())
                        panel_count += 1
                        print(f'[quest-update] panel refreshed guild={gid} channel={channel.id} message={message.id}', flush=True)
                except discord.HTTPException as exc:
                    print(f'[quest-update] panel refresh failed guild={gid}: {type(exc).__name__}', flush=True)
            for key, claim in list(g.get('claims', {}).items()):
                if not timed_play_quest(claim) or not claim.get('thread') or claim['status'] not in ('draft', 'pending'):
                    continue
                try:
                    thread = guild.get_thread(claim['thread']) or await bm.bot.fetch_channel(claim['thread'])
                    message = await thread.fetch_message(claim['thread'])
                    view = ReviewPanel(timed=True)
                    view.finish_session.disabled = claim['status'] != 'draft'
                    await message.edit(embed=ui.embed(claim['title'], session_description(claim), 'brand'),
                                       view=view, allowed_mentions=discord.AllowedMentions.none())
                    session_count += 1
                except discord.NotFound:
                    dead = claim.get('thread')

                    def _clear_thread(store_g, key=key):
                        c = store_g.get('claims', {}).get(key)
                        if c:
                            c['thread'] = None
                            c['thread_missing'] = True
                        return True

                    try:
                        ledger.change(gid, _clear_thread)
                    except Exception as cleanup_exc:
                        print(f'[quest-update] could not clean session {key}: '
                              f'{type(cleanup_exc).__name__}', flush=True)
                    print(f'[quest-update] deleted thread cleaned up thread={dead}', flush=True)
                except discord.HTTPException as exc:
                    print(f'[quest-update] session refresh failed thread={claim["thread"]}: {type(exc).__name__}', flush=True)
            matches = [c['name'] for c in g.get('challenges', {}).values() if timed_play_quest(c)]
            print(f'[quest-update] complete guild={gid} panels={panel_count} sessions={session_count} timed_quests={matches}', flush=True)

    bm.bot._holiday_quest_refresh_task = asyncio.create_task(refresh_quest_panels())





