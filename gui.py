"""Графический интерфейс напоминалки на Tkinter."""

import threading
import tkinter as tk
from datetime import date, datetime, time, timedelta
from tkinter import messagebox, ttk

from tkcalendar import DateEntry

import database as db_module
from database import (
    STATUS_CANCELLED,
    STATUS_DONE,
    STATUS_OVERDUE,
    STATUS_PENDING,
)
from notifications import NotificationManager

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
        self.root.title("Напоминалка")
        self.root.geometry("920x620")
        self.root.minsize(760, 520)
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=26)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        container = ttk.Frame(self.root, padding=12)
        container.pack(fill="both", expand=True)

        self._build_form(container)
        self._build_toolbar(container)
        self._build_list(container)
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

        self.date_entry = DateEntry(
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
