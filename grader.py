#!/usr/bin/env python3
"""
Central grading engine for unlu-sd2026.

Features:
- Discovers student forks of exercise templates
- Runs hidden tests with partial scoring
- Enforces deadlines (LATE / skip)
- Reports results to Google Sheets
- Posts commit comments on student repos
- Sends Discord webhook notifications
- Enforces retry limits (max submissions per exercise)
- Detects plagiarism (cross-fork similarity)
"""

import difflib
import json
import os
import re
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
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")
FORCE = os.environ.get("INPUT_FORCE", "false").lower() == "true"
FILTER_EXERCISE = os.environ.get("INPUT_EXERCISE", "").strip()

GH_HEADERS = {
    "Authorization": f"token {GH_TOKEN}",
    "Accept": "application/vnd.github.v3+json",
}
BOT_MARKER = "<!-- grader-bot -->"
NOW = datetime.now(timezone.utc)


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
    url = f"https://api.github.com/repos/{owner}/{repo}/commits"
    try:
        commits = gh_get(url, params={"sha": branch, "per_page": 1})
        if commits:
            return commits[0]["sha"], commits[0]["commit"]["author"]["date"]
    except requests.HTTPError:
        try:
            commits = gh_get(url, params={"sha": "master", "per_page": 1})
            if commits:
                return commits[0]["sha"], commits[0]["commit"]["author"]["date"]
        except requests.HTTPError:
            pass
    return None, None


def count_grader_comments(owner, repo):
    """Count how many grader-bot comments exist across all commits."""
    url = f"https://api.github.com/repos/{owner}/{repo}/comments"
    try:
        comments = gh_get(url, params={"per_page": 100})
        return sum(1 for c in comments if BOT_MARKER in c.get("body", ""))
    except requests.HTTPError:
        return 0


def already_graded(owner, repo, sha):
    if FORCE:
        return False
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments"
    try:
        comments = gh_get(url)
        return any(BOT_MARKER in c.get("body", "") for c in comments)
    except requests.HTTPError:
        return False


def post_commit_comment(owner, repo, sha, body):
    url = f"https://api.github.com/repos/{owner}/{repo}/commits/{sha}/comments"
    try:
        gh_post(url, {"body": f"{BOT_MARKER}\n{body}"})
        print(f"    Commented on {owner}/{repo}@{sha[:7]}")
    except requests.HTTPError as e:
        print(f"    Warning: Could not comment on {owner}/{repo}: {e}")


# ── Deadline ──
def check_deadline(exercise, commit_date_str):
    """Check if submission is on time. Returns ('on_time', 'late', or 'closed')."""
    deadline_str = exercise.get("deadline")
    if not deadline_str:
        return "on_time"

    deadline = datetime.fromisoformat(deadline_str).replace(tzinfo=timezone.utc)
    commit_date = datetime.fromisoformat(commit_date_str.replace("Z", "+00:00"))

    late_days = exercise.get("late_days", 0)
    hard_deadline = deadline

    if commit_date <= deadline:
        return "on_time"

    if late_days > 0:
        from datetime import timedelta

        hard_deadline = deadline + timedelta(days=late_days)
        if commit_date <= hard_deadline:
            return "late"

    if exercise.get("accept_late", True):
        return "late"

    return "closed"


# ── Test runner ──
def run_tests(fork_dir, test_dir, exercise_type):
    """Run pytest with verbose per-test results. Returns (passed, total, output, test_details)."""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(fork_dir)

    cmd = ["pytest", str(test_dir), "--tb=short", "-v", "--no-header"]
    cwd = str(fork_dir)

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=180, cwd=cwd, env=env
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return 0, 0, "TIMEOUT: Tests took too long (>180s)", []

    # Parse per-test results for partial scoring
    test_details = []
    passed = 0
    failed = 0
    for line in output.splitlines():
        if " PASSED" in line:
            test_name = line.split(" PASSED")[0].strip()
            test_details.append({"name": test_name, "status": "passed"})
            passed += 1
        elif " FAILED" in line:
            test_name = line.split(" FAILED")[0].strip()
            test_details.append({"name": test_name, "status": "failed"})
            failed += 1
        elif " ERROR" in line:
            test_name = line.split(" ERROR")[0].strip()
            test_details.append({"name": test_name, "status": "error"})
            failed += 1

    total = passed + failed
    if total == 0 and result.returncode != 0:
        total = 1
        failed = 1

    return passed, total, output, test_details


# ── Partial scoring ──
def calculate_score(passed, total, exercise):
    """Calculate percentage score. Can be weighted per-test in the future."""
    if total == 0:
        return 0
    return round((passed / total) * 100)


