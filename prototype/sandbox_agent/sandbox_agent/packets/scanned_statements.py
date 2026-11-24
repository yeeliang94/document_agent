"""Figures that exist only as pictures: scanned financial statements and photographed pages.

- Two statements are whole scanned PDFs (30 to 40 pages, no text layer). The figures sit on one page of each.
- The third arrives as three photographed pages (JPG); only one of them holds the figures.
- One statement is stated in RM thousand, the others in RM.

The scans are real documents, so neither they nor their figures are kept in this repository. The builder reads
both from the folder named by SCANNED_SAMPLES: "Doc 1.pdf" to "Doc 3.pdf", and figures.json with the expected rows
({"rows": [{column: value, ...}]}, one per company, keyed by Registration No.). The packet is built only when asked
for by name (--only scanned_statements).
"""

from __future__ import annotations

import io
import json
import os
import shutil
from pathlib import Path

from openpyxl import Workbook

from .common import header, write_json, zip_packet

TEMPLATE = 'statement_figures.xlsx'
SHEET = 'Figures'
COLUMNS = ['Registration No.', 'Company', 'Year End', 'Stated In', 'Revenue', 'Profit Before Tax',
           'Profit For The Year', 'Prior Year Revenue']  # fmt: skip
GOAL = (
    'From the audited financial statements in the packet, fill the template with one row per company, following '
    'its Read Me sheet.'
)
PHOTO_PAGES = (11, 12, 13)  # of Doc 3, counted from 1: the auditors' last page, comprehensive income, financial position
FIGURES = ('Statements/FS 2025 - scan A.pdf', 'Statements/FS 2025 - scan B.pdf', 'Photos/IMG_4172.jpg')


def samples() -> Path:
    if not os.environ.get('SCANNED_SAMPLES'):
        raise SystemExit('scanned_statements needs SCANNED_SAMPLES: the folder with the scans and figures.json.')
    folder = Path(os.environ['SCANNED_SAMPLES']).expanduser()
    missing = [n for n in ('Doc 1.pdf', 'Doc 2.pdf', 'Doc 3.pdf', 'figures.json') if not (folder / n).is_file()]
    if missing:
        raise SystemExit(f'scanned_statements needs {", ".join(missing)} in {folder}.')
    return folder


def build(out: Path, seed: int = 20261031) -> dict:
    from PIL import Image, ImageFilter
    from pypdf import PdfReader

    source = samples()
    out.mkdir(parents=True, exist_ok=True)
    packet = out / 'packet'
    (packet / 'Statements').mkdir(parents=True)
    (packet / 'Photos').mkdir()
    shutil.copy(source / 'Doc 1.pdf', packet / 'Statements' / 'FS 2025 - scan A.pdf')
    shutil.copy(source / 'Doc 2.pdf', packet / 'Statements' / 'FS 2025 - scan B.pdf')
    pages = PdfReader(source / 'Doc 3.pdf').pages
    for number, index in enumerate(PHOTO_PAGES, start=1):
        image = Image.open(io.BytesIO(pages[index - 1].images[0].data)).convert('L')
        # As a phone would leave them: smaller, a little soft, slightly crooked, and one lying on its side.
        image.thumbnail((1500, 2200))
        image = image.filter(ImageFilter.GaussianBlur(0.8)).rotate(3 if number != 2 else 93, expand=True, fillcolor=235)
        image.save(packet / 'Photos' / f'IMG_{4170 + number}.jpg', quality=55)
    zip_packet(packet, out / 'packet.zip')

    wb = Workbook()
    readme = wb.active
    readme.title = 'Read Me'
    for line in ['One row per company.', 'Registration No.: the 12-digit number, digits only.',
                 'Year End as YYYY-MM-DD.', 'Amounts exactly as printed for the current year, as plain numbers. '
                 'Do not convert thousands to units.', 'Stated In: write RM or RM thousand, as the statement says.',
                 'Prior Year Revenue: the comparative figure, as printed.']:  # fmt: skip
        readme.append([line])
    header(wb.create_sheet(SHEET), COLUMNS)
    wb.save(out / TEMPLATE)

    rows = json.loads((source / 'figures.json').read_text(encoding='utf-8'))['rows']
    spec = {'key': ['Registration No.'], 'fields': [c for c in COLUMNS[2:]], 'rows': rows}
    deliverable = f'{Path(TEMPLATE).stem}_output.xlsx'
    key = {'deliverables': {deliverable: {'sheets': {SHEET: spec}}},
           'files': {f: 'used' for f in FIGURES}, 'questions': []}  # fmt: skip
    write_json(out / 'answer_key.json', key)
    write_json(out / 'job.json', {'id': 'scanned_statements', 'module': 'payroll',
                                  'title': 'Figures from scanned statements', 'goal': GOAL, 'zip': 'packet.zip',
                                  'deliverables': [{'name': deliverable, 'template': TEMPLATE}]})  # fmt: skip
    return key
