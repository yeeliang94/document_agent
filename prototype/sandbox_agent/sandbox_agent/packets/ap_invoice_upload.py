"""Generate a synthetic AP invoice-upload packet whose deliverable fills a template, and its answer key.

Everything is invented. The agent fills ap_upload_template.xlsx with every November 2026 supplier invoice for SG01 and
MY01. The traps:
- single-page invoice PDFs sit in per-supplier folders with inconsistent names and depths
- invoices print dates in different formats, and two print payment terms instead of a due date
- the bill-to company name, not an entity code, identifies the entity (the template's Instructions sheet maps them)
- tax is printed as words ("GST @ 9%", "Service Tax 8%") and must be mapped to the template's tax codes
- a supplier-portal CSV export repeats some PDF invoices (dedupe by supplier and invoice number) and adds a few that
  exist only there
- an email cancels one portal invoice, which must not be uploaded
- a credit note PDF must be entered with Document Type "Credit Note", positive amounts and no due date
- an invoice billed to TH01 (out of scope) sits among the PDFs, and the portal holds another TH01 invoice
- a purchase-order list is reference only; a marketing brochure, last month's upload file and a .DS_Store file are not
  relevant
"""

from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.worksheet.datavalidation import DataValidation

from .common import header, packet_files, write_json, write_pdf, zip_packet

ENTITIES = {
    'SG01': ('Lumen Harbor Pte Ltd', '20 Collyer Quay #12-01, Singapore 049319'),
    'MY01': ('Lumen Harbor Sdn Bhd', 'Level 18, Menara Contoh, Jalan Ampang, 50450 Kuala Lumpur'),
    'TH01': ('Lumen Harbor (Thailand) Co., Ltd.', '99 Sukhumvit Road, Khlong Toei, Bangkok 10110'),
}
# tax code -> (printed wording, rate)
TAX = {
    'SG-GST9': ('GST @ 9%', 0.09),
    'SG-GST0': ('GST @ 0% (zero-rated)', 0.0),
    'MY-ST10': ('Sales Tax 10%', 0.10),
    'MY-SV8': ('Service Tax 8%', 0.08),
    'NT': ('No tax - overseas supplier', 0.0),
}
CURRENCIES = ['SGD', 'MYR', 'USD']
DOC_TYPES = ['Invoice', 'Credit Note']
COLUMNS = ['Entity', 'Supplier ID', 'Supplier Name', 'Invoice Number', 'Invoice Date', 'Due Date', 'Currency',
           'Net Amount', 'Tax Amount', 'Gross Amount', 'Tax Code', 'Document Type']  # fmt: skip


@dataclass(frozen=True)
class Vendor:
    sid: str
    name: str
    folder: str  # where its PDFs live
    prefix: str
    currency: str
    tax: str
    address: str


