"""Windows-first dark desktop dashboard for nationwide public business contacts."""

from __future__ import annotations

import csv
import datetime as dt
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from . import __version__
from .core import (BASE_DIR, CATEGORIES, REGIONS, CONTACT_STATUSES,
                   company_key, connect_db, export_csv, parse_ua_phones, read_contacts,
                   save_contact, set_phone_status)
from .discovery import (RegionIndex, WebsiteReader, download_ukraine_osm,
                        ensure_regions, read_site_numbers)
from .osm_index import build_osm_index, find_indexed_leads, index_is_current, INDEX_FILE
from .osm_storage import OSM_FILE, cleanup_abandoned_download, inspect_osm

STATUSES = CONTACT_STATUSES
BG = "#090f1c"
PANEL = "#121d30"
PANEL_RAISED = "#182740"
TEXT = "#eaf0fa"
MUTED = "#8da0ba"
ACCENT = "#dc2934"
FONT = "Segoe UI"
ASSETS = Path(__file__).resolve().parent / "assets"


def dark_theme_logo() -> Image.Image:
    """Keep the genuine transparent red emblem; make its black lettering white."""
    logo = Image.open(ASSETS / "polski-bramy-logo.png").convert("RGBA")
    logo = logo.crop((45, 56, 559, 202))
    # The transparent PNG already has no background. The white rectangle was a
    # UI card; replace only the dark text's RGB, preserving its exact alpha.
    text_start = 164
    lettering = logo.crop((text_start, 0, logo.width, logo.height))
    white = Image.new("RGBA", lettering.size, (242, 246, 250, 0))
    white.putalpha(lettering.getchannel("A"))
    logo.paste(white, (text_start, 0))
    return logo


