#!/usr/bin/env python3
"""Local, source-linked internship discovery and application pipeline."""

from __future__ import annotations

import argparse
import csv
import html
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_core import paths
from pipeline_core.artifacts import write_artifact
from pipeline_core.clock import now_iso, parse_datetime
from pipeline_core.config import load_env_file, load_json, load_profile, load_sources
from pipeline_core.discovery import DISCOVERY_VENDOR_ORDER, report_discovery
from pipeline_core.fetch import fetch_all
from pipeline_core.identity import sort_key
from pipeline_core.importers import enrich_descriptions, import_discovered, import_emails, import_manual
from pipeline_core.liveness import check_liveness
from pipeline_core.read_model import RANKED_VIEW_PER_COMPANY
from pipeline_core.regions import region_label
from pipeline_core.retention import purge_expired
from pipeline_core.scoring import score_all
from pipeline_core.store import connect, VALID_STATUSES


# `run` exits with this (EX_TEMPFAIL) when a source could not be reached at all,
# so the scheduled wrapper knows the fetch is incomplete and retries it later
# instead of recording the day as done.
EXIT_TEMPFAIL = 75


def stale_label(last_seen_at: str, stale_after_days: int) -> str:
    last_seen = parse_datetime(last_seen_at)
    if not last_seen:
        return "unknown"
    age = (datetime.now(timezone.utc) - last_seen.astimezone(timezone.utc)).days
    return f"{age}d since checked" + (" — STALE" if age > stale_after_days else "")


def display_reasons(reasons: list[str], limit: int = 5) -> list[str]:
    return [reason for reason in reasons if reason != "35 base"][:limit]


def cap_per_company(
    ranked: list[sqlite3.Row], limit: int, per_company: int = RANKED_VIEW_PER_COMPANY
) -> tuple[list[sqlite3.Row], dict[str, int]]:
    """The first `limit` of `ranked`, keeping each employer's top `per_company`.

    Also returns, per employer that reached the cap, how many of its postings
    were left out, keyed by `sort_key`. An employer cut short by `limit`
    rather than the cap is not in it: those postings did not rank high enough,
    which the shortlist's length already says.
    """

    totals: dict[str, int] = {}
    for job in ranked:
        key = sort_key(job["company"])
        totals[key] = totals.get(key, 0) + 1
    shown: dict[str, int] = {}
    kept: list[sqlite3.Row] = []
    for job in ranked:
        if len(kept) >= limit:
            break
        key = sort_key(job["company"])
        if shown.get(key, 0) >= per_company:
            continue
        shown[key] = shown.get(key, 0) + 1
        kept.append(job)
    hidden = {
        key: totals[key] - count
        for key, count in shown.items()
        if count >= per_company and totals[key] > count
    }
    return kept, hidden


