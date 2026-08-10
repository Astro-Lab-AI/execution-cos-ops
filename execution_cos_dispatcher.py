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
  - Each call to create_task() hits POST https://api.manus.ai/v2/task.create
    with a prompt containing exactly one AL ID. Per Manus's API, this creates
    a brand-new task = a brand-new agent session with no memory of any other
    task. There is no code path in this script that puts two AL IDs in one
    prompt.
  - The dispatcher waits (polls GET /v2/task.listMessages) for each task to
    reach a terminal state before starting the next. "Sequential" is
    enforced by the dispatcher's own control flow, not by asking the model
    nicely.
  - The skill itself (SKILL.md Step 1) independently refuses to run if its
    prompt looks like it's being asked to cover more than one project. That
    is a belt-and-suspenders check for the case where this script is bypassed
    and someone invokes the skill directly with a bad prompt. It should
    never actually fire if this dispatcher is used correctly.

WHY v2, NOT v1 (confirmed 2026-08-07): v1's task-creation call has no way to
tell a task which connectors it already has. Every v1-created task fell back
to showing a "connectors need to be enabled" card and sat at "waiting for
user" forever — even after Gmail/Google Workspace were authorized at the
ACCOUNT level in Manus settings, because that authorization and per-TASK
connector attachment are two different things. v2's task.create accepts a
`connectors` array (and `force_skills`) directly, so a correctly-built task
never asks. This script resolves connector/skill IDs once via
connector.list / skill.list and attaches them to every dispatched task.

PREREQUISITE — one-time, done ONCE for the whole account, not per task:
  In Manus -> Settings -> Connectors, connect and authorize Google
  Workspace and Gmail for the account this API key belongs to. This script
  will refuse to run (not silently proceed) if either is missing — see
  lookup_connector_ids().

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
import re
import sys
import time
from dataclasses import dataclass

import requests

MANUS_API_BASE = "https://api.manus.ai/v2"
MANUS_API_KEY = os.environ.get("MANUS_API_KEY")

