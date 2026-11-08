from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
import html
from html.parser import HTMLParser
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import unicodedata
import webbrowser
from collections.abc import Callable, Iterable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from uuid import uuid4

import lang_jp
import languages
from lang_jp import (
    ensure_runtime_schema,
    ensure_source_image_dimensions,
    format_source_resolution,
    language_filter_clause,
    list_language_rows,
    resolve_repo_local_path,
)
from languages import (
    ALL_LANGUAGES_CODE,
    AUTO_LANGUAGE_CODE,
    get_language,
    normalize_language_code,
)
from ocr_backends import plan_ocr_engines


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "lang_jp.sqlite3"
TERM_PRIORITY_ORDER_SQL = """
            COALESCE(priority_score, 0) DESC,
            julianday(COALESCE(last_seen_at, first_seen_at)) DESC,
            lemma COLLATE NOCASE ASC,
            id ASC
"""
FLASHCARD_PRIORITY_ORDER_SQL = """
            COALESCE(ts.priority_score, 0) DESC,
            julianday(COALESCE(ts.last_seen_at, ts.first_seen_at)) DESC,
            t.lemma COLLATE NOCASE ASC,
            f.id ASC
"""

PAGE_STYLE = """
<style>
  :root {
    --bg-top: #f7f0df;
    --bg-bottom: #e6dcc7;
    --panel: rgba(255, 251, 244, 0.94);
    --panel-strong: rgba(255, 247, 235, 0.98);
    --ink: #1f2421;
    --muted: #5f655f;
    --line: #d7c3a1;
    --accent: #b25c38;
    --accent-soft: #f3ddc8;
    --accent-cool: #335c67;
    --shadow: 0 18px 45px rgba(85, 61, 32, 0.14);
    --radius-lg: 22px;
    --radius-md: 14px;
    --font-ui: "Bahnschrift", "Trebuchet MS", sans-serif;
    --font-display: "Palatino Linotype", "Book Antiqua", serif;
  }

  * { box-sizing: border-box; }

  body {
    margin: 0;
    font-family: var(--font-ui);
    color: var(--ink);
    background:
      radial-gradient(circle at top left, rgba(255, 255, 255, 0.72), transparent 32%),
      linear-gradient(160deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
    min-height: 100vh;
  }

  a { color: var(--accent-cool); text-decoration: none; }
  a:hover { text-decoration: underline; }

  .shell {
    width: min(1200px, calc(100vw - 28px));
    margin: 28px auto 40px;
  }

  .hero {
    background: linear-gradient(135deg, rgba(255, 245, 230, 0.98), rgba(245, 231, 208, 0.9));
    border: 1px solid rgba(178, 92, 56, 0.24);
    box-shadow: var(--shadow);
    border-radius: 30px;
    padding: 26px 28px 22px;
    margin-bottom: 18px;
  }

  .hero h1 {
    margin: 0 0 8px;
    font-family: var(--font-display);
    font-size: clamp(2rem, 4vw, 3rem);
    line-height: 1.04;
    letter-spacing: 0.02em;
  }

  .hero p {
    margin: 0;
    color: var(--muted);
    max-width: 76ch;
    line-height: 1.55;
  }

  .nav {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    margin-top: 18px;
  }

  .nav a {
    display: inline-flex;
    align-items: center;
    min-height: 42px;
    padding: 0 14px;
    border-radius: 999px;
    border: 1px solid rgba(51, 92, 103, 0.15);
    background: rgba(255, 253, 249, 0.78);
    color: var(--ink);
    font-weight: 600;
  }

  .nav a.is-active {
    background: linear-gradient(135deg, var(--accent) 0%, #c6754d 100%);
    color: white;
    border-color: rgba(178, 92, 56, 0.3);
  }

  .grid {
    display: grid;
    grid-template-columns: repeat(12, minmax(0, 1fr));
    gap: 16px;
  }

  .panel {
    grid-column: span 12;
    background: var(--panel);
    border: 1px solid rgba(215, 195, 161, 0.9);
    border-radius: var(--radius-lg);
    box-shadow: var(--shadow);
    padding: 18px 18px 16px;
    overflow: hidden;
  }

  .panel h2, .panel h3 {
    margin: 0 0 12px;
    font-family: var(--font-display);
    letter-spacing: 0.01em;
  }

  .panel p { color: var(--muted); line-height: 1.55; }
  .panel-wide { grid-column: span 8; }
  .panel-side { grid-column: span 4; }

  .stats {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 12px;
  }

  .stat {
    background: linear-gradient(180deg, rgba(255, 248, 238, 0.96), rgba(244, 232, 212, 0.9));
    border-radius: var(--radius-md);
    padding: 14px 16px;
    border: 1px solid rgba(178, 92, 56, 0.16);
  }

  .stat .label {
    display: block;
    color: var(--muted);
    font-size: 0.9rem;
    margin-bottom: 8px;
  }

  .stat .value {
    font-size: 1.7rem;
    font-weight: 700;
  }

  .search-row {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 14px;
  }

  form.search {
    background: var(--panel-strong);
    border: 1px solid rgba(215, 195, 161, 0.9);
    border-radius: var(--radius-md);
    padding: 14px;
  }

  .search label {
    display: block;
    font-size: 0.88rem;
    color: var(--muted);
    margin-bottom: 8px;
    font-weight: 600;
  }

  .search .inputs,
  .filters {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }

  .search-actions {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
  }

  input[type="search"],
  input[type="number"],
  select {
    flex: 1 1 180px;
    min-height: 42px;
    border-radius: 999px;
    border: 1px solid rgba(51, 92, 103, 0.16);
    background: white;
    padding: 0 14px;
    font: inherit;
    color: var(--ink);
  }

  button,
  .button-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 42px;
    border-radius: 999px;
    border: 0;
    padding: 0 16px;
    font: inherit;
    font-weight: 700;
    cursor: pointer;
    color: white;
    background: linear-gradient(135deg, var(--accent) 0%, #ca7a4d 100%);
  }

  button[disabled] {
    cursor: wait;
    opacity: 0.78;
  }

  .button-link { text-decoration: none; }
  .button-link:hover { text-decoration: none; }

  .secondary-link,
  .pagination-link {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 42px;
    padding: 0 16px;
    border-radius: 999px;
    border: 1px solid rgba(51, 92, 103, 0.16);
    background: rgba(255, 252, 247, 0.92);
    color: var(--accent-cool);
    font-weight: 600;
    text-decoration: none;
  }

  .secondary-link:hover,
  .pagination-link:hover {
    text-decoration: none;
    border-color: rgba(51, 92, 103, 0.3);
    background: rgba(255, 248, 239, 0.98);
  }

  .pagination-link.is-current {
    border-color: rgba(178, 92, 56, 0.28);
    background: linear-gradient(135deg, rgba(178, 92, 56, 0.14), rgba(202, 122, 77, 0.2));
    color: var(--accent);
  }

  .pagination-link.is-disabled {
    color: rgba(95, 101, 95, 0.7);
    border-style: dashed;
    background: rgba(255, 252, 247, 0.72);
  }

  .pagination-gap {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-height: 42px;
    padding: 0 4px;
    color: var(--muted);
  }

  .chips {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
  }

  .lang-bar {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    align-items: center;
    margin-top: 14px;
  }

  .lang-bar .lang-label {
    font-size: 0.82rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted, #6b6b6b);
    margin-right: 4px;
  }

  .lang-bar a {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    min-height: 32px;
    padding: 0 12px;
    border-radius: 999px;
    border: 1px solid rgba(51, 92, 103, 0.18);
    background: rgba(255, 253, 249, 0.85);
    color: var(--ink);
    font-size: 0.9rem;
    font-weight: 600;
  }

  .lang-bar a .count {
    font-weight: 400;
    opacity: 0.7;
    font-size: 0.8rem;
  }

  .lang-bar a.is-active {
    background: var(--ink, #1f2a2e);
    color: white;
    border-color: transparent;
  }

  .lang-bar a.lang-settings {
    margin-left: auto;
    border-style: dashed;
    font-weight: 500;
  }

  .stats-compact { grid-template-columns: repeat(3, minmax(0, 1fr)); margin-bottom: 12px; }
  .stats-compact .stat { padding: 10px 12px; }
  .stats-compact .stat .value { font-size: 1.3rem; }
  .lang-card h2 { display: flex; align-items: center; gap: 8px; }
  .lang-card h2 a { text-decoration: none; color: inherit; }

  .capture-language-grid {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(190px, 1fr));
    gap: 6px 14px;
    margin: 12px 0 14px;
  }

  .capture-language { display: flex; align-items: center; gap: 8px; }
  .capture-language input { width: 16px; height: 16px; }
  .source-language-form { margin-top: 14px; flex-wrap: wrap; gap: 12px; align-items: center; }
  .source-language-form label { display: inline-flex; align-items: center; gap: 6px; }
  input[type="text"] {
    min-height: 38px;
    border-radius: var(--radius-md, 10px);
    border: 1px solid rgba(51, 92, 103, 0.24);
    padding: 0 12px;
    font: inherit;
    background: rgba(255, 253, 249, 0.9);
  }

  .chip {
    display: inline-flex;
    align-items: center;
    min-height: 30px;
    padding: 0 10px;
    border-radius: 999px;
    background: var(--accent-soft);
    color: #7d4528;
    font-size: 0.88rem;
    font-weight: 700;
  }

  .meta-list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
    gap: 10px;
    margin: 0;
    padding: 0;
    list-style: none;
  }

  .meta-list li {
    background: rgba(255, 252, 247, 0.82);
    border: 1px solid rgba(215, 195, 161, 0.75);
    border-radius: var(--radius-md);
    padding: 12px 14px;
  }

  .meta-list strong {
    display: block;
    margin-bottom: 6px;
  }

  table { width: 100%; border-collapse: collapse; }

  th,
  td {
    text-align: left;
    vertical-align: top;
    padding: 10px 12px;
    border-bottom: 1px solid rgba(215, 195, 161, 0.7);
  }

  th {
    color: var(--muted);
    font-size: 0.84rem;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }

  th[aria-sort="ascending"],
  th[aria-sort="descending"] {
    color: var(--accent-cool);
  }

  .sort-toggle {
    display: inline-flex;
    align-items: center;
    gap: 8px;
    min-height: auto;
    width: 100%;
    padding: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    color: inherit;
    font: inherit;
    font-weight: inherit;
    letter-spacing: inherit;
    text-transform: inherit;
    justify-content: flex-start;
  }

  .sort-toggle::after {
    content: "↕";
    font-size: 0.86rem;
    color: rgba(95, 101, 95, 0.7);
  }

  th[aria-sort="ascending"] .sort-toggle::after {
    content: "↑";
    color: var(--accent);
  }

  th[aria-sort="descending"] .sort-toggle::after {
    content: "↓";
    color: var(--accent);
  }

  tbody tr:hover { background: rgba(255, 245, 230, 0.48); }

  .empty {
    padding: 18px;
    border-radius: var(--radius-md);
    background: rgba(255, 252, 247, 0.8);
    border: 1px dashed rgba(215, 195, 161, 0.9);
    color: var(--muted);
  }

  /* Catalog text carries lang= (see language_attrs), so every script gets a
     font that has it and Japanese kanji are not drawn with Chinese shapes. */
  [lang|="ja"] { font-family: "Bahnschrift", "Trebuchet MS", "Yu Gothic UI", "Yu Gothic", "Meiryo", "Hiragino Sans", "Noto Sans CJK JP", sans-serif; }
  [lang|="zh"] { font-family: "Bahnschrift", "Trebuchet MS", "Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", sans-serif; }
  [lang|="ko"] { font-family: "Bahnschrift", "Trebuchet MS", "Malgun Gothic", "Apple SD Gothic Neo", "Noto Sans CJK KR", sans-serif; }
  [lang|="th"] { font-family: "Bahnschrift", "Trebuchet MS", "Leelawadee UI", "Leelawadee", "Tahoma", "Noto Sans Thai", sans-serif; }
  [lang|="hi"] { font-family: "Bahnschrift", "Trebuchet MS", "Nirmala UI", "Mangal", "Noto Sans Devanagari", sans-serif; }
  [lang|="ar"] { font-family: "Segoe UI", "Tahoma", "Noto Naskh Arabic", sans-serif; }
  [lang|="he"] { font-family: "Segoe UI", "Arial", "Noto Sans Hebrew", sans-serif; }

  .muted { color: var(--muted); }
  .mono { font-family: "Consolas", "Courier New", monospace; word-break: break-word; }
  .stack { display: grid; gap: 14px; }

  pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    padding: 16px;
    border-radius: var(--radius-md);
    background: rgba(255, 251, 246, 0.95);
    border: 1px solid rgba(215, 195, 161, 0.8);
    color: #3d433e;
    font-family: "Consolas", "Courier New", monospace;
  }

  pre[lang] { font-family: "Consolas", "Courier New", monospace; }
  /* Each line of a mixed-language text picks its own direction (Arabic, Hebrew). */
  pre[dir="auto"] { unicode-bidi: plaintext; }

  img.source-image {
    display: block;
    width: 100%;
    border-radius: var(--radius-md);
    border: 1px solid rgba(215, 195, 161, 0.9);
    background: white;
  }

  .footer-note {
    margin-top: 16px;
    color: var(--muted);
    font-size: 0.92rem;
  }

  .results-toolbar {
    display: flex;
    gap: 14px;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    margin: 14px 0 16px;
  }

  .results-summary {
    margin: 0;
    color: var(--muted);
  }

  .pagination {
    display: flex;
    gap: 10px;
    align-items: center;
    flex-wrap: wrap;
    justify-content: flex-end;
  }

  .pagination-status {
    color: var(--muted);
    font-size: 0.92rem;
  }

  .pagination-links {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }

  .notice-banner {
    margin-bottom: 16px;
    padding: 14px 16px;
    border-radius: var(--radius-md);
    border: 1px solid rgba(51, 92, 103, 0.2);
    background: rgba(255, 252, 247, 0.92);
  }

  .notice-banner.is-error {
    border-color: rgba(178, 44, 44, 0.28);
    background: rgba(255, 236, 232, 0.95);
    color: #7b2d1f;
  }

  .action-row {
    display: flex;
    gap: 10px;
    flex-wrap: wrap;
    align-items: center;
    margin: 14px 0 10px;
  }

  form.inline-action {
    margin: 0;
  }

  .anki-sync-root {
    display: grid;
    gap: 10px;
  }

  .anki-sync-progress {
    display: grid;
    gap: 8px;
    width: min(720px, 100%);
    padding: 12px 14px;
    border-radius: var(--radius-md);
    border: 1px solid rgba(51, 92, 103, 0.16);
    background: rgba(255, 252, 247, 0.9);
  }

  .anki-sync-progress[hidden] {
    display: none;
  }

  .anki-sync-progress-head {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }

  .anki-sync-progress-title {
    font-weight: 700;
  }

  .anki-sync-progress-value {
    color: var(--accent-cool);
    font-weight: 700;
  }

  .anki-sync-progress-track {
    position: relative;
    height: 12px;
    overflow: hidden;
    border-radius: 999px;
    background: rgba(51, 92, 103, 0.12);
  }

  .anki-sync-progress-fill {
    position: relative;
    height: 100%;
    width: 0%;
    border-radius: inherit;
    background: linear-gradient(90deg, var(--accent-cool) 0%, var(--accent) 100%);
    transition: width 0.35s ease;
  }

  .anki-sync-root.is-running .anki-sync-progress-fill::after {
    content: "";
    position: absolute;
    inset: 0;
    background: linear-gradient(120deg, transparent 20%, rgba(255, 255, 255, 0.45) 50%, transparent 80%);
    transform: translateX(-100%);
    animation: anki-sync-sheen 1.35s linear infinite;
  }

  .anki-sync-root.is-complete .anki-sync-progress-fill {
    background: linear-gradient(90deg, #335c67 0%, #5b8c73 100%);
  }

  .anki-sync-root.is-error .anki-sync-progress {
    border-color: rgba(178, 44, 44, 0.24);
    background: rgba(255, 236, 232, 0.92);
  }

  .anki-sync-root.is-error .anki-sync-progress-fill {
    background: linear-gradient(90deg, #a64331 0%, #cf7754 100%);
  }

  .anki-sync-progress-message {
    color: var(--muted);
    line-height: 1.45;
  }

  @keyframes anki-sync-sheen {
    from { transform: translateX(-100%); }
    to { transform: translateX(100%); }
  }

  @media (max-width: 900px) {
    .panel-wide, .panel-side { grid-column: span 12; }
  }
</style>
"""

