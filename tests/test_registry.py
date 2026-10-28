import json
import tempfile
import unittest
from pathlib import Path

from apps_dashboard import registry as reg


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "sub" / "registry.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_is_empty(self):
        self.assertEqual(reg.load(self.path), reg.empty())

    def test_upsert_merges_and_removes(self):
        data = reg.empty()
        reg.upsert(data, "viewer", {"port": 8766, "name": "Viewer", "launchd": "com.x"})
        entry = reg.upsert(data, "viewer", {"name": None, "launchd": "", "description": "d"})
        self.assertEqual({k: v for k, v in entry.items() if k != "updated"},
                         {"port": 8766, "name": "Viewer", "description": "d"})
        reg.save(data, self.path)
        self.assertEqual(reg.load(self.path)["apps"]["viewer"]["port"], 8766)

    def test_bad_ids_and_values(self):
        with self.assertRaises(ValueError):
            reg.upsert(reg.empty(), "Bad Id", {"port": 1})
        self.assertEqual(reg.clean_entry({"port": 70000, "on_demand": 1, "junk": 2, "name": ""}), {"on_demand": True})

    def test_load_drops_junk(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"apps": {"ok": {"port": "8766"}, "Not OK": {"port": 1}, "x": []},
                                         "ignore_ports": [1, "2", True]}))
        data = reg.load(self.path)
        self.assertEqual(data["apps"], {"ok": {"port": 8766}})
        self.assertEqual(data["ignore_ports"], [1])

    def test_registry_rereads_on_change_and_keeps_last_good(self):
        r = reg.Registry(self.path)
        self.assertEqual(r.get()["apps"], {})
        data = reg.empty()
        reg.upsert(data, "a", {"port": 1000})
        reg.save(data, self.path)
        self.assertIn("a", r.get()["apps"])
        self.path.write_text("{broken")
        self.assertIn("a", r.get()["apps"])
        self.assertIsNotNone(r.error)

    def test_unknown_top_level_keys_are_dropped(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"apps": {"ok": {"port": 8778, "via": 8800, "url": "https://x.example/"}},
                                         "peers": ["https://other.example.ts.net/"], "trusted": {"logins": ["a@b"]}}))
        self.assertEqual(reg.load(self.path), {"version": 1, "apps": {"ok": {"port": 8778}}, "ignore_ports": []})


if __name__ == "__main__":
    unittest.main()
