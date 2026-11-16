import os
import unittest
from unittest import mock

import creator


def guild_data():
    return {
        "settings": dict(creator.DEFAULTS),
        "creators": {},
        "submissions": {},
        "credits": {},
        "ledger": [],
        "redemptions": {},
        "missions": {},
    }


class LinkTests(unittest.TestCase):
    def test_extracts_first_url_and_trims_punctuation(self):
        self.assertEqual(creator.extract_url("watch this https://youtu.be/abc123, lol"),
                         "https://youtu.be/abc123")
        self.assertIsNone(creator.extract_url("no link here"))

    def test_platform_detection(self):
        self.assertEqual(creator.platform_for("https://www.youtube.com/watch?v=x"), "youtube")
        self.assertEqual(creator.platform_for("https://vm.tiktok.com/x/"), "tiktok")
        self.assertEqual(creator.platform_for("https://clips.twitch.tv/abc"), "twitch")
        self.assertIsNone(creator.platform_for("https://example.com/video"))

    def test_youtube_id_and_normalization(self):
        self.assertEqual(creator.youtube_id("https://youtu.be/abc123?t=4"), "abc123")
        self.assertEqual(creator.youtube_id("https://www.youtube.com/shorts/xyz789"), "xyz789")
        self.assertEqual(creator.normalize_url("https://youtu.be/abc123?t=4"),
                         "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(creator.normalize_url("https://www.tiktok.com/@a/video/1?is_from_webapp=1"),
                         "https://tiktok.com/@a/video/1")

    def test_iso_duration(self):
        self.assertEqual(creator.parse_iso_duration("PT1M7S"), 67)
        self.assertEqual(creator.parse_iso_duration("PT1H2M3S"), 3723)
        self.assertIsNone(creator.parse_iso_duration("bad"))


class CreditTests(unittest.TestCase):
    def test_tiers(self):
        self.assertEqual(creator.tier_for(0)["id"], "creator")
        self.assertEqual(creator.tier_for(9)["id"], "creator")
        self.assertEqual(creator.tier_for(10)["id"], "plus")
        self.assertEqual(creator.tier_for(25)["id"], "partner")
        self.assertEqual(creator.next_tier(0)["videos"], 10)

    def test_credits_base_long_video_and_tier_multiplier(self):
        g = guild_data()
        first = creator.credits_for(g, 1, 400)
        self.assertEqual(first["base"], 60)  # 50 base + 10 long video
        self.assertEqual(first["mission"], 25)  # first weekly mission included
        self.assertEqual(first["total"], 85)
        # 10 approved videos -> Creator Plus 1.25x
        for i in range(10):
            sid = f"V{i}"
            g["submissions"][sid] = {"id": sid, "user_id": "1", "status": "approved",
                                     "decided_at": creator.now_ts()}
        plus = creator.credits_for(g, 1, 60)
        self.assertEqual(plus["base"], 63)  # round(50 * 1.25)
        self.assertEqual(plus["tier"]["id"], "plus")

    def test_missions_award_once_per_week(self):
        g = guild_data()
        total = 0
        for _ in range(3):
            bonus, labels = creator.mission_bonus(g, 7, creator.now_ts())
            total += bonus
        self.assertEqual(total, 125)  # 25 for 1 video + 100 for 3 videos
        again, labels = creator.mission_bonus(g, 7, creator.now_ts())
        self.assertEqual(again, 0)
        self.assertEqual(labels, [])

    def test_credit_balance_and_ledger(self):
        g = guild_data()
        self.assertEqual(creator.add_credits(g, 5, 100, "test"), 100)
        self.assertEqual(creator.add_credits(g, 5, -30, "spend"), 70)
        self.assertEqual(creator.credits(g, 5), 70)
        self.assertEqual(g["ledger"][0]["amount"], -30)

    def test_duplicate_detection_ignores_rejected(self):
        g = guild_data()
        url = "https://youtu.be/abc123"
        g["submissions"]["A"] = {"id": "A", "url": creator.normalize_url(url),
                                 "status": "approved"}
        self.assertEqual(creator.duplicate_of(g, "https://www.youtube.com/watch?v=abc123"), "A")
        g["submissions"]["A"]["status"] = "rejected"
        self.assertIsNone(creator.duplicate_of(g, url))

    def test_shop_items_are_unique_and_positive(self):
        ids = [i["id"] for i in creator.REWARD_CATALOG]
        self.assertEqual(ids, ["cash", "wheel", "doublexp"])
        self.assertEqual(len(ids), len(set(ids)))
        self.assertTrue(all(i["price"] > 0 for i in creator.REWARD_CATALOG))
        self.assertEqual(creator.find_item("cash")["price"], 250)
        self.assertEqual(creator.find_item("wheel")["price"], 400)
        self.assertEqual(creator.find_item("doublexp")["price"], 1500)
        self.assertIsNone(creator.find_item("nope"))

    def test_wheel_weights_are_published_and_bounded(self):
        self.assertEqual(sum(p["chance"] for p in creator.WHEEL_PRIZES), 100)

        class Rng:
            def __init__(self, value):
                self.value = value

            def uniform(self, _a, _b):
                return self.value

        self.assertEqual(creator.roll_wheel(Rng(0))["tier"], "Common")
        self.assertEqual(creator.roll_wheel(Rng(59.9))["tier"], "Common")
        self.assertEqual(creator.roll_wheel(Rng(60))["tier"], "Rare")
        self.assertEqual(creator.roll_wheel(Rng(89.9))["tier"], "Rare")
        self.assertEqual(creator.roll_wheel(Rng(90))["tier"], "Limited")
        self.assertEqual(creator.roll_wheel(Rng(99.9))["tier"], "Limited")
        self.assertEqual(creator.roll_wheel(Rng(100))["tier"], "Limited")


