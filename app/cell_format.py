"""How a spreadsheet cell looks in Excel: its shown text (from its number format) and its font, fill, alignment and borders.

The preview draws cells close to how Excel shows them. Anything this module cannot read with confidence (an unusual
number format, an odd colour) is left out, so the cell falls back to the plain value rather than a wrong one.
"""

import colorsys
import re
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal, localcontext
from functools import lru_cache
from xml.etree import ElementTree

from openpyxl.styles.colors import COLOR_INDEX

# Excel's names for the theme colours, in the order a cell's theme number counts them.
THEME_ORDER = ('lt1', 'dk1', 'lt2', 'dk2', 'accent1', 'accent2', 'accent3', 'accent4', 'accent5', 'accent6', 'hlink')
NAMED = {
    'black': '#000000',
    'blue': '#0000ff',
    'cyan': '#00ffff',
    'green': '#008000',
    'magenta': '#ff00ff',
    'red': '#b42318',  # the app's softer red, as approved in the prototype
    'white': '#ffffff',
    'yellow': '#ffff00',
}
BORDER_WIDTH = {'hair': 1, 'thin': 1, 'dotted': 1, 'dashed': 1, 'dashDot': 1, 'dashDotDot': 1}
BORDER_WIDTH |= {'medium': 2, 'mediumDashed': 2, 'mediumDashDot': 2, 'mediumDashDotDot': 2, 'slantDashDot': 2}
BORDER_WIDTH |= {'thick': 3, 'double': 3}
ALIGN = {'left': 'left', 'right': 'right', 'center': 'center', 'centerContinuous': 'center', 'justify': 'left'}
MONTHS = ('January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October',
          'November', 'December')  # fmt: skip
DAYS = ('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')


def theme_colours(theme_xml: bytes | None) -> list[str]:
    """The workbook's theme colours as #rrggbb, in theme-number order. Empty when the file has no theme."""
    if not theme_xml:
        return []
    try:
        root = ElementTree.fromstring(theme_xml)
    except ElementTree.ParseError:
        return []
    scheme = next((e for e in root.iter() if e.tag.endswith('}clrScheme')), None)
    if scheme is None:
        return []
    found = {}
    for slot in scheme:
        name = slot.tag.rsplit('}', 1)[-1]
        colour = next(iter(slot), None)
        if colour is None:
            continue
        value = colour.get('lastClr') if colour.tag.endswith('}sysClr') else colour.get('val')
        if value and re.fullmatch(r'[0-9A-Fa-f]{6}', value):
            found[name] = '#' + value.lower()
    return [found.get(name, '') for name in THEME_ORDER]


def _tint(hex_colour: str, tint: float) -> str:
    """Lighten (positive) or darken (negative) a colour the way Excel applies a theme tint."""
    r, g, b = (int(hex_colour[i : i + 2], 16) / 255 for i in (1, 3, 5))
    h, light, s = colorsys.rgb_to_hls(r, g, b)
    light = light * (1 + tint) if tint < 0 else light * (1 - tint) + tint
    return '#' + ''.join(f'{round(v * 255):02x}' for v in colorsys.hls_to_rgb(h, min(max(light, 0), 1), s))


def colour(value, theme: list[str]) -> str | None:
    """An openpyxl colour as #rrggbb, or None when it is unset, automatic or cannot be worked out."""
    if value is None:
        return None
    try:
        kind = value.type
        if kind == 'rgb' and isinstance(value.rgb, str) and re.fullmatch(r'[0-9A-Fa-f]{8}', value.rgb):
            base = '#' + value.rgb[2:].lower()
        elif kind == 'theme' and 0 <= value.theme < len(theme) and theme[value.theme]:
            base = theme[value.theme]
        elif kind == 'indexed' and 0 <= value.indexed < 64:
            base = '#' + COLOR_INDEX[value.indexed][2:].lower()
        else:
            return None
        return _tint(base, value.tint) if value.tint else base
    except (AttributeError, TypeError, ValueError):
        return None


