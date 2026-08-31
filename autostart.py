"""Ярлык в папке «Автозагрузка» Windows — без дополнительных пакетов."""

import os
import subprocess
import sys
from pathlib import Path

SHORTCUT_NAME = "Напоминалка.lnk"


def project_root():
    return Path(__file__).resolve().parent


def startup_dir():
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuntimeError("Нет переменной APPDATA — автозагрузка недоступна.")
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
    )


def shortcut_path():
    return startup_dir() / SHORTCUT_NAME


def pythonw_path():
    candidate = Path(sys.executable).with_name("pythonw.exe")
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def is_enabled():
    return shortcut_path().exists()


def _ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def enable():
    """Создаёт ярлык: pythonw + main.py, рабочий каталог — папка проекта."""
    folder = startup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    target = pythonw_path()
    script = project_root() / "main.py"
    workdir = project_root()
    link = shortcut_path()
    arguments = f'"{script}" --tray'

    command = "\n".join(
        [
            "$ws = New-Object -ComObject WScript.Shell",
            f"$s = $ws.CreateShortcut({_ps_quote(link)})",
            f"$s.TargetPath = {_ps_quote(target)}",
            f"$s.Arguments = {_ps_quote(arguments)}",
            f"$s.WorkingDirectory = {_ps_quote(workdir)}",
            "$s.WindowStyle = 7",
            "$s.Save()",
        ]
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "не удалось создать ярлык")
    return link


def disable():
    path = shortcut_path()
    if path.exists():
        path.unlink()
    return path


if __name__ == "__main__":
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    if action == "install":
        print(enable())
    elif action == "uninstall":
        print(disable())
    else:
        sys.exit("Использование: python autostart.py install|uninstall")
