"""Planned emulation coverage of an interactive run (design D12, Req. 22.1-22.3, 22.8).

Pure derivation, no I/O. After the operationalization phase, each technique of the
campaign gets an emulability status:

* ``planned_emulable``: the ``op_agent`` / ``env_agent`` negotiation accepted the
  technique's environment requirements.
* ``not_emulable`` with a reason code: ``no_command`` (no candidate command was
  found), or the code of the last objection of the negotiation (``port_conflict``,
  ``image_unavailable``, ``os_unsupported``, ``requires_vm``).

``planned_coverage`` is the share of campaign techniques that are
``planned_emulable``. The result is stored in ``agent_runs.coverage``.
"""

from __future__ import annotations

from typing import Any

STATUS_PLANNED_EMULABLE = "planned_emulable"
STATUS_NOT_EMULABLE = "not_emulable"
REASON_NO_COMMAND = "no_command"
STATUS_EXECUTED_SUCCESS = "executed_success"
STATUS_EXECUTED_FAILURE = "executed_failure"
STATUS_NOT_EXECUTED = "not_executed"


def _collect_technique_ids(value: Any, found: list[str]) -> None:
    """Collect every ``technique_id`` string found in a nested flow payload."""
    if isinstance(value, dict):
        technique_id = value.get("technique_id")
        if isinstance(technique_id, str) and technique_id and technique_id not in found:
            found.append(technique_id)
        for child in value.values():
            _collect_technique_ids(child, found)
    elif isinstance(value, list):
        for child in value:
            _collect_technique_ids(child, found)


def _techniques_with_command(operationalization: dict[str, Any]) -> set[str]:
    """Technique ids that have at least one non-empty executor command."""
    abilities = (operationalization.get("abilities") or {}).get("abilities") or []
    with_command: set[str] = set()
    for ability in abilities:
        technique_id = ability.get("technique_id")
        if technique_id and any(ex.get("command") for ex in ability.get("executors") or []):
            with_command.add(technique_id)
    return with_command


def _percentage(emulable: int, total: int) -> float:
    return round(100.0 * emulable / total, 1) if total else 0.0


def _campaign_technique_ids(
    artifacts: dict[str, Any], operationalization: dict[str, Any]
) -> list[str]:
    """Campaign technique ids: the flow's techniques plus those the operationalization knows."""
    negotiation = (operationalization.get("negotiation") or {}).get("techniques") or {}
    technique_ids: list[str] = []
    _collect_technique_ids((artifacts.get("flow") or {}).get("content"), technique_ids)
    for source in (negotiation, operationalization.get("environment_requirements") or {}):
        for technique_id in source:
            if technique_id not in technique_ids:
                technique_ids.append(technique_id)
    return technique_ids


def derive_planned_coverage(artifacts: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the planned emulability per technique and the ``planned_coverage``.

    ``artifacts`` is the graph state's ``artifacts`` mapping. Returns ``None`` when
    the operationalization artifact is absent. The campaign techniques are those of
    the flow artifact plus every technique the operationalization knows about.
    """
    operationalization = artifacts.get("operationalization")
    if not isinstance(operationalization, dict):
        return None

    negotiation = (operationalization.get("negotiation") or {}).get("techniques") or {}
    with_command = _techniques_with_command(operationalization)
    technique_ids = _campaign_technique_ids(artifacts, operationalization)

    techniques: list[dict[str, Any]] = []
    for technique_id in technique_ids:
        status_entry = negotiation.get(technique_id)
        if status_entry is None:
            status, reason = STATUS_NOT_EMULABLE, REASON_NO_COMMAND
        elif status_entry.get("status") != STATUS_PLANNED_EMULABLE:
            status = STATUS_NOT_EMULABLE
            reason = status_entry.get("reason_code") or REASON_NO_COMMAND
        elif technique_id not in with_command:
            status, reason = STATUS_NOT_EMULABLE, REASON_NO_COMMAND
        else:
            status, reason = STATUS_PLANNED_EMULABLE, None
        techniques.append({"technique_id": technique_id, "status": status, "reason_code": reason})

    emulable = sum(1 for t in techniques if t["status"] == STATUS_PLANNED_EMULABLE)
    return {
        "planned_coverage": _percentage(emulable, len(techniques)),
        "planned_emulable": emulable,
        "total_techniques": len(techniques),
        "techniques": techniques,
    }


def derive_achieved_coverage(
    artifacts: dict[str, Any], report: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Derive the post-execution status per technique and the ``achieved_coverage``.

    Maps each ``ability_results`` entry of the execution report to its technique
    through the operationalization abilities (the same ``ability_id`` ->
    ``technique_id`` bridge ``measurement.py`` uses). A technique is
    ``executed_success`` when at least one of its abilities succeeded,
    ``executed_failure`` when some ability finished without success, and
    ``not_executed`` otherwise. ``achieved_coverage`` is the share of campaign
    techniques with ``executed_success``. Returns ``None`` when there is no
    operationalization artifact or no execution report.
    """
    operationalization = artifacts.get("operationalization")
    if not isinstance(operationalization, dict) or not isinstance(report, dict):
        return None
    if "ability_results" not in report:
        return None

    technique_by_ability: dict[str, str] = {}
    abilities = (operationalization.get("abilities") or {}).get("abilities") or []
    for ability in abilities:
        if ability.get("ability_id") and ability.get("technique_id"):
            technique_by_ability[ability["ability_id"]] = ability["technique_id"]

    outcome: dict[str, str] = {}
    for result in report.get("ability_results") or []:
        if not isinstance(result, dict):
            continue
        technique_id = technique_by_ability.get(result.get("ability_id"))
        if technique_id is None:
            continue
        status = result.get("status")
        if status == "success":
            outcome[technique_id] = STATUS_EXECUTED_SUCCESS
        elif status == "failure":
            outcome.setdefault(technique_id, STATUS_EXECUTED_FAILURE)

    techniques = [
        {
            "technique_id": technique_id,
            "status": outcome.get(technique_id, STATUS_NOT_EXECUTED),
        }
        for technique_id in _campaign_technique_ids(artifacts, operationalization)
    ]
    succeeded = sum(1 for t in techniques if t["status"] == STATUS_EXECUTED_SUCCESS)
    return {
        "achieved_coverage": _percentage(succeeded, len(techniques)),
        "achieved_techniques": techniques,
        "executed_success": succeeded,
    }
