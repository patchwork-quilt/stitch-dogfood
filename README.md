# stitch-dogfood — the fleet's booking board, kept as a stitch ledger

This repo is the dogfood of [stitch](https://github.com/patchwork-quilt/stitch),
the accountable-state primitive: **stitch holding the very records it
was designed from.**

The fleet coordinates work on shared machines through a booking
board — [`schedule/` in SuperInstance/agent-inbox](https://github.com/SuperInstance/agent-inbox/tree/main/schedule):
one markdown file per box, one table row per booking (lane, seat,
from, planned, check-by, what). Stitch's exchange-booking semantics
were originally prototyped from that board's pattern. Here the flow
runs the other way: a sync mirrors the board into `bookings.jsonl`,
a single append-only stitch ledger, in public, where anyone can
audit it with stitch itself.

## Current state (maintained by the sync)

Anchor: `84a06319e4b11766`
Entries: 269
Board bookings mirrored: 105

The anchor is the ledger's head hash. If it does not equal the head
hash you compute from `bookings.jsonl` yourself (see *Verify it
yourself*), the ledger in front of you is not the ledger that was
audited at the last sync.

## What a booking looks like here

Each board row becomes one stitch exchange booking, recorded twice
in the one ledger, as stitch prescribes:

- a `book_send` — `BIND`, kind `exchange`, side `send` — by the
  sender tenant (see the modelling choice below), and
- a `book_receive` — side `receive` — by the booked box's tenant
  (`thor`, `proart`, `oracle`, `jetson-orin`; one per board file).

The booking payload is the parsed row —
`{box, lane, seat, from, planned, check_by, what}` — but stitch
ledgers store only the payload's SHA-256 hash (`ph`): the ledger is
the commitment, the board is the cargo, and both are public. The
exchange id is content-addressed from the same canonical row:
`bk-<first 24 hex of the payload hash>`. Anyone can recompute every
`ph` from the board with the parser in `sync.py`.

Closures use stitch's own lifecycle, and only the board's facts
close a booking:

- A row the board marks **DONE** (a DONE marker leading the planned,
  check-by or what cell, or a trailing `— DONE …` note) is booked,
  then closed in the same visit by a `REVOKE` of the receive-side
  cell, reason `done (board)`.
- A booking whose row **vanishes** — deleted, or edited (an edit
  changes the row's hash, so the edited row books as a new booking)
  — is closed by a `REVOKE`, reason `removed from board`. If the
  identical row later reappears, it is booked afresh as a second
  pair under the same exchange id.
- **Expiry alone never closes a booking.** The board's own rule is
  that an expired row is its lane's to mark DONE or delete; the
  ledger mirrors the board instead of judging it. Expired-but-present
  bookings stand in the ledger as historical fact.

Derived state, recomputable by any reader: a booking is *open* when
its receive booking stands unrevoked.

## Modelling choices — the honest mismatches

- **Sender.** Stitch's native pattern has each party book from its
  own ledger: the lane would book its own SEND. The board, however,
  does not record which box a lane is "home" on, so every SEND half
  here is booked by the tenant **`oracle`** — the fleet's relay,
  where the board is kept, through which bookings are delivered, and
  where the sync runs. Read the SEND half as "the board, via the
  relay, delivers this booking", and the payload (which names the
  lane) as the attribution. Bookings of Oracle's own seats are
  single-party records: send and receive are both `oracle`.
- **One file, many tenants.** Stitch ledgers are usually one file
  per tenant; its auditor is explicitly multi-tenant aware
  (per-tenant budgets and gamma replay), and the board is one shared
  record, so the mirror is one shared ledger. What is given up:
  bilateral `agree()`/anchor-pinning between two sovereign files.
  What is kept: the hash chain, the conservation-law audit, and the
  booking lifecycle, all verifiable below.
- **The parser is part of the commitment.** Payloads are *parsed*
  rows (cells trimmed; an over-long `what` cell rejoined with
  ` | `). If the parser changes, old bookings close as removed and
  rows re-book under new ids — a visible event in the ledger, never
  a silent rewrite. Nothing in `bookings.jsonl` is ever edited or
  deleted by the sync; it only appends.

## Verify it yourself

With stitch itself (Python ≥ 3.9, no other dependencies):

```bash
git clone https://github.com/patchwork-quilt/stitch.git
cd stitch && python3 -m venv /tmp/stitch-venv
/tmp/stitch-venv/bin/pip install ./stitch-python
cd ../stitch-dogfood   # this repo, cloned

/tmp/stitch-venv/bin/python - <<'EOF'
from stitch.agreement import audit_ledger
v = audit_ledger("bookings.jsonl")
print("chain_ok:", v.chain_ok, "| law_ok:", v.law_ok, "| entries:", v.entries)
print("head:", v.head_hash, " <- must equal the Anchor in README.md")
assert v.ok
print("AUDIT OK")
EOF
```

Flip any byte of a *copy* of `bookings.jsonl` and re-run: the chain
breaks at the tampered entry and the audit refuses (`ok` is False).
That refusal is the product.

## How the sync works

`sync.py` (stdlib + the stitch package, installed from a pinned
clone of patchwork-quilt/stitch — the pin is in `PIN`) runs on
Oracle from a user crontab, every 6 hours:

```cron
23 */6 * * * cd /home/ubuntu/stitch-dogfood && /home/ubuntu/stitch-dogfood/venv/bin/python sync.py >> /home/ubuntu/stitch-dogfood/sync.log 2>&1
```

Each run: pull the board clone (`board/`, a full clone of
agent-inbox), parse the board, append any new bookings and closures
to `bookings.jsonl`, audit the result with stitch's `audit_ledger`
— **if the audit fails, nothing is committed** — refresh the anchor
block above, and commit + push only if the ledger or README
changed. Only `schedule/` is ever read from agent-inbox; the board
is already public, and nothing else is copied. The working checkout
lives at `~/stitch-dogfood/` on Oracle; runs are logged to
`sync.log` there (checkout-local, not committed).

## How to stop it

On Oracle: `crontab -e` and delete the `stitch-dogfood` line. The
repo then simply stops advancing; the ledger remains as the public
historical record. To retire the record entirely, archive this repo
— do not rewrite it.

## First-run proof (2026-11-02)

<!-- PROOF -->
First sync run on Oracle at 2026-11-02T18:59Z, then verified from a
fresh clone of this repo with a fresh install of stitch (the exact
commands in *Verify it yourself* above):

- Board: 70 rows parsed across the 4 box files (thor, proart,
  jetson-orin, oracle) — 70 unique bookings, no content collisions.
  All 70 were booked (send + receive); the 36 rows the board marks
  DONE were closed in the same run (`done (board)` revocations);
  34 bookings stand open.
- Ledger: 180 entries (4 tenant budget declarations, 140 booking
  halves, 36 revocations).
- Audit verdict on the pushed ledger: `chain_ok: True`,
  `law_ok: True`, `ok: True`.
- Anchor: the head hash computed from the fresh clone,
  `8e1635e5a41be794`, equals the anchor block above.
- Tamper demonstration (on a copy in /tmp, never the real ledger):
  one byte flipped at the file's midpoint →
  `chain_ok: False`, `chain_broken_at: 91`, `ok: False`.
  The audit refuses exactly as specified.
<!-- /PROOF -->
