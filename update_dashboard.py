#!/usr/bin/env python3
"""
Basecamp Daily Status Dashboard Updater
Fetches todos assigned to Ryan from Basecamp API and generates dashboard.html
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

# ── Config ────────────────────────────────────────────────────────────────────
ACCOUNT_ID   = "4114768"
API_BASE     = f"https://3.basecampapi.com/{ACCOUNT_ID}"
LAUNCHPAD    = "https://launchpad.37signals.com"
USER_AGENT   = "StatusDashboard (ryanwhitesidemarketer@gmail.com)"
CLIENT_ID    = "b39f2de41f7ebeaf472f3f8d798f9abda7c349e9"
CLIENT_SECRET= "5738f42bd9fb89995ebd1af59114803470d620e1"

SCRIPT_DIR    = Path(__file__).parent
SNAPSHOT_FILE = SCRIPT_DIR / "snapshot.json"
DASHBOARD_FILE= SCRIPT_DIR / "dashboard.html"

# ── Helpers ───────────────────────────────────────────────────────────────────

def make_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "User-Agent":    USER_AGENT,
        "Accept":        "application/json",
    }


def get_all_pages(url, headers):
    """Follow Basecamp pagination via Link header."""
    results = []
    while url:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code != 200:
            print(f"  WARN {r.status_code}: {url}")
            break
        data = r.json()
        if isinstance(data, list):
            results.extend(data)
        else:
            results.append(data)
        m = re.search(r'<([^>]+)>;\s*rel="next"', r.headers.get("Link", ""))
        url = m.group(1) if m else None
    return results


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text or "").strip()


def is_ryan(name, ryan_id, person_id=None):
    if person_id and str(person_id) == str(ryan_id):
        return True
    if not name:
        return False
    nl = name.lower()
    return "ryan" in nl and any(x in nl for x in ["whiteside", "w.", " w"])


def find_linked_todo(html):
    """Return (url, bucket_id, todo_id) if a Basecamp todo link is in the HTML."""
    m = re.search(
        r'https://3\.basecamp\.com/\d+/buckets/(\d+)/todos/(\d+)',
        html or ""
    )
    if m:
        return m.group(0), m.group(1), m.group(2)
    return None, None, None


def try_refresh(refresh_tok):
    r = requests.post(
        f"{LAUNCHPAD}/authorization/token",
        params={"type": "refresh"},
        headers={"User-Agent": USER_AGENT},
        data={
            "client_id":     CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "refresh_token": refresh_tok,
        },
        timeout=30,
    )
    if r.status_code == 200:
        d = r.json()
        return d["access_token"], d.get("refresh_token", refresh_tok)
    return None, None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    access_token  = os.environ.get("BASECAMP_ACCESS_TOKEN", "").strip()
    refresh_tok   = os.environ.get("BASECAMP_REFRESH_TOKEN", "").strip()

    if not access_token:
        sys.exit("ERROR: BASECAMP_ACCESS_TOKEN not set")

    headers = make_headers(access_token)

    # Verify token (refresh if expired)
    auth_resp = requests.get(f"{LAUNCHPAD}/authorization.json", headers=headers, timeout=30)
    if auth_resp.status_code == 401 and refresh_tok:
        print("Access token expired — refreshing...")
        access_token, new_refresh = try_refresh(refresh_tok)
        if not access_token:
            sys.exit("ERROR: Token refresh failed")
        headers = make_headers(access_token)
        # Output new tokens so GitHub Actions can update secrets
        print(f"::set-output name=new_access_token::{access_token}")
        if new_refresh and new_refresh != refresh_tok:
            print(f"::set-output name=new_refresh_token::{new_refresh}")
        auth_resp = requests.get(f"{LAUNCHPAD}/authorization.json", headers=headers, timeout=30)

    if auth_resp.status_code != 200:
        sys.exit(f"ERROR: Auth check failed ({auth_resp.status_code})")

    auth_data  = auth_resp.json()
    ryan_id    = auth_data["identity"]["id"]
    ryan_first = auth_data["identity"]["first_name"]
    ryan_last  = auth_data["identity"]["last_name"]
    print(f"Authenticated as {ryan_first} {ryan_last} (ID: {ryan_id})")

    # ── Collect todos assigned to Ryan (single efficient endpoint) ───────────
    print("Fetching assignments...")
    raw_todos = []
    items = get_all_pages(f"{API_BASE}/my/assignments.json", headers)
    for item in items:
        if item.get("type") == "Todo" and not item.get("completed", False):
            raw_todos.append(item)

    print(f"Found {len(raw_todos)} active todos assigned to Ryan")

    # ── Process each todo ─────────────────────────────────────────────────────
    tasks = {}
    for todo in raw_todos:
        tid    = todo["id"]
        bucket = todo.get("bucket", {})
        bid    = bucket.get("id")
        title  = todo.get("title", "Untitled").strip()
        print(f"  → {title}")

        # Full todo detail (includes description for linked-task detection)
        dr = requests.get(f"{API_BASE}/buckets/{bid}/todos/{tid}.json", headers=headers, timeout=30)
        if dr.status_code != 200:
            print(f"    SKIP ({dr.status_code})")
            continue
        detail = dr.json()

        due_on      = detail.get("due_on")
        description = detail.get("description", "") or ""

        # Comments
        comments = get_all_pages(
            f"{API_BASE}/buckets/{bid}/todos/{tid}/comments.json", headers
        )

        # Check description for a linked Basecamp task
        linked_url, linked_bid, linked_tid = find_linked_todo(description)
        ryan_in_linked = False
        if linked_tid:
            linked_comments = get_all_pages(
                f"{API_BASE}/buckets/{linked_bid}/todos/{linked_tid}/comments.json",
                headers,
            )
            for lc in linked_comments:
                c = lc.get("creator", {})
                if is_ryan(c.get("name"), ryan_id, c.get("id")):
                    ryan_in_linked = True
                    break

        # Last commenter info
        last_commenter        = None
        last_commenter_is_ryan= False
        last_comment_date     = None
        last_note             = ""
        ryan_last_comment_date= None

        for comment in comments:
            c    = comment.get("creator", {})
            name = c.get("name", "")
            if is_ryan(name, ryan_id, c.get("id")):
                ryan_last_comment_date = comment.get("created_at", "")[:10]

        if comments:
            last    = comments[-1]
            c       = last.get("creator", {})
            last_commenter         = c.get("name", "Unknown")
            last_commenter_is_ryan = is_ryan(last_commenter, ryan_id, c.get("id"))
            last_comment_date      = last.get("created_at", "")[:10]
            last_note              = strip_html(last.get("content", ""))[:300]

        # ── Waiting-status logic ──────────────────────────────────────────────
        if last_commenter_is_ryan or ryan_in_linked:
            status = "waiting_on_others"
        elif not comments:
            # No conversation yet — Ryan hasn't missed anything
            status = "waiting_on_others"
        else:
            status = "action_required"

        # Override: due within 7 days and Ryan hasn't responded → action required
        if due_on and not last_commenter_is_ryan and not ryan_in_linked:
            from datetime import timedelta
            due_dt    = datetime.strptime(due_on, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            days_left = (due_dt - datetime.now(timezone.utc)).days
            if days_left <= 7:
                status = "action_required"

        # Assignees
        assignees = detail.get("assignees", [])
        def short_name(a):
            n = a.get("name", "")
            if is_ryan(n, ryan_id, a.get("id")):
                return "Ryan W."
            parts = n.split()
            return f"{parts[0]} {parts[-1][0]}." if len(parts) >= 2 else n
        assignees_str = ", ".join(short_name(a) for a in assignees) or "Ryan W."

        tasks[title] = {
            "url":                 f"https://3.basecamp.com/{ACCOUNT_ID}/buckets/{bid}/todos/{tid}",
            "noteCount":           len(comments),
            "dueDate":             due_on,
            "assignees":           assignees_str,
            "lastCommenter":       last_commenter,
            "lastCommentDate":     last_comment_date,
            "ryanLastCommentDate": ryan_last_comment_date,
            "waitingStatus":       status,
            "lastNote":            last_note,
            "linkedTaskUrl":       linked_url,
        }

    # ── Save snapshot ─────────────────────────────────────────────────────────
    now_str  = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"
    snapshot = {"lastRun": now_str, "tasks": tasks}
    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))
    print(f"\nSnapshot saved: {len(tasks)} tasks")

    # ── Generate dashboard ────────────────────────────────────────────────────
    generate_dashboard(tasks, now_str)
    print("Dashboard generated ✓")


# ── Dashboard HTML ─────────────────────────────────────────────────────────────

def generate_dashboard(tasks, last_run):
    action   = {k: v for k, v in tasks.items() if v["waitingStatus"] == "action_required"}
    waiting  = {k: v for k, v in tasks.items() if v["waitingStatus"] == "waiting_on_others"}

    def card(title, task, css):
        due_html  = f'<span class="due-badge">Due: {task["dueDate"]}</span>' if task.get("dueDate") else ""
        note      = task.get("lastNote", "")
        commenter = task.get("lastCommenter", "")
        date      = task.get("lastCommentDate", "")
        count     = task.get("noteCount", 0)
        meta      = f"{commenter} · {date} · {count} {'note' if count == 1 else 'notes'}" if commenter else f"{count} notes"
        note_html = f'<div class="card-note">{note}</div>' if note else ""
        return f"""<div class="card {css}">
  <div class="card-top">
    <a class="card-title" href="{task['url']}" target="_blank">{title}</a>
    {due_html}
  </div>
  <div class="card-meta">{meta}</div>
  {note_html}