def report(conn: sqlite3.Connection, sources_config: dict[str, Any], limit: int) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    ranked = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL AND status NOT IN ('rejected', 'withdrawn')
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        """
    ).fetchall()
    # The CSV is the uncapped export; the Markdown shortlist is read top to
    # bottom, so one employer may fill at most its per-employer share of it.
    jobs = ranked[:limit]
    shortlist, hidden = cap_per_company(ranked, limit)
    generated = now_iso()
    lines = [
        "# Opportunity shortlist",
        "",
        f"Generated `{generated}` from source data. Scores are ranking hints, not facts.",
        "",
        "## Top matches",
        "",
    ]
    if not shortlist:
        lines.append("No active postings yet. Run `python3 pipeline.py run` or import login-only results.")
    shown: dict[str, int] = {}
    for index, job in enumerate(shortlist, start=1):
        reasons = json.loads(job["score_explanation"])
        reasons_for_display = display_reasons(reasons)
        lines.extend(
            [
                f"### {index}. [{job['title']}]({job['url']}) — {job['company']} ({job['score']}/100)",
                "",
                f"- Location: {job['location'] or 'not provided'}",
                f"- Type: {job['role_type']} · Status: {job['status']}",
                f"- Source: {job['source_name']} · Freshness: {stale_label(job['last_seen_at'], stale_days)}",
                f"- Why ranked here: {'; '.join(reasons_for_display) or 'base score only'}",
                f"- Pipeline ID: `{job['id']}`",
                "",
            ]
        )
        key = sort_key(job["company"])
        shown[key] = shown.get(key, 0) + 1
        if key in hidden and shown[key] == RANKED_VIEW_PER_COMPANY:
            # Said at the employer's last listed posting, never left silent.
            lines.extend(
                [
                    f"*+{hidden[key]} more from {job['company']}, not listed here: the shortlist "
                    f"shows each employer's top {RANKED_VIEW_PER_COMPANY}. The web dashboard's "
                    f"\"+{hidden[key]} more\" button on this employer lists them all.*",
                    "",
                ]
            )
    lines.extend(["## Manual check queue", ""])
    for item in sources_config.get("manual_check_sources", []):
        cadence = item.get("cadence", "weekly")
        lines.append(f"- [{item['name']}]({item['url']}) — {cadence}; {item.get('note', '')}".rstrip())
    lines.extend(
        [
            "",
            "## Next actions",
            "",
            "1. Open the top roles and verify eligibility/deadline at the source.",
            "2. Mark a role: `python3 pipeline.py update <ID> shortlisted`.",
            "3. Add login-only finds to `data/manual_jobs.csv`, then rerun the pipeline.",
            "4. Never treat an aggregator copy as authoritative; apply on the employer or university page.",
            "",
        ]
    )
    paths.OUTPUT_MD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_MD.write_text("\n".join(lines), encoding="utf-8")

    with paths.OUTPUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["id", "score", "status", "company", "title", "location", "role_type", "url", "source", "last_seen_at"]
        )
        for job in jobs:
            writer.writerow(
                [
                    job["id"],
                    job["score"],
                    job["status"],
                    job["company"],
                    job["title"],
                    job["location"],
                    job["role_type"],
                    job["url"],
                    job["source_name"],
                    job["last_seen_at"],
                ]
            )
    print(
        f"Wrote {len(shortlist)} matches to {paths.OUTPUT_MD.relative_to(paths.ROOT)} "
        f"(top {RANKED_VIEW_PER_COMPANY} per employer) and {len(jobs)} to {paths.OUTPUT_CSV.relative_to(paths.ROOT)}"
    )
    return len(jobs)


_DASHBOARD_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Internship Opportunity Dashboard</title>
<style>
  :root {
    color-scheme: light;
    --surface: #fcfcfb;
    --plane: #f9f9f7;
    --ink: #0b0b0b;
    --ink-2: #52514e;
    --ink-muted: #898781;
    --hairline: #e1e0d9;
    --rule: #c3c2b7;
    --ring: rgba(11, 11, 11, 0.10);
    --wash: rgba(11, 11, 11, 0.03);
    /* Categorical slots 1-3 of the validated palette; region is identity, not
       magnitude, so each region keeps its hue no matter how the table is
       filtered or sorted. Every chip also carries its text label, which is the
       relief for aqua sitting under 3:1 on the light surface. */
    --region-1: #2a78d6;
    --region-2: #eb6834;
    --region-3: #1baf7a;
    --region-0: #898781;
  }
  @media (prefers-color-scheme: dark) {
    :root:where(:not([data-theme="light"])) {
      color-scheme: dark;
      --surface: #1a1a19;
      --plane: #0d0d0d;
      --ink: #ffffff;
      --ink-2: #c3c2b7;
      --ink-muted: #898781;
      --hairline: #2c2c2a;
      --rule: #383835;
      --ring: rgba(255, 255, 255, 0.10);
      --wash: rgba(255, 255, 255, 0.04);
      --region-1: #3987e5;
      --region-2: #d95926;
      --region-3: #199e70;
      --region-0: #898781;
    }
  }
  * { box-sizing: border-box; }
  body {
    font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    margin: 0; padding: 2.5rem 1.5rem 4rem; max-width: 1180px; margin-inline: auto;
    background: var(--plane); color: var(--ink);
    -webkit-font-smoothing: antialiased;
  }
  h1 { font-size: 1.5rem; font-weight: 620; letter-spacing: -0.015em; margin: 0 0 0.3rem; }
  .meta { color: var(--ink-muted); font-size: 0.82rem; margin: 0 0 1.75rem; }

  /* Stat tiles: the headline numbers, proportional figures per the type rule. */
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 0.75rem; margin-bottom: 1.75rem; }
  .tile { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; padding: 0.9rem 1rem; }
  .tile-value { font-size: 1.75rem; font-weight: 600; letter-spacing: -0.02em; line-height: 1.1; }
  .tile-label { font-size: 0.75rem; color: var(--ink-muted); margin-top: 0.2rem; }

  .controls { display: flex; flex-wrap: wrap; gap: 0.5rem; margin-bottom: 0.9rem; }
  .controls select, .controls input {
    padding: 0.45rem 0.6rem; font: inherit; font-size: 0.85rem;
    background: var(--surface); color: var(--ink);
    border: 1px solid var(--ring); border-radius: 8px;
  }
  .controls input { flex: 1 1 220px; min-width: 180px; }
  .controls select:focus-visible, .controls input:focus-visible { outline: 2px solid var(--region-1); outline-offset: 1px; }
  .count { font-size: 0.8rem; color: var(--ink-muted); margin: 0 0 0.75rem; }

  .table-wrap { background: var(--surface); border: 1px solid var(--ring); border-radius: 12px; overflow-x: auto; }
  table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
  thead th {
    text-align: left; font-size: 0.72rem; font-weight: 600; text-transform: uppercase;
    letter-spacing: 0.06em; color: var(--ink-muted);
    padding: 0.7rem 0.9rem; border-bottom: 1px solid var(--rule); white-space: nowrap;
  }
  tbody td { padding: 0.8rem 0.9rem; border-bottom: 1px solid var(--hairline); vertical-align: top; }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: var(--wash); }
  tr.is-new td:first-child { box-shadow: inset 2px 0 0 var(--ink); }
  tr.is-read { opacity: 0.55; }

  .job-title { color: var(--ink); font-weight: 550; text-decoration: none; }
  .job-title:hover { text-decoration: underline; }
  .company { color: var(--ink-2); font-size: 0.82rem; margin-top: 0.15rem; }
  .badge {
    display: inline-block; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.05em;
    padding: 0.1rem 0.35rem; border-radius: 4px; margin-left: 0.45rem; vertical-align: 1px;
    background: var(--ink); color: var(--surface);
  }

  /* Score: length carries magnitude, so the meter stays neutral — a hue here
     would read as a fourth region. */
  .score-value { font-weight: 600; font-variant-numeric: tabular-nums; }
  .meter { width: 64px; height: 3px; border-radius: 2px; background: var(--hairline); margin-top: 0.4rem; }
  .meter-fill { height: 100%; border-radius: 2px; background: var(--ink-2); }

  .chip { display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.75rem; color: var(--ink-2); margin-top: 0.25rem; }
  .chip::before { content: ""; width: 7px; height: 7px; border-radius: 50%; background: var(--chip-color, var(--region-0)); flex: none; }
  .region-1 { --chip-color: var(--region-1); }
  .region-2 { --chip-color: var(--region-2); }
  .region-3 { --chip-color: var(--region-3); }
  .region-0 { --chip-color: var(--region-0); }
  .loc { color: var(--ink); }
  .muted { color: var(--ink-muted); }

  details { margin-top: 0.4rem; }
  details summary { cursor: pointer; font-size: 0.76rem; color: var(--ink-muted); }
  details p { font-size: 0.78rem; color: var(--ink-2); margin: 0.35rem 0 0; line-height: 1.45; }
</style>
</head>
<body>
<h1>Internship Opportunity Dashboard</h1>
<p class="meta">Generated GENERATED_AT_PLACEHOLDER &middot; Scores are ranking hints, not facts.</p>
<div class="tiles">
  <div class="tile"><div class="tile-value" id="tile-total">0</div><div class="tile-label">Opportunities shown</div></div>
  <div class="tile"><div class="tile-value" id="tile-new">0</div><div class="tile-label">New since last visit</div></div>
  <div class="tile"><div class="tile-value" id="tile-region">0</div><div class="tile-label">In target regions</div></div>
  <div class="tile"><div class="tile-value" id="tile-top">0</div><div class="tile-label">Top score</div></div>
</div>
<div class="controls">
  <input type="text" id="search" placeholder="Search title or company">
  <select id="sort">
    <option value="score-desc">Score (high to low)</option>
    <option value="score-asc">Score (low to high)</option>
    <option value="company">Company (A-Z)</option>
    <option value="posted">Most recently posted</option>
    <option value="discovered">Most recently discovered</option>
  </select>
  <select id="filter-region"><option value="">All regions</option></select>
  <select id="filter-role"><option value="">All role types</option></select>
  <select id="filter-status"><option value="">All statuses</option></select>
  <select id="filter-source"><option value="">All sources</option></select>
</div>
<p class="count" id="count"></p>
<div class="table-wrap">
<table>
  <thead>
    <tr><th>Title / Company</th><th>Score</th><th>Location</th><th>Type</th><th>Status</th><th>Source</th></tr>
  </thead>
  <tbody id="rows"></tbody>
</table>
</div>
<script id="job-data" type="application/json">JOB_DATA_PLACEHOLDER</script>
<script>
(function () {
  var LAST_OPENED_KEY = 'internship_dashboard_last_opened_at';
  var READ_PREFIX = 'internship_dashboard_read:';
  var jobs = JSON.parse(document.getElementById('job-data').textContent);

  // Some browsers (and strict configurations, e.g. Safari privacy settings)
  // treat file:// pages as an opaque origin and throw on any localStorage
  // access rather than just being absent. Fall back to an in-memory store
  // so the dashboard still renders — new/read just won't persist there.
  var memoryStore = {};
  var storageAvailable = true;
  try {
    var probeKey = '__internship_dashboard_probe__';
    window.localStorage.setItem(probeKey, '1');
    window.localStorage.removeItem(probeKey);
  } catch (err) {
    storageAvailable = false;
  }
  var safeStorage = {
    getItem: function (key) {
      if (storageAvailable) {
        try { return window.localStorage.getItem(key); } catch (err) { /* fall through */ }
      }
      return Object.prototype.hasOwnProperty.call(memoryStore, key) ? memoryStore[key] : null;
    },
    setItem: function (key, value) {
      if (storageAvailable) {
        try { window.localStorage.setItem(key, value); return; } catch (err) { /* fall through */ }
      }
      memoryStore[key] = value;
    },
  };
  if (!storageAvailable) {
    console.warn('Dashboard: localStorage unavailable on this origin; new/read status will not persist across reloads.');
  }

  var storedLastOpened = safeStorage.getItem(LAST_OPENED_KEY);
  var isFirstEverOpen = storedLastOpened === null;
  var lastOpenedAt = storedLastOpened ? new Date(storedLastOpened) : null;

  jobs.forEach(function (job) {
    job.isNew = !isFirstEverOpen && lastOpenedAt !== null && !!job.first_seen_at
      && new Date(job.first_seen_at) > lastOpenedAt;
    job.isRead = safeStorage.getItem(READ_PREFIX + job.id) === '1';
  });
  safeStorage.setItem(LAST_OPENED_KEY, new Date().toISOString());

  var roleSelect = document.getElementById('filter-role');
  var statusSelect = document.getElementById('filter-status');
  var sourceSelect = document.getElementById('filter-source');
  var regionSelect = document.getElementById('filter-region');

  function uniqueSorted(values) {
    return Array.from(new Set(values.filter(Boolean))).sort();
  }
  function populate(select, values) {
    uniqueSorted(values).forEach(function (value) {
      var opt = document.createElement('option');
      opt.value = value;
      opt.textContent = value;
      select.appendChild(opt);
    });
  }
  populate(roleSelect, jobs.map(function (j) { return j.role_type; }));
  populate(statusSelect, jobs.map(function (j) { return j.status; }));
  populate(sourceSelect, jobs.map(function (j) { return j.source_name; }));
  populate(regionSelect, jobs.map(function (j) { return j.region; }));

  // Colour follows the region, never its rank: the slot is fixed once from the
  // full job set, so filtering the table never repaints the survivors. Past the
  // three validated slots regions fold into the muted slot rather than cycling
  // hues, which would put two indistinguishable colours on screen.
  var SLOTS = ['region-1', 'region-2', 'region-3'];
  var RESERVED_REGIONS = { 'Other': 1, 'Unknown': 1, 'Remote': 1 };
  var REGION_CLASS = { 'Other': 'region-0', 'Unknown': 'region-0' };
  (function assignRegionSlots() {
    var targets = uniqueSorted(jobs.map(function (j) { return j.region; }))
      .filter(function (name) { return !RESERVED_REGIONS[name]; });
    targets.forEach(function (name, i) {
      REGION_CLASS[name] = i < SLOTS.length ? SLOTS[i] : 'region-0';
    });
    REGION_CLASS.Remote = targets.length < SLOTS.length ? SLOTS[targets.length] : 'region-0';
  })();

  // Meter length is relative to the best score on the board, so the bars stay
  // comparable to each other rather than to an arbitrary ceiling.
  var meterMax = jobs.reduce(function (max, job) { return Math.max(max, job.score); }, 1);

  var rowsEl = document.getElementById('rows');
  var countEl = document.getElementById('count');
  var searchEl = document.getElementById('search');
  var sortEl = document.getElementById('sort');

  function sortJobs(list) {
    var mode = sortEl.value;
    var sorted = list.slice();
    if (mode === 'score-desc') {
      sorted.sort(function (a, b) { return b.score - a.score; });
    } else if (mode === 'score-asc') {
      sorted.sort(function (a, b) { return a.score - b.score; });
    } else if (mode === 'company') {
      sorted.sort(function (a, b) { return a.company.localeCompare(b.company); });
    } else if (mode === 'posted') {
      sorted.sort(function (a, b) {
        return new Date(b.posted_at || b.last_seen_at) - new Date(a.posted_at || a.last_seen_at);
      });
    } else if (mode === 'discovered') {
      sorted.sort(function (a, b) { return new Date(b.first_seen_at) - new Date(a.first_seen_at); });
    }
    return sorted;
  }

  function markRead(job) {
    job.isRead = true;
    safeStorage.setItem(READ_PREFIX + job.id, '1');
  }

  function render() {
    var query = searchEl.value.trim().toLowerCase();
    var role = roleSelect.value;
    var status = statusSelect.value;
    var source = sourceSelect.value;

    var region = regionSelect.value;

    var filtered = jobs.filter(function (job) {
      if (role && job.role_type !== role) return false;
      if (status && job.status !== status) return false;
      if (source && job.source_name !== source) return false;
      if (region && job.region !== region) return false;
      if (query) {
        var haystack = (job.title + ' ' + job.company).toLowerCase();
        if (haystack.indexOf(query) === -1) return false;
      }
      return true;
    });
    filtered = sortJobs(filtered);

    rowsEl.textContent = '';
    filtered.forEach(function (job) {
      var tr = document.createElement('tr');
      tr.className = (job.isNew ? 'is-new ' : '') + (job.isRead ? 'is-read' : '');

      var titleTd = document.createElement('td');
      var link = document.createElement('a');
      link.href = job.url;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.className = 'job-title';
      link.textContent = job.title;
      link.addEventListener('click', function () { markRead(job); tr.classList.add('is-read'); });
      titleTd.appendChild(link);
      if (job.isNew) {
        var badge = document.createElement('span');
        badge.className = 'badge badge-new';
        badge.textContent = 'NEW';
        titleTd.appendChild(badge);
      }
      var companyDiv = document.createElement('div');
      companyDiv.className = 'company';
      companyDiv.textContent = job.company;
      titleTd.appendChild(companyDiv);

      if (job.reasons && job.reasons.length) {
        var details = document.createElement('details');
        var summary = document.createElement('summary');
        summary.textContent = 'Why ranked here';
        details.appendChild(summary);
        var reasonsP = document.createElement('p');
        reasonsP.textContent = job.reasons.join('; ');
        details.appendChild(reasonsP);
        titleTd.appendChild(details);
      }
      tr.appendChild(titleTd);

      var scoreTd = document.createElement('td');
      var scoreValue = document.createElement('div');
      scoreValue.className = 'score-value';
      scoreValue.textContent = job.score;
      scoreTd.appendChild(scoreValue);
      var meter = document.createElement('div');
      meter.className = 'meter';
      var meterFill = document.createElement('div');
      meterFill.className = 'meter-fill';
      meterFill.style.width = Math.round((Math.max(0, job.score) / meterMax) * 100) + '%';
      meter.appendChild(meterFill);
      scoreTd.appendChild(meter);
      tr.appendChild(scoreTd);

      var locationTd = document.createElement('td');
      var locationLine = document.createElement('div');
      locationLine.className = job.location ? 'loc' : 'muted';
      locationLine.textContent = job.location || 'not provided';
      locationTd.appendChild(locationLine);
      if (job.region) {
        var chip = document.createElement('span');
        chip.className = 'chip ' + (REGION_CLASS[job.region] || 'region-0');
        chip.textContent = job.region;
        locationTd.appendChild(chip);
      }
      tr.appendChild(locationTd);

      var typeTd = document.createElement('td');
      typeTd.textContent = job.role_type;
      tr.appendChild(typeTd);

      var statusTd = document.createElement('td');
      statusTd.textContent = job.status;
      tr.appendChild(statusTd);

      var sourceTd = document.createElement('td');
      sourceTd.textContent = job.source_name + ' · ' + job.freshness;
      tr.appendChild(sourceTd);

      rowsEl.appendChild(tr);
    });

    countEl.textContent = filtered.length + ' of ' + jobs.length + ' opportunities shown';

    var inRegion = filtered.filter(function (job) {
      return job.region && !RESERVED_REGIONS[job.region];
    }).length;
    var topScore = filtered.reduce(function (max, job) { return Math.max(max, job.score); }, -Infinity);
    document.getElementById('tile-total').textContent = filtered.length;
    document.getElementById('tile-new').textContent = filtered.filter(function (job) { return job.isNew; }).length;
    document.getElementById('tile-region').textContent = inRegion;
    document.getElementById('tile-top').textContent = filtered.length ? topScore : '—';
  }

  searchEl.addEventListener('input', render);
  sortEl.addEventListener('change', render);
  roleSelect.addEventListener('change', render);
  statusSelect.addEventListener('change', render);
  sourceSelect.addEventListener('change', render);
  regionSelect.addEventListener('change', render);

  render();
})();
</script>
</body>
</html>
"""


