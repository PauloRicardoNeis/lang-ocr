"""Local JMdict lookup for term meanings.

JMdict_e is published by the Electronic Dictionary Research and Development Group
(EDRDG) under CC BY-SA 4.0. It is imported once from the XML release into a
separate SQLite file (``data/jmdict.sqlite3`` by default) and then queried by
(kanji form, reading), falling back to kana-only matches.

This module only uses the standard library, so the importer can run with
``python -I`` against a downloaded file:

    python -I jmdict.py import data\\jmdict_src\\JMdict_e.gz
    python jmdict.py lookup 優しい --reading ヤサシク
    python jmdict.py lookup ます --pos 助動詞
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import sqlite3
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterable, Iterator


ROOT = Path(__file__).resolve().parent
DEFAULT_JMDICT_DB_PATH = ROOT / "data" / "jmdict.sqlite3"
JMDICT_DOWNLOAD_URL = "https://www.edrdg.org/pub/Nihongo/JMdict_e.gz"
DEFAULT_GLOSS_LIMIT = 3
IMPORT_BATCH_SIZE = 5000

# Priority tags that JMdict itself uses to mark an entry as "common".
COMMON_PRIORITY_TAGS = frozenset({"news1", "ichi1", "spec1", "spec2", "gai1"})
DISFAVORED_SENSE_MISC = frozenset({"arch", "obs"})
XML_PREDEFINED_ENTITIES = frozenset({"amp", "lt", "gt", "quot", "apos"})
XML_LANG_ATTRIBUTE = "{http://www.w3.org/XML/1998/namespace}lang"
ENTITY_REFERENCE_RE = re.compile(r"&([A-Za-z_][\w.-]*);")
CREATED_COMMENT_RE = re.compile(r"<!--\s*JMdict created:\s*([^\s-][^>]*?)\s*-->")
TERM_POS_SEPARATOR_RE = re.compile(r"[\s,、]+")

JMDICT_SCHEMA_SQL = """
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE kanji_forms (
    ent_seq INTEGER NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    lookup_key TEXT NOT NULL,
    info TEXT NOT NULL,
    priority TEXT NOT NULL,
    PRIMARY KEY (ent_seq, position)
);

CREATE TABLE reading_forms (
    ent_seq INTEGER NOT NULL,
    position INTEGER NOT NULL,
    text TEXT NOT NULL,
    lookup_key TEXT NOT NULL,
    no_kanji INTEGER NOT NULL,
    restrictions TEXT NOT NULL,
    info TEXT NOT NULL,
    priority TEXT NOT NULL,
    PRIMARY KEY (ent_seq, position)
);

