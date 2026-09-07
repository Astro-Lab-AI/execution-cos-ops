#!/usr/bin/env python3
"""
send_test_digest.py
===============================================================================
One-off test tool: builds and sends a REAL digest email using transcripts
from tasks that ALREADY completed in a prior real dispatch run, instead of
dispatching a brand-new batch of 14 tasks just to test email formatting.
Read-only against Manus (fetches existing transcripts), spends zero new
Manus credits, dispatches nothing.

Hardcodes the task IDs from the 2026-09-07 07:36 UTC run (the first fully
clean run after the Gmail connector was fixed) -- edit TASK_IDS below to
test against a different run if needed.

USAGE
  python send_test_digest.py
"""

import importlib.util
import os
import sys

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "execution_cos_dispatcher.py")

# (al_id, name, task_id) -- from run 34086403145, in dispatch order.
TASK_IDS = [
    ("AL-2026-012", "WiderProperty", "fVeT3fYDAqPY4Sa7eJR3cz"),
    ("AL-2026-024", "ARVAD", "FWHcqFesg8DGWzRpm5NQsC"),
    ("AL-2026-027", "NUMERIC", "23ZMBByPcw35EpTrbdUcHz"),
    ("AL-2026-033", "APDC", "MTUcJ9Cop7tSLh7FWymybz"),
    ("AL-2026-034", "AskHermis", "jPPs2e76LuKKpEAiuR3x3T"),
    ("AL-2026-047", "Relocate Now", "bzLpFXgRAtPmk2Yvs9WnS3"),
    ("AL-2026-056", "Jardineiros Timesheet", "a8urY5AjbeFCKeXrENXcs9"),
    ("AL-2026-057", "Numeric 2", "R8dFiWeqrUUTHYHmj6tRtr"),
    ("AL-2026-062", "BackOffice UFL", "5C8y5H5SYvyFF9sCxuejyD"),
    ("AL-2026-077", "Automated Content Creation and Social Media Post Scheduling", "YRKC924JCZuYc5DruCMPE6"),
    ("AL-2026-079", "SDG Monitoring and Reporting", "oVzJ3EErHxZKoEW5SJQhKM"),
    ("AL-2026-088", "Portas do Sol - Eventos", "cXETVEVmf42x6s48RRWBD7"),
    ("AL-2026-090", "Portas do Sol - Apartamentos", "c9kN223Ud69D6EWSQVhR4G"),
    ("AL-2026-108", "STGT/KENOR", "HVf5gPHm8facKLpivNB3NU"),
]


def load_dispatcher():
    spec = importlib.util.spec_from_file_location("dispatcher", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    disp = load_dispatcher()
    if not disp.MANUS_API_KEY:
        sys.exit("MANUS_API_KEY not set.")

    results = []
    for al_id, name, task_id in TASK_IDS:
        print(f"fetching transcript for {al_id}...")
        messages = disp.get_task_transcript(task_id)
        summary = disp.get_final_assistant_message(messages) or "(no summary produced)"
        bucket = disp.classify_for_digest(summary)
        results.append({"al_id": al_id, "name": name, "status": "stopped",
                         "wall_seconds": 0, "summary": summary,
                         "digest_bucket": bucket})
        print(f"  bucket: {bucket}")

    subject, text_body, html_body = disp.build_digest_email(results, run_seconds=0)
    subject = "[TEST] " + subject
    disp.send_digest_email(subject, text_body, html_body)


if __name__ == "__main__":
    main()
