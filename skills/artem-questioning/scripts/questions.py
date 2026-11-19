#!/usr/bin/env python3
"""A local, evidence-batched question queue. No model or network calls."""

import argparse
from collections import Counter
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile


EXPAND = {"issue", "unknown"}
EVIDENCED = {"pass", "issue"}
STATUSES = {"open", "pass", "issue", "unknown", "deferred"}
QUESTION_FIELDS = ("question", "decision", "check", "batch")
DEPTH = {"questions": 24, "evidenced": 12, "followups": 4, "second_cycle": 8, "contrast_checks": 1}


PACKET_LIMITS = {"standard": 3, "important": 6}
QUESTION_GUIDES = {"standard": 32, "important": 56}


def budget_view(document):
    mode = document.get("effort", "standard")
    used = len(document.get("probes", {}))
    limit = PACKET_LIMITS[mode]
    return {"mode": mode, "used": used, "limit": limit, "remaining": max(0, limit - used),
            "question_guide": QUESTION_GUIDES[mode]}


def set_effort(document, mode):
    document["effort"] = mode
    return budget_view(document)


def reserve_probe(document, args):
    request = {field: required(vars(args), field) for field in ("question_id", "if_yes", "if_no")}
    key = required(vars(args), "id")
    probes = document.setdefault("probes", {})
    if key in probes:
        if probes[key] != request:
            raise ValueError(f"{key}: existing packet has a different purpose")
        return {"granted": False, "reason": "already_reserved", **budget_view(document)}
    state = document["questions"]
    item = state[request["question_id"]]
    if not active_questions(state)[item["id"]] or unmet_requirements(state, item):
        raise ValueError(f"{key}: question is inactive or waiting for evidence")
    if normalized(request["if_yes"]) == normalized(request["if_no"]):
        raise ValueError(f"{key}: name different actions for the possible answers")
    if not budget_view(document)["remaining"]:
        return {"granted": False, "reason": "limit_reached", **budget_view(document)}
    probes[key] = request
    return {"granted": True, **budget_view(document)}


def required(row, field):
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{row.get('id', 'Unidentified row')}: {field} is required")
    return value.strip()


def normalized(value):
    return " ".join(value.casefold().replace("ё", "е").split()).rstrip("?!.")


def signature(row):
    # Identical wording in another scope, branch or decision remains useful.
    return (row.get("parent"), row["batch"], row.get("component"), row.get("area"), row.get("cycle"),
            tuple((p["hypothesis"], p["expect"], p.get("action")) for p in row.get("predictions", ())),
            tuple(row.get("requires", ())),
            *(normalized(row[field]) for field in QUESTION_FIELDS[:-1]))


def root_question(state, key):
    question = state[key]
    while "parent" in question:
        question = state[question["parent"]]
    return question


def unmet_requirements(state, question):
    return [key for key in question.get("requires", ()) if state[key]["status"] not in EVIDENCED]


def reopen_dependents(state, key):
    changed = {key}
    # Prerequisites must have been added before a dependent question.
    for other, row in state.items():
        affected = changed.intersection(row.get("requires", ()))
        if not affected:
            continue
        changed.add(other)
        if row["status"] != "open":
            row.setdefault("history", []).append(row["result"])
            row.update(status="open", result={"status": "open",
                        "answer": f"Изменились ответы в предпосылках: {', '.join(sorted(affected))}."})
        root_question(state, other).pop("conclusion", None)


