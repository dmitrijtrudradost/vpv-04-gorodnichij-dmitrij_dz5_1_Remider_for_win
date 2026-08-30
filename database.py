"""Слой доступа к данным напоминалки: хранение напоминаний в SQLite3."""

import sqlite3
import threading
from datetime import datetime, timedelta

DB_FILENAME = "reminders.db"

DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"

STATUS_PENDING = "Ожидает"
STATUS_DONE = "Готово"
STATUS_OVERDUE = "Просрочено"
STATUS_CANCELLED = "Отменено"

ALL_STATUSES = (STATUS_PENDING, STATUS_DONE, STATUS_OVERDUE, STATUS_CANCELLED)

# Насколько напоминание должно «перезреть», прежде чем считать его просроченным.
OVERDUE_GRACE_SECONDS = 60


def now_str():
    """Текущее время в том же строковом формате, в котором оно лежит в базе."""
    return datetime.now().strftime(DATETIME_FORMAT)


def to_db_format(value):
    """Приводит datetime или строку к формату хранения."""
    if isinstance(value, datetime):
        return value.strftime(DATETIME_FORMAT)
    return str(value).strip()


class ReminderDatabase:
    """Все операции с таблицей напоминаний.

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
        """Создаёт таблицу и индекс, если их ещё нет."""
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
                """
            )
            self._conn.commit()

    def add_reminder(self, title, description, due_time):
        """Добавляет напоминание и возвращает его id."""
        with self._lock:
            cursor = self._conn.execute(
                """
                INSERT INTO reminders (title, description, due_time, status, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    title.strip(),
                    (description or "").strip(),
                    to_db_format(due_time),
                    STATUS_PENDING,
                    now_str(),
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
        """Напоминания, отсортированные по времени срабатывания.

        По умолчанию возвращает ожидающие — то есть ближайшее событие первым.
        """
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
        new_time = datetime.now() + timedelta(minutes=minutes)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reminders SET due_time = ?, status = ? WHERE id = ?",
                (to_db_format(new_time), STATUS_PENDING, reminder_id),
            )
            self._conn.commit()
            return cursor.rowcount > 0

    def mark_overdue(self):
        """Переводит в «Просрочено» ожидающие записи, опоздавшие больше минуты.

        Возвращает количество обновлённых записей.
        """
        threshold = datetime.now() - timedelta(seconds=OVERDUE_GRACE_SECONDS)
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE reminders SET status = ? WHERE status = ? AND due_time < ?",
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

    def close(self):
        with self._lock:
            self._conn.close()
