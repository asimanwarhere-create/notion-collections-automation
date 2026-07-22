#!/usr/bin/env python3
"""
Collections pipeline automation.

Runs headless on GitHub Actions every 15 minutes. On each run it:

  1. Reads every row of the Notion "Collections" database.
  2. Writes a computed "Overall Status (Auto)" select onto every collection row
     (Live / Ready to launch / In Progress / Killed) so the donut chart can group
     by a real property (charts cannot group by a formula).
  3. Computes the pipeline metric counts and upserts them into the "Pipeline
     Summary" database (keyed by Metric title).
  4. Populates the "Weekly Output by Person" matrix: ensures the current
     Friday-week number column exists (newest-first), then upserts each person's
     weekly Listings / Creatives counts.
  5. Refreshes a "Last updated: <date time>" line at the very top of the
     Executive Overview page.

Everywhere in the pipeline logic, a stage counts as complete when its status is
either "Done" or "Not Needed" (the two exceptions are noted inline). The only
secret needed is a Notion integration token, supplied via NOTION_TOKEN. The
integration must be shared with the Collections DB, Pipeline Summary DB, Weekly
Output DB, and the Executive Overview page.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
NOTION_VERSION = "2022-06-28"

# --- Fixed IDs (not secret) ---------------------------------------------------
COLLECTIONS_DB_ID = "303f675f-38fb-80c0-9eab-ef5f51354d63"   # source: Collections
SUMMARY_DB_ID = "47ea2af1-a825-4302-b2bc-c7ca189d92db"       # target: Pipeline Summary
WEEKLY_DB_ID = "7b9e7c88-785a-4239-827c-da91cd20dee7"        # target: Weekly Output by Person
EXEC_PAGE_ID = "3a3f675f-38fb-81a2-a736-ec260be0a3b8"        # target: Executive Overview page

# Property that the script writes the computed launch status into (a real select
# so the donut chart can group by it). Kept separate from the manual "Overall Status".
AUTO_STATUS_PROP = "Overall Status (Auto)"

# A stage is complete if its status is one of these.
DONE = {"Done", "Not Needed"}

# Marker so we can find (and reuse) the top-of-page "last updated" line each run.
UPDATED_MARKER = "Last updated:"

HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Notion-Version": NOTION_VERSION,
    "Content-Type": "application/json",
}


# --- Notion REST helpers ------------------------------------------------------
def query_all(database_id):
    """Return every page in a database, following pagination."""
    url = f"https://api.notion.com/v1/databases/{database_id}/query"
    results, payload = [], {"page_size": 100}
    while True:
        resp = requests.post(url, headers=HEADERS, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        results.extend(data["results"])
        if not data.get("has_more"):
            return results
        payload["start_cursor"] = data["next_cursor"]


def patch_page(page_id, properties):
    url = f"https://api.notion.com/v1/pages/{page_id}"
    resp = requests.patch(url, headers=HEADERS, json={"properties": properties}, timeout=30)
    resp.raise_for_status()


def status_name(page, prop):
    """Read a status-type property's option name, or '' if unset."""
    p = page["properties"].get(prop, {})
    val = p.get("status")
    return val["name"] if val else ""


def select_name(page, prop):
    """Read a select-type property's option name, or '' if unset."""
    p = page["properties"].get(prop, {})
    val = p.get("select")
    return val["name"] if val else ""


def number_val(page, prop):
    p = page["properties"].get(prop, {})
    return p.get("number")


def people_ids(page, prop):
    """Return the list of person ids on a people-type property."""
    p = page["properties"].get(prop, {})
    return [u["id"] for u in p.get("people", [])]


def date_start(page, prop):
    """Return the ISO start string of a date property, or '' if unset."""
    p = page["properties"].get(prop, {})
    val = p.get("date")
    return val["start"] if val else ""


# --- 1 & 2. Compute per-row Overall Status (Auto) and write it back -----------
def compute_auto_status(page):
    listing = status_name(page, "Listing Status")
    creative = status_name(page, "Creative Tasks Status")
    launch = status_name(page, "Launch Status")
    manual = select_name(page, "Overall Status")

    if launch in DONE:
        return "Live"
    if listing in DONE and creative in DONE:
        return "Ready to launch"
    if manual == "Killed":
        return "Killed"
    return "In Progress"


