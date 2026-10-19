"""Application-issued progress from verified tool outcomes; workbook-specific adapter."""

from .state import identity
from .supervisor import CapabilityResult, Supervisor


def observe(deps, name, args, result):
    state = deps.state
    supervisor = Supervisor(state, deps.actor)
    events = []

    def emit(kind, key, version, linked=False):
        if supervisor.accept(
            CapabilityResult(progress=[{'kind': kind, 'key': key, 'version': version, 'linked': linked}])
        ):
            events.append({'kind': kind, 'key': key, 'version': version})

    if not isinstance(result, dict) or result.get('error'):
        return events
    if name in ('read_sheet', 'read_document', 'inspect_workbook', 'search_cells', 'inspect_regions', 'view_page'):
        entry = state.file_entry(args.get('path'))[1]
        if not entry and name == 'view_page':
            entry = next(
                ({'sha256': v['source_hash']} for _, v in state.all('visual') if v['artifact'] == args.get('path')),
                None,
            )
        if entry:
            key = identity(deps.actor, args, entry['sha256'])
            state.put('evidence', key, {'source_hash': entry['sha256'], 'location': args, 'actor': deps.actor})
            emit('evidence_inspected', key, entry['sha256'])
            result['evidence_id'] = key
    if name == 'inspect_source':
        for sheet in result.get('sheets', []):
            entry = state.file_entry(sheet['path'])[1]
            emit(
                'cursor_extended',
                sheet['id'],
                identity(entry['sha256'], sheet.get('next_row'), sheet.get('scan_complete')),
            )
    if name in (
        'extract_dataset',
        'extract_region',
        'transform_dataset',
        'resolve_records',
        'record_observations',
    ) and result.get('id'):
        rows = state.get('rows', result['id'], [])
        emit(
            'dataset_changed',
            identity(result.get('path'), result.get('sheet'), result.get('columns')),
            identity([{key: row[key] for key in ('values', 'sources', 'issues')} for row in rows]),
        )
    if name == 'save_output_requirements' and result.get('id'):
        emit('interpretation_settled', result['id'], identity({k: v for k, v in result.items() if k != 'id'}))
    if name in ('populate_template', 'write_cells', 'recover_output_operation'):
        for key, item in state.all('assignment'):
            emit('output_assigned', key, identity(item['values'], item['sources']))
    if name in ('record_decisions', 'save_interpretation'):
        for kind in ('decision', 'region_role', 'inventory'):
            for key, item in state.all(kind):
                if kind == 'inventory' and item.get('status') not in ('reviewed', 'excluded'):
                    continue
                meaningful = {k: v for k, v in item.items() if k not in ('rationale', 'reason', 'evidence')}
                emit('interpretation_settled', key, identity(meaningful))
    if name in ('validate_output', 'validate_job') and result.get('passed'):
        emit('validation_improved', name, result.get('sha256', identity(result)))
    if name == 'apply_instructions' and result.get('applied'):
        for instruction in state.instructions():
            if instruction['seq'] in result['applied'] and instruction['kind'] == 'answer':
                emit('question_resolved', str(instruction['seq']), identity(instruction['text']), True)
    if events:
        message = name.replace('_', ' ') + ': saved useful change'
        if name in ('extract_dataset', 'extract_region'):
            message = f'Extracted {result["rows"]} candidate records; {result.get("exceptions", 0)} have exceptions.'
        elif name == 'populate_template':
            message = f'Wrote {result["records_written"]} output records; {result["unresolved"]} remain held.'
        elif name == 'save_interpretation':
            region = state.get('region', result['region'])
            message = f'Classified {region["sheet"]}!{region["bounds"]} as {result["role"].replace("_", " ")}.'
        elif name == 'save_output_requirements':
            message = f'Saved requirements for {len(result["areas"])} destination areas.'
        checkpoint = {'tool': name, 'events': events[:20], 'message': message}
        state.put('checkpoint', deps.actor, checkpoint)
    return events
