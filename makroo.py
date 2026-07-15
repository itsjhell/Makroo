"""
Makroo — Autoclicker + Registratore di Macro
============================================
Due modalità:
  1. Autoclicker (solo mouse): intervallo configurabile, tasto SX/DX,
     click singolo/doppio, ripetizioni o loop infinito, posizione
     attuale o coordinate fisse, hotkey start/stop personalizzabili.
  2. Macro: registra tastiera + mouse (movimenti opzionali), riproduci
     N volte o all'infinito, velocità regolabile, salva/carica su file,
     hotkey personalizzabili (anche pulsanti del mouse).

Richiede: Python 3 + pynput  (pip install pynput)
"""

import ctypes
import json
import os
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pynput import keyboard, mouse

APP_NAME = "Makroo"
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "makroo_config.json")

# Flag Windows per riconoscere gli eventi generati via software (da noi):
# così ignoriamo i nostri stessi click/tasti sintetici.
LLKHF_INJECTED = 0x10  # tastiera
LLMHF_INJECTED = 0x01  # mouse


def _kb_filter(msg, data):
    """Filtro low-level tastiera: scarta gli eventi iniettati."""
    return not (data.flags & LLKHF_INJECTED)


def _ms_filter(msg, data):
    """Filtro low-level mouse: scarta gli eventi iniettati."""
    return not (data.flags & LLMHF_INJECTED)


# ---------------------------------------------------------------------------
# Hotkey: rappresentazione serializzabile di un tasto o pulsante mouse
# ---------------------------------------------------------------------------

def key_to_spec(key):
    """Converte un tasto pynput in un dict serializzabile."""
    if isinstance(key, keyboard.Key):
        return {"kind": "key", "name": key.name}
    vk = getattr(key, "vk", None)
    ch = getattr(key, "char", None)
    spec = {"kind": "key"}
    if vk is not None:
        spec["vk"] = vk
    if ch:
        spec["char"] = ch.lower()
    return spec


def button_to_spec(button):
    return {"kind": "mouse", "button": button.name}


def spec_match(a, b):
    if not a or not b or a.get("kind") != b.get("kind"):
        return False
    if a["kind"] == "mouse":
        return a.get("button") == b.get("button")
    if "name" in a or "name" in b:
        return a.get("name") == b.get("name")
    if a.get("vk") is not None and b.get("vk") is not None:
        return a["vk"] == b["vk"]
    return a.get("char") is not None and a.get("char") == b.get("char")


def spec_id(spec):
    return json.dumps(spec, sort_keys=True) if spec else ""


def spec_label(spec):
    """Nome leggibile della hotkey."""
    if not spec:
        return "—"
    if spec["kind"] == "mouse":
        names = {
            "left": "Mouse SX",
            "right": "Mouse DX",
            "middle": "Mouse centrale",
            "x1": "Mouse 4 (X1)",
            "x2": "Mouse 5 (X2)",
        }
        return names.get(spec["button"], "Mouse " + spec["button"])
    if "name" in spec:
        return spec["name"].replace("_", " ").upper()
    if spec.get("char"):
        return spec["char"].upper()
    return "VK %s" % spec.get("vk")


# ---------------------------------------------------------------------------
# Gestore hotkey globali (tastiera + mouse, sempre in ascolto)
# ---------------------------------------------------------------------------

class HotkeyManager:
    def __init__(self):
        self._bindings = {}          # id -> (spec, callback)
        self._down = set()           # anti auto-repeat
        self._lock = threading.Lock()
        self.enabled = True
        self._kb = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            win32_event_filter=_kb_filter,
        )
        self._ms = mouse.Listener(
            on_click=self._on_click,
            win32_event_filter=_ms_filter,
        )
        self._kb.start()
        self._ms.start()

    def bind(self, hid, spec, callback):
        with self._lock:
            self._bindings[hid] = (spec, callback)

    def matches_any(self, spec):
        """True se lo spec corrisponde a una hotkey registrata."""
        with self._lock:
            return any(spec_match(s, spec) for s, _ in self._bindings.values())

    def _fire(self, spec):
        if not self.enabled:
            return
        with self._lock:
            targets = [cb for s, cb in self._bindings.values() if spec_match(s, spec)]
        for cb in targets:
            try:
                cb()
            except Exception:
                pass

    def _on_press(self, key):
        spec = key_to_spec(key)
        sid = spec_id(spec)
        if sid in self._down:
            return  # auto-repeat di Windows: ignora
        self._down.add(sid)
        self._fire(spec)

    def _on_release(self, key):
        self._down.discard(spec_id(key_to_spec(key)))

    def _on_click(self, x, y, button, pressed):
        if pressed:
            self._fire(button_to_spec(button))


