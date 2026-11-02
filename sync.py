#!/usr/bin/env python3
"""
sync.py — mirror the fleet's booking board into a stitch ledger.

The board is `schedule/` in SuperInstance/agent-inbox: one markdown
file per box, one table row per booking (lane, seat, from, planned,
check-by, what). This script keeps `bookings.jsonl` — a single
append-only stitch ledger — in step with the board:

  * a board row seen for the first time is booked as a stitch
    exchange: `book_send` by the relay tenant, `book_receive` by the
    booked box's tenant, over a payload that is the parsed row;
  * a row the board marks DONE is booked and immediately closed with
    a REVOKE of the receive-side cell (reason "done (board)");
  * a booking whose row has vanished from the board (deleted, or
    edited — an edit changes the row's hash, so the old booking
    vanishes and the edited row books as a new one) is closed with a
    REVOKE of the receive-side cell (reason "removed from board");
  * a booking that was closed as removed and whose row reappears is
    booked afresh (a second pair under the same exchange id);
  * expiry alone never closes a booking: the board's own rule is
    that an expired row is the owning lane's to mark DONE or delete,
    and this ledger mirrors the board rather than judging it.

Exchange ids are content-addressed: "bk-" + sha256(canonical JSON of
the parsed row)[:24]. The ledger stores only the payload *hash*
(stitch bookings are commitments, not cargo); the payload is the
parsed row, recomputable by anyone from the public board using this
public parser. The parser is therefore part of the commitment: if it
changes, old bookings close as removed and rows re-book under new
ids — a visible event, not a silent rewrite. History is never
rewritten; the ledger is append-only, exactly as stitch intends.

Sender tenant: the board does not record which box a lane is "home"
on, so every SEND half is booked by the tenant `oracle` — the
fleet's relay, where the board is kept and through which bookings
are delivered (and where this sync runs). The RECEIVE half is booked
by the booked box. Bookings of Oracle's own seats are therefore
single-party records (send and receive both `oracle`). This is a
documented simplification of stitch's native pattern, in which each
lane would book its own SEND from its own ledger.

After appending, the script audits the ledger with stitch's own
`audit_ledger`. If the audit does not pass, it exits non-zero and
commits nothing (fail closed). Otherwise it refreshes the anchor
block in README.md and, if bookings.jsonl or README.md changed,
commits and pushes.

Run by cron on Oracle every 6 hours; see README.md.
Stdlib + the stitch package (pinned clone; see PIN).
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOARD_DIR = Path(os.environ.get("BOARD_DIR", ROOT / "board"))
LEDGER_PATH = ROOT / "bookings.jsonl"
README_PATH = ROOT / "README.md"
BOARD_REPO = "https://github.com/SuperInstance/agent-inbox.git"

SENDER = "oracle"  # relay tenant; see module docstring
REASON_DONE = "done (board)"
REASON_REMOVED = "removed from board"


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def run(cmd, cwd=None, timeout=1200):
    return subprocess.run(cmd, cwd=cwd, timeout=timeout,
                          capture_output=True, text=True)


# ---------------------------------------------------------------------
# Board parsing
# ---------------------------------------------------------------------

def _starts_done(text):
    return text.strip().upper().startswith("DONE")


def row_done(planned, check_by, what, suffix):
    """A row is DONE per the board's own marking conventions: a DONE
    marker leading the planned / check-by / what cell, or a trailing
    '— DONE …' note after the table row."""
    if _starts_done(planned) or _starts_done(check_by) or _starts_done(what):
        return True
    return bool(suffix and re.search(r"\bDONE\b", suffix))


def parse_row(box, line):
    s = line.strip()
    if not s.startswith("|"):
        return None
    suffix = ""
    if not s.endswith("|"):
        idx = s.rfind("|")
        if idx < 0:
            return None
        suffix = s[idx + 1:].strip().lstrip("—–-").strip()
        s = s[:idx + 1]
    cells = [c.strip() for c in s.strip("|").split("|")]
    if len(cells) < 6:
        return None
    lane, seat, frm, planned, check_by = cells[0], cells[1], cells[2], cells[3], cells[4]
    what = " | ".join(cells[5:])
    if lane.lower() == "lane" or set(lane) <= set("-: "):
        return None  # header / separator
    if not lane or not frm:
        return None
    payload = {
        "box": box, "lane": lane, "seat": seat, "from": frm,
        "planned": planned, "check_by": check_by, "what": what,
    }
    canon = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False)
    xchg = "bk-" + hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]
    return {"payload": payload, "xchg": xchg,
            "done": row_done(planned, check_by, what, suffix)}


def parse_board(board_dir):
    """Return (rows_in_order, unique_by_xchg, row_count)."""
    sched = board_dir / "schedule"
    rows = []
    for path in sorted(sched.glob("*.md")):
        if path.name == "README.md":
            continue
        box = path.stem
        for line in path.read_text(encoding="utf-8").splitlines():
            row = parse_row(box, line)
            if row:
                rows.append(row)
    by_xchg = {}
    for row in rows:
        by_xchg.setdefault(row["xchg"], row)
    return rows, by_xchg, len(rows)


# ---------------------------------------------------------------------
# Ledger state
# ---------------------------------------------------------------------

def scan_ledger(path):
    """Reconstruct booking state from the existing ledger file.

    Returns (declared_tenants, state) where state maps xchg ->
    {"box": str, "receive_cells": [cell_id...],
     "open_receive": cell_id or None, "close_reason": str or None}.
    """
    declared = set()
    state = {}
    cell_xchg = {}
    if not path.exists():
        return declared, state
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        e = json.loads(line)
        if e.get("op") == "BIND" and e.get("kind") == "tenant":
            declared.add(e.get("ns"))
        elif e.get("op") == "BIND" and e.get("kind") == "exchange":
            p = e.get("payload") or {}
            xchg = p.get("xchg")
            if not xchg:
                continue
            st = state.setdefault(xchg, {"box": None, "receive_cells": [],
                                         "open_receive": None,
                                         "close_reason": None})
            cell_xchg[e.get("cell")] = xchg
            if p.get("side") == "receive":
                st["box"] = e.get("ns")
                st["receive_cells"].append(e.get("cell"))
                st["open_receive"] = e.get("cell")
                st["close_reason"] = None
        elif e.get("op") == "REVOKE":
            xchg = cell_xchg.get(e.get("cell"))
            if xchg is not None and state[xchg]["open_receive"] == e.get("cell"):
                state[xchg]["open_receive"] = None
                state[xchg]["close_reason"] = e.get("reason")
    return declared, state


# ---------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------

def ensure_board():
    if (BOARD_DIR / ".git").exists():
        r = run(["git", "-C", str(BOARD_DIR), "pull", "--ff-only"], timeout=600)
        if r.returncode != 0:
            log(f"board pull FAILED: {r.stderr.strip()[:300]}")
            return False
        return True
    if BOARD_DIR.exists():
        log(f"board dir {BOARD_DIR} exists without .git — using as-is (fixture mode)")
        return True
    log(f"cloning board into {BOARD_DIR} …")
    r = run(["git", "clone", BOARD_REPO, str(BOARD_DIR)], timeout=1800)
    if r.returncode != 0:
        log(f"board clone FAILED: {r.stderr.strip()[:300]}")
        return False
    return True


def git_commit_push(newly_booked, closed_done, closed_removed, anchor):
    if not (ROOT / ".git").exists():
        log("not a git checkout — skipping commit/push")
        return True
    run(["git", "add", "bookings.jsonl", "README.md"], cwd=ROOT)
    status = run(["git", "status", "--porcelain", "--", "bookings.jsonl", "README.md"],
                 cwd=ROOT)
    if status.stdout.strip():
        board_head = run(["git", "-C", str(BOARD_DIR), "rev-parse", "--short", "HEAD"],
                         cwd=ROOT)
        b = board_head.stdout.strip() or "?"
        msg = (f"sync: board@{b} — {newly_booked} booked, "
               f"{closed_done} done, {closed_removed} removed; "
               f"anchor {anchor[:12] if anchor else '-'}")
        c = run(["git", "commit", "-m", msg], cwd=ROOT)
        if c.returncode != 0:
            log(f"commit FAILED: {c.stderr.strip()[:300]}")
            return False
        log(f"committed: {msg}")
    ahead = run(["git", "rev-list", "--count", "origin/main..HEAD"], cwd=ROOT)
    if ahead.returncode == 0 and ahead.stdout.strip() not in ("", "0"):
        p = run(["git", "push"], cwd=ROOT, timeout=600)
        if p.returncode != 0:
            log(f"push FAILED: {p.stderr.strip()[:300]}")
            return False
        log("pushed")
    else:
        log("no changes to commit")
    return True


# ---------------------------------------------------------------------
# README anchor block
# ---------------------------------------------------------------------

def update_readme(anchor, entries, mirrored):
    if not README_PATH.exists():
        return
    text = README_PATH.read_text(encoding="utf-8")
    text = re.sub(r"^Anchor: `[^`]*`", f"Anchor: `{anchor}`", text, flags=re.M)
    text = re.sub(r"^Entries: .*$", f"Entries: {entries}", text, flags=re.M)
    text = re.sub(r"^Board bookings mirrored: .*$",
                  f"Board bookings mirrored: {mirrored}", text, flags=re.M)
    README_PATH.write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------

def main():
    from stitch.agreement import audit_ledger, book_receive, book_send
    from stitch.core import Cell, Ledger, Tenant
    from stitch.stitches import declare_budget, revoke

    log("sync start")
    if not ensure_board():
        return 1

    rows, by_xchg, row_count = parse_board(BOARD_DIR)
    declared, state = scan_ledger(LEDGER_PATH)
    log(f"board: {row_count} rows, {len(by_xchg)} unique bookings")

    tenants = {}

    def tenant(name):
        if name not in tenants:
            tenants[name] = Tenant(identity=name)
        return tenants[name]

    ledger = Ledger(str(LEDGER_PATH))
    appended = newly_booked = closed_done = closed_removed = 0

    def ensure_declared(name):
        if name not in declared:
            declare_budget(tenant(name), ledger)
            declared.add(name)

    def book(row):
        payload = row["payload"]
        box = payload["box"]
        ensure_declared(SENDER)
        ensure_declared(box)
        book_send(tenant(SENDER), ledger, row["xchg"], peer=box, payload=payload)
        cell = book_receive(tenant(box), ledger, row["xchg"], peer=SENDER,
                            payload=payload)
        state[row["xchg"]] = {"box": box, "receive_cells": [cell.id],
                              "open_receive": cell.id, "close_reason": None}
        return cell

    def close(xchg, reason):
        st = state[xchg]
        cell = Cell(id=st["open_receive"], tenant=st["box"], kind="exchange",
                    payload={}, parent_hash=None, created_at=0.0)
        revoke(tenant(st["box"]), ledger, cell, reason)
        st["open_receive"] = None
        st["close_reason"] = reason

    # Book new / reappeared bookings, in board order.
    for row in by_xchg.values():
        xchg = row["xchg"]
        st = state.get(xchg)
        if st is None:
            cell = book(row)
            newly_booked += 1
            appended += 2
            if row["done"]:
                close(xchg, REASON_DONE)
                closed_done += 1
                appended += 1
        elif st["open_receive"] is None and st["close_reason"] == REASON_REMOVED:
            cell = book(row)
            newly_booked += 1
            appended += 2
            if row["done"]:
                close(xchg, REASON_DONE)
                closed_done += 1
                appended += 1
        # else: already booked and open, or terminally done — nothing.

    # Close bookings whose rows vanished from the board.
    for xchg in sorted(state):
        st = state[xchg]
        if st["open_receive"] is not None and xchg not in by_xchg:
            close(xchg, REASON_REMOVED)
            closed_removed += 1
            appended += 1

    ledger.close()
    open_count = sum(1 for st in state.values() if st["open_receive"] is not None)
    log(f"appended {appended} entries ({newly_booked} booked, "
        f"{closed_done} done-closed, {closed_removed} removed-closed); "
        f"{open_count} bookings open")

    verdict = audit_ledger(str(LEDGER_PATH))
    log(f"audit: ok={verdict.ok} chain_ok={verdict.chain_ok} "
        f"law_ok={verdict.law_ok} entries={verdict.entries} "
        f"head={verdict.head_hash}")
    if not verdict.ok:
        log("AUDIT FAILED — committing nothing (fail closed)")
        return 1

    update_readme(verdict.head_hash, verdict.entries, len(by_xchg))
    if not git_commit_push(newly_booked, closed_done, closed_removed,
                           verdict.head_hash):
        return 1
    log("sync done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
