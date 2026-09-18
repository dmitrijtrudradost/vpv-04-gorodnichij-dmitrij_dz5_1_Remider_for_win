"""Графический интерфейс напоминалки на Tkinter."""

import threading
import tkinter as tk
from datetime import date, datetime, time, timedelta
from tkinter import messagebox, ttk

from tkcalendar import DateEntry

import database as db_module
from database import (
    BRANCH_DEFERRED,
    BRANCH_DONE,
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_OVERDUE,
    STATUS_PENDING,
    STEP_BLOCKED,
    STEP_DONE,
    STEP_OVERDUE,
    STEP_WAITING,
    USER_TEMPLATE_PREFIX,
    StepDependencyError,
)
from notifications import NotificationManager
from templates import (
    ACTION_CHECKLIST,
    GUARD_FIELDS,
    TEMPLATE_EDO,
    TEMPLATE_GUARD,
    TEMPLATE_LABELS,
)
import autostart

APP_VERSION = "2.2"

FILTER_ALL = "Все"
QUICK_MINUTES = (1, 5, 15, 30, 45, 60)
REFRESH_INTERVAL_MS = 2000
PISTACHIO = "#93C572"
PISTACHIO_ACTIVE = "#7eaf5f"
SNOOZE_MINUTES = 5

TIME_FIELDS = (("часы", 23), ("минуты", 59), ("секунды", 59))

STATUS_COLORS = {
    STATUS_PENDING: "#1a4f8a",
    STATUS_DONE: "#1f7a3d",
    STATUS_OVERDUE: "#b02020",
    STATUS_CANCELLED: "#7a7a7a",
}

try:
    import pystray
    from PIL import Image, ImageDraw

    TRAY_AVAILABLE = True
except Exception:
    pystray = None
    TRAY_AVAILABLE = False


class PersistentDateEntry(DateEntry):
    """DateEntry, у которого стрелки месяца и года не закрывают выпадающее окно.

    В tkcalendar клик по шапке календаря вызывает FocusOut, и штатный
    ``_on_focus_out_cal`` прячет Toplevel, пока кнопка ещё не сменила месяц.
    """

    def _pointer_over_calendar(self):
        if not self._top_cal.winfo_ismapped():
            return False
        x, y = self._top_cal.winfo_pointerxy()
        left = self._top_cal.winfo_rootx()
        top = self._top_cal.winfo_rooty()
        right = left + self._top_cal.winfo_width()
        bottom = top + self._top_cal.winfo_height()
        return left <= x <= right and top <= y <= bottom

    def _focus_inside_calendar(self):
        widget = self.focus_get()
        if widget is None:
            return False
        calendar_path = str(self._top_cal)
        return widget is self._calendar or str(widget).startswith(calendar_path + ".")

    def _on_focus_out_cal(self, event):
        if self._focus_inside_calendar() or self._pointer_over_calendar():
            self._calendar.focus_force()
            return
        self._top_cal.withdraw()
        self.state(["!pressed"])


EDITABLE_CLASSES = {"TEntry", "Entry", "Text", "TSpinbox", "Spinbox", "DateEntry"}


def _clipboard_widget(event):
    widget = event.widget
    try:
        if widget.winfo_class() not in EDITABLE_CLASSES:
            return None
    except tk.TclError:
        return None
    return widget


def _delete_selection(widget):
    try:
        widget.delete("sel.first", "sel.last")
    except tk.TclError:
        pass


def _insert_text(widget, text):
    for args in (("insert", text), (tk.INSERT, text)):
        try:
            widget.insert(*args)
            return
        except tk.TclError:
            continue
    try:
        widget.insert(widget.index("insert"), text)
    except tk.TclError:
        try:
            widget.insert("end", text)
        except tk.TclError:
            pass


def bind_clipboard_shortcuts(root):
    """ПКМ-меню и Ctrl+C/V/X/A у полей ввода (в т.ч. русская раскладка)."""

    def _copy(event):
        widget = _clipboard_widget(event)
        if widget is None:
            return None
        try:
            text = widget.selection_get()
        except tk.TclError:
            return "break"
        widget.clipboard_clear()
        widget.clipboard_append(text)
        try:
            root.clipboard_clear()
            root.clipboard_append(text)
        except tk.TclError:
            pass
        return "break"

    def _cut(event):
        if _copy(event) is None:
            return None
        widget = _clipboard_widget(event)
        if widget is not None:
            _delete_selection(widget)
        return "break"

    def _paste(event):
        widget = _clipboard_widget(event)
        if widget is None:
            return None
        text = ""
        try:
            text = widget.clipboard_get()
        except tk.TclError:
            try:
                text = root.clipboard_get()
            except tk.TclError:
                return "break"
        _delete_selection(widget)
        _insert_text(widget, text)
        return "break"

    def _select_all(event):
        widget = _clipboard_widget(event)
        if widget is None:
            return None
        try:
            widget.tag_add("sel", "1.0", "end-1c")
            return "break"
        except tk.TclError:
            pass
        try:
            widget.select_range(0, "end")
            widget.icursor("end")
        except tk.TclError:
            pass
        return "break"

    def _ctrl_key(event):
        key = (event.keysym or "").lower()
        mapping = {
            "c": _copy,
            "с": _copy,
            "v": _paste,
            "м": _paste,
            "x": _cut,
            "ч": _cut,
            "a": _select_all,
            "ф": _select_all,
        }
        handler = mapping.get(key)
        if handler is None:
            return None
        return handler(event)

    def _popup(event):
        widget = _clipboard_widget(event)
        if widget is None:
            return None
        menu = tk.Menu(widget, tearoff=0)
        menu.add_command(label="Вырезать", command=lambda: _cut(event))
        menu.add_command(label="Копировать", command=lambda: _copy(event))
        menu.add_command(label="Вставить", command=lambda: _paste(event))
        menu.add_separator()
        menu.add_command(label="Выделить всё", command=lambda: _select_all(event))
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    sequences = (
        ("<Control-c>", _copy),
        ("<Control-C>", _copy),
        ("<Control-v>", _paste),
        ("<Control-V>", _paste),
        ("<Control-x>", _cut),
        ("<Control-X>", _cut),
        ("<Control-a>", _select_all),
        ("<Control-A>", _select_all),
        ("<Control-KeyPress>", _ctrl_key),
        ("<Control-Insert>", _copy),
        ("<Shift-Insert>", _paste),
        ("<Shift-Delete>", _cut),
        ("<Button-3>", _popup),
    )
    for sequence, handler in sequences:
        root.bind_all(sequence, handler, add="+")
        for cls in EDITABLE_CLASSES:
            try:
                root.bind_class(cls, sequence, handler, add="+")
            except tk.TclError:
                pass