# ---------------------------------------------------------------------------
# Autoclicker
# ---------------------------------------------------------------------------

class AutoClicker:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.clicks_done = 0

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, interval_s, button, clicks_per_action, count, position):
        """count=None -> infinito; position=None -> cursore attuale."""
        if self.running:
            return
        self._stop.clear()
        self.clicks_done = 0
        self._thread = threading.Thread(
            target=self._run,
            args=(max(0.001, interval_s), button, clicks_per_action, count, position),
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, interval, button, clicks, count, position):
        ctrl = mouse.Controller()
        done = 0
        while not self._stop.is_set():
            if position is not None:
                ctrl.position = position
            ctrl.click(button, clicks)
            done += 1
            self.clicks_done = done
            if count is not None and done >= count:
                break
            if self._stop.wait(interval):
                break


# ---------------------------------------------------------------------------
# Registratore di macro
# ---------------------------------------------------------------------------

class MacroRecorder:
    MOVE_MIN_DT = 0.01  # max ~100 campioni di movimento al secondo

    def __init__(self, is_hotkey):
        self._is_hotkey = is_hotkey  # callable(spec) -> bool
        self._kb = None
        self._ms = None
        self._t0 = 0.0
        self._last_move = -1.0
        self._lock = threading.Lock()
        self.events = []
        self.recording = False
        self.record_moves = True

    def start(self, record_moves):
        if self.recording:
            return
        self.record_moves = record_moves
        self.events = []
        self._last_move = -1.0
        self._t0 = time.perf_counter()
        self._kb = keyboard.Listener(
            on_press=self._on_kd,
            on_release=self._on_ku,
            win32_event_filter=_kb_filter,
        )
        self._ms = mouse.Listener(
            on_click=self._on_click,
            on_move=self._on_move,
            on_scroll=self._on_scroll,
            win32_event_filter=_ms_filter,
        )
        self._kb.start()
        self._ms.start()
        self.recording = True

    def stop(self):
        if not self.recording:
            return self.events
        self.recording = False
        for lst in (self._kb, self._ms):
            try:
                lst.stop()
            except Exception:
                pass
        self._kb = self._ms = None
        return self.events

    def _t(self):
        return time.perf_counter() - self._t0

    def _add(self, ev):
        with self._lock:
            self.events.append(ev)

    def _key_fields(self, key):
        if isinstance(key, keyboard.Key):
            return {"name": key.name}
        f = {}
        vk = getattr(key, "vk", None)
        ch = getattr(key, "char", None)
        if vk is not None:
            f["vk"] = vk
        if ch:
            f["char"] = ch
        return f

    def _on_kd(self, key):
        if self._is_hotkey(key_to_spec(key)):
            return  # non registrare le hotkey del programma
        self._add({"t": self._t(), "e": "kd", **self._key_fields(key)})

    def _on_ku(self, key):
        if self._is_hotkey(key_to_spec(key)):
            return
        self._add({"t": self._t(), "e": "ku", **self._key_fields(key)})

    def _on_click(self, x, y, button, pressed):
        if self._is_hotkey(button_to_spec(button)):
            return
        self._add({
            "t": self._t(),
            "e": "md" if pressed else "mu",
            "x": x, "y": y, "btn": button.name,
        })

    def _on_move(self, x, y):
        if not self.record_moves:
            return
        now = self._t()
        if now - self._last_move < self.MOVE_MIN_DT:
            return
        self._last_move = now
        self._add({"t": now, "e": "mm", "x": x, "y": y})

    def _on_scroll(self, x, y, dx, dy):
        self._add({"t": self._t(), "e": "ms", "x": x, "y": y, "dx": dx, "dy": dy})


# ---------------------------------------------------------------------------
# Riproduttore di macro
# ---------------------------------------------------------------------------

def _rebuild_key(ev):
    if "name" in ev:
        try:
            return keyboard.Key[ev["name"]]
        except KeyError:
            return None
    if ev.get("vk") is not None:
        return keyboard.KeyCode.from_vk(ev["vk"])
    if ev.get("char"):
        return keyboard.KeyCode.from_char(ev["char"])
    return None


