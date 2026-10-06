"""The Modbus tab: connection settings, live register table, graph setup (up
to six registers) and their charts.

Polling runs on a ModbusPoller thread that reports through the tab's queue.
Raw register values are kept per poll and decoded when drawing, so changing
a graph's register, format or scale re-plots its whole history at once.
"""

import os
import queue
import time
import tkinter as tk
from tkinter import messagebox, ttk

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from gui_common import PAGE, WINDOWS, TabBase
from modbus_core import (FORMATS, MAX_GRAPHS, QUANTITIES, REG_TYPES, ModbusPoller, decode,
                         load_settings, ref, save_settings)

MAX_POLLS = 100000              # polls kept for plotting (~5.8 days at 5 s)
NONE = "(none)"


def _ago(seconds):
    s = int(seconds)
    return "%ds" % s if s < 120 else "%dm" % (s // 60) if s < 7200 else "%dh" % (s // 3600)


class ModbusTab(TabBase):
    def __init__(self, root, frame, notebook, out, settings_path, connect=False, sim=False):
        super().__init__(root, frame, notebook)
        self.out = out
        self.settings_path = settings_path
        self.s = load_settings(settings_path)
        # --modbus-sim is for this run only; the saved choice stays as it was.
        self.saved_sim = None if not sim else self.s["sim"]
        if sim:
            self.s["sim"] = True
        self.poller = None
        self.sim_server = None
        self.gen = 0                # connection number; stale worker messages are dropped
        self.active = None          # (reg_type, first, count) being polled
        self.source = None          # identifies the device the charts' data came from
        self.polls = []             # (time, first register, raw values or None)
        self.last_values = None
        self.loading = True
        self._build()
        self.loading = False
        self._rebuild_table()
        if connect or sim:
            self._connect()
        root.after(200, self._poll)
        root.after(1000, self._tick)

    # layout -----------------------------------------------------------
    def _build(self):
        f, s = self.frame, self.s
        bar = ttk.Frame(f, padding=(8, 8, 8, 4))
        bar.pack(fill="x")
        self.v = {k: tk.StringVar(value=str(s[k])) for k in
                  ("host", "port", "unit", "reg_type", "start", "count")}
        self.v["interval"] = tk.StringVar(value="%g" % s["interval"])
        self.sim_var = tk.BooleanVar(value=s["sim"])
        self.inputs = []            # widgets locked while connected

        def entry(label, key, width, padx=10):
            ttk.Label(bar, text=label).pack(side="left", padx=(padx, 4))
            e = ttk.Entry(bar, textvariable=self.v[key], width=width)
            e.pack(side="left")
            return e

        self.host_entry = entry("Device IP", "host", 15, 0)
        self.port_entry = entry("Port", "port", 6)
        self.inputs += [self.host_entry, self.port_entry, entry("Unit ID", "unit", 4)]
        ttk.Label(bar, text="Read").pack(side="left", padx=(16, 4))
        type_box = ttk.Combobox(bar, textvariable=self.v["reg_type"], values=list(REG_TYPES),
                                state="readonly", width=16)
        type_box.pack(side="left")
        self.inputs += [type_box, entry("from", "start", 6, 6), entry("count", "count", 5, 6)]
        entry("Poll every", "interval", 4, 16)
        ttk.Label(bar, text="s").pack(side="left", padx=(2, 0))
        sim_box = ttk.Checkbutton(bar, text="Simulated device", variable=self.sim_var,
                                  command=self._sim_toggled)
        sim_box.pack(side="left", padx=(16, 0))
        self.inputs.append(sim_box)
        self.conn_btn = ttk.Button(bar, text="Connect", command=self._toggle)
        self.conn_btn.pack(side="left", padx=(12, 0))
        ttk.Button(bar, text="Open log folder", command=self._open_folder).pack(side="right")
        self.file_var = tk.StringVar(value="Not connected")
        ttk.Label(bar, textvariable=self.file_var, style="Status.TLabel").pack(side="right", padx=8)
        for key in ("reg_type", "start", "count"):
            self.v[key].trace_add("write", lambda *a: self._rebuild_table())
        self.v["interval"].trace_add("write", lambda *a: self._interval_changed())
        self._sim_toggled()

        self.status_var = tk.StringVar(value="")
        ttk.Label(f, textvariable=self.status_var, style="Status.TLabel",
                  padding=(8, 0, 8, 4)).pack(fill="x")

        # Register table under one chart column, graph setup under the other two.
        top = ttk.Frame(f)
        top.pack(fill="x", padx=8)
        top.columnconfigure(0, weight=1, uniform="top")
        top.columnconfigure(1, weight=2, uniform="top")
        table_box = ttk.Frame(top)
        table_box.grid(row=0, column=0, sticky="nsew")
        cols = [("addr", "Register", 62), ("ref", "Ref", 62), ("raw", "Value", 62),
                ("hex", "Hex", 56), ("graph", "Graphed as", 120)]
        self.tree = ttk.Treeview(table_box, columns=[c[0] for c in cols], show="headings", height=6)
        for key, title, width in cols:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40, anchor="w" if key == "graph" else "e",
                             stretch=key == "graph")
        self.tree.tag_configure("graphed", background="#e8f0fb")
        scroll = ttk.Scrollbar(table_box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)

        setup = ttk.Frame(top, padding=(12, 0, 0, 0))
        setup.grid(row=0, column=1, sticky="nsew")
        for col, text in enumerate(("", "Register", "Name", "Shows", "Format", "Scale", "Latest")):
            ttk.Label(setup, text=text, style="Status.TLabel").grid(row=0, column=col, sticky="w", padx=3)
        self.rows = []
        for i, g in enumerate(s["graphs"]):
            r = {k: tk.StringVar() for k in ("register", "name", "quantity", "format", "scale", "latest")}
            r["register"].set(NONE if g["register"] is None else str(g["register"]))
            r["name"].set(g["name"])
            r["quantity"].set(g["quantity"])
            r["format"].set(g["format"])
            r["scale"].set("%g" % g["scale"])
            ttk.Label(setup, text="Graph %d" % (i + 1)).grid(row=i + 1, column=0, sticky="w", padx=(0, 6))
            r["reg_box"] = ttk.Combobox(setup, textvariable=r["register"], state="readonly", width=8)
            r["reg_box"].grid(row=i + 1, column=1, padx=3, pady=1)
            ttk.Entry(setup, textvariable=r["name"], width=18).grid(row=i + 1, column=2, padx=3)
            qty = ttk.Combobox(setup, textvariable=r["quantity"], values=list(QUANTITIES),
                               state="readonly", width=11)
            qty.grid(row=i + 1, column=3, padx=3)
            fmt = ttk.Combobox(setup, textvariable=r["format"], values=FORMATS, state="readonly", width=8)
            fmt.grid(row=i + 1, column=4, padx=3)
            ttk.Entry(setup, textvariable=r["scale"], width=8).grid(row=i + 1, column=5, padx=3)
            ttk.Label(setup, textvariable=r["latest"], width=16).grid(row=i + 1, column=6, sticky="w", padx=3)
            r["reg_box"].bind("<<ComboboxSelected>>", lambda e: self._graphs_changed())
            qty.bind("<<ComboboxSelected>>", lambda e, r=r: self._quantity_chosen(r))
            fmt.bind("<<ComboboxSelected>>", lambda e: self._graphs_changed())
            for key in ("name", "scale"):
                r[key].trace_add("write", lambda *a: self._graphs_changed())
            self.rows.append(r)

        panes = ttk.PanedWindow(f, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=8)
        plot_frame = ttk.Frame(panes)
        ctl = ttk.Frame(plot_frame)
        ctl.pack(fill="x")
        ttk.Label(ctl, text="Show").pack(side="left")
        self.window_var = tk.StringVar(value="6 hours")
        box = ttk.Combobox(ctl, textvariable=self.window_var, state="readonly", width=10,
                           values=[w[0] for w in WINDOWS])
        box.pack(side="left", padx=6)
        box.bind("<<ComboboxSelected>>", lambda e: self._mark_dirty())
        ttk.Button(ctl, text="Clear charts", command=self._clear_charts).pack(side="left", padx=(8, 0))
        self._events_checkbox(ctl).pack(side="left", padx=12)
        self.fixed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(ctl, text="Fix axis", variable=self.fixed_var,
                        command=self._mark_dirty).pack(side="left")
        ttk.Label(ctl, text="Hover a chart for values. Choose what each graph shows above.",
                  style="Status.TLabel").pack(side="right")

        self.fig = Figure(figsize=(12, 5), facecolor=PAGE, layout="constrained")
        self.axes = self.fig.subplots(2, 3)
        self.canvas = FigureCanvasTkAgg(self.fig, master=plot_frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.canvas.mpl_connect("motion_notify_event", self._on_hover)
        panes.add(plot_frame, weight=4)
        self._build_events(panes)

    # settings -----------------------------------------------------------
    def _read_connection(self):
        """Connection settings from the fields; ValueError says what is wrong."""
        def number(key, kind, low, high, label):
            try:
                v = kind(self.v[key].get().strip())
            except ValueError:
                raise ValueError("%s must be a number" % label) from None
            if not low <= v <= high:
                raise ValueError("%s must be between %g and %g" % (label, low, high))
            return v

        reg_type = self.v["reg_type"].get()
        c = {"host": self.v["host"].get().strip(), "reg_type": reg_type, "sim": self.sim_var.get(),
             "port": number("port", int, 1, 65535, "Port"),
             "unit": number("unit", int, 0, 255, "Unit ID"),
             "start": number("start", int, 0, 65535, "Start register"),
             "count": number("count", int, 1, REG_TYPES[reg_type][3], "Count"),
             "interval": number("interval", float, 0.5, 3600, "Poll interval")}
        if not c["host"] and not c["sim"]:
            raise ValueError("enter the device's IP address")
        if c["start"] + c["count"] > 65536:
            raise ValueError("start + count goes past register 65535")
        return c

    def _save(self):
        data = dict(self.s)
        if self.saved_sim is not None and data["sim"]:
            data["sim"] = self.saved_sim
        try:
            save_settings(self.settings_path, data)
        except OSError as e:
            self._add_log("warning", "could not save Modbus settings: %s" % e)

    def _sim_toggled(self):
        state = "disabled" if self.sim_var.get() or self.poller else "normal"
        for w in (self.host_entry, self.port_entry):
            w.configure(state=state)

    def _interval_changed(self):
        try:
            v = float(self.v["interval"].get())
        except ValueError:
            return
        if 0.5 <= v <= 3600:
            self.s["interval"] = v
            if self.poller:
                self.poller.interval = v
                self._save()

    def _quantity_chosen(self, r):
        # Picking what a graph shows also picks that quantity's usual encoding.
        unit, fmt, scale = QUANTITIES[r["quantity"].get()]
        r["format"].set(fmt)
        r["scale"].set("%g" % scale)
        self._graphs_changed()

    def _graphs_changed(self):
        if self.loading:
            return
        for r, g in zip(self.rows, self.s["graphs"]):
            reg = r["register"].get()
            g["register"] = None if reg == NONE else int(reg)
            g["name"] = r["name"].get().strip()
            g["quantity"] = r["quantity"].get()
            g["format"] = r["format"].get()
            try:
                g["scale"] = float(r["scale"].get())      # keep the last good value while typing
            except ValueError:
                pass
        self._save()
        self._refresh_graph_column()
        self._refresh_latest()
        self._mark_dirty()

    # register table -------------------------------------------------------
    def _layout(self):
        """(reg_type, first, count) being polled, else what the fields say."""
        if self.active:
            return self.active
        try:
            reg_type = self.v["reg_type"].get()
            first, count = int(self.v["start"].get()), int(self.v["count"].get())
        except ValueError:
            return None
        if reg_type not in REG_TYPES or not (0 <= first and 1 <= count <= REG_TYPES[reg_type][3]):
            return None
        return reg_type, first, min(count, 65536 - first)

    def _rebuild_table(self):
        if self.loading:
            return
        layout = self._layout()
        self.tree.delete(*self.tree.get_children())
        addrs = []
        if layout:
            reg_type, first, count = layout
            addrs = list(range(first, first + count))
            for a in addrs:
                self.tree.insert("", "end", iid=str(a), values=(a, ref(reg_type, a), "", "", ""))
        for r in self.rows:
            r["reg_box"]["values"] = [NONE] + [str(a) for a in addrs]
        self._refresh_graph_column()
        self._refresh_latest()
        if self.last_values is not None and self.active:
            self._fill_values(self.active[1], self.last_values)

    def _refresh_graph_column(self):
        names = {}
        for i, g in enumerate(self.s["graphs"]):
            if g["register"] is not None:
                words = 1 if g["format"].endswith("16") else 2
                for a in range(g["register"], g["register"] + words):
                    names.setdefault(a, []).append(g["name"] or "Graph %d" % (i + 1))
        for iid in self.tree.get_children():
            graphed = names.get(int(iid))
            self.tree.set(iid, "graph", ", ".join(graphed) if graphed else "")
            self.tree.item(iid, tags=("graphed",) if graphed else ())

    def _fill_values(self, first, values):
        bits = self.active and REG_TYPES[self.active[0]][0] in (1, 2)
        for i, v in enumerate(values):
            iid = str(first + i)
            if self.tree.exists(iid):
                self.tree.set(iid, "raw", "-" if v is None else v)
                self.tree.set(iid, "hex", "" if v is None or bits else "%04X" % v)

    def _value(self, g, first, values):
        if g["register"] is None or values is None:
            return None
        v = decode(values, g["register"] - first, g["format"])
        return None if v is None else v * g["scale"]

    def _refresh_latest(self):
        for r, g in zip(self.rows, self.s["graphs"]):
            if g["register"] is None:
                r["latest"].set("")
                continue
            layout = self._layout()
            words = 1 if g["format"].endswith("16") else 2
            if layout and not layout[1] <= g["register"] <= layout[1] + layout[2] - words:
                r["latest"].set("not polled")
                continue
            last = next((p for p in reversed(self.polls) if p[2] is not None), None)
            v = self._value(g, last[1], last[2]) if last else None
            unit = QUANTITIES[g["quantity"]][0]
            r["latest"].set("-" if v is None else "%.2f %s" % (v, unit))

    # connection -------------------------------------------------------------
    def _toggle(self):
        if self.poller:
            self._disconnect()
        else:
            self._connect()

    def _connect(self):
        try:
            c = self._read_connection()
        except ValueError as e:
            messagebox.showerror("Modbus", str(e), parent=self.root)
            return
        self.s.update(c)
        self._save()
        host, port = c["host"], c["port"]
        if c["sim"]:
            if self.sim_server is None:
                from modbus_sim import SimServer
                self.sim_server = SimServer().start()
            host, port = "127.0.0.1", self.sim_server.port
        source = ("sim" if c["sim"] else "%s:%d" % (host, port), c["unit"], c["reg_type"])
        if self.polls and source != self.source:
            self.polls.clear()
            self._add_log("info", "charts cleared: different device or register type")
        self.source = source
        folder = os.path.join(self.out, "sim") if c["sim"] else self.out
        self.gen += 1
        emit = lambda kind, payload, gen=self.gen: self.q.put((gen, kind, payload))
        self.active = (c["reg_type"], c["start"], c["count"])
        self.last_values = None
        self.poller = ModbusPoller(host, port, c["unit"], c["reg_type"], c["start"], c["count"],
                                   c["interval"], emit, folder)
        self.poller.start()
        for w in self.inputs:
            w.configure(state="disabled")
        self.conn_btn.configure(text="Disconnect")
        self.file_var.set("Logging to " + os.path.abspath(folder))
        self._rebuild_table()
        self._mark_dirty()

    def _disconnect(self):
        # Don't wait: the poller may be in a connect timeout. Its late
        # messages carry an old generation number and are ignored.
        self.poller.stop_event.set()
        self.poller = None
        self.active = None
        self.gen += 1
        self._add_log("info", "disconnected")
        for w in self.inputs:
            w.configure(state="readonly" if isinstance(w, ttk.Combobox) else "normal")
        self._sim_toggled()
        self.conn_btn.configure(text="Connect")
        self.file_var.set("Not connected")

    def _open_folder(self):
        folder = os.path.join(self.out, "sim") if self.sim_var.get() else self.out
        os.makedirs(folder, exist_ok=True)
        os.startfile(folder)

    def shutdown(self):
        if self.poller:
            self.poller.stop_event.set()
        if self.sim_server:
            self.sim_server.stop()

    # data flow ----------------------------------------------------------------
    def _poll(self):
        try:
            for _ in range(1000):
                gen, kind, payload = self.q.get_nowait()
                if gen != self.gen:
                    continue
                if kind == "poll":
                    now, values = payload
                    self.polls.append((now, self.active[1], values))
                    del self.polls[:-MAX_POLLS]
                    if values is not None:
                        self.last_values = values
                        self._fill_values(self.active[1], values)
                    self._refresh_latest()
                    self.plot_dirty = True
                elif kind == "log":
                    self._add_log(*payload)
        except queue.Empty:
            pass
        self.root.after(200, self._poll)

    def _tick(self):
        self._refresh_status(time.time())
        if self.plot_dirty and self.visible():
            self.plot_dirty = False
            self._draw()
        self.root.after(1000, self._tick)

    def _refresh_status(self, now):
        p = self.poller
        if not p:
            self.status_var.set("Enter the device's IP address and the registers to read, then press "
                                "Connect. Tick Simulated device to try it without hardware.")
            return
        if p.csv.path:
            self.file_var.set("Logging to " + p.csv.path)
        where = "simulated device" if self.s["sim"] else "%s:%d" % (p.host, p.port)
        if p.last_ok is None:
            link = "connecting" if not p.failed else "NO RESPONSE"
        elif now - p.last_ok > max(3 * p.interval, 15):
            link = "NO RESPONSE for %s" % _ago(now - p.last_ok)
        else:
            link = "responding"
        self.status_var.set("   ".join(x for x in (
            "%s unit %d: %s" % (where, p.unit, link),
            "polls %d" % p.polls, "failed %d" % p.failed,
            "last reply %s ago" % _ago(now - p.last_ok) if p.last_ok else "",
            "reading registers one at a time" if p.single else "") if x))

    def _clear_charts(self):
        self.polls.clear()
        self._add_log("info", "charts cleared (log files are unchanged)")
        self._refresh_latest()
        self._mark_dirty()

    def _view_end(self, now):
        if self.poller or not self.polls:
            return now
        return self.polls[-1][0]

    # charts -------------------------------------------------------------------
    def _draw(self):
        end = self._view_end(time.time())
        span = dict(WINDOWS)[self.window_var.get()]
        t0 = end - span if span else None
        polls = [p for p in self.polls if t0 is None or p[0] >= t0]
        xlim = self._xlim(self.polls[0][0], t0, end) if self.polls else None
        gap = max(3 * self.s["interval"], 30)
        self.hover.clear()
        for i, ax in enumerate(self.axes.flat):
            g = self.s["graphs"][i] if i < MAX_GRAPHS else {"register": None}
            ax.clear()
            if g["register"] is None:
                self._style(ax, "Graph %d" % (i + 1))
                self._waiting(ax, "no register chosen")
                continue
            unit = QUANTITIES[g["quantity"]][0]
            self._style(ax, "%s  -  %s   (register %d)" % (
                g["name"] or "Graph %d" % (i + 1), unit, g["register"]))
            pts = [(t, self._value(g, first, values)) for t, first, values in polls]
            valid = [(t, v) for t, v in pts if v is not None]
            if not valid:
                self._waiting(ax, "waiting for readings" if self.poller or not self.polls
                              else "no readings for this register")
                continue
            self._line(ax, pts, gap)
            self._latest(ax, valid, 9)
            if self.fixed_var.get():
                self._zero_based(ax, [v for t, v in valid])
            ax.set_xlim(*xlim)
            self._set_hover(ax, unit, [(t, v, None) for t, v in pts], "no reply / unreadable")
        self.tip = None
        self.canvas.draw_idle()