VENDORS = [
    Vendor('V20114', 'Brightfield Office Supplies Pte Ltd', 'AP/Invoices received/Nov 2026/SG/Brightfield', 'BOS-',
           'SGD', 'SG-GST9', '3 Ubi Avenue 7, Singapore 408867'),
    Vendor('V20131', 'Kestrel Logistics Pte Ltd', 'AP/Invoices received/Nov 2026/SG/kestrel logistics/scans',
           'KL', 'SGD', 'SG-GST9', '21 Pioneer Crescent, Singapore 628559'),
    Vendor('V20158', 'Pinegate Facilities Services Pte Ltd', 'AP/Invoices received/Nov 2026/SG/PINEGATE', 'PF/26/',
           'SGD', 'SG-GST9', '8 Kallang Way 2, Singapore 349621'),
    Vendor('V20177', 'Tidewater Export Services Pte Ltd', 'AP/Invoices received/Nov 2026/SG/Tidewater - export',
           'TW', 'SGD', 'SG-GST0', '1 Harbour Drive, Singapore 117352'),
    Vendor('V30210', 'Merbau Print & Packaging Sdn Bhd', 'AP/Invoices received/Nov 2026/Malaysia/Merbau Print', 'MPP',
           'MYR', 'MY-ST10', 'Lot 12, Jalan Industri 3, 47100 Puchong, Selangor'),
    Vendor('V30244', 'Selatan Courier Services Sdn Bhd', 'AP/Invoices received/Nov 2026/Malaysia/selatan_courier',
           'SCS-', 'MYR', 'MY-SV8', '45 Jalan Pelabuhan, 42000 Port Klang, Selangor'),
    Vendor('V30262', 'Gunung Cleaning Sdn Bhd', 'AP/Invoices received/Nov 2026/Malaysia/Gunung Cleaning/Nov',
           'GC', 'MYR', 'MY-SV8', '7 Jalan Gombak, 53000 Kuala Lumpur'),
    Vendor('V40015', 'Arcturus Cloud Software LLC', 'AP/Invoices received/Nov 2026/Overseas/Arcturus Cloud', 'ACS-',
           'USD', 'NT', '500 Example Parkway, Suite 300, Austin, TX 78701, USA'),
    Vendor('V30288', 'Rimba Catering Sdn Bhd', '', 'RC', 'MYR', 'MY-SV8', '3 Jalan Bukit Bintang, 55100 Kuala Lumpur'),
    Vendor('V20190', 'Orchid Lane Stationers Pte Ltd', '', 'OLS', 'SGD', 'SG-GST9', '15 Kaki Bukit Road, Singapore'),
]  # fmt: skip


@dataclass
class Doc:
    vendor: Vendor
    entity: str
    number: str
    issued: date
    terms: int
    items: list[tuple[str, float]] = field(default_factory=list)
    kind: str = 'Invoice'
    in_pdf: bool = True
    in_portal: bool = False
    show_due: bool = True
    cancelled: bool = False
    po: str = ''
    refers_to: str = ''

    @property
    def net(self) -> float:
        return round(sum(a for _, a in self.items), 2)

    @property
    def tax(self) -> float:
        return round(self.net * TAX[self.vendor.tax][1], 2)

    @property
    def gross(self) -> float:
        return round(self.net + self.tax, 2)

    @property
    def due(self) -> date | None:
        return None if self.kind == 'Credit Note' else self.issued + timedelta(days=self.terms)


SERVICES = {
    'V20114': ['A4 copy paper (box of 5 reams)', 'Toner cartridges', 'Desk organisers', 'Whiteboard markers'],
    'V20131': ['Local delivery - pallets', 'Warehouse handling', 'Fuel surcharge', 'Container haulage'],
    'V20158': ['Monthly facilities maintenance', 'Aircon servicing', 'Pest control', 'Lighting repairs'],
    'V20177': ['Export documentation', 'Freight forwarding to Jakarta', 'Customs clearance (export)'],
    'V30210': ['Printed cartons', 'Product labels', 'Shrink wrap rolls'],
    'V30244': ['Courier services - October', 'Same-day deliveries', 'Return shipments'],
    'V30262': ['Office cleaning - November', 'Carpet shampoo', 'Window cleaning'],
    'V40015': ['Cloud platform subscription', 'Support plan - premium', 'Additional storage (TB)'],
    'V30288': ['Staff lunch catering', 'Meeting refreshments'],
    'V20190': ['Stationery order', 'Printed name cards'],
}


def _doc(rng: random.Random, vendor: Vendor, entity: str, used: set, kind: str = 'Invoice', **extra) -> Doc:
    while True:
        number = f'{"CN-" if kind == "Credit Note" else vendor.prefix}{rng.randint(1000, 98999)}'
        if (vendor.sid, number) not in used:
            used.add((vendor.sid, number))
            break
    count = 1 if kind == 'Credit Note' else rng.randint(1, min(3, len(SERVICES[vendor.sid])))
    items = [(text, round(rng.uniform(120, 9800), 2)) for text in rng.sample(SERVICES[vendor.sid], count)]
    doc = Doc(vendor, entity, number, date(2026, 11, rng.randint(2, 11)), rng.choice([14, 30, 30, 45, 60]), items,
              kind, po=f'PO-{entity[:2]}-{rng.randint(4100, 4999)}')  # fmt: skip
    for name, value in extra.items():
        setattr(doc, name, value)
    return doc


