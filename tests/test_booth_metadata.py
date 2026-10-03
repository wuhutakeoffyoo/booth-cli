# -*- coding: utf-8 -*-
"""Offline regressions for BOOTH pagination and additive item metadata."""

import contextlib
import copy
import html
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import booth  # noqa: E402


THUMBNAIL = (
    "https://booth.pximg.net/c/300x300_a2_g5/12345678/"
    "jacket_base_resized.jpg?format=webp&quality=80"
)
LEGACY_IMAGE = (
    "https://booth.pximg.net/12345678/jacket.jpg?format=webp&quality=80"
)


def search_card(item_id=12345678, thumbnail=None):
    image = (
        f'<img class="item-card__image" data-original="{html.escape(thumbnail, quote=True)}">'
        if thumbnail else ""
    )
    return (
        f'<li class="item-card " data-product-id="{item_id}" '
        'data-product-brand="wardrobe" data-product-price="1200">'
        '<p class="item-card__title">'
        f'<a href="https://wardrobe.booth.pm/items/{item_id}">Jacket {item_id}</a></p>'
        '<a class="item-card__shop-name-anchor" href="https://wardrobe.booth.pm/">'
        '<span class="item-card__shop-name">Wardrobe</span></a>'
        f'{image}</li>'
    )


def shop_page(raw):
    return '<div data-item="{}"></div>'.format(
        html.escape(json.dumps(raw), quote=True)
    )


class TestSearchPagination(unittest.TestCase):
    def test_rel_next_link_remains_supported(self):
        for link in (
            '<a rel="next" href="?page=2">Next</a>',
            "<a href='?page=2' rel='next'>Next</a>",
            '<link rel="next" href="https://booth.pm/ja/search/jacket?page=2">',
        ):
            with self.subTest(link=link):
                self.assertTrue(booth.parse_search_page(link)["has_next"])

    def test_current_pagination_arrow_link_supports_both_quote_styles(self):
        for link in (
            '<nav class="pagination"><a class="pager" href="?page=2">'
            '<i class="icon-arrow-open-right"></i></a></nav>',
            "<nav class='pagination'><a href='?sort=new&amp;page=2' class='pager'>"
            "<span><i class='icon icon-arrow-open-right extra'></i></span></a></nav>",
            '<a href="/ja/search/jacket?sort=new&amp;page=2">'
            '<i class="icon-arrow-open-right"></i></a>',
        ):
            with self.subTest(link=link):
                self.assertTrue(booth.parse_search_page(link)["has_next"])

    def test_disabled_controls_are_not_next_pages(self):
        for link in (
            '<a class="pager disabled" href="?page=2"><i class="icon-arrow-open-right"></i></a>',
            '<a aria-disabled="true" href="?page=2"><i class="icon-arrow-open-right"></i></a>',
            '<li class="disabled"><a href="?page=2"><i class="icon-arrow-open-right"></i></a></li>',
            "<li aria-disabled='true'><span><a href='?page=2'>"
            "<i class='icon-arrow-open-right'></i></a></span></li>",
            '<a rel="next" class="disabled" href="?page=2">Next</a>',
            '<div aria-disabled="true"><a rel="next" href="?page=2">Next</a></div>',
        ):
            with self.subTest(link=link):
                self.assertFalse(booth.parse_search_page(link)["has_next"])

    def test_missing_or_placeholder_destinations_are_not_next_pages(self):
        for link in (
            '<a><i class="icon-arrow-open-right"></i></a>',
            '<a href=""><i class="icon-arrow-open-right"></i></a>',
            '<a href="#"><i class="icon-arrow-open-right"></i></a>',
            '<a rel="next">Next</a>',
            '<a rel="next" href="#">Next</a>',
        ):
            with self.subTest(link=link):
                self.assertFalse(booth.parse_search_page(link)["has_next"])

    def test_unrelated_arrows_and_lookalike_class_names_are_not_next_pages(self):
        for link in (
            '<a href="?page=2"><i class="icon-arrow-open-right-disabled"></i></a>',
            '<a href="?page=2"><i class="prefix-icon-arrow-open-right"></i></a>',
            '<a href="/ja/items/12345678"><i class="icon-arrow-open-right"></i></a>',
            '<a href="?not_page=2"><i class="icon-arrow-open-right"></i></a>',
            '<a href="?q=page%3D2"><i class="icon-arrow-open-right"></i></a>',
            '<i class="icon-arrow-open-right"></i><a href="?page=2">2</a>',
        ):
            with self.subTest(link=link):
                self.assertFalse(booth.parse_search_page(link)["has_next"])

    def test_unrelated_disabled_sibling_does_not_disable_next_link(self):
        page = (
            '<nav class="pagination"><span class="disabled">Previous</span>'
            '<a href="?page=2"><i class="icon-arrow-open-right"></i></a></nav>'
        )
        self.assertTrue(booth.parse_search_page(page)["has_next"])


