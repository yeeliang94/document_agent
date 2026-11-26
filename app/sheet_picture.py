"""A picture of a sheet drawn as Excel shows it, for the agent to look at its own output.

Drawn on this machine from the saved file with Pillow, using what the file preview already uses: each cell's look
(app/cell_format.py) and formula results (app/formula_calc.py). It draws fonts (bold, italic, underline, size,
colour), fills, borders, column widths, row heights, merged cells, number formats, formula results, text that spills
into empty neighbours, #### where a number does not fit, gridlines and frozen panes. Hidden rows and columns are
left out. What it cannot draw (charts, pictures, conditional colours, formulas it cannot work out) is named in
`notes`, never guessed. Close to Excel, not identical: Excel's own fonts are not on every machine.

A sheet too big for one picture is drawn as tiles, each with its own row and column labels.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from datetime import date, datetime, time
from functools import lru_cache
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.utils import get_column_letter
from openpyxl.utils.cell import range_boundaries
from PIL import Image, ImageDraw, ImageFont

from . import cell_format, formula_calc

SCALE = 1.25  # one Excel pixel, in picture pixels: large enough to read, small enough to send
DIGIT = 7  # Excel pixels one digit of the default font takes; a column's width counts these
MAX_ROWS = 200  # rows drawn per call; the rest are named in the notes
MAX_COLUMNS = 40
TILE_WIDTH = 1500  # picture pixels
TILE_HEIGHT = 1200
MAX_TILES = 4
MAX_FILE = 40 * 1024**2  # a workbook larger than this is too slow to open whole for a picture
HEADER_FILL = '#f3f4f6'
GRID = '#dadce0'
FONTS = {  # regular, bold: the first pair found on this machine is used
    'mac': ('/System/Library/Fonts/Supplemental/Arial.ttf', '/System/Library/Fonts/Supplemental/Arial Bold.ttf'),
    'windows': ('C:/Windows/Fonts/arial.ttf', 'C:/Windows/Fonts/arialbd.ttf'),
    'linux': ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf', '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'),
    'liberation': ('/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
                   '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf'),
}  # fmt: skip


@dataclass
class Drawn:
    """One cell as it was drawn: what a test, or the notes, can name."""

    text: str
    span: str  # the cell, or the whole merged block it heads
    bold: bool = False
    italic: bool = False
    fill: str | None = None
    color: str = '#000000'
    align: str = 'left'
    borders: dict = field(default_factory=dict)  # side -> colour


@dataclass
class Picture:
    sheet: str
    images: list[bytes] = field(default_factory=list)  # PNG, one per tile
    tiles: list[str] = field(default_factory=list)  # the range each tile shows, e.g. "A1:H40"
    cells: dict[str, Drawn] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    sheets: list[str] = field(default_factory=list)  # every sheet in the workbook, hidden ones marked


class _Fonts:
    """Fonts sized so one digit is as wide as in Excel, whatever font this machine has."""

    def __init__(self):
        self.paths = next(
            (pair for pair in FONTS.values() if all(Path(p).exists() for p in pair)), (None, None)
        )

    @lru_cache(maxsize=64)  # noqa: B019 - one _Fonts lives for the whole process
    def get(self, size: float, bold: bool) -> tuple[ImageFont.FreeTypeFont, bool]:
        """The font for a cell's point size, and whether bold must be faked (no bold font on this machine)."""
        path = self.paths[1] if bold and self.paths[1] else self.paths[0]
        target = DIGIT * SCALE * size / 11
        px = max(6, round(target * 1.8))
        for _ in range(8):  # a few steps to the size whose digit has the wanted width
            font = ImageFont.truetype(path, px) if path else ImageFont.load_default(size=px)
            width = font.getlength('0') or target
            if abs(width - target) < 0.4:
                break
            px = max(6, round(px * target / width))
        font = ImageFont.truetype(path, px) if path else ImageFont.load_default(size=px)
        return font, bold and not self.paths[1]


_FONTS = _Fonts()


def too_narrow(text: str, size: float, bold: bool, pixels: int) -> bool:
    """True when a number's text does not fit a column this many Excel pixels wide, so Excel shows ####."""
    font, _ = _FONTS.get(float(size or 11), bold)
    return font.getlength(text) > round(pixels * SCALE) - 2 * round(2 * SCALE)