def _documents(rng: random.Random) -> list[Doc]:
    v = {x.sid: x for x in VENDORS}
    used: set = set()
    docs = [
        _doc(rng, v['V20114'], 'SG01', used),
        _doc(rng, v['V20114'], 'SG01', used, in_portal=True),
        _doc(rng, v['V20131'], 'SG01', used, show_due=False),
        _doc(rng, v['V20158'], 'SG01', used, in_portal=True),
        _doc(rng, v['V20177'], 'SG01', used),
        _doc(rng, v['V30210'], 'MY01', used),
        _doc(rng, v['V30244'], 'MY01', used, in_portal=True),
        _doc(rng, v['V30262'], 'MY01', used, show_due=False),
        _doc(rng, v['V40015'], 'SG01', used, in_portal=True),
        _doc(rng, v['V40015'], 'MY01', used),
        _doc(rng, v['V40015'], 'TH01', used),  # out of scope
        # portal only
        _doc(rng, v['V30288'], 'MY01', used, in_pdf=False, in_portal=True),
        _doc(rng, v['V20190'], 'SG01', used, in_pdf=False, in_portal=True),
        _doc(rng, v['V30244'], 'MY01', used, in_pdf=False, in_portal=True),
        _doc(rng, v['V30244'], 'MY01', used, in_pdf=False, in_portal=True, cancelled=True),
        _doc(rng, v['V40015'], 'TH01', used, in_pdf=False, in_portal=True),
    ]
    original = docs[0]
    docs.append(_doc(rng, v['V20114'], 'SG01', used, kind='Credit Note', refers_to=original.number,
                     issued=min(original.issued + timedelta(days=3), date(2026, 11, 12)),
                     items=[('Return of damaged goods', round(original.net * 0.25, 2))]))  # fmt: skip
    return docs


def _date_text(rng: random.Random, value: date) -> str:
    return rng.choice([f'{value.day} {value:%B %Y}', f'{value:%d-%b-%Y}', value.isoformat(), f'{value:%d %b %Y}'])


def _pdf_lines(rng: random.Random, doc: Doc) -> list[str]:
    vendor = doc.vendor
    bill_name, bill_address = ENTITIES[doc.entity]
    wording, _ = TAX[vendor.tax]
    title = 'CREDIT NOTE' if doc.kind == 'Credit Note' else ('TAX INVOICE' if vendor.tax.startswith('SG') else 'INVOICE')
    lines = [vendor.name, vendor.address, '', title, '',
             f'{"Credit Note" if doc.kind == "Credit Note" else "Invoice"} No: {doc.number}',
             f'Date: {_date_text(rng, doc.issued)}']  # fmt: skip
    if doc.kind == 'Credit Note':
        lines.append(f'Reference invoice: {doc.refers_to}')
    elif doc.show_due:
        lines.append(f'Due Date: {_date_text(rng, doc.due)}')
    else:
        lines.append(f'Payment terms: {doc.terms} days from invoice date')
    lines += ['', 'Bill To:', bill_name, bill_address, f'Your vendor no.: {vendor.sid}    Your PO: {doc.po}', '',
              f'Description{" " * 40}Amount ({vendor.currency})']  # fmt: skip
    lines += [f'{text:<50}{amount:>12,.2f}' for text, amount in doc.items]
    lines += ['', f'{"Subtotal":<50}{doc.net:>12,.2f}', f'{wording:<50}{doc.tax:>12,.2f}',
              f'{"Total " + vendor.currency:<50}{doc.gross:>12,.2f}', '']  # fmt: skip
    if doc.kind == 'Credit Note':
        lines.append('This credit note reduces the amount owed. Please offset it against your next payment.')
    else:
        lines.append(f'Please pay by bank transfer quoting invoice {doc.number}.')
    return lines


