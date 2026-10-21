"""Bounded model interface for the durable loop and region-based workbook work."""

from pydantic_ai import RunContext

from .datasets import Column, extract_dataset
from .regions import RegionRole, indexed_cells, save_role, search
from .requirements import OutputSpecification, save_requirements
from .state import identity, paginate
from .tools import JobDeps, registrar
from .workplan import WorkPlan, save_plan


def register_goal_tools(agent, readonly=False):
    tool = registrar(agent, readonly, {'search_cells', 'inspect_regions'})

    @tool
    async def update_work_plan(ctx: RunContext[JobDeps], plan: WorkPlan) -> dict:
        """Save your objective, work items, blockers, next actions and free-form notes. It survives restarts and context rebuilds; rewrite it whenever useful."""
        return await ctx.deps.action('update_work_plan', {}, lambda: save_plan(ctx.deps.state, plan))

    @tool
    async def search_cells(
        ctx: RunContext[JobDeps], path: str, query: str, sheet: str | None = None, offset: int = 0, limit: int = 25
    ) -> dict:
        """Search indexed cell text across sheets, including hidden sheets and content below previews. Returns coordinates, nearby content and index completeness."""

        def read():
            ctx.deps.resolve(path)
            return search(ctx.deps.state, path, query, sheet, offset, limit)

        return await ctx.deps.action(
            'search_cells', {'path': path, 'query': query, 'sheet': sheet, 'offset': offset, 'limit': limit}, read
        )

    @tool
    async def inspect_regions(
        ctx: RunContext[JobDeps], path: str, region: str | None = None, offset: int = 0, limit: int = 25
    ) -> dict:
        """Query candidate regions or inspect one with bounded boundary samples. Suggestions are uncertain; use read_sheet for exact multi-row headers/formulas. Also lists embedded image references."""

        def read():
            ctx.deps.resolve(path)
            state = ctx.deps.state
            items = [
                {'id': key, **value, 'role': state.get('region_role', key)}
                for key, value in state.all('region')
                if value['path'] == path and not value.get('superseded') and (not region or key == region)
            ]
            if region:
                for item in items:
                    cells = indexed_cells(state, item['source'])
                    from openpyxl.utils.cell import range_boundaries

                    left, top, right, bottom = range_boundaries(item['bounds'])
                    selected = [c for c in cells if left <= c['column'] <= right and top <= c['row'] <= bottom]
                    # Include end samples to reveal totals and boundaries, not only the first preview.
                    item['boundary_samples'] = selected[:20] + selected[-10:]
            return paginate(items, offset, limit) | {
                'visuals': [{'id': key, **value} for key, value in state.all('visual') if value['path'] == path][:25]
            }

        return await ctx.deps.action(
            'inspect_regions', {'path': path, 'region': region, 'offset': offset, 'limit': limit}, read
        )

    @tool
    async def revise_region(
        ctx: RunContext[JobDeps], region: str, bounds: str, supersede: list[str], reason: str
    ) -> dict:
        """Resize/split/merge candidate boundaries. Superseding an accepted mapping invalidates its datasets and output validation. New regions require a fresh role and extraction; remaining occupied cells must still be classified."""

        def revise():
            from openpyxl.utils.cell import range_boundaries

            state = ctx.deps.state
            old = state.get('region', region)
            limits = range_boundaries(bounds)
            if (
                not old
                or None in limits
                or min(limits) < 1
                or limits[2] > 16384
                or limits[3] > 1048576
                or not reason.strip()
            ):
                raise ValueError('Use an existing region and valid explicit bounds with a reason')
            for key in supersede:
                item = state.get('region', key)
                if not item or item['source'] != old['source']:
                    raise ValueError('Cannot supersede a region on another sheet')
            key = identity(old['source'], bounds, old['index_version'])
            updated = old | {
                'bounds': bounds,
                'revision_reason': reason,
                'superseded': False,
                'header_rows': [limits[1]],
                'samples': [],
            }
            for target in supersede:
                mapping = state.get('region_mapping', target)
                if mapping:
                    state.put('region_mapping', target, mapping | {'stale': reason})
                state.put('region', target, state.get('region', target) | {'superseded': True})
            state.put('region', key, updated)
            return {'id': key, **updated}

        return await ctx.deps.action('revise_region', {'region': region, 'bounds': bounds}, revise)

    @tool
    async def save_interpretation(ctx: RunContext[JobDeps], decision: RegionRole) -> dict:
        """Accept a region as records, reference, instructions, outside scope or unresolved. Cite evidence_id from an actual inspected source result; names alone do not establish period/entity."""

        return await ctx.deps.action(
            'save_interpretation', decision.model_dump(), lambda: save_role(ctx.deps.state, decision)
        )

    @tool
    async def extract_region(
        ctx: RunContext[JobDeps],
        region: str,
        start_row: int,
        end_row: int,
        columns: list[Column],
        header_rows: list[int],
    ) -> dict:
        """Extract selected columns from an accepted records or reference region. Reference rows support joins without requiring output assignments. Region/mapping record IDs keep adjacent tables independent. Preserve types and formula-cache exceptions."""

        def extract():
            item = ctx.deps.state.get('region', region)
            if not item:
                raise ValueError('Unknown region')
            ctx.deps.resolve(item['path'])
            return extract_dataset(
                ctx.deps.state,
                item['path'],
                item['sheet'],
                start_row,
                end_row,
                columns,
                header_rows,
                ctx.deps.check_stop,
                region_id=region,
            )

        return await ctx.deps.action('extract_region', {'region': region}, extract)

    @tool
    async def save_output_requirements(ctx: RunContext[JobDeps], specification: OutputSpecification) -> dict:
        """Save source-backed destination meanings, writable/preserved areas, required fields, allowed values, totals and material questions. Classify every discovered template area before completion."""

        return await ctx.deps.action(
            'save_output_requirements', {'path': specification.path}, lambda: save_requirements(ctx.deps, specification)
        )

    @tool
    async def resolve_visual(ctx: RunContext[JobDeps], visual: str, role: str, evidence: str, reason: str) -> dict:
        """Resolve an embedded image after viewing its pixels; cite its evidence_id and explain instructions/reference/outside_scope relevance."""

        def save():
            state = ctx.deps.state
            item = state.get('visual', visual)
            read = state.get('evidence', evidence, {})
            if (
                not item
                or read.get('actor', 'main') != ctx.deps.actor
                or role not in ('instructions', 'reference', 'outside_scope')
                or (
                    read.get('location', {}).get('path') != item['artifact']
                    if item.get('artifact')
                    else (role != 'outside_scope' or read.get('source_hash') != item['source_hash'])
                )
                or not reason.strip()
            ):
                if not item:
                    raise ValueError(
                        'Unknown visual. resolve_visual is for images embedded in workbooks (listed by inspect_regions); '
                        'account for a standalone image file with record_decisions instead.'
                    )
                raise ValueError(
                    'Cite the evidence_id from viewing this image with view_page, use role instructions, reference '
                    'or outside_scope, and give a reason'
                )
            state.put(
                'visual',
                visual,
                item
                | {
                    'status': 'resolved',
                    'role': role,
                    'reason': reason,
                    'evidence': evidence,
                    'instruction_revision': state.revision('instruction_revision'),
                },
            )
            return {'id': visual, 'status': 'resolved'}

        return await ctx.deps.action('resolve_visual', {'visual': visual}, save)
