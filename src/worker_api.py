"""Standalone Paperless KIplus worker API and web interface.

Purpose:
- Runs Paperless KIplus as an independent HTTP worker outside Home Assistant.
- Provides a browser-based control panel plus a small JSON API for Home
  Assistant remote execution and config synchronization.

Input / Output:
- Input: worker config YAML, HTTP commands (`run`, `stop`, `resume`, ...), and
  environment / CLI options for the worker itself.
- Output: live status, logs, config import/export, and a fully standalone web
  UI that can operate the sorter without Home Assistant.

Important invariants:
- The worker owns process execution in standalone / Unraid mode.
- The sorter config file is the single runtime source of truth for standalone
  runs, whether edited in the web UI or pushed from Home Assistant.
- API responses stay intentionally close to the Home Assistant runner state so
  remote mode can mirror them with minimal translation.

How to debug:
- Open `/` in the browser and inspect the status, config metadata, and logs.
- Query `/api/status` directly to see the raw runtime payload.
- Check the persisted files under the worker data directory:
  `config/`, `state/`, `logs/`, `exports/`.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import shlex
import subprocess
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import yaml

from entity_review import (
    EntityRecord,
    build_ai_prompt_context,
    filter_candidates_by_rules,
    find_duplicate_candidates,
    load_review_store,
    review_rules_from_store,
    upsert_review_rule,
)
from paperless_ai_sorter import (
    RUN_PAUSE_EXIT_CODE,
    RUN_STATE_FILE_DEFAULT,
    STOP_REQUEST_FILE_DEFAULT,
    ConfigError,
    PaperlessApiError,
    PaperlessClient,
    load_config,
)

LOGGER = logging.getLogger("paperless_worker")
RUNTIME_EVENT_MARKER = "PAPERLESS_RUNTIME_EVENT "
TAIL_LIMIT_CHARS = 20000
FORCE_STOP_GRACE_SECONDS = 5.0
DEFAULT_PORT = 8787
DEFAULT_HOST = "0.0.0.0"

WORKER_WEB_UI_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Paperless KIplus Worker</title>
  <style>
    :root {
      --bg: #f6f2eb;
      --panel: #fffdf9;
      --line: #d9c8b2;
      --text: #2c241a;
      --muted: #7c6b57;
      --accent: #0b6e4f;
      --accent-2: #c97a00;
      --danger: #a83232;
      --shadow: 0 10px 30px rgba(44,36,26,0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(201,122,0,0.08), transparent 28%),
        linear-gradient(180deg, #f8f3ec 0%, #efe5d6 100%);
      color: var(--text);
    }
    header {
      padding: 24px 28px;
      border-bottom: 1px solid var(--line);
      background: rgba(255,253,249,0.92);
      position: sticky;
      top: 0;
      backdrop-filter: blur(10px);
      z-index: 10;
    }
    h1 { margin: 0 0 8px; font-size: 28px; }
    p { margin: 0; color: var(--muted); }
    main {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 20px;
      padding: 24px;
    }
    .panel {
      background: var(--panel);
      border: 1px solid rgba(217,200,178,0.9);
      border-radius: 18px;
      box-shadow: var(--shadow);
      padding: 20px;
    }
    .panel.wide { grid-column: 1 / -1; }
    h2 { margin: 0 0 14px; font-size: 18px; }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 14px;
      padding: 12px;
      background: #fff;
    }
    .stat small { display: block; color: var(--muted); margin-bottom: 6px; }
    .stat strong { font-size: 22px; }
    .controls, .links {
      display: flex;
      flex-wrap: wrap;
      gap: 10px;
    }
    button, a.button {
      appearance: none;
      border: none;
      border-radius: 999px;
      background: var(--accent);
      color: white;
      padding: 10px 14px;
      font-weight: 600;
      cursor: pointer;
      text-decoration: none;
    }
    button.secondary, a.button.secondary { background: #46627a; }
    button.warn, a.button.warn { background: var(--accent-2); }
    button.danger, a.button.danger { background: var(--danger); }
    button:disabled { opacity: 0.6; cursor: not-allowed; }
    label {
      display: block;
      font-size: 13px;
      color: var(--muted);
      margin-bottom: 6px;
    }
    input, textarea, select {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 12px;
      padding: 10px 12px;
      font: inherit;
      background: white;
      color: var(--text);
    }
    textarea {
      min-height: 340px;
      resize: vertical;
      font-family: "SFMono-Regular", Consolas, monospace;
      font-size: 12px;
      line-height: 1.45;
    }
    .field-row {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 12px;
      margin-bottom: 12px;
    }
    .meta {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
      margin-top: 12px;
    }
    code, pre {
      font-family: "SFMono-Regular", Consolas, monospace;
    }
    pre {
      margin: 0;
      max-height: 420px;
      overflow: auto;
      padding: 14px;
      border-radius: 14px;
      background: #201a14;
      color: #f8f3ec;
      white-space: pre-wrap;
      word-break: break-word;
    }
    .message {
      margin-top: 12px;
      padding: 12px 14px;
      border-radius: 12px;
      background: #fff6ea;
      border: 1px solid #ebd5b3;
      color: #694a0c;
      display: none;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 8px 12px;
      border-radius: 999px;
      background: #e7f5ee;
      color: var(--accent);
      font-weight: 700;
      margin-bottom: 14px;
    }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <header>
    <h1>Paperless KIplus Worker</h1>
    <p>Standalone Weboberfläche für Unraid, Docker und den optionalen Home-Assistant-Remote-Modus.</p>
  </header>
  <main>
    <section class="panel">
      <div class="status-pill" id="status-pill">Status: lädt …</div>
      <div class="stats">
        <div class="stat"><small>Fortschritt</small><strong id="progress-percent">0%</strong></div>
        <div class="stat"><small>Aktuell</small><strong id="current-title">-</strong></div>
        <div class="stat"><small>Letztes fertiges Dokument</small><strong id="last-title">-</strong></div>
        <div class="stat"><small>Resume</small><strong id="resume-state">nein</strong></div>
      </div>
      <div class="meta">
        <div><strong>Gescannt:</strong> <span id="stat-scanned">0</span></div>
        <div><strong>Aktualisiert:</strong> <span id="stat-updated">0</span></div>
        <div><strong>Übersprungen:</strong> <span id="stat-skipped">0</span></div>
        <div><strong>Fehler:</strong> <span id="stat-failed">0</span></div>
        <div><strong>Tokens letzter Lauf:</strong> <span id="stat-tokens">0</span></div>
        <div><strong>Kosten letzter Lauf:</strong> <span id="stat-cost">0.0</span></div>
      </div>
      <div class="links" style="margin-top: 16px;">
        <a id="current-link" class="button secondary" target="_blank" rel="noreferrer">Aktuelles Dokument öffnen</a>
        <a id="last-link" class="button secondary" target="_blank" rel="noreferrer">Letztes fertiges Dokument öffnen</a>
        <a class="button warn" href="/review">Dopplungen reviewen</a>
        <a id="download-log-link" class="button secondary" target="_blank" rel="noreferrer">Log herunterladen</a>
        <a id="download-config-link" class="button secondary" target="_blank" rel="noreferrer">Config herunterladen</a>
      </div>
    </section>

    <section class="panel">
      <h2>Steuerung</h2>
      <div class="field-row">
        <div>
          <label for="token-input">API-Token</label>
          <input id="token-input" type="password" placeholder="Optionaler Worker-Token" />
        </div>
        <div>
          <label for="max-documents">Max. Dokumente</label>
          <input id="max-documents" type="number" min="0" value="0" />
        </div>
        <div>
          <label for="mode-select">Laufmodus</label>
          <select id="mode-select">
            <option value="normal">Normal</option>
            <option value="backfill">Backfill</option>
            <option value="resume">Resume</option>
          </select>
        </div>
        <div>
          <label for="dry-run">Dry-Run</label>
          <select id="dry-run">
            <option value="false">Nein</option>
            <option value="true">Ja</option>
          </select>
        </div>
      </div>
      <div class="controls">
        <button id="run-btn">Lauf starten</button>
        <button id="restart-btn" class="warn">Frisch neu starten</button>
        <button id="resume-btn" class="secondary">Resume</button>
        <button id="pause-btn" class="secondary">Pausieren</button>
        <button id="stop-btn" class="danger">Sofort stoppen</button>
        <button id="reset-metrics-btn" class="secondary">Metriken zurücksetzen</button>
        <button id="reset-failed-btn" class="secondary">Fehlgeschlagene zurücksetzen</button>
        <button id="refresh-btn" class="secondary">Status aktualisieren</button>
      </div>
      <div class="message" id="action-message"></div>
    </section>

    <section class="panel wide">
      <h2>Konfiguration</h2>
      <p class="muted">Diese YAML ist die Laufzeit-Konfiguration des Workers. Home Assistant kann sie optional automatisch überschreiben.</p>
      <div class="controls" style="margin: 14px 0;">
        <button id="load-config-btn" class="secondary">Config neu laden</button>
        <button id="save-config-btn">Config speichern</button>
      </div>
      <textarea id="config-editor" spellcheck="false"></textarea>
      <div class="meta">
        <div><strong>Quelle:</strong> <span id="config-source">-</span></div>
        <div><strong>Zuletzt geändert:</strong> <span id="config-updated-at">-</span></div>
        <div><strong>Validierung:</strong> <span id="config-validation">-</span></div>
      </div>
    </section>

    <section class="panel wide">
      <h2>Live-Log</h2>
      <pre id="log-output">Lade Log …</pre>
    </section>
  </main>
  <script>
    const tokenInput = document.getElementById('token-input');
    const actionMessage = document.getElementById('action-message');
    const configEditor = document.getElementById('config-editor');
    const statusPill = document.getElementById('status-pill');
    const logOutput = document.getElementById('log-output');
    const modeSelect = document.getElementById('mode-select');
    const maxDocumentsInput = document.getElementById('max-documents');
    const dryRunSelect = document.getElementById('dry-run');

    const savedToken = localStorage.getItem('paperless_kiplus_worker_token') || '';
    tokenInput.value = savedToken;
    tokenInput.addEventListener('change', () => {
      localStorage.setItem('paperless_kiplus_worker_token', tokenInput.value.trim());
    });

    function apiHeaders() {
      const headers = { 'Accept': 'application/json' };
      const token = tokenInput.value.trim();
      if (token) {
        headers['Authorization'] = `Bearer ${token}`;
      }
      return headers;
    }

    async function apiJson(path, method = 'GET', payload = null) {
      const options = { method, headers: apiHeaders() };
      if (payload !== null) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(path, options);
      const text = await response.text();
      let data = {};
      if (text.trim()) {
        data = JSON.parse(text);
      }
      if (!response.ok) {
        throw new Error(data.message || text || `HTTP ${response.status}`);
      }
      return data;
    }

    function showMessage(text, isError = false) {
      actionMessage.style.display = 'block';
      actionMessage.style.background = isError ? '#fdeeee' : '#fff6ea';
      actionMessage.style.borderColor = isError ? '#e8b2b2' : '#ebd5b3';
      actionMessage.style.color = isError ? '#7a1b1b' : '#694a0c';
      actionMessage.textContent = text;
    }

    function setLink(id, url) {
      const element = document.getElementById(id);
      if (url) {
        element.href = url;
        element.style.pointerEvents = 'auto';
        element.style.opacity = '1';
      } else {
        element.href = '#';
        element.style.pointerEvents = 'none';
        element.style.opacity = '0.5';
      }
    }

    function renderStatus(payload) {
      const status = payload.status || 'idle';
      statusPill.textContent = `Status: ${status}`;
      document.getElementById('progress-percent').textContent = `${Number(payload.progress_percent || 0).toFixed(2)}%`;
      document.getElementById('current-title').textContent = payload.progress_current_document_title || '-';
      document.getElementById('last-title').textContent = payload.last_completed_document_title || '-';
      document.getElementById('resume-state').textContent = payload.resume_available ? 'ja' : 'nein';
      document.getElementById('stat-scanned').textContent = payload.progress_scanned || 0;
      document.getElementById('stat-updated').textContent = payload.progress_updated || 0;
      document.getElementById('stat-skipped').textContent = payload.progress_skipped || 0;
      document.getElementById('stat-failed').textContent = payload.progress_failed || 0;
      document.getElementById('stat-tokens').textContent = payload.last_run_total_tokens || 0;
      document.getElementById('stat-cost').textContent = Number(payload.last_run_cost_eur || 0).toFixed(6);
      document.getElementById('config-source').textContent = payload.config_source || '-';
      document.getElementById('config-updated-at').textContent = payload.config_updated_at || '-';
      document.getElementById('config-validation').textContent = payload.config_validation_message || '-';
      setLink('current-link', payload.progress_current_document_url || '');
      setLink('last-link', payload.last_completed_document_url || '');
      setLink('download-log-link', '/api/logs/download');
      setLink('download-config-link', '/api/config/download');
    }

    async function refreshStatus() {
      try {
        const payload = await apiJson('/api/status');
        renderStatus(payload);
      } catch (error) {
        showMessage(`Status konnte nicht geladen werden: ${error.message}`, true);
      }
    }

    async function refreshConfig() {
      try {
        const payload = await apiJson('/api/config/export');
        configEditor.value = payload.yaml_text || '';
      } catch (error) {
        showMessage(`Config konnte nicht geladen werden: ${error.message}`, true);
      }
    }

    async function refreshLog() {
      try {
        const payload = await apiJson('/api/logs');
        logOutput.textContent = payload.log_text || '[Kein Log vorhanden]';
      } catch (error) {
        logOutput.textContent = `Log konnte nicht geladen werden: ${error.message}`;
      }
    }

    async function callAction(path, payload = {}) {
      try {
        const data = await apiJson(path, 'POST', payload);
        if (data.status) {
          renderStatus(data.status);
        }
        showMessage(data.message || 'Aktion ausgeführt.');
        await refreshLog();
      } catch (error) {
        showMessage(error.message, true);
      }
    }

    document.getElementById('run-btn').addEventListener('click', async () => {
      const mode = modeSelect.value;
      if (mode === 'resume') {
        await callAction('/api/resume', { force: true });
        return;
      }
      await callAction('/api/run', {
        force: false,
        dry_run: dryRunSelect.value === 'true',
        max_documents: Number(maxDocumentsInput.value || 0),
        backfill_existing_documents: mode === 'backfill'
      });
    });

    document.getElementById('restart-btn').addEventListener('click', async () => {
      await callAction('/api/restart', {
        force: true,
        backfill_existing_documents: modeSelect.value === 'backfill'
      });
    });
    document.getElementById('resume-btn').addEventListener('click', async () => callAction('/api/resume', { force: true }));
    document.getElementById('pause-btn').addEventListener('click', async () => callAction('/api/stop', {}));
    document.getElementById('stop-btn').addEventListener('click', async () => callAction('/api/stop_now', {}));
    document.getElementById('reset-metrics-btn').addEventListener('click', async () => callAction('/api/metrics/reset', {}));
    document.getElementById('reset-failed-btn').addEventListener('click', async () => callAction('/api/failed/reset', {}));
    document.getElementById('refresh-btn').addEventListener('click', async () => {
      await refreshStatus();
      await refreshLog();
    });
    document.getElementById('load-config-btn').addEventListener('click', refreshConfig);
    document.getElementById('save-config-btn').addEventListener('click', async () => {
      await callAction('/api/config/import', { yaml_text: configEditor.value, source: 'web_ui' });
      await refreshConfig();
      await refreshStatus();
    });

    async function tick() {
      await refreshStatus();
      await refreshLog();
    }

    refreshConfig();
    tick();
    setInterval(tick, 5000);
  </script>
</body>
</html>
"""