# Names to match against connector.list / skill.list. Confirmed against the
# Manus v2 API docs 2026-08-07: v1 (previously used here) has no way to
# specify which connectors a task should already have, so every task fell
# back to asking a human to click through a connector-enable card — that
# card is what stalled every dispatched task, even after the account-level
# OAuth for Gmail/Workspace was completed. v2's task.create accepts
# `connectors` and `force_skills` directly, which removes that prompt
# entirely because the task already has what it needs at creation time.
REQUIRED_CONNECTOR_NAMES = ["Google Workspace", "Gmail"]
REQUIRED_SKILL_NAME = "execution-cos"

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
# v2's real status vocabulary (confirmed against the task-lifecycle docs):
#   running -> keep polling      stopped -> done, success
#   error   -> done, failed      waiting -> needs a human (see below)
TERMINAL_STATUSES = {"stopped", "error"}
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
    folder_url: str = ""   # real Drive URL, resolved via the cell's hyperlink
    folder_id: str = ""    # extracted from folder_url


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

    # BUG FIXED 2026-08-10: this used to call spreadsheets().values().get(),
    # which only returns each cell's DISPLAYED text. Column E's cells all
    # display the literal string "Open Drive" — that's the hyperlink's
    # link text, not its target. That meaningless string was going straight
    # into every dispatched prompt as the project's "location," so the
    # agent had nothing to work with and had to search Drive by name
    # instead. WiderProperty's real folder is named "12. WiderProperty"
    # (an old numbering convention with no "AL-2026-012" in it at all), so
    # that search failed, and the agent created a BRAND NEW duplicate
    # project folder rather than finding the real one — confirmed in Drive
    # 2026-08-10, folder "AL-2026-012 — WiderProperty" created that same
    # day, sitting in a different shared drive entirely.
    #
    # spreadsheets().get() with this fields mask returns each cell's real
    # hyperlink TARGET (cell.hyperlink), not its display text.
    resp = sheets.spreadsheets().get(
        spreadsheetId=CRM_SHEET_ID,
        ranges=[RANGE],
        fields="sheets.data.rowData.values(formattedValue,hyperlink)"
    ).execute()
    rows = resp.get("sheets", [{}])[0].get("data", [{}])[0].get("rowData", [])

    IDX_AL_ID, IDX_STAGE, IDX_FOLDER_URL = 0, 3, 4

    missing_folder = []
    projects = []
    for row_data in rows:
        cells = row_data.get("values", [])
        if len(cells) <= IDX_STAGE:
            continue  # row doesn't even reach the stage column, skip
        al_id = (cells[IDX_AL_ID].get("formattedValue") or "").strip()
        stage = (cells[IDX_STAGE].get("formattedValue") or "").strip().lower()
        if not al_id.startswith("AL-") or stage in EXCLUDED_STAGES:
            continue

        folder_url = ""
        if len(cells) > IDX_FOLDER_URL:
            cell = cells[IDX_FOLDER_URL]
            # The real link target, NOT cell.get("formattedValue") which
            # would just be "Open Drive" again.
            folder_url = cell.get("hyperlink") or ""

        folder_id = ""
        if folder_url:
            m = re.search(r"/folders/([a-zA-Z0-9_-]+)", folder_url)
            if m:
                folder_id = m.group(1)

        if not folder_url:
            # HARDENED 2026-08-10 per Tomás: "It's impossible that a project
            # doesn't have a folder. If that is the case then it should
            # throw an error. Not hallucinate." Originally this only
            # printed a warning and skipped the one project — too soft.
            # Every project in this sheet is expected to have a real Drive
            # link, so hitting this means something is systemically
            # broken (wrong range, a sheet edit, a parsing bug), not that
            # this one project is a legitimate exception. The correct
            # response is to stop the ENTIRE run and force a human to look,
            # not quietly continue with 40 other projects while one is
            # silently dropped.
            missing_folder.append(al_id)
            continue

        projects.append(Project(al_id=al_id, name=al_id, stage=stage,
                                folder_url=folder_url, folder_id=folder_id))

    if missing_folder:
        sys.exit(
            f"ABORTED: {len(missing_folder)} eligible project(s) have no "
            f"resolvable folder hyperlink in column {COL_FOLDER_URL}: "
            f"{', '.join(missing_folder)}. This should never happen — every "
            f"project is expected to have a real Drive link. Fix the sheet "
            f"(or this script's column mapping) before running again. "
            f"Refusing to dispatch ANY project this run rather than proceed "
            f"with some projects silently missing.")

    return projects


def build_prompt(project: Project) -> str:
    """
    The entire isolation guarantee lives in this function containing exactly
    one AL ID. Do not template this to accept a list.

    FIXED 2026-08-10: now embeds the project's actual, resolved Drive folder
    URL and forbids creating a new one. Before this fix, the prompt only
    said the AL ID plus a decorative "(Open Drive)" label with no real
    location in it, so the agent had to search Drive by name for something
    matching the AL ID — and for projects using an older folder-naming
    convention (no AL ID in the name), that search failed and the agent
    created a brand new duplicate folder instead of finding the real one.
    """
    return (
        f"Run the execution-cos skill for project {project.al_id} only. "
        f"The project's Drive folder is exactly this one, do not search "
        f"Drive by name for it: {project.folder_url} "
        f"(folder ID: {project.folder_id}). Operate only inside this "
        f"folder. Under no circumstances create a new project folder, "
        f"even if a name-based search would find nothing — this exact "
        f"folder is authoritative. "
        f"Do not reference, summarise, or ingest context from any other "
        f"AL project. If you cannot determine a single unambiguous project "
        f"from this instruction, stop and say so rather than guessing or "
        f"covering more than one."
    )


def _manus_headers():
    return {"x-manus-api-key": MANUS_API_KEY, "Content-Type": "application/json"}


def lookup_connector_ids(names):
    """GET /v2/connector.list, match by name (case-insensitive substring).
    Raises if any required connector is missing — better to fail loudly
    here than dispatch 40 tasks that all hit the same missing-connector
    wall the old v1 code silently walked into."""
    resp = requests.get(f"{MANUS_API_BASE}/connector.list",
                         headers=_manus_headers(), timeout=30)
    resp.raise_for_status()
    available = resp.json().get("data", [])
    ids, missing = [], []
    for name in names:
        match = next((c for c in available
                      if name.lower() in c.get("name", "").lower()), None)
        if match:
            ids.append(match["id"])
        else:
            missing.append(name)
    if missing:
        sys.exit(f"Required connector(s) not found in this account: {missing}. "
                 f"Connect them in Manus -> Settings -> Connectors first "
                 f"(account-level OAuth, not a per-task thing).")
    return ids