</div>"""

    action_html  = "\n".join(card(t, d, "card-action")  for t, d in sorted(action.items()))
    waiting_html = "\n".join(card(t, d, "card-waiting") for t, d in sorted(waiting.items()))
    if not action_html:
        action_html  = '<p class="empty">All clear — nothing needs your attention!</p>'
    if not waiting_html:
        waiting_html = '<p class="empty">No items waiting on others.</p>'

    updated = last_run.replace("T", " ").replace("Z", " UTC")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Basecamp Daily Status</title>
<style>
* {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        background: #f0f2f5; color: #1a1a2e; min-height: 100vh; }}
header {{ background: #1a1a2e; color: #fff; padding: 20px 28px;
          display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 8px; }}
header h1 {{ font-size: 20px; font-weight: 700; letter-spacing: -.3px; }}
.updated {{ font-size: 12px; color: #8888aa; }}
.stats {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 12px; padding: 20px 28px 8px; }}
.stat {{ background: #fff; border-radius: 10px; padding: 14px 18px; text-align: center;
         box-shadow: 0 1px 4px rgba(0,0,0,.07); }}
.stat-num {{ font-size: 32px; font-weight: 800; line-height: 1; }}
.stat-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: #888; margin-top: 4px; }}
.stat-action  .stat-num {{ color: #e03131; }}
.stat-waiting .stat-num {{ color: #e67700; }}
.stat-total   .stat-num {{ color: #2f9e44; }}
.section {{ padding: 16px 28px; }}
.section-header {{ font-size: 14px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: .06em; margin-bottom: 10px; padding-bottom: 8px; border-bottom: 2px solid; }}
.section-action  .section-header {{ color: #e03131; border-color: #ffc9c9; }}
.section-waiting .section-header {{ color: #e67700; border-color: #ffe8cc; }}
.card {{ background: #fff; border-radius: 10px; padding: 14px 16px; margin-bottom: 8px;
         border-left: 4px solid; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
.card-action  {{ border-left-color: #e03131; }}
.card-waiting {{ border-left-color: #e67700; }}
.card-top {{ display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 5px; }}
.card-title {{ font-weight: 600; font-size: 14px; color: #1a1a2e; text-decoration: none; flex: 1; }}
.card-title:hover {{ text-decoration: underline; }}
.due-badge {{ font-size: 11px; background: #fff0f0; color: #e03131; border-radius: 4px;
              padding: 2px 7px; font-weight: 600; white-space: nowrap; }}
.card-meta {{ font-size: 12px; color: #999; margin-bottom: 5px; }}
.card-note {{ font-size: 13px; color: #555; line-height: 1.5; }}
.empty {{ color: #aaa; font-size: 14px; padding: 8px 0; }}
@media (max-width: 480px) {{
  header, .section {{ padding-left: 16px; padding-right: 16px; }}
  .stats {{ padding: 16px; gap: 8px; }}
}}
</style>
</head>
<body>
<header>
  <h1>📋 Basecamp Daily Status</h1>
  <span class="updated">Updated: {updated} · Refreshes at 3am daily</span>
</header>
<div class="stats">
  <div class="stat stat-action"> <div class="stat-num">{len(action)}</div> <div class="stat-label">Action Needed</div></div>
  <div class="stat stat-waiting"><div class="stat-num">{len(waiting)}</div><div class="stat-label">Waiting On</div></div>
  <div class="stat stat-total">  <div class="stat-num">{len(tasks)}</div> <div class="stat-label">Total Active</div></div>
</div>
<div class="section section-action">
  <div class="section-header">⚠ Action Required</div>
  {action_html}
</div>
<div class="section section-waiting">
  <div class="section-header">⏳ Waiting on Others</div>
  {waiting_html}
</div>
</body>
</html>"""
    DASHBOARD_FILE.write_text(html)


if __name__ == "__main__":
    main()
