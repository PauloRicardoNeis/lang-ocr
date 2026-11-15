from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import settings
from settings import (
    REGISTRY,
    SECTIONS,
    SOURCE_DB,
    SOURCE_DEFAULT,
    SOURCE_ENV,
    SettingError,
    Settings,
    load_settings,
    reset_section,
    reset_settings,
    set_setting,
    set_settings,
)


def stored(connection: sqlite3.Connection) -> dict[str, str]:
    return dict(connection.execute("SELECT key, value FROM settings").fetchall())


class RegistryTest(unittest.TestCase):
    def test_keys_and_env_names_are_unique(self) -> None:
        keys = [setting.key for setting in REGISTRY]
        self.assertEqual(len(keys), len(set(keys)))
        env_names = [setting.env for setting in REGISTRY if setting.env]
        self.assertEqual(len(env_names), len(set(env_names)))

    def test_every_setting_belongs_to_a_section_and_has_a_valid_default(self) -> None:
        section_keys = {section.key for section in SECTIONS}
        for setting in REGISTRY:
            with self.subTest(key=setting.key):
                self.assertIn(setting.section, section_keys)
                self.assertTrue(setting.key.startswith(setting.section + "."))
                self.assertTrue(setting.label and setting.help)
                parsed = settings.parse_value(setting, setting.default)
                self.assertEqual(
                    settings.serialize_value(setting, parsed),
                    settings.serialize_value(setting, setting.default),
                )

    def test_every_section_has_settings(self) -> None:
        for section in SECTIONS:
            self.assertTrue(settings.section_settings(section.key), section.key)


class ResolutionTest(unittest.TestCase):
    def test_default_then_row_then_env(self) -> None:
        self.assertEqual(Settings.resolve({}, {})["ocr.max_side"], 1600)
        self.assertEqual(Settings.resolve({}, {}).source("ocr.max_side"), SOURCE_DEFAULT)

        from_row = Settings.resolve({"ocr.max_side": "1920"}, {})
        self.assertEqual(from_row["ocr.max_side"], 1920)
        self.assertEqual(from_row.source("ocr.max_side"), SOURCE_DB)
        self.assertTrue(from_row.is_changed("ocr.max_side"))

        pinned = Settings.resolve({"ocr.max_side": "1920"}, {"LANG_JP_OCR_MAX_SIDE": "2560"})
        self.assertEqual(pinned["ocr.max_side"], 2560)
        self.assertEqual(pinned.source("ocr.max_side"), SOURCE_ENV)
        self.assertTrue(pinned.is_pinned("ocr.max_side"))

    def test_invalid_env_or_row_falls_through(self) -> None:
        snapshot = Settings.resolve(
            {"ocr.max_side": "1920", "ocr.min_confidence": "7"},
            {"LANG_JP_OCR_MAX_SIDE": "huge", "LANG_JP_OCR_MIN_CONFIDENCE": " "},
        )
        self.assertEqual(snapshot["ocr.max_side"], 1920)
        self.assertEqual(snapshot.source("ocr.max_side"), SOURCE_DB)
        self.assertEqual(snapshot["ocr.min_confidence"], 0.30)
        self.assertEqual(snapshot.source("ocr.min_confidence"), SOURCE_DEFAULT)

    def test_unknown_rows_are_ignored_and_unknown_keys_raise(self) -> None:
        snapshot = Settings.resolve({"gone.setting": "1"}, {})
        self.assertNotIn("gone.setting", snapshot.values)
        with self.assertRaises(SettingError):
            snapshot.get("gone.setting")

    def test_env_spellings_of_the_old_variables(self) -> None:
        snapshot = Settings.resolve(
            {},
            {
                "LANG_JP_EASYOCR_GPU": "cuda",
                "LANG_JP_AUTO_TRANSLATE_TERM_MEANINGS": "0",
                "LANG_JP_ENABLE_SOURCE_TRANSLATION": "1",
                "LANG_JP_OCR_BACKEND": "EasyOCR",
                "LANG_JP_EASYOCR_LANGS": "ja, en ,ja",
            },
        )
        self.assertEqual(snapshot["ocr.easyocr_gpu"], "gpu")
        self.assertIs(snapshot["translation.auto_term_meanings"], False)
        self.assertIs(snapshot["translation.translate_every_capture"], True)
        self.assertEqual(snapshot["ocr.backend"], "easyocr")
        self.assertEqual(snapshot["ocr.easyocr_languages"], "ja,en")

    def test_replace_validates(self) -> None:
        snapshot = Settings.resolve({}, {}).replace(ocr__max_side=0, capture__debug_log=True)
        self.assertEqual(snapshot["ocr.max_side"], 0)
        self.assertIs(snapshot["capture.debug_log"], True)
        with self.assertRaises(SettingError):
            snapshot.replace(ocr__max_side=-5)


