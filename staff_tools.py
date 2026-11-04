"""Confirmed staff departures and a separate, owner-managed update whitelist."""
from __future__ import annotations

import asyncio
import discord
from discord.ext import commands
import ui


def removable(role, guild):
    return not role.is_default() and not role.managed and role < guild.me.top_role


def setup(bm):
    if bm.bot.get_command('staffquit'):
        return
    locks = {}

    def owner(uid):
        return uid in bm.OWNER_IDS

    def publisher(uid, gid):
        return owner(uid) or uid in bm.get_config(gid).get('update_owner_ids', [])

    class PersonalView(discord.ui.View):
        def __init__(self, uid):
            super().__init__(timeout=120)
            self.uid = uid
            self.busy = False

        async def interaction_check(self, interaction):
            if interaction.user.id != self.uid:
                await interaction.response.send_message(embed=ui.err('This confirmation belongs to someone else.'), ephemeral=True)
                return False
            return True

        @discord.ui.button(label='Cancel', style=discord.ButtonStyle.secondary)
        async def cancel(self, interaction, button):
            if self.busy:
                return await interaction.response.send_message('Already processing.', ephemeral=True)
            self.stop()
            await interaction.response.edit_message(embed=ui.info('Cancelled. No changes made.'), view=None)

    class QuitView(PersonalView):
        def __init__(self, uid, role_ids):
            super().__init__(uid)
            self.role_ids = set(role_ids)

        @discord.ui.button(label='Leave staff team', style=discord.ButtonStyle.danger)
        async def confirm(self, interaction, button):
            if self.busy:
                return await interaction.response.send_message('Already processing.', ephemeral=True)
            self.busy = True
            await interaction.response.defer(ephemeral=True)
            guild = interaction.guild
            async with locks.setdefault((guild.id, self.uid), asyncio.Lock()):
                try:
                    ids = set(bm.get_config(guild.id).get('staff_quit_role_ids', []))
                    if ids != self.role_ids:
                        return await interaction.edit_original_response(embed=ui.warn('The role settings changed. Run the command again to review them.'), view=None)
                    member = await guild.fetch_member(self.uid)
                    roles = [r for r in member.roles if r.id in ids]
                    if not roles:
                        return await interaction.edit_original_response(embed=ui.info('You no longer have any configured staff roles.'), view=None)
                    if member.id == guild.owner_id or not guild.me.guild_permissions.manage_roles or member.top_role >= guild.me.top_role or any(not removable(r, guild) for r in roles):
                        return await interaction.edit_original_response(embed=ui.err('Discord role hierarchy prevents this change. Ask an owner to check the bot’s role and Manage Roles permission.'), view=None)
                    # One member update avoids a sequence of partially removed roles.
                    await member.edit(roles=[r for r in member.roles if r.id not in ids and not r.is_default()], reason='Staff member confirmed voluntary resignation')
                    await interaction.edit_original_response(embed=ui.ok('You have left the staff team. Your configured staff roles were removed.', 'Resignation confirmed'), view=None)
                except discord.HTTPException:
                    await interaction.edit_original_response(embed=ui.err('Discord could not complete the role change. Ask an owner to check your roles before trying again.'), view=None)
                finally:
                    self.stop()

    class QuitPanel(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=None)

        @discord.ui.button(label='Leave staff team', style=discord.ButtonStyle.danger, custom_id='wigglebot:staffquit:open:v1')
        async def open_confirmation(self, interaction, button):
            if interaction.guild is None:
                return await interaction.response.send_message('Use this in the server.', ephemeral=True)
            ids = bm.get_config(interaction.guild_id).get('staff_quit_role_ids', [])
            roles = [r for r in interaction.user.roles if r.id in ids]
            if not roles:
                return await interaction.response.send_message(embed=ui.info('You have no configured staff roles to remove, or resignations are disabled.'), ephemeral=True)
            em = ui.warn('Are you sure you want to leave the staff team?\n\n**Roles that will be removed**\n' + '\n'.join(r.mention for r in roles) + '\n\nYour other roles stay. Choose Cancel to keep your roles.', 'Confirm your resignation')
            await interaction.response.send_message(embed=em, view=QuitView(interaction.user.id, ids), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    # Stable custom ID keeps posted panels usable after bot restarts.
    bm.bot.add_view(QuitPanel())

    class QuitSetup(PersonalView):
        def __init__(self, uid):
            super().__init__(uid)
            self.role_ids = []
            self.channel_id = None

        async def interaction_check(self, interaction):
            if not await super().interaction_check(interaction):
                return False
            if not owner(interaction.user.id):
                await interaction.response.send_message(embed=ui.err('Bot owners only.'), ephemeral=True)
                return False
            return True

        def summary(self):
            em = ui.embed('Staff departure · Setup', '1. Choose roles to remove.\n2. Choose the staff channel.\n3. Review your choices and post the panel.', 'brand')
            em.add_field(name='Roles to remove', value=', '.join(f'<@&{rid}>' for rid in self.role_ids) or 'Choose below', inline=False)
            em.add_field(name='Panel channel', value=f'<#{self.channel_id}>' if self.channel_id else 'Choose below', inline=False)
            return em

        @discord.ui.select(cls=discord.ui.RoleSelect, placeholder='Choose staff roles to remove', min_values=1, max_values=25, row=0)
        async def roles(self, interaction, select):
            if any(not removable(r, interaction.guild) for r in select.values):
                return await interaction.response.send_message(embed=ui.err('Choose normal roles below the bot’s highest role.'), ephemeral=True)
            self.role_ids = [r.id for r in select.values]
            await interaction.response.edit_message(embed=self.summary())

        @discord.ui.select(cls=discord.ui.ChannelSelect, placeholder='Choose where the staff panel appears', channel_types=[discord.ChannelType.text], row=1)
        async def channel(self, interaction, select):
            self.channel_id = select.values[0].id
            await interaction.response.edit_message(embed=self.summary())

        @discord.ui.button(label='Save and post panel', style=discord.ButtonStyle.primary, row=2)
        async def save(self, interaction, button):
            if self.busy:
                return await interaction.response.send_message('Already processing.', ephemeral=True)
            guild = interaction.guild
            channel = guild.get_channel(self.channel_id or 0)
            roles = [guild.get_role(rid) for rid in self.role_ids]
            if not roles or any(r is None or not removable(r, guild) for r in roles) or not isinstance(channel, discord.TextChannel):
                return await interaction.response.send_message(embed=ui.err('Choose valid staff roles and a text channel first.'), ephemeral=True)
            perms = channel.permissions_for(guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links and guild.me.guild_permissions.manage_roles):
                return await interaction.response.send_message(embed=ui.err('The bot needs Manage Roles, plus View Channel, Send Messages, and Embed Links in the selected channel.'), ephemeral=True)
            self.busy = True
            await interaction.response.defer()
            old = bm.get_config(guild.id).get('staff_quit_role_ids', [])
            try:
                bm.update_config(guild.id, staff_quit_role_ids=self.role_ids)
                panel = await channel.send(embed=ui.embed('Staff team · Departure', 'Ready to step down?\n\nSelect **Leave staff team** to privately review your staff roles and confirm your decision. Clicking this button alone does not remove any roles.', 'brand'), view=QuitPanel(), allowed_mentions=discord.AllowedMentions.none())
            except (discord.HTTPException, OSError):
                bm.update_config(guild.id, staff_quit_role_ids=old)
                self.busy = False
                return await interaction.edit_original_response(embed=ui.err('Could not save or post the panel. Settings were restored; check permissions and try again.'), view=self)
            self.stop()
            await interaction.edit_original_response(embed=ui.ok(f'Staff departure panel posted in {channel.mention}.\n[Open panel]({panel.jump_url})', 'Setup complete'), view=None)

    @bm.bot.command(name='staffquitsetup')
    @commands.guild_only()
    async def staffquitsetup(ctx):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        view = QuitSetup(ctx.author.id)
        await ctx.send(embed=view.summary(), view=view, allowed_mentions=discord.AllowedMentions.none())

    @bm.bot.command(name='staffquit', aliases=['resign'])
    @commands.guild_only()
    async def staffquit(ctx):
        ids = bm.get_config(ctx.guild.id).get('staff_quit_role_ids', [])
        roles = [r for r in ctx.author.roles if r.id in ids]
        if not roles:
            return await ctx.send(embed=ui.info('You have no configured staff roles to remove. An owner can configure this with `staffquitroles`.'), allowed_mentions=discord.AllowedMentions.none())
        em = ui.warn('Are you sure you want to leave the staff team?\n\n**Roles that will be removed**\n' + '\n'.join(r.mention for r in roles) + '\n\nYour other roles stay. This confirmation expires in two minutes.', 'Leave the staff team')
        await ctx.send(embed=em, view=QuitView(ctx.author.id, ids), allowed_mentions=discord.AllowedMentions.none())

    @bm.bot.command(name='staffquitroles')
    @commands.guild_only()
    async def staffquitroles(ctx, roles: commands.Greedy[discord.Role]):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        if not roles:
            ids = bm.get_config(ctx.guild.id).get('staff_quit_role_ids', [])
            return await ctx.send(embed=ui.info('Current roles: ' + (', '.join(f'<@&{rid}>' for rid in ids) or 'None') + f'\nUse `{bm.PREFIX}staffquitroles @Role @Role` to replace the list, or `{bm.PREFIX}staffquitoff` to disable.'), allowed_mentions=discord.AllowedMentions.none())
        if any(not removable(r, ctx.guild) for r in roles):
            return await ctx.send(embed=ui.err('Choose ordinary roles below the bot’s highest role. Integration roles and @everyone cannot be removed.'))
        bm.update_config(ctx.guild.id, staff_quit_role_ids=list(dict.fromkeys(r.id for r in roles)))
        await ctx.send(embed=ui.ok('Staff resignation roles saved. Members will review these roles before confirming.'))

    @bm.bot.command(name='staffquitoff')
    @commands.guild_only()
    async def staffquitoff(ctx):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        bm.update_config(ctx.guild.id, staff_quit_role_ids=[])
        await ctx.send(embed=ui.ok('Staff resignation disabled.'))

    @bm.bot.command(name='updateowner')
    @commands.guild_only()
    async def updateowner(ctx, action: str = 'list', member: discord.Member = None):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Only configured bot owners can manage the update whitelist.'))
        ids = list(bm.get_config(ctx.guild.id).get('update_owner_ids', []))
        if action == 'list':
            return await ctx.send(embed=ui.info('Update owners: ' + (', '.join(f'<@{uid}>' for uid in ids) or 'None') + '\nConfigured bot owners also have access.'), allowed_mentions=discord.AllowedMentions.none())
        if action not in ('add', 'remove') or member is None:
            return await ctx.send(embed=ui.info(f'Use `{bm.PREFIX}updateowner add|remove @member` or `{bm.PREFIX}updateowner list`.'))
        ids = [uid for uid in ids if uid != member.id]
        if action == 'add':
            ids.append(member.id)
        bm.update_config(ctx.guild.id, update_owner_ids=ids)
        await ctx.send(embed=ui.ok('Update owner whitelist saved.'))

    @bm.bot.command(name='updatesetup')
    @commands.guild_only()
    async def updatesetup(ctx, target: str, channel: discord.TextChannel, role: discord.Role):
        if not owner(ctx.author.id):
            return await ctx.send(embed=ui.err('Bot owners only.'))
        target = target.lower()
        if target not in ('discord', 'wigglesworth') or role.is_default() or role.managed:
            return await ctx.send(embed=ui.err('Choose discord or wigglesworth and a normal ping role.'))
        bm.update_config(ctx.guild.id, **{f'update_{target}_channel_id': channel.id, f'update_{target}_role_id': role.id})
        await ctx.send(embed=ui.ok(f'{target.title()} update channel and ping role saved.'))

    class PublishView(PersonalView):
        def __init__(self, uid, target, body, gid):
            super().__init__(uid)
            self.target, self.body, self.gid = target, body, gid
            cfg = bm.get_config(gid)
            self.destination = (cfg.get(f'update_{target}_channel_id'), cfg.get(f'update_{target}_role_id'))

        @discord.ui.button(label='Publish update', style=discord.ButtonStyle.primary)
        async def publish(self, interaction, button):
            if not publisher(self.uid, self.gid) or self.busy:
                return await interaction.response.send_message(embed=ui.err('Publishing is unavailable or your access changed.'), ephemeral=True)
            self.busy = True
            await interaction.response.defer(ephemeral=True)
            cfg = bm.get_config(self.gid)
            dest = (cfg.get(f'update_{self.target}_channel_id'), cfg.get(f'update_{self.target}_role_id'))
            channel = interaction.guild.get_channel(dest[0] or 0)
            role = interaction.guild.get_role(dest[1] or 0)
            if dest != self.destination or not isinstance(channel, discord.TextChannel) or role is None or role.is_default():
                self.stop()
                return await interaction.edit_original_response(embed=ui.err('Update settings are missing or changed. Ask an owner to run updatesetup, then start again.'), view=None)
            perms = channel.permissions_for(interaction.guild.me)
            if not (perms.view_channel and perms.send_messages) or not (role.mentionable or perms.mention_everyone):
                self.stop()
                return await interaction.edit_original_response(embed=ui.err('The bot needs channel access and permission to mention the configured role.'), view=None)
            try:
                # A single message keeps the selected ping after the update body.
                await channel.send(f'## {self.target.title()} update\n{self.body}\n\n{role.mention}', allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role], replied_user=False))
                await interaction.edit_original_response(embed=ui.ok(f'Published in {channel.mention}.', 'Update published'), view=None)
            except discord.HTTPException:
                await interaction.edit_original_response(embed=ui.err('Discord did not confirm publication. Check the channel before retrying.'), view=None)
            finally:
                self.stop()

    class UpdateModal(discord.ui.Modal, title='Write an update'):
        body = discord.ui.TextInput(label='Update message', style=discord.TextStyle.paragraph, max_length=1800)

        def __init__(self, uid, target):
            super().__init__()
            self.uid, self.target = uid, target

        async def on_submit(self, interaction):
            if interaction.user.id != self.uid or not publisher(self.uid, interaction.guild_id):
                return await interaction.response.send_message(embed=ui.err('Update owners only.'), ephemeral=True)
            body = self.body.value.strip()
            if not body:
                return await interaction.response.send_message(embed=ui.err('Write a message first.'), ephemeral=True)
            view = PublishView(self.uid, self.target, body, interaction.guild_id)
            cid, rid = view.destination
            em = ui.embed(f'{self.target.title()} update · Preview', body, 'brand')
            em.add_field(name='Destination', value=f'<#{cid}>\nPing after message: <@&{rid}>' if cid and rid else 'Not configured — run updatesetup.')
            await interaction.response.send_message(embed=em, view=view, ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    class UpdatePicker(PersonalView):
        @discord.ui.select(placeholder='Which one was updated?', options=[discord.SelectOption(label='Discord', value='discord'), discord.SelectOption(label='Wigglesworth', value='wigglesworth')])
        async def select(self, interaction, select):
            if not publisher(interaction.user.id, interaction.guild_id):
                return await interaction.response.send_message(embed=ui.err('Update owners only.'), ephemeral=True)
            await interaction.response.send_modal(UpdateModal(self.uid, select.values[0]))

    @bm.bot.command(name='updatepost')
    @commands.guild_only()
    async def updatepost(ctx):
        if not publisher(ctx.author.id, ctx.guild.id):
            return await ctx.send(embed=ui.err('Only bot owners and the separate update owner whitelist can publish updates.'))
        await ctx.send(embed=ui.embed('Publish an update', 'Which one was updated?\nChoose Discord or Wigglesworth, write your message, then review it before publishing.', 'brand'), view=UpdatePicker(ctx.author.id))

