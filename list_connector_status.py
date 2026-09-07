#!/usr/bin/env python3
"""
list_connector_status.py
===============================================================================
Read-only diagnostic: calls Manus's connector.list directly and prints the
FULL raw response for each of the four connectors this dispatcher requires
(Google Workspace, Gmail, GitHub, Discord) -- not just the id/name pair
lookup_connector_ids() extracts and discards everything else from.

WHY THIS EXISTS: confirmed 2026-09-07, task.detail (already used as a
waiting-state diagnostic in wait_for_completion) carries no per-connector
health/expiry field at all -- just generic task metadata. If Manus's
connector.list response has any status/expiry/needs_reauth field, THIS is
where it would show up, and this is a single lightweight read call rather
than a full ~50-minute dispatch cycle needed just to observe the failure
again.

Does not dispatch anything, does not touch the CRM sheet or any Brain --
purely reads from the Manus API using the same MANUS_API_KEY already used
elsewhere.

USAGE
  python list_connector_status.py
"""

import importlib.util
import os
import sys

MODULE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "execution_cos_dispatcher.py")


def load_dispatcher():
    spec = importlib.util.spec_from_file_location("dispatcher", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def main():
    disp = load_dispatcher()
    if not disp.MANUS_API_KEY:
        sys.exit("MANUS_API_KEY not set.")

    resp = disp.requests.get(f"{disp.MANUS_API_BASE}/connector.list",
                              headers=disp._manus_headers(), timeout=30)
    resp.raise_for_status()
    available = resp.json().get("data", [])

    print(f"{len(available)} connector(s) visible to this API key.\n")
    for name in disp.REQUIRED_CONNECTOR_NAMES:
        match = next((c for c in available
                      if name.lower() in c.get("name", "").lower()), None)
        print(f"=== {name} ===")
        if match:
            for k, v in match.items():
                print(f"  {k}: {v}")
        else:
            print("  NOT FOUND in connector.list")
        print()

    print("--- full raw connector.list for anything not matched above ---")
    matched_ids = {
        c["id"] for name in disp.REQUIRED_CONNECTOR_NAMES
        for c in available if name.lower() in c.get("name", "").lower()
    }
    for c in available:
        if c.get("id") not in matched_ids:
            print(c)


if __name__ == "__main__":
    main()
