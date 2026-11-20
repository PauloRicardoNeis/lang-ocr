"""The settings page of the web UI: forms rendered from the registry, and saving them.

Everything here is driven by ``settings.REGISTRY``: a setting added there gets
its row, its control and its validation with no change in this module. The
functions take a ``Settings`` snapshot and return HTML or plain data;
``lang_jp_web`` places them in the page and wires the route and the action.

Each section is one form. Its fields are static (the live updater never
replaces something the user may be typing into), so a form can show a value
that the tray changed after the page was rendered. To keep a save from undoing
such a change, every field carries the value it was rendered with and only the
fields the user actually changed are written (``apply_settings_form``). The
values in use are shown next to the form as a live summary.
"""

from __future__ import annotations

import html
import sqlite3
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import settings as registry
from settings import (
    APPLIES_DAEMON_RESTART,
    APPLIES_ENGINES,
    KIND_BOOL,
    KIND_CHOICE,
    KIND_FLOAT,
    KIND_HOTKEY,
    KIND_INT,
    KIND_LANGUAGE,
    KIND_LANGUAGE_LIST,
    Section,
    Setting,
    SettingError,
    Settings,
)

# A field named ``was:<key>`` holds the value the form was rendered with.
RENDERED_VALUE_PREFIX = "was:"
LANGUAGE_DATALIST_ID = "setting-language-codes"
APPLIES_LABELS = {
    APPLIES_ENGINES: "recarrega os motores",
    APPLIES_DAEMON_RESTART: "reinicia o tray",
}


def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def field_id(setting: Setting) -> str:
    return "setting-" + setting.key.replace(".", "-")


def display_value(setting: Setting, value: object) -> str:
    """A value as the page words it ("ligado", a choice's label, "vazio")."""
    if setting.kind == KIND_BOOL:
        return "ligado" if value else "desligado"
    if setting.kind == KIND_CHOICE:
        return dict(setting.choices).get(str(value), str(value))
    text = registry.serialize_value(setting, value)
    return text if text else "vazio"


def control_html(setting: Setting, snapshot: Settings) -> str:
    """The input of one setting, with the hidden field holding its rendered value."""
    key = escape(setting.key)
    identifier = field_id(setting)
    value = snapshot.get(setting.key)
    text = snapshot.text(setting.key)
    rendered = f'<input type="hidden" name="{escape(RENDERED_VALUE_PREFIX + setting.key)}" value="{escape(text)}">'
    if setting.kind == KIND_BOOL:
        checked = " checked" if value else ""
        # The hidden "0" makes an unchecked switch arrive as a value instead of nothing.
        return (
            f'{rendered}<input type="hidden" name="{key}" value="0">'
            f'<label class="switch"><input type="checkbox" id="{identifier}" name="{key}" value="1"{checked}>'
            f"<span></span></label>"
        )
    if setting.kind == KIND_CHOICE:
        options = "".join(
            f'<option value="{escape(choice)}"{" selected" if choice == value else ""}>{escape(label)}</option>'
            for choice, label in setting.choices
        )
        return f'{rendered}<select id="{identifier}" name="{key}">{options}</select>'
    if setting.kind in (KIND_INT, KIND_FLOAT):
        bounds = ""
        if setting.minimum is not None:
            bounds += f' min="{setting.minimum:g}"'
        if setting.maximum is not None:
            bounds += f' max="{setting.maximum:g}"'
        # Floats take any step: values set elsewhere (a zoomed overlay scale of
        # 1.32) must not make the browser refuse to submit the form.
        step = "1" if setting.kind == KIND_INT else "any"
        return (
            f'{rendered}<input type="number" id="{identifier}" name="{key}" value="{escape(text)}"'
            f'{bounds} step="{step}" required>'
        )
    attributes = ' autocomplete="off" spellcheck="false"'
    if setting.kind == KIND_LANGUAGE:
        attributes += f' list="{LANGUAGE_DATALIST_ID}" required'
    elif setting.kind == KIND_LANGUAGE_LIST:
        attributes += ' placeholder="ja,en" required'
    elif setting.kind == KIND_HOTKEY:
        attributes += ' placeholder="Ctrl+Alt+Q"'
    return f'{rendered}<input type="text" id="{identifier}" name="{key}" value="{escape(text)}"{attributes}>'


def lock_html(setting: Setting, snapshot: Settings) -> str:
    """What replaces the input of a setting pinned by its environment variable."""
    return (
        f'<div class="setting-lock">Fixado por <code>{escape(setting.env)}</code> = '
        f"<code>{escape(snapshot.text(setting.key))}</code>. Remova a variavel de ambiente para editar aqui.</div>"
    )


def row_html(setting: Setting, snapshot: Settings) -> str:
    pinned = snapshot.is_pinned(setting.key)
    chips = ""
    if setting.applies in APPLIES_LABELS:
        chips += f'<span class="chip is-cool">{escape(APPLIES_LABELS[setting.applies])}</span>'
    control = lock_html(setting, snapshot) if pinned else control_html(setting, snapshot)
    control_class = "setting-control is-switch" if setting.kind == KIND_BOOL and not pinned else "setting-control"
    return f"""
    <div class="setting-row">
      <div class="setting-text">
        <label class="setting-label" for="{field_id(setting)}">{escape(setting.label)}</label>
        <span class="setting-chips">{chips}</span>
        <p class="setting-help">{escape(setting.help)} Padrao: {escape(display_value(setting, setting.default))}.</p>
      </div>
      <div class="{control_class}">{control}</div>
    </div>"""