def configure_map(document, rows):
    if len(rows) != 1:
        raise ValueError("map needs one object with its components")
    obj = rows[0]
    survey = {key: required(obj, key) for key in ("object", "decision_question")}
    previous = document.get("survey", {}).get("symptom_questions", [])
    symptoms = obj.get("symptom_questions", previous)
    if (not isinstance(symptoms, list) or not all(isinstance(k, str) and k.strip() == k and k for k in symptoms)
            or len(set(symptoms)) != len(symptoms) or survey["decision_question"] in symptoms):
        raise ValueError("symptom_questions must contain distinct additional primary question IDs")
    if symptoms:
        survey["symptom_questions"] = symptoms
    components = {}
    rows = obj.get("components")
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("components must be a list of objects")
    for row in rows:
        key = required(row, "id")
        group = required(row, "group")
        areas = row.get("areas")
        if key in components or group not in {"internal", "external"}:
            raise ValueError(f"{key}: component ID must be unique and group internal or external")
        if not isinstance(areas, list) or not areas or not all(isinstance(a, str) and a.strip() for a in areas):
            raise ValueError(f"{key}: component areas are required")
        components[key] = {"name": required(row, "name"), "group": group,
                           "function": required(row, "function"), "areas": [a.strip() for a in areas]}
    if {row["group"] for row in components.values()} != {"internal", "external"}:
        raise ValueError("map needs internal components and an external condition")
    for row in document["questions"].values():
        check_component(row, components)
        check_symptom_root(row, survey)
    survey["components"] = components
    document["survey"] = survey
    return {"object": survey["object"], "components": len(components)}


def check_symptom_root(question, survey):
    if question["id"] in survey.get("symptom_questions", ()):
        if question["priority"] != 0 or question["cycle"] != 1 or "parent" in question:
            raise ValueError(f"{question['id']}: symptom must be a primary first-cycle question")


def check_component(question, components):
    component = required(question, "component")
    area = required(question, "area")
    if component not in components or area not in components[component]["areas"]:
        raise ValueError(f"{question['id']}: component and area must exist in the map")


def add_questions(document, rows):
    state = document["questions"]
    survey = document.get("survey")
    existing = {signature(row): key for key, row in state.items()}
    duplicates = {}
    added = []
    first_reviewed = None
    for row in rows:
        key = required(row, "id")
        question = {"id": key, **{field: required(row, field) for field in QUESTION_FIELDS}}
        priority = row.get("priority", 1)
        if type(priority) is not int or priority not in (0, 1, 2):
            raise ValueError(f"{key}: priority must be 0, 1 or 2")
        question["priority"] = priority
        if "parent" in row:
            question["parent"] = required(row, "parent")
        if survey:
            question.update({field: required(row, field) for field in ("component", "area")})
            check_component(question, survey["components"])
            cycle = row.get("cycle", 1)
            if type(cycle) is not int or cycle not in (1, 2):
                raise ValueError(f"{key}: cycle must be 1 or 2")
            question["cycle"] = cycle
            check_symptom_root(question, survey)
        if "requires" in row:
            refs = row["requires"]
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) for ref in refs):
                raise ValueError(f"{key}: requires must be a nonempty list of IDs")
            if len(set(refs)) != len(refs) or any(ref == key or ref not in state for ref in refs):
                raise ValueError(f"{key}: prerequisites must be distinct previously added questions")
            if question.get("cycle") == 1 and any(state[ref].get("cycle") == 2 for ref in refs):
                raise ValueError(f"{key}: cycle 1 cannot depend on cycle 2")
            question["requires"] = sorted(refs)
        if "predictions" in row:
            pairs = row["predictions"]
            if not isinstance(pairs, list) or len(pairs) < 2 or not all(isinstance(pair, dict) for pair in pairs):
                raise ValueError(f"{key}: predictions must contain at least two hypotheses and observations")
            predictions = [{field: required(pair, field) for field in ("hypothesis", "expect")} for pair in pairs]
            if any(len({normalized(p[field]) for p in predictions}) != len(predictions)
                   for field in ("hypothesis", "expect")):
                raise ValueError(f"{key}: each hypothesis must forecast a distinct observation")
            if any("action" in pair for pair in pairs):
                for prediction, pair in zip(predictions, pairs):
                    prediction["action"] = required(pair, "action")
                if len({normalized(p["action"]) for p in predictions}) < 2:
                    raise ValueError(f"{key}: forecast actions must include at least two different decisions")
            question["predictions"] = predictions
        if key in state:
            recorded_fields = {"status", "result", "history", "conclusion", "draft", "drafts"}
            current = {k: v for k, v in state[key].items() if k not in recorded_fields}
            if current != question:
                raise ValueError(f"{key}: ID already holds a different question")
            continue
        parent = question.get("parent")
        if parent and (parent not in state or state[parent]["status"] not in EXPAND):
            raise ValueError(f"{key}: parent must have a recorded issue or an unresolved answer")
        identity = signature(question)
        if identity in existing:
            duplicates[key] = existing[identity]
            continue
        if question.get("cycle") == 2:
            if first_reviewed is None:
                first_reviewed = coverage_review(document)["first_reviewed"]
            if not first_reviewed:
                raise ValueError(f"{key}: complete the first survey before opening cycle 2")
        state[key] = {**question, "status": "open"}
        root_question(state, key).pop("conclusion", None)
        existing[identity] = key
        added.append(key)
    return {"added": added, "duplicates": duplicates, "total": len(state)}