class TestThumbnailMetadata(unittest.TestCase):
    def test_search_keeps_server_thumbnail_and_legacy_image(self):
        page = search_card(thumbnail=THUMBNAIL)
        self.assertIn("&amp;quality=80", page)
        item = booth.parse_search_page(page)["items"][0]
        self.assertEqual(item["thumbnail"], THUMBNAIL)
        self.assertEqual(item["image"], LEGACY_IMAGE)

    def test_search_missing_thumbnail_is_explicit_none(self):
        item = booth.parse_search_page(search_card())["items"][0]
        self.assertIsNone(item["thumbnail"])
        self.assertIsNone(item["image"])

    def test_shop_keeps_first_server_thumbnail_and_legacy_image(self):
        page = shop_page({
            "id": 12345678,
            "name": "Jacket",
            "thumbnail_image_urls": [THUMBNAIL, "https://booth.pximg.net/other.jpg"],
        })
        self.assertIn("&amp;quality=80", page)
        item = booth.parse_shop_page(page)[0]
        self.assertEqual(item["thumbnail"], THUMBNAIL)
        self.assertEqual(item["image"], LEGACY_IMAGE)

    def test_shop_missing_thumbnail_is_explicit_none(self):
        for raw in ({"id": 1}, {"id": 1, "thumbnail_image_urls": []}):
            with self.subTest(raw=raw):
                item = booth.parse_shop_page(shop_page(raw))[0]
                self.assertIsNone(item["thumbnail"])
                self.assertIsNone(item["image"])

    def test_detail_uses_first_resized_image_and_keeps_legacy_images(self):
        original = "https://booth.pximg.net/12345678/original_base_resized.png"
        second = "https://booth.pximg.net/c/1200x1200/second_base_resized.jpg"
        raw = {"id": 1, "images": [
            {"original": original, "resized": THUMBNAIL},
            {"resized": second},
        ]}
        before = copy.deepcopy(raw)
        item = booth.trim_item(raw)
        self.assertEqual(item["thumbnail"], THUMBNAIL)
        self.assertEqual(item["original_images"], [original])
        self.assertEqual(item["images"], [
            "https://booth.pximg.net/12345678/original.png",
            "https://booth.pximg.net/c/1200x1200/second.jpg",
        ])
        self.assertEqual(raw, before)

    def test_detail_falls_back_to_unmodified_first_original(self):
        item = booth.trim_item({"images": [
            {"original": THUMBNAIL},
            {"resized": "https://booth.pximg.net/second.jpg"},
        ]})
        self.assertEqual(item["thumbnail"], THUMBNAIL)
        self.assertEqual(item["images"][0],
                         "https://booth.pximg.net/c/300x300_a2_g5/12345678/"
                         "jacket.jpg?format=webp&quality=80")

    def test_detail_without_images_has_explicit_none_thumbnail(self):
        for raw in ({}, {"images": []}, {"images": None}):
            with self.subTest(raw=raw):
                item = booth.trim_item(raw)
                self.assertIsNone(item["thumbnail"])
                self.assertEqual(item["images"], [])
                self.assertEqual(item["original_images"], [])

    def test_detail_empty_image_does_not_hide_later_thumbnail(self):
        item = booth.trim_item({"images": [{"original": ""}, {"resized": THUMBNAIL}]})
        self.assertEqual(item["thumbnail"], THUMBNAIL)

    def test_detail_only_empty_addresses_has_none_thumbnail(self):
        item = booth.trim_item({"images": [{"original": "", "resized": ""}]})
        self.assertIsNone(item["thumbnail"])


