# -*- coding: utf-8 -*-
"""watch（关注清单）单测：存储 roundtrip / 变动检测 / 结果标记 / 信封转换。不联网。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import booth  # noqa: E402


class TestWishStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        booth.WISH_DB_PATH = Path(self._tmp.name) / "wish.sqlite3"
        booth._WISH_CONN = None

    def tearDown(self):
        if booth._WISH_CONN not in (None, False):
            booth._WISH_CONN.close()
        booth._WISH_CONN = None
        self._tmp.cleanup()

    def _row(self, iid, name="n", price=1000, sold=0, updated="2026-01-01T00:00:00Z"):
        return {"id": iid, "name": name, "price": price,
                "is_sold_out": sold, "updated_at": updated}

    def _add(self, kind, iid, **kw):
        with mock.patch.object(booth, "fetch_item", return_value=self._row(iid, **kw)):
            booth._cmd_library(booth.build_parser().parse_args(
                [kind, "add", str(iid), "--json"]), kind)

    def test_add_list_remove_roundtrip(self):
        self._add("watch", 123, price=1500)
        self.assertIn(123, booth.wish_ids())
        self.assertNotIn(123, booth.own_ids())  # watch/own 互不污染

        with mock.patch("sys.stdout"):
            booth._cmd_library(booth.build_parser().parse_args(
                ["watch", "remove", "123", "--json"]), "watch")
        self.assertNotIn(123, booth.wish_ids())
        with mock.patch("sys.stdout"):  # 幂等
            booth._cmd_library(booth.build_parser().parse_args(
                ["watch", "remove", "123", "--json"]), "watch")

    def test_own_roundtrip_and_kind_isolation(self):
        self._add("own", 7, price=500)
        self._add("watch", 7, price=500)  # 同一商品可同时已购+关注
        self.assertEqual(booth.own_ids(), {7})
        self.assertEqual(booth.wish_ids(), {7})
        with mock.patch("sys.stdout"):
            booth._cmd_library(booth.build_parser().parse_args(
                ["own", "remove", "7", "--json"]), "own")
        self.assertEqual(booth.own_ids(), set())
        self.assertEqual(booth.wish_ids(), {7})  # 删 own 不影响 watch

    def test_changes_detection(self):
        import io, json
        booth._wish_db().execute(
            "INSERT INTO library (kind, id, name, price, is_sold_out, updated_at, added_at, last_checked) "
            "VALUES ('watch', 1, '旧名', 2000, 1, '2026-01-01T00:00:00Z', 0, 0)")
        booth._wish_db().commit()
        new = self._row(1, name="新名", price=1500, sold=0,
                        updated="2026-02-02T00:00:00Z")
        buf = io.StringIO()
        with mock.patch.object(booth, "fetch_item", return_value=new), \
                mock.patch("sys.stdout", buf):
            booth._cmd_library(booth.build_parser().parse_args(["watch", "check", "--json"]), "watch")
        data = json.loads(buf.getvalue())
        self.assertEqual(data["checked"], 1)
        self.assertEqual(len(data["changed"]), 1)
        changes = data["changed"][0]["changes"]
        self.assertTrue(any("↓" in c for c in changes))       # 降价
        self.assertTrue(any("补货" in c for c in changes))     # 售罄→在售
        self.assertTrue(any("更新" in c for c in changes))     # updated_at 变化
        self.assertTrue(any("改名" in c for c in changes))     # 改名
        # 快照已更新：再查无变动
        buf2 = io.StringIO()
        with mock.patch.object(booth, "fetch_item", return_value=new), \
                mock.patch("sys.stdout", buf2):
            booth._cmd_library(booth.build_parser().parse_args(["watch", "check", "--json"]), "watch")
        self.assertEqual(json.loads(buf2.getvalue())["changed"], [])

    def test_unavailable_store_degrades(self):
        # NUL 字节路径在 Windows/Linux 上都无法创建——OS 无关的「存储不可用」模拟
        # （用不存在的盘符在 Linux 上是合法相对路径，mkdir 会成功，测不出降级）
        booth.WISH_DB_PATH = Path("Z:/no/such/\x00dir/wish.sqlite3")
        booth._WISH_CONN = None
        self.assertEqual(booth.wish_ids(), set())  # 搜索标记静默降级
        argv = booth.build_parser().parse_args(["watch", "list"])
        with self.assertRaises(booth.BoothError):
            booth.cmd_watch(argv)  # 显式操作给出明确错误

    def test_mark_watched(self):
        booth._wish_db().execute(
            "INSERT INTO library (kind, id, name, price, is_sold_out, updated_at, added_at, last_checked) "
            "VALUES ('watch', 7, 'x', 1, 0, NULL, 0, 0)")
        booth._wish_db().execute(
            "INSERT INTO library (kind, id, name, price, is_sold_out, updated_at, added_at, last_checked) "
            "VALUES ('own', 9, 'y', 1, 0, NULL, 0, 0)")
        booth._wish_db().commit()
        items = [{"id": 7, "name": "a"}, {"id": 9, "name": "b"}, {"id": 8, "name": "c"}]
        booth._mark_watched(items)
        self.assertTrue(items[0].get("watched"))
        self.assertFalse(items[0].get("owned", False))
        self.assertTrue(items[1].get("owned"))
        self.assertFalse(items[1].get("watched", False))
        self.assertNotIn("watched", items[2])
        self.assertNotIn("owned", items[2])

    def test_envelope_argv(self):
        argv = booth.bot_params_to_argv("watch", {"op": "add", "ids": [123, "456"]})
        self.assertEqual(argv, ["watch", "add", "123", "456"])
        ns = booth.build_parser().parse_args(argv + ["--json"])
        self.assertEqual(ns.op, "add")
        self.assertEqual(ns.ids, ["123", "456"])
        self.assertEqual(booth.bot_params_to_argv("watch", {"op": "list"}),
                         ["watch", "list"])
        own_argv = booth.bot_params_to_argv("own", {"op": "add", "ids": [9]})
        self.assertEqual(own_argv, ["own", "add", "9"])
        ns_own = booth.build_parser().parse_args(own_argv + ["--json"])
        self.assertEqual(ns_own.op, "add")

    def test_legacy_watch_table_migration(self):
        # 旧版单 watch 表迁移到 library(kind='watch') 且旧表被删除
        conn = booth._wish_db()
        conn.execute("CREATE TABLE watch (id INTEGER PRIMARY KEY, name TEXT, price INTEGER, "
                     "is_sold_out INTEGER, updated_at TEXT, added_at REAL, last_checked REAL)")
        conn.execute("INSERT INTO watch (id, name, price) VALUES (55, 'legacy', 100)")
        conn.commit()
        conn.close()  # 关闭旧连接再重开（未关闭会占住文件，Windows 下 tempdir 清理失败）
        booth._WISH_CONN = None  # 触发重新打开（执行迁移）
        booth._wish_db()
        self.assertIn(55, booth.wish_ids())
        migrated = booth._wish_db().execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='watch'").fetchone()
        self.assertIsNone(migrated)

    def test_search_json_marks_owned_and_watched(self):
        import io, json as j
        conn = booth._wish_db()
        conn.execute("INSERT INTO library (kind, id, name, price, is_sold_out, updated_at, "
                     "added_at, last_checked) VALUES ('own', 3368697, 'x', 1, 0, NULL, 0, 0)")
        conn.execute("INSERT INTO library (kind, id, name, price, is_sold_out, updated_at, "
                     "added_at, last_checked) VALUES ('watch', 999, 'y', 1, 0, NULL, 0, 0)")
        conn.commit()
        page = ('<html>対象商品 1 件'
                '<li class="item-card " data-product-id="3368697" data-product-price="2980">'
                '<p class="item-card__title"><a href="/ja/items/3368697">Ciel</a></p>'
                '</li></html>')
        buf = io.StringIO()
        with mock.patch.object(booth, "get_html", return_value=(page, "u")), \
                mock.patch("sys.stdout", buf):
            booth.cmd_search(booth.build_parser().parse_args(
                ["search", "ciel", "--json"]))
        data = j.loads(buf.getvalue())
        it = data["items"][0]
        self.assertEqual(it["id"], 3368697)
        self.assertTrue(it.get("owned"))
        self.assertNotIn("watched", it)  # 只在关注清单的 999 不在结果里


if __name__ == "__main__":
    unittest.main()
