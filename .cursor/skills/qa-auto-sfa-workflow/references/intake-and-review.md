# Intake and Test Plan Review

Use this reference for step 1: review PRDs, test plans, design docs, and
coverage inputs before writing test cases.

## Source Inputs

Supported input paths:

- Local files supplied by the user.
- Files retrieved through Glean or document MCP connectors.
- Manually pasted PRD/test-plan excerpts.

For local PDF, Word, PowerPoint, Excel, HTML, or image-backed docs, use
Microsoft MarkItDown as the preferred conversion layer. It is designed to
convert many file formats into Markdown for LLM consumption. Prefer the Python
library or CLI in a separate working folder, then inspect the generated
Markdown before reasoning from it.

Example command shape:

```bash
markitdown input.pdf > intake/input.md
markitdown input.docx > intake/input-docx.md
```

If MarkItDown is not installed, document the blocker and either ask the user to
install it or continue from provided Markdown/text only. Do not silently OCR or
parse binary documents with ad hoc shell strings.

## Glean or MCP Retrieval

When the user names a document but does not provide a file:

1. Search the relevant connector, usually Glean first for broad enterprise
   search.
2. Fetch the smallest useful document content.
3. Save a manifest entry with source system, title, URL/path, timestamp, and
   any truncation note.
4. Convert only retrieved file content that needs conversion. Native Markdown
   or text can go straight into the review bundle.

## Intake Manifest

Create `intake/manifest.md` with:

| Field | Meaning |
|---|---|
| Source | Local path, Glean result, Drive/SharePoint path, or manual paste |
| Type | PRD, test plan, requirements, API spec, release note, unknown |
| Converted file | Markdown file generated or `inline` |
| Trust level | source, user-provided, inferred |
| Notes | truncation, missing sections, password-protected file, unreadable pages |

Ask the user to approve the manifest before step 2 if the source set is large,
ambiguous, or missing a referenced document.

## Review Output

Create `review/test-plan-review.md` with:

- Scope summary.
- In-scope and out-of-scope feature list.
- Target platforms and release types.
- Acceptance criteria.
- Coverage matrix by feature, platform, priority, and test type.
- Dependencies and test data.
- Ambiguities, blockers, and questions for the owner.
- Proposed test case groups.

Do not generate the final Excel until open questions that change coverage are
resolved or explicitly marked as assumptions.