class MacroPlayer:
    def __init__(self):
        self._stop = threading.Event()
        self._thread = None
        self.current_loop = 0

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, events, speed, count):
        """count=None -> loop infinito. speed: moltiplicatore (2 = doppia velocità)."""
        if self.running or not events:
            return
        self._stop.clear()
        self.current_loop = 0
        self._thread = threading.Thread(
            target=self._run, args=(list(events), max(0.01, speed), count), daemon=True
        )
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _run(self, events, speed, count):
        kctrl = keyboard.Controller()
        mctrl = mouse.Controller()
        pressed_keys = set()
        pressed_btns = set()
        loop = 0
        try:
            while not self._stop.is_set() and (count is None or loop < count):
                loop += 1
                self.current_loop = loop
                start = time.perf_counter()
                for ev in events:
                    target = ev["t"] / speed
                    while True:
                        wait = target - (time.perf_counter() - start)
                        if wait <= 0:
                            break
                        if self._stop.wait(min(wait, 0.05)):
                            return
                    if self._stop.is_set():
                        return
                    self._exec(ev, kctrl, mctrl, pressed_keys, pressed_btns)
        finally:
            # rilascia tutto ciò che è rimasto premuto (evita tasti "incastrati")
            for k in pressed_keys:
                try:
                    kctrl.release(k)
                except Exception:
                    pass
            for b in pressed_btns:
                try:
                    mctrl.release(b)
                except Exception:
                    pass

    def _exec(self, ev, kctrl, mctrl, pressed_keys, pressed_btns):
        try:
            e = ev["e"]
            if e == "kd":
                key = _rebuild_key(ev)
                if key is not None:
                    kctrl.press(key)
                    pressed_keys.add(key)
            elif e == "ku":
                key = _rebuild_key(ev)
                if key is not None:
                    kctrl.release(key)
                    pressed_keys.discard(key)
            elif e == "mm":
                mctrl.position = (ev["x"], ev["y"])
            elif e == "md":
                btn = mouse.Button[ev["btn"]]
                mctrl.position = (ev["x"], ev["y"])
                mctrl.press(btn)
                pressed_btns.add(btn)
            elif e == "mu":
                btn = mouse.Button[ev["btn"]]
                mctrl.position = (ev["x"], ev["y"])
                mctrl.release(btn)
                pressed_btns.discard(btn)
            elif e == "ms":
                mctrl.position = (ev["x"], ev["y"])
                mctrl.scroll(ev["dx"], ev["dy"])
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dialog per catturare una hotkey (tasto o pulsante mouse)
# ---------------------------------------------------------------------------

class HotkeyCapture:
    """Apre una finestrella e cattura il primo tasto/pulsante premuto."""

    def __init__(self, app, on_done):
        self.app = app
        self.on_done = on_done
        self.result = None
        self._got = threading.Event()

        app.hotkeys.enabled = False  # non far scattare le hotkey mentre catturiamo

        self.top = tk.Toplevel(app.root)
        self.top.title("Imposta hotkey")
        self.top.attributes("-topmost", True)
        self.top.resizable(False, False)
        self.top.grab_set()
        ttk.Label(
            self.top,
            text="Premi un tasto o un pulsante del mouse...\n(ESC per annullare)",
            padding=20,
            justify="center",
        ).pack()
        self.top.protocol("WM_DELETE_WINDOW", self._cancel)

        self._kb = keyboard.Listener(on_press=self._on_key, win32_event_filter=_kb_filter)
        self._ms = mouse.Listener(on_click=self._on_click, win32_event_filter=_ms_filter)
        # piccolo ritardo per non catturare il click che ha aperto il dialog
        app.root.after(150, self._start_listeners)
        self._poll()

    def _start_listeners(self):
        self._kb.start()
        self._ms.start()

    def _on_key(self, key):
        if key == keyboard.Key.esc:
            self.result = None
        else:
            self.result = key_to_spec(key)
        self._got.set()

    def _on_click(self, x, y, button, pressed):
        if pressed:
            self.result = button_to_spec(button)
            self._got.set()

    def _poll(self):
        if self._got.is_set():
            self._finish()
        else:
            self.top.after(50, self._poll)

    def _cancel(self):
        self.result = None
        self._finish()

    def _finish(self):
        for lst in (self._kb, self._ms):
            try:
                lst.stop()
            except Exception:
                pass
        self.top.grab_release()
        self.top.destroy()
        self.app.hotkeys.enabled = True
        if self.result is not None:
            self.on_done(self.result)


# ---------------------------------------------------------------------------
# Interfaccia grafica
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = {
    "always_on_top": True,
    "autoclicker": {
        "interval_value": 100,
        "interval_unit": "millisecondi",
        "button": "Sinistro",
        "click_type": "Singolo",
        "repeat_mode": "inf",       # "inf" | "n"
        "repeat_count": 100,
        "pos_mode": "current",      # "current" | "fixed"
        "x": 0,
        "y": 0,
        "hk_start": {"kind": "key", "name": "f6"},
        "hk_stop": {"kind": "key", "name": "f6"},
    },
    "macro": {
        "record_moves": True,
        "repeat_mode": "n",
        "repeat_count": 1,
        "speed": "1x",
        "speed_custom": 1.0,
        "hk_rec": {"kind": "key", "name": "f7"},
        "hk_rec_stop": {"kind": "key", "name": "f7"},
        "hk_play": {"kind": "key", "name": "f8"},
        "hk_play_stop": {"kind": "key", "name": "f8"},
    },
}