class ValidationTest(unittest.TestCase):
    def parse(self, key: str, raw: object) -> object:
        return settings.parse_value(settings.setting_for(key), raw)

    def test_numbers(self) -> None:
        self.assertEqual(self.parse("ocr.min_confidence", "0,45"), 0.45)
        self.assertEqual(self.parse("ocr.max_side", "1920.0"), 1920)
        for key, raw in (
            ("ocr.max_side", "1920.5"),
            ("ocr.max_side", "-1"),
            ("ocr.max_side", "999999"),
            ("ocr.min_confidence", "1.5"),
            ("ocr.min_confidence", "nan"),
            ("ocr.min_confidence", "abc"),
            ("ocr.max_side", True),
        ):
            with self.subTest(key=key, raw=raw), self.assertRaises(SettingError):
                self.parse(key, raw)

    def test_booleans_choices_languages_and_hotkeys(self) -> None:
        self.assertIs(self.parse("capture.debug_log", "ON"), True)
        self.assertIs(self.parse("capture.debug_log", False), False)
        self.assertEqual(self.parse("ocr.backend", " MeikiOCR "), "meikiocr")
        self.assertEqual(self.parse("translation.meaning_language", "pt-BR"), "pt-BR")
        self.assertEqual(self.parse("hotkeys.overlay_toggle", "Ctrl + Alt + Q"), "Ctrl+Alt+Q")
        self.assertEqual(self.parse("hotkeys.manual_capture", ""), "")
        for key, raw in (
            ("capture.debug_log", "maybe"),
            ("ocr.backend", "tesseract"),
            ("translation.meaning_language", "portuguese!"),
            ("translation.meaning_language", ""),
            ("ocr.easyocr_languages", " , "),
            ("hotkeys.overlay_toggle", "Ctrl+"),
            ("hotkeys.overlay_toggle", "x" * 500),
        ):
            with self.subTest(key=key, raw=raw), self.assertRaises(SettingError):
                self.parse(key, raw)


class StorageTest(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:")
        self.connection.executescript(settings.SETTINGS_SCHEMA_SQL)

    def tearDown(self) -> None:
        self.connection.close()

    def test_only_non_default_values_are_stored(self) -> None:
        self.assertEqual(set_setting(self.connection, "ocr.max_side", "1920"), 1920)
        self.assertEqual(stored(self.connection), {"ocr.max_side": "1920"})
        set_setting(self.connection, "ocr.max_side", 1600)
        self.assertEqual(stored(self.connection), {})

    def test_load_reads_rows(self) -> None:
        set_setting(self.connection, "capture.debug_log", True)
        set_setting(self.connection, "ocr.min_confidence", 0.45)
        snapshot = load_settings(self.connection, {})
        self.assertIs(snapshot["capture.debug_log"], True)
        self.assertEqual(snapshot["ocr.min_confidence"], 0.45)
        self.assertEqual(snapshot.text("ocr.min_confidence"), "0.45")
        self.assertEqual(stored(self.connection)["capture.debug_log"], "1")

    def test_load_without_the_table_gives_defaults(self) -> None:
        empty = sqlite3.connect(":memory:")
        try:
            self.assertEqual(load_settings(empty, {})["ocr.max_side"], 1600)
        finally:
            empty.close()

    def test_set_settings_stores_nothing_when_one_value_is_invalid(self) -> None:
        with self.assertRaises(SettingError):
            set_settings(self.connection, {"ocr.max_side": "1920", "ocr.min_confidence": "9"})
        self.assertEqual(stored(self.connection), {})
        with self.assertRaises(SettingError):
            set_setting(self.connection, "no.such", "1")

    def test_same_value_does_not_touch_the_row(self) -> None:
        set_setting(self.connection, "ocr.max_side", 1920)
        self.connection.execute("UPDATE settings SET updated_at = 'before'")
        set_setting(self.connection, "ocr.max_side", "1920")
        self.assertEqual(
            self.connection.execute("SELECT updated_at FROM settings").fetchone()[0], "before"
        )

    def test_reset(self) -> None:
        set_settings(
            self.connection,
            {"ocr.max_side": 1920, "ocr.backend": "easyocr", "capture.debug_log": True},
        )
        reset_settings(self.connection, ["ocr.max_side"])
        self.assertEqual(set(stored(self.connection)), {"ocr.backend", "capture.debug_log"})
        reset_section(self.connection, "ocr")
        self.assertEqual(set(stored(self.connection)), {"capture.debug_log"})
        with self.assertRaises(SettingError):
            reset_section(self.connection, "nope")


class CatalogIntegrationTest(unittest.TestCase):
    """The table in a catalog database, the change log and the CLI commands."""

    def setUp(self) -> None:
        import tempfile

        import lang_jp

        self.lang_jp = lang_jp
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "catalog.sqlite3"
        connection = lang_jp.db_connect(self.db_path)
        try:
            connection.executescript(lang_jp.SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def run_cli(self, *argv: str) -> str:
        import contextlib
        import io

        args = self.lang_jp.build_parser().parse_args(["--db", str(self.db_path), *argv])
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            args.func(args)
        return output.getvalue()

    def test_runtime_schema_creates_the_table_and_logs_changes(self) -> None:
        import catalog_changes

        connection = self.lang_jp.db_connect(self.db_path)
        try:
            revision = catalog_changes.current_revision(connection)
            set_setting(connection, "ocr.max_side", 1920)
            connection.commit()
            status = catalog_changes.changes_since(
                connection, revision, catalog_changes.Watch.of("setting")
            )
            self.assertTrue(status.changed)
            unrelated = catalog_changes.changes_since(
                connection, revision, catalog_changes.Watch.of("term")
            )
            self.assertFalse(unrelated.changed)
        finally:
            connection.close()

    def test_cli_set_list_reset(self) -> None:
        self.assertIn("ocr.min_confidence = 0.45", self.run_cli("set-setting", "ocr.min_confidence", "0.45"))
        listing = self.run_cli("list-settings")
        self.assertIn("ocr.min_confidence\t0.45\t(db)", listing)
        self.assertIn("ocr.backend\tauto\t(default)", listing)
        self.assertIn("(default)", self.run_cli("reset-setting", "ocr.min_confidence"))
        self.assertIn("ocr.min_confidence\t0.3\t(default)", self.run_cli("list-settings"))
        with self.assertRaises(SystemExit):
            self.run_cli("set-setting", "ocr.min_confidence", "5")


if __name__ == "__main__":
    unittest.main()