def write_auto_status(rows):
    """Write the computed select onto each row that needs a change."""
    changed = 0
    for r in rows:
        desired = compute_auto_status(r)
        current = select_name(r, AUTO_STATUS_PROP)
        if current != desired:
            patch_page(r["id"], {AUTO_STATUS_PROP: {"select": {"name": desired}}})
            changed += 1
    return changed


# --- 3. Pipeline Summary metric counts ----------------------------------------
def compute_counts(rows):
    total = len(rows)
    listing_done = creative_done = launched = 0
    not_yet_listed = awaiting_creative = creative_ahead = 0
    ready_not_launched = blocked = 0

    for r in rows:
        listing = status_name(r, "Listing Status")
        creative = status_name(r, "Creative Tasks Status")
        launch = status_name(r, "Launch Status")

        l_done = listing in DONE
        c_done = creative in DONE
        la_done = launch in DONE

        if l_done:
            listing_done += 1
        else:
            not_yet_listed += 1
        if c_done:
            creative_done += 1
        if la_done:
            launched += 1

        if l_done and not c_done:
            awaiting_creative += 1                       # listers ahead; waiting on creatives
        if creative in {"In progress", "Done"} and listing in {"Not started", ""}:
            creative_ahead += 1                          # creatives ahead of listers
        if l_done and c_done and launch not in {"Done", "Blocked"}:
            ready_not_launched += 1                      # both prep stages done, not shipped
        if launch == "Blocked":
            blocked += 1

    # Keys MUST match the "Metric" titles seeded in the Pipeline Summary DB.
    return {
        "Total collections": total,
        "Listing done": listing_done,
        "Creative done": creative_done,
        "Launched": launched,
        "Not yet listed (listers' work left)": not_yet_listed,
        "Awaiting creative (Listing\u2192Creative gap)": awaiting_creative,
        "Creative ahead, not yet listed": creative_ahead,
        "Ready to launch, but not launched yet": ready_not_launched,
        "Blocked launches": blocked,
    }


def summary_page_map():
    """Map each existing Metric title -> its page id in the summary DB."""
    mapping = {}
    for page in query_all(SUMMARY_DB_ID):
        title = page["properties"]["Metric"]["title"]
        name = title[0]["plain_text"] if title else ""
        mapping[name] = page["id"]
    return mapping


def update_metric(page_id, count, now_iso):
    patch_page(page_id, {
        "Count": {"number": count},
        "Last Updated": {"date": {"start": now_iso}},
    })


def update_summary(rows, now_iso):
    counts = compute_counts(rows)
    pages = summary_page_map()
    missing = [m for m in counts if m not in pages]
    if missing:
        print(f"WARNING: no summary row for: {missing}", file=sys.stderr)
    for metric, count in counts.items():
        if metric in pages:
            update_metric(pages[metric], count, now_iso)
            print(f"  {metric}: {count}")


# --- 4. Weekly Output by Person matrix ----------------------------------------
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def friday_window(now):
    """Return (start_date, end_date, label) for the Friday-start week of `now`."""
    days_since_friday = (now.weekday() + 3) % 7   # Mon=0..Sun=6; Friday=4 -> 0
    start = (now - timedelta(days=days_since_friday)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=6, hours=23, minutes=59, seconds=59)
    if start.month == end.month:
        label = f"{MONTHS[start.month - 1]} {start.day}-{end.day}"
    else:
        label = f"{MONTHS[start.month - 1]} {start.day}-{MONTHS[end.month - 1]} {end.day}"
    return start, end, label


def get_database(db_id):
    url = f"https://api.notion.com/v1/databases/{db_id}"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()


def ensure_week_column(db_id, label):
    """Add a number column named `label` if it does not already exist."""
    db = get_database(db_id)
    if label in db["properties"]:
        return
    url = f"https://api.notion.com/v1/databases/{db_id}"
    body = {"properties": {label: {"number": {}}}}
    resp = requests.patch(url, headers=HEADERS, json=body, timeout=30)
    resp.raise_for_status()
    print(f"  added Weekly Output column: {label}")


def in_window(iso, start, end):
    if not iso:
        return False
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return start <= d <= end