CREATE TABLE senses (
    ent_seq INTEGER NOT NULL,
    position INTEGER NOT NULL,
    pos TEXT NOT NULL,
    misc TEXT NOT NULL,
    stagk TEXT NOT NULL,
    stagr TEXT NOT NULL,
    glosses TEXT NOT NULL,
    PRIMARY KEY (ent_seq, position)
);
"""

JMDICT_INDEX_SQL = """
CREATE INDEX idx_kanji_forms_lookup_key ON kanji_forms(lookup_key);
CREATE INDEX idx_reading_forms_lookup_key ON reading_forms(lookup_key);
"""


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(unicodedata.normalize("NFKC", value).split())


def to_hiragana(value: str) -> str:
    chars = []
    for char in value:
        codepoint = ord(char)
        if 0x30A1 <= codepoint <= 0x30F6 or codepoint in (0x30FD, 0x30FE):
            chars.append(chr(codepoint - 0x60))
        else:
            chars.append(char)
    return "".join(chars)


def is_kana_text(value: str | None) -> bool:
    if not value:
        return False
    for char in value:
        codepoint = ord(char)
        if not (0x3041 <= codepoint <= 0x309F or 0x30A0 <= codepoint <= 0x30FF):
            return False
    return True


def is_japanese_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3041 <= codepoint <= 0x30FF  # hiragana, katakana, ー
        or 0x3005 <= codepoint <= 0x3007  # 々 〆 〇
        or 0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
        or 0x20000 <= codepoint <= 0x3134F
    )


def kanji_lookup_key(value: str | None) -> str:
    return normalize_text(value)


def reading_lookup_key(value: str | None) -> str:
    return to_hiragana(normalize_text(value))


def common_prefix_length(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def is_common(priority: Iterable[str]) -> bool:
    return any(tag in COMMON_PRIORITY_TAGS for tag in priority)


def unique_glosses(glosses: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for gloss in glosses:
        normalized = " ".join(str(gloss).split())
        key = normalized.casefold()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------


def _replace_entity_reference(match: re.Match[str]) -> str:
    name = match.group(1)
    if name in XML_PREDEFINED_ENTITIES:
        return match.group(0)
    return name


def iter_jmdict_body_lines(lines: Iterable[str], meta: dict[str, str]) -> Iterator[str]:
    """Yield the XML body from the ``<JMdict>`` root on.

    The internal DTD is skipped and every DTD entity reference is replaced by its
    bare name, so ``<pos>&v5r;</pos>`` is parsed as ``v5r`` instead of the long
    description. Skipping the DTD also means no entity expansion happens at all.
    """
    in_body = False
    for line in lines:
        if "created" not in meta and "JMdict created" in line:
            created = CREATED_COMMENT_RE.search(line)
            if created:
                meta["created"] = created.group(1)
        if not in_body:
            stripped = line.lstrip()
            if not stripped.startswith("<JMdict>"):
                continue
            in_body = True
            line = stripped
        yield ENTITY_REFERENCE_RE.sub(_replace_entity_reference, line)


def _texts(element: ET.Element, tag: str) -> list[str]:
    return [normalize_text(child.text) for child in element.findall(tag) if child.text]


def _json(values: Iterable[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def parse_entry(
    entry: ET.Element,
) -> tuple[int, list[tuple], list[tuple], list[tuple]]:
    ent_seq = int(normalize_text(entry.findtext("ent_seq")))

    kanji_rows = []
    for position, k_ele in enumerate(entry.findall("k_ele")):
        keb = normalize_text(k_ele.findtext("keb"))
        if not keb:
            continue
        kanji_rows.append(
            (
                ent_seq,
                position,
                keb,
                kanji_lookup_key(keb),
                _json(_texts(k_ele, "ke_inf")),
                _json(_texts(k_ele, "ke_pri")),
            )
        )

    reading_rows = []
    for position, r_ele in enumerate(entry.findall("r_ele")):
        reb = normalize_text(r_ele.findtext("reb"))
        if not reb:
            continue
        reading_rows.append(
            (
                ent_seq,
                position,
                reb,
                reading_lookup_key(reb),
                1 if r_ele.find("re_nokanji") is not None else 0,
                _json(_texts(r_ele, "re_restr")),
                _json(_texts(r_ele, "re_inf")),
                _json(_texts(r_ele, "re_pri")),
            )
        )

    sense_rows = []
    inherited_pos: list[str] = []
    for position, sense in enumerate(entry.findall("sense")):
        pos = _texts(sense, "pos") or inherited_pos
        inherited_pos = pos
        glosses = [
            normalize_text(gloss.text)
            for gloss in sense.findall("gloss")
            if gloss.text and gloss.get(XML_LANG_ATTRIBUTE, "eng") == "eng"
        ]
        if not glosses:
            continue
        sense_rows.append(
            (
                ent_seq,
                position,
                _json(pos),
                _json(_texts(sense, "misc")),
                _json(_texts(sense, "stagk")),
                _json(_texts(sense, "stagr")),
                _json(glosses),
            )
        )
    return ent_seq, kanji_rows, reading_rows, sense_rows


def iter_jmdict_entries(lines: Iterable[str], meta: dict[str, str]) -> Iterator[ET.Element]:
    parser = ET.XMLPullParser(events=("start", "end"))
    root: ET.Element | None = None
    for line in iter_jmdict_body_lines(lines, meta):
        parser.feed(line)
        for event, element in parser.read_events():
            if root is None:
                root = element
            if event == "end" and element.tag == "entry":
                yield element
                del root[:]
    if root is None:
        raise ValueError("No <JMdict> root element was found in the input.")
    parser.close()


def load_jmdict(
    connection: sqlite3.Connection,
    lines: Iterable[str],
    *,
    source_name: str = "",
) -> int:
    """Create the JMdict tables on ``connection`` and fill them from XML lines."""
    connection.executescript(JMDICT_SCHEMA_SQL)
    meta: dict[str, str] = {}
    kanji_batch: list[tuple] = []
    reading_batch: list[tuple] = []
    sense_batch: list[tuple] = []
    entry_count = 0

    def flush() -> None:
        connection.executemany(
            "INSERT INTO kanji_forms VALUES (?, ?, ?, ?, ?, ?)",
            kanji_batch,
        )
        connection.executemany(
            "INSERT INTO reading_forms VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            reading_batch,
        )
        connection.executemany(
            "INSERT INTO senses VALUES (?, ?, ?, ?, ?, ?, ?)",
            sense_batch,
        )
        kanji_batch.clear()
        reading_batch.clear()
        sense_batch.clear()

    for element in iter_jmdict_entries(lines, meta):
        _, kanji_rows, reading_rows, sense_rows = parse_entry(element)
        kanji_batch.extend(kanji_rows)
        reading_batch.extend(reading_rows)
        sense_batch.extend(sense_rows)
        entry_count += 1
        if entry_count % IMPORT_BATCH_SIZE == 0:
            flush()
    flush()

    if entry_count == 0:
        raise ValueError("The JMdict input did not contain any <entry> elements.")

    connection.executescript(JMDICT_INDEX_SQL)
    meta.update(
        {
            "source": source_name,
            "entries": str(entry_count),
            "imported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
    )
    connection.executemany(
        "INSERT INTO meta (key, value) VALUES (?, ?)",
        sorted(meta.items()),
    )
    return entry_count


def open_jmdict_text(path: Path):
    if path.suffix.lower() == ".gz":
        return gzip.open(path, "rt", encoding="utf-8")
    return path.open("r", encoding="utf-8")


def import_jmdict(source_path: Path, db_path: Path = DEFAULT_JMDICT_DB_PATH) -> int:
    """Import a JMdict XML file (optionally .gz) into ``db_path``.

    The data is written to a temporary file first and then swapped in, so a failed
    import leaves the previous dictionary untouched.
    """
    source_path = Path(source_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = db_path.with_name(db_path.name + ".importing")
    if temp_path.exists():
        temp_path.unlink()

    connection = sqlite3.connect(temp_path)
    try:
        connection.execute("PRAGMA journal_mode = OFF")
        connection.execute("PRAGMA synchronous = OFF")
        with open_jmdict_text(source_path) as stream:
            entry_count = load_jmdict(connection, stream, source_name=source_path.name)
        connection.commit()
    except BaseException:
        connection.close()
        temp_path.unlink(missing_ok=True)
        raise
    connection.close()
    os.replace(temp_path, db_path)
    return entry_count


# --------------------------------------------------------------------------
# Lookup
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PosPreference:
    """JMdict pos codes that a catalog part of speech prefers.

    ``prefixes`` match JMdict codes by prefix, so ``v5`` matches ``v5r``. With
    ``picks_sense`` off the preference only ranks entries, and each entry keeps
    its usual first sense.
    """

    prefixes: tuple[str, ...]
    picks_sense: bool = True

    def matches(self, codes: Iterable[str]) -> bool:
        return any(code.startswith(self.prefixes) for code in codes)


# Sudachi's 名詞 also covers adverbial, na-adjectival and counter nouns, and the
# older English "noun" label covers pronouns. So it accepts those codes and does
# not pick a later sense: 結局 keeps "after all" and はず keeps "should (be)".
# "n" also covers n-adv, n-pr, n-pref, n-suf, n-t and num.
NOUN_POS = PosPreference(("n", "pn", "adj-na", "adj-no", "ctr"), picks_sense=False)
# "vs-" leaves out bare "vs" (a noun that takes suru) and the vi/vt markers.
VERB_POS = PosPreference(("v1", "v2", "v4", "v5", "vk", "vn", "vr", "vs-", "vz", "v-unspec"))
I_ADJECTIVE_POS = PosPreference(("adj-i",))  # adj-i, adj-ix
NA_ADJECTIVE_POS = PosPreference(("adj-na", "adj-t"))  # adj-na, adj-nari, adj-t
ADVERB_POS = PosPreference(("adv",))  # adv, adv-to
AUXILIARY_POS = PosPreference(("aux", "cop"))  # aux, aux-v, aux-adj, cop
PARTICLE_POS = PosPreference(("prt",))
SUFFIX_POS = PosPreference(("suf", "n-suf", "ctr"))
PREFIX_POS = PosPreference(("pref", "n-pref"))
INTERJECTION_POS = PosPreference(("int",))
PRONOUN_POS = PosPreference(("pn",))
ADNOMINAL_POS = PosPreference(("adj-pn",))
CONJUNCTION_POS = PosPreference(("conj",))
EXPRESSION_POS = PosPreference(("exp",))

# Sudachi's coarse part of speech (the first token of terms.pos) and the older
# English labels found in the catalog.
TERM_POS_PREFERENCES: dict[str, PosPreference] = {
    "名詞": NOUN_POS,
    "noun": NOUN_POS,
    "proper-noun": NOUN_POS,
    "ui-noun": NOUN_POS,
    "動詞": VERB_POS,
    "verb": VERB_POS,
    "形容詞": I_ADJECTIVE_POS,
    "adjective": I_ADJECTIVE_POS,
    "形状詞": NA_ADJECTIVE_POS,
    "na-adj": NA_ADJECTIVE_POS,
    "na-adjective": NA_ADJECTIVE_POS,
    "adjectival_noun": NA_ADJECTIVE_POS,
    "副詞": ADVERB_POS,
    "adverb": ADVERB_POS,
    "助動詞": AUXILIARY_POS,
    "auxiliary": AUXILIARY_POS,
    "助詞": PARTICLE_POS,
    "particle": PARTICLE_POS,
    "接尾辞": SUFFIX_POS,
    "suffix": SUFFIX_POS,
    "接頭辞": PREFIX_POS,
    "prefix": PREFIX_POS,
    "感動詞": INTERJECTION_POS,
    "interjection": INTERJECTION_POS,
    "代名詞": PRONOUN_POS,
    "pronoun": PRONOUN_POS,
    "連体詞": ADNOMINAL_POS,
    "adnominal": ADNOMINAL_POS,
    "接続詞": CONJUNCTION_POS,
    "conjunction": CONJUNCTION_POS,
    "expression": EXPRESSION_POS,
}


def jmdict_pos_preference(term_pos: str | None) -> PosPreference | None:
    """Map a catalog part of speech to the JMdict pos codes it prefers.

    Only the first token counts (``動詞 非自立可能`` -> ``動詞``). Unknown or
    empty values return None, which means no preference.
    """
    normalized = normalize_text(term_pos)
    if not normalized:
        return None
    coarse = TERM_POS_SEPARATOR_RE.split(normalized)[0].lower()
    return TERM_POS_PREFERENCES.get(coarse)


@dataclass(frozen=True)
class KanjiForm:
    position: int
    text: str
    common: bool


@dataclass(frozen=True)
class ReadingForm:
    position: int
    text: str
    lookup_key: str
    no_kanji: bool
    restrictions: tuple[str, ...]
    common: bool
    priority: tuple[str, ...] = ()

    def applies_to(self, kanji: str) -> bool:
        if self.no_kanji:
            return False
        return not self.restrictions or kanji in self.restrictions

    @property
    def basic(self) -> bool:
        """ichi1: listed in a basic vocabulary, whatever the usual spelling."""
        return "ichi1" in self.priority


@dataclass(frozen=True)
class Sense:
    position: int
    pos: tuple[str, ...]
    misc: tuple[str, ...]
    stagk: tuple[str, ...]
    stagr: tuple[str, ...]
    glosses: tuple[str, ...]

    def applies_to(self, kanji: str | None, reading: str) -> bool:
        if self.stagk and kanji is not None and kanji not in self.stagk:
            return False
        return not self.stagr or reading in self.stagr

    @property
    def disfavored(self) -> bool:
        return bool(DISFAVORED_SENSE_MISC.intersection(self.misc))

    def matches_pos(self, pos: PosPreference | None) -> bool:
        return pos is not None and pos.matches(self.pos)


@dataclass(frozen=True)
class Entry:
    ent_seq: int
    kanji: tuple[KanjiForm, ...]
    readings: tuple[ReadingForm, ...]
    senses: tuple[Sense, ...]

    def applicable_senses(self, kanji: str | None, reading: str) -> list[Sense]:
        return [sense for sense in self.senses if sense.applies_to(kanji, reading)]

    def matches_pos(self, kanji: str | None, reading: str, pos: PosPreference | None) -> bool:
        return any(sense.matches_pos(pos) for sense in self.applicable_senses(kanji, reading))

    def best_sense(
        self,
        kanji: str | None,
        reading: str,
        pos: PosPreference | None = None,
    ) -> Sense | None:
        """Pick the first sense that is not archaic, preferring the part of speech.

        The part of speech is a preference only: without a matching sense the
        first non-archaic sense wins, as it does when the part of speech is unknown.
        """
        applicable = self.applicable_senses(kanji, reading)
        if not applicable:
            return None
        preferred = pos if pos is not None and pos.picks_sense else None
        return max(
            applicable,
            key=lambda sense: (
                sense.matches_pos(preferred) and not sense.disfavored,
                not sense.disfavored,
                sense.matches_pos(preferred),
                -sense.position,
            ),
        )


@dataclass(frozen=True)
class JMdictMatch:
    ent_seq: int
    kanji: str | None
    reading: str
    pos: tuple[str, ...]
    glosses: tuple[str, ...]

    def gloss_list(self, limit: int = DEFAULT_GLOSS_LIMIT) -> list[str]:
        return unique_glosses(self.glosses)[: max(1, limit)]

    def gloss_text(self, limit: int = DEFAULT_GLOSS_LIMIT) -> str:
        return "; ".join(self.gloss_list(limit))


class JMdict:
    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, db_path: Path = DEFAULT_JMDICT_DB_PATH) -> JMdict | None:
        """Open an imported dictionary read-only, or return None if it is missing."""
        db_path = Path(db_path)
        if not db_path.is_file():
            return None
        connection = sqlite3.connect(
            f"{db_path.resolve().as_uri()}?mode=ro",
            uri=True,
        )
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> JMdict:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def metadata(self) -> dict[str, str]:
        return dict(self.connection.execute("SELECT key, value FROM meta").fetchall())

    def load_entry(self, ent_seq: int) -> Entry:
        kanji = tuple(
            KanjiForm(position, text, is_common(json.loads(priority)))
            for position, text, priority in self.connection.execute(
                """
                SELECT position, text, priority
                FROM kanji_forms
                WHERE ent_seq = ?
                ORDER BY position
                """,
                (ent_seq,),
            )
        )
        readings = tuple(
            ReadingForm(
                position,
                text,
                lookup_key,
                bool(no_kanji),
                tuple(json.loads(restrictions)),
                is_common(json.loads(priority)),
                tuple(json.loads(priority)),
            )
            for position, text, lookup_key, no_kanji, restrictions, priority in (
                self.connection.execute(
                    """
                    SELECT position, text, lookup_key, no_kanji, restrictions, priority
                    FROM reading_forms
                    WHERE ent_seq = ?
                    ORDER BY position
                    """,
                    (ent_seq,),
                )
            )
        )
        senses = tuple(
            Sense(
                position,
                tuple(json.loads(pos)),
                tuple(json.loads(misc)),
                tuple(json.loads(stagk)),
                tuple(json.loads(stagr)),
                tuple(json.loads(glosses)),
            )
            for position, pos, misc, stagk, stagr, glosses in self.connection.execute(
                """
                SELECT position, pos, misc, stagk, stagr, glosses
                FROM senses
                WHERE ent_seq = ?
                ORDER BY position
                """,
                (ent_seq,),
            )
        )
        return Entry(ent_seq, kanji, readings, senses)

    def lookup(
        self,
        lemma: str | None,
        *,
        reading: str | None = None,
        pos: str | None = None,
    ) -> JMdictMatch | None:
        """Find the best JMdict sense for a catalog term.

        Lemmas with kanji match a kanji form; the reading picks between entries
        and readings (exact match first, then the longest shared prefix, since
        catalog readings are often conjugated surface readings like ウレ for
        売れる). Kana-only lemmas match reading forms. Lemmas without any kana or
        kanji (OCR noise such as ``s`` or ``1``) never match.

        ``pos`` is the catalog part of speech (Sudachi's ``助動詞 *`` or an older
        English label such as ``verb``). A sense with a matching JMdict pos is
        preferred over commonness, so kana grammar words like ます pick the
        auxiliary verb instead of 鱒. It never rules a match out.
        """
        lemma_text = normalize_text(lemma)
        if not any(is_japanese_char(char) for char in lemma_text):
            return None
        pos_preference = jmdict_pos_preference(pos)
        if is_kana_text(lemma_text):
            return self._lookup_kana(lemma_text, pos_preference)
        reading_text = normalize_text(reading)
        reading_key = reading_lookup_key(reading_text) if is_kana_text(reading_text) else ""
        return self._lookup_kanji(lemma_text, reading_key, pos_preference)

    def _lookup_kanji(
        self,
        lemma_text: str,
        reading_key: str,
        pos: PosPreference | None,
    ) -> JMdictMatch | None:
        best_score: tuple | None = None
        best_match: JMdictMatch | None = None
        rows = self.connection.execute(
            "SELECT DISTINCT ent_seq FROM kanji_forms WHERE lookup_key = ? ORDER BY ent_seq",
            (kanji_lookup_key(lemma_text),),
        ).fetchall()
        for (ent_seq,) in rows:
            entry = self.load_entry(ent_seq)
            for kanji in entry.kanji:
                if kanji_lookup_key(kanji.text) != kanji_lookup_key(lemma_text):
                    continue
                for reading in entry.readings:
                    if not reading.applies_to(kanji.text):
                        continue
                    sense = entry.best_sense(kanji.text, reading.text, pos)
                    if sense is None:
                        continue
                    score = (
                        bool(reading_key) and reading.lookup_key == reading_key,
                        common_prefix_length(reading.lookup_key, reading_key),
                        entry.matches_pos(kanji.text, reading.text, pos),
                        kanji.common,
                        reading.common,
                        -kanji.position,
                        -reading.position,
                        -entry.ent_seq,
                    )
                    if best_score is None or score > best_score:
                        best_score = score
                        best_match = JMdictMatch(
                            entry.ent_seq,
                            kanji.text,
                            reading.text,
                            sense.pos,
                            sense.glosses,
                        )
        return best_match

    def _lookup_kana(
        self,
        lemma_text: str,
        pos: PosPreference | None,
    ) -> JMdictMatch | None:
        best_score: tuple | None = None
        best_match: JMdictMatch | None = None
        lemma_key = reading_lookup_key(lemma_text)
        rows = self.connection.execute(
            "SELECT DISTINCT ent_seq FROM reading_forms WHERE lookup_key = ? ORDER BY ent_seq",
            (lemma_key,),
        ).fetchall()
        for (ent_seq,) in rows:
            entry = self.load_entry(ent_seq)
            for reading in entry.readings:
                if reading.lookup_key != lemma_key:
                    continue
                sense = entry.best_sense(None, reading.text, pos)
                if sense is None:
                    continue
                # A "usually kana" mark on any current sense counts: 物 is uk
                # only in its later senses, and 来る only as an auxiliary.
                written_in_kana = (
                    not entry.kanji
                    or reading.no_kanji
                    or "uk" in sense.misc
                    or any(
                        "uk" in candidate.misc
                        for candidate in entry.applicable_senses(None, reading.text)
                        if not candidate.disfavored
                    )
                )
                score = (
                    reading.text == lemma_text,
                    entry.matches_pos(None, reading.text, pos),
                    written_in_kana,
                    reading.common,
                    # Among common homophones, basic vocabulary wins (上 over 飢え,
                    # 居る over 要る).
                    reading.basic,
                    -reading.position,
                    -entry.ent_seq,
                )
                if best_score is None or score > best_score:
                    best_score = score
                    best_match = JMdictMatch(
                        entry.ent_seq,
                        None,
                        reading.text,
                        sense.pos,
                        sense.glosses,
                    )
        return best_match


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def import_command(args: argparse.Namespace) -> None:
    entry_count = import_jmdict(args.source, args.db)
    print(f"Imported {entry_count} JMdict entries from {args.source} into {args.db}")


def lookup_command(args: argparse.Namespace) -> None:
    dictionary = JMdict.open(args.db)
    if dictionary is None:
        raise SystemExit(f"JMdict database not found at {args.db}. Run the import command first.")
    with dictionary:
        match = dictionary.lookup(args.lemma, reading=args.reading, pos=args.pos)
    if match is None:
        print("No match.")
        return
    print(f"ent_seq: {match.ent_seq}")
    print(f"kanji:   {match.kanji or '-'}")
    print(f"reading: {match.reading}")
    print(f"pos:     {', '.join(match.pos) or '-'}")
    print(f"glosses: {match.gloss_text(args.gloss_limit)}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Import and query a local JMdict (EDRDG, CC BY-SA 4.0) dictionary.",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_JMDICT_DB_PATH,
        help=f"JMdict SQLite path (default: {DEFAULT_JMDICT_DB_PATH})",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    import_parser = subparsers.add_parser(
        "import",
        help=f"Import a JMdict XML file such as JMdict_e.gz from {JMDICT_DOWNLOAD_URL}.",
    )
    import_parser.add_argument("source", type=Path, help="Path to JMdict_e.gz or JMdict_e (XML).")
    import_parser.set_defaults(func=import_command)

    lookup_parser = subparsers.add_parser("lookup", help="Show the best match for a lemma.")
    lookup_parser.add_argument("lemma")
    lookup_parser.add_argument("--reading")
    lookup_parser.add_argument("--pos", help="Catalog part of speech, such as 助動詞 or verb.")
    lookup_parser.add_argument("--gloss-limit", type=int, default=DEFAULT_GLOSS_LIMIT)
    lookup_parser.set_defaults(func=lookup_command)
    return parser


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