def record_results(document, rows):
    state = document["questions"]
    recorded = set()
    for row in rows:
        key = required(row, "id")
        status = required(row, "status")
        if key in recorded or key not in state or status not in STATUSES:
            raise ValueError(f"{key}: duplicate result, unknown question or invalid status")
        recorded.add(key)
        item = state[key]
        result = {"status": status, "answer": required(row, "answer")}
        if status in EVIDENCED:
            if unmet := unmet_requirements(state, item):
                raise ValueError(f"{key}: unresolved prerequisites: {', '.join(unmet)}")
            if "predictions" in item:
                result["observed"] = required(row, "observed")
        for field in ("source", "scope"):
            if status in EVIDENCED or field in row:
                result[field] = required(row, field)
        if status in {"issue", "unknown", "deferred"}:
            result["next"] = required(row, "next")
        if item.get("result") == result:
            continue
        if "result" in item:
            item.setdefault("history", []).append(item["result"])
        item.update(status=status, result=result)
        root_question(state, key).pop("conclusion", None)
        reopen_dependents(state, key)
    return {"recorded": len(recorded)}


def record_drafts(document, rows):
    state = document["questions"]
    drafts = set()
    for row in rows:
        key = required(row, "id")
        kind = required(row, "kind")
        model = required(row, "model") if "model" in row else "gpt-6-luna"
        evidence = row.get("evidence", [])
        if (key, model) in drafts or key not in state or kind not in {"supported", "hypothesis", "unknown"}:
            raise ValueError(f"{key}: duplicate draft, unknown question or invalid kind")
        if model not in {"gpt-6-luna", "gpt-6.1-terra"}:
            raise ValueError(f"{key}: unknown helper model")
        if not isinstance(evidence, list) or not all(isinstance(ref, str) and ref.strip() for ref in evidence):
            raise ValueError(f"{key}: evidence must be a list of fact IDs")
        if kind == "supported" and not evidence:
            raise ValueError(f"{key}: supported draft needs fact IDs")
        draft = {"kind": kind, "packet": required(row, "packet"),
                 "answer": required(row, "answer"), "evidence": evidence}
        if "model" in row:
            draft["model"] = model
        if "counterexample" in row:
            counter = row["counterexample"]
            if not isinstance(counter, dict):
                raise ValueError(f"{key}: counterexample must describe an observation and its fact IDs")
            refs = counter.get("evidence")
            if not isinstance(refs, list) or not refs or not all(isinstance(ref, str) and ref.strip() for ref in refs):
                raise ValueError(f"{key}: counterexample needs supporting fact IDs")
            draft["counterexample"] = {"observation": required(counter, "observation"), "evidence": refs}
        if kind != "supported" or "next" in row:
            draft["next"] = required(row, "next")
        item = state[key]
        previous = item.get("draft", {})
        previous_model = previous.get("model", "gpt-6-luna")
        if "drafts" in item or (previous and previous_model != model):
            saved = item.setdefault("drafts", {})
            if previous:
                saved[previous_model] = previous
            saved[model] = draft
        item["draft"] = draft
        drafts.add((key, model))
    return {"drafts_recorded": len(drafts)}


