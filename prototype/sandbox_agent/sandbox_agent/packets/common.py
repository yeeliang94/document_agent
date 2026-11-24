"""Helpers every test packet uses: zipping, minimal Word and PDF writers, and writing job.json and the answer key.

A packet builder is a function build(out: Path, seed: int) that writes into out/:
- packet.zip            the inputs, under one top folder named "packet"
- job.json              the goal and deliverables (see jobspec.py)
- a template workbook and/or a format JSON for each deliverable
- answer_key.json       expected rows, expected file decisions and expected questions (see score.py)
- user_answers.json     optional scripted replies to ask_user (see jobspec.Answerer)
Everything is synthetic.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from openpyxl.styles import Font, PatternFill


def header(ws, columns: list):
    ws.append(columns)
    for cell in ws[ws.max_row]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill('solid', fgColor='DDEBF7')


def zip_packet(packet: Path, zip_path: Path, extra: dict[str, bytes] | None = None):
    """Zip packet/ so every entry starts with "packet/". extra adds entries (packet-relative path -> bytes)."""
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as archive:
        for file in sorted(packet.rglob('*')):
            if file.is_file():
                archive.write(file, f'packet/{file.relative_to(packet).as_posix()}')
        for name, data in (extra or {}).items():
            archive.writestr(f'packet/{name}', data)


def packet_files(packet: Path) -> list[str]:
    return sorted(f.relative_to(packet).as_posix() for f in packet.rglob('*') if f.is_file())


def write_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str), encoding='utf-8')


def write_docx(path: Path, paragraphs: list[str]):
    """A minimal Word document: one paragraph per item. Opens in Word and python-docx."""
    body = ''.join(f'<w:p><w:r><w:t xml:space="preserve">{escape(text)}</w:t></w:r></w:p>' for text in paragraphs)
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f'<w:body>{body}</w:body></w:document>'
    )
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as docx:
        docx.writestr(
            '[Content_Types].xml',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            '</Types>',
        )
        docx.writestr(
            '_rels/.rels',
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/></Relationships>',
        )
        docx.writestr('word/document.xml', document)


def write_pdf(path: Path, lines: list[str]):
    """A minimal one-page text PDF (Helvetica, 10 pt). Text is extractable with pypdf. Keep to about 60 lines."""

    def pdf_text(text: str) -> str:
        return (
            text.replace('\\', '\\\\')
            .replace('(', '\\(')
            .replace(')', '\\)')
            .encode('latin-1', 'replace')
            .decode('latin-1')
        )

    stream = 'BT /F1 10 Tf 50 800 Td 13 TL ' + ' '.join(f'({pdf_text(line)}) Tj T*' for line in lines) + ' ET'
    objects = [
        '<< /Type /Catalog /Pages 2 0 R >>',
        '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
        '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 5 0 R >> >> '
        '/Contents 4 0 R >>',
        f'<< /Length {len(stream.encode("latin-1"))} >>\nstream\n{stream}\nendstream',
        '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',
    ]
    out = b'%PDF-1.4\n'
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f'{number} 0 obj\n{body}\nendobj\n'.encode('latin-1')
    xref = len(out)
    out += f'xref\n0 {len(objects) + 1}\n0000000000 65535 f \n'.encode()
    out += b''.join(f'{o:010d} 00000 n \n'.encode() for o in offsets)
    out += f'trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n'.encode()
    path.write_bytes(out)
