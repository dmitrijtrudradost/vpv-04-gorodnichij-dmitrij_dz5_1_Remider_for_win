"""Фоновый мониторинг напоминаний и показ уведомлений."""

import queue
import threading
import tkinter as tk
from tkinter import ttk

import database as db_module
from database import STEP_DONE, StepDependencyError
from templates import ACTION_CHECKLIST, ACTION_EDO_SIGN, ACTION_GUARD_SIGN

CHECK_INTERVAL_SECONDS = 1
UI_QUEUE_POLL_MS = 200
SNOOZE_MINUTES = 5
APP_ID = "Напоминалка"

# Каждое следующее окно сдвигается, чтобы стопка напоминаний не сливалась в одно.
POPUP_OFFSET_PX = 32
POPUP_MAX_OFFSETS = 8

try:
    from windows_toasts import Toast, ToastScenario, WindowsToaster

    WINDOWS_TOASTS_IMPORTED = True
except Exception:
    Toast = ToastScenario = WindowsToaster = None
    WINDOWS_TOASTS_IMPORTED = False

try:
    from winotify import Notification as WinotifyNotification

    WINOTIFY_IMPORTED = True
except Exception:
    WinotifyNotification = None
    WINOTIFY_IMPORTED = False

try:
    import winsound
except Exception:
    winsound = None


