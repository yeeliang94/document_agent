"""Generate a synthetic supplier payment-run packet whose deliverable is a journal built to a format, and its answer key.

Everything is invented. The agent must build payment_journal.xlsx from scratch to journal_format.json, following the
payment run memo inside the packet. The traps:
- the AP open-items export has one tab per currency, with title rows above the headers
- only invoices marked Approved in the approval log are paid (some are Pending or Rejected)
- an approved invoice falls due after the 20 November cutoff and must wait for the next run
- an invoice due exactly on the cutoff date is paid
- two suppliers are ON HOLD in the supplier master and must be skipped, though their invoices are approved and due
- the pay currency sits on a hidden sheet of the supplier master
- a credit note must be netted against the same supplier's invoices (one journal, net amount)
- a "late invoices" csv attached to an email export adds invoices missing from the export, and repeats one that is
  already there (it must be paid once)
- last month's payment journal, a bank statement, a supplier contract and a superseded draft payment-run proposal are
  in the packet and are not relevant
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font

from .common import header, packet_files, write_docx, write_json, write_pdf, zip_packet

CUTOFF = date(2026, 11, 20)
RUN_DATE = '2026-11-20'
EXPORT_DATE = date(2026, 11, 13)
AP_CONTROL = '2100'
BANK = {'SGD': '1010', 'USD': '1020', 'EUR': '1030'}
CURRENCIES = ['SGD'] * 13 + ['USD'] * 7 + ['EUR'] * 4
NAMES = [
    'Amberline Packaging', 'Bluecrest Facilities', 'Cobalt Ridge Electrical', 'Driftwood Catering',
    'Eastgate Security Services', 'Fernvale Printing', 'Glasshouse Analytics', 'Halcyon Freight',
    'Ironbark Engineering', 'Juniper Office Supplies', 'Kingfisher Cleaning', 'Lattice Software',
    'Marlowe Legal Consulting', 'Northbank Telecom', 'Oakmere Furniture', 'Pebblestone Logistics',
    'Quayside Marine Parts', 'Redfern Staffing', 'Saltmarsh Chemicals', 'Thistledown Travel',
    'Umberfield Laboratories', 'Vantage Point Media', 'Willowmere Hardware', 'Yarrow Data Centres',
]  # fmt: skip
SUFFIX = {'SGD': 'Pte Ltd', 'USD': 'Inc', 'EUR': 'GmbH'}
COUNTRY = {'SGD': 'Singapore', 'USD': 'United States', 'EUR': 'Germany'}

MEMO = 'Finance/AP/Payment Runs/2026/11 Nov/20-Nov-2026 run/Payment run memo - 20 Nov 2026.docx'
AGING = 'Finance/AP/reports/Aging/2026-11/AP_OpenItems_Aging_2026-11-13.xlsx'
APPROVALS = 'Finance/AP/approvals/WF export/invoice_approval_log_nov26.csv'
MASTER = 'Finance/Master Data/Vendors/supplier_master_2026.xlsx'
EMAIL = 'Finance/AP/Payment Runs/2026/11 Nov/20-Nov-2026 run/inbox export/RE late invoices for 20 Nov run/message.txt'
LATE = (
    'Finance/AP/Payment Runs/2026/11 Nov/20-Nov-2026 run/inbox export/RE late invoices for 20 Nov run/'
    'late_invoices_20Nov.csv'
)
COA = 'Finance/GL/reference/chart_of_accounts_extract.xlsx'
LAST_MONTH = 'Finance/AP/Payment Runs/2026/10 Oct/PAY journal 23-Oct-2026 POSTED.xlsx'
DRAFT = 'Finance/AP/Payment Runs/2026/11 Nov/20-Nov-2026 run/old/payment_run_proposal_v1_DRAFT_superseded.xlsx'
STATEMENT = 'Treasury/Bank/statements/2026-10/SGD_operating_account_statement_Oct2026.pdf'
CONTRACT = 'Legal/Contracts/Suppliers/Halcyon Freight/Halcyon_Freight_Master_Services_Agreement_2025.pdf'
JUNK = 'Finance/AP/Payment Runs/2026/11 Nov/desktop.ini'


@dataclass
class Supplier:
    sid: str
    name: str
    currency: str
    terms: int
    on_hold: bool = False


@dataclass
class Item:
    sid: str
    number: str
    doc_date: date
    due: date
    amount: float  # positive; a credit note reduces what is paid
    kind: str = 'Invoice'
    status: str = 'Approved'
    late: bool = False  # only in the late-invoices csv


def _suppliers(rng: random.Random) -> list[Supplier]:
    names = NAMES[:]
    rng.shuffle(names)
    currencies = CURRENCIES[:]
    rng.shuffle(currencies)
    ids = sorted(rng.sample(range(1001, 1999), len(names)))
    people = [
        Supplier(f'S{n}', f'{name} {SUFFIX[ccy]}', ccy, rng.choice([14, 30, 30, 45, 60]))
        for n, name, ccy in zip(ids, names, currencies, strict=True)
    ]
    # Halcyon Freight has a contract in the packet, so it stays active.
    candidates = [s for s in people if not s.name.startswith('Halcyon')]
    next(s for s in candidates if s.currency == 'SGD').on_hold = True
    next(s for s in candidates if s.currency == 'USD').on_hold = True
    return people


def _number(rng: random.Random, supplier: Supplier, used: set, kind: str = 'Invoice') -> str:
    style = int(supplier.sid[1:]) % 3
    while True:
        if kind == 'Credit Note':
            number = f'CN-{rng.randint(1000, 9999)}'
        elif style == 0:
            number = f'INV-{rng.randint(10000, 99999)}'
        elif style == 1:
            number = f'{supplier.sid[1:]}-{rng.randint(100, 999)}'
        else:
            number = f'SI{rng.randint(260000, 269999)}'
        if (supplier.sid, number) not in used:
            used.add((supplier.sid, number))
            return number


def _item(rng, supplier, used, due: date, status='Approved', kind='Invoice', low=400, high=48000) -> Item:
    amount = round(rng.uniform(low, high), 2)
    return Item(supplier.sid, _number(rng, supplier, used, kind), due - timedelta(days=supplier.terms), due, amount,
                kind, status)  # fmt: skip


def _items(rng: random.Random, suppliers: list[Supplier]) -> tuple[list[Item], dict[str, str]]:
    used: set = set()
    items: list[Item] = []
    for supplier in suppliers:
        for _ in range(rng.randint(1, 3)):
            due = date(2026, 10, 20) + timedelta(days=rng.randint(0, 54))
            status = rng.choices(['Approved', 'Pending', 'Rejected'], [88, 8, 4])[0]
            items.append(_item(rng, supplier, used, due, status))
    active = [s for s in suppliers if not s.on_hold]
    picks = rng.sample(active, 6)
    traps = dict(zip(['credit_note', 'after_cutoff', 'unapproved', 'on_cutoff', 'late_new', 'late_dup'],
                     [s.sid for s in picks], strict=True))  # fmt: skip
    for supplier in picks:  # each trap supplier has something to pay in this run
        items.append(_item(rng, supplier, used, date(2026, 11, rng.randint(2, 18)), low=6000, high=30000))
    by_id = {s.sid: s for s in suppliers}
    credit = by_id[traps['credit_note']]
    items.append(_item(rng, credit, used, date(2026, 11, 6), kind='Credit Note', low=300, high=2500))
    items.append(_item(rng, by_id[traps['after_cutoff']], used, date(2026, 11, 27)))
    items.append(_item(rng, by_id[traps['unapproved']], used, date(2026, 11, 12), status='Pending'))
    items.append(_item(rng, by_id[traps['on_cutoff']], used, CUTOFF))
    for supplier in (s for s in suppliers if s.on_hold):
        items.append(_item(rng, supplier, used, date(2026, 11, 10)))
    late = _item(rng, by_id[traps['late_new']], used, date(2026, 11, 19))
    late.late = True
    items.append(late)
    other = rng.choice([s for s in active if s.sid not in traps.values()])
    late2 = _item(rng, other, used, date(2026, 11, 16))
    late2.late = True
    items.append(late2)
    traps['late_new_2'] = other.sid
    return items, traps


def _payable(item: Item, supplier: Supplier) -> bool:
    return not supplier.on_hold and item.status == 'Approved' and item.due <= CUTOFF


def _title(ws, lines):
    for line in lines:
        ws.append([line])
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([])


def _write_aging(path: Path, suppliers, items, rng):
    by_id = {s.sid: s for s in suppliers}
    wb = Workbook()
    summary = wb.active
    summary.title = 'Summary'
    _title(
        summary, ['Accounts payable - open items aging', f'Run date: {EXPORT_DATE:%d %b %Y}', 'Source: ERP AP module']
    )
    header(summary, ['Currency', 'Open items', 'Open amount'])
    for currency in ('SGD', 'USD', 'EUR'):
        rows = [i for i in items if not i.late and by_id[i.sid].currency == currency]
        sign = [(-1 if i.kind == 'Credit Note' else 1) * i.amount for i in rows]
        summary.append([currency, len(rows), round(sum(sign), 2)])
        ws = wb.create_sheet(f'Open items {currency}')
        _title(ws, ['Accounts payable - open items', f'As at {EXPORT_DATE:%d %b %Y}', f'Document currency: {currency}'])
        header(ws, ['Supplier ID', 'Supplier Name', 'Document No', 'Doc Type', 'Doc Date', 'Due Date', 'Days Past Due',
                    'Open Amount', 'Aging Bucket', 'Payment Block'])  # fmt: skip
        for i in sorted(rows, key=lambda r: (r.sid, r.due)):
            overdue = (EXPORT_DATE - i.due).days
            bucket = 'Not due' if overdue <= 0 else '1-30' if overdue <= 30 else '31-60'
            ws.append([i.sid, by_id[i.sid].name, i.number, 'KR' if i.kind == 'Invoice' else 'KG', i.doc_date, i.due,
                       max(overdue, 0), (-1 if i.kind == 'Credit Note' else 1) * i.amount, bucket,
                       rng.choice(['', '', '', 'R'])])  # fmt: skip
    legend = wb.create_sheet('Doc types')
    header(legend, ['Code', 'Meaning'])
    legend.append(['KR', 'Vendor invoice'])
    legend.append(['KG', 'Vendor credit memo'])
    legend.append(['R', 'Payment block R = invoice verification (ERP only; approvals are in the workflow log)'])
    wb.save(path)


def _write_approvals(path: Path, items, rng):
    approvers = ['j.tan', 'r.okafor', 'm.lindqvist', 'a.rahman']
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['wf_id', 'vendor_code', 'document_ref', 'doc_amount', 'approval_status', 'approver',
                         'action_date', 'comment'])  # fmt: skip
        rows = items[:]
        rng.shuffle(rows)
        for n, i in enumerate(rows, start=50001):
            acted = '' if i.status == 'Pending' else (i.doc_date + timedelta(days=rng.randint(2, 9))).isoformat()
            comment = {'Rejected': 'Price does not match PO', 'Pending': 'Awaiting budget holder'}.get(i.status, '')
            writer.writerow([f'WF{n}', i.sid, i.number, f'{(-1 if i.kind == "Credit Note" else 1) * i.amount:.2f}',
                             i.status.upper(), rng.choice(approvers) if acted else '', acted, comment])  # fmt: skip


def _write_master(path: Path, suppliers, rng):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Suppliers'
    header(ws, ['Supplier ID', 'Supplier Name', 'Country', 'Payment Terms', 'Status', 'Hold Reason', 'Buyer',
                'Created'])  # fmt: skip
    for s in suppliers:
        ws.append([s.sid, s.name, COUNTRY[s.currency], f'Net {s.terms}', 'ON HOLD' if s.on_hold else 'Active',
                   'Bank details under verification' if s.on_hold else '', rng.choice(['Procurement', 'Facilities',
                   'IT']), date(2023, rng.randint(1, 12), rng.randint(1, 28))])  # fmt: skip
    bank = wb.create_sheet('Bank & Currency')
    header(bank, ['Supplier ID', 'Pay Currency', 'Bank Name', 'Account Number', 'SWIFT', 'Last Verified'])
    for s in suppliers:
        bank.append([s.sid, s.currency, rng.choice(['Harbourline Bank', 'Meridian Trust Bank', 'Orchid Commercial Bank']),
                     f'{rng.randint(100, 999)}-{rng.randint(100000, 999999)}-{rng.randint(0, 9)}',
                     f'XMPL{rng.choice(["SG", "US", "DE"])}{rng.randint(10, 99)}', date(2026, rng.randint(1, 9), 1)])  # fmt: skip
    bank.sheet_state = 'hidden'
    wb.save(path)


def _write_late(email: Path, attachment: Path, suppliers, items, duplicate: Item):
    by_id = {s.sid: s for s in suppliers}
    email.write_text(
        'From: AP Clerk <ap.clerk@example.invalid>\n'
        'To: AP Lead <ap.lead@example.invalid>\n'
        'Date: Mon, 16 Nov 2026 17:42\n'
        'Subject: RE: late invoices for 20 Nov run\n'
        'Attachment: late_invoices_20Nov.csv\n\n'
        'Hi,\n\n'
        'Attached are the invoices that came in after the aging export was run on Friday. They are approved in the\n'
        "workflow and should go in Thursday's run if they are due. Some of these may already be in the system,\n"
        'so please check before adding them.\n\n'
        'Thanks\n',
        encoding='utf-8',
    )
    with open(attachment, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Vendor No', 'Vendor', 'Inv #', 'Inv Date', 'Due', 'Amount', 'Ccy'])
        for i in [x for x in items if x.late] + [duplicate]:
            writer.writerow([i.sid, by_id[i.sid].name, i.number, f'{i.doc_date:%d-%b-%Y}', f'{i.due:%d-%b-%Y}',
                             f'{i.amount:,.2f}', by_id[i.sid].currency])  # fmt: skip


def _write_memo(path: Path):
    write_docx(
        path,
        [
            'MEMO - Supplier payment run, Friday 20 November 2026',
            'To: Accounts Payable team    From: Financial Controller    Date: 17 November 2026',
            'This memo replaces the draft proposal circulated on 10 November.',
            'Scope',
            '1. Pay every supplier invoice that is approved in the invoice approval workflow log and falls due on or '
            'before 20 November 2026. Invoices that are pending or rejected in the log are not paid.',
            '2. Invoices due after 20 November 2026 wait for the December run, even when they are approved.',
            '3. Do not pay suppliers flagged ON HOLD in the supplier master.',
            "4. Approved credit notes are netted against the same supplier's invoices in this run; pay the net amount.",
            '5. Include the late invoices sent by the AP clerk, but never pay the same invoice twice.',
            'Journal',
            '6. Raise one journal per supplier and currency, with the reference PAY-2026-11-<Supplier ID> '
            '(for example PAY-2026-11-S1234).',
            '7. Line 1 debits the AP control account 2100 with the net amount paid to the supplier. Line 2 credits '
            'the bank account for the payment currency: SGD 1010, USD 1020, EUR 1030.',
            "8. Use the supplier's pay currency from the supplier master. Amounts stay in that currency; no FX "
            'conversion.',
            '9. Journal date for every line: 20 November 2026.',
            'Please have the journal ready for my review by Wednesday 18 November.',
        ],
    )


def _write_coa(path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Accounts'
    header(ws, ['Account', 'Name', 'Type', 'Currency'])
    for row in [
        ('1010', 'Bank - SGD operating', 'Asset', 'SGD'), ('1020', 'Bank - USD operating', 'Asset', 'USD'),
        ('1030', 'Bank - EUR operating', 'Asset', 'EUR'), ('1040', 'Bank - SGD payroll', 'Asset', 'SGD'),
        ('1200', 'Trade receivables', 'Asset', ''), ('2100', 'Accounts payable - trade control', 'Liability', ''),
        ('2110', 'Accrued expenses', 'Liability', ''), ('2150', 'GST payable', 'Liability', 'SGD'),
        ('5100', 'Bank charges', 'Expense', ''), ('7900', 'FX gains and losses', 'Expense', ''),
    ]:  # fmt: skip
        ws.append(list(row))
    wb.save(path)


def _write_journal_like(path: Path, suppliers, rng, prefix: str, when: str, title: str | None):
    """Last month's posted journal (same layout) or a superseded draft proposal."""
    wb = Workbook()
    ws = wb.active
    if title is None:
        ws.title = 'Journal'
        header(ws, ['Journal Ref', 'Journal Date', 'Line', 'Account', 'Supplier ID', 'Description', 'Debit', 'Credit',
                    'Currency'])  # fmt: skip
        for s in rng.sample(suppliers, 17):
            amount = round(rng.uniform(800, 40000), 2)
            ref = f'{prefix}{s.sid}'
            ws.append([ref, when, 1, AP_CONTROL, s.sid, f'Supplier payment {when} {s.name}', amount, None, s.currency])
            ws.append([ref, when, 2, BANK[s.currency], s.sid, f'Supplier payment {when} {s.name}', None, amount,
                       s.currency])  # fmt: skip
    else:
        ws.title = 'Proposal'
        _title(ws, [title, 'Prepared 10 Nov 2026 from the 6 Nov aging. Superseded by the controller memo of 17 Nov.'])
        header(ws, ['Supplier ID', 'Supplier Name', 'Currency', 'Proposed Amount', 'Comment'])
        for s in rng.sample(suppliers, 19):
            ws.append([s.sid, s.name, s.currency, round(rng.uniform(800, 50000), 2), rng.choice(['', '', 'check'])])
    wb.save(path)


