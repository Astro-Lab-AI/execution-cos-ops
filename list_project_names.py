#!/usr/bin/env python3
"""
list_project_names.py
===============================================================================
Read-only diagnostic: prints the CRM sheet's real header row plus, for every
currently-eligible AL ID (same eligibility rule as execution_cos_dispatcher.py
-- every Stage except Completed/Lost), whatever the sheet holds in each
column next to it. The dispatcher itself never reads a "name" column -- its
Project.name is just set equal to the AL ID -- so this exists purely to let a
human validate the eligible-project list by client name before a real
dispatch, not to change any dispatcher behavior.

Read-only: uses the same Sheets readonly scope already granted to the
service account. Does not call the Manus API and does not dispatch anything.

SETUP / USAGE: same as execution_cos_dispatcher.py.
  python list_project_names.py
"""

import os
import sys

from google.oauth2 import service_account
from googleapiclient.discovery import build

CRM_SHEET_ID = "1xhcUpvdnNlkL85zYH_v50Bqk_XDO-hazeMhWAwBulM0"
CRM_TAB = "Pilots"
# Kept in sync with execution_cos_dispatcher.py's INCLUDED_STAGES -- this
# script exists to preview what a real dispatch would hit, so it must use
# the same eligibility rule, not an independent guess at one.
INCLUDED_STAGES = {"in production"}
COL_AL_ID = "A"
COL_STAGE = "D"
# Row 1 is a warning banner, row 2 is blank, row 3 is the real header, data
# starts row 4 -- same layout execution_cos_dispatcher.py already documents.
HEADER_RANGE = f"{CRM_TAB}!A3:Z3"
DATA_RANGE = f"{CRM_TAB}!A4:Z1000"


def main():
    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "service_account.json")
    creds = service_account.Credentials.from_service_account_file(
        creds_path, scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"])
    sheets = build("sheets", "v4", credentials=creds)

    header_resp = sheets.spreadsheets().values().get(
        spreadsheetId=CRM_SHEET_ID, range=HEADER_RANGE).execute()
    headers = header_resp.get("values", [[]])[0]
    print("Column headers (row 3):")
    for i, h in enumerate(headers):
        col_letter = chr(ord("A") + i)
        print(f"  {col_letter}: {h}")
    print()

    data_resp = sheets.spreadsheets().values().get(
        spreadsheetId=CRM_SHEET_ID, range=DATA_RANGE).execute()
    rows = data_resp.get("values", [])

    idx_al_id = ord(COL_AL_ID) - ord("A")
    idx_stage = ord(COL_STAGE) - ord("A")

    print(f"Eligible projects (stage in {INCLUDED_STAGES}), full row:")
    count = 0
    for row in rows:
        if len(row) <= idx_stage:
            continue
        al_id = (row[idx_al_id] if len(row) > idx_al_id else "").strip()
        stage = (row[idx_stage] if len(row) > idx_stage else "").strip().lower()
        if not al_id.startswith("AL-") or stage not in INCLUDED_STAGES:
            continue
        count += 1
        cells = " | ".join(f"{chr(ord('A')+i)}={v}" for i, v in enumerate(row))
        print(f"  {cells}")

    print(f"\n{count} eligible projects listed above.")


if __name__ == "__main__":
    main()