def _pdf_path(doc: Doc, n: int) -> str:
    safe = doc.number.replace('/', '_')
    styles = [f'{safe}.pdf', f'Invoice_{safe}.pdf', f'scan_{n:03d}_{safe}.pdf', f'{doc.vendor.prefix.strip("-/").replace("/", "_")} inv {safe}.pdf']
    name = f'Credit note {safe}.pdf' if doc.kind == 'Credit Note' else styles[n % len(styles)]
    return f'{doc.vendor.folder}/{name}'


def _write_portal(path: Path, docs: list[Doc]):
    with open(path, 'w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(['Portal Ref', 'Vendor ID', 'Vendor Name', 'Buyer Company', 'Invoice No', 'Invoice Date',
                         'Due Date', 'Currency', 'Net', 'Tax', 'Total', 'Tax Type', 'Status', 'Submitted On'])  # fmt: skip
        for n, doc in enumerate(sorted((d for d in docs if d.in_portal), key=lambda d: d.issued), start=88101):
            writer.writerow([f'SP-{n}', doc.vendor.sid, doc.vendor.name, ENTITIES[doc.entity][0], doc.number,
                             doc.issued.isoformat(), doc.due.isoformat(), doc.vendor.currency, f'{doc.net:.2f}',
                             f'{doc.tax:.2f}', f'{doc.gross:.2f}', TAX[doc.vendor.tax][0], 'Submitted',
                             (doc.issued + timedelta(days=1)).isoformat()])  # fmt: skip


def _write_po_list(path: Path, docs: list[Doc], rng: random.Random):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Open POs'
    ws.append(['Open purchase orders - extracted 12 Nov 2026'])
    ws['A1'].font = Font(bold=True, size=13)
    ws.append([])
    header(ws, ['PO Number', 'Entity', 'Supplier ID', 'Supplier Name', 'PO Date', 'Currency', 'PO Value', 'Received %',
                'Status'])  # fmt: skip
    for doc in docs:
        if doc.kind == 'Invoice':
            ws.append([doc.po, doc.entity, doc.vendor.sid, doc.vendor.name, doc.issued - timedelta(days=rng.randint(10, 40)),
                       doc.vendor.currency, round(doc.net * rng.choice([1, 1, 1.2, 1.5]), 2), rng.choice([100, 100, 60]),
                       'Open'])  # fmt: skip
    wb.save(path)


def _write_last_month(path: Path, rng: random.Random):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Invoices'
    header(ws, COLUMNS)
    for vendor in rng.sample(VENDORS[:8], 7):
        issued = date(2026, 10, rng.randint(1, 28))
        net = round(rng.uniform(300, 15000), 2)
        tax = round(net * TAX[vendor.tax][1], 2)
        entity = 'MY01' if vendor.currency == 'MYR' else 'SG01'
        ws.append([entity, vendor.sid, vendor.name, f'{vendor.prefix}{rng.randint(1000, 98999)}', issued.isoformat(),
                   (issued + timedelta(days=30)).isoformat(), vendor.currency, net, tax, round(net + tax, 2),
                   vendor.tax, 'Invoice'])  # fmt: skip
    note = wb.create_sheet('Upload log')
    note.append(['Uploaded 4 Nov 2026 by AP team. Batch AP-2026-10. Status: posted.'])
    wb.save(path)