def _write_noise_pdfs(statement: Path, contract: Path, rng, freight: str):
    lines = ['Harbourline Bank - Account statement', 'Account: SGD operating 001-2345678-9 (example)',
             'Period: 01 Oct 2026 - 31 Oct 2026', '', 'Date        Description                         Debit      Credit']  # fmt: skip
    balance = 1_250_000.00
    for day in range(1, 31, 2):
        amount = round(rng.uniform(500, 60000), 2)
        balance -= amount
        lines.append(f'{day:02d}-Oct-2026  GIRO payment ref {rng.randint(100000, 999999)}         {amount:>10,.2f}')
    lines += ['', f'Closing balance: SGD {balance:,.2f}']
    write_pdf(statement, lines)
    write_pdf(
        contract,
        [
            'MASTER SERVICES AGREEMENT',
            f'Between {freight} ("Supplier") and the Customer (example group entity)',
            'Effective date: 1 March 2025. Term: 36 months.',
            '1. Services. The Supplier provides sea and air freight forwarding as ordered by the Customer.',
            '2. Fees. Fees follow the rate card in Schedule 2, reviewed annually.',
            '3. Payment. The Customer pays undisputed invoices within 30 days of receipt.',
            "4. Liability. Each party's liability is capped at the fees paid in the prior 12 months.",
            '5. Termination. Either party may terminate on 90 days written notice.',
            'Signed for and on behalf of both parties.',
        ],
    )


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, journal_format.json, job.json and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)
    suppliers = _suppliers(rng)
    items, traps = _items(rng, suppliers)
    duplicate = rng.choice([i for i in items if not i.late and i.kind == 'Invoice' and i.sid == traps['late_dup']
                            and i.status == 'Approved' and i.due <= CUTOFF])  # fmt: skip

    def path(relative: str) -> Path:
        target = packet / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    _write_memo(path(MEMO))
    _write_aging(path(AGING), suppliers, items, rng)
    _write_approvals(path(APPROVALS), items, rng)
    _write_master(path(MASTER), suppliers, rng)
    _write_late(path(EMAIL), path(LATE), suppliers, items, duplicate)
    _write_coa(path(COA))
    _write_journal_like(path(LAST_MONTH), suppliers, rng, 'PAY-2026-10-', '2026-10-23', None)
    _write_journal_like(path(DRAFT), suppliers, rng, '', '', 'DRAFT v1 - payment run proposal 20 Nov 2026')
    freight = next(s.name for s in suppliers if s.name.startswith('Halcyon'))
    _write_noise_pdfs(path(STATEMENT), path(CONTRACT), rng, freight)
    path(JUNK).write_text(
        '[.ShellClassInfo]\r\nIconResource=C:\\Windows\\System32\\shell32.dll,4\r\n', encoding='utf-8'
    )
    zip_packet(packet, out / 'packet.zip')

    rows, paid = [], []
    for supplier in suppliers:
        due = [i for i in items if i.sid == supplier.sid and _payable(i, supplier)]
        net = round(sum(i.amount if i.kind == 'Invoice' else -i.amount for i in due), 2)
        if net <= 0:
            continue
        paid.append(supplier.sid)
        ref = f'PAY-2026-11-{supplier.sid}'
        description = f'Supplier payment 20-Nov-2026 {supplier.name}'
        for line, account, debit, credit in ((1, AP_CONTROL, net, None), (2, BANK[supplier.currency], None, net)):
            rows.append({'Journal Ref': ref, 'Journal Date': RUN_DATE, 'Line': line, 'Account': account,
                         'Supplier ID': supplier.sid, 'Description': description, 'Debit': debit, 'Credit': credit,
                         'Currency': supplier.currency})  # fmt: skip

    fmt = {
        'sheets': [
            {
                'name': 'Journal',
                'header_row': 1,
                'columns': [
                    {'name': 'Journal Ref', 'type': 'text', 'required': True, 'pattern': '^PAY-2026-11-'},
                    {'name': 'Journal Date', 'type': 'date', 'required': True},
                    {'name': 'Line', 'type': 'integer', 'required': True},
                    {'name': 'Account', 'type': 'text', 'required': True, 'allowed': [AP_CONTROL, *BANK.values()]},
                    {'name': 'Supplier ID', 'type': 'text', 'required': True},
                    {'name': 'Description', 'type': 'text'},
                    {'name': 'Debit', 'type': 'number', 'min': 0},
                    {'name': 'Credit', 'type': 'number', 'min': 0},
                    {'name': 'Currency', 'type': 'text', 'required': True, 'allowed': list(BANK)},
                ],
                'unique': [['Journal Ref', 'Line']],
            }
        ],
        'rules': [
            {'type': 'balanced', 'sheet': 'Journal', 'debit': 'Debit', 'credit': 'Credit', 'group_by': 'Journal Ref'},
            {'type': 'one_side', 'sheet': 'Journal', 'columns': ['Debit', 'Credit']},
        ],
        'notes': 'General journal import layout. One row per journal line, numbered from 1 within each journal. '
        'Amounts in the transaction currency with 2 decimals; leave the unused side of a line blank.',
    }
    write_json(out / 'journal_format.json', fmt)

    files = packet_files(packet)
    decisions = {f: 'not_relevant' for f in files}
    for used in (AGING, APPROVALS, MASTER, LATE):
        decisions[used] = 'used'
    for reference in (MEMO, EMAIL, COA):
        decisions[reference] = 'reference'
    key = {
        'deliverables': {
            'payment_journal.xlsx': {
                'sheets': {
                    'Journal': {
                        'key': ['Journal Ref', 'Account'],
                        'fields': ['Journal Date', 'Supplier ID', 'Debit', 'Credit', 'Currency'],
                        'rows': rows,
                    }
                }
            }
        },
        'files': decisions,
        'questions': [],
        'suppliers_paid': paid,
        'skipped_on_hold': [s.sid for s in suppliers if s.on_hold],
        'traps': {**traps, 'duplicated_invoice': f'{duplicate.sid}/{duplicate.number}'},
    }
    write_json(out / 'answer_key.json', key)
    write_json(
        out / 'job.json',
        {
            'id': 'payment_journal',
            'module': 'ap',
            'title': 'Supplier payment run journal, 20 November 2026',
            'goal': 'Please prepare the payment journal for the 20 November 2026 supplier payment run. Follow the '
            'payment run memo in the packet for what to pay and how to post it, and build the journal in our '
            'import format.',
            'zip': 'packet.zip',
            'deliverables': [{'name': 'payment_journal.xlsx', 'format': 'journal_format.json'}],
        },
    )
    return key
