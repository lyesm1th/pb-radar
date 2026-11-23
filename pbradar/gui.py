"""Windows-first dark desktop dashboard for nationwide public business contacts."""

from __future__ import annotations

import csv
import datetime as dt
import json
import sys
import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from PIL import Image, ImageTk

from . import __version__
from .branding import red_mark_icon
from .catalogs import scan_catalogs
from .core import (BASE_DIR, CATEGORIES, REGIONS, CONTACT_STATUSES, OWNERSHIP_FILTERS,
                   OWNERSHIP_TYPES, company_key, connect_db, copyable_contact_value,
                   export_csv, filter_contacts, filter_ownership,
                   group_contacts_by_company, mark_site_checked, parse_ua_phones,
                   read_column_widths, read_contacts,
                   read_setting, save_contact, set_company_ownership, set_phone_status,
                   site_needs_check, visible_link, write_setting)
from .discovery import (RegionIndex, WebsiteReader, download_ukraine_osm,
                        ensure_regions, read_site_numbers)
from .lun import scan_lun
from .osm_index import build_osm_index, find_indexed_leads, index_is_current, INDEX_FILE
from .osm_storage import OSM_FILE, cleanup_abandoned_download, inspect_osm
from .prozorro import read_cursor, scan_prozorro
from .sources import SOURCE_STYLES, group_source_label, source_label
from .verification import verify_saved

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
    DEFAULT_FILTER_PANE_RATIO = 0.38

    def __init__(self):
        ctk.set_appearance_mode("dark")
        super().__init__()
        self.title(f"Polski Bramy · Радар контактів {__version__}")
        self.geometry("1340x880")
        self.minsize(1050, 735)
        self.configure(fg_color=BG)
        mark = red_mark_icon(64)
        self.window_icon = ImageTk.PhotoImage(mark, master=self)
        self.iconphoto(True, self.window_icon)
        if sys.platform.startswith("win"):
            # CustomTkinter overwrites `iconphoto()` with its own blue icon
            # 200 ms after CTk.__init__ unless iconbitmap() has been called.
            # The packaged ICO both sets the native icon and blocks that reset.
            ico = ASSETS / "PB-Radar.ico"
            if ico.is_file():
                self.iconbitmap(str(ico))
            else:
                # Source-only run without a pre-built ICO: restore our icon
                # after the delayed CustomTkinter callback.
                self.after(250, lambda: self.iconphoto(True, self.window_icon))
        cleanup_abandoned_download(OSM_FILE)
        cleanup_abandoned_download(INDEX_FILE)
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.working = False
        self.rows: dict[str, dict] = {}
        self.expanded_groups: set[str] = set()
        self.list_categories: set[str] | None = None
        self.list_regions: set[str] | None = None
        self.build_ui()
        self.refresh()
        self.refresh_osm_state()
        self.after(150, self.poll)

    def build_ui(self):
        with connect_db() as con:
            saved_widths = read_column_widths(con)
            ownership_mode = read_setting(con, "ownership_filter", OWNERSHIP_FILTERS[0])
            source_enabled = read_setting(con, "show_prozorro", "1") != "0"
            # The old three-pane layout could leave a huge, empty status pane.
            # Start from a clean two-pane setting rather than reusing that data.
            self.saved_filter_ratio = read_setting(con, "filter_pane_ratio_v2", "")
        self.ownership_mode = tk.StringVar(value=ownership_mode if ownership_mode in OWNERSHIP_FILTERS
                                           else OWNERSHIP_FILTERS[0])
        self.show_prozorro = tk.BooleanVar(value=source_enabled)
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
        ctk.CTkLabel(heading, text="Каталоги + ЛУН + OSM + Prozorro  ·  Без CRM", text_color=MUTED,
                     font=(FONT, 11)).pack(anchor="w", pady=(4, 0))
        self.reset_layout_btn = ctk.CTkButton(
            top, text="↺", width=35, height=34, corner_radius=10,
            fg_color=PANEL_RAISED, hover_color="#315174", text_color=TEXT,
            font=(FONT, 19), command=self.reset_layout)
        self.reset_layout_btn.pack(side="right", padx=(12, 0))
        self.osm_badge = ctk.CTkLabel(top, text="OSM · перевірка…", text_color=ACCENT,
                                      fg_color=PANEL_RAISED, corner_radius=12, height=34,
                                      font=(FONT, 11, "bold"))
        self.osm_badge.pack(side="right", padx=(12, 0))
        ctk.CTkLabel(top, text=f"Версія {__version__}", text_color=MUTED,
                     font=(FONT, 11)).pack(side="right")

        # Only the filters and contact area resize. The action/status section
        # has a natural, fixed height, so it cannot consume the whole window.
        self.layout = tk.PanedWindow(self, orient="vertical", bg="#20344e", bd=0,
                                     sashwidth=6, sashpad=0, sashrelief="flat",
                                     showhandle=False,
                                     opaqueresize=True)
        self.layout.pack(fill="both", expand=True, padx=26, pady=(0, 18))

        filters = ctk.CTkFrame(self.layout, fg_color=BG)
        filters.grid_columnconfigure((0, 1), weight=1, uniform="filters")
        filters.grid_rowconfigure(0, weight=1)
        self.cat_vars = self._filter_panel(filters, 0, "НАПРЯМКИ РОБОТИ", CATEGORIES)
        self.reg_vars = self._filter_panel(filters, 1, "ГЕОГРАФІЯ ПОШУКУ", REGIONS)
        self.layout.add(filters, height=280, minsize=195, stretch="never")

        bottom = ctk.CTkFrame(self.layout, fg_color=BG)
        self.layout.add(bottom, minsize=375, stretch="always")
        controls = ctk.CTkFrame(bottom, fg_color=BG)
        controls.pack(fill="x")
        toolbar = ctk.CTkFrame(controls, fg_color="transparent")
        toolbar.pack(fill="x", pady=(7, 9))
        self.start_btn = self.button(toolbar, "▶   Радар · номери", self.start_catalog_search, ACCENT,
                                     width=147)
        self.stop_btn = self.button(toolbar, "■   Зупинити", self.stop_search, PANEL_RAISED)
        self.osm_search_btn = self.button(toolbar, "OSM · пошук", self.start_search, PANEL_RAISED,
                                          width=110)
        self.prozorro_btn = self.button(toolbar, "Prozorro · контакти", self.start_prozorro,
                                       "#24528a", width=145)
        self.lun_btn = self.button(toolbar, "ЛУН · забудовники", self.start_lun,
                                   "#24528a", width=145)
        sources = ctk.CTkFrame(controls, fg_color="transparent")
        sources.pack(fill="x", pady=(0, 8))
        self.download_btn = self.button(sources, "↓   Завантажити / оновити OSM", self.download_osm,
                                        "#24528a", height=31)
        self.source_btn = self.button(sources, "Власний PBF", self.choose_source,
                                      PANEL_RAISED, height=31)
        self.verify_btn = self.button(sources, "✓ Звірити видимі", self.start_verification,
                                      "#24528a", width=140, height=31)
        self.export_btn = self.button(sources, "Експорт CSV", self.export, PANEL_RAISED,
                                      height=31, side="right")
        self.import_btn = self.button(sources, "Імпорт CSV", self.import_contacts, PANEL_RAISED,
                                      height=31, side="right")

        status = ctk.CTkFrame(controls, fg_color=PANEL, corner_radius=12, height=62)
        status.pack(fill="x", pady=(0, 9))
        status.pack_propagate(False)
        self.status_text = tk.StringVar(value="Готово. «Радар · номери» працює без OSM: збирає телефони з каталогів компаній.")
        ctk.CTkLabel(status, textvariable=self.status_text, text_color=TEXT,
                     font=(FONT, 11), anchor="w").pack(fill="x", padx=15, pady=(8, 0))
        self.progress = ctk.CTkProgressBar(status, height=4, mode="indeterminate",
                                          fg_color="#20334c", progress_color=ACCENT)
        self.progress.pack(fill="x", padx=15, pady=(6, 0))
        self.progress.set(0)
        self.osm_detail = ctk.CTkLabel(status, text="", text_color=MUTED, font=(FONT, 10), anchor="w")
        self.osm_detail.pack(fill="x", padx=15, pady=(3, 0))
        panel = ctk.CTkFrame(bottom, fg_color=PANEL, corner_radius=15)
        panel.pack(fill="both", expand=True)
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

        visibility_line = ctk.CTkFrame(panel, fg_color="transparent")
        visibility_line.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(visibility_line, text="ТИП ОРГАНІЗАЦІЇ", text_color=MUTED,
                     font=(FONT, 11, "bold")).pack(side="left", padx=(0, 10))
        self.ownership_filter = ctk.CTkOptionMenu(
            visibility_line, values=list(OWNERSHIP_FILTERS), variable=self.ownership_mode,
            command=self.change_visibility, width=170, fg_color="#233a58",
            button_color="#2d5b81", font=(FONT, 11), dropdown_font=(FONT, 11))
        self.ownership_filter.pack(side="left", padx=(0, 18))
        ctk.CTkSwitch(visibility_line, text="Показувати Prozorro", variable=self.show_prozorro,
                      command=self.change_visibility, progress_color=ACCENT,
                      button_color="#d5fdf6", font=(FONT, 11)).pack(side="left")
        self.ownership_selected = ctk.CTkOptionMenu(
            visibility_line, values=list(OWNERSHIP_TYPES), command=self.change_ownership,
            width=172, fg_color="#233a58", button_color="#2d5b81",
            font=(FONT, 11), dropdown_font=(FONT, 11), state="disabled")
        self.ownership_selected.set(OWNERSHIP_TYPES[2])
        self.ownership_selected.pack(side="right")
        ctk.CTkLabel(visibility_line, text="Позначити вибрану:", text_color=MUTED,
                     font=(FONT, 11)).pack(side="right", padx=(0, 10))

        search_line = ctk.CTkFrame(panel, fg_color="transparent")
        search_line.pack(fill="x", padx=16, pady=(0, 10))
        ctk.CTkLabel(search_line, text="ПОШУК В БАЗІ", text_color=MUTED,
                     font=(FONT, 11, "bold")).pack(side="left", padx=(0, 12))
        self.search_var = tk.StringVar()
        self._search_after = None
        self.search_var.trace_add("write", self.schedule_search)
        self.button(search_line, "Очистити", lambda: self.search_var.set(""), PANEL_RAISED,
                    width=80, height=32, side="right")
        self.button(search_line, "Скинути ширину", self.reset_column_widths, PANEL_RAISED,
                    width=125, height=32, side="right")
        ctk.CTkEntry(search_line, textvariable=self.search_var,
                     placeholder_text="Компанія, телефон, область, тип, категорія, статус, сайт…",
                     fg_color="#0d1727", border_color="#29445f", height=34,
                     font=(FONT, 11)).pack(side="left", fill="x", expand=True)

        explanation_line = ctk.CTkFrame(panel, fg_color="transparent")
        explanation_line.pack(fill="x", padx=16, pady=(0, 7))
        self.filter_explanation = tk.StringVar(value="")
        ctk.CTkLabel(explanation_line, textvariable=self.filter_explanation, text_color="#e1b16c",
                     font=(FONT, 10), anchor="w").pack(side="left", fill="x", expand=True)
        self.expand_btn = self.button(explanation_line, "Розгорнути групи", self.toggle_all_groups,
                                      PANEL_RAISED, width=130, height=27, side="right")
        self.button(explanation_line, "Показати всю базу", self.show_all_contacts,
                    PANEL_RAISED, width=130, height=27, side="right")
        self.button(explanation_line, "↻ Оновити список", self.update_list_filters,
                    "#24528a", width=135, height=27, side="right")

        cols = ("mark", "source", "name", "category", "region", "ownership", "edrpou", "phone", "verification", "verified_owner", "line_state", "role", "activity", "checked_at", "status", "website")
        headers = ("✓", "Джерело", "Компанія", "Категорія", "Область", "Тип організації", "ЄДРПОУ", "Телефон", "Звірка", "Кому приписаний", "Стан лінії", "Призначення",
                   "Активність", "Перевірено", "Статус", "Сайт / посилання")
        widths = (45, 130, 200, 160, 125, 165, 100, 150, 175, 190, 170, 145, 150, 145, 155, 190)
        self.default_column_widths = dict(zip(cols, widths))
        table_box = tk.Frame(panel, bg=PANEL, highlightthickness=0)
        table_box.pack(fill="both", expand=True, padx=12)
        self.table = ttk.Treeview(table_box, columns=cols, show="tree headings", selectmode="browse")
        self.table.heading("#0", text="")
        self.table.column("#0", width=29, minwidth=29, stretch=False)
        for col, title, width in zip(cols, headers, widths):
            self.table.heading(col, text=title)
            self.table.column(col, width=saved_widths.get(col, width), minwidth=40 if col == "mark" else 65,
                              anchor="center" if col == "mark" else "w",
                              stretch=False)
        vert = ttk.Scrollbar(table_box, orient="vertical", command=self.table.yview)
        hor = ttk.Scrollbar(table_box, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=vert.set, xscrollcommand=hor.set)
        vert.pack(side="right", fill="y")
        hor.pack(side="bottom", fill="x")
        self.table.pack(fill="both", expand=True)
        self._dragging_separator = False
        self._focused_column = "#8"  # Phone column until the user selects another cell.
        self.table.bind("<ButtonPress-1>", self.note_column_drag)
        self.table.bind("<ButtonRelease-1>", self.toggle_from_click)
        self.table.bind("<ButtonRelease-1>", self.save_resized_columns, add="+")
        self.table.bind("<<TreeviewSelect>>", self.sync_status)
        self.table.bind("<<TreeviewOpen>>", self.record_group_open)
        self.table.bind("<<TreeviewClose>>", self.record_group_close)
        self.table.bind("<Double-1>", self.open_selected)
        self.table.bind("<Button-3>", self.show_cell_menu)
        self.table.bind("<Control-c>", self.copy_selected_cell)
        self.copy_menu = tk.Menu(self.table, tearoff=False, bg=PANEL_RAISED, fg=TEXT,
                                 activebackground="#315174", activeforeground="white")
        self.copy_menu.add_command(label="Копіювати номер", command=lambda: self.copy_contact_field("phone"))
        self.copy_menu.add_command(label="Копіювати посилання", command=lambda: self.copy_contact_field("website"))
        self.copy_menu.add_command(label="Копіювати комірку (Ctrl+C)", command=self.copy_selected_cell)
        self.copy_menu.add_separator()
        self.copy_menu.add_command(label="Відкрити посилання", command=self.open_contact_link)
        self.copy_menu.add_command(label="Деталі звірки", command=self.show_verification_details)
        legend = ctk.CTkFrame(panel, fg_color="transparent")
        legend.pack(fill="x", padx=16, pady=(5, 8))
        ctk.CTkLabel(legend, text="Джерела:", text_color=MUTED,
                     font=(FONT, 10)).pack(side="left", padx=(0, 9))
        for label in ("ЛУН", "List.in.ua", "NOVOBUDOVY", "Prozorro", "OSM", "Сайт", "Без джерела"):
            ctk.CTkLabel(legend, text="● " + label, text_color=SOURCE_STYLES[label][0],
                         font=(FONT, 10)).pack(side="left", padx=(0, 13))
        self.layout.bind("<ButtonRelease-1>", lambda _event: self.after(80, self.save_pane_ratios))
        self.after(250, self.restore_layout)

    def note_column_drag(self, event):
        self._dragging_separator = self.table.identify_region(event.x, event.y) == "separator"
        if self.table.identify_region(event.x, event.y) == "cell":
            self._focused_column = self.table.identify_column(event.x)

    def show_cell_menu(self, event):
        item_id = self.table.identify_row(event.y)
        if not item_id or item_id not in self.rows:
            return
        self.table.selection_set(item_id)
        self.table.focus(item_id)
        self.table.focus_set()
        self._focused_column = self.table.identify_column(event.x)
        row = self.rows[item_id]
        self.copy_menu.entryconfigure(0, state="normal" if copyable_contact_value(row, "phone") else "disabled")
        link_state = "normal" if visible_link(row) else "disabled"
        self.copy_menu.entryconfigure(1, state=link_state)
        self.copy_menu.entryconfigure(4, state=link_state)
        try:
            self.copy_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.copy_menu.grab_release()

    def copy_text(self, value: str, label: str) -> bool:
        if not value:
            return False
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status_text.set(f"Скопійовано {label}: {value}")
        return True

    def copy_contact_field(self, column: str) -> bool:
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows:
            return False
        row = self.rows[selected[0]]
        label = "номер" if column == "phone" else "посилання"
        return self.copy_text(copyable_contact_value(row, column), label)

    def copy_selected_cell(self, _event=None):
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows:
            return "break"
        field = self._focused_column
        if not field.startswith("#") or not field[1:].isdigit():
            return "break"
        index = int(field[1:]) - 1
        columns = self.table["columns"]
        if not 0 <= index < len(columns):
            return "break"
        column = columns[index]
        row = self.rows[selected[0]]
        if column == "phone" and row.get("is_group"):
            return "break"  # Never copy the "7 номерів" summary as a real phone.
        value = self.table.set(selected[0], column)
        self.copy_text(str(value), "комірку")
        return "break"

    def open_contact_link(self):
        selected = self.table.selection()
        if selected and selected[0] in self.rows:
            url = visible_link(self.rows[selected[0]])
            if url:
                webbrowser.open(url)

    def show_verification_details(self):
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows:
            return
        row = self.rows[selected[0]]
        if row.get("is_group"):
            return
        messagebox.showinfo("PB Radar · звірка",
            f"Компанія: {row.get('name', '')}\n"
            f"Телефон: {row.get('phone', '')}\n"
            f"Звірка: {row.get('verification', 'Не перевірено')}\n"
            f"Кому приписаний: {row.get('verified_owner', 'Не встановлено')}\n"
            f"Компанія: {row.get('company_state', 'Не перевірено')}\n"
            f"Лінія: {row.get('line_state', 'Не перевірено дзвінком')}\n"
            f"Перевірено: {row.get('verified_at', 'Немає')}\n"
            f"Підтвердження: {row.get('evidence_url', '')}\n\n"
            "Публікація на сайті не доводить чинність номера або юридичну активність фірми.")

    def start_verification(self):
        ids = {int(row["id"]) for row in self.rows.values() if not row.get("is_group")}
        if not ids:
            messagebox.showinfo("Звірка", "Немає видимих контактів.")
            return
        def work():
            r = verify_saved(ids=ids, cancel=self.cancel,
                progress=lambda s: self.ui_event("message", s),
                refresh=lambda: self.ui_event("refresh", None))
            self.ui_event("refresh", None)
            return (f"Звірено {r['checked']} з {r['total']}; "
                    f"на власних сайтах підтверджено {r['confirmed']}. "
                    "Доступність лінії підтверджує тільки дзвінок.")
        self.run_task(work)

    def save_resized_columns(self, _event=None):
        if self._dragging_separator:
            self._dragging_separator = False
            # Tk adjusts the actual column width after button release.
            self.after(80, self.persist_column_widths)

    def persist_column_widths(self):
        widths = {col: self.table.column(col, "width") for col in self.table["columns"]}
        with connect_db() as con:
            write_setting(con, "column_widths", json.dumps(widths, ensure_ascii=False))

    def reset_column_widths(self):
        for col, width in self.default_column_widths.items():
            self.table.column(col, width=width)
        self.persist_column_widths()

    def apply_filter_pane_ratio(self, ratio):
        height = self.layout.winfo_height()
        if height < 580:
            return False
        first = max(195, min(int(height * ratio), height - 385))
        self.layout.sash_place(0, 0, first)
        return True

    def restore_layout(self):
        try:
            ratio = float(self.saved_filter_ratio)
            if not 0.20 <= ratio <= 0.58:
                raise ValueError("Invalid filter height")
        except (ValueError, TypeError):
            ratio = self.DEFAULT_FILTER_PANE_RATIO
        if not self.apply_filter_pane_ratio(ratio):
            self.after(150, self.restore_layout)

    def save_pane_ratios(self, _event=None):
        height = self.layout.winfo_height()
        if height < 580:
            return
        ratio = round(self.layout.sash_coord(0)[1] / height, 4)
        if 0.19 <= ratio <= 0.60:
            self.saved_filter_ratio = str(ratio)
            with connect_db() as con:
                write_setting(con, "filter_pane_ratio_v2", self.saved_filter_ratio)

    def reset_layout(self):
        """Restore UI sizes, never business filters or locally saved contacts."""
        self.reset_column_widths()
        self.apply_filter_pane_ratio(self.DEFAULT_FILTER_PANE_RATIO)
        self.saved_filter_ratio = str(self.DEFAULT_FILTER_PANE_RATIO)
        with connect_db() as con:
            write_setting(con, "filter_pane_ratio_v2", self.saved_filter_ratio)
        self.status_text.set("Розміри панелей і стовпців повернуто до стандартних.")

    def show_all_contacts(self):
        """Return every display filter AND its visible checkboxes to 'all'."""
        self.list_categories = None
        self.list_regions = None
        self._set_filters(self.cat_vars, True)
        self._set_filters(self.reg_vars, True)
        self.ownership_mode.set("Усі")
        self.show_prozorro.set(True)
        self.only_new.set(False)
        self.search_var.set("")
        self.change_visibility()

    def update_list_filters(self):
        """Apply category/oblast checkboxes to saved records without a new scan."""
        categories = {name for name, var in self.cat_vars.items() if var.get()}
        regions = {name for name, var in self.reg_vars.items() if var.get()}
        self.list_categories = None if len(categories) == len(CATEGORIES) else categories
        self.list_regions = None if len(regions) == len(REGIONS) else regions
        self.refresh()
        self.status_text.set(f"Список оновлено за вибором: напрямків {len(categories)}, "
                             f"областей {len(regions)}. Базу номерів не змінено.")

    def toggle_all_groups(self):
        parents = [item for item in self.table.get_children() if self.table.get_children(item)]
        if not parents:
            return
        expand = not all(bool(self.table.item(item, "open")) for item in parents)
        for item in parents:
            self.table.item(item, open=expand)
            if expand:
                self.expanded_groups.add(item)
            else:
                self.expanded_groups.discard(item)
        self.expand_btn.configure(text="Згорнути групи" if expand else "Розгорнути групи")

    def record_group_open(self, _event=None):
        self.expanded_groups.add(self.table.focus())

    def record_group_close(self, _event=None):
        self.expanded_groups.discard(self.table.focus())

    def change_visibility(self, _value=None):
        with connect_db() as con:
            write_setting(con, "ownership_filter", self.ownership_mode.get())
            write_setting(con, "show_prozorro", "1" if self.show_prozorro.get() else "0")
        self.refresh()

    def change_ownership(self, _value=None):
        selected = self.table.selection()
        if not selected or selected[0] not in self.rows:
            return
        row = self.rows[selected[0]]
        ownership = self.ownership_selected.get()
        with connect_db() as con:
            set_company_ownership(con, row["company_id"], ownership)
        self.refresh()
        self.status_text.set(f"{row['name']}: {ownership} · позначено вручну")

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
        box = ctk.CTkScrollableFrame(card, height=125, fg_color="#0d1727", corner_radius=9,
                                      scrollbar_button_color="#29445f")
        box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
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
                    self.osm_search_btn.configure(state="normal")
                    self.lun_btn.configure(state="normal")
                    self.prozorro_btn.configure(state="normal")
                    self.verify_btn.configure(state="normal")
                    self.download_btn.configure(state="normal")
                    self.source_btn.configure(state="normal")
                    self.progress.stop()
                    self.progress.set(0)
                    self.status_text.set(value)
                    self.refresh_osm_state()
                elif kind == "error":
                    self.working = False
                    self.start_btn.configure(state="normal")
                    self.osm_search_btn.configure(state="normal")
                    self.lun_btn.configure(state="normal")
                    self.prozorro_btn.configure(state="normal")
                    self.verify_btn.configure(state="normal")
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
        self.osm_search_btn.configure(state="disabled")
        self.lun_btn.configure(state="disabled")
        self.prozorro_btn.configure(state="disabled")
        self.verify_btn.configure(state="disabled")
        self.download_btn.configure(state="disabled")
        self.source_btn.configure(state="disabled")
        self.progress.configure(mode="indeterminate")
        self.progress.start()

        def runner():
            try:
                text = fn()
                self.ui_event("done", text)
            except InterruptedError:
                self.ui_event("refresh", None)
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

    def start_catalog_search(self):
        categories = {name for name, var in self.cat_vars.items() if var.get()}
        regions = {name for name, var in self.reg_vars.items() if var.get()}
        if not categories or not regions:
            messagebox.showwarning("Фільтри", "Вибери хоча б одну категорію та область.")
            return
        self.update_list_filters()

        def work():
            found = scan_catalogs(
                categories, regions, cancel=self.cancel,
                progress=lambda s: self.ui_event("message", s),
                refresh=lambda: self.ui_event("refresh", None))
            lun_text = ""
            if "Забудовники" in categories and not self.cancel.is_set():
                try:
                    lun = scan_lun(regions, cancel=self.cancel,
                                   progress=lambda s: self.ui_event("message", s),
                                   refresh=lambda: self.ui_event("refresh", None))
                    lun_text = f" ЛУН: {lun['profiles']} профілів, нових {lun['new']}."
                except RuntimeError as exc:
                    lun_text = f" ЛУН тимчасово недоступний: {exc}."
            self.ui_event("message", "Звіряю знайдені контакти з власними сайтами компаній…")
            checked = verify_saved(cancel=self.cancel,
                progress=lambda s: self.ui_event("message", s),
                refresh=lambda: self.ui_event("refresh", None))
            return (f"Звірено {checked['checked']:,}; підтверджено {checked['confirmed']:,}. "
                    + f"Каталоги: сторінок {found['pages']:,}, контактних записів "
                    f"{found['listed']:,}, нових унікальних номерів {found['new']:,}. "
                    + lun_text + " "
                    + f"Недоступних розділів: {found['unavailable']:,}; помилок: {found['failed']:,}. "
                    "Повторний запуск продовжить незавершені каталоги.")

        self.run_task(work)

    def start_lun(self):
        regions = {name for name, var in self.reg_vars.items() if var.get()}
        categories = {name for name, var in self.cat_vars.items() if var.get()}
        if not regions or "Забудовники" not in categories:
            messagebox.showwarning("ЛУН", "Вибери «Забудовники» та хоча б одну область.")
            return
        self.update_list_filters()

        def work():
            result = scan_lun(regions, cancel=self.cancel,
                              progress=lambda s: self.ui_event("message", s),
                              refresh=lambda: self.ui_event("refresh", None))
            return (f"ЛУН: перевірено {result['profiles']:,} профілів із {result['total']:,}, "
                    f"профілів із телефонами {result['with_phone']:,}, "
                    f"нових унікальних номерів {result['new']:,}; "
                    f"залишилось {result['remaining']:,}. Результат збережено.")

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
        self.update_list_filters()

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
            found, new_count, sites_scanned = 0, 0, 0
            with connect_db() as con:
                for pos, lead in enumerate(leads, 1):
                    if self.cancel.is_set():
                        raise InterruptedError()
                    numbers = {num: lead["source_url"] for num in lead["phones"]}
                    activity = "Невідомо"
                    if lead["website"] and (lead["website"] in checked_websites or
                                            site_needs_check(con, lead["website"])):
                        website = lead["website"]
                        if website not in checked_websites:
                            checked_websites[website] = read_site_numbers(website, reader)
                            sites_scanned += 1
                            mark_site_checked(con, website, len(checked_websites[website][0]))
                        site_phones, activity = checked_websites[website]
                        numbers.update(site_phones)
                    for number, source_url in numbers.items():
                        new_count += save_contact(con, lead["name"], lead["category"],
                                                  lead["region"], number, lead["website"],
                                                  source_url, activity,
                                                  social=lead.get("social", ""))
                        found += 1
                    if pos % 10 == 0:
                        con.commit()
                        self.ui_event("refresh", None)
                        self.ui_event("message", f"Перевірено {pos}/{len(leads)} компаній · "
                                      f"сайтів: {sites_scanned} · телефонів: {found} · нових: {new_count}")
            self.ui_event("refresh", None)
            return (f"Готово: перевірено {len(leads)} організацій і {sites_scanned} сайтів, "
                    f"додано {new_count} нових телефонів.")

        self.run_task(work)

    def start_prozorro(self):
        categories = {name for name, var in self.cat_vars.items() if var.get()}
        regions = {name for name, var in self.reg_vars.items() if var.get()}
        if not categories or not regions:
            messagebox.showwarning("Фільтри", "Вибери хоча б одну категорію та область.")
            return
        available = {"Відділи закупівель", "Архітектори", "Архітектурні бюро",
                     "Проєктні організації", "Будівельні компанії", "Монтажні організації",
                     "Будівництво складів", "Протипожежні системи",
                     "Ландшафтні дизайнери", "Дизайнери інтер'єру"}
        if not (categories & available):
            messagebox.showinfo("Prozorro", "Для цього джерела вибери закупівлі, архітекторів, "
                                "дизайнерів, проєктні, будівельні або монтажні організації.")
            return
        with connect_db() as con:
            has_cursor = bool(read_cursor(con))
        resume = True
        if has_cursor:
            answer = messagebox.askyesnocancel(
                "Prozorro · продовження пошуку",
                "Продовжити з останньої перевіреної сторінки?\n\n"
                "Так — шукати старіші записи без повторного проходу.\n"
                "Ні — почати з найновіших тендерів.\n"
                "Скасувати — не запускати пошук.")
            if answer is None:
                return
            resume = answer
        self.update_list_filters()

        def work():
            result = scan_prozorro(
                categories, set() if len(regions) == len(REGIONS) else regions,
                cancel=self.cancel, resume=resume,
                progress=lambda s: self.ui_event("message", s),
                refresh=lambda: self.ui_event("refresh", None))
            return (f"Prozorro: переглянуто {result['examined']:,} закупівель, "
                    f"контактів {result['leads']:,}, нових номерів {result['new']:,}.")

        self.run_task(work)

    def stop_search(self):
        if self.working:
            self.cancel.set()
            self.status_text.set("Зупиняю після поточного запиту…")

    def schedule_search(self, *_):
        if self._search_after is not None:
            self.after_cancel(self._search_after)
        self._search_after = self.after(180, self.refresh)

    def refresh(self):
        self._search_after = None
        with connect_db() as con:
            all_rows = read_contacts(con)
            source_rows = read_contacts(con, include_prozorro=self.show_prozorro.get())
            category_rows = (source_rows if self.list_categories is None else
                             read_contacts(con, include_prozorro=self.show_prozorro.get(),
                                           categories=self.list_categories))
            region_rows = (category_rows if self.list_regions is None else
                           read_contacts(con, include_prozorro=self.show_prozorro.get(),
                                         categories=self.list_categories, regions=self.list_regions))
        excluded_source = len(all_rows) - len(source_rows)
        excluded_category = len(source_rows) - len(category_rows)
        excluded_region = len(category_rows) - len(region_rows)
        ownership_rows = filter_ownership(region_rows, self.ownership_mode.get())
        excluded_ownership = len(region_rows) - len(ownership_rows)
        eligible_rows = ([row for row in ownership_rows if row["status"] == "Не опрацьований"]
                         if self.only_new.get() else ownership_rows)
        excluded_status = len(ownership_rows) - len(eligible_rows)
        values = eligible_rows
        values = filter_contacts(values, self.search_var.get())
        excluded_search = len(eligible_rows) - len(values)
        groups = group_contacts_by_company(values)
        for id_ in self.table.get_children():
            self.table.delete(id_)
        self.rows = {}
        for position, entries in enumerate(groups):
            row = entries[0]
            tag = "even" if position % 2 == 0 else "odd"
            group_label = group_source_label(entries)
            source_tag = "source:" + group_label
            if len(entries) == 1:
                id_ = "contact:" + str(row["id"])
                self.rows[id_] = row
                self.table.insert("", "end", iid=id_, values=self.contact_values(row),
                                  tags=(tag, source_tag))
                continue
            group_id = ("group:edr:" + row["edrpou"]) if row.get("edrpou") else (
                "group:company:" + str(row["company_id"]))
            self.rows[group_id] = {**row, "is_group": True, "source_label": group_label}
            reviewed_in_group = sum(item["status"] != "Не опрацьований" for item in entries)
            label = f"{len(entries)} номери · {reviewed_in_group} опрацьовано"
            regions_in_group = list(dict.fromkeys(item["region"] for item in entries))
            region_label = regions_in_group[0] if len(regions_in_group) == 1 else f"{len(regions_in_group)} області"
            self.table.insert("", "end", iid=group_id, open=group_id in self.expanded_groups,
                              values=("", "● " + group_label, row["name"], row["category"], region_label,
                                      row["ownership"], row.get("edrpou", ""), label,
                                      f"Звірено {sum(bool(x.get('verified_at')) for x in entries)}/{len(entries)}",
                                      "", "", "", row["activity"], "", "", visible_link(row)),
                              tags=(tag, "group", source_tag))
            for item in entries:
                child_id = "contact:" + str(item["id"])
                self.rows[child_id] = item
                child_values = list(self.contact_values(item))
                child_values[2] = ""  # Name on parent
                child_values[3] = ""  # Category on parent
                child_values[5] = ""  # Ownership on parent
                child_values[6] = ""  # Registration ID on parent
                self.table.insert(group_id, "end", iid=child_id, values=child_values,
                                  tags=("child", "source:" + str(item.get("source_label") or source_label(item.get("source_url", "")))))
        self.table.tag_configure("even", background=PANEL)
        self.table.tag_configure("odd", background="#17253a")
        self.table.tag_configure("group", foreground="#f3f7fe")
        self.table.tag_configure("child", background="#0e1a2b", foreground="#b7c9e1")
        for label, (_, background) in SOURCE_STYLES.items():
            self.table.tag_configure("source:" + label, background=background)
        reviewed = sum(row["status"] != "Не опрацьований" for row in all_rows)
        self.count_text.set(f"Показано: {len(groups)} фірм / {len(values)} номерів із {len(all_rows)}  ·  "
                            f"Нові у базі: {len(all_rows) - reviewed}  ·  Опрацьовані: {reviewed}")
        hidden = len(all_rows) - len(values)
        if hidden:
            reasons = [("джерело", excluded_source), ("напрямки", excluded_category),
                       ("області", excluded_region), ("тип", excluded_ownership),
                       ("статус", excluded_status), ("пошук", excluded_search)]
            detail = "  ·  ".join(f"{label}: {count}" for label, count in reasons if count)
            self.filter_explanation.set(f"Приховано фільтрами {hidden} номерів ({detail}).")
        else:
            self.filter_explanation.set("Показані всі номери з бази. Групи можна розгортати стрілкою.")
        parents = [item for item in self.table.get_children() if self.table.get_children(item)]
        is_expanded = bool(parents) and all(bool(self.table.item(item, "open")) for item in parents)
        self.expand_btn.configure(text="Згорнути групи" if is_expanded else "Розгорнути групи")

    @staticmethod
    def contact_values(row):
        return (("☑" if row["status"] != "Не опрацьований" else "☐",
                 "● " + str(row.get("source_label") or source_label(row.get("source_url", "")))) + tuple(
            row.get(k, "") for k in
            ("name", "category", "region", "ownership", "edrpou", "phone",
             "verification", "verified_owner", "line_state", "role", "activity", "checked_at", "status")) +
            (visible_link(row),))

    def sync_status(self, _event=None):
        selected = self.table.selection()
        if selected and selected[0] in self.rows:
            row = self.rows[selected[0]]
            self.status_box.set(row["status"] if not row.get("is_group") else STATUSES[0])
            self.status_box.configure(state="disabled" if row.get("is_group") else "normal")
            self.ownership_selected.set(self.rows[selected[0]]["ownership"])
            self.ownership_selected.configure(state="normal")
        else:
            self.status_box.configure(state="disabled")
            self.ownership_selected.configure(state="disabled")

    def toggle_from_click(self, event):
        if self.table.identify_column(event.x) != "#1":
            return
        row_id = self.table.identify_row(event.y)
        if not row_id or row_id not in self.rows:
            return
        row = self.rows[row_id]
        if row.get("is_group"):
            return
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
        if self.rows[selected[0]].get("is_group"):
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
            if row.get("is_group"):
                if event and self.table.identify_column(event.x) == "#16":
                    self.copy_contact_field("website")
                    return
                if event and self.table.identify_region(event.x, event.y) == "tree":
                    return  # The native expand-arrow handler already does this.
                is_open = bool(self.table.item(selected[0], "open"))
                self.table.item(selected[0], open=not is_open)
                if is_open:
                    self.expanded_groups.discard(selected[0])
                else:
                    self.expanded_groups.add(selected[0])
                return
            if event and self.table.identify_column(event.x) == "#1":
                return
            if event and self.table.identify_column(event.x) == "#8":
                self.copy_contact_field("phone")
                return
            if event and self.table.identify_column(event.x) == "#16":
                self.copy_contact_field("website")
                return
            self.open_contact_link()

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
                    region = row.get("region", "Не визначено")
                    website = row.get("website", "")
                    imported_ownership = (row.get("ownership") or "").strip()
                    edrpou = (row.get("edrpou") or "").strip()
                    for phone in parse_ua_phones(raw):
                        count += save_contact(con, name, row.get("category", "Імпорт"),
                                              region, phone, website, row.get("source_url", ""),
                                              ownership=imported_ownership, edrpou=edrpou)
                        imported_status = (row.get("status") or "").strip()
                        if imported_status in STATUSES and imported_status != "Не опрацьований":
                            set_phone_status(con, phone, imported_status)
                    if imported_ownership in OWNERSHIP_TYPES:
                        company = (con.execute("SELECT id FROM companies WHERE edrpou=? LIMIT 1", (edrpou,)).fetchone()
                                   if edrpou else None)
                        if not company:
                            company = con.execute("SELECT id FROM companies WHERE company_key=?",
                                                  (company_key(name, website, region),)).fetchone()
                        if company:
                            set_company_ownership(con, company[0], imported_ownership)
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
                rows = read_contacts(con, ("Не опрацьований",) if self.only_new.get() else (),
                                     include_prozorro=self.show_prozorro.get(),
                                     categories=self.list_categories, regions=self.list_regions)
            rows = filter_ownership(rows, self.ownership_mode.get())
            rows = filter_contacts(rows, self.search_var.get())
            export_csv(Path(path), rows)
            messagebox.showinfo("Експорт", f"Збережено {len(rows)} контактів.")


def main():
    App().mainloop()


if __name__ == "__main__":
    main()
