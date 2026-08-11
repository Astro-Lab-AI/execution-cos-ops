#!/usr/bin/env python3
"""
verify_noop_gate.py
===============================================================================
Runs the full dispatch -> inspect -> verify cycle by itself, so nobody has to
manually paste GitHub Actions logs back and forth to check whether the no-op
cost gate (SKILL.md Step 4) actually fired correctly.

WHAT THIS CHECKS, SPECIFICALLY: the exact violation caught on 2026-08-10 —
a run whose own summary says "this was a no-op, nothing new" but which
still called batchUpdate and rewrote the document anyway. That's not a
generic "did it go well" check; it's a targeted detector for one concrete,
previously-confirmed failure mode. A project that legitimately found new
content and rewrote for real is NOT a violation — this script tells the
two apart by looking for both signals together, not just one.

This runs entirely locally, importing execution_cos_dispatcher.py directly
rather than going through GitHub Actions — same credentials, same script,
just no web UI round-trip and no manual log-reading required.

SETUP (once, if not already done for the dispatcher)
  Same folder as execution_cos_dispatcher.py. Needs:
    - service_account.json (or whatever GOOGLE_APPLICATION_CREDENTIALS points to)
    - MANUS_API_KEY exported in your shell

USAGE
  python verify_noop_gate.py --limit 5
  python verify_noop_gate.py --limit 5 --dry-run   # just show who'd be dispatched
"""

import argparse
import importlib.util
import os
import re
import sys

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "execution_cos_dispatcher.py")

# Signals used together, not separately — a project can legitimately say
# "no new content" (correct no-op) OR legitimately call batchUpdate (real
# new content, real rewrite). Only BOTH together, on the same task, is the
# violation this script exists to catch.
NO_OP_CLAIM_PATTERNS = [
    r"no[\s-]?op\b", r"no new (content|context|files|emails|messages)",
    r"no changes (since|found)", r"nothing new", r"already ingested",
]
REWRITE_EVIDENCE_PATTERNS = [
    r"batchUpdate", r"clear the document", r"Run the Brain update script",
    r"Write script to update the Project_Brain",
    r"Update and write the Project Brain document\[doing\]",
]


def load_dispatcher():
    spec = importlib.util.spec_from_file_location("dispatcher", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def classify_transcript(text: str) -> dict:
    """Returns which no-op-claim and rewrite-evidence patterns matched, and
    the resulting classification. Does NOT just count matches — reports
    which specific strings matched, so the verdict is auditable rather than
    a black box."""
    claims = [p for p in NO_OP_CLAIM_PATTERNS if re.search(p, text, re.I)]
    rewrites = [p for p in REWRITE_EVIDENCE_PATTERNS if re.search(p, text, re.I)]

    if claims and rewrites:
        verdict = "VIOLATION"
    elif claims and not rewrites:
        verdict = "CLEAN_NOOP"
    elif rewrites and not claims:
        verdict = "REAL_REWRITE"
    else:
        verdict = "UNCLEAR"
    return {"verdict": verdict, "claims_matched": claims,
            "rewrites_matched": rewrites}


def run(limit: int, dry_run: bool):
    disp = load_dispatcher()

    if not disp.MANUS_API_KEY and not dry_run:
        sys.exit("MANUS_API_KEY not set.")

    projects = disp.fetch_eligible_projects()[:limit]
    print(f"{len(projects)} projects for this cycle: "
          f"{', '.join(p.al_id for p in projects)}\n")

    if dry_run:
        return

    print("Resolving connectors and skill...")
    connector_ids = disp.lookup_connector_ids(disp.REQUIRED_CONNECTOR_NAMES)
    skill_id = disp.lookup_skill_id(disp.REQUIRED_SKILL_NAME)

    results = []
    for i, p in enumerate(projects, 1):
        print(f"\n[{i}/{len(projects)}] dispatching {p.al_id}...")
        prompt = disp.build_prompt(p)
        try:
            task_id = disp.create_task(prompt, connector_ids, skill_id)
        except Exception as e:
            print(f"  FAILED to create task: {e}")
            results.append({"al_id": p.al_id, "status": "create_failed"})
            continue

        status = disp.wait_for_completion(task_id, p.al_id)
        print(f"  {p.al_id} -> {status}")

        row = {"al_id": p.al_id, "task_id": task_id, "status": status}
        if status == "stopped":
            print(f"  fetching transcript for {p.al_id}...")
            try:
                messages = disp.get_task_transcript(task_id)
                text = disp.format_transcript(messages)
                classification = classify_transcript(text)
                row.update(classification)
                print(f"  verdict: {classification['verdict']}")
            except Exception as e:
                row["verdict"] = "TRANSCRIPT_FETCH_FAILED"
                print(f"  could not fetch/classify transcript: {e}")

            try:
                detail = disp.get_task_detail(task_id)
                row["credits"] = detail.get("credit_usage")
            except Exception:
                row["credits"] = None

        results.append(row)

    print("\n" + "=" * 70)
    print("VERIFICATION REPORT")
    print("=" * 70)
    for r in results:
        line = f"  {r['al_id']:16s} {r.get('status', '?'):12s}"
        if "credits" in r and r["credits"] is not None:
            line += f" {r['credits']:>4} cr "
        line += f" {r.get('verdict', '(no transcript)')}"
        print(line)

    violations = [r for r in results if r.get("verdict") == "VIOLATION"]
    clean_noops = [r for r in results if r.get("verdict") == "CLEAN_NOOP"]
    real_rewrites = [r for r in results if r.get("verdict") == "REAL_REWRITE"]

    print(f"\n  {len(clean_noops)} clean no-ops (gate worked correctly)")
    print(f"  {len(real_rewrites)} real rewrites (genuinely new content, expected)")
    print(f"  {len(violations)} VIOLATIONS (claimed no-op, rewrote anyway)")

    if violations:
        print("\n  !! GATE VIOLATIONS DETECTED:")
        for r in violations:
            print(f"     {r['al_id']}  task={r['task_id']}")
            print(f"       claimed no-op via: {r['claims_matched']}")
            print(f"       but rewrote via:   {r['rewrites_matched']}")
        print("\n  The no-op cost gate is not being respected. Do not trust")
        print("  credit projections until this is fixed — a project that")
        print("  says 'nothing changed' and rewrites anyway means every")
        print("  'cheap' run may still be paying full rewrite cost.")
        sys.exit(1)
    else:
        print("\n  No violations found in this batch. (Note: this checks the")
        print("  batch that actually ran, not a proof the gate can never")
        print("  fail — rerun periodically, especially after any skill edit.)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(limit=args.limit, dry_run=args.dry_run)
