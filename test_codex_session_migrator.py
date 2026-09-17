import importlib.util
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path


MODULE_PATH = Path(__file__).with_name("codex_session_migrator.py")
SPEC = importlib.util.spec_from_file_location("codex_session_migrator", MODULE_PATH)
assert SPEC and SPEC.loader
migrator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(migrator)


class MigratorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / ".codex"
        self.sessions = self.codex / "sessions" / "2026" / "09" / "17"
        self.sessions.mkdir(parents=True)
        self.backups = self.root / "backups"

        self.official = self.sessions / "official.jsonl"
        self.third_party = self.sessions / "third-party.jsonl"
        self.official.write_text(
            json.dumps(
                {
                    "timestamp": "2026-09-17T00:00:00Z",
                    "type": "session_meta",
                    "payload": {"id": "session-official", "model_provider": "openai"},
                },
                separators=(",", ":"),
            )
            + "\n"
            + json.dumps(
                {"type": "response_item", "payload": {"text": "正文不能改变"}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        self.third_party.write_text(
            json.dumps(
                {
                    "type": "session_meta",
                    "payload": {"id": "session-third", "model_provider": "custom"},
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as conn:
            conn.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, title TEXT)")
            conn.executemany(
                "INSERT INTO threads VALUES (?, ?, ?)",
                [
                    ("session-official", "openai", "official"),
                    ("session-third", "custom", "third"),
                ],
            )
            conn.commit()

    def tearDown(self):
        self.temp.cleanup()

    def provider_in_file(self, path):
        return migrator.read_session_meta(path)[1]

    def provider_in_db(self, thread_id):
        with closing(sqlite3.connect(self.codex / "state_5.sqlite")) as conn:
            return conn.execute(
                "SELECT model_provider FROM threads WHERE id=?", (thread_id,)
            ).fetchone()[0]

    def test_scan_reports_both_sources(self):
        report = migrator.scan(self.codex)
        self.assertEqual(report["jsonl_providers"], {"openai": 1, "custom": 1})
        self.assertEqual(
            report["state_databases"][0]["providers"], {"custom": 1, "openai": 1}
        )

    def test_dry_run_changes_nothing_and_creates_no_generation(self):
        result = migrator.migrate(
            self.codex, self.backups, {"openai"}, "custom", dry_run=True
        )
        self.assertEqual(len(result["jsonl_changes"]), 1)
        self.assertEqual(len(result["state_db_changes"][0]["threads"]), 1)
        self.assertEqual(self.provider_in_file(self.official), "openai")
        self.assertEqual(self.provider_in_db("session-official"), "openai")
        self.assertEqual(list(self.backups.glob("*/manifest.json")), [])

    def test_migrate_and_precise_restore(self):
        body_before = self.official.read_text(encoding="utf-8").splitlines()[1]
        result = migrator.migrate(
            self.codex, self.backups, {"openai"}, "custom", dry_run=False
        )
        generation = Path(result["backup_dir"])
        self.assertTrue((generation / "manifest.json").is_file())
        self.assertEqual(self.provider_in_file(self.official), "custom")
        self.assertEqual(self.provider_in_file(self.third_party), "custom")
        self.assertEqual(self.provider_in_db("session-official"), "custom")
        self.assertEqual(self.provider_in_db("session-third"), "custom")
        self.assertEqual(self.official.read_text(encoding="utf-8").splitlines()[1], body_before)

        restored = migrator.restore(generation, dry_run=False)
        self.assertEqual(restored["restored_jsonl_files"], 1)
        self.assertEqual(restored["restored_state_rows"], 1)
        self.assertEqual(self.provider_in_file(self.official), "openai")
        self.assertEqual(self.provider_in_file(self.third_party), "custom")
        self.assertEqual(self.provider_in_db("session-official"), "openai")
        self.assertEqual(self.provider_in_db("session-third"), "custom")

    def test_restore_is_idempotent(self):
        result = migrator.migrate(
            self.codex, self.backups, {"openai"}, "custom", dry_run=False
        )
        migrator.restore(Path(result["backup_dir"]), dry_run=False)
        again = migrator.restore(Path(result["backup_dir"]), dry_run=False)
        self.assertEqual(again["restored_jsonl_files"], 0)
        self.assertEqual(again["restored_state_rows"], 0)

    def test_manifest_path_cannot_escape_codex_dir(self):
        result = migrator.migrate(
            self.codex, self.backups, {"openai"}, "custom", dry_run=False
        )
        generation = Path(result["backup_dir"])
        manifest_path = generation / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["jsonl_changes"][0]["path"] = "../outside.jsonl"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaises(migrator.MigrationError):
            migrator.restore(generation, dry_run=True)

    def test_cross_computer_export_import_merges_without_overwriting(self):
        package = self.root / "transfer.codexsessions"
        exported = migrator.export_transfer_package(
            self.codex, package, providers={"openai"}
        )
        self.assertEqual(exported["sessions"], 1)

        target = self.root / "target-codex"
        target.mkdir()
        with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
            conn.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, model_provider TEXT, "
                "title TEXT, rollout_path TEXT, cwd TEXT)"
            )
            conn.commit()

        result = migrator.import_transfer_package(
            target,
            package,
            provider_override="custom",
            cwd_override="D:/new-project",
        )
        self.assertEqual(result["imported"], 1)
        self.assertEqual(result["indexed"], 1)
        imported_files = list((target / "sessions").rglob("*.jsonl"))
        self.assertEqual(len(imported_files), 1)
        meta = migrator.read_full_session_meta(imported_files[0])
        self.assertEqual(meta["model_provider"], "custom")
        self.assertEqual(meta["cwd"], "D:/new-project")
        self.assertIn("正文不能改变", imported_files[0].read_text(encoding="utf-8"))
        with closing(sqlite3.connect(target / "state_5.sqlite")) as conn:
            row = conn.execute(
                "SELECT model_provider, cwd, rollout_path FROM threads WHERE id=?",
                ("session-official",),
            ).fetchone()
        self.assertEqual(row[0], "custom")
        self.assertEqual(row[1], "D:/new-project")
        self.assertEqual(Path(row[2]), imported_files[0])

        again = migrator.import_transfer_package(target, package)
        self.assertEqual(again["imported"], 0)
        self.assertEqual(again["skipped_existing"], 1)


if __name__ == "__main__":
    unittest.main()
