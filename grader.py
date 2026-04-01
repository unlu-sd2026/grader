#!/usr/bin/env python3
"""
Central grading engine for unlu-sd2026.

Discovers student forks of exercise template repos, runs hidden tests,
reports results to Google Sheets, and comments on student commits.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ── Config ──
ORG = "unlu-sd2026"
GH_TOKEN = os.environ.get("GH_TOKEN", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
TEST_SUITE_DIR = Path("/tmp/test-suite")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
FORCE = os.environ.get("INPUT_FORCE", "false").lower() == "true"
FILTER_EXERCISE = os.environ.get("INPUT_EXERCISE", "").strip()

GH_HEADERS = {
    "Authorization": f"token {GH_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
BOT_MARKER = "<!-- grader-bot -->"


# ── GitHub API helpers ──
def gh_get(url, params=None):
    resp = requests.get(url, headers=GH_HEADERS, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def gh_post(url, data):
    resp = requests.post(url, headers=GH_HEADERS, json=data, timeout=30)
    resp.raise_for_status()
    return resp.json()


def list_forks(owner, repo):
    """List all forks of a repo."""
    forks = []
    page = 1
    while True:
        url = f"https://api.github.com/repos/{owner}/{repo}/forks"
        batch = gh_get(url, params={"per_page": 100, "page": page})
        if not batch:
            break
        forks.extend(batch)
        page += 1
    return forks


def get_latest_commit(owner, repo, branch="main"):
    """Get the latest commit SHA and date for a repo branch."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    try:
        commits = gh_get(url, params={"sha": branch, "per_page": 1})
        if commits:
            return commits[0]["sha"], commits[0]["commit"]["author"]["date"]
    except requests.HTTPError:
        # Try 'master' branch as fallback
        try:
            commits = gh_get(url, params={"sha": "master", "per_page": 1})
            if commits:
                return commits[0]["sha"], commits[0]["commit"]["author"]["date"]
        except requests.HTTPError:
            pass
    return None, None


def already_graded(owner, repo, sha):
    """Check if a commit already has a grader comment."""
    if FORCE:
        return False
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments"
    try:
        comments = gh_get(url)
        return any(BOT_MARKER in (c.get("body", "")) for c in comments)
    except requests.HTTPError:
        return False


def post_commit_comment(owner, repo, sha, body):
    """Post a comment on a specific commit."""
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments"
    try:
        gh_post(url, {"body": f"{BOT_MARKER}\n{body}"})
        print(f"    Commented on {owner}/{repo}@{sha[:7]}")
    except requests.HTTPError as e:
        print(f"    Warning: Could not comment on {owner}/{repo}: {e}")


