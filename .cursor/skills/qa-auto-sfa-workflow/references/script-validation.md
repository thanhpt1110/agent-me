# Script Implementation and Validation

Use this reference for step 4 and optional post-release validation.

## Human-in-the-loop Folder Decision

Before writing automation scripts, ask the human to choose:

- Target automation repo.
- Test folder or module.
- Naming convention.
- Pytest marker/template ID convention.
- Required fixtures, environment, and secrets.
- Whether the script should be created now or only planned.

Do not infer the destination folder from the PRD alone. If the repo has
existing tests for the same feature, show candidate paths and ask for a
decision.

## Script Plan

Create `scripts/script-plan.md` with:

- Test cases selected for automation.
- Proposed files and functions/classes.
- Fixture and environment requirements.
- Mapping from DevTest Template ID to pytest marker, usually `T<templateId>`.
- Manual steps that remain.
- Risks and blockers.

Only after approval should an agent edit the target test repo.

## DevTest Helper Validation Utility

`cloud-service-qa/devtest-helper` includes
`utils/script_validation_workflow_helper.py` and `utils/task_example.yaml`.
The utility can run pytest, generate a pytest-html report, upload logs, and
post script validation phases back to DevTest.

Copy into the target test repo:

```bash
cp <devtest-helper>/utils/script_validation_workflow_helper.py <test-repo>/
cp <devtest-helper>/utils/task_example.yaml <test-repo>/
```

Install requirements in the target repo environment:

```bash
pip install -r <devtest-helper>/utils/requirements.txt
```

Task file shape:

```yaml
repoName: "ngc_cli_test"
userName: "thaphan"
taskFolder: "LinuxRelease"
task:
  - templateId: 3209659
    taskFolder: LinuxRelease
```

Run examples:

```bash
python3 script_validation_workflow_helper.py task_example.yaml -l True -p 2
python3 script_validation_workflow_helper.py task_example.yaml -p 1 2 3 4 5
```

Known supported repo names from the example:

```text
ngc_ui_test
api_catalog_ui_test
ngc_cli_test
nvcf_api_test
forge_api_test
forge_cli_test
forge_ui_test
```

If the target repo is unsupported, keep the validation plan as an artifact and
ask the tool owner to add support before attempting upload.

## Result Update Utility

`cqa_helper/update_result.py` can update DevTest task results using a
CQA-derived DevTest token and a `testInfo.config` file. Use it only after the
owner confirms the project, folder, driver version, username, and task IDs.

Never commit `testInfo.config`, access tokens, PATs, or generated logs with
private URLs.
