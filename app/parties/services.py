"""Party services (spec §M4, PTY-03..09)."""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.accounting import posting
from app.accounting.models import Account, JournalEntry, JournalLine
from app.core import audit, fx
from app.core.money import quantize_amount, quantize_rate, to_decimal
from app.extensions import db
from app.parties.models import Party, PartyAccount, PartyTxn

BOOK = "AED"


class InsufficientPartyBalance(Exception):
    pass


# --- party + sub-accounts -------------------------------------------------
def create_party(*, name_ar, phone="", address="", types="person", branch_id=None,
                 notes="", user_id=None):
    p = Party(name_ar=name_ar, phone=phone, address=address, types=types,
              branch_id=branch_id, notes=notes, created_by_id=user_id)
    db.session.add(p)
    db.session.flush()
    audit.record(action="party.create", entity="party", entity_id=p.id,
                 new={"name": name_ar})
    return p


def sub_account(party: Party, currency_code: str, *, user_id=None):
    """Get or create the party's sub-account in a currency (PTY-02)."""
    currency_code = currency_code.upper()
    pa = db.session.scalar(db.select(PartyAccount).filter_by(
        party_id=party.id, currency_code=currency_code))
    if pa:
        return pa
    parent = db.session.scalar(db.select(Account).filter_by(code="1204"))
    n = db.session.scalar(db.select(db.func.count(Account.id))
                          .filter(Account.code.like("1204-%")))
    acc = Account(code=f"1204-{(n or 0) + 1:03d}",
                  name_ar=f"{party.name_ar} — {currency_code}",
                  name_en=f"{party.name_ar} {currency_code}", type="asset",
                  is_postable=True, parent_id=parent.id if parent else None,
                  created_by_id=user_id)
    db.session.add(acc)
    db.session.flush()
    pa = PartyAccount(party_id=party.id, currency_code=currency_code,
                      account_id=acc.id)
    db.session.add(pa)
    db.session.flush()
    return pa


def opening_balance(*, party, currency_code, amount, user_id=None):
    """Set a party's opening balance (money already with them) — MIG-04."""
    currency_code = currency_code.upper()
    amt = quantize_amount(amount)
    if amt == 0:
        return None
    pa = sub_account(party, currency_code, user_id=user_id)
    r = fx.rate_to_book(currency_code) or quantize_rate(1)
    lines = [
        posting.line(pa.account_id, currency=currency_code, amount=amt,
                     side="debit", fx_rate=r),
        posting.line(posting.account_for("opening.equity"), currency=currency_code,
                     amount=amt, side="credit", fx_rate=r),
    ]
    e = _post(lines, branch_id=party.branch_id, doc="party.opening",
              doc_id=party.id, memo=f"رصيد افتتاحي — {party.name_ar}", user_id=user_id)
    _txn(party, currency_code, "pay", amt, e, "رصيد افتتاحي", user_id)
    return e


def party_balance(party: Party, currency_code: str) -> Decimal:
    pa = db.session.scalar(db.select(PartyAccount).filter_by(
        party_id=party.id, currency_code=currency_code.upper()))
    if pa is None:
        return Decimal("0")
    return _native_balance(pa.account_id)


def _native_balance(account_id):
    dr, cr = db.session.execute(
        db.select(db.func.coalesce(db.func.sum(JournalLine.debit_original), 0),
                  db.func.coalesce(db.func.sum(JournalLine.credit_original), 0))
        .join(JournalEntry, JournalEntry.id == JournalLine.entry_id)
        .filter(JournalLine.account_id == account_id,
                JournalEntry.status == "posted")).one()
    return quantize_amount(to_decimal(dr) - to_decimal(cr))


# --- operations -----------------------------------------------------------
def pay_to_party(*, party, from_treasury, amount, party_currency=None, rate=None,
                 memo=None, user_id=None):
    """Give money from a treasury to a party (PTY-03/04). Cross-currency allowed."""
    src_cur = from_treasury.currency_code
    party_currency = (party_currency or src_cur).upper()
    sent = quantize_amount(amount)
    pa = sub_account(party, party_currency, user_id=user_id)

    if src_cur == party_currency:
        received = sent
        r_src = fx.rate_to_book(src_cur) or quantize_rate(1)
        lines = [
            posting.line(pa.account_id, currency=party_currency, amount=received,
                         side="debit", fx_rate=r_src),
            posting.line(from_treasury.account_id, currency=src_cur, amount=sent,
                         side="credit", fx_rate=r_src),
        ]
    else:  # cross-currency (PTY-04)
        conv = fx.require_rate(src_cur, party_currency, manual=rate)  # src->party
        received = quantize_amount(sent * conv)
        r_src = fx.rate_to_book(src_cur) or quantize_rate(1)
        r_party = fx.rate_to_book(party_currency) or quantize_rate(1)
        lines = _cross_lines(debit_account=pa.account_id, debit_cur=party_currency,
                             debit_amt=received, debit_rate=r_party,
                             credit_account=from_treasury.account_id, credit_cur=src_cur,
                             credit_amt=sent, credit_rate=r_src)

    e = _post(lines, branch_id=from_treasury.branch_id, doc="party.pay",
              doc_id=party.id, memo=memo or f"صرف لـ {party.name_ar}", user_id=user_id)
    _txn(party, party_currency, "pay", received, e, memo, user_id)
    audit.record(action="party.pay", entity="party", entity_id=party.id,
                 new={"amount": str(received), "currency": party_currency})
    return e


