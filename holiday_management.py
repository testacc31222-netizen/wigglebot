"""Owner grants, management fulfilment queue and rotating Snowflake rankings."""
import asyncio
import re
from datetime import timedelta
import discord
from discord.ext import commands
import ui
from holiday_store import LedgerError, PHOENIX


def matching_channel(channels, name):
    matches = [c for c in channels if re.sub(r'[^a-z0-9]+', '-', c.name.lower()).strip('-') == name]
    return matches[0] if len(matches) == 1 else None


def setup(bm, ledger, owner, management, staff, now, reply, shop_factory=None, pass_factory=None):
    def board_embed(gid):
        scores = ledger.leaderboard(gid, now())[:15]
        local = now().astimezone(PHOENIX)
        reset = local.replace(hour=local.hour//2*2, minute=0, second=0, microsecond=0) + timedelta(hours=2)
        desc = '\n'.join(f'**{idx}.** <@{uid}> · **{amount:,}** Snowflakes' for idx, (uid, amount) in enumerate(scores, 1)) or 'No Snowflakes earned in this round yet.'
        return ui.embed('Snowflakes · Two-hour leaderboard', desc + f'\n\nResets <t:{int(reset.timestamp())}:R>.\nOnly earnings in this round count. Pass levels and balances never reset.', 'brand')

    class Board(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Refresh leaderboard', custom_id='holiday:board:refresh:v1')
        async def refresh(self, i, button):
            if not staff(i.user) and not management(i.user):
                return await reply(i, 'Holiday Pass participants only.', True)
            await i.response.edit_message(embed=board_embed(i.guild_id))

    class GrantModal(discord.ui.Modal, title='Grant Snowflakes'):
        amount = discord.ui.TextInput(label='Amount (1–100,000)', max_length=6)
        reason = discord.ui.TextInput(label='Reason', max_length=300)

        def __init__(self, uid):
            super().__init__()
            self.uid = uid

        async def on_submit(self, i):
            if not owner(i.user.id):
                return await reply(i, 'Bot owners only.', True)
            await i.response.defer(ephemeral=True)
            try:
                member = await i.guild.fetch_member(self.uid)
                if not staff(member):
                    raise LedgerError('Choose an eligible Holiday Pass participant.')
                amount = int(self.amount.value)
                ledger.grant(i.guild_id, self.uid, amount, i.user.id, self.reason.value, i.id, now())
                await reply(i, f'Granted **{amount:,} Snowflakes** to <@{self.uid}>. Their pass progress, balance, and current leaderboard earnings have been updated.')
            except (ValueError, OSError, discord.HTTPException) as e:
                await reply(i, str(e) if isinstance(e, LedgerError) else 'Could not confirm the grant. Enter a valid amount and check the member’s balance before retrying.', True)

    class Queue(discord.ui.View):
        def __init__(self, uid, gid):
            super().__init__(timeout=600)
            self.uid, self.gid = uid, gid
            self.page, self.filter, self.selected = 0, 'open', None
            self.render()

        async def interaction_check(self, i):
            if i.user.id != self.uid or not management(i.user):
                await reply(i, 'Open your own management panel with .holidaymanage.', True)
                return False
            return True

        def rows(self):
            return [(k, o) for k, o in ledger.guild(self.gid)['orders'].items() if (o['status'] in ('pending', 'approved')) == (self.filter == 'open')]

        def embed(self):
            rows = self.rows()
            pages = max(1, (len(rows)+24)//25)
            self.page = min(self.page, pages-1)
            em = ui.embed('Holiday Pass · Management', '**Purchase queue**\nPending = needs approval. Approved = paid, awaiting your delivery.\nSelect a purchase below to review it.', 'brand')
            em.add_field(name='Queue', value=f'{len(rows)} requests · Page {self.page+1}/{pages}', inline=False)
            if self.selected:
                o = ledger.guild(self.gid)['orders'].get(self.selected)
                if o:
                    em.add_field(name='Selected purchase', value=f'`{self.selected}`\nMember: <@{o["user"]}>\nItem: **{o["item"]}**\nCost: **{o["cost"]} Snowflakes**\nStatus: **{o["status"]}**', inline=False)
            return em

        def render(self):
            self.clear_items()
            rows = self.rows()
            self.page = min(self.page, max(0, (len(rows)-1)//25))
            rows = rows[self.page*25:(self.page+1)*25]
            if rows:
                select = discord.ui.Select(placeholder='Choose a purchase', options=[discord.SelectOption(label=f'{o["item"]} · {o["status"]}'[:100], value=k, description=f'Member {o["user"]} · {o["cost"]} Snowflakes · {k}') for k, o in rows])
                async def choose(i):
                    self.selected = select.values[0]
                    self.render()
                    await i.response.edit_message(embed=self.embed(), view=self)
                select.callback = choose
                self.add_item(select)
            for label, action in [('Approve purchase', 'approved'), ('Reject purchase', 'rejected'), ('Mark delivered', 'delivered')]:
                o = ledger.guild(self.gid)['orders'].get(self.selected, {})
                button = discord.ui.Button(label=label, style=discord.ButtonStyle.success if action != 'rejected' else discord.ButtonStyle.danger, disabled=o.get('status') != ('approved' if action == 'delivered' else 'pending'), row=1)
                async def act(i, action=action):
                    try:
                        ledger.review_order(self.gid, self.selected, i.user.id, action, now())
                        self.render()
                        await i.response.edit_message(embed=self.embed(), view=self)
                    except (LedgerError, OSError) as e:
                        await reply(i, str(e), True)
                button.callback = act
                self.add_item(button)
            for label, delta in [('Previous', -1), ('Next', 1)]:
                button = discord.ui.Button(label=label, row=2, disabled=self.page == 0 if delta < 0 else (self.page+1)*25 >= len(self.rows()))
                async def navigate(i, delta=delta):
                    self.page += delta
                    self.selected = None
                    self.render()
                    await i.response.edit_message(embed=self.embed(), view=self)
                button.callback = navigate
                self.add_item(button)
            toggle = discord.ui.Button(label='View history' if self.filter == 'open' else 'View open orders', row=2)
            async def history(i):
                self.filter = 'history' if self.filter == 'open' else 'open'
                self.page, self.selected = 0, None
                self.render()
                await i.response.edit_message(embed=self.embed(), view=self)
            toggle.callback = history
            self.add_item(toggle)
            refresh = discord.ui.Button(label='Refresh', row=2)
            async def reload(i):
                self.render()
                await i.response.edit_message(embed=self.embed(), view=self)
            refresh.callback = reload
            self.add_item(refresh)
            if owner(self.uid):
                member = discord.ui.UserSelect(placeholder='Grant Snowflakes · choose a participant', row=3)
                async def grant(i):
                    if not owner(i.user.id):
                        return await reply(i, 'Bot owners only.', True)
                    await i.response.send_modal(GrantModal(member.values[0].id))
                member.callback = grant
                self.add_item(member)

    @bm.bot.command(name='holidaymanage', aliases=['holidayorders'])
    @commands.guild_only()
    async def manage(ctx):
        if not management(ctx.author):
            return await ctx.send(embed=ui.err('Management reviewers or bot owners only.'))
        # Public entry button opens the actual purchase details privately.
        view = discord.ui.View(timeout=300)
        button = discord.ui.Button(label='Open management panel', style=discord.ButtonStyle.primary)
        async def open_panel(i):
            if not management(i.user):
                return await reply(i, 'Management only.', True)
            queue = Queue(i.user.id, i.guild_id)
            await i.response.send_message(embed=queue.embed(), view=queue, ephemeral=True)
        button.callback = open_panel
        view.add_item(button)
        await ctx.send(embed=ui.info('Review purchases, mark rewards delivered, and grant Snowflakes from one panel.', 'Holiday Pass · Management'), view=view)

    @bm.bot.command(name='snowflakeleaderboard', aliases=['snowboard'])
    @commands.guild_only()
    async def leaderboard(ctx):
        if not staff(ctx.author) and not management(ctx.author):
            return await ctx.send(embed=ui.err('Holiday Pass participants only.'))
        message = await ctx.send(embed=board_embed(ctx.guild.id), view=Board(), allowed_mentions=discord.AllowedMentions.none())
        ledger.change(ctx.guild.id, lambda g: g.update(board_message=message.id, board_channel=ctx.channel.id))

    bm.bot.add_view(Board())

    async def update_boards():
        await bm.bot.wait_until_ready()
        while not bm.bot.is_closed():
            for gid, g in list(ledger.data.items()):
                if not g.get('board_message'):
                    continue
                channel = bm.bot.get_channel(g.get('board_channel'))
                if channel is None:
                    continue
                try:
                    await channel.get_partial_message(g['board_message']).edit(embed=board_embed(int(gid)), view=Board(), allowed_mentions=discord.AllowedMentions.none())
                except discord.NotFound:
                    ledger.change(gid, lambda data: data.pop('board_message', None))
                except discord.HTTPException:
                    pass
            await asyncio.sleep(60)

    bm.bot._holiday_board_task = asyncio.create_task(update_boards())

    async def post_requested_panels():
        """Post the two panels requested by the server owner; preserve settings."""
        await bm.bot.wait_until_ready()
        guild = bm.bot.get_guild(1551097539804008580)
        if guild is None or shop_factory is None:
            return
        for name, flag in [('holiday-pass', 'requested_pass_board_posted'), ('holiday-shop', 'requested_shop_panel_posted')]:
            if ledger.guild(guild.id).get(flag):
                continue
            channel = matching_channel(guild.text_channels, name)
            if channel is None:
                print(f'[holiday-panels] need unique channel {name}; available: ' + ', '.join(c.name for c in guild.text_channels))
                continue
            try:
                g = ledger.guild(guild.id)
                if name == 'holiday-pass' and g.get('board_channel') == channel.id and g.get('board_message'):
                    message = await channel.fetch_message(g['board_message'])
                    await message.edit(embed=board_embed(guild.id), view=Board())
                elif name == 'holiday-pass':
                    message = await channel.send(embed=board_embed(guild.id), view=Board(), allowed_mentions=discord.AllowedMentions.none())
                else:
                    message = await channel.send(embed=ui.embed('Holiday Shop', 'Spend Snowflakes on gold, holiday rewards, and community perks.\n\nSelect **Browse Holiday Shop** to see rewards and prices. Requests go to management for approval and delivery.\n\nSpending never lowers your Holiday Pass level.', 'brand'), view=shop_factory(), allowed_mentions=discord.AllowedMentions.none())
                changes = {flag: message.id}
                if name == 'holiday-pass':
                    changes.update(board_message=message.id, board_channel=channel.id)
                ledger.change(guild.id, lambda data: data.update(changes))
                print(f'[holiday-panels] posted {name} channel={channel.id} message={message.id}')
            except (discord.HTTPException, OSError) as exc:
                print(f'[holiday-panels] could not confirm {name}: {type(exc).__name__}')

    bm.bot._holiday_surface_task = asyncio.create_task(post_requested_panels())

    async def post_requested_proof_panel():
        await bm.bot.wait_until_ready()
        guild = bm.bot.get_guild(1551097539804008580)
        if guild is None or pass_factory is None or ledger.guild(guild.id).get('requested_community_proof_panel'):
            return
        channel = matching_channel(guild.text_channels, 'holiday-pass')
        forum = guild.get_channel(ledger.guild(guild.id).get('forum') or 1566963036236025956)
        if channel is None or not isinstance(forum, discord.ForumChannel):
            print('[holiday-proof-panel] missing holiday-pass channel or submissions forum')
            return
        try:
            from holiday_defaults import COMMUNITY_CHALLENGES
            def enable(g):
                g.update(participation='community', forum=forum.id)
                for cid, (name, task, points, cadence) in COMMUNITY_CHALLENGES.items():
                    g['challenges'].setdefault(cid, {'name': name, 'instructions': task, 'points': points, 'cadence': cadence, 'enabled': True, 'audience': 'community'})
            ledger.change(guild.id, enable)
            message = await channel.send(embed=ui.embed('Community Holiday Pass', '**Submit your proof in three steps**\n\n1. Select **Challenges & proof** below.\n2. Choose your challenge to open a post in ' + forum.mention + '.\n3. Upload your screenshots or video in that post, then click **Submit proof for review**.\n\nManagement reviews your evidence before awarding Snowflakes. Check **My Holiday Pass** for your progress and request status.\n\n**100 Snowflakes = 1 level.** Everyone in the community can participate.', 'brand'), view=pass_factory(), allowed_mentions=discord.AllowedMentions.none())
            ledger.change(guild.id, lambda g: g.update(requested_community_proof_panel=message.id))
            perms = forum.permissions_for(guild.default_role)
            accessible = perms.view_channel and perms.send_messages_in_threads and perms.attach_files and perms.read_message_history
            print(f'[holiday-proof-panel] posted channel={channel.id} message={message.id} forum={forum.id} everyone_can_submit={accessible}')
        except (discord.HTTPException, OSError) as exc:
            print(f'[holiday-proof-panel] could not confirm posting: {type(exc).__name__}')

    bm.bot._holiday_proof_surface_task = asyncio.create_task(post_requested_proof_panel())


