"""Live-only desktop trainer. Startup and refresh are strictly read-only."""
from __future__ import annotations
import argparse
import json
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox

import pump_live
import money_live
import fuel_live
import engine_live

BG = "#10161c"
CARD = "#1a242e"
FIELD = "#101820"
TEXT = "#e9eef2"
MUTED = "#a4b3c0"
ACCENT = "#eac268"
MODULES = {"money": money_live, "fuel": fuel_live, "engine": engine_live, "pump": pump_live}


def inspect_all():
    return {name: module.read_state() for name, module in MODULES.items()}


def pending_recovery():
    return any(module.has_pending_recovery() for module in MODULES.values())


class App:
    def __init__(self, root, smoke=False):
        self.root, self.smoke = root, smoke
        self.busy = False
        self.events = queue.Queue()
        self.widgets = []
        root.title("Groundwork Simulator — Live Trainer")
        root.geometry("900x780")
        root.minsize(760, 620)
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.connection = tk.StringVar(value="Ready to check the running game")
        self.status = tk.StringVar(value="No changes are made until you choose an action.")
        self.states = {name: tk.StringVar(value="Not checked") for name in MODULES}
        self.money = tk.StringVar(value="1000000")
        self.engine = tk.StringVar(value="155")
        self.pump = tk.StringVar(value="100")
        self._style()
        self._layout()
        root.after(80, self.poll)
        if smoke:
            root.after(180, root.destroy)
        else:
            root.after(100, self.refresh)

    def _style(self):
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Segoe UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT)
        style.configure("Note.TLabel", background=CARD, foreground=MUTED, font=("Segoe UI", 9))
        style.configure("State.TLabel", background=CARD, foreground=ACCENT, font=("Segoe UI", 9))
        style.configure("TButton", background="#293846", foreground=TEXT,
                        borderwidth=0, padding=(13, 9), font=("Segoe UI", 10))
        style.map("TButton", background=[("active", "#394c5e"), ("disabled", "#202b35")],
                  foreground=[("disabled", "#667683")])
        style.configure("Accent.TButton", background=ACCENT, foreground=BG)
        style.map("Accent.TButton", background=[("active", "#f4d48c"), ("disabled", "#6e654d")])
        style.configure("TEntry", fieldbackground=FIELD, foreground=TEXT, insertcolor=TEXT, padding=7)
        style.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=TEXT, padding=7)
        style.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", TEXT)])
        self.root.option_add("*TCombobox*Listbox.background", FIELD)
        self.root.option_add("*TCombobox*Listbox.foreground", TEXT)

    def button(self, parent, text, command, accent=False):
        item = ttk.Button(parent, text=text, command=command,
                          style="Accent.TButton" if accent else "TButton")
        self.widgets.append((item, "normal"))
        return item

    def entry(self, parent, var, width=13):
        item = ttk.Entry(parent, textvariable=var, width=width)
        self.widgets.append((item, "normal"))
        return item

    def _layout(self):
        top = ttk.Frame(self.root, padding=(24, 18, 24, 8))
        top.pack(fill="x")
        tk.Label(top, text="GROUNDWORK", bg=BG, fg=ACCENT,
                 font=("Segoe UI Semibold", 11), anchor="w").pack(anchor="w")
        tk.Label(top, text="Live trainer", bg=BG, fg=TEXT,
                 font=("Segoe UI Semibold", 26), anchor="w").pack(anchor="w")
        ttk.Label(top, text="Money, fuel, engine power and water pumps — while you play.",
                  foreground=MUTED).pack(anchor="w", pady=(2, 12))
        row = ttk.Frame(top)
        row.pack(fill="x")
        ttk.Label(row, textvariable=self.connection, foreground=ACCENT,
                  wraplength=660).pack(side="left", fill="x", expand=True)
        self.button(row, "Refresh", self.refresh).pack(side="right")

        holder = ttk.Frame(self.root)
        holder.pack(fill="both", expand=True)
        canvas = tk.Canvas(holder, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        content = ttk.Frame(canvas, padding=(24, 8, 15, 8))
        item = canvas.create_window(0, 0, window=content, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(item, width=e.width))
        content.bind("<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all")))
        self.root.bind("<MouseWheel>", lambda e: canvas.yview_scroll(-int(e.delta / 120), "units"))

        money = self.card(content, "01", "Money override", "money")
        controls = ttk.Frame(money, style="Card.TFrame")
        controls.pack(fill="x", pady=(9, 8))
        self.entry(controls, self.money).pack(side="left")
        ttk.Label(controls, text="EUR", style="Card.TLabel").pack(side="left", padx=(6, 14))
        self.button(controls, "Enable override", self.set_money, True).pack(side="left")
        self.button(controls, "Restore normal balance", lambda: self.run_action(
            "Restoring normal balance…", money_live.restore_money, True)).pack(side="left", padx=8)
        self.note(money, "Temporary spendable balance. Purchases still affect your underlying account; "
                  "restoring normal balance or restarting reveals that balance, which may be lower or negative.")

        fuel = self.card(content, "02", "Machine fuel", "fuel")
        controls = ttk.Frame(fuel, style="Card.TFrame")
        controls.pack(fill="x", pady=(9, 8))
        self.button(controls, "Enable unlimited fuel", lambda: self.run_action(
            "Enabling unlimited machine fuel…", lambda: fuel_live.set_enabled(True)), True).pack(side="left")
        self.button(controls, "Restore normal fuel", lambda: self.run_action(
            "Restoring normal fuel use…", fuel_live.restore_fuel, True)).pack(side="left", padx=8)
        self.note(fuel, "Refills and holds the operated machine at 100% while simulation runs. Switch machines "
                  "to refill others. Refilled fuel remains after Restore. Generator and fuel trailers are excluded.")

        engine = self.card(content, "03", "Engine power · hydraulics", "engine")
        controls = ttk.Frame(engine, style="Card.TFrame")
        controls.pack(fill="x", pady=(9, 8))
        self.entry(controls, self.engine, 9).pack(side="left")
        ttk.Label(controls, text="kW", style="Card.TLabel").pack(side="left", padx=(6, 14))
        self.button(controls, "Apply power", self.set_engine, True).pack(side="left")
        self.button(controls, "Restore normal power", lambda: self.run_action(
            "Restoring normal engine power…", engine_live.restore_engine, True)).pack(side="left", padx=8)
        self.note(engine, "Changes the live hydraulic engine power limit. Mechanical limits may still apply.")

        pump = self.card(content, "04", "Water-pump capacity", "pump")
        controls = ttk.Frame(pump, style="Card.TFrame")
        controls.pack(fill="x", pady=(9, 8))
        choices = ttk.Combobox(controls, textvariable=self.pump, values=[100, 200, 300, 500, 1000],
                               state="readonly", width=8)
        self.widgets.append((choices, "readonly"))
        choices.pack(side="left")
        ttk.Label(controls, text="L/s", style="Card.TLabel").pack(side="left", padx=(6, 14))
        self.button(controls, "Apply capacity", self.set_pump, True).pack(side="left")
        self.button(controls, "Restore 100 L/s", lambda: self.run_action(
            "Restoring normal water pumps…", pump_live.restore_capacity, True)).pack(side="left", padx=8)
        self.note(pump, "Nominal capacity for all water pumps. Intake, hoses, head and downstream demand still limit flow.")

        bottom = ttk.Frame(self.root, padding=(24, 10, 24, 16))
        bottom.pack(fill="x")
        ttk.Label(bottom, text="Experimental · Build 26171350 · Keep the game running", foreground=MUTED).pack(anchor="w")
        status_label = ttk.Label(bottom, textvariable=self.status, wraplength=820)
        status_label.pack(anchor="w", pady=(6, 8))
        bottom.bind("<Configure>", lambda e: status_label.configure(wraplength=max(300, e.width - 48)))
        self.button(bottom, "Restore all controls", self.restore_all).pack(side="left")
        self.button(bottom, "How it works", self.help).pack(side="right")

    def card(self, parent, number, title, key):
        card = ttk.Frame(parent, style="Card.TFrame", padding=16)
        card.pack(fill="x", pady=(0, 10))
        ttk.Label(card, text=number + "   " + title, style="Card.TLabel",
                  font=("Segoe UI Semibold", 13)).pack(anchor="w")
        state_label = ttk.Label(card, textvariable=self.states[key], style="State.TLabel", wraplength=760)
        state_label.pack(anchor="w", pady=(4, 0))
        card.bind("<Configure>", lambda e: state_label.configure(wraplength=max(300, e.width - 32)), add="+")
        return card

    def note(self, parent, text):
        label = ttk.Label(parent, text=text, style="Note.TLabel", wraplength=750)
        label.pack(anchor="w")
        parent.bind("<Configure>", lambda e: label.configure(wraplength=max(300, e.width - 32)), add="+")

    def _busy(self, busy):
        self.busy = busy
        for widget, state in self.widgets:
            widget.configure(state="disabled" if busy else state)

    def start(self, label, fn, callback):
        if self.busy:
            return
        self._busy(True)
        self.status.set(label)
        def worker():
            try:
                self.events.put((True, fn(), callback))
            except Exception as exc:
                self.events.put((False, str(exc), callback))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            ok, result, callback = self.events.get_nowait()
        except queue.Empty:
            pass
        else:
            self._busy(False)
            if ok:
                callback(result)
            else:
                self.status.set(result)
                messagebox.showerror("Trainer action could not finish", result, parent=self.root)
        self.root.after(80, self.poll)

    def show_state(self, state):
        detected = any(value.get("running") for value in state.values())
        self.connection.set("Game running · Choose a live control" if detected else "Start Groundwork Simulator, load your save, then refresh")
        for name, value in state.items():
            self.states[name].set(value.get("status", "Status unavailable"))

    def refresh(self):
        def done(state):
            self.show_state(state)
            self.status.set("Live status checked. No values were changed.")
        self.start("Checking the running game…", inspect_all, done)

    def run_action(self, label, fn, allow_recovery=False):
        if pending_recovery() and not allow_recovery:
            messagebox.showerror("Restore required", "Use Restore all controls to finish recovering the interrupted change first.", parent=self.root)
            return
        def work():
            result = fn()
            return result, inspect_all()
        def done(payload):
            result, state = payload
            self.show_state(state)
            self.status.set(result.get("summary", "Live control updated."))
        self.start(label, work, done)

    def set_money(self):
        try:
            amount = int(self.money.get().replace(",", "").strip())
            if not 0 <= amount <= 2_000_000_000:
                raise ValueError()
        except ValueError:
            messagebox.showerror("Money amount", "Enter a whole amount from 0 to 2,000,000,000.", parent=self.root)
            return
        self.run_action("Setting live money override…", lambda: money_live.set_amount(amount))

    def set_engine(self):
        try:
            value = float(self.engine.get().strip())
        except ValueError:
            messagebox.showerror("Engine power", "Enter an engine power value in kW.", parent=self.root)
            return
        self.run_action("Changing live hydraulic power…", lambda: engine_live.set_power_kw(value))

    def set_pump(self):
        capacity = int(self.pump.get())
        self.run_action("Changing water-pump capacity…", lambda: pump_live.set_capacity(capacity))

    def restore_all(self):
        def work():
            actions = [(money_live, money_live.restore_money), (fuel_live, fuel_live.restore_fuel),
                       (engine_live, engine_live.restore_engine), (pump_live, pump_live.restore_capacity)]
            for module, restore in actions:
                if module.has_pending_recovery():
                    restore()
            failures = []
            for _, fn in actions:
                try:
                    fn()
                except Exception as exc:
                    failures.append(str(exc))
            if failures:
                raise RuntimeError("Some controls could not be restored:\n" + "\n".join(failures))
            return {"summary": "All trainer controls restored to normal."}
        self.run_action("Restoring normal game behaviour…", work, True)

    def help(self):
        messagebox.showinfo("Live trainer", "Keep Groundwork Simulator running and load your local save.\n\n"
            "All controls act on the running game. No save-file edit or restart is needed to enable them. "
            "Use each Restore button or Restore all controls to switch them off. Closing the trainer leaves "
            "active controls in place; restarting the game resets the overrides. Fuel already replenished "
            "and recorded transactions remain.\n\n"
            "If Windows denies access, launch the trainer using Run as administrator. Only the exact supported "
            "game build is accepted. These controls are experimental; changed gameplay is not yet validated.\n\n"
            "Money override does not erase purchases or expenses from your real account.", parent=self.root)

    def close(self):
        if self.busy:
            messagebox.showinfo("Working", "Wait for the current action to finish before closing.", parent=self.root)
            return
        if pending_recovery():
            messagebox.showerror("Recovery required", "The game is paused after an interrupted change. "
                "Use Restore all controls before closing, or close the game yourself.", parent=self.root)
            return
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--smoke-test", action="store_true")
    parser.add_argument("--diagnostics", type=Path)
    args = parser.parse_args()
    if args.diagnostics:
        args.diagnostics.write_text(json.dumps({"live": inspect_all(), "writes_to_game": False}, indent=2), encoding="utf-8")
        return 0
    root = tk.Tk()
    if args.smoke_test:
        root.withdraw()
    App(root, smoke=args.smoke_test)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
