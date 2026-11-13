"""Solution-family portfolio state for Sapper graphs.

Family summaries are explicit CLI data and are not injected into routine prompts.
All writes are expected to run while the Store's graph lock is held.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path

FAMILY_STATES = {"probing", "promising", "scaling", "parked", "solved", "ruled_out", "need_human"}
TERMINAL_STATES = {"parked", "solved", "ruled_out", "need_human"}
KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _path(store, key: str) -> Path:
    if not isinstance(key, str) or not KEY_RE.fullmatch(key):
        raise ValueError("family key must match [a-z][a-z0-9_-]{0,31}")
    return store.root / "families" / f"{key}.json"


def get(store, key: str) -> dict:
    path = _path(store, key)
    if not path.is_file():
        raise ValueError(f"family {key} not found")
    with path.open("r", encoding="utf-8") as stream:
        return json.load(stream)


def list_all(store) -> list[dict]:
    directory = store.root / "families"
    if not directory.is_dir():
        return []
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.json"))]


def _save(store, family: dict) -> dict:
    family["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = store.root / "families" / f"{family['id']}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(family, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        try:
            os.unlink(name)
        except FileNotFoundError:
            pass
    return family


def create(store, key: str, label: str, hypothesis: str, metric_name: str,
           budget_attempts: int, budget_usd: float, stall_limit: int,
           direction: str, actor: str) -> dict:
    path = _path(store, key)
    if not all(isinstance(value, str) and value.strip()
               for value in (label, hypothesis, metric_name)):
        raise ValueError("family label, hypothesis and metric name are required")
    if isinstance(budget_attempts, bool) or not isinstance(budget_attempts, int) or budget_attempts < 1:
        raise ValueError("family attempt budget must be a positive integer")
    if isinstance(budget_usd, bool) or budget_usd <= 0:
        raise ValueError("family USD budget must be positive")
    if isinstance(stall_limit, bool) or not isinstance(stall_limit, int) or stall_limit < 1:
        raise ValueError("family stall limit must be a positive integer")
    if direction not in {"higher", "lower"}:
        raise ValueError("metric direction must be higher or lower")
    if path.exists():
        raise ValueError(f"family {key} already exists")
    now = _now()
    family = {"id": key, "label": label.strip(), "hypothesis": hypothesis.strip(),
              "metric_name": metric_name.strip(), "metric_direction": direction,
              "metric": None, "metric_fact": None, "metric_history": [],
              "attempts": [], "spend_usd": 0.0, "spend_history": [],
              "budget_attempts": budget_attempts, "budget_usd": float(budget_usd),
              "stall_limit": stall_limit, "no_progress": 0, "state": "probing",
              "stop_reason": None, "review_due": False, "decisions": [],
              "creator": actor, "created_at": now, "updated_at": now}
    return _save(store, family)


def reserve_intent(store, family_key: str | None, intent_id: str) -> None:
    if family_key is None:
        return
    family = get(store, family_key)
    if family["state"] in TERMINAL_STATES:
        raise ValueError(f"family {family_key} is {family['state']}: {family.get('stop_reason') or 'no work allowed'}")
    attempts = family["attempts"]
    active = sum(row["state"] in {"open", "claimed"} for row in attempts)
    if active >= 2:
        raise ValueError(f"family {family_key} active-intent limit reached (2)")
    if len(attempts) >= family["budget_attempts"]:
        family.update(state="parked", stop_reason="attempt budget exhausted", review_due=True)
        _save(store, family)
        raise ValueError(f"family {family_key} attempt budget exhausted; parked")
    if family["spend_usd"] >= family["budget_usd"]:
        family.update(state="parked", stop_reason="USD budget exhausted", review_due=True)
        _save(store, family)
        raise ValueError(f"family {family_key} USD budget exhausted; parked")
    attempts.append({"intent_id": intent_id, "state": "open", "created_at": _now(),
                     "result_fact": None, "concluded_at": None})
    _save(store, family)


def validate_fact_result(store, intent: dict, fact: dict) -> None:
    key = intent.get("family")
    if not key:
        return
    family = get(store, key)
    data = fact.get("data") or {}
    raw = data.get("metric")
    if raw is None:
        return
    if data.get("metric_name") != family["metric_name"]:
        raise ValueError(f"family metric must be named {family['metric_name']!r}")
    try:
        float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("family fact data.metric must be numeric") from exc


def transition_intent(store, intent: dict, state: str, *, fact: dict | None = None,
                      actor: str | None = None) -> None:
    key = intent.get("family")
    if not key:
        return
    family = get(store, key)
    attempt = next((item for item in family["attempts"] if item["intent_id"] == intent["id"]), None)
    if attempt is None or attempt["state"] not in {"open", "claimed"}:
        return
    if state in {"open", "claimed"}:
        attempt["state"] = state
        attempt["worker"] = intent.get("worker")
        _save(store, family)
        return
    attempt.update(state=state, result_fact=fact.get("id") if fact else None,
                   concluded_at=_now(), worker=actor or intent.get("worker"))
    improved = False
    if fact and fact.get("evidence"):
        fact_data = fact.get("data") or {}
        raw = fact_data.get("metric")
        if raw is not None and fact_data.get("metric_name") != family["metric_name"]:
            raise ValueError(f"family metric must be named {family['metric_name']!r}")
        if raw is not None:
            try:
                value = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError("family fact data.metric must be numeric") from exc
            previous = family["metric"]
            improved = previous is None or (value > previous if family["metric_direction"] == "higher" else value < previous)
            family["metric_history"].append({"fact_id": fact["id"], "value": value,
                "improved": improved, "actor": actor or intent.get("worker"), "at": _now()})
            if improved:
                family.update(metric=value, metric_fact=fact["id"], no_progress=0)
                if family["state"] == "probing":
                    family["state"] = "promising"
                elif family["state"] == "promising":
                    family["state"] = "scaling"
    if not improved and state == "done":
        family["no_progress"] += 1
    if family["no_progress"] >= family["stall_limit"]:
        family.update(state="parked", stop_reason="stall limit reached without verified metric improvement", review_due=True)
    if len(family["attempts"]) >= family["budget_attempts"] and not any(
            row["state"] in {"open", "claimed"} for row in family["attempts"]):
        if family["state"] not in {"solved", "ruled_out", "need_human"}:
            family.update(state="parked", stop_reason="attempt budget exhausted", review_due=True)
    _save(store, family)


def add_spend(store, key: str, amount: float, note: str, actor: str) -> dict:
    if isinstance(amount, bool) or amount <= 0 or not note.strip():
        raise ValueError("spend amount must be positive and rationale non-empty")
    family = get(store, key)
    if family["state"] in {"solved", "ruled_out", "need_human"}:
        raise ValueError(f"family {key} is terminal: {family['state']}")
    family["spend_usd"] = round(family["spend_usd"] + amount, 6)
    family["spend_history"].append({"amount_usd": amount, "note": note.strip(),
                                    "actor": actor, "at": _now()})
    if family["spend_usd"] >= family["budget_usd"]:
        family.update(state="parked", stop_reason="USD budget exhausted", review_due=True)
    return _save(store, family)


def set_metric(store, key: str, fact_id: str, actor: str) -> dict:
    family = get(store, key)
    fact = store.get_fact(fact_id)
    if not fact.get("evidence"):
        raise ValueError("metric fact must have evidence provenance")
    fact_data = fact.get("data") or {}
    raw = fact_data.get("metric")
    if raw is None:
        raise ValueError("metric fact needs data.metric=<number>")
    if fact_data.get("metric_name") != family["metric_name"]:
        raise ValueError(f"metric fact must include data.metric_name={family['metric_name']}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("fact data.metric must be numeric") from exc
    if any(row["fact_id"] == fact_id for row in family["metric_history"]):
        raise ValueError(f"fact {fact_id} already counted for family {key}")
    old = family["metric"]
    improved = old is None or (value > old if family["metric_direction"] == "higher" else value < old)
    family["metric_history"].append({"fact_id": fact_id, "value": value, "improved": improved,
                                     "actor": actor, "at": _now()})
    if improved:
        family.update(metric=value, metric_fact=fact_id, no_progress=0)
        if family["state"] == "probing":
            family["state"] = "promising"
        elif family["state"] == "promising":
            family["state"] = "scaling"
    else:
        family["no_progress"] += 1
    if family["no_progress"] >= family["stall_limit"]:
        family.update(state="parked", stop_reason="stall limit reached without verified metric improvement", review_due=True)
    return _save(store, family)


def set_budget(store, key: str, attempts: int, usd: float, rationale: str, actor: str) -> dict:
    family = get(store, key)
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < len(family["attempts"]):
        raise ValueError("attempt budget must be an integer no lower than attempts already reserved")
    if isinstance(usd, bool) or usd <= family["spend_usd"]:
        raise ValueError("USD budget must be greater than recorded spend")
    if not rationale.strip():
        raise ValueError("budget change requires a rationale")
    family["budget_attempts"] = attempts
    family["budget_usd"] = float(usd)
    family["decisions"].append({"state": family["state"], "rationale": "budget changed: " + rationale.strip(),
                                "actor": actor, "at": _now()})
    return _save(store, family)


def decide(store, key: str, state: str, rationale: str, actor: str) -> dict:
    if state not in FAMILY_STATES or not rationale.strip():
        raise ValueError("valid family state and rationale are required")
    family = get(store, key)
    if family["state"] in {"solved", "ruled_out"} and state != family["state"]:
        raise ValueError(f"terminal family {family['state']} cannot be reopened")
    if family["state"] == "parked" and state in {"probing", "promising", "scaling"}:
        family["no_progress"] = 0
    family.update(state=state,
                  stop_reason=rationale.strip() if state in TERMINAL_STATES else None,
                  review_due=False)
    family["decisions"].append({"state": state, "rationale": rationale.strip(),
                                "actor": actor, "at": _now()})
    return _save(store, family)
