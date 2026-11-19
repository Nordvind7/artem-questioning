"""Replay one decision from the synthetic case through the public queue CLI."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", action="store_true", help="All 20 contacts reached the CRM")
    control = parser.parse_args().control
    script = Path(__file__).resolve().parents[2] / "skills/artem-questioning/scripts/questions.py"
    with tempfile.TemporaryDirectory() as folder:
        state = Path(folder) / "questions.json"

        def step(command, rows=None):
            args = [sys.executable, str(script), str(state), command]
            if rows is not None:
                args.append("-")
            return subprocess.run(args, input=json.dumps(rows, ensure_ascii=False), text=True,
                                  capture_output=True, check=True).stdout

        step("map", [{"object": "Заявки на консультацию", "decision_question": "Q1", "components": [
            {"id": "site", "group": "internal", "name": "Сайт", "function": "Принимает форму", "areas": ["отправка"]},
            {"id": "crm", "group": "external", "name": "Получатель", "function": "Принимает карточку", "areas": ["доставка"]}]}])
        step("add", [{"id": "Q1", "component": "crm", "area": "доставка", "batch": "joined-ids", "priority": 0,
                      "question": "Нужно ли восстановить реальные обращения?",
                      "check": "Сопоставить серверные ID с фактическим приёмом в CRM",
                      "decision": "Выбрать первое исправление", "predictions": [
                          {"hypothesis": "Потерялся только учёт", "expect": "Все 20 ID получены в CRM"},
                          {"hypothesis": "Есть также сбой доставки", "expect": "Часть ID отсутствует у получателя"}]}])
        delivered = 20 if control else 11
        observation = f"Из 20 ID получатель подтвердил {delivered}. В аналитике виден один."
        decision = ("Проверить и восстановить учёт событий на iPhone." if control else
                    "Восстановить доставку 9 обращений, сверив ID и исключив дубли.")
        step("record", [{"id": "Q1", "status": "pass" if control else "issue", "answer": observation,
                         "observed": observation, "source": "RESULT.md · Контрпример" if control else "CASE.md · Сверка ID",
                         "scope": "iPhone, вторая учебная неделя", "next": "Повторить сверку по ID после исправления"}])
        step("conclude", [{"id": "Q1", "outcome": "resolved", "evidence": ["Q1"], "decision": decision}])
        print(step("report"), end="")


if __name__ == "__main__":
    main()
