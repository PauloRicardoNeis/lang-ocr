from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import html
import json
import mimetypes
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import webbrowser
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlencode, urlparse
from uuid import uuid4

from lang_jp import (
    ensure_runtime_schema,
    ensure_source_image_dimensions,
    format_source_resolution,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_DB_PATH = ROOT / "data" / "lang_jp.sqlite3"
TERM_PRIORITY_ORDER_SQL = """
            COALESCE(priority_score, 0) DESC,
            COALESCE(last_seen_at, first_seen_at) DESC,
            lemma COLLATE NOCASE ASC,
            id ASC
"""
FLASHCARD_PRIORITY_ORDER_SQL = """
            COALESCE(ts.priority_score, 0) DESC,
            COALESCE(ts.last_seen_at, ts.first_seen_at) DESC,
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
      return value.toLocaleLowerCase("pt-BR");
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
      const defaultSortColumn = Number.parseInt(table.dataset.defaultSortColumn || "", 10);
      const defaultSortDirection = table.dataset.defaultSortDirection || "desc";
      if (Number.isInteger(defaultSortColumn) && headers[defaultSortColumn]) {
        sortTable(table, headers, tbody, defaultSortColumn, defaultSortDirection);
      }
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


def text(value: object | None, fallback: str = "-") -> str:
    if value is None:
        return fallback
    string = str(value).strip()
    return string if string else fallback


def escape(value: object | None, fallback: str = "-") -> str:
    return html.escape(text(value, fallback))


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


def format_datetime_display(value: object | None) -> str:
    raw_value = text(value, "")
    if not raw_value or raw_value == "-":
        return "-"
    normalized = normalize_iso_datetime(raw_value)
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized):
            parsed = datetime.strptime(normalized, "%Y-%m-%d")
            return parsed.strftime("%d/%m/%Y")
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return raw_value
    return parsed.strftime("%d/%m/%Y %H:%M:%S")


def sort_value_cell(display_value: str, sort_value: object | None) -> tuple[str, str]:
    return (display_value, text(sort_value, ""))


def datetime_cell(value: object | None) -> tuple[str, str]:
    return sort_value_cell(escape(format_datetime_display(value)), value)


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


def nav_link(path: str, label: str, active_path: str) -> str:
    active_class = "is-active" if path == active_path else ""
    return f'<a class="{active_class}" href="{path}">{html.escape(label)}</a>'


def layout(*, title: str, body: str, active_path: str, extra_scripts: str = "") -> str:
    nav = "".join(
        (
            nav_link("/", "Visao geral", active_path),
            nav_link("/terms", "Termos", active_path),
            nav_link("/kanji", "Kanji", active_path),
            nav_link("/sources", "Fontes", active_path),
            nav_link("/flashcards", "Cards", active_path),
        )
    )
    return f"""<!doctype html>
<html lang="pt-BR">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>{html.escape(title)}</title>
    {PAGE_STYLE}
  </head>
  <body>
    <div class="shell">
      <section class="hero">
        <h1>JP Catalog</h1>
        <p>Interface local para navegar no banco de vocabulario sem depender de comandos longos no terminal.</p>
        <nav class="nav">{nav}</nav>
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

    default_sort_column = next(
        (index for index, header in enumerate(headers) if header.lower() == "score"),
        None,
    )
    table_attrs = ' class="sortable-table"'
    if default_sort_column is not None:
        table_attrs += (
            f' data-default-sort-column="{default_sort_column}"'
            ' data-default-sort-direction="desc"'
        )

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
        <input type="search" name="q" value="{html.escape(q)}" placeholder="{html.escape(placeholder)}">
        {extras}
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


def dashboard_page(db_path: Path, params: dict[str, list[str]] | None = None) -> str:
    params = params or {}
    with db_connect(db_path) as connection:
        stats = connection.execute(
            """
            SELECT
                (SELECT COUNT(*) FROM sources) AS source_count,
                (SELECT COUNT(*) FROM terms) AS term_count,
                (SELECT COUNT(*) FROM kanji) AS kanji_count,
                (SELECT COUNT(*) FROM source_terms) AS link_count,
                (SELECT COUNT(*) FROM flashcards WHERE export_status = 'pending') AS pending_flashcards
            """
        ).fetchone()
        top_terms = connection.execute(
            """
            SELECT
                id,
                lemma,
                COALESCE(reading, '-') AS reading,
                COALESCE(meaning_pt, '-') AS meaning_pt,
                total_occurrences,
                distinct_sources,
                printf('%.1f', priority_score) AS priority_score
            FROM v_terms_overview
            WHERE
                COALESCE(priority_score, 0) > 0
                AND NOT (
                    LOWER(TRIM(COALESCE(meaning_pt, ''))) LIKE 'nome%proprio%'
                )
            ORDER BY
            """ + TERM_PRIORITY_ORDER_SQL + """
            LIMIT 8
            """
        ).fetchall()
        top_kanji = connection.execute(
            """
            SELECT
                id,
                literal,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                printf('%.1f', priority_score) AS priority_score
            FROM v_kanji_overview
            ORDER BY priority_score DESC, id DESC
            LIMIT 8
            """
        ).fetchall()
        recent_sources = connection.execute(
            """
            SELECT
                id,
                COALESCE(title, '(sem titulo)') AS title,
                COALESCE(scene, '-') AS scene,
                source_path,
                image_width,
                image_height,
                COALESCE(captured_at, created_at) AS seen_at
            FROM sources
            ORDER BY seen_at DESC, id DESC
            LIMIT 8
            """
        ).fetchall()
        recent_source_rows = [
            [
                f'<a href="/sources/{row["id"]}">#{row["id"]}</a>',
                escape(row["title"]),
                escape(source_resolution_label(connection, row)),
                escape(row["scene"]),
                datetime_cell(row["seen_at"]),
            ]
            for row in recent_sources
        ]
        recent_cards = connection.execute(
            """
            SELECT
                f.id,
                t.lemma,
                printf('%.1f', COALESCE(ts.priority_score, 0)) AS priority_score,
                f.template_name,
                f.export_status,
                f.front_text
            FROM flashcards AS f
            JOIN terms AS t
                ON t.id = f.term_id
            LEFT JOIN term_stats AS ts
                ON ts.term_id = f.term_id
            ORDER BY
            """ + FLASHCARD_PRIORITY_ORDER_SQL + """
            LIMIT 8
            """
        ).fetchall()

    term_rows = [
        [
            f'<a href="/terms/{row["id"]}">{escape(row["lemma"])}</a>',
            escape(row["reading"]),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
        ]
        for row in top_terms
    ]
    kanji_rows = [
        [
            f'<a href="/kanji/{row["id"]}">{escape(row["literal"])}</a>',
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
        ]
        for row in top_kanji
    ]
    card_rows = [
        [
            f'<a href="/flashcards?q={quote(str(row["lemma"]))}">{escape(row["lemma"])}</a>',
            escape(row["priority_score"]),
            escape(row["template_name"]),
            status_chip(text(row["export_status"])),
            escape(truncate(text(row["front_text"], ""), size=44)),
        ]
        for row in recent_cards
    ]

    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <div class="stats">
          <div class="stat"><span class="label">Fontes</span><span class="value">{escape(stats["source_count"], "0")}</span></div>
          <div class="stat"><span class="label">Termos</span><span class="value">{escape(stats["term_count"], "0")}</span></div>
          <div class="stat"><span class="label">Kanji</span><span class="value">{escape(stats["kanji_count"], "0")}</span></div>
          <div class="stat"><span class="label">Ligacoes</span><span class="value">{escape(stats["link_count"], "0")}</span></div>
          <div class="stat"><span class="label">Cards pendentes</span><span class="value">{escape(stats["pending_flashcards"], "0")}</span></div>
        </div>
        {sync_anki_action(next_path="/")}
      </section>

      <section class="panel">
        <h2>Busca rapida</h2>
        <div class="search-row">
          {search_form(action="/terms", label="Buscar termos", placeholder="Japones, leitura ou significado")}
          {search_form(action="/kanji", label="Buscar kanji", placeholder="Ex.: 魔, 学, 回")}
          {search_form(action="/sources", label="Buscar fontes", placeholder="Titulo, cena ou texto bruto")}
          {search_form(action="/flashcards", label="Buscar cards", placeholder="Frente, termo ou significado")}
        </div>
      </section>

      <section class="panel panel-wide">
        <h2>Termos prioritarios</h2>
        {table_html(["Termo", "Leitura", "Significado", "Ocorrencias", "Fontes", "Score"], term_rows)}
      </section>

      <section class="panel panel-side">
        <h2>Acesso rapido</h2>
        <div class="stack">
          <a class="button-link" href="/terms">Abrir lista completa de termos</a>
          <a class="button-link" href="/kanji">Abrir catalogo de kanji</a>
          <a class="button-link" href="/sources">Abrir fontes recentes</a>
          <a class="button-link" href="/flashcards?status=pending">Ver cards pendentes</a>
        </div>
        <p class="footer-note">A interface navega no catalogo e tambem permite disparar a sincronizacao com o Anki. O banco continua sendo a fonte de verdade.</p>
      </section>

      <section class="panel panel-wide">
        <h2>Fontes recentes</h2>
        {table_html(["ID", "Titulo", "Resolucao", "Cena", "Quando"], recent_source_rows)}
      </section>

      <section class="panel panel-side">
        <h2>Kanji prioritarios</h2>
        {table_html(["Kanji", "Palavras", "Ocorrencias", "Fontes", "Score"], kanji_rows)}
      </section>

      <section class="panel">
        <h2>Cards recentes</h2>
        {table_html(["Termo", "Score", "Template", "Status", "Frente"], card_rows)}
      </section>
    </div>
    """
    return layout(
        title="JP Catalog",
        body=body,
        active_path="/",
        extra_scripts=auto_reload_script(db_path),
    )


def terms_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    order = first_value(params, "order", "priority")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    order_clause = {
        "priority": TERM_PRIORITY_ORDER_SQL.strip(),
        "recent": "last_seen_at DESC, id DESC",
        "lemma": "lemma COLLATE NOCASE ASC, id ASC",
    }.get(order, TERM_PRIORITY_ORDER_SQL.strip())

    filters = []
    sql_params: list[object] = []
    if q.strip():
        filters.append(
            "(lemma LIKE ? OR COALESCE(reading, '') LIKE ? OR COALESCE(meaning_pt, '') LIKE ?)"
        )
        like = sql_like(q)
        sql_params.extend([like, like, like])

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
                lemma,
                COALESCE(reading, '-') AS reading,
                COALESCE(meaning_pt, '-') AS meaning_pt,
                total_occurrences,
                distinct_sources,
                printf('%.1f', priority_score) AS priority_score,
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
    }
    pagination_markup = pagination_nav(
        path="/terms",
        base_params=base_params,
        pagination=pagination,
    )
    reset_href = "/terms" if any(value is not None for value in base_params.values()) or pagination.page > 1 else None
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
    table_rows = [
        [
            f'<a href="/terms/{row["id"]}">{escape(row["lemma"])}</a>',
            escape(row["reading"]),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
            datetime_cell(row["last_seen_at"]),
        ]
        for row in rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel">
        <h2>Termos</h2>
        {search_form(action="/terms", label="Buscar no catalogo", placeholder="Ex.: 回復, かいふく, cura", q=q, extras=extras, reset_href=reset_href)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)} Ordenacao atual: <strong>{html.escape(order)}</strong>.</p>
          {pagination_markup}
        </div>
        {table_html(["Termo", "Leitura", "Significado", "Ocorrencias", "Fontes", "Score", "Ultima vez"], table_rows)}
      </section>
    </div>
    """
    return layout(
        title="Termos",
        body=body,
        active_path="/terms",
        extra_scripts=auto_reload_script(db_path),
    )


def kanji_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    order = first_value(params, "order", "priority")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))
    order_clause = {
        "priority": "priority_score DESC, id DESC",
        "recent": "last_seen_at DESC, id DESC",
        "literal": "literal COLLATE NOCASE ASC, id ASC",
    }.get(order, "priority_score DESC, id DESC")

    filters = []
    sql_params: list[object] = []
    if q.strip():
        filters.append("(literal LIKE ? OR COALESCE(meaning_pt, '') LIKE ?)")
        like = sql_like(q)
        sql_params.extend([like, like])

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
                literal,
                COALESCE(canonical_meanings, meaning_pt, '-') AS meaning,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                printf('%.1f', priority_score) AS priority_score,
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
    }
    pagination_markup = pagination_nav(
        path="/kanji",
        base_params=base_params,
        pagination=pagination,
    )
    reset_href = "/kanji" if any(value is not None for value in base_params.values()) or pagination.page > 1 else None
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
            f'<a href="/kanji/{row["id"]}">{escape(row["literal"])}</a>',
            escape(row["meaning"]),
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
            datetime_cell(row["last_seen_at"]),
        ]
        for row in rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <h2>Kanji</h2>
        {sync_anki_action(next_path="/kanji")}
        {search_form(action="/kanji", label="Buscar no catalogo", placeholder="Ex.: 魔, 学, 回", q=q, extras=extras, reset_href=reset_href)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)} Ordenacao atual: <strong>{html.escape(order)}</strong>.</p>
          {pagination_markup}
        </div>
        {table_html(["Kanji", "Significado", "Palavras", "Ocorrencias", "Fontes", "Score", "Ultima vez"], table_rows)}
      </section>
    </div>
    """
    return layout(
        title="Kanji",
        body=body,
        active_path="/kanji",
        extra_scripts=auto_reload_script(db_path),
    )


def kanji_detail_page(db_path: Path, kanji_id: int) -> str | None:
    with db_connect(db_path) as connection:
        kanji = connection.execute(
            """
            SELECT
                id,
                literal,
                COALESCE(canonical_meanings, meaning_pt, '-') AS meaning,
                COALESCE(on_readings, '-') AS on_readings,
                COALESCE(kun_readings, '-') AS kun_readings,
                total_occurrences,
                distinct_sources,
                distinct_terms,
                printf('%.1f', priority_score) AS priority_score,
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
                t.lemma,
                COALESCE(t.reading, '-') AS reading,
                COALESCE(t.meaning_pt, '-') AS meaning_pt,
                SUM(st.count_in_source) AS total_occurrences,
                COUNT(DISTINCT st.source_id) AS distinct_sources,
                printf('%.1f', COALESCE(ts.priority_score, 0)) AS priority_score
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
                t.lemma,
                t.reading,
                t.meaning_pt,
                ts.priority_score
            ORDER BY
                total_occurrences DESC,
                COALESCE(ts.priority_score, 0) DESC,
                t.lemma COLLATE NOCASE ASC
            LIMIT 50
            """,
            (kanji_id, kanji["literal"], kanji["literal"]),
        ).fetchall()
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
            ORDER BY sk.count_in_source DESC, seen_at DESC, sk.id DESC
            LIMIT 50
            """,
            (kanji_id,),
        ).fetchall()

    related_term_rows = [
        [
            f'<a href="/terms/{row["term_id"]}">{escape(row["lemma"])}</a>',
            escape(row["reading"]),
            escape(row["meaning_pt"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
        ]
        for row in term_rows
    ]
    related_source_rows = [
        [
            f'<a href="/sources/{row["source_id"]}">#{row["source_id"]}</a>',
            escape(row["count_in_source"]),
            escape(row["title"]),
            escape(row["game"]),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in source_rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        <h2>{escape(kanji["literal"])}</h2>
        <div class="chips">
          <span class="chip">Significado: {escape(kanji["meaning"], "-")}</span>
          <span class="chip">On: {escape(kanji["on_readings"], "-")}</span>
          <span class="chip">Kun: {escape(kanji["kun_readings"], "-")}</span>
          <span class="chip">Palavras: {escape(kanji["distinct_terms"], "0")}</span>
          <span class="chip">Ocorrencias: {escape(kanji["total_occurrences"], "0")}</span>
          <span class="chip">Fontes: {escape(kanji["distinct_sources"], "0")}</span>
          <span class="chip">Score: {escape(kanji["priority_score"], "0")}</span>
        </div>
      </section>

      <section class="panel panel-side">
        <h2>Atalhos</h2>
        <div class="stack">
          <a class="button-link" href="/kanji">Voltar para kanji</a>
          <a class="button-link" href="/terms">Abrir termos</a>
          <a class="button-link" href="/sources">Abrir fontes</a>
        </div>
      </section>

      <section class="panel panel-wide">
        <h3>Termos relacionados</h3>
        {table_html(["Termo", "Leitura", "Significado", "Ocorrencias", "Fontes", "Score"], related_term_rows)}
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
        <h3>Fontes que contem este kanji</h3>
        {table_html(["Fonte", "Count", "Titulo", "Game", "Cena", "Quando"], related_source_rows)}
      </section>
    </div>
    """
    return layout(title=f"Kanji {kanji['literal']}", body=body, active_path="/kanji")


def term_detail_page(db_path: Path, term_id: int) -> str | None:
    with db_connect(db_path) as connection:
        term = connection.execute(
            """
            SELECT
                t.id,
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
                printf('%.1f', COALESCE(ts.priority_score, 0)) AS priority_score,
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
            ORDER BY st.count_in_source DESC, seen_at DESC, st.id DESC
            """,
            (term_id,),
        ).fetchall()
        segment_occurrences = connection.execute(
            """
            SELECT
                ss.source_id,
                ss.segment_text,
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
            ORDER BY seen_at DESC, ss.id DESC, sst.id DESC
            """,
            (term_id,),
        ).fetchall()
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
        kanji_literals = extract_kanji_sequence(
            term["lemma"],
            *(row["surface_form"] for row in forms),
            *(row["surface_form"] for row in occurrences),
        )
        if kanji_literals:
            placeholders = ", ".join("?" for _ in kanji_literals)
            kanji_rows = connection.execute(
                f"""
                SELECT
                    id,
                    literal,
                    total_occurrences,
                    distinct_sources,
                    distinct_terms,
                    printf('%.1f', priority_score) AS priority_score
                FROM v_kanji_overview
                WHERE literal IN ({placeholders})
                """,
                kanji_literals,
            ).fetchall()
        else:
            kanji_rows = []

    form_chips = "".join(
        f'<span class="chip">{escape(row["surface_form"])} / {escape(row["reading"])}</span>'
        for row in forms
    ) or '<span class="muted">Nenhuma forma registrada.</span>'
    occurrence_rows = [
        [
            f'<a href="/sources/{row["source_id"]}">#{row["source_id"]}</a>',
            escape(row["surface_form"]),
            escape(row["count_in_source"]),
            escape(row["confidence"]),
            escape(row["title"]),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in occurrences
    ]
    segment_occurrence_rows = [
        [
            f'<a href="/sources/{row["source_id"]}">#{row["source_id"]}</a>',
            escape(row["segment_text"]),
            escape(row["segment_reading"]),
            escape(row["surface_form"]),
            escape(row["count_in_segment"]),
            escape(row["confidence"]),
            escape(row["title"]),
            escape(row["scene"]),
            datetime_cell(row["seen_at"]),
        ]
        for row in segment_occurrences
    ]
    flashcard_rows = [
        [
            f'<a href="/flashcards?q={quote(str(term["lemma"]))}">#{row["id"]}</a>',
            escape(row["template_name"]),
            escape(row["deck_name"]),
            status_chip(text(row["export_status"])),
            escape(truncate(text(row["front_text"], ""), size=52)),
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
            f'<a href="/kanji/{row["id"]}">{escape(row["literal"])}</a>',
            escape(row["distinct_terms"]),
            escape(row["total_occurrences"]),
            escape(row["distinct_sources"]),
            escape(row["priority_score"]),
        ]
        for row in ordered_kanji_rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        <h2>{escape(term["lemma"])}</h2>
        <div class="chips">
          <span class="chip">Leitura: {escape(term["reading"] or "-")}</span>
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
          <div class="stat"><span class="label">Score</span><span class="value">{escape(term["priority_score"], "0")}</span></div>
        </div>
      </section>

      <section class="panel panel-wide">
        <h3>Onde apareceu</h3>
        {table_html(["Fonte", "Forma vista", "Count", "Confianca", "Titulo", "Cena", "Quando"], occurrence_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Segmentacao revisada</h3>
        {table_html(["Fonte", "Segmento bruto", "Leitura do segmento", "Forma ligada", "Count", "Confianca", "Titulo", "Cena", "Quando"], segment_occurrence_rows)}
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

      <section class="panel panel-side">
        <h3>Kanji derivados</h3>
        {table_html(["Kanji", "Palavras", "Ocorrencias", "Fontes", "Score"], kanji_table_rows)}
      </section>

      <section class="panel">
        <h3>Flashcards</h3>
        {table_html(["Card", "Template", "Deck", "Status", "Frente"], flashcard_rows)}
      </section>
    </div>
    """
    return layout(title=f"Termo {term['lemma']}", body=body, active_path="/terms")


SOURCES_TABLE_HEADERS = ["ID", "Tipo", "Titulo", "Resolucao", "Cena", "Termos", "Quando", "Texto"]


def load_sources_rows(
    db_path: Path, params: dict[str, list[str]]
) -> tuple[str, int, PaginationState, list[dict[str, object]]]:
    q = first_value(params, "q")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))

    filters = []
    sql_params: list[object] = []
    if q.strip():
        filters.append(
            """
            (
                COALESCE(title, '') LIKE ?
                OR COALESCE(game, '') LIKE ?
                OR COALESCE(scene, '') LIKE ?
                OR COALESCE(raw_text, '') LIKE ?
            )
            """
        )
        like = sql_like(q)
        sql_params.extend([like, like, like, like])

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
            ORDER BY seen_at DESC, s.id DESC
            LIMIT ? OFFSET ?
            """,
            [*sql_params, pagination.limit, pagination.offset],
        ).fetchall()
        normalized_rows = []
        for row in rows:
            normalized_rows.append(
                {
                    "id": row["id"],
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


def sources_table_rows(rows: list[dict[str, object]]) -> list[list[str]]:
    return [
        [
            f'<a href="/sources/{row["id"]}">#{row["id"]}</a>',
            escape(row["source_type"]),
            escape(row["title"]),
            escape(row["resolution"]),
            escape(row["scene"]),
            escape(row["linked_terms"]),
            datetime_cell(row["seen_at"]),
            escape(truncate(text(row["raw_text"], ""), size=72)),
        ]
        for row in rows
    ]


def sources_table_markup(rows: list[dict[str, object]]) -> str:
    return table_html(SOURCES_TABLE_HEADERS, sources_table_rows(rows))


def build_sources_payload(
    *,
    db_path: Path,
    q: str,
    limit: int,
    pagination: PaginationState,
    rows: list[dict[str, object]],
) -> dict[str, object]:
    base_params = {
        "q": q.strip() or None,
        "limit": limit if limit != 50 else None,
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
        "table_html": sources_table_markup(rows),
    }


def sources_live_payload(db_path: Path, params: dict[str, list[str]]) -> dict[str, object]:
    q, limit, pagination, rows = load_sources_rows(db_path, params)
    return build_sources_payload(
        db_path=db_path,
        q=q,
        limit=limit,
        pagination=pagination,
        rows=rows,
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

    extras = f'<input type="number" name="limit" value="{limit}" min="1" max="200">'
    payload = build_sources_payload(
        db_path=db_path,
        q=q,
        limit=limit,
        pagination=pagination,
        rows=rows,
    )
    reset_href = "/sources" if q.strip() or limit != 50 or pagination.page > 1 else None
    live_params = {
        "q": q.strip() or None,
        "limit": limit if limit != 50 else None,
        "page": pagination.page if pagination.page > 1 else None,
    }
    live_query = urlencode(
        [(key, str(value)) for key, value in live_params.items() if value is not None]
    )
    live_endpoint = f"/sources/live?{live_query}" if live_query else "/sources/live"
    body = f"""
    <div class="grid">
      <section class="panel" data-sources-live-root data-live-revision="{html.escape(str(payload["revision"]))}">
        <h2>Fontes</h2>
        {search_form(action="/sources", label="Buscar em fontes", placeholder="Ex.: battle menu, config, texto OCR", q=q, extras=extras, reset_href=reset_href)}
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
    )


def source_detail_page(db_path: Path, source_id: int) -> str | None:
    with db_connect(db_path) as connection:
        source = connection.execute(
            """
            SELECT
                id,
                source_type,
                language_code,
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

        term_rows = connection.execute(
            """
            SELECT
                st.term_id,
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
                COALESCE(ss.reading, '-') AS segment_reading,
                COUNT(sst.id) AS linked_terms,
                COALESCE(group_concat(DISTINCT t.lemma), '-') AS linked_lemmas
            FROM source_segments AS ss
            LEFT JOIN source_segment_terms AS sst
                ON sst.source_segment_id = ss.id
            LEFT JOIN terms AS t
                ON t.id = sst.term_id
            WHERE ss.source_id = ?
            GROUP BY ss.id, ss.segment_text, ss.reading
            ORDER BY ss.id ASC
            """,
            (source_id,),
        ).fetchall()
        segmented_term_rows = connection.execute(
            """
            SELECT
                ss.segment_text,
                COALESCE(ss.reading, '-') AS segment_reading,
                t.id AS term_id,
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
            (source_id,),
        ).fetchall()
        kanji_rows = connection.execute(
            """
            SELECT
                k.id,
                k.literal,
                sk.count_in_source,
                printf('%.1f', COALESCE(ks.priority_score, 0)) AS priority_score
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
    if source_path and Path(source_path).exists():
        image_block = f"""
        <section class="panel">
          <h3>Imagem</h3>
          <img class="source-image" src="/sources/{source_id}/image" alt="Source image {source_id}">
        </section>
        """

    table_rows = [
        [
            f'<a href="/terms/{row["term_id"]}">{escape(row["lemma"])}</a>',
            escape(row["reading"]),
            escape(row["meaning_pt"]),
            escape(row["surface_form"]),
            escape(row["count_in_source"]),
            escape(row["confidence"]),
        ]
        for row in term_rows
    ]
    segment_summary_rows = [
        [
            escape(row["segment_text"]),
            escape(row["segment_reading"]),
            escape(row["linked_terms"]),
            escape(row["linked_lemmas"]),
        ]
        for row in segment_rows
    ]
    segmented_term_table_rows = [
        [
            escape(row["segment_text"]),
            escape(row["segment_reading"]),
            f'<a href="/terms/{row["term_id"]}">{escape(row["lemma"])}</a>',
            escape(row["term_reading"]),
            escape(row["meaning_pt"]),
            escape(row["surface_form"]),
            escape(row["count_in_segment"]),
            escape(row["confidence"]),
        ]
        for row in segmented_term_rows
    ]
    kanji_table_rows = [
        [
            f'<a href="/kanji/{row["id"]}">{escape(row["literal"])}</a>',
            escape(row["count_in_source"]),
            escape(row["priority_score"]),
        ]
        for row in kanji_rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel panel-wide">
        <h2>{escape(source["title"] or "(sem titulo)")}</h2>
        <ul class="meta-list">
          <li><strong>Tipo</strong>{escape(source["source_type"])}</li>
          <li><strong>Idioma</strong>{escape(source["language_code"])}</li>
          <li><strong>Resolucao original</strong>{escape(resolution_label)}</li>
          <li><strong>Cena</strong>{escape(source["scene"] or "-")}</li>
          <li><strong>Capturado em</strong>{escape(format_datetime_display(source["captured_at"] or source["created_at"]))}</li>
          <li><strong>Arquivo</strong><span class="mono">{escape(source["source_path"] or "-")}</span></li>
        </ul>
      </section>

      <section class="panel panel-side">
        <h2>Atalhos</h2>
        <div class="stack">
          <a class="button-link" href="/sources">Voltar para fontes</a>
          <a class="button-link" href="/kanji">Abrir kanji</a>
          <a class="button-link" href="/terms">Abrir termos</a>
          <a class="button-link" href="/flashcards">Abrir cards</a>
        </div>
      </section>

      {image_block}

      <section class="panel panel-wide">
        <h3>Texto bruto</h3>
        <pre>{html.escape(text(source["raw_text"], "(sem texto bruto)"))}</pre>
      </section>

      <section class="panel panel-side">
        <h3>Tradução EN</h3>
        <p><strong>Status:</strong> {escape(source["translated_text_en_status"] or "missing")}</p>
        <p><strong>Provider:</strong> {escape(source["translated_text_en_provider"] or "-")}</p>
        <pre>{html.escape(text(source["translated_text_en"], "(sem tradução em inglês)"))}</pre>
      </section>

      <section class="panel panel-side">
        <h3>Notas</h3>
        <pre>{html.escape(text(source["notes"], "(sem notas)"))}</pre>
      </section>

      <section class="panel panel-wide">
        <h3>Segmentos revisados desta fonte</h3>
        {table_html(["Segmento bruto", "Leitura", "Termos ligados", "Lemmas"], segment_summary_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Termos ligados a esta fonte</h3>
        {table_html(["Termo", "Leitura", "Significado", "Forma vista", "Count", "Confianca"], table_rows)}
      </section>

      <section class="panel panel-wide">
        <h3>Decomposicao termo a termo</h3>
        {table_html(["Segmento bruto", "Leitura do segmento", "Termo", "Leitura", "Significado", "Forma ligada", "Count", "Confianca"], segmented_term_table_rows)}
      </section>

      <section class="panel panel-side">
        <h3>Kanji ligados a esta fonte</h3>
        {table_html(["Kanji", "Count", "Score"], kanji_table_rows)}
      </section>
    </div>
    """
    return layout(title=f"Fonte {source_id}", body=body, active_path="/sources")


def flashcards_page(db_path: Path, params: dict[str, list[str]]) -> str:
    q = first_value(params, "q")
    status = first_value(params, "status", "all")
    limit = parse_limit(first_value(params, "limit", "50"))
    page = parse_page(first_value(params, "page", "1"))

    filters = []
    sql_params: list[object] = []
    if status != "all":
        filters.append("f.export_status = ?")
        sql_params.append(status)
    if q.strip():
        filters.append(
            """
            (
                f.front_text LIKE ?
                OR f.back_text LIKE ?
                OR t.lemma LIKE ?
                OR COALESCE(t.reading, '') LIKE ?
                OR COALESCE(t.meaning_pt, '') LIKE ?
            )
            """
        )
        like = sql_like(q)
        sql_params.extend([like, like, like, like, like])

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
                COALESCE(t.reading, '-') AS reading,
                printf('%.1f', COALESCE(ts.priority_score, 0)) AS priority_score,
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
    }
    pagination_markup = pagination_nav(
        path="/flashcards",
        base_params=base_params,
        pagination=pagination,
    )
    reset_href = "/flashcards" if any(value is not None for value in base_params.values()) or pagination.page > 1 else None
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
    table_rows = [
        [
            f'<a href="/terms/{row["term_id"]}">{escape(row["lemma"])}</a>',
            escape(row["reading"]),
            escape(row["priority_score"]),
            escape(row["template_name"]),
            escape(row["deck_name"]),
            status_chip(text(row["export_status"])),
            escape(row["source_id"]),
            escape(truncate(text(row["front_text"], ""), size=52)),
        ]
        for row in rows
    ]
    body = f"""
    <div class="grid">
      <section class="panel">
        {notice_banner(params)}
        <h2>Flashcards</h2>
        {sync_anki_action(next_path="/flashcards")}
        {search_form(action="/flashcards", label="Buscar cards", placeholder="Ex.: guardar, persona, progress", q=q, extras=extras, reset_href=reset_href)}
        <div class="results-toolbar">
          <p class="results-summary">{results_summary_text(pagination)}</p>
          {pagination_markup}
        </div>
        {table_html(["Termo", "Leitura", "Score", "Template", "Deck", "Status", "Fonte", "Frente"], table_rows)}
      </section>
    </div>
    """
    return layout(
        title="Flashcards",
        body=body,
        active_path="/flashcards",
        extra_scripts=auto_reload_script(db_path),
    )


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
            page = term_detail_page(self.db_path, term_id)
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
            page = kanji_detail_page(self.db_path, kanji_id)
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
            page = source_detail_page(self.db_path, source_id)
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

        self.respond_not_found("Acao nao encontrada.")

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
        source_path = Path(str(raw_path)) if raw_path else None
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