PAGE_SCRIPT = """
<script>
  (() => {
    function normalizeValue(raw) {
      return (raw || "").replace(/\\s+/g, " ").trim();
    }

    function detectType(values) {
      for (const value of values) {
        if (!value || value === "-") {
          continue;
        }
        const numeric = value.replace(/,/g, "").replace(/^#/, "");
        if (/^-?\\d+(?:\\.\\d+)?$/.test(numeric)) {
          return "number";
        }
        if (/^\\d{4}-\\d{2}-\\d{2}(?:[ T]\\d{2}:\\d{2}(?::\\d{2})?)?$/.test(value)) {
          return "date";
        }
        return "text";
      }
      return "text";
    }

    function sortableText(value) {
      // Columns mix languages, so lower-case without a locale (a pt-BR or tr
      // rule would be wrong for the others) and fold accents only on Latin
      // letters, so "água" sorts next to "agua" instead of after "z". Kana,
      // hangul, Devanagari and every other script keep their exact text.
      const lowered = value.toLowerCase();
      if (!/[\\u00c0-\\u024f\\u1e00-\\u1eff\\u0300-\\u036f]/.test(lowered)) {
        return lowered;
      }
      return lowered
        .normalize("NFD")
        .replace(/([a-z])[\\u0300-\\u036f]+/g, "$1")
        .normalize("NFC");
    }

    function comparableValue(value, type) {
      if (!value || value === "-") {
        return type === "text" ? "" : Number.NEGATIVE_INFINITY;
      }
      if (type === "number") {
        return Number(value.replace(/,/g, "").replace(/^#/, ""));
      }
      if (type === "date") {
        return Date.parse(value.replace(" ", "T")) || 0;
      }
      return sortableText(value);
    }

    function updateHeaderState(headers, activeIndex, direction) {
      headers.forEach((header, index) => {
        header.setAttribute(
          "aria-sort",
          index === activeIndex
            ? (direction === "asc" ? "ascending" : "descending")
            : "none"
        );
      });
    }

    function sortTable(table, headers, tbody, index, direction) {
      const rows = Array.from(tbody.querySelectorAll("tr")).map((row, rowIndex) => ({
        row,
        rowIndex,
        value: normalizeValue(
          row.cells[index]?.dataset.sortValue || row.cells[index]?.textContent || ""
        ),
      }));
      const type = detectType(rows.map((item) => item.value));

      headers.forEach((otherHeader, otherIndex) => {
        const otherButton = otherHeader.querySelector(".sort-toggle");
        if (!otherButton) {
          return;
        }
        otherButton.dataset.sortDirection = otherIndex === index ? direction : "none";
      });
      updateHeaderState(headers, index, direction);

      rows.sort((left, right) => {
        const leftValue = comparableValue(left.value, type);
        const rightValue = comparableValue(right.value, type);

        if (leftValue < rightValue) {
          return direction === "asc" ? -1 : 1;
        }
        if (leftValue > rightValue) {
          return direction === "asc" ? 1 : -1;
        }
        return left.rowIndex - right.rowIndex;
      });

      tbody.replaceChildren(...rows.map((item) => item.row));
    }

    function readSortState(table) {
      if (!table) {
        return null;
      }
      const headers = Array.from(table.querySelectorAll("thead th"));
      for (let index = 0; index < headers.length; index += 1) {
        const direction = headers[index].getAttribute("aria-sort");
        if (direction === "ascending") {
          return { index, direction: "asc" };
        }
        if (direction === "descending") {
          return { index, direction: "desc" };
        }
      }
      return null;
    }

    function applySortState(table, state) {
      if (!table || !state || !Number.isInteger(state.index)) {
        return false;
      }
      const headers = Array.from(table.querySelectorAll("thead th"));
      const tbody = table.querySelector("tbody");
      if (!headers.length || !tbody || !headers[state.index]) {
        return false;
      }
      const direction = state.direction === "asc" ? "asc" : state.direction === "desc" ? "desc" : "";
      if (!direction) {
        return false;
      }
      sortTable(table, headers, tbody, state.index, direction);
      return true;
    }

    function initializeSortableTable(table) {
      if (table.dataset.sortReady === "1") {
        return;
      }
      const headers = Array.from(table.querySelectorAll("thead th"));
      const tbody = table.querySelector("tbody");
      if (!headers.length || !tbody) {
        return;
      }

      headers.forEach((header, index) => {
        const button = header.querySelector(".sort-toggle");
        if (!button) {
          return;
        }

        button.addEventListener("click", () => {
          const previousDirection = button.dataset.sortDirection || "none";
          let nextDirection;

          if (previousDirection === "asc") {
            nextDirection = "desc";
          } else if (previousDirection === "desc") {
            nextDirection = "asc";
          } else {
            const values = Array.from(tbody.querySelectorAll("tr")).map((row) =>
              normalizeValue(
                row.cells[index]?.dataset.sortValue || row.cells[index]?.textContent || ""
              )
            );
            nextDirection = detectType(values) === "text" ? "asc" : "desc";
          }
          sortTable(table, headers, tbody, index, nextDirection);
        });
      });

      table.dataset.sortReady = "1";
    }

    function initSortableTables(root = document) {
      root.querySelectorAll("table.sortable-table").forEach((table) => {
        initializeSortableTable(table);
      });
    }

    window.jpCatalog = Object.assign(window.jpCatalog || {}, {
      initSortableTables,
      readSortState,
      applySortState,
    });

    initSortableTables(document);
  })();

  (() => {
    const pollIntervalMs = 900;
    const syncState = {
      active: false,
    };

    function setSyncingAnki(nextActive) {
      syncState.active = Boolean(nextActive);
    }

    function isSyncingAnki() {
      return syncState.active;
    }

    function appendNoticeToPath(path, message, level) {
      const url = new URL(path || "/", window.location.origin);
      url.searchParams.set("notice", message);
      url.searchParams.set("level", level);
      return `${url.pathname}${url.search}${url.hash}`;
    }

    function syncStatusTitle(status) {
      if (status === "completed") {
        return "Sincronizacao concluida";
      }
      if (status === "failed") {
        return "Falha na sincronizacao";
      }
      return "Sincronizando com o Anki";
    }

    function setRootState(root, status) {
      root.classList.toggle("is-running", status === "running");
      root.classList.toggle("is-complete", status === "completed");
      root.classList.toggle("is-error", status === "failed");
    }

    function updateProgressUI(root, payload) {
      const panel = root.querySelector("[data-anki-sync-progress]");
      const titleNode = root.querySelector("[data-anki-sync-title]");
      const valueNode = root.querySelector("[data-anki-sync-value]");
      const messageNode = root.querySelector("[data-anki-sync-message]");
      const fillNode = root.querySelector("[data-anki-sync-fill]");
      if (!panel || !titleNode || !valueNode || !messageNode || !fillNode) {
        return;
      }

      const progress = Math.max(0, Math.min(100, Number(payload.progress) || 0));
      panel.hidden = false;
      panel.setAttribute("aria-valuenow", String(Math.round(progress)));
      titleNode.textContent = syncStatusTitle(payload.status);
      valueNode.textContent = `${Math.round(progress)}%`;
      messageNode.textContent = payload.message || "";
      fillNode.style.width = `${progress}%`;
      setRootState(root, payload.status);
    }

    async function startSync(root, form, submitButton) {
      if (root.dataset.syncBusy === "1") {
        return;
      }

      root.dataset.syncBusy = "1";
      submitButton.disabled = true;
      submitButton.textContent = "Sincronizando...";
      setSyncingAnki(true);
      updateProgressUI(root, {
        status: "running",
        progress: 4,
        message: "Preparando sincronizacao com o Anki...",
      });

      const formData = new FormData(form);
      const nextPath = String(formData.get("next") || window.location.pathname || "/");

      try {
        const startResponse = await fetch(form.action, {
          method: "POST",
          headers: {
            "Accept": "application/json",
            "X-Requested-With": "fetch",
          },
          body: new URLSearchParams(formData),
          cache: "no-store",
        });
        const startPayload = await startResponse.json();
        if (!startResponse.ok || !startPayload || !startPayload.job_id) {
          throw new Error(startPayload && startPayload.message ? startPayload.message : "Nao foi possivel iniciar a sincronizacao.");
        }

        updateProgressUI(root, startPayload);
        const pollUrl = startPayload.poll_url;
        let completed = false;

        while (!completed) {
          await new Promise((resolve) => window.setTimeout(resolve, pollIntervalMs));
          const statusResponse = await fetch(pollUrl, {
            headers: { "Accept": "application/json" },
            cache: "no-store",
          });
          const statusPayload = await statusResponse.json();
          if (!statusResponse.ok) {
            throw new Error(statusPayload && statusPayload.message ? statusPayload.message : "Nao foi possivel consultar o andamento da sincronizacao.");
          }
          updateProgressUI(root, statusPayload);
          if (statusPayload.status === "completed" || statusPayload.status === "failed") {
            completed = true;
            const noticeMessage = statusPayload.notice_message || statusPayload.message || "Sincronizacao finalizada.";
            const noticeLevel = statusPayload.notice_level || (statusPayload.status === "failed" ? "error" : "info");
            window.setTimeout(() => {
              window.location.assign(appendNoticeToPath(nextPath, noticeMessage, noticeLevel));
            }, 320);
          }
        }
      } catch (error) {
        const message = error instanceof Error ? error.message : "Falha ao sincronizar com o Anki.";
        updateProgressUI(root, {
          status: "failed",
          progress: 100,
          message,
        });
        root.dataset.syncBusy = "0";
        submitButton.disabled = false;
        submitButton.textContent = "Sincronizar com o Anki";
        setSyncingAnki(false);
      }
    }

    function initAnkiSync(root) {
      if (!root || root.dataset.syncReady === "1") {
        return;
      }
      const form = root.querySelector("[data-anki-sync-form]");
      const submitButton = root.querySelector("[data-anki-sync-submit]");
      if (!form || !submitButton) {
        return;
      }

      form.addEventListener("submit", (event) => {
        event.preventDefault();
        startSync(root, form, submitButton);
      });

      root.dataset.syncReady = "1";
    }

    document.querySelectorAll("[data-anki-sync-root]").forEach((root) => {
      initAnkiSync(root);
    });

    window.jpCatalog = Object.assign(window.jpCatalog || {}, {
      isSyncingAnki,
      setSyncingAnki,
    });
  })();
</script>
"""


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback) -> bool | None:
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


def db_connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path, timeout=30, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 30000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.create_function("search_fold", 1, search_fold, deterministic=True)
    connection.create_function("html_text", 1, html_fragment_text, deterministic=True)
    ensure_runtime_schema(connection)
    return connection


def database_revision_token(db_path: Path) -> str:
    parts: list[str] = []
    for candidate in (db_path, db_path.parent / f"{db_path.name}-wal"):
        if not candidate.exists():
            parts.append(f"{candidate.name}:missing")
            continue
        stat = candidate.stat()
        parts.append(f"{candidate.name}:{stat.st_size}:{stat.st_mtime_ns}")
    return "|".join(parts)


def live_revision_payload(db_path: Path) -> dict[str, str]:
    return {"revision": database_revision_token(db_path)}


SYNC_JOB_TTL_SECONDS = 30 * 60
SYNC_JOB_LOCK = threading.Lock()
SYNC_JOB_STATES: dict[str, dict[str, object]] = {}


def build_notice_path(path: str, *, message: str, level: str) -> str:
    parsed = urlparse(path or "/")
    query = parse_qs(parsed.query)
    query["notice"] = [message]
    query["level"] = [level]
    encoded = urlencode(
        [(key, value) for key, values in query.items() for value in values]
    )
    target = parsed.path or "/"
    return f"{target}?{encoded}" if encoded else target


def build_catalog_command(db_path: Path, *command_parts: str) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(ROOT / "lang_jp.py"),
        "--db",
        str(db_path),
        *command_parts,
    ]


def run_catalog_command_for_db(
    db_path: Path,
    *command_parts: str,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        build_catalog_command(db_path, *command_parts),
        cwd=str(ROOT.parent),
        capture_output=True,
        text=True,
    )


