"""Точка входа приложения-напоминалки."""

import sys

from database import DB_FILENAME, ReminderDatabase
from gui import ReminderApp


def configure_console():
    """Логи содержат кириллицу, а консоль Windows не всегда в UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass


def main():
    configure_console()

    start_in_tray = "--tray" in sys.argv

    db = ReminderDatabase(DB_FILENAME)
    app = None
    try:
        app = ReminderApp(db)
        app.run(start_in_tray=start_in_tray)
    finally:
        if app is not None:
            app.quit_app()
        db.close()


if __name__ == "__main__":
    main()