def width_px(ws, column: int) -> tuple[int, bool]:
    """A column's width in Excel pixels, and whether it is hidden."""
    letter = get_column_letter(column)
    dim = ws.column_dimensions.get(letter)
    if dim is None:  # openpyxl keeps a run of equal columns under its first letter
        dim = next((d for d in ws.column_dimensions.values() if d.min and d.max and d.min <= column <= d.max), None)
    width = dim.width if dim is not None and dim.width else None
    if width is None:
        width = ws.sheet_format.defaultColWidth or (ws.sheet_format.baseColWidth or 8) + 0.43
    return round(width * DIGIT + 5), bool(dim is not None and dim.hidden)


def _height_px(ws, row: int) -> tuple[int, bool]:
    dim = ws.row_dimensions.get(row)
    points = dim.ht if dim is not None and dim.ht else ws.sheet_format.defaultRowHeight or 15
    return round(points * 4 / 3), bool(dim is not None and dim.hidden)


def _general(value) -> str:
    """A value as Excel's General format shows it."""
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value.is_integer() and abs(value) < 1e11:
            return str(int(value))
        return f'{value:.10g}'
    if isinstance(value, datetime):
        return value.strftime('%d/%m/%Y') + ('' if value.time() == time(0) else value.strftime(' %H:%M'))
    if isinstance(value, date):
        return value.strftime('%d/%m/%Y')
    if isinstance(value, time):
        return value.strftime('%H:%M:%S')
    return str(value)


def _is_number(value) -> bool:
    return isinstance(value, (int, float, datetime, date, time)) and not isinstance(value, bool)


def _border(css: str) -> tuple[int, str]:
    width, _, colour = css.split(' ', 2)
    return int(width.removesuffix('px')), colour.split()[-1]


def _used(ws) -> tuple[int, int]:
    """The last row and column holding a value or a merged block; formatting alone stretches nothing."""
    rows = cols = 0
    for cell in ws._cells.values():  # noqa: SLF001 - openpyxl has no public list of stored cells
        if cell.value not in (None, ''):
            rows, cols = max(rows, cell.row), max(cols, cell.column)
    for block in ws.merged_cells.ranges:
        rows, cols = max(rows, block.max_row), max(cols, block.max_col)
    return max(rows, 1), max(cols, 1)


def _runs(numbers: list[int]) -> str:
    out, start = [], None
    for i, n in enumerate(numbers):
        if start is None:
            start = n
        if i == len(numbers) - 1 or numbers[i + 1] != n + 1:
            out.append(str(start) if start == n else f'{start}-{n}')
            start = None
    return ', '.join(out)


def draw(path: Path, sheet: str | None = None, cells: str | None = None, max_tiles: int = MAX_TILES) -> Picture:
    """Draw one sheet (the first visible one by default), or the block `cells` of it, e.g. "A1:H40"."""
    if path.stat().st_size > MAX_FILE:
        raise ValueError(f'{path.name} is over {MAX_FILE // 1024**2} MB, too large to draw.')
    formulas = load_workbook(path)
    values = load_workbook(path, data_only=True)
    names = [ws.title + ('' if ws.sheet_state == 'visible' else ' (hidden)') for ws in formulas.worksheets]
    if sheet and sheet not in formulas.sheetnames:
        raise ValueError(f'{path.name} has no sheet "{sheet}". Its sheets: {", ".join(names)}.')
    title = sheet or next((ws.title for ws in formulas.worksheets if ws.sheet_state == 'visible'), formulas.sheetnames[0])
    ws, wv = formulas[title], values[title]
    picture = Picture(sheet=title, sheets=names)
    if cells:
        try:
            c1, r1, c2, r2 = range_boundaries(cells.replace('$', '').upper())
        except (TypeError, ValueError):
            raise ValueError(f'"{cells}" is not a block of cells such as A1:H40.') from None
        c1, r1, c2, r2 = c1 or 1, r1 or 1, c2 or _used(ws)[1], r2 or _used(ws)[0]
    else:
        r2, c2 = _used(ws)
        r1 = c1 = 1
    if r2 - r1 + 1 > MAX_ROWS:
        picture.notes.append(f'Rows {r1 + MAX_ROWS}-{r2} are not drawn; ask for them with cells.')
        r2 = r1 + MAX_ROWS - 1
    if c2 - c1 + 1 > MAX_COLUMNS:
        picture.notes.append(
            f'Columns {get_column_letter(c1 + MAX_COLUMNS)}-{get_column_letter(c2)} are not drawn; ask for them with cells.'
        )
        c2 = c1 + MAX_COLUMNS - 1
    rows = [(r, *_height_px(ws, r)) for r in range(r1, r2 + 1)]
    cols = [(c, *width_px(ws, c)) for c in range(c1, c2 + 1)]
    hidden_rows = [r for r, _, hidden in rows if hidden]
    hidden_cols = [c for c, _, hidden in cols if hidden]
    if hidden_rows:
        picture.notes.append(f'Hidden rows left out: {_runs(hidden_rows)}.')
    if hidden_cols:
        picture.notes.append(f'Hidden columns left out: {", ".join(get_column_letter(c) for c in hidden_cols)}.')
    rows = [(r, h) for r, h, hidden in rows if not hidden]
    cols = [(c, w) for c, w, hidden in cols if not hidden]
    if not rows or not cols:
        picture.notes.append('Nothing to draw: every row or column asked for is hidden.')
        return picture

    sheet_cells = _Cells(ws, wv, formulas, values, title, picture)
    for top, bottom, left, right in _tiles(rows, cols, max_tiles, picture):
        tile_rows, tile_cols = rows[top:bottom], cols[left:right]
        picture.tiles.append(
            f'{get_column_letter(tile_cols[0][0])}{tile_rows[0][0]}:{get_column_letter(tile_cols[-1][0])}{tile_rows[-1][0]}'
        )
        picture.images.append(_paint(ws, sheet_cells, tile_rows, tile_cols))
    picture.notes += sheet_cells.notes()
    formulas.close()
    values.close()
    return picture


