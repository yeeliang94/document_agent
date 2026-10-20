"""Source-backed destination requirements and checks against current output bytes."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Literal

from openpyxl import load_workbook
from openpyxl.utils.cell import range_boundaries
from pydantic import BaseModel, Field

from .files import within
from .state import identity


class RequiredField(BaseModel):
    column: int = Field(ge=1, le=16384)
    meaning: str = Field(min_length=1, max_length=500)
    required: bool = True
    kind: Literal['any', 'identifier', 'decimal', 'date'] = 'any'
    allowed_values: list[str] = Field(default_factory=list, max_length=100)
    unique: bool = False
    expected_total: str | None = None


class OutputArea(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    sheet: str
    bounds: str
    role: Literal['required', 'optional', 'reference', 'preserved', 'unresolved']
    fields: list[RequiredField] = Field(default_factory=list, max_length=100)
    min_records: int = Field(default=0, ge=0)
    evidence: list[str] = Field(min_length=1, max_length=20)
    rationale: str = Field(min_length=1, max_length=2000)


class OutputSpecification(BaseModel):
    objective: str = Field(min_length=1, max_length=3000)
    path: str
    areas: list[OutputArea] = Field(min_length=1, max_length=50)
    material_questions: list[str] = Field(default_factory=list, max_length=20)


def contains(outer, inner):
    a, b, c, d = range_boundaries(outer)
    e, f, g, h = range_boundaries(inner)
    return a <= e <= g <= c and b <= f <= h <= d


def save_requirements(deps, specification):
    deps.resolve(specification.path, output=True)
    state = deps.state
    template = next(entry for _, entry in state.all('inventory') if entry.get('role') == 'template')
    if len({area.id for area in specification.areas}) != len(specification.areas):
        raise ValueError('Area IDs must be unique')
    for area in specification.areas:
        bounds = range_boundaries(area.bounds)
        if None in bounds or min(bounds) < 1 or bounds[2] > 16384 or bounds[3] > 1048576:
            raise ValueError('Use explicit valid Excel ranges')
        if (bounds[2] - bounds[0] + 1) * (bounds[3] - bounds[1] + 1) > 1_000_000:
            raise ValueError('Requirement areas must be bounded to one million cells')
        if any(not bounds[0] <= field.column <= bounds[2] for field in area.fields):
            raise ValueError('Field column is outside its area')
        for key in area.evidence:
            evidence = state.get('evidence', key)
            if (
                not evidence
                or evidence['source_hash'] != template['sha256']
                or evidence.get('actor', 'main') != deps.actor
            ):
                raise ValueError(
                    'Cite inspected destination evidence IDs. Available references: '
                    + str(
                        [
                            {'id': key, 'location': value['location']}
                            for key, value in state.all('evidence')
                            if value['source_hash'] == template['sha256'] and value.get('actor', 'main') == deps.actor
                        ][:8]
                    )
                )
    result = specification.model_dump() | {
        'instruction_revision': state.revision('instruction_revision'),
        'template_hash': template['sha256'],
    }
    key = identity(specification.path)
    old = state.get('requirement', key)
    if old:
        state.put('requirement_history', identity(old), old)
    state.put('requirement', key, result)
    return {'id': key, **result}


def check_requirements(state, path):
    specification = state.get('requirement', identity(path))
    failures = []
    if not specification:
        return ['Save source-backed destination requirements before final validation']
    if specification.get('stale') or specification['instruction_revision'] != state.revision('instruction_revision'):
        failures.append('Destination requirements are stale')
    failures.extend('Unresolved requirement: ' + q for q in specification['material_questions'])
    template_ids = {key for key, source in state.all('inventory') if source.get('role') == 'template'}
    template_sheets = {key for key, source in state.all('inventory') if source.get('parent') in template_ids}
    if not template_sheets or any(not state.get('inventory', key).get('scan_complete') for key in template_sheets):
        failures.append('Destination-wide discovery has not completed')
    for _, region in state.all('region'):
        if region.get('superseded') or region['source'] not in template_sheets:
            continue
        if not any(
            area['sheet'] == region['sheet']
            and area['role'] != 'unresolved'
            and contains(area['bounds'], region['bounds'])
            for area in specification['areas']
        ):
            failures.append('Unclassified destination area: ' + region['sheet'] + '!' + region['bounds'])
    workbook = load_workbook(within(state.root, path), data_only=False)
    try:
        for area in specification['areas']:
            if area['role'] == 'unresolved':
                failures.append('Unresolved destination area: ' + area['id'])
                continue
            if area['sheet'] not in workbook.sheetnames:
                failures.append('Missing destination sheet: ' + area['sheet'])
                continue
            if area['role'] not in ('required', 'optional'):
                continue
            left, top, right, bottom = range_boundaries(area['bounds'])
            sheet = workbook[area['sheet']]
            active = [
                row
                for row in range(top, bottom + 1)
                if any(sheet.cell(row, col).value is not None for col in range(left, right + 1))
            ]
            if len(active) < max(area['min_records'], 1 if area['role'] == 'required' else 0):
                failures.append('Too few output records: ' + area['id'])
            for field in area['fields']:
                values = [sheet.cell(row, field['column']).value for row in active]
                if field['required'] and any(v is None or v == '' for v in values):
                    failures.append('Missing required value: ' + field['meaning'])
                present = [v for v in values if v is not None and v != '']
                if field['unique'] and len(set(map(str, present))) != len(present):
                    failures.append('Duplicate values: ' + field['meaning'])
                if field['allowed_values'] and any(str(v) not in field['allowed_values'] for v in present):
                    failures.append('Disallowed value: ' + field['meaning'])
                if field['kind'] == 'identifier' and any(not isinstance(v, str) for v in present):
                    failures.append('Identifier must retain text representation: ' + field['meaning'])
                if field['kind'] == 'decimal' and any(
                    not isinstance(v, (int, float)) or isinstance(v, bool) for v in present
                ):
                    failures.append('Numeric field is not stored as an Excel number: ' + field['meaning'])
                if field['kind'] == 'date':
                    for value in present:
                        try:
                            if not isinstance(value, (date, datetime)):
                                date.fromisoformat(str(value))
                        except ValueError:
                            failures.append('Invalid effective date: ' + field['meaning'])
                            break
                if field['kind'] == 'decimal' or field['expected_total'] is not None:
                    try:
                        numbers = [Decimal(str(v)) for v in present]
                        if not all(v.is_finite() for v in numbers):
                            raise InvalidOperation
                        if field['expected_total'] is not None and sum(numbers) != Decimal(field['expected_total']):
                            failures.append('Total mismatch: ' + field['meaning'])
                    except (InvalidOperation, ValueError):
                        failures.append('Uncheckable numeric value or formula cache: ' + field['meaning'])
    finally:
        workbook.close()
    return failures


def check_write_scope(state, path, sheet, first_row, last_row, columns):
    from .regions import accounting_version

    if accounting_version(state) != 2:
        return
    specification = state.get('requirement', identity(path), {})
    if (
        not specification
        or specification.get('stale')
        or specification['instruction_revision'] != state.revision('instruction_revision')
    ):
        raise ValueError('Save current output requirements before writing')
    areas = [
        (area['role'], range_boundaries(area['bounds'])) for area in specification['areas'] if area['sheet'] == sheet
    ]
    for column in columns:
        if any(
            role not in ('required', 'optional') and left <= column <= right and top <= last_row and first_row <= bottom
            for role, (left, top, right, bottom) in areas
        ):
            raise ValueError(f'Write intersects a protected destination area: {sheet}!{column}:{first_row}-{last_row}')
        if not any(
            role in ('required', 'optional') and left <= column <= right and top <= first_row <= last_row <= bottom
            for role, (left, top, right, bottom) in areas
        ):
            raise ValueError(f'Write falls outside established input areas: {sheet}!{column}:{first_row}-{last_row}')
