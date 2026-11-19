import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "skills/artem-questioning/scripts/questions.py"


def question(key, batch, priority=1, **extra):
    return {"id": key, "batch": batch, "priority": priority,
            "question": f"Проверить условие {key}?", "check": "Сверить строки выгрузки",
            "decision": "Уточнить исправление", **extra}


class QuestioningQueueTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.state = Path(folder.name) / "questions.json"

    def command(self, *args, rows=None, ok=True, unfinished=False):
        if rows is not None:
            args = (*args, "-")
        proc = subprocess.run([sys.executable, str(SCRIPT), str(self.state), *args],
                              input=None if rows is None else json.dumps(rows),
                              text=True, capture_output=True, timeout=5)
        if unfinished:
            self.assertEqual(proc.returncode, 3, proc.stderr)
            return json.loads(proc.stdout)
        if ok:
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        return proc.stderr

    def test_batches_keep_full_queue_on_disk_and_resume(self):
        rows = [question(f"Q{i}", f"source-{i % 4}", priority=0 if i < 4 else 1)
                for i in range(120)]
        repeated = dict(rows[0], id="copy", question="  ПРОВЕРИТЬ условие Q0! ")
        other_scope = dict(rows[0], id="other-scope", batch="Source-0")
        added = self.command("add", rows=[*rows, repeated, other_scope])
        self.assertEqual(added["total"], 121)
        self.assertEqual(added["duplicates"], {"copy": "Q0"})
        first = self.command("plan")
        self.assertEqual(first["phase"], "initial")
        self.assertEqual(first["ready_batches"], 5)
        self.assertEqual(sum(group["count"] for group in first["batches"]), 4)
        self.command("record", rows=[
            {"id": row["id"], "status": "pass", "answer": "Совпадает",
             "source": "work/report.json", "scope": row["batch"]}
            for row in [*rows[:4], other_scope]
        ])
        follow_up = self.command("plan", "--limit", "2", "--size", "3")
        self.assertEqual(follow_up["statuses"], {"pass": 5, "open": 116})
        self.assertEqual(follow_up["ready_batches"], 4)
        self.assertEqual([item["count"] for item in follow_up["batches"]], [29, 29])
        self.assertEqual([len(item["questions"]) for item in follow_up["batches"]], [3, 3])
        self.assertEqual(self.command("plan", "--batch", "source-0", "--offset", "1")
                         ["batches"][0]["questions"][0]["id"], "Q8")

    def test_matching_plan_view_returns_only_a_fingerprint(self):
        self.command("add", rows=[question("Q1", "front", priority=0),
                                  question("Q2", "other", priority=0)])
        first = self.command("plan")
        fingerprint = first["fingerprint"]
        self.assertEqual(self.command("plan", "--since", fingerprint),
                         {"unchanged": True, "fingerprint": fingerprint})

        narrower = self.command("plan", "--limit", "1", "--since", fingerprint)
        self.assertNotEqual(narrower["fingerprint"], fingerprint)
        self.assertEqual(len(narrower["batches"]), 1)

        self.command("record", rows=[{"id": "Q1", "status": "pass", "answer": "Совпадает",
                                      "source": "report.json", "scope": "front"}])
        updated = self.command("plan", "--since", fingerprint)
        self.assertNotEqual(updated["fingerprint"], fingerprint)
        self.assertEqual(updated["statuses"], {"pass": 1, "open": 1})
        self.assertEqual(updated["batches"][0]["questions"][0]["id"], "Q2")
        self.assertEqual(updated, self.command("plan"))

    def test_followups_wait_for_an_answer_and_stop_when_the_branch_closes(self):
        self.command("add", rows=[question("Q1", "front", priority=0), question("Q2", "other", priority=0)])
        child = question("Q1a", "trace", parent="Q1")
        self.assertIn("parent", self.command("add", rows=[child], ok=False))
        self.command("record", rows=[{"id": "Q1", "status": "issue", "answer": "Тормозит нажатие",
                                      "source": "trace.json", "scope": "Android", "next": "Сравнить сети"}])
        self.command("add", rows=[child])
        self.assertEqual(self.command("plan")["batches"][0]["id"], "other")
        self.command("record", rows=[{"id": "Q2", "status": "unknown", "answer": "Данных мало",
                                      "next": "Посмотреть выгрузку регистраций"}])
        self.assertEqual(self.command("plan")["batches"][0]["id"], "trace")
        self.command("record", rows=[{"id": "Q1", "status": "pass", "answer": "Сценарий работает",
                                      "source": "new-trace.json", "scope": "Android"}])
        stopped = self.command("plan")
        self.assertEqual((stopped["ready_batches"], stopped["blocked_open"]), (0, 1))
        result = self.command("show", "Q1")["Q1"]
        self.assertEqual(result["result"]["source"], "new-trace.json")
        self.assertEqual(result["history"][0]["source"], "trace.json")

    def test_failed_record_leaves_previous_state_intact(self):
        self.command("add", rows=[question("Q1", "a"), question("Q2", "a")])
        original = self.state.read_bytes()
        self.command("record", rows=[
            {"id": "Q1", "status": "pass", "answer": "Совпадает", "source": "source", "scope": "a"},
            {"id": "Q2", "status": "pass", "answer": "Нет источника"},
        ], ok=False)
        self.assertEqual(self.state.read_bytes(), original)

    def test_empty_plan_still_shows_substantial_unexpanded_findings(self):
        self.command("add", rows=[
            question("Q1", "traffic", priority=0),
            question("Q2", "contacts", priority=0),
            question("Q3", "background", priority=2),
        ])
        self.command("record", rows=[
            {"id": "Q1", "status": "issue", "answer": "Путь обрывается",
             "source": "journey.json", "scope": "проверенный сегмент", "next": "Сравнить прежний и текущий пути"},
            {"id": "Q2", "status": "unknown", "answer": "Источники расходятся", "next": "Сверить визиты с заявками"},
            {"id": "Q3", "status": "unknown", "answer": "Неясно", "next": "Получить контекст"},
        ])
        before = self.command("plan", "--limit", "1")
        self.assertEqual((before["ready_batches"], before["unexpanded_count"]), (0, 2))
        self.assertEqual([row["id"] for row in before["unexpanded"]], ["Q1"])
        self.assertEqual(before["unexpanded"][0]["next"], "Сравнить прежний и текущий пути")
        self.command("add", rows=[question("Q1a", "old-and-new", parent="Q1")])
        after = self.command("plan")
        self.assertEqual(after["ready_batches"], 1)
        self.assertEqual([row["id"] for row in after["unexpanded"]], ["Q2"])

    def test_child_pass_does_not_resolve_an_unknown_primary_question(self):
        self.command("add", rows=[question("Q1", "traffic", priority=0)])
        self.command("record", rows=[{"id": "Q1", "status": "unknown", "answer": "Причина потери неизвестна",
                                      "next": "Проверить шаг перехода"}])
        self.command("add", rows=[question("Q1a", "events", parent="Q1")])
        self.command("record", rows=[{"id": "Q1a", "status": "pass", "answer": "Место потери известно",
                                      "source": "events.json", "scope": "мобильные визиты"}])
        plan = self.command("plan")
        self.assertEqual((plan["ready_batches"], plan["unexpanded_count"]), (0, 0))
        self.assertEqual(self.command("review", unfinished=True)["branches"][0]["review"], "decide")
        self.command("conclude", rows=[{"id": "Q1", "outcome": "resolved", "decision": "Чинить кнопку",
                                        "evidence": ["Q1a"]}], ok=False)
        self.command("conclude", rows=[{"id": "Q1", "outcome": "limited", "decision": "Локализовать сбой до записи",
                                        "missing": "Доходит ли нажатие до обработчика",
                                        "limit": "Доступны только агрегированные события", "evidence": ["Q1a"]}])
        self.assertTrue(self.command("review")["summary_ready"])
        self.command("add", rows=[question("Q1b", "trace", parent="Q1")])
        branch = self.command("review", unfinished=True)["branches"][0]
        self.assertEqual(branch["review"], "continue")
        self.assertNotIn("conclusion", branch)

    def test_resolved_conclusion_requires_evidence_in_the_same_branch(self):
        self.command("add", rows=[question("Q1", "a", priority=0), question("Q2", "b", priority=0)])
        self.command("record", rows=[
            {"id": "Q1", "status": "issue", "answer": "Воспроизведён сбой",
             "source": "first.json", "scope": "a", "next": "Исправить найденную причину"},
            {"id": "Q2", "status": "pass", "answer": "Вторая проверка прошла",
             "source": "second.json", "scope": "b"},
        ])
        closed = {"id": "Q1", "outcome": "resolved", "decision": "Исправить причину", "evidence": ["Q2"]}
        self.command("conclude", rows=[closed], ok=False)
        self.command("conclude", rows=[dict(closed, evidence=["Q1"])])
        self.assertTrue(self.command("review")["summary_ready"])

    def test_helper_draft_cannot_close_a_question(self):
        q = question("Q1", "traffic", priority=0)
        self.command("add", rows=[q])
        draft = {"id": "Q1", "packet": "before-after", "kind": "supported", "model": "gpt-6.1-terra",
                 "answer": "Визиты пришли на страницу", "evidence": ["E1"],
                 "counterexample": {"observation": "Один маршрут заканчивается раньше", "evidence": ["E2"]}}
        self.command("draft", rows=[dict(draft, evidence=[])], ok=False)
        self.command("draft", rows=[dict(draft, counterexample={"observation": "Не подтверждено"})], ok=False)
        self.command("draft", rows=[draft])
        item = self.command("show", "Q1")["Q1"]
        self.assertEqual(item["status"], "open")
        self.assertEqual(item["draft"]["evidence"], ["E1"])
        self.assertEqual(item["draft"]["counterexample"]["evidence"], ["E2"])
        self.assertEqual(self.command("review", unfinished=True)["branches"][0]["review"], "continue")
        self.assertEqual(self.command("add", rows=[q])["added"], [])
        self.command("record", rows=[{"id": "Q1", "status": "pass", "answer": "Строки сверены",
                                      "source": "original.json", "scope": "проверенная группа"}])
        item = self.command("show", "Q1")["Q1"]
        self.assertEqual(item["draft"]["answer"], "Визиты пришли на страницу")
        self.assertEqual(item["result"]["source"], "original.json")
        self.assertTrue(self.command("review")["summary_ready"])

    def test_two_helpers_can_contribute_to_the_same_question(self):
        q = question("Q1", "step", priority=0)
        self.command("add", rows=[q])
        external = {"id": "Q1", "packet": "L1", "kind": "supported",
                    "answer": "Похожее расхождение описано в открытом кейсе", "evidence": ["PUBLIC1"]}
        visual = dict(external, model="gpt-6.1-terra", packet="T1", answer="В форме видна кнопка",
                      evidence=["SHOT1"])
        self.command("draft", rows=[external])
        self.command("draft", rows=[visual, dict(external, model="gpt-6-luna")])
        record = self.command("show", "Q1")["Q1"]
        self.assertEqual(record["status"], "open")
        self.assertEqual({model: value["evidence"] for model, value in record["drafts"].items()},
                         {"gpt-6-luna": ["PUBLIC1"], "gpt-6.1-terra": ["SHOT1"]})
        self.assertEqual(self.command("add", rows=[q])["added"], [])
        report = subprocess.check_output([sys.executable, str(SCRIPT), str(self.state), "report"], text=True)
        self.assertIn("Черновик Luna", report)
        self.assertIn("Черновик Terra", report)

    def test_question_requires_all_predecessors_and_reopens_if_they_change(self):
        self.command("add", rows=[
            question("Q1", "definition"), question("Q2", "attribution"),
            question("Q3", "funnel", priority=0, requires=["Q2", "Q1"]),
            question("Q4", "result", requires=["Q3"]),
        ])
        def result(key, answer="Сверено"):
            return {"id": key, "status": "pass", "answer": answer, "source": "source.json", "scope": "один период"}

        self.assertEqual(self.command("plan")["waiting_for"][0], {"id": "Q3", "requires": ["Q1", "Q2"]})
        self.command("record", rows=[result("Q1")])
        self.assertEqual(self.command("plan")["waiting_for"][0]["requires"], ["Q2"])
        self.command("record", rows=[result("Q3")], ok=False)
        self.command("record", rows=[result("Q2")])
        self.assertEqual(self.command("plan")["batches"][0]["questions"][0]["id"], "Q3")
        self.command("record", rows=[result("Q3"), result("Q4")])
        self.assertTrue(self.command("review")["summary_ready"])
        self.command("record", rows=[result("Q1", "Сменилось определение")])
        reopened = self.command("show", "Q3", "Q4")
        self.assertEqual([r["status"] for r in reopened.values()], ["open", "open"])
        self.assertEqual(reopened["Q3"]["history"][0]["source"], "source.json")
        self.assertFalse(self.command("review", unfinished=True)["summary_ready"])

    def test_causal_check_is_planned_with_distinct_predictions(self):
        first = {"hypothesis": "Сбой", "expect": "Переход не откроется"}
        equal = {"hypothesis": "Другая причина", "expect": "  переход не откроется! "}
        self.command("add", rows=[question("Q1", "journey", predictions=[first, equal])], ok=False)
        second = {"hypothesis": "Редкое действие", "expect": "Контрольный переход откроется"}
        self.command("add", rows=[question("Q1", "journey", predictions=[first, second])])
        planned = self.command("plan")["batches"][0]["questions"][0]
        self.assertEqual(planned["predictions"], [first, second])
        result = {"id": "Q1", "status": "pass", "answer": "Проверено", "source": "trace.json", "scope": "мобильный вход"}
        self.command("record", rows=[result], ok=False)
        self.command("record", rows=[dict(result, observed="Контрольный переход открылся")])
        self.assertEqual(self.command("show", "Q1")["Q1"]["result"]["observed"], "Контрольный переход открылся")

    def prepare_survey(self, resolved, *, central_predictions=True):
        self.command("map", rows=[{"object": "Причина потери заявки", "decision_question": "Q0",
                                   "components": [
                                       {"id": "site", "name": "Сайт", "group": "internal",
                                        "function": "Показывает услугу", "areas": ["переход"]},
                                       {"id": "market", "name": "Рынок", "group": "external",
                                        "function": "Определяет окружение", "areas": ["спрос"]},
                                   ]}])
        base = [question(f"Q{i}", "landing", priority=0 if i == 0 else 1,
                         component="site", area="переход") for i in range(19)]
        market = question("M1", "market", component="market", area="спрос")
        if resolved and central_predictions:
            base[0]["predictions"] = [{"hypothesis": "Ошибка доставки", "expect": "Контакт не доставлен"},
                                      {"hypothesis": "Ошибка учёта", "expect": "Получатель видит контакт"}]
        self.command("add", rows=[*base, market])
        primary = {"id": "Q0", "status": "issue" if resolved else "unknown",
                   "answer": "Есть наблюдение о причине", "source": "events.json",
                   "scope": "1–10 ноября", "next": "Сверить текущее решение"}
        secondary = dict(primary, id="Q1", status="issue")
        if resolved and central_predictions:
            primary["observed"] = "Контрольный контакт принят получателем"
        self.command("record", rows=[primary, secondary])
        if resolved and central_predictions:
            self.command("conclude", rows=[{"id": "Q0", "outcome": "resolved", "decision": "Причина воспроизведена",
                                            "evidence": ["Q0"]}])
        return base, market

    def complete_survey(self, base, market):
        children = [question(f"C{i}", "trace", parent="Q1", component="site", area="переход") for i in range(4)]
        children[0]["predictions"] = [{"hypothesis": "Технический сбой", "expect": "Ошибка обработчика"},
                                       {"hypothesis": "Потеря события", "expect": "Обработчик срабатывает"}]
        self.command("add", rows=children)
        self.command("record", rows=[
            {"id": row["id"], "status": "pass", "answer": "Проверено", "source": "report.json", "scope": "11–18 ноября",
             **({"observed": "Обработчик сработал"} if "predictions" in row else {})}
            for row in [*base[2:], *children]
        ])
        self.command("record", rows=[{"id": market["id"], "status": "unknown", "answer": "Нет данных об аукционе",
                                      "next": "При появлении разрешённого отчёта сравнить цену и объём"}])

    def test_early_resolution_does_not_skip_first_cycle(self):
        base, market = self.prepare_survey(resolved=True)
        partial = self.command("review", "--deep", unfinished=True)["coverage"]
        self.assertFalse(partial["first_reviewed"])
        self.assertEqual(partial["first"]["missing_components"], ["market"])
        self.command("add", rows=[question("S1", "new", component="site", area="переход", cycle=2)], ok=False)
        self.complete_survey(base, market)
        complete = self.command("review", "--deep")["coverage"]
        self.assertTrue(complete["first_reviewed"])
        self.assertEqual(complete["contrast_checks"], 2)
        self.assertEqual(complete["first"], {"questions": 24, "reviewed": 24, "evidenced": 23,
                                             "followups": 4, "missing_components": []})
        self.assertFalse(complete["second_required"])

    def test_unrelated_contrast_cannot_close_central_decision_in_new_or_saved_survey(self):
        base, market = self.prepare_survey(True, central_predictions=False)
        self.complete_survey(base, market)
        claim = {"id": "Q0", "outcome": "resolved", "decision": "Меняем форму", "evidence": ["Q0"]}
        self.command("conclude", rows=[claim], ok=False)
        legacy = json.loads(self.state.read_text())
        legacy["questions"]["Q0"]["conclusion"] = {k: v for k, v in claim.items() if k != "id"}
        self.state.write_text(json.dumps(legacy))
        coverage = self.command("review", "--deep", unfinished=True)["coverage"]
        self.assertEqual(coverage["decision_basis"], "missing_decision_contrast")
        self.assertTrue(coverage["second_required"])
        self.assertEqual(self.command("review", "--deep", unfinished=True)["branches"][0]["review"], "decide")
        report = subprocess.check_output([sys.executable, str(SCRIPT), str(self.state), "report"], text=True)
        self.assertIn("Прежний вывод требует проверки", report)
        self.assertIn(claim["decision"], report)

    def test_same_wording_for_distinct_predictions_is_kept(self):
        q = question("Q1", "events", predictions=[{"hypothesis": "Сбой", "expect": "Ошибка"},
                                                  {"hypothesis": "Потеря учёта", "expect": "Доставлено"}])
        changed = dict(q, id="Q2", predictions=[q["predictions"][0], {"hypothesis": "Задержка", "expect": "В очереди"}])
        result = self.command("add", rows=[q, changed, dict(q, id="Q3")])
        self.assertEqual(result["added"], ["Q1", "Q2"])
        self.assertEqual(result["duplicates"], {"Q3": "Q1"})

    def test_unresolved_primary_needs_second_cycle_even_with_explicit_limit(self):
        base, market = self.prepare_survey(resolved=False)
        self.complete_survey(base, market)
        self.command("conclude", rows=[{"id": "Q0", "outcome": "limited", "decision": "Отложить изменение",
                                        "missing": "Данные о браузере", "limit": "Есть только агрегаты"}])
        pending = self.command("review", "--deep", unfinished=True)
        self.assertTrue(pending["coverage"]["second_required"])
        cycle = [question(f"S{i}", "new", component="site", area="переход", cycle=2) for i in range(8)]
        self.command("add", rows=cycle)
        self.command("record", rows=[
            {"id": row["id"], "status": "unknown", "answer": "Агрегат не отвечает", "next": "Нужна трассировка"}
            for row in cycle
        ])
        self.assertEqual(self.command("review", "--deep")["coverage"]["second"], {"questions": 8, "reviewed": 8})