def build_dashboard_html(jobs: list[dict[str, Any]], generated_at: str) -> str:
    # Escaping "</" prevents a job title/description containing "</script>"
    # from breaking out of the embedded JSON data block.
    payload = json.dumps(jobs).replace("</", "<\\/")
    doc = _DASHBOARD_HTML_TEMPLATE.replace("GENERATED_AT_PLACEHOLDER", html.escape(generated_at))
    doc = doc.replace("JOB_DATA_PLACEHOLDER", payload)
    return doc


def render_dashboard(
    conn: sqlite3.Connection,
    sources_config: dict[str, Any],
    dashboard_limit: int,
    profile: dict[str, Any] | None = None,
) -> int:
    stale_days = int(sources_config.get("stale_after_days", 7))
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE active=1 AND duplicate_of IS NULL
        ORDER BY score DESC, COALESCE(posted_at, last_seen_at) DESC
        LIMIT ?
        """,
        (dashboard_limit,),
    ).fetchall()
    payload = [
        {
            "id": row["id"],
            "title": row["title"],
            "company": row["company"],
            "location": row["location"],
            "region": region_label(row["location"], profile or {}),
            "role_type": row["role_type"],
            "status": row["status"],
            "score": row["score"],
            "reasons": display_reasons(json.loads(row["score_explanation"])),
            "source_name": row["source_name"],
            "url": row["url"],
            "first_seen_at": row["first_seen_at"],
            "last_seen_at": row["last_seen_at"],
            "posted_at": row["posted_at"],
            "freshness": stale_label(row["last_seen_at"], stale_days),
        }
        for row in rows
    ]
    paths.OUTPUT_DASHBOARD.parent.mkdir(parents=True, exist_ok=True)
    paths.OUTPUT_DASHBOARD.write_text(build_dashboard_html(payload, now_iso()), encoding="utf-8")
    print(f"Wrote {len(payload)} opportunities to {paths.OUTPUT_DASHBOARD.relative_to(paths.ROOT)}")
    return len(payload)


def update_status(
    conn: sqlite3.Connection,
    job_id: str,
    status: str,
    notes: str | None,
    follow_up: str | None,
) -> None:
    if status not in VALID_STATUSES:
        raise SystemExit(f"Invalid status. Choose one of: {', '.join(sorted(VALID_STATUSES))}")
    existing = conn.execute("SELECT id FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not existing:
        raise SystemExit(f"No job found with ID {job_id}")
    applied_at = now_iso() if status == "applied" else None
    conn.execute(
        """
        UPDATE jobs
        SET status=?,
            notes=COALESCE(?, notes),
            follow_up_at=COALESCE(?, follow_up_at),
            applied_at=CASE WHEN ?='applied' THEN COALESCE(applied_at, ?) ELSE applied_at END
        WHERE id=?
        """,
        (status, notes, follow_up, status, applied_at, job_id),
    )
    conn.commit()
    # Plain ASCII arrow: the Windows console defaults to cp1252, which has no
    # mapping for U+2192, so an arrow here crashed `update` with a
    # UnicodeEncodeError before it could print the confirmation.
    print(f"Updated {job_id} -> {status}")


def show_status(conn: sqlite3.Connection) -> None:
    totals = conn.execute(
        "SELECT status, COUNT(*) AS count FROM jobs GROUP BY status ORDER BY count DESC"
    ).fetchall()
    active = conn.execute("SELECT COUNT(*) FROM jobs WHERE active=1 AND duplicate_of IS NULL").fetchone()[0]
    print(f"{active} active unique postings")
    for row in totals:
        print(f"  {row['status']}: {row['count']}")
    errors = conn.execute(
        """
        SELECT run.source_key, run.finished_at, run.error
        FROM fetch_runs AS run
        JOIN (
            SELECT source_key, MAX(id) AS latest_id
            FROM fetch_runs
            GROUP BY source_key
        ) AS latest ON latest.latest_id=run.id
        WHERE run.outcome='error'
        ORDER BY run.id DESC
        """
    ).fetchall()
    if errors:
        print("Recent source errors:")
        for row in errors:
            print(f"  {row['source_key']} at {row['finished_at']}: {row['error']}")


def doctor(profile: dict[str, Any], sources_config: dict[str, Any]) -> int:
    exit_code = 0
    missing: list[str] = []
    for key in (
        "graduation_year",
        "preferred_locations",
        "regions",
        "skills",
        "interest_keywords",
        "work_authorized_us",
        "requires_sponsorship",
        "hours_per_week",
        "available_terms",
        "compensation_preferences",
    ):
        value = profile.get(key)
        # preferred_locations is only the fallback for a profile with no regions.
        if key == "preferred_locations" and profile.get("regions"):
            continue
        if value is None or value == []:
            missing.append(key)
    if missing:
        print("Profile is usable but incomplete:")
        for key in missing:
            print(f"  - {key}")
        print("Edit config/profile.json, or ask your agent to follow SETUP.md.")
        exit_code = 1

    usajobs_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "usajobs" and source.get("enabled", True)
    ]
    if usajobs_sources and not os.environ.get("USAJOBS_API_KEY"):
        print("USAJOBS source is enabled but USAJOBS_API_KEY is not set.")
        print("Register a free key at https://developer.usajobs.gov/, then copy .env.example")
        print("to .env and fill it in (.env is gitignored).")
        exit_code = 1
    # The API rejects requests whose User-Agent is not the address the key was
    # registered under, so a missing email fails just as hard as a missing key.
    if usajobs_sources and not os.environ.get("USAJOBS_CONTACT_EMAIL"):
        if any(not source.get("contact_email") for source in usajobs_sources):
            print("USAJOBS source is enabled but no contact email is set.")
            print("Set USAJOBS_CONTACT_EMAIL in .env to the address the key was registered with.")
            exit_code = 1

    adzuna_sources = [
        source
        for source in sources_config.get("ats_sources", [])
        if source.get("kind") == "adzuna" and source.get("enabled", True)
    ]
    # Adzuna issues the pair together and rejects a request missing either half,
    # so both are reported rather than only the first one found missing.
    adzuna_missing = [
        name for name in ("ADZUNA_APP_ID", "ADZUNA_APP_KEY") if not os.environ.get(name)
    ]
    if adzuna_sources and adzuna_missing:
        print(f"Adzuna source is enabled but {' and '.join(adzuna_missing)} not set.")
        print("Register a free application at https://developer.adzuna.com/, then copy")
        print(".env.example to .env and fill both values in (.env is gitignored).")
        exit_code = 1

    if exit_code == 0:
        print("Profile has all high-impact fields.")
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    resume_help = "Skip sources already fetched successfully since this ISO-8601 time"
    fetch_parser = sub.add_parser("fetch", help="Fetch enabled public ATS sources")
    fetch_parser.add_argument("--resume-since", help=resume_help)
    import_parser = sub.add_parser("import-csv", help="Import login-only or manually found postings")
    import_parser.add_argument("path", nargs="?", default=str(paths.MANUAL_PATH))
    import_email_parser = sub.add_parser(
        "import-emails", help="Import LinkedIn job-alert email JSON (see README)"
    )
    import_email_parser.add_argument("path", nargs="?", default=str(paths.EMAIL_IMPORT_PATH))
    import_discovered_parser = sub.add_parser(
        "import-discovered",
        help="Import agent-discovered postings from search/public pages/lists (see README)",
    )
    import_discovered_parser.add_argument("path", nargs="?", default=str(paths.DISCOVERED_IMPORT_PATH))
    enrich_parser = sub.add_parser(
        "enrich", help="Backfill descriptions an agent read from public posting pages"
    )
    enrich_parser.add_argument("path", nargs="?", default=str(paths.ENRICHMENT_PATH))
    enrich_parser.add_argument(
        "--force",
        action="store_true",
        help="Replace existing descriptions too, not just thin ones",
    )
    sub.add_parser("score", help="Recompute transparent fit scores")
    report_parser = sub.add_parser("report", help="Write Markdown, CSV, and dashboard shortlists")
    report_parser.add_argument("--limit", type=int, default=30)
    report_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser = sub.add_parser("run", help="Fetch, import, score, and report")
    run_parser.add_argument("--limit", type=int, default=30)
    run_parser.add_argument("--dashboard-limit", type=int, default=300)
    run_parser.add_argument("--resume-since", help=resume_help)
    update_parser = sub.add_parser("update", help="Update application status")
    update_parser.add_argument("job_id")
    update_parser.add_argument("status")
    update_parser.add_argument("--notes")
    update_parser.add_argument("--follow-up", help="ISO date, e.g. 2026-08-05")
    resume_parser = sub.add_parser(
        "resume", help="Render your resume, optionally emphasised for one posting"
    )
    resume_parser.add_argument("--job", help="Posting ID to tailor emphasis toward")
    resume_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    cover_parser = sub.add_parser("cover-letter", help="Draft a cover letter for one posting")
    cover_parser.add_argument("--job", required=True, help="Posting ID (see output/shortlist.md)")
    cover_parser.add_argument("--pdf", action="store_true", help="Also render a PDF")
    discover_parser = sub.add_parser(
        "discover-ats",
        help="Resolve company names to Greenhouse/Ashby/Lever boards (preview by default)",
    )
    discover_parser.add_argument("companies", nargs="*", help="Company names to probe")
    discover_parser.add_argument(
        "--in",
        dest="companies_path",
        help="JSON file holding a list of company names, or {\"companies\": [...]}",
    )
    discover_parser.add_argument(
        "--vendors",
        help=f"Comma-separated subset of {','.join(DISCOVERY_VENDOR_ORDER)}",
    )
    discover_parser.add_argument(
        "--write",
        action="store_true",
        help="Append identity-confirmed entries to config/sources.local.json",
    )
    discover_parser.add_argument(
        "--shared",
        action="store_true",
        help="With --write, append to the tracked shared catalog config/sources.json instead",
    )
    discover_parser.add_argument(
        "--include-unverified",
        action="store_true",
        help="Also write Ashby/Lever hits, whose APIs expose no company name to check",
    )
    liveness_parser = sub.add_parser(
        "liveness",
        help="Check whether imported postings are still open, and retire dead ones",
    )
    liveness_parser.add_argument(
        "--limit", type=int, help="Check at most N postings, least recently seen first"
    )
    liveness_parser.add_argument(
        "--all",
        action="store_true",
        dest="check_all",
        help="Also check ATS-sourced rows, which their own source batch already retires",
    )
    liveness_parser.add_argument(
        "--dry-run", action="store_true", help="Report verdicts without retiring anything"
    )
    purge_parser = sub.add_parser(
        "purge-expired",
        help="Delete retired postings and postings past their stated deadline",
    )
    purge_parser.add_argument(
        "--dry-run", action="store_true", help="Report what would be deleted without deleting"
    )
    sub.add_parser("status", help="Show pipeline counts and recent fetch errors")
    sub.add_parser("doctor", help="Check whether high-impact profile fields are filled")
    return parser


def main() -> int:
    # Company names and job titles come from scraped pages and routinely carry
    # characters the Windows console's cp1252 default cannot encode ("Ørsted",
    # curly quotes, typographic dashes). Printing one raises UnicodeEncodeError
    # mid-command, which would abort a run that was otherwise succeeding. The
    # test suite guards the literals in this file; only this guards the data.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError):
            # Not a real console (piped, captured in tests): nothing to fix.
            pass

    args = build_parser().parse_args()
    load_env_file()
    profile = load_profile()
    sources = load_sources()
    conn = connect()
    try:
        if args.command == "fetch":
            if fetch_all(conn, sources, args.resume_since):
                return EXIT_TEMPFAIL
        elif args.command == "import-csv":
            import_manual(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-emails":
            import_emails(conn, Path(args.path).expanduser().resolve())
        elif args.command == "import-discovered":
            import_discovered(conn, Path(args.path).expanduser().resolve())
        elif args.command == "enrich":
            enrich_descriptions(conn, Path(args.path).expanduser().resolve(), args.force)
        elif args.command == "score":
            score_all(conn, profile)
        elif args.command == "report":
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
        elif args.command == "run":
            transient_failures = fetch_all(conn, sources, args.resume_since)
            # Score and report even when some sources were unreachable, so the
            # shortlist reflects what did arrive; the exit code asks for a retry.
            import_manual(conn, paths.MANUAL_PATH)
            score_all(conn, profile)
            report(conn, sources, args.limit)
            render_dashboard(conn, sources, args.dashboard_limit, profile)
            if transient_failures:
                print(
                    f"{transient_failures} source(s) were unreachable; rerun with "
                    "--resume-since to fetch only what is missing",
                    file=sys.stderr,
                )
                return EXIT_TEMPFAIL
        elif args.command == "update":
            update_status(conn, args.job_id, args.status, args.notes, args.follow_up)
        elif args.command in {"resume", "cover-letter"}:
            write_artifact(conn, args.command, args.job, args.pdf)
        elif args.command == "discover-ats":
            companies = list(args.companies)
            if args.companies_path:
                payload = load_json(Path(args.companies_path).expanduser().resolve())
                listed = payload["companies"] if isinstance(payload, dict) else payload
                companies += [str(name).strip() for name in listed if str(name).strip()]
            if not companies:
                raise SystemExit("No company names given. Pass them as arguments or via --in.")
            report_discovery(
                companies,
                sources,
                args.write,
                args.include_unverified,
                args.vendors.split(",") if args.vendors else None,
                shared=args.shared,
            )
        elif args.command == "liveness":
            check_liveness(conn, args.limit, args.check_all, args.dry_run)
        elif args.command == "purge-expired":
            purge_expired(conn, dry_run=args.dry_run)
        elif args.command == "status":
            show_status(conn)
        elif args.command == "doctor":
            return doctor(profile, sources)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