def _tiles(rows, cols, max_tiles: int, picture: Picture):
    """Split the rows and columns into blocks that each fit one picture."""

    def blocks(sizes, room):
        out, start, used = [], 0, 0
        for i, size in enumerate(sizes):
            if used + size > room and i > start:
                out.append((start, i))
                start, used = i, 0
            used += size
        out.append((start, len(sizes)))
        return out

    row_blocks = blocks([round(h * SCALE) for _, h in rows], TILE_HEIGHT - 30)
    col_blocks = blocks([round(w * SCALE) for _, w in cols], TILE_WIDTH - 50)
    every = [(t, b, left, right) for t, b in row_blocks for left, right in col_blocks]
    if len(every) > max_tiles:
        last = every[max_tiles - 1]
        picture.notes.append(
            f'Drawn as {max_tiles} of {len(every)} pictures, up to row {rows[last[1] - 1][0]}; '
            'ask for the rest with cells.'
        )
    return every[:max_tiles]


class _Cells:
    """What each cell shows and how it looks, worked out once for every tile."""

    def __init__(self, ws, wv, formulas, values, title, picture: Picture):
        self.ws, self.wv, self.title, self.picture = ws, wv, title, picture
        self.calc = formula_calc.Calculator(formulas, values)
        self.styles = cell_format.Styles(cell_format.theme_colours(formulas.loaded_theme))
        self.merged = {}  # top-left (row, col) -> (r2, c2); other cells of a block -> None
        for block in ws.merged_cells.ranges:
            for r in range(block.min_row, block.max_row + 1):
                for c in range(block.min_col, block.max_col + 1):
                    self.merged[(r, c)] = None
            self.merged[(block.min_row, block.min_col)] = (block.max_row, block.max_col)
        self.unworked = 0
        self.seen: set = set()

    def look(self, r: int, c: int) -> tuple[str, dict, bool]:
        """The text a cell shows, its style, and whether it is a number (for alignment and ####)."""
        cell, typed = self.wv.cell(r, c), self.ws.cell(r, c).value
        value, _ = formula_calc.shown_value(cell, typed, self.calc, self.title, r, c)
        if isinstance(value, str) and value.startswith('=') and isinstance(typed, str) and typed.startswith('='):
            if (r, c) not in self.seen:
                self.seen.add((r, c))
                self.unworked += 1
        look = dict(cell_format.style(self.ws.cell(r, c), self.styles.theme)) if self.ws.cell(r, c).has_style else {}
        shown = cell_format.shown_text(value, self.ws.cell(r, c).number_format)
        text = shown[0] if shown else _general(value)
        if shown and shown[1]:
            look['color'] = shown[1]
        number = _is_number(value) or (isinstance(value, str) and value in formula_calc.ERRORS)
        return text, look, number

    def notes(self) -> list[str]:
        out = []
        if self.unworked:
            out.append(f'{self.unworked} formula(s) could not be worked out here and are shown as their text.')
        charts, images = len(getattr(self.ws, '_charts', [])), len(getattr(self.ws, '_images', []))
        if charts:
            out.append(f'{charts} chart(s) on this sheet are not drawn.')
        if images:
            out.append(f'{images} picture(s) on this sheet are not drawn.')
        rules = sum(len(r.rules) for r in self.ws.conditional_formatting)
        if rules:
            out.append(f'{rules} conditional colour rule(s) are not drawn: cells show their plain look.')
        return out


