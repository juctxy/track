import unittest

from owner_finder import (
    OWNER_WAIT_SECONDS,
    _unwrap,
    collection_url,
    frame_cache_filename,
    hoard_cache_filename,
    parse_frame_targets,
)


class CollectionHelperTests(unittest.TestCase):
    def test_tracking_alert_does_not_wait_for_owner_scan(self):
        self.assertLessEqual(OWNER_WAIT_SECONDS, 1.0)

    def test_builds_encoded_url_with_all_hoard_filters(self):
        url = collection_url(
            "42", page=1, page_size=5, rarity="SSR",
            character="Lee Hakhyun", series="Omniscient Reader's Viewpoint", banner="Permanent",
        )
        self.assertIn("rarity=SSR", url)
        self.assertIn("characterSearch=Lee+Hakhyun", url)
        self.assertIn("seriesSearch=Omniscient+Reader%27s+Viewpoint", url)
        self.assertIn("banner=Permanent", url)

    def test_uses_pagination_total_count_not_returned_page_length(self):
        cards, pages, total = _unwrap({"cards": [{"cardCatalogId": 1}], "pagination": {"totalPages": 12, "totalCount": 57}})
        self.assertEqual((len(cards), pages, total), (1, 12, 57))

    def test_cache_key_changes_when_banner_changes(self):
        permanent = hoard_cache_filename("Lee Hakhyun", "ORV", "SSR", "Permanent")
        limited = hoard_cache_filename("Lee Hakhyun", "ORV", "SSR", "Limited")
        self.assertNotEqual(permanent, limited)
        self.assertTrue(permanent.startswith("hoard_"))

    def test_cache_key_normalizes_casing(self):
        title = hoard_cache_filename("Lee Hakhyun", "ORV", "SSR", "Permanent")
        lower = hoard_cache_filename("Lee Hakhyun", "ORV", "ssr", "permanent")
        self.assertEqual(title, lower)

    def test_builds_url_without_rarity_or_banner(self):
        url = collection_url(
            "42", page=1, page_size=1,
            character="Lee Hakhyun", series="Omniscient Reader's Viewpoint"
        )
        self.assertNotIn("rarity=", url)
        self.assertNotIn("banner=", url)
        self.assertIn("characterSearch=Lee+Hakhyun", url)
        self.assertIn("seriesSearch=Omniscient+Reader%27s+Viewpoint", url)

    def test_cache_key_with_empty_rarity_and_banner(self):
        all_key = hoard_cache_filename("Lee Hakhyun", "ORV", "", "")
        self.assertEqual(all_key, "hoard_Lee_Hakhyun_ORV_ALL_ALL.json")

    def test_parse_frame_targets(self):
        query = "Aqualis Twilight, Siren Fuchsia, f:Blood Negative, Aqualis Twilight"
        targets = parse_frame_targets(query)
        self.assertEqual(targets, ["Aqualis Twilight", "Siren Fuchsia", "Blood Negative"])

    def test_frame_cache_filename_order_and_casing_invariant(self):
        f1 = frame_cache_filename(["Aqualis Twilight", "Siren Fuchsia"])
        f2 = frame_cache_filename(["siren fuchsia", "aqualis twilight"])
        self.assertEqual(f1, f2)
        self.assertTrue(f1.startswith("frame_"))

    def test_frame_matching_from_inventory_sections(self):
        sample_items = [
            {"key": "f:baron (gift)", "count": 1, "name": "Baron (Gift)"},
            {"key": "f:siren fuchsia", "count": 2, "name": "Siren Fuchsia"},
            {"key": "f:aqualis twilight", "count": 1, "name": "Aqualis Twilight"},
        ]
        targets = parse_frame_targets("Aqualis Twilight, Siren Fuchsia")
        norm = {t.casefold(): t for t in targets}
        matched = 0
        breakdown = {}
        for item in sample_items:
            n = item.get("name", "")
            k = item.get("key", "").removeprefix("f:").strip()
            if n.casefold() in norm or k.casefold() in norm:
                canonical = norm.get(n.casefold()) or norm.get(k.casefold()) or n
                cnt = item.get("count", 1)
                breakdown[canonical] = breakdown.get(canonical, 0) + cnt
                matched += cnt
        self.assertEqual(matched, 3)
        self.assertEqual(breakdown, {"Aqualis Twilight": 1, "Siren Fuchsia": 2})


if __name__ == "__main__":
    unittest.main()
