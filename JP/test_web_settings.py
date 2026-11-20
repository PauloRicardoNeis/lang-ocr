from __future__ import annotations

import http.client
import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlencode, urlparse

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import lang_jp_web
import settings
import web_settings
from lang_jp import SCHEMA_PATH, db_connect
from settings import REGISTRY, SECTIONS, SettingError, Settings, load_settings, set_setting


class SettingsPageTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "catalog.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            connection.commit()
        finally:
            connection.close()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def snapshot(self) -> Settings:
        connection = db_connect(self.db_path)
        try:
            return load_settings(connection)
        finally:
            connection.close()

    def stored(self) -> dict[str, str]:
        connection = db_connect(self.db_path)
        try:
            return dict(connection.execute("SELECT key, value FROM settings").fetchall())
        finally:
            connection.close()

    def apply(self, fields: dict[str, object]) -> web_settings.SettingsFormResult:
        """Save a form the way the browser sends it: every value is a list of strings."""
        form = {
            name: [str(item) for item in value] if isinstance(value, list) else [str(value)]
            for name, value in fields.items()
        }
        connection = db_connect(self.db_path)
        try:
            result = web_settings.apply_settings_form(connection, form)
            connection.commit()
            return result
        finally:
            connection.close()


class SettingsViewTest(SettingsPageTestCase):
    def test_page_shows_every_section_and_setting(self) -> None:
        page = lang_jp_web.settings_page(self.db_path, {})
        self.assertIn("<title>Ajustes - Lang Catalog</title>", page)
        self.assertIn('<a class="is-active" href="/settings">Ajustes</a>', page)
        for section in SECTIONS:
            self.assertIn(f'id="{section.key}"', page)
            self.assertIn(f'href="#{section.key}"', page)
            self.assertIn(f'name="section" value="{section.key}"', page)
        for setting in REGISTRY:
            with self.subTest(key=setting.key):
                self.assertIn(f'name="{setting.key}"', page)
                self.assertIn(f'name="was:{setting.key}"', page)
        self.assertIn("Tudo no padrao", page)

    def test_fields_are_static_and_summaries_are_live(self) -> None:
        connection = db_connect(self.db_path)
        try:
            set_setting(connection, "ocr.max_side", 1920)
            set_setting(connection, "capture.debug_log", True)
            connection.commit()
        finally:
            connection.close()
        view = lang_jp_web.settings_view(self.db_path, {})
        self.assertEqual(view.watch.encode(), "setting")
        self.assertEqual(
            set(view.regions), {"settings-index", *(f"settings-{section.key}" for section in SECTIONS)}
        )
        for markup in view.regions.values():
            for field in ("<input", "<select", "<form", "<button"):
                self.assertNotIn(field, markup)
        self.assertIn("Lado maximo da imagem: 1920", view.regions["settings-ocr"])
        self.assertIn("Log detalhado: ligado", view.regions["settings-capture"])
        self.assertIn("Tudo no padrao", view.regions["settings-hotkeys"])
        self.assertIn("2 ajustes fora do padrao", view.regions["settings-index"])
        # The form shows the stored value; a changed advanced setting opens its group.
        self.assertIn('name="ocr.max_side" value="1920"', view.body)
        self.assertNotIn("<details class=\"setting-advanced\" open>", view.body)
        connection = db_connect(self.db_path)
        try:
            set_setting(connection, "ocr.max_regions", 5)
            connection.commit()
        finally:
            connection.close()
        self.assertIn(
            '<details class="setting-advanced" open>', lang_jp_web.settings_view(self.db_path, {}).body
        )

    def test_controls_follow_the_kind_of_setting(self) -> None:
        body = lang_jp_web.settings_view(self.db_path, {}).body
        self.assertIn('<option value="auto" selected>Automatico</option>', body)
        self.assertIn('<input type="number" id="setting-ocr-max_side" name="ocr.max_side" value="1600" min="0" max="8192" step="1" required>', body)
        self.assertIn('name="ocr.min_confidence" value="0.3" min="0" max="1" step="any" required', body)
        # A switch posts "0" when it is off and "0" then "1" when it is on.
        self.assertIn(
            '<input type="hidden" name="capture.auto_promote_terms" value="0">'
            '<label class="switch"><input type="checkbox" id="setting-capture-auto_promote_terms"'
            ' name="capture.auto_promote_terms" value="1" checked>',
            body,
        )
        self.assertIn('name="translation.meaning_language" value="pt" autocomplete="off" spellcheck="false" list="setting-language-codes" required', body)
        self.assertIn('<datalist id="setting-language-codes">', body)
        self.assertIn("recarrega os motores", body)
        self.assertIn("reinicia o tray", body)
        self.assertIn("Padrao: desligado.", body)
        self.assertIn("Padrao: vazio.", body)

    def test_pinned_setting_is_shown_locked(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_OCR_MIN_CONFIDENCE": "0.45"}):
            body = lang_jp_web.settings_view(self.db_path, {}).body
        self.assertIn("Fixado por <code>LANG_JP_OCR_MIN_CONFIDENCE</code> = <code>0.45</code>", body)
        self.assertNotIn('name="ocr.min_confidence"', body)
        self.assertIn('name="ocr.max_side"', body)

    def test_notice_lands_in_the_section_that_was_saved(self) -> None:
        params = {"notice": ["OCR salvo."], "section": ["ocr"]}
        body = lang_jp_web.settings_view(self.db_path, params).body
        ocr_panel = body.split('id="ocr"', 1)[1].split("</section>", 1)[0]
        self.assertIn('<div class="notice-banner">OCR salvo.</div>', ocr_panel)
        self.assertEqual(body.count("notice-banner"), 1)
        # Without a known section the notice is at the top of the page.
        body = lang_jp_web.settings_view(self.db_path, {"notice": ["Ops"], "level": ["error"]}).body
        self.assertIn('<div class="notice-banner is-error">Ops</div>', body.split('id="ocr"', 1)[0])

    def test_capture_page_links_to_the_ocr_settings(self) -> None:
        self.assertIn('href="/settings#ocr"', lang_jp_web.capture_page(self.db_path, {}))


class ApplySettingsFormTest(SettingsPageTestCase):
    def test_only_fields_the_user_changed_are_written(self) -> None:
        # The form was rendered with the defaults; meanwhile the tray set the OCR size.
        connection = db_connect(self.db_path)
        try:
            set_setting(connection, "ocr.max_side", 2560)
            connection.commit()
        finally:
            connection.close()
        result = self.apply(
            {
                "section": "ocr",
                "ocr.max_side": "1600",
                "was:ocr.max_side": "1600",
                "ocr.min_confidence": "0,45",
                "was:ocr.min_confidence": "0.3",
                "ocr.backend": "auto",
                "was:ocr.backend": "auto",
            }
        )
        self.assertEqual(self.stored(), {"ocr.max_side": "2560", "ocr.min_confidence": "0.45"})
        self.assertEqual(result.message, "OCR salvo: Confianca minima.")
        self.assertEqual(result.values["ocr.max_side"], "2560")
        self.assertEqual(self.apply({"section": "ocr", "ocr.backend": "auto", "was:ocr.backend": "auto"}).message, "OCR: nada mudou.")

    def test_a_client_without_rendered_values_sets_what_it_sends(self) -> None:
        self.apply({"section": "ocr", "ocr.max_side": "1920"})
        self.assertEqual(self.stored(), {"ocr.max_side": "1920"})

    def test_switches_and_blank_hotkeys(self) -> None:
        self.apply(
            {
                "section": "capture",
                "capture.auto_promote_terms": "0",
                "was:capture.auto_promote_terms": "1",
                "capture.debug_log": ["0", "1"],
                "was:capture.debug_log": "0",
            }
        )
        snapshot = self.snapshot()
        self.assertIs(snapshot["capture.auto_promote_terms"], False)
        self.assertIs(snapshot["capture.debug_log"], True)

        self.apply({"section": "hotkeys", "hotkeys.overlay_mode": "", "was:hotkeys.overlay_mode": "Ctrl+Alt+W"})
        self.assertEqual(self.snapshot()["hotkeys.overlay_mode"], "")
        self.assertEqual(self.stored()["hotkeys.overlay_mode"], "")

    def test_an_invalid_value_stores_nothing(self) -> None:
        with self.assertRaises(SettingError):
            self.apply(
                {
                    "section": "ocr",
                    "ocr.max_side": "1920",
                    "was:ocr.max_side": "1600",
                    "ocr.min_confidence": "7",
                    "was:ocr.min_confidence": "0.3",
                }
            )
        self.assertEqual(self.stored(), {})
        with self.assertRaises(SettingError):
            self.apply({"section": "nope"})

    def test_keys_of_other_sections_and_pinned_keys_are_ignored(self) -> None:
        with patch.dict(os.environ, {"LANG_JP_OCR_MAX_SIDE": "2560"}):
            self.apply(
                {
                    "section": "ocr",
                    "ocr.max_side": "800",
                    "capture.debug_log": "1",
                    "ocr.max_regions": "5",
                }
            )
        self.assertEqual(self.stored(), {"ocr.max_regions": "5"})

    def test_reset_puts_the_section_back(self) -> None:
        self.apply({"section": "ocr", "ocr.max_side": "1920", "ocr.backend": "easyocr"})
        self.apply({"section": "capture", "capture.debug_log": "1"})
        result = self.apply({"section": "ocr", "op": "reset", "ocr.max_side": "999"})
        self.assertEqual(self.stored(), {"capture.debug_log": "1"})
        self.assertEqual(result.message, "OCR: ajustes de volta ao padrao.")


class SettingsHttpTest(SettingsPageTestCase):
    def setUp(self) -> None:
        super().setUp()
        handler = type("TestHandler", (lang_jp_web.CatalogRequestHandler,), {"db_path": self.db_path})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def request(self, method: str, path: str, fields=None, *, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=30)
        try:
            all_headers = dict(headers or {})
            body = None
            if fields is not None:
                body = urlencode(fields, doseq=True)
                all_headers["Content-Type"] = "application/x-www-form-urlencoded"
            connection.request(method, path, body=body, headers=all_headers)
            response = connection.getresponse()
            return response.status, response.getheader("Location") or "", response.read().decode("utf-8")
        finally:
            connection.close()

    def test_save_redirects_to_the_section_with_a_notice(self) -> None:
        status, location, _ = self.request(
            "POST",
            "/actions/settings",
            {"section": "ocr", "ocr.min_confidence": "0.45", "was:ocr.min_confidence": "0.3"},
        )
        self.assertEqual(status, 303)
        parsed = urlparse(location)
        self.assertEqual((parsed.path, parsed.fragment), ("/settings", "ocr"))
        query = parse_qs(parsed.query)
        self.assertEqual(query["section"], ["ocr"])
        self.assertEqual(query["level"], ["info"])
        self.assertEqual(query["notice"], ["OCR salvo: Confianca minima."])
        self.assertEqual(self.stored(), {"ocr.min_confidence": "0.45"})

        status, _, page = self.request("GET", location.split("#", 1)[0])
        self.assertEqual(status, 200)
        self.assertIn("OCR salvo: Confianca minima.", page)
        self.assertIn("Confianca minima: 0.45", page)

    def test_out_of_range_value_comes_back_as_an_error(self) -> None:
        status, location, _ = self.request(
            "POST", "/actions/settings", {"section": "ocr", "ocr.max_side": "999999"}
        )
        self.assertEqual(status, 303)
        self.assertIn("level=error", location)
        self.assertTrue(location.endswith("#ocr"))
        self.assertEqual(self.stored(), {})

        status, location, _ = self.request("POST", "/actions/settings", {"section": "<bad>"})
        self.assertIn("level=error", location)
        self.assertNotIn("#", location)

    def test_blank_fields_reach_the_action(self) -> None:
        self.request(
            "POST",
            "/actions/settings",
            {"section": "hotkeys", "hotkeys.overlay_mode": "", "was:hotkeys.overlay_mode": "Ctrl+Alt+W"},
        )
        self.assertEqual(self.stored(), {"hotkeys.overlay_mode": ""})

    def test_json_clients_get_json(self) -> None:
        status, _, body = self.request(
            "POST",
            "/actions/settings",
            {"section": "overlay", "overlay.mode": "words"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["section"], "overlay")
        self.assertEqual(payload["settings"]["overlay.mode"], "words")

        status, _, body = self.request(
            "POST",
            "/actions/settings",
            {"section": "overlay", "overlay.mode": "sideways"},
            headers={"Accept": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("overlay.mode", json.loads(body)["message"])

    def test_api_lists_every_setting_with_its_source(self) -> None:
        self.request("POST", "/actions/settings", {"section": "ocr", "ocr.max_side": "1920"})
        status, _, body = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual([section["key"] for section in payload["sections"]], [s.key for s in SECTIONS])
        by_key = {entry["key"]: entry for entry in payload["settings"]}
        self.assertEqual(set(by_key), {setting.key for setting in REGISTRY})
        self.assertEqual(by_key["ocr.max_side"]["value"], 1920)
        self.assertEqual(by_key["ocr.max_side"]["default"], 1600)
        self.assertEqual(by_key["ocr.max_side"]["source"], settings.SOURCE_DB)
        self.assertEqual(by_key["ocr.backend"]["source"], settings.SOURCE_DEFAULT)
        self.assertEqual(by_key["ocr.backend"]["choices"][0], {"value": "auto", "label": "Automatico"})
        self.assertEqual(by_key["ocr.min_confidence"]["env"], "LANG_JP_OCR_MIN_CONFIDENCE")

    def test_live_updater_sees_a_change_made_elsewhere(self) -> None:
        start = lang_jp_web.catalog_revision_at(self.db_path)
        connection = db_connect(self.db_path)
        try:
            set_setting(connection, "capture.interval_seconds", 15)
            connection.commit()
        finally:
            connection.close()
        status = lang_jp_web.live_changes_payload(self.db_path, {"since": [str(start)], "watch": ["setting"]})
        self.assertTrue(status["changed"])
        _, _, body = self.request(
            "GET", "/settings", headers={"Accept": lang_jp_web.FRAGMENTS_MEDIA_TYPE}
        )
        regions = json.loads(body)["regions"]
        self.assertIn("Intervalo entre capturas: 15", regions["settings-capture"]["html"])


if __name__ == "__main__":
    unittest.main()
