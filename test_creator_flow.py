"""End-to-end creator flow with fake Discord objects (no gateway, no network)."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import creator
import ui


class Perms:
    administrator = False
    manage_guild = False
    manage_messages = True


class FakeMember:
    def __init__(self, uid, name="Creator", staff=False):
        self.id = uid
        self.name = name
        self.display_name = name
        self.nick = None
        self.global_name = name
        self.mention = f"<@{uid}>"
        self.bot = False
        self.roles = []
        self.guild_permissions = Perms() if staff else SimpleNamespace(
            administrator=False, manage_guild=False, manage_messages=False)
        self.dms = []

    async def send(self, text):
        self.dms.append(text)


class FakeMessage:
    _seq = 100

    def __init__(self, channel, author, content=""):
        FakeMessage._seq += 1
        self.id = FakeMessage._seq
        self.channel = channel
        self.guild = channel.guild
        self.author = author
        self.content = content
        self.reactions = []
        self.replies = []
        self.edits = 0
        self.view = None
        self.embed = None
        self.deleted = False
        channel.messages[self.id] = self

    async def add_reaction(self, emoji):
        self.reactions.append(emoji)

    async def remove_reaction(self, emoji, user):
        if emoji in self.reactions:
            self.reactions.remove(emoji)

    async def reply(self, text=None, mention_author=False, allowed_mentions=None):
        self.replies.append(text)
        return FakeMessage(self.channel, self.guild.me, text or "")

    async def delete(self):
        self.deleted = True

    async def edit(self, view=None, embed=None):
        self.edits += 1
        self.view = view
        if embed is not None:
            self.embed = embed


class FakeChannel:
    def __init__(self, guild, name):
        self.guild = guild
        self.name = name
        self.id = abs(hash(name)) % 10_000_000
        self.sent = []
        self.messages = {}

    async def send(self, content=None, embed=None, view=None, allowed_mentions=None):
        msg = FakeMessage(self, self.guild.me, content or "")
        msg.embed = embed
        msg.view = view
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        return self.messages[int(mid)]


class FakeGuild:
    def __init__(self):
        self.id = 42
        self.name = "Test Guild"
        self.me = FakeMember(999, "Wigglesworth")
        self.roles = []
        self.members = {}
        self.channels = {}
        for name in ("submissions", "review", "approved"):
            self.channels[name] = FakeChannel(self, name)

    def get_channel(self, cid):
        return next((c for c in self.channels.values() if c.id == int(cid)), None)

    def get_role(self, rid):
        return next((r for r in self.roles if r.id == rid), None)

    def get_member(self, uid):
        return self.members.get(int(uid))


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        creator.DATA_FILE = Path(self.tmp.name) / "creator.json"
        creator.DATA = {}
        self.bm = SimpleNamespace(DATA_DIR=Path(self.tmp.name), ui=ui, OWNER_IDS=set(), PREFIX=".")
        creator.bm = self.bm
        self.guild = FakeGuild()
        self.member = FakeMember(7, "VidMaker")
        self.staff = FakeMember(8, "Mod", staff=True)
        self.guild.members = {7: self.member, 8: self.staff}
        g = creator.gdata(self.guild.id)
        g["settings"].update({
            "submissions_channel": self.guild.channels["submissions"].id,
            "review_channel": self.guild.channels["review"].id,
            "approved_channel": self.guild.channels["approved"].id,
            "min_seconds": 60,
        })
        creator.save()

        mapping = {"submissions_channel": "submissions", "review_channel": "review",
                   "approved_channel": "approved"}

        async def fake_channel(guild, key):
            return guild.channels.get(mapping.get(key, key))
        self._orig_channel = creator._channel
        creator._channel = fake_channel

    def tearDown(self):
        creator._channel = self._orig_channel
        self.tmp.cleanup()

    def checks(self, verdict="approve"):
        return {
            "platform": "youtube", "url": "https://www.youtube.com/watch?v=abc123",
            "original_url": "https://youtu.be/abc123", "title": "Wigglesworth gameplay",
            "author": "VidMaker", "thumbnail": "", "duration_s": 400, "views": 1500,
            "public": True, "channel_match": True, "verified": True,
            "reasons": [] if verdict == "approve" else ["Staff look needed."],
            "warnings": [], "verdict": verdict,
        }

    async def test_auto_approved_video_awards_credits_and_posts(self):
        async def fake_inspect(url, creator_doc, settings, session=None):
            return self.checks("approve")
        orig = creator.inspect_video
        creator.inspect_video = fake_inspect
        try:
            msg = FakeMessage(self.guild.channels["submissions"], self.member, "https://youtu.be/abc123")
            res = await creator.process_submission(self.guild, self.member, msg.content, msg)
        finally:
            creator.inspect_video = orig
        self.assertTrue(res["auto"])
        g = creator.gdata(self.guild.id)
        sub = g["submissions"][res["id"]]
        self.assertEqual(sub["status"], "approved")
        self.assertEqual(sub["credits"], 85)  # 50 base + 10 long + 25 weekly mission
        self.assertEqual(creator.credits(g, self.member.id), 85)
        self.assertEqual(len(self.guild.channels["approved"].sent), 1)
        self.assertIn("✅", msg.reactions)
        # saved to disk
        saved = json.loads(creator.DATA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(saved["guilds"]["42"]["submissions"][res["id"]]["status"], "approved")

    async def test_unclear_video_goes_to_review_channel(self):
        async def fake_inspect(url, creator_doc, settings, session=None):
            return self.checks("review")
        orig = creator.inspect_video
        creator.inspect_video = fake_inspect
        try:
            msg = FakeMessage(self.guild.channels["submissions"], self.member, "https://youtu.be/abc123")
            res = await creator.process_submission(self.guild, self.member, msg.content, msg)
        finally:
            creator.inspect_video = orig
        self.assertFalse(res["auto"])
        g = creator.gdata(self.guild.id)
        sub = g["submissions"][res["id"]]
        self.assertEqual(sub["status"], "needs_review")
        self.assertEqual(creator.credits(g, self.member.id), 0)
        review = self.guild.channels["review"].sent
        self.assertEqual(len(review), 1)
        self.assertIsNotNone(review[0].view)
        field_text = "\n".join(f.value for f in review[0].embed.fields)
        self.assertIn("Staff look needed", field_text)
        self.assertEqual(g["by_review_message"][str(review[0].id)], res["id"])

        # Staff approves from the review card.
        ok = await creator.approve_submission(self.guild, sub, self.staff, source="button")
        self.assertTrue(ok["ok"])
        self.assertEqual(sub["status"], "approved")
        self.assertEqual(creator.credits(g, self.member.id), 85)
        self.assertEqual(len(self.guild.channels["approved"].sent), 1)

    async def test_redeem_deducts_credits_and_queues_fulfillment(self):
        g = creator.gdata(self.guild.id)
        creator.add_credits(g, self.member.id, 500, "test grant")
        creator.save()
        res = await creator.redeem(self.guild, self.member, "cash")
        self.assertTrue(res["ok"])
        g = creator.gdata(self.guild.id)
        self.assertEqual(creator.credits(g, self.member.id), 250)
        queued = self.guild.channels["review"].sent
        self.assertEqual(len(queued), 1)
        red_id = res["id"]
        self.assertEqual(g["redemptions"][red_id]["status"], "requested")
        self.assertEqual(g["by_redeem_message"][str(queued[0].id)], red_id)

    async def test_duplicate_submission_is_rejected(self):
        async def fake_inspect(url, creator_doc, settings, session=None):
            return self.checks("approve")
        orig = creator.inspect_video
        creator.inspect_video = fake_inspect
        try:
            msg = FakeMessage(self.guild.channels["submissions"], self.member, "https://youtu.be/abc123")
            await creator.process_submission(self.guild, self.member, msg.content, msg)
            dup = await creator.process_submission(self.guild, self.member,
                                                  "https://www.youtube.com/watch?v=abc123")
        finally:
            creator.inspect_video = orig
        self.assertFalse(dup["ok"])
        self.assertEqual(dup["error"], "duplicate")


if __name__ == "__main__":
    unittest.main()
