from __future__ import annotations

import json
from pathlib import Path

from agent_me import parallel_queue


def test_enqueue_plan_claim_and_aggregate(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(parallel_queue, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(parallel_queue, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(parallel_queue, "HISTORY_PATH", tmp_path / "history.jsonl")

    item_id = parallel_queue.enqueue_item(
        "Draft Slack follow-up",
        "Write a response but do not send it.",
        priority="HIGH",
        task_type="slack-response",
        project="agent-me",
    )

    spec = parallel_queue.FilterSpec(view="today", priorities=["HIGH"])
    plan = parallel_queue.manifest(spec, run_id="run-test")
    assert plan["count"] == 1
    assert plan["items"][0]["dispatch_class"] == "DRAFT"

    claimed = parallel_queue.claim_items(spec, run_id="run-test")
    assert claimed["count"] == 1
    assert claimed["items"][0]["status"] == "IN_PROGRESS"

    summary = parallel_queue.aggregate_returns(
        [
            {
                "item_id": item_id,
                "status": "DONE",
                "classification_actual": "DRAFT",
                "artifacts": ["output/parallel-runs/run-test/artifacts-by-item/draft.md"],
                "drafts": [
                    {
                        "channel": "slack",
                        "path": "output/parallel-runs/run-test/artifacts-by-item/draft.md",
                        "target": "D123",
                    }
                ],
            }
        ],
        run_id="run-test",
    )

    assert summary["completed"][0]["item_id"] == item_id
    assert summary["drafts"][0]["target"] == "D123"
    queue = json.loads((tmp_path / "queue.json").read_text())
    assert queue["items"][0]["status"] == "DONE"


def test_dispatch_hints_cover_guardrails() -> None:
    assert parallel_queue.suggest_dispatch_class({"status": "PENDING_REVIEW"}) == "HUMAN"
    assert parallel_queue.suggest_dispatch_class({"status": "QUEUED", "waiting_on": "token"}) == "BLOCKED"
    assert parallel_queue.suggest_dispatch_class({"status": "QUEUED", "task_type": "email-send"}) == "DRAFT"
    assert parallel_queue.suggest_dispatch_class({"status": "QUEUED", "task_type": "browser"}) == "PLAYWRIGHT"
    assert parallel_queue.suggest_dispatch_class({"status": "QUEUED", "skill": "refresh-brief"}) == "AUTO"
    assert parallel_queue.suggest_dispatch_class({"status": "QUEUED", "task_type": "research"}) == "PREPARE"


def test_cli_add_and_plan(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(parallel_queue, "QUEUE_DIR", tmp_path)
    monkeypatch.setattr(parallel_queue, "QUEUE_PATH", tmp_path / "queue.json")
    monkeypatch.setattr(parallel_queue, "HISTORY_PATH", tmp_path / "history.jsonl")

    assert parallel_queue.main([
        "add",
        "--title",
        "Prepare report",
        "--description",
        "Collect evidence",
        "--priority",
        "CRITICAL",
        "--task-type",
        "research",
    ]) == 0
    capsys.readouterr()

    assert parallel_queue.main(["plan", "--view", "today", "--priority", "CRITICAL"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["items"][0]["title"] == "Prepare report"
