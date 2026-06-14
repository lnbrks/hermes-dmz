# Inbox Cleanup Procedure

Rough pseudocode of the workflow this repo implements. Tools are ad-hoc; this
document captures _intent_ so the scripts can be read, reused, or rebuilt later.

## Core principles

1. **Funnel cheap-to-expensive.** Free signals first (Gmail labels, headers),
   LLM only on the residual.
2. **Separate classification from mutation.** Every step writes a JSON list of
   IDs. Nothing touches Gmail until a separate `delete_emails.py` /
   `archive_emails.py` script runs against that list.
3. **Three buckets, not two**:
   - **Trash** — "will I ever want to see this again?" → no. Pure marketing,
     surveys, expired offers.
   - **Archive** — "might I want to *find* this later?" → yes. Receipts,
     shipping, payment notifications, past appointments. Default for
     transactional residue.
   - **Inbox** — "is there something I'm supposed to *do*?" → yes. Unanswered
     personal mail, bills, security alerts, upcoming appointments.
4. **Risk asymmetry favors archive.** Trash is recoverable for 30 days;
   archive is forever. When in doubt, archive over trash.
5. **Allowlist what's sacred.** `excluded_domains.txt` protects banks, medical
   portals, shipping carriers — never considered for deletion regardless of
   signals.

## Pipeline

```
                   ┌────────────────────┐
                   │  Gmail (live)      │
                   └─────────┬──────────┘
                             │ fetch_inbox.py
                             ▼
                   ┌────────────────────┐
                   │ inbox.ndjson       │  (one JSON per email: id, labels,
                   │ (local snapshot)   │   headers, snippet, internalDate)
                   └─────────┬──────────┘
                             │
        ┌────────────────────┼────────────────────┐
        ▼                    ▼                    ▼
  ┌──────────┐         ┌──────────┐         ┌──────────┐
  │ MARKETING│         │  TRIAGE  │         │ UNSUBSCRIBE│
  │ classify │         │ inbox    │         │  link gen   │
  └──────────┘         └──────────┘         └──────────┘
       │                    │                    │
       ▼                    ▼                    ▼
  to_delete_*.json    triage_results.json   unsubscribe_links.txt
       │                    │                    │
       ▼                    ▼                    ▼
  delete_emails.py    archive_emails.py     (manual: browser)
       │                    │
       ▼                    ▼
  TRASH label         remove INBOX label
```

## Steps

### Step 0 — Setup (one-time)

- Auth `gws` CLI with Gmail `read + modify` scopes
- Anthropic API key in `.env`
- Curate `excluded_domains.txt` with banks, medical portals, payment
  processors, shipping carriers — never delete these

### Step 1 — Snapshot the inbox

```python
fetch_inbox.py --query "in:inbox" > inbox.ndjson
```

Pulls metadata (no body content): id, labels, From/Subject/Date/List-Unsubscribe
headers, snippet, internalDate. Read-only. The rest of the pipeline operates
on this local snapshot.

### Step 2 — Marketing classification (round N)

**Goal:** identify emails safe to TRASH.

**Filter inputs:**
- Email has `CATEGORY_PROMOTIONS` label, OR
- Email has `List-Unsubscribe` header, OR
- Email is from a high-volume marketing domain (see `marketing_senders.py`)
- AND domain is not in `excluded_domains.txt`
- AND email is not already in a previous `to_delete_*.json`

**LLM step (batched, ~30-40 emails per call, cheap model):**

```
prompt: "Classify each as DELETE (purely promotional: sales, newsletters,
        surveys, fundraising, expired offers, review requests) or KEEP
        (receipts, order confirmations, shipping, security alerts,
        appointment reminders, refunds, payment confirmations, billing).
        When in doubt, KEEP."
output: JSON array of {action, reason}
```

**Outputs:**
- `to_delete_round{N}.json` — list of IDs

**Run multiple rounds** as new emails arrive. Each round is filtered to skip
IDs already in any prior `to_delete_*.json`.

### Step 3 — Generate unsubscribe links

```python
marketing_senders.py → marketing_senders.json
```

