"""Desktop UI for the Groundwork Simulator offline save trainer.

Run ``python trainer.py``. All game-file operations live in trainer_core.py.
The UI never writes game files during startup or inspection.
"""
from __future__ import annotations

import argparse
import math
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Callable

import trainer_core as core
import pump_live


BG = "#10151b"
CARD = "#19212a"
FIELD = "#111820"
TEXT = "#e7edf3"
MUTED = "#a3b2c0"
ACCENT = "#edc264"


def optional_number(value: str, title: str, minimum: float, maximum: float) -> float | None:
    """A blank field means preserve the existing value."""
    value = value.strip()
    if not value:
        return None
    try:
        result = float(value)
    except ValueError as exc:
        raise ValueError(f"{title} must be a number, or blank to leave it unchanged.") from exc
    if not math.isfinite(result) or not minimum <= result <= maximum:
        raise ValueError(f"{title} must be between {minimum:g} and {maximum:g}.")
    return result


def money_amount(value: str) -> int:
    value = value.strip() or "0"
    try:
        result = int(value)
    except ValueError as exc:
        raise ValueError("Money to add must be a whole number, for example 100000.") from exc
    if result < 0 or result > 1_000_000_000:
        raise ValueError("Money to add must be between 0 and 1,000,000,000 euros.")
    return result


def describe(result: object) -> str:
    if isinstance(result, (list, tuple)):
        return "\n".join(str(item) for item in result)
    if isinstance(result, dict):
        return "\n".join(f"{key}: {value}" for key, value in result.items())
    return str(result)