def _paint(ws, cells: _Cells, rows, cols) -> bytes:
    head_w, head_h = round(40 * SCALE), round(20 * SCALE)
    xs, x = {}, head_w
    for c, w in cols:
        xs[c] = (x, x + round(w * SCALE))
        x = xs[c][1]
    ys, y = {}, head_h
    for r, h in rows:
        ys[r] = (y, y + round(h * SCALE))
        y = ys[r][1]
    image = Image.new('RGB', (x + 1, y + 1), 'white')
    pen = ImageDraw.Draw(image)
    label, _ = _FONTS.get(9, False)
    grid = ws.sheet_view.showGridLines is not False
    shown_rows, shown_cols = [r for r, _ in rows], [c for c, _ in cols]

    def box(r1, c1, r2, c2):
        """The pixel box of a block of cells, clipped to this tile; None when no part of it is in the tile."""
        rs = [r for r in shown_rows if r1 <= r <= r2]
        cs = [c for c in shown_cols if c1 <= c <= c2]
        if not rs or not cs:
            return None
        return xs[cs[0]][0], ys[rs[0]][0], xs[cs[-1]][1], ys[rs[-1]][1]

    # Fills and gridlines first, then text, then borders on top, as Excel paints them.
    blocks = []
    for r in shown_rows:
        for c in shown_cols:
            merged = cells.merged.get((r, c), False)
            if merged is None:
                continue  # inside a merged block, drawn by its first cell
            r2, c2 = merged if merged else (r, c)
            area = box(r, c, r2, c2)
            text, look, number = cells.look(r, c)
            blocks.append((r, c, r2, c2, area, text, look, number))
            if look.get('fill'):
                pen.rectangle(area, fill=look['fill'])
            elif grid:
                pen.rectangle(area, outline=GRID)
    # A merged block whose first cell is in an earlier tile still shows its fill here.
    for (r, c), end in cells.merged.items():
        if end and (r not in shown_rows or c not in shown_cols):
            area = box(r, c, *end)
            if area:
                text, look, number = cells.look(r, c)
                blocks.append((r, c, *end, area, text, look, number))
                if look.get('fill'):
                    pen.rectangle(area, fill=look['fill'])
    filled = {(b[0], b[1]) for b in blocks if b[5]}  # cells holding text stop spill-over from the left
    for r, c, r2, c2, area, text, look, number in blocks:
        text = _text(image, pen, ws, area, (r, c, r2, c2), text, look, number, (filled, cells.merged, xs, shown_cols))
        span = f'{get_column_letter(c)}{r}' + (f':{get_column_letter(c2)}{r2}' if (r2, c2) != (r, c) else '')
        cells.picture.cells.setdefault(
            f'{get_column_letter(c)}{r}',
            Drawn(text=text, span=span, bold=bool(look.get('bold')), italic=bool(look.get('italic')),
                  fill=look.get('fill'), color=look.get('color', '#000000'),
                  align=look.get('align') or ('right' if number else 'left'),
                  borders={side: _border(css)[1] for side, css in (look.get('border') or {}).items()}),
        )  # fmt: skip
    for block in blocks:
        area, look = block[4], block[6]
        for side, css in (look.get('border') or {}).items():
            width, colour = _border(css)
            width = max(1, round(width * SCALE))
            x1, y1, x2, y2 = area
            line = {'top': (x1, y1, x2, y1), 'bottom': (x1, y2, x2, y2), 'left': (x1, y1, x1, y2),
                    'right': (x2, y1, x2, y2)}[side]  # fmt: skip
            pen.line(line, fill=colour, width=width)
    # Row and column labels, and the frozen panes' edge.
    pen.rectangle((0, 0, x, head_h), fill=HEADER_FILL)
    pen.rectangle((0, 0, head_w, y), fill=HEADER_FILL)
    for c in shown_cols:
        x1, x2 = xs[c]
        pen.line((x2, 0, x2, head_h), fill=GRID)
        name = get_column_letter(c)
        pen.text(((x1 + x2) / 2, head_h / 2), name, fill='#4b5563', font=label, anchor='mm')
    for r in shown_rows:
        y1, y2 = ys[r]
        pen.line((0, y2, head_w, y2), fill=GRID)
        pen.text((head_w / 2, (y1 + y2) / 2), str(r), fill='#4b5563', font=label, anchor='mm')
    pen.line((head_w, 0, head_w, y), fill='#9ca3af')
    pen.line((0, head_h, x, head_h), fill='#9ca3af')
    if ws.freeze_panes:
        fc, fr, _, _ = range_boundaries(ws.freeze_panes + ':' + ws.freeze_panes)
        if fr > 1 and fr - 1 in ys:
            pen.line((head_w, ys[fr - 1][1], x, ys[fr - 1][1]), fill='#6b7280', width=2)
        if fc > 1 and fc - 1 in xs:
            pen.line((xs[fc - 1][1], head_h, xs[fc - 1][1], y), fill='#6b7280', width=2)
    out = io.BytesIO()
    image.save(out, 'PNG', optimize=True)
    return out.getvalue()


