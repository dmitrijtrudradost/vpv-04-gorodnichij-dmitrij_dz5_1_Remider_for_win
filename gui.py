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

APP_VERSION = "2.0"

FILTER_ALL = "Все"
QUICK_MINUTES = (1, 5, 15, 30)
REFRESH_INTERVAL_MS = 2000

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


class ReminderApp:
    """Главное окно: список напоминаний, форма добавления и иконка в трее."""

    def __init__(self, db):
        self.db = db
        self.root = tk.Tk()
        self.notifier = NotificationManager(
            db, self.root, on_change=self.refresh_reminders
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
        self.root.geometry("960x680")
        self.root.minsize(800, 560)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

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
            state="readonly",
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
                text=f"+{minutes} мин",
                width=8,
                command=lambda m=minutes: self.set_quick_time(m),
            ).pack(side="left", padx=2)

        ttk.Button(form, text="Добавить напоминание", command=self.add_reminder).grid(
            row=4, column=1, sticky="e", padx=(8, 0), pady=(10, 0)
        )

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

        ttk.Label(form, text="Название:").grid(row=1, column=0, sticky="w", pady=3)
        self.process_title_var = tk.StringVar()
        ttk.Entry(form, textvariable=self.process_title_var).grid(
            row=1, column=1, sticky="ew", padx=(8, 0), pady=3
        )

        self.guard_frame = ttk.LabelFrame(form, text="Данные договора охраны", padding=8)
        self.guard_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
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

        ttk.Button(form, text="Создать процесс", command=self.create_process).grid(
            row=3, column=1, sticky="e", pady=(10, 0)
        )
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

        wrapper = ttk.Frame(parent)
        wrapper.pack(fill="both", expand=True)
        columns = ("status", "detail")
        self.process_tree = ttk.Treeview(
            wrapper, columns=columns, show="tree headings", selectmode="browse"
        )
        self.process_tree.heading("#0", text="Процесс / ветка / шаг")
        self.process_tree.heading("status", text="Статус")
        self.process_tree.heading("detail", text="Подробности")
        self.process_tree.column("#0", width=420)
        self.process_tree.column("status", width=130, anchor="center")
        self.process_tree.column("detail", width=280)
        scroll = ttk.Scrollbar(wrapper, orient="vertical", command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scroll.set)
        self.process_tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.process_tree.tag_configure("waiting", foreground="#1a4f8a")
        self.process_tree.tag_configure("done", foreground="#1f7a3d")
        self.process_tree.tag_configure("stale", foreground="#b02020")
        self.process_tree.tag_configure("blocked", foreground="#7a7a7a")
        self.process_tree.tag_configure("deferred", foreground="#7a7a7a")

    def _on_template_change(self):
        label = self.template_var.get()
        is_guard = label == TEMPLATE_LABELS[TEMPLATE_GUARD]
        if is_guard:
            self.guard_frame.grid()
        else:
            self.guard_frame.grid_remove()

    def _selected_template_type(self):
        label = self.template_var.get()
        for key, value in TEMPLATE_LABELS.items():
            if value == label:
                return key
        return TEMPLATE_EDO

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
                values=(process["status"], TEMPLATE_LABELS.get(process["template_type"], "")),
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
                    values=(branch["status"], extra),
                    tags=(tag,) if tag else (),
                    open=not branch["is_deferred"],
                )
                for step in branch["steps"]:
                    detail = step.get("result_note") or step.get("completion_type")
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
                        values=(step["status"], detail),
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

    def run(self):
        self._setup_tray()
        self.notifier.start()
        self.root.mainloop()