def active_questions(state):
    # Parents always precede their children in insertion order.
    active = {}
    for key, row in state.items():
        parent = row.get("parent")
        active[key] = parent is None or (active[parent] and state[parent]["status"] in EXPAND)
    return active


def make_plan(state, limit, size, batch=None, offset=0):
    active = active_questions(state)
    pending = [row for key, row in state.items() if active[key] and row["status"] == "open"]
    blocked = {row["id"]: unmet for row in pending if (unmet := unmet_requirements(state, row))}
    ready = [row for row in pending if row["id"] not in blocked]
    first_pass = [row for row in ready if row["priority"] == 0 and "parent" not in row]
    groups = {}
    def order(row):
        changes_action = any("action" in p for p in row.get("predictions", ()))
        return (row["priority"], not changes_action)

    for row in sorted(first_pass or ready, key=order):
        groups.setdefault(row["batch"], []).append(row)
    selected = [name for name in groups if batch is None or name == batch][:limit]
    parents = {row["parent"] for row in state.values() if "parent" in row}
    unexpanded = [row for key, row in state.items()
                  if active[key] and key not in parents and row["priority"] < 2 and row["status"] in EXPAND]
    unexpanded.sort(key=lambda row: row["priority"])
    return {
        "phase": "initial" if first_pass else "follow_up",
        "statuses": dict(Counter(row["status"] for row in state.values())),
        "ready_batches": len(groups),
        "blocked_open": sum(row["status"] == "open" and not active[key] for key, row in state.items()),
        "blocked_by_requirements": len(blocked),
        "waiting_for": [{"id": key, "requires": blocked[key]} for key in list(blocked)[:limit]],
        "unexpanded_count": len(unexpanded),
        "unexpanded": [
            {"id": row["id"], "question": row["question"], "status": row["status"], "next": row["result"]["next"]}
            for row in unexpanded[:limit]
        ],
        "batches": [
            {"id": name, "count": len(groups[name]), "offset": offset,
             "questions": [{k: row[k] for k in ("id", "question", "decision", "check", "priority", "predictions") if k in row}
                           for row in groups[name][offset:offset + size]]}
            for name in selected
        ],
    }


def plan_response(plan, since):
    content = json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
    if since == fingerprint:
        return {"unchanged": True, "fingerprint": fingerprint}
    return {**plan, "fingerprint": fingerprint}


def decision_ids(survey):
    if not survey:
        return ()
    return (survey["decision_question"], *survey.get("symptom_questions", ()))


def decision_basis(document, conclusion, decision_id):
    """Inspect cited evidence, including when opening a queue saved by an older version."""
    if not conclusion:
        return "missing_conclusion"
    if conclusion["outcome"] == "limited":
        return "limited"
    state = document["questions"]
    active = active_questions(state)
    for key in conclusion["evidence"]:
        row = state.get(key)
        if (row and active[key] and root_question(state, key)["id"] == decision_id
                and row["status"] in EVIDENCED and row.get("predictions") and row["result"].get("observed")):
            return "supported"
    return "missing_decision_contrast"


