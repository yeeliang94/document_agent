"""The two playbooks every new installation starts with: their names and their first instructions.

A job still stores its playbook's id under `module`, the earlier name, so that older runs keep opening."""

DEFAULT_CONTEXT = {
    'payroll': 'Prepare the monthly payroll ingestion workbook from the supplied files. Match employee IDs; reconcile joiners, changes, allowances, one-time payments and resignations. Read template instructions and lookup tabs. Preserve formulas. Investigate conflicts and ask when material facts are missing. Continue independent records.',
    'ap': 'Prepare the accounts payable upload. Match supplier IDs and invoice numbers, identify duplicates, reconcile amounts, and preserve the supplied template. Ask about material unresolved conflicts.',
}

MODULE_LABELS = {'payroll': 'Payroll', 'ap': 'Accounts payable'}
