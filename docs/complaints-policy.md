# RBGA Complaints Policy

*Status: draft for exec sign-off. This document is the spec for the complaints
module — the front-end form copy and the data model must comply with it. See
`CLAUDE.md` for the technical constraints already in force.*

## 1. Purpose

The RMIT Board Game Association (RBGA) provides an **anonymous** channel for
members to raise concerns about the conduct of a member, the committee, an exec,
or the president. This policy states what we collect, what we promise submitters,
who may read complaints, how they are escalated, and how long they are kept.

## 2. What we collect

A complaint record contains **only**:

- **category** — who the complaint is *about* (member / committee / exec / president);
- **body** — the complaint text;
- **contact** — *optional*, and only if the submitter chose to leave one;
- **created_at** — server timestamp.

The server records **nothing identifying**: no IP address, no user agent, no
session identifier. This is enforced in code (`rbga/api/routers/complaints.py`)
and must stay that way.

## 3. Our promise to submitters (form copy)

The submission form must state, in plain language:

- Complaints are **anonymous** — we do not record who you are.
- The **contact field is optional**. It is the *only* way you can de-anonymise
  yourself, so leave it blank if you want to stay anonymous. Only fill it in if
  you want a reply.
- Where your complaint goes (see the escalation ladder below), and that a
  **president-level complaint is forwarded to RUSU**, an external body — if you
  provided contact details, they may be shared with RUSU as part of that referral.

## 4. Who can read complaints

- **Submitting is public.** Anyone can `POST` a complaint.
- **Reading is restricted.** `GET /complaints` requires the reviewer token
  (`COMPLAINTS_API_TOKEN`). If no token is configured, **nobody** can read
  (fail closed).
- Reviewer-token access is limited to the roles named in the escalation ladder
  who are handling a given complaint. Token holders must not disclose complaint
  contents outside the handling process described here.

## 5. Escalation ladder

`category` records who the complaint is **about** (the subject). The **handler**
is derived from it, and the guiding rule is **conflict of interest: a complaint
is never handled by the person it concerns, or by a body they sit on.**

| Complaint is about | Handled by | Escalates to (if unresolved or conflicted) |
|--------------------|------------|--------------------------------------------|
| A member           | Committee  | Exec                                       |
| The committee      | Exec       | President                                  |
| An exec            | President  | RUSU                                       |
| The president      | **RUSU directly** (skip the internal chain) | — |

RUSU (RMIT University Student Union) is the **external backstop** in two cases:

1. **President-level complaints** — there is no impartial internal handler above
   the president, so these go straight to RUSU.
2. **Anything the internal chain cannot or will not resolve.**

## 6. External disclosure to RUSU

Escalation to RUSU is a disclosure to a party outside the club and is the most
privacy-sensitive step in this process. When a complaint is referred to RUSU:

- Only the **category** and **body** are forwarded by default.
- The **contact** field is forwarded **only if the submitter provided one**, and
  the form must have told them this could happen (see §3).
- The referral, and what was forwarded, is recorded on the complaint
  (`status = escalated`, `escalated_to = rusu`).

## 7. Complaint lifecycle

Each complaint carries a **status**: `new → acknowledged → escalated → closed`
(escalated is optional; not every complaint is escalated). The `escalated_to`
field records the handler a complaint was escalated to (committee / exec /
president / rusu) when status is `escalated`.

## 8. Retention

*(The retention period is to be confirmed by exec — set it as the number of days
in `COMPLAINTS_RETENTION_DAYS`.)*

- Complaints are retained for **[N days]** after they are `closed`, then
  permanently deleted.
- Deletion is automated: `python -m rbga.db.purge_complaints` runs on a schedule
  (cron on the box) and deletes complaints whose `closed_at` is older than
  `COMPLAINTS_RETENTION_DAYS`. If the variable is unset, nothing is purged
  (fail-safe). The purge logs only a count, never complaint contents.

## 9. Where complaint data lives

- Complaints live in their **own database schema** (`COMPLAINTS_SCHEMA`) with
  their **own DB role**, so a leak of the bot/board-games credentials cannot
  reach them.
- Complaint data must move **off personal infrastructure** (the home Debian box)
  to the **club-owned Oracle Cloud instance** by **[target date]**. The home
  server is not the long-term home for complaint records.

## 10. Going public — the gate

This module may go public only once **all** of the following are true:

1. This policy is signed off by the exec.
2. The submission form shows the §3 promises and the escalation/RUSU disclosure.
3. A retention purge job exists (§8).
4. Complaint data is on the club-owned Oracle instance with its own DB role (§9).

The backend endpoints existing is **not** sufficient to go live.
