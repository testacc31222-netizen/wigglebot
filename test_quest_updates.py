import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

from holiday_store import HolidayStore, LedgerError
from quest_rules import timed_play_quest, explicitly_whitelisted
from test_holiday import NOV


class QuestRulesTests(unittest.TestCase):
    def test_only_requested_play_quest_is_timed(self):
        for name in ('Play for 30 minutes', 'Play for 30 minutes quest', 'Play 30 mins'):
            self.assertTrue(timed_play_quest({'name': name}))
        for name in ('Snow Patrol', 'Moderate for 30 minutes', 'Play for 20 minutes', 'Play for 300 minutes'):
            self.assertFalse(timed_play_quest({'name': name}))

    def test_whitelist_requires_explicit_user_or_role_even_for_admins(self):
        member = NS(id=10, bot=False, roles=[NS(id=20)], guild_permissions=NS(administrator=True))
        self.assertFalse(explicitly_whitelisted(member, {}))
        self.assertTrue(explicitly_whitelisted(member, {'whitelisted_user_ids': [10]}))
        self.assertTrue(explicitly_whitelisted(member, {'whitelisted_role_ids': [20]}))
        self.assertFalse(explicitly_whitelisted(member, {'whitelisted_role_ids': [21]}))
        member.bot = True
        self.assertFalse(explicitly_whitelisted(member, {'whitelisted_user_ids': [10]}))


class TimedQuestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'pass.json'
        self.store = HolidayStore(self.path)
        self.store.change(1, lambda g: g['challenges'].update(play={
            'name': 'Play for 30 minutes', 'points': 30, 'cadence': 'daily', 'enabled': True}))
        self.key = self.store.create_claim(1, 2, 'play', NOV)

    def test_timer_persists_and_only_awards_after_review_once(self):
        with self.assertRaises(LedgerError):
            self.store.submit(1, self.key, [], NOV + timedelta(minutes=30, seconds=-1))
        self.store = HolidayStore(self.path)
        self.store.submit(1, self.key, [], NOV + timedelta(minutes=30))
        self.assertEqual(self.store.guild(1)['claims'][self.key]['status'], 'pending')
        self.assertEqual(self.store.guild(1)['proofs'], {})
        self.assertNotIn('2', self.store.guild(1)['accounts'])
        with self.assertRaises(LedgerError):
            self.store.submit(1, self.key, [], NOV + timedelta(minutes=31))
        with self.assertRaises(LedgerError):
            self.store.review_claim(1, self.key, 2, True)
        self.store.review_claim(1, self.key, 3, True, now=NOV + timedelta(minutes=31))
        with self.assertRaises(LedgerError):
            self.store.review_claim(1, self.key, 3, True)
        self.assertEqual(self.store.guild(1)['accounts']['2'], {'earned': 30, 'balance': 30})
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 2, 'play', NOV + timedelta(minutes=32))

    def test_normal_quests_still_require_evidence(self):
        key = self.store.create_claim(1, 2, 'snow-patrol', NOV)
        with self.assertRaises(LedgerError):
            self.store.submit(1, key, [], NOV + timedelta(minutes=30))

    def test_timer_cannot_bypass_deadline(self):
        self.store.change(1, lambda g: g['claims'][self.key].update(expires=NOV.timestamp()+60))
        with self.assertRaises(LedgerError):
            self.store.submit(1, self.key, [], NOV + timedelta(minutes=30))


class QuestUITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import discord
        from discord.ext import commands
        import holiday
        self.temp = tempfile.TemporaryDirectory()
        self.bot = commands.Bot(command_prefix='.', intents=discord.Intents.none())
        self.bot.wait_until_ready = AsyncMock()
        self.config = {}
        self.bm = NS(bot=self.bot, OWNER_IDS={1}, DATA_DIR=Path(self.temp.name),
                     get_config=lambda gid: self.config)
        self.guild = NS(id=1, text_channels=[])
        self.store = HolidayStore(Path(self.temp.name) / 'holiday_pass.json')
        self.store.change(1, lambda g: g.update(participation='community', challenges={
            'play': {'name': 'Play for 30 minutes', 'points': 30, 'cadence': 'daily'}}))
        self.key = self.store.create_claim(1, 2, 'play', NOV)
        self.store.change(1, lambda g: g['claims'][self.key].update(thread=90))
        holiday.setup(self.bm)
        await self.bot._holiday_quest_refresh_task

    async def asyncTearDown(self):
        import asyncio
        tasks = [getattr(self.bot, name) for name in (
            '_holiday_board_task', '_holiday_surface_task', '_holiday_proof_surface_task',
            '_holiday_announcement_task', '_holiday_quest_refresh_task')]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.bot.close()
        self.temp.cleanup()

    def timed_view(self):
        return next(v for v in self.bot.persistent_views if any(
            c.custom_id == 'holiday:finish-session:v1' for c in v.children))

    async def test_timed_buttons_have_no_submit_proof_and_reject_other_users(self):
        view = self.timed_view()
        self.assertNotIn('holiday:submit:v1', [c.custom_id for c in view.children])
        i = NS(guild_id=1, channel_id=90, user=NS(id=3, bot=False, guild=self.guild, roles=[]),
               response=NS(defer=AsyncMock(), is_done=lambda: True),
               followup=NS(send=AsyncMock()))
        await view.finish_session.callback(i)
        self.assertIn('Only the eligible participant', i.followup.send.call_args.kwargs['embed'].description)
        saved = HolidayStore(self.store.path)
        self.assertEqual(saved.guild(1)['claims'][self.key]['status'], 'draft')

    async def test_fun_command_denies_unlisted_and_suppresses_all_mentions(self):
        cmd = self.bot.get_command('wigglesworth')
        ctx = NS(author=NS(id=2, bot=False, roles=[], display_name='@everyone'),
                 guild=self.guild, send=AsyncMock())
        await cmd.callback(ctx)
        self.assertIn('whitelisted', ctx.send.call_args.kwargs['embed'].description)
        self.config['whitelisted_user_ids'] = [2]
        await cmd.callback(ctx)
        kw = ctx.send.call_args.kwargs
        self.assertIn('hereby named', kw['embed'].description)
        self.assertEqual(kw['allowed_mentions'].to_dict(), {'parse': []})
        self.assertNotIn('@everyone', kw['embed'].description)


if __name__ == '__main__':
    unittest.main()