INSTRUCTIONS = [
    'AP invoice upload - how to fill this file',
    'One row per supplier invoice or credit note on the Invoices sheet. Do not change the headers or the other sheets.',
    'Entity codes: SG01 = Lumen Harbor Pte Ltd (Singapore); MY01 = Lumen Harbor Sdn Bhd (Malaysia). Only these two '
    'entities are uploaded through this file.',
    'Supplier ID is our vendor number (it is printed on supplier invoices as "Your vendor no.").',
    'Dates as YYYY-MM-DD. If an invoice gives payment terms instead of a due date, Due Date = Invoice Date + terms.',
    'Net Amount is before tax, Tax Amount is the tax charged, Gross Amount = Net + Tax, all in the invoice currency.',
    'Tax codes: SG-GST9 = Singapore GST 9%; SG-GST0 = Singapore zero-rated GST; MY-ST10 = Malaysia sales tax 10%; '
    'MY-SV8 = Malaysia service tax 8%; NT = no tax (overseas supplier).',
    'Credit notes: Document Type "Credit Note", amounts entered as positive numbers, Due Date left blank.',
    'Never upload the same supplier invoice twice, and leave out invoices the supplier has cancelled.',
]


def build_template(path: Path):
    wb = Workbook()
    ws = wb.active
    ws.title = 'Invoices'
    header(ws, COLUMNS)
    summary = wb.create_sheet('Summary')
    header(summary, ['Entity', 'Documents', 'Gross Amount (all currencies)'])
    for row, entity in enumerate(['SG01', 'MY01'], start=2):
        summary.append([entity, f'=COUNTIF(Invoices!A:A,A{row})', f'=SUMIF(Invoices!A:A,A{row},Invoices!J:J)'])
    summary.append(['Total', '=SUM(B2:B3)', '=SUM(C2:C3)'])
    notes = wb.create_sheet('Instructions')
    for line in INSTRUCTIONS:
        notes.append([line])
    notes['A1'].font = Font(bold=True, size=13)
    lists = wb.create_sheet('Lists')
    lists.append(['Entity', 'Currency', 'Tax Code', 'Document Type'])
    columns = [['SG01', 'MY01'], CURRENCIES, list(TAX), DOC_TYPES]
    for i in range(max(len(c) for c in columns)):
        lists.append([c[i] if i < len(c) else None for c in columns])
    lists.sheet_state = 'hidden'

    def dropdown(cells: str, source: str):
        rule = DataValidation(type='list', formula1=source, allow_blank=False)
        rule.add(cells)
        ws.add_data_validation(rule)

    dropdown('A2:A2000', 'Lists!$A$2:$A$3')
    dropdown('G2:G2000', 'Lists!$B$2:$B$4')
    dropdown('K2:K2000', 'Lists!$C$2:$C$6')
    dropdown('L2:L2000', 'Lists!$D$2:$D$3')
    amounts = DataValidation(type='decimal', operator='greaterThanOrEqual', formula1='0', allow_blank=False)
    amounts.add('H2:J2000')
    ws.add_data_validation(amounts)
    wb.save(path)


EMAIL = 'AP/inbox/2026-11-10 Selatan Courier - invoice cancellation.txt'
PORTAL = 'AP/Supplier Portal/exports/portal_invoice_export_20261112.csv'
PO_LIST = 'Procurement/PO reports/2026/Open POs Nov-2026.xlsx'
BROCHURE = 'Marketing/vendor brochures/Arcturus_Cloud_Platform_brochure_2027.pdf'
LAST_MONTH = 'AP/Uploads/2026/10-Oct/AP_upload_Oct2026_POSTED.xlsx'
JUNK = 'AP/Invoices received/Nov 2026/.DS_Store'