def compute_weekly(rows, start, end):
    """Return {person_id: {'Listings': n, 'Creatives': n}} for the window."""
    tally = {}
    for r in rows:
        # Listings: strict "Done" only (Not Needed EXCLUDED for personal credit).
        if status_name(r, "Listing Status") == "Done" and in_window(
                date_start(r, "Listing Ready Date"), start, end):
            for pid in people_ids(r, "Listing Owner"):
                tally.setdefault(pid, {"Listings": 0, "Creatives": 0})["Listings"] += 1
        # Creatives: Creative Ready Date in window (credited to Creative Tasks Owner).
        if in_window(date_start(r, "Creative Ready Date"), start, end):
            for pid in people_ids(r, "Creative Tasks Owner"):
                tally.setdefault(pid, {"Listings": 0, "Creatives": 0})["Creatives"] += 1
    return tally


def weekly_row_map():
    """Map (person_id, metric) -> row page id in the Weekly Output DB."""
    mapping = {}
    for page in query_all(WEEKLY_DB_ID):
        metric = select_name(page, "Metric")
        pids = people_ids(page, "Person")
        for pid in pids:
            mapping[(pid, metric)] = page["id"]
    return mapping


def update_weekly(rows, now):
    start, end, label = friday_window(now)
    ensure_week_column(WEEKLY_DB_ID, label)
    tally = compute_weekly(rows, start, end)
    row_map = weekly_row_map()
    for pid, metrics in tally.items():
        for metric_name, key in (("Listings", "Listings"), ("Creatives", "Creatives")):
            count = metrics[key]
            row_id = row_map.get((pid, metric_name))
            if row_id and count:
                patch_page(row_id, {label: {"number": count}})
    print(f"  weekly window {label}: {sum(sum(v.values()) for v in tally.values())} items")


# --- 5. Top-of-page "last updated" line ---------------------------------------
def block_children(block_id):
    url = f"https://api.notion.com/v1/blocks/{block_id}/children?page_size=100"
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    return resp.json()["results"]


def find_updated_block(page_id):
    """Return the id of the existing 'Last updated:' paragraph, or None."""
    for block in block_children(page_id):
        if block["type"] == "paragraph":
            rt = block["paragraph"].get("rich_text", [])
            text = "".join(t.get("plain_text", "") for t in rt)
            if text.startswith(UPDATED_MARKER):
                return block["id"]
    return None


def updated_paragraph(text):
    return {
        "type": "paragraph",
        "paragraph": {
            "rich_text": [{
                "type": "text",
                "text": {"content": text},
                "annotations": {"italic": True, "color": "gray"},
            }]
        },
    }


def refresh_updated_line(page_id, now):
    text = f"{UPDATED_MARKER} {now.strftime('%Y-%m-%d %H:%M UTC')}"
    block_id = find_updated_block(page_id)
    if block_id:
        # Reuse the existing top line, updating it in place (keeps its position).
        url = f"https://api.notion.com/v1/blocks/{block_id}"
        resp = requests.patch(url, headers=HEADERS,
                              json={"paragraph": updated_paragraph(text)["paragraph"]},
                              timeout=30)
        resp.raise_for_status()
    else:
        # No marker line yet: append one. Notion's API cannot prepend, so seed a
        # "Last updated:" line at the very top of the page once by hand and every
        # run thereafter updates it in place (see setup instructions).
        url = f"https://api.notion.com/v1/blocks/{page_id}/children"
        resp = requests.patch(url, headers=HEADERS,
                              json={"children": [updated_paragraph(text)]}, timeout=30)
        resp.raise_for_status()
    print(f"  {text}")


# --- main ---------------------------------------------------------------------
def main():
    now = datetime.now(timezone.utc)
    now_iso = now.isoformat()

    rows = query_all(COLLECTIONS_DB_ID)
    print(f"Scanned {len(rows)} collections.")

    changed = write_auto_status(rows)
    print(f"Overall Status (Auto): {changed} rows updated.")

    print("Pipeline Summary:")
    update_summary(rows, now_iso)

    print("Weekly Output:")
    update_weekly(rows, now)

    print("Exec page timestamp:")
    refresh_updated_line(EXEC_PAGE_ID, now)

    print(f"Done at {now_iso}.")


if __name__ == "__main__":
    main()