def lookup_skill_id(name):
    """GET /v2/skill.list, match by name. Returns None (not fatal) if not
    found — force_skills is a reliability improvement, not a hard
    requirement; the task can still run on whatever's default-enabled."""
    resp = requests.get(f"{MANUS_API_BASE}/skill.list",
                         headers=_manus_headers(), timeout=30)
    resp.raise_for_status()
    for s in resp.json().get("data", []):
        if name.lower() in s.get("name", "").lower():
            return s["id"]
    print(f"  WARNING: skill '{name}' not found via skill.list — task will "
          f"rely on whatever's default-enabled instead of force_skills.")
    return None


def create_task(prompt: str, connector_ids: list, skill_id) -> str:
    body = {
        "message": {
            "content": prompt,
            "connectors": connector_ids,
        },
        # False is already the v2 default, but set explicitly: this is a
        # headless run, nobody is there to answer a clarifying question.
        "interactive_mode": False,
        # Runs identically, just doesn't show up in the Manus web app's
        # task list — this is background automation, not something Tomás
        # needs to see 41 entries of. Still fully reachable via task_url in
        # the create response and via task.listMessages for polling.
        "hide_in_task_list": True,
    }
    if skill_id:
        body["message"]["force_skills"] = [skill_id]
    resp = requests.post(f"{MANUS_API_BASE}/task.create",
                          headers=_manus_headers(), json=body, timeout=30)
    resp.raise_for_status()
    return resp.json()["task_id"]


