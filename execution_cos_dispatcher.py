#!/usr/bin/env python3
"""
execution_cos_dispatcher.py
===============================================================================
Replaces the old cron behaviour of "one Manus session that loops over 41
projects" with "41 independent Manus API tasks, one project each, run
sequentially." This is the actual enforcement mechanism for isolation — the
SKILL.md refusal rule is a second line of defence, not the primary one. A
model can be talked out of a written instruction; it cannot be talked out of
context it was never given. If this script never puts more than one AL ID
into a task's prompt, cross-project bleed is structurally impossible,
regardless of what the model inside that task decides to do.

WHY SEQUENTIAL, NOT PARALLEL:
  1. Attribution: if project #17 fails, you know it's #17. Fire 41 at once
     and a shared-resource failure (Gmail rate limit, Drive quota, GitHub
     API limit) can take out several simultaneously with a tangled log.
  2. Shared external resources: Gmail search, Drive, GitHub, and Discord are
     all rate-limited per-account. 41 concurrent sessions hitting the same
     Gmail account is the kind of thing that gets you temporarily throttled,
     which then reads to each task like "no new emails" — a silent false
     negative baked into 41 Brains at once.
  3. This mirrors the exact failure being fixed: the original bug was one
     session doing too much at once. Running the *dispatcher* the same way
     (fire-and-forget parallel) would just move the "too much at once"
     problem up one layer instead of removing it.

HOW THIS ENFORCES "ONE PROJECT, ONE SESSION":
  - Each call to create_task() hits POST https://api.manus.ai/v1/tasks with
    a prompt containing exactly one AL ID. Per Manus's API, this creates a
    brand-new task = a brand-new agent session with no memory of any other
    task. There is no code path in this script that puts two AL IDs in one
    prompt.
  - The dispatcher waits (polls GET /v1/tasks/{id}) for each task to reach a
    terminal state before starting the next. "Sequential" is enforced by
    the dispatcher's own control flow, not by asking the model nicely.
  - The skill itself (SKILL.md Step 1) independently refuses to run if its
    prompt looks like it's being asked to cover more than one project. That
    is a belt-and-suspenders check for the case where this script is bypassed
    and someone invokes the skill directly with a bad prompt. It should
    never actually fire if this dispatcher is used correctly.

SETUP
  pip install requests
  export MANUS_API_KEY=...          (Manus dashboard -> Settings -> API)
  Fill in CRM_SHEET_ID / CRM_TAB below if they differ from defaults.

USAGE
  python execution_cos_dispatcher.py --dry-run     # list eligible projects, launch nothing
  python execution_cos_dispatcher.py               # real run, sequential
  python execution_cos_dispatcher.py --limit 3      # sanity-check on 3 projects first

This script reads the CRM via the same Google Drive/Sheets access the skill
uses (left as a pluggable function below — wire it to whatever the
Astrolab environment already uses to read Sheets; kept separate from the
Manus API calls so the dispatch logic is easy to audit independently of
how project discovery works).
===============================================================================
"""

import argparse
import os
import sys
import time
from dataclasses import dataclass

import requests

MANUS_API_BASE = "https://api.manus.ai/v1"
MANUS_API_KEY = os.environ.get("MANUS_API_KEY")

CRM_SHEET_ID = "1xhcUpvdnNlkL85zYH_v50Bqk_XDO-hazeMhWAwBulM0"
CRM_TAB = "Pilots"
# Blocklist, not allowlist: "all pilots except Completed or Lost" per
# Tomás's instruction 2026-08-07. Deliberately NOT an enumerated allowlist
# of {"in production", "contract", "specifications", ...} — a stage value
# added to the sheet later (one we've never seen before) is correctly
# INCLUDED by default under this rule, rather than silently dropped
# because it wasn't in a hardcoded allowlist.
EXCLUDED_STAGES = {"completed", "lost"}