def refund_from_party(*, party, to_treasury, amount, party_currency=None, rate=None,
                      memo=None, user_id=None):
    """Party returns money to a treasury (PTY-05). Realized FX if rate moved."""
    dst_cur = to_treasury.currency_code
    party_currency = (party_currency or dst_cur).upper()
    give_back = quantize_amount(amount)  # in party currency
    if give_back > party_balance(party, party_currency):
        raise InsufficientPartyBalance(
            f"المبلغ يتجاوز رصيد الطرف بعملة {party_currency}.")
    pa = sub_account(party, party_currency, user_id=user_id)

    if dst_cur == party_currency:
        r = fx.rate_to_book(dst_cur) or quantize_rate(1)
        lines = [
            posting.line(to_treasury.account_id, currency=dst_cur, amount=give_back,
                         side="debit", fx_rate=r),
            posting.line(pa.account_id, currency=party_currency, amount=give_back,
                         side="credit", fx_rate=r),
        ]
    else:
        conv = fx.require_rate(party_currency, dst_cur, manual=rate)  # party->dst
        received = quantize_amount(give_back * conv)
        r_dst = fx.rate_to_book(dst_cur) or quantize_rate(1)
        r_party = fx.rate_to_book(party_currency) or quantize_rate(1)
        lines = _cross_lines(debit_account=to_treasury.account_id, debit_cur=dst_cur,
                             debit_amt=received, debit_rate=r_dst,
                             credit_account=pa.account_id, credit_cur=party_currency,
                             credit_amt=give_back, credit_rate=r_party)

    e = _post(lines, branch_id=to_treasury.branch_id, doc="party.refund",
              doc_id=party.id, memo=memo or f"استرداد من {party.name_ar}",
              user_id=user_id)
    _txn(party, party_currency, "refund", give_back, e, memo, user_id)
    audit.record(action="party.refund", entity="party", entity_id=party.id,
                 new={"amount": str(give_back), "currency": party_currency})
    return e


def party_spend(*, party, expense_account_id, amount, currency, memo=None,
                user_id=None):
    """Party spends from its balance on an expense (PTY-05)."""
    currency = currency.upper()
    amt = quantize_amount(amount)
    if amt > party_balance(party, currency):
        raise InsufficientPartyBalance(
            f"المصروف يتجاوز رصيد الطرف بعملة {currency}.")
    pa = sub_account(party, currency, user_id=user_id)
    r = fx.rate_to_book(currency) or quantize_rate(1)
    lines = [
        posting.line(expense_account_id, currency=currency, amount=amt, side="debit", fx_rate=r),
        posting.line(pa.account_id, currency=currency, amount=amt, side="credit", fx_rate=r),
    ]
    e = _post(lines, branch_id=party.branch_id, doc="party.spend", doc_id=party.id,
              memo=memo or f"صرف من {party.name_ar}", user_id=user_id)
    _txn(party, currency, "spend", amt, e, memo, user_id)
    audit.record(action="party.spend", entity="party", entity_id=party.id,
                 new={"amount": str(amt), "currency": currency})
    return e


def transfer_between_parties(*, from_party, to_party, amount, currency, memo=None,
                             user_id=None):
    currency = currency.upper()
    amt = quantize_amount(amount)
    if amt > party_balance(from_party, currency):
        raise InsufficientPartyBalance("المبلغ يتجاوز رصيد الطرف المرسل.")
    src = sub_account(from_party, currency, user_id=user_id)
    dst = sub_account(to_party, currency, user_id=user_id)
    r = fx.rate_to_book(currency) or quantize_rate(1)
    lines = [
        posting.line(dst.account_id, currency=currency, amount=amt, side="debit", fx_rate=r),
        posting.line(src.account_id, currency=currency, amount=amt, side="credit", fx_rate=r),
    ]
    e = _post(lines, branch_id=from_party.branch_id, doc="party.transfer",
              doc_id=from_party.id,
              memo=memo or f"تحويل من {from_party.name_ar} إلى {to_party.name_ar}",
              user_id=user_id)
    _txn(from_party, currency, "transfer_out", amt, e, memo, user_id)
    _txn(to_party, currency, "transfer_in", amt, e, memo, user_id)
    return e


