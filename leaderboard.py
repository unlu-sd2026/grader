#!/usr/bin/env python3
"""
Generate a leaderboard HTML page from the Google Sheet results.

Can be run locally or as a GitHub Pages deployment.
Usage: python leaderboard.py > docs/index.html
"""

import json
import os
import re
from datetime import datetime, timezone

from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

SHEET_ID = os.environ.get("SHEET_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")


def get_sheet_data():
    creds = Credentials.from_service_account_info(
        json.loads(GOOGLE_CREDENTIALS),
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"],
    )
    service = build("sheets", "v4", credentials=creds)
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range="resultados!A:Z")
        .execute()
    )
    return result.get("values", [])


def parse_score(cell):
    """Parse a cell like '✅ 11/11 (100%) [2026-04-01 18:23]' into (passed, total, pct, status)."""
    if not cell:
        return 0, 0, 0, "pending"

    match = re.search(r"(\d+)/(\d+)", cell)
    if not match:
        return 0, 0, 0, "pending"

    passed = int(match.group(1))
    total = int(match.group(2))
    pct = round((passed / total) * 100) if total > 0 else 0

    if "\u2705" in cell or "✅" in cell:
        status = "pass"
    elif "LATE" in cell:
        status = "late"
    else:
        status = "fail"

    return passed, total, pct, status


def generate_html(data):
    if not data or len(data) < 2:
        return "<html><body><h1>No data yet</h1></body></html>"

    headers = data[0]
    exercises = headers[1:]  # Skip 'student' column (and 'email' if present)
    # Filter out non-exercise columns
    exercise_cols = []
    for i, h in enumerate(headers):
        if h.startswith("ejercicio"):
            exercise_cols.append((i, h))

    students = []
    for row in data[1:]:
        if not row or not row[0]:
            continue
        student = row[0]
        total_pct = 0
        cells = []
        for col_idx, col_name in exercise_cols:
            cell_val = row[col_idx] if col_idx < len(row) else ""
            passed, total, pct, status = parse_score(cell_val)
            total_pct += pct
            cells.append(
                {"passed": passed, "total": total, "pct": pct, "status": status}
            )

        avg = round(total_pct / len(exercise_cols)) if exercise_cols else 0
        students.append({"name": student, "avg": avg, "cells": cells})

    # Sort by average score descending
    students.sort(key=lambda s: s["avg"], reverse=True)

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    exercise_headers = "".join(f"<th>{e[1]}</th>" for e in exercise_cols)

    rows_html = ""
    for rank, s in enumerate(students, 1):
        cells_html = ""
        for c in s["cells"]:
            if c["status"] == "pass":
                cls = "pass"
                icon = "✅"
            elif c["status"] == "late":
                cls = "late"
                icon = "⏰"
            elif c["status"] == "fail":
                cls = "fail"
                icon = "❌"
            else:
                cls = "pending"
                icon = "⏳"

            if c["total"] > 0:
                cells_html += f'<td class="{cls}">{icon} {c["passed"]}/{c["total"]} ({c["pct"]}%)</td>'
            else:
                cells_html += f'<td class="{cls}">{icon}</td>'

        medal = ""
        if rank == 1:
            medal = "🥇 "
        elif rank == 2:
            medal = "🥈 "
        elif rank == 3:
            medal = "🥉 "

        rows_html += f"""
        <tr>
            <td class="rank">{rank}</td>
            <td class="student">{medal}{s["name"]}</td>
            <td class="avg">{s["avg"]}%</td>
            {cells_html}
        </tr>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Leaderboard — SD 2026</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Inter', -apple-system, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            padding: 2rem;
        }}
        h1 {{
            color: #58a6ff;
            font-size: 1.8rem;
            margin-bottom: 0.3rem;
        }}
        .subtitle {{
            color: #8b949e;
            font-size: 0.9rem;
            margin-bottom: 2rem;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
            background: #161b22;
            border-radius: 8px;
            overflow: hidden;
        }}
        th {{
            background: #21262d;
            padding: 12px 16px;
            text-align: left;
            font-weight: 600;
            color: #58a6ff;
            font-size: 0.85rem;
            text-transform: uppercase;
        }}
        td {{
            padding: 10px 16px;
            border-top: 1px solid #21262d;
            font-size: 0.9rem;
        }}
        tr:hover {{ background: #1c2128; }}
        .rank {{ width: 50px; text-align: center; color: #8b949e; }}
        .student {{ font-weight: 600; }}
        .avg {{ font-weight: 700; color: #f0f6fc; }}
        .pass {{ color: #3fb950; }}
        .fail {{ color: #f85149; }}
        .late {{ color: #d29922; }}
        .pending {{ color: #484f58; }}
        .footer {{
            margin-top: 1.5rem;
            color: #484f58;
            font-size: 0.8rem;
        }}
    </style>
</head>
<body>
    <h1>🏆 Leaderboard — Sistemas Distribuidos 2026</h1>
    <p class="subtitle">Updated: {now} · Auto-generated by the grading system</p>

    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>Student</th>
                <th>Average</th>
                {exercise_headers}
            </tr>
        </thead>
        <tbody>
            {rows_html}
        </tbody>
    </table>

    <p class="footer">
        ✅ Pass · ❌ Fail · ⏰ Late · ⏳ Pending
    </p>
</body>
</html>"""


def main():
    data = get_sheet_data()
    html = generate_html(data)

    # Write to docs/index.html for GitHub Pages
    from pathlib import Path

    docs_dir = Path(__file__).parent / "docs"
    docs_dir.mkdir(exist_ok=True)
    (docs_dir / "index.html").write_text(html)
    print(f"Leaderboard written to {docs_dir / 'index.html'}")


if __name__ == "__main__":
    main()