def record_conclusions(document, rows):
    state = document["questions"]
    active = active_questions(state)
    for row in rows:
        key = required(row, "id")
        item = state[key]
        if item["priority"] != 0 or "parent" in item or item["status"] == "open":
            raise ValueError(f"{key}: conclude an answered primary question")
        outcome = required(row, "outcome")
        evidence = row.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
            raise ValueError(f"{key}: evidence must be a list of question IDs")
        for supporting_id in evidence:
            supporting = state[supporting_id]
            if (root_question(state, supporting_id)["id"] != key or not active[supporting_id]
                    or supporting["status"] not in {"pass", "issue"}):
                raise ValueError(f"{key}: {supporting_id} must be an evidenced result in this branch")
        conclusion = {"outcome": outcome, "decision": required(row, "decision"), "evidence": evidence}
        if outcome == "resolved":
            if item["status"] not in {"pass", "issue"} or not evidence:
                raise ValueError(f"{key}: resolved needs an answered primary question and supporting evidence")
        elif outcome == "limited":
            conclusion.update({field: required(row, field) for field in ("missing", "limit")})
        else:
            raise ValueError(f"{key}: outcome must be resolved or limited")
        if key in decision_ids(document.get("survey")) and decision_basis(document, conclusion, key) == "missing_decision_contrast":
            raise ValueError(f"{key}: cite a completed contrast check in this decision branch, or record a limited conclusion")
        item["conclusion"] = conclusion
    return {"conclusions_recorded": len(rows)}


def coverage_review(document):
    survey = document["survey"]
    state = document["questions"]
    first = [row for row in state.values() if row["cycle"] == 1]
    reviewed = [row for row in first if row["status"] != "open"]
    evidenced = [row for row in reviewed if row["status"] in EVIDENCED]
    contrast_checks = sum(row["status"] in EVIDENCED and "predictions" in row and "observed" in row.get("result", {})
                          for row in state.values())
    missing = sorted(set(survey["components"]) - {row["component"] for row in reviewed})
    followups = sum("parent" in row for row in reviewed)
    first_done = (len(reviewed) == len(first) and len(first) >= DEPTH["questions"]
                  and followups >= DEPTH["followups"] and not missing)
    target = state.get(survey["decision_question"], {})
    target_ok = target.get("priority") == 0 and "parent" not in target and target.get("cycle") == 1
    bases = {}
    for key in decision_ids(survey):
        item = state.get(key, {})
        if item.get("priority") == 0 and "parent" not in item and item.get("cycle") == 1:
            bases[key] = decision_basis(document, item.get("conclusion"), key)
        else:
            bases[key] = "missing_primary_question"
    basis = bases[survey["decision_question"]]
    resolved = all(value == "supported" for value in bases.values())
    second = [row for row in state.values() if row["cycle"] == 2]
    second_required = first_done and (not resolved or bool(second))
    second_done = (len(second) >= DEPTH["second_cycle"] and all(row["status"] != "open" for row in second))
    return {
        "thresholds": DEPTH,
        "first": {"questions": len(first), "reviewed": len(reviewed), "evidenced": len(evidenced),
                  "followups": followups, "missing_components": missing},
        "first_reviewed": first_done,
        "decision_question_ready": target_ok,
        "decision_basis": basis,
        **({"symptom_bases": {key: bases[key] for key in survey["symptom_questions"]}} if survey.get("symptom_questions") else {}),
        "contrast_checks": contrast_checks,
        "second_required": second_required,
        "second": {"questions": len(second), "reviewed": sum(row["status"] != "open" for row in second)},
        "coverage_ready": (first_done and all(value in {"supported", "limited"} for value in bases.values())
                           and len(evidenced) >= DEPTH["evidenced"]
                           and contrast_checks >= DEPTH["contrast_checks"]
                           and (not second_required or second_done)),
    }