class EligibilityTests(unittest.IsolatedAsyncioTestCase):
    async def inspect(self, url, payloads, creator_doc=None, settings=None, api_key=""):
        async def fake_get(session, target):
            for part, value in payloads.items():
                if part in target:
                    return value
            return None
        settings = settings or dict(creator.DEFAULTS)
        with mock.patch.object(creator, "_get_json", fake_get), \
                mock.patch.dict(os.environ, {"YOUTUBE_API_KEY": api_key}):
            return await creator.inspect_video(url, creator_doc or {"channel_name": "Mock Creator"},
                                               settings, session=object())

    async def test_youtube_match_auto_approves(self):
        payloads = {
            "youtube.com/oembed": {"title": "Wigglesworth gameplay", "author_name": "Mock Creator",
                                   "author_url": "https://www.youtube.com/@MockCreator",
                                   "thumbnail_url": "https://img/x.jpg"},
            "googleapis.com": {"items": [{
                "snippet": {"title": "Wigglesworth gameplay", "channelTitle": "Mock Creator"},
                "contentDetails": {"duration": "PT8M12S"},
                "statistics": {"viewCount": "1234"},
                "status": {"privacyStatus": "public"},
            }]},
        }
        res = await self.inspect("https://youtu.be/abc123", payloads,
                                 creator_doc={"channel": "https://youtube.com/@mockcreator"},
                                 api_key="key")
        self.assertTrue(res["verified"])
        self.assertEqual(res["duration_s"], 492)
        self.assertEqual(res["views"], 1234)
        self.assertTrue(res["channel_match"])
        self.assertEqual(res["verdict"], "approve")
        self.assertEqual(res["reasons"], [])

    async def test_private_or_short_video_goes_to_review(self):
        payloads = {
            "youtube.com/oembed": {"title": "Clip", "author_name": "Mock Creator"},
            "googleapis.com": {"items": [{
                "snippet": {"title": "Clip", "channelTitle": "Mock Creator"},
                "contentDetails": {"duration": "PT15S"},
                "statistics": {"viewCount": "5"},
                "status": {"privacyStatus": "private"},
            }]},
        }
        res = await self.inspect("https://youtu.be/abc123", payloads, api_key="key")
        self.assertEqual(res["verdict"], "review")
        self.assertTrue(any("shorter" in r for r in res["reasons"]))
        self.assertTrue(any("not public" in r for r in res["reasons"]))

    async def test_channel_mismatch_goes_to_review(self):
        payloads = {
            "youtube.com/oembed": {"title": "Repost", "author_name": "Reposter",
                                   "author_url": "https://www.youtube.com/@reposter"},
            "googleapis.com": {"items": [{
                "snippet": {"title": "Repost", "channelTitle": "Reposter"},
                "contentDetails": {"duration": "PT4M"},
                "statistics": {"viewCount": "99"},
                "status": {"privacyStatus": "public"},
            }]},
        }
        res = await self.inspect("https://youtu.be/abc123", payloads,
                                 creator_doc={"channel": "https://youtube.com/@mockcreator"},
                                 api_key="key")
        self.assertFalse(res["channel_match"])
        self.assertEqual(res["verdict"], "review")
        self.assertTrue(any("does not match" in r for r in res["reasons"]))

    def test_channel_handle_and_match(self):
        self.assertEqual(creator.channel_handle("https://youtube.com/@MockCreator"), "mockcreator")
        self.assertTrue(creator.channel_match(
            {"platform": "youtube", "author": "Mock Creator",
             "author_url": "https://www.youtube.com/@mockcreator"},
            {"channel": "https://youtube.com/@MockCreator"}))
        self.assertFalse(creator.channel_match(
            {"platform": "youtube", "author": "Other", "author_url": "https://youtube.com/@other"},
            {"channel": "https://youtube.com/@MockCreator"}))

    async def test_unverifiable_link_goes_to_review(self):
        res = await self.inspect("https://youtu.be/abc123", {})
        self.assertEqual(res["verdict"], "review")
        self.assertFalse(res["verified"])
        self.assertTrue(res["reasons"])

    async def test_unsupported_platform_goes_to_review(self):
        res = await self.inspect("https://example.com/video", {})
        self.assertEqual(res["platform"], "other")
        self.assertEqual(res["verdict"], "review")

    async def test_medal_is_always_human_reviewed(self):
        res = await self.inspect("https://medal.tv/games/x/clips/1", {})
        self.assertEqual(res["verdict"], "review")
        self.assertTrue(any("auto-verified" in r for r in res["reasons"]))


if __name__ == "__main__":
    unittest.main()