def _light(hex_colour: str | None) -> bool:
    if not hex_colour:
        return False
    r, g, b = (int(hex_colour[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b > 200


def style(cell, theme: list[str]) -> dict:
    """The look of one cell, keeping only what differs from a plain cell."""
    out = {}
    font = cell.font
    if font is not None:
        if font.b:
            out['bold'] = True
        if font.i:
            out['italic'] = True
        if font.u and font.u != 'none':
            out['underline'] = True
        if font.strike:
            out['strike'] = True
        text = colour(font.color, theme)
        if text and text != '#000000':
            out['color'] = text
    fill = cell.fill
    if fill is not None and getattr(fill, 'patternType', None) == 'solid':
        background = colour(fill.fgColor, theme)
        if background and background != '#ffffff':
            out['fill'] = background
    if 'fill' not in out and _light(out.get('color')):
        out.pop('color')  # light text whose fill the preview cannot draw would vanish on the white sheet
    align = ALIGN.get(getattr(cell.alignment, 'horizontal', None) or '')
    if align:
        out['align'] = align
    border = cell.border
    if border is not None:
        for side in ('top', 'right', 'bottom', 'left'):
            edge = getattr(border, side, None)
            width = BORDER_WIDTH.get(getattr(edge, 'style', None) or '')
            if width:
                line = 'double' if edge.style == 'double' else 'solid'
                out.setdefault('border', {})[side] = f'{width}px {line} {colour(edge.color, theme) or "#18181b"}'
    return out


class Styles:
    """The distinct cell styles of one sheet, each worked out once and numbered in the order first seen."""

    def __init__(self, theme: list[str]):
        self.theme = theme
        self.found: list[dict] = []
        self._numbers: dict[tuple, int | None] = {}

    def number(self, cell) -> int | None:
        """The number of this cell's style in `found`, or None when it looks like a plain cell."""
        key = tuple(cell.style_array)  # font, fill, border, alignment and format ids: the whole style
        if key not in self._numbers:
            look = style(cell, self.theme)
            self._numbers[key] = len(self.found) if look else None
            if look:
                self.found.append(look)
        return self._numbers[key]


# --- Number formats ---------------------------------------------------------------------------------------------


@lru_cache(maxsize=256)
def _sections(code: str) -> tuple[str, ...]:
    """Split a format code on the semicolons that separate its positive, negative, zero and text parts."""
    parts, current, quoted, bracket, escaped = [], '', False, False, False
    for ch in code:
        if escaped:
            current, escaped = current + ch, False
            continue
        if ch == '\\' and not quoted:
            escaped = True
        elif ch == '"':
            quoted = not quoted
        elif ch == '[' and not quoted:
            bracket = True
        elif ch == ']' and not quoted:
            bracket = False
        elif ch == ';' and not quoted and not bracket:
            parts.append(current)
            current = ''
            continue
        current += ch
    return (*parts, current)


@lru_cache(maxsize=512)
def _brackets(section: str) -> tuple[str, str | None] | None:
    """Take the [Red] and [$€-409] parts out of a section. None when it has a condition or elapsed time we skip."""
    shown_colour = None

    def swap(match):
        nonlocal shown_colour
        inside = match.group(1)
        if inside.lower() in NAMED:
            shown_colour = NAMED[inside.lower()]
            return ''
        if inside.startswith('$'):
            return '"' + inside[1:].split('-', 1)[0] + '"'
        raise ValueError(inside)

    try:
        return re.sub(r'\[([^\]]*)\]', swap, section), shown_colour
    except ValueError:
        return None


@lru_cache(maxsize=512)
def _tokens(section: str) -> tuple[tuple[bool, str], ...]:
    """A section as (is_literal, text) pieces: quoted text, escaped characters and spacing are literal."""
    return tuple(_walk(section))


def _walk(section: str):
    i = 0
    while i < len(section):
        ch = section[i]
        if ch == '"':
            end = section.find('"', i + 1)
            end = len(section) if end < 0 else end
            yield True, section[i + 1 : end]
            i = end + 1
        elif ch == '\\' and i + 1 < len(section):
            yield True, section[i + 1]
            i += 2
        elif ch == '_' and i + 1 < len(section):
            yield True, ' '
            i += 2
        elif ch == '*' and i + 1 < len(section):
            i += 2
        else:
            yield False, ch
            i += 1


def _number(value: float, section: str) -> str | None:
    pieces = list(_tokens(section))
    code = ''.join(text for literal, text in pieces if not literal)
    if '/' in code or re.search(r'[Ee][+-]', code) or '@' in code:
        return None  # fractions, scientific and text formats: show the plain value instead
    value *= 100 ** code.count('%')
    # The run of digit placeholders: everything from the first to the last of 0 # ? . , inside the code.
    spots = [i for i, (literal, text) in enumerate(pieces) if not literal and text in '0#?.,']
    if not spots or not any(pieces[i][1] in '0#?' for i in spots):
        return None if any(not literal and text.isalpha() for literal, text in pieces) else _join(pieces)
    first, last = spots[0], spots[-1]
    if any(literal or text not in '0#?.,' for literal, text in pieces[first : last + 1]):
        return None  # masks like 000-00-0000 put text between the digits; show the plain value instead
    run = ''.join(text for _, text in pieces[first : last + 1])
    trailing = len(run) - len(run.rstrip(','))
    value /= 1000**trailing
    run = run.rstrip(',')
    whole, _, decimals = run.partition('.')
    places, fixed = len(decimals), decimals.count('0')
    with localcontext(prec=60):  # room for every digit Excel can show, so a huge number still rounds
        rounded = Decimal(repr(abs(value))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    text = f'{rounded:{"," if "," in whole else ""}.{places}f}'
    integer, _, fraction = text.partition('.')
    fraction = fraction[:fixed] + fraction[fixed:].rstrip('0')
    digits = whole.count('0')
    if integer == '0' and digits == 0:
        integer = ''
    elif len(integer.replace(',', '')) < digits:
        integer = integer.zfill(digits)
    shown = _join(pieces[:first]) + integer + ('.' + fraction if fraction else '') + _join(pieces[last + 1 :])
    # Only a one-part format reaches here with a negative number; Excel puts the minus sign in front of everything.
    return '-' + shown if value < 0 and rounded else shown


def _join(pieces) -> str:
    return ''.join(text for _, text in pieces)


def _date(value, section: str) -> str | None:
    if isinstance(value, time):
        value = datetime.combine(date(1900, 1, 1), value)
    elif not isinstance(value, datetime):
        value = datetime.combine(value, time())
    # AM/PM and A/P are markers, not month or minute codes, so hold them aside as quoted text first.
    section = re.sub(r'(?i)am/pm', '"\x00"', section)
    section = re.sub(r'(?i)a/p', '"\x01"', section)
    pieces = list(_tokens(section))
    # Group repeated letters into codes (yyyy, mmm, hh), keeping literal text as it is.
    groups = []
    for literal, text in pieces:
        low = text.lower()
        if not literal and low in 'ymdhs' and groups and not groups[-1][0] and groups[-1][1][0] == low:
            groups[-1] = (False, groups[-1][1] + low)
        elif not literal and low in 'ymdhs':
            groups.append((False, low))
        else:
            groups.append((True, text))
    twelve = any(literal and ('\x00' in part or '\x01' in part) for literal, part in groups)
    codes = [i for i, (literal, _) in enumerate(groups) if not literal]
    out = ''
    for i, (literal, part) in enumerate(groups):
        if literal:
            out += part
            continue
        kind = part[0]
        if kind == 'm':
            before = next((groups[j][1] for j in reversed(codes) if j < i), '')
            after = next((groups[j][1] for j in codes if j > i), '')
            if len(part) <= 2 and (before.startswith('h') or after.startswith('s')):
                kind = 'minute'
        out += _date_part(value, kind, len(part), twelve)
    morning = value.hour < 12
    return out.replace('\x00', 'AM' if morning else 'PM').replace('\x01', 'A' if morning else 'P')


def _date_part(value: datetime, kind: str, size: int, twelve: bool) -> str:
    if kind == 'y':
        return f'{value.year:04d}' if size > 2 else f'{value.year % 100:02d}'
    if kind == 'm':
        return (str(value.month), f'{value.month:02d}', MONTHS[value.month - 1][:3], MONTHS[value.month - 1],
                MONTHS[value.month - 1][0])[min(size, 5) - 1]  # fmt: skip
    if kind == 'd':
        if size >= 3:
            return DAYS[value.weekday()] if size > 3 else DAYS[value.weekday()][:3]
        return f'{value.day:02d}' if size == 2 else str(value.day)
    if kind == 'h':
        hour = (value.hour % 12 or 12) if twelve else value.hour
        return f'{hour:02d}' if size >= 2 else str(hour)
    number = value.minute if kind == 'minute' else value.second
    return f'{number:02d}' if size >= 2 else str(number)


def shown_text(value, code: str | None) -> tuple[str, str | None] | None:
    """The text and any colour Excel shows for a value under a number format, or None to show the plain value."""
    if not code or code in ('General', '@') or value is None or isinstance(value, (bool, str)):
        return None
    sections = _sections(code)
    if isinstance(value, (datetime, date, time)):
        section = sections[0]
    elif isinstance(value, (int, float)):
        if value < 0 and len(sections) > 1:
            section, value = sections[1], -value
        elif value == 0 and len(sections) > 2:
            section = sections[2]
        else:
            section = sections[0]
    else:
        return None
    taken = _brackets(section)
    if taken is None:
        return None
    section, shown_colour = taken
    try:
        if isinstance(value, (datetime, date, time)):
            text = _date(value, section)
        elif re.search(r'(?<!\\)[yYdD]|(?<![0#?])[hHsS]', ''.join(t for lit, t in _tokens(section) if not lit)):
            return None  # a date format on a plain number: leave it to the plain value
        else:
            text = _number(float(value), section)
    except (IndexError, ValueError, OverflowError):
        return None
    # Accounting formats pad with spaces to line up columns; the preview aligns cells itself.
    return None if text is None else (text.strip(), shown_colour)
