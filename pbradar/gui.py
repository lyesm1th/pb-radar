"""Windows-first Tkinter desktop UI for on-demand nationwide contact discovery."""

from __future__ import annotations

import csv
import datetime as dt
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from . import __version__
from .core import (BASE_DIR, CATEGORIES, REGIONS, CONTACT_STATUSES,
                   connect_db, export_csv, parse_ua_phones, read_contacts,
                   save_contact, set_phone_status)
from .discovery import (OSM_URL, RegionIndex, WebsiteReader, download_file,
                        ensure_regions, find_osm_leads, read_site_numbers)

OSM_FILE = BASE_DIR / "cache" / "ukraine-latest.osm.pbf"
STATUSES = CONTACT_STATUSES


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"PB Radar {__version__} · Пошук контактів")
        self.geometry("1190x790")
        self.minsize(950, 680)
        self.configure(bg="#0b1220")
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.working = False
        self.rows: dict[str, dict] = {}
        self.build_ui()
        self.refresh()
        self.after(150, self.poll)

    def build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background="#f8fafc", fieldbackground="#f8fafc", foreground="#15243a",
                        rowheight=28, font=("Segoe UI", 9), borderwidth=0)
        style.configure("Treeview.Heading", background="#dfe7f1", foreground="#1b3455",
                        font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#d7e8ff")], foreground=[("selected", "#142237")])

        top = tk.Frame(self, bg="#0b1220")
        top.pack(fill="x", padx=22, pady=(20, 12))
        tk.Label(top, text="PB  /  RADAR", fg="#ffffff", bg="#0b1220",
                 font=("Segoe UI Semibold", 23)).pack(side="left")
        tk.Label(top, text="Пошук публічних робочих телефонів · Україна",
                 fg="#a6b7cf", bg="#0b1220", font=("Segoe UI", 11)).pack(side="left", padx=22)

        filters = tk.Frame(self, bg="#142239", padx=14, pady=12)
        filters.pack(fill="x", padx=22)
        cframe = tk.Frame(filters, bg="#142239")
        cframe.pack(side="left", fill="both", expand=True, padx=(0, 12))
        rframe = tk.Frame(filters, bg="#142239")
        rframe.pack(side="left", fill="both", expand=True)
        for frame, label in ((cframe, "КАТЕГОРІЇ"), (rframe, "ОБЛАСТІ")):
            tk.Label(frame, text=label, bg="#142239", fg="#a8bdde",
                     font=("Segoe UI Semibold", 10)).pack(anchor="w", pady=(0, 7))
        self.cat_list = tk.Listbox(cframe, selectmode="multiple", height=8, exportselection=False,
                                   bg="#1e3453", fg="#eef5ff", selectbackground="#3a81db",
                                   font=("Segoe UI", 10), borderwidth=0, highlightthickness=0)
        self.reg_list = tk.Listbox(rframe, selectmode="multiple", height=8, exportselection=False,
                                   bg="#1e3453", fg="#eef5ff", selectbackground="#3a81db",
                                   font=("Segoe UI", 10), borderwidth=0, highlightthickness=0)
        for value in CATEGORIES:
            self.cat_list.insert("end", value)
        for value in REGIONS:
            self.reg_list.insert("end", value)
        for box, frame in ((self.cat_list, cframe), (self.reg_list, rframe)):
            box.pack(fill="both", expand=True)
            box.selection_set(0, "end")
            buttons = tk.Frame(frame, bg="#142239")
            buttons.pack(anchor="e", pady=(5, 0))
            self.small_button(buttons, "Усі", lambda l=box: l.selection_set(0, "end"))
            self.small_button(buttons, "Очистити", lambda l=box: l.selection_clear(0, "end"))

        toolbar = tk.Frame(self, bg="#0b1220")
        toolbar.pack(fill="x", padx=22, pady=(12, 10))
        self.start_btn = self.button(toolbar, "▶  ПОЧАТИ ПОШУК", self.start_search, "#34a77a")
        self.stop_btn = self.button(toolbar, "Зупинити", self.stop_search, "#394e68")
        self.source_btn = self.button(toolbar, "Джерело OSM", self.choose_source, "#294c77")
        self.download_btn = self.button(toolbar, "Завантажити дані України", self.download_osm, "#294c77")
        self.import_btn = self.button(toolbar, "Імпорт CSV", self.import_contacts, "#294c77")
        self.export_btn = self.button(toolbar, "Експорт CSV", self.export, "#294c77")
        self.status_text = tk.StringVar(value="Готово до пошуку · джерело OSM не завантажено" if not OSM_FILE.exists() else "Готово до пошуку")
        tk.Label(self, textvariable=self.status_text, bg="#0b1220", fg="#a8bdde",
                 font=("Segoe UI", 10), anchor="w").pack(fill="x", padx=22, pady=(0, 9))

        panel = tk.Frame(self, bg="#f8fafc", padx=10, pady=10)
        panel.pack(fill="both", expand=True, padx=22, pady=(0, 20))
        line = tk.Frame(panel, bg="#f8fafc")
        line.pack(fill="x", pady=(0, 8))
        self.count_text = tk.StringVar(value="Контакти: 0")
        tk.Label(line, textvariable=self.count_text, bg="#f8fafc", fg="#17304f",
                 font=("Segoe UI Semibold", 12)).pack(side="left")
        self.only_new = tk.BooleanVar(value=True)
        tk.Checkbutton(line, text="Лише не опрацьовані", variable=self.only_new,
                       command=self.refresh, bg="#f8fafc", fg="#17304f").pack(side="left", padx=20)
        tk.Label(line, text="Статус вибраного:", bg="#f8fafc", fg="#17304f").pack(side="right", padx=8)
        self.status_box = ttk.Combobox(line, state="readonly", values=STATUSES, width=24)
        self.status_box.set(STATUSES[0])
        self.status_box.pack(side="right")
        self.status_box.bind("<<ComboboxSelected>>", self.change_status)

        cols = ("mark", "name", "category", "region", "phone", "role", "activity", "checked_at", "status", "website")
        headers = ("✓", "Компанія", "Категорія", "Область", "Телефон", "Призначення",
                   "Активність", "Перевірено", "Статус", "Сайт / посилання")
        widths = (45, 170, 150, 115, 145, 105, 150, 145, 130, 170)
        self.table = ttk.Treeview(panel, columns=cols, show="headings", selectmode="browse")
        for col, title, width in zip(cols, headers, widths):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=40 if col == "mark" else 90,
                              anchor="center" if col == "mark" else "w",
                              stretch=(col in ("name", "website")))
        vert = ttk.Scrollbar(panel, orient="vertical", command=self.table.yview)
        hor = ttk.Scrollbar(panel, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vert.set, xscrollcommand=hor.set)
        vert.pack(side="right", fill="y")
        hor.pack(side="bottom", fill="x")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<ButtonRelease-1>", self.toggle_from_click)
        self.table.bind("<<TreeviewSelect>>", self.sync_status)
        self.table.bind("<Double-1>", self.open_selected)
        tk.Label(panel, text="Клік по ☐/☑ — позначити номер. Щоб переглянути вже опрацьовані, зніми фільтр. Подвійний клік по номеру — копіювати.",
                 fg="#657995", bg="#f8fafc", font=("Segoe UI", 9)).pack(anchor="w", pady=(8, 0))

    @staticmethod
    def small_button(parent, label, action):
        tk.Button(parent, text=label, command=action, fg="#d7e7fc", bg="#263f62",
                  relief="flat", padx=7).pack(side="left", padx=3)

    @staticmethod
    def button(parent, label, action, color):
        btn = tk.Button(parent, text=label, command=action, bg=color, fg="white",
                        relief="flat", activeforeground="white", padx=11, pady=8,
                        font=("Segoe UI Semibold", 9), cursor="hand2")
        btn.pack(side="left", padx=(0, 7))
        return btn

    def ui_event(self, kind, value):
        self.events.put((kind, value))

    def poll(self):
        try:
            for _ in range(80):
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.status_text.set(value)
                elif kind == "refresh":
                    self.refresh()
                elif kind == "done":
                    self.working = False
                    self.start_btn.configure(state="normal")
                    self.download_btn.configure(state="normal")
                    self.status_text.set(value)
                elif kind == "error":
                    self.working = False
                    self.start_btn.configure(state="normal")
                    self.download_btn.configure(state="normal")
                    messagebox.showerror("PB Radar", value)
        except queue.Empty:
            pass
        self.after(150, self.poll)

    def run_task(self, fn):
        if self.working:
            return
        self.working = True
        self.cancel.clear()
        self.start_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")

        def runner():
            try:
                text = fn()
                self.ui_event("done", text)
            except InterruptedError:
                self.ui_event("done", "Зупинено користувачем. Знайдені контакти збережено.")
            except Exception as exc:
                self.ui_event("error", f"Не вдалося виконати дію: {exc}")

        threading.Thread(target=runner, daemon=True).start()

    def choose_source(self):
        filename = filedialog.askopenfilename(title="Файл даних OpenStreetMap",
                                              filetypes=[("OSM PBF", "*.osm.pbf"), ("Усі файли", "*.*")])
        if filename:
            self.custom_osm = Path(filename)
            self.status_text.set(f"Джерело: {filename}")

    def download_osm(self):
        if not messagebox.askyesno("Початкові дані", "Завантажити відкриті дані OSM для України (~850 МБ)?\nЦе потрібно лише один раз, пізніше можна оновити файл."):
            return
        self.run_task(lambda: (download_file(OSM_URL, OSM_FILE,
                                            lambda s: self.ui_event("message", s)),
                               "Дані України завантажено. Можна запускати пошук.")[1])

    def start_search(self):
        categories = {CATEGORIES[i] for i in self.cat_list.curselection()}
        regions = {REGIONS[i] for i in self.reg_list.curselection()}
        source = getattr(self, "custom_osm", OSM_FILE)
        if not source.exists():
            messagebox.showinfo("Потрібне джерело", "Спочатку завантаж дані України або вибери локальний OSM PBF файл.")
            return
        if not categories or not regions:
            messagebox.showwarning("Фільтри", "Вибери хоча б одну категорію та область.")
            return
        all_regions = len(regions) == len(REGIONS)

        def work():
            self.ui_event("message", "Підготовка меж областей…")
            try:
                index = RegionIndex(ensure_regions(lambda m: self.ui_event("message", m)))
            except Exception:
                if not all_regions:
                    raise RuntimeError("Межі областей недоступні. Для вибраних областей потрібне підключення до geoBoundaries.")
                index = RegionIndex({"features": []})
            self.ui_event("message", "Пошук організацій у відкритих даних України…")
            leads = find_osm_leads(source, categories, set() if all_regions else regions,
                                   index, progress=lambda m: self.ui_event("message", m),
                                   cancel=self.cancel)
            reader = WebsiteReader()
            checked_websites: dict[str, tuple[dict[str, str], str]] = {}
            found, new_count = 0, 0
            with connect_db() as con:
                for pos, lead in enumerate(leads, 1):
                    if self.cancel.is_set():
                        raise InterruptedError()
                    numbers = {num: lead["source_url"] for num in lead["phones"]}
                    activity = "Невідомо"
                    if lead["website"] and len(checked_websites) < 120:
                        website = lead["website"]
                        if website not in checked_websites:
                            checked_websites[website] = read_site_numbers(website, reader)
                        site_phones, activity = checked_websites[website]
                        numbers.update(site_phones)
                    for number, source_url in numbers.items():
                        new_count += save_contact(con, lead["name"], lead["category"],
                                                  lead["region"], number, lead["website"],
                                                  source_url, activity)
                        found += 1
                    if pos % 10 == 0:
                        con.commit()
                        self.ui_event("refresh", None)
                        self.ui_event("message", f"Перевірено {pos}/{len(leads)} компаній · телефонів: {found} · нових: {new_count}")
            self.ui_event("refresh", None)
            return f"Готово: перевірено {len(leads)} організацій, додано {new_count} нових телефонів."

        self.run_task(work)

    def stop_search(self):
        if self.working:
            self.cancel.set()
            self.status_text.set("Зупиняю після поточного запиту…")

    def refresh(self):
        with connect_db() as con:
            all_rows = read_contacts(con)
        values = [row for row in all_rows if row["status"] == "Не опрацьований"] if self.only_new.get() else all_rows
        for id_ in self.table.get_children():
            self.table.delete(id_)
        self.rows = {}
        for row in values:
            id_ = str(row["id"])
            self.rows[id_] = row
            values_for_row = ("☑" if row["status"] != "Не опрацьований" else "☐",) + tuple(
                row.get(k, "") for k in
                ("name", "category", "region", "phone", "role", "activity", "checked_at", "status", "website"))
            self.table.insert("", "end", iid=id_, values=values_for_row)
        reviewed = sum(row["status"] != "Не опрацьований" for row in all_rows)
        self.count_text.set(f"До опрацювання: {len(all_rows) - reviewed}  ·  Опрацьовано: {reviewed}")

    def sync_status(self, _event=None):
        selected = self.table.selection()
        if selected and selected[0] in self.rows:
            self.status_box.set(self.rows[selected[0]]["status"])

    def toggle_from_click(self, event):
        if self.table.identify_column(event.x) != "#1":
            return
        row_id = self.table.identify_row(event.y)
        if not row_id or row_id not in self.rows:
            return
        row = self.rows[row_id]
        new_status = "Опрацьовано" if row["status"] == "Не опрацьований" else "Не опрацьований"
        with connect_db() as con:
            set_phone_status(con, row["phone"], new_status)
        self.refresh()
        self.status_text.set(f"{row['phone']} → {new_status}")
        return "break"

    def change_status(self, _event=None):
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows:
            return
        phone = self.rows[selected[0]]["phone"]
        new_status = self.status_box.get()
        with connect_db() as con:
            set_phone_status(con, phone, new_status)
        self.refresh()
        self.status_text.set(f"{phone} → {new_status}")

    def open_selected(self, event=None):
        selected = self.table.selection()
        if selected:
            row = self.rows.get(selected[0], {})
            if event and self.table.identify_column(event.x) == "#1":
                return
            if event and self.table.identify_column(event.x) == "#5":
                self.clipboard_clear()
                self.clipboard_append(row.get("phone", ""))
                self.status_text.set("Номер скопійовано: " + row.get("phone", ""))
                return
            url = row.get("source_url") or row.get("website")
            if url:
                webbrowser.open(url)

    def import_contacts(self):
        filename = filedialog.askopenfilename(title="Імпорт бази", filetypes=[("CSV", "*.csv")])
        if not filename:
            return
        count = 0
        try:
            with open(filename, encoding="utf-8-sig", newline="") as handle, connect_db() as con:
                preview = handle.read(1024)
                handle.seek(0)
                delimiter = ";" if preview.count(";") > preview.count(",") else ","
                for row in csv.DictReader(handle, delimiter=delimiter):
                    name = (row.get("name") or row.get("Назва") or "").strip()
                    if not name:
                        continue
                    raw = row.get("phone") or row.get("Телефон") or ""
                    for phone in parse_ua_phones(raw):
                        count += save_contact(con, name, row.get("category", "Імпорт"),
                                              row.get("region", "Не визначено"), phone,
                                              row.get("website", ""), row.get("source_url", ""))
                        imported_status = (row.get("status") or "").strip()
                        if imported_status in STATUSES and imported_status != "Не опрацьований":
                            set_phone_status(con, phone, imported_status)
            self.refresh()
            messagebox.showinfo("Імпорт завершено", f"Додано нових контактів: {count}")
        except (OSError, UnicodeError, csv.Error) as exc:
            messagebox.showerror("Помилка імпорту", str(exc))

    def export(self):
        path = filedialog.asksaveasfilename(title="Експорт контактів", defaultextension=".csv",
                                            filetypes=[("Excel-сумісний CSV", "*.csv")],
                                            initialfile="PB-Radar-contacts-" + dt.date.today().isoformat() + ".csv")
        if path:
            with connect_db() as con:
                rows = read_contacts(con, ("Не опрацьований",) if self.only_new.get() else ())
            export_csv(Path(path), rows)
            messagebox.showinfo("Експорт", f"Збережено {len(rows)} контактів.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