Then format into `unsubscribe_links.txt` sorted by promo volume. Manual: open
each one-click link in a browser. **Important: unsubscribing prevents future
inflow but does not delete historical email.**

### Step 4 — Inspect & spot-check

- Print top senders by delete count
- For senders where user has real history (e.g. ever purchased), spot-check
  by searching the delete list for keywords (order, receipt, shipping, refund,
  support) → confirm matches are marketing copy not real records
- Also verify real purchase records were KEPT (look at what's not in delete)

### Step 5 — Custom per-sender rules (optional)

Apply hand-coded overrides to the delete list, e.g.:
- For sender X: remove items < N days old from delete list
- For sender Y: keep purchase records, delete everything else
- For sender Z: delete all "message notification" emails (no content) but
  keep appointment confirmations

These rules are codified inline in a one-off script per cleanup session.

### Step 6 — Trash the marketing emails

```python
delete_emails.py --input to_delete_round{N}.json
```

Uses Gmail `batchModify`: adds TRASH label, removes INBOX label. Recoverable
from Trash for 30 days. Prompts for `yes` before executing.

### Step 7 — Triage what's left into action vs archive

**Goal:** for everything remaining (not deleted), decide whether it goes
back in the inbox or to archive.

**LLM step (batched, cheap model):**

```
prompt: "Today is {date}. Classify each email:
        - ACTION: requires response, decision, follow-up (unanswered
          personal mail, appointments to confirm, bills, security alerts,
          unresolved support issues, expiring offers)
        - ARCHIVE: useful record but no action needed (receipts, shipping,
          payments, past appointments, read newsletters, welcome emails,
          completed transactions). Anything > 2 months old that was
          time-sensitive → stale → archive.
        When in doubt, ARCHIVE."
output: JSON array of {action, reason}
```

**Filter:**
- Skip anything in any `to_delete_*.json`
- Optionally exclude specific personal contacts who warrant manual review
  (e.g. friends — keep all their email in inbox)

**Outputs:**
- `triage_results.json` with `action_needed` (detail) + `to_archive` (IDs)

### Step 8 — Archive

```python
archive_emails.py --input to_archive.json
```

Uses Gmail `batchModify`: removes INBOX label, does NOT add TRASH. Mail
remains in All Mail, searchable, forever. Prompts for `yes`.

### Step 9 — What's left in the inbox

After all of the above, the inbox should contain:
- ~80-100 action items per cleanup cycle
- Any specific personal senders pulled out for manual review
- Anything that arrived during the cleanup itself

Optionally, generate a `REMAINING.md` listing the action items by date and
relevance, with checkboxes for manual processing.

## Repeating

To run periodically:

1. **Re-snapshot** the inbox to a fresh ndjson
2. **Filter to "new" arrivals** (IDs not in the previous snapshot)
3. Run steps 2-8 on the new subset
4. Repeat unsubscribes when new high-volume senders emerge

## Tool inventory

| Tool                      | Purpose                                  | Mutates Gmail? |
|---------------------------|------------------------------------------|----------------|
| `fetch_inbox.py`          | Snapshot inbox to ndjson                 | No (read-only) |
| `marketing_senders.py`    | Find high-volume marketing senders       | No             |
| `emails_to_delete.py`     | Round 1: classify per-email via Claude   | No             |
| `round2_delete.py`        | Round 2: re-filter using direct signals  | No             |
| `round3_marketing.py`     | Round N: same as round2 but for new ndjson | No           |
| `triage_inbox.py`         | Action vs archive classifier             | No             |
| `delete_emails.py`        | Apply TRASH label from ID list           | **Yes**        |
| `archive_emails.py`       | Remove INBOX label from ID list          | **Yes**        |

## Future automation notes

If automating periodically:
- Run nightly: fetch + filter new emails + marketing classifier + trash
- Run weekly: triage + archive
- Run monthly: regenerate marketing_senders + check for new unsubscribe candidates
- Never auto-archive personal senders (maintain a manual allowlist)
- Always preserve the per-round JSON outputs for auditability and rollback
- Consider: a "review queue" for any email the classifier was uncertain on
  (e.g., kept-but-not-clearly-transactional) — these accumulate human-flagged
  rules for next run