def get_task_detail(task_id: str) -> dict:
    """GET /v2/task.detail — richer than the status_update events, has
    credit_usage and precise created_at/updated_at timestamps. Called once
    per task AFTER it reaches a terminal state, purely for evaluation
    reporting — never used to decide control flow, so a failure here
    should not derail the run."""
    resp = requests.get(f"{MANUS_API_BASE}/task.detail",
                         params={"task_id": task_id},
                         headers=_manus_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json().get("task", {})


def get_task_status(task_id: str) -> dict:
    """Reads the most recent status_update event via task.listMessages and
    normalises it to {"status": ..., "detail": ...}. status is one of
    'running', 'stopped' (=completed), 'error' (=failed), 'waiting'
    (=needs a human — see below), or 'unknown' if no status event yet.

    BUG FIXED 2026-08-10: this used to read resp.json().get("data", []).
    The real v2 response puts the event list under "messages", not "data".
    "data" doesn't exist in this response at all, so this was silently
    reading an empty list on every single call, every project, every run —
    meaning get_task_status could NEVER see a terminal status, no matter
    how fast the real Manus task actually finished. Every "timeout" result
    reported so far may have been a real success that this bug simply
    never detected, sitting idle until the 30-minute ceiling fired instead
    of returning the moment the task actually completed."""
    resp = requests.get(
        f"{MANUS_API_BASE}/task.listMessages",
        params={"task_id": task_id, "order": "desc", "limit": 10},
        headers=_manus_headers(), timeout=30)
    resp.raise_for_status()
    for event in resp.json().get("messages", []):
        if event.get("type") == "status_update":
            su = event["status_update"]
            return {"status": su.get("agent_status", "unknown"),
                    "detail": su.get("status_detail")}
    return {"status": "unknown", "detail": None}


def get_task_transcript(task_id: str) -> list:
    """Fetches the FULL event history for a task, paginated,
    chronological, with verbose=true so tool calls and the agent's own
    reasoning are included — not just the five basic event types. This is
    for diagnosing what a task actually did, not for control flow.

    Returns a flat list of message dicts in the same shape the API gives
    them, oldest first."""
    all_messages, cursor = [], None
    while True:
        params = {"task_id": task_id, "order": "asc", "limit": 200,
                   "verbose": "true"}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(f"{MANUS_API_BASE}/task.listMessages",
                             params=params, headers=_manus_headers(),
                             timeout=30)
        resp.raise_for_status()
        body = resp.json()
        all_messages.extend(body.get("messages", []))
        if not body.get("has_more"):
            break
        cursor = body.get("next_cursor")
        if not cursor:
            break  # has_more=true but no cursor given — stop rather than loop forever
    return all_messages


def format_transcript(messages: list) -> str:
    """Turns the raw event list into a readable line-per-event summary:
    what tool was used and its result, the agent's own stated reasoning,
    plan step transitions, and any errors — in chronological order with
    relative timestamps. This is what answers 'was it doing real distinct
    work, or stuck repeating something' without anyone needing to watch
    the Manus replay UI."""
    if not messages:
        return "(no messages returned for this task)"
    t0 = messages[0].get("timestamp", 0) / 1000.0
    lines = []
    for m in messages:
        t = (m.get("timestamp", 0) / 1000.0) - t0
        mtype = m.get("type", "?")
        prefix = f"[+{t:6.0f}s] {mtype:22s}"
        if mtype == "tool_used":
            tu = m.get("tool_used", {})
            lines.append(f"{prefix} {tu.get('tool','?'):16s} "
                         f"[{tu.get('status','?')}] {tu.get('brief','')}")
        elif mtype == "explanation":
            lines.append(f"{prefix} {m.get('explanation', {}).get('content', '')}")
        elif mtype == "status_update":
            su = m.get("status_update", {})
            lines.append(f"{prefix} agent_status -> {su.get('agent_status','?')}  "
                         f"{su.get('brief','')}")
        elif mtype == "assistant_message":
            content = m.get("assistant_message", {}).get("content", "")
            lines.append(f"{prefix} {content[:200]}")
        elif mtype == "error_message":
            em = m.get("error_message", {})
            lines.append(f"{prefix} !! {em.get('error_type','')}: {em.get('content','')}")
        elif mtype == "plan_update":
            steps = m.get("plan_update", {}).get("steps", [])
            summary = ", ".join(f"{s.get('title','?')}[{s.get('status','?')}]"
                                for s in steps)
            lines.append(f"{prefix} {summary}")
        elif mtype == "new_plan_step":
            lines.append(f"{prefix} + {m.get('new_plan_step', {}).get('title','')}")
        else:
            lines.append(prefix)
    return "\n".join(lines)


def wait_for_completion(task_id: str, al_id: str) -> str:
    """Poll until the task reaches a terminal state. This is what makes the
    run sequential: the next create_task() call does not happen until this
    returns.

    A 404 in the first PROPAGATION_GRACE_SECONDS after task creation is
    treated as "not indexed yet, not really missing" and retried on a short
    interval. A 404 that persists past that window is a real problem (wrong
    ID, task deleted, account mismatch) and gets raised rather than looped
    on forever.

    'waiting' is deliberately NOT auto-confirmed here. Some waiting events
    (gmailSendAction, deployAction) have real-world side effects, and the
    skill's own rules already require explicit human confirmation before
    taking action — a headless dispatcher blindly accepting those would
    violate that. If connectors/skill are correctly passed at creation
    (see create_task), 'waiting' should be rare for a read-and-write-a-doc
    task; if it happens anyway, this surfaces it and moves on rather than
    guessing what to click."""
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
        if status == "waiting":
            detail = data.get("detail") or {}
            print(f"  [{al_id}] task is WAITING on a human "
                  f"({detail.get('waiting_for_event_type', 'unknown')}): "
                  f"{detail.get('waiting_description', '')!r}. "
                  f"Not auto-confirming — flagging and moving on.")
            return "waiting"
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

    # Resolved ONCE per script run, not per project — every dispatched task
    # gets the same connector/skill IDs. This is the actual fix for the
    # "waiting for user to click Apply" stall: v2's task.create accepts
    # these directly, so the task never needs to ask.
    print("Resolving required connectors and skill via Manus API v2...")
    connector_ids = lookup_connector_ids(REQUIRED_CONNECTOR_NAMES)
    skill_id = lookup_skill_id(REQUIRED_SKILL_NAME)
    print(f"  connectors: {connector_ids}")
    print(f"  skill: {skill_id or '(not found — using default-enabled skills)'}")

    results = []
    for i, p in enumerate(projects, 1):
        dispatch_started_at = time.time()
        print(f"\n[{i}/{len(projects)}] dispatching {p.al_id} ({p.name})")
        prompt = build_prompt(p)
        try:
            task_id = create_task(prompt, connector_ids, skill_id)
        except requests.HTTPError as e:
            print(f"  FAILED to create task: {e}")
            results.append({"al_id": p.al_id, "status": "create_failed"})
            continue

        print(f"  task {task_id} created — waiting for completion "
              f"before dispatching the next project")
        status = wait_for_completion(task_id, p.al_id)
        wall_seconds = time.time() - dispatch_started_at
        print(f"  {p.al_id} -> {status}  ({wall_seconds:.0f}s)")

        # Evaluation-only: pull credit usage after the task is done. This is
        # never allowed to affect control flow — a metrics fetch failing is
        # not a reason to mark a real, successful task as failed.
        credits = None
        try:
            credits = get_task_detail(task_id).get("credit_usage")
            if credits is not None:
                print(f"  {p.al_id} credit_usage: {credits}")
        except requests.HTTPError as e:
            print(f"  (could not fetch credit_usage for {p.al_id}: {e})")

        results.append({"al_id": p.al_id, "status": status,
                         "wall_seconds": wall_seconds, "credits": credits})

    print("\n" + "=" * 60)
    print("SUMMARY")
    for r in results:
        line = f"  {r['al_id']:16s} {r['status']}"
        if "wall_seconds" in r:
            line += f"  {r['wall_seconds']:.0f}s"
        if r.get("credits") is not None:
            line += f"  {r['credits']} credits"
        print(line)

    completed = [r for r in results if r["status"] == "stopped"]
    if completed:
        times = [r["wall_seconds"] for r in completed]
        creds = [r["credits"] for r in completed if r.get("credits") is not None]
        print(f"\nEVALUATION ({len(completed)} completed successfully):")
        print(f"  wall time   avg {sum(times)/len(times):.0f}s   "
              f"min {min(times):.0f}s   max {max(times):.0f}s   "
              f"total {sum(times):.0f}s ({sum(times)/60:.1f} min)")
        if creds:
            print(f"  credits     avg {sum(creds)/len(creds):.0f}   "
                  f"min {min(creds)}   max {max(creds)}   "
                  f"total {sum(creds)} for this batch of {len(creds)}")
            print(f"  projected full run of 41 projects, at this average: "
                  f"~{(sum(creds)/len(creds))*41:.0f} credits, "
                  f"~{(sum(times)/len(times))*41/60:.0f} min sequential wall time")
        else:
            print("  credits     (not available — check API key permissions "
                  "for task.detail)")

    failed = [r["al_id"] for r in results if r["status"] != "stopped"]
    if failed:
        print(f"\n{len(failed)} project(s) need attention: {', '.join(failed)}")
        # Without this, GitHub Actions reports the whole run as "succeeded"
        # any time the script itself doesn't crash — even if every single
        # project timed out or errored. Confirmed 2026-08-10: a run where
        # AL-2026-012 hit the 30-minute ceiling still showed a green
        # checkmark, because "the script ran without an exception" and
        # "the actual work completed" are different claims. A non-zero
        # exit here is what makes the CI status mean something.
        sys.exit(1)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                     help="List eligible projects without dispatching")
    ap.add_argument("--limit", type=int, default=None,
                     help="Only dispatch the first N projects (sanity check)")
    ap.add_argument("--inspect", metavar="TASK_ID", default=None,
                     help="Print the full event transcript for one existing "
                          "task (tool calls, agent reasoning, status "
                          "changes) and exit. Does not dispatch anything.")
    args = ap.parse_args()
    if args.inspect:
        if not MANUS_API_KEY:
            sys.exit("MANUS_API_KEY not set.")
        messages = get_task_transcript(args.inspect)
        print(f"{len(messages)} events for task {args.inspect}\n")
        print(format_transcript(messages))
    else:
        run(dry_run=args.dry_run, limit=args.limit)
