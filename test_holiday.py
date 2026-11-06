import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from holiday_store import HolidayStore, LedgerError

NOV = datetime(2026, 11, 3, 18, tzinfo=timezone.utc)
DEC = datetime(2026, 12, 3, 18, tzinfo=timezone.utc)


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'pass.json'
        self.store = HolidayStore(self.path)
        self.store.change(1, lambda g: g['challenges'].update(snow={'name': 'Snow Patrol', 'points': 400, 'cadence': 'daily', 'enabled': True}))

    def tearDown(self):
        self.temp.cleanup()

    def earn(self, uid=2, proof='proof', at=NOV):
        key = self.store.create_claim(1, uid, 'snow', at)
        self.store.submit(1, key, [proof], at)
        self.store.review_claim(1, key, 3, True)
        return key

    def test_once_per_reset_no_double_approval_and_persistence(self):
        key = self.earn()
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 2, 'snow', NOV)
        with self.assertRaises(LedgerError):
            self.store.review_claim(1, key, 3, True)
        loaded = HolidayStore(self.path)
        self.assertEqual(loaded.guild(1)['accounts']['2'], {'earned': 400, 'balance': 400})

    def test_shop_does_not_lower_levels(self):
        self.earn()
        key = self.store.request_order(1, 2, 'gold500', NOV)
        self.store.review_order(1, key, 3, 'approved', NOV)
        self.store.review_order(1, key, 3, 'delivered', NOV)
        account = self.store.guild(1)['accounts']['2']
        self.assertEqual(account, {'earned': 400, 'balance': 250})
        with self.assertRaises(LedgerError):
            self.store.review_order(1, key, 3, 'approved', NOV)

    def test_reused_proof_and_self_review_blocked(self):
        self.earn()
        key = self.store.create_claim(1, 4, 'snow', NOV)
        with self.assertRaises(LedgerError):
            self.store.submit(1, key, ['proof'], NOV)
        self.store.submit(1, key, ['different'], NOV)
        with self.assertRaises(LedgerError):
            self.store.review_claim(1, key, 4, True)
        self.assertNotIn('4', self.store.guild(1)['accounts'])

    def test_carryover_rotation_and_event_end(self):
        self.earn()
        with self.assertRaises(LedgerError):
            self.store.request_order(1, 2, 'christmas', NOV)
        self.earn(proof='december', at=DEC)
        self.assertEqual(self.store.guild(1)['accounts']['2']['earned'], 800)
        self.store.request_order(1, 2, 'christmas', DEC)
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 5, 'snow', datetime(2027, 1, 1, 7, tzinfo=timezone.utc))

    def test_failed_transaction_leaves_balance_unchanged(self):
        self.earn()
        key = self.store.request_order(1, 2, 'gold500', NOV)
        with self.assertRaises(LedgerError):
            self.store.review_order(1, key, 2, 'approved', NOV)
        self.assertEqual(self.store.guild(1)['accounts']['2']['balance'], 400)

    def test_weekly_reset_and_secret_expiry(self):
        self.store.change(1, lambda g: g['challenges'].update(week={'name': 'Weekly', 'points': 100, 'cadence': 'weekly'}, secret={'name': 'Secret', 'points': 500, 'cadence': 'secret', 'expires': NOV.timestamp()-1}))
        self.store.create_claim(1, 2, 'week', NOV)
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 2, 'week', datetime(2026, 11, 8, 18, tzinfo=timezone.utc))
        self.store.create_claim(1, 2, 'week', datetime(2026, 11, 9, 18, tzinfo=timezone.utc))
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 2, 'secret', NOV)