def _text(image, pen, ws, area, place, text, look, number, around) -> str:
    """Draw a cell's text; returns what was shown (#### for a number too wide for its column)."""
    if not text:
        return text
    r, c, r2, c2 = place
    filled, merged, xs, shown_cols = around
    cell = ws.cell(r, c)
    font_spec = cell.font
    size = float(font_spec.sz or 11) if font_spec is not None else 11.0
    font, fake_bold = _FONTS.get(size, bool(look.get('bold')))
    x1, y1, x2, y2 = area
    pad = round(2 * SCALE)
    room = x2 - x1 - 2 * pad
    align = look.get('align') or ('right' if number else 'left')
    alignment = cell.alignment
    if number and font.getlength(text) > room and (r2, c2) == (r, c):
        text = '#' * max(1, int(room // max(font.getlength('#'), 1)))  # Excel shows #### for a number that does not fit
    lines = [text]
    if alignment is not None and alignment.wrap_text:
        lines = _wrap(text, font, room)
    elif not number and align == 'left' and font.getlength(text) > room:
        # Text spills into the empty cells to its right, as in Excel.
        for nxt in shown_cols[shown_cols.index(c2) + 1 :] if c2 in shown_cols else []:
            if (r, nxt) in filled or merged.get((r, nxt), False) is not False:
                break
            x2 = xs[nxt][1]
            if font.getlength(text) <= x2 - x1 - 2 * pad:
                break
    line_height = font.size * 1.2
    vertical = getattr(alignment, 'vertical', None) or 'bottom'
    total = line_height * len(lines)
    top = {'top': y1 + pad, 'center': (y1 + y2 - total) / 2}.get(vertical, y2 - pad - total)
    colour = look.get('color', '#000000')
    for i, line in enumerate(lines):
        width = font.getlength(line)
        if align == 'right':
            lx = x2 - pad - width
        elif align == 'center':
            lx = (x1 + x2 - width) / 2
        else:
            lx = x1 + pad
        ly = top + i * line_height
        if ly + line_height > y2 + 1 and i:
            break
        crop = _clip(image, line, font, lx, ly, x1, x2, colour, fake_bold)
        if crop and look.get('underline'):
            pen.line((lx, ly + font.size * 1.05, lx + min(width, x2 - lx), ly + font.size * 1.05), fill=colour)
    return text


def _clip(image, text, font, lx, ly, x1, x2, colour, fake_bold) -> bool:
    """Draw text, cut at the cell's (or its spill-over's) edges."""
    width = max(1, round(font.getlength(text)) + 2)
    height = max(1, round(font.size * 1.3))
    patch = Image.new('L', (width, height), 0)
    stamp = ImageDraw.Draw(patch)
    stamp.text((0, 0), text, fill=255, font=font)
    if fake_bold:
        stamp.text((1, 0), text, fill=255, font=font)
    left, right = max(x1, round(lx)), min(x2, round(lx) + width)
    if right <= left:
        return False
    patch = patch.crop((left - round(lx), 0, right - round(lx), height))
    image.paste(Image.new('RGB', patch.size, colour), (left, round(ly)), patch)
    return True


def _wrap(text: str, font, room: float) -> list[str]:
    lines = []
    for paragraph in text.split('\n'):
        line = ''
        for word in paragraph.split(' '):
            trial = f'{line} {word}'.strip()
            if line and font.getlength(trial) > room:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def tokens(png: bytes) -> int:
    """Roughly what one picture costs the model to read, by OpenAI's rule for detailed images: scaled to fit 2048
    pixels, then its short side to 768, and 170 tokens per 512-pixel square plus 85."""
    with Image.open(io.BytesIO(png)) as image:
        w, h = image.size
    scale = min(1, 2048 / max(w, h))
    w, h = w * scale, h * scale
    scale = min(1, 768 / min(w, h))
    w, h = w * scale, h * scale
    return 85 + 170 * (-(-int(w) // 512)) * (-(-int(h) // 512))
