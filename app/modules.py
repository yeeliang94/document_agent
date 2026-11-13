"""The modules a job can belong to, and each one's default house rules (editable on the Modules page)."""

DEFAULT_CONTEXT = {
    'payroll': 'Prepare the monthly payroll ingestion workbook from the supplied files. Match employee IDs; reconcile joiners, changes, allowances, one-time payments and resignations. Read template instructions and lookup tabs. Preserve formulas. Investigate conflicts and ask when material facts are missing. Continue independent records.',
    'ap': 'Prepare the accounts payable upload. Match supplier IDs and invoice numbers, identify duplicates, reconcile amounts, and preserve the supplied template. Ask about material unresolved conflicts.',
}

MODULE_LABELS = {'payroll': 'Payroll', 'ap': 'Accounts payable'}