class TestVariationMetadata(unittest.TestCase):
    def test_name_falls_back_when_legacy_fields_are_empty(self):
        variation = {
            "field1": "", "field2": None, "field3": "", "field4": None,
            "field5": "", "field6": None, "name": "Full set",
            "price": 0, "status": "available", "is_end_of_sale": True,
        }
        item = booth.trim_item({"variations": [variation]})
        self.assertEqual(item["variations"], [
            {"name": "Full set", "price": 0, "status": "available"}
        ])

    def test_legacy_fields_have_precedence_and_keep_their_order(self):
        variation = {
            "field1": "Jacket", "field2": "", "field3": "Blue", "field6": "Large",
            "name": "Fallback must not replace fields", "price": 1200,
            "is_end_of_sale": True,
        }
        item = booth.trim_item({"variations": [variation]})
        self.assertEqual(item["variations"], [
            {"name": "Jacket / Blue / Large", "price": 1200, "status": "end_of_sale"}
        ])

    def test_absent_or_empty_name_is_none(self):
        for variation in ({}, {"name": ""}, {"name": None}):
            with self.subTest(variation=variation):
                item = booth.trim_item({"variations": [variation]})
                self.assertEqual(item["variations"], [
                    {"name": None, "price": None, "status": None}
                ])


class TestSearchCommandPagination(unittest.TestCase):
    def test_fetches_two_real_pages_and_stops_at_disabled_next(self):
        first = (
            '<html><body><div>対象商品 2 件</div><ul class="items">'
            + search_card(12345678, THUMBNAIL)
            + '</ul><nav class="pagination"><span class="current">1</span>'
            '<a href="/ja/search/jacket?sort=new&amp;page=2">'
            '<i class="icon-arrow-open-right"></i></a></nav></body></html>'
        )
        last = (
            '<html><body><div>対象商品 2 件</div><ul class="items">'
            + search_card(12345679, THUMBNAIL)
            + '</ul><nav class="pagination"><a href="?page=1">'
            '<i class="icon-arrow-open-left"></i></a><span class="current">2</span>'
            '<span class="disabled"><a href="?page=3" aria-disabled="true">'
            '<i class="icon-arrow-open-right"></i></a></span></nav></body></html>'
        )
        args = booth.build_parser().parse_args([
            "search", "jacket", "--sort", "new", "--pages", "5",
            "--limit", "20", "--no-vrc", "--json",
        ])
        output = io.StringIO()
        responses = [(first, "https://booth.pm/ja/search/jacket?sort=new"),
                     (last, "https://booth.pm/ja/search/jacket?sort=new&page=2")]
        with mock.patch.object(booth, "get_html", side_effect=responses) as get_html, \
                mock.patch.object(booth.time, "sleep"), \
                mock.patch.object(booth._OPENER, "open", side_effect=AssertionError("network forbidden")), \
                contextlib.redirect_stdout(output):
            booth.cmd_search(args)

        result = json.loads(output.getvalue())
        self.assertEqual(get_html.call_count, 2)
        requested_pages = [
            parse_qs(urlsplit(call.args[0]).query).get("page", ["1"])[0]
            for call in get_html.call_args_list
        ]
        self.assertEqual(requested_pages, ["1", "2"])
        self.assertEqual(result["pages_fetched"], 2)
        self.assertEqual(result["page"], 2)
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["count"], 2)
        self.assertFalse(result["has_next"])
        self.assertEqual([item["id"] for item in result["items"]], [12345678, 12345679])


if __name__ == "__main__":
    unittest.main()
