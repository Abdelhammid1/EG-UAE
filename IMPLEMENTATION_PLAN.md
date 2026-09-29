# Implementation Plan — النظام المحاسبي والإداري (مصر والإمارات)

**Source of truth:** `مواصفات التنفيذ النظام المحاسبي والإداري (مصر والإمارات).md`
**Stack:** Flask 3 · PostgreSQL · Jinja2 + HTMX + Alpine.js + Tailwind
**Deployment:** Cloud, single tenant
**Team:** Solo developer + Claude
**Date:** 2026-09-29

---

## 0. The one-paragraph summary

We build a single-company accounting kernel first and hang every business module off it. Nothing touches a balance directly — every movement of money or stock produces an immutable journal entry. Every configurable number lives in a settings registry, never in code, and every document freezes the settings that applied to it. On top of that kernel we build a UI whose guiding rule is: **the user describes what happened; the system does the accounting.** A cashier never sees a debit. An owner never sees a form with 40 fields.

---

## 1. The decision you asked about: Stitch vs. Claude-built design system

**Recommendation: I build the design system and templates. Do not use Stitch for this product.**

This is not a close call, and here is the honest reasoning rather than a preference:

| Factor | Google Stitch | Claude-built design system |
|---|---|---|
| Arabic / RTL | LTR-first. Mirrored layouts, Arabic typography, numeral direction and form flow all need manual repair on every screen. | RTL is a first-class constraint from the first component. One `dir` switch flips the whole app. |
| Screen count & consistency | Generates each screen independently → 60 screens drift into 60 dialects. Consistency *is* the product in an ERP. | One token set + one macro library. Screen 60 looks exactly like screen 1 because it reuses the same parts. |
| Data density | Tuned for consumer/marketing/mobile surfaces. Weak at dense tables, totals rows, drill-downs, journal panels. | Built specifically for accounting density: tabular numerals, sticky totals, inline drill-down. |
| Output fidelity | Produces a picture or throwaway HTML. You then re-implement it in Jinja by hand — twice the work, and the design degrades in translation. | Produces the actual Jinja macros + Tailwind classes your Flask app renders. What you approve is what ships. |
| Iteration with your boss | Re-prompt, regenerate, re-implement. Slow loop. | Change a token or a macro, every screen updates. Same-day turnaround on feedback. |
| Domain logic in the UI | Cannot know that "sell below minimum price" must be blocked with a reason, or that a settings change needs an old→new confirmation dialog. | The UI is designed around the spec's actual rules, so the guardrails are part of the design, not bolted on. |

**Where Stitch could still earn 30 minutes:** generating two or three visual moods for the POS screen if you want options to put in front of your boss. That is optional, off the critical path, and I've included a ready-to-paste prompt in Appendix A so you are not blocked if you want to try it.

**What I do instead, in week 1:** a clickable HTML prototype of the 8 screens that decide whether this product feels good — POS, shift close, owner dashboard, sales invoice, transfer permit wizard, treasury transfer, product card, settings change dialog. Real Arabic, real RTL, real data shapes. You show it to your boss before a single line of backend is written. Feedback goes straight into the component library that the Flask app then consumes.

---

## 2. The five things that make or break this build

Get these right and the rest is ordinary CRUD. Get them wrong and no amount of UI polish saves it.

### 2.1 Money is `Decimal`, never `float`

- Python `Decimal` everywhere, PostgreSQL `NUMERIC(19,4)` for amounts, `NUMERIC(19,8)` for FX rates.
- A `Money` value object (`amount`, `currency`) with arithmetic that refuses to add two different currencies.
- Decimal places per currency come from the `currency` table, not from code.
- **One rounding rule for the whole system**: `ROUND_HALF_UP`, stored as a setting, applied in exactly one function. (Spec §15.1)
- A test that runs a thousand random transactions and asserts the trial balance is exactly zero.

### 2.2 Every money row carries three values

Spec §5.2 is non-negotiable and it shapes the schema:

