"""Встроенные шаблоны процессов версии 2.0."""

TEMPLATE_EDO = "эдо_договор"
TEMPLATE_GUARD = "охрана_объекта"

COMPLETION_MANUAL = "ручная отметка"
COMPLETION_CHECKLIST = "чек-лист"
COMPLETION_WAIT = "ожидание стороны"

ACTION_EDO_SIGN = "edo_sign"
ACTION_GUARD_SIGN = "guard_sign"
ACTION_CHECKLIST = "checklist"

ESC_FIRST = "первое"
ESC_REPEAT = "повторное"
ESC_FREQUENT = "частое"

# Жёсткие интервалы из согласованного плана.
DEFAULT_ESCALATION = (
    {"interval_type": ESC_FIRST, "delay_minutes": 120, "fixed_time": ""},
    {"interval_type": ESC_REPEAT, "delay_minutes": 0, "fixed_time": "09:00"},
    {"interval_type": ESC_FREQUENT, "delay_minutes": 180, "fixed_time": ""},
)

GUARD_SIGN_THRESHOLD_WORKDAYS = 2
GUARD_DEFER_DAYS = 3
STALE_WAITING_HOURS = 4

TEMPLATE_LABELS = {
    TEMPLATE_EDO: "ЭДО-договор",
    TEMPLATE_GUARD: "Охрана объекта",
}

GUARD_FIELDS = (
    ("contract_number", "Номер договора"),
    ("contract_date", "Дата договора"),
    ("customer", "Заказчик"),
    ("object_address", "Адрес объекта"),
    ("legal_address", "Юридический адрес"),
    ("object_type", "Тип объекта"),
    ("district", "Район СПб"),
    ("start_date", "Дата начала услуг"),
    ("end_date", "Дата окончания услуг"),
)


def _step(key, title, completion, action_kind="", depends=None, checklist=(), escalate=False):
    return {
        "key": key,
        "title": title,
        "completion_type": completion,
        "action_kind": action_kind,
        "depends_on": depends,
        "checklist": list(checklist),
        "escalation": list(DEFAULT_ESCALATION) if escalate else [],
    }


TEMPLATES = {
    TEMPLATE_EDO: {
        "default_title": "Подписание договора через ЭДО",
        "branches": [
            {
                "key": "main",
                "title": "Подписание через ЭДО",
                "deferred": False,
                "steps": [
                    _step("e1", "Направлено приглашение к ЭДО", COMPLETION_MANUAL),
                    _step(
                        "e2",
                        "Приглашение принято контрагентом",
                        COMPLETION_MANUAL,
                        depends="e1",
                    ),
                    _step(
                        "e3",
                        "Договор выложен в ЭДО",
                        COMPLETION_MANUAL,
                        depends="e2",
                    ),
                    _step(
                        "e4",
                        "Ожидание подписания договора контрагентом",
                        COMPLETION_WAIT,
                        ACTION_EDO_SIGN,
                        depends="e3",
                        escalate=True,
                    ),
                    _step(
                        "e5",
                        "Финальный чек-лист после подписания",
                        COMPLETION_CHECKLIST,
                        ACTION_CHECKLIST,
                        depends="e4",
                        checklist=(
                            "Договор (подписанный обеими сторонами) скачан",
                            "Файл выложен в папку на сервере",
                            "Бухгалтерии предоставлен/указан доступ к папке",
                        ),
                        escalate=True,
                    ),
                ],
            }
        ],
    },
    TEMPLATE_GUARD: {
        "default_title": "Начало охраны объекта",
        "branches": [
            {
                "key": "A",
                "title": "Ветка А — Должностная инструкция",
                "deferred": False,
                "steps": [
                    _step("a1", "Разработать текст ДИ", COMPLETION_MANUAL),
                    _step(
                        "a2",
                        "Распечатать, поставить печать и подпись со своей стороны",
                        COMPLETION_MANUAL,
                        depends="a1",
                    ),
                    _step(
                        "a3",
                        "Передать на подписание заказчику",
                        COMPLETION_MANUAL,
                        depends="a2",
                    ),
                    _step(
                        "a4",
                        "Ожидание подписи заказчиком",
                        COMPLETION_WAIT,
                        ACTION_GUARD_SIGN,
                        depends="a3",
                        escalate=True,
                    ),
                    _step(
                        "a5",
                        "Получен финальный скан подписанной ДИ",
                        COMPLETION_MANUAL,
                        depends="a4",
                    ),
                ],
            },
            {
                "key": "B",
                "title": "Ветка Б — Уведомление о начале услуг",
                "deferred": False,
                "steps": [
                    _step("b1", "Заполнить таблицу данными договора", COMPLETION_MANUAL),
                    _step(
                        "b2",
                        "Приложить скан подписанной ДИ",
                        COMPLETION_MANUAL,
                        depends="a5",
                    ),
                    _step(
                        "b3",
                        "Отправить таблицу со сканом ДИ коллеге по электронной почте",
                        COMPLETION_MANUAL,
                        depends="b2",
                    ),
                    _step(
                        "b4",
                        "Распечатать таблицу на бумаге для коллеги",
                        COMPLETION_MANUAL,
                        depends="b3",
                    ),
                    _step(
                        "b5",
                        "Подтверждено: коллега подал уведомление через Госуслуги",
                        COMPLETION_MANUAL,
                        depends="b4",
                    ),
                ],
            },
            {
                "key": "C",
                "title": "Ветка В — Уведомление об окончании услуг",
                "deferred": True,
                "activation_field": "end_date",
                "steps": [
                    _step(
                        "c1",
                        "Заполнить таблицу с датой окончания услуг",
                        COMPLETION_MANUAL,
                    ),
                    _step(
                        "c2",
                        "Отправить таблицу коллеге по электронной почте",
                        COMPLETION_MANUAL,
                        depends="c1",
                    ),
                    _step(
                        "c3",
                        "Подтверждено: коллега подал уведомление о прекращении услуг",
                        COMPLETION_MANUAL,
                        depends="c2",
                    ),
                ],
            },
        ],
    },
}


def get_template(template_type):
    if template_type not in TEMPLATES:
        raise ValueError(f"Неизвестный шаблон: {template_type!r}")
    return TEMPLATES[template_type]
