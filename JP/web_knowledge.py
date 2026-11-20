"""How the web UI words and shows knowledge: labels, chips, the filter, the panel.

The data comes from ``knowledge``; the functions here take it as plain values
and return HTML or SQL fragments. ``lang_jp_web`` places them in its pages and
wires the route and the action, so no page spells out a status by itself.
"""

from __future__ import annotations

import html
from collections.abc import Callable, Mapping, Sequence

import knowledge
from knowledge import (
    MANUAL_PROVIDER,
    STATUS_KNOWN,
    STATUS_LEARNING,
    STATUS_MATURE,
    STATUS_UNKNOWN,
    STATUS_YOUNG,
)

FILTER_PARAM = "knowledge"
ACTION_PATH = "/actions/knowledge"
ACTION_MARK = "mark"
ACTION_CLEAR = "clear"

STATUS_LABELS = {
    STATUS_UNKNOWN: "Para aprender",
    STATUS_LEARNING: "Aprendendo",
    STATUS_YOUNG: "Recente",
    STATUS_MATURE: "Maduro",
    STATUS_KNOWN: "Conhecido",
}
PROVIDER_LABELS = {
    "anki": "Anki",
    MANUAL_PROVIDER: "Marcado por voce",
}

def escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def status_label(status: object) -> str:
    return STATUS_LABELS.get(str(status), str(status))


def provider_label(provider: object) -> str:
    return PROVIDER_LABELS.get(str(provider), str(provider))


def status_chip(status: object) -> str:
    name = str(status) if str(status) in STATUS_LABELS else STATUS_UNKNOWN
    return f'<span class="chip knowledge-chip knowledge-{name}">{escape(status_label(name))}</span>'


def status_cell(status: object, score: object) -> tuple[str, str]:
    """A table cell: the chip, sorted by score so the column orders weakest to strongest."""
    return status_chip(status), f"{float(score or 0):.4f}"  # type: ignore[arg-type]


def filter_value(raw: str) -> str | None:
    """The status a ``?knowledge=`` value asks for, or None for no filter."""
    value = raw.strip().lower()
    return value if value in knowledge.STATUSES else None


def filter_clause(status: str | None) -> tuple[str, list[object]]:
    """SQL over a ``v_*_overview`` view, like ``language_filter_clause``."""
    if status is None:
        return "", []
    return "knowledge_status = ?", [status]


def filter_select(selected: str | None) -> str:
    options = ['<option value="">Conhecimento: todos</option>']
    options.extend(
        f'<option value="{status}"{" selected" if status == selected else ""}>'
        f"{escape(STATUS_LABELS[status])}</option>"
        for status in knowledge.STATUSES
    )
    return f'<select name="{FILTER_PARAM}" aria-label="Conhecimento">{"".join(options)}</select>'


def format_days(value: object) -> str:
    if value is None:
        return "-"
    days = float(value)  # type: ignore[arg-type]
    return f"{days:.0f} d" if days >= 1 else "< 1 d"


def evidence_note(entry: Mapping[str, object]) -> str:
    """The provider-specific facts worth a word next to an evidence row."""
    detail = entry.get("detail")
    if not isinstance(detail, Mapping):
        return ""
    notes = []
    if detail.get("relearning"):
        notes.append("reaprendendo")
    if detail.get("suspended"):
        notes.append("suspenso")
    studied_cards = detail.get("studied_cards")
    if isinstance(studied_cards, int) and studied_cards > 1:
        notes.append(f"{studied_cards} cards")
    return f' <span class="muted">({escape(", ".join(notes))})</span>' if notes else ""


def panel_html(
    *,
    item_kind: str,
    item_id: int,
    status: object,
    score: object,
    evidence: Sequence[Mapping[str, object]],
    next_path: str,
    format_datetime: Callable[[object], str],
    render_table: Callable[[list[str], list[list[str]]], str],
) -> str:
    """The knowledge of one item: the verdict, what each source says, and the manual mark."""
    marked = any(entry["provider"] == MANUAL_PROVIDER for entry in evidence)
    if evidence:
        evidence_html = render_table(
            ["Fonte", "Status", "Estabilidade", "Revisoes", "Lapsos", "Ultima revisao", "Proxima"],
            [
                [
                    escape(provider_label(entry["provider"])) + evidence_note(entry),
                    status_chip(entry["status"]),
                    escape(format_days(entry["stability_days"])),
                    escape(entry["reviews"]),
                    escape(entry["lapses"]),
                    escape(format_datetime(entry["last_reviewed_at"])),
                    escape(format_datetime(entry["due_at"])),
                ]
                for entry in evidence
            ],
        )
    else:
        evidence_html = '<p class="muted">Nenhuma fonte de estudo tem dados sobre este item ainda.</p>'
    action, label = (
        (ACTION_CLEAR, "Remover marcacao de conhecido")
        if marked
        else (ACTION_MARK, "Marcar como conhecido")
    )
    return f"""
      <section class="panel panel-wide knowledge-panel">
        <h3>Conhecimento</h3>
        <div class="chips">
          {status_chip(status)}
          <span class="chip knowledge-chip">Forca: {float(score or 0) * 100:.0f}%</span>
        </div>
        {evidence_html}
        <form class="inline-action" action="{ACTION_PATH}" method="post">
          <input type="hidden" name="kind" value="{escape(item_kind)}">
          <input type="hidden" name="id" value="{int(item_id)}">
          <input type="hidden" name="op" value="{action}">
          <input type="hidden" name="next" value="{escape(next_path)}">
          <button type="submit">{label}</button>
        </form>
      </section>"""


def api_entry(
    status: object,
    score: object,
    evidence: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {"status": status, "score": score, "evidence": [dict(entry) for entry in evidence]}