class TrainerApp:
    def __init__(self, root: tk.Tk, save_dir: Path | None = None, settings: Path | None = None,
                 smoke_test: bool = False) -> None:
        self.root = root
        self.root.title("Groundwork Simulator — Trainer")
        self.root.geometry("840x680")
        self.root.minsize(740, 600)
        self.root.configure(background=BG)
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.events: queue.Queue = queue.Queue()
        self.busy = False
        self.closed = False
        self.current_operation = ""
        self.save_paths: list[Path] = []
        self.listed_directory: Path | None = None
        self.last_backup: Path | None = None
        self.inspected_paths: tuple[Path, Path] | None = None
        self.controls: list[tuple[tk.Widget, str]] = []
        detected_save, detected_settings = core.default_paths()
        self.save_dir = tk.StringVar(value=str(save_dir or detected_save))
        self.settings = tk.StringVar(value=str(settings or detected_settings))
        self.slot = tk.StringVar()
        self.money = tk.StringVar(value="0")
        self.fuel = tk.BooleanVar(value=False)
        self.engine = tk.StringVar()
        self.hydraulic = tk.StringVar()
        self.pump_capacity = tk.StringVar(value="200")
        self.pump_status = tk.StringVar(value="Game status has not been checked. Use Check game below.")
        self.snapshot = tk.StringVar(value="Select a save slot, then inspect its current values.")
        self.settings_snapshot = tk.StringVar(value="Engine and hydraulic settings have not been inspected.")
        self.status = tk.StringVar(value="Ready · No game files have been changed.")
        self._theme()
        self._build()
        self.root.after(100, self._poll)
        if smoke_test:
            self.status.set("Smoke test passed: desktop controls created.")
            self.root.after(120, self._close)
        else:
            self.root.after(150, self.refresh_saves)

    def _theme(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        self.root.option_add("*Font", "{Segoe UI} 10")
        style.configure(".", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("Title.TLabel", font=("Segoe UI Semibold", 22), foreground=TEXT)
        style.configure("Section.TLabel", background=CARD, foreground=ACCENT,
                        font=("Segoe UI Semibold", 10))
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT,
                        insertcolor=TEXT, bordercolor="#354554", padding=5)
        style.map("TEntry", fieldbackground=[("disabled", CARD)],
                  foreground=[("disabled", MUTED)])
        style.configure("TCombobox", fieldbackground=FIELD, foreground=TEXT,
                        background="#2a3744", arrowcolor=TEXT, bordercolor="#354554", padding=4)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD), ("disabled", CARD)],
                  selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", TEXT)],
                  foreground=[("disabled", MUTED)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)
        self.root.option_add("*TCombobox*Listbox.selectBackground", "#3c5063")
        style.configure("TButton", background="#2a3744", foreground=TEXT, padding=(11, 7),
                        borderwidth=0, font=("Segoe UI", 9))
        style.map("TButton", background=[("active", "#3a4c5c"), ("disabled", "#222b35")],
                  foreground=[("disabled", "#768594")])
        style.configure("Accent.TButton", background=ACCENT, foreground="#161c22",
                        padding=(18, 9), font=("Segoe UI Semibold", 10))
        style.map("Accent.TButton", background=[("active", "#ffda80"), ("disabled", "#5d563e")],
                  foreground=[("disabled", "#a49d84")])
        style.configure("TCheckbutton", background=CARD, foreground=TEXT,
                        indicatorbackground=FIELD, indicatorforeground=ACCENT)
        style.map("TCheckbutton", background=[("active", CARD)],
                  foreground=[("disabled", MUTED)], indicatorbackground=[("selected", ACCENT)])
        style.configure("Vertical.TScrollbar", background="#344554", troughcolor=BG,
                        borderwidth=0, arrowcolor=MUTED)
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=FIELD,
                        bordercolor=BG, lightcolor=ACCENT, darkcolor=ACCENT)

    def _interactive(self, widget: tk.Widget, state: str = "normal") -> tk.Widget:
        self.controls.append((widget, state))
        return widget

    def _label(self, parent: tk.Widget, text: str, **kwargs) -> ttk.Label:
        return ttk.Label(parent, text=text, style="Card.TLabel", **kwargs)

    def _build(self) -> None:
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        header = ttk.Frame(self.root, padding=(22, 15, 22, 10))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="GROUNDWORK", style="Title.TLabel").pack(anchor="w")
        ttk.Label(header, text="SAVE TRAINER  /  Money, fuel and machine tuning", style="Muted.TLabel").pack(anchor="w")

        middle = ttk.Frame(self.root)
        middle.grid(row=1, column=0, sticky="nsew")
        middle.rowconfigure(0, weight=1)
        middle.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(middle, background=BG, highlightthickness=0, borderwidth=0)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(middle, orient="vertical", command=self.canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.canvas.configure(yscrollcommand=scrollbar.set)
        content = ttk.Frame(self.canvas, padding=(22, 0, 10, 8))
        content.columnconfigure(0, weight=1)
        content_id = self.canvas.create_window((0, 0), window=content, anchor="nw")
        content.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(content_id, width=event.width))
        self.root.bind_all("<MouseWheel>", self._mousewheel)

        paths = ttk.Frame(content, padding=13, style="Card.TFrame")
        paths.grid(row=0, column=0, sticky="ew", pady=(0, 9))
        paths.columnconfigure(1, weight=1)
        self._label(paths, "Save folder").grid(row=0, column=0, sticky="w", padx=(0, 10), pady=3)
        self._interactive(ttk.Entry(paths, textvariable=self.save_dir)).grid(row=0, column=1, sticky="ew", pady=3)
        self._interactive(ttk.Button(paths, text="Browse…", command=self.browse_save_dir)).grid(row=0, column=2, padx=(8, 0), pady=3)
        self._label(paths, "Save slot").grid(row=1, column=0, sticky="w", padx=(0, 10), pady=3)
        self.slot_box = ttk.Combobox(paths, textvariable=self.slot, state="readonly")
        self._interactive(self.slot_box, "readonly").grid(row=1, column=1, sticky="ew", pady=3)
        self.slot_box.bind("<<ComboboxSelected>>", lambda _event: self.inspect_selected())
        self._interactive(ttk.Button(paths, text="Refresh", command=self.refresh_saves)).grid(row=1, column=2, padx=(8, 0), pady=3, sticky="ew")
        self._label(paths, "Game settings").grid(row=2, column=0, sticky="w", padx=(0, 10), pady=3)
        self._interactive(ttk.Entry(paths, textvariable=self.settings)).grid(row=2, column=1, sticky="ew", pady=3)
        self._interactive(ttk.Button(paths, text="Browse…", command=self.browse_settings)).grid(row=2, column=2, padx=(8, 0), pady=3)

        snapshot = ttk.Frame(content, padding=(13, 10), style="Card.TFrame")
        snapshot.grid(row=1, column=0, sticky="ew", pady=(0, 9))
        snapshot.columnconfigure(0, weight=1)
        ttk.Label(snapshot, textvariable=self.snapshot, style="Card.TLabel", wraplength=680).grid(row=0, column=0, sticky="w")
        ttk.Label(snapshot, textvariable=self.settings_snapshot, style="CardMuted.TLabel", wraplength=680).grid(row=1, column=0, sticky="w", pady=(5, 0))

        changes = ttk.Frame(content, padding=13, style="Card.TFrame")
        changes.grid(row=2, column=0, sticky="ew")
        changes.columnconfigure(0, weight=1)
        changes.columnconfigure(1, weight=1)
        funds = ttk.Frame(changes, style="Card.TFrame", padding=(0, 0, 18, 0))
        funds.grid(row=0, column=0, sticky="nsew")
        funds.columnconfigure(0, weight=1)
        ttk.Label(funds, text="MONEY & FUEL", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        self._label(funds, "Add money (€)").grid(row=1, column=0, sticky="w")
        self._interactive(ttk.Entry(funds, textvariable=self.money, width=13)).grid(row=1, column=1, sticky="e")
        ttk.Label(funds, text="Adds to the selected save; 0 leaves money unchanged.",
                  style="CardMuted.TLabel", wraplength=310).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 8))
        self._interactive(ttk.Checkbutton(funds, text="Refill machine fuel to 100%", variable=self.fuel)).grid(row=3, column=0, columnspan=2, sticky="w", pady=(1, 0))

        tuning = ttk.Frame(changes, style="Card.TFrame", padding=(8, 0, 0, 0))
        tuning.grid(row=0, column=1, sticky="nsew")
        tuning.columnconfigure(0, weight=1)
        ttk.Label(tuning, text="MACHINE SETTINGS", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 7))
        self._label(tuning, "Engine limit · 50–300 kW").grid(row=1, column=0, sticky="w", padx=(0, 8))
        self._interactive(ttk.Entry(tuning, textvariable=self.engine, width=9)).grid(row=1, column=1, sticky="e")
        self._label(tuning, "Hydraulic flow · 1–3").grid(row=2, column=0, sticky="w", pady=(6, 0), padx=(0, 8))
        self._interactive(ttk.Entry(tuning, textvariable=self.hydraulic, width=9)).grid(row=2, column=1, sticky="e", pady=(6, 0))
        ttk.Label(tuning, text="Global hydraulic engine power and flow budget. Blank = unchanged; separate from water pumps.",
                  style="CardMuted.TLabel", wraplength=330).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        water = ttk.Frame(content, padding=13, style="Card.TFrame")
        water.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        water.columnconfigure(0, weight=1)
        ttk.Label(water, text="EXPERIMENTAL · LIVE WATER PUMPS", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w")
        ttk.Label(water, text="Run the game to adjust global nominal pump capacity for the wash plant. Not gameplay verified.",
                  style="CardMuted.TLabel", wraplength=690).grid(row=1, column=0, columnspan=4, sticky="w", pady=(4, 7))
        capacity_row = ttk.Frame(water, style="Card.TFrame")
        capacity_row.grid(row=2, column=0, sticky="w")
        self._label(capacity_row, "Capacity (L/s)").pack(side="left", padx=(0, 8))
        self._interactive(ttk.Combobox(capacity_row, textvariable=self.pump_capacity,
                                      values=("100", "200", "300", "500", "1000"), width=7,
                                      state="readonly"), "readonly").pack(side="left")
        self._interactive(ttk.Button(water, text="Check game", command=self.inspect_pumps)).grid(row=2, column=1, padx=(8, 0))
        self._interactive(ttk.Button(water, text="Apply live", command=lambda: self.change_pumps(False))).grid(row=2, column=2, padx=(8, 0))
        self._interactive(ttk.Button(water, text="Restore 100 L/s", command=lambda: self.change_pumps(True))).grid(row=2, column=3, padx=(8, 0))
        ttk.Label(water, textvariable=self.pump_status, style="Card.TLabel", wraplength=690).grid(row=3, column=0, columnspan=4, sticky="w", pady=(8, 4))
        ttk.Label(water, text="Intake, hoses, head and downstream demand still limit actual flow. Close this trainer and the live setting remains active; restore 100 L/s or restart the game to reset.",
                  style="CardMuted.TLabel", wraplength=690).grid(row=4, column=0, columnspan=4, sticky="w")

        footer = ttk.Frame(self.root, padding=(22, 10, 22, 14))
        footer.grid(row=2, column=0, sticky="ew")
        footer.columnconfigure(0, weight=1)
        ttk.Label(footer, text="Save changes: close the game first, then reload the save / restart after applying.",
                  style="Muted.TLabel", wraplength=730).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 9))
        self._interactive(ttk.Button(footer, text="Restore backup…", command=self.restore)).grid(row=1, column=0, sticky="w")
        self._interactive(ttk.Button(footer, text="Inspect save", command=self.inspect_selected)).grid(row=1, column=1, sticky="e", padx=(8, 8))
        self._interactive(ttk.Button(footer, text="Back up & apply", style="Accent.TButton", command=self.apply)).grid(row=1, column=2, sticky="e")
        self.progress = ttk.Progressbar(footer, mode="indeterminate", maximum=100)
        self.progress.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(10, 7))
        self.status_label = ttk.Label(footer, textvariable=self.status, style="Muted.TLabel", wraplength=750)
        self.status_label.grid(row=3, column=0, columnspan=3, sticky="w")

    def _mousewheel(self, event) -> None:
        # Leave the slot selector's own wheel behavior alone.
        if event.widget is self.slot_box:
            return
        if self.canvas.bbox("all") and self.canvas.bbox("all")[3] > self.canvas.winfo_height():
            self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def _set_busy(self, busy: bool, label: str = "") -> None:
        self.busy = busy
        for control, normal_state in self.controls:
            control.configure(state="disabled" if busy else normal_state)
        if busy:
            self.current_operation = label
            self.status.set(label)
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(value=0)

    def _run(self, label: str, operation: Callable, finished: Callable) -> None:
        if self.busy:
            return
        self._set_busy(True, label)

        def worker() -> None:
            try:
                result = operation()
                self.events.put((finished, result, None))
            except Exception as exc:
                self.events.put((finished, None, str(exc) or type(exc).__name__))

        threading.Thread(target=worker, name="save-trainer-worker", daemon=True).start()

    def _poll(self) -> None:
        if self.closed:
            return
        try:
            finished, result, error = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._set_busy(False)
            if error:
                self.status.set(f"Could not complete operation: {error}")
                messagebox.showerror("Groundwork Save Trainer", error, parent=self.root)
            else:
                try:
                    finished(result)
                except Exception as exc:
                    self.status.set(f"Could not display result: {exc}")
                    messagebox.showerror("Groundwork Save Trainer", str(exc), parent=self.root)
        self.root.after(100, self._poll)

    def _selected(self) -> Path:
        index = self.slot_box.current()
        if index < 0 or index >= len(self.save_paths):
            raise ValueError("Choose a save slot first. Use Browse or Refresh to find your saves.")
        current_directory = Path(self.save_dir.get().strip()).expanduser().resolve()
        if self.listed_directory != current_directory:
            raise ValueError("The save folder has changed. Click Refresh to select a slot in that folder first.")
        return self.save_paths[index]

    def _paths(self) -> tuple[Path, Path]:
        save = self._selected()
        settings_text = self.settings.get().strip()
        if not settings_text:
            raise ValueError("Choose the game settings file first.")
        return save, Path(settings_text).expanduser()

    def browse_save_dir(self) -> None:
        chosen = filedialog.askdirectory(title="Select the Groundwork Simulator save folder",
                                         initialdir=self.save_dir.get() or None, parent=self.root)
        if chosen:
            self.save_dir.set(chosen)
            self.refresh_saves()

    def browse_settings(self) -> None:
        chosen = filedialog.askopenfilename(title="Select the game settings file",
                                            initialdir=str(Path(self.settings.get()).parent),
                                            filetypes=[("All files", "*.*")], parent=self.root)
        if chosen:
            self.settings.set(chosen)
            if self.slot_box.current() >= 0:
                self.inspect_selected()

    def refresh_saves(self) -> None:
        text = self.save_dir.get().strip()
        if not text:
            messagebox.showerror("Save folder required", "Choose your game save folder.", parent=self.root)
            return
        directory = Path(text).expanduser()
        previous = self.save_paths[self.slot_box.current()] if 0 <= self.slot_box.current() < len(self.save_paths) else None

        def loaded(paths: list[Path]) -> None:
            self.save_paths = [Path(path) for path in paths]
            self.listed_directory = directory.resolve()
            self.slot_box.configure(values=[path.name for path in self.save_paths])
            self.inspected_paths = None
            if not self.save_paths:
                self.slot.set("")
                self.snapshot.set("No save slots found in this folder.")
                self.settings_snapshot.set("Create a save in the game, then refresh this list.")
                self.status.set("No save slots found. Choose the folder containing the game's saves.")
                return
            selected = self.save_paths.index(previous) if previous in self.save_paths else 0
            self.slot_box.current(selected)
            self.inspect_selected()

        self._run("Looking for save slots…", lambda: core.list_saves(directory), loaded)

    @staticmethod
    def _read(save: Path, settings: Path) -> tuple[dict, dict | None, str | None]:
        save_info = core.inspect_save(save)
        try:
            settings_info = core.inspect_settings(settings)
            return save_info, settings_info, None
        except Exception as exc:
            # Money and fuel inspection remains useful when settings are absent.
            return save_info, None, str(exc)

    def _show_inspection(self, paths: tuple[Path, Path], result: tuple[dict, dict | None, str | None]) -> None:
        info, settings_info, settings_error = result
        self.inspected_paths = paths
        balance = info.get("opening_balance_eur")
        balance_text = f"€{balance:,.0f}" if isinstance(balance, (int, float)) else "unavailable"
        low, high = info.get("fuel_min_percent"), info.get("fuel_max_percent")
        if isinstance(low, (int, float)) and isinstance(high, (int, float)):
            fuel_text = f"{low:g}–{high:g}%" if low != high else f"{low:g}%"
        else:
            fuel_text = "unavailable"
        self.snapshot.set(
            f"{info.get('display_name', paths[0].stem)}  ·  {info.get('machine_count', 0)} machines  ·  "
            f"{info.get('pump_count', 0)} water pumps\n"
            f"Opening balance: {balance_text}  ·  Machine fuel: {fuel_text}"
        )
        if settings_info is not None:
            self.settings_snapshot.set(
                f"Current global settings: engine {settings_info.get('engine_kw', '—')} kW  ·  "
                f"hydraulic flow {settings_info.get('hydraulic_flow', '—')}"
            )
        else:
            self.settings_snapshot.set(f"Settings could not be inspected: {settings_error}")

    def inspect_selected(self) -> None:
        if self.busy:
            return
        try:
            paths = self._paths()
        except ValueError as exc:
            messagebox.showerror("Select a save", str(exc), parent=self.root)
            return

        def inspected(result) -> None:
            self._show_inspection(paths, result)
            self.status.set("Inspection complete · No game files have been changed.")

        self._run("Reading the selected save and settings…", lambda: self._read(*paths), inspected)

    def _options(self) -> dict:
        options = {
            "add_money": money_amount(self.money.get()),
            "refill_fuel": bool(self.fuel.get()),
            "engine_kw": optional_number(self.engine.get(), "Engine power", 50, 300),
            "hydraulic_flow": optional_number(self.hydraulic.get(), "Hydraulic flow", 1, 3),
        }
        if not (options["add_money"] or options["refill_fuel"] or
                options["engine_kw"] is not None or options["hydraulic_flow"] is not None):
            raise ValueError("Enter money to add, select fuel refill, or enter a machine setting.")
        return options

    def apply(self) -> None:
        if self.busy:
            return
        try:
            paths = self._paths()
            options = self._options()
        except ValueError as exc:
            messagebox.showerror("Check your changes", str(exc), parent=self.root)
            return

        def perform() -> tuple[dict, object, str | None]:
            if core.is_game_running():
                raise RuntimeError("Close Groundwork Simulator before applying changes, then try again.")
            result = core.apply_changes(*paths, options)
            try:
                return result, self._read(*paths), None
            except Exception as exc:
                return result, None, str(exc)

        def applied(payload) -> None:
            result, inspection, inspection_error = payload
            backup = result.get("backup_dir")
            self.last_backup = Path(backup) if backup else None
            self.money.set("0")
            self.fuel.set(False)
            self.engine.set("")
            self.hydraulic.set("")
            if inspection:
                self._show_inspection(paths, inspection)
            summary = describe(result.get("summary", "Changes applied successfully."))
            self.status.set("Changes applied and backed up. Reload your save / restart the game.")
            details = f"{summary}\n\nBackup: {backup or 'See backend result'}\n\nReload your save / restart the game to use the changes."
            if inspection_error:
                details += f"\n\nThe changes were saved, but refreshing the display failed: {inspection_error}"
            messagebox.showinfo("Changes applied", details, parent=self.root)

        self._run("Creating backup and applying your changes…", perform, applied)

    def restore(self) -> None:
        if self.busy:
            return
        initial = str(self.last_backup.parent) if self.last_backup else self.save_dir.get()
        selected = filedialog.askdirectory(title="Select a trainer backup folder to restore",
                                           initialdir=initial or None, parent=self.root)
        if not selected:
            return
        backup = Path(selected)
        confirmed = messagebox.askyesno(
            "Restore this backup?",
            f"Restore the game files recorded in this backup?\n\n{backup}\n\n"
            "This replaces the matching current save/settings files with their backed-up versions. "
            "Automatic restore refuses to overwrite newer progress or settings.\n\n"
            "Close Groundwork Simulator before continuing.",
            icon="warning", default="no", parent=self.root,
        )
        if not confirmed:
            return

        def perform() -> dict:
            if core.is_game_running():
                raise RuntimeError("Close Groundwork Simulator before restoring a backup, then try again.")
            return core.restore_backup(backup)

        def restored(result) -> None:
            self.inspected_paths = None
            self.snapshot.set("Backup restored. Inspect the selected save to refresh its current values.")
            self.settings_snapshot.set("Reload your save / restart the game to use the restored files.")
            self.status.set("Backup restored. Click Inspect save to refresh the display.")
            messagebox.showinfo("Backup restored", describe(result.get("summary", "Backup restored successfully.")), parent=self.root)

        self._run("Restoring the selected backup…", perform, restored)

    def _show_pump_state(self, state: dict) -> None:
        running = bool(state.get("running"))
        capacity = state.get("capacity_lps")
        note = str(state.get("status") or "").strip()
        if not running:
            self.pump_status.set("Game not detected. " + (note or "Start Groundwork Simulator, then check again."))
        elif capacity is None:
            self.pump_status.set("Game is running. " + (note or "Current capacity could not be read."))
        else:
            self.pump_status.set(f"Game is running · Current nominal capacity: {capacity:g} L/s" + (f" · {note}" if note else ""))

    def inspect_pumps(self) -> None:
        def inspected(state: dict) -> None:
            self._show_pump_state(state)
            self.status.set("Live-pump inspection complete. No capacity setting was changed.")

        self._run("Checking the running game and water-pump capacity…", pump_live.read_state, inspected)

    def change_pumps(self, restore: bool) -> None:
        if self.busy:
            return
        try:
            capacity = 100 if restore else int(self.pump_capacity.get())
            if capacity not in (100, 200, 300, 500, 1000):
                raise ValueError("Select a supported pump capacity from the list.")
        except ValueError as exc:
            messagebox.showerror("Check pump capacity", str(exc), parent=self.root)
            return

        def perform():
            state = pump_live.read_state()
            if not state.get("running"):
                raise RuntimeError("Start Groundwork Simulator before changing live water-pump capacity.")
            result = pump_live.restore_capacity() if restore else pump_live.set_capacity(capacity)
            try:
                return result, pump_live.read_state(), None
            except Exception as exc:
                return result, None, str(exc)

        def changed(payload) -> None:
            result, state, read_error = payload
            if state:
                self._show_pump_state(state)
            summary = describe(result.get("summary", f"Nominal water-pump capacity changed to {capacity} L/s."))
            self.status.set(summary)
            if restore:
                self.pump_capacity.set("100")
            details = summary + "\n\nThis control is experimental and has not been verified in gameplay. Actual flow still depends on the connected water system."
            if not restore:
                details += "\n\nUse Restore 100 L/s or restart the game to reset. Closing the trainer does not reset it."
            if read_error:
                details += "\n\nThe update completed, but reading the new capacity failed: " + read_error
            messagebox.showinfo("Live water pumps", details, parent=self.root)

        label = "Restoring normal water-pump capacity…" if restore else f"Applying {capacity} L/s nominal water-pump capacity…"
        self._run(label, perform, changed)

    def _close(self) -> None:
        if self.busy:
            messagebox.showinfo("Operation in progress", "Please wait for the current operation to finish before closing.", parent=self.root)
            return
        if pump_live.has_pending_recovery():
            messagebox.showerror(
                "Water-pump recovery required",
                "The game is paused after an interrupted pump change. Use Restore 100 L/s before closing the trainer; otherwise close the game yourself.",
                parent=self.root,
            )
            return
        self.closed = True
        self.root.destroy()


def main() -> int:
    parser = argparse.ArgumentParser(description="Groundwork Simulator offline save trainer")
    parser.add_argument("--save-dir", type=Path, help="Folder containing the game's save slots")
    parser.add_argument("--settings", type=Path, help="Game settings file")
    parser.add_argument("--smoke-test", action="store_true", help="Create the UI, then exit without reading or changing saves")
    parser.add_argument("--diagnostics", type=Path, help="Write read-only save/settings diagnostics and exit")
    args = parser.parse_args()
    if args.diagnostics:
        import json
        save_dir, settings_path = core.default_paths()
        saves = core.list_saves(args.save_dir or save_dir)
        result = {
            "settings": core.inspect_settings(args.settings or settings_path),
            "save": core.inspect_save(saves[0]) if saves else None,
            "pumps": pump_live.read_state(),
            "writes_to_game": False,
        }
        args.diagnostics.write_text(json.dumps(result, indent=2), encoding="utf-8")
        return 0
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
    TrainerApp(root, args.save_dir, args.settings, smoke_test=args.smoke_test)
    root.mainloop()
    if args.smoke_test:
        print("UI smoke test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