def section_form_html(section: Section, snapshot: Settings) -> str:
    """The form of one section: its settings, the advanced ones folded away."""
    entries = registry.section_settings(section.key)
    main_rows = "".join(row_html(setting, snapshot) for setting in entries if not setting.advanced)
    advanced = [setting for setting in entries if setting.advanced]
    advanced_html = ""
    if advanced:
        # Open when one of them is in use, so a changed value is never hidden.
        open_attribute = " open" if any(snapshot.is_changed(setting.key) for setting in advanced) else ""
        advanced_html = (
            f'<details class="setting-advanced"{open_attribute}><summary>Avancado ({len(advanced)})</summary>'
            + "".join(row_html(setting, snapshot) for setting in advanced)
            + "</details>"
        )
    label = escape(section.label)
    confirm = escape(f"Voltar todos os ajustes de {section.label} ao padrao?")
    return f"""
    <form class="settings-form" action="/actions/settings" method="post">
      <input type="hidden" name="section" value="{escape(section.key)}">
      {main_rows}
      {advanced_html}
      <div class="action-row">
        <button type="submit">Salvar {label}</button>
        <button type="submit" class="button-quiet" name="op" value="reset" formnovalidate
          onclick="return window.confirm('{confirm}');">Restaurar padroes</button>
      </div>
    </form>"""


def section_summary_html(section: Section, snapshot: Settings) -> str:
    """The values in use that differ from the default; the live part of a section."""
    changed = [
        setting for setting in registry.section_settings(section.key) if snapshot.is_changed(setting.key)
    ]
    if not changed:
        return '<p class="setting-summary muted">Tudo no padrao.</p>'
    items = "".join(
        f'<span class="chip">{escape(setting.label)}: {escape(display_value(setting, snapshot.get(setting.key)))}</span>'
        for setting in changed
    )
    return f'<p class="setting-summary"><span class="muted">Em uso:</span> {items}</p>'


def changed_count(snapshot: Settings) -> int:
    return sum(1 for setting in registry.REGISTRY if snapshot.is_changed(setting.key))


def section_index_html(snapshot: Settings) -> str:
    """Links to every section and how many settings are off their default."""
    links = "".join(
        f'<a class="secondary-link" href="#{escape(section.key)}">{escape(section.label)}</a>'
        for section in registry.SECTIONS
    )
    count = changed_count(snapshot)
    note = "Tudo no padrao" if count == 0 else f"{count} ajuste{'s' if count != 1 else ''} fora do padrao"
    return f'<nav class="section-index">{links}<span class="muted">{note}</span></nav>'


def language_datalist_html(language_options: Iterable[tuple[str, str]]) -> str:
    """Suggestions for the language fields: ``(code, name)`` pairs."""
    options = "".join(
        f'<option value="{escape(code)}">{escape(name)}</option>' for code, name in language_options
    )
    return f'<datalist id="{LANGUAGE_DATALIST_ID}">{options}</datalist>'


@dataclass(frozen=True)
class SettingsFormResult:
    section: Section
    message: str
    # The effective value (as stored text) of every setting of the section afterwards.
    values: dict[str, str]


def _last(form: Mapping[str, list[str]], name: str) -> str | None:
    values = form.get(name)
    return values[-1] if values else None


def apply_settings_form(
    connection: sqlite3.Connection,
    form: Mapping[str, list[str]],
) -> SettingsFormResult:
    """Save one section's form, or reset the section (``op=reset``). The caller commits.

    Only fields present in the form are considered, and of those only the ones
    whose value differs from the one the form was rendered with (``was:<key>``;
    a client that sends no such field changes every key it sends). Pinned
    settings are never written. Raises ``SettingError`` with nothing stored
    when a value is invalid.
    """
    section = registry.section_for(_last(form, "section") or "")
    snapshot = registry.load_settings(connection)
    editable = [
        setting for setting in registry.section_settings(section.key) if not snapshot.is_pinned(setting.key)
    ]
    if _last(form, "op") == "reset":
        registry.reset_settings(connection, (setting.key for setting in editable))
        message = f"{section.label}: ajustes de volta ao padrao."
    else:
        changes: dict[str, object] = {}
        for setting in editable:
            submitted = _last(form, setting.key)
            if submitted is None:
                continue
            value = registry.parse_value(setting, submitted)
            rendered = _last(form, RENDERED_VALUE_PREFIX + setting.key)
            if rendered is not None and registry.serialize_value(setting, value) == rendered:
                continue
            changes[setting.key] = value
        registry.set_settings(connection, changes)
        if changes:
            labels = ", ".join(registry.setting_for(key).label for key in changes)
            message = f"{section.label} salvo: {labels}."
        else:
            message = f"{section.label}: nada mudou."
    after = registry.load_settings(connection)
    return SettingsFormResult(
        section=section,
        message=message,
        values={setting.key: after.text(setting.key) for setting in registry.section_settings(section.key)},
    )


def settings_api_payload(snapshot: Settings) -> dict[str, object]:
    """Every setting with its value, where it comes from and how to edit it."""
    return {
        "sections": [
            {"key": section.key, "label": section.label, "help": section.help}
            for section in registry.SECTIONS
        ],
        "settings": [
            {
                "key": setting.key,
                "section": setting.section,
                "kind": setting.kind,
                "label": setting.label,
                "help": setting.help,
                "value": snapshot.get(setting.key),
                "default": setting.default,
                "source": snapshot.source(setting.key),
                "env": setting.env,
                "applies": setting.applies,
                "choices": [{"value": value, "label": label} for value, label in setting.choices],
                "minimum": setting.minimum,
                "maximum": setting.maximum,
                "advanced": setting.advanced,
            }
            for setting in registry.REGISTRY
        ],
    }


__all__ = [
    "SettingError",
    "SettingsFormResult",
    "apply_settings_form",
    "changed_count",
    "language_datalist_html",
    "section_form_html",
    "section_index_html",
    "section_summary_html",
    "settings_api_payload",
]
