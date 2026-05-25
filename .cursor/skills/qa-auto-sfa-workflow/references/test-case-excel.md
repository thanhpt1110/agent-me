# Test Case Excel Generation

Use this reference for step 2: generate a DevTest-compatible Excel workbook
from the reviewed plan.

## Python Library

Use `openpyxl` for populated `.xlsx` generation.

Reason:

- The downstream `devtest-helper` repo reads `.xlsx` through its
  `ExcelHelper`, which uses `openpyxl`.
- `openpyxl` allows explicit header order, widths, wrapping, hyperlinks, and
  future cell formatting without changing the downstream reader.
- `pandas` is acceptable for quick CSV/dataframe transforms, but the canonical
  workbook writer should be `openpyxl`.

## Workbook Source of Truth

If the user provides a ST2110/API sample workbook, copy its sheet name, header
order, required columns, enum spelling, and any formatting that affects import.
If no sample is available, use the `devtest-helper` create-template schema.

Fallback DevTest workbook headers:

```text
folderPath
Template ID
Title
Template State
Template Owner
Test Description
Test Procedure
Expected Result
E. Setup Time
E.Test Time
Origin
Template Priority
Automation Status
Automation Status Linux
KPI
Execution Owner
SWQA User Mode
Functional Test Type
Release
Tool/App. Location
Tool/App. Required for Execution
```

Important details:

- `folderPath` and `Title` are required for `python3 main.py -f <file> -c`.
- Leave `Template ID` blank for create runs. The helper writes new IDs back to
  the workbook data after creation.
- Use `Template Priority` values that can normalize to P0/P1/P2. The helper
  maps P0 to `P0 - Must Have`, P1 to `P1 - Should Have`, and other values to
  `P2 - Nice To Have`.
- Use multiline plain text in `Test Procedure`, `Expected Result`, and
  `Test Description`; `devtest-helper` formats newlines into DevTest HTML.
- Default `Automation Status` to `In A&T` unless the owner requests another
  state.

## No Existing Workbook

If the user has not provided an Excel sample and no workbook exists, create one
instead of stopping:

```bash
python .cursor/skills/qa-auto-sfa-workflow/scripts/create_devtest_excel_template.py \
  test-cases/devtest-test-cases.xlsx
```

That script creates a header-only `.xlsx` with the fallback headers above. It
uses only the Python standard library so it can run even before `openpyxl` is
installed. For actual generated test-case rows, use `openpyxl` when available.

## Test Case Quality Rules

Each row should have:

- One clear verification objective.
- Preconditions and test data in the description or first procedure steps.
- Deterministic steps.
- Observable expected results.
- Priority tied to requirement risk.
- Platform mapping when the feature differs by Linux, Windows, or both.
- Traceability back to source sections from the intake review.

## Review Artifact

Alongside the workbook, create `test-cases/test-case-review.md` with:

- Count by priority.
- Count by platform.
- Count by functional area.
- Assumptions that affected test generation.
- Any PRD/test-plan gaps.
- A "ready for DevTest create" yes/no recommendation.

Do not run DevTest create/update before the user reviews this artifact.