class App(ctk.CTk):
    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__()
        self.title(f"Polski Bramy · Радар контактів {__version__}")
        self.geometry("1340x880")
        self.minsize(1050, 735)
        self.configure(fg_color=BG)
        mark = Image.open(ASSETS / "polski-bramy-mark.png").convert("RGBA")
        # Use the exact supplied emblem, tightly cropped for the taskbar icon.
        mark = mark.crop((45, 56, 190, 202)).resize((64, 64), Image.Resampling.LANCZOS)
        self.window_icon = ImageTk.PhotoImage(mark, master=self)
        self.iconphoto(True, self.window_icon)
        cleanup_abandoned_download(OSM_FILE)
        cleanup_abandoned_download(INDEX_FILE)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.working = False
        self.rows: dict[str, dict] = {}
        self.build_ui()
        self.refresh()
        self.refresh_osm_state()
        self.after(150, self.poll)

    def build_ui(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=TEXT,
                        rowheight=33, font=(FONT, 10), borderwidth=0, relief="flat")
        style.configure("Treeview.Heading", background=PANEL_RAISED, foreground="#cbd8ec",
                        font=(FONT, 10, "bold"), relief="flat", padding=(6, 9))
        style.map("Treeview", background=[("selected", "#60303b")], foreground=[("selected", "#ffffff")])
        style.map("Treeview.Heading", background=[("active", "#253957")])

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=26, pady=(19, 12))
        logo_image = dark_theme_logo()
        self.brand_logo = ctk.CTkImage(light_image=logo_image, dark_image=logo_image,
                                        size=(250, 71))
        ctk.CTkLabel(top, text="", image=self.brand_logo,
                     fg_color="transparent").pack(side="left")
        heading = ctk.CTkFrame(top, fg_color="transparent")
        heading.pack(side="left", padx=17)
        ctk.CTkLabel(heading, text="Пошук публічних робочих контактів",
                     text_color=TEXT, font=(FONT, 15, "bold")).pack(anchor="w")
        ctk.CTkLabel(heading, text="Вся Україна  ·  OSM  ·  Без CRM", text_color=MUTED,
                     font=(FONT, 11)).pack(anchor="w", pady=(4, 0))
        self.osm_badge = ctk.CTkLabel(top, text="OSM · перевірка…", text_color=ACCENT,
                                      fg_color=PANEL_RAISED, corner_radius=12, height=34,
                                      font=(FONT, 11, "bold"))
        self.osm_badge.pack(side="right", padx=(12, 0))
        ctk.CTkLabel(top, text=f"Версія {__version__}", text_color=MUTED,
                     font=(FONT, 11)).pack(side="right")

        filters = ctk.CTkFrame(self, fg_color="transparent")
        filters.pack(fill="x", padx=26, pady=(0, 12))
        filters.grid_columnconfigure((0, 1), weight=1, uniform="filters")
        self.cat_vars = self._filter_panel(filters, 0, "НАПРЯМКИ РОБОТИ", CATEGORIES)
        self.reg_vars = self._filter_panel(filters, 1, "ГЕОГРАФІЯ ПОШУКУ", REGIONS)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.pack(fill="x", padx=26, pady=(0, 9))
        self.start_btn = self.button(toolbar, "▶   Запустити радар", self.start_search, ACCENT)
        self.stop_btn = self.button(toolbar, "■   Зупинити", self.stop_search, PANEL_RAISED)
        self.download_btn = self.button(toolbar, "↓   Завантажити / оновити OSM", self.download_osm, "#24528a")
        self.source_btn = self.button(toolbar, "Власний PBF", self.choose_source, PANEL_RAISED)
        self.export_btn = self.button(toolbar, "Експорт CSV", self.export, PANEL_RAISED, side="right")
        self.import_btn = self.button(toolbar, "Імпорт CSV", self.import_contacts, PANEL_RAISED, side="right")

        status = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=12, height=62)
        status.pack(fill="x", padx=26, pady=(0, 12))
        status.pack_propagate(False)
        self.status_text = tk.StringVar(value="Готово. Завантажте OSM усієї України для першого пошуку.")
        ctk.CTkLabel(status, textvariable=self.status_text, text_color=TEXT,
                     font=(FONT, 11), anchor="w").pack(fill="x", padx=15, pady=(8, 0))
        self.progress = ctk.CTkProgressBar(status, height=4, mode="indeterminate",
                                          fg_color="#20334c", progress_color=ACCENT)
        self.progress.pack(fill="x", padx=15, pady=(6, 0))
        self.progress.set(0)
        self.osm_detail = ctk.CTkLabel(status, text="", text_color=MUTED, font=(FONT, 10), anchor="w")
        self.osm_detail.pack(fill="x", padx=15, pady=(3, 0))

        panel = ctk.CTkFrame(self, fg_color=PANEL, corner_radius=15)
        panel.pack(fill="both", expand=True, padx=26, pady=(0, 18))
        line = ctk.CTkFrame(panel, fg_color="transparent")
        line.pack(fill="x", padx=16, pady=(13, 10))
        self.count_text = tk.StringVar(value="Контактів: 0")
        ctk.CTkLabel(line, textvariable=self.count_text, text_color=TEXT,
                     font=(FONT, 14, "bold")).pack(side="left")
        self.only_new = tk.BooleanVar(value=True)
        ctk.CTkSwitch(line, text="Лише нові", variable=self.only_new,
                      command=self.refresh, progress_color=ACCENT,
                      button_color="#d5fdf6", font=(FONT, 11)).pack(side="left", padx=22)
        self.status_box = ctk.CTkOptionMenu(line, values=list(STATUSES), command=self.change_status,
                                             fg_color="#233a58", button_color="#2d5b81",
                                             button_hover_color="#3975a6", width=226,
                                             font=(FONT, 11), dropdown_font=(FONT, 11))
        self.status_box.set(STATUSES[0])
        self.status_box.pack(side="right")
        ctk.CTkLabel(line, text="Статус номера", text_color=MUTED,
                     font=(FONT, 11)).pack(side="right", padx=(0, 10))

        cols = ("mark", "name", "category", "region", "phone", "role", "activity", "checked_at", "status", "website")
        headers = ("✓", "Компанія", "Категорія", "Область", "Телефон", "Призначення",
                   "Активність", "Перевірено", "Статус", "Сайт / посилання")
        widths = (45, 170, 150, 115, 145, 105, 150, 145, 130, 170)
        table_box = tk.Frame(panel, bg=PANEL, highlightthickness=0)
        table_box.pack(fill="both", expand=True, padx=12)
        self.table = ttk.Treeview(table_box, columns=cols, show="headings", selectmode="browse")
        for col, title, width in zip(cols, headers, widths):
            self.table.heading(col, text=title)
            self.table.column(col, width=width, minwidth=40 if col == "mark" else 90,
                              anchor="center" if col == "mark" else "w",
                              stretch=(col in ("name", "website")))
        vert = ttk.Scrollbar(table_box, orient="vertical", command=self.table.yview)
        hor = ttk.Scrollbar(table_box, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vert.set, xscrollcommand=hor.set)
        vert.pack(side="right", fill="y")
        hor.pack(side="bottom", fill="x")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<ButtonRelease-1>", self.toggle_from_click)
        self.table.bind("<<TreeviewSelect>>", self.sync_status)
        self.table.bind("<Double-1>", self.open_selected)
        ctk.CTkLabel(panel, text="☐ / ☑ — змінити статус  ·  Двічі на номер — копіювати  ·  Двічі на рядок — відкрити джерело",
                     text_color=MUTED, font=(FONT, 10)).pack(anchor="w", padx=16, pady=(5, 9))

    def _filter_panel(self, parent, column, title, options):
        card = ctk.CTkFrame(parent, fg_color=PANEL, corner_radius=15)
        card.grid(row=0, column=column, sticky="nsew", padx=(0, 6) if column == 0 else (6, 0))
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 6))
        ctk.CTkLabel(head, text=title, text_color=TEXT, font=(FONT, 12, "bold")).pack(side="left")
        variables = {name: tk.BooleanVar(value=True) for name in options}
        self.button(head, "Усі", lambda: self._set_filters(variables, True), PANEL_RAISED,
                    width=46, height=27, side="right")
        self.button(head, "Очистити", lambda: self._set_filters(variables, False), PANEL_RAISED,
                    width=76, height=27, side="right")
        box = ctk.CTkScrollableFrame(card, height=174, fg_color="#0d1727", corner_radius=9,
                                      scrollbar_button_color="#29445f")
        box.pack(fill="x", padx=12, pady=(0, 12))
        box.grid_columnconfigure((0, 1), weight=1)
        for i, (name, var) in enumerate(variables.items()):
            ctk.CTkCheckBox(box, text=name, variable=var, width=22, height=24,
                            checkbox_width=17, checkbox_height=17, corner_radius=5,
                            fg_color=ACCENT, hover_color="#b61d2e", border_color="#4d627d",
                            text_color="#dce8f6", font=(FONT, 11)).grid(
                                row=i // 2, column=i % 2, sticky="w", padx=(7, 3), pady=5)
        return variables

    @staticmethod
    def _set_filters(variables, value):
        for variable in variables.values():
            variable.set(value)

    @staticmethod
    def button(parent, label, action, color, *, dark_text=False, width=110, height=37, side="left"):
        btn = ctk.CTkButton(parent, text=label, command=action, fg_color=color,
                            text_color=BG if dark_text else TEXT,
                            hover_color="#c12130" if color == ACCENT else "#315174",
                            font=(FONT, 11, "bold"), corner_radius=10,
                            height=height, width=width)
        btn.pack(side=side, padx=(0, 7))
        return btn

    def refresh_osm_state(self):
        source = getattr(self, "custom_osm", OSM_FILE)
        state = inspect_osm(source)
        text = "OSM · " + state.status
        if index_is_current(source, INDEX_FILE):
            text += " · індекс ✓"
        self.osm_badge.configure(text="   " + text + "   ", text_color=state.color)
        self.osm_detail.configure(text=state.description + "  ·  Один файл України + швидкий індекс")

    def ui_event(self, kind, value):
        self.events.put((kind, value))

    def poll(self):
        try:
            for _ in range(80):
                kind, value = self.events.get_nowait()
                if kind == "message":
                    self.status_text.set(value)
                elif kind == "index_progress":
                    processed, total = value
                    self.progress.stop()
                    self.progress.configure(mode="determinate")
                    self.progress.set(min(1.0, processed / total) if total else 1.0)
                elif kind == "refresh":
                    self.refresh()
                elif kind == "done":
                    self.working = False
                    self.start_btn.configure(state="normal")
                    self.download_btn.configure(state="normal")
                    self.source_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.set(0)
                    self.status_text.set(value)
                    self.refresh_osm_state()
                elif kind == "error":
                    self.working = False
                    self.start_btn.configure(state="normal")
                    self.download_btn.configure(state="normal")
                    self.source_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.set(0)
                    self.status_text.set(value)
                    self.refresh_osm_state()
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
        self.source_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

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
            self.refresh_osm_state()

    def download_osm(self):
        if not messagebox.askyesno("OSM · вся Україна", "Завантажити актуальний OSM-файл для всієї України (~1 ГБ)?\n\nПісля завантаження радар один раз створить швидкий індекс областей. При оновленні старий файл буде замінено, без накопичення копій."):
            return

        def work():
            self.ui_event("message", "Шукаю доступне джерело OSM для всієї України…")
            source_name = download_ukraine_osm(OSM_FILE,
                                               progress=lambda s: self.ui_event("message", s),
                                               cancel=self.cancel)
            self.ui_event("message", "Готую одноразовий індекс областей та організацій…")
            count = build_osm_index(OSM_FILE, RegionIndex(ensure_regions()),
                                    progress=lambda s: self.ui_event("message", s),
                                    cancel=self.cancel,
                                    on_progress=lambda done, total: self.ui_event("index_progress", (done, total)))
            self.custom_osm = OSM_FILE
            return (f"OSM України оновлено ({source_name}) · індекс готовий · "
                    f"організацій: {count:,}. Можна шукати за областями.")

        self.run_task(work)

    def start_search(self):
        categories = {name for name, var in self.cat_vars.items() if var.get()}
        regions = {name for name, var in self.reg_vars.items() if var.get()}
        source = getattr(self, "custom_osm", OSM_FILE)
        if not source.exists():
            messagebox.showinfo("Потрібне джерело", "Спочатку завантаж дані України або вибери локальний OSM PBF файл.")
            return
        if not categories or not regions:
            messagebox.showwarning("Фільтри", "Вибери хоча б одну категорію та область.")
            return
        all_regions = len(regions) == len(REGIONS)

        def work():
            if not index_is_current(source):
                self.ui_event("message", "Вперше індексую OSM усієї України · це може тривати кілька хвилин…")
                build_osm_index(source, RegionIndex(ensure_regions()),
                                progress=lambda s: self.ui_event("message", s), cancel=self.cancel,
                                on_progress=lambda done, total: self.ui_event("index_progress", (done, total)))
            self.ui_event("message", "Вибираю організації з локального індексу…")
            leads = find_indexed_leads(source, categories, set() if all_regions else regions)
            reader = WebsiteReader()
            checked_websites: dict[str, tuple[dict[str, str], str]] = {}
            found, new_count = 0, 0
            with connect_db() as con:
                already_checked = {
                    row[0]: row[1] for row in con.execute(
                        "SELECT company_key, checked_at FROM companies WHERE checked_at != ''")
                }
                for pos, lead in enumerate(leads, 1):
                    if self.cancel.is_set():
                        raise InterruptedError()
                    numbers = {num: lead["source_url"] for num in lead["phones"]}
                    activity = "Невідомо"
                    key = company_key(lead["name"], lead["website"], lead["region"])
                    last_check = already_checked.get(key, "")
                    recently_processed = False
                    if last_check:
                        try:
                            recently_processed = (dt.datetime.now(dt.timezone.utc) -
                                                  dt.datetime.fromisoformat(last_check)).days < 7
                        except ValueError:
                            pass
                    if lead["website"] and not recently_processed and len(checked_websites) < 120:
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
        for position, row in enumerate(values):
            id_ = str(row["id"])
            self.rows[id_] = row
            values_for_row = ("☑" if row["status"] != "Не опрацьований" else "☐",) + tuple(
                row.get(k, "") for k in
                ("name", "category", "region", "phone", "role", "activity", "checked_at", "status", "website"))
            self.table.insert("", "end", iid=id_, values=values_for_row,
                              tags=("even" if position % 2 == 0 else "odd",))
        self.table.tag_configure("even", background=PANEL)
        self.table.tag_configure("odd", background="#17253a")
        reviewed = sum(row["status"] != "Не опрацьований" for row in all_rows)
        self.count_text.set(f"Нові: {len(all_rows) - reviewed}    ·    Опрацьовані: {reviewed}    ·    Усього: {len(all_rows)}")

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
