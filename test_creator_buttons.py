"""Regression tests for shop buttons, delivery cards and gold withdrawals."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import creator
import ui
from test_creator_flow import FakeChannel, FakeGuild, FakeMember


class FakeResponse:
    def __init__(self):
        self.sent = []

    async def send_message(self, text=None, **kwargs):
        self.sent.append((text, kwargs))
        return None

    async def send(self, text=None, **kwargs):
        self.sent.append((text, kwargs))
        return None

    async def defer(self, **kwargs):
        self.deferred = kwargs


class FakeInteraction:
    def __init__(self, guild, user, message=None):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.message = message
        self.response = FakeResponse()
        self.followup = FakeResponse()


class ButtonFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        creator.DATA_FILE = Path(self.tmp.name) / "creator.json"
        creator.DATA = {}
        self.bm = SimpleNamespace(DATA_DIR=Path(self.tmp.name), ui=ui,
                                  OWNER_IDS=set(), PREFIX=".")
        creator.bm = self.bm
        self.guild = FakeGuild()
        self.member = FakeMember(7, "VidMaker")
        self.staff = FakeMember(8, "Mod", staff=True)
        self.member.guild = self.guild
        self.staff.guild = self.guild
        self.guild.members = {7: self.member, 8: self.staff}
        g = creator.gdata(self.guild.id)
        g["settings"]["review_channel"] = self.guild.channels["review"].id
        creator.save()
        self._orig_channel = creator._channel

        async def fake_channel(guild, key):
            return guild.channels.get({"review_channel": "review"}.get(key, key))
        creator._channel = fake_channel

    def tearDown(self):
        creator._channel = self._orig_channel
        self.tmp.cleanup()

    async def test_shop_button_purchase_maps_its_delivery_card(self):
        """The Buy buttons must record the card -> order link or Fulfilled fails."""
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 1000, "test grant")
        creator.save()

        view = creator.ShopBuyView()
        await view._buy_item(FakeInteraction(self.guild, self.member), "cash")

        cards = self.guild.channels["review"].sent
        self.assertEqual(len(cards), 1)
        g = creator.gdata(self.guild.id)
        rid = g["by_redeem_message"][str(cards[0].id)]
        self.assertEqual(g["redemptions"][rid]["status"], "requested")
        self.assertEqual(g["redemptions"][rid]["message_id"], cards[0].id)
        self.assertTrue(g["redemptions"][rid]["owner_notified"])
        self.assertEqual(creator.credits(g, self.member.id), 750)

    async def test_delivery_card_lookup_survives_lost_mapping(self):
        """Old cards still resolve through message_id / the printed request id."""
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 500, "test grant")
        creator.save()
        res = await creator.redeem(self.guild, self.member, "cash")
        card = self.guild.channels["review"].sent[-1]
        g = creator.gdata(self.guild.id)
        g["by_redeem_message"].clear()

        found = creator.find_redemption_for_message(g, card)
        self.assertIsNotNone(found)
        self.assertEqual(found["id"], res["id"])

    async def test_fulfilled_button_handles_a_shop_button_purchase(self):
        """Regression: the old code answered 'Redemption not found' here."""
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 1000, "test grant")
        creator.save()
        await creator.ShopBuyView()._buy_item(
            FakeInteraction(self.guild, self.member), "cash")
        card = self.guild.channels["review"].sent[-1]

        interaction = FakeInteraction(self.guild, self.staff, message=card)
        await creator.RedeemView()._finish(interaction, True)

        g = creator.gdata(self.guild.id)
        rid = g["by_redeem_message"][str(card.id)]
        self.assertEqual(g["redemptions"][rid]["status"], "fulfilled")
        self.assertIn(rid, interaction.followup.sent[-1][0])
        self.assertIn("delivered", self.member.dms[-1])

    async def test_gold_withdrawal_needs_allowance_and_can_be_accepted(self):
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 10, "test")
        creator.save()
        queued = creator.queue_allowances(self.guild, g)
        self.assertEqual(len(queued), 1)

        res = await creator.request_gold_withdrawal(
            self.guild, g, self.member, 500)
        self.assertTrue(res["ok"], res)
        w = creator.find_withdrawal(g, res["wid"])
        self.assertEqual(w["status"], "pending")
        self.assertEqual(creator.pending_gold(g, self.member.id), 0)

        done = await creator.fulfill_gold_withdrawal(
            self.guild, g, w, self.staff)
        self.assertTrue(done["ok"])
        self.assertEqual(creator.gdata(self.guild.id)["allowances"]
                         [str(self.member.id)]["status"], "paid")
        self.assertIn("500 Gold", self.member.dms[-1])

        again = await creator.fulfill_gold_withdrawal(self.guild, g, w, self.staff)
        self.assertFalse(again["ok"])

    async def test_cancelling_a_withdrawal_returns_the_gold(self):
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 10, "test")
        creator.save()
        creator.queue_allowances(self.guild, g)

        res = await creator.request_gold_withdrawal(
            self.guild, g, self.member, 250)
        self.assertTrue(res["ok"], res)
        w = creator.find_withdrawal(g, res["wid"])
        cancelled = await creator.cancel_gold_withdrawal(
            self.guild, g, w, self.staff)
        self.assertTrue(cancelled["ok"])
        self.assertEqual(creator.pending_gold(g, self.member.id), 500)

    async def test_withdrawal_rejects_more_than_pending_gold(self):
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 10, "test")
        creator.save()
        creator.queue_allowances(self.guild, g)

        res = await creator.request_gold_withdrawal(
            self.guild, g, self.member, 501)
        self.assertFalse(res["ok"])
        self.assertIn("500 Gold", res["error"])
        self.assertEqual(g["withdrawals"], {})


if __name__ == "__main__":
    unittest.main()