# ── Test runner ──
def run_tests(fork_dir, test_dir, exercise_type):
    """Run pytest on the test suite against student code. Returns (passed, total, output)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fork_dir)

    cmd = ["pytest", str(test_dir), "--tb=short", "-q", "--no-header"]

    # For docker exercises, we need to be in the fork dir
    if exercise_type in ("docker", "kubernetes"):
        cwd = str(fork_dir)
    else:
        cwd = str(fork_dir)

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=120,
            cwd=cwd,
            env=env,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 0, 0, "TIMEOUT: Tests took too long (>120s)"

    # Parse pytest output
    passed = 0
    failed = 0
    for line in output.splitlines():
        # Match "X passed" and "X failed"
        if "passed" in line:
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "passed" and i > 0:
                    try:
                        passed = int(parts[i - 1])
                    except ValueError:
                        pass
                if p == "failed" and i > 0:
                    try:
                        failed = int(parts[i - 1])
                    except ValueError:
                        pass
        if "error" in line.lower() and "failed" not in line.lower():
            # Count errors as failures
            parts = line.split()
            for i, p in enumerate(parts):
                if p == "error" and i > 0:
                    try:
                        failed += int(parts[i - 1])
                    except ValueError:
                        pass

    total = passed + failed
    if total == 0 and result.returncode != 0:
        # Something went wrong but couldn't parse output
        total = 1
        failed = 1

    return passed, total, output


# ── Google Sheets ──
def get_sheet_service():
    if not GOOGLE_CREDENTIALS:
        return None
    try:
        data = json.loads(GOOGLE_CREDENTIALS)
        if "client_email" not in data:
            print(
                "  Warning: GOOGLE_CREDENTIALS missing required fields, skipping Sheets"
            )
            return None
        creds = Credentials.from_service_account_info(
            data,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        return build("sheets", "v4", credentials=creds)
    except Exception as e:
        print(f"  Warning: Could not init Google Sheets: {e}")
        return None


def col_letter(index):
    result = ""
    while index >= 0:
        result = chr(index % 26 + ord("A")) + result
        index = index // 26 - 1
    return result


def report_to_sheet(service, student, exercise_col, passed, total):
    """Update the Google Sheet with the grading result."""
    if not service or not SHEET_ID:
        print("    Skipping Google Sheets (not configured)")
        return

    # Find student row
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range="resultados!A:A")
        .execute()
    )
    values = result.get("values", [])
    row = None
    for i, r in enumerate(values):
        if r and r[0] == student:
            row = i + 1
            break

    if row is None:
        # Add new student
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="resultados!A:A",
            valueInputOption="USER_ENTERED",
            body={"values": [[student]]},
        ).execute()
        # Re-find the row
        result = (
            service.spreadsheets()
            .values()
            .get(spreadsheetId=SHEET_ID, range="resultados!A:A")
            .execute()
        )
        values = result.get("values", [])
        for i, r in enumerate(values):
            if r and r[0] == student:
                row = i + 1
                break

    # Find exercise column
    result = (
        service.spreadsheets()
        .values()
        .get(spreadsheetId=SHEET_ID, range="resultados!1:1")
        .execute()
    )
    headers = result.get("values", [[]])[0]
    ex_col = None
    for i, h in enumerate(headers):
        if h == exercise_col:
            ex_col = i
            break

    if ex_col is None:
        print(f"    Warning: Column '{exercise_col}' not found in sheet header")
        return

    # Write result
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    icon = "\u2705" if passed == total and total > 0 else "\u274c"
    value = f"{icon} {passed}/{total} ({now})"
    cell = f"resultados!{col_letter(ex_col)}{row}"

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()

    print(f"    Sheet updated: {student} → {value}")


# ── Main grading loop ──
def grade_fork(fork, exercise, sheet_service):
    """Grade a single student fork."""
    fork_owner = fork["owner"]["login"]
    fork_repo = fork["name"]
    fork_full = f"{fork_owner}/{fork_repo}"

    print(f"  Checking {fork_full}...")

    # Get latest commit
    sha, commit_date = get_latest_commit(fork_owner, fork_repo)
    if not sha:
        print("    No commits found, skipping")
        return

    # Check if already graded
    if already_graded(fork_owner, fork_repo, sha):
        print(f"    Already graded ({sha[:7]}), skipping")
        return

    print(f"    Grading commit {sha[:7]}...")

    # Clone the fork
    work_dir = tempfile.mkdtemp()
    fork_dir = Path(work_dir) / "student"

    try:
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                f"https://x-access-token:{GH_TOKEN}@github.com/{fork_full}.git",
                str(fork_dir),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        if not fork_dir.exists():
            print(f"    Failed to clone {fork_full}")
            return

        # Copy tests from test-suite
        test_src = TEST_SUITE_DIR / "exercises" / exercise["test_dir"]
        if not test_src.exists():
            print(f"    No tests found at {test_src}")
            return

        test_dst = Path(work_dir) / "tests"
        shutil.copytree(test_src, test_dst)

        # Run tests
        passed, total, output = run_tests(
            fork_dir, test_dst, exercise.get("type", "python")
        )

        print(f"    Result: {passed}/{total} passed")

        # Report to Google Sheets
        report_to_sheet(
            sheet_service,
            fork_owner,
            exercise["sheet_column"],
            passed,
            total,
        )

        # Comment on commit
        if passed == total and total > 0:
            body = f"\u2705 **{passed}/{total} tests passed.** All tests passed!"
        elif total > 0:
            body = f"\u274c **{passed}/{total} tests passed.** Check the details below.\n\n<details>\n<summary>Test output</summary>\n\n```\n{output[-2000:]}\n```\n</details>"
        else:
            body = f"\u274c **Tests could not run.** There may be import errors or missing files.\n\n<details>\n<summary>Output</summary>\n\n```\n{output[-2000:]}\n```\n</details>"

        post_commit_comment(fork_owner, fork_repo, sha, body)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    print("=" * 60)
    print("  GRADER — unlu-sd2026")
    print(f"  Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Force re-grade: {FORCE}")
    print("=" * 60)

    # Load exercises config
    config_path = Path(__file__).parent / "exercises.yml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    exercises = config.get("exercises", [])
    if FILTER_EXERCISE:
        exercises = [e for e in exercises if e["name"] == FILTER_EXERCISE]
        if not exercises:
            print(f"Exercise '{FILTER_EXERCISE}' not found in exercises.yml")
            sys.exit(1)

    # Init Google Sheets
    sheet_service = get_sheet_service()

    total_graded = 0

    for exercise in exercises:
        repo_full = exercise["repo"]
        owner, repo = repo_full.split("/")
        print(f"\n{'─' * 40}")
        print(f"Exercise: {exercise['name']} ({repo_full})")
        print(f"{'─' * 40}")

        # List forks
        forks = list_forks(owner, repo)
        print(f"  Found {len(forks)} fork(s)")

        for fork in forks:
            # Skip forks owned by the org itself
            if fork["owner"]["login"] == ORG:
                continue
            grade_fork(fork, exercise, sheet_service)
            total_graded += 1

    print(f"\n{'=' * 60}")
    print(f"  Done. Processed {total_graded} fork(s).")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