def final_review(document, deep=False):
    state = document["questions"]
    active = active_questions(state)
    pending = Counter(root_question(state, key)["id"] for key, row in state.items()
                      if active[key] and row["priority"] < 2 and row["status"] == "open")
    branches = []
    for key, row in state.items():
        if row["priority"] != 0 or "parent" in row:
            continue
        conclusion = row.get("conclusion")
        if pending[key]:
            status = "continue"
        elif key in decision_ids(document.get("survey")) and decision_basis(document, conclusion, key) not in {"supported", "limited"}:
            status = "decide"
        elif conclusion:
            status = conclusion["outcome"]
        else:
            status = "decide" if row["status"] != "pass" else "passed"
        branch = {"id": key, "question": row["question"], "review": status}
        if pending[key]:
            branch["pending_checks"] = pending[key]
        if conclusion:
            branch["conclusion"] = conclusion
        elif row["status"] in EXPAND:
            branch["latest_finding"] = row["result"]["answer"]
            branch["next"] = row["result"]["next"]
        branches.append(branch)
    ready = bool(branches) and all(b["review"] in {"resolved", "limited", "passed"} for b in branches)
    result = {"summary_ready": ready, "branches": branches}
    if "survey" in document:
        result["coverage"] = coverage_review(document)
        result["summary_ready"] = ready and result["coverage"]["coverage_ready"]
    elif deep:
        result.update(summary_ready=False, coverage={"coverage_ready": False, "missing": "object map"})
    return result


def markdown_report(document):
    survey = document.get("survey", {})
    components = survey.get("components", {})
    lines = ["# " + survey.get("object", "Вопросы и ответы")]
    decision_id = survey.get("decision_question")
    for key in decision_ids(survey):
        if key not in document["questions"]:
            lines.extend(["", f"Открытый вопрос {key} из карты пока не поставлен."])
    for key, row in document["questions"].items():
        conclusion = row.get("conclusion")
        if key not in decision_ids(survey) and not conclusion:
            continue
        lines.extend(["", "## " + ("Главное решение" if key == decision_id else "Вывод по ветке") + f" · {key}",
                      "", row["question"]])
        if not conclusion:
            lines.append("Вывод открыт.")
            continue
        if key in decision_ids(survey) and decision_basis(document, conclusion, key) == "missing_decision_contrast":
            lines.append("Прежний вывод требует проверки, которая различает версии в этой ветке.")
        lines.append(conclusion["decision"])
        if conclusion["evidence"]:
            lines.append("Основания — " + ", ".join(conclusion["evidence"]))
        if conclusion["outcome"] == "limited":
            lines.extend(["Предел проверки — " + conclusion["limit"], "Нужный факт — " + conclusion["missing"]])
    groups = {}
    for row in document["questions"].values():
        layer = (row.get("cycle", 1), row.get("component"))
        groups.setdefault(layer, []).append(row)
    statuses = {"open": "Открыт", "pass": "Проверено", "issue": "Обнаружен сбой",
                "unknown": "Нет подтверждённого ответа", "deferred": "Отложен"}
    draft_labels = {"supported": "опирается на пакет", "hypothesis": "гипотеза", "unknown": "недостаточно данных"}
    for (cycle, component), rows in sorted(groups.items(), key=lambda item: item[0][0]):
        heading = components.get(component, {}).get("name", "Вопросы")
        lines += ["", f"## Цикл {cycle}. {heading}"]
        for row in rows:
            lines += ["", f"### {row['id']}. {row['question']}", "", f"- {statuses[row['status']]}."]
            if "parent" in row:
                lines.append(f"- Продолжает {row['parent']}.")
            if "requires" in row:
                lines.append(f"- Предпосылки — {', '.join(row['requires'])}.")
            for prediction in row.get("predictions", ()):
                lines.append(f"- **Версия** — {prediction['hypothesis']}. Ожидаемое наблюдение — {prediction['expect']}")
                if "action" in prediction:
                    lines.append(f"  При таком результате — {prediction['action']}")
            drafts = row.get("drafts", {}).values()
            if not drafts and "draft" in row:
                drafts = [row["draft"]]
            for draft in drafts:
                model = "Terra" if draft.get("model") == "gpt-6.1-terra" else "Luna"
                lines.append(f"- **Черновик {model} ({draft_labels[draft['kind']]}, пакет {draft['packet']})** — {draft['answer']}")
                if draft["evidence"]:
                    lines.append(f"- Факты пакета — {', '.join(draft['evidence'])}.")
                if "next" in draft:
                    lines.append(f"- Уточняющая проверка — {draft['next']}")
                if counter := draft.get("counterexample"):
                    lines.append(f"- Контрпример — {counter['observation']} ({', '.join(counter['evidence'])}).")
            for field, label in (("answer", "Ответ"), ("source", "Источник"),
                                 ("scope", "Условия"), ("observed", "Наблюдение по прогнозу"),
                                 ("next", "Следующий шаг")):
                if field in row.get("result", {}):
                    lines.append(f"- **{label}** — {row['result'][field]}")
    return "\n".join(lines)