def stream_catalog_command_for_db(
    db_path: Path,
    *command_parts: str,
    on_output: Callable[[str], None] | None = None,
) -> tuple[int, list[str]]:
    process = subprocess.Popen(
        build_catalog_command(db_path, *command_parts),
        cwd=str(ROOT.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    lines: list[str] = []
    if process.stdout is not None:
        try:
            for raw_line in process.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                lines.append(line)
                if on_output is not None:
                    on_output(line)
        finally:
            process.stdout.close()
    return process.wait(), lines


def command_output_lines(*, stdout: str, stderr: str) -> list[str]:
    return [
        line.strip()
        for line in (stdout + "\n" + stderr).splitlines()
        if line.strip()
    ]


def sync_result_message(lines: list[str], fallback: str) -> str:
    return next(
        (line for line in reversed(lines) if line.startswith("Synced ")),
        fallback,
    )


def sync_failure_message(lines: list[str], fallback: str) -> str:
    return lines[-1] if lines else fallback


def prune_sync_jobs(*, now: float | None = None) -> None:
    cutoff = (now if now is not None else time.time()) - SYNC_JOB_TTL_SECONDS
    stale_job_ids = [
        job_id
        for job_id, state in SYNC_JOB_STATES.items()
        if state.get("status") != "running"
        and float(state.get("updated_at") or 0.0) < cutoff
    ]
    for job_id in stale_job_ids:
        SYNC_JOB_STATES.pop(job_id, None)


def sync_job_payload(state: dict[str, object]) -> dict[str, object]:
    job_id = str(state["job_id"])
    return {
        "job_id": job_id,
        "status": str(state.get("status") or "running"),
        "step": str(state.get("step") or "queued"),
        "progress": int(state.get("progress") or 0),
        "message": str(state.get("message") or ""),
        "notice_message": str(state.get("notice_message") or ""),
        "notice_level": str(state.get("notice_level") or "info"),
        "poll_url": f"/actions/sync-anki/status?{urlencode([('job_id', job_id)])}",
    }


def get_sync_job_state(job_id: str) -> dict[str, object] | None:
    with SYNC_JOB_LOCK:
        prune_sync_jobs()
        state = SYNC_JOB_STATES.get(job_id)
        return dict(state) if state is not None else None


def set_sync_job_state(job_id: str, **changes: object) -> None:
    with SYNC_JOB_LOCK:
        state = SYNC_JOB_STATES.get(job_id)
        if state is None:
            return
        state.update(changes)
        state["updated_at"] = time.time()


def advance_sync_job(
    job_id: str,
    *,
    progress: int,
    message: str,
    status: str | None = None,
    step: str | None = None,
    notice_message: str | None = None,
    notice_level: str | None = None,
) -> None:
    current_state = get_sync_job_state(job_id)
    current_progress = 0
    if current_state is not None:
        try:
            current_progress = int(current_state.get("progress") or 0)
        except (TypeError, ValueError):
            current_progress = 0
    changes: dict[str, object] = {
        "progress": max(progress, current_progress),
        "message": message,
    }
    if status is not None:
        changes["status"] = status
    if step is not None:
        changes["step"] = step
    if notice_message is not None:
        changes["notice_message"] = notice_message
    if notice_level is not None:
        changes["notice_level"] = notice_level
    set_sync_job_state(job_id, **changes)


def run_sync_anki_job(db_path: Path, job_id: str) -> None:
    term_lines: list[str] = []
    kanji_lines: list[str] = []

    try:
        advance_sync_job(
            job_id,
            progress=10,
            step="sync_terms",
            message="Sincronizando termos com o Anki...",
        )

        def handle_term_output(line: str) -> None:
            progress = 24
            if line.startswith("Prepared flashcards:"):
                progress = 38
            elif line.startswith("Synced "):
                progress = 56
            advance_sync_job(job_id, progress=progress, step="sync_terms", message=line)

        term_returncode, term_lines = stream_catalog_command_for_db(
            db_path,
            "sync-anki-words",
            on_output=handle_term_output,
        )
        if term_returncode != 0:
            message = sync_failure_message(
                term_lines,
                "Falha ao sincronizar termos com o Anki.",
            )
            advance_sync_job(
                job_id,
                progress=100,
                status="failed",
                step="failed",
                message=message,
                notice_message=message,
                notice_level="error",
            )
            return

        term_message = sync_result_message(term_lines, "Termos sincronizados.")
        advance_sync_job(
            job_id,
            progress=62,
            step="sync_kanji",
            message=f"{term_message} Iniciando sincronizacao de kanji...",
        )

        def handle_kanji_output(line: str) -> None:
            progress = 74
            if line.startswith("Prepared kanji flashcards:"):
                progress = 84
            elif line.startswith("Synced "):
                progress = 96
            advance_sync_job(job_id, progress=progress, step="sync_kanji", message=line)

        kanji_returncode, kanji_lines = stream_catalog_command_for_db(
            db_path,
            "sync-anki-kanji",
            on_output=handle_kanji_output,
        )
        if kanji_returncode != 0:
            message = sync_failure_message(
                kanji_lines,
                "Falha ao sincronizar kanji com o Anki.",
            )
            advance_sync_job(
                job_id,
                progress=100,
                status="failed",
                step="failed",
                message=message,
                notice_message=message,
                notice_level="error",
            )
            return

        kanji_message = sync_result_message(kanji_lines, "Kanji sincronizados.")
        final_message = f"{term_message} {kanji_message}"
        advance_sync_job(
            job_id,
            progress=100,
            status="completed",
            step="completed",
            message=final_message,
            notice_message=final_message,
            notice_level="info",
        )
    except Exception as exc:  # pragma: no cover - defensive fallback
        message = str(exc).strip() or "Falha inesperada ao sincronizar com o Anki."
        advance_sync_job(
            job_id,
            progress=100,
            status="failed",
            step="failed",
            message=message,
            notice_message=message,
            notice_level="error",
        )


def start_sync_anki_job(db_path: Path) -> dict[str, object]:
    with SYNC_JOB_LOCK:
        prune_sync_jobs()
        active_state = next(
            (
                dict(state)
                for state in SYNC_JOB_STATES.values()
                if state.get("status") == "running"
            ),
            None,
        )
        if active_state is not None:
            payload = sync_job_payload(active_state)
            payload["existing"] = True
            return payload

        job_id = uuid4().hex
        state = {
            "job_id": job_id,
            "status": "running",
            "step": "queued",
            "progress": 4,
            "message": "Preparando sincronizacao com o Anki...",
            "notice_message": "",
            "notice_level": "info",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        SYNC_JOB_STATES[job_id] = state

    worker = threading.Thread(
        target=run_sync_anki_job,
        args=(db_path, job_id),
        daemon=True,
        name="anki-sync-job",
    )
    worker.start()

    payload = sync_job_payload(state)
    payload["existing"] = False
    return payload


def first_value(params: dict[str, list[str]], key: str, default: str = "") -> str:
    value = params.get(key, [default])
    return value[0] if value else default


def parse_limit(raw_value: str, default: int = 50, *, maximum: int = 200) -> int:
    try:
        value = int(raw_value or default)
    except ValueError:
        return default
    return max(1, min(value, maximum))


def parse_page(raw_value: str, default: int = 1) -> int:
    try:
        value = int(raw_value or default)
    except ValueError:
        return default
    return max(1, value)


@dataclass(frozen=True)
class PaginationState:
    page: int
    limit: int
    total_count: int
    total_pages: int
    offset: int
    page_start: int
    page_end: int

    @property
    def has_previous(self) -> bool:
        return self.page > 1

    @property
    def has_next(self) -> bool:
        return self.page < self.total_pages


def build_query_path(path: str, params: dict[str, object | None]) -> str:
    query_parts: list[tuple[str, str]] = []
    for key, value in params.items():
        if value is None:
            continue
        text_value = str(value).strip()
        if not text_value:
            continue
        query_parts.append((key, text_value))
    encoded = urlencode(query_parts)
    return f"{path}?{encoded}" if encoded else path


def build_pagination(*, total_count: int, limit: int, page: int) -> PaginationState:
    total_pages = max(1, (total_count + limit - 1) // limit) if total_count else 1
    safe_page = min(max(1, page), total_pages)
    offset = (safe_page - 1) * limit
    if total_count:
        page_start = offset + 1
        page_end = min(offset + limit, total_count)
    else:
        page_start = 0
        page_end = 0
    return PaginationState(
        page=safe_page,
        limit=limit,
        total_count=total_count,
        total_pages=total_pages,
        offset=offset,
        page_start=page_start,
        page_end=page_end,
    )


def pagination_window(pagination: PaginationState) -> list[int | None]:
    if pagination.total_pages <= 7:
        return list(range(1, pagination.total_pages + 1))

    pages = {1, pagination.total_pages}
    for candidate in range(pagination.page - 1, pagination.page + 2):
        if 1 <= candidate <= pagination.total_pages:
            pages.add(candidate)

    items: list[int | None] = []
    previous_page = None
    for page_number in sorted(pages):
        if previous_page is not None and page_number - previous_page > 1:
            items.append(None)
        items.append(page_number)
        previous_page = page_number
    return items


def pagination_link_html(
    label: str,
    *,
    href: str | None = None,
    current: bool = False,
    disabled: bool = False,
) -> str:
    classes = ["pagination-link"]
    if current:
        classes.append("is-current")
    if disabled:
        classes.append("is-disabled")
    class_attr = " ".join(classes)
    if current:
        return f'<span class="{class_attr}" aria-current="page">{html.escape(label)}</span>'
    if disabled or not href:
        return f'<span class="{class_attr}" aria-disabled="true">{html.escape(label)}</span>'
    return f'<a class="{class_attr}" href="{html.escape(href, quote=True)}">{html.escape(label)}</a>'


def pagination_nav(
    *,
    path: str,
    base_params: dict[str, object | None],
    pagination: PaginationState,
) -> str:
    if pagination.total_pages <= 1:
        return ""

    def page_href(page_number: int) -> str:
        params = dict(base_params)
        params["page"] = page_number if page_number > 1 else None
        return build_query_path(path, params)

    link_parts = [
        pagination_link_html(
            "Anterior",
            href=page_href(pagination.page - 1) if pagination.has_previous else None,
            disabled=not pagination.has_previous,
        )
    ]
    for item in pagination_window(pagination):
        if item is None:
            link_parts.append('<span class="pagination-gap" aria-hidden="true">...</span>')
            continue
        link_parts.append(
            pagination_link_html(
                str(item),
                href=page_href(item),
                current=item == pagination.page,
            )
        )
    link_parts.append(
        pagination_link_html(
            "Proxima",
            href=page_href(pagination.page + 1) if pagination.has_next else None,
            disabled=not pagination.has_next,
        )
    )
    return f"""
    <nav class="pagination" aria-label="Paginacao">
      <span class="pagination-status">Pagina {pagination.page} de {pagination.total_pages}</span>
      <div class="pagination-links">{''.join(link_parts)}</div>
    </nav>
    """


DETAIL_TABLE_LIMIT = 100


def truncation_note(shown: int, total: int) -> str:
    if total <= shown:
        return ""
    return f'<p class="muted">Mostrando {shown} de {total}.</p>'


def results_summary_text(pagination: PaginationState) -> str:
    if pagination.total_count == 0:
        return "Nenhum resultado encontrado."
    result_label = "resultado" if pagination.total_count == 1 else "resultados"
    return (
        f"Mostrando {pagination.page_start} a {pagination.page_end} "
        f"de {pagination.total_count} {result_label}."
    )


def sql_like(value: str) -> str:
    return f"%{value.strip()}%"


def search_fold(value: object | None) -> str:
    """Case-insensitive search key for text in any language.

    SQLite's ``LIKE`` only ignores case for ASCII, so ``Água``, ``ПРИВЕТ`` or
    ``Ελλάδα`` never matched their lower-case forms. ``casefold`` covers every
    cased script and leaves kana, kanji, hangul, Arabic, Devanagari and Thai
    untouched; NFC makes precomposed and decomposed input compare equal. No
    accent folding: stripping combining marks would break dakuten, matras and
    vowel points in the non-Latin scripts.
    """
    if value is None:
        return ""
    return unicodedata.normalize("NFC", str(value)).casefold()


def folded_like_filter(columns: Iterable[str], q: str) -> tuple[str, list[object]]:
    """``(sql, params)`` matching ``q`` as a substring of any column, case-insensitively."""
    column_list = list(columns)
    pattern = sql_like(search_fold(q))
    sql = " OR ".join(f"search_fold({column}) LIKE ?" for column in column_list)
    return f"({sql})", [pattern] * len(column_list)


def text(value: object | None, fallback: str = "-") -> str:
    if value is None:
        return fallback
    string = str(value).strip()
    return string if string else fallback


def escape(value: object | None, fallback: str = "-") -> str:
    return html.escape(text(value, fallback))


def format_score(value: object | None) -> str:
    # Scores stay numeric in SQL so ORDER BY never compares formatted text.
    try:
        return f"{float(value):.1f}"
    except (TypeError, ValueError):
        return "-"


def normalize_iso_datetime(value: str) -> str:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    match = re.fullmatch(
        r"(?P<base>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})"
        r"(?P<fraction>\.\d+)?"
        r"(?P<timezone>[+-]\d{2}:\d{2})?",
        normalized,
    )
    if not match:
        return normalized
    fraction = match.group("fraction")
    if fraction:
        fraction = "." + fraction[1:7].ljust(6, "0")
    return f"{match.group('base')}{fraction or ''}{match.group('timezone') or ''}"


def parse_stored_datetime(value: object | None) -> datetime | None:
    # Naive values come from SQLite CURRENT_TIMESTAMP, which is UTC; captured_at
    # values carry their own offset.
    raw_value = text(value, "")
    if not raw_value or raw_value == "-":
        return None
    try:
        parsed = datetime.fromisoformat(normalize_iso_datetime(raw_value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def format_datetime_display(value: object | None, *, tz: tzinfo | None = None) -> str:
    raw_value = text(value, "")
    if not raw_value or raw_value == "-":
        return "-"
    normalized = normalize_iso_datetime(raw_value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
        try:
            return datetime.strptime(normalized, "%Y-%m-%d").strftime("%d/%m/%Y")
        except ValueError:
            return raw_value
    parsed = parse_stored_datetime(raw_value)
    if parsed is None:
        return raw_value
    return parsed.astimezone(tz).strftime("%d/%m/%Y %H:%M:%S")


def datetime_sort_value(value: object | None) -> str:
    parsed = parse_stored_datetime(value)
    if parsed is None:
        return text(value, "")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def sort_value_cell(display_value: str, sort_value: object | None) -> tuple[str, str]:
    return (display_value, text(sort_value, ""))


def datetime_cell(value: object | None) -> tuple[str, str]:
    return sort_value_cell(escape(format_datetime_display(value)), datetime_sort_value(value))


def source_resolution_label(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
) -> str:
    row_keys = set(row.keys())
    source_id = int(row["id"]) if "id" in row_keys else None
    source_path = row["source_path"] if "source_path" in row_keys else None
    image_width = row["image_width"] if "image_width" in row_keys else None
    image_height = row["image_height"] if "image_height" in row_keys else None
    image_width, image_height = ensure_source_image_dimensions(
        connection,
        source_id=source_id,
        source_path=source_path,
        image_width=image_width,
        image_height=image_height,
    )
    return format_source_resolution(image_width, image_height) or "-"


HTML_BLOCK_TAGS = frozenset(
    {"br", "div", "p", "li", "ul", "ol", "tr", "td", "th", "table", "hr", "h1", "h2", "h3", "h4", "h5", "h6"}
)


class VisibleTextParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in HTML_BLOCK_TAGS:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in HTML_BLOCK_TAGS:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_fragment_text(value: object | None) -> str:
    # Flashcard fronts/backs are stored as Anki HTML; show and search only what the card displays.
    raw_value = text(value, "")
    if not raw_value:
        return ""
    parser = VisibleTextParser()
    parser.feed(raw_value)
    parser.close()
    return " ".join("".join(parser.parts).split())


def truncate(value: str | None, *, size: int = 100) -> str:
    if not value:
        return "-"
    clean = " ".join(value.split())
    if len(clean) <= size:
        return clean
    return clean[: size - 1].rstrip() + "…"


def is_kanji_char(char: str) -> bool:
    codepoint = ord(char)
    return (
        0x3400 <= codepoint <= 0x4DBF
        or 0x4E00 <= codepoint <= 0x9FFF
        or 0xF900 <= codepoint <= 0xFAFF
    )


def extract_kanji_sequence(*values: str | None) -> list[str]:
    sequence: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value:
            continue
        for char in str(value).strip():
            if not is_kanji_char(char) or char in seen:
                continue
            seen.add(char)
            sequence.append(char)
    return sequence


def split_meaning_glosses(value: str | None) -> list[str]:
    if not value:
        return []
    normalized = " ".join(str(value).strip().split())
    if not normalized or normalized == "-":
        return []
    parts = re.split(r"\s*[;,/]\s*|\s+-\s+", normalized)
    glosses: list[str] = []
    seen: set[str] = set()
    for part in parts:
        gloss = " ".join(part.strip().split()).strip(". ")
        if not gloss:
            continue
        key = gloss.lower()
        if key in seen:
            continue
        seen.add(key)
        glosses.append(gloss)
    return glosses


def kanji_gloss_candidates(rows: list[sqlite3.Row], *, limit: int = 6) -> list[str]:
    ranked: dict[str, tuple[float, int]] = {}
    for index, row in enumerate(rows):
        try:
            weight = float(row["priority_score"])
        except (TypeError, ValueError):
            weight = 0.0
        try:
            weight += float(row["total_occurrences"])
        except (TypeError, ValueError):
            pass
        for gloss in split_meaning_glosses(row["meaning_pt"]):
            previous = ranked.get(gloss)
            if previous is None or weight > previous[0]:
                ranked[gloss] = (weight, index)
    ordered = sorted(
        ranked.items(),
        key=lambda item: (-item[1][0], item[1][1], item[0].lower()),
    )
    return [gloss for gloss, _ in ordered[:limit]]


def current_language(params: dict[str, list[str]] | None) -> str:
    """Language selected through ``?lang=``; ``all`` when none or invalid."""
    raw = first_value(params or {}, "lang", ALL_LANGUAGES_CODE)
    normalized = normalize_language_code(raw)
    if (
        not normalized
        or normalized == ALL_LANGUAGES_CODE
        or not languages.is_valid_language_code(normalized)
    ):
        return ALL_LANGUAGES_CODE
    return normalized


def language_href(path: str, lang: str, extra: dict[str, object | None] | None = None) -> str:
    params: dict[str, object | None] = dict(extra or {})
    params["lang"] = lang if lang != ALL_LANGUAGES_CODE else None
    return build_query_path(path, params)


def hidden_language_input(lang: str) -> str:
    """Keeps the selected language when a search form is submitted."""
    if lang == ALL_LANGUAGES_CODE:
        return ""
    return f'<input type="hidden" name="lang" value="{html.escape(lang, quote=True)}">'


def language_attrs(code: object | None) -> str:
    """``lang`` and ``dir`` attributes for text stored in a catalog language.

    ``lang`` picks the right font (Japanese vs Chinese glyphs, Thai, Devanagari,
    Hangul) and line breaking; ``dir="auto"`` lets Arabic and Hebrew render
    right to left inside the pt-BR page.
    """
    normalized = normalize_language_code(str(code)) if code is not None else ""
    if (
        not normalized
        or normalized in (ALL_LANGUAGES_CODE, AUTO_LANGUAGE_CODE)
        or not languages.is_valid_language_code(normalized)
    ):
        return ' dir="auto"'
    return f' lang="{html.escape(normalized, quote=True)}" dir="auto"'


def reading_attrs(code: object | None) -> str:
    """Attributes for a term reading.

    Japanese readings are kana, so they keep ``lang="ja"``; every other
    language stores a Latin romanization (Revised Romanization, pinyin,
    Cyrillic transliteration), tagged ``xx-Latn``.
    """
    normalized = normalize_language_code(str(code)) if code is not None else ""
    if not normalized or not languages.is_valid_language_code(normalized):
        return ' dir="auto"'
    if get_language(normalized).romanizer == "kana":
        return language_attrs(normalized)
    return language_attrs(f"{languages.base_language_code(normalized)}-Latn")


def language_text(value: object | None, code: object | None, fallback: str = "-") -> str:
    """Escaped catalog text wrapped in a span carrying its language."""
    display = text(value, fallback)
    if display == fallback:
        return html.escape(display)
    return f"<span{language_attrs(code)}>{html.escape(display)}</span>"


def reading_text(value: object | None, code: object | None, fallback: str = "-") -> str:
    display = text(value, fallback)
    if display == fallback:
        return html.escape(display)
    return f"<span{reading_attrs(code)}>{html.escape(display)}</span>"


def term_link(term_id: object, lemma: object | None, code: object | None, lang: str = ALL_LANGUAGES_CODE) -> str:
    href = language_href(f"/terms/{term_id}", lang)
    return f'<a href="{html.escape(href, quote=True)}"{language_attrs(code)}>{escape(lemma)}</a>'


def kanji_link(kanji_id: object, literal: object | None, code: object | None, lang: str = ALL_LANGUAGES_CODE) -> str:
    href = language_href(f"/kanji/{kanji_id}", lang)
    return f'<a href="{html.escape(href, quote=True)}"{language_attrs(code)}>{escape(literal)}</a>'


def source_link(source_id: object, lang: str = ALL_LANGUAGES_CODE) -> str:
    href = language_href(f"/sources/{source_id}", lang)
    return f'<a href="{html.escape(href, quote=True)}">#{html.escape(str(source_id))}</a>'


def language_has_readings(lang: str) -> bool:
    """Whether a reading column means anything for the selected language.

    ``all`` keeps the column (Japanese kana, Korean and Russian romanization,
    pinyin); a single Latin-script language has no readings at all.
    """
    return lang == ALL_LANGUAGES_CODE or get_language(lang).has_readings


def any_language_has_readings(codes: Iterable[object | None]) -> bool:
    return any(get_language(str(code)).has_readings for code in codes if code)


def language_has_characters(lang: str) -> bool:
    """Whether kanji/hanzi pages, stats and links apply to the selected language."""
    return lang == ALL_LANGUAGES_CODE or get_language(lang).has_characters


def characters_label(lang: str = ALL_LANGUAGES_CODE) -> str:
    """Name of the character catalog: kanji for Japanese, hanzi for Chinese."""
    if lang == ALL_LANGUAGES_CODE:
        return "Kanji"
    base = languages.base_language_code(lang)
    if base == "ja":
        return "Kanji"
    if base == "zh":
        return "Hanzi"
    return "Caracteres"


def language_label(code: object | None) -> str:
    """``Spanish (es)``: the profile name plus the code, for chips and lists."""
    normalized = normalize_language_code(str(code or "")) or "-"
    return f"{get_language(normalized).name} ({normalized})"


def language_bar_html(
    db_path: Path,
    *,
    active_path: str,
    lang: str,
    extra_params: dict[str, object | None] | None = None,
) -> str:
    """Chips to switch the catalog language, keeping the current page's filters."""
    with db_connect(db_path) as connection:
        rows = list_language_rows(connection)
    chips = [
        (
            f'<a class="{"is-active" if lang == ALL_LANGUAGES_CODE else ""}" '
            f'href="{html.escape(language_href(active_path, ALL_LANGUAGES_CODE, extra_params), quote=True)}">Todas</a>'
        )
    ]
    # Every built-in language is registered; the bar only lists the ones that
    # already have data, plus the active one, so it stays readable. The capture
    # page lists them all.
    for row in rows:
        code = str(row["code"])
        has_data = (row["term_count"] or 0) > 0 or (row["source_count"] or 0) > 0
        if lang != code and (not row["enabled"] or not has_data):
            continue
        profile = get_language(code)
        active = "is-active" if lang == code else ""
        chips.append(
            f'<a class="{active}" href="{html.escape(language_href(active_path, code, extra_params), quote=True)}" '
            f'title="{html.escape(profile.display_label)}">'
            f"{html.escape(profile.name)}"
            f'<span class="count">{escape(row["term_count"], "0")}</span></a>'
        )
    chips.append('<a class="lang-settings" href="/capture" title="Idiomas capturados e perfis">Captura</a>')
    return f'<div class="lang-bar"><span class="lang-label">Idioma</span>{"".join(chips)}</div>'


def nav_link(path: str, label: str, active_path: str, lang: str = ALL_LANGUAGES_CODE) -> str:
    active_class = "is-active" if path == active_path else ""
    path = language_href(path, lang)
    return f'<a class="{active_class}" href="{html.escape(path, quote=True)}">{html.escape(label)}</a>'


CATALOG_TITLE = "Lang Catalog"


def page_title(title: str) -> str:
    """Browser tab title; every page ends in ``Lang Catalog``.

    Capture does not skip it by default; a ``title`` skip rule for
    ``Lang Catalog`` (tray "Skip windows", /capture, or
    ``lang_jp.py skip-window title "Lang Catalog"``) keeps the catalog's own
    pages from being captured back as new sources.
    """
    if not title or title == CATALOG_TITLE:
        return CATALOG_TITLE
    return f"{title} - {CATALOG_TITLE}"


def layout(
    *,
    title: str,
    body: str,
    active_path: str,
    extra_scripts: str = "",
    lang: str = ALL_LANGUAGES_CODE,
    language_bar: str = "",
) -> str:
    # Character pages only make sense for languages that have characters.
    show_kanji = language_has_characters(lang)
    nav = "".join(
        (
            nav_link("/", "Visao geral", active_path, lang),
            nav_link("/terms", "Termos", active_path, lang),
            nav_link("/kanji", characters_label(lang), active_path, lang) if show_kanji else "",
            nav_link("/sources", "Fontes", active_path, lang),
            nav_link("/flashcards", "Cards", active_path, lang),
            nav_link("/capture", "Captura", active_path),
        )
    )
    return f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(page_title(title))}</title>
    {PAGE_STYLE}
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <h1>Lang Catalog</h1>
        <p>Interface local para navegar no catalogo de vocabulario em todos os idiomas sem depender de comandos longos no terminal.</p>
        <nav class="nav">{nav}</nav>
        {language_bar}
      </section>
      {body}
    </div>
    {PAGE_SCRIPT}
    {extra_scripts}
  </body>
</html>
"""


def table_html(headers: list[str], rows: list[list[str | tuple[str, str]]]) -> str:
    if not rows:
        return '<div class="empty">Nenhum resultado encontrado.</div>'

    # Rows arrive in the server's chosen order; the browser only re-sorts on a header click.
    table_attrs = ' class="sortable-table"'

    head_html = "".join(
        (
            '<th aria-sort="none">'
            f'<button class="sort-toggle" type="button" data-sort-direction="none">{html.escape(header)}</button>'
            "</th>"
        )
        for header in headers
    )
    body_parts: list[str] = []
    for row in rows:
        body_cells = "".join(
            (
                f'<td data-sort-value="{html.escape(cell[1], quote=True)}">{cell[0]}</td>'
                if isinstance(cell, tuple)
                else f"<td>{cell}</td>"
            )
            for cell in row
        )
        body_parts.append(f"<tr>{body_cells}</tr>")
    return (
        "<div style=\"overflow:auto;\">"
        f"<table{table_attrs}><thead><tr>{head_html}</tr></thead><tbody>{''.join(body_parts)}</tbody></table>"
        "</div>"
    )


def search_form(
    *,
    action: str,
    label: str,
    placeholder: str,
    q: str = "",
    extras: str = "",
    reset_href: str | None = None,
    lang: str = ALL_LANGUAGES_CODE,
) -> str:
    reset_link = (
        f'<a class="secondary-link" href="{html.escape(reset_href, quote=True)}">Limpar filtros</a>'
        if reset_href
        else ""
    )
    return f"""
    <form class="search" action="{action}" method="get">
      <label>{html.escape(label)}</label>
      <div class="inputs">
        <input type="search" name="q" value="{html.escape(q)}" placeholder="{html.escape(placeholder)}" dir="auto">
        {extras}{hidden_language_input(lang)}
        <div class="search-actions">
          <button type="submit">Buscar</button>
          {reset_link}
        </div>
      </div>
    </form>
    """


def status_chip(value: str) -> str:
    return f'<span class="chip">{html.escape(value)}</span>'


def notice_banner(params: dict[str, list[str]]) -> str:
    message = first_value(params, "notice", "").strip()
    if not message:
        return ""
    level = first_value(params, "level", "info").strip().lower()
    classes = "notice-banner"
    if level == "error":
        classes += " is-error"
    return f'<div class="{classes}">{html.escape(message)}</div>'


def sync_anki_action(*, next_path: str = "/") -> str:
    safe_next = html.escape(next_path, quote=True)
    return f"""
    <div class="anki-sync-root" data-anki-sync-root>
      <div class="action-row">
        <form class="inline-action" action="/actions/sync-anki" method="post" data-anki-sync-form>
          <input type="hidden" name="next" value="{safe_next}">
          <button type="submit" data-anki-sync-submit>Sincronizar com o Anki</button>
        </form>
        <span class="muted">A acao sincroniza termos e kanji com o Anki, atualizando os decks e a prioridade dos cards novos.</span>
      </div>
      <div
        class="anki-sync-progress"
        data-anki-sync-progress
        role="progressbar"
        aria-live="polite"
        aria-valuemin="0"
        aria-valuemax="100"
        aria-valuenow="0"
        hidden
      >
        <div class="anki-sync-progress-head">
          <span class="anki-sync-progress-title" data-anki-sync-title>Sincronizando com o Anki</span>
          <span class="anki-sync-progress-value" data-anki-sync-value>0%</span>
        </div>
        <div class="anki-sync-progress-track">
          <div class="anki-sync-progress-fill" data-anki-sync-fill></div>
        </div>
        <div class="anki-sync-progress-message" data-anki-sync-message>Preparando sincronizacao com o Anki...</div>
      </div>
    </div>
    """


LANGUAGE_CARD_TOP_TERMS = 6


def language_cards(
    connection: sqlite3.Connection,
    language_rows: list[sqlite3.Row],
    *,
    lang: str = ALL_LANGUAGES_CODE,
) -> str:
    """One card per language with data: counts plus its top terms (plan item D5).

    With ``lang=all`` every language that has terms or sources gets a card, side
    by side; with one language selected only that card shows. Languages that are
    registered but still empty are listed in one line so the page stays short.
    """
    cards: list[str] = []
    empty: list[str] = []
    for row in language_rows:
        code = str(row["code"])
        term_count = int(row["term_count"] or 0)
        source_count = int(row["source_count"] or 0)
        if lang != ALL_LANGUAGES_CODE and code != lang:
            continue
        if term_count == 0 and source_count == 0 and lang == ALL_LANGUAGES_CODE:
            label = get_language(code).name
            capture = "" if row["capture_enabled"] else " (captura off)"
            empty.append(f"{html.escape(label)}{capture}")
            continue
        profile = get_language(code)
        top_terms = connection.execute(
            """
            SELECT id, lemma, COALESCE(reading, '') AS reading, COALESCE(meaning_pt, '-') AS meaning_pt,
                   total_occurrences, priority_score
            FROM v_terms_overview
            WHERE language_code = ?
              AND NOT (LOWER(TRIM(COALESCE(meaning_pt, ''))) LIKE 'nome%proprio%')
            ORDER BY
            """ + TERM_PRIORITY_ORDER_SQL + """
            LIMIT ?
            """,
            (code, LANGUAGE_CARD_TOP_TERMS),
        ).fetchall()
        term_rows = [
            [
                term_link(term["id"], term["lemma"], code, lang)
                + (
                    f' <span class="muted"{reading_attrs(code)}>{escape(term["reading"])}</span>'
                    if profile.has_readings and term["reading"]
                    else ""
                ),
                escape(truncate(str(term["meaning_pt"]), size=40)),
                escape(term["total_occurrences"]),
                escape(format_score(term["priority_score"])),
            ]
            for term in top_terms
        ]
        capture_state = "captura ligada" if row["capture_enabled"] else "captura desligada"
        cards.append(
            f"""
      <section class="panel panel-side lang-card" data-language="{html.escape(code)}">
        <h2><a href="{html.escape(language_href("/", code), quote=True)}">{html.escape(profile.name)}</a> <span class="chip">{html.escape(code)}</span></h2>
        <div class="stats stats-compact">
          <div class="stat"><span class="label">Termos</span><span class="value">{escape(term_count, "0")}</span></div>
          <div class="stat"><span class="label">Fontes</span><span class="value">{escape(source_count, "0")}</span></div>
          <div class="stat"><span class="label">Cards pendentes</span><span class="value">{escape(row["pending_flashcards"], "0")}</span></div>
        </div>
        {table_html(["Termo", "Significado", "Ocorr.", "Score"], term_rows)}
        <p class="footer-note">Deck {html.escape(str(row["deck_name"]))} | {capture_state} |
          <a href="{html.escape(language_href("/terms", code), quote=True)}">termos</a> |
          <a href="{html.escape(language_href("/sources", code), quote=True)}">fontes</a></p>
      </section>
            """
        )
    if empty and lang == ALL_LANGUAGES_CODE:
        cards.append(
            f"""
      <section class="panel">
        <h2>Idiomas sem dados ainda</h2>
        <p>{", ".join(empty)}.</p>
        <p class="footer-note">Estes idiomas ja estao registrados; assim que a captura ou o <code>ingest-text</code> encontrar texto neles, o card aparece aqui. <a href="/capture">Ajustar a captura</a>.</p>
      </section>
            """
        )
    return "".join(cards)


def dashboard_page(db_path: Path, params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    lang = current_language(params)
    lang_sql, lang_params = language_filter_clause("language_code", lang)
    lang_and = f"AND {lang_sql}" if lang_sql else ""
    lang_where = f"WHERE {lang_sql}" if lang_sql else ""
    term_lang_sql, _ = language_filter_clause("t.language_code", lang)
    term_lang_and = f"AND {term_lang_sql}" if term_lang_sql else ""
    with db_connect(db_path) as connection:
        stats = connection.execute(
            f"""
            SELECT
                (SELECT COUNT(*) FROM sources WHERE 1 = 1 {lang_and}) AS source_count,
                (SELECT COUNT(*) FROM terms WHERE 1 = 1 {lang_and}) AS term_count,
                (SELECT COUNT(*) FROM kanji WHERE 1 = 1 {lang_and}) AS kanji_count,
                (
                    SELECT COUNT(*)
                    FROM source_terms AS st
                    JOIN terms AS t ON t.id = st.term_id
                    WHERE 1 = 1 {term_lang_and}
                ) AS link_count,
                (
                    SELECT COUNT(*)
                    FROM flashcards AS f
                    JOIN terms AS t ON t.id = f.term_id
                    WHERE f.export_status = 'pending' {term_lang_and}
                ) AS pending_flashcards
            """,
            [*lang_params, *lang_params, *lang_params, *lang_params, *lang_params],
        ).fetchone()
        top_terms = connection.execute(
            f"""
            SELECT
                id,
                language_code,
                lemma,
                COALESCE(reading, '-') AS reading,
                COALESCE(meaning_pt, '-') AS meaning_pt,
                total_occurrences,
                distinct_sources,
                priority_score
            FROM v_terms_overview
            WHERE
                COALESCE(priority_score, 0) > 0
                AND NOT (
                    LOWER(TRIM(COALESCE(meaning_pt, ''))) LIKE 'nome%proprio%'
                )
                {lang_and}
            ORDER BY
            """ + TERM_PRIORITY_ORDER_SQL + """
            LIMIT 8
            """,
            lang_params,
        ).fetchall()
        show_characters = language_has_characters(lang)
        top_kanji = connection.execute(
            f"""
            SELECT
                id,
                language_code,
                literal,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                priority_score
            FROM v_kanji_overview
            {lang_where}
            ORDER BY priority_score DESC, id DESC
            LIMIT 8
            """,
            lang_params,
        ).fetchall() if show_characters else []
        recent_sources = connection.execute(
            f"""
            SELECT
                id,
                language_code,
                COALESCE(title, '(sem titulo)') AS title,
                COALESCE(scene, '-') AS scene,
                source_path,
                image_width,
                image_height,
                COALESCE(captured_at, created_at) AS seen_at
            FROM sources
            {lang_where}
            ORDER BY julianday(seen_at) DESC, id DESC
            LIMIT 8
            """,
            lang_params,
        ).fetchall()
        recent_source_rows = [
            [
                source_link(row["id"], lang),
                escape(row["language_code"]),
                language_text(row["title"], None),
                escape(source_resolution_label(connection, row)),
                escape(row["scene"]),
                datetime_cell(row["seen_at"]),
            ]
            for row in recent_sources
        ]
        recent_cards = connection.execute(
            f"""
            SELECT
                f.id,
                t.lemma,
                t.language_code,
                COALESCE(ts.priority_score, 0) AS priority_score,
                f.template_name,
                f.export_status,
                f.front_text
            FROM flashcards AS f
            JOIN terms AS t
                ON t.id = f.term_id
            LEFT JOIN term_stats AS ts
                ON ts.term_id = f.term_id
            WHERE 1 = 1 {term_lang_and}
            ORDER BY
            """ + FLASHCARD_PRIORITY_ORDER_SQL + """
            LIMIT 8
            """,
            lang_params,
        ).fetchall()
        language_rows = list_language_rows(connection)
        language_cards_html = language_cards(connection, language_rows, lang=lang)

    show_readings = language_has_readings(lang)
    term_rows = [
        [
            term_link(row["id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            *([reading_text(row["reading"], row["language_code"])] if show_readings else []),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
        ]
        for row in top_terms
    ]
    term_headers = [
        "Termo",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Significado",
        "Ocorrencias",
        "Fontes",
        "Score",
    ]
    kanji_rows = [
        [
            kanji_link(row["id"], row["literal"], row["language_code"], lang),
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
        ]
        for row in top_kanji
    ]
    card_rows = [
        [
            (
                f'<a href="{html.escape(language_href("/flashcards", lang, {"q": row["lemma"]}), quote=True)}"'
                f'{language_attrs(row["language_code"])}>{escape(row["lemma"])}</a>'
            ),
            escape(row["language_code"]),
            escape(format_score(row["priority_score"])),
            escape(row["template_name"]),
            status_chip(text(row["export_status"])),
            language_text(truncate(html_fragment_text(row["front_text"]), size=44), row["language_code"]),
        ]
        for row in recent_cards
    ]
    characters = characters_label(lang)
    kanji_stat = (
        f'<div class="stat"><span class="label">{html.escape(characters)}</span><span class="value">{escape(stats["kanji_count"], "0")}</span></div>'
        if show_characters
        else ""
    )
    kanji_search = (
        search_form(action="/kanji", label=f"Buscar {characters.lower()}", placeholder="Ex.: 魔, 学, river", lang=lang)
        if show_characters
        else ""
    )
    kanji_quick_link = (
        f'<a class="button-link" href="{html.escape(language_href("/kanji", lang), quote=True)}">Abrir catalogo de {html.escape(characters.lower())}</a>'
        if show_characters
        else ""
    )
    kanji_panel = (
        f"""
      <section class="panel panel-side">
        <h2>{html.escape(characters)} prioritarios</h2>
        {table_html([characters, "Palavras", "Ocorrencias", "Fontes", "Score"], kanji_rows)}
      </section>
        """
        if show_characters
        else ""
    )
    language_table_rows = [
        [
            f'<a href="{html.escape(language_href("/", str(row["code"])), quote=True)}">{escape(get_language(str(row["code"])).name)}</a>',
            escape(row["term_count"], "0"),
            escape(row["source_count"], "0"),
            escape(row["pending_flashcards"], "0"),
            escape(row["deck_name"]),
        ]
        for row in language_rows
    ]

    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <div class="stats">
          <div class="stat"><span class="label">Fontes</span><span class="value">{escape(stats["source_count"], "0")}</span></div>
          <div class="stat"><span class="label">Termos</span><span class="value">{escape(stats["term_count"], "0")}</span></div>
          {kanji_stat}
          <div class="stat"><span class="label">Ligacoes</span><span class="value">{escape(stats["link_count"], "0")}</span></div>
          <div class="stat"><span class="label">Cards pendentes</span><span class="value">{escape(stats["pending_flashcards"], "0")}</span></div>
        </div>
        {sync_anki_action(next_path=language_href("/", lang))}
      </section>

      <section class="panel">
        <h2>Busca rapida</h2>
        <div class="search-row">
          {search_form(action="/terms", label="Buscar termos", placeholder="Termo, leitura ou significado", lang=lang)}
          {kanji_search}
          {search_form(action="/sources", label="Buscar fontes", placeholder="Titulo, cena ou texto bruto", lang=lang)}
          {search_form(action="/flashcards", label="Buscar cards", placeholder="Frente, termo ou significado", lang=lang)}
        </div>
      </section>

      <section class="panel panel-wide">
        <h2>Termos prioritarios</h2>
        {table_html(term_headers, term_rows)}
      </section>

      <section class="panel panel-side">
        <h2>Idiomas</h2>
        {table_html(["Idioma", "Termos", "Fontes", "Cards pendentes", "Deck"], language_table_rows)}
        <p class="footer-note"><a href="/capture">Escolher quais idiomas a captura guarda</a></p>
      </section>

      {language_cards_html}

      <section class="panel panel-side">
        <h2>Acesso rapido</h2>
        <div class="stack">
          <a class="button-link" href="{html.escape(language_href("/terms", lang), quote=True)}">Abrir lista completa de termos</a>
          {kanji_quick_link}
          <a class="button-link" href="{html.escape(language_href("/sources", lang), quote=True)}">Abrir fontes recentes</a>
          <a class="button-link" href="{html.escape(language_href("/flashcards", lang, {"status": "pending"}), quote=True)}">Ver cards pendentes</a>
        </div>
        <p class="footer-note">A interface navega no catalogo e tambem permite disparar a sincronizacao com o Anki. O banco continua sendo a fonte de verdade.</p>
      </section>

      <section class="panel panel-wide">
        <h2>Fontes recentes</h2>
        {table_html(["ID", "Idioma", "Titulo", "Resolucao", "Cena", "Quando"], recent_source_rows)}
      </section>

      {kanji_panel}

      <section class="panel">
        <h2>Cards recentes</h2>
        {table_html(["Termo", "Idioma", "Score", "Template", "Status", "Frente"], card_rows)}
      </section>
    </div>
    """
    return layout(
        title="Lang Catalog",
        body=body,
        active_path="/",
        extra_scripts=auto_reload_script(db_path),
        lang=lang,
        language_bar=language_bar_html(db_path, active_path="/", lang=lang),
    )


def terms_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    order = first_value(params, "order", "priority")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)
    order_clause = {
        "priority": TERM_PRIORITY_ORDER_SQL.strip(),
        "recent": "julianday(last_seen_at) DESC, id DESC",
        "lemma": "lemma COLLATE NOCASE ASC, id ASC",
    }.get(order, TERM_PRIORITY_ORDER_SQL.strip())

    filters = []
    sql_params: list[object] = []
    lang_sql, lang_params = language_filter_clause("language_code", lang)
    if lang_sql:
        filters.append(lang_sql)
        sql_params.extend(lang_params)
    if q.strip():
        search_sql, search_params = folded_like_filter(("lemma", "reading", "meaning_pt"), q)
        filters.append(search_sql)
        sql_params.extend(search_params)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM v_terms_overview
            {where_clause}
            """,
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                id,
                language_code,
                lemma,
                COALESCE(reading, '-') AS reading,
                COALESCE(meaning_pt, '-') AS meaning_pt,
                total_occurrences,
                distinct_sources,
                priority_score,
                COALESCE(last_seen_at, '-') AS last_seen_at
            FROM v_terms_overview
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()

    base_params = {
        "q": q.strip() or None,
        "order": order if order != "priority" else None,
        "limit": limit if limit != 50 else None,
        "lang": lang if lang != ALL_LANGUAGES_CODE else None,
    }
    pagination_markup = pagination_nav(
        path="/terms",
        base_params=base_params,
        pagination=pagination,
    )
    # "Limpar filtros" clears the search but keeps the language from the bar.
    reset_href = (
        language_href("/terms", lang)
        if any(value is not None for key, value in base_params.items() if key != "lang") or pagination.page > 1
        else None
    )
    extras = f"""
      <div class="filters">
        <select name="order">
          <option value="priority" {"selected" if order == "priority" else ""}>Score</option>
          <option value="recent" {"selected" if order == "recent" else ""}>Mais recentes</option>
          <option value="lemma" {"selected" if order == "lemma" else ""}>Lemma</option>
        </select>
        <input type="number" name="limit" value="{limit}" min="1" max="200">
      </div>
    """
    show_readings = language_has_readings(lang)
    table_rows = [
        [
            term_link(row["id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            *([reading_text(row["reading"], row["language_code"])] if show_readings else []),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
            datetime_cell(row["last_seen_at"]),
        ]
        for row in rows
    ]
    headers = [
        "Termo",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Significado",
        "Ocorrencias",
        "Fontes",
        "Score",
        "Ultima vez",
    ]
    body = f"""
    <div class="grid">
      <section class="panel">
        <h2>Termos</h2>
        {search_form(action="/terms", label="Buscar no catalogo", placeholder="Ex.: 回復, かいふく, cura, traicionar", q=q, extras=extras, reset_href=reset_href, lang=lang)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)} Ordenacao atual: <strong>{html.escape(order)}</strong>.</p>
          {pagination_markup}
        </div>
        {table_html(headers, table_rows)}
      </section>
    </div>
    """
    return layout(
        title="Termos",
        body=body,
        active_path="/terms",
        extra_scripts=auto_reload_script(db_path),
        lang=lang,
        language_bar=language_bar_html(
            db_path,
            active_path="/terms",
            lang=lang,
            extra_params={key: value for key, value in base_params.items() if key != "lang"},
        ),
    )


def kanji_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    order = first_value(params, "order", "priority")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)
    order_clause = {
        "priority": "priority_score DESC, id DESC",
        "recent": "julianday(last_seen_at) DESC, id DESC",
        "literal": "literal COLLATE NOCASE ASC, id ASC",
    }.get(order, "priority_score DESC, id DESC")

    filters = []
    sql_params: list[object] = []
    lang_sql, lang_params = language_filter_clause("language_code", lang)
    if lang_sql:
        filters.append(lang_sql)
        sql_params.extend(lang_params)
    if q.strip():
        search_sql, search_params = folded_like_filter(("literal", "meaning_pt", "canonical_meanings"), q)
        filters.append(search_sql)
        sql_params.extend(search_params)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(filters)

    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM v_kanji_overview
            {where_clause}
            """,
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                id,
                language_code,
                literal,
                COALESCE(canonical_meanings, meaning_pt, '-') AS meaning,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                priority_score,
                COALESCE(last_seen_at, '-') AS last_seen_at
            FROM v_kanji_overview
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()

    base_params = {
        "q": q.strip() or None,
        "order": order if order != "priority" else None,
        "limit": limit if limit != 50 else None,
        "lang": lang if lang != ALL_LANGUAGES_CODE else None,
    }
    pagination_markup = pagination_nav(
        path="/kanji",
        base_params=base_params,
        pagination=pagination,
    )
    reset_href = (
        language_href("/kanji", lang)
        if any(value is not None for key, value in base_params.items() if key != "lang") or pagination.page > 1
        else None
    )
    extras = f"""
      <div class="filters">
        <select name="order">
          <option value="priority" {"selected" if order == "priority" else ""}>Score</option>
          <option value="recent" {"selected" if order == "recent" else ""}>Mais recentes</option>
          <option value="literal" {"selected" if order == "literal" else ""}>Literal</option>
        </select>
        <input type="number" name="limit" value="{limit}" min="1" max="200">
      </div>
    """
    table_rows = [
        [
            kanji_link(row["id"], row["literal"], row["language_code"], lang),
            escape(row["meaning"]),
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
            datetime_cell(row["last_seen_at"]),
        ]
        for row in rows
    ]
    characters = characters_label(lang)
    no_characters_note = (
        ""
        if language_has_characters(lang)
        else (
            f'<div class="notice-banner">{html.escape(get_language(lang).name)} nao usa caracteres como kanji ou hanzi, '
            "entao nao ha nada para listar aqui neste idioma.</div>"
        )
    )
    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <h2>{html.escape(characters)}</h2>
        {no_characters_note}
        {sync_anki_action(next_path=language_href("/kanji", lang))}
        {search_form(action="/kanji", label="Buscar no catalogo", placeholder="Ex.: 魔, 学, river", q=q, extras=extras, reset_href=reset_href, lang=lang)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)} Ordenacao atual: <strong>{html.escape(order)}</strong>.</p>
          {pagination_markup}
        </div>
        {table_html([characters, "Significado", "Palavras", "Ocorrencias", "Fontes", "Score", "Ultima vez"], table_rows)}
      </section>
    </div>
    """
    return layout(
        title=characters,
        body=body,
        active_path="/kanji",
        lang=lang,
        language_bar=language_bar_html(
            db_path,
            active_path="/kanji",
            lang=lang,
            extra_params={key: value for key, value in base_params.items() if key != "lang"},
        ),
        extra_scripts=auto_reload_script(db_path),
    )


def kanji_detail_page(
    db_path: Path,
    kanji_id: int,
    params: dict[str, list[str]] | None = None,
) -> str | None:
    lang = current_language(params)
    with db_connect(db_path) as connection:
        kanji = connection.execute(
            """
            SELECT
                id,
                language_code,
                literal,
                COALESCE(canonical_meanings, meaning_pt, '-') AS meaning,
                COALESCE(on_readings, '-') AS on_readings,
                COALESCE(kun_readings, '-') AS kun_readings,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                priority_score,
                COALESCE(first_seen_at, '-') AS first_seen_at,
                COALESCE(last_seen_at, '-') AS last_seen_at,
                created_at,
                updated_at
            FROM v_kanji_overview
            WHERE id = ?
            """,
            (kanji_id,),
        ).fetchone()
        if kanji is None:
            return None

        term_rows = connection.execute(
            """
            SELECT
                t.id AS term_id,
                t.language_code,
                t.lemma,
                COALESCE(t.reading, '-') AS reading,
                COALESCE(t.meaning_pt, '-') AS meaning_pt,
                SUM(st.count_in_source) AS total_occurrences,
                COUNT(DISTINCT st.source_id) AS distinct_sources,
                COALESCE(ts.priority_score, 0) AS priority_score
            FROM source_kanji AS sk
            JOIN source_terms AS st
                ON st.source_id = sk.source_id
            JOIN terms AS t
                ON t.id = st.term_id
            LEFT JOIN term_stats AS ts
                ON ts.term_id = t.id
            WHERE
                sk.kanji_id = ?
                AND (
                    instr(st.surface_form, ?) > 0
                    OR instr(t.lemma, ?) > 0
                )
            GROUP BY
                t.id,
                t.language_code,
                t.lemma,
                t.reading,
                t.meaning_pt,
                ts.priority_score
            ORDER BY
                total_occurrences DESC,
                COALESCE(ts.priority_score, 0) DESC,
                t.lemma COLLATE NOCASE ASC
            """,
            (kanji_id, kanji["literal"], kanji["literal"]),
        ).fetchall()
        related_terms_total = len(term_rows)
        term_rows = term_rows[:DETAIL_TABLE_LIMIT]
        related_sources_total = connection.execute(
            "SELECT COUNT(*) FROM source_kanji WHERE kanji_id = ?",
            (kanji_id,),
        ).fetchone()[0]
        source_rows = connection.execute(
            """
            SELECT
                sk.source_id,
                sk.count_in_source,
                COALESCE(s.title, '(sem titulo)') AS title,
                COALESCE(s.game, '-') AS game,
                COALESCE(s.scene, '-') AS scene,
                COALESCE(s.captured_at, s.created_at) AS seen_at
            FROM source_kanji AS sk
            JOIN sources AS s
                ON s.id = sk.source_id
            WHERE sk.kanji_id = ?
            ORDER BY sk.count_in_source DESC, julianday(seen_at) DESC, sk.id DESC
            LIMIT ?
            """,
            (kanji_id, DETAIL_TABLE_LIMIT),
        ).fetchall()

    kanji_language = str(kanji["language_code"] or languages.DEFAULT_LANGUAGE_CODE)
    characters = characters_label(kanji_language)
    character_noun = "caractere" if characters == "Caracteres" else characters.lower()
    related_term_rows = [
        [
            term_link(row["term_id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            reading_text(row["reading"], row["language_code"]),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
        ]
        for row in term_rows
    ]
    related_source_rows = [
        [
            source_link(row["source_id"], lang),
            escape(row["count_in_source"]),
            language_text(row["title"], None),
            escape(row["game"]),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in source_rows
    ]
    # On/kun readings are Japanese; a hanzi entry has none, so the chips only
    # show for Japanese or when the metadata actually exists.
    reading_chips = (
        f"""
          <span class="chip">On: <span{language_attrs("ja")}>{escape(kanji["on_readings"], "-")}</span></span>
          <span class="chip">Kun: <span{language_attrs("ja")}>{escape(kanji["kun_readings"], "-")}</span></span>"""
        if languages.base_language_code(kanji_language) == "ja"
        or text(kanji["on_readings"], "-") != "-"
        or text(kanji["kun_readings"], "-") != "-"
        else ""
    )
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        <h2{language_attrs(kanji_language)}>{escape(kanji["literal"])}</h2>
        <div class="chips">
          <span class="chip">Idioma: {escape(language_label(kanji_language))}</span>
          <span class="chip">Significado: {escape(kanji["meaning"], "-")}</span>{reading_chips}
          <span class="chip">Palavras: {escape(kanji["distinct_terms"], "0")}</span>
          <span class="chip">Ocorrencias: {escape(kanji["total_occurrences"], "0")}</span>
          <span class="chip">Fontes: {escape(kanji["distinct_sources"], "0")}</span>
          <span class="chip">Score: {escape(format_score(kanji["priority_score"]))}</span>
        </div>
      </section>

      <section class="panel panel-side">
        <h2>Atalhos</h2>
        <div class="stack">
          <a class="button-link" href="{html.escape(language_href("/kanji", lang), quote=True)}">Voltar para {html.escape(characters.lower())}</a>
          <a class="button-link" href="{html.escape(language_href("/terms", lang), quote=True)}">Abrir termos</a>
          <a class="button-link" href="{html.escape(language_href("/sources", lang), quote=True)}">Abrir fontes</a>
        </div>
      </section>

      <section class="panel panel-wide">
        <h3>Termos relacionados</h3>
        {truncation_note(len(term_rows), related_terms_total)}
        {table_html(["Termo", "Idioma", "Leitura", "Significado", "Ocorrencias", "Fontes", "Score"], related_term_rows)}
      </section>

      <section class="panel panel-side">
        <h3>Resumo</h3>
        <ul class="meta-list">
          <li><strong>Primeira vez</strong>{escape(format_datetime_display(kanji["first_seen_at"]))}</li>
          <li><strong>Ultima vez</strong>{escape(format_datetime_display(kanji["last_seen_at"]))}</li>
          <li><strong>Criado em</strong>{escape(format_datetime_display(kanji["created_at"]))}</li>
          <li><strong>Atualizado em</strong>{escape(format_datetime_display(kanji["updated_at"]))}</li>
        </ul>
      </section>

      <section class="panel">
        <h3>Fontes que contem este {html.escape(character_noun)}</h3>
        {truncation_note(len(source_rows), related_sources_total)}
        {table_html(["Fonte", "Count", "Titulo", "Game", "Cena", "Quando"], related_source_rows)}
      </section>
    </div>
    """
    return layout(title=f"{characters} {kanji['literal']}", body=body, active_path="/kanji", lang=lang)


def term_detail_page(
    db_path: Path,
    term_id: int,
    params: dict[str, list[str]] | None = None,
) -> str | None:
    lang = current_language(params)
    with db_connect(db_path) as connection:
        term = connection.execute(
            """
            SELECT
                t.id,
                t.language_code,
                t.lemma,
                t.reading,
                t.meaning_pt,
                t.pos,
                t.notes,
                t.created_at,
                t.updated_at,
                COALESCE(ts.total_occurrences, 0) AS total_occurrences,
                COALESCE(ts.distinct_sources, 0) AS distinct_sources,
                COALESCE(ts.distinct_forms, 0) AS distinct_forms,
                COALESCE(ts.priority_score, 0) AS priority_score,
                COALESCE(ts.first_seen_at, '-') AS first_seen_at,
                COALESCE(ts.last_seen_at, '-') AS last_seen_at
            FROM terms AS t
            LEFT JOIN term_stats AS ts
                ON ts.term_id = t.id
            WHERE t.id = ?
            """,
            (term_id,),
        ).fetchone()
        if term is None:
            return None

        forms = connection.execute(
            """
            SELECT
                surface_form,
                COALESCE(reading, '-') AS reading
            FROM term_forms
            WHERE term_id = ?
            ORDER BY surface_form COLLATE NOCASE ASC
            """,
            (term_id,),
        ).fetchall()
        occurrences = connection.execute(
            """
            SELECT
                st.source_id,
                st.surface_form,
                st.count_in_source,
                COALESCE(st.confidence, '-') AS confidence,
                COALESCE(s.title, '(sem titulo)') AS title,
                COALESCE(s.game, '-') AS game,
                COALESCE(s.scene, '-') AS scene,
                COALESCE(s.captured_at, s.created_at) AS seen_at
            FROM source_terms AS st
            JOIN sources AS s
                ON s.id = st.source_id
            WHERE st.term_id = ?
            ORDER BY st.count_in_source DESC, julianday(seen_at) DESC, st.id DESC
            LIMIT ?
            """,
            (term_id, DETAIL_TABLE_LIMIT),
        ).fetchall()
        occurrences_total = connection.execute(
            "SELECT COUNT(*) FROM source_terms WHERE term_id = ?",
            (term_id,),
        ).fetchone()[0]
        occurrence_surface_forms = [
            row["surface_form"]
            for row in connection.execute(
                "SELECT DISTINCT surface_form FROM source_terms WHERE term_id = ?",
                (term_id,),
            )
        ]
        segment_occurrences = connection.execute(
            """
            SELECT
                ss.source_id,
                ss.segment_text,
                COALESCE(ss.language_code, s.language_code) AS segment_language,
                COALESCE(ss.reading, '-') AS segment_reading,
                sst.surface_form,
                sst.count_in_segment,
                COALESCE(sst.confidence, '-') AS confidence,
                COALESCE(s.title, '(sem titulo)') AS title,
                COALESCE(s.scene, '-') AS scene,
                COALESCE(s.captured_at, s.created_at) AS seen_at
            FROM source_segment_terms AS sst
            JOIN source_segments AS ss
                ON ss.id = sst.source_segment_id
            JOIN sources AS s
                ON s.id = ss.source_id
            WHERE sst.term_id = ?
            ORDER BY julianday(seen_at) DESC, ss.id DESC, sst.id DESC
            LIMIT ?
            """,
            (term_id, DETAIL_TABLE_LIMIT),
        ).fetchall()
        segment_occurrences_total = connection.execute(
            "SELECT COUNT(*) FROM source_segment_terms WHERE term_id = ?",
            (term_id,),
        ).fetchone()[0]
        flashcards = connection.execute(
            """
            SELECT
                id,
                template_name,
                deck_name,
                export_status,
                front_text
            FROM flashcards
            WHERE term_id = ?
            ORDER BY id DESC
            """,
            (term_id,),
        ).fetchall()
        term_language = str(term["language_code"] or languages.DEFAULT_LANGUAGE_CODE)
        profile = get_language(term_language)
        # Only languages with characters (Japanese kanji, Chinese hanzi) derive
        # them; a Spanish or Korean term gets no character panel at all.
        kanji_literals = (
            extract_kanji_sequence(
                term["lemma"],
                *(row["surface_form"] for row in forms),
                *occurrence_surface_forms,
            )
            if profile.has_characters
            else []
        )
        if kanji_literals:
            placeholders = ", ".join("?" for _ in kanji_literals)
            kanji_rows = connection.execute(
                f"""
                SELECT
                    id,
                    language_code,
                    literal,
                    total_occurrences,
                    distinct_sources,
                    distinct_terms,
                    priority_score
                FROM v_kanji_overview
                WHERE literal IN ({placeholders})
                """,
                kanji_literals,
            ).fetchall()
        else:
            kanji_rows = []

    # A reading only means something when the profile has one (kana for
    # Japanese, romanization for Korean, Chinese, Russian); a stray value on a
    # Latin-script term still shows rather than being hidden.
    show_readings = profile.has_readings or bool(text(term["reading"], ""))

    def form_chip(row: sqlite3.Row) -> str:
        surface = language_text(row["surface_form"], term_language)
        if not show_readings and text(row["reading"], "-") == "-":
            return f'<span class="chip">{surface}</span>'
        return f'<span class="chip">{surface} / {reading_text(row["reading"], term_language)}</span>'

    form_chips = "".join(form_chip(row) for row in forms) or '<span class="muted">Nenhuma forma registrada.</span>'
    occurrence_rows = [
        [
            source_link(row["source_id"], lang),
            language_text(row["surface_form"], term_language),
            escape(row["count_in_source"]),
            escape(row["confidence"]),
            language_text(row["title"], None),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in occurrences
    ]
    segment_occurrence_rows = [
        [
            source_link(row["source_id"], lang),
            language_text(row["segment_text"], row["segment_language"]),
            escape(row["segment_language"]),
            *([reading_text(row["segment_reading"], row["segment_language"])] if show_readings else []),
            language_text(row["surface_form"], term_language),
            escape(row["count_in_segment"]),
            escape(row["confidence"]),
            language_text(row["title"], None),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in segment_occurrences
    ]
    segment_headers = [
        "Fonte",
        "Segmento bruto",
        "Idioma do segmento",
        *(["Leitura do segmento"] if show_readings else []),
        "Forma ligada",
        "Count",
        "Confianca",
        "Titulo",
        "Cena",
        "Quando",
    ]
    flashcards_href = language_href("/flashcards", lang, {"q": term["lemma"]})
    flashcard_rows = [
        [
            f'<a href="{html.escape(flashcards_href, quote=True)}">#{row["id"]}</a>',
            escape(row["template_name"]),
            escape(row["deck_name"]),
            status_chip(text(row["export_status"])),
            language_text(truncate(html_fragment_text(row["front_text"]), size=52), term_language),
        ]
        for row in flashcards
    ]
    kanji_order = {literal: index for index, literal in enumerate(kanji_literals)}
    ordered_kanji_rows = sorted(
        kanji_rows,
        key=lambda row: (
            kanji_order.get(str(row["literal"]), len(kanji_order)),
            str(row["literal"]),
        ),
    )
    kanji_table_rows = [
        [
            kanji_link(row["id"], row["literal"], term_language, lang),
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(format_score(row["priority_score"])),
        ]
        for row in ordered_kanji_rows
    ]
    characters = characters_label(term_language)
    kanji_panel = (
        f"""
      <section class="panel panel-side">
        <h3>{html.escape(characters)} derivados</h3>
        {table_html([characters, "Palavras", "Ocorrencias", "Fontes", "Score"], kanji_table_rows)}
      </section>
        """
        if profile.has_characters
        else ""
    )
    reading_chip = (
        f'<span class="chip">Leitura: {reading_text(term["reading"], term_language)}</span>'
        if show_readings
        else ""
    )
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        <h2{language_attrs(term_language)}>{escape(term["lemma"])}</h2>
        <div class="chips">
          <span class="chip">Idioma: {escape(language_label(term_language))}</span>
          {reading_chip}
          <span class="chip">Significado: {escape(term["meaning_pt"] or "-")}</span>
          <span class="chip">Classe: {escape(term["pos"] or "-")}</span>
        </div>
      </section>

      <section class="panel panel-side">
        <h2>Resumo</h2>
        <div class="stats">
          <div class="stat"><span class="label">Ocorrencias</span><span class="value">{escape(term["total_occurrences"], "0")}</span></div>
          <div class="stat"><span class="label">Fontes</span><span class="value">{escape(term["distinct_sources"], "0")}</span></div>
          <div class="stat"><span class="label">Formas</span><span class="value">{escape(term["distinct_forms"], "0")}</span></div>
          <div class="stat"><span class="label">Score</span><span class="value">{escape(format_score(term["priority_score"]))}</span></div>
        </div>
      </section>

      <section class="panel panel-wide">
        <h3>Onde apareceu</h3>
        {truncation_note(len(occurrences), occurrences_total)}
        {table_html(["Fonte", "Forma vista", "Count", "Confianca", "Titulo", "Cena", "Quando"], occurrence_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Segmentacao revisada</h3>
        {truncation_note(len(segment_occurrences), segment_occurrences_total)}
        {table_html(segment_headers, segment_occurrence_rows)}
      </section>

      <section class="panel panel-side">
        <h3>Formas registradas</h3>
        <div class="chips">{form_chips}</div>
        <div style="height: 14px;"></div>
        <ul class="meta-list">
          <li><strong>Primeira vez</strong>{escape(format_datetime_display(term["first_seen_at"]))}</li>
          <li><strong>Ultima vez</strong>{escape(format_datetime_display(term["last_seen_at"]))}</li>
          <li><strong>Criado em</strong>{escape(format_datetime_display(term["created_at"]))}</li>
          <li><strong>Atualizado em</strong>{escape(format_datetime_display(term["updated_at"]))}</li>
        </ul>
      </section>

      {kanji_panel}

      <section class="panel">
        <h3>Flashcards</h3>
        {table_html(["Card", "Template", "Deck", "Status", "Frente"], flashcard_rows)}
      </section>
    </div>
    """
    return layout(title=f"Termo {term['lemma']}", body=body, active_path="/terms", lang=lang)


SOURCES_TABLE_HEADERS = ["ID", "Idioma", "Tipo", "Titulo", "Resolucao", "Cena", "Termos", "Quando", "Texto"]


def load_sources_rows(
    db_path: Path, params: dict[str, list[str]]
) -> tuple[str, int, PaginationState, list[dict[str, object]]]:
    q = first_value(params, "q")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)

    filters = []
    sql_params: list[object] = []
    lang_sql, lang_params = language_filter_clause("s.language_code", lang)
    if lang_sql:
        filters.append(lang_sql)
        sql_params.extend(lang_params)
    if q.strip():
        search_sql, search_params = folded_like_filter(("title", "game", "scene", "raw_text"), q)
        filters.append(search_sql)
        sql_params.extend(search_params)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(part.strip() for part in filters)

    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM sources AS s
            {where_clause}
            """,
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                s.id,
                s.language_code,
                s.source_type,
                COALESCE(s.title, '(sem titulo)') AS title,
                COALESCE(s.scene, '-') AS scene,
                s.source_path,
                s.image_width,
                s.image_height,
                COALESCE(s.captured_at, s.created_at) AS seen_at,
                COALESCE(s.raw_text, '') AS raw_text,
                (
                    SELECT COUNT(*)
                    FROM source_terms AS st
                    WHERE st.source_id = s.id
                ) AS linked_terms
            FROM sources AS s
            {where_clause}
            ORDER BY julianday(seen_at) DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()
        normalized_rows = []
        for row in rows:
            normalized_rows.append(
                {
                    "id": row["id"],
                    "language_code": row["language_code"],
                    "source_type": row["source_type"],
                    "title": row["title"],
                    "resolution": source_resolution_label(connection, row),
                    "scene": row["scene"],
                    "linked_terms": row["linked_terms"],
                    "seen_at": row["seen_at"],
                    "raw_text": row["raw_text"],
                }
            )
    return q, limit, pagination, normalized_rows


def sources_table_rows(
    rows: list[dict[str, object]],
    lang: str = ALL_LANGUAGES_CODE,
) -> list[list[str]]:
    return [
        [
            source_link(row["id"], lang),
            escape(row["language_code"]),
            escape(row["source_type"]),
            language_text(row["title"], None),
            escape(row["resolution"]),
            escape(row["scene"]),
            escape(row["linked_terms"]),
            datetime_cell(row["seen_at"]),
            language_text(truncate(text(row["raw_text"], ""), size=72), row["language_code"]),
        ]
        for row in rows
    ]


def sources_table_markup(rows: list[dict[str, object]], lang: str = ALL_LANGUAGES_CODE) -> str:
    return table_html(SOURCES_TABLE_HEADERS, sources_table_rows(rows, lang))


def build_sources_payload(
    *,
    db_path: Path,
    q: str,
    limit: int,
    pagination: PaginationState,
    rows: list[dict[str, object]],
    lang: str = ALL_LANGUAGES_CODE,
) -> dict[str, object]:
    base_params = {
        "q": q.strip() or None,
        "limit": limit if limit != 50 else None,
        "lang": lang if lang != ALL_LANGUAGES_CODE else None,
    }
    revision = (
        f"{database_revision_token(db_path)}:{pagination.page}:{pagination.total_count}:"
        + ",".join(str(row["id"]) for row in rows)
    )
    return {
        "revision": revision,
        "count_label": results_summary_text(pagination),
        "pagination_html": pagination_nav(
            path="/sources",
            base_params=base_params,
            pagination=pagination,
        ),
        "table_html": sources_table_markup(rows, lang),
    }


def sources_live_payload(db_path: Path, params: dict[str, list[str]]) -> dict[str, object]:
    q, limit, pagination, rows = load_sources_rows(db_path, params)
    return build_sources_payload(
        db_path=db_path,
        q=q,
        limit=limit,
        pagination=pagination,
        rows=rows,
        lang=current_language(params),
    )


def auto_reload_script(db_path: Path, endpoint: str = "/live/revision", interval_ms: int = 4000) -> str:
    endpoint_literal = json.dumps(endpoint, ensure_ascii=False)
    revision_literal = json.dumps(database_revision_token(db_path), ensure_ascii=False)
    interval_literal = json.dumps(interval_ms)
    return f"""
<script>
  (() => {{
    const endpoint = {endpoint_literal};
    const pollIntervalMs = {interval_literal};
    const sortStateKey =
      "jpCatalog:auto-sort:" + window.location.pathname + window.location.search;
    let currentRevision = {revision_literal};
    let inFlight = false;

    function hasFocusedField() {{
      const active = document.activeElement;
      return Boolean(active && active.matches("input, textarea, select"));
    }}

    function saveSortStates() {{
      if (!window.jpCatalog || typeof window.jpCatalog.readSortState !== "function") {{
        return;
      }}
      try {{
        const states = Array.from(document.querySelectorAll("table.sortable-table")).map((table) =>
          window.jpCatalog.readSortState(table)
        );
        window.sessionStorage.setItem(sortStateKey, JSON.stringify(states));
      }} catch (_error) {{
      }}
    }}

    function restoreSortStates() {{
      if (!window.jpCatalog || typeof window.jpCatalog.applySortState !== "function") {{
        return;
      }}
      try {{
        const rawStates = window.sessionStorage.getItem(sortStateKey);
        if (!rawStates) {{
          return;
        }}
        window.sessionStorage.removeItem(sortStateKey);
        const states = JSON.parse(rawStates);
        if (!Array.isArray(states)) {{
          return;
        }}
        const tables = Array.from(document.querySelectorAll("table.sortable-table"));
        states.forEach((state, index) => {{
          window.jpCatalog.applySortState(tables[index], state);
        }});
      }} catch (_error) {{
      }}
    }}

    async function refreshPage() {{
      if (
        inFlight
        || document.hidden
        || hasFocusedField()
        || (
          window.jpCatalog
          && typeof window.jpCatalog.isSyncingAnki === "function"
          && window.jpCatalog.isSyncingAnki()
        )
      ) {{
        return;
      }}
      inFlight = true;
      try {{
        const response = await fetch(endpoint, {{
          headers: {{ "Accept": "application/json" }},
          cache: "no-store",
        }});
        if (!response.ok) {{
          return;
        }}
        const payload = await response.json();
        const nextRevision = payload && typeof payload.revision === "string" ? payload.revision : "";
        if (!nextRevision || nextRevision === currentRevision) {{
          return;
        }}
        currentRevision = nextRevision;
        saveSortStates();
        window.location.reload();
      }} catch (_error) {{
      }} finally {{
        inFlight = false;
      }}
    }}

    restoreSortStates();
    const timer = window.setInterval(refreshPage, pollIntervalMs);
    document.addEventListener("visibilitychange", () => {{
      if (!document.hidden) {{
        refreshPage();
      }}
    }});
    window.addEventListener("beforeunload", () => {{
      window.clearInterval(timer);
    }}, {{ once: true }});
  }})();
</script>
"""


def sources_live_script(endpoint: str) -> str:
    endpoint_literal = json.dumps(endpoint, ensure_ascii=False)
    return f"""
<script>
  (() => {{
    const root = document.querySelector("[data-sources-live-root]");
    if (!root) {{
      return;
    }}
    const endpoint = {endpoint_literal};
    const countNode = root.querySelector("[data-sources-count]");
    const paginationNode = root.querySelector("[data-sources-pagination]");
    const tableNode = root.querySelector("[data-sources-table]");
    let currentRevision = root.dataset.liveRevision || "";
    let inFlight = false;

    async function refreshSources() {{
      if (inFlight || document.hidden) {{
        return;
      }}
      inFlight = true;
      const existingTable = tableNode ? tableNode.querySelector("table.sortable-table") : null;
      const previousSortState =
        window.jpCatalog && typeof window.jpCatalog.readSortState === "function"
          ? window.jpCatalog.readSortState(existingTable)
          : null;
      try {{
        const response = await fetch(endpoint, {{
          headers: {{ "Accept": "application/json" }},
          cache: "no-store",
        }});
        if (!response.ok) {{
          return;
        }}
        const payload = await response.json();
        if (!payload || payload.revision === currentRevision) {{
          return;
        }}
        currentRevision = payload.revision || "";
        root.dataset.liveRevision = currentRevision;
        if (countNode) {{
          countNode.textContent = payload.count_label || "";
        }}
        if (paginationNode) {{
          paginationNode.innerHTML = payload.pagination_html || "";
        }}
        if (tableNode) {{
          tableNode.innerHTML = payload.table_html || "";
          if (window.jpCatalog && typeof window.jpCatalog.initSortableTables === "function") {{
            window.jpCatalog.initSortableTables(tableNode);
            if (previousSortState && typeof window.jpCatalog.applySortState === "function") {{
              const nextTable = tableNode.querySelector("table.sortable-table");
              window.jpCatalog.applySortState(nextTable, previousSortState);
            }}
          }}
        }}
      }} catch (_error) {{
      }} finally {{
        inFlight = false;
      }}
    }}

    const timer = window.setInterval(refreshSources, 4000);
    document.addEventListener("visibilitychange", () => {{
      if (!document.hidden) {{
        refreshSources();
      }}
    }});
    window.addEventListener("beforeunload", () => {{
      window.clearInterval(timer);
    }}, {{ once: true }});
  }})();
</script>
"""


def sources_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q, limit, pagination, rows = load_sources_rows(db_path, params)
    lang = current_language(params)

    extras = f'<input type="number" name="limit" value="{limit}" min="1" max="200">'
    payload = build_sources_payload(
        db_path=db_path,
        q=q,
        limit=limit,
        pagination=pagination,
        rows=rows,
        lang=lang,
    )
    reset_href = language_href("/sources", lang) if q.strip() or limit != 50 or pagination.page > 1 else None
    live_params = {
        "q": q.strip() or None,
        "limit": limit if limit != 50 else None,
        "page": pagination.page if pagination.page > 1 else None,
        "lang": lang if lang != ALL_LANGUAGES_CODE else None,
    }
    live_query = urlencode(
        [(key, str(value)) for key, value in live_params.items() if value is not None]
    )
    live_endpoint = f"/sources/live?{live_query}" if live_query else "/sources/live"
    body = f"""
    <div class="grid">
      <section class="panel" data-sources-live-root data-live-revision="{html.escape(str(payload["revision"]))}">
        <h2>Fontes</h2>
        {search_form(action="/sources", label="Buscar em fontes", placeholder="Ex.: battle menu, config, texto OCR", q=q, extras=extras, reset_href=reset_href, lang=lang)}
        <div class="results-toolbar">
          <p class="results-summary" data-sources-count>{html.escape(str(payload["count_label"]))}</p>
          <div data-sources-pagination>{payload["pagination_html"]}</div>
        </div>
        <div data-sources-table>{payload["table_html"]}</div>
      </section>
    </div>
    """
    return layout(
        title="Fontes",
        body=body,
        active_path="/sources",
        extra_scripts=sources_live_script(live_endpoint),
        lang=lang,
        language_bar=language_bar_html(
            db_path,
            active_path="/sources",
            lang=lang,
            extra_params={"q": q.strip() or None, "limit": limit if limit != 50 else None},
        ),
    )


def source_language_form(
    connection: sqlite3.Connection,
    *,
    source_id: int,
    current_language: str,
    lang: str = ALL_LANGUAGES_CODE,
) -> str:
    """Change a source's language and re-run its analysis from the page (plan item D4)."""
    options = [
        f'<option value="{AUTO_LANGUAGE_CODE}">auto (detectar pelo texto)</option>'
    ]
    seen = set()
    for row in list_language_rows(connection):
        code = str(row["code"])
        seen.add(code)
        selected = " selected" if code == current_language else ""
        options.append(
            f'<option value="{html.escape(code)}"{selected}>{html.escape(get_language(code).name)} ({html.escape(code)})</option>'
        )
    if current_language not in seen:
        options.append(f'<option value="{html.escape(current_language)}" selected>{html.escape(current_language)}</option>')
    return f"""
        <form class="inline-action source-language-form" action="/actions/sources/{source_id}/language" method="post">
          <label>Idioma da fonte
            <select name="language">{"".join(options)}</select>
          </label>
          <label><input type="checkbox" name="reanalyze" value="1" checked> Reanalisar segmentos e candidatos</label>
          <label><input type="checkbox" name="promote" value="1"> Criar termos ja</label>
          {hidden_language_input(lang)}
          <button type="submit">Salvar idioma</button>
        </form>
    """


def source_detail_page(
    db_path: Path,
    source_id: int,
    params: dict[str, list[str]] | None = None,
) -> str | None:
    params = params or {}
    lang = current_language(params)
    with db_connect(db_path) as connection:
        source = connection.execute(
            """
            SELECT
                id,
                source_type,
                language_code,
                language_confidence,
                title,
                game,
                scene,
                source_path,
                image_width,
                image_height,
                raw_text,
                notes,
                captured_at,
                created_at,
                (
                    SELECT translated_text
                    FROM source_translations AS st
                    WHERE st.source_id = sources.id
                      AND st.target_language = 'en'
                      AND st.status = 'translated'
                    ORDER BY st.updated_at DESC, st.id DESC
                    LIMIT 1
                ) AS translated_text_en,
                (
                    SELECT status
                    FROM source_translations AS st
                    WHERE st.source_id = sources.id
                      AND st.target_language = 'en'
                    ORDER BY st.updated_at DESC, st.id DESC
                    LIMIT 1
                ) AS translated_text_en_status,
                (
                    SELECT provider
                    FROM source_translations AS st
                    WHERE st.source_id = sources.id
                      AND st.target_language = 'en'
                    ORDER BY st.updated_at DESC, st.id DESC
                    LIMIT 1
                ) AS translated_text_en_provider
            FROM sources
            WHERE id = ?
            """,
            (source_id,),
        ).fetchone()
        if source is None:
            return None
        resolution_label = source_resolution_label(connection, source)
        language_form = source_language_form(
            connection,
            source_id=source_id,
            current_language=str(source["language_code"]),
            lang=lang,
        )
        # One pass over the segment's candidates: a separate "approved" subquery
        # let SQLite pick the approval_status index and scan every approved
        # candidate in the catalog per segment (seconds on a long capture).
        analysis_rows = connection.execute(
            """
            SELECT
                sas.segment_index,
                COALESCE(sas.language_code, '-') AS language_code,
                sas.raw_text,
                COUNT(sac.id) AS candidate_count,
                COALESCE(SUM(sac.approval_status = 'approved'), 0) AS approved_count
            FROM source_analysis_segments AS sas
            LEFT JOIN source_analysis_candidates AS sac
                ON sac.source_analysis_segment_id = sas.id
            WHERE sas.source_id = ?
            GROUP BY sas.id
            ORDER BY sas.segment_index ASC
            LIMIT ?
            """,
            (source_id, DETAIL_TABLE_LIMIT),
        ).fetchall()

        term_rows = connection.execute(
            """
            SELECT
                st.term_id,
                t.language_code,
                t.lemma,
                COALESCE(t.reading, '-') AS reading,
                COALESCE(t.meaning_pt, '-') AS meaning_pt,
                st.surface_form,
                st.count_in_source,
                COALESCE(st.confidence, '-') AS confidence
            FROM source_terms AS st
            JOIN terms AS t
                ON t.id = st.term_id
            WHERE st.source_id = ?
            ORDER BY st.count_in_source DESC, t.lemma COLLATE NOCASE ASC
            """,
            (source_id,),
        ).fetchall()
        segment_rows = connection.execute(
            """
            SELECT
                ss.id,
                ss.segment_text,
                COALESCE(ss.language_code, ?) AS segment_language,
                COALESCE(ss.reading, '-') AS segment_reading,
                COUNT(sst.id) AS linked_terms,
                COALESCE(group_concat(DISTINCT t.lemma), '-') AS linked_lemmas
            FROM source_segments AS ss
            LEFT JOIN source_segment_terms AS sst
                ON sst.source_segment_id = ss.id
            LEFT JOIN terms AS t
                ON t.id = sst.term_id
            WHERE ss.source_id = ?
            GROUP BY ss.id, ss.segment_text, ss.language_code, ss.reading
            ORDER BY ss.id ASC
            """,
            (str(source["language_code"]), source_id),
        ).fetchall()
        segmented_term_rows = connection.execute(
            """
            SELECT
                ss.segment_text,
                COALESCE(ss.language_code, ?) AS segment_language,
                COALESCE(ss.reading, '-') AS segment_reading,
                t.id AS term_id,
                t.language_code,
                t.lemma,
                COALESCE(t.reading, '-') AS term_reading,
                COALESCE(t.meaning_pt, '-') AS meaning_pt,
                sst.surface_form,
                sst.count_in_segment,
                COALESCE(sst.confidence, '-') AS confidence
            FROM source_segment_terms AS sst
            JOIN source_segments AS ss
                ON ss.id = sst.source_segment_id
            JOIN terms AS t
                ON t.id = sst.term_id
            WHERE ss.source_id = ?
            ORDER BY ss.id ASC, sst.id ASC
            """,
            (str(source["language_code"]), source_id),
        ).fetchall()
        kanji_rows = connection.execute(
            """
            SELECT
                k.id,
                k.language_code,
                k.literal,
                sk.count_in_source,
                COALESCE(ks.priority_score, 0) AS priority_score
            FROM source_kanji AS sk
            JOIN kanji AS k
                ON k.id = sk.kanji_id
            LEFT JOIN kanji_stats AS ks
                ON ks.kanji_id = k.id
            WHERE sk.source_id = ?
            ORDER BY
                sk.count_in_source DESC,
                COALESCE(ks.priority_score, 0) DESC,
                k.literal COLLATE NOCASE ASC
            """,
            (source_id,),
        ).fetchall()

    image_block = ""
    source_path = text(source["source_path"], "")
    resolved_source_path = resolve_repo_local_path(source_path)
    if resolved_source_path is not None and resolved_source_path.exists():
        image_block = f"""
        <section class="panel">
          <h3>Imagem</h3>
          <img class="source-image" src="/sources/{source_id}/image" alt="Imagem da fonte {source_id}">
        </section>
        """

    source_language = str(source["language_code"] or languages.DEFAULT_LANGUAGE_CODE)
    language_profile = get_language(source_language)
    # A capture mixes languages, so every term and segment shows its own; the
    # reading columns stay only when one of those languages has readings.
    show_readings = any_language_has_readings(
        [source_language]
        + [row["language_code"] for row in term_rows]
        + [row["segment_language"] for row in segment_rows]
        + [row["language_code"] for row in segmented_term_rows]
    )

    def reading_cell(value: object | None, code: object | None) -> list[str]:
        return [reading_text(value, code)] if show_readings else []

    table_rows = [
        [
            term_link(row["term_id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            *reading_cell(row["reading"], row["language_code"]),
            escape(row["meaning_pt"]),
            language_text(row["surface_form"], row["language_code"]),
            escape(row["count_in_source"]),
            escape(row["confidence"]),
        ]
        for row in term_rows
    ]
    segment_summary_rows = [
        [
            language_text(row["segment_text"], row["segment_language"]),
            escape(row["segment_language"]),
            *reading_cell(row["segment_reading"], row["segment_language"]),
            escape(row["linked_terms"]),
            language_text(row["linked_lemmas"], row["segment_language"]),
        ]
        for row in segment_rows
    ]
    segmented_term_table_rows = [
        [
            language_text(row["segment_text"], row["segment_language"]),
            *reading_cell(row["segment_reading"], row["segment_language"]),
            term_link(row["term_id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            *reading_cell(row["term_reading"], row["language_code"]),
            escape(row["meaning_pt"]),
            language_text(row["surface_form"], row["language_code"]),
            escape(row["count_in_segment"]),
            escape(row["confidence"]),
        ]
        for row in segmented_term_rows
    ]
    term_headers = [
        "Termo",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Significado",
        "Forma vista",
        "Count",
        "Confianca",
    ]
    segment_headers = [
        "Segmento bruto",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Termos ligados",
        "Lemmas",
    ]
    segmented_term_headers = [
        "Segmento bruto",
        *(["Leitura do segmento"] if show_readings else []),
        "Termo",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Significado",
        "Forma ligada",
        "Count",
        "Confianca",
    ]
    kanji_table_rows = [
        [
            kanji_link(row["id"], row["literal"], row["language_code"], lang),
            escape(row["count_in_source"]),
            escape(format_score(row["priority_score"])),
        ]
        for row in kanji_rows
    ]
    analysis_table_rows = [
        [
            escape(row["segment_index"]),
            escape(row["language_code"]),
            language_text(truncate(str(row["raw_text"]), size=80), row["language_code"]),
            escape(row["candidate_count"]),
            escape(row["approved_count"]),
        ]
        for row in analysis_rows
    ]
    # Characters only exist for languages that have them; a Latin source with
    # no kanji rows gets no empty kanji panel or shortcut.
    show_characters = bool(kanji_rows) or language_profile.has_characters
    characters = characters_label(source_language if language_profile.has_characters else ALL_LANGUAGES_CODE)
    kanji_panel = (
        f"""
      <section class="panel panel-side">
        <h3>{html.escape(characters)} ligados a esta fonte</h3>
        {table_html([characters, "Count", "Score"], kanji_table_rows)}
      </section>
        """
        if show_characters
        else ""
    )
    kanji_shortcut = (
        f'<a class="button-link" href="{html.escape(language_href("/kanji", lang), quote=True)}">Abrir {html.escape(characters.lower())}</a>'
        if show_characters
        else ""
    )
    confidence_label = (
        f" (confianca {float(source['language_confidence']):.2f})"
        if source["language_confidence"] is not None
        else ""
    )
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        {notice_banner(params)}
        <h2 dir="auto">{escape(source["title"] or "(sem titulo)")}</h2>
        <ul class="meta-list">
          <li><strong>Tipo</strong>{escape(source["source_type"])}</li>
          <li><strong>Idioma</strong>{escape(language_profile.name)} ({escape(source["language_code"])}){html.escape(confidence_label)}</li>
          <li><strong>Jogo / perfil</strong>{escape(source["game"] or "-")}</li>
          <li><strong>Resolucao original</strong>{escape(resolution_label)}</li>
          <li><strong>Cena</strong>{escape(source["scene"] or "-")}</li>
          <li><strong>Capturado em</strong>{escape(format_datetime_display(source["captured_at"] or source["created_at"]))}</li>
          <li><strong>Arquivo</strong><span class="mono">{escape(source["source_path"] or "-")}</span></li>
        </ul>
        {language_form}
      </section>

      <section class="panel panel-side">
        <h2>Atalhos</h2>
        <div class="stack">
          <a class="button-link" href="{html.escape(language_href("/sources", lang), quote=True)}">Voltar para fontes</a>
          {kanji_shortcut}
          <a class="button-link" href="{html.escape(language_href("/terms", lang), quote=True)}">Abrir termos</a>
          <a class="button-link" href="{html.escape(language_href("/flashcards", lang), quote=True)}">Abrir cards</a>
        </div>
        <form class="inline-action source-delete-form" action="/actions/sources/{source_id}/delete" method="post" onsubmit="return confirm('Excluir a fonte #{source_id} de vez? Segmentos e vinculos de termos serao removidos; os termos ficam.');">
          <label><input type="checkbox" name="delete_image" value="1" checked> Apagar tambem a imagem</label>
          {hidden_language_input(lang)}
          <button type="submit">Excluir fonte</button>
        </form>
      </section>

      {image_block}

      <section class="panel panel-wide">
        <h3>Texto bruto</h3>
        <pre{language_attrs(source_language) if text(source["raw_text"], "") else ""}>{html.escape(text(source["raw_text"], "(sem texto bruto)"))}</pre>
      </section>

      <section class="panel panel-side">
        <h3>Tradução EN</h3>
        <p><strong>Status:</strong> {escape(source["translated_text_en_status"] or "missing")}</p>
        <p><strong>Provider:</strong> {escape(source["translated_text_en_provider"] or "-")}</p>
        <pre>{html.escape(text(source["translated_text_en"], "(sem tradução em inglês)"))}</pre>
        <form class="inline-action source-translate-form" action="/actions/sources/{source_id}/translate" method="post">
          {hidden_language_input(lang)}
          <button type="submit">{"Traduzir de novo" if source["translated_text_en"] else "Traduzir"}</button>
        </form>
      </section>

      <section class="panel panel-side">
        <h3>Notas</h3>
        <pre>{html.escape(text(source["notes"], "(sem notas)"))}</pre>
      </section>

      <section class="panel panel-wide">
        <h3>Segmentos analisados (rascunho por idioma)</h3>
        {table_html(["#", "Idioma", "Texto", "Candidatos", "Aprovados"], analysis_table_rows)}
        {truncation_note(len(analysis_rows), len(analysis_rows)) if len(analysis_rows) >= DETAIL_TABLE_LIMIT else ""}
      </section>

      <section class="panel panel-wide">
        <h3>Segmentos revisados desta fonte</h3>
        {table_html(segment_headers, segment_summary_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Termos ligados a esta fonte</h3>
        {table_html(term_headers, table_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Decomposicao termo a termo</h3>
        {table_html(segmented_term_headers, segmented_term_table_rows)}
      </section>

      {kanji_panel}
    </div>
    """
    return layout(title=f"Fonte {source_id}", body=body, active_path="/sources", lang=lang)


FLASHCARD_SEARCH_COLUMNS = (
    "html_text(f.front_text)",
    "html_text(f.back_text)",
    "t.lemma",
    "t.reading",
    "t.meaning_pt",
)


def flashcard_filters(
    *,
    lang: str,
    status: str,
    q: str,
) -> tuple[list[str], list[object]]:
    """WHERE parts shared by the cards page and ``/api/flashcards``."""
    filters: list[str] = []
    sql_params: list[object] = []
    lang_sql, lang_params = language_filter_clause("t.language_code", lang)
    if lang_sql:
        filters.append(lang_sql)
        sql_params.extend(lang_params)
    if status != "all":
        filters.append("f.export_status = ?")
        sql_params.append(status)
    if q.strip():
        search_sql, search_params = folded_like_filter(FLASHCARD_SEARCH_COLUMNS, q)
        filters.append(search_sql)
        sql_params.extend(search_params)
    return filters, sql_params


def flashcards_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    status = first_value(params, "status", "all")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)

    filters, sql_params = flashcard_filters(lang=lang, status=status, q=q)

    where_clause = ""
    if filters:
        where_clause = "WHERE " + " AND ".join(part.strip() for part in filters)

    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM flashcards AS f
            JOIN terms AS t
                ON t.id = f.term_id
            {where_clause}
            """,
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                f.id,
                t.id AS term_id,
                t.lemma,
                t.language_code,
                COALESCE(t.reading, '-') AS reading,
                COALESCE(ts.priority_score, 0) AS priority_score,
                f.template_name,
                f.deck_name,
                f.export_status,
                f.front_text,
                COALESCE(f.source_id, '-') AS source_id
            FROM flashcards AS f
            JOIN terms AS t
                ON t.id = f.term_id
            LEFT JOIN term_stats AS ts
                ON ts.term_id = f.term_id
            {where_clause}
            ORDER BY
            """ + FLASHCARD_PRIORITY_ORDER_SQL + """
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()

    base_params = {
        "q": q.strip() or None,
        "status": status if status != "all" else None,
        "limit": limit if limit != 50 else None,
        "lang": lang if lang != ALL_LANGUAGES_CODE else None,
    }
    pagination_markup = pagination_nav(
        path="/flashcards",
        base_params=base_params,
        pagination=pagination,
    )
    reset_href = (
        language_href("/flashcards", lang)
        if any(value is not None for key, value in base_params.items() if key != "lang") or pagination.page > 1
        else None
    )
    extras = f"""
      <div class="filters">
        <select name="status">
          <option value="all" {"selected" if status == "all" else ""}>Todos</option>
          <option value="pending" {"selected" if status == "pending" else ""}>Pending</option>
          <option value="exported" {"selected" if status == "exported" else ""}>Exported</option>
        </select>
        <input type="number" name="limit" value="{limit}" min="1" max="200">
      </div>
    """
    show_readings = language_has_readings(lang)
    table_rows = [
        [
            term_link(row["term_id"], row["lemma"], row["language_code"], lang),
            escape(row["language_code"]),
            *([reading_text(row["reading"], row["language_code"])] if show_readings else []),
            escape(format_score(row["priority_score"])),
            escape(row["template_name"]),
            escape(row["deck_name"]),
            status_chip(text(row["export_status"])),
            escape(row["source_id"]),
            language_text(truncate(html_fragment_text(row["front_text"]), size=52), row["language_code"]),
        ]
        for row in rows
    ]
    headers = [
        "Termo",
        "Idioma",
        *(["Leitura"] if show_readings else []),
        "Score",
        "Template",
        "Deck",
        "Status",
        "Fonte",
        "Frente",
    ]
    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <h2>Flashcards</h2>
        {sync_anki_action(next_path=language_href("/flashcards", lang))}
        {search_form(action="/flashcards", label="Buscar cards", placeholder="Ex.: guardar, persona, progress", q=q, extras=extras, reset_href=reset_href, lang=lang)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)}</p>
          {pagination_markup}
        </div>
        {table_html(headers, table_rows)}
      </section>
    </div>
    """
    return layout(
        title="Flashcards",
        body=body,
        active_path="/flashcards",
        extra_scripts=auto_reload_script(db_path),
        lang=lang,
        language_bar=language_bar_html(
            db_path,
            active_path="/flashcards",
            lang=lang,
            extra_params={key: value for key, value in base_params.items() if key != "lang"},
        ),
    )


def skip_rule_form(kind: str, pattern: str, *, op: str, label: str) -> str:
    return f"""
        <form class="inline-action" action="/actions/capture-skip-rules" method="post">
          <input type="hidden" name="op" value="{html.escape(op, quote=True)}">
          <input type="hidden" name="kind" value="{html.escape(kind, quote=True)}">
          <input type="hidden" name="pattern" value="{html.escape(pattern, quote=True)}">
          <button type="submit">{html.escape(label)}</button>
        </form>
    """


def skip_rules_html(rules: list[sqlite3.Row]) -> str:
    env_rows = [
        [escape(kind), f'<span class="mono">{html.escape(pattern)}</span>', f"variavel {html.escape(env_name)}"]
        for kind, env_name in (
            ("process", lang_jp.CAPTURE_SKIP_PROCESSES_ENV),
            ("title", lang_jp.CAPTURE_SKIP_TITLES_ENV),
        )
        for pattern in lang_jp.env_capture_skip_patterns(env_name)
    ]
    if not rules and not env_rows:
        return '<p class="muted">Nenhuma janela ignorada: tudo na tela e capturado.</p>'
    rows = [
        [
            escape(row["kind"]),
            f'<span class="mono">{html.escape(str(row["pattern"]))}</span>',
            skip_rule_form(str(row["kind"]), str(row["pattern"]), op="remove", label="Remover"),
        ]
        for row in rules
    ]
    return table_html(["Tipo", "Padrao", "Acoes"], rows + env_rows)


def skip_process_button(process_name: str | None, rules: list[sqlite3.Row]) -> str:
    process = lang_jp.normalize_token(process_name).lower()
    if not process:
        return ""
    if any(row["kind"] == "process" and row["pattern"] == process for row in rules):
        return ' <span class="muted">(ignorado)</span>'
    return skip_rule_form("process", process, op="add", label="Ignorar app")


def capture_page(db_path: Path, params: dict[str, list[str]]) -> str:
    """Capture settings: which languages the worker keeps, the OCR plan, window profiles."""
    with db_connect(db_path) as connection:
        language_rows = list_language_rows(connection)
        capture_codes = lang_jp.capture_language_codes(connection)
        profiles = lang_jp.list_capture_profiles(connection)
        skip_rules = lang_jp.list_capture_skip_rules(connection)
        recent_jobs = connection.execute(
            """
            SELECT
                cj.id, cj.status, cj.captured_at, cj.window_title, cj.process_name,
                cj.capture_profile_id,
                (SELECT name FROM capture_profiles AS cp WHERE cp.id = cj.capture_profile_id) AS profile_name,
                (SELECT s.id FROM sources AS s WHERE s.capture_job_id = cj.id LIMIT 1) AS source_id,
                (SELECT s.language_code FROM sources AS s WHERE s.capture_job_id = cj.id LIMIT 1) AS source_language
            FROM capture_jobs AS cj
            ORDER BY cj.id DESC
            LIMIT 12
            """
        ).fetchall()
    override = os.environ.get(lang_jp.CAPTURE_LANGUAGES_ENV, "").strip()
    plan = plan_ocr_engines(capture_codes)

    gloss_language = languages.base_language_code(lang_jp.DEFAULT_TERM_TRANSLATION_TARGET)
    checkboxes = []
    for row in language_rows:
        code = str(row["code"])
        profile = get_language(code)
        checked = " checked" if code in capture_codes else ""
        notes = []
        if not row["enabled"]:
            notes.append("idioma desativado")
        if languages.base_language_code(code) == gloss_language:
            notes.append("idioma dos significados")
        notes_html = "".join(f' <span class="muted">({html.escape(note)})</span>' for note in notes)
        checkboxes.append(
            f'<label class="capture-language"><input type="checkbox" name="codes" value="{html.escape(code)}"{checked}> '
            f"{html.escape(profile.name)} <span class=\"muted\">{html.escape(code)}</span>{notes_html}</label>"
        )
    override_note = (
        f'<div class="notice-banner">A variavel {html.escape(lang_jp.CAPTURE_LANGUAGES_ENV)}={html.escape(override)} '
        "esta definida no ambiente do worker e sobrepoe estas opcoes enquanto existir.</div>"
        if override
        else ""
    )
    engines_html = "".join(f"<li><span class=\"mono\">{html.escape(name)}</span></li>" for name in plan.engine_names) or "<li>-</li>"
    unsupported_html = (
        f'<p class="muted">Sem motor de OCR para: {html.escape(", ".join(plan.unsupported))}. '
        "O texto nesses idiomas so entra por <code>ingest-text</code>.</p>"
        if plan.unsupported
        else ""
    )

    profile_rows = []
    for row in profiles:
        state = "ativo" if row["enabled"] else "inativo"
        toggle_label = "Desativar" if row["enabled"] else "Ativar"
        actions = f"""
            <form class="inline-action" action="/actions/capture-profiles/toggle" method="post">
              <input type="hidden" name="id" value="{row['id']}">
              <input type="hidden" name="enabled" value="{0 if row['enabled'] else 1}">
              <button type="submit">{toggle_label}</button>
            </form>
            <form class="inline-action" action="/actions/capture-profiles/delete" method="post" onsubmit="return confirm('Remover o perfil {html.escape(str(row['name']), quote=True)}?');">
              <input type="hidden" name="id" value="{row['id']}">
              <button type="submit">Remover</button>
            </form>
        """
        profile_rows.append(
            [
                escape(row["name"]),
                escape(row["window_title_pattern"] or "-"),
                escape(row["process_name_pattern"] or "-"),
                escape(row["language_codes"]),
                escape(row["priority"]),
                status_chip(state),
                actions,
            ]
        )
    job_rows = [
        [
            escape(row["id"]),
            status_chip(text(row["status"])),
            language_text(truncate(text(row["window_title"], "-"), size=60), None),
            escape(row["process_name"] or "-") + skip_process_button(row["process_name"], skip_rules),
            escape(row["profile_name"] or "-"),
            (
                f'<a href="/sources/{row["source_id"]}">#{row["source_id"]} ({escape(row["source_language"])})</a>'
                if row["source_id"] is not None
                else "-"
            ),
            datetime_cell(row["captured_at"]),
        ]
        for row in recent_jobs
    ]
    language_options = "".join(
        f'<option value="{html.escape(str(row["code"]))}">{html.escape(get_language(str(row["code"])).name)} ({html.escape(str(row["code"]))})</option>'
        for row in language_rows
    )
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        {notice_banner(params)}
        <h2>Idiomas capturados</h2>
        <p>O worker guarda o texto de tela nos idiomas marcados e descarta o resto. A escolha fica no banco (<code>languages.capture_enabled</code>) e vale para o tray, a CLI e esta pagina.</p>
        {override_note}
        <form class="capture-languages-form" action="/actions/capture-languages" method="post">
          <div class="capture-language-grid">{"".join(checkboxes)}</div>
          <div class="action-row">
            <button type="submit">Salvar idiomas</button>
            <button type="submit" name="preset" value="all">Todos os idiomas</button>
            <button type="submit" name="preset" value="ja">Somente japones</button>
          </div>
        </form>
      </section>

      <section class="panel panel-side">
        <h2>Motores de OCR</h2>
        <p>Escolhidos automaticamente pelo conjunto acima: meikiocr para japones, um leitor EasyOCR por grupo de idiomas compativeis.</p>
        <ul class="meta-list">{engines_html}</ul>
        {unsupported_html}
        <p class="footer-note">Captura atual: {html.escape(", ".join(capture_codes))}</p>
      </section>

      <section class="panel panel-wide">
        <h2>Perfis de captura por janela</h2>
        <p>Um perfil prende uma janela (titulo ou processo) a um conjunto fixo de idiomas: com um idioma a deteccao e pulada, com varios ela fica restrita a eles.</p>
        {table_html(["Nome", "Titulo da janela", "Processo", "Idiomas", "Prioridade", "Estado", "Acoes"], profile_rows)}
        <form class="search capture-profile-form" action="/actions/capture-profiles/save" method="post">
          <label>Novo perfil (ou mesmo nome para substituir)</label>
          <div class="inputs">
            <input type="text" name="name" placeholder="Nome, ex.: Dragon Quest ES" required>
            <input type="text" name="window_pattern" placeholder="Titulo da janela, ex.: *dragon quest*">
            <input type="text" name="process_pattern" placeholder="Processo, ex.: dq11.exe">
            <input type="text" name="languages" placeholder="Idiomas, ex.: es,en" required list="capture-language-codes">
            <datalist id="capture-language-codes">{language_options}</datalist>
            <input type="number" name="priority" value="100" min="0" max="9999" title="Prioridade (menor vence)">
            <div class="search-actions"><button type="submit">Salvar perfil</button></div>
          </div>
        </form>
      </section>

      <section class="panel panel-wide">
        <h2>Janelas ignoradas</h2>
        <p>Capturas dessas janelas sao descartadas antes do OCR (<code>reason=skipped_window</code> no log). <strong>Processo</strong> compara o nome exato do executavel; <strong>titulo</strong> procura o texto dentro do titulo da janela. A lista fica no banco (<code>capture_skip_rules</code>), comeca vazia e vale para o tray, a CLI (<code>skip-window</code>) e esta pagina.</p>
        {skip_rules_html(skip_rules)}
        <form class="search capture-skip-form" action="/actions/capture-skip-rules" method="post">
          <input type="hidden" name="op" value="add">
          <label>Ignorar uma janela</label>
          <div class="inputs">
            <select name="kind">
              <option value="process">Processo (nome exato)</option>
              <option value="title">Titulo contem</option>
            </select>
            <input type="text" name="pattern" placeholder="ex.: claude.exe ou Lang Catalog" required>
            <div class="search-actions"><button type="submit">Ignorar</button></div>
          </div>
        </form>
      </section>

      <section class="panel panel-wide">
        <h2>Capturas recentes</h2>
        <p>Titulo e processo da janela em primeiro plano em cada captura: use-os para criar perfis.</p>
        {table_html(["Job", "Status", "Janela", "Processo", "Perfil", "Fonte", "Quando"], job_rows)}
      </section>
    </div>
    """
    return layout(
        title="Captura",
        body=body,
        active_path="/capture",
        language_bar=language_bar_html(db_path, active_path="/capture", lang=ALL_LANGUAGES_CODE),
    )


def api_capture_payload(db_path: Path) -> dict[str, object]:
    with db_connect(db_path) as connection:
        capture_codes = lang_jp.capture_language_codes(connection)
        profiles = lang_jp.list_capture_profiles(connection)
        skip_patterns = lang_jp.capture_skip_patterns(connection)
    plan = plan_ocr_engines(capture_codes)
    return {
        "capture_languages": list(capture_codes),
        "override_env": os.environ.get(lang_jp.CAPTURE_LANGUAGES_ENV) or None,
        "skip_rules": {kind: list(values) for kind, values in skip_patterns.items()},
        "ocr_plan": {
            "backend": plan.backend,
            "engines": list(plan.engine_names),
            "meikiocr": plan.use_meikiocr,
            "easyocr_groups": [list(group) for group in plan.easyocr_groups],
            "unsupported": list(plan.unsupported),
        },
        "profiles": [
            {
                "id": row["id"],
                "name": row["name"],
                "window_title_pattern": row["window_title_pattern"],
                "process_name_pattern": row["process_name_pattern"],
                "language_codes": lang_jp.parse_language_code_list(row["language_codes"]),
                "enabled": bool(row["enabled"]),
                "priority": row["priority"],
                "notes": row["notes"],
            }
            for row in profiles
        ],
    }


def api_language_entry(row: sqlite3.Row) -> dict[str, object]:
    profile = get_language(str(row["code"]))
    return {
        "code": row["code"],
        "name": row["name"],
        "native_name": row["native_name"],
        "script": row["script"],
        "deck_name": row["deck_name"],
        "enabled": bool(row["enabled"]),
        "capture_enabled": bool(row["capture_enabled"]),
        "word_delimited": profile.word_delimited,
        "has_readings": profile.has_readings,
        "has_characters": profile.has_characters,
        "rtl": profile.rtl,
        "term_count": row["term_count"],
        "source_count": row["source_count"],
        "pending_flashcards": row["pending_flashcards"],
    }


def api_languages_payload(db_path: Path) -> dict[str, object]:
    with db_connect(db_path) as connection:
        rows = list_language_rows(connection)
    return {"languages": [api_language_entry(row) for row in rows]}


def api_page_meta(pagination: PaginationState, lang: str, **extra: object) -> dict[str, object]:
    return {
        "lang": lang,
        "page": pagination.page,
        "limit": pagination.limit,
        "total": pagination.total_count,
        "pages": pagination.total_pages,
        **extra,
    }


def api_terms_payload(db_path: Path, params: dict[str, list[str]]) -> dict[str, object]:
    """Terms as JSON: ``?lang=``, ``?q=``, ``?order=priority|recent|lemma``, ``?limit=``, ``?page=``."""
    q = first_value(params, "q")
    order = first_value(params, "order", "priority")
    limit = parse_limit(first_value(params, "limit", "50"), maximum=500)
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)
    order_clause = {
        "priority": TERM_PRIORITY_ORDER_SQL.strip(),
        "recent": "julianday(last_seen_at) DESC, id DESC",
        "lemma": "lemma COLLATE NOCASE ASC, id ASC",
    }.get(order, TERM_PRIORITY_ORDER_SQL.strip())
    filters: list[str] = []
    sql_params: list[object] = []
    lang_sql, lang_params = language_filter_clause("language_code", lang)
    if lang_sql:
        filters.append(lang_sql)
        sql_params.extend(lang_params)
    if q.strip():
        search_sql, search_params = folded_like_filter(("lemma", "reading", "meaning_pt"), q)
        filters.append(search_sql)
        sql_params.extend(search_params)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"SELECT COUNT(*) FROM v_terms_overview {where_clause}",
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                id, language_code, lemma, reading, meaning_pt, pos,
                total_occurrences, distinct_sources, distinct_forms,
                priority_score, first_seen_at, last_seen_at
            FROM v_terms_overview
            {where_clause}
            ORDER BY {order_clause}
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()
    return {
        "meta": api_page_meta(pagination, lang, q=q, order=order),
        "terms": [dict(row) for row in rows],
    }


def api_term_detail_payload(db_path: Path, term_id: int) -> dict[str, object] | None:
    with db_connect(db_path) as connection:
        term = connection.execute(
            """
            SELECT
                v.id, v.language_code, v.lemma, v.reading, v.meaning_pt, v.pos, t.notes,
                v.total_occurrences, v.distinct_sources, v.distinct_forms,
                v.priority_score, v.first_seen_at, v.last_seen_at
            FROM v_terms_overview AS v
            JOIN terms AS t ON t.id = v.id
            WHERE v.id = ?
            """,
            (term_id,),
        ).fetchone()
        if term is None:
            return None
        forms = connection.execute(
            "SELECT surface_form, reading FROM term_forms WHERE term_id = ? ORDER BY surface_form",
            (term_id,),
        ).fetchall()
        occurrences = connection.execute(
            """
            SELECT
                st.source_id, st.surface_form, st.count_in_source, st.confidence,
                s.title, s.game, s.scene, s.language_code AS source_language_code,
                COALESCE(s.captured_at, s.created_at) AS seen_at
            FROM source_terms AS st
            JOIN sources AS s ON s.id = st.source_id
            WHERE st.term_id = ?
            ORDER BY st.count_in_source DESC, julianday(seen_at) DESC, st.id DESC
            LIMIT 200
            """,
            (term_id,),
        ).fetchall()
        cards = connection.execute(
            """
            SELECT id, template_name, deck_name, export_status, anki_note_id
            FROM flashcards
            WHERE term_id = ?
            ORDER BY id
            """,
            (term_id,),
        ).fetchall()
    payload = dict(term)
    payload["language"] = api_language_entry_from_profile(str(term["language_code"]))
    payload["forms"] = [dict(row) for row in forms]
    payload["occurrences"] = [dict(row) for row in occurrences]
    payload["flashcards"] = [dict(row) for row in cards]
    return payload


def api_language_entry_from_profile(code: str) -> dict[str, object]:
    profile = get_language(code)
    return {
        "code": profile.code,
        "name": profile.name,
        "native_name": profile.native_name,
        "script": profile.script,
        "word_delimited": profile.word_delimited,
        "has_readings": profile.has_readings,
        "has_characters": profile.has_characters,
        "rtl": profile.rtl,
    }


def api_sources_payload(db_path: Path, params: dict[str, list[str]]) -> dict[str, object]:
    q, limit, pagination, rows = load_sources_rows(db_path, params)
    lang = current_language(params)
    return {
        "meta": api_page_meta(pagination, lang, q=q),
        "sources": [
            {
                "id": row["id"],
                "language_code": row["language_code"],
                "source_type": row["source_type"],
                "title": row["title"],
                "scene": row["scene"],
                "linked_terms": row["linked_terms"],
                "seen_at": row["seen_at"],
                "raw_text": row["raw_text"],
                "image_url": f"/sources/{row['id']}/image",
            }
            for row in rows
        ],
    }


def api_flashcards_payload(db_path: Path, params: dict[str, list[str]]) -> dict[str, object]:
    """Cards as JSON: ``?lang=``, ``?status=``, ``?q=``, ``?limit=``, ``?page=``."""
    q = first_value(params, "q")
    status = first_value(params, "status", "all")
    limit = parse_limit(first_value(params, "limit", "50"), maximum=500)
    page = parse_page(first_value(params, "page", "1"))
    lang = current_language(params)
    filters, sql_params = flashcard_filters(lang=lang, status=status, q=q)
    where_clause = f"WHERE {' AND '.join(filters)}" if filters else ""
    with db_connect(db_path) as connection:
        total_count = connection.execute(
            f"""
            SELECT COUNT(*)
            FROM flashcards AS f
            JOIN terms AS t ON t.id = f.term_id
            {where_clause}
            """,
            sql_params,
        ).fetchone()[0]
        pagination = build_pagination(total_count=total_count, limit=limit, page=page)
        rows = connection.execute(
            f"""
            SELECT
                f.id, f.term_id, t.language_code, t.lemma, t.reading, t.meaning_pt,
                f.template_name, f.deck_name, f.note_type, f.front_text, f.back_text,
                f.tags, f.export_status, f.anki_note_id, f.source_id,
                COALESCE(ts.priority_score, 0) AS priority_score
            FROM flashcards AS f
            JOIN terms AS t ON t.id = f.term_id
            LEFT JOIN term_stats AS ts ON ts.term_id = f.term_id
            {where_clause}
            ORDER BY
            """ + FLASHCARD_PRIORITY_ORDER_SQL + """
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()
    return {
        "meta": api_page_meta(pagination, lang, status=status, q=q),
        "flashcards": [dict(row) for row in rows],
    }


class CatalogRequestHandler(BaseHTTPRequestHandler):
    db_path: Path = DEFAULT_DB_PATH

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        path = parsed.path.rstrip("/") or "/"

        if path == "/":
            self.respond_html(dashboard_page(self.db_path, params))
            return
        if path == "/live/revision":
            self.respond_json(live_revision_payload(self.db_path))
            return
        if path == "/api/languages":
            self.respond_json(api_languages_payload(self.db_path))
            return
        if path == "/api/capture":
            self.respond_json(api_capture_payload(self.db_path))
            return
        if path == "/capture":
            self.respond_html(capture_page(self.db_path, params))
            return
        if path == "/api/terms":
            self.respond_json(api_terms_payload(self.db_path, params))
            return
        if path.startswith("/api/terms/"):
            term_id = self.parse_int_tail(path, "/api/terms/")
            payload = api_term_detail_payload(self.db_path, term_id) if term_id is not None else None
            if payload is None:
                self.respond_json({"error": "term_not_found"}, status=HTTPStatus.NOT_FOUND)
                return
            self.respond_json(payload)
            return
        if path == "/api/sources":
            self.respond_json(api_sources_payload(self.db_path, params))
            return
        if path == "/api/flashcards":
            self.respond_json(api_flashcards_payload(self.db_path, params))
            return
        if path == "/actions/sync-anki/status":
            self.respond_sync_anki_job_status(params)
            return
        if path == "/terms":
            self.respond_html(terms_page(self.db_path, params))
            return
        if path.startswith("/terms/"):
            term_id = self.parse_int_tail(path, "/terms/")
            if term_id is None:
                self.respond_not_found("Termo invalido.")
                return
            page = term_detail_page(self.db_path, term_id, params)
            if page is None:
                self.respond_not_found("Termo nao encontrado.")
                return
            self.respond_html(page)
            return
        if path == "/kanji":
            self.respond_html(kanji_page(self.db_path, params))
            return
        if path.startswith("/kanji/"):
            kanji_id = self.parse_int_tail(path, "/kanji/")
            if kanji_id is None:
                self.respond_not_found("Kanji invalido.")
                return
            page = kanji_detail_page(self.db_path, kanji_id, params)
            if page is None:
                self.respond_not_found("Kanji nao encontrado.")
                return
            self.respond_html(page)
            return
        if path == "/sources":
            self.respond_html(sources_page(self.db_path, params))
            return
        if path == "/sources/live":
            self.respond_json(sources_live_payload(self.db_path, params))
            return
        if path.startswith("/sources/") and path.endswith("/image"):
            source_id = self.parse_int_tail(path[:-6], "/sources/")
            if source_id is None:
                self.respond_not_found("Fonte invalida.")
                return
            self.respond_source_image(source_id)
            return
        if path.startswith("/sources/"):
            source_id = self.parse_int_tail(path, "/sources/")
            if source_id is None:
                self.respond_not_found("Fonte invalida.")
                return
            page = source_detail_page(self.db_path, source_id, params)
            if page is None:
                self.respond_not_found("Fonte nao encontrada.")
                return
            self.respond_html(page)
            return
        if path == "/flashcards":
            self.respond_html(flashcards_page(self.db_path, params))
            return

        self.respond_not_found("Pagina nao encontrada.")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/actions/sync-anki":
            self.run_sync_anki_action()
            return
        if path == "/actions/sync-kanji-anki":
            self.run_sync_anki_action()
            return
        if path == "/actions/capture-languages":
            self.run_capture_languages_action()
            return
        if path == "/actions/capture-skip-rules":
            self.run_capture_skip_rule_action()
            return
        if path == "/actions/capture-profiles/save":
            self.run_capture_profile_action("save")
            return
        if path == "/actions/capture-profiles/toggle":
            self.run_capture_profile_action("toggle")
            return
        if path == "/actions/capture-profiles/delete":
            self.run_capture_profile_action("delete")
            return
        if path.startswith("/actions/sources/") and path.endswith("/language"):
            source_id = self.parse_int_tail(path[: -len("/language")], "/actions/sources/")
            if source_id is None:
                self.respond_not_found("Fonte invalida.")
                return
            self.run_source_language_action(source_id)
            return
        if path.startswith("/actions/sources/") and path.endswith("/delete"):
            source_id = self.parse_int_tail(path[: -len("/delete")], "/actions/sources/")
            if source_id is None:
                self.respond_not_found("Fonte invalida.")
                return
            self.run_source_delete_action(source_id)
            return
        if path.startswith("/actions/sources/") and path.endswith("/translate"):
            source_id = self.parse_int_tail(path[: -len("/translate")], "/actions/sources/")
            if source_id is None:
                self.respond_not_found("Fonte invalida.")
                return
            self.run_source_translate_action(source_id)
            return

        self.respond_not_found("Acao nao encontrada.")

    def run_source_translate_action(self, source_id: int) -> None:
        form_data = self.read_form_data()
        next_path = language_href(f"/sources/{source_id}", current_language(form_data))
        try:
            with db_connect(self.db_path) as connection:
                result = lang_jp.translate_source_on_demand(connection, source_id=source_id)
                connection.commit()
        except SystemExit as exc:
            if self.prefers_json_response():
                self.respond_json({"message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.redirect(self.append_notice(next_path, message=str(exc), level="error"))
            return
        if result["status"] == "translated":
            message, level = f"Fonte traduzida ({result['lines']} linhas).", "info"
        else:
            message, level = f"Falha na traducao: {result['error']}", "error"
        if self.prefers_json_response():
            self.respond_json({"message": message, **result})
            return
        self.redirect(self.append_notice(next_path, message=message, level=level))

    def run_source_delete_action(self, source_id: int) -> None:
        form_data = self.read_form_data()
        lang = current_language(form_data)
        delete_image = first_value(form_data, "delete_image", "") == "1"
        try:
            with db_connect(self.db_path) as connection:
                summary = lang_jp.delete_source(
                    connection,
                    source_id=source_id,
                    delete_image=delete_image,
                )
                connection.commit()
        except SystemExit as exc:
            self.redirect(
                self.append_notice(
                    language_href(f"/sources/{source_id}", lang),
                    message=str(exc),
                    level="error",
                )
            )
            return
        image_removed = lang_jp.remove_deleted_source_image(summary)
        message = (
            f"Fonte #{source_id} excluida: {summary['segment_count']} segmentos, "
            f"{summary['term_count']} termos desvinculados"
        )
        if summary["orphaned_terms"]:
            message += f" ({len(summary['orphaned_terms'])} sem outra fonte)"
        message += "; imagem apagada." if image_removed else "."
        if self.prefers_json_response():
            self.respond_json(
                {
                    "message": message,
                    "source_id": source_id,
                    "orphaned_terms": summary["orphaned_terms"],
                    "image_removed": image_removed,
                }
            )
            return
        self.redirect(self.append_notice(language_href("/sources", lang), message=message, level="info"))

    def run_capture_languages_action(self) -> None:
        form_data = self.read_form_data()
        preset = first_value(form_data, "preset", "").strip()
        try:
            with db_connect(self.db_path) as connection:
                if preset == "all":
                    for code in languages.BUILTIN_LANGUAGES:
                        lang_jp.ensure_language_row(connection, code, capture_enabled=True)
                    connection.execute(
                        "UPDATE languages SET capture_enabled = 1, updated_at = CURRENT_TIMESTAMP"
                    )
                elif preset:
                    lang_jp.set_capture_languages(connection, preset.split(","))
                else:
                    lang_jp.set_capture_languages(connection, form_data.get("codes", []))
                codes = lang_jp.capture_language_codes(connection, override="")
                connection.commit()
        except SystemExit as exc:
            self.redirect(self.append_notice("/capture", message=str(exc), level="error"))
            return
        message = f"Captura: {', '.join(codes)}."
        if self.prefers_json_response():
            self.respond_json({"capture_languages": list(codes), "message": message})
            return
        self.redirect(self.append_notice("/capture", message=message, level="info"))

    def run_capture_skip_rule_action(self) -> None:
        form_data = self.read_form_data()
        op = first_value(form_data, "op", "add").strip()
        kind = first_value(form_data, "kind", "")
        pattern = first_value(form_data, "pattern", "")
        try:
            with db_connect(self.db_path) as connection:
                if op == "remove":
                    changed = lang_jp.remove_capture_skip_rule(connection, kind, pattern)
                else:
                    changed = lang_jp.add_capture_skip_rule(connection, kind, pattern)
                connection.commit()
            kind, pattern = lang_jp.normalize_capture_skip_rule(kind, pattern)
        except SystemExit as exc:
            if self.prefers_json_response():
                self.respond_json({"message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
                return
            self.redirect(self.append_notice("/capture", message=str(exc), level="error"))
            return
        if op == "remove":
            message = f"Janela volta a ser capturada: {kind} {pattern}." if changed else f"{kind} {pattern} nao estava na lista."
        else:
            message = f"Janela ignorada: {kind} {pattern}." if changed else f"{kind} {pattern} ja estava na lista."
        if self.prefers_json_response():
            self.respond_json({"message": message, "kind": kind, "pattern": pattern, "changed": changed})
            return
        self.redirect(self.append_notice("/capture", message=message, level="info"))

    def run_capture_profile_action(self, action: str) -> None:
        form_data = self.read_form_data()
        try:
            with db_connect(self.db_path) as connection:
                if action == "save":
                    priority_raw = first_value(form_data, "priority", "100").strip() or "100"
                    try:
                        priority = int(priority_raw)
                    except ValueError:
                        priority = 100
                    profile_id = lang_jp.upsert_capture_profile(
                        connection,
                        name=first_value(form_data, "name"),
                        language_codes=first_value(form_data, "languages"),
                        window_title_pattern=first_value(form_data, "window_pattern") or None,
                        process_name_pattern=first_value(form_data, "process_pattern") or None,
                        enabled=first_value(form_data, "enabled", "1") != "0",
                        priority=priority,
                        notes=first_value(form_data, "notes") or None,
                    )
                    message = f"Perfil #{profile_id} salvo."
                else:
                    try:
                        profile_id = int(first_value(form_data, "id", ""))
                    except ValueError:
                        raise SystemExit("Perfil invalido.")
                    if action == "toggle":
                        enabled = first_value(form_data, "enabled", "1") != "0"
                        if not lang_jp.set_capture_profile_enabled(connection, profile_id, enabled):
                            raise SystemExit("Perfil nao encontrado.")
                        message = f"Perfil #{profile_id} {'ativado' if enabled else 'desativado'}."
                    else:
                        if not lang_jp.delete_capture_profile(connection, profile_id=profile_id):
                            raise SystemExit("Perfil nao encontrado.")
                        message = f"Perfil #{profile_id} removido."
                connection.commit()
        except SystemExit as exc:
            self.redirect(self.append_notice("/capture", message=str(exc), level="error"))
            return
        if self.prefers_json_response():
            self.respond_json({"message": message})
            return
        self.redirect(self.append_notice("/capture", message=message, level="info"))

    def run_source_language_action(self, source_id: int) -> None:
        form_data = self.read_form_data()
        language = first_value(form_data, "language", AUTO_LANGUAGE_CODE) or AUTO_LANGUAGE_CODE
        reanalyze = first_value(form_data, "reanalyze", "") == "1"
        promote = first_value(form_data, "promote", "") == "1"
        next_path = language_href(f"/sources/{source_id}", current_language(form_data))
        try:
            with db_connect(self.db_path) as connection:
                result = lang_jp.change_source_language(
                    connection,
                    source_id=source_id,
                    language=language,
                    reanalyze=reanalyze,
                    promote=promote,
                )
                connection.commit()
        except SystemExit as exc:
            self.redirect(self.append_notice(next_path, message=str(exc), level="error"))
            return
        message = f"Idioma da fonte: {result['previous_language']} -> {result['language_code']}."
        reanalyzed = result["reanalyzed"]
        if isinstance(reanalyzed, dict):
            message += (
                f" Reanalisada: {reanalyzed['segments']} segmentos, {reanalyzed['candidates']} candidatos"
                f" ({', '.join(sorted(reanalyzed['languages'])) or result['language_code']})."
            )
        if promote:
            message += f" Termos criados: {result['promoted_terms']}."
        if self.prefers_json_response():
            self.respond_json({"message": message, **{k: v for k, v in result.items() if k != "reanalyzed"}})
            return
        self.redirect(self.append_notice(next_path, message=message, level="info"))

    def log_message(self, format: str, *args: object) -> None:
        return

    def parse_int_tail(self, path: str, prefix: str) -> int | None:
        try:
            return int(path.removeprefix(prefix))
        except ValueError:
            return None

    def prefers_json_response(self) -> bool:
        accept = (self.headers.get("Accept") or "").lower()
        requested_with = (self.headers.get("X-Requested-With") or "").lower()
        return "application/json" in accept or requested_with == "fetch"

    def respond_html(self, payload: str, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = payload.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def respond_json(self, payload: object, *, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, *, status: HTTPStatus = HTTPStatus.SEE_OTHER) -> None:
        self.send_response(status)
        self.send_header("Location", location)
        self.end_headers()

    def respond_not_found(self, message: str) -> None:
        body = layout(
            title="Nao encontrado",
            body=f'<div class="grid"><section class="panel"><h2>Nao encontrado</h2><div class="empty">{html.escape(message)}</div></section></div>',
            active_path="/",
        )
        self.respond_html(body, status=HTTPStatus.NOT_FOUND)

    def respond_source_image(self, source_id: int) -> None:
        with db_connect(self.db_path) as connection:
            row = connection.execute(
                "SELECT source_path FROM sources WHERE id = ?",
                (source_id,),
            ).fetchone()
        raw_path = row["source_path"] if row is not None else None
        source_path = resolve_repo_local_path(raw_path)
        if source_path is None or not source_path.exists():
            self.respond_not_found("Imagem da fonte nao encontrada.")
            return

        data = source_path.read_bytes()
        content_type = mimetypes.guess_type(source_path.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_form_data(self) -> dict[str, list[str]]:
        content_length = self.headers.get("Content-Length")
        if content_length is None:
            return {}
        try:
            length = int(content_length)
        except ValueError:
            return {}
        raw_body = self.rfile.read(length).decode("utf-8")
        return parse_qs(raw_body)

    def run_catalog_command(self, *command_parts: str) -> subprocess.CompletedProcess[str]:
        return run_catalog_command_for_db(self.db_path, *command_parts)

    def append_notice(self, path: str, *, message: str, level: str) -> str:
        return build_notice_path(path, message=message, level=level)

    def respond_sync_anki_job_status(self, params: dict[str, list[str]]) -> None:
        job_id = first_value(params, "job_id", "").strip()
        if not job_id:
            self.respond_json(
                {"message": "Job de sincronizacao nao informado."},
                status=HTTPStatus.BAD_REQUEST,
            )
            return

        state = get_sync_job_state(job_id)
        if state is None:
            self.respond_json(
                {"message": "Job de sincronizacao nao encontrado."},
                status=HTTPStatus.NOT_FOUND,
            )
            return

        self.respond_json(sync_job_payload(state))

    def run_sync_anki_action(self) -> None:
        form_data = self.read_form_data()
        if self.prefers_json_response():
            self.respond_json(start_sync_anki_job(self.db_path))
            return

        next_path = first_value(form_data, "next", "/") or "/"
        term_result = self.run_catalog_command("sync-anki-words")
        term_lines = command_output_lines(
            stdout=term_result.stdout,
            stderr=term_result.stderr,
        )
        if term_result.returncode != 0:
            message = sync_failure_message(
                term_lines,
                "Falha ao sincronizar termos com o Anki.",
            )
            self.redirect(self.append_notice(next_path, message=message, level="error"))
            return

        kanji_result = self.run_catalog_command("sync-anki-kanji")
        kanji_lines = command_output_lines(
            stdout=kanji_result.stdout,
            stderr=kanji_result.stderr,
        )
        if kanji_result.returncode != 0:
            message = sync_failure_message(
                kanji_lines,
                "Falha ao sincronizar kanji com o Anki.",
            )
            self.redirect(self.append_notice(next_path, message=message, level="error"))
            return

        term_message = sync_result_message(term_lines, "Termos sincronizados.")
        kanji_message = sync_result_message(kanji_lines, "Kanji sincronizados.")
        message = f"{term_message} {kanji_message}"
        self.redirect(self.append_notice(next_path, message=message, level="info"))


def serve_catalog_ui(
    *,
    db_path: Path = DEFAULT_DB_PATH,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    if not db_path.exists():
        raise SystemExit(f"Database file not found: {db_path}")

    handler = type(
        "BoundCatalogRequestHandler",
        (CatalogRequestHandler,),
        {"db_path": db_path},
    )
    with ThreadingHTTPServer((host, port), handler) as server:
        actual_host, actual_port = server.server_address[:2]
        url = f"http://{actual_host}:{actual_port}/"
        print(f"JP Catalog UI available at {url}")
        print("Press Ctrl+C to stop the server.")
        if open_browser:
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")