UNIT_SECONDS = {
    "millisecondi": 0.001,
    "secondi": 1.0,
    "minuti": 60.0,
    "ore": 3600.0,
}

SPEED_CHOICES = ["0.25x", "0.5x", "1x", "2x", "4x", "Personalizzata"]


def _deep_merge(base, extra):
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class MakrooApp:
    def __init__(self):
        self.cfg = self._load_config()

        self.hotkeys = HotkeyManager()
        self.clicker = AutoClicker()
        self.recorder = MacroRecorder(is_hotkey=self.hotkeys.matches_any)
        self.player = MacroPlayer()
        self.macro_events = []
        self.macro_name = None
        self._pick_listener = None
        self._pick_result = None

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.resizable(False, False)
        try:
            self.root.iconbitmap(default="")
        except Exception:
            pass

        self._build_ui()
        self._apply_topmost()
        self._rebind_hotkeys()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll()

    # ----------------------------- config ---------------------------------

    def _load_config(self):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return _deep_merge(DEFAULT_CONFIG, json.load(f))
        except Exception:
            return json.loads(json.dumps(DEFAULT_CONFIG))

    def _save_config(self):
        self._collect_config()
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, indent=2, ensure_ascii=False)
        except Exception:
            pass

    def _collect_config(self):
        a = self.cfg["autoclicker"]
        a["interval_value"] = self._get_float(self.ac_interval, 100)
        a["interval_unit"] = self.ac_unit.get()
        a["button"] = self.ac_button.get()
        a["click_type"] = self.ac_click_type.get()
        a["repeat_mode"] = self.ac_repeat_mode.get()
        a["repeat_count"] = int(self._get_float(self.ac_repeat_count, 100))
        a["pos_mode"] = self.ac_pos_mode.get()
        a["x"] = int(self._get_float(self.ac_x, 0))
        a["y"] = int(self._get_float(self.ac_y, 0))

        m = self.cfg["macro"]
        m["record_moves"] = bool(self.mc_moves.get())
        m["repeat_mode"] = self.mc_repeat_mode.get()
        m["repeat_count"] = int(self._get_float(self.mc_repeat_count, 1))
        m["speed"] = self.mc_speed.get()
        m["speed_custom"] = self._get_float(self.mc_speed_custom, 1.0)

        self.cfg["always_on_top"] = bool(self.topmost_var.get())

    @staticmethod
    def _get_float(var, default):
        try:
            return float(str(var.get()).replace(",", "."))
        except (ValueError, tk.TclError):
            return default

    # ------------------------------- UI -----------------------------------

    def _build_ui(self):
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass

        top = ttk.Frame(self.root, padding=(10, 8, 10, 0))
        top.pack(fill="x")
        ttk.Label(top, text=APP_NAME, font=("Segoe UI", 14, "bold")).pack(side="left")
        self.topmost_var = tk.BooleanVar(value=self.cfg["always_on_top"])
        ttk.Checkbutton(
            top, text="Sempre in primo piano",
            variable=self.topmost_var, command=self._apply_topmost,
        ).pack(side="right")

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=8)

        self._build_autoclicker_tab()
        self._build_macro_tab()

        self.statusbar = ttk.Label(self.root, text="Pronto", anchor="w",
                                   padding=(10, 4), relief="groove")
        self.statusbar.pack(fill="x", side="bottom")

    # --------------------------- tab autoclicker ---------------------------

    def _build_autoclicker_tab(self):
        a = self.cfg["autoclicker"]
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  🖱  Autoclicker  ")

        # Intervallo
        f_int = ttk.LabelFrame(tab, text="Intervallo tra i click", padding=8)
        f_int.pack(fill="x", pady=(0, 6))
        self.ac_interval = tk.StringVar(value=str(a["interval_value"]))
        ttk.Spinbox(f_int, from_=1, to=999999, textvariable=self.ac_interval,
                    width=10).pack(side="left")
        self.ac_unit = tk.StringVar(value=a["interval_unit"])
        ttk.Combobox(f_int, textvariable=self.ac_unit, state="readonly", width=12,
                     values=list(UNIT_SECONDS.keys())).pack(side="left", padx=6)

        # Tipo di click
        f_btn = ttk.LabelFrame(tab, text="Click", padding=8)
        f_btn.pack(fill="x", pady=6)
        ttk.Label(f_btn, text="Tasto:").grid(row=0, column=0, sticky="w")
        self.ac_button = tk.StringVar(value=a["button"])
        ttk.Combobox(f_btn, textvariable=self.ac_button, state="readonly", width=10,
                     values=["Sinistro", "Destro"]).grid(row=0, column=1, padx=6)
        ttk.Label(f_btn, text="Tipo:").grid(row=0, column=2, sticky="w", padx=(12, 0))
        self.ac_click_type = tk.StringVar(value=a["click_type"])
        ttk.Combobox(f_btn, textvariable=self.ac_click_type, state="readonly", width=10,
                     values=["Singolo", "Doppio"]).grid(row=0, column=3, padx=6)

        # Ripetizioni
        f_rep = ttk.LabelFrame(tab, text="Ripetizioni", padding=8)
        f_rep.pack(fill="x", pady=6)
        self.ac_repeat_mode = tk.StringVar(value=a["repeat_mode"])
        ttk.Radiobutton(f_rep, text="Loop infinito", value="inf",
                        variable=self.ac_repeat_mode).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(f_rep, text="Numero di click:", value="n",
                        variable=self.ac_repeat_mode).grid(row=1, column=0, sticky="w")
        self.ac_repeat_count = tk.StringVar(value=str(a["repeat_count"]))
        ttk.Spinbox(f_rep, from_=1, to=99999999, textvariable=self.ac_repeat_count,
                    width=10).grid(row=1, column=1, padx=6)

        # Posizione
        f_pos = ttk.LabelFrame(tab, text="Posizione del click", padding=8)
        f_pos.pack(fill="x", pady=6)
        self.ac_pos_mode = tk.StringVar(value=a["pos_mode"])
        ttk.Radiobutton(f_pos, text="Posizione attuale del mouse", value="current",
                        variable=self.ac_pos_mode,
                        command=self._update_pos_state).grid(row=0, column=0,
                                                             columnspan=4, sticky="w")
        ttk.Radiobutton(f_pos, text="Coordinate fisse:", value="fixed",
                        variable=self.ac_pos_mode,
                        command=self._update_pos_state).grid(row=1, column=0, sticky="w")
        ttk.Label(f_pos, text="X").grid(row=1, column=1, sticky="e")
        self.ac_x = tk.StringVar(value=str(a["x"]))
        self.ac_x_entry = ttk.Entry(f_pos, textvariable=self.ac_x, width=7)
        self.ac_x_entry.grid(row=1, column=2, padx=3)
        ttk.Label(f_pos, text="Y").grid(row=1, column=3, sticky="e")
        self.ac_y = tk.StringVar(value=str(a["y"]))
        self.ac_y_entry = ttk.Entry(f_pos, textvariable=self.ac_y, width=7)
        self.ac_y_entry.grid(row=1, column=4, padx=3)
        self.ac_pick_btn = ttk.Button(f_pos, text="Scegli punto…",
                                      command=self._pick_position)
        self.ac_pick_btn.grid(row=1, column=5, padx=(8, 0))
        self.mouse_pos_label = ttk.Label(f_pos, text="Cursore: 0, 0",
                                         foreground="gray")
        self.mouse_pos_label.grid(row=2, column=0, columnspan=6, sticky="w",
                                  pady=(4, 0))

        # Hotkey
        f_hk = ttk.LabelFrame(tab, text="Hotkey (funzionano anche con Makroo in secondo piano)",
                              padding=8)
        f_hk.pack(fill="x", pady=6)
        self.ac_hk_start_btn = self._hotkey_row(
            f_hk, 0, "Avvia:", ("autoclicker", "hk_start"))
        self.ac_hk_stop_btn = self._hotkey_row(
            f_hk, 1, "Ferma:", ("autoclicker", "hk_stop"))
        ttk.Label(f_hk, text="Stessa hotkey per avvia e ferma = start/stop alternati.",
                  foreground="gray").grid(row=2, column=0, columnspan=3,
                                          sticky="w", pady=(4, 0))

        # Start / Stop
        f_go = ttk.Frame(tab)
        f_go.pack(fill="x", pady=(10, 0))
        self.ac_start_btn = ttk.Button(f_go, text="▶  AVVIA",
                                       command=self.ac_start)
        self.ac_start_btn.pack(side="left", expand=True, fill="x", padx=(0, 4),
                               ipady=6)
        self.ac_stop_btn = ttk.Button(f_go, text="■  FERMA",
                                      command=self.ac_stop, state="disabled")
        self.ac_stop_btn.pack(side="left", expand=True, fill="x", padx=(4, 0),
                              ipady=6)

        self.ac_status = ttk.Label(tab, text="Pronto", anchor="center",
                                   font=("Segoe UI", 10, "bold"))
        self.ac_status.pack(fill="x", pady=(8, 0))

        self._update_pos_state()

    # ----------------------------- tab macro -------------------------------

    def _build_macro_tab(self):
        m = self.cfg["macro"]
        tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(tab, text="  ⏺  Macro  ")

        # Registrazione
        f_rec = ttk.LabelFrame(tab, text="Registrazione", padding=8)
        f_rec.pack(fill="x", pady=(0, 6))
        self.mc_moves = tk.BooleanVar(value=m["record_moves"])
        ttk.Checkbutton(f_rec, text="Registra anche i movimenti del mouse",
                        variable=self.mc_moves).pack(anchor="w")
        row = ttk.Frame(f_rec)
        row.pack(fill="x", pady=(6, 0))
        self.mc_rec_btn = ttk.Button(row, text="⏺  Registra",
                                     command=self.mc_record_toggle)
        self.mc_rec_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))

        # Riproduzione
        f_play = ttk.LabelFrame(tab, text="Riproduzione", padding=8)
        f_play.pack(fill="x", pady=6)

        rrow = ttk.Frame(f_play)
        rrow.pack(fill="x")
        self.mc_repeat_mode = tk.StringVar(value=m["repeat_mode"])
        ttk.Radiobutton(rrow, text="Loop infinito", value="inf",
                        variable=self.mc_repeat_mode).pack(side="left")
        ttk.Radiobutton(rrow, text="Ripetizioni:", value="n",
                        variable=self.mc_repeat_mode).pack(side="left", padx=(12, 0))
        self.mc_repeat_count = tk.StringVar(value=str(m["repeat_count"]))
        ttk.Spinbox(rrow, from_=1, to=99999999, textvariable=self.mc_repeat_count,
                    width=8).pack(side="left", padx=6)

        srow = ttk.Frame(f_play)
        srow.pack(fill="x", pady=(6, 0))
        ttk.Label(srow, text="Velocità:").pack(side="left")
        self.mc_speed = tk.StringVar(value=m["speed"])
        cb = ttk.Combobox(srow, textvariable=self.mc_speed, state="readonly",
                          width=14, values=SPEED_CHOICES)
        cb.pack(side="left", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self._update_speed_state())
        self.mc_speed_custom = tk.StringVar(value=str(m["speed_custom"]))
        self.mc_speed_entry = ttk.Entry(srow, textvariable=self.mc_speed_custom,
                                        width=7)
        self.mc_speed_entry.pack(side="left")
        ttk.Label(srow, text="x").pack(side="left")

        prow = ttk.Frame(f_play)
        prow.pack(fill="x", pady=(8, 0))
        self.mc_play_btn = ttk.Button(prow, text="▶  Riproduci",
                                      command=self.mc_play_toggle)
        self.mc_play_btn.pack(side="left", expand=True, fill="x")

        # File
        f_file = ttk.LabelFrame(tab, text="File macro", padding=8)
        f_file.pack(fill="x", pady=6)
        ttk.Button(f_file, text="💾  Salva macro…",
                   command=self.mc_save).pack(side="left", expand=True,
                                              fill="x", padx=(0, 4))
        ttk.Button(f_file, text="📂  Carica macro…",
                   command=self.mc_load).pack(side="left", expand=True,
                                              fill="x", padx=(4, 0))

        # Hotkey
        f_hk = ttk.LabelFrame(tab, text="Hotkey", padding=8)
        f_hk.pack(fill="x", pady=6)
        self._hotkey_row(f_hk, 0, "Avvia registrazione:", ("macro", "hk_rec"))
        self._hotkey_row(f_hk, 1, "Ferma registrazione:", ("macro", "hk_rec_stop"))
        self._hotkey_row(f_hk, 2, "Avvia riproduzione:", ("macro", "hk_play"))
        self._hotkey_row(f_hk, 3, "Ferma riproduzione:", ("macro", "hk_play_stop"))
        ttk.Label(f_hk, text="Stessa hotkey per avvia e ferma = start/stop alternati.",
                  foreground="gray").grid(row=4, column=0, columnspan=3,
                                          sticky="w", pady=(4, 0))

        self.mc_status = ttk.Label(tab, text="Nessuna macro in memoria",
                                   anchor="center", font=("Segoe UI", 10, "bold"))
        self.mc_status.pack(fill="x", pady=(8, 0))

    # ------------------------- righe hotkey generiche -----------------------

    def _hotkey_row(self, parent, row, label, cfg_path):
        section, key = cfg_path
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w",
                                           pady=2)
        btn = ttk.Button(parent, text=spec_label(self.cfg[section][key]),
                         width=18)
        btn.grid(row=row, column=1, padx=6, pady=2, sticky="w")

        def change():
            def done(spec):
                self.cfg[section][key] = spec
                btn.config(text=spec_label(spec))
                self._rebind_hotkeys()
            HotkeyCapture(self, done)

        btn.config(command=change)
        parent.grid_columnconfigure(1, weight=0)
        return btn

    def _rebind_hotkeys(self):
        a, m = self.cfg["autoclicker"], self.cfg["macro"]
        ac_toggle = spec_match(a["hk_start"], a["hk_stop"])
        rec_toggle = spec_match(m["hk_rec"], m["hk_rec_stop"])
        play_toggle = spec_match(m["hk_play"], m["hk_play_stop"])

        self.hotkeys.bind("ac_start", a["hk_start"],
                          self._hk_ac_toggle if ac_toggle else self.ac_start)
        self.hotkeys.bind("ac_stop", a["hk_stop"],
                          self._hk_ac_toggle if ac_toggle else self.ac_stop)
        self.hotkeys.bind("mc_rec", m["hk_rec"],
                          self._hk_rec_toggle if rec_toggle else self._hk_rec_start)
        self.hotkeys.bind("mc_rec_stop", m["hk_rec_stop"],
                          self._hk_rec_toggle if rec_toggle else self._hk_rec_stop)
        self.hotkeys.bind("mc_play", m["hk_play"],
                          self._hk_play_toggle if play_toggle else self._hk_play_start)
        self.hotkeys.bind("mc_play_stop", m["hk_play_stop"],
                          self._hk_play_toggle if play_toggle else self._hk_play_stop)

    # callback hotkey (girano nel thread del listener: nessun accesso diretto a tk)
    def _hk_ac_toggle(self):
        if self.clicker.running:
            self.clicker.stop()
        else:
            self.ac_start()

    def _hk_rec_toggle(self):
        if self.recorder.recording:
            self._hk_rec_stop()
        else:
            self._hk_rec_start()

    def _hk_rec_start(self):
        if self.recorder.recording or self.player.running:
            return
        self.recorder.start(bool(self.mc_moves.get()))

    def _hk_rec_stop(self):
        if not self.recorder.recording:
            return
        self.macro_events = self.recorder.stop()
        self.macro_name = None

    def _hk_play_toggle(self):
        if self.player.running:
            self.player.stop()
        else:
            self._hk_play_start()

    def _hk_play_start(self):
        if self.player.running or self.recorder.recording or not self.macro_events:
            return
        count = None
        if self.mc_repeat_mode.get() == "n":
            count = max(1, int(self._get_float(self.mc_repeat_count, 1)))
        self.player.start(self.macro_events, self._speed_factor(), count)

    def _hk_play_stop(self):
        self.player.stop()

    # --------------------------- azioni autoclicker ------------------------

    def ac_start(self):
        if self.clicker.running:
            return
        interval = self._get_float(self.ac_interval, 100) * \
            UNIT_SECONDS.get(self.ac_unit.get(), 0.001)
        button = mouse.Button.left if self.ac_button.get() == "Sinistro" \
            else mouse.Button.right
        clicks = 2 if self.ac_click_type.get() == "Doppio" else 1
        count = None
        if self.ac_repeat_mode.get() == "n":
            count = max(1, int(self._get_float(self.ac_repeat_count, 100)))
        position = None
        if self.ac_pos_mode.get() == "fixed":
            position = (int(self._get_float(self.ac_x, 0)),
                        int(self._get_float(self.ac_y, 0)))
        self.clicker.start(interval, button, clicks, count, position)

    def ac_stop(self):
        self.clicker.stop()

    def _update_pos_state(self):
        state = "normal" if self.ac_pos_mode.get() == "fixed" else "disabled"
        for w in (self.ac_x_entry, self.ac_y_entry, self.ac_pick_btn):
            w.config(state=state)

    def _pick_position(self):
        """Il prossimo click fisico imposta le coordinate."""
        if self._pick_listener is not None:
            return
        self.statusbar.config(text="Clicca in un punto qualsiasi dello schermo "
                                   "per fissare le coordinate…")
        self.hotkeys.enabled = False
        self._pick_result = None

        def on_click(x, y, button, pressed):
            if pressed:
                self._pick_result = (int(x), int(y))
                return False  # ferma il listener

        self._pick_listener = mouse.Listener(on_click=on_click,
                                             win32_event_filter=_ms_filter)
        self._pick_listener.start()

    # ------------------------------ azioni macro ---------------------------

    def mc_record_toggle(self):
        if self.recorder.recording:
            self._hk_rec_stop()
        else:
            self._hk_rec_start()

    def mc_play_toggle(self):
        if self.player.running:
            self.player.stop()
        else:
            self._hk_play_start()
            if not self.macro_events:
                messagebox.showinfo(APP_NAME,
                                    "Nessuna macro in memoria.\n"
                                    "Registra una macro o caricane una da file.")

    def _speed_factor(self):
        choice = self.mc_speed.get()
        if choice == "Personalizzata":
            return max(0.01, self._get_float(self.mc_speed_custom, 1.0))
        try:
            return float(choice.rstrip("x"))
        except ValueError:
            return 1.0

    def _update_speed_state(self):
        state = "normal" if self.mc_speed.get() == "Personalizzata" else "disabled"
        self.mc_speed_entry.config(state=state)

    def mc_save(self):
        if not self.macro_events:
            messagebox.showinfo(APP_NAME, "Nessuna macro da salvare.")
            return
        path = filedialog.asksaveasfilename(
            title="Salva macro",
            defaultextension=".json",
            filetypes=[("Macro Makroo", "*.json")],
            initialdir=APP_DIR,
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"app": APP_NAME, "version": 1,
                           "events": self.macro_events}, f)
            self.macro_name = os.path.basename(path)
            self.statusbar.config(text=f"Macro salvata: {self.macro_name}")
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Errore nel salvataggio:\n{exc}")

    def mc_load(self):
        path = filedialog.askopenfilename(
            title="Carica macro",
            filetypes=[("Macro Makroo", "*.json"), ("Tutti i file", "*.*")],
            initialdir=APP_DIR,
        )
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            events = data.get("events")
            if not isinstance(events, list) or not events:
                raise ValueError("file macro non valido o vuoto")
            self.macro_events = events
            self.macro_name = os.path.basename(path)
            self.statusbar.config(text=f"Macro caricata: {self.macro_name} "
                                       f"({len(events)} eventi)")
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            messagebox.showerror(APP_NAME, f"Impossibile caricare la macro:\n{exc}")

    # ------------------------------ poll UI --------------------------------

    def _poll(self):
        # posizione mouse live
        try:
            x, y = mouse.Controller().position
            self.mouse_pos_label.config(text=f"Cursore: {int(x)}, {int(y)}")
        except Exception:
            pass

        # risultato "scegli punto"
        if self._pick_listener is not None and not self._pick_listener.running:
            self._pick_listener = None
            self.hotkeys.enabled = True
            if self._pick_result:
                self.ac_x.set(str(self._pick_result[0]))
                self.ac_y.set(str(self._pick_result[1]))
                self.statusbar.config(
                    text=f"Coordinate impostate: {self._pick_result[0]}, "
                         f"{self._pick_result[1]}")

        # stato autoclicker
        if self.clicker.running:
            self.ac_status.config(
                text=f"● IN ESECUZIONE — click effettuati: "
                     f"{self.clicker.clicks_done}", foreground="#c00000")
            self.ac_start_btn.config(state="disabled")
            self.ac_stop_btn.config(state="normal")
        else:
            done = self.clicker.clicks_done
            self.ac_status.config(
                text="Pronto" if not done else f"Fermo — ultimi click: {done}",
                foreground="#006000" if not done else "black")
            self.ac_start_btn.config(state="normal")
            self.ac_stop_btn.config(state="disabled")

        # stato macro
        if self.recorder.recording:
            self.mc_status.config(
                text=f"⏺ REGISTRAZIONE… ({len(self.recorder.events)} eventi)",
                foreground="#c00000")
            self.mc_rec_btn.config(text="⏹  Ferma registrazione")
            self.mc_play_btn.config(state="disabled")
        elif self.player.running:
            self.mc_status.config(
                text=f"▶ RIPRODUZIONE… (ciclo {self.player.current_loop})",
                foreground="#0050c0")
            self.mc_rec_btn.config(text="⏺  Registra", state="disabled")
            self.mc_play_btn.config(text="⏹  Ferma riproduzione", state="normal")
        else:
            n = len(self.macro_events)
            name = f" — {self.macro_name}" if self.macro_name else ""
            self.mc_status.config(
                text=f"Macro in memoria: {n} eventi{name}" if n
                else "Nessuna macro in memoria",
                foreground="black")
            self.mc_rec_btn.config(text="⏺  Registra", state="normal")
            self.mc_play_btn.config(text="▶  Riproduci", state="normal")

        self._update_speed_state()
        self.root.after(100, self._poll)

    # ------------------------------ varie ----------------------------------

    def _apply_topmost(self):
        self.root.attributes("-topmost", bool(self.topmost_var.get()))

    def _on_close(self):
        self.clicker.stop()
        self.player.stop()
        if self.recorder.recording:
            self.recorder.stop()
        self._save_config()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def main():
    # DPI awareness: coordinate coerenti su schermi con ridimensionamento
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    MakrooApp().run()


if __name__ == "__main__":
    main()
