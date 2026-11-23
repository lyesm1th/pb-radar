"""Dedicated workspace for contacts selected with the checkmark in PB Radar."""
from __future__ import annotations

import calendar
import datetime as dt
import tkinter as tk
import webbrowser
from tkinter import messagebox, ttk

import customtkinter as ctk

from .core import (CONTACT_STATUSES, connect_db, read_work_contacts,
                   save_work_result, set_work_selected, sort_work_contacts,
                   visible_link)
from .sources import SOURCE_STYLES, source_label

BG = "#090f1c"
PANEL = "#121d30"
PANEL_RAISED = "#182740"
TEXT = "#eaf0fa"
MUTED = "#8da0ba"
ACCENT = "#dc2934"
FONT = "Segoe UI"


class WorkQueueWindow(ctk.CTkToplevel):
    """Contacts in work; phone is the stable identifier, not a row index."""

    def __init__(self, master, *, on_change):
        super().__init__(master)
        self.title("Polski Bramy · Контакти в роботі")
        self.geometry("1220x750")
        self.minsize(970, 610)
        self.configure(fg_color=BG)
        self.on_change = on_change
        self.active_phone: str | None = None
        self.rows: dict[str, dict] = {}
        self.dirty = False
        self._loading = False
        self._selection_lock = False
        self.sort_key = "last_contact_date"
        self.sort_descending = True
        self.date_picker = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(16, 9))
        ctk.CTkLabel(top, text="КОНТАКТИ В РОБОТІ", text_color=TEXT,
                     font=(FONT, 19, "bold")).pack(side="left")
        self.counter = tk.StringVar(value="")
        ctk.CTkLabel(top, textvariable=self.counter, text_color=MUTED,
                     font=(FONT, 12)).pack(side="left", padx=18)
        ctk.CTkButton(top, text="↻ Оновити", width=105, fg_color=PANEL_RAISED,
                      command=self.refresh).pack(side="right")

        content = ctk.CTkFrame(self, fg_color="transparent")
        content.pack(fill="both", expand=True, padx=20, pady=(0, 17))
        left = ctk.CTkFrame(content, fg_color=PANEL)
        right = ctk.CTkFrame(content, fg_color=PANEL)
        left.pack(side="left", fill="both", expand=True, padx=(0, 9))
        right.pack(side="left", fill="both", expand=False, padx=(9, 0))
        right.configure(width=365)
        right.pack_propagate(False)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self.refresh())
        ctk.CTkEntry(left, textvariable=self.search_var,
                     placeholder_text="Пошук за компанією, номером, статусом або результатом…",
                     fg_color="#0d1727", border_color="#29445f", height=38
                     ).pack(fill="x", padx=12, pady=12)

        table_box = tk.Frame(left, bg=PANEL, highlightthickness=0)
        table_box.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        columns = ("name", "phone", "status", "last_contact_date", "contact_result", "source")
        style = ttk.Style(self)
        style.configure("PBWork.Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, rowheight=31, font=(FONT, 10))
        style.configure("PBWork.Treeview.Heading", background=PANEL_RAISED,
                        foreground=TEXT, font=(FONT, 10, "bold"))
        style.map("PBWork.Treeview", background=[("selected", "#60303b")],
                  foreground=[("selected", "#ffffff")])
        self.table = ttk.Treeview(table_box, columns=columns, show="headings",
                                  selectmode="browse", style="PBWork.Treeview")
        self.heading_titles = {}
        for key, title, width in (
            ("name", "Компанія", 195), ("phone", "Телефон", 140),
            ("status", "Поточний статус", 156),
            ("last_contact_date", "Дата", 106),
            ("contact_result", "Результат контакту", 250),
            ("source", "Джерело", 115),
        ):
            self.heading_titles[key] = title
            self.table.heading(key, text=title, command=lambda field=key: self.sort_by(field))
            self.table.column(key, width=width, minwidth=65, stretch=False)
        self.update_sort_headings()
        y = ttk.Scrollbar(table_box, orient="vertical", command=self.table.yview)
        x = ttk.Scrollbar(table_box, orient="horizontal", command=self.table.xview)
        self.table.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        y.pack(side="right", fill="y")
        x.pack(side="bottom", fill="x")
        self.table.pack(fill="both", expand=True)
        self.table.bind("<<TreeviewSelect>>", self.choose_row)
        for label, (_, color) in SOURCE_STYLES.items():
            self.table.tag_configure("src:" + label, background=color)

        ctk.CTkLabel(right, text="КАРТКА КОНТАКТУ", font=(FONT, 15, "bold"),
                     text_color=TEXT).pack(anchor="w", padx=16, pady=(17, 8))
        self.selected_name = tk.StringVar(value="Оберіть контакт зліва")
        ctk.CTkLabel(right, textvariable=self.selected_name, text_color=TEXT,
                     wraplength=330, justify="left", font=(FONT, 12, "bold")
                     ).pack(fill="x", padx=16, pady=(0, 8))
        self.selected_phone = tk.StringVar(value="")
        ctk.CTkLabel(right, textvariable=self.selected_phone,
                     text_color="#8ac2ff", font=(FONT, 13)
                     ).pack(anchor="w", padx=16, pady=(0, 8))
        self.copy_button = ctk.CTkButton(right, text="Копіювати номер",
                                         fg_color=PANEL_RAISED, command=self.copy_phone)
        self.copy_button.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkLabel(right, text="Поточний статус", text_color=MUTED,
                     font=(FONT, 11)).pack(anchor="w", padx=16)
        self.status_field = ctk.CTkOptionMenu(
            right, values=list(CONTACT_STATUSES), command=lambda _: self.mark_dirty(with_date=True),
            fg_color="#233a58", button_color="#2d5b81")
        self.status_field.pack(fill="x", padx=16, pady=(4, 11))
        ctk.CTkLabel(right, text="Дата останнього контакту (РРРР-ММ-ДД)",
                     text_color=MUTED, font=(FONT, 10)).pack(anchor="w", padx=16)
        self.date_var = tk.StringVar()
        self.date_var.trace_add("write", lambda *_: self.mark_dirty())
        date_line = ctk.CTkFrame(right, fg_color="transparent")
        date_line.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkEntry(date_line, textvariable=self.date_var,
                     placeholder_text="Автоматично після результату",
                     fg_color="#0d1727", border_color="#29445f", width=190
                     ).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(date_line, text="▦", width=39, fg_color=PANEL_RAISED,
                      hover_color="#29445f", command=self.open_calendar
                      ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(date_line, text="Сьогодні", width=64, fg_color=PANEL_RAISED,
                      hover_color="#29445f", command=self.set_today
                      ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(right, text="Результат розмови / коментар",
                     text_color=MUTED, font=(FONT, 11)).pack(anchor="w", padx=16)
        self.result = ctk.CTkTextbox(right, wrap="word", fg_color="#0d1727",
                                     border_width=1, border_color="#29445f",
                                     font=(FONT, 11))
        self.result.pack(fill="both", expand=True, padx=16, pady=(5, 12))
        self.result.bind("<KeyRelease>", lambda _: self.mark_dirty(with_date=True))
        self.save_button = ctk.CTkButton(right, text="Зберегти результат", fg_color=ACCENT,
                                         command=self.save)
        self.save_button.pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(right, text="Відкрити сайт / джерело",
                      fg_color=PANEL_RAISED, command=self.open_link
                      ).pack(fill="x", padx=16, pady=(0, 8))
        ctk.CTkButton(right, text="↩ Повернути до загального списку",
                      fg_color=PANEL_RAISED, command=self.return_to_radar
                      ).pack(fill="x", padx=16, pady=(0, 14))

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def mark_dirty(self, *, with_date: bool = False):
        if self._loading or not self.active_phone:
            return
        if with_date and not self.date_var.get().strip():
            # A date is recorded when the manager edits the result/status,
            # not merely when the contact is moved to the work queue.
            self.date_var.set(dt.datetime.now().astimezone().date().isoformat())
        self.dirty = True

    def set_today(self):
        if self.active_phone:
            self.date_var.set(dt.datetime.now().astimezone().date().isoformat())

    def update_sort_headings(self):
        for field, title in self.heading_titles.items():
            marker = (" ↓" if self.sort_descending else " ↑") if field == self.sort_key else ""
            self.table.heading(field, text=title + marker)

    def sort_by(self, field: str):
        if self.dirty and not self._allow_leave():
            return
        if field == self.sort_key:
            self.sort_descending = not self.sort_descending
        else:
            self.sort_key = field
            self.sort_descending = field == "last_contact_date"
        self.update_sort_headings()
        self.refresh()

    def open_calendar(self):
        """Small built-in calendar: no paid service or new dependency."""
        if not self.active_phone:
            return
        if self.date_picker and self.date_picker.winfo_exists():
            self.date_picker.lift()
            self.date_picker.focus_force()
            return
        today = dt.datetime.now().astimezone().date()
        try:
            initial = dt.date.fromisoformat(self.date_var.get().strip())
        except ValueError:
            initial = today
        popup = ctk.CTkToplevel(self)
        self.date_picker = popup
        popup.title("Дата контакту")
        popup.geometry("320x345")
        popup.minsize(310, 330)
        popup.resizable(False, False)
        popup.configure(fg_color=BG)
        popup.transient(self)
        popup.lift()
        selected_month = [initial.year, initial.month]
        months = ("", "Січень", "Лютий", "Березень", "Квітень", "Травень",
                  "Червень", "Липень", "Серпень", "Вересень", "Жовтень", "Листопад", "Грудень")
        header = ctk.CTkFrame(popup, fg_color="transparent")
        header.pack(fill="x", padx=12, pady=(12, 6))
        caption = ctk.CTkLabel(header, text="", text_color=TEXT, font=(FONT, 14, "bold"))
        caption.pack(side="left", expand=True)
        cells = ctk.CTkFrame(popup, fg_color="transparent")
        cells.pack(fill="both", expand=True, padx=12)
        actions = ctk.CTkFrame(popup, fg_color="transparent")
        actions.pack(fill="x", padx=12, pady=(5, 12))

        def choose(day):
            self.date_var.set(day.isoformat())
            popup.destroy()
            self.date_picker = None

        def render():
            year, month = selected_month
            caption.configure(text=f"{months[month]} {year}")
            for widget in cells.winfo_children():
                widget.destroy()
            for col, weekday in enumerate(("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд")):
                ctk.CTkLabel(cells, text=weekday, text_color=MUTED, width=36,
                             font=(FONT, 11, "bold")).grid(row=0, column=col, padx=1, pady=1)
            for row, week in enumerate(calendar.monthcalendar(year, month), 1):
                for col, day in enumerate(week):
                    if not day:
                        continue
                    value = dt.date(year, month, day)
                    ctk.CTkButton(
                        cells, text=str(day), width=34, height=32,
                        fg_color=ACCENT if value == initial else PANEL_RAISED,
                        hover_color="#29445f",
                        state="disabled" if value > today else "normal",
                        command=lambda selected=value: choose(selected)
                    ).grid(row=row, column=col, padx=1, pady=1)

        def navigate(delta):
            year, month = selected_month
            offset = year * 12 + month - 1 + delta
            new_year, remainder = divmod(offset, 12)
            if new_year < 1900 or new_year > today.year:
                return
            new_month = remainder + 1
            if dt.date(new_year, new_month, 1) > today:
                return
            selected_month[:] = [new_year, new_month]
            render()

        ctk.CTkButton(header, text="‹", width=36, fg_color=PANEL_RAISED,
                      command=lambda: navigate(-1)).pack(side="left")
        ctk.CTkButton(header, text="›", width=36, fg_color=PANEL_RAISED,
                      command=lambda: navigate(1)).pack(side="right")
        ctk.CTkButton(actions, text="Сьогодні", fg_color=PANEL_RAISED,
                      command=lambda: choose(today)).pack(side="left", expand=True, fill="x", padx=(0, 6))
        ctk.CTkButton(actions, text="Очистити", fg_color=PANEL_RAISED,
                      command=lambda: choose_empty()).pack(side="right", expand=True, fill="x")

        def choose_empty():
            self.date_var.set("")
            popup.destroy()
            self.date_picker = None

        render()
        popup.focus_force()

    def _allow_leave(self) -> bool:
        if not self.dirty:
            return True
        choice = messagebox.askyesnocancel(
            "Незбережений результат", "Зберегти зміни до переходу?",
            parent=self)
        if choice is None:
            return False
        if choice:
            return self.save(reload=False)
        self.dirty = False
        return True

    def refresh(self):
        if self.dirty:
            return  # Do not overwrite notes being typed by a manager.
        with connect_db() as con:
            all_rows = read_work_contacts(con)
        needle = self.search_var.get().casefold().strip()
        filtered = [
            row for row in all_rows
            if not needle or needle in " ".join(str(row.get(k) or "") for k in
                ("name", "phone", "status", "category", "region", "contact_result",
                 "last_contact_date", "source_label")).casefold()
        ]
        field = "source_label" if self.sort_key == "source" else self.sort_key
        self.rows = {
            row["phone"]: row
            for row in sort_work_contacts(filtered, field, self.sort_descending)
        }
        self.counter.set(f"В роботі: {len(all_rows)}  ·  Знайдено: {len(self.rows)}")
        previous = self.active_phone
        self._selection_lock = True
        for child in self.table.get_children():
            self.table.delete(child)
        for row in self.rows.values():
            label = row.get("source_label") or source_label(row.get("source_url") or "")
            self.table.insert("", "end", iid=row["phone"],
                              values=(row["name"], row["phone"], row["status"],
                                      row["last_contact_date"], row["contact_result"][:110], label),
                              tags=("src:" + label,))
        if previous in self.rows:
            self.table.selection_set(previous)
            self.table.focus(previous)
            self._show(self.rows[previous])
        elif self.rows:
            first = next(iter(self.rows))
            self.table.selection_set(first)
            self.table.focus(first)
            self._show(self.rows[first])
        else:
            self._show(None)
        self._selection_lock = False

    def choose_row(self, _event=None):
        if self._selection_lock:
            return
        selected = self.table.selection()
        if not selected:
            return
        phone = selected[0]
        if phone == self.active_phone:
            return
        if not self._allow_leave():
            if self.active_phone and self.table.exists(self.active_phone):
                self._selection_lock = True
                self.table.selection_set(self.active_phone)
                self._selection_lock = False
            return
        self._show(self.rows.get(phone))

    def _show(self, row):
        self._loading = True
        self.active_phone = row["phone"] if row else None
        self.selected_name.set(row["name"] if row else "Оберіть контакт зліва")
        self.selected_phone.set(row["phone"] if row else "")
        self.status_field.set(row["status"] if row else CONTACT_STATUSES[0])
        self.date_var.set(row["last_contact_date"] if row else "")
        self.result.delete("1.0", "end")
        if row:
            self.result.insert("1.0", row["contact_result"])
        self.dirty = False
        self._loading = False

    def save(self, reload=True):
        if not self.active_phone:
            return False
        try:
            with connect_db() as con:
                save_work_result(con, self.active_phone, self.status_field.get(),
                                 self.result.get("1.0", "end-1c"), self.date_var.get().strip())
        except ValueError as exc:
            messagebox.showerror("Не збережено", str(exc), parent=self)
            return False
        self.dirty = False
        if reload:
            self.on_change()
            self.refresh()
        else:
            # Switching rows must finish before the main window repaints us.
            self.after_idle(self.on_change)
        return True

    def copy_phone(self):
        if self.active_phone:
            self.clipboard_clear()
            self.clipboard_append(self.active_phone)
            self.update_idletasks()

    def open_link(self):
        row = self.rows.get(self.active_phone or "")
        url = visible_link(row) if row else ""
        if url:
            webbrowser.open(url)

    def return_to_radar(self):
        if not self.active_phone or not self._allow_leave():
            return
        phone = self.active_phone
        with connect_db() as con:
            set_work_selected(con, phone, False)
        self.dirty = False
        self.active_phone = None
        self.on_change()
        self.refresh()

    def close(self):
        if self._allow_leave():
            self.destroy()
