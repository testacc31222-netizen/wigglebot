import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
import discord
from discord.ext import commands
import staff_tools
import ui


class StaffToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = commands.Bot(command_prefix='.', intents=discord.Intents.none())
        self.cfg = {}
        self.bm = NS(bot=self.bot, OWNER_IDS={1}, PREFIX='.', get_config=lambda gid: self.cfg.copy(), update_config=lambda gid, **kw: self.cfg.update(kw))
        staff_tools.setup(self.bm)
        self.ctx = NS(author=NS(id=1), guild=NS(id=10), send=AsyncMock())

    async def asyncTearDown(self):
        await self.bot.close()

    async def invoke(self, name, *args):
        await self.bot.get_command(name).callback(self.ctx, *args)

    async def test_setup_idempotent_and_whitelist_is_separate(self):
        staff_tools.setup(self.bm)
        self.ctx.author.id = 2
        self.cfg['whitelisted_user_ids'] = [2]
        await self.invoke('updatepost')
        self.assertNotIn('view', self.ctx.send.call_args.kwargs)
        self.ctx.author.id = 1
        await self.invoke('updateowner', 'add', NS(id=2))
        self.ctx.author.id = 2
        await self.invoke('updatepost')
        self.assertIn('view', self.ctx.send.call_args.kwargs)

    async def test_setup_panel_and_persistent_private_confirmation(self):
        await self.invoke('staffquitsetup')
        setup_view = self.ctx.send.call_args.kwargs['view']
        self.assertTrue(any(isinstance(c, discord.ui.RoleSelect) for c in setup_view.children))
        self.assertTrue(any(isinstance(c, discord.ui.ChannelSelect) for c in setup_view.children))
        panel = next(v for v in self.bot.persistent_views if any(c.custom_id == 'wigglebot:staffquit:open:v1' for c in v.children))
        self.assertTrue(panel.is_persistent())
        self.cfg['staff_quit_role_ids'] = [40]
        intr = NS(guild=NS(id=10), guild_id=10, user=NS(id=2, roles=[NS(id=40, mention='<@&40>')]), response=NS(send_message=AsyncMock()))
        await panel.children[0].callback(intr)
        self.assertTrue(intr.response.send_message.call_args.kwargs['ephemeral'])
        self.assertIn('view', intr.response.send_message.call_args.kwargs)
        self.ctx.author.id = 2
        await self.invoke('staffquitsetup')
        self.assertNotIn('view', self.ctx.send.call_args.kwargs)

    async def test_invalid_role_is_named_and_save_disabled(self):
        await self.invoke('staffquitsetup')
        view = self.ctx.send.call_args.kwargs['view']
        select = next(c for c in view.children if isinstance(c, discord.ui.RoleSelect))
        select._values = [NS(id=40, mention='<@&40>')]
        intr = NS(guild=NS(id=10), response=NS(edit_message=AsyncMock()))
        with patch('staff_tools.removable', return_value=False):
            await select.callback(intr)
        self.assertTrue(view.save.disabled)
        self.assertIn('<@&40>', view.role_error)
        self.assertIs(intr.response.edit_message.call_args.kwargs['view'], view)
        view.channel_id = 55
        with patch('staff_tools.removable', return_value=True):
            await select.callback(intr)
        self.assertFalse(view.save.disabled)
        self.assertEqual(view.role_ids, [40])

    async def test_quit_requires_own_confirmation_and_checks_config(self):
        self.cfg['staff_quit_role_ids'] = [40]
        self.ctx.author.roles = [NS(id=40, mention='<@&40>')]
        await self.invoke('staffquit')
        view = self.ctx.send.call_args.kwargs['view']
        intr = NS(user=NS(id=9), response=NS(send_message=AsyncMock()))
        self.assertFalse(await view.interaction_check(intr))
        self.cfg['staff_quit_role_ids'] = [41]
        intr = NS(user=NS(id=1), guild=NS(id=10), response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        confirm = next(b for b in view.children if b.label == 'Leave staff team')
        await confirm.callback(intr)
        self.assertIn('settings changed', intr.edit_original_response.call_args.kwargs['embed'].description)

    async def test_removed_publisher_cannot_publish_preview(self):
        self.cfg['update_owner_ids'] = [2]
        self.ctx.author.id = 2
        await self.invoke('updatepost')
        picker = self.ctx.send.call_args.kwargs['view']
        select = next(c for c in picker.children if isinstance(c, discord.ui.Select))
        select._values = ['discord']
        intr = NS(user=NS(id=2), guild_id=10, response=NS(send_modal=AsyncMock(), send_message=AsyncMock()))
        await select.callback(intr)
        modal = intr.response.send_modal.call_args.args[0]
        modal.body._value = 'A new update'
        await modal.on_submit(intr)
        preview = intr.response.send_message.call_args.kwargs['view']
        self.cfg['update_owner_ids'] = []
        publish = next(b for b in preview.children if b.label == 'Publish update')
        await publish.callback(intr)
        self.assertTrue(intr.response.send_message.call_args.kwargs['ephemeral'])
        self.assertIn('access changed', intr.response.send_message.call_args.kwargs['embed'].description)

    async def test_confirm_removes_only_configured_roles(self):
        class Role:
            def __init__(self, rid):
                self.id, self.mention, self.managed = rid, f'<@&{rid}>', False
            def is_default(self):
                return self.id == 0
            def __lt__(self, other):
                return self.id < other.id
            def __ge__(self, other):
                return self.id >= other.id
        everyone, staff, other = Role(0), Role(40), Role(20)
        self.cfg['staff_quit_role_ids'] = [40]
        self.ctx.author.roles = [everyone, other, staff]
        await self.invoke('staffquit')
        view = self.ctx.send.call_args.kwargs['view']
        member = NS(id=1, roles=[everyone, other, staff], top_role=staff, edit=AsyncMock())
        guild = NS(id=10, owner_id=99, fetch_member=AsyncMock(return_value=member), me=NS(top_role=Role(80), guild_permissions=NS(manage_roles=True)))
        intr = NS(guild=guild, response=NS(defer=AsyncMock()), edit_original_response=AsyncMock())
        await next(b for b in view.children if b.label == 'Leave staff team').callback(intr)
        self.assertEqual(member.edit.call_args.kwargs['roles'], [other])

    def test_ui_preserves_rank_information(self):
        card = ui.rank_embed(name='Test', avatar=None, level=13, xp=18933, base=16900, nxt=19600, rank=1, tracked=333, guild_name='Wigglesworth', messages=1603)
        self.assertEqual([f.name for f in card.fields], ['Server Rank', 'Total XP', 'Messages'])
        self.assertIn('18,933', card.fields[1].value)
        self.assertIn('1,603', card.fields[2].value)


if __name__ == '__main__':
    unittest.main()