def read_state(path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"questions": {}}


def read_rows(path):
    raw = sys.stdin.read() if path == "-" else Path(path).read_text(encoding="utf-8")
    rows = json.loads(raw)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Input must be a JSON array of objects")
    return rows


def mutate(path, operation, rows):
    with path.with_suffix(path.suffix + ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state = read_state(path)
        result = operation(state, rows)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".questions-", delete=False) as target:
            temporary = Path(target.name)
            try:
                json.dump(state, target, ensure_ascii=False, indent=2)
                target.write("\n")
                target.flush()
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state", type=Path, help="State JSON in an existing task directory")
    commands = parser.add_subparsers(dest="action", required=True)
    mutations = {"add": add_questions, "record": record_results, "conclude": record_conclusions,
                 "map": configure_map, "draft": record_drafts}
    for name in mutations:
        commands.add_parser(name).add_argument("input", help="JSON array file or - for stdin")
    commands.add_parser("effort", help="Set the packet allowance without resetting usage").add_argument("mode", choices=PACKET_LIMITS)
    probe = commands.add_parser("probe", help="Reserve one external evidence packet before starting it")
    probe.add_argument("id", help="Stable ID for this source, scope and check")
    probe.add_argument("question_id", help="Question this evidence could help answer")
    probe.add_argument("--if-yes", required=True, help="Action if the check confirms the hypothesis")
    probe.add_argument("--if-no", required=True, help="Action if the check contradicts the hypothesis")
    plan = commands.add_parser("plan")
    plan.add_argument("--limit", type=int, default=4, help="Number of evidence batches")
    plan.add_argument("--size", type=int, default=6, help="Number of questions shown per batch")
    plan.add_argument("--batch", help="Show only this evidence batch")
    plan.add_argument("--offset", type=int, default=0, help="Offset into each shown batch")
    plan.add_argument("--since", help="Fingerprint of the same view already available in context")
    commands.add_parser("show").add_argument("ids", nargs="+")
    commands.add_parser("review").add_argument("--deep", action="store_true", help="Require an object map and full survey")
    commands.add_parser("report", help="Show the questions and their evidence as Markdown")
    args = parser.parse_args()
    try:
        if args.action == "effort":
            result = mutate(args.state, set_effort, args.mode)
        elif args.action == "probe":
            result = mutate(args.state, reserve_probe, args)
        elif args.action in mutations:
            result = mutate(args.state, mutations[args.action], read_rows(args.input))
        else:
            if not args.state.is_file():
                raise ValueError(f"State not found: {args.state}")
            document = read_state(args.state)
            if args.action == "show":
                result = {key: document["questions"][key] for key in args.ids}
            elif args.action == "review":
                result = final_review(document, args.deep)
            elif args.action == "report":
                print(markdown_report(document))
                return 0
            else:
                if args.limit < 1 or args.size < 1 or args.offset < 0:
                    raise ValueError("limit and size must be positive, offset must be nonnegative")
                result = make_plan(document["questions"], args.limit, args.size, args.batch, args.offset)
                if "survey" in document:
                    result["coverage"] = coverage_review(document)
                result["budget"] = budget_view(document)
                result = plan_response(result, args.since)
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"questions: {exc}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if args.action == "probe" and not result["granted"]:
        return 4
    if args.action == "review" and not result["summary_ready"]:
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
