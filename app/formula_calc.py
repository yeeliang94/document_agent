"""Works out formulas that have no saved result, so a sheet a program wrote previews with numbers, not formula text.

Excel saves each formula's result when it saves a file; openpyxl, which the agent writes with, does not. This reads
the whole referenced sheets on demand and evaluates the common functions. Anything it does not know (a defined name,
an array formula, a function outside the list below, a loop) raises `Unsupported`, and the preview keeps the formula.
"""

from __future__ import annotations

import math
import re
from datetime import date, datetime, time, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, ROUND_UP, Decimal
from functools import lru_cache

from openpyxl.utils import column_index_from_string
from openpyxl.utils.datetime import CALENDAR_WINDOWS_1900 as EPOCH_1900
from openpyxl.utils.datetime import from_excel, to_excel

MAX_CELLS = 500_000  # cells read across all sheets one preview may load to work out its formulas
MAX_RANGE = 200_000  # cells in one range a formula reads
MAX_HELD = 2_000_000  # cells held across all the ranges one preview's formulas read


class Unsupported(Exception):
    """A formula this calculator cannot work out; the preview shows its text instead."""


class XlError(Exception):
    """An Excel error value such as #DIV/0!, which Excel shows in the cell."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


ERRORS = ('#NULL!', '#DIV/0!', '#VALUE!', '#REF!', '#NAME?', '#NUM!', '#N/A')
_SHEET = r"(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!"
_CELL = r'\$?[A-Za-z]{1,3}\$?\d+'
_TOKEN = re.compile(
    rf"""\s*(?:
      (?P<str>"(?:[^"]|"")*")
    | (?P<err>\#(?:NULL!|DIV/0!|VALUE!|REF!|NAME\?|NUM!|N/A))
    | (?P<ref>(?:{_SHEET})?(?:{_CELL}(?::{_CELL})?|\$?[A-Za-z]{{1,3}}:\$?[A-Za-z]{{1,3}}))(?![\w(])
    | (?P<func>(?:_xlfn\.|_xlws\.)?[A-Za-z][\w.]*)\(
    | (?P<bool>TRUE|FALSE)(?![\w(])
    | (?P<num>(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)
    | (?P<op><>|<=|>=|[-+*/^&=<>%(),])
    )""",
    re.VERBOSE | re.IGNORECASE,
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, at, text = [], 0, text.rstrip()
    while at < len(text):
        m = _TOKEN.match(text, at)
        if not m or m.end() == at:
            raise Unsupported(text[at:])  # a defined name, a table reference, an array constant
        kind = m.lastgroup
        out.append((kind, m.group(kind)))
        at = m.end()
    return out


def _ref(text: str, here: str):
    sheet, _, place = text.rpartition('!')
    sheet = sheet[1:-1].replace("''", "'") if sheet.startswith("'") else sheet or here
    place = place.replace('$', '').upper()
    if ':' not in place:
        col, row = re.fullmatch(r'([A-Z]+)(\d+)', place).groups()
        return ('ref', sheet, int(row), column_index_from_string(col))
    a, b = place.split(':')
    if a.isalpha():  # a whole column like A:A
        return ('range', sheet, 1, column_index_from_string(a), None, column_index_from_string(b))
    (c1, r1), (c2, r2) = (re.fullmatch(r'([A-Z]+)(\d+)', p).groups() for p in (a, b))
    r1, r2, c1, c2 = int(r1), int(r2), column_index_from_string(c1), column_index_from_string(c2)
    return ('range', sheet, min(r1, r2), min(c1, c2), max(r1, r2), max(c1, c2))


class _Parser:
    """Excel's precedence, loosest first: comparison, &, + -, * /, ^, negation, %."""

    def __init__(self, text: str, sheet: str):
        self.tokens, self.at, self.sheet = _tokens(text), 0, sheet

    def peek(self):
        return self.tokens[self.at] if self.at < len(self.tokens) else (None, None)

    def take(self, value=None):
        kind, text = self.peek()
        if kind is None or (value is not None and text != value):
            raise Unsupported(f'expected {value}')
        self.at += 1
        return kind, text

    def parse(self):
        node = self.binary(0)
        if self.at != len(self.tokens):
            raise Unsupported('trailing text')
        return node

    LEVELS = (('=', '<>', '<', '>', '<=', '>='), ('&',), ('+', '-'), ('*', '/'), ('^',))

    def binary(self, level):
        if level == len(self.LEVELS):
            return self.unary()
        node = self.binary(level + 1)
        while self.peek()[0] == 'op' and self.peek()[1] in self.LEVELS[level]:
            op = self.take()[1]
            node = ('op', op, node, self.binary(level + 1))
        return node

    def unary(self):
        if self.peek() in (('op', '-'), ('op', '+')):
            op = self.take()[1]
            node = self.unary()
            return ('neg', node) if op == '-' else node
        node = self.primary()
        while self.peek() == ('op', '%'):
            self.take()
            node = ('pct', node)
        return node

    def primary(self):
        kind, text = self.take()
        if kind == 'num':
            return ('val', float(text))
        if kind == 'str':
            return ('val', text[1:-1].replace('""', '"'))
        if kind == 'bool':
            return ('val', text.upper() == 'TRUE')
        if kind == 'err':
            return ('err', text.upper())
        if kind == 'ref':
            return _ref(text, self.sheet)
        if kind == 'func':
            name, args = re.sub(r'^_xl(fn|ws)\.', '', text, flags=re.I).upper(), []
            if self.peek() != ('op', ')'):
                args.append(self.argument())
                while self.peek() == ('op', ','):
                    self.take()
                    args.append(self.argument())
            self.take(')')
            return ('call', name, args)
        if (kind, text) == ('op', '('):
            node = self.binary(0)
            self.take(')')
            return node
        raise Unsupported(text)

    def argument(self):
        return ('val', None) if self.peek() in (('op', ','), ('op', ')')) else self.binary(0)


def _same_size(node, like: list[list]):
    """A sum range as SUMIF reads it: Excel takes only its top-left cell, and sums a block the size of the criteria
    range from there, so =SUMIF(A1:A2,"x",B1) adds B1 and B2."""
    height, width = len(like), len(like[0]) if like else 0
    if node[0] == 'ref' or (node[0] == 'range' and node[4] is not None):
        sheet, row, col = node[1], node[2], node[3]
        return ('range', sheet, row, col, row + max(height, 1) - 1, col + max(width, 1) - 1)
    return node


def _error_or(value):
    """An error Excel saved in a cell (#DIV/0!, #N/A) as an error, so totals and IFERROR treat it as one."""
    return XlError(value) if isinstance(value, str) and value in ERRORS else value


class Range:
    """The values of a block of cells, row by row."""

    def __init__(self, rows: list[list]):
        self.rows = rows

    def flat(self):
        return [v for row in self.rows for v in row]


def _serial(value) -> float:
    if isinstance(value, datetime):
        return to_excel(value)
    if isinstance(value, date):
        return to_excel(datetime.combine(value, time()))
    return (value.hour * 3600 + value.minute * 60 + value.second) / 86400


def _number(value) -> float:
    if isinstance(value, XlError):
        raise value
    if value is None:
        return 0.0
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (datetime, date, time)):
        return _serial(value)
    try:
        return float(str(value).replace(',', '').strip())
    except ValueError:
        raise XlError('#VALUE!') from None


def _text(value) -> str:
    if isinstance(value, XlError):
        raise value
    if value is None:
        return ''
    if isinstance(value, bool):
        return 'TRUE' if value else 'FALSE'
    if isinstance(value, float):
        return f'{value:.15g}'
    if isinstance(value, (datetime, date, time)):
        return f'{_serial(value):.15g}'
    return str(value)


def _truth(value) -> bool:
    if isinstance(value, XlError):
        raise value
    if isinstance(value, str):
        if value.upper() in ('TRUE', 'FALSE'):
            return value.upper() == 'TRUE'
        raise XlError('#VALUE!')
    return bool(_number(value))


def _is_number(v) -> bool:
    """A value Excel stores as a number: dates and times are day numbers; TRUE/FALSE are not."""
    return isinstance(v, (int, float, datetime, date, time)) and not isinstance(v, bool)


def _summed(values, keep_place: bool = False) -> list:
    """The numbers a SUMIF, SUMIFS or SUMPRODUCT adds: text and TRUE/FALSE are skipped (or None where `keep_place`),
    and an error among them is Excel's answer, not something to leave out."""
    out = []
    for v in values:
        if isinstance(v, XlError):
            raise v
        if _is_number(v):
            out.append(_number(v))
        elif keep_place:
            out.append(None)
    return out


def _day(serial: float) -> datetime:
    """The date of a day number. Day 0 and earlier have no date openpyxl can give, so leave those to the formula."""
    if serial < 1:
        raise Unsupported('a day number before 1900')
    try:
        return from_excel(serial)
    except (ValueError, OverflowError):
        raise XlError('#NUM!') from None


def _round(value: float, digits: float, mode) -> float:
    places = Decimal(1).scaleb(-int(digits))
    return (
        float(Decimal(repr(value)).quantize(places, rounding=mode))
        if int(digits) >= 0
        else float((Decimal(repr(value)) / places).quantize(Decimal(1), rounding=mode) * places)
    )


def _compare(a, b) -> int:
    """Excel's order: numbers, then text (ignoring case), then TRUE/FALSE. An empty cell matches 0 or ""."""

    def rank(v):
        if isinstance(v, XlError):
            raise v
        if isinstance(v, bool):
            return 2, v
        if isinstance(v, str):
            return 1, v.casefold()
        return 0, _digits(_number(v))

    if a is None:
        a = '' if isinstance(b, str) else False if isinstance(b, bool) else 0.0
    if b is None:
        b = '' if isinstance(a, str) else False if isinstance(a, bool) else 0.0
    x, y = rank(a), rank(b)
    return (x > y) - (x < y)


def _criterion(criterion):
    """The test SUMIF, COUNTIF and their kin apply: ">=5", "<>paid", "Smith*", or a plain value."""
    if not isinstance(criterion, str):
        return lambda v: v is not None and _compare(v, criterion) == 0
    m = re.match(r'(<=|>=|<>|<|>|=)?(.*)', criterion, re.S)
    op, target = m.group(1) or '=', m.group(2)
    try:
        target = float(target.replace(',', ''))
    except ValueError:
        pass
    if isinstance(target, str) and op in ('=', '<>') and re.search(r'[*?~]', target):
        # * and ? are wildcards; ~ makes the next character plain, as in "~*"
        parts = re.findall(r'~.|[*?]|[^*?~]+|~$', target, re.S)
        pattern = re.compile(
            ''.join(
                '.*' if p == '*' else '.' if p == '?' else re.escape(p[1:] if p.startswith('~') else p) for p in parts
            ),
            re.I | re.S,
        )
        hit = lambda v: isinstance(v, str) and bool(pattern.fullmatch(v))  # noqa: E731
        return hit if op == '=' else lambda v: not hit(v)
    if target == '' and op in ('=', '<>'):
        return (lambda v: v in (None, '')) if op == '=' else (lambda v: v not in (None, ''))

    def test(v):
        if v is None or isinstance(v, XlError) or isinstance(target, float) != _is_number(v):
            return op == '<>'
        c = _compare(v, target)
        return {'=': c == 0, '<>': c != 0, '<': c < 0, '>': c > 0, '<=': c <= 0, '>=': c >= 0}[op]

    return test


@lru_cache(maxsize=4096)
def _parse(text: str, sheet: str):
    """A formula's parsed form. A sheet repeats one formula down its rows, so each text is parsed once."""
    return _Parser(text, sheet).parse()


def _digits(value: float) -> float:
    return float(f'{value:.15g}')  # Excel keeps 15 significant digits, so 0.1+0.2 is 0.3


class _Sheet:
    """One sheet's cells, read from the top only as far as a formula needs, so a big sheet is not read whole."""

    def __init__(self, calc: Calculator, title: str):
        self.calc, self.title = calc, title
        self.cells: dict[tuple[int, int], tuple] = {}
        self.rows = zip(
            calc.formulas[title].iter_rows(values_only=True),
            calc.values[title].iter_rows(values_only=True),
            strict=False,
        )
        self.reached, self.finished = 0, False

    def read_to(self, row: int | None) -> None:
        while not self.finished and (row is None or self.reached < row):
            try:
                raw, saved = next(self.rows)
            except StopIteration:
                self.finished = True
                return
            self.reached += 1
            self.calc.read += len(raw)
            if self.calc.read > MAX_CELLS:
                raise Unsupported('workbook too large')
            for c, v in enumerate(raw, 1):
                if v is not None:
                    kept = saved[c - 1] if c - 1 < len(saved) else None
                    self.cells[(self.reached, c)] = (_error_or(v), _error_or(kept))

    def get(self, row: int, col: int) -> tuple:
        self.read_to(row)
        return self.cells.get((row, col), (None, None))

    def last_row(self) -> int:
        self.read_to(None)
        return max((r for r, _ in self.cells), default=0)


class Calculator:
    """Results for one workbook. `formulas` and `values` are the same file opened without and with saved results."""

    def __init__(self, formulas, values):
        self.formulas, self.values = formulas, values
        self.names = {name.casefold(): name for name in formulas.sheetnames}  # Excel finds a sheet in any case
        self.sheets: dict[str, _Sheet] = {}
        self.read = 0
        self.done: dict[tuple, object] = {}
        self.ranges: dict[tuple, Range] = {}
        self.held = 0
        self.busy: set[tuple] = set()
        # A workbook counting days from 1904 (old Mac files) would have its dates come out four years off.
        self.mac_dates = getattr(formulas, 'epoch', EPOCH_1900) != EPOCH_1900

    def result(self, sheet: str, row: int, col: int):
        """What Excel would show in a cell: a number, text, TRUE/FALSE or an error code. Raises Unsupported."""
        if self.mac_dates:
            raise Unsupported('the 1904 date system')
        try:
            value = self.cell(sheet, row, col)
        except RecursionError:
            raise Unsupported('too deep') from None
        if value is None:
            return 0  # =E1 on an empty E1 shows 0
        if isinstance(value, XlError):
            return value.code
        if isinstance(value, float):
            if not math.isfinite(value):
                return '#NUM!'
            value = _digits(value)
            return int(value) if value.is_integer() and abs(value) < 1e15 else value
        return value

    def sheet(self, name: str) -> _Sheet:
        title = self.names.get(name.casefold())
        if title is None:
            raise XlError('#REF!')
        if title not in self.sheets:
            self.sheets[title] = _Sheet(self, title)
        return self.sheets[title]

    def cell(self, sheet: str, row: int, col: int):
        held = self.sheet(sheet)
        key = (held.title, row, col)
        if key in self.done:
            return self.done[key]
        raw, saved = held.get(row, col)
        if not (isinstance(raw, str) and raw.startswith('=')):
            if raw is not None and not isinstance(raw, (str, int, float, bool, datetime, date, time, XlError)):
                raise Unsupported(type(raw).__name__)  # an array or data-table formula
            return raw
        if saved is not None:
            return saved
        if key in self.busy:
            raise Unsupported('circular reference')
        self.busy.add(key)
        try:
            value = self.scalar(_parse(raw[1:], held.title))
        except XlError as error:
            value = error
        finally:
            self.busy.discard(key)
        self.done[key] = value
        return value

    def range(self, node) -> Range:
        _, sheet, r1, c1, r2, c2 = node
        held = self.sheet(sheet)
        if r2 is None:  # a whole column runs to the sheet's last row
            r2 = held.last_row()
        key = (held.title, r1, c1, r2, c2)
        if key not in self.ranges:  # =A2/SUM(A:A) on every row reads the column once, not once per row
            size = (r2 - r1 + 1) * (c2 - c1 + 1)
            self.held += size
            if size > MAX_RANGE or self.held > MAX_HELD:
                raise Unsupported('ranges too large')
            self.ranges[key] = Range([[self.cell(sheet, r, c) for c in range(c1, c2 + 1)] for r in range(r1, r2 + 1)])
        return self.ranges[key]

    def scalar(self, node):
        value = self.eval(node)
        if isinstance(value, Range):
            if len(value.rows) == 1 and len(value.rows[0]) == 1:
                return value.rows[0][0]
            raise Unsupported('a range where one value belongs')
        if isinstance(value, XlError):
            raise value
        return value

    def eval(self, node):
        kind = node[0]
        if kind == 'val':
            return node[1]
        if kind == 'err':
            raise XlError(node[1])
        if kind == 'ref':
            value = self.cell(*node[1:])
            if isinstance(value, XlError):
                raise value
            return value
        if kind == 'range':
            return self.range(node)
        if kind == 'neg':
            return -_number(self.scalar(node[1]))
        if kind == 'pct':
            return _number(self.scalar(node[1])) / 100
        if kind == 'op':
            return self.operate(node[1], self.scalar(node[2]), self.scalar(node[3]))
        if kind == 'call':
            return self.call(node[1], node[2])
        raise Unsupported(kind)

    @staticmethod
    def operate(op, a, b):
        if op == '&':
            return _text(a) + _text(b)
        if op in ('=', '<>', '<', '>', '<=', '>='):
            c = _compare(a, b)
            return {'=': c == 0, '<>': c != 0, '<': c < 0, '>': c > 0, '<=': c <= 0, '>=': c >= 0}[op]
        x, y = _number(a), _number(b)
        if op == '+':
            return x + y
        if op == '-':
            return x - y
        if op == '*':
            return x * y
        if op == '/':
            if y == 0:
                raise XlError('#DIV/0!')
            return x / y
        try:
            return float(x**y)
        except (OverflowError, ZeroDivisionError, TypeError):
            raise XlError('#NUM!') from None

    def numbers(self, args) -> list[float]:
        """The numbers SUM and its kin add up: typed arguments count when they read as numbers, while text and
        TRUE/FALSE inside a range are skipped."""
        out = []
        for arg in args:
            value = self.eval(arg)
            if isinstance(value, Range) or arg[0] == 'ref':  # a cell reference counts like a one-cell range
                out.extend(_summed(value.flat() if isinstance(value, Range) else [value]))
            elif value is not None:
                out.append(_number(value))
        return out

    def values_of(self, args) -> list:
        out = []
        for arg in args:
            value = self.eval(arg)
            out.extend(value.flat() if isinstance(value, Range) else [value])
        return out

    def block(self, node) -> list[list]:
        value = self.eval(node)
        return value.rows if isinstance(value, Range) else [[value]]

    def call(self, name, args):  # noqa: C901, PLR0911, PLR0912 - one branch per Excel function
        n = len(args)
        one = lambda i: self.scalar(args[i])  # noqa: E731
        num = lambda i, default=None: _number(one(i)) if i < n else default  # noqa: E731
        if name == 'SUM':
            return math.fsum(self.numbers(args))
        if name == 'PRODUCT':
            return math.prod(self.numbers(args))
        if name == 'COUNT':
            return float(sum(_is_number(v) for v in self.values_of(args)))
        if name in ('AVERAGE', 'MIN', 'MAX'):
            found = self.numbers(args)
            if name == 'AVERAGE':
                if not found:
                    raise XlError('#DIV/0!')
                return math.fsum(found) / len(found)
            return (min if name == 'MIN' else max)(found, default=0.0)
        if name == 'COUNTA':
            return float(sum(v not in (None,) for v in self.values_of(args)))
        if name == 'COUNTBLANK':
            return float(sum(v in (None, '') for v in self.values_of(args)))
        if name in ('ROUND', 'ROUNDUP', 'ROUNDDOWN'):
            mode = {'ROUND': ROUND_HALF_UP, 'ROUNDUP': ROUND_UP, 'ROUNDDOWN': ROUND_DOWN}[name]
            return _round(num(0), num(1, 0.0), mode)
        if name == 'INT':
            return float(math.floor(num(0)))
        if name == 'ABS':
            return abs(num(0))
        if name == 'MOD':
            if num(1) == 0:
                raise XlError('#DIV/0!')
            return num(0) - num(1) * math.floor(num(0) / num(1))
        if name == 'POWER':
            return self.operate('^', num(0), num(1))
        if name == 'IF':
            if _truth(one(0)):
                return self.scalar(args[1]) if n > 1 else True
            return self.scalar(args[2]) if n > 2 else False
        if name in ('IFERROR', 'IFNA'):
            try:
                return one(0)
            except XlError as error:
                if name == 'IFNA' and error.code != '#N/A':
                    raise
                return one(1)
        if name == 'AND':
            return all(_truth(v) for v in self.values_of(args) if v is not None)
        if name == 'OR':
            return any(_truth(v) for v in self.values_of(args) if v is not None)
        if name == 'NOT':
            return not _truth(one(0))
        if name in ('TRUE', 'FALSE'):
            return name == 'TRUE'
        if name in ('CONCATENATE', 'CONCAT'):
            return ''.join(_text(v) for v in self.values_of(args))
        if name == 'TEXTJOIN':
            sep, skip = _text(one(0)), _truth(one(1))
            return sep.join(_text(v) for v in self.values_of(args[2:]) if not (skip and v in (None, '')))
        if name in ('LEFT', 'RIGHT'):
            text, count = _text(one(0)), int(num(1, 1.0))
            return text[:count] if name == 'LEFT' else text[max(0, len(text) - count) :] if count else ''
        if name == 'MID':
            text, start, count = _text(one(0)), int(num(1)), int(num(2))
            if start < 1 or count < 0:
                raise XlError('#VALUE!')
            return text[start - 1 : start - 1 + count]
        if name == 'LEN':
            return float(len(_text(one(0))))
        if name in ('UPPER', 'LOWER', 'PROPER'):
            text = _text(one(0))
            return text.upper() if name == 'UPPER' else text.lower() if name == 'LOWER' else text.title()
        if name == 'TRIM':
            return re.sub(' +', ' ', _text(one(0))).strip(' ')
        if name == 'VALUE':
            return _number(one(0))
        if name in ('SUMIF', 'COUNTIF', 'AVERAGEIF'):
            where, test = self.block(args[0]), _criterion(one(1))
            add = self.block(_same_size(args[2], where)) if n > 2 else where
            hits = [
                add[r][c] if r < len(add) and c < len(add[r]) else None
                for r, row in enumerate(where)
                for c, v in enumerate(row)
                if test(v)
            ]
            if name == 'COUNTIF':
                return float(len(hits))
            found = _summed(hits)
            if name == 'SUMIF':
                return math.fsum(found)
            if not found:
                raise XlError('#DIV/0!')
            return math.fsum(found) / len(found)
        if name in ('SUMIFS', 'COUNTIFS', 'AVERAGEIFS'):
            first = 0 if name == 'COUNTIFS' else 1
            pairs = [(self.block(args[i]), _criterion(one(i + 1))) for i in range(first, n - 1, 2)]
            size = (len(pairs[0][0]), len(pairs[0][0][0]) if pairs[0][0] else 0)
            if any((len(b), len(b[0]) if b else 0) != size for b, _ in pairs):
                raise XlError('#VALUE!')
            spots = [(r, c) for r in range(size[0]) for c in range(size[1]) if all(t(b[r][c]) for b, t in pairs)]
            if name == 'COUNTIFS':
                return float(len(spots))
            add = self.block(args[0])
            found = _summed(add[r][c] for r, c in spots)
            if name == 'SUMIFS':
                return math.fsum(found)
            if not found:
                raise XlError('#DIV/0!')
            return math.fsum(found) / len(found)
        if name == 'SUMPRODUCT':
            blocks = [self.block(a) for a in args]
            flat = [[v for row in b for v in row] for b in blocks]
            if len({len(f) for f in flat}) != 1:
                raise XlError('#VALUE!')
            columns = [[x or 0.0 for x in _summed(f, keep_place=True)] for f in flat]
            return math.fsum(math.prod(column[i] for column in columns) for i in range(len(flat[0])))
        if name in ('VLOOKUP', 'HLOOKUP'):
            key, table, index = one(0), self.block(args[1]), int(num(2))
            exact = n > 3 and not _truth(one(3))
            lines = table if name == 'VLOOKUP' else [list(col) for col in zip(*table, strict=False)]
            if index < 1 or not lines or index > len(lines[0]):
                raise XlError('#REF!')
            found = self.find(key, [line[0] for line in lines], exact)
            return lines[found][index - 1]
        if name == 'MATCH':
            values = [v for row in self.block(args[1]) for v in row]
            kind = int(num(2, 1.0))
            if kind == -1:
                raise Unsupported('MATCH descending')
            return float(self.find(one(0), values, kind == 0) + 1)
        if name == 'XLOOKUP':
            if (n > 4 and num(4) != 0) or (n > 5 and num(5) != 1):
                raise Unsupported('XLOOKUP match or search mode')
            keys = [v for row in self.block(args[1]) for v in row]
            results = self.block(args[2])
            flat = [v for row in results for v in row] if len(results) == 1 or len(results[0]) == 1 else None
            if flat is None or len(flat) != len(keys):
                raise Unsupported('XLOOKUP returning a row')
            try:
                return flat[self.find(one(0), keys, True)]
            except XlError:
                if n > 3:
                    return one(3)
                raise
        if name == 'INDEX':
            table, r = self.block(args[0]), int(num(1))
            c = int(num(2, 0.0))
            if len(table) == 1 and c == 0:
                r, c = 1, r
            if c == 0 and len(table[0]) == 1:
                c = 1
            if r < 1 or c < 1 or r > len(table) or c > len(table[0]):
                raise XlError('#REF!') if r and c else Unsupported('INDEX of a whole row or column')
            return table[r - 1][c - 1]
        if name in ('ISBLANK', 'ISNUMBER', 'ISTEXT', 'ISERROR', 'ISNA'):
            try:
                value = self.eval(args[0])
                value = value.rows[0][0] if isinstance(value, Range) else value
            except XlError as error:
                return name == 'ISERROR' or (name == 'ISNA' and error.code == '#N/A')
            if name == 'ISBLANK':
                return value is None
            if name == 'ISNUMBER':
                return isinstance(value, (int, float, datetime, date, time)) and not isinstance(value, bool)
            if name == 'ISTEXT':
                return isinstance(value, str)
            return False
        if name == 'NA':
            raise XlError('#N/A')
        if name == 'DATE':
            year, month, day = int(num(0)), int(num(1)), int(num(2))
            year, month = year + (month - 1) // 12, (month - 1) % 12 + 1
            return datetime(year, month, 1) + timedelta(days=day - 1)
        if name in ('YEAR', 'MONTH', 'DAY'):
            return float(getattr(_day(num(0)), name.lower()))
        if name in ('EOMONTH', 'EDATE'):
            when, months = _day(num(0)), int(num(1))
            year, month = when.year + (when.month - 1 + months) // 12, (when.month - 1 + months) % 12 + 1
            if name == 'EOMONTH':
                return datetime(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)
            last = (datetime(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
            return datetime(year, month, min(when.day, last))
        raise Unsupported(name)

    @staticmethod
    def find(key, values: list, exact: bool) -> int:
        """Where a lookup lands: the first equal value, or for an approximate match on sorted values, the last one
        not above the key."""
        if exact:
            test = _criterion(key) if isinstance(key, str) and re.search(r'[*?]', key) else None
            for i, v in enumerate(values):
                if test(v) if test else v is not None and not isinstance(v, XlError) and _compare(v, key) == 0:
                    return i
            raise XlError('#N/A')
        best = None
        for i, v in enumerate(values):
            if v is None or isinstance(v, XlError) or isinstance(v, str) != isinstance(key, str):
                continue
            if _compare(v, key) > 0:
                break
            best = i
        if best is None:
            raise XlError('#N/A')
        return best
