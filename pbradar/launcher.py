"""Versioned, hash-verified GitHub Releases launcher for PB Radar.

The launcher is intentionally independent of the application: app releases
never replace the running launcher or the user's SQLite database.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import messagebox, simpledialog
from urllib.parse import urlparse

import requests
from PIL import ImageTk

from .core import BASE_DIR
from .branding import red_mark_icon
from .osm_storage import OSM_FILE

REPOSITORY = "lyesm1th/pb-radar"
API_URL = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
RELEASE_ASSET = "PB-Radar-App.zip"
APP_FILENAME = "PB-Radar.exe"
VERSIONS = BASE_DIR / "versions"
CURRENT = BASE_DIR / "current.json"


def ensure_data_directories() -> None:
    """Persistent per-user data lives outside the installed launcher folder."""
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    OSM_FILE.parent.mkdir(parents=True, exist_ok=True)
    VERSIONS.mkdir(parents=True, exist_ok=True)


def safe_version(value: str) -> str:
    if not value.startswith("v") or not value[1:].replace(".", "").isdigit() or len(value) > 35:
        raise ValueError("Некоректний тег версії")
    return value


def read_current() -> str:
    try:
        data = json.loads(CURRENT.read_text(encoding="utf-8"))
        return safe_version(data["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return ""


def app_path(version: str) -> Path:
    return VERSIONS / safe_version(version) / APP_FILENAME


def stored_versions() -> list[str]:
    if not VERSIONS.exists():
        return []
    found = []
    for p in VERSIONS.iterdir():
        if p.is_dir():
            try:
                if app_path(p.name).is_file():
                    found.append(p.name)
            except ValueError:
                pass
    return sorted(found, reverse=True)


def fetch_latest() -> dict:
    response = requests.get(API_URL, timeout=(8, 18), headers={
        "Accept": "application/vnd.github+json", "User-Agent": "PB-Radar-Launcher/1.0"})
    response.raise_for_status()
    release = response.json()
    version = safe_version(release["tag_name"])
    for asset in release.get("assets", []):
        if asset.get("name") != RELEASE_ASSET:
            continue
        digest = asset.get("digest", "")
        url = asset.get("browser_download_url", "")
        if not digest.startswith("sha256:") or len(digest) != 71:
            raise ValueError("GitHub не надав контрольну суму збірки")
        if urlparse(url).scheme != "https" or urlparse(url).hostname != "github.com":
            raise ValueError("Неправильне джерело оновлення")
        return {"version": version, "url": url, "sha256": digest[7:].lower()}
    raise ValueError("У останньому релізі відсутній PB-Radar-App.zip")


def install_release(info: dict, progress=None) -> Path:
    """Install into a new version directory; never delete the current version."""
    version = safe_version(info["version"])
    expected = info["sha256"]
    if not (len(expected) == 64 and all(c in "0123456789abcdef" for c in expected)):
        raise ValueError("Контрольна сума недійсна")
    if app_path(version).is_file():
        activate_version(version)
        return app_path(version)
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    VERSIONS.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="pbradar-update-", dir=str(BASE_DIR)))
    try:
        archive = tmp / RELEASE_ASSET
        digest = hashlib.sha256()
        total = 0
        with requests.get(info["url"], stream=True, timeout=(10, 90),
                          headers={"User-Agent": "PB-Radar-Launcher/1.0"}) as response:
            response.raise_for_status()
            with archive.open("wb") as target:
                for block in response.iter_content(chunk_size=512 * 1024):
                    if block:
                        total += len(block)
                        if total > 180_000_000:
                            raise ValueError("Занадто великий пакет оновлення")
                        digest.update(block)
                        target.write(block)
                        if progress:
                            progress(f"Завантажено {total // (1024 * 1024)} МБ")
        if digest.hexdigest() != expected:
            raise ValueError("Контрольна сума не збігається — оновлення відхилено")
        with zipfile.ZipFile(archive) as z:
            contents = z.namelist()
            if contents != [APP_FILENAME]:
                raise ValueError("Некоректний вміст пакета оновлення")
            member = z.getinfo(APP_FILENAME)
            if member.file_size > 200_000_000:
                raise ValueError("Завеликий виконуваний файл")
            with z.open(member) as source, (tmp / APP_FILENAME).open("wb") as target:
                shutil.copyfileobj(source, target)
        final_dir = VERSIONS / version
        tmp_install = VERSIONS / (version + ".pending")
        tmp_install.mkdir(exist_ok=False)
        try:
            shutil.move(str(tmp / APP_FILENAME), str(tmp_install / APP_FILENAME))
            tmp_install.replace(final_dir)
        except Exception:
            shutil.rmtree(tmp_install, ignore_errors=True)
            raise
        activate_version(version)
        return app_path(version)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def activate_version(version: str):
    if not app_path(version).is_file():
        raise FileNotFoundError("Ця версія не встановлена")
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    temp_manifest = CURRENT.with_suffix(".pending")
    temp_manifest.write_text(json.dumps({"version": version}), encoding="utf-8")
    os.replace(temp_manifest, CURRENT)


class Launcher(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PB Radar · Launcher")
        self.geometry("540x405")
        self.resizable(False, False)
        self.configure(bg="#0b1220")
        self.window_icon = ImageTk.PhotoImage(red_mark_icon(64), master=self)
        self.iconphoto(True, self.window_icon)
        self.info = None
        self.busy = False
        ensure_data_directories()

        tk.Label(self, text="PB  /  RADAR", fg="#ffffff", bg="#0b1220",
                 font=("Segoe UI Semibold", 29)).pack(pady=(36, 5))
        tk.Label(self, text="ЗНАХОДЬ КОНТАКТИ. ВІДКРИВАЙ МОЖЛИВОСТІ.",
                 fg="#9db4d2", bg="#0b1220", font=("Segoe UI", 10)).pack()
        self.status = tk.StringVar(value="Перевіряю оновлення…")
        tk.Label(self, textvariable=self.status, fg="#d8e7fa", bg="#0b1220",
                 wraplength=490, font=("Segoe UI", 11)).pack(pady=(30, 10))
        self.primary = tk.Button(self, text="Зачекай…", command=self.primary_action,
                                 state="disabled", bg="#32a779", fg="white", relief="flat",
                                 width=25, height=2, font=("Segoe UI Semibold", 11))
        self.primary.pack()
        self.secondary = tk.Button(self, text="Перевірити ще раз", command=self.check,
                                   bg="#223a5a", fg="#c2d6f0", relief="flat")
        self.secondary.pack(pady=(12, 0))
        tk.Button(self, text="Повернути попередню версію", command=self.rollback,
                  bg="#0b1220", fg="#88a6c9", relief="flat").pack(pady=(7, 0))
        tk.Button(self, text="📁  Відкрити папку даних / OSM", command=self.open_data_folder,
                  bg="#0b1220", fg="#9db4d2", relief="flat").pack(pady=(12, 0))
        self.after(100, self.check)

    def open_data_folder(self):
        ensure_data_directories()
        try:
            if os.name == "nt":
                os.startfile(str(BASE_DIR))
            else:
                subprocess.Popen(["xdg-open", str(BASE_DIR)])
        except OSError as exc:
            messagebox.showerror("Не вдалося відкрити папку", str(exc))

    def run_async(self, operation):
        if self.busy:
            return
        self.busy = True
        self.primary.config(state="disabled")

        def worker():
            try:
                operation()
            except Exception as exc:
                self.after(0, lambda msg=str(exc): self.on_error(msg))
            finally:
                self.after(0, lambda: setattr(self, "busy", False))

        threading.Thread(target=worker, daemon=True).start()

    def on_error(self, msg):
        installed = read_current()
        if installed and app_path(installed).is_file():
            self.status.set(f"Немає зв'язку з оновленнями. Встановлено {installed}.")
            self.primary.config(state="normal", text="ЗАПУСТИТИ")
        else:
            self.status.set("Оновлення недоступне: " + msg[:150])
            self.primary.config(state="disabled", text="Немає встановленої версії")

    def check(self):
        self.status.set("Перевіряю оновлення на GitHub…")

        def worker():
            info = fetch_latest()
            self.after(0, lambda: self.on_latest(info))

        self.run_async(worker)

    def on_latest(self, info):
        self.info = info
        current = read_current()
        if current == info["version"] and app_path(current).exists():
            self.status.set(f"Встановлена актуальна версія {current}")
            self.primary.config(state="normal", text="ЗАПУСТИТИ")
        else:
            self.status.set(f"Доступна версія {info['version']} · встановлено {current or 'нічого'}")
            self.primary.config(state="normal", text="ОНОВИТИ" if current else "ВСТАНОВИТИ")

    def primary_action(self):
        if not self.info:
            self.launch_current()
        elif read_current() == self.info["version"] and app_path(self.info["version"]).exists():
            self.launch_current()
        else:
            self.status.set("Завантажую та перевіряю збірку…")

            def install():
                install_release(self.info, lambda txt: self.after(0, lambda m=txt: self.status.set(m)))
                self.after(0, self.launch_current)

            self.run_async(install)

    def launch_current(self):
        version = read_current()
        program = app_path(version) if version else None
        if not program or not program.is_file():
            messagebox.showerror("Помилка", "Не знайдено встановленої програми")
            return
        try:
            subprocess.Popen([str(program)], cwd=str(program.parent), close_fds=True)
            self.destroy()
        except OSError as exc:
            messagebox.showerror("Не вдалося запустити", str(exc))

    def rollback(self):
        versions = [v for v in stored_versions() if v != read_current()]
        if not versions:
            messagebox.showinfo("Попередні версії", "Інших встановлених версій немає")
            return
        selected = simpledialog.askstring("Відновити версію",
                                          "Доступні версії: " + ", ".join(versions) +
                                          "\nВведи потрібну версію:", parent=self)
        if selected is None:
            return
        if selected not in versions:
            messagebox.showwarning("Версія", "Такої версії немає серед встановлених")
            return
        activate_version(selected)
        self.info = None
        self.status.set(f"Відновлено {selected}. Дані користувача збережено.")
        self.primary.config(state="normal", text="ЗАПУСТИТИ")


def main():
    Launcher().mainloop()


if __name__ == "__main__":
    main()