def build(out: Path, seed: int = 20261031) -> dict:
    """Write packet/, packet.zip, ap_upload_template.xlsx, job.json and answer_key.json under out/."""
    rng = random.Random(seed)
    packet = out / 'packet'
    if packet.exists():
        raise SystemExit(f'{packet} already exists; choose an empty folder')
    packet.mkdir(parents=True)

    def path(relative: str) -> Path:
        target = packet / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    docs = _documents(rng)
    pdfs = {}
    for n, doc in enumerate(d for d in docs if d.in_pdf):
        relative = _pdf_path(doc, n)
        write_pdf(path(relative), _pdf_lines(rng, doc))
        pdfs[relative] = doc
    _write_portal(path(PORTAL), docs)
    cancelled = next(d for d in docs if d.cancelled)
    path(EMAIL).write_text(
        f'From: Accounts Receivable <ar@selatan-courier.example.invalid>\n'
        f'To: Accounts Payable <ap@lumen-harbor.example.invalid>\n'
        f'Date: Tue, 10 Nov 2026 09:14\n'
        f'Subject: Cancellation of invoice {cancelled.number}\n\n'
        f'Dear AP team,\n\n'
        f'Our invoice {cancelled.number} dated {cancelled.issued:%d %b %Y} for MYR {cancelled.gross:,.2f} was raised in '
        f'error and is cancelled. Please do not process or pay it. It still shows as submitted on the supplier\n'
        f'portal until our system syncs at month end.\n\n'
        f'Our other November invoices are unaffected.\n\n'
        f'Regards,\nSelatan Courier Services Sdn Bhd - Accounts Receivable\n',
        encoding='utf-8',
    )
    _write_po_list(path(PO_LIST), docs, rng)
    write_pdf(
        path(BROCHURE),
        ['Arcturus Cloud Platform - 2027 product brochure', '', 'Scale without limits.',
         'New in 2027: managed analytics, regional storage in Singapore and Kuala Lumpur, and 24/7 premium support.',
         'Plans: Starter, Business, Enterprise. Ask your account manager about multi-year discounts.',
         'This brochure is for information only and is not an invoice or an offer.'],  # fmt: skip
    )
    _write_last_month(path(LAST_MONTH), rng)
    path(JUNK).write_bytes(b'\x00\x00\x00\x01Bud1' + b'\x00' * 120)
    build_template(out / 'ap_upload_template.xlsx')
    zip_packet(packet, out / 'packet.zip')

    rows = []
    for doc in docs:
        if doc.entity not in ('SG01', 'MY01') or doc.cancelled:
            continue
        rows.append(dict(zip(COLUMNS, [
            doc.entity, doc.vendor.sid, doc.vendor.name, doc.number, doc.issued.isoformat(),
            doc.due.isoformat() if doc.due else None, doc.vendor.currency, doc.net, doc.tax, doc.gross, doc.vendor.tax,
            doc.kind,
        ], strict=True)))  # fmt: skip

    files = packet_files(packet)
    decisions = {f: 'not_relevant' for f in files}
    for relative, doc in pdfs.items():
        decisions[relative] = 'used' if doc.entity in ('SG01', 'MY01') else 'not_relevant'
    decisions[PORTAL] = 'used'
    decisions[EMAIL] = 'reference'
    decisions[PO_LIST] = 'reference'
    key = {
        'deliverables': {
            'ap_upload.xlsx': {
                'sheets': {
                    'Invoices': {
                        'key': ['Supplier ID', 'Invoice Number'],
                        'fields': [c for c in COLUMNS if c not in ('Supplier ID', 'Invoice Number')],
                        'rows': rows,
                    }
                }
            }
        },
        'files': decisions,
        'questions': [],
        'excluded': {
            'cancelled': f'{cancelled.vendor.sid}/{cancelled.number}',
            'other_entity': [f'{d.vendor.sid}/{d.number}' for d in docs if d.entity == 'TH01'],
            'portal_duplicates_of_pdf': [f'{d.vendor.sid}/{d.number}' for d in docs if d.in_pdf and d.in_portal],
        },
    }
    write_json(out / 'answer_key.json', key)
    write_json(
        out / 'job.json',
        {
            'id': 'ap_invoice_upload',
            'module': 'ap',
            'title': 'AP invoice upload, November 2026',
            'goal': 'Please prepare the November 2026 AP invoice upload for entities SG01 and MY01 from the invoices '
            'in the packet. Fill in our upload template, one row per invoice or credit note.',
            'zip': 'packet.zip',
            'deliverables': [{'name': 'ap_upload.xlsx', 'template': 'ap_upload_template.xlsx'}],
        },
    )
    return key
