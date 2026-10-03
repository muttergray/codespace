import copy
import csv
import http.server
import io
import json
import os
import sys
import tempfile
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import must_to_letterboxd as m  # noqa: E402

FIXTURE = os.path.join(HERE, "fixtures", "must_backup.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as file:
        return json.load(file)


def read_csv(path):
    with open(path, encoding="utf-8", newline="") as file:
        return list(csv.DictReader(file))


class HelpersTest(unittest.TestCase):
    def test_normalize_username(self):
        for value in ["vladimirsalov", "@vladimirsalov", " vladimirsalov ",
                      "https://mustapp.com/@vladimirsalov", "https://mustapp.com/@vladimirsalov/watched",
                      "http://www.mustapp.com/vladimirsalov?tab=1"]:
            self.assertEqual(m.normalize_username(value), "vladimirsalov", value)

    def test_rating10(self):
        self.assertEqual(m.rating10(7), "7")
        self.assertEqual(m.rating10(7.0), "7")
        self.assertEqual(m.rating10("10"), "10")
        for value in [None, 0, 11, -1, 7.5, "7.5", "", "abc", True, [], {}]:
            self.assertEqual(m.rating10(value), "", value)

    def test_date_part(self):
        self.assertEqual(m.date_part("2024-03-10T20:00:00.000Z"), "2024-03-10")
        self.assertEqual(m.date_part(None), "")
        self.assertEqual(m.date_part("garbage"), "")

    def test_csv_parts_split_under_limit(self):
        rows = [[str(i), "x" * 100] for i in range(100)]
        parts = m.csv_parts(["A", "B"], rows, max_bytes=1000)
        self.assertGreater(len(parts), 1)
        seen = []
        for part in parts:
            self.assertLessEqual(len(part.encode("utf-8")), 1000)
            reader = list(csv.reader(io.StringIO(part)))
            self.assertEqual(reader[0], ["A", "B"])
            seen += [row[0] for row in reader[1:]]
        self.assertEqual(seen, [str(i) for i in range(100)])

    def test_csv_parts_empty_has_header(self):
        self.assertEqual(m.csv_parts(["A", "B"], []), ["A,B\n"])


class BuildEntriesTest(unittest.TestCase):
    def test_splits_lists_and_types(self):
        films, tv = m.build_entries(load_fixture())
        self.assertEqual(sum(e["list"] == "watched" for e in films), 12)
        self.assertEqual(sum(e["list"] == "want" for e in films), 2)
        self.assertEqual(sorted(e["title"] for e in tv), ["Breaking Bad", "Severance"])

    def test_merges_reviews_by_product_id(self):
        backup = load_fixture()
        backup["reviews"].reverse()  # order must not matter when ids are present
        films, _ = m.build_entries(backup)
        brat = next(e for e in films if e["must_id"] == 103)
        self.assertEqual(brat["review"], "Лучший фильм\r\nдевяностых")

    def test_merges_reviews_by_index_without_ids(self):
        backup = load_fixture()
        for item in backup["reviews"]:
            del item["user_product_info"]["product_id"]
        films, _ = m.build_entries(backup)
        self.assertEqual(next(e for e in films if e["must_id"] == 104)["review"], "IMAX 🔥")

    def test_missing_reviews_ok(self):
        backup = load_fixture()
        backup["reviews"] = []
        films, _ = m.build_entries(backup)
        self.assertTrue(all(e["review"] == "" for e in films))

    def test_prefers_watched_at(self):
        backup = load_fixture()
        backup["products"][0]["user_product_info"]["watched_at"] = "2019-05-05T00:00:00Z"
        films, _ = m.build_entries(backup)
        self.assertEqual(films[0]["date"], "2019-05-05")

    def test_product_missing_from_response_is_skipped(self):
        backup = load_fixture()
        backup["profile"]["lists"]["watched"].append(999)
        films, _ = m.build_entries(backup)
        self.assertNotIn(999, [e["must_id"] for e in films])


class DatePolicyTest(unittest.TestCase):
    def watched(self):
        films, _ = m.build_entries(load_fixture())
        return [e for e in films if e["list"] == "watched"]

    def test_smart(self):
        watched = self.watched()
        stats = m.apply_date_policy(watched, "smart", 30, 5)
        self.assertEqual((stats["kept"], stats["window"], stats["bulk"], stats["missing"]), (4, 3, 5, 0))
        kept = {e["title"]: e["watched_date"] for e in watched if e["watched_date"]}
        self.assertEqual(kept, {"Oppenheimer": "2023-07-25", "Unknown Festival Short": "2023-08-01",
                                "Dune: Part Two": "2024-03-10", "Perfect Days": "2025-01-05"})

    def test_all_and_none(self):
        watched = self.watched()
        self.assertEqual(m.apply_date_policy(watched, "all")["kept"], 12)
        self.assertTrue(all(e["watched_date"] == e["date"] for e in watched))
        m.apply_date_policy(watched, "none")
        self.assertTrue(all(e["watched_date"] == "" for e in watched))

    def test_window_boundary(self):
        entries = [{"date": d} for d in ["2021-01-01", "2021-01-30", "2021-01-31", "2021-02-01"]]
        m.apply_date_policy(entries, "smart", window_days=30, bulk_per_day=0)
        self.assertEqual([e["watched_date"] for e in entries], ["", "", "2021-01-31", "2021-02-01"])

    def test_empty(self):
        stats = m.apply_date_policy([], "smart")
        self.assertEqual(stats["kept"], 0)


class ConvertTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_outputs(self):
        files, report = m.convert(load_fixture(), self.out, tag="must-import")
        names = sorted(os.path.basename(f) for f in files)
        self.assertEqual(names, ["testuser_letterboxd_watched.csv", "testuser_letterboxd_watchlist.csv",
                                 "testuser_must_backup.json", "testuser_must_tv.csv", "testuser_report.txt"])
        watched = read_csv(os.path.join(self.out, "testuser_letterboxd_watched.csv"))
        self.assertEqual(list(watched[0].keys()), m.WATCHED_COLUMNS)
        self.assertEqual(len(watched), 12)
        tiger = next(r for r in watched if r["Title"].startswith("Crouching"))
        self.assertEqual(tiger["Title"], "Crouching Tiger, Hidden Dragon")
        self.assertEqual(tiger["Review"], 'Wire-fu at its best.<br>Saw it twice, "wow".')
        self.assertEqual(tiger["Tags"], "")  # no diary date -> no tag
        dune = next(r for r in watched if r["Title"] == "Dune: Part Two")
        self.assertEqual((dune["Year"], dune["Rating10"], dune["WatchedDate"], dune["Tags"]),
                         ("2024", "9", "2024-03-10", "must-import"))
        dated = [r["WatchedDate"] for r in watched if r["WatchedDate"]]
        self.assertEqual(dated, sorted(dated))
        watchlist = read_csv(os.path.join(self.out, "testuser_letterboxd_watchlist.csv"))
        self.assertEqual([r["Title"] for r in watchlist], ["Mickey 17", '"Weird" Title, With Comma'])
        self.assertIn("Watched films:   12", report)

    def test_no_reviews(self):
        m.convert(load_fixture(), self.out, include_reviews=False)
        watched = read_csv(os.path.join(self.out, "testuser_letterboxd_watched.csv"))
        self.assertTrue(all(r["Review"] == "" for r in watched))

    def test_backup_roundtrip(self):
        m.convert(load_fixture(), self.out)
        again = m.load_backup(os.path.join(self.out, "testuser_must_backup.json"))
        self.assertEqual(again, load_fixture())

    def test_big_export_is_split(self):
        backup = load_fixture()
        base = backup["products"][0]
        for i in range(5000):
            item = copy.deepcopy(base)
            item["product"]["id"] = item["user_product_info"]["product_id"] = 10_000 + i
            item["product"]["title"] = f"Film {i}"
            item["user_product_info"]["review"] = {"body": "A long review. " * 20}
            backup["products"].append(item)
            backup["profile"]["lists"]["watched"].append(10_000 + i)
        files, _ = m.convert(backup, self.out)
        parts = sorted(f for f in files if "letterboxd_watched_part" in f)
        self.assertGreater(len(parts), 1)
        total = 0
        for path in parts:
            self.assertLess(os.path.getsize(path), 1024 * 1024)
            total += len(read_csv(path))
        self.assertEqual(total, 12 + 5000)


class FakeMust(http.server.BaseHTTPRequestHandler):
    backup = None
    requests = []

    def log_message(self, *args):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        FakeMust.requests.append(("GET", self.path, {k.lower(): v for k, v in self.headers.items()}, None))
        if self.path == "/api/users/uri/testuser":
            self.reply(self.backup["profile"])
        else:
            self.reply({"error": {"message": "User not found"}}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        FakeMust.requests.append(("POST", self.path, {k.lower(): v for k, v in self.headers.items()}, body))
        if self.headers.get("bearer") != m.MUST_HEADERS["bearer"]:
            return self.reply({"error": "no bearer"}, 403)
        ids = body["ids"]
        source = self.backup["reviews"] if self.path.endswith("embed=review") else self.backup["products"]
        by_id = {m.product_id(item) or item["user_product_info"]["product_id"]: item for item in source}
        self.reply([by_id[i] for i in ids if i in by_id])


class FetchTest(unittest.TestCase):
    def setUp(self):
        os.environ["no_proxy"] = os.environ["NO_PROXY"] = "127.0.0.1,localhost"
        FakeMust.backup = load_fixture()
        FakeMust.requests = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeMust)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.old_api = m.MUST_API
        m.MUST_API = f"http://127.0.0.1:{self.server.server_port}/api"

    def tearDown(self):
        m.MUST_API = self.old_api
        self.server.shutdown()
        self.server.server_close()

    def test_fetch_matches_fixture(self):
        backup = m.fetch_must_backup("testuser")
        self.assertEqual(backup["profile"], FakeMust.backup["profile"])
        self.assertEqual(m.build_entries(backup), m.build_entries(FakeMust.backup))
        posts = [r for r in FakeMust.requests if r[0] == "POST"]
        self.assertEqual({r[1] for r in posts}, {"/api/users/id/4242/products?embed=product",
                                                 "/api/users/id/4242/products?embed=review"})
        self.assertTrue(all(r[2]["accept-language"] == "en" for r in FakeMust.requests))

    def test_batches_of_100(self):
        extra = list(range(50_000, 50_250))
        FakeMust.backup["profile"]["lists"]["want"] += extra
        m.fetch_must_backup("testuser")
        sizes = [len(r[3]["ids"]) for r in FakeMust.requests if r[1].endswith("embed=product")]
        self.assertEqual(sizes, [100, 100, 66])  # 16 fixture ids + 250

    def test_unknown_user(self):
        with self.assertRaises(RuntimeError) as error:
            m.fetch_must_backup("nobody")
        self.assertIn("404", str(error.exception))

    def test_private_profile(self):
        FakeMust.backup["profile"]["is_private"] = True
        with self.assertRaises(RuntimeError) as error:
            m.fetch_must_backup("testuser")
        self.assertIn("private", str(error.exception))

    def test_cli_end_to_end(self):
        with tempfile.TemporaryDirectory() as out:
            self.assertEqual(m.main(["@testuser", "--out-dir", out, "--dates", "all"]), 0)
            watched = read_csv(os.path.join(out, "testuser_letterboxd_watched.csv"))
            self.assertEqual(sum(1 for r in watched if r["WatchedDate"]), 12)


if __name__ == "__main__":
    unittest.main()
