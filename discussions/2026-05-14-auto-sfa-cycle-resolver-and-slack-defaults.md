# 2026-05-14 - Auto SFA cycle resolver and Slack defaults

## Context

The Auto SFA integration was refreshed after reading the current
`magic-auto/INTEGRATION.md` contract and validating the manual `magic-auto`
commands.

Important CLI contracts:

- Create/template prep uses `uv run dtoperator.py update-template -u
  "<Display Name>" --folder-id <folder_id> --win-linux "<Linux Only|Windows
  Only|Both>" -f`.
- Omitting `--win-linux` remains backwards-compatible and falls back to
  `Linux Only`, but agent-me now sends the selected value explicitly for
  dashboard runs.
- Release destination lookup uses `uv run dtoperator.py
  resolve-destination-folder -s <source_folder_id> -q`; the result may change
  with the current date/cycle.
- Release execution still uses `uv run dtoperator.py sfa -u "<Display Name>"
  -f` with a per-run temp config.
- DevTest credentials are passed per call as `--username`/`--password` when
  provided. Passwords are not persisted into server config or public history.

## Dashboard Create SFA Tasks

Current behavior:

- Required inputs: Display Name and folder id.
- Optional: specific template IDs.
- Internal project id remains fixed at `1072`.
- The UI labels the display-name mapping plainly: `Automation Dev Linux =
  Display Name`.
- `Win_Linux` is shown above the specific-ID selector and has three choices:
  `Linux Only` (default), `Windows Only`, and `Both`.
- The backend command always passes the chosen `--win-linux` value, so DevTest
  reflects the user's selection instead of silently staying on `Linux Only`.
- Browser localStorage persists Display Name, DevTest credentials, and
  `Win_Linux`.

## Dashboard Release SFA Tasks

Current behavior:

- Type selector appears above source/destination:
  - `Linux Release` (default) maps source folder `50722`.
  - `Release` maps source folder `47877`.
- Changing Type updates the source folder and calls the backend resolver once
  for that selected type.
- Editing source or destination manually does not call the resolver again.
  Manual edits are respected until Type changes.
- The destination hint uses the compact path shape
  `Type/MM-YYYY/WeekA-B` plus a short note that it is auto-resolved by today's
  date and still editable.
- The resolver endpoint handles `uv` virtualenv warning lines and extracts the
  final numeric destination id from `magic-auto` output.
- Browser localStorage persists Release type and the shared Display
  Name/credential settings.

Known 2026-05-14 resolved examples:

- Source `50722` (`Linux Release`) resolved to destination `1155188`.
- Source `47877` (`Release`) resolved to destination `891171`.

## Slack Create SFA Tasks

Slack intentionally keeps Create simpler than the dashboard.

Accepted compact prompt examples:

```text
Create SFA Tasks for "Thanh Phan" in folder "422490"
Tao SFA Tasks cho Thanh Phan trong folder 422490
```

Required information:

- Display Name, for example `Thanh Phan`.
- Folder id.

Defaults:

- `Win_Linux = Linux Only`.
- Project id `1072`.
- Template mode uses the display-name filter unless specific template IDs were
  already supplied through the active thread state.

Optional override:

- Add `Win_Linux: Windows Only` or `Win_Linux: Both` to the same prompt or a
  follow-up message in the active Slack thread.

The Slack help text asks only for the two required fields first, then documents
the optional platform override in English.

## Slack Release SFA Tasks

Slack Release also uses the smallest safe contract.

Accepted compact prompt examples:

```text
Release SFA Tasks for "Thanh Phan" with URL_PATH https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123
Phat hanh SFA Tasks cho Thanh Phan voi link https://gitlab-master.nvidia.com/group/repo/-/merge_requests/123
```

Required information:

- Display Name, for example `Thanh Phan`.
- URL_PATH.

Defaults:

- Type: `Linux Release`.
- Source folder: `50722`.
- Destination folder: resolved with the backend `resolve-destination-folder`
  helper before launch.
- End date: today's date in `Asia/Ho_Chi_Minh`.
- Start date: seven days before the end date.
- Complexity: `L2`.

Optional override:

- Add `type: Release` to use source folder `47877` and resolve the Release
  destination.
- Add `type: Linux Release` to switch back to source folder `50722` and resolve
  the Linux Release destination.

Slack no longer asks for source folder, destination folder, start date, end
date, or complexity for the default Release path. If destination resolution
fails, the bot keeps the thread active and asks the user to retry or use the
dashboard.

## Implementation Notes

- Natural-language parsing lives in `agent_me.auto_sfa` so Slack and any
  future chat entrypoint share the same parser.
- Mixed natural-language plus keyed override prompts are supported, for
  example `Create SFA Tasks for "Thanh Phan" in folder "422490" Win_Linux:
  Both` and `Release SFA Tasks for "Thanh Phan" with URL_PATH <link> type:
  Release`.
- Slack's flow detector recognizes English and accent-insensitive Vietnamese
  create/release phrasing, but exact plain-text commands such as `create sfa
  tasks` and `release sfa tasks` still open the guided flow first.
- The dashboard and Slack runners continue to share the same request builders
  and command builders; defaults are applied before builder validation.
- Slack Auto SFA button instructions are English. Vietnamese user prompts are
  still accepted by the parser where supported.
- Cancel replies use the shared English string ``Auto SFA cancelled. Send
  `auto sfa` to start again.`` for both typed cancellation and the Cancel Auto
  SFA button, so the button path cannot drift back to Vietnamese copy.
- Verification completed after this update with `uv run ruff check .` and
  `uv run pytest -q`. Focused tests cover natural-language parser behavior,
  Slack release defaults, destination resolution injection, and existing
  dashboard/backend contracts.
