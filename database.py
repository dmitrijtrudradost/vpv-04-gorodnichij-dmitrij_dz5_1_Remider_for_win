"""Слой доступа к данным напоминалки: хранение напоминаний и процессов в SQLite3."""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, time, date

from templates import (
    ACTION_CHECKLIST,
    COMPLETION_CHECKLIST,
    COMPLETION_WAIT,
    ESC_FIRST,
    ESC_FREQUENT,
    ESC_REPEAT,
    GUARD_DEFER_DAYS,
    GUARD_SIGN_THRESHOLD_WORKDAYS,
    STALE_WAITING_HOURS,
    get_template,
)

USER_TEMPLATE_PREFIX = "user:"

DB_FILENAME = "reminders.db"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT = "%Y-%m-%d"

STATUS_PENDING = "Ожидает"
STATUS_DONE = "Готово"
STATUS_OVERDUE = "Просрочено"
STATUS_CANCELLED = "Отменено"

ALL_STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_OVERDUE, STATUS_CANCELLED)

PROCESS_ACTIVE = "активен"
PROCESS_DONE = "завершён"

BRANCH_ACTIVE = "активна"
BRANCH_DEFERRED = "отложена"
BRANCH_DONE = "завершена"

STEP_NOT_STARTED = "не начат"
STEP_WAITING = "в ожидании"
STEP_DONE = "выполнен"
STEP_OVERDUE = "просрочен"
STEP_BLOCKED = "заблокирован"

OVERDUE_GRACE_SECONDS = 60
WORK_START_HOUR = 9
WORK_END_HOUR = 18


class StepDependencyError(Exception):
    """Шаг нельзя закрыть: не выполнена зависимость или чек-лист."""


def as_depends_list(value):
    """Ключи зависимостей шага: строка, список или пусто."""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if item]
    return [str(value)]


def now_str():
    """Текущее время в том же строковом формате, в котором оно лежит в базе."""
    return datetime.now().strftime(DATETIME_FORMAT)


def to_db_format(value):
    """Приводит datetime или строку к формату хранения."""
    if isinstance(value, datetime):
        return value.strftime(DATETIME_FORMAT)
    return str(value).strip()


def parse_dt(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min)
    text = str(value).strip()
    for fmt in (DATETIME_FORMAT, DATE_FORMAT, "%d.%m.%Y", "%d.%m.%y", "%d/%m/%Y"):
        try:
            parsed = datetime.strptime(text, fmt)
            if fmt == DATETIME_FORMAT:
                return parsed
            return datetime.combine(parsed.date(), time.min)
        except ValueError:
            continue
    return None


def is_weekend(day):
    return day.weekday() >= 5


def next_workday(day):
    candidate = day + timedelta(days=1)
    while is_weekend(candidate):
        candidate += timedelta(days=1)
    return candidate


def add_workdays(start, count):
    current = start if isinstance(start, date) else start.date()
    added = 0
    while added < count:
        current += timedelta(days=1)
        if not is_weekend(current):
            added += 1
    return current


def clamp_to_work_hours(moment, end_hour=WORK_END_HOUR, end_minute=0):
    """Сдвигает момент в будни 09:00 … граница включительно (по умолчанию 18:00)."""
    current = moment
    after_end = (current.hour, current.minute, current.second, current.microsecond) > (
        end_hour,
        end_minute,
        0,
        0,
    )
    if is_weekend(current.date()) or after_end:
        nxt = next_workday(current.date())
        return datetime.combine(nxt, time(WORK_START_HOUR, 0))
    if current.hour < WORK_START_HOUR:
        return current.replace(hour=WORK_START_HOUR, minute=0, second=0, microsecond=0)
    return current.replace(microsecond=0)


def next_workday_morning(moment=None, hour=WORK_START_HOUR, minute=0):
    base = moment or datetime.now()
    nxt = next_workday(base.date())
    return datetime.combine(nxt, time(hour, minute))


def parse_hhmm(text):
    raw = (text or "09:00").strip()
    parts = raw.split(":")
    hour = int(parts[0])
    minute = int(parts[1]) if len(parts) > 1 else 0
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError(f"Некорректное время: {raw}")
    return hour, minute


def format_hhmm(hour, minute):
    return f"{int(hour):02d}:{int(minute):02d}"


def parse_notify_until(text):
    """Граница «уведомлять до»; пустое значение — 18:00."""
    try:
        return parse_hhmm(text or "18:00")
    except (TypeError, ValueError, IndexError):
        return WORK_END_HOUR, 0