```
amount_original   NUMERIC(19,4)
currency_code     CHAR(3)
fx_rate           NUMERIC(19,8)
amount_aed        NUMERIC(19,4)   -- book currency
```

This appears as a reusable SQLAlchemy composite on journal lines, invoice lines, payments, expenses — everywhere. `amount_aed` is stored, not computed on read, so history never shifts when a rate changes.

### 2.3 The settings registry is a subsystem, not a table of key/values

Spec §1 principle 3 and §2 demand a lot more than a config file:

```
setting_definition(key, section, value_type, scope, locks_after_first_use,
                   requires_owner_approval, default_value, label_ar, label_en)

setting_value(id, key, scope_type, scope_id, value_json,
              effective_from, created_by, created_at, approved_by)
```

- `settings.get(key, scope, at=document_date)` — reads are always time-aware.
- Changing a value: confirmation dialog showing **name / old value / new value / effective from**, the fixed sentence from §2 ("هذا التغيير يسري على العمليات الجديدة فقط…"), then an audit entry.
- Under every setting, its own change history renders inline. (§2 rule 5)
- Locked-after-first-use settings (book currency, treasury currency, bank account currency) are enforced by a service check *and* a DB constraint.

### 2.4 Documents snapshot their settings

Every document table gets `settings_snapshot JSONB`. At creation we freeze the tax rate, valuation method, funder commission, FX rate, expense-allocation method — whatever applied. Nothing about a posted document is ever recomputed from live settings. This single column resolves spec §1.4, §2.6, §10.6, §16.4 and §16.23 at once.

### 2.5 Immutability is enforced by the database, not by discipline

- `audit_log`: the application's DB role has `INSERT` and `SELECT` only. `REVOKE UPDATE, DELETE`. A trigger raises on any attempt. (§15.1.3)
- `journal_entry` / `journal_line` with `status = 'posted'`: `BEFORE UPDATE OR DELETE` trigger raises an exception. Corrections are reversal entries only. (§13.5)
- Document numbering: `SELECT … FOR UPDATE` on `document_sequence(branch_id, doc_type)` inside the same transaction as the insert. This is the fix for edge case §16.22.
- Stock issue: `SELECT … FOR UPDATE` on candidate batches. This is the fix for §16.16 and §15.2.

---

## 3. Architecture

### 3.1 Project layout

```
marsoud/
├── app/
│   ├── __init__.py            # application factory
│   ├── extensions.py          # db, migrate, login, babel, csrf, rq
│   ├── core/
│   │   ├── money.py           # Money, rounding, currency scales
│   │   ├── fx.py              # rate fetch, manual override, rate log
│   │   ├── settings.py        # registry, time-aware get, change flow
│   │   ├── audit.py           # append-only log + @audited decorator
│   │   ├── numbering.py       # per-branch per-type sequences
│   │   ├── permissions.py     # matrix, @requires, branch scoping
│   │   └── approvals.py       # separation of duties, owner approval
│   ├── auth/                  # login, 2FA, lockout, session timeout
│   ├── admin/                 # the single settings screen (§2)
│   ├── accounting/            # COA, journal, periods, FX differences
│   ├── treasury/              # cash boxes, banks, transfers, reconciliation
│   ├── inventory/             # products, warehouses, batches, stocktake
│   ├── shipping/              # transfer permits, landed cost (§8)
│   ├── purchasing/            # suppliers, PO, invoices, returns
│   ├── custody/               # العهد (§12)
│   ├── sales/                 # customers, invoices, returns, installments, BNPL
│   ├── pos/                   # POS screen + shifts (§11)
│   ├── reports/               # §14, async via RQ
│   └── ui/                    # Jinja macros = the design system
├── templates/
├── static/          # tailwind output, alpine, htmx, fonts
├── migrations/      # alembic
├── tests/
├── docker-compose.yml
└── pyproject.toml
```

### 3.2 Libraries

