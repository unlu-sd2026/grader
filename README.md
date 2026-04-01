# Grader

Central autograding engine for unlu-sd2026. Runs on a schedule, discovers student forks, executes hidden tests, and reports results to Google Sheets.

## How it works

1. A scheduled GitHub Action runs every 10 minutes (or on manual trigger)
2. For each exercise template repo, it lists all forks via the GitHub API
3. For each fork with new commits since last grading:
   - Clones the student's code
   - Clones the private `test-suite` repo
   - Runs `pytest` on the hidden tests against the student's code
   - Reports results to Google Sheets (✅/❌ + score)
   - Posts a commit comment on the student's repo

## Manual trigger

```bash
gh workflow run grade.yml -R unlu-sd2026/grader
```

## Secrets required (org-level)

- `TEST_SUITE_TOKEN` — PAT with read access to `test-suite`
- `GOOGLE_CREDENTIALS` — Google Sheets service account JSON
- `SHEET_ID` — Google Sheet ID