def format_notify_label(rules, notify_until="18:00"):
    """Краткая сводка правил эскалации для колонки дерева."""
    if not rules:
        return "—"
    until = format_hhmm(*parse_notify_until(notify_until))
    parts = []
    for rule in rules:
        kind = rule["interval_type"]
        if kind == ESC_FIRST:
            minutes = int(rule.get("delay_minutes") or 0)
            hours, rest = divmod(max(minutes, 0), 60)
            if rest:
                parts.append(f"через {hours} ч {rest} мин")
            else:
                parts.append(f"через {max(hours, 1)} ч")
        elif kind == ESC_REPEAT:
            stamp = rule.get("fixed_time") or "09:00"
            parts.append(f"пн–пт {stamp}–{until}")
        elif kind == ESC_FREQUENT:
            minutes = int(rule.get("delay_minutes") or 0)
            hours = max(1, minutes // 60) if minutes else 3
            parts.append(f"каждые {hours} ч")
    return " → ".join(parts) if parts else "—"


class ReminderDatabase:
    """Все операции с таблицей напоминаний и слоем процессов.

    Одно соединение используется и главным потоком (GUI), и фоновым потоком
    мониторинга, поэтому оно создаётся с ``check_same_thread=False``, а каждый
    запрос защищён блокировкой.
    """

    def __init__(self, db_path=DB_FILENAME):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self.init_database()

    def init_database(self):
        """Создаёт таблицы и индексы, если их ещё нет. Старую reminders не ломает."""
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS reminders (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    title       TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    due_time    TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'Ожидает',
                    created_at  TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reminders_due
                    ON reminders(status, due_time);

                CREATE TABLE IF NOT EXISTS processes (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    template_type TEXT NOT NULL,
                    title         TEXT NOT NULL,
                    status        TEXT NOT NULL DEFAULT 'активен',
                    metadata      TEXT NOT NULL DEFAULT '{}',
                    created_at    TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS branches (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    process_id      INTEGER NOT NULL,
                    title           TEXT NOT NULL,
                    status          TEXT NOT NULL,
                    is_deferred     INTEGER NOT NULL DEFAULT 0,
                    activation_date TEXT,
                    FOREIGN KEY (process_id) REFERENCES processes(id)
                );

                CREATE TABLE IF NOT EXISTS steps (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    branch_id        INTEGER NOT NULL,
                    title            TEXT NOT NULL,
                    order_index      INTEGER NOT NULL,
                    status           TEXT NOT NULL,
                    completion_type  TEXT NOT NULL,
                    action_kind      TEXT NOT NULL DEFAULT '',
                    depends_on_step_id INTEGER,
                    reminder_id      INTEGER,
                    waiting_since    TEXT,
                    escalation_index INTEGER NOT NULL DEFAULT 0,
                    result_note      TEXT NOT NULL DEFAULT '',
                    notify_until     TEXT NOT NULL DEFAULT '18:00',
                    FOREIGN KEY (branch_id) REFERENCES branches(id)
                );

                CREATE TABLE IF NOT EXISTS step_checklist_items (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id    INTEGER NOT NULL,
                    text       TEXT NOT NULL,
                    is_checked INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (step_id) REFERENCES steps(id)
                );

                CREATE TABLE IF NOT EXISTS escalation_rules (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    step_id       INTEGER NOT NULL,
                    interval_type TEXT NOT NULL,
                    delay_minutes INTEGER NOT NULL DEFAULT 0,
                    fixed_time    TEXT NOT NULL DEFAULT '',
                    is_active     INTEGER NOT NULL DEFAULT 1,
                    FOREIGN KEY (step_id) REFERENCES steps(id)
                );

                CREATE TABLE IF NOT EXISTS process_templates (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    name           TEXT NOT NULL,
                    structure_json TEXT NOT NULL,
                    created_at     TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS step_dependencies (
                    step_id            INTEGER NOT NULL,
                    depends_on_step_id INTEGER NOT NULL,
                    PRIMARY KEY (step_id, depends_on_step_id),
                    FOREIGN KEY (step_id) REFERENCES steps(id),
                    FOREIGN KEY (depends_on_step_id) REFERENCES steps(id)
                );
                """
            )
            columns = [
                row[1]
                for row in self._conn.execute("PRAGMA table_info(reminders)").fetchall()
            ]
            if "step_id" not in columns:
                self._conn.execute("ALTER TABLE reminders ADD COLUMN step_id INTEGER")
            step_columns = [
                row[1]
                for row in self._conn.execute("PRAGMA table_info(steps)").fetchall()
            ]
            if step_columns and "notify_until" not in step_columns:
                self._conn.execute(
                    "ALTER TABLE steps ADD COLUMN notify_until TEXT NOT NULL DEFAULT '18:00'"
                )
            self._conn.execute(
                """
                INSERT OR IGNORE INTO step_dependencies (step_id, depends_on_step_id)
                SELECT id, depends_on_step_id FROM steps
                WHERE depends_on_step_id IS NOT NULL
                """
            )
            self._conn.commit()

    def add_reminder(self, title, description, due_time, step_id=None):
        """Добавляет напоминание и возвращает его id."""
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reminders (title, description, due_time, status, created_at, step_id)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    title.strip(),
                    (description or "").strip(),
                    to_db_format(due_time),
                    STATUS_PENDING,
                    now_str(),
                    step_id,
                ),
            )
            self._conn.commit()
            return cursor.lastrowid

    def get_all_reminders(self, status=None):
        """Все напоминания, при указанном ``status`` — только с этим статусом.

        Порядок: сначала ожидающие, затем остальные, внутри групп — по времени.
        """
        query = "SELECT * FROM reminders"
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += " ORDER BY status = ? DESC, due_time ASC"
        params = params + (STATUS_PENDING,)

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def get_due_reminders(self):
        """Ожидающие напоминания, время которых уже наступило."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM reminders
                WHERE status = ? AND due_time <= ?
                ORDER BY due_time ASC
                """,
                (STATUS_PENDING, now_str()),
            ).fetchall()
        return [dict(row) for row in rows]

    def sort_by_due_time(self, status=STATUS_PENDING, ascending=True):
        """Напоминания, отсортированные по времени срабатывания."""
        direction = "ASC" if ascending else "DESC"
        query = "SELECT * FROM reminders"
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)
        query += f" ORDER BY due_time {direction}"

        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]

    def update_status(self, reminder_id, status):
        """Меняет статус напоминания. Возвращает True, если запись найдена."""
        if status not in ALL_STATUSES:
            raise ValueError(f"Неизвестный статус: {status!r}")

        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reminders SET status = ? WHERE id = ?",
                (status, reminder_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def delete_reminder(self, reminder_id):
        """Удаляет напоминание. Возвращает True, если запись найдена."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM reminders WHERE id = ?", (reminder_id,)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def snooze_reminder(self, reminder_id, minutes):
        """Переносит напоминание на ``minutes`` минут вперёд от текущего момента."""
        return self.snooze_reminder_to(
            reminder_id, datetime.now() + timedelta(minutes=minutes)
        )

    def snooze_reminder_to(self, reminder_id, new_due_time):
        """Ставит абсолютное due_time, не трогая правила эскалации шага."""
        if isinstance(new_due_time, datetime) and new_due_time <= datetime.now():
            raise ValueError("Новое время должно быть в будущем.")
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reminders SET due_time = ?, status = ? WHERE id = ?",
                (to_db_format(new_due_time), STATUS_PENDING, reminder_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def mark_overdue(self):
        """Переводит в «Просрочено» ожидающие одиночные записи, опоздавшие больше минуты.

        Записи процессов (с ``step_id``) не трогаются: их время двигает эскалация.
        """
        threshold = datetime.now() - timedelta(seconds=OVERDUE_GRACE_SECONDS)
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE reminders
                SET status = ?
                WHERE status = ? AND due_time < ?
                  AND (step_id IS NULL OR step_id = 0)
                """,
                (STATUS_OVERDUE, STATUS_PENDING, to_db_format(threshold)),
            )
            self._conn.commit()
            return cursor.rowcount

    def get_reminder_by_id(self, reminder_id):
        """Одно напоминание по id или None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM reminders WHERE id = ?", (reminder_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_reminders_count(self, status=None):
        """Количество напоминаний, при указанном ``status`` — только с ним."""
        query = "SELECT COUNT(*) FROM reminders"
        params = ()
        if status:
            query += " WHERE status = ?"
            params = (status,)

        with self._lock:
            return self._conn.execute(query, params).fetchone()[0]

    def get_counts_by_status(self):
        """Словарь «статус -> количество» для всех статусов сразу."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS total FROM reminders GROUP BY status"
            ).fetchall()
        counts = {status: 0 for status in ALL_STATUSES}
        for row in rows:
            counts[row["status"]] = row["total"]
        return counts

    # ----------------------------------------------------------- Processes

    def _resolve_template_spec(self, template_type):
        if str(template_type).startswith(USER_TEMPLATE_PREFIX):
            raw_id = str(template_type)[len(USER_TEMPLATE_PREFIX) :]
            try:
                template_id = int(raw_id)
            except ValueError as error:
                raise ValueError(f"Неизвестный шаблон: {template_type!r}") from error
            row = self.get_process_template(template_id)
            if row is None:
                raise ValueError(f"Пользовательский шаблон {template_id} не найден.")
            spec = json.loads(row["structure_json"] or "{}")
            if not isinstance(spec, dict) or not spec.get("branches"):
                raise ValueError("В шаблоне нет веток.")
            spec.setdefault("default_title", row["name"])
            return spec
        return get_template(template_type)

    def list_process_templates(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM process_templates ORDER BY created_at DESC, id DESC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_process_template(self, template_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM process_templates WHERE id = ?", (template_id,)
            ).fetchone()
        return dict(row) if row else None

    def save_process_template(self, name, structure):
        """Сохраняет пользовательский шаблон. Возвращает id."""
        title = (name or "").strip()
        if not title:
            raise ValueError("Введите название шаблона.")
        spec = structure if isinstance(structure, dict) else json.loads(structure)
        if not spec.get("branches"):
            raise ValueError("Добавьте хотя бы одну ветку.")
        spec.setdefault("default_title", title)
        payload = json.dumps(spec, ensure_ascii=False)
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO process_templates (name, structure_json, created_at)
                VALUES (?, ?, ?)
                """,
                (title, payload, now_str()),
            )
            self._conn.commit()
            return cursor.lastrowid

    def update_process_template(self, template_id, name, structure):
        """Перезаписывает пользовательский шаблон. Запущенные процессы не трогает."""
        title = (name or "").strip()
        if not title:
            raise ValueError("Введите название шаблона.")
        spec = structure if isinstance(structure, dict) else json.loads(structure)
        if not spec.get("branches"):
            raise ValueError("Добавьте хотя бы одну ветку.")
        spec.setdefault("default_title", title)
        payload = json.dumps(spec, ensure_ascii=False)
        with self._lock:
            cursor = self._conn.execute(
                """
                UPDATE process_templates
                SET name = ?, structure_json = ?
                WHERE id = ?
                """,
                (title, payload, int(template_id)),
            )
            self._conn.commit()
            if cursor.rowcount == 0:
                raise ValueError("Шаблон не найден.")
            return int(template_id)

    def delete_process_template(self, template_id):
        """Удаляет пользовательский шаблон. Запущенные процессы не трогает."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM process_templates WHERE id = ?", (int(template_id),)
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def finish_process(self, process_id, result_note="Закрыт вручную"):
        """Отмечает все шаги выполненными и переводит процесс в «завершён»."""
        tree = self.get_process_tree(process_id)
        if tree is None:
            return False
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT steps.id FROM steps
                JOIN branches ON branches.id = steps.branch_id
                WHERE branches.process_id = ?
                """,
                (process_id,),
            ).fetchall()
            for row in rows:
                self._conn.execute(
                    """
                    UPDATE steps SET status = ?, result_note = ?
                    WHERE id = ? AND status != ?
                    """,
                    (STEP_DONE, result_note, row["id"], STEP_DONE),
                )
                self._cancel_step_reminders(row["id"])
            self._conn.execute(
                "UPDATE branches SET status = ? WHERE process_id = ?",
                (BRANCH_DONE, process_id),
            )
            self._conn.execute(
                "UPDATE processes SET status = ? WHERE id = ?",
                (PROCESS_DONE, process_id),
            )
            self._conn.commit()
        return True

    def create_process(self, template_type, title, metadata=None):
        """Создаёт процесс по шаблону и возвращает его id."""
        spec = self._resolve_template_spec(template_type)
        payload = dict(metadata or {})
        process_title = (title or spec["default_title"]).strip()

        for branch_spec in spec["branches"]:
            if not branch_spec.get("deferred"):
                continue
            raw_end = payload.get(branch_spec.get("activation_field") or "end_date")
            if parse_dt(raw_end) is None:
                raise ValueError("Для отложенной ветки нужна дата окончания услуг.")

        with self._lock:
            try:
                cursor = self._conn.execute(
                    """
                    INSERT INTO processes (template_type, title, status, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (template_type, process_title, PROCESS_ACTIVE, json.dumps(payload, ensure_ascii=False), now_str()),
                )
                process_id = cursor.lastrowid
                key_to_id = {}

                for branch_spec in spec["branches"]:
                    deferred = bool(branch_spec.get("deferred"))
                    activation = None
                    if deferred:
                        raw_end = payload.get(branch_spec.get("activation_field") or "end_date")
                        end_at = parse_dt(raw_end)
                        if end_at is None:
                            raise ValueError("Для отложенной ветки нужна дата окончания услуг.")
                        wake = end_at.date() - timedelta(days=GUARD_DEFER_DAYS)
                        activation = wake.strftime(DATE_FORMAT)
                        if wake <= date.today():
                            deferred = False

                    branch_status = BRANCH_DEFERRED if deferred else BRANCH_ACTIVE
                    branch_cursor = self._conn.execute(
                        """
                        INSERT INTO branches (process_id, title, status, is_deferred, activation_date)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (process_id, branch_spec["title"], branch_status, int(deferred), activation),
                    )
                    branch_id = branch_cursor.lastrowid

                    for index, step_spec in enumerate(branch_spec["steps"]):
                        deps = as_depends_list(step_spec.get("depends_on"))
                        initial = STEP_NOT_STARTED
                        if not deferred:
                            initial = STEP_BLOCKED if deps else STEP_WAITING
                        step_cursor = self._conn.execute(
                            """
                            INSERT INTO steps (
                                branch_id, title, order_index, status, completion_type,
                                action_kind, depends_on_step_id, waiting_since, escalation_index,
                                notify_until
                            ) VALUES (?, ?, ?, ?, ?, ?, NULL, ?, 0, ?)
                            """,
                            (
                                branch_id,
                                step_spec["title"],
                                index,
                                initial,
                                step_spec.get("completion_type") or "ручная отметка",
                                step_spec.get("action_kind") or "",
                                now_str() if initial == STEP_WAITING else None,
                                step_spec.get("notify_until") or "18:00",
                            ),
                        )
                        step_id = step_cursor.lastrowid
                        step_key = step_spec.get("key") or f"s{branch_id}_{index}"
                        key_to_id[step_key] = step_id

                        for text in step_spec.get("checklist") or ():
                            self._conn.execute(
                                "INSERT INTO step_checklist_items (step_id, text, is_checked) VALUES (?, ?, 0)",
                                (step_id, text),
                            )
                        for rule in step_spec.get("escalation") or ():
                            self._conn.execute(
                                """
                                INSERT INTO escalation_rules
                                    (step_id, interval_type, delay_minutes, fixed_time, is_active)
                                VALUES (?, ?, ?, ?, 1)
                                """,
                                (
                                    step_id,
                                    rule["interval_type"],
                                    int(rule.get("delay_minutes") or 0),
                                    rule.get("fixed_time") or "",
                                ),
                            )

                for branch_spec in spec["branches"]:
                    for step_spec in branch_spec["steps"]:
                        step_key = step_spec.get("key")
                        if step_key not in key_to_id:
                            continue
                        dep_ids = []
                        seen = set()
                        for dep_key in as_depends_list(step_spec.get("depends_on")):
                            if dep_key not in key_to_id:
                                continue
                            dep_id = key_to_id[dep_key]
                            if dep_id == key_to_id[step_key] or dep_id in seen:
                                continue
                            seen.add(dep_id)
                            dep_ids.append(dep_id)
                        step_id = key_to_id[step_key]
                        first = dep_ids[0] if dep_ids else None
                        self._conn.execute(
                            "UPDATE steps SET depends_on_step_id = ? WHERE id = ?",
                            (first, step_id),
                        )
                        for dep_id in dep_ids:
                            self._conn.execute(
                                """
                                INSERT OR IGNORE INTO step_dependencies
                                    (step_id, depends_on_step_id)
                                VALUES (?, ?)
                                """,
                                (step_id, dep_id),
                            )

                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        self._refresh_branch_step_states(process_id)
        return process_id

    def get_active_processes(self):
        """Активные процессы с вложенными ветками и шагами."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM processes WHERE status = ? ORDER BY created_at DESC",
                (PROCESS_ACTIVE,),
            ).fetchall()
        return [self.get_process_tree(row["id"]) for row in rows]

    def get_process_tree(self, process_id):
        """Полное дерево одного процесса."""
        with self._lock:
            process = self._conn.execute(
                "SELECT * FROM processes WHERE id = ?", (process_id,)
            ).fetchone()
            if process is None:
                return None
            branches = self._conn.execute(
                "SELECT * FROM branches WHERE process_id = ? ORDER BY id",
                (process_id,),
            ).fetchall()
            tree = dict(process)
            tree["metadata"] = json.loads(tree.get("metadata") or "{}")
            tree["branches"] = []
            for branch in branches:
                branch_data = dict(branch)
                steps = self._conn.execute(
                    "SELECT * FROM steps WHERE branch_id = ? ORDER BY order_index",
                    (branch["id"],),
                ).fetchall()
                branch_data["steps"] = []
                for step in steps:
                    step_data = dict(step)
                    step_data["checklist"] = [
                        dict(item)
                        for item in self._conn.execute(
                            "SELECT * FROM step_checklist_items WHERE step_id = ? ORDER BY id",
                            (step["id"],),
                        ).fetchall()
                    ]
                    step_data["stale"] = self._is_step_stale(step_data)
                    step_data["escalation"] = self._escalation_rules(step["id"])
                    step_data["notify_until"] = step_data.get("notify_until") or "18:00"
                    step_data["notify_label"] = format_notify_label(
                        step_data["escalation"],
                        step_data["notify_until"],
                    )
                    live = self._live_reminder_for_step(step["id"])
                    step_data["next_due"] = live["due_time"] if live else None
                    branch_data["steps"].append(step_data)
                tree["branches"].append(branch_data)
        return tree

    def get_step(self, step_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM steps WHERE id = ?", (step_id,)).fetchone()
        return dict(row) if row else None

    def get_step_by_reminder(self, reminder_id):
        reminder = self.get_reminder_by_id(reminder_id)
        if not reminder or not reminder.get("step_id"):
            return None
        return self.get_step(reminder["step_id"])

    def get_step_escalation(self, step_id):
        """Правила шага в виде, удобном для диалога правки."""
        rules = self._escalation_rules(step_id)
        if not rules:
            return None
        settings = {
            "first_hours": 2,
            "repeat_time": "09:00",
            "frequent_hours": 3,
            "notify_until": "18:00",
        }
        step = self.get_step(step_id)
        if step:
            settings["notify_until"] = step.get("notify_until") or "18:00"
        for rule in rules:
            if rule["interval_type"] == ESC_FIRST:
                settings["first_hours"] = max(1, int(rule["delay_minutes"] or 120) // 60)
            elif rule["interval_type"] == ESC_REPEAT:
                settings["repeat_time"] = rule.get("fixed_time") or "09:00"
            elif rule["interval_type"] == ESC_FREQUENT:
                settings["frequent_hours"] = max(1, int(rule["delay_minutes"] or 180) // 60)
        return settings

    def update_step_escalation(
        self, step_id, first_minutes, repeat_time, frequent_minutes, notify_until="18:00"
    ):
        """Обновляет три правила эскалации шага и пересчитывает ближайшее due."""
        first_minutes = int(first_minutes)
        frequent_minutes = int(frequent_minutes)
        if not 60 <= first_minutes <= 48 * 60:
            raise ValueError("Первое напоминание: от 1 до 48 часов.")
        if not 60 <= frequent_minutes <= 12 * 60:
            raise ValueError("Повтор: от 1 до 12 часов.")
        try:
            hour, minute = parse_hhmm(repeat_time)
            until_hour, until_minute = parse_hhmm(notify_until)
        except (TypeError, ValueError, IndexError):
            raise ValueError("Время в формате ЧЧ:ММ.")
        if (until_hour, until_minute) <= (WORK_START_HOUR, 0):
            raise ValueError("Граница «уведомлять до» должна быть позже 09:00.")
        if (hour, minute) < (WORK_START_HOUR, 0) or (hour, minute) > (until_hour, until_minute):
            raise ValueError("Время повтора должно быть в пределах 09:00 и «уведомлять до».")
        stamp = format_hhmm(hour, minute)
        until_stamp = format_hhmm(until_hour, until_minute)
        rules = self._escalation_rules(step_id)
        if not rules:
            raise ValueError("У этого шага нет расписания уведомлений.")

        with self._lock:
            self._conn.execute(
                "UPDATE steps SET notify_until = ? WHERE id = ?",
                (until_stamp, step_id),
            )
            for rule in rules:
                if rule["interval_type"] == ESC_FIRST:
                    self._conn.execute(
                        "UPDATE escalation_rules SET delay_minutes = ?, fixed_time = '' WHERE id = ?",
                        (first_minutes, rule["id"]),
                    )
                elif rule["interval_type"] == ESC_REPEAT:
                    self._conn.execute(
                        "UPDATE escalation_rules SET delay_minutes = 0, fixed_time = ? WHERE id = ?",
                        (stamp, rule["id"]),
                    )
                elif rule["interval_type"] == ESC_FREQUENT:
                    self._conn.execute(
                        "UPDATE escalation_rules SET delay_minutes = ?, fixed_time = '' WHERE id = ?",
                        (frequent_minutes, rule["id"]),
                    )
            self._conn.commit()

        step = self.get_step(step_id)
        if step and step["status"] == STEP_WAITING:
            self.schedule_step_reminder(step_id, advance=False)
        return True

    def get_checklist_items(self, step_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM step_checklist_items WHERE step_id = ? ORDER BY id",
                (step_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_step_status(self, step_id, new_status, result_note=""):
        """Меняет статус шага с проверкой зависимости и чек-листа."""
        step = self.get_step(step_id)
        if step is None:
            return False

        if new_status == STEP_DONE:
            self._assert_step_can_complete(step)

        with self._lock:
            self._conn.execute(
                "UPDATE steps SET status = ?, result_note = ? WHERE id = ?",
                (new_status, result_note, step_id),
            )
            if new_status == STEP_DONE:
                self._cancel_step_reminders(step_id)
            self._conn.commit()

        branch = self._get_branch(step["branch_id"])
        self._refresh_branch_step_states(branch["process_id"])
        return True

    def check_checklist_item(self, step_id, item_id, checked=True):
        """Отмечает пункт чек-листа. Последний пункт закрывает шаг."""
        step = self.get_step(step_id)
        if step is None:
            return False
        missing = self._unfinished_dependency_titles(step["id"])
        if missing:
            titles = ", ".join(f"«{title}»" for title in missing)
            raise StepDependencyError(f"Сначала завершите {titles}.")

        with self._lock:
            self._conn.execute(
                "UPDATE step_checklist_items SET is_checked = ? WHERE id = ? AND step_id = ?",
                (1 if checked else 0, item_id, step_id),
            )
            self._conn.commit()

        items = self.get_checklist_items(step_id)
        if items and all(item["is_checked"] for item in items):
            self.update_step_status(step_id, STEP_DONE)
            return True
        return False

    def activate_deferred_branch(self, branch_id):
        """Переводит спящую ветку в активную и открывает первый шаг."""
        with self._lock:
            branch = self._conn.execute(
                "SELECT * FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
            if branch is None:
                return False
            self._conn.execute(
                "UPDATE branches SET is_deferred = 0, status = ? WHERE id = ?",
                (BRANCH_ACTIVE, branch_id),
            )
            self._conn.commit()
        self._refresh_branch_step_states(branch["process_id"])
        return True

    def activate_due_deferred_branches(self):
        """Активирует отложенные ветки, чья дата уже наступила. Возвращает число."""
        today = date.today().strftime(DATE_FORMAT)
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT id FROM branches
                WHERE is_deferred = 1 AND activation_date IS NOT NULL
                  AND activation_date <= ?
                """,
                (today,),
            ).fetchall()
        count = 0
        for row in rows:
            if self.activate_deferred_branch(row["id"]):
                count += 1
        return count

    def ensure_waiting_step_reminders(self):
        """Для шагов «в ожидании» с правилами эскалации заводит живое напоминание."""
        created = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM steps WHERE status = ?", (STEP_WAITING,)
            ).fetchall()
        for row in rows:
            step = dict(row)
            if not self._step_has_escalation(step["id"]):
                continue
            if self._live_reminder_for_step(step["id"]):
                continue
            self.schedule_step_reminder(step["id"], advance=False)
            created += 1
        return created

    def postpone_step_escalation(self, step_id):
        """«Ещё нет»: оставляет шаг открытым и ставит следующее due_time."""
        step = self.get_step(step_id)
        if step is None:
            return False
        with self._lock:
            self._conn.execute(
                "UPDATE steps SET escalation_index = escalation_index + 1 WHERE id = ?",
                (step_id,),
            )
            self._conn.commit()
        self.schedule_step_reminder(step_id, advance=False)
        return True

    def schedule_step_reminder(self, step_id, advance=False):
        """Создаёт или двигает единственное активное напоминание шага."""
        step = self.get_step(step_id)
        if step is None:
            return None
        if advance:
            with self._lock:
                self._conn.execute(
                    "UPDATE steps SET escalation_index = escalation_index + 1 WHERE id = ?",
                    (step_id,),
                )
                self._conn.commit()
            step = self.get_step(step_id)

        due = self._compute_next_due(step)
        title = step["title"]
        description = step.get("result_note") or "Шаг процесса требует внимания."
        live = self._live_reminder_for_step(step_id)
        if live:
            with self._lock:
                self._conn.execute(
                    "UPDATE reminders SET due_time = ?, status = ?, title = ?, description = ? WHERE id = ?",
                    (to_db_format(due), STATUS_PENDING, title, description, live["id"]),
                )
                self._conn.execute(
                    "UPDATE steps SET reminder_id = ? WHERE id = ?",
                    (live["id"], step_id),
                )
                self._conn.commit()
            return live["id"]

        reminder_id = self.add_reminder(title, description, due, step_id=step_id)
        with self._lock:
            self._conn.execute(
                "UPDATE steps SET reminder_id = ? WHERE id = ?",
                (reminder_id, step_id),
            )
            self._conn.commit()
        return reminder_id

    def guard_threshold_reached(self, step_id):
        """Пора ли предлагать «подписать самостоятельно» (2 рабочих дня)."""
        step = self.get_step(step_id)
        if step is None or not step.get("waiting_since"):
            return False
        started = parse_dt(step["waiting_since"])
        if started is None:
            return False
        threshold = datetime.combine(
            add_workdays(started.date(), GUARD_SIGN_THRESHOLD_WORKDAYS),
            time(WORK_START_HOUR, 0),
        )
        return datetime.now() >= threshold

    def close(self):
        with self._lock:
            self._conn.close()

    # ----------------------------------------------------------- Internals

    def _dependency_ids(self, step_id):
        rows = self._conn.execute(
            """
            SELECT depends_on_step_id FROM step_dependencies
            WHERE step_id = ?
            """,
            (step_id,),
        ).fetchall()
        ids = [row["depends_on_step_id"] for row in rows if row["depends_on_step_id"]]
        if ids:
            return ids
        row = self._conn.execute(
            "SELECT depends_on_step_id FROM steps WHERE id = ?",
            (step_id,),
        ).fetchone()
        if row and row["depends_on_step_id"]:
            return [row["depends_on_step_id"]]
        return []

    def _unfinished_dependency_titles(self, step_id):
        with self._lock:
            titles = []
            for dep_id in self._dependency_ids(step_id):
                dep = self._conn.execute(
                    "SELECT title, status FROM steps WHERE id = ?",
                    (dep_id,),
                ).fetchone()
                if dep is not None and dep["status"] != STEP_DONE:
                    titles.append(dep["title"])
            return titles

    def _dependencies_satisfied_locked(self, step):
        for dep_id in self._dependency_ids(step["id"]):
            dep = self._conn.execute(
                "SELECT status FROM steps WHERE id = ?",
                (dep_id,),
            ).fetchone()
            if dep is None or dep["status"] != STEP_DONE:
                return False
        return True

    def _get_branch(self, branch_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM branches WHERE id = ?", (branch_id,)
            ).fetchone()
        return dict(row) if row else None

    def _assert_step_can_complete(self, step):
        missing = self._unfinished_dependency_titles(step["id"])
        if missing:
            titles = ", ".join(f"«{title}»" for title in missing)
            raise StepDependencyError(
                f"Нельзя закрыть шаг: сначала завершите {titles}."
            )
        if step["completion_type"] == COMPLETION_CHECKLIST:
            items = self.get_checklist_items(step["id"])
            if items and not all(item["is_checked"] for item in items):
                raise StepDependencyError(
                    "Шаг с чек-листом нельзя закрыть, пока не отмечены все пункты."
                )

    def _is_step_stale(self, step):
        if step["status"] not in (STEP_WAITING, STEP_OVERDUE):
            return step["status"] == STEP_OVERDUE
        if not step.get("waiting_since"):
            return False
        started = parse_dt(step["waiting_since"])
        if started is None:
            return False
        return datetime.now() - started >= timedelta(hours=STALE_WAITING_HOURS)

    def _step_has_escalation(self, step_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM escalation_rules WHERE step_id = ? AND is_active = 1",
                (step_id,),
            ).fetchone()
        return row[0] > 0

    def _live_reminder_for_step(self, step_id):
        with self._lock:
            row = self._conn.execute(
                """
                SELECT * FROM reminders
                WHERE step_id = ? AND status = ?
                ORDER BY id DESC LIMIT 1
                """,
                (step_id, STATUS_PENDING),
            ).fetchone()
        return dict(row) if row else None

    def _cancel_step_reminders(self, step_id):
        self._conn.execute(
            "UPDATE reminders SET status = ? WHERE step_id = ? AND status = ?",
            (STATUS_DONE, step_id, STATUS_PENDING),
        )

    def _compute_next_due(self, step):
        rules = self._escalation_rules(step["id"])
        until_hour, until_minute = parse_notify_until(step.get("notify_until"))
        if not rules:
            return clamp_to_work_hours(
                datetime.now() + timedelta(hours=2), until_hour, until_minute
            )
        index = min(max(step.get("escalation_index") or 0, 0), len(rules) - 1)
        rule = rules[index]
        now = datetime.now()
        if rule["interval_type"] == ESC_REPEAT or rule["fixed_time"]:
            hour, minute = parse_hhmm(rule.get("fixed_time") or "09:00")
            if (hour, minute) < (WORK_START_HOUR, 0):
                hour, minute = WORK_START_HOUR, 0
            if (hour, minute) > (until_hour, until_minute):
                hour, minute = until_hour, until_minute
            return next_workday_morning(now, hour, minute)
        minutes = int(rule["delay_minutes"] or 0) or (
            180 if rule["interval_type"] == ESC_FREQUENT else 120
        )
        return clamp_to_work_hours(
            now + timedelta(minutes=minutes), until_hour, until_minute
        )

    def _escalation_rules(self, step_id):
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT * FROM escalation_rules
                WHERE step_id = ? AND is_active = 1
                ORDER BY id
                """,
                (step_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def _refresh_branch_step_states(self, process_id):
        tree = None
        with self._lock:
            branches = self._conn.execute(
                "SELECT * FROM branches WHERE process_id = ? ORDER BY id",
                (process_id,),
            ).fetchall()
            for branch in branches:
                if branch["is_deferred"]:
                    continue
                steps = [
                    dict(row)
                    for row in self._conn.execute(
                        "SELECT * FROM steps WHERE branch_id = ? ORDER BY order_index",
                        (branch["id"],),
                    ).fetchall()
                ]
                all_done = True
                for step in steps:
                    dep_ok = self._dependencies_satisfied_locked(step)
                    if step["status"] == STEP_DONE:
                        continue
                    all_done = False
                    if not dep_ok:
                        if step["status"] != STEP_BLOCKED:
                            self._conn.execute(
                                "UPDATE steps SET status = ?, waiting_since = NULL WHERE id = ?",
                                (STEP_BLOCKED, step["id"]),
                            )
                        continue
                    if step["status"] in (STEP_WAITING, STEP_OVERDUE):
                        continue
                    self._conn.execute(
                        """
                        UPDATE steps
                        SET status = ?, waiting_since = COALESCE(waiting_since, ?)
                        WHERE id = ?
                        """,
                        (STEP_WAITING, now_str(), step["id"]),
                    )

                if all_done:
                    self._conn.execute(
                        "UPDATE branches SET status = ? WHERE id = ?",
                        (BRANCH_DONE, branch["id"]),
                    )
                elif not branch["is_deferred"]:
                    self._conn.execute(
                        "UPDATE branches SET status = ? WHERE id = ?",
                        (BRANCH_ACTIVE, branch["id"]),
                    )

            unfinished = self._conn.execute(
                """
                SELECT COUNT(*) FROM branches
                WHERE process_id = ? AND status != ?
                """,
                (process_id, BRANCH_DONE),
            ).fetchone()[0]
            self._conn.execute(
                "UPDATE processes SET status = ? WHERE id = ?",
                (PROCESS_DONE if unfinished == 0 else PROCESS_ACTIVE, process_id),
            )
            self._conn.commit()

        waiting = []
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT s.id FROM steps s
                JOIN branches b ON b.id = s.branch_id
                WHERE b.process_id = ? AND s.status = ?
                """,
                (process_id, STEP_WAITING),
            ).fetchall()
            waiting = [row["id"] for row in rows]
        for step_id in waiting:
            if self._step_has_escalation(step_id) and not self._live_reminder_for_step(step_id):
                self.schedule_step_reminder(step_id, advance=False)
        return tree