class GrantTests(unittest.TestCase):
    setUp = LedgerTests.setUp
    tearDown = LedgerTests.tearDown

    def test_community_access_keeps_reviews_restricted_and_filters_staff_tasks(self):
        from types import SimpleNamespace as NS
        from holiday_defaults import eligible, visible_challenge, COMMUNITY_CHALLENGES
        member = NS(bot=False, roles=[])
        g = self.store.guild(1)
        self.assertFalse(eligible(member, g))
        self.store.change(1, lambda data: data.update(participation='community'))
        g = self.store.guild(1)
        self.assertTrue(eligible(member, g))
        self.assertFalse(eligible(NS(bot=True, roles=[]), g))
        self.assertEqual(g['review_roles'], [])
        self.assertFalse(visible_challenge('snow-patrol', g['challenges']['snow-patrol'], g))
        with self.assertRaises(LedgerError):
            self.store.create_claim(1, 2, 'snow-patrol', NOV)
        self.assertEqual(len(COMMUNITY_CHALLENGES), 8)
    def test_grants_and_two_hour_reset_preserve_balance(self):
        from datetime import timedelta
        self.store.grant(1, 2, 250, 1, 'Event help', 'one', NOV)
        self.assertEqual(self.store.leaderboard(1, NOV), [('2', 250)])
        with self.assertRaises(LedgerError):
            self.store.grant(1, 2, 250, 1, 'Event help', 'one', NOV)
        later = NOV + timedelta(hours=2)
        self.assertEqual(self.store.leaderboard(1, later), [])
        self.assertEqual(self.store.guild(1)['accounts']['2'], {'earned': 250, 'balance': 250})
        self.store.grant(1, 4, 40, 1, 'New round', 'two', later)
        self.assertEqual(self.store.leaderboard(1, later), [('4', 40)])

    def test_purchase_requires_no_forum(self):
        self.store.grant(1, 2, 500, 1, 'Bonus', 'bonus', NOV)
        key = self.store.request_order(1, 2, 'gold500', NOV)
        self.assertIsNone(self.store.guild(1)['orders'][key]['thread'])
        self.assertEqual(self.store.guild(1)['accounts']['2']['balance'], 500)
        self.store.review_order(1, key, 3, 'approved', NOV)
        self.store.review_order(1, key, 3, 'delivered', NOV)
        self.assertEqual(self.store.guild(1)['accounts']['2']['balance'], 350)


class UIRegistrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_quick_setup_two_role_choices_and_prefilled_forum(self):
        import discord
        import holiday
        from discord.ext import commands
        from types import SimpleNamespace as NS
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as tmp:
            bot = commands.Bot(command_prefix='.', intents=discord.Intents.none())
            bot.wait_until_ready = AsyncMock()
            holiday.setup(NS(bot=bot, OWNER_IDS={1}, DATA_DIR=Path(tmp)))
            await bot._holiday_announcement_task
            ctx = NS(author=NS(id=1), guild=NS(id=1551097539804008580), send=AsyncMock())
            await bot.get_command('holidaysetup').callback(ctx)
            view = ctx.send.call_args.kwargs['view']
            self.assertEqual(view.forum_id, 1566963036236025956)
            self.assertEqual(view.step, 0)
            intr = NS(response=NS(edit_message=AsyncMock()))
            for rid in (40, 50):
                picker = next(c for c in view.children if isinstance(c, discord.ui.RoleSelect))
                picker._values = [NS(id=rid, is_default=lambda: False)]
                await picker.callback(intr)
            self.assertEqual(view.step, 2)
            self.assertEqual(view.staff_ids, [40])
            self.assertEqual(view.reviewer_ids, [50])
            self.assertTrue(any(getattr(c, 'label', '') == 'Start Holiday Pass' for c in view.children))
            self.assertFalse((Path(tmp) / 'holiday_pass.json').exists())
            await bot.close()

    async def test_persistent_views_register_once(self):
        import discord
        import holiday
        from discord.ext import commands
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            bot = commands.Bot(command_prefix='.', intents=discord.Intents.none())
            from unittest.mock import AsyncMock
            bot.wait_until_ready = AsyncMock()
            module = SimpleNamespace(bot=bot, OWNER_IDS={1}, DATA_DIR=Path(tmp))
            holiday.setup(module)
            holiday.setup(module)
            await bot._holiday_announcement_task
            self.assertIsNotNone(bot.get_command('holidaysetup'))
            self.assertEqual(len(bot.persistent_views), 4)
            self.assertTrue(all(v.is_persistent() for v in bot.persistent_views))
            await bot.close()


if __name__ == '__main__':
    unittest.main()



