"""Окно конструктора процессов на pywebview + React Flow."""

import json
import os
import subprocess
import sys
from pathlib import Path

from templates import (
    COMPLETION_CHECKLIST,
    COMPLETION_MANUAL,
    COMPLETION_WAIT,
    DEFAULT_ESCALATION,
    TEMPLATES,
    TEMPLATE_LABELS,
)

ROOT = Path(__file__).resolve().parent
DIST_INDEX = ROOT / "process_builder_web" / "dist" / "index.html"


def _jsonable(value):
    return json.loads(json.dumps(value, ensure_ascii=False))


class ProcessBuilderApi:
    """Мост Python ↔ JavaScript для окна конструктора."""

    def __init__(self, db, on_saved=None):
        self.db = db
        self.on_saved = on_saved

    def defaults(self):
        return {
            "completion_manual": COMPLETION_MANUAL,
            "completion_checklist": COMPLETION_CHECKLIST,
            "completion_wait": COMPLETION_WAIT,
            "default_escalation": _jsonable(list(DEFAULT_ESCALATION)),
            "notify_until": "18:00",
            "repeat_time": "09:00",
        }

    def list_builtins(self):
        return [
            {"key": key, "name": TEMPLATE_LABELS.get(key, key), "readonly": True}
            for key in TEMPLATES
        ]

    def get_builtin(self, key):
        spec = TEMPLATES.get(key)
        if spec is None:
            return None
        return _jsonable(spec)

    def list_templates(self):
        rows = []
        for item in self.db.list_process_templates():
            rows.append(
                {
                    "id": item["id"],
                    "name": item["name"],
                    "key": f"user:{item['id']}",
                    "readonly": False,
                }
            )
        return rows

    def get_user_template(self, template_id):
        row = self.db.get_process_template(int(template_id))
        if row is None:
            return None
        return {
            "id": row["id"],
            "name": row["name"],
            "structure": json.loads(row["structure_json"] or "{}"),
            "readonly": False,
        }

    def save_template(self, name, structure, template_id=None):
        spec = structure if isinstance(structure, dict) else json.loads(structure)
        if template_id not in (None, "", 0, "0"):
            saved_id = self.db.update_process_template(int(template_id), name, spec)
        else:
            saved_id = self.db.save_process_template(name, spec)
        if self.on_saved:
            try:
                self.on_saved()
            except Exception:
                pass
        return {"id": saved_id, "key": f"user:{saved_id}"}


def open_process_builder(db=None, on_saved=None):
    """Запускает конструктор отдельным процессом: pywebview требует главный поток."""
    if not DIST_INDEX.exists():
        raise FileNotFoundError(
            f"Не найден {DIST_INDEX}. Собранная веб-часть должна лежать в process_builder_web/dist."
        )
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NO_WINDOW
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--window"],
        cwd=str(ROOT),
        creationflags=creationflags,
        close_fds=False,
    )
    if on_saved:
        try:
            on_saved()
        except Exception:
            pass


def _run_window():
    try:
        import webview
        from database import ReminderDatabase
    except Exception as error:
        _show_error(str(error))
        return
    db = ReminderDatabase()
    api = ProcessBuilderApi(db)
    url = DIST_INDEX.resolve().as_uri()
    try:
        webview.create_window(
            "Конструктор процессов",
            url,
            js_api=api,
            width=1200,
            height=800,
            min_size=(900, 600),
        )
        webview.start()
    except Exception as error:
        _show_error(str(error))
    finally:
        db.close()


def _show_error(message):
    import tkinter as tk
    from tkinter import messagebox

    root = tk.Tk()
    root.withdraw()
    messagebox.showerror("Конструктор процессов", message)
    root.destroy()


if __name__ == "__main__":
    if "--window" in sys.argv:
        _run_window()
    else:
        sys.exit("Использование: python process_builder.py --window")
