from __future__ import annotations

import argparse
import contextlib
import gzip
import io
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError

TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

import jmdict
import lang_jp
from lang_jp import SCHEMA_PATH, db_connect


JMDICT_FIXTURE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE JMdict [
<!ELEMENT JMdict (entry*)>
<!ENTITY adj-i "adjective (keiyoushi)">
<!ENTITY aux "auxiliary">
<!ENTITY aux-v "auxiliary verb">
<!ENTITY n "noun (common) (futsuumeishi)">
<!ENTITY n-suf "noun, used as a suffix">
<!ENTITY prt "particle">
<!ENTITY suf "suffix">
<!ENTITY v1 "Ichidan verb">
<!ENTITY v5k-s "Godan verb - Iku/Yuku special class">
<!ENTITY v5k "Godan verb with 'ku' ending">
<!ENTITY v5r "Godan verb with 'ru' ending">
<!ENTITY v5s "Godan verb with 'su' ending">
<!ENTITY vi "intransitive verb">
<!ENTITY vt "transitive verb">
<!ENTITY arch "archaic">
<!ENTITY pol "polite (teineigo) language">
<!ENTITY uk "word usually written using kana alone">
]>
<!-- JMdict created: 2026-10-20 -->
<JMdict>
<entry>
<ent_seq>1000010</ent_seq>
<k_ele><keb>優しい</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>やさしい</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&adj-i;</pos>
<gloss>tender</gloss>
<gloss>kind</gloss>
<gloss>Kind</gloss>
<gloss>gentle</gloss>
<gloss>graceful</gloss>
<gloss xml:lang="dut">zacht</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000020</ent_seq>
<k_ele><keb>易しい</keb></k_ele>
<r_ele><reb>やさしい</reb></r_ele>
<sense>
<pos>&adj-i;</pos>
<gloss>easy</gloss>
<gloss>simple</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000030</ent_seq>
<k_ele><keb>上</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>うえ</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<gloss>above</gloss>
<gloss>up</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000040</ent_seq>
<k_ele><keb>上</keb></k_ele>
<r_ele><reb>じょう</reb></r_ele>
<sense>
<pos>&n-suf;</pos>
<gloss>from the standpoint of</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000050</ent_seq>
<k_ele><keb>売れる</keb></k_ele>
<r_ele><reb>うれる</reb></r_ele>
<sense>
<pos>&v1;</pos>
<pos>&vi;</pos>
<gloss>to sell (well)</gloss>
</sense>
<sense>
<gloss>to be well known</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000060</ent_seq>
<k_ele><keb>行く</keb></k_ele>
<r_ele><reb>いく</reb></r_ele>
<r_ele><reb>ゆく</reb></r_ele>
<sense>
<pos>&v5k-s;</pos>
<misc>&arch;</misc>
<gloss>to die (archaic)</gloss>
</sense>
<sense>
<pos>&v5k-s;</pos>
<gloss>to go</gloss>
<gloss>to move (towards)</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000070</ent_seq>
<k_ele><keb>開く</keb></k_ele>
<r_ele><reb>ひらく</reb></r_ele>
<r_ele><reb>あく</reb></r_ele>
<sense>
<stagr>ひらく</stagr>
<pos>&v5k;</pos>
<gloss>to open (e.g. a book)</gloss>
</sense>
<sense>
<stagr>あく</stagr>
<pos>&v5k;</pos>
<gloss>to open (e.g. doors)</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000080</ent_seq>
<k_ele><keb>甲</keb></k_ele>
<k_ele><keb>乙</keb></k_ele>
<r_ele><reb>こう</reb><re_restr>甲</re_restr></r_ele>
<r_ele><reb>おつ</reb><re_restr>乙</re_restr></r_ele>
<sense>
<stagk>甲</stagk>
<pos>&n;</pos>
<gloss>first (in a sequence)</gloss>
</sense>
<sense>
<stagk>乙</stagk>
<pos>&n;</pos>
<gloss>second (in a sequence)</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000090</ent_seq>
<k_ele><keb>迚も</keb></k_ele>
<r_ele><reb>とても</reb></r_ele>
<sense>
<pos>&n;</pos>
<misc>&uk;</misc>
<gloss>very</gloss>
<gloss>awfully</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000100</ent_seq>
<r_ele><reb>アイテム</reb><re_pri>gai1</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<gloss>item</gloss>
<gloss>R&amp;D item</gloss>
</sense>
</entry>
<entry>
<ent_seq>1000110</ent_seq>
<k_ele><keb>１</keb></k_ele>
<r_ele><reb>いち</reb></r_ele>
<sense>
<pos>&n;</pos>
<gloss>one</gloss>
</sense>
</entry>
<entry>
<ent_seq>1524900</ent_seq>
<k_ele><keb>鱒</keb><ke_pri>news2</ke_pri></k_ele>
<r_ele><reb>ます</reb><re_pri>news2</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<misc>&uk;</misc>
<gloss>trout</gloss>
</sense>
</entry>
<entry>
<ent_seq>2210290</ent_seq>
<k_ele><keb>〼</keb></k_ele>
<r_ele><reb>ます</reb></r_ele>
<sense>
<pos>&aux-v;</pos>
<misc>&pol;</misc>
<gloss>expresses politeness towards the listener (or reader)</gloss>
</sense>
</entry>
<entry>
<ent_seq>1403120</ent_seq>
<k_ele><keb>増す</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>ます</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&v5s;</pos>
<pos>&vi;</pos>
<pos>&vt;</pos>
<gloss>to increase</gloss>
</sense>
</entry>
<entry>
<ent_seq>1465580</ent_seq>
<k_ele><keb>入る</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>いる</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&v5r;</pos>
<pos>&vi;</pos>
<gloss>to enter</gloss>
</sense>
<sense>
<pos>&suf;</pos>
<pos>&v5r;</pos>
<gloss>to do fully</gloss>
</sense>
</entry>
<entry>
<ent_seq>1546640</ent_seq>
<k_ele><keb>要る</keb><ke_pri>news2</ke_pri><ke_pri>spec1</ke_pri></k_ele>
<r_ele><reb>いる</reb><re_pri>news2</re_pri><re_pri>spec1</re_pri></r_ele>
<sense>
<pos>&v5r;</pos>
<pos>&vi;</pos>
<misc>&uk;</misc>
<gloss>to be needed</gloss>
</sense>
</entry>
<entry>
<ent_seq>1577980</ent_seq>
<k_ele><keb>居る</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>いる</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&v1;</pos>
<pos>&vi;</pos>
<misc>&uk;</misc>
<gloss>to be (of animate objects)</gloss>
<gloss>to exist</gloss>
</sense>
<sense>
<pos>&aux-v;</pos>
<pos>&v1;</pos>
<misc>&uk;</misc>
<gloss>to be ...-ing</gloss>
</sense>
</entry>
<entry>
<ent_seq>1322990</ent_seq>
<k_ele><keb>者</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>もの</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<gloss>person</gloss>
</sense>
</entry>
<entry>
<ent_seq>1502390</ent_seq>
<k_ele><keb>物</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>もの</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<gloss>thing</gloss>
<gloss>object</gloss>
</sense>
<sense>
<pos>&n;</pos>
<misc>&uk;</misc>
<gloss>used to emphasize emotion, judgment, etc.</gloss>
</sense>
</entry>
<entry>
<ent_seq>2780660</ent_seq>
<r_ele><reb>もの</reb><re_pri>spec1</re_pri></r_ele>
<sense>
<pos>&prt;</pos>
<gloss>indicates reason or excuse</gloss>
</sense>
</entry>
<entry>
<ent_seq>1476430</ent_seq>
<k_ele><keb>筈</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>はず</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&aux;</pos>
<misc>&uk;</misc>
<gloss>should (be)</gloss>
<gloss>bound (to be)</gloss>
</sense>
<sense>
<pos>&n;</pos>
<gloss>nock (of a bow)</gloss>
</sense>
</entry>
<entry>
<ent_seq>2461070</ent_seq>
<k_ele><keb>巴豆</keb></k_ele>
<r_ele><reb>はず</reb></r_ele>
<sense>
<pos>&n;</pos>
<misc>&uk;</misc>
<gloss>purging croton (Croton tiglium)</gloss>
</sense>
</entry>
<entry>
<ent_seq>1516925</ent_seq>
<k_ele><keb>方</keb><ke_pri>ichi1</ke_pri></k_ele>
<r_ele><reb>かた</reb><re_pri>ichi1</re_pri></r_ele>
<sense>
<pos>&n;</pos>
<gloss>direction</gloss>
<gloss>way</gloss>
</sense>
<sense>
<pos>&n-suf;</pos>
<gloss>method of</gloss>
<gloss>manner of</gloss>
</sense>
</entry>
</JMdict>
"""


def load_fixture(connection: sqlite3.Connection) -> int:
    return jmdict.load_jmdict(
        connection,
        io.StringIO(JMDICT_FIXTURE),
        source_name="fixture.xml",
    )


class JMdictLookupTest(unittest.TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(":memory:")
        load_fixture(connection)
        self.dictionary = jmdict.JMdict(connection)

    def tearDown(self) -> None:
        self.dictionary.close()

    def test_import_keeps_entity_names_and_metadata(self) -> None:
        metadata = self.dictionary.metadata()
        self.assertEqual(metadata["entries"], "23")
        self.assertEqual(metadata["created"], "2026-10-20")
        self.assertEqual(metadata["source"], "fixture.xml")
        entry = self.dictionary.load_entry(1000050)
        self.assertEqual(entry.senses[0].pos, ("v1", "vi"))
        # pos carries over to later senses that do not repeat it.
        self.assertEqual(entry.senses[1].pos, ("v1", "vi"))

    def test_kanji_lemma_with_exact_reading(self) -> None:
        match = self.dictionary.lookup("上", reading="ジョウ")
        self.assertIsNotNone(match)
        self.assertEqual((match.ent_seq, match.reading), (1000040, "じょう"))
        self.assertEqual(match.gloss_text(), "from the standpoint of")

        match = self.dictionary.lookup("上", reading="うえ")
        self.assertEqual(match.ent_seq, 1000030)

    def test_kanji_lemma_without_kana_reading_prefers_common_entry(self) -> None:
        match = self.dictionary.lookup("上", reading="上")
        self.assertEqual(match.ent_seq, 1000030)

    def test_conjugated_surface_reading_matches_by_prefix(self) -> None:
        match = self.dictionary.lookup("売れる", reading="ウレ")
        self.assertEqual(match.reading, "うれる")
        self.assertEqual(match.gloss_text(), "to sell (well)")

        match = self.dictionary.lookup("行く", reading="ユカ")
        self.assertEqual(match.reading, "ゆく")

    def test_archaic_sense_is_skipped(self) -> None:
        match = self.dictionary.lookup("行く", reading="イッ")
        self.assertEqual(match.reading, "いく")
        self.assertEqual(match.gloss_text(), "to go; to move (towards)")

    def test_sense_reading_restriction(self) -> None:
        self.assertEqual(
            self.dictionary.lookup("開く", reading="アイ").gloss_text(),
            "to open (e.g. doors)",
        )
        self.assertEqual(
            self.dictionary.lookup("開く", reading="ヒラ").gloss_text(),
            "to open (e.g. a book)",
        )

    def test_reading_and_sense_kanji_restrictions(self) -> None:
        match = self.dictionary.lookup("乙")
        self.assertEqual(match.reading, "おつ")
        self.assertEqual(match.gloss_text(), "second (in a sequence)")

    def test_kana_lemma_falls_back_to_reading_forms(self) -> None:
        match = self.dictionary.lookup("やさしい", reading="ヤサシク")
        self.assertIsNotNone(match)
        self.assertIsNone(match.kanji)
        # Both 優しい and 易しい read やさしい; the common entry wins.
        self.assertEqual(match.ent_seq, 1000010)

        self.assertEqual(self.dictionary.lookup("とても").gloss_text(), "very; awfully")
        self.assertEqual(self.dictionary.lookup("トテモ").ent_seq, 1000090)

    def test_gloss_text_dedupes_limits_and_ignores_other_languages(self) -> None:
        match = self.dictionary.lookup("優しい", reading="やさしい")
        self.assertEqual(match.glosses, ("tender", "kind", "Kind", "gentle", "graceful"))
        self.assertEqual(match.gloss_text(), "tender; kind; gentle")
        self.assertEqual(match.gloss_text(1), "tender")
        self.assertEqual(
            self.dictionary.lookup("アイテム").gloss_text(),
            "item; R&D item",
        )

    def test_unknown_or_empty_lemma_has_no_match(self) -> None:
        self.assertIsNone(self.dictionary.lookup("体用", reading="タイヨウ"))
        self.assertIsNone(self.dictionary.lookup("ティション"))
        self.assertIsNone(self.dictionary.lookup(""))
        self.assertIsNone(self.dictionary.lookup(None))
        # Lemmas without kana or kanji are OCR noise, even if NFKC would match.
        self.assertIsNone(self.dictionary.lookup("1", reading="イチ"))
        self.assertIsNone(self.dictionary.lookup("１"))

    def test_input_without_root_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            jmdict.load_jmdict(sqlite3.connect(":memory:"), io.StringIO("<x/>"))

    def test_catalog_pos_maps_to_jmdict_codes(self) -> None:
        self.assertIs(jmdict.jmdict_pos_preference("動詞 非自立可能"), jmdict.VERB_POS)
        self.assertIs(jmdict.jmdict_pos_preference("verb"), jmdict.VERB_POS)
        self.assertIs(jmdict.jmdict_pos_preference("助動詞 *"), jmdict.AUXILIARY_POS)
        self.assertIs(jmdict.jmdict_pos_preference("Noun"), jmdict.NOUN_POS)
        for unknown in (None, "", "補助記号 句点", "ui-label"):
            self.assertIsNone(jmdict.jmdict_pos_preference(unknown))
        self.assertTrue(jmdict.VERB_POS.matches(["v5r", "vi"]))
        # Bare "vs" marks a noun that takes suru, not a verb.
        self.assertFalse(jmdict.VERB_POS.matches(["n", "vs"]))
        self.assertTrue(jmdict.AUXILIARY_POS.matches(["aux-adj"]))

    def test_auxiliary_pos_picks_the_auxiliary_over_a_kana_noun(self) -> None:
        # Without a part of speech the kana-usual 鱒 wins.
        self.assertEqual(self.dictionary.lookup("ます", reading="マス").ent_seq, 1524900)

        match = self.dictionary.lookup("ます", reading="マス", pos="助動詞 *")
        self.assertEqual(match.ent_seq, 2210290)
        self.assertEqual(match.pos, ("aux-v",))
        self.assertEqual(
            match.gloss_text(),
            "expresses politeness towards the listener (or reader)",
        )

    def test_verb_pos_picks_居る(self) -> None:
        for pos in ("動詞 非自立可能", "動詞", "verb"):
            match = self.dictionary.lookup("いる", reading="イ", pos=pos)
            self.assertEqual(match.ent_seq, 1577980, pos)
            self.assertEqual(match.gloss_text(), "to be (of animate objects); to exist")

    def test_noun_pos_ranks_entries_but_keeps_the_first_sense(self) -> None:
        self.assertEqual(
            self.dictionary.lookup("もの", reading="モノ", pos="助詞 終助詞").ent_seq,
            2780660,
        )
        match = self.dictionary.lookup("もの", reading="モノ", pos="名詞 普通名詞")
        self.assertEqual(match.ent_seq, 1502390)
        self.assertEqual(match.gloss_text(), "thing; object")

        # JMdict files はず as an auxiliary; a noun tag must not pick "nock".
        match = self.dictionary.lookup("はず", reading="ハズ", pos="名詞 普通名詞")
        self.assertEqual(match.ent_seq, 1476430)
        self.assertEqual(match.gloss_text(), "should (be); bound (to be)")

    def test_pos_picks_the_matching_sense(self) -> None:
        self.assertEqual(
            self.dictionary.lookup("方", reading="カタ").gloss_text(),
            "direction; way",
        )
        self.assertEqual(
            self.dictionary.lookup("方", reading="カタ", pos="接尾辞 名詞的").gloss_text(),
            "method of; manner of",
        )

    def test_pos_without_a_matching_entry_is_ignored(self) -> None:
        for pos in ("副詞", "補助記号 句点"):
            match = self.dictionary.lookup("ます", reading="マス", pos=pos)
            self.assertEqual(match.ent_seq, 1524900, pos)
        self.assertEqual(
            self.dictionary.lookup("優しい", reading="やさしい", pos="助詞").ent_seq,
            1000010,
        )


class JMdictImportFileTest(unittest.TestCase):
    def test_import_from_gzip_and_open_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            temp_dir = Path(temp_name)
            source = temp_dir / "JMdict_e.gz"
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                stream.write(JMDICT_FIXTURE)
            db_path = temp_dir / "jmdict.sqlite3"

            self.assertEqual(jmdict.import_jmdict(source, db_path), 23)
            self.assertFalse((temp_dir / "jmdict.sqlite3.importing").exists())

            dictionary = jmdict.JMdict.open(db_path)
            self.assertIsNotNone(dictionary)
            with dictionary:
                self.assertEqual(dictionary.lookup("優しい").ent_seq, 1000010)
                with self.assertRaises(sqlite3.OperationalError):
                    dictionary.connection.execute("DELETE FROM senses")

            self.assertIsNone(jmdict.JMdict.open(temp_dir / "missing.sqlite3"))


class TermMeaningFromJMdictTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        temp_path = Path(self.temp_dir.name)
        self.jmdict_path = temp_path / "jmdict.sqlite3"
        connection = sqlite3.connect(self.jmdict_path)
        try:
            load_fixture(connection)
            connection.commit()
        finally:
            connection.close()

        self.db_path = temp_path / "lang_jp_test.sqlite3"
        connection = db_connect(self.db_path)
        try:
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        finally:
            connection.close()

        self.patches = [
            patch.object(lang_jp, "JMDICT_DB_PATH", self.jmdict_path),
            patch.object(lang_jp, "DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS", True),
            patch.object(lang_jp, "DEFAULT_TERM_TRANSLATION_TARGET", "pt"),
        ]
        for active_patch in self.patches:
            active_patch.start()

    def tearDown(self) -> None:
        for active_patch in reversed(self.patches):
            active_patch.stop()
        self.temp_dir.cleanup()

    def test_jmdict_gloss_is_translated_from_english(self) -> None:
        with patch.object(
            lang_jp,
            "google_gtx_translate_text",
            return_value="terno, gentil, gentil",
        ) as translate_mock:
            meaning = lang_jp.auto_translate_term_meaning_pt("やさしい", reading="ヤサシク")
        self.assertEqual(meaning, "terno; gentil")
        # One comma-separated list, so the glosses give each other context.
        translate_mock.assert_called_once_with(
            "tender, kind, gentle",
            source_language="en",
            target_language="pt",
        )

    def test_translation_split_keeps_commas_inside_parentheses(self) -> None:
        self.assertEqual(
            lang_jp.split_top_level_list("cartão (informações, arquivo, etc.), item; x"),
            ["cartão (informações, arquivo, etc.)", " item", " x"],
        )
        with patch.object(
            lang_jp,
            "google_gtx_translate_text",
            return_value="item, item de P&D",
        ):
            self.assertEqual(
                lang_jp.auto_translate_term_meaning_pt("アイテム"),
                "item; item de P&D",
            )

    def test_unchanged_translation_keeps_the_gloss(self) -> None:
        with patch.object(lang_jp, "google_gtx_translate_text", return_value=None):
            meaning = lang_jp.auto_translate_term_meaning_pt("アイテム")
        self.assertEqual(meaning, "item; R&D item")

    def test_english_target_skips_translation(self) -> None:
        with (
            patch.object(lang_jp, "DEFAULT_TERM_TRANSLATION_TARGET", "en"),
            patch.object(lang_jp, "google_gtx_translate_text") as translate_mock,
        ):
            meaning = lang_jp.auto_translate_term_meaning_pt("売れる", reading="ウレ")
        self.assertEqual(meaning, "to sell (well)")
        translate_mock.assert_not_called()

    def test_part_of_speech_picks_the_dictionary_entry(self) -> None:
        with patch.object(lang_jp, "DEFAULT_TERM_TRANSLATION_TARGET", "en"):
            self.assertEqual(
                lang_jp.auto_translate_term_meaning_pt("ます", reading="マス", pos="助動詞 *"),
                "expresses politeness towards the listener (or reader)",
            )
            self.assertEqual(
                lang_jp.auto_translate_term_meaning_pt("ます", reading="マス"),
                "trout",
            )

    def test_falls_back_to_lemma_translation_without_jmdict_match(self) -> None:
        with patch.object(
            lang_jp,
            "google_gtx_translate_text",
            return_value="uso corporal",
        ) as translate_mock:
            meaning = lang_jp.auto_translate_term_meaning_pt("体用", reading="タイヨウ")
        self.assertEqual(meaning, "uso corporal")
        translate_mock.assert_called_once_with(
            "体用",
            source_language="ja",
            target_language="pt",
        )

    def test_falls_back_when_dictionary_is_missing(self) -> None:
        with (
            patch.object(lang_jp, "JMDICT_DB_PATH", Path(self.temp_dir.name) / "missing.sqlite3"),
            patch.object(lang_jp, "google_gtx_translate_text", return_value="x") as translate_mock,
        ):
            lang_jp.auto_translate_term_meaning_pt("やさしい")
        translate_mock.assert_called_once_with(
            "やさしい",
            source_language="ja",
            target_language="pt",
        )

    def test_network_error_leaves_meaning_empty(self) -> None:
        with patch.object(
            lang_jp,
            "google_gtx_translate_text",
            side_effect=URLError("offline"),
        ):
            self.assertIsNone(lang_jp.auto_translate_term_meaning_pt("優しい"))

    def test_disabled_auto_translate_does_nothing(self) -> None:
        with (
            patch.object(lang_jp, "DEFAULT_AUTO_TRANSLATE_TERM_MEANINGS", False),
            patch.object(lang_jp, "google_gtx_translate_text") as translate_mock,
        ):
            self.assertIsNone(lang_jp.auto_translate_term_meaning_pt("優しい"))
        translate_mock.assert_not_called()

    def test_new_term_gets_dictionary_meaning(self) -> None:
        connection = db_connect(self.db_path)
        try:
            with patch.object(
                lang_jp,
                "google_gtx_translate_text",
                return_value="vender (bem)",
            ):
                term_id = lang_jp.upsert_term_record(
                    connection,
                    lemma="売れる",
                    reading="ウレ",
                    meaning_pt=None,
                    pos="動詞 一般",
                    notes=None,
                )
            row = connection.execute(
                "SELECT meaning_pt FROM terms WHERE id = ?",
                (term_id,),
            ).fetchone()
        finally:
            connection.close()
        self.assertEqual(row["meaning_pt"], "vender (bem)")

    def test_new_kana_grammar_term_uses_its_part_of_speech(self) -> None:
        connection = db_connect(self.db_path)
        try:
            with patch.object(
                lang_jp,
                "google_gtx_translate_text",
                return_value="estar, existir",
            ) as translate_mock:
                term_id = lang_jp.upsert_term_record(
                    connection,
                    lemma="いる",
                    reading="イ",
                    meaning_pt=None,
                    pos="動詞 非自立可能",
                    notes=None,
                )
            row = connection.execute(
                "SELECT meaning_pt FROM terms WHERE id = ?",
                (term_id,),
            ).fetchone()
        finally:
            connection.close()
        translate_mock.assert_called_once_with(
            "to be (of animate objects), to exist",
            source_language="en",
            target_language="pt",
        )
        self.assertEqual(row["meaning_pt"], "estar; existir")

    def run_report(self, **overrides: object) -> str:
        args = argparse.Namespace(
            db=self.db_path,
            jmdict_db=self.jmdict_path,
            limit=50,
            missing_only=False,
            no_translate=False,
        )
        for key, value in overrides.items():
            setattr(args, key, value)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            lang_jp.jmdict_meaning_report(args)
        return output.getvalue()

    def insert_terms(self) -> None:
        connection = db_connect(self.db_path)
        try:
            connection.executemany(
                """
                INSERT INTO terms (canonical_key, lemma, reading, meaning_pt)
                VALUES (?, ?, ?, ?)
                """,
                [
                    ("やさしい::ヤサシク", "やさしい", "ヤサシク", "tipo"),
                    ("売れる::ウレ", "売れる", "ウレ", "vender (bem)"),
                    ("行く::イク", "行く", "イク", None),
                    ("体用::タイヨウ", "体用", "タイヨウ", "Para uso corporal"),
                ],
            )
            connection.commit()
        finally:
            connection.close()

    def test_report_lists_differences_without_writing(self) -> None:
        self.insert_terms()
        before = self.db_path.read_bytes()
        translations = {
            "tender, kind, gentle": "macio, gentil, gentil",
            "to sell (well)": "vender (bem)",
            "to go, to move (towards)": "ir, mover (em direção a)",
        }
        with patch.object(
            lang_jp,
            "google_gtx_translate_text",
            side_effect=lambda text, **_: translations[text],
        ):
            output = self.run_report()

        lines = output.splitlines()
        self.assertTrue(any("やさしい" in line and "macio; gentil" in line for line in lines))
        self.assertTrue(any("行く" in line and "ir; mover" in line for line in lines))
        # Same meaning after normalisation, and no JMdict match: not listed.
        self.assertNotIn("売れる", output)
        self.assertNotIn("体用", output)
        self.assertIn("Scanned 4 terms, 3 with a JMdict match, listed 2", output)
        self.assertEqual(self.db_path.read_bytes(), before)

    def test_report_missing_only_and_no_translate(self) -> None:
        self.insert_terms()
        with patch.object(lang_jp, "google_gtx_translate_text") as translate_mock:
            output = self.run_report(missing_only=True, no_translate=True)
        translate_mock.assert_not_called()
        self.assertIn("to go; to move (towards)", output)
        self.assertNotIn("やさしい", output)
        self.assertIn("listed 1", output)

    def test_report_uses_the_stored_part_of_speech(self) -> None:
        connection = db_connect(self.db_path)
        try:
            connection.execute(
                """
                INSERT INTO terms (canonical_key, lemma, reading, meaning_pt, pos)
                VALUES ('ます::マス', 'ます', 'マス', 'truta', '助動詞 *')
                """
            )
            connection.commit()
        finally:
            connection.close()
        output = self.run_report(no_translate=True)
        self.assertIn("expresses politeness", output)
        self.assertNotIn("trout", output)

    def test_report_requires_imported_dictionary(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_report(jmdict_db=Path(self.temp_dir.name) / "missing.sqlite3")

    def test_read_only_connection_rejects_writes(self) -> None:
        self.insert_terms()
        connection = lang_jp.db_connect_read_only(self.db_path)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("UPDATE terms SET meaning_pt = 'x'")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
