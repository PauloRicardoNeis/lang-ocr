"""Settings registry: every user-tunable option, declared once.

A setting has a key ("ocr.min_confidence"), a type, a default and the text the
settings page shows. Values live in the ``settings`` table of the catalog
database, so the capture worker, the reading overlay, the tray daemon, the web
UI and the CLI all see the same value, and a change applies without restarting
anything (each consumer reads a fresh snapshot at its point of use).

Resolution order for one key:

1. the environment variable named by ``Setting.env``, when set and valid. It
   *pins* the value: the page shows it read-only. This keeps the old
   ``LANG_JP_*`` variables working for one-off runs.
2. the row in ``settings``;
3. the registry default.

Only values that differ from the default are stored, so a default changed in
code reaches every database that never overrode it.

Not settings: where things are and how processes are spawned (database path,
ports, python executables). Those stay in ``capture_daemon.toml`` because they
are needed before the database can be opened. Capture languages, capture
profiles and skip rules keep their own tables.

This module imports nothing from the rest of the catalog.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Mapping

SETTINGS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""

KIND_BOOL = "bool"
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_CHOICE = "choice"
KIND_TEXT = "text"
KIND_LANGUAGE = "language"
KIND_LANGUAGE_LIST = "language_list"
KIND_HOTKEY = "hotkey"

# When a change takes effect.
APPLIES_NEXT_JOB = "next_job"
# The next job reloads the OCR models (the worker does it by itself).
APPLIES_ENGINES = "engines"
# Read once when the tray daemon starts.
APPLIES_DAEMON_RESTART = "daemon_restart"

SOURCE_ENV = "env"
SOURCE_DB = "db"
SOURCE_DEFAULT = "default"

TRUE_WORDS = frozenset({"1", "true", "yes", "on"})
FALSE_WORDS = frozenset({"0", "false", "no", "off"})
LANGUAGE_CODE_RE = re.compile(r"^[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*$")
HOTKEY_RE = re.compile(r"^[A-Za-z0-9]+(?:\+[A-Za-z0-9]+)*$")
MAX_TEXT_LENGTH = 200


@dataclass(frozen=True)
class Section:
    key: str
    label: str
    help: str


@dataclass(frozen=True)
class Setting:
    key: str
    kind: str
    default: object
    section: str
    label: str
    help: str
    env: str | None = None
    applies: str = APPLIES_NEXT_JOB
    # KIND_CHOICE: (value, label) pairs, in display order.
    choices: tuple[tuple[str, str], ...] = ()
    # Other spellings accepted for a choice ("cuda" for "gpu").
    aliases: tuple[tuple[str, str], ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    advanced: bool = False

    def choice_values(self) -> tuple[str, ...]:
        return tuple(value for value, _ in self.choices)


SECTIONS: tuple[Section, ...] = (
    Section("ocr", "OCR", "Como o texto de cada captura e lido."),
    Section("capture", "Captura", "Quando a tela e capturada e o que o worker faz com ela."),
    Section("translation", "Traducao", "Significados dos termos e traducao das fontes."),
    Section("hotkeys", "Atalhos", "Atalhos globais registrados pelo tray."),
)

REGISTRY: tuple[Setting, ...] = (
    # --- OCR ---------------------------------------------------------------
    Setting(
        "ocr.backend",
        KIND_CHOICE,
        "auto",
        "ocr",
        "Motor de OCR",
        "Automatico usa meikiocr para japones e EasyOCR para os outros idiomas.",
        env="LANG_JP_OCR_BACKEND",
        applies=APPLIES_ENGINES,
        choices=(("auto", "Automatico"), ("meikiocr", "meikiocr"), ("easyocr", "EasyOCR")),
    ),
    Setting(
        "ocr.easyocr_gpu",
        KIND_CHOICE,
        "auto",
        "ocr",
        "EasyOCR na GPU",
        "Automatico usa a GPU quando o CUDA esta disponivel.",
        env="LANG_JP_EASYOCR_GPU",
        applies=APPLIES_ENGINES,
        choices=(("auto", "Automatico"), ("gpu", "GPU"), ("cpu", "CPU")),
        aliases=(
            ("1", "gpu"),
            ("true", "gpu"),
            ("yes", "gpu"),
            ("on", "gpu"),
            ("cuda", "gpu"),
            ("0", "cpu"),
            ("false", "cpu"),
            ("no", "cpu"),
            ("off", "cpu"),
        ),
    ),
    Setting(
        "ocr.max_side",
        KIND_INT,
        1600,
        "ocr",
        "Lado maximo da imagem",
        "Em pixels; a captura e reduzida ate esse tamanho antes do OCR. 0 mantem o tamanho original.",
        env="LANG_JP_OCR_MAX_SIDE",
        minimum=0,
        maximum=8192,
        step=1,
    ),
    Setting(
        "ocr.multi_max_side",
        KIND_INT,
        3840,
        "ocr",
        "Lado maximo com varios idiomas",
        "Usado quando varios leitores EasyOCR dividem a mesma deteccao; texto de interface pequeno vira lixo abaixo disso.",
        env="LANG_JP_MULTI_OCR_MAX_SIDE",
        minimum=0,
        maximum=8192,
        step=1,
    ),
    Setting(
        "ocr.min_confidence",
        KIND_FLOAT,
        0.30,
        "ocr",
        "Confianca minima",
        "Caixas do EasyOCR lidas abaixo disso sao descartadas.",
        env="LANG_JP_OCR_MIN_CONFIDENCE",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    ),
    Setting(
        "ocr.min_term_confidence",
        KIND_FLOAT,
        0.5,
        "ocr",
        "Confianca minima do termo",
        "Linhas nao japonesas lidas abaixo disso ficam na fonte mas nao geram candidatos.",
        env="LANG_JP_MIN_TERM_CONFIDENCE",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
    ),
    Setting(
        "ocr.confident_read",
        KIND_FLOAT,
        0.7,
        "ocr",
        "Leitura confiante",
        "Uma caixa lida com essa confianca por um leitor EasyOCR nao e enviada aos proximos.",
        env="LANG_JP_OCR_CONFIDENT_READ",
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        advanced=True,
    ),
    Setting(
        "ocr.meikiocr_min_char_confidence",
        KIND_FLOAT,
        0.3,
        "ocr",
        "Confianca minima por caractere (meikiocr)",
        "Caracteres lidos abaixo disso sao descartados da linha.",
        applies=APPLIES_ENGINES,
        minimum=0.0,
        maximum=1.0,
        step=0.05,
        advanced=True,
    ),
    Setting(
        "ocr.horizontal_min_ratio",
        KIND_FLOAT,
        1.2,
        "ocr",
        "Proporcao minima de linha horizontal",
        "Largura dividida pela altura a partir da qual uma caixa do EasyOCR conta como horizontal.",
        env="LANG_JP_OCR_HORIZONTAL_MIN_RATIO",
        minimum=0.1,
        maximum=20.0,
        step=0.1,
        advanced=True,
    ),
    Setting(
        "ocr.horizontal_max_angle",
        KIND_FLOAT,
        18.0,
        "ocr",
        "Inclinacao maxima de linha horizontal",
        "Em graus.",
        env="LANG_JP_OCR_HORIZONTAL_MAX_ANGLE",
        minimum=0.0,
        maximum=90.0,
        step=1,
        advanced=True,
    ),
    Setting(
        "ocr.region_padding",
        KIND_INT,
        12,
        "ocr",
        "Margem das regioes",
        "Pixels acrescentados em volta de uma regiao relida (EasyOCR legado).",
        env="LANG_JP_OCR_REGION_PADDING",
        minimum=0,
        maximum=400,
        step=1,
        advanced=True,
    ),
    Setting(
        "ocr.max_regions",
        KIND_INT,
        3,
        "ocr",
        "Regioes por captura",
        "Quantas regioes de texto sao mantidas (EasyOCR legado).",
        env="LANG_JP_OCR_MAX_REGIONS",
        minimum=1,
        maximum=50,
        step=1,
        advanced=True,
    ),
    Setting(
        "ocr.easyocr_languages",
        KIND_LANGUAGE_LIST,
        "ja,en",
        "ocr",
        "Idiomas do EasyOCR legado",
        "Usado apenas quando o motor e EasyOCR; nos outros modos os leitores seguem os idiomas capturados.",
        env="LANG_JP_EASYOCR_LANGS",
        applies=APPLIES_ENGINES,
        advanced=True,
    ),
    Setting(
        "ocr.progress_interval_seconds",
        KIND_FLOAT,
        5.0,
        "ocr",
        "Intervalo do log de progresso",
        "Segundos entre as linhas de progresso de um OCR demorado.",
        env="LANG_JP_OCR_PROGRESS_INTERVAL",
        minimum=0.5,
        maximum=600.0,
        step=0.5,
        advanced=True,
    ),
    # --- Capture -----------------------------------------------------------
    Setting(
        "capture.interval_seconds",
        KIND_INT,
        # Must equal DaemonConfig::default().interval_seconds in
        # desktop_capture_daemon/src/main.rs (the daemon consumes this one).
        60,
        "capture",
        "Intervalo entre capturas",
        "Em segundos. Tambem no tray, em Capture interval.",
        minimum=1,
        maximum=3600,
        step=1,
    ),
    Setting(
        "capture.after_source_ready",
        KIND_BOOL,
        # Must equal DaemonConfig::default().capture_after_source_ready.
        False,
        "capture",
        "Esperar a fonte anterior",
        "Conta o intervalo a partir do fim do job anterior, em vez de um relogio fixo.",
    ),
    Setting(
        "capture.auto_promote_terms",
        KIND_BOOL,
        True,
        "capture",
        "Aprovar termos conhecidos",
        "Candidatos que o dicionario conhece viram termos da fonte sem revisao.",
        env="LANG_JP_AUTO_PROMOTE_SOURCE_TERMS",
    ),
    Setting(
        "capture.debug_log",
        KIND_BOOL,
        False,
        "capture",
        "Log detalhado",
        "Escreve cada etapa do worker em capture_daemon.log.",
        env="LANG_JP_CAPTURE_DEBUG",
    ),
    Setting(
        "capture.stale_job_hours",
        KIND_FLOAT,
        4.0,
        "capture",
        "Recuperar jobs travados",
        "Horas depois das quais um job parado em processamento volta para a fila quando o worker inicia.",
        env="LANG_JP_CAPTURE_STALE_JOB_HOURS",
        minimum=0.1,
        maximum=720.0,
        step=0.5,
        advanced=True,
    ),
    # --- Translation -------------------------------------------------------
    Setting(
        "translation.auto_term_meanings",
        KIND_BOOL,
        True,
        "translation",
        "Traduzir significados automaticamente",
        "Preenche o significado dos termos novos que o dicionario nao cobre.",
        env="LANG_JP_AUTO_TRANSLATE_TERM_MEANINGS",
    ),
    Setting(
        "translation.meaning_language",
        KIND_LANGUAGE,
        "pt",
        "translation",
        "Idioma dos significados",
        "Idioma em que os significados dos termos sao escritos.",
        env="LANG_JP_TERM_TRANSLATION_TARGET",
    ),
    Setting(
        "translation.translate_every_capture",
        KIND_BOOL,
        False,
        "translation",
        "Traduzir cada captura",
        "Desligado, a traducao de uma fonte e feita sob demanda na pagina dela.",
        env="LANG_JP_ENABLE_SOURCE_TRANSLATION",
    ),
    Setting(
        "translation.source_language",
        KIND_LANGUAGE,
        "en",
        "translation",
        "Idioma da traducao das fontes",
        "Idioma de destino da traducao do texto inteiro de uma fonte.",
        env="LANG_JP_SOURCE_TRANSLATION_TARGET",
    ),
    Setting(
        "translation.overlay_language",
        KIND_LANGUAGE,
        "en",
        "translation",
        "Idioma da traducao do overlay",
        "Idioma de destino do modo traducao do overlay de leitura.",
        env="LANG_OVERLAY_TARGET",
    ),
    Setting(
        "translation.term_timeout_seconds",
        KIND_FLOAT,
        10.0,
        "translation",
        "Tempo limite da traducao de termos",
        "Segundos de espera por resposta do tradutor.",
        env="LANG_JP_TERM_TRANSLATION_TIMEOUT_SECONDS",
        minimum=1.0,
        maximum=120.0,
        step=1,
        advanced=True,
    ),
    # --- Hotkeys -----------------------------------------------------------
    Setting(
        "hotkeys.manual_capture",
        KIND_HOTKEY,
        "",
        "hotkeys",
        "Capturar agora",
        "Vazio desliga o atalho. Exemplo: Alt+Q.",
        applies=APPLIES_DAEMON_RESTART,
    ),
    Setting(
        "hotkeys.overlay_toggle",
        KIND_HOTKEY,
        "Ctrl+Alt+Q",
        "hotkeys",
        "Mostrar ou esconder o overlay",
        "Le a tela e mostra o overlay; de novo para esconder.",
        env="LANG_OVERLAY_HOTKEY",
        applies=APPLIES_DAEMON_RESTART,
    ),
    Setting(
        "hotkeys.overlay_mode",
        KIND_HOTKEY,
        "Ctrl+Alt+W",
        "hotkeys",
        "Trocar o modo do overlay",
        "Alterna entre traducao, furigana e palavras.",
        env="LANG_OVERLAY_MODE_HOTKEY",
        applies=APPLIES_DAEMON_RESTART,
    ),
)

_BY_KEY: dict[str, Setting] = {setting.key: setting for setting in REGISTRY}


class SettingError(SystemExit):
    """An unknown key or an invalid value.

    A ``SystemExit`` like the catalog's other validation errors: the CLI prints
    the message and exits, the web actions catch it and show a notice.
    """


def setting_for(key: str) -> Setting:
    try:
        return _BY_KEY[key]
    except KeyError:
        raise SettingError(f"Unknown setting: {key!r}") from None


def section_settings(section: str) -> tuple[Setting, ...]:
    return tuple(setting for setting in REGISTRY if setting.section == section)


def section_for(key: str) -> Section:
    for section in SECTIONS:
        if section.key == key:
            return section
    raise SettingError(f"Unknown settings section: {key!r}")


def _parse_number(setting: Setting, raw: object) -> int | float:
    text = str(raw).strip().replace(",", ".")
    try:
        number = float(text)
    except ValueError:
        raise SettingError(f"{setting.key}: {raw!r} is not a number.") from None
    if number != number or number in (float("inf"), float("-inf")):
        raise SettingError(f"{setting.key}: {raw!r} is not a number.")
    if setting.kind == KIND_INT:
        if number != int(number):
            raise SettingError(f"{setting.key}: {raw!r} is not a whole number.")
        number = int(number)
    if setting.minimum is not None and number < setting.minimum:
        raise SettingError(f"{setting.key}: {number} is below the minimum {setting.minimum:g}.")
    if setting.maximum is not None and number > setting.maximum:
        raise SettingError(f"{setting.key}: {number} is above the maximum {setting.maximum:g}.")
    return number


def _parse_language(setting: Setting, raw: str) -> str:
    code = raw.strip()
    if not LANGUAGE_CODE_RE.match(code):
        raise SettingError(f"{setting.key}: {raw!r} is not a language code.")
    return code


def parse_value(setting: Setting, raw: object) -> object:
    """Validate ``raw`` (a typed value or its text form) and return the typed value."""
    if setting.kind == KIND_BOOL:
        if isinstance(raw, bool):
            return raw
        word = str(raw).strip().lower()
        if word in TRUE_WORDS:
            return True
        if word in FALSE_WORDS:
            return False
        raise SettingError(f"{setting.key}: {raw!r} is not on/off.")
    if setting.kind in (KIND_INT, KIND_FLOAT):
        if isinstance(raw, bool):
            raise SettingError(f"{setting.key}: {raw!r} is not a number.")
        return _parse_number(setting, raw)
    text = str(raw).strip()
    if len(text) > MAX_TEXT_LENGTH:
        raise SettingError(f"{setting.key}: value is too long.")
    if setting.kind == KIND_CHOICE:
        value = text.lower()
        value = dict(setting.aliases).get(value, value)
        if value not in setting.choice_values():
            raise SettingError(
                f"{setting.key}: {raw!r} is not one of {', '.join(setting.choice_values())}."
            )
        return value
    if setting.kind == KIND_LANGUAGE:
        return _parse_language(setting, text)
    if setting.kind == KIND_LANGUAGE_LIST:
        codes = [_parse_language(setting, part) for part in text.split(",") if part.strip()]
        if not codes:
            raise SettingError(f"{setting.key}: at least one language code is needed.")
        return ",".join(dict.fromkeys(codes))
    if setting.kind == KIND_HOTKEY:
        compact = "+".join(part.strip() for part in text.split("+")) if text else ""
        if compact and not HOTKEY_RE.match(compact):
            raise SettingError(f"{setting.key}: {raw!r} is not a hotkey such as Ctrl+Alt+Q.")
        return compact
    return text


def serialize_value(setting: Setting, value: object) -> str:
    """The text stored in ``settings.value`` (and shown in a form field)."""
    if setting.kind == KIND_BOOL:
        return "1" if value else "0"
    if setting.kind == KIND_FLOAT:
        return f"{float(value):g}"  # type: ignore[arg-type]
    return str(value)


def _same_value(setting: Setting, left: object, right: object) -> bool:
    return serialize_value(setting, left) == serialize_value(setting, right)


@dataclass(frozen=True)
class Settings:
    """An immutable snapshot of every setting, with where each value came from."""

    values: Mapping[str, object]
    sources: Mapping[str, str]

    @classmethod
    def resolve(
        cls,
        rows: Mapping[str, str] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> "Settings":
        """Combine stored rows, environment pins and defaults.

        A stored or pinned value that no longer validates (a range narrowed in
        code, a typo in an env var) is skipped, never an error: the next source
        in the order decides.
        """
        rows = rows or {}
        environ = os.environ if environ is None else environ
        values: dict[str, object] = {}
        sources: dict[str, str] = {}
        for setting in REGISTRY:
            candidates: list[tuple[str, str]] = []
            if setting.env and str(environ.get(setting.env, "")).strip():
                candidates.append((SOURCE_ENV, str(environ[setting.env])))
            if setting.key in rows:
                candidates.append((SOURCE_DB, rows[setting.key]))
            values[setting.key] = setting.default
            sources[setting.key] = SOURCE_DEFAULT
            for source, raw in candidates:
                try:
                    values[setting.key] = parse_value(setting, raw)
                except SettingError:
                    continue
                sources[setting.key] = source
                break
        return cls(values=values, sources=sources)

    @classmethod
    def defaults(cls, environ: Mapping[str, str] | None = None) -> "Settings":
        """The snapshot of a caller without a database (diagnostics, tests)."""
        return cls.resolve({}, environ)

    def get(self, key: str) -> object:
        setting_for(key)
        return self.values[key]

    def __getitem__(self, key: str) -> object:
        return self.get(key)

    def source(self, key: str) -> str:
        setting_for(key)
        return self.sources[key]

    def is_pinned(self, key: str) -> bool:
        return self.source(key) == SOURCE_ENV

    def is_changed(self, key: str) -> bool:
        """Whether the effective value differs from the registry default."""
        setting = setting_for(key)
        return not _same_value(setting, self.values[key], setting.default)

    def text(self, key: str) -> str:
        return serialize_value(setting_for(key), self.values[key])

    def replace(self, **overrides: object) -> "Settings":
        """A copy with some values swapped (``ocr__max_side=0``); for tests and one-off runs."""
        values = dict(self.values)
        for name, raw in overrides.items():
            key = name.replace("__", ".")
            values[key] = parse_value(setting_for(key), raw)
        return Settings(values=values, sources=dict(self.sources))


def stored_rows(connection: sqlite3.Connection) -> dict[str, str]:
    """The raw ``settings`` rows; empty when the table does not exist yet."""
    try:
        rows = connection.execute("SELECT key, value FROM settings").fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(row[0]): str(row[1]) for row in rows}


def load_settings(
    connection: sqlite3.Connection,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    return Settings.resolve(stored_rows(connection), environ)


def set_setting(connection: sqlite3.Connection, key: str, raw: object) -> object:
    """Store ``raw`` for ``key`` and return the typed value. The caller commits.

    A value equal to the default removes the row instead.
    """
    setting = setting_for(key)
    value = parse_value(setting, raw)
    if _same_value(setting, value, setting.default):
        connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        return value
    connection.execute(
        """
        INSERT INTO settings (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value = excluded.value,
            updated_at = CURRENT_TIMESTAMP
        WHERE settings.value IS NOT excluded.value
        """,
        (key, serialize_value(setting, value)),
    )
    return value


def set_settings(connection: sqlite3.Connection, values: Mapping[str, object]) -> dict[str, object]:
    """Validate every value first, then store them all (nothing is stored on an error)."""
    parsed = {key: parse_value(setting_for(key), raw) for key, raw in values.items()}
    for key, value in parsed.items():
        set_setting(connection, key, value)
    return parsed


def reset_settings(connection: sqlite3.Connection, keys: Iterable[str]) -> None:
    """Back to the registry default. The caller commits."""
    for key in keys:
        setting_for(key)
        connection.execute("DELETE FROM settings WHERE key = ?", (key,))


def reset_section(connection: sqlite3.Connection, section: str) -> None:
    section_for(section)
    reset_settings(connection, (setting.key for setting in section_settings(section)))