# --- helpers --------------------------------------------------------------
def _cross_lines(*, debit_account, debit_cur, debit_amt, debit_rate,
                 credit_account, credit_cur, credit_amt, credit_rate):
    """Two legs in different currencies + an FX plug so book balances (§CUR-04)."""
    book_dr = quantize_amount(debit_amt * debit_rate)
    book_cr = quantize_amount(credit_amt * credit_rate)
    lines = [
        posting.line(debit_account, currency=debit_cur, amount=debit_amt,
                     side="debit", fx_rate=debit_rate),
        posting.line(credit_account, currency=credit_cur, amount=credit_amt,
                     side="credit", fx_rate=credit_rate),
    ]
    diff = quantize_amount(book_cr - book_dr)  # credit heavier -> need debit plug
    if diff > 0:
        lines.append(posting.line(posting.account_for("fx.loss"), currency=BOOK,
                                  amount=diff, side="debit", fx_rate=1))
    elif diff < 0:
        lines.append(posting.line(posting.account_for("fx.gain"), currency=BOOK,
                                  amount=-diff, side="credit", fx_rate=1))
    return lines


def _post(lines, *, branch_id, doc, doc_id, memo, user_id):
    e = posting.build_entry(entry_date=date.today(), branch_id=branch_id,
                            source_doc_type=doc, source_doc_id=doc_id, memo=memo,
                            user_id=user_id, lines=lines)
    posting.post_entry(e, user_id=user_id)
    return e


def _txn(party, currency, kind, amount, entry, memo, user_id):
    db.session.add(PartyTxn(party_id=party.id, currency_code=currency, kind=kind,
                            amount=quantize_amount(amount), memo=memo,
                            journal_entry_id=entry.id, created_by_id=user_id))
    db.session.flush()


# --- summary (PTY-09) -----------------------------------------------------
def party_summary(party: Party):
    """Per currency: took / returned / spent / remaining (PTY-09)."""
    rows = {}
    for pa in party.accounts:
        rows.setdefault(pa.currency_code, {"took": Decimal("0"),
                                           "returned": Decimal("0"),
                                           "spent": Decimal("0")})
    txns = db.session.scalars(db.select(PartyTxn).filter_by(party_id=party.id)).all()
    for t in txns:
        b = rows.setdefault(t.currency_code, {"took": Decimal("0"),
                                              "returned": Decimal("0"),
                                              "spent": Decimal("0")})
        if t.kind in ("pay", "transfer_in"):
            b["took"] += t.amount
        elif t.kind in ("refund", "transfer_out"):
            b["returned"] += t.amount
        elif t.kind == "spend":
            b["spent"] += t.amount
    out = []
    for cur, b in rows.items():
        remaining = quantize_amount(b["took"] - b["returned"] - b["spent"])
        out.append({"currency": cur, "took": quantize_amount(b["took"]),
                    "returned": quantize_amount(b["returned"]),
                    "spent": quantize_amount(b["spent"]), "remaining": remaining,
                    "balance": party_balance(party, cur)})
    return out


def all_parties_summary():
    return [{"party": p, "rows": party_summary(p)}
            for p in db.session.scalars(db.select(Party)).all()]


# --- "Where is the money?" (PTY-08) ---------------------------------------
def where_is_money(*, display_currency=BOOK, at=None):
    """Aggregate all money locations by currency, with a chosen-currency total."""
    from app.treasury.models import Treasury, BankAccount

    buckets = {}  # currency -> {treasuries, banks, parties, funders}

    def add(currency, key, amount):
        b = buckets.setdefault(currency, {"treasuries": Decimal("0"),
                                          "banks": Decimal("0"),
                                          "parties": Decimal("0"),
                                          "funders": Decimal("0")})
        b[key] += amount

    for t in db.session.scalars(db.select(Treasury).filter_by(is_active=True)):
        add(t.currency_code, "treasuries", _native_balance(t.account_id))
    for bk in db.session.scalars(db.select(BankAccount).filter_by(is_active=True)):
        add(bk.currency_code, "banks", _native_balance(bk.account_id))
    for pa in db.session.scalars(db.select(PartyAccount)):
        add(pa.currency_code, "parties", _native_balance(pa.account_id))
    try:
        from app.sales.models import Funder
        for f in db.session.scalars(db.select(Funder)):
            add(f.currency_code, "funders", _native_balance(f.account_id))
    except Exception:
        pass

    rows, grand = [], Decimal("0")
    for cur, b in sorted(buckets.items()):
        total = quantize_amount(b["treasuries"] + b["banks"] + b["parties"]
                                + b["funders"])
        rate = fx.get_rate(cur, display_currency, at) or quantize_rate(1)
        converted = quantize_amount(total * rate)
        grand += converted
        rows.append({"currency": cur, **{k: quantize_amount(v) for k, v in b.items()},
                     "total": total, "rate": rate, "converted": converted})
    return {"rows": rows, "grand": quantize_amount(grand),
            "display_currency": display_currency}