ENTITY_REVIEW_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Paperless KIplus Entity Review</title>
  <style>
    :root {
      --bg: #f7f5f1;
      --panel: #fffefa;
      --line: #d7d0c4;
      --text: #24211d;
      --muted: #6f675d;
      --accent: #0f766e;
      --accent-2: #9a5b00;
      --danger: #a83232;
      --ok: #0b6e4f;
      --shadow: 0 8px 24px rgba(36,33,29,0.08);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      padding: 18px 24px;
      background: rgba(255,254,250,0.96);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 5;
    }
    h1 { margin: 0; font-size: 22px; }
    h2 { margin: 0 0 12px; font-size: 17px; }
    p { margin: 0; color: var(--muted); }
    main {
      display: grid;
      grid-template-columns: minmax(360px, 1.1fr) minmax(360px, 0.9fr);
      gap: 18px;
      padding: 18px;
      align-items: start;
    }
    .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
      padding: 16px;
    }
    .wide { grid-column: 1 / -1; }
    .toolbar {
      display: grid;
      grid-template-columns: 1fr 130px 140px auto;
      gap: 10px;
      align-items: end;
      margin-bottom: 12px;
    }
    label {
      display: block;
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 5px;
    }
    input, select, textarea {
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      background: #fff;
      color: var(--text);
    }
    textarea {
      min-height: 150px;
      resize: vertical;
      line-height: 1.4;
    }
    button, a.button {
      border: 0;
      border-radius: 6px;
      padding: 9px 12px;
      background: var(--accent);
      color: white;
      font-weight: 700;
      cursor: pointer;
      text-decoration: none;
      white-space: nowrap;
    }
    button.secondary, a.button.secondary { background: #52677a; }
    button.warn { background: var(--accent-2); }
    button.danger { background: var(--danger); }
    button:disabled { opacity: 0.55; cursor: not-allowed; }
    .stats {
      display: grid;
      grid-template-columns: repeat(4, minmax(0, 1fr));
      gap: 8px;
      margin-bottom: 12px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      background: #fff;
    }
    .stat small { display: block; color: var(--muted); margin-bottom: 4px; }
    .stat strong { font-size: 20px; }
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      vertical-align: top;
      border-bottom: 1px solid var(--line);
      padding: 9px 8px;
    }
    th {
      color: var(--muted);
      font-size: 12px;
      font-weight: 700;
      background: #faf8f4;
      position: sticky;
      top: 69px;
      z-index: 2;
    }
    tbody tr { cursor: pointer; }
    tbody tr:hover, tbody tr.selected { background: #eef8f5; }
    .pill {
      display: inline-block;
      padding: 3px 7px;
      border-radius: 999px;
      background: #e8eee9;
      color: var(--ok);
      font-size: 12px;
      font-weight: 700;
    }
    .muted { color: var(--muted); }
    .grid2 {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-top: 12px;
    }
    .message {
      margin-top: 12px;
      padding: 10px 12px;
      border-radius: 6px;
      border: 1px solid #ead1a4;
      background: #fff8e8;
      color: #654400;
      display: none;
      white-space: pre-wrap;
    }
    .message.error {
      border-color: #e5b1b1;
      background: #fff0f0;
      color: #7a1b1b;
    }
    pre {
      max-height: 260px;
      overflow: auto;
      margin: 0;
      padding: 12px;
      border-radius: 6px;
      background: #211e1a;
      color: #f7f5f1;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.45;
    }
    @media (max-width: 980px) {
      main { grid-template-columns: 1fr; }
      .toolbar { grid-template-columns: 1fr 1fr; }
      .stats { grid-template-columns: repeat(2, 1fr); }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>Entity Review</h1>
      <p>Dokumenttypen und Korrespondenten auf Dopplungen prüfen, zusammenführen und als KI-Regel speichern.</p>
    </div>
    <a class="button secondary" href="/">Worker</a>
  </header>
  <main>
    <section class="panel">
      <div class="toolbar">
        <div>
          <label for="search-input">Suche</label>
          <input id="search-input" placeholder="Name, Typ, Grund …" />
        </div>
        <div>
          <label for="type-filter">Typ</label>
          <select id="type-filter">
            <option value="all">Alle</option>
            <option value="document_type">Dokumenttypen</option>
            <option value="correspondent">Korrespondenten</option>
          </select>
        </div>
        <div>
          <label for="threshold-input">Ähnlichkeit</label>
          <input id="threshold-input" type="number" min="0.5" max="1" step="0.01" value="0.84" />
        </div>
        <button id="refresh-btn">Aktualisieren</button>
      </div>
      <div class="stats">
        <div class="stat"><small>Kandidaten</small><strong id="stat-candidates">0</strong></div>
        <div class="stat"><small>Regeln</small><strong id="stat-rules">0</strong></div>
        <div class="stat"><small>Dokumenttypen</small><strong id="stat-doc-types">0</strong></div>
        <div class="stat"><small>Korrespondenten</small><strong id="stat-correspondents">0</strong></div>
      </div>
      <table>
        <thead>
          <tr>
            <th>Typ</th>
            <th>Dopplung</th>
            <th>Ziel</th>
            <th>Score</th>
          </tr>
        </thead>
        <tbody id="candidate-body"></tbody>
      </table>
      <div id="list-message" class="message"></div>
    </section>

    <section class="panel">
      <h2>Entscheidung</h2>
      <p id="selection-hint">Wähle links einen Kandidaten aus.</p>
      <div id="decision-form" style="display: none;">
        <div class="grid2">
          <div>
            <label for="alias-select">Dopplung / Alias</label>
            <select id="alias-select"></select>
          </div>
          <div>
            <label for="canonical-select">Zielwert</label>
            <select id="canonical-select"></select>
          </div>
        </div>
        <div class="grid2" style="margin-top: 10px;">
          <div>
            <label for="selected-type">Entity-Typ</label>
            <input id="selected-type" disabled />
          </div>
          <div>
            <label for="delete-alias">Alias nach Merge löschen</label>
            <select id="delete-alias">
              <option value="true">Ja</option>
              <option value="false">Nein</option>
            </select>
          </div>
        </div>
        <div style="margin-top: 10px;">
          <label for="context-input">Zusätzlicher Kontext für die KI</label>
          <textarea id="context-input" placeholder="Warum sind diese Namen gleich? Wann soll die KI den Zielwert verwenden?"></textarea>
        </div>
        <div class="actions">
          <button id="swap-btn" class="secondary">Alias/Ziel tauschen</button>
          <button id="save-rule-btn">Nur KI-Regel speichern</button>
          <button id="dry-run-btn" class="warn">Merge planen</button>
          <button id="merge-btn" class="danger">Merge anwenden</button>
          <button id="ignore-btn" class="secondary">Kein Duplikat</button>
        </div>
        <div id="action-message" class="message"></div>
      </div>
    </section>

    <section class="panel wide">
      <h2>KI-Kontext aus geprüften Regeln</h2>
      <pre id="ai-context">Noch keine Regeln geladen.</pre>
    </section>
  </main>

  <script>
    const state = {
      payload: null,
      candidates: [],
      filtered: [],
      selected: null
    };

    const candidateBody = document.getElementById('candidate-body');
    const actionMessage = document.getElementById('action-message');
    const listMessage = document.getElementById('list-message');
    const decisionForm = document.getElementById('decision-form');
    const selectionHint = document.getElementById('selection-hint');
    const aliasSelect = document.getElementById('alias-select');
    const canonicalSelect = document.getElementById('canonical-select');
    const contextInput = document.getElementById('context-input');
    const selectedTypeInput = document.getElementById('selected-type');

    function apiHeaders() {
      const headers = { 'Accept': 'application/json' };
      const token = localStorage.getItem('paperless_kiplus_worker_token') || '';
      if (token.trim()) {
        headers.Authorization = `Bearer ${token.trim()}`;
      }
      return headers;
    }

    async function apiJson(path, method = 'GET', payload = null) {
      const options = { method, headers: apiHeaders() };
      if (payload !== null) {
        options.headers['Content-Type'] = 'application/json';
        options.body = JSON.stringify(payload);
      }
      const response = await fetch(path, options);
      const text = await response.text();
      const data = text.trim() ? JSON.parse(text) : {};
      if (!response.ok) {
        throw new Error(data.message || text || `HTTP ${response.status}`);
      }
      return data;
    }

    function showMessage(element, text, isError = false) {
      element.style.display = 'block';
      element.classList.toggle('error', Boolean(isError));
      element.textContent = text;
    }

    function hideMessage(element) {
      element.style.display = 'none';
      element.textContent = '';
    }

    function entityLabel(type) {
      return type === 'document_type' ? 'Dokumenttyp' : 'Korrespondent';
    }

    function entityOptions(type) {
      const group = state.payload?.entities?.[type] || [];
      return group.slice().sort((a, b) => String(a.name).localeCompare(String(b.name), 'de'));
    }

    function fillSelect(select, type, selectedId) {
      select.innerHTML = '';
      for (const item of entityOptions(type)) {
        const option = document.createElement('option');
        option.value = String(item.id);
        option.textContent = `${item.name} (${item.document_count || 0})`;
        option.dataset.name = item.name;
        select.appendChild(option);
      }
      select.value = String(selectedId);
    }

    function selectedEntity(select) {
      const option = select.options[select.selectedIndex];
      return {
        id: Number(select.value),
        name: option ? option.dataset.name || option.textContent : ''
      };
    }

    function renderStats(payload) {
      document.getElementById('stat-candidates').textContent = payload.candidates.length;
      document.getElementById('stat-rules').textContent = payload.rules.length;
      document.getElementById('stat-doc-types').textContent = payload.entities.document_type.length;
      document.getElementById('stat-correspondents').textContent = payload.entities.correspondent.length;
      document.getElementById('ai-context').textContent = payload.ai_context || 'Noch keine gespeicherten Prefer-/Merge-Regeln vorhanden.';
    }

    function candidateMatches(candidate, query, typeFilter) {
      if (typeFilter !== 'all' && candidate.entity_type !== typeFilter) {
        return false;
      }
      if (!query) {
        return true;
      }
      const haystack = [
        candidate.entity_type,
        candidate.alias_name,
        candidate.canonical_name,
        candidate.reason,
        String(candidate.similarity)
      ].join(' ').toLowerCase();
      return haystack.includes(query);
    }

    function renderCandidates() {
      const query = document.getElementById('search-input').value.trim().toLowerCase();
      const typeFilter = document.getElementById('type-filter').value;
      state.filtered = state.candidates.filter(candidate => candidateMatches(candidate, query, typeFilter));
      candidateBody.innerHTML = '';
      for (const candidate of state.filtered) {
        const row = document.createElement('tr');
        row.dataset.pairKey = candidate.pair_key;
        if (state.selected && state.selected.pair_key === candidate.pair_key) {
          row.classList.add('selected');
        }
        row.innerHTML = `
          <td><span class="pill">${entityLabel(candidate.entity_type)}</span><br><span class="muted">${candidate.reason}</span></td>
          <td>${escapeHtml(candidate.alias_name)}<br><span class="muted">${candidate.alias_document_count || 0} Dokumente</span></td>
          <td>${escapeHtml(candidate.canonical_name)}<br><span class="muted">${candidate.canonical_document_count || 0} Dokumente</span></td>
          <td><strong>${Number(candidate.similarity || 0).toFixed(2)}</strong></td>
        `;
        row.addEventListener('click', () => selectCandidate(candidate));
        candidateBody.appendChild(row);
      }
      if (!state.filtered.length) {
        showMessage(listMessage, 'Keine Kandidaten für diese Filter gefunden.');
      } else {
        hideMessage(listMessage);
      }
    }

    function escapeHtml(value) {
      return String(value || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function selectCandidate(candidate) {
      state.selected = candidate;
      selectionHint.style.display = 'none';
      decisionForm.style.display = 'block';
      selectedTypeInput.value = entityLabel(candidate.entity_type);
      fillSelect(aliasSelect, candidate.entity_type, candidate.alias_id);
      fillSelect(canonicalSelect, candidate.entity_type, candidate.canonical_id);
      contextInput.value = '';
      hideMessage(actionMessage);
      renderCandidates();
    }

    function buildDecisionPayload(action = 'prefer') {
      if (!state.selected) {
        throw new Error('Kein Kandidat ausgewählt.');
      }
      const alias = selectedEntity(aliasSelect);
      const canonical = selectedEntity(canonicalSelect);
      if (alias.id === canonical.id) {
        throw new Error('Alias und Ziel dürfen nicht identisch sein.');
      }
      return {
        entity_type: state.selected.entity_type,
        alias_id: alias.id,
        alias_name: alias.name,
        canonical_id: canonical.id,
        canonical_name: canonical.name,
        context: contextInput.value,
        action,
        delete_alias: document.getElementById('delete-alias').value === 'true'
      };
    }

    async function refresh() {
      hideMessage(listMessage);
      const threshold = Number(document.getElementById('threshold-input').value || 0.84);
      const payload = await apiJson(`/api/review/entities?threshold=${encodeURIComponent(threshold)}`);
      state.payload = payload;
      state.candidates = payload.candidates || [];
      renderStats(payload);
      renderCandidates();
    }

    document.getElementById('refresh-btn').addEventListener('click', async () => {
      try {
        await refresh();
      } catch (error) {
        showMessage(listMessage, `Review konnte nicht geladen werden: ${error.message}`, true);
      }
    });
    document.getElementById('search-input').addEventListener('input', renderCandidates);
    document.getElementById('type-filter').addEventListener('change', renderCandidates);

    document.getElementById('swap-btn').addEventListener('click', () => {
      const aliasValue = aliasSelect.value;
      aliasSelect.value = canonicalSelect.value;
      canonicalSelect.value = aliasValue;
    });

    document.getElementById('save-rule-btn').addEventListener('click', async () => {
      try {
        const result = await apiJson('/api/review/rules', 'POST', buildDecisionPayload('prefer'));
        showMessage(actionMessage, result.message || 'KI-Regel gespeichert.');
        await refresh();
      } catch (error) {
        showMessage(actionMessage, error.message, true);
      }
    });

    document.getElementById('dry-run-btn').addEventListener('click', async () => {
      try {
        const result = await apiJson('/api/review/merge', 'POST', { ...buildDecisionPayload('merge'), dry_run: true });
        showMessage(actionMessage, `Merge-Plan: ${result.affected_count || 0} Dokumente würden umgehängt. Vorschau-IDs: ${(result.affected_document_ids_preview || []).join(', ') || '-'}\n${result.message || ''}`);
      } catch (error) {
        showMessage(actionMessage, error.message, true);
      }
    });

    document.getElementById('merge-btn').addEventListener('click', async () => {
      try {
        const payload = buildDecisionPayload('merge');
        const ok = confirm(`Wirklich zusammenführen?\n\n${payload.alias_name}\n→ ${payload.canonical_name}`);
        if (!ok) {
          return;
        }
        const result = await apiJson('/api/review/merge', 'POST', { ...payload, dry_run: false });
        showMessage(actionMessage, result.message || `Merge angewendet: ${result.updated_count || 0} Dokumente aktualisiert.`);
        await refresh();
      } catch (error) {
        showMessage(actionMessage, error.message, true);
      }
    });

    document.getElementById('ignore-btn').addEventListener('click', async () => {
      try {
        const result = await apiJson('/api/review/rules', 'POST', buildDecisionPayload('ignore'));
        showMessage(actionMessage, result.message || 'Als kein Duplikat gespeichert.');
        await refresh();
      } catch (error) {
        showMessage(actionMessage, error.message, true);
      }
    });

    refresh().catch(error => showMessage(listMessage, `Review konnte nicht geladen werden: ${error.message}`, true));
  </script>
</body>
</html>
"""


def build_paperless_document_url(base_url: str, document_id: int | None) -> str:
    """Build a Paperless document detail URL from base URL and document id."""

    normalized_base = str(base_url or "").strip().rstrip("/")
    if not normalized_base or document_id is None:
        return ""
    return f"{normalized_base}/documents/{int(document_id)}/details"


@dataclass
class WorkerPaths:
    """Resolved filesystem layout for the worker runtime."""

    data_dir: Path
    config_dir: Path
    state_dir: Path
    logs_dir: Path
    exports_dir: Path
    config_file: Path
    run_state_file: Path
    stop_request_file: Path
    metrics_file: Path
    entity_review_rules_file: Path
    worker_meta_file: Path
    log_file: Path


class WorkerManager:
    """Owns the standalone sorter process, config, and runtime state."""

    def __init__(
        self,
        *,
        data_dir: Path,
        sorter_command: list[str],
        auth_token: str,
    ) -> None:
        self.paths = WorkerPaths(
            data_dir=data_dir,
            config_dir=data_dir / "config",
            state_dir=data_dir / "state",
            logs_dir=data_dir / "logs",
            exports_dir=data_dir / "exports",
            config_file=data_dir / "config" / "config.yaml",
            run_state_file=data_dir / "state" / "run_state.json",
            stop_request_file=data_dir / "state" / "stop.request",
            metrics_file=data_dir / "state" / "run_metrics.json",
            entity_review_rules_file=data_dir / "state" / "entity_review_rules.json",
            worker_meta_file=data_dir / "state" / "worker_meta.json",
            log_file=data_dir / "logs" / "worker.log",
        )
        self.sorter_command = list(sorter_command)
        self.auth_token = auth_token
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.stdout_thread: threading.Thread | None = None
        self.stderr_thread: threading.Thread | None = None
        self.wait_thread: threading.Thread | None = None
        self.auto_resume_timer: threading.Timer | None = None
        self.force_stop_timer: threading.Timer | None = None
        self.log_lines: list[str] = []
        self.latest_runtime_payload: dict[str, Any] = {}

        self.running = False
        self.stop_requested = False
        self.force_stop_requested = False
        self.resume_available = False
        self.pause_reason = ""
        self.auto_resume_at: datetime | None = None
        self.last_started: datetime | None = None
        self.last_finished: datetime | None = None
        self.last_exit_code: int | None = None
        self.last_status = "idle"
        self.last_message = "not started"
        self.last_command_executed = ""
        self.last_stdout_tail = ""
        self.last_stderr_tail = ""
        self.last_summary_line = ""
        self.last_cost_line = ""
        self.last_log_export_path = ""
        self.last_log_export_url = "/api/logs/download"
        self.last_scanned = 0
        self.last_updated = 0
        self.last_skipped = 0
        self.last_failed = 0
        self.last_run_total_tokens = 0
        self.last_run_cost_eur = 0.0
        self.last_run_bypass_skipped = 0
        self.total_tokens = 0
        self.total_cost_eur = 0.0
        self.total_bypass_skipped = 0
        self.active_quarantine_count = 0
        self.active_bypass_count = 0
        self.progress_total_documents = 0
        self.progress_completed_documents = 0
        self.progress_percent = 0.0
        self.progress_scanned = 0
        self.progress_updated = 0
        self.progress_skipped = 0
        self.progress_failed = 0
        self.progress_bypassed = 0
        self.progress_bypass_skipped = 0
        self.progress_prefiltered_ki_tagged = 0
        self.progress_budget_used = 0
        self.progress_pending_documents = 0
        self.progress_current_document_id: int | None = None
        self.progress_current_document_title = ""
        self.progress_current_document_url = ""
        self.progress_last_event_at: datetime | None = None
        self.last_completed_document_id: int | None = None
        self.last_completed_document_title = ""
        self.last_completed_document_url = ""
        self.last_completed_document_at: datetime | None = None
        self.paperless_base_url = ""
        self.config_source = "worker_local"
        self.config_updated_at: datetime | None = None
        self.config_validation_ok = False
        self.config_validation_message = "Noch keine Konfiguration gespeichert."

        self._ensure_directories()
        self._restore_state_on_startup()

    def _ensure_directories(self) -> None:
        for path in (
            self.paths.data_dir,
            self.paths.config_dir,
            self.paths.state_dir,
            self.paths.logs_dir,
            self.paths.exports_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def _restore_state_on_startup(self) -> None:
        self._load_worker_metadata()
        self._refresh_config_state()
        self._refresh_metrics_from_file()
        self._refresh_failed_state_counts()
        self._refresh_resume_state()
        if self.resume_available and self.auto_resume_at is not None:
            self._schedule_auto_resume()

    @staticmethod
    def _parse_datetime(value: Any) -> datetime | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None

    @staticmethod
    def _safe_int(value: Any, default: int = 0) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _safe_float(value: Any, default: float = 0.0) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _append_log_line(self, stream_name: str, line: str) -> None:
        stripped = line.rstrip("\n")
        prefixed = f"[{stream_name}] {stripped}"
        self.log_lines.append(prefixed)
        self.log_lines = self.log_lines[-1500:]
        combined_text = "\n".join(self.log_lines)
        if len(combined_text) > TAIL_LIMIT_CHARS:
            combined_text = combined_text[-TAIL_LIMIT_CHARS:]
            self.log_lines = combined_text.splitlines()
        if stream_name == "STDOUT":
            self.last_stdout_tail = (self.last_stdout_tail + stripped + "\n")[-TAIL_LIMIT_CHARS:]
        else:
            self.last_stderr_tail = (self.last_stderr_tail + stripped + "\n")[-TAIL_LIMIT_CHARS:]
        if "Kosten/Token:" in stripped:
            self.last_cost_line = stripped
        if stripped.startswith("Fertig."):
            self.last_summary_line = stripped
        self.paths.log_file.write_text(combined_text, encoding="utf-8")
        if RUNTIME_EVENT_MARKER in stripped:
            _, _, payload_text = stripped.partition(RUNTIME_EVENT_MARKER)
            try:
                payload = json.loads(payload_text)
            except json.JSONDecodeError:
                return
            self._apply_runtime_payload(payload)

    def _apply_runtime_payload(self, payload: dict[str, Any]) -> None:
        self.latest_runtime_payload = dict(payload)
        self.progress_total_documents = self._safe_int(payload.get("progress_total_documents") or payload.get("progress", {}).get("total_documents"))
        self.progress_completed_documents = self._safe_int(payload.get("progress_completed_documents") or payload.get("progress", {}).get("completed_documents"))
        self.progress_percent = self._safe_float(payload.get("progress_percent") or payload.get("progress", {}).get("percent"))
        self.progress_scanned = self._safe_int(payload.get("progress_scanned") or payload.get("progress", {}).get("scanned"))
        self.progress_updated = self._safe_int(payload.get("progress_updated") or payload.get("progress", {}).get("updated"))
        self.progress_skipped = self._safe_int(payload.get("progress_skipped") or payload.get("progress", {}).get("skipped"))
        self.progress_failed = self._safe_int(payload.get("progress_failed") or payload.get("progress", {}).get("failed"))
        self.progress_bypassed = self._safe_int(payload.get("progress_bypassed") or payload.get("progress", {}).get("bypassed"))
        self.progress_bypass_skipped = self._safe_int(payload.get("progress_bypass_skipped") or payload.get("progress", {}).get("bypass_skipped"))
        self.progress_prefiltered_ki_tagged = self._safe_int(payload.get("progress_prefiltered_ki_tagged") or payload.get("progress", {}).get("prefilt_ki_tagged"))
        self.progress_budget_used = self._safe_int(payload.get("progress_budget_used") or payload.get("progress", {}).get("budget_used"))
        self.progress_pending_documents = len(payload.get("pending_documents") or [])
        current_document = payload.get("current_document") or {}
        if isinstance(current_document, dict):
            self.progress_current_document_id = current_document.get("id")
            self.progress_current_document_title = str(current_document.get("title") or "")
        self.progress_last_event_at = self._parse_datetime(payload.get("updated_at")) or datetime.now(UTC)
        self.progress_current_document_url = build_paperless_document_url(
            self.paperless_base_url,
            self.progress_current_document_id,
        )
        completed_ids = {self._safe_int(doc_id) for doc_id in (payload.get("completed_document_ids") or [])}
        if self.progress_current_document_id is not None and self.progress_current_document_id in completed_ids:
            self.last_completed_document_id = self.progress_current_document_id
            self.last_completed_document_title = self.progress_current_document_title
            self.last_completed_document_url = self.progress_current_document_url
            self.last_completed_document_at = self.progress_last_event_at
        self.last_scanned = self.progress_scanned
        self.last_updated = self.progress_updated
        self.last_skipped = self.progress_skipped
        self.last_failed = self.progress_failed
        kind = str(payload.get("kind") or "")
        status = str(payload.get("status") or "")
        self.pause_reason = str(payload.get("pause_reason") or self.pause_reason or "")
        retry_after_seconds = self._safe_float(payload.get("retry_after_seconds"), None)
        if kind == "paused":
            self.resume_available = True
            if retry_after_seconds is not None:
                self.auto_resume_at = datetime.now(UTC) + timedelta(seconds=retry_after_seconds)
            else:
                self.auto_resume_at = None
        elif status == "success":
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            self.progress_pending_documents = 0

    def _load_worker_metadata(self) -> None:
        if not self.paths.worker_meta_file.exists():
            return
        try:
            payload = json.loads(self.paths.worker_meta_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        self.config_source = str(payload.get("config_source") or self.config_source)
        self.config_updated_at = self._parse_datetime(payload.get("config_updated_at"))
        self.config_validation_ok = bool(payload.get("config_validation_ok", False))
        self.config_validation_message = str(
            payload.get("config_validation_message") or self.config_validation_message
        )

    def _save_worker_metadata(self) -> None:
        payload = {
            "config_source": self.config_source,
            "config_updated_at": self.config_updated_at.isoformat() if self.config_updated_at else None,
            "config_validation_ok": self.config_validation_ok,
            "config_validation_message": self.config_validation_message,
        }
        self.paths.worker_meta_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _load_config_text(self) -> str:
        if not self.paths.config_file.exists():
            return ""
        return self.paths.config_file.read_text(encoding="utf-8")

    def _load_config_mapping(self) -> dict[str, Any]:
        text = self._load_config_text().strip()
        if not text:
            return {}
        try:
            parsed = yaml.safe_load(text) or {}
        except yaml.YAMLError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _refresh_config_state(self) -> None:
        payload = self._load_config_mapping()
        self.paperless_base_url = str(payload.get("paperless_url") or "").strip().rstrip("/")
        if not self.paths.config_file.exists():
            self.config_validation_ok = False
            self.config_validation_message = "Keine Worker-Konfiguration gespeichert."
            return
        try:
            load_config(str(self.paths.config_file), False)
            self.config_validation_ok = True
            self.config_validation_message = "Konfiguration ist gültig."
        except ConfigError as exc:
            self.config_validation_ok = False
            self.config_validation_message = str(exc)

    def import_config_yaml(self, yaml_text: str, *, source: str) -> dict[str, Any]:
        raw = str(yaml_text or "")
        try:
            parsed = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise ValueError(f"YAML ist ungültig: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("Die Worker-Konfiguration muss ein YAML-Objekt sein.")
        self.paths.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(raw, encoding="utf-8")
        self.config_source = source
        self.config_updated_at = datetime.now(UTC)
        self._refresh_config_state()
        self._save_worker_metadata()
        return {
            "ok": True,
            "message": "Konfiguration gespeichert.",
            "config_validation_ok": self.config_validation_ok,
            "config_validation_message": self.config_validation_message,
        }

    def export_config_payload(self) -> dict[str, Any]:
        return {
            "yaml_text": self._load_config_text(),
            "config_source": self.config_source,
            "config_updated_at": self.config_updated_at.isoformat() if self.config_updated_at else None,
            "config_validation_ok": self.config_validation_ok,
            "config_validation_message": self.config_validation_message,
        }

    def _refresh_metrics_from_file(self) -> None:
        if not self.paths.metrics_file.exists():
            self.last_run_total_tokens = 0
            self.last_run_cost_eur = 0.0
            self.last_run_bypass_skipped = 0
            self.total_tokens = 0
            self.total_cost_eur = 0.0
            self.total_bypass_skipped = 0
            return
        try:
            payload = json.loads(self.paths.metrics_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        last = payload.get("last_run") or {}
        totals = payload.get("totals") or {}
        self.last_run_total_tokens = self._safe_int(last.get("total_tokens"))
        self.last_run_cost_eur = self._safe_float(last.get("cost_eur"))
        self.last_run_bypass_skipped = self._safe_int(last.get("bypass_skipped"))
        self.total_tokens = self._safe_int(totals.get("total_tokens"))
        self.total_cost_eur = self._safe_float(totals.get("cost_eur"))
        self.total_bypass_skipped = self._safe_int(totals.get("bypass_skipped"))

    def _refresh_failed_state_counts(self) -> None:
        config_payload = self._load_config_mapping()
        failed_docs_name = str(config_payload.get("failed_documents_file", "failed_documents.json")).strip() or "failed_documents.json"
        bypass_name = str(config_payload.get("tag_bypass_file", "tag_bypass_documents.json")).strip() or "tag_bypass_documents.json"
        failed_docs_path = Path(failed_docs_name)
        if not failed_docs_path.is_absolute():
            failed_docs_path = self.paths.data_dir / failed_docs_path
        bypass_path = Path(bypass_name)
        if not bypass_path.is_absolute():
            bypass_path = self.paths.data_dir / bypass_path

        def _read_json(path: Path) -> dict[str, Any]:
            if not path.exists():
                return {}
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                return payload if isinstance(payload, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}

        failed_payload = _read_json(failed_docs_path)
        bypass_payload = _read_json(bypass_path)
        now_ts = datetime.now(UTC).timestamp()
        self.active_quarantine_count = sum(
            1
            for value in failed_payload.values()
            if isinstance(value, (int, float, str)) and self._safe_float(value, 0.0) > now_ts
        )
        self.active_bypass_count = len(bypass_payload)

    def _refresh_resume_state(self) -> None:
        if not self.paths.run_state_file.exists():
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return
        try:
            payload = json.loads(self.paths.run_state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return
        if not isinstance(payload, dict):
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return
        self._apply_runtime_payload({"kind": "paused", **payload})
        self.resume_available = True
        self.pause_reason = str(payload.get("pause_reason") or self.pause_reason or "")
        retry_after_seconds = payload.get("retry_after_seconds")
        retry_after_value = None if retry_after_seconds is None else self._safe_float(retry_after_seconds, 0.0)
        updated_at = self._parse_datetime(payload.get("updated_at"))
        if retry_after_value is not None and updated_at is not None:
            self.auto_resume_at = updated_at + timedelta(seconds=retry_after_value)
        elif retry_after_value is not None:
            self.auto_resume_at = datetime.now(UTC) + timedelta(seconds=retry_after_value)
        else:
            self.auto_resume_at = None

    def _write_zero_metrics(self) -> None:
        payload = {
            "last_run": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_eur": 0.0,
                "bypass_skipped": 0,
                "finished_at": None,
                "model": None,
            },
            "totals": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "cost_eur": 0.0,
                "bypass_skipped": 0,
                "runs": 0,
            },
        }
        self.paths.metrics_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._refresh_metrics_from_file()

    def _delete_runtime_file(self, path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass

    def _clear_restart_state_files(self) -> None:
        self._delete_runtime_file(self.paths.run_state_file)
        self._delete_runtime_file(self.paths.stop_request_file)

    def _build_command(
        self,
        *,
        dry_run: bool,
        all_documents: bool,
        max_documents: int,
        backfill_existing_documents: bool,
        resume_run: bool,
    ) -> list[str]:
        args = list(self.sorter_command)
        if "--config" not in args:
            args.extend(["--config", str(self.paths.config_file)])
        if dry_run and "--dry-run" not in args:
            args.append("--dry-run")
        if all_documents and "--all-documents" not in args:
            args.append("--all-documents")
        if backfill_existing_documents and "--backfill-existing-documents" not in args:
            args.append("--backfill-existing-documents")
        if max_documents > 0 and "--max-documents" not in args:
            args.extend(["--max-documents", str(max_documents)])
        if resume_run and "--resume-run" not in args:
            args.append("--resume-run")
        if "--run-state-file" not in args:
            args.extend(["--run-state-file", str(self.paths.run_state_file)])
        if "--stop-request-file" not in args:
            args.extend(["--stop-request-file", str(self.paths.stop_request_file)])
        if "--entity-review-rules-file" not in args:
            args.extend(["--entity-review-rules-file", str(self.paths.entity_review_rules_file)])
        return args

    def _stream_reader(self, pipe: Any, *, stream_name: str) -> None:
        try:
            for line in iter(pipe.readline, ""):
                if not line:
                    break
                with self.lock:
                    self._append_log_line(stream_name, line)
        finally:
            try:
                pipe.close()
            except Exception:
                pass

    def _persist_force_stop_resume_state(self) -> None:
        if self.paths.run_state_file.exists():
            return
        source_payload = self.latest_runtime_payload if isinstance(self.latest_runtime_payload, dict) else {}
        if not source_payload:
            return
        payload = dict(source_payload)
        payload.pop("kind", None)
        payload["status"] = "paused"
        payload["pause_reason"] = "force_stop"
        payload["retry_after_seconds"] = None
        payload["updated_at"] = datetime.now(UTC).isoformat()
        self.paths.run_state_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _force_kill_process(self) -> None:
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return
        try:
            process.kill()
        except OSError:
            return

    def _cancel_auto_resume(self) -> None:
        if self.auto_resume_timer is not None:
            self.auto_resume_timer.cancel()
        self.auto_resume_timer = None

    def _schedule_auto_resume(self) -> None:
        self._cancel_auto_resume()
        if self.auto_resume_at is None:
            return
        delay = max(0.0, (self.auto_resume_at - datetime.now(UTC)).total_seconds())

        def _resume() -> None:
            try:
                self.resume_run(force=True)
            except Exception as exc:  # noqa: BLE001
                LOGGER.error("Auto-Resume fehlgeschlagen: %s", exc)

        self.auto_resume_timer = threading.Timer(delay, _resume)
        self.auto_resume_timer.daemon = True
        self.auto_resume_timer.start()

    def _finalize_process(self, exit_code: int) -> None:
        with self.lock:
            self.last_exit_code = exit_code
            self._refresh_metrics_from_file()
            self._refresh_failed_state_counts()
            if exit_code == 0:
                self.last_status = "success"
                self.last_message = "script completed successfully"
                self.resume_available = False
                self.pause_reason = ""
                self.auto_resume_at = None
                self.progress_current_document_title = ""
                self.progress_current_document_id = None
                self.progress_current_document_url = ""
            elif exit_code == RUN_PAUSE_EXIT_CODE:
                self._refresh_resume_state()
                if self.pause_reason == "manual_stop":
                    self.last_status = "paused"
                    self.last_message = "run paused manually; resume available"
                else:
                    self.last_status = "waiting_auto_resume"
                    self.last_message = f"run paused due to {self.pause_reason or 'provider backoff'}"
                    if self.auto_resume_at is not None:
                        self.last_message += f" until {self.auto_resume_at.isoformat()}"
                        self._schedule_auto_resume()
            elif self.force_stop_requested:
                self._refresh_resume_state()
                self.last_status = "force_stopped"
                self.auto_resume_at = None
                self.pause_reason = self.pause_reason or "force_stop"
                if self.resume_available:
                    self.last_message = "run stopped immediately; resume available from last saved progress"
                else:
                    self.last_message = "run stopped immediately; no resume state was available yet"
            else:
                self.last_status = "error"
                self.last_message = f"script failed with exit code {exit_code}"
            self.running = False
            self.stop_requested = False
            self.force_stop_requested = False
            self.last_finished = datetime.now(UTC)
            self.process = None

    def _wait_for_process(self, process: subprocess.Popen[str]) -> None:
        exit_code = process.wait()
        self._finalize_process(exit_code)

    def _start_process(
        self,
        *,
        dry_run: bool,
        all_documents: bool,
        max_documents: int,
        backfill_existing_documents: bool,
        resume_run: bool,
    ) -> None:
        self._refresh_config_state()
        if not self.config_validation_ok:
            raise ValueError(self.config_validation_message)
        if resume_run and not self.paths.run_state_file.exists():
            raise ValueError("Resume angefordert, aber es existiert kein pausierter Laufzustand.")
        self._cancel_auto_resume()
        self._clear_restart_state_files() if not resume_run else None
        self._delete_runtime_file(self.paths.stop_request_file)
        self.log_lines = []
        self.last_stdout_tail = ""
        self.last_stderr_tail = ""
        self.last_summary_line = ""
        self.last_cost_line = ""
        self.last_log_export_path = str(self.paths.log_file)
        self.last_started = datetime.now(UTC)
        self.last_status = "running"
        self.last_message = "paused run is resuming" if resume_run else "script is running"
        self.running = True
        self.stop_requested = False
        self.force_stop_requested = False
        self.resume_available = False
        self.pause_reason = ""
        self.auto_resume_at = None
        self.progress_current_document_url = ""
        if not resume_run:
            self.last_completed_document_id = None
            self.last_completed_document_title = ""
            self.last_completed_document_url = ""
            self.last_completed_document_at = None
        args = self._build_command(
            dry_run=dry_run,
            all_documents=all_documents,
            max_documents=max_documents,
            backfill_existing_documents=backfill_existing_documents,
            resume_run=resume_run,
        )
        self.last_command_executed = shlex.join(args)
        process = subprocess.Popen(
            args,
            cwd=str(self.paths.data_dir),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.process = process
        self.stdout_thread = threading.Thread(
            target=self._stream_reader,
            args=(process.stdout,),
            kwargs={"stream_name": "STDOUT"},
            daemon=True,
        )
        self.stderr_thread = threading.Thread(
            target=self._stream_reader,
            args=(process.stderr,),
            kwargs={"stream_name": "STDERR"},
            daemon=True,
        )
        self.wait_thread = threading.Thread(
            target=self._wait_for_process,
            args=(process,),
            daemon=True,
        )
        self.stdout_thread.start()
        self.stderr_thread.start()
        self.wait_thread.start()

    def start_run(
        self,
        *,
        force: bool = False,
        dry_run: bool = False,
        all_documents: bool = False,
        max_documents: int = 0,
        backfill_existing_documents: bool = False,
        resume_run: bool = False,
    ) -> dict[str, Any]:
        with self.lock:
            if self.running and not force:
                self.last_status = "skipped_running"
                self.last_message = "run skipped because another run is active"
                return self.status_payload()
            self._start_process(
                dry_run=dry_run,
                all_documents=all_documents,
                max_documents=max_documents,
                backfill_existing_documents=backfill_existing_documents,
                resume_run=resume_run,
            )
            return self.status_payload()

    def request_stop(self) -> dict[str, Any]:
        with self.lock:
            if not self.running:
                self.last_status = "stop_ignored"
                self.last_message = "no active run to stop"
                return self.status_payload()
            self.paths.stop_request_file.write_text(
                json.dumps(
                    {"requested_at": datetime.now(UTC).isoformat(), "reason": "manual_stop"},
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            self.stop_requested = True
            self.last_status = "stop_requested"
            self.last_message = "stop requested; runner pauses after current document/batch"
            return self.status_payload()

    def force_stop(self) -> dict[str, Any]:
        with self.lock:
            if not self.running or self.process is None or self.process.poll() is not None:
                self.last_status = "stop_ignored"
                self.last_message = "no active run to stop immediately"
                return self.status_payload()
            self._persist_force_stop_resume_state()
            self._delete_runtime_file(self.paths.stop_request_file)
            self.stop_requested = True
            self.force_stop_requested = True
            self.last_status = "stop_now_requested"
            self.last_message = "immediate stop requested; terminating active process"
            try:
                self.process.terminate()
            except OSError:
                pass
            if self.force_stop_timer is not None:
                self.force_stop_timer.cancel()
            self.force_stop_timer = threading.Timer(FORCE_STOP_GRACE_SECONDS, self._force_kill_process)
            self.force_stop_timer.daemon = True
            self.force_stop_timer.start()
            return self.status_payload()

    def resume_run(self, *, force: bool = True) -> dict[str, Any]:
        return self.start_run(force=force, resume_run=True)

    def restart_run(
        self,
        *,
        force: bool = True,
        backfill_existing_documents: bool | None = None,
    ) -> dict[str, Any]:
        with self.lock:
            base_payload: dict[str, Any] = {}
            if self.paths.run_state_file.exists():
                try:
                    base_payload = json.loads(self.paths.run_state_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    base_payload = {}
            if not base_payload:
                base_payload = dict(self.latest_runtime_payload)
            mode = base_payload.get("mode") or {}
            restart_backfill = bool(mode.get("backfill_existing_documents", False))
            if backfill_existing_documents is not None:
                restart_backfill = bool(backfill_existing_documents)
            if self.running:
                self.force_stop()
                deadline = time.time() + 45.0
                while self.running and time.time() < deadline:
                    time.sleep(0.25)
                if self.running:
                    raise RuntimeError("Vorheriger Prozess konnte nicht rechtzeitig beendet werden.")
            self._clear_restart_state_files()
            self.latest_runtime_payload = {}
            self.resume_available = False
            self.pause_reason = ""
            self.auto_resume_at = None
            return self.start_run(force=force, backfill_existing_documents=restart_backfill)

    def reset_metrics(self) -> dict[str, Any]:
        with self.lock:
            self._write_zero_metrics()
            self.last_status = "metrics_reset"
            self.last_message = "token/cost metrics reset"
            return self.status_payload()

    def reset_failed_documents(self) -> dict[str, Any]:
        with self.lock:
            config_payload = self._load_config_mapping()
            file_candidates = {
                str(config_payload.get("failed_documents_file", "failed_documents.json")).strip(),
                str(config_payload.get("failed_patch_cache_file", "failed_patch_cache.json")).strip(),
                str(config_payload.get("tag_bypass_file", "tag_bypass_documents.json")).strip(),
            }
            deleted_count = 0
            for name in sorted(name for name in file_candidates if name):
                path = Path(name)
                if not path.is_absolute():
                    path = self.paths.data_dir / path
                if path.exists():
                    try:
                        path.unlink()
                        deleted_count += 1
                    except OSError as exc:
                        LOGGER.warning("Konnte Failed-Datei nicht löschen (%s): %s", path, exc)
            self._refresh_failed_state_counts()
            self.last_status = "failed_docs_reset"
            self.last_message = f"failed/quarantine documents reset ({deleted_count} files)"
            return self.status_payload()

    @staticmethod
    def _entity_endpoint(entity_type: str) -> str:
        endpoints = {
            "document_type": "/api/document_types/",
            "correspondent": "/api/correspondents/",
        }
        endpoint = endpoints.get(str(entity_type or "").strip())
        if not endpoint:
            raise ValueError(f"Nicht unterstützter Entity-Typ: {entity_type}")
        return endpoint

    @staticmethod
    def _document_field_for_entity(entity_type: str) -> str:
        fields = {
            "document_type": "document_type",
            "correspondent": "correspondent",
        }
        field = fields.get(str(entity_type or "").strip())
        if not field:
            raise ValueError(f"Nicht unterstützter Entity-Typ: {entity_type}")
        return field

    def _load_paperless_client(self) -> PaperlessClient:
        self._refresh_config_state()
        if not self.config_validation_ok:
            raise ValueError(self.config_validation_message)
        config = load_config(str(self.paths.config_file), False)
        return PaperlessClient(config)

    def _list_entity_records(
        self,
        client: PaperlessClient,
        *,
        entity_type: str,
    ) -> list[EntityRecord]:
        """Read Paperless entity metadata without exposing document contents."""

        endpoint = self._entity_endpoint(entity_type)
        records: list[EntityRecord] = []
        next_url = endpoint
        params: dict[str, Any] | None = {"page_size": 100}
        while next_url:
            page = client._request("GET", next_url, params=params)
            params = None
            for item in page.get("results", []) or []:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or item.get("path") or "").strip()
                if not name:
                    continue
                try:
                    entity_id = int(item.get("id"))
                except (TypeError, ValueError):
                    continue
                records.append(
                    EntityRecord(
                        entity_type=entity_type,
                        entity_id=entity_id,
                        name=name,
                        document_count=self._safe_int(
                            item.get("document_count")
                            or item.get("documents_count")
                            or item.get("count")
                        ),
                    )
                )
            next_url = str(page.get("next") or "")
        return records

    @staticmethod
    def _entity_payload(records: list[EntityRecord]) -> list[dict[str, Any]]:
        return [
            {
                "id": record.entity_id,
                "name": record.name,
                "document_count": record.document_count,
            }
            for record in sorted(records, key=lambda item: item.name.casefold())
        ]

    def _load_review_rules(self) -> list[Any]:
        store = load_review_store(self.paths.entity_review_rules_file)
        return review_rules_from_store(store)

    def entity_review_rules_payload(self) -> dict[str, Any]:
        rules = self._load_review_rules()
        return {
            "ok": True,
            "rules_file": str(self.paths.entity_review_rules_file),
            "rules": [rule.to_payload() for rule in rules],
            "ai_context": build_ai_prompt_context(rules),
        }

    def entity_review_payload(self, *, threshold: float = 0.84) -> dict[str, Any]:
        """Build the safe JSON payload behind `/review`.

        Why this endpoint does live API calls:
        - Review candidates must reflect current Paperless metadata.
        - We only load entity lists, not OCR content.
        - The response intentionally contains no Paperless token or config text.
        """

        client = self._load_paperless_client()
        document_type_records = self._list_entity_records(client, entity_type="document_type")
        correspondent_records = self._list_entity_records(client, entity_type="correspondent")
        rules = self._load_review_rules()
        candidates = []
        for records in (document_type_records, correspondent_records):
            candidates.extend(
                find_duplicate_candidates(
                    records,
                    min_similarity=threshold,
                    max_candidates=250,
                )
            )
        candidates = filter_candidates_by_rules(candidates, rules)
        candidates.sort(
            key=lambda candidate: (
                -candidate.similarity,
                candidate.entity_type,
                candidate.canonical_name.casefold(),
                candidate.alias_name.casefold(),
            )
        )

        return {
            "ok": True,
            "rules_file": str(self.paths.entity_review_rules_file),
            "threshold": threshold,
            "entities": {
                "document_type": self._entity_payload(document_type_records),
                "correspondent": self._entity_payload(correspondent_records),
            },
            "candidates": [candidate.to_payload() for candidate in candidates],
            "rules": [rule.to_payload() for rule in rules],
            "ai_context": build_ai_prompt_context(rules),
        }

    def save_entity_review_rule(self, payload: dict[str, Any]) -> dict[str, Any]:
        rule = upsert_review_rule(
            self.paths.entity_review_rules_file,
            entity_type=str(payload.get("entity_type") or ""),
            alias_id=int(payload.get("alias_id")),
            alias_name=str(payload.get("alias_name") or ""),
            canonical_id=int(payload.get("canonical_id")),
            canonical_name=str(payload.get("canonical_name") or ""),
            action=str(payload.get("action") or "prefer"),
            context=str(payload.get("context") or ""),
        )
        action_label = "Kein Duplikat gespeichert." if rule.action == "ignore" else "KI-Regel gespeichert."
        return {
            "ok": True,
            "message": action_label,
            "rule": rule.to_payload(),
            "rules_file": str(self.paths.entity_review_rules_file),
            "ai_context": build_ai_prompt_context(self._load_review_rules()),
        }

    @staticmethod
    def _document_matches_entity(document: dict[str, Any], field: str, entity_id: int) -> bool:
        try:
            return int(document.get(field) or 0) == int(entity_id)
        except (TypeError, ValueError):
            return False

    def _document_ids_for_entity(
        self,
        client: PaperlessClient,
        *,
        entity_type: str,
        entity_id: int,
    ) -> list[int]:
        """Find document ids assigned to one entity with a safe fallback scan."""

        field = self._document_field_for_entity(entity_type)
        safe_entity_id = int(entity_id)
        filter_candidates = (
            {f"{field}__id": safe_entity_id},
            {field: safe_entity_id},
        )

        for params in filter_candidates:
            document_ids: list[int] = []
            filter_looked_supported = True
            try:
                for document in client.iter_documents(None, extra_params=params):
                    if self._document_matches_entity(document, field, safe_entity_id):
                        try:
                            document_ids.append(int(document.get("id")))
                        except (TypeError, ValueError):
                            continue
                    else:
                        filter_looked_supported = False
                        break
            except PaperlessApiError:
                continue
            if filter_looked_supported:
                return sorted(set(document_ids))

        document_ids = []
        for document in client.iter_documents(None):
            if self._document_matches_entity(document, field, safe_entity_id):
                try:
                    document_ids.append(int(document.get("id")))
                except (TypeError, ValueError):
                    continue
        return sorted(set(document_ids))

    def merge_review_entities(self, payload: dict[str, Any]) -> dict[str, Any]:
        entity_type = str(payload.get("entity_type") or "").strip()
        alias_id = int(payload.get("alias_id"))
        canonical_id = int(payload.get("canonical_id"))
        if alias_id == canonical_id:
            raise ValueError("Alias und Ziel dürfen nicht identisch sein.")
        alias_name = str(payload.get("alias_name") or "")
        canonical_name = str(payload.get("canonical_name") or "")
        dry_run = bool(payload.get("dry_run", True))
        delete_alias = bool(payload.get("delete_alias", True))
        field = self._document_field_for_entity(entity_type)
        endpoint = self._entity_endpoint(entity_type)
        client = self._load_paperless_client()
        document_ids = self._document_ids_for_entity(
            client,
            entity_type=entity_type,
            entity_id=alias_id,
        )

        if dry_run:
            return {
                "ok": True,
                "dry_run": True,
                "message": "Dry-Run abgeschlossen. Es wurden keine Paperless-Daten geändert.",
                "affected_count": len(document_ids),
                "affected_document_ids_preview": document_ids[:25],
                "delete_alias": delete_alias,
            }

        updated_count = 0
        for document_id in document_ids:
            client.update_document(document_id, {field: canonical_id})
            updated_count += 1

        deleted_alias = False
        delete_warning = ""
        if delete_alias:
            try:
                client._request("DELETE", f"{endpoint}{alias_id}/", retries=2)
                deleted_alias = True
            except PaperlessApiError as exc:
                delete_warning = (
                    "Dokumente wurden umgehängt, aber der alte Paperless-Eintrag "
                    f"konnte nicht gelöscht werden: {exc}"
                )

        rule = upsert_review_rule(
            self.paths.entity_review_rules_file,
            entity_type=entity_type,
            alias_id=alias_id,
            alias_name=alias_name,
            canonical_id=canonical_id,
            canonical_name=canonical_name,
            action="merge",
            context=str(payload.get("context") or ""),
        )
        return {
            "ok": True,
            "dry_run": False,
            "message": delete_warning or "Merge angewendet und KI-Regel gespeichert.",
            "affected_count": len(document_ids),
            "updated_count": updated_count,
            "deleted_alias": deleted_alias,
            "affected_document_ids_preview": document_ids[:25],
            "rule": rule.to_payload(),
            "ai_context": build_ai_prompt_context(self._load_review_rules()),
        }

    def status_payload(self) -> dict[str, Any]:
        return {
            "status": self.last_status,
            "message": self.last_message,
            "running": self.running,
            "last_exit_code": self.last_exit_code,
            "last_started": self.last_started.isoformat() if self.last_started else None,
            "last_finished": self.last_finished.isoformat() if self.last_finished else None,
            "cooldown_until": None,
            "command": shlex.join(self.sorter_command),
            "last_command_executed": self.last_command_executed,
            "workdir": str(self.paths.data_dir),
            "config_file": str(self.paths.config_file),
            "default_dry_run": False,
            "default_all_documents": False,
            "default_max_documents": 0,
            "managed_config_enabled": True,
            "managed_config_yaml_chars": len(self._load_config_text()),
            "input_cost_per_1k_tokens_eur": None,
            "output_cost_per_1k_tokens_eur": None,
            "metrics_file": str(self.paths.metrics_file),
            "stdout_tail": self.last_stdout_tail,
            "stderr_tail": self.last_stderr_tail,
            "summary_line": self.last_summary_line,
            "cost_line": self.last_cost_line,
            "log_text": "\n".join(self.log_lines),
            "last_scanned": self.last_scanned,
            "last_updated": self.last_updated,
            "last_skipped": self.last_skipped,
            "last_failed": self.last_failed,
            "last_run_total_tokens": self.last_run_total_tokens,
            "last_run_cost_eur": round(self.last_run_cost_eur, 6),
            "last_run_bypass_skipped": self.last_run_bypass_skipped,
            "total_tokens": self.total_tokens,
            "total_cost_eur": round(self.total_cost_eur, 6),
            "total_bypass_skipped": self.total_bypass_skipped,
            "last_log_export_path": str(self.paths.log_file),
            "last_log_export_url": self.last_log_export_url,
            "active_quarantine_count": self.active_quarantine_count,
            "active_bypass_count": self.active_bypass_count,
            "run_state_file": str(self.paths.run_state_file),
            "stop_request_file": str(self.paths.stop_request_file),
            "resume_available": self.resume_available,
            "pause_reason": self.pause_reason or None,
            "auto_resume_at": self.auto_resume_at.isoformat() if self.auto_resume_at else None,
            "stop_requested": self.stop_requested,
            "force_stop_requested": self.force_stop_requested,
            "progress_total_documents": self.progress_total_documents,
            "progress_completed_documents": self.progress_completed_documents,
            "progress_percent": round(self.progress_percent, 2),
            "progress_scanned": self.progress_scanned,
            "progress_updated": self.progress_updated,
            "progress_skipped": self.progress_skipped,
            "progress_failed": self.progress_failed,
            "progress_bypassed": self.progress_bypassed,
            "progress_bypass_skipped": self.progress_bypass_skipped,
            "progress_prefiltered_ki_tagged": self.progress_prefiltered_ki_tagged,
            "progress_budget_used": self.progress_budget_used,
            "progress_pending_documents": self.progress_pending_documents,
            "progress_current_document_id": self.progress_current_document_id,
            "progress_current_document_title": self.progress_current_document_title,
            "progress_current_document_url": self.progress_current_document_url,
            "last_completed_document_id": self.last_completed_document_id,
            "last_completed_document_title": self.last_completed_document_title,
            "last_completed_document_url": self.last_completed_document_url,
            "last_completed_document_at": self.last_completed_document_at.isoformat() if self.last_completed_document_at else None,
            "progress_last_event_at": self.progress_last_event_at.isoformat() if self.progress_last_event_at else None,
            "paperless_base_url": self.paperless_base_url,
            "config_source": self.config_source,
            "config_updated_at": self.config_updated_at.isoformat() if self.config_updated_at else None,
            "config_validation_ok": self.config_validation_ok,
            "config_validation_message": self.config_validation_message,
            "last_config_sync_status": "worker_local",
            "last_config_sync_at": self.config_updated_at.isoformat() if self.config_updated_at else None,
        }

    def heimdall_payload(self) -> dict[str, Any]:
        """Build a small, safe dashboard payload for broker/Heimdall probes.

        Why this separate payload exists:
        - `/api/status` intentionally contains rich operational state for Home
          Assistant, including paths and log tails.
        - Heimdall only needs a compact health summary that can be fetched
          without a browser session or API token.
        - We deliberately do not include secrets, raw logs, stack traces,
          request bodies, document names, Paperless URLs, or config text.
        """

        with self.lock:
            worker_state = "running" if self.running else "online"
            config_state = "valid" if self.config_validation_ok else "needs_config"
            last_run_at = self.last_finished or self.last_started
            last_run_value = last_run_at.isoformat() if last_run_at else "never"
            details = [
                f"Config: {config_state}",
                f"Last run: {last_run_value}",
            ]
            if self.resume_available:
                details.append("Resume available")

            return {
                "status": "ok",
                "summary": f"Paperless KIplus worker {worker_state}; config {config_state}",
                "stats": [
                    {"label": "Status", "value": worker_state},
                    {"label": "Running", "value": "yes" if self.running else "no"},
                    {"label": "Failed", "value": str(self.last_failed)},
                ],
                "details": details,
            }


class WorkerRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler for the standalone worker API and web UI."""

    manager: WorkerManager

    server_version = "PaperlessKIplusWorker/1.0"

    def _json_response(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _text_response(
        self,
        text: str,
        *,
        content_type: str,
        status: HTTPStatus = HTTPStatus.OK,
    ) -> None:
        encoded = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _read_json_body(self) -> dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0") or 0)
        if content_length <= 0:
            return {}
        raw = self.rfile.read(content_length).decode("utf-8")
        if not raw.strip():
            return {}
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("JSON-Body muss ein Objekt sein.")
        return payload

    def _is_authorized(self) -> bool:
        if not self.manager.auth_token:
            return True
        header = str(self.headers.get("Authorization") or "").strip()
        return header == f"Bearer {self.manager.auth_token}"

    def _require_auth(self) -> bool:
        if self._is_authorized():
            return True
        self._json_response(
            {"ok": False, "message": "Unauthorized"},
            status=HTTPStatus.UNAUTHORIZED,
        )
        return False

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/":
                self._text_response(WORKER_WEB_UI_HTML, content_type="text/html; charset=utf-8")
                return
            if path == "/review":
                self._text_response(ENTITY_REVIEW_HTML, content_type="text/html; charset=utf-8")
                return
            if not path.startswith("/api/"):
                self._json_response({"ok": False, "message": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if path == "/api/heimdall/v1":
                self._json_response(self.manager.heimdall_payload())
                return
            if not self._require_auth():
                return
            if path == "/api/status":
                self._json_response(self.manager.status_payload())
                return
            if path == "/api/logs":
                self._json_response(
                    {
                        "log_text": "\n".join(self.manager.log_lines),
                        "stdout_tail": self.manager.last_stdout_tail,
                        "stderr_tail": self.manager.last_stderr_tail,
                        "summary_line": self.manager.last_summary_line,
                        "cost_line": self.manager.last_cost_line,
                    }
                )
                return
            if path == "/api/logs/download":
                self._text_response(
                    "\n".join(self.manager.log_lines) or "[Kein Log vorhanden]",
                    content_type="text/plain; charset=utf-8",
                )
                return
            if path == "/api/config/export":
                payload = self.manager.export_config_payload()
                self._json_response(payload)
                return
            if path == "/api/config/download":
                self._text_response(
                    self.manager._load_config_text(),
                    content_type="application/x-yaml; charset=utf-8",
                )
                return
            if path == "/api/review/entities":
                query = parse_qs(parsed.query)
                threshold_raw = (query.get("threshold") or ["0.84"])[0]
                try:
                    threshold = float(threshold_raw)
                except (TypeError, ValueError):
                    threshold = 0.84
                self._json_response(self.manager.entity_review_payload(threshold=threshold))
                return
            if path == "/api/review/rules":
                self._json_response(self.manager.entity_review_rules_payload())
                return
            self._json_response({"ok": False, "message": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("GET %s fehlgeschlagen: %s", path, exc)
            self._json_response({"ok": False, "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if not path.startswith("/api/"):
                self._json_response({"ok": False, "message": "Not found"}, status=HTTPStatus.NOT_FOUND)
                return
            if not self._require_auth():
                return
            payload = self._read_json_body()
            if path == "/api/run":
                status_payload = self.manager.start_run(
                    force=bool(payload.get("force", False)),
                    dry_run=bool(payload.get("dry_run", False)),
                    all_documents=bool(payload.get("all_documents", False)),
                    max_documents=int(payload.get("max_documents", 0) or 0),
                    backfill_existing_documents=bool(payload.get("backfill_existing_documents", False)),
                )
                self._json_response({"ok": True, "message": "Lauf gestartet.", "status": status_payload})
                return
            if path == "/api/resume":
                status_payload = self.manager.resume_run(force=bool(payload.get("force", True)))
                self._json_response({"ok": True, "message": "Resume ausgelöst.", "status": status_payload})
                return
            if path == "/api/restart":
                status_payload = self.manager.restart_run(
                    force=bool(payload.get("force", True)),
                    backfill_existing_documents=payload.get("backfill_existing_documents"),
                )
                self._json_response({"ok": True, "message": "Neustart ausgelöst.", "status": status_payload})
                return
            if path == "/api/stop":
                status_payload = self.manager.request_stop()
                self._json_response({"ok": True, "message": "Pausieren angefordert.", "status": status_payload})
                return
            if path == "/api/stop_now":
                status_payload = self.manager.force_stop()
                self._json_response({"ok": True, "message": "Sofort-Stopp angefordert.", "status": status_payload})
                return
            if path == "/api/metrics/reset":
                status_payload = self.manager.reset_metrics()
                self._json_response({"ok": True, "message": "Metriken zurückgesetzt.", "status": status_payload})
                return
            if path == "/api/failed/reset":
                status_payload = self.manager.reset_failed_documents()
                self._json_response({"ok": True, "message": "Fehlgeschlagene Dokumente zurückgesetzt.", "status": status_payload})
                return
            if path == "/api/config/import":
                result = self.manager.import_config_yaml(
                    str(payload.get("yaml_text") or ""),
                    source=str(payload.get("source") or "api"),
                )
                self._json_response({"ok": True, **result, "status": self.manager.status_payload()})
                return
            if path == "/api/review/rules":
                result = self.manager.save_entity_review_rule(payload)
                self._json_response(result)
                return
            if path == "/api/review/merge":
                result = self.manager.merge_review_entities(payload)
                self._json_response(result)
                return
            self._json_response({"ok": False, "message": "Not found"}, status=HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._json_response({"ok": False, "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # noqa: BLE001
            LOGGER.exception("POST %s fehlgeschlagen: %s", path, exc)
            self._json_response({"ok": False, "message": str(exc)}, status=HTTPStatus.INTERNAL_SERVER_ERROR)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        LOGGER.info("worker_http | %s", format % args)


class WorkerHttpServer(ThreadingHTTPServer):
    """Threading HTTP server carrying the shared worker manager."""

    def __init__(self, server_address: tuple[str, int], manager: WorkerManager) -> None:
        self.manager = manager
        WorkerRequestHandler.manager = manager
        super().__init__(server_address, WorkerRequestHandler)


def parse_args() -> argparse.Namespace:
    """Parse CLI args for the standalone worker."""

    env_data_dir = os.getenv("PAPERLESS_KIPLUS_DATA_DIR", "/data")
    env_host = os.getenv("PAPERLESS_KIPLUS_HOST", DEFAULT_HOST)
    env_port = int(os.getenv("PAPERLESS_KIPLUS_PORT", str(DEFAULT_PORT)))
    env_token = os.getenv("PAPERLESS_KIPLUS_TOKEN", "")
    default_sorter = f"{sys.executable} {Path(__file__).with_name('paperless_ai_sorter.py')}"
    env_command = os.getenv("PAPERLESS_KIPLUS_SORTER_COMMAND", default_sorter)

    parser = argparse.ArgumentParser(description="Paperless KIplus standalone worker")
    parser.add_argument("--data-dir", default=env_data_dir, help="Persistente Worker-Daten")
    parser.add_argument("--host", default=env_host, help="Bind-Adresse des Webservers")
    parser.add_argument("--port", type=int, default=env_port, help="Port des Webservers")
    parser.add_argument("--auth-token", default=env_token, help="Optionaler Bearer-Token für API-Aufrufe")
    parser.add_argument("--sorter-command", default=env_command, help="CLI-Befehl für paperless_ai_sorter.py")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    manager = WorkerManager(
        data_dir=Path(args.data_dir),
        sorter_command=shlex.split(args.sorter_command),
        auth_token=str(args.auth_token or ""),
    )
    server = WorkerHttpServer((args.host, int(args.port)), manager)
    LOGGER.info(
        "Starte Paperless KIplus Worker | host=%s port=%s data_dir=%s",
        args.host,
        args.port,
        manager.paths.data_dir,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOGGER.info("Worker wird beendet ...")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