| Need | Choice | Why |
|---|---|---|
| Web | Flask 3 (app factory + blueprints) | Your call, and a good one for this shape of app |
| ORM | SQLAlchemy 2.0 + Alembic | Explicit transactions, `FOR UPDATE`, composites for Money |
| DB | PostgreSQL 16 | `NUMERIC`, JSONB snapshots, triggers, advisory locks |
| Auth | Flask-Login + pyotp | 2FA for owner and sysadmin (§15.2.2) |
| Forms/CSRF | Flask-WTF | CSRF on every mutation |
| i18n | Flask-Babel | ar/en switch, RTL flip (§15.5) |
| Front-end | HTMX + Alpine.js + Tailwind | No SPA, no build pipeline beyond the Tailwind CLI |
| Jobs | RQ + Redis | Async reports (§15.3), nightly FX fetch, backups |
| Excel | openpyxl | Export on every list + opening-balance import |
| PDF | WeasyPrint | Best Arabic/RTL PDF output in Python |
| Printing | ESC/POS via `python-escpos` | Thermal receipts (§11) |
| Tests | pytest + factory_boy + testcontainers | The 24 edge cases in §16 become 24 test modules |

### 3.3 Request pipeline

Every request passes through, in order:

1. **Auth** — logged in, session not idle-expired.
2. **Permission** — `@requires('sales.invoice.create')` against the effective permission set (union of the user's roles + individual grants, per §3 rule 1).
3. **Branch scope** — `g.allowed_branch_ids` is set from the user; every query on a branch-scoped model is auto-filtered by a SQLAlchemy event listener. A user can never read another branch's row even via a crafted URL. (§15.2.5)
4. **Transaction** — one DB transaction per request; posting services commit or raise.
5. **Audit** — sensitive handlers wrapped by `@audited`, capturing old and new values.

Permissions are enforced here, server-side, on every request. The UI hides buttons as a courtesy, never as a control. (§15.2.4)

---

## 4. Data model — the parts worth deciding now

### 4.1 Accounting kernel

```
account(id, code, name_ar, name_en, type, parent_id, is_active)
accounting_period(id, start_date, end_date, status, closed_by, approved_by)
journal_entry(id, number, date, branch_id, period_id, source_doc_type,
              source_doc_id, status, created_by, posted_by, posted_at,
              reverses_entry_id)
journal_line(id, entry_id, account_id, debit_*, credit_*, memo)   -- * = money triple
```

- Posting service asserts `Σ debit_aed == Σ credit_aed` before writing `status='posted'`.
- Posting into a closed period raises. (§13.6)
- `account_mapping(operation_code, account_id)` — the §13 table of automatic entries is **configuration**, not hardcoded. The accountant binds operations to accounts from settings.

### 4.2 Inventory valuation

```
stock_batch(id, product_id, warehouse_id, qty_received, qty_remaining,
            unit_cost_*, valuation_method_at_entry, entered_at, source_doc)
stock_movement(id, product_id, warehouse_id, batch_id, direction, qty,
               unit_cost_*, doc_type, doc_id, created_at)
```

The issue algorithm (spec §7.3, deliberately simple — no full cost layers):

1. `SELECT … FOR UPDATE` all batches for (product, warehouse) with `qty_remaining > 0`.
2. Group by `valuation_method_at_entry`.
3. FIFO groups: consume oldest first. Weighted-average groups: consume at that group's weighted average.
4. Write movements, decrement `qty_remaining`, store the resulting COGS **on the sales document** (§13, "تكلفة البضاعة المباعة").

A product holding batches under two methods shows split by batch in the warehouse report. (§7.4)

### 4.3 Serial tracking

```
product_serial(id, product_id, serial, status, warehouse_id, batch_id,
               received_doc_id, sold_doc_id)
UNIQUE (product_id, serial)
```

Covers §16.14 (cannot sell a sold serial — and the error tells you which invoice sold it) and §16.15 (cannot receive the same serial twice).

### 4.4 Transfer permits & landed cost

```
transfer_permit(id, number, from_warehouse, to_warehouse, status,
                fx_rate, sent_at, received_at, closed_at)
transfer_line(id, permit_id, product_id, qty_sent, qty_received,
              unit_cost_*, allocated_shipping, allocated_customs, final_unit_cost)
transfer_expense(id, permit_id, kind, amount_*, paid_from_type, paid_from_id,
                 allocation_method, allocated_at)
```

States: `draft → sent → received → closed`, cancellable only before `sent`. On send, value moves to the **goods-in-transit** account so warehouse reports never show a phantom shortfall (§8.2). Expenses recorded after receipt post a cost-adjustment entry that touches only remaining quantity — already-sold units are never re-priced (§8 rule 1).

### 4.5 BNPL (تابي / تمارا)

```
funder(id, name, branch_id, commission_pct, fixed_fee, receivable_account_id,
       settlement_bank_account_id)   -- no default values, per §10
funder_settlement(id, funder_id, received_amount_*, bank_account_id,
                  invoice_ids[], commission_amount_*, settled_at)
```

At sale: receivable on the **funder**, not the customer; status "awaiting settlement"; no cash movement. At settlement: the accountant enters what actually arrived, the system computes the gap and books it as financing commission expense, then closes the receivables. Settling more than owed is blocked unless a reason is recorded and approved (§16.20).

---

## 5. UI plan — "very very easy" made concrete

This is the part your boss will judge. Ease of use here is not a style; it's a set of enforced rules.

### 5.1 The seven laws

1. **One job per screen.** If a screen can do two things, it becomes two screens.
2. **The home screen is the role.** A cashier opens the app into full-screen POS and nothing else. An accountant opens into a work queue: things awaiting approval, unsettled funders, unreconciled bank lines, open consignments. An owner opens into the dashboard. Nobody hunts through a menu.
3. **Navigation is verbs, not modules.** Not `المحاسبة ← القيود ← جديد`. Instead: `سجّل مصروف`, `حوّل فلوس`, `استلم بضاعة`, `اقفل الوردية`. The module tree still exists for accountants who want it, one level down.
4. **The accounting is invisible by default.** Every document renders a collapsed `القيد المحاسبي` panel showing the debits and credits it generated. The cashier never opens it. The accountant always does. Same screen, two audiences.
5. **Block before, never error after.** A price below the minimum is not a validation error on submit — the field refuses it as you type and shows *why* and *who can override*. Consignment balance counts down live as the rep types. The save button is disabled with a visible reason, never silently.
6. **Every number is a door.** A figure on any report drills to the documents behind it, and a document drills to its journal entry, and a journal line drills back to its document. This single behaviour eliminates most "why is this number wrong?" support calls.
7. **Nothing is ever lost.** Drafts autosave. Nothing deletes. Everything reverses, and the original stays visible — which the spec requires anyway (§1.5), so we turn a compliance rule into a safety feature the user can feel.

### 5.2 Design tokens

- **Typeface:** IBM Plex Sans Arabic (matching Latin companion, free, excellent at UI sizes). Money and quantities in Latin digits with `font-variant-numeric: tabular-nums` — columns align, and Latin digits are unambiguous in accounting.
- **Color:** neutral slate surface; a single deep-teal primary; semantic green/amber/red reserved for state only. **Numbers are never coloured decoratively** — red means negative, full stop.
- **Density:** one comfortable default, with a compact toggle on tables that persists per user.
- **Shape:** 8px radius, 1px borders instead of shadows, shadows only for overlays.
- **Dark mode:** yes. Cashiers work evenings and the POS is a full-screen light source.
- **RTL:** Tailwind logical properties (`ms-`, `me-`, `ps-`, `pe-`) exclusively. Icons that imply direction get mirrored via `rtl:-scale-x-100`.

### 5.3 Component library (`app/ui/` Jinja macros)

| Group | Components |
|---|---|
| Shell | `app_shell`, `sidebar`, `topbar`, `breadcrumb`, `global_search` (Ctrl+K over invoices, barcodes, serials, customers, suppliers), `branch_switcher`, `lang_toggle`, `user_menu` |
| Data | `data_table` (filter + sort + paginate + Excel/PDF export, one implementation used by every list per §4.5), `empty_state`, `stat_tile`, `kpi_row`, `badge`, `money`, `qty`, `date_time` |
| Forms | `field`, `select`, `combobox` (HTMX server-search), `money_input` (amount + currency + suggested rate + live AED preview), `date_picker`, `file_drop`, `switch`, `radio_cards` |
| Feedback | `toast`, `confirm_dialog`, `setting_change_dialog` (name / old / new / effective-from + the fixed §2 sentence), `inline_error`, `skeleton` |
| Documents | `doc_header` (number, date, branch, status chip), `doc_lines_editor`, `doc_totals` (sticky), `journal_panel` (collapsed), `attachments`, `audit_trail_panel`, `approval_bar` |
| POS | `pos_shell`, `product_grid`, `cart`, `payment_panel` (split payments), `shift_bar`, `numpad` |
| Flow | `wizard` (max 3 steps, permanent live-summary panel on the side) |

Wizards are reserved for exactly the four genuinely multi-part operations: transfer permit, shift close, funder settlement, period close. Everything else is a single form.

### 5.4 POS specifics (§11)

Full screen, no app chrome. Minimum 56px touch targets. Barcode scanner input is captured globally without focusing a field. Keyboard shortcuts printed on-screen, not hidden in a help page (F2 search, F4 payment, F8 hold, F9 return). Split payment is one panel with a running remainder, not a modal chain. Shift bar always visible at the top with the open drawer, cashier name and elapsed time.

### 5.5 Prototype-first, always

Every phase begins with me generating the screens as static HTML with realistic Arabic data, you review them in the browser, and only then do we wire them to Flask. A screen that is wrong is cheap to fix before it has a controller behind it.

---

## 6. Phases

Each phase ends with something demonstrable and its own tests. Estimates assume you working with me generating code; a range is given because week 1 always teaches us something.

### Phase 0 — Foundation (2 weeks)
Repo, Docker Compose (Postgres + Redis), Flask factory, Alembic. `Money` and currency registry. Auth with lockout, idle timeout, 2FA for owner/sysadmin. Users, roles, permission matrix, branch scoping. Append-only audit log with DB-level protection. Settings registry + change-confirmation flow + inline history. Document numbering under row lock. Arabic/English i18n with RTL. **The design system and the clickable prototype of the 8 key screens.**
> **Demo:** log in, switch language, see the settings screen work with real confirmation dialogs and audit history. Plus the full clickable prototype for your boss.

### Phase 1 — Accounting kernel (2 weeks)
Chart of accounts (hierarchical, default Arabic template the accountant can edit). Journal entries, posting engine, balance enforcement, DB immutability triggers. Accounting periods with owner approval to close/reopen. Reversal entries. `account_mapping` for the §13 automatic-entry table. FX service: auto fetch, manual override, full rate log, hard stop when no rate is available (§16.19). Trial balance report.
> **Demo:** post a manual entry, watch the trial balance move, try to edit a posted entry and be refused by the database.

### Phase 2 — Cash & banks (1.5 weeks)
Treasuries and bank accounts, currency locked after first movement. Deposit, withdrawal, same-currency transfer, cross-currency transfer with editable received amount and difference booked as bank charge or FX difference. Bank fees. Optional approval threshold. Bank reconciliation screen with statement import. Per-treasury and per-account reports.
> **Demo:** the §6 worked example — transfer UAE → Egypt, edit the rate, confirm, see both accounts and both journal sides.

### Phase 2.5 — Vertical slice (1 week)
Deliberate thin slice through the real kernel: one product, one warehouse, one cash sale from a minimal POS, producing a real journal entry, a real treasury movement and a real inventory decrement, visible in three reports. This de-risks the architecture before we build breadth, and gives you a working end-to-end story at roughly week 7.
> **Demo:** this is the one to show your boss. It is small, but everything in it is real.

### Phase 3 — Products & inventory (2.5 weeks)
Product card: barcode, category, UoM, default price, **minimum price**, valuation method, reorder level, optional serial tracking, weight. Warehouses, branch↔warehouse configuration (default, allowed, transfers on/off). Stock batches with valuation-method-at-entry and the issue algorithm. Stock movements, damage/loss, stocktake sessions with variance approval. Serial registry. Barcode label printing. Reorder alerts. Opening balances via Excel import with an unbalanced-import rejection (§16.24).
> **Demo:** receive stock two ways, sell it, see COGS computed per batch method.

### Phase 4 — Purchasing, suppliers & consignments (2 weeks)
Suppliers with statements and aging. Optional purchase order → purchase invoice (cash/credit). Supplier payments, partial with allocation across invoices. Purchase returns. Due-payment alerts. Consignments (العهد): issue from treasury, spend on purchases and expenses, live available balance with hard block on overspend (§16.8), attachment requirement configurable, settlement and close, alert for departed reps with open consignments (§16.9).
> **Demo:** the §12 worked example — 5,000 EGP consignment, 3,200 purchase, 1,800 remaining, all three numbers agreeing across four screens.

### Phase 5 — Inter-warehouse transfers & landed cost (1.5 weeks)
Transfer permit lifecycle. Goods-in-transit accounting. Shipping and customs as separate expense lines, multiple per permit. Allocation by value (default), quantity or weight. Receipt with quantity variance → loss entry after approval. Post-receipt cost adjustment on remaining quantity only. Per-permit report with final unit cost and suggested minimum sale price.
> **Demo:** the §8 worked example — 3,000 + 1,000 phones with 200 shipping allocating 150/50.

### Phase 6 — Sales, customers & POS (3 weeks)
Customers with credit limits (over-limit blocks credit sales except with manager permission). Quotes, sales invoices, collections with allocation, sales returns at original cost and price (§16.4). Internal installment schedules with overdue tracking and alerts. VAT per branch, optional, no default rate. BNPL: funder receivable at sale, manual settlement with commission, open-receivable and commission reports. Then POS: the full screen, shifts with opening/closing cash and over/short entries, one open shift per drawer, blocked close with held invoices (§16.21), returns and exchanges, thermal + A4 printing with "copy" reprints, daily close report.
> **Demo:** a full trading day — open shift, mixed-payment sales, a return, close with a deliberate cash shortage, see the variance entry.

### Phase 7 — Reports & dashboard (2 weeks)
Every report group in §14. Shared rules: period + branch filters, permission-aware, Excel/PDF/print, entity reports in the entity's own currency, consolidated reports in a user-chosen currency with the rate shown in the header. Financial statements: balance sheet, trial balance, income statement, cash flow, general ledger, FX differences (realized + unrealized). Unrealized FX revaluation at period close with automatic reversal next period. Owner dashboard. Large reports run async via RQ with a notification when ready.

### Phase 8 — Hardening & handover (2 weeks)
All 24 edge cases from §16 as named test modules. Concurrency tests: two simultaneous sales of the last unit, two simultaneous invoices on one sequence. Security pass: server-side permission coverage audit, branch-leak tests, HTTPS, password hashing, rate limiting. Automated daily backup **with a tested restore drill**. Performance: POS response budget, report async thresholds. Seeded demo dataset, Arabic user manual, short training videos, handover checklist.

**Total: 17–19 weeks (≈4–4.5 months).** The prototype lands in week 1 and the first genuinely real demo in week 7.

---

## 7. Risks, named honestly

| Risk | Why it bites | What we do |
|---|---|---|
| The 10 open questions in §17 | Answers can invalidate built work, especially #1 (branch↔warehouse) and #5 (who approves returns) | Every one is implemented as a setting with the spec's assumed default. A changed answer is a settings change, not a rewrite. Get written answers before Phase 3 and Phase 6 regardless. |
| E-invoicing (ETA Egypt) | Explicitly out of scope, but it is a **legal** obligation for the client | Send the written notice in §17.1 now, in Arabic, before you write code. Get it acknowledged. This is the single item most likely to become someone's fault later. |
| Simplified valuation (§7.3) | "Each batch carries its own method" is unusual and can produce a COGS the accountant disputes | Build the warehouse report showing per-batch detail early (Phase 3) and walk the accountant through it. Written sign-off. |
| Offline POS (§11, §17.9) | Marked out of scope, but it is the first thing a cashier asks for when the internet drops | Confirm in writing it is excluded. If it is ever added, it is a separate project — it changes the concurrency model everywhere. |
| Scope creep from "the old one was bad" | Being told a predecessor was bad invites unbounded expectations | The spec is the contract. Anything not in it is a written change request. Show the prototype early and often so "bad" gets defined before it gets asserted. |
| Solo bus factor | One developer, four months | Everything in git from day one, Docker Compose so the environment is reproducible, tests as the specification of intent, the plan in the repo. |

---

## 8. Definition of "perfect enough to hand to a customer"

Not a feeling — a checklist:

- [ ] Trial balance is exactly zero on a thousand-transaction fuzz test.
- [ ] All 24 edge cases in §16 have a passing named test.
- [ ] No configurable value appears as a literal anywhere in the codebase (enforced by a lint rule).
- [ ] A posted journal entry cannot be modified even with direct database access from the application role.
- [ ] The audit log cannot be modified by anyone, including the system administrator.
- [ ] Every screen works identically in Arabic RTL and English LTR.
- [ ] POS completes a sale in under 15 seconds from scan to printed receipt.
- [ ] A backup has been restored onto a clean machine and the result verified.
- [ ] A cashier who has never seen the system can complete a sale after 5 minutes of instruction.
- [ ] An accountant can trace any number on any report to its source document in three clicks.

That last pair is what your boss will actually remember.

---

## Appendix A — Stitch prompt (optional, only if you want visual alternatives)

Use this only for POS mood exploration. Do not use Stitch output as the source of truth for any screen.

> Design a point-of-sale screen for an Arabic-language retail system used in Egypt and the UAE. Right-to-left layout. Full screen, no navigation chrome. Left side: a scrollable grid of product cards showing Arabic name, price in AED, and stock count, with a category filter row above and a large search field at the top. Right side: a fixed cart panel listing added items with quantity steppers and line totals, a subtotal/VAT/total block pinned at the bottom, and a prominent payment button. A thin status bar across the top shows the cashier name, the open shift, and elapsed time. Touch-first with large targets. Calm, professional, low-saturation palette — deep teal accent on a neutral slate background, no gradients. Typeface: IBM Plex Sans Arabic. Money shown in Latin digits. Provide a dark variant.

---

## Appendix B — Spec coverage map

| Spec section | Phase |
|---|---|
| §1 Principles | 0 (enforced structurally throughout) |
| §2 Company settings | 0 |
| §3 Roles & permissions | 0 |
| §4 Core entities | 0, 1, 3 |
| §5 Currencies & FX | 1 (rates), 7 (unrealized) |
| §6 Treasuries & banks | 2 |
| §7 Products & valuation | 3 |
| §8 Transfers & shipping | 5 |
| §9 Purchasing & suppliers | 4 |
| §10 Sales, customers, BNPL | 6 |
| §11 POS & shifts | 6 |
| §12 Consignments | 4 |
| §13 Accounting & entries | 1 |
| §14 Reports | 7 (each module ships its own reports earlier) |
| §15 Audit, security, NFRs | 0 (audit/security), 8 (verification) |
| §16 Edge cases | 8 (tests), behaviour built in its own phase |
| §17 Out of scope & open items | 7 (risk register, written confirmations) |