class NotificationManager:
    """Следит за базой и показывает уведомления, когда наступает время.

    Уведомление доставляется сразу двумя путями: системным баннером Windows и
    собственным окном поверх всех окон. Ни то, ни другое не закрывается само —
    баннер получает ``scenario="reminder"``, а окно живёт до нажатия кнопки.

    Для баннера сначала используется ``windows-toasts``: только он умеет
    выставить ``scenario``. Если пакета нет, остаётся ``winotify``, но он держит
    баннер не дольше 25 секунд. Бэкенд, выбросивший ошибку, отключается до
    перезапуска, чтобы не спотыкаться о него на каждом уведомлении.
    """

    def __init__(self, db, root, on_change=None):
        self.db = db
        self.root = root
        self.on_change = on_change

        self._thread = None
        self._stop_event = threading.Event()
        # Ключ (id, due_time): при переносе времени напоминание сработает снова.
        self._notified = set()
        self._ui_queue = queue.Queue()
        self._ui_poll_id = None
        self._toast_lock = threading.Lock()

        self._windows_toasts_available = WINDOWS_TOASTS_IMPORTED
        self._winotify_available = WINOTIFY_IMPORTED
        self._toaster = None
        self._open_popups = []

    @property
    def backend_name(self):
        """Название текущего способа доставки баннера — для статус-бара."""
        if self._windows_toasts_available:
            return "windows-toasts (постоянные)"
        if self._winotify_available:
            return "winotify (до 25 сек)"
        return "только окно"

    def start(self):
        """Запускает фоновый поток и разбор очереди задач для главного потока."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._monitor_loop, name="reminder-monitor", daemon=True
        )
        self._thread.start()
        self._schedule_ui_poll()

    def stop(self):
        self._stop_event.set()
        if self._ui_poll_id is not None:
            try:
                self.root.after_cancel(self._ui_poll_id)
            except Exception:
                pass
            self._ui_poll_id = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _schedule_ui_poll(self):
        self._ui_poll_id = self.root.after(UI_QUEUE_POLL_MS, self._process_ui_queue)

    def _process_ui_queue(self):
        """Выполняет в главном потоке задачи, поставленные фоновым потоком.

        Tkinter не потокобезопасен, поэтому окна создаются только здесь.
        """
        try:
            while True:
                callback, args = self._ui_queue.get_nowait()
                try:
                    callback(*args)
                except Exception as error:
                    print(f"[notifications] ошибка в UI-задаче: {error}")
        except queue.Empty:
            pass

        if not self._stop_event.is_set():
            self._schedule_ui_poll()

    def _post_to_ui(self, callback, *args):
        self._ui_queue.put((callback, args))

    def _monitor_loop(self):
        while not self._stop_event.is_set():
            try:
                self._tick()
            except Exception as error:
                print(f"[notifications] ошибка мониторинга: {error}")
            self._stop_event.wait(CHECK_INTERVAL_SECONDS)

    def _tick(self):
        changed = self.db.mark_overdue() > 0
        if self.db.activate_due_deferred_branches() > 0:
            changed = True
        if self.db.ensure_waiting_step_reminders() > 0:
            changed = True

        due = self.db.get_due_reminders()
        current_keys = {(item["id"], item["due_time"]) for item in due}
        # Всё, что вышло из числа наступивших, можно забыть: повторно оно
        # сработает только если пользователь перенесёт время.
        self._notified &= current_keys

        for reminder in due:
            key = (reminder["id"], reminder["due_time"])
            if key in self._notified:
                continue
            self._notified.add(key)
            # Показ в отдельном потоке: обращение к системным уведомлениям
            # может занять время, а цикл мониторинга должен продолжать тикать.
            threading.Thread(
                target=self._show_notification,
                args=(reminder,),
                name=f"notify-{reminder['id']}",
                daemon=True,
            ).start()
            changed = True

        if changed and self.on_change:
            self._post_to_ui(self.on_change)

    def _show_notification(self, reminder):
        """Показывает и системный баннер, и своё окно.

        Оба способа используются одновременно: баннер выглядит привычно, а окно
        гарантированно дождётся реакции, даже если системные уведомления
        подавлены настройками Windows.
        """
        title = reminder["title"]
        message = reminder["description"] or f"Время: {reminder['due_time']}"

        self._beep()
        self._show_system_banner(title, message)
        self._post_to_ui(self._show_popup, reminder)

    def _show_system_banner(self, title, message):
        """Системный баннер: windows-toasts, при его отсутствии — winotify."""
        if self._notify_via_windows_toasts(title, message):
            return True
        return self._notify_via_winotify(title, message)

    def _notify_via_windows_toasts(self, title, message):
        if not self._windows_toasts_available:
            return False
        with self._toast_lock:
            try:
                if self._toaster is None:
                    self._toaster = WindowsToaster(APP_ID)
                toast = Toast(text_fields=[title, message])
                # Именно этот сценарий заставляет Windows держать баннер на
                # экране, пока пользователь сам его не уберёт.
                toast.scenario = ToastScenario.Reminder
                self._toaster.show_toast(toast)
                return True
            except Exception as error:
                print(f"[notifications] windows-toasts отключён: {error}")
                self._windows_toasts_available = False
                self._toaster = None
                return False

    def _notify_via_winotify(self, title, message):
        if not self._winotify_available:
            return False
        try:
            # "long" — максимум, который умеет winotify: около 25 секунд.
            toast = WinotifyNotification(
                app_id=APP_ID, title=title, msg=message, duration="long"
            )
            toast.show()
            return True
        except Exception as error:
            print(f"[notifications] winotify отключён: {error}")
            self._winotify_available = False
            return False

    @staticmethod
    def _beep():
        if winsound is None:
            return
        try:
            winsound.MessageBeep(winsound.MB_ICONASTERISK)
        except Exception:
            pass

    def _show_popup(self, reminder):
        """Окно напоминания поверх всех окон. Только из главного потока."""
        step = None
        if reminder.get("id") and reminder.get("step_id"):
            step = self.db.get_step(reminder["step_id"])

        popup = tk.Toplevel(self.root)
        popup.title("Напоминание")
        popup.configure(padx=20, pady=16)
        popup.resizable(False, False)
        popup.attributes("-topmost", True)

        ttk.Label(
            popup,
            text=reminder["title"],
            font=("Segoe UI", 14, "bold"),
            wraplength=420,
            justify="left",
        ).pack(anchor="w")

        ttk.Label(
            popup,
            text=f"Время: {reminder['due_time']}",
            font=("Segoe UI", 9),
            foreground="#666666",
        ).pack(anchor="w", pady=(2, 10))

        description = (reminder["description"] or "").strip()
        if description:
            ttk.Label(
                popup,
                text=description,
                font=("Segoe UI", 10),
                wraplength=420,
                justify="left",
            ).pack(anchor="w", pady=(0, 12))

        buttons = ttk.Frame(popup)
        buttons.pack(anchor="e")

        def close_popup():
            if popup in self._open_popups:
                self._open_popups.remove(popup)
            if popup.winfo_exists():
                popup.destroy()

        def postpone_wait():
            if step:
                self.db.postpone_step_escalation(step["id"])
            self._notify_change()
            close_popup()

        def complete_step(note=""):
            try:
                self.db.update_step_status(step["id"], STEP_DONE, result_note=note)
            except StepDependencyError as error:
                from tkinter import messagebox

                messagebox.showwarning("Шаг заблокирован", str(error), parent=popup)
                return
            self._notify_change()
            close_popup()

        def mark_done():
            if step and step.get("action_kind") == ACTION_CHECKLIST:
                close_popup()
                self._show_checklist(step["id"])
                return
            if reminder.get("id"):
                self.db.update_status(reminder["id"], db_module.STATUS_DONE)
            self._notify_change()
            close_popup()

        def snooze():
            if reminder.get("id"):
                self.db.snooze_reminder(reminder["id"], SNOOZE_MINUTES)
            self._notify_change()
            close_popup()

        action = (step or {}).get("action_kind") or ""
        if step and action == ACTION_EDO_SIGN:
            ttk.Button(
                buttons,
                text="Договор подписан контрагентом",
                command=lambda: complete_step("Подписан контрагентом"),
            ).pack(side="left", padx=(0, 6))
            ttk.Button(
                buttons, text="Ещё нет, договор в ожидании", command=postpone_wait
            ).pack(side="left", padx=(0, 6))
            popup.protocol("WM_DELETE_WINDOW", postpone_wait)
        elif step and action == ACTION_GUARD_SIGN:
            if self.db.guard_threshold_reached(step["id"]):
                ttk.Button(
                    buttons,
                    text="Подписать самостоятельно",
                    command=lambda: complete_step("Подписано самостоятельно"),
                ).pack(side="left", padx=(0, 6))
                ttk.Button(
                    buttons,
                    text="Продолжать ждать",
                    command=lambda: complete_step("Продолжаем ждать подпись"),
                ).pack(side="left", padx=(0, 6))
                popup.protocol("WM_DELETE_WINDOW", postpone_wait)
            else:
                ttk.Button(
                    buttons, text="Ещё ждём подпись заказчика", command=postpone_wait
                ).pack(side="left", padx=(0, 6))
                ttk.Button(buttons, text="Закрыть", command=postpone_wait).pack(side="left")
                popup.protocol("WM_DELETE_WINDOW", postpone_wait)
        elif reminder.get("id"):
            ttk.Button(buttons, text="Готово", command=mark_done).pack(
                side="left", padx=(0, 6)
            )
            ttk.Button(
                buttons, text=f"Отложить на {SNOOZE_MINUTES} мин", command=snooze
            ).pack(side="left", padx=(0, 6))
            ttk.Button(buttons, text="Закрыть", command=close_popup).pack(side="left")
            popup.protocol("WM_DELETE_WINDOW", close_popup)
        else:
            ttk.Button(buttons, text="Закрыть", command=close_popup).pack(side="left")
            popup.protocol("WM_DELETE_WINDOW", close_popup)

        self._open_popups.append(popup)
        self._place_popup(popup)

        popup.lift()
        popup.focus_force()

    def _show_checklist(self, step_id):
        """Модальное окно блокирующего чек-листа."""
        from tkinter import messagebox

        items = self.db.get_checklist_items(step_id)
        step = self.db.get_step(step_id)
        if step is None:
            return

        dialog = tk.Toplevel(self.root)
        dialog.title("Чек-лист шага")
        dialog.configure(padx=20, pady=16)
        dialog.transient(self.root)
        dialog.attributes("-topmost", True)

        ttk.Label(
            dialog,
            text=step["title"],
            font=("Segoe UI", 12, "bold"),
            wraplength=420,
        ).pack(anchor="w", pady=(0, 10))
        ttk.Label(
            dialog,
            text="Шаг закроется только когда отмечены все пункты.",
            foreground="#666666",
        ).pack(anchor="w", pady=(0, 8))

        vars_by_id = {}
        for item in items:
            var = tk.BooleanVar(value=bool(item["is_checked"]))
            vars_by_id[item["id"]] = var
            ttk.Checkbutton(dialog, text=item["text"], variable=var).pack(
                anchor="w", pady=2
            )

        def save_and_maybe_close():
            try:
                for item_id, var in vars_by_id.items():
                    self.db.check_checklist_item(step_id, item_id, var.get())
            except StepDependencyError as error:
                messagebox.showwarning("Шаг заблокирован", str(error), parent=dialog)
                return
            remaining = [
                item
                for item in self.db.get_checklist_items(step_id)
                if not item["is_checked"]
            ]
            self._notify_change()
            if remaining:
                messagebox.showinfo(
                    "Чек-лист неполный",
                    "Отмечены не все пункты — шаг остаётся открытым.",
                    parent=dialog,
                )
                return
            dialog.destroy()

        ttk.Button(dialog, text="Сохранить", command=save_and_maybe_close).pack(
            anchor="e", pady=(12, 0)
        )
        self._place_popup(dialog)
        dialog.focus_force()

    def _notify_change(self):
        if self.on_change:
            try:
                self.on_change()
            except Exception as error:
                print(f"[notifications] ошибка обновления интерфейса: {error}")

    def _place_popup(self, window):
        """Ставит окно по центру, сдвигая его, если предыдущие ещё открыты."""
        window.update_idletasks()
        width = window.winfo_width()
        height = window.winfo_height()

        step = (len(self._open_popups) - 1) % POPUP_MAX_OFFSETS
        shift = step * POPUP_OFFSET_PX

        x = (window.winfo_screenwidth() - width) // 2 + shift
        y = (window.winfo_screenheight() - height) // 3 + shift
        window.geometry(f"+{x}+{y}")

    def show_manual_notification(self, title, message):
        """Показать уведомление немедленно, минуя расписание.

        Вызывается и из интерфейса, и из меню трея (другой поток), поэтому
        доставка уходит в отдельный поток, а popup — в очередь главного.
        """
        threading.Thread(
            target=self._deliver_manual,
            args=(title, message),
            name="notify-manual",
            daemon=True,
        ).start()

    def _deliver_manual(self, title, message):
        self._beep()
        self._show_system_banner(title, message)
        self._post_to_ui(
            self._show_popup,
            {
                "id": 0,
                "title": title,
                "description": message,
                "due_time": db_module.now_str(),
            },
        )

    def show_checklist(self, step_id):
        """Открыть чек-лист из вкладки процессов."""
        self._show_checklist(step_id)

    def test_notification(self):
        """Проверка доставки уведомлений из интерфейса."""
        self.show_manual_notification(
            "Тестовое напоминание",
            f"Системный баннер: {self.backend_name}. Это окно закроется только "
            f"по кнопке.",
        )