def _quick_label(minutes):
    if minutes == 60:
        return "+1 час"
    return f"+{minutes} мин"


def open_snooze_dialog(parent, db, reminder_id, on_done=None):
    """Диалог: +5 минут или произвольная дата/время."""
    reminder = db.get_reminder_by_id(reminder_id)
    if reminder is None:
        messagebox.showinfo("Нет записи", "Напоминание уже удалено.", parent=parent)
        return
    if reminder["status"] != STATUS_PENDING:
        messagebox.showinfo(
            "Нельзя отложить",
            "Отложить можно только напоминание со статусом «Ожидает».",
            parent=parent,
        )
        return

    win = tk.Toplevel(parent)
    win.title("Отложить напоминание")
    win.transient(parent)
    win.resizable(False, False)
    frame = ttk.Frame(win, padding=12)
    frame.pack(fill="both", expand=True)

    ttk.Label(
        frame,
        text=reminder["title"],
        font=("Segoe UI", 9, "bold"),
        wraplength=360,
    ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

    ttk.Label(frame, text="Дата и время:").grid(row=1, column=0, sticky="w", pady=4)
    time_row = ttk.Frame(frame)
    time_row.grid(row=1, column=1, columnspan=2, sticky="w", padx=(8, 0), pady=4)

    date_entry = PersistentDateEntry(
        time_row,
        width=12,
        locale="ru_RU",
        date_pattern="yyyy-mm-dd",
        mindate=date.today(),
        font=("Segoe UI", 9),
    )
    date_entry.pack(side="left")
    ttk.Label(time_row, text="в").pack(side="left", padx=8)

    hour_var = tk.StringVar()
    minute_var = tk.StringVar()
    second_var = tk.StringVar()
    target = datetime.now() + timedelta(minutes=SNOOZE_MINUTES)
    date_entry.set_date(target.date())
    hour_var.set(f"{target.hour:02d}")
    minute_var.set(f"{target.minute:02d}")
    second_var.set(f"{target.second:02d}")
    for index, (variable, (_name, limit)) in enumerate(
        zip((hour_var, minute_var, second_var), TIME_FIELDS)
    ):
        if index:
            ttk.Label(time_row, text=":").pack(side="left")
        ttk.Spinbox(
            time_row,
            from_=0,
            to=limit,
            textvariable=variable,
            width=3,
            wrap=True,
            format="%02.0f",
            font=("Segoe UI", 9),
        ).pack(side="left")

    def read_due():
        parts = []
        for variable, (name, limit) in zip(
            (hour_var, minute_var, second_var), TIME_FIELDS
        ):
            raw = variable.get().strip()
            if not raw.isdigit():
                raise ValueError(f"Поле «{name}» должно содержать число.")
            value = int(raw)
            if value > limit:
                raise ValueError(f"Поле «{name}» не может быть больше {limit}.")
            parts.append(value)
        return datetime.combine(date_entry.get_date(), time(*parts))

    def save():
        try:
            due = read_due()
            db.snooze_reminder_to(reminder_id, due)
        except ValueError as error:
            messagebox.showerror("Не удалось отложить", str(error), parent=win)
            return
        win.destroy()
        if on_done:
            on_done()

    def save_five():
        try:
            db.snooze_reminder(reminder_id, SNOOZE_MINUTES)
        except ValueError as error:
            messagebox.showerror("Не удалось отложить", str(error), parent=win)
            return
        win.destroy()
        if on_done:
            on_done()

    buttons = ttk.Frame(frame)
    buttons.grid(row=2, column=0, columnspan=3, sticky="e", pady=(14, 0))
    ttk.Button(buttons, text=f"На {SNOOZE_MINUTES} мин", command=save_five).pack(
        side="left", padx=2
    )
    ttk.Button(buttons, text="Отложить", command=save).pack(side="right", padx=2)
    ttk.Button(buttons, text="Отмена", command=win.destroy).pack(side="right", padx=2)

    win.grab_set()
    win.wait_window()


class ReminderApp:
    """Главное окно: список напоминаний, форма добавления и иконка в трее."""

    def __init__(self, db):
        self.db = db
        self.root = tk.Tk()
        self.notifier = NotificationManager(
            db,
            self.root,
            on_change=self.refresh_reminders,
            snooze_dialog=self.open_snooze_for_reminder,
        )

        self._tray_icon = None
        self._tray_thread = None
        self._tray_hint_shown = False
        self._refresh_job = None
        self._shutting_down = False

        self.setup_ui()
        self.refresh_reminders()

    # ------------------------------------------------------------------ UI

    def setup_ui(self):
        self.root.title(f"Напоминалка {APP_VERSION}")
        self.root.geometry("1100x680")
        self.root.minsize(900, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))
        bind_clipboard_shortcuts(self.root)

        notebook = ttk.Notebook(self.root)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        reminders_tab = ttk.Frame(notebook, padding=8)
        processes_tab = ttk.Frame(notebook, padding=8)
        notebook.add(reminders_tab, text="Напоминания")
        notebook.add(processes_tab, text="Процессы")

        self._build_form(reminders_tab)
        self._build_toolbar(reminders_tab)
        self._build_list(reminders_tab)
        self._build_process_form(processes_tab)
        self._build_process_tree(processes_tab)
        self._build_status_bar()

    def _build_form(self, parent):
        form = ttk.LabelFrame(parent, text="Новое напоминание", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Заголовок:").grid(row=0, column=0, sticky="w", pady=3)
        self.title_var = tk.StringVar()
        title_entry = ttk.Entry(form, textvariable=self.title_var)
        title_entry.grid(row=0, column=1, sticky="ew", padx=(8, 0), pady=3)
        title_entry.focus_set()

        ttk.Label(form, text="Описание:").grid(row=1, column=0, sticky="nw", pady=3)
        self.description_text = tk.Text(form, height=3, wrap="word", font=("Segoe UI", 9))
        self.description_text.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=3)

        ttk.Label(form, text="Дата и время:").grid(row=2, column=0, sticky="w", pady=3)

        time_row = ttk.Frame(form)
        time_row.grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=3)

        self.date_entry = PersistentDateEntry(
            time_row,
            width=12,
            locale="ru_RU",
            date_pattern="yyyy-mm-dd",
            mindate=date.today(),
            font=("Segoe UI", 9),
        )
        self.date_entry.pack(side="left")

        ttk.Label(time_row, text="в").pack(side="left", padx=8)

        self.hour_var = tk.StringVar()
        self.minute_var = tk.StringVar()
        self.second_var = tk.StringVar()
        time_vars = (self.hour_var, self.minute_var, self.second_var)

        for index, (variable, (_name, limit)) in enumerate(zip(time_vars, TIME_FIELDS)):
            if index:
                ttk.Label(time_row, text=":").pack(side="left")
            ttk.Spinbox(
                time_row,
                from_=0,
                to=limit,
                textvariable=variable,
                width=3,
                wrap=True,
                format="%02.0f",
                font=("Segoe UI", 9),
            ).pack(side="left")

        quick_row = ttk.Frame(form)
        quick_row.grid(row=3, column=1, sticky="w", padx=(8, 0), pady=(6, 0))

        ttk.Label(quick_row, text="Быстро:", foreground="#777777").pack(
            side="left", padx=(0, 6)
        )
        for minutes in QUICK_MINUTES:
            ttk.Button(
                quick_row,
                text=_quick_label(minutes),
                width=8,
                command=lambda m=minutes: self.set_quick_time(m),
            ).pack(side="left", padx=2)

        tk.Button(
            form,
            text="Добавить напоминание",
            command=self.add_reminder,
            bg=PISTACHIO,
            activebackground=PISTACHIO_ACTIVE,
            relief="raised",
            padx=10,
            pady=4,
            font=("Segoe UI", 9),
        ).grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(10, 0))

        self.set_quick_time(5)

    def _build_toolbar(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(12, 6))

        ttk.Label(toolbar, text="Фильтр по статусу:").pack(side="left")

        self.filter_var = tk.StringVar(value=FILTER_ALL)
        filter_box = ttk.Combobox(
            toolbar,
            textvariable=self.filter_var,
            values=(FILTER_ALL,) + db_module.ALL_STATUSES,
            state="readonly",
            width=14,
        )
        filter_box.pack(side="left", padx=(8, 16))
        filter_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_reminders())

        ttk.Button(toolbar, text="Обновить", command=self.refresh_reminders).pack(
            side="left", padx=2
        )
        ttk.Button(toolbar, text="Тест уведомления", command=self.test_notification).pack(
            side="left", padx=2
        )

        ttk.Button(toolbar, text="Удалить", command=self.delete_reminder).pack(
            side="right", padx=2
        )
        ttk.Button(toolbar, text="Отменить", command=self.mark_as_cancelled).pack(
            side="right", padx=2
        )
        ttk.Button(toolbar, text="Отметить готовым", command=self.mark_as_done).pack(
            side="right", padx=2
        )
        ttk.Button(toolbar, text="Отложить", command=self.snooze_selected_reminder).pack(
            side="right", padx=2
        )

    def _build_list(self, parent):
        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="both", expand=True)

        columns = ("id", "title", "due_time", "status", "description")
        self.tree = ttk.Treeview(
            wrapper, columns=columns, show="headings", selectmode="browse"
        )

        headings = {
            "id": ("ID", 50, "center"),
            "title": ("Заголовок", 220, "w"),
            "due_time": ("Сработает", 150, "center"),
            "status": ("Статус", 100, "center"),
            "description": ("Описание", 320, "w"),
        }
        for column, (text, width, anchor) in headings.items():
            self.tree.heading(column, text=text)
            self.tree.column(column, width=width, anchor=anchor)

        scrollbar = ttk.Scrollbar(wrapper, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        for status, color in STATUS_COLORS.items():
            self.tree.tag_configure(status, foreground=color)

        self.tree.bind("<Double-1>", self.on_double_click)

    def _build_status_bar(self):
        self.status_var = tk.StringVar()
        ttk.Label(
            self.root,
            textvariable=self.status_var,
            relief="sunken",
            anchor="w",
            padding=(8, 3),
        ).pack(fill="x", side="bottom")

    def _build_process_form(self, parent):
        form = ttk.LabelFrame(parent, text="Новый процесс", padding=10)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)

        ttk.Label(form, text="Шаблон:").grid(row=0, column=0, sticky="w", pady=3)
        self.template_var = tk.StringVar(value=TEMPLATE_LABELS[TEMPLATE_EDO])
        self.template_box = ttk.Combobox(
            form,
            textvariable=self.template_var,
            values=tuple(TEMPLATE_LABELS.values()),
            state="readonly",
            width=28,
        )
        self.template_box.grid(row=0, column=1, sticky="w", padx=(8, 0), pady=3)
        self.template_box.bind("<<ComboboxSelected>>", lambda _e: self._on_template_change())
        ttk.Button(
            form, text="Конструктор процессов", command=self.open_process_builder
        ).grid(row=0, column=2, sticky="w", padx=(8, 0), pady=3)
        self.delete_template_btn = ttk.Button(
            form, text="Удалить шаблон", command=self.delete_selected_template
        )
        self.delete_template_btn.grid(row=0, column=3, sticky="w", padx=(8, 0), pady=3)

        ttk.Label(form, text="Название:").grid(row=1, column=0, sticky="w", pady=3)
        self.process_title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.process_title_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=3
        )

        self._guard_open = False
        self.guard_wrap = ttk.Frame(form)
        self.guard_wrap.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.guard_wrap.columnconfigure(0, weight=1)
        self.guard_toggle_btn = ttk.Button(
            self.guard_wrap,
            text="Показать/Скрыть данные договора охраны",
            command=self._toggle_guard_fields,
        )
        self.guard_toggle_btn.grid(row=0, column=0, sticky="w")
        self.guard_frame = ttk.Frame(self.guard_wrap, padding=(0, 6, 0, 0))
        self.guard_frame.grid(row=1, column=0, sticky="ew")
        self.guard_frame.columnconfigure(1, weight=1)
        self.guard_vars = {}
        for index, (key, label) in enumerate(GUARD_FIELDS):
            ttk.Label(self.guard_frame, text=f"{label}:").grid(
                row=index, column=0, sticky="w", pady=2
            )
            var = tk.StringVar()
            self.guard_vars[key] = var
            ttk.Entry(self.guard_frame, textvariable=var).grid(
                row=index, column=1, sticky="ew", padx=(8, 0), pady=2
            )
        self._sync_guard_fields()

        ttk.Button(form, text="Создать процесс", command=self.create_process).grid(
            row=3, column=1, sticky="e", pady=(10, 0)
        )
        self.refresh_template_choices()
        self._on_template_change()

    def _build_process_tree(self, parent):
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(10, 6))
        ttk.Button(toolbar, text="Обновить процессы", command=self.refresh_processes).pack(
            side="left", padx=2
        )
        ttk.Button(
            toolbar, text="Отметить шаг выполненным", command=self.complete_selected_step
        ).pack(side="left", padx=2)
        ttk.Button(
            toolbar, text="Открыть чек-лист", command=self.open_selected_checklist
        ).pack(side="left", padx=2)
        ttk.Button(
            toolbar,
            text="Расписание уведомлений",
            command=self.edit_selected_notify_schedule,
        ).pack(side="left", padx=2)
        ttk.Button(
            toolbar,
            text="Завершить процесс",
            command=self.finish_selected_process,
        ).pack(side="left", padx=2)

        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="both", expand=True)
        columns = ("status", "detail", "notify")
        self.process_tree = ttk.Treeview(
            wrapper, columns=columns, show="tree headings", selectmode="browse"
        )
        self.process_tree.heading("#0", text="Процесс / ветка / шаг")
        self.process_tree.heading("status", text="Статус")
        self.process_tree.heading("detail", text="Подробности")
        self.process_tree.heading("notify", text="Уведомления")
        self.process_tree.column("#0", width=360)
        self.process_tree.column("status", width=110, anchor="center")
        self.process_tree.column("detail", width=200)
        self.process_tree.column("notify", width=280)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scroll.set)
        self.process_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.process_tree.tag_configure("waiting", foreground="#1a4f8a")
        self.process_tree.tag_configure("done", foreground="#1f7a3d")
        self.process_tree.tag_configure("stale", foreground="#b02020")
        self.process_tree.tag_configure("blocked", foreground="#7a7a7a")
        self.process_tree.tag_configure("deferred", foreground="#7a7a7a")
        self.process_tree.bind("<Double-1>", self.on_process_tree_double_click)

    def _on_template_change(self):
        label = self.template_var.get()
        is_guard = label == TEMPLATE_LABELS[TEMPLATE_GUARD]
        if is_guard:
            self.guard_wrap.grid()
            self._sync_guard_fields()
        else:
            self._guard_open = False
            self.guard_wrap.grid_remove()
            self._sync_guard_fields()
        is_user = str(self._selected_template_type()).startswith(USER_TEMPLATE_PREFIX)
        if hasattr(self, "delete_template_btn"):
            self.delete_template_btn.state(["!disabled"] if is_user else ["disabled"])

    def _sync_guard_fields(self):
        if not hasattr(self, "guard_frame"):
            return
        if self._guard_open:
            self.guard_frame.grid()
        else:
            self.guard_frame.grid_remove()

    def _toggle_guard_fields(self):
        self._guard_open = not self._guard_open
        self._sync_guard_fields()

    def refresh_template_choices(self):
        choices = list(TEMPLATE_LABELS.items())
        for row in self.db.list_process_templates():
            choices.append((f"{USER_TEMPLATE_PREFIX}{row['id']}", row["name"]))
        self._template_choices = choices
        labels = [label for _key, label in choices]
        current = self.template_var.get()
        self.template_box["values"] = labels
        if current not in labels:
            self.template_var.set(labels[0] if labels else "")
            self._on_template_change()

    def _template_display_name(self, template_type):
        if template_type in TEMPLATE_LABELS:
            return TEMPLATE_LABELS[template_type]
        if str(template_type).startswith(USER_TEMPLATE_PREFIX):
            raw = str(template_type)[len(USER_TEMPLATE_PREFIX) :]
            try:
                row = self.db.get_process_template(int(raw))
            except ValueError:
                row = None
            if row:
                return row["name"]
        return template_type or ""

    def _selected_template_type(self):
        label = self.template_var.get()
        for key, value in getattr(self, "_template_choices", TEMPLATE_LABELS.items()):
            if value == label:
                return key
        return TEMPLATE_EDO

    def open_process_builder(self):
        try:
            from process_builder import open_process_builder
            open_process_builder(
                self.db,
                on_saved=lambda: self.root.after(0, self.refresh_template_choices),
            )
        except Exception as error:
            messagebox.showerror(
                "Конструктор процессов",
                f"Не удалось открыть конструктор: {error}\n"
                "Установите зависимость pywebview: pip install -r requirements.txt",
            )

    def create_process(self):
        template_type = self._selected_template_type()
        title = self.process_title_var.get().strip()
        metadata = {}
        if template_type == TEMPLATE_GUARD:
            for key, label in GUARD_FIELDS:
                value = self.guard_vars[key].get().strip()
                if not value:
                    messagebox.showwarning("Пустое поле", f"Заполните поле «{label}».")
                    return
                metadata[key] = value
        try:
            self.db.create_process(template_type, title, metadata)
        except ValueError as error:
            messagebox.showerror("Не удалось создать процесс", str(error))
            return
        self.process_title_var.set("")
        self.refresh_processes()
        messagebox.showinfo("Процесс создан", "Процесс добавлен на вкладку «Процессы».")

    def refresh_processes(self):
        if self._shutting_down or not hasattr(self, "process_tree"):
            return
        if hasattr(self, "template_box"):
            self.refresh_template_choices()
        selected = self.process_tree.selection()
        for item in self.process_tree.get_children():
            self.process_tree.delete(item)

        for process in self.db.get_active_processes():
            process_iid = f"p-{process['id']}"
            self.process_tree.insert(
                "",
                "end",
                iid=process_iid,
                text=process["title"],
                values=(
                    process["status"],
                    TEMPLATE_LABELS.get(process["template_type"])
                    or self._template_display_name(process["template_type"]),
                    "",
                ),
                open=True,
            )
            for branch in process["branches"]:
                branch_iid = f"b-{branch['id']}"
                extra = ""
                if branch["is_deferred"] and branch.get("activation_date"):
                    extra = f"активация {branch['activation_date']}"
                tag = "deferred" if branch["status"] == BRANCH_DEFERRED else ""
                if branch["status"] == BRANCH_DONE:
                    tag = "done"
                self.process_tree.insert(
                    process_iid,
                    "end",
                    iid=branch_iid,
                    text=branch["title"],
                    values=(branch["status"], extra, ""),
                    tags=(tag,) if tag else (),
                    open=not branch["is_deferred"],
                )
                for step in branch["steps"]:
                    detail = step.get("result_note") or step.get("completion_type")
                    if step["status"] == STEP_WAITING and step.get("next_due"):
                        detail = f"сработает {step['next_due']}"
                    if step["status"] == STEP_OVERDUE or step.get("stale"):
                        tag = "stale"
                    elif step["status"] == STEP_DONE:
                        tag = "done"
                    elif step["status"] == STEP_BLOCKED:
                        tag = "blocked"
                    elif step["status"] == STEP_WAITING:
                        tag = "waiting"
                    else:
                        tag = ""
                    self.process_tree.insert(
                        branch_iid,
                        "end",
                        iid=f"s-{step['id']}",
                        text=step["title"],
                        values=(
                            step["status"],
                            detail,
                            step.get("notify_label") or "—",
                        ),
                        tags=(tag,) if tag else (),
                    )

        if selected and self.process_tree.exists(selected[0]):
            self.process_tree.selection_set(selected[0])

    def _selected_step_id(self):
        selection = self.process_tree.selection()
        if not selection:
            return None
        iid = selection[0]
        if not iid.startswith("s-"):
            return None
        return int(iid.split("-", 1)[1])

    def _selected_process_id(self):
        selection = self.process_tree.selection()
        if not selection:
            return None
        iid = selection[0]
        while iid:
            if iid.startswith("p-"):
                return int(iid.split("-", 1)[1])
            iid = self.process_tree.parent(iid)
        return None

    def finish_selected_process(self):
        process_id = self._selected_process_id()
        if process_id is None:
            messagebox.showinfo(
                "Ничего не выбрано",
                "Выберите процесс (или его ветку/шаг) в дереве.",
            )
            return
        tree = self.db.get_process_tree(process_id)
        if tree is None:
            return
        title = tree.get("title") or f"#{process_id}"
        if not messagebox.askyesno(
            "Завершить процесс",
            f"Завершить процесс «{title}»?\n\n"
            "Все оставшиеся шаги будут отмечены выполненными, "
            "пустые ветки закроются, напоминания шагов снимутся.",
        ):
            return
        self.db.finish_process(process_id)
        self.refresh_processes()
        self.refresh_reminders()

    def delete_selected_template(self):
        template_type = self._selected_template_type()
        if not str(template_type).startswith(USER_TEMPLATE_PREFIX):
            messagebox.showinfo(
                "Нельзя удалить",
                "Встроенные шаблоны «ЭДО-договор» и «Охрана объекта» удалить нельзя.",
            )
            return
        raw_id = str(template_type)[len(USER_TEMPLATE_PREFIX) :]
        try:
            template_id = int(raw_id)
        except ValueError:
            return
        name = self.template_var.get()
        if not messagebox.askyesno(
            "Удалить шаблон",
            f"Удалить шаблон «{name}»?\nЗапущенные процессы по нему останутся.",
        ):
            return
        self.db.delete_process_template(template_id)
        self.refresh_template_choices()

    def complete_selected_step(self):
        step_id = self._selected_step_id()
        if step_id is None:
            messagebox.showinfo("Ничего не выбрано", "Выберите шаг в дереве процесса.")
            return
        step = self.db.get_step(step_id)
        if step is None:
            return
        if step.get("action_kind") == ACTION_CHECKLIST:
            self.notifier.show_checklist(step_id)
            self.refresh_processes()
            return
        try:
            self.db.update_step_status(step_id, STEP_DONE)
        except StepDependencyError as error:
            messagebox.showwarning("Шаг заблокирован", str(error))
            return
        self.refresh_processes()
        self.refresh_reminders()

    def open_selected_checklist(self):
        step_id = self._selected_step_id()
        if step_id is None:
            messagebox.showinfo("Ничего не выбрано", "Выберите шаг с чек-листом.")
            return
        step = self.db.get_step(step_id)
        if step is None or step.get("action_kind") != ACTION_CHECKLIST:
            messagebox.showinfo("Это не чек-лист", "У выбранного шага нет чек-листа.")
            return
        self.notifier.show_checklist(step_id)
        self.refresh_processes()

    def on_process_tree_double_click(self, _event=None):
        if self._selected_step_id() is None:
            return
        self.edit_selected_notify_schedule()

    def edit_selected_notify_schedule(self):
        step_id = self._selected_step_id()
        if step_id is None:
            messagebox.showinfo("Ничего не выбрано", "Выберите шаг в дереве процесса.")
            return
        settings = self.db.get_step_escalation(step_id)
        if settings is None:
            messagebox.showinfo(
                "Нет уведомлений",
                "У этого шага нет расписания уведомлений, править нечего.",
            )
            return
        self._open_escalation_dialog(step_id, settings)

    def _open_escalation_dialog(self, step_id, settings):
        step = self.db.get_step(step_id)
        win = tk.Toplevel(self.root)
        win.title("Расписание уведомлений")
        win.transient(self.root)
        win.resizable(False, False)
        frame = ttk.Frame(win, padding=12)
        frame.pack(fill="both", expand=True)

        ttk.Label(
            frame,
            text=step["title"] if step else "Шаг",
            font=("Segoe UI", 9, "bold"),
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        first_var = tk.StringVar(value=str(settings["first_hours"]))
        ttk.Label(frame, text="Первое через, часов:").grid(
            row=1, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            frame,
            from_=1,
            to=48,
            textvariable=first_var,
            width=6,
            wrap=True,
        ).grid(row=1, column=1, sticky="w", padx=(8, 0), pady=4)

        hour, minute = db_module.parse_hhmm(settings["repeat_time"])
        hour_var = tk.StringVar(value=f"{hour:02d}")
        minute_var = tk.StringVar(value=f"{minute:02d}")
        ttk.Label(frame, text="Повтор в следующий рабочий день в:").grid(
            row=2, column=0, sticky="w", pady=4
        )
        time_row = ttk.Frame(frame)
        time_row.grid(row=2, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Spinbox(
            time_row, from_=9, to=23, textvariable=hour_var, width=3, wrap=True, format="%02.0f"
        ).pack(side="left")
        ttk.Label(time_row, text=":").pack(side="left")
        ttk.Spinbox(
            time_row, from_=0, to=59, textvariable=minute_var, width=3, wrap=True, format="%02.0f"
        ).pack(side="left")

        freq_var = tk.StringVar(value=str(settings["frequent_hours"]))
        ttk.Label(frame, text="Дальше каждые, часов:").grid(
            row=3, column=0, sticky="w", pady=4
        )
        ttk.Spinbox(
            frame,
            from_=1,
            to=12,
            textvariable=freq_var,
            width=6,
            wrap=True,
        ).grid(row=3, column=1, sticky="w", padx=(8, 0), pady=4)

        until_hour, until_minute = db_module.parse_notify_until(settings.get("notify_until"))
        until_hour_var = tk.StringVar(value=f"{until_hour:02d}")
        until_minute_var = tk.StringVar(value=f"{until_minute:02d}")
        ttk.Label(frame, text="Уведомлять до:").grid(row=4, column=0, sticky="w", pady=4)
        until_row = ttk.Frame(frame)
        until_row.grid(row=4, column=1, sticky="w", padx=(8, 0), pady=4)
        ttk.Spinbox(
            until_row,
            from_=9,
            to=23,
            textvariable=until_hour_var,
            width=3,
            wrap=True,
            format="%02.0f",
        ).pack(side="left")
        ttk.Label(until_row, text=":").pack(side="left")
        ttk.Spinbox(
            until_row,
            from_=0,
            to=59,
            textvariable=until_minute_var,
            width=3,
            wrap=True,
            format="%02.0f",
        ).pack(side="left")
        ttk.Label(
            frame,
            text="пн–пт, с 09:00 (по умолчанию до 18:00)",
            foreground="#777777",
        ).grid(row=4, column=2, sticky="w", padx=(8, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, columnspan=3, sticky="e", pady=(14, 0))

        def save():
            try:
                first_hours = int(first_var.get().strip())
                frequent_hours = int(freq_var.get().strip())
                stamp = f"{int(hour_var.get()):02d}:{int(minute_var.get()):02d}"
                until_stamp = (
                    f"{int(until_hour_var.get()):02d}:{int(until_minute_var.get()):02d}"
                )
                self.db.update_step_escalation(
                    step_id,
                    first_hours * 60,
                    stamp,
                    frequent_hours * 60,
                    until_stamp,
                )
            except ValueError as error:
                messagebox.showerror("Не удалось сохранить", str(error), parent=win)
                return
            win.destroy()
            self.refresh_processes()
            self.refresh_reminders()

        ttk.Button(buttons, text="Сохранить", command=save).pack(side="right", padx=2)
        ttk.Button(buttons, text="Отмена", command=win.destroy).pack(side="right", padx=2)

        win.grab_set()
        win.wait_window()

    # -------------------------------------------------------------- Actions

    def set_quick_time(self, minutes):
        """Выставляет в календаре и спинбоксах «сейчас + minutes минут»."""
        target = datetime.now() + timedelta(minutes=minutes)
        self.date_entry.set_date(target.date())
        self.hour_var.set(f"{target.hour:02d}")
        self.minute_var.set(f"{target.minute:02d}")
        self.second_var.set(f"{target.second:02d}")

    def _read_due_time(self):
        """Собирает дату из календаря и время из спинбоксов.

        Спинбоксы допускают ручной ввод, поэтому значения всё же проверяются.
        """
        parts = []
        variables = (self.hour_var, self.minute_var, self.second_var)
        for variable, (name, limit) in zip(variables, TIME_FIELDS):
            raw = variable.get().strip()
            if not raw.isdigit():
                raise ValueError(f"Поле «{name}» должно содержать число.")
            value = int(raw)
            if value > limit:
                raise ValueError(f"Поле «{name}» не может быть больше {limit}.")
            parts.append(value)

        return datetime.combine(self.date_entry.get_date(), time(*parts))

    def add_reminder(self):
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("Пустой заголовок", "Введите заголовок напоминания.")
            return

        try:
            due_time = self._read_due_time()
        except ValueError as error:
            messagebox.showerror("Неверное время", str(error))
            return

        description = self.description_text.get("1.0", "end").strip()
        self.db.add_reminder(title, description, due_time)

        self.title_var.set("")
        self.description_text.delete("1.0", "end")
        self.set_quick_time(5)
        self.refresh_reminders()

    def refresh_reminders(self):
        """Перечитывает список из базы с учётом фильтра.

        Вызывается и по таймеру, и из фонового потока (через очередь), поэтому
        сохраняет выделенную строку, чтобы список не «прыгал» под курсором.
        """
        if self._shutting_down:
            return

        selected = self._selected_id()

        for item in self.tree.get_children():
            self.tree.delete(item)

        status_filter = self.filter_var.get()
        status = None if status_filter == FILTER_ALL else status_filter

        for reminder in self.db.get_all_reminders(status):
            description = (reminder["description"] or "").replace("\n", " ")
            self.tree.insert(
                "",
                "end",
                iid=str(reminder["id"]),
                values=(
                    reminder["id"],
                    reminder["title"],
                    reminder["due_time"],
                    reminder["status"],
                    description,
                ),
                tags=(reminder["status"],),
            )

        if selected is not None and self.tree.exists(str(selected)):
            self.tree.selection_set(str(selected))

        self.update_status_bar()
        self.refresh_processes()
        self._schedule_refresh()

    def _schedule_refresh(self):
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
        self._refresh_job = self.root.after(REFRESH_INTERVAL_MS, self.refresh_reminders)

    def update_status_bar(self):
        counts = self.db.get_counts_by_status()
        total = sum(counts.values())
        self.status_var.set(
            f"Всего: {total}    "
            f"Ожидает: {counts[STATUS_PENDING]}    "
            f"Готово: {counts[STATUS_DONE]}    "
            f"Просрочено: {counts[STATUS_OVERDUE]}    "
            f"Отменено: {counts[STATUS_CANCELLED]}    "
            f"|  Уведомления: {self.notifier.backend_name}"
        )

    def _selected_id(self):
        selection = self.tree.selection()
        if not selection:
            return None
        return int(selection[0])

    def _require_selection(self):
        reminder_id = self._selected_id()
        if reminder_id is None:
            messagebox.showinfo("Ничего не выбрано", "Выберите напоминание в списке.")
        return reminder_id

    def mark_as_done(self):
        reminder_id = self._require_selection()
        if reminder_id is None:
            return
        self.db.update_status(reminder_id, STATUS_DONE)
        self.refresh_reminders()

    def snooze_selected_reminder(self):
        reminder_id = self._require_selection()
        if reminder_id is None:
            return
        self.open_snooze_for_reminder(reminder_id)

    def open_snooze_for_reminder(self, reminder_id, parent=None):
        open_snooze_dialog(
            parent or self.root,
            self.db,
            reminder_id,
            on_done=self.refresh_reminders,
        )

    def mark_as_cancelled(self):
        reminder_id = self._require_selection()
        if reminder_id is None:
            return
        self.db.update_status(reminder_id, STATUS_CANCELLED)
        self.refresh_reminders()

    def delete_reminder(self):
        reminder_id = self._require_selection()
        if reminder_id is None:
            return

        reminder = self.db.get_reminder_by_id(reminder_id)
        if reminder is None:
            self.refresh_reminders()
            return

        if messagebox.askyesno(
            "Удаление", f"Удалить напоминание «{reminder['title']}»?"
        ):
            self.db.delete_reminder(reminder_id)
            self.refresh_reminders()

    def on_double_click(self, _event):
        """Показывает подробности напоминания по двойному клику."""
        reminder_id = self._selected_id()
        if reminder_id is None:
            return

        reminder = self.db.get_reminder_by_id(reminder_id)
        if reminder is None:
            self.refresh_reminders()
            return

        description = (reminder["description"] or "").strip() or "(без описания)"
        messagebox.showinfo(
            f"Напоминание #{reminder['id']}",
            f"Заголовок: {reminder['title']}\n"
            f"Статус: {reminder['status']}\n"
            f"Сработает: {reminder['due_time']}\n"
            f"Создано: {reminder['created_at']}\n\n"
            f"Описание:\n{description}",
        )

    def test_notification(self):
        self.notifier.test_notification()

    # ------------------------------------------------------------------ Tray

    def _setup_tray(self):
        """Иконка в трее, чтобы приложение жило при закрытом окне."""
        if not TRAY_AVAILABLE:
            return

        image = Image.new("RGB", (64, 64), "#1a4f8a")
        draw = ImageDraw.Draw(image)
        draw.ellipse((12, 10, 52, 50), fill="#ffffff")
        draw.line((32, 30, 32, 18), fill="#1a4f8a", width=4)
        draw.line((32, 30, 43, 34), fill="#1a4f8a", width=4)

        menu = pystray.Menu(
            pystray.MenuItem("Открыть", self._tray_show, default=True),
            pystray.MenuItem("Тест уведомления", self._tray_test),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Запускать при входе в Windows", self._tray_enable_autostart
            ),
            pystray.MenuItem("Не запускать при входе", self._tray_disable_autostart),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self._tray_exit),
        )

        self._tray_icon = pystray.Icon("Напоминалка", image, "Напоминалка", menu)
        self._tray_thread = threading.Thread(
            target=self._tray_icon.run, name="tray-icon", daemon=True
        )
        self._tray_thread.start()

    def _tray_show(self, *_args):
        self.root.after(0, self._restore_window)

    def _restore_window(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def _tray_test(self, *_args):
        self.notifier.test_notification()

    def _tray_enable_autostart(self, *_args):
        self.root.after(0, self._enable_autostart)

    def _tray_disable_autostart(self, *_args):
        self.root.after(0, self._disable_autostart)

    def _enable_autostart(self):
        try:
            path = autostart.enable()
            messagebox.showinfo(
                "Автозапуск",
                f"Ярлык создан:\n{path}\n\nПрограмма будет стартовать при входе в Windows.",
            )
        except Exception as error:
            messagebox.showerror("Автозапуск", str(error))

    def _disable_autostart(self):
        try:
            autostart.disable()
            messagebox.showinfo("Автозапуск", "Ярлык из папки «Автозагрузка» удалён.")
        except Exception as error:
            messagebox.showerror("Автозапуск", str(error))

    def _tray_exit(self, *_args):
        self.root.after(0, self.quit_app)

    # -------------------------------------------------------------- Lifecycle

    def on_closing(self):
        """Крестик сворачивает приложение в трей, а не завершает его.

        Иначе фоновый мониторинг остановится и напоминания не сработают.
        """
        if self._tray_icon is not None:
            self.root.withdraw()
            if not self._tray_hint_shown:
                self._tray_hint_shown = True
                self.notifier.show_manual_notification(
                    "Напоминалка работает в трее",
                    "Окно свёрнуто, напоминания продолжат приходить. "
                    "Полный выход — через меню иконки в трее.",
                )
            return

        # Без трея сворачивать некуда, поэтому спрашиваем про выход.
        if messagebox.askyesno(
            "Выход",
            "Вы уверены, что хотите выйти?\nНапоминания перестанут приходить.",
        ):
            self.quit_app()

    def quit_app(self):
        if self._shutting_down:
            return
        self._shutting_down = True

        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
            self._refresh_job = None

        self.notifier.stop()

        if self._tray_icon is not None:
            try:
                self._tray_icon.stop()
            except Exception:
                pass
            self._tray_icon = None

        # Окно может быть уже уничтожено, если mainloop прервался с ошибкой.
        try:
            self.root.quit()
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self, start_in_tray=False):
        self._setup_tray()
        if start_in_tray and self._tray_icon is not None:
            self.root.withdraw()
            self._tray_hint_shown = True
        self.notifier.start()
        self.root.mainloop()