POLL_INTERVAL_SECONDS = 20
POLL_TIMEOUT_SECONDS = 60 * 30   # 30 min ceiling per project; tune to reality
TERMINAL_STATUSES = {"completed", "failed", "stopped"}
# A task that was JUST created can 404 on its first status check for a few
# seconds while Manus finishes indexing it — this is not the same as the
# task ID being wrong. Confirmed in production 2026-08-07: create_task
# returned a valid task_id, and the very next call to get_task_status on
# that same ID 404'd immediately. Give it this long before treating a 404
# as a real failure.
PROPAGATION_GRACE_SECONDS = 30
PROPAGATION_RETRY_INTERVAL_SECONDS = 3


@dataclass
class Project:
    al_id: str
    name: str
    stage: str


def fetch_eligible_projects() -> list[Project]:
    """
    Reads the CRM via the Google Sheets API using a service account.

    SETUP (one-time, ~10 minutes):
      1. console.cloud.google.com -> select/create a project
      2. APIs & Services -> Library -> enable "Google Sheets API"
      3. APIs & Services -> Credentials -> Create Credentials -> Service Account
      4. Open the new service account -> Keys -> Add Key -> JSON. Download it.
      5. Open the "Pipeline Overview — AstroLab CRM" sheet in your browser -> Share ->
         paste the service account's email (looks like
         xxx@yyy.iam.gserviceaccount.com, visible in the JSON file and in
         the Cloud Console) -> give it Viewer access.
      6. Save the downloaded JSON file somewhere private, e.g.
         service_account.json next to this script (or set
         GOOGLE_APPLICATION_CREDENTIALS to its path).

    Adjust COL_AL_ID / COL_NAME / COL_STAGE below if your sheet's column
    order differs — this reads by column LETTER, not by header name, since
    that's simpler to get right on the first try. Open the sheet once and
    confirm which letter each field is actually in before running for real.
    """
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    CREDS_PATH = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    COL_AL_ID = "A"       # e.g. "AL-2026-012"
    COL_STAGE = "D"       # "In Production" / "Completed" / "Contract" / etc
    COL_FOLDER_URL = "E"  # Google Drive folder link for the project
    # Row 1 is a warning banner, row 2 is blank, row 3 is the real header.
    # Data starts at row 4. Confirmed 2026-08-07 against the live sheet —
    # do not "simplify" this back to row 2 without re-checking the sheet.
    RANGE = f"{CRM_TAB}!{COL_AL_ID}4:{COL_FOLDER_URL}1000"

    creds = service_account.Credentials.from_service_account_file(
        CREDS_PATH, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = build("sheets", "v4", credentials=creds)
    result = sheets.spreadsheets().values().get(
        spreadsheetId=CRM_SHEET_ID, range=RANGE).execute()

    # Column indices within each returned row, relative to COL_AL_ID = index 0
    IDX_AL_ID, IDX_STAGE, IDX_FOLDER_URL = 0, 3, 4

    projects = []
    for row in result.get("values", []):
        if len(row) <= IDX_STAGE:
            continue  # row doesn't even reach the stage column, skip
        al_id = row[IDX_AL_ID].strip()
        stage = row[IDX_STAGE].strip().lower()
        folder_url = row[IDX_FOLDER_URL].strip() if len(row) > IDX_FOLDER_URL else ""
        if not al_id.startswith("AL-") or stage in EXCLUDED_STAGES:
            continue
        # No reliable "name" column confirmed yet, so use the folder URL as
        # the human-readable identifier for logs. The dispatcher only needs
        # the AL ID to build an isolated prompt (see build_prompt below) —
        # `name` here is for your own readability in the console output,
        # not used to select or scope anything.
        projects.append(Project(al_id=al_id, name=folder_url or "(no folder link)", stage=stage))
    return projects


def build_prompt(project: Project) -> str:
    """
    The entire isolation guarantee lives in this function containing exactly
    one AL ID. Do not template this to accept a list.
    """
    return (
        f"Run the execution-cos skill for project {project.al_id} "
        f"({project.name}) only. Do not reference, summarise, or ingest "
        f"context from any other AL project. If you cannot determine a "
        f"single unambiguous project from this instruction, stop and say so "
        f"rather than guessing or covering more than one."
    )


def create_task(prompt: str) -> str:
    resp = requests.post(
        f"{MANUS_API_BASE}/tasks",
        headers={"API_KEY": MANUS_API_KEY, "Content-Type": "application/json"},
        json={"prompt": prompt, "agentProfile": "manus-1.6"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["task_id"]


def get_task_status(task_id: str) -> dict:
    resp = requests.get(
        f"{MANUS_API_BASE}/tasks/{task_id}",
        headers={"API_KEY": MANUS_API_KEY},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def wait_for_completion(task_id: str, al_id: str) -> str:
    """Poll until the task reaches a terminal state. This is what makes the
    run sequential: the next create_task() call does not happen until this
    returns.

    A 404 in the first PROPAGATION_GRACE_SECONDS after task creation is
    treated as "not indexed yet, not really missing" and retried on a short
    interval. A 404 that persists past that window is a real problem (wrong
    ID, task deleted, account mismatch) and gets raised rather than looped
    on forever."""
    created_at = time.time()
    deadline = created_at + POLL_TIMEOUT_SECONDS
    while time.time() < deadline:
        try:
            data = get_task_status(task_id)
        except requests.HTTPError as e:
            if (e.response is not None and e.response.status_code == 404
                    and time.time() - created_at < PROPAGATION_GRACE_SECONDS):
                print(f"  [{al_id}] task not indexed yet (404), retrying "
                      f"in {PROPAGATION_RETRY_INTERVAL_SECONDS}s...")
                time.sleep(PROPAGATION_RETRY_INTERVAL_SECONDS)
                continue
            raise  # past the grace window, or not a 404 — this is real
        status = data.get("status", "unknown")
        if status in TERMINAL_STATUSES:
            return status
        time.sleep(POLL_INTERVAL_SECONDS)
    print(f"  [{al_id}] TIMEOUT after {POLL_TIMEOUT_SECONDS}s waiting on {task_id} "
          f"— treating as failed, moving on.")
    return "timeout"


def run(dry_run: bool, limit: int | None) -> None:
    if not MANUS_API_KEY and not dry_run:
        sys.exit("MANUS_API_KEY not set. Export it or use --dry-run.")

    projects = fetch_eligible_projects()
    if limit:
        projects = projects[:limit]

    print(f"{len(projects)} eligible projects (all stages except {EXCLUDED_STAGES}).")
    if dry_run:
        print("DRY RUN — no tasks will be created.\n")
        for p in projects:
            print(f"  would dispatch: {p.al_id}  {p.name}  [{p.stage}]")
        return

    results = []
    for i, p in enumerate(projects, 1):
        print(f"\n[{i}/{len(projects)}] dispatching {p.al_id} ({p.name})")
        prompt = build_prompt(p)
        try:
            task_id = create_task(prompt)
        except requests.HTTPError as e:
            print(f"  FAILED to create task: {e}")
            results.append((p.al_id, "create_failed"))
            continue

        print(f"  task {task_id} created — waiting for completion "
              f"before dispatching the next project")
        status = wait_for_completion(task_id, p.al_id)
        print(f"  {p.al_id} -> {status}")
        results.append((p.al_id, status))

    print("\n" + "=" * 60)
    print("SUMMARY")
    for al_id, status in results:
        print(f"  {al_id:16s} {status}")
    failed = [a for a, s in results if s not in ("completed",)]
    if failed:
        print(f"\n{len(failed)} project(s) need attention: {', '.join(failed)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="List eligible projects without dispatching")
    ap.add_argument("--limit", type=int, default=None,
                     help="Only dispatch the first N projects (sanity check)")
    args = ap.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
