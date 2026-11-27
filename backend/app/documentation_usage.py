from .config import settings
from .dispatcher import STAGE_FILES, StageFailure, read_verdict
from .pricing import estimate
from .usage import FIELDS


def documentation_usage(conn, task, runs):
    documents = [run for run in runs if run["stage"] == 1]
    if not documents:
        return None

    launches_by_run = {}
    for row in conn.execute(
        "SELECT l.* FROM documentation_launches l JOIN stage_runs r ON r.id=l.run_id "
        "WHERE r.task_id=? ORDER BY l.id",
        (task["id"],),
    ):
        launch = dict(row)
        launch["cost_usd"] = estimate(conn, launch)
        launches_by_run.setdefault(launch["run_id"], []).append(launch)

    attempts = []
    for run in documents:
        launches = launches_by_run.get(run["id"])
        if not launches:
            launches = [{
                "id": run["id"], "kind": "legacy" if run["cli_used"] else "unrecorded",
                "cli": run["cli_used"], "model": run["model_used"],
                "is_fallback": run["is_fallback"], "status": run["status"],
                "usage_partial": True, "cost_usd": None, "cache_write_tokens": 0,
                **{key: run[key] for key in FIELDS},
            }]
            if not run["is_fallback"] and not run["usage_partial"]:
                launches[0]["cost_usd"] = estimate(conn, {**launches[0], "model_mixed": False})
        attempts.append({"attempt": run["attempt"], "error": run["error"], "launches": launches})

    directory = settings.storage_dir / "tasks" / str(task["id"])
    returns = 0
    review_history_partial = False
    implementation_reviews = []
    accepted = task.get("accepted_document")
    accepted_at = task.get("document_accepted_at")
    preceding_document = None
    legacy_approval = None
    for run in runs:
        if run["status"] != "completed":
            continue
        stage, attempt = run["stage"], run["attempt"]
        if stage == 1:
            preceding_document = STAGE_FILES[1].format(attempt=attempt)
        elif stage in (2, 4):
            artifact = STAGE_FILES[stage].format(attempt=attempt)
            try:
                verdict = read_verdict(directory / artifact, document=stage == 2)
            except StageFailure:
                if stage == 2:
                    review_history_partial = True
                continue
            if stage == 2:
                if verdict["status"] == "REJECTED":
                    returns += 1
                else:
                    legacy_approval = run["reviewed_document"] or preceding_document, run["finished_at"]
            elif verdict.get("comments") or verdict.get("notes"):
                implementation_reviews.append({
                    "attempt": attempt, "artifact": artifact, "status": verdict["status"],
                    "comments": verdict.get("comments", []), "notes": verdict.get("notes", []),
                    "started_at": run["started_at"],
                })

    if not accepted_at:
        if legacy_approval:
            accepted, accepted_at = legacy_approval
        elif task["current_stage"] >= 3:
            # Older manual approvals have no review verdict or document pointer.
            first_implementation = next((run for run in runs if run["stage"] in (3, 4)), None)
            eligible = [run for run in documents if run["status"] == "completed" and (
                not first_implementation or run["id"] < first_implementation["id"]
            )]
            if eligible:
                accepted = STAGE_FILES[1].format(attempt=eligible[-1]["attempt"])
                accepted_at = eligible[-1]["finished_at"]

    accepted_attempt = next((run["attempt"] for run in documents if
                             STAGE_FILES[1].format(attempt=run["attempt"]) == accepted), None)
    covered = [launch for item in attempts if accepted_attempt is None or item["attempt"] <= accepted_attempt
               for launch in item["launches"]]
    known_costs = [launch["cost_usd"] for launch in covered if launch["cost_usd"] is not None]
    return {
        "attempts": attempts, "accepted_document": accepted, "accepted_attempt": accepted_attempt,
        "cost_usd": sum(known_costs) if known_costs else None,
        "cost_partial": (bool(accepted and accepted_attempt is None)
                         or any(launch["usage_partial"] or launch["cost_usd"] is None for launch in covered)),
        "doc_returns": returns, "review_history_partial": review_history_partial,
        "implementation_reviews": [review for review in implementation_reviews
                                   if not accepted_at or review["started_at"] >= accepted_at],
    }