# ── Plagiarism detection ──
def get_source_fingerprint(fork_dir):
    """Create a hash fingerprint of the student's source files."""
    src_dir = fork_dir / "src"
    if not src_dir.exists():
        return ""

    content = ""
    for f in sorted(src_dir.rglob("*.py")):
        text = f.read_text(errors="ignore")
        # Normalize: remove comments, blank lines, whitespace
        lines = []
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                # Remove inline comments
                stripped = re.sub(r"#.*$", "", stripped).strip()
                if stripped:
                    lines.append(stripped)
        content += "\n".join(lines)

    return content


def check_plagiarism(fork_fingerprints, fork_owner, fingerprint, threshold=0.85):
    """Compare a student's code against all other submissions.
    Returns list of (other_student, similarity) above threshold."""
    matches = []
    for other_owner, other_fp in fork_fingerprints.items():
        if other_owner == fork_owner or not other_fp or not fingerprint:
            continue
        ratio = difflib.SequenceMatcher(None, fingerprint, other_fp).ratio()
        if ratio >= threshold:
            matches.append((other_owner, round(ratio * 100)))
    return matches


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
            data, scopes=["https://www.googleapis.com/auth/spreadsheets"]
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


def report_to_sheet(
    service, student, exercise_col, passed, total, score, deadline_status
):
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
        service.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range="resultados!A:A",
            valueInputOption="USER_ENTERED",
            body={"values": [[student]]},
        ).execute()
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

    # Build value
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    icon = "\u2705" if passed == total and total > 0 else "\u274c"
    late_tag = " LATE" if deadline_status == "late" else ""
    value = f"{icon} {passed}/{total} ({score}%){late_tag} [{now}]"
    cell = f"resultados!{col_letter(ex_col)}{row}"

    service.spreadsheets().values().update(
        spreadsheetId=SHEET_ID,
        range=cell,
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()

    print(f"    Sheet updated: {student} → {value}")


# ── Discord ──
def send_discord(
    student, exercise_name, passed, total, score, deadline_status, plagiarism_matches
):
    if not DISCORD_WEBHOOK:
        return

    icon = "\u2705" if passed == total and total > 0 else "\u274c"
    late_tag = " `LATE`" if deadline_status == "late" else ""
    plag_warning = ""
    if plagiarism_matches:
        plag_warning = "\n\u26a0\ufe0f **Plagiarism alert:** similar to " + ", ".join(
            f"`{m[0]}` ({m[1]}%)" for m in plagiarism_matches
        )

    embed = {
        "title": f"{icon} {exercise_name}",
        "description": (
            f"**{student}** — {passed}/{total} tests ({score}%){late_tag}{plag_warning}"
        ),
        "color": 0x2ECC71 if passed == total and total > 0 else 0xE74C3C,
        "timestamp": NOW.isoformat(),
    }

    try:
        requests.post(
            DISCORD_WEBHOOK,
            json={"embeds": [embed]},
            timeout=10,
        )
    except Exception as e:
        print(f"    Warning: Discord notification failed: {e}")


# ── Main grading loop ──
def grade_fork(fork, exercise, sheet_service, fork_fingerprints):
    fork_owner = fork["owner"]["login"]
    fork_repo = fork["name"]
    fork_full = f"{fork_owner}/{fork_repo}"

    print(f"  Checking {fork_full}...")

    # Get latest commit
    sha, commit_date = get_latest_commit(fork_owner, fork_repo)
    if not sha:
        print("    No commits found, skipping")
        return

    # Check retry limit
    max_retries = exercise.get("max_retries", 0)
    if max_retries > 0:
        comment_count = count_grader_comments(fork_owner, fork_repo)
        if comment_count >= max_retries and not FORCE:
            print(f"    Retry limit reached ({comment_count}/{max_retries}), skipping")
            post_commit_comment(
                fork_owner,
                fork_repo,
                sha,
                f"\u26d4 **Retry limit reached ({max_retries} submissions max).** Contact the instructor if you need more attempts.",
            )
            return

    # Check deadline
    deadline_status = check_deadline(exercise, commit_date)
    if deadline_status == "closed":
        print("    Deadline closed, skipping")
        post_commit_comment(
            fork_owner,
            fork_repo,
            sha,
            "\u23f0 **Deadline passed.** This exercise is no longer accepting submissions.",
        )
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

        # Plagiarism fingerprint
        fingerprint = get_source_fingerprint(fork_dir)
        fork_fingerprints[fork_owner] = fingerprint
        plagiarism_matches = check_plagiarism(
            fork_fingerprints, fork_owner, fingerprint
        )
        if plagiarism_matches:
            print(f"    ⚠ Plagiarism alert: similar to {plagiarism_matches}")

        # Copy tests from test-suite
        test_src = TEST_SUITE_DIR / "exercises" / exercise["test_dir"]
        if not test_src.exists():
            print(f"    No tests found at {test_src}")
            return

        test_dst = Path(work_dir) / "tests"
        shutil.copytree(test_src, test_dst)

        # Run tests
        passed, total, output, test_details = run_tests(
            fork_dir, test_dst, exercise.get("type", "python")
        )
        score = calculate_score(passed, total, exercise)

        print(f"    Result: {passed}/{total} ({score}%)")

        # Report to Google Sheets
        report_to_sheet(
            sheet_service,
            fork_owner,
            exercise["sheet_column"],
            passed,
            total,
            score,
            deadline_status,
        )

        # Build commit comment
        late_badge = ""
        if deadline_status == "late":
            late_badge = "\n\n\u23f0 **Submitted after deadline (LATE)**"

        plag_badge = ""
        if plagiarism_matches:
            plag_badge = (
                "\n\n\u26a0\ufe0f **Plagiarism warning:** high similarity with "
                + ", ".join(f"`{m[0]}` ({m[1]}%)" for m in plagiarism_matches)
            )

        retry_info = ""
        if max_retries > 0:
            current = count_grader_comments(fork_owner, fork_repo)
            retry_info = f"\n\n\U0001f504 Submission {current}/{max_retries}"

        if passed == total and total > 0:
            body = f"\u2705 **{passed}/{total} tests passed ({score}%).** All tests passed!{late_badge}{retry_info}"
        elif total > 0:
            # Build per-test breakdown
            breakdown = "\n".join(
                f"{'✅' if t['status'] == 'passed' else '❌'} {t['name'].split('::')[-1]}"
                for t in test_details[:30]
            )
            body = (
                f"\u274c **{passed}/{total} tests passed ({score}%).**{late_badge}{plag_badge}{retry_info}\n\n"
                f"### Test breakdown\n{breakdown}\n\n"
                f"<details>\n<summary>Full output</summary>\n\n```\n{output[-2000:]}\n```\n</details>"
            )
        else:
            body = (
                f"\u274c **Tests could not run ({score}%).** "
                f"Import errors or missing files.{late_badge}{retry_info}\n\n"
                f"<details>\n<summary>Output</summary>\n\n```\n{output[-2000:]}\n```\n</details>"
            )

        post_commit_comment(fork_owner, fork_repo, sha, body)

        # Discord notification
        send_discord(
            fork_owner,
            exercise["name"],
            passed,
            total,
            score,
            deadline_status,
            plagiarism_matches,
        )

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def main():
    print("=" * 60)
    print("  GRADER — unlu-sd2026")
    print(f"  Time: {NOW.strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"  Force re-grade: {FORCE}")
    print("=" * 60)

    config_path = Path(__file__).parent / "exercises.yml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    exercises = config.get("exercises", [])
    if FILTER_EXERCISE:
        exercises = [e for e in exercises if e["name"] == FILTER_EXERCISE]
        if not exercises:
            print(f"Exercise '{FILTER_EXERCISE}' not found in exercises.yml")
            sys.exit(1)

    sheet_service = get_sheet_service()
    total_graded = 0

    for exercise in exercises:
        repo_full = exercise["repo"]
        owner, repo = repo_full.split("/")
        print(f"\n{'─' * 40}")
        print(f"Exercise: {exercise['name']} ({repo_full})")

        deadline_str = exercise.get("deadline", "none")
        max_retries = exercise.get("max_retries", "unlimited")
        print(f"  Deadline: {deadline_str} | Max retries: {max_retries}")
        print(f"{'─' * 40}")

        forks = list_forks(owner, repo)
        print(f"  Found {len(forks)} fork(s)")

        # Collect fingerprints for plagiarism detection across forks
        fork_fingerprints = {}

        for fork in forks:
            if fork["owner"]["login"] == ORG:
                continue
            grade_fork(fork, exercise, sheet_service, fork_fingerprints)
            total_graded += 1

        # Report plagiarism summary
        if fork_fingerprints and len(fork_fingerprints) > 1:
            print(
                f"\n  Plagiarism check: {len(fork_fingerprints)} submissions compared"
            )

    print(f"\n{'=' * 60}")
    print(f"  Done. Processed {total_graded} fork(s).")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
