#!/usr/bin/env python3
"""
Loot Farmer - Clash of Clans farming bot for BlueStacks (ADB).

How it works
------------
Every loop it takes ONE screenshot, works out which screen the game is on
(home, attack menu, army, scouting, battle, surrender dialog, battle end)
from your captured button templates, and does the right thing for that
screen. Because it always looks before it taps, it can pick up from any
screen - after a popup, a lag spike, a game restart or an emulator restart.

Unrecognised screen for a while -> press Back -> relaunch the game ->
(if enabled) restart the emulator. It never gives up, it just backs off.

Run:           python bot.py
Self-check:    python bot.py --selftest
Old version:   bot_old.py (untouched backup)
"""
import importlib.util
import subprocess
import sys


def _ensure(pkgs):
    missing = [pip for mod, pip in pkgs.items() if importlib.util.find_spec(mod) is None]
    if missing:
        print("Installing:", ", ".join(missing))
        subprocess.check_call([sys.executable, "-m", "pip", "install", *missing])


_ensure({"cv2": "opencv-python", "PIL": "Pillow", "numpy": "numpy", "sv_ttk": "sv-ttk",
         "pytesseract": "pytesseract"})

import collections
import difflib
import http.server
import json
import logging
import logging.handlers
import os
import queue
import re
import secrets
import shutil
import socket
import struct
import tempfile
import threading
import time
import traceback
import tkinter as tk
from tkinter import messagebox, ttk

import cv2
import numpy as np
import sv_ttk
from PIL import Image, ImageTk

try:
    import pytesseract
    HAVE_TESS = True
except ImportError:
    HAVE_TESS = False

# ---------------------------------------------------------------------------
# Paths, constants, config
# ---------------------------------------------------------------------------
APP_VERSION = 16  # bumped by `python bot.py --publish`; friends get an Update button when GitHub has a higher one
UPDATE_REPO = "Geo-Col/LootFarmer"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "bot.log")
# Shipped inside the bot folder so nothing needs installing (used when the configured path doesn't exist)
BUNDLED = {"adb_path": os.path.join(BASE_DIR, "platform-tools", "adb.exe"),
           "tesseract_path": os.path.join(BASE_DIR, "Tesseract-OCR", "tesseract.exe")}
os.makedirs(TEMPLATE_DIR, exist_ok=True)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # no console flash per adb call

BUTTONS = [
    ("attack_button", "Attack! (home screen)"),
    ("find_match_button", "Find a Match"),
    ("confirm_attack_button", "Attack! (green, army screen)"),
    ("next_button", "Next (skip base)"),
    ("surrender_button", "Surrender / End Battle"),
    ("surrender_confirm_button", "Okay (confirm surrender)"),
    ("return_home_button", "Return Home"),
    ("upgrade_more_button", "Upgrade More (wall selected)  - walls"),
    ("upgrade_more_disabled", "Upgrade More greyed out (only wall of its level)  - walls"),
    ("wall_upgrade_hammers", "Upgrade More bar: double-hammer icon  - walls"),
    ("wall_gold_upgrade_button", "Upgrade More bar: gold Upgrade  - walls"),
    ("wall_elixir_upgrade_button", "Upgrade More bar: elixir Upgrade  - walls"),
    ("wall_okay_button", "Upgrade Walls dialog: Okay  - walls"),
    ("confirm_wall_upgrade_button", "Upgrade window: green Confirm  - upgrades"),
    ("building_upgrade_button", "Selected building's Upgrade button  - upgrades"),
    ("settings_cog_button", "Settings cog (home)  - accounts"),
    ("switch_account_button", "Blue switch-account button  - accounts"),
    ("supercell_id_header", "Supercell ID panel logo  - accounts"),
    ("lab_picker_title", "Lab 'Choose what to upgrade' title  - upgrades"),
    ("guardians_title", "'Guardians' window title  - upgrades"),
    ("reload_game", "'Anyone there?' disconnect: RELOAD GAME  - recovery"),
    ("bb_boat", "Boat's sail bubble (both villages)  - builder base"),
    ("bb_attack_button", "Builder Base Attack! (axes)  - builder base"),
    ("bb_find_now", "Builder Base Find Now!  - builder base"),
    ("bb_return_home", "Builder Base Return Home  - builder base"),
    ("bb_elixir_bubble", "Builder Base elixir bubble  - builder base"),
    ("bb_gold_bubble", "Builder Base gold bubble  - builder base"),
    ("bb_cart_title", "Elixir Cart window title  - builder base"),
    ("bb_cart_collect", "Elixir Cart green Collect  - builder base"),
    ("bb_bonus_title", "'Star Bonus!' popup title  - builder base"),
    ("bb_bonus_okay", "Star Bonus popup Okay  - builder base"),
    ("bb_gem_bubble", "Gem Mine gem bubble  - builder base"),
    ("bb_clock_bubble", "Clock Tower boost-ready bubble  - builder base"),
    ("bb_free_boost", "Clock Tower 'Free Boost!' button  - builder base"),
    ("bb_boost_title", "'Free Boost!' window title  - builder base"),
    ("bb_boost_button", "'Free Boost!' window green Boost  - builder base"),
]
REQUIRED_BUTTONS = [n for n, _ in BUTTONS[:7]]
OCR_REGIONS = [
    ("loot_gold_region", "Available loot - gold"),
    ("loot_elixir_region", "Available loot - elixir"),
    ("damage_percent_region", "Overall damage %"),
    ("bank_gold_region", "Your storage - gold"),
    ("bank_elixir_region", "Your storage - elixir"),
    ("bank_dark_region", "Your storage - dark elixir"),
]
POINTS = [
    ("deploy_point", "Troop drop point (line start)"),
    ("deploy_line_end", "Troop line end (same side of the base)"),
    ("spell_point", "Spell line start (optional)"),
    ("spell_line_end", "Spell line end (optional)"),
    ("list_scroll_start", "Builder list swipe start"),
    ("list_scroll_end", "Builder list swipe end"),
]
# Checked in this order; first template above the confidence wins. Overlays
# and more specific screens come before the screens they sit on top of
# (e.g. scouting shows Next AND End Battle, so Next must win).
STATES = [
    ("return_home_button", "END"),
    ("next_button", "SCOUT"),
    ("confirm_attack_button", "ARMY"),
    ("find_match_button", "MENU"),
    ("surrender_button", "BATTLE"),
    ("attack_button", "HOME"),
    ("bb_return_home", "BBEND"),   # started / restarted while on the Builder Base: finish up and sail home
    ("bb_attack_button", "BBHOME"),
]
STATE_LABELS = {
    None: "Unknown screen", "HOME": "Home village", "MENU": "Attack menu", "ARMY": "Army screen",
    "SCOUT": "Scouting base", "BATTLE": "In battle", "CONFIRM": "Surrendering", "END": "Battle over",
    "BBHOME": "Builder Base", "BBEND": "Builder Base battle over",
}

DEFAULTS = {
    "adb_path": r"C:\Program Files\platform-tools\adb.exe",
    "device": "",
    "auto_connect_target": "127.0.0.1:5555",
    "match_confidence": 0.7,
    "min_screenshot_interval": 0.4,
    "tap_delay": 0.5,
    "loot_force_attack": False,
    "loot_gold_threshold": 700000,
    "loot_elixir_threshold": 700000,
    "loot_require_both": False,
    "loot_settle_delay": 2.5,
    "loot_recheck_delay": 1.5,
    "loot_max_plausible": 20000000,
    "surrender_damage_threshold": 50,
    "damage_confirm_count": 2,
    "battle_max_wait": 180,
    "damage_stall_seconds": 20,
    "timer_poll_interval": 2.0,
    "hold_deploy": True,
    "deploy_pan": "top-left",  # pan the camera into this corner before deploying ("off" to disable)
    "hero_abilities": True,
    "hero_ability_delay": 10,
    "deploy_spells": False,
    "rotate_accounts": False,
    "stop_when_all_busy": True,
    "accounts": "",
    "hold_ms_per_troop": 120,
    "deploy_select_delay": 0.15,
    "deploy_tap_delay": 0.08,
    "matchmaking_settle_delay": 2.0,
    "return_home_delay": 3.0,
    "bank_spend_enabled": False,
    "builder_upgrades_enabled": False,
    "skip_town_hall": True,
    "lab_upgrades_enabled": False,
    "builder_base_enabled": False,  # visit the Builder Base on every account: collect, cart, upgrades, daily stars
    "bb_visit_hours": 3,
    "bb_attacks_enabled": True,
    "bb_builder_upgrades": True,
    "bb_lab_upgrades": True,
    "bb_bonus_days": {},  # account -> date its daily Star Bonus was collected (no more attacks that day)
    "bank_spend_threshold": 15000000,
    "bank_scroll_duration_ms": 600,
    "bank_scroll_delay": 0.6,
    "bank_post_spend_delay": 1.0,
    "watchdog_enabled": True,
    "gfx_profile": 0,  # which GFX_PROFILES entry BlueStacks runs with; moved on automatically after crashes
    "emulator_exe_path": "",
    "discord_webhook": "https://discord.com/api/webhooks/1541513961076293833/CtGCtGAaIJxf-MD68mBWdj6uZJ0Sf6x_E4Cj5bKIlmOsAoBgg91-1_IIqJ5s8V1olDQ7",  # the Anywhere link is posted here on every start
    "emulator_launch_args": "",
    "coc_package_name": "com.supercell.clashofclans",
    "coc_activity_name": "com.supercell.titan.GameApp",
    "watchdog_attack_timeout": 60,
    "game_launch_wait": 25,
    "emulator_boot_wait": 40,
    "adb_ready_timeout": 90,
    "tesseract_path": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    "ocr_region_padding_px": 4,
    "ocr_min_digits": 3,
    "groq_api_key": "",
    "groq_model": "",
    "use_groq_for_loot": True,
    "use_groq_for_bank": True,
    "groq_supervisor": True,
    "phone_view_enabled": True,
    "phone_view_port": 8765,
    "phone_view_key": "",  # random secret generated on first run; the phone link must include it
    "public_link_enabled": True,  # needs cloudflared.exe next to bot.py
    "coords": {},
    "ocr_regions": {},
    "fixed_points": {},
}

# (group, blurb, [(key, label, type)]) - type: bool/int/float/str/"secret"
SETTINGS = [
    ("Loot", "Which bases are worth attacking.", [
        ("loot_force_attack", "Attack every base (skip loot check)", bool),
        ("loot_require_both", "Require gold AND elixir (off = either)", bool),
        ("loot_gold_threshold", "Minimum gold", int),
        ("loot_elixir_threshold", "Minimum elixir", int),
        ("loot_settle_delay", "Wait before first read (s)", float),
        ("loot_recheck_delay", "Wait after Next (s)", float),
        ("loot_max_plausible", "Ignore reads above", int),
    ]),
    ("Battle", "Deploying and surrendering.", [
        ("deploy_spells", "Also drop spells at the drop point", bool),
        ("deploy_pan", "Pan view before deploying (top-left/top-right/bottom-left/bottom-right/off)", str),
        ("hero_abilities", "Use hero abilities", bool),
        ("hero_ability_delay", "Use abilities this long after deploying (s)", float),
        ("hold_deploy", "Hold-to-deploy (single drop spot only)", bool),
        ("surrender_damage_threshold", "Surrender at damage %", float),
        ("damage_confirm_count", "Damage reads in a row", int),
        ("damage_stall_seconds", "Surrender if damage stalls for (s)", float),
        ("battle_max_wait", "Surrender after (s) regardless", float),
        ("timer_poll_interval", "Damage check every (s)", float),
        ("hold_ms_per_troop", "Hold time per troop (ms)", int),
        ("deploy_select_delay", "Pause after selecting unit (s)", float),
        ("deploy_tap_delay", "Pause between taps (s)", float),
    ]),
    ("Upgrades", "From the home screen: free builders and the lab take the most expensive thing you can "
                 "afford; walls use storage above the threshold.", [
        ("builder_upgrades_enabled", "Builders: upgrade buildings / heroes", bool),
        ("skip_town_hall", "Never upgrade the Town Hall (don't rush)", bool),
        ("lab_upgrades_enabled", "Lab: research troops / spells", bool),
        ("bank_spend_enabled", "Buy walls when storage is full", bool),
        ("bank_spend_threshold", "Spend when storage >=", int),
        ("bank_scroll_duration_ms", "List swipe duration (ms)", int),
        ("bank_scroll_delay", "Pause after swipe (s)", float),
        ("bank_post_spend_delay", "Pause after purchase (s)", float),
    ]),
    ("Builder Base", "Each account gets a visit by boat: collect the collectors and the Elixir Cart, then the "
                     "switches below. Same on/off as the dashboard's Builder Base switch.", [
        ("builder_base_enabled", "Visit the Builder Base", bool),
        ("bb_attacks_enabled", "Attack until the daily Star Bonus", bool),
        ("bb_builder_upgrades", "Builders: upgrade buildings / Battle Machine", bool),
        ("bb_lab_upgrades", "Star Lab: research", bool),
        ("bb_visit_hours", "Revisit each account every (h)", float),
    ]),
    ("Accounts", "When every enabled builder + lab slot is busy (and walls are spent), switch to the next "
                 "Supercell ID account. All busy everywhere = keep farming here and re-check in 30 min.", [
        ("rotate_accounts", "Rotate accounts", bool),
        ("stop_when_all_busy", "Stop the bot when every account is busy", bool),
        ("accounts", "Only these (comma list, blank = all)", str),
    ]),
    ("Recovery", "Game relaunch is always on. Emulator restart needs the .exe path.", [
        ("watchdog_enabled", "Allow emulator restart", bool),
        ("gfx_profile", "BlueStacks graphics fallback (0 = as set; rises after crashes)", int),
        ("emulator_exe_path", "Emulator .exe", str),
        ("emulator_launch_args", "Emulator launch args", str),
        ("coc_package_name", "Game package", str),
        ("coc_activity_name", "Game activity", str),
        ("watchdog_attack_timeout", "Stuck-screen timeout (s)", float),
        ("game_launch_wait", "Game load wait (s)", float),
        ("emulator_boot_wait", "Emulator boot wait (s)", float),
        ("adb_ready_timeout", "ADB ready timeout (s)", float),
    ]),
    ("Engine", "Recognition, OCR and connection.", [
        ("groq_supervisor", "Groq fixes unknown screens and popups", bool),
        ("use_groq_for_loot", "Groq reads base loot (Tesseract backup)", bool),
        ("use_groq_for_bank", "Groq reads your storage (Tesseract backup)", bool),
        ("match_confidence", "Button match confidence (0-1)", float),
        ("min_screenshot_interval", "Min gap between screenshots (s)", float),
        ("tap_delay", "Pause after button tap (s)", float),
        ("ocr_region_padding_px", "OCR box padding (px)", int),
        ("ocr_min_digits", "OCR minimum digits", int),
        ("tesseract_path", "tesseract.exe", str),
        ("phone_view_enabled", "Phone live view (restart app to apply)", bool),
        ("phone_view_port", "Phone view port", int),
        ("public_link_enabled", "Public phone link (restart app)", bool),
        ("adb_path", "adb.exe", str),
        ("auto_connect_target", "Auto-connect address", str),
        ("groq_api_key", "Groq API key", "secret"),
        ("groq_model", "Groq vision model", str),
    ]),
]

RUNTIME_KEYS = ("scans", "plans", "account_tags", "_last_account_idx")  # saved by the bot, not settings
_cfg_lock = threading.Lock()


def load_config():
    cfg, err = json.loads(json.dumps(DEFAULTS)), None
    try:
        if os.path.exists(CONFIG_FILE):
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg.update(json.load(f))
    except Exception as e:  # keep a copy: the next save would otherwise replace it with defaults
        try:
            shutil.copy2(CONFIG_FILE, CONFIG_FILE + ".broken")
        except OSError:
            pass
        err = f"config.json couldn't be read ({e}); running on defaults. The old file is kept as config.json.broken."
    for k in [k for k in cfg if k not in DEFAULTS and k not in RUNTIME_KEYS]:
        del cfg[k]  # settings of removed features: dropped on the next save
    tagged = set((cfg.get("account_tags") or {}).values())
    misread = lambda a: a not in tagged and difflib.get_close_matches(a, tagged, 1, 0.75)
    for k in ("scans", "plans", "bb_bonus_days"):  # nothing filed under an unreadable / misread account name
        cfg[k] = {a: v for a, v in (cfg.get(k) or {}).items() if a not in ("", "?") and not misread(a)}
    for k in ("coords", "ocr_regions", "fixed_points"):
        cfg[k] = cfg.get(k) or {}
    for k, path in BUNDLED.items():
        if not (cfg.get(k) and os.path.exists(cfg[k])) and os.path.exists(path):
            cfg[k] = path
    cfg["bb_bonus_days"] = dict(cfg.get("bb_bonus_days") or {})
    cfg["discord_webhook"] = cfg.get("discord_webhook") or DEFAULTS["discord_webhook"]  # blank saved = the built-in
    player = r"C:\Program Files\BlueStacks_nxt\HD-Player.exe"
    if not os.path.isfile(cfg.get("emulator_exe_path") or "") and os.path.isfile(player):
        cfg["emulator_exe_path"] = player  # so crash recovery can restart BlueStacks
    return cfg, err


def save_config(cfg):
    """Atomic write: a kill mid-save can't corrupt config.json. The day's first save keeps yesterday's file in
    backups/ (last 7 days), so settings / drop lines / plans can always be got back."""
    with _cfg_lock:
        try:
            bdir = os.path.join(BASE_DIR, "backups")
            day = os.path.join(bdir, f"config-{time.strftime('%Y-%m-%d')}.json")
            if os.path.exists(CONFIG_FILE) and not os.path.exists(day):
                os.makedirs(bdir, exist_ok=True)
                shutil.copy2(CONFIG_FILE, day)
                for old in sorted(f for f in os.listdir(bdir) if f.startswith("config-"))[:-7]:
                    os.remove(os.path.join(bdir, old))
        except OSError:
            pass  # a backup problem must never stop the save
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
        for i in range(10):  # antivirus / OneDrive can hold the file for a moment
            try:
                return os.replace(tmp, CONFIG_FILE)
            except PermissionError:
                if i == 9:
                    raise
                time.sleep(0.3)


def tpath(name):
    return os.path.join(TEMPLATE_DIR, f"{name}.png")


log_file = logging.getLogger("lootfarmer")
log_file.setLevel(logging.INFO)
_fh = logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=2_000_000, backupCount=2, encoding="utf-8")
_fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
log_file.addHandler(_fh)


# ---------------------------------------------------------------------------
# ADB
# ---------------------------------------------------------------------------
class ADBError(Exception):
    pass


def decode_raw(data):
    """Raw `screencap` output: 12 or 16 byte header (w, h, fmt[, colorspace]) + RGBA.
    Skipping the device-side PNG encode is the single biggest speed/CPU win."""
    if not data or len(data) < 16:
        return None
    w, h = struct.unpack_from("<II", data)
    n = w * h * 4
    if not w or not h or len(data) - n not in (12, 16):
        return None
    rgba = np.frombuffer(data, np.uint8, n, len(data) - n).reshape(h, w, 4)
    return cv2.cvtColor(rgba, cv2.COLOR_RGBA2BGR)


def _pipe_reader(pipe, q):
    for line in iter(pipe.readline, b""):
        q.put(line)
    q.put(None)


class ADB:
    """Screenshots via exec-out raw; input via ONE persistent `adb shell`
    instead of spawning adb.exe for every tap."""

    def __init__(self, path, device):
        self.path, self.device = path, device
        self._sh = self._q = None
        self._n = 0
        self._lock = threading.Lock()
        self._persist_ok = True
        self._persist_fails = 0

    def _cmd(self, *a):
        return [self.path, *(["-s", self.device] if self.device else []), *a]

    def run(self, *a, timeout=15):
        try:
            return subprocess.run(self._cmd(*a), capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise ADBError(f"adb.exe not found at '{self.path}'")
        except OSError as e:
            raise ADBError(f"couldn't run adb.exe: {e}")
        except subprocess.TimeoutExpired:
            raise ADBError(f"adb {' '.join(a[:2])} timed out")

    def host(self, *a, timeout=15):
        try:
            r = subprocess.run([self.path, *a], capture_output=True, text=True, timeout=timeout,
                               creationflags=NO_WINDOW)
        except FileNotFoundError:
            raise ADBError(f"adb.exe not found at '{self.path}'")
        except OSError as e:
            raise ADBError(f"couldn't run adb.exe: {e}")
        except subprocess.TimeoutExpired:
            raise ADBError(f"adb {a[0]} timed out")
        return (r.stdout + r.stderr).strip()

    def devices(self):
        lines = self.host("devices", timeout=10).splitlines()[1:]
        return [p[0] for p in (ln.split() for ln in lines) if len(p) >= 2 and p[1] == "device"]

    def connect(self, target):
        return self.host("connect", target, timeout=10)

    def close_shell(self):
        with self._lock:
            self._kill()

    def _kill(self):
        if self._sh:
            try:
                self._sh.kill()
            except Exception:
                pass
        self._sh = None

    def _persistent(self, cmd, timeout):
        with self._lock:
            if self._sh is None or self._sh.poll() is not None:
                try:
                    self._sh = subprocess.Popen(self._cmd("shell"), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, creationflags=NO_WINDOW)
                except OSError as e:
                    raise ADBError(f"couldn't start adb shell: {e}")
                self._q = queue.Queue()
                threading.Thread(target=_pipe_reader, args=(self._sh.stdout, self._q), daemon=True).start()
            self._n += 1
            tag = f"@@{self._n}@@"  # sent as "@@"N"@@" so a tty echo of the command can't match it
            try:
                self._sh.stdin.write(f'{cmd}; echo "@@"{self._n}"@@"\n'.encode())
                self._sh.stdin.flush()
            except OSError:
                self._kill()
                raise ADBError("adb shell closed")
            out, deadline = [], time.time() + timeout
            while True:
                try:
                    line = self._q.get(timeout=max(0.05, deadline - time.time()))
                except queue.Empty:
                    if time.time() >= deadline:
                        self._kill()
                        raise ADBError(f"adb shell timed out ({cmd[:40]})")
                    continue
                if line is None:
                    self._kill()
                    raise ADBError("adb shell disconnected")
                s = line.decode(errors="replace").rstrip("\r\n")
                if tag in s:
                    return "\n".join(out)
                out.append(s)

    def shell(self, cmd, timeout=10):
        if self._persist_ok:
            try:
                out = self._persistent(cmd, timeout)
                self._persist_fails = 0
                return out
            except ADBError:
                self._persist_fails += 1
        r = self.run("shell", cmd, timeout=timeout)
        if r.returncode != 0 and not r.stdout:
            raise ADBError((r.stderr or b"adb shell failed").decode(errors="replace").strip()[:120])
        if self._persist_fails >= 3:  # device answers, persistent shell doesn't: stop using it
            self._persist_ok = False
        return r.stdout.decode(errors="replace")

    def screenshot(self):
        img = decode_raw(self.run("exec-out", "screencap", timeout=20).stdout)  # slow while the emulator boots
        if img is None:
            png = self.run("exec-out", "screencap", "-p", timeout=10).stdout
            img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR) if png else None
        if img is None:
            raise ADBError("screenshot failed - is the emulator running?")
        return img

    def tap(self, x, y):
        self.shell(f"input tap {int(x)} {int(y)}")

    def taps(self, x, y, n):
        self.shell("; ".join([f"input tap {int(x)} {int(y)}"] * n), timeout=10 + n)

    def swipe(self, x1, y1, x2, y2, ms):
        self.shell(f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(ms)}", timeout=10 + ms / 1000)

    def back(self):
        self.shell("input keyevent 4")

    def ready(self):
        try:
            return "ok" in self.run("shell", "echo", "ok", timeout=5).stdout.decode(errors="replace")
        except ADBError:
            return False


# ---------------------------------------------------------------------------
# Vision + OCR
# ---------------------------------------------------------------------------
SCALE = 0.5  # match at half resolution: ~4x faster, still precise to a couple of px


class Vision:
    def __init__(self, tdir=TEMPLATE_DIR):
        self.tdir = tdir
        self._t = {}
        self._frame = self._small = None

    def template(self, name):
        p = os.path.join(self.tdir, f"{name}.png")
        try:
            m = os.path.getmtime(p)
        except OSError:
            return None
        c = self._t.get(name)
        if c is None or c[0] != m:
            img = cv2.imread(p, cv2.IMREAD_COLOR)
            if img is None:
                return None
            c = (m, img.shape[:2], cv2.resize(img, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_AREA))
            self._t[name] = c
        return c

    def score(self, frame, name):
        """(score, cx, cy) of the best match in full-res coords, or None if no template."""
        t = self.template(name)
        if t is None:
            return None
        if self._frame is not frame:
            self._frame, self._small = frame, cv2.resize(frame, None, fx=SCALE, fy=SCALE,
                                                         interpolation=cv2.INTER_AREA)
        s, tt = self._small, t[2]
        if tt.shape[0] > s.shape[0] or tt.shape[1] > s.shape[1]:
            return None
        _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(s, tt, cv2.TM_CCOEFF_NORMED))
        (h, w) = t[1]
        return v, int(loc[0] / SCALE + w / 2), int(loc[1] / SCALE + h / 2)

    def find(self, frame, name, conf):
        r = self.score(frame, name)
        return (r[1], r[2]) if r and r[0] >= conf else None

    def undimmed(self, frame, name, hit):
        """Template matching ignores brightness, so a button under a popup's dark overlay still 'matches'.
        Clean screens measure 0.97-1.0 of the template's brightness, overlaid ones 0.3-0.5."""
        t = self.template(name)
        h, w = t[1]
        x, y = hit[0] - w // 2, hit[1] - h // 2
        patch = frame[max(0, y):y + h, max(0, x):x + w]
        return patch.size > 0 and patch.mean() >= 0.8 * t[2].mean()


def crop(frame, bbox, pad=0):
    x0, y0, x1, y1 = bbox
    h, w = frame.shape[:2]
    x0, y0, x1, y1 = max(0, x0 - pad), max(0, y0 - pad), min(w, x1 + pad), min(h, y1 + pad)
    return frame[y0:y1, x0:x1] if x1 > x0 and y1 > y0 else None


def ocr_number(frame, bbox, pad=4, min_digits=1, thorough=True):
    """Digits-only OCR. One cheap Tesseract pass; if that fails, a 12-pass vote
    (thorough) or just one inverted pass (fast polling, e.g. damage %)."""
    c = crop(frame, bbox, pad)
    if not HAVE_TESS or c is None:
        return None
    gray = cv2.cvtColor(cv2.resize(c, None, fx=4, fy=4, interpolation=cv2.INTER_CUBIC), cv2.COLOR_BGR2GRAY)
    gray = cv2.bilateralFilter(gray, 5, 40, 40)
    otsu = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

    def read(img, psm):
        try:
            txt = pytesseract.image_to_string(
                img, config=f"--psm {psm} -c tessedit_char_whitelist=0123456789", timeout=5)
        except Exception:
            return None
        d = re.sub(r"\D", "", txt)
        return int(d) if len(d) >= min_digits else None

    v = read(otsu, 7)
    if v is not None or not thorough:
        return v if v is not None else read(cv2.bitwise_not(otsu), 7)
    variants = [otsu, cv2.bitwise_not(otsu),
                cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 5),
                cv2.dilate(otsu, np.ones((2, 2), np.uint8))]
    results = [r for img in variants for psm in (7, 8, 6) if (r := read(img, psm)) is not None]
    if not results:
        return None
    counts = {r: results.count(r) for r in results}
    best = max(counts.values())
    return max((r for r, n in counts.items() if n == best), key=lambda r: len(str(r)))


_groq = {}
GROQ_W = 960  # screenshots go to Groq at 960px wide: fast upload, still readable

GROQ_SCREENS = {"home": "HOME", "attack_menu": "MENU", "army": "ARMY", "scouting": "SCOUT",
                "battle": "BATTLE", "battle_end": "END"}  # dialogs are never auto-confirmed
# Never let the AI tap anything that could spend gems/money.
GROQ_BLOCKED = ("gem", "buy", "purchase", "shop", "offer", "boost", "upgrade", "train", "pass", "spend",
                "$", "\u00a3", "\u20ac", "pay", "store")

RESCUE_PROMPT = """This is a WxH screenshot of Clash of Clans running in an emulator, sent by a bot that \
farms loot. The bot doesn't recognise this screen. Reply with ONLY this JSON:
{"screen": "home|attack_menu|army|scouting|battle|surrender_dialog|battle_end|loading|popup|other",
 "button": [x, y] or null, "action": "tap|back|wait|relaunch", "target": "text on what to tap", "reason": "few words"}
"button" is in this image's pixels. For these screens give the centre of the key button: home = orange \
"Attack!" (bottom-left); attack_menu = "Find a Match"; army = green "Attack!"; scouting = "Next"; \
battle = "Surrender" or "End Battle"; surrender_dialog = "Okay"; battle_end = "Return Home".
Loading / connecting / clouds: action "wait". Any popup, news, reward, event, offer or dialog: action "tap" \
on its close "X", "Okay", "Close", "Later", "Continue", "Reload" or "Try Again". Exit-game dialog: tap "Cancel".
NEVER tap anything that spends gems or money or says Buy, Purchase, Shop, Boost, Upgrade or Train.
Black, frozen, Android home screen, crash dialog or not Clash of Clans: action "relaunch"."""
LOOT_PROMPT = """Clash of Clans scouting screen. Read the "Available Loot" numbers at the top-left: first row \
is gold (yellow coin), second row is elixir (pink drop). Reply with ONLY JSON \
{"gold": integer or null, "elixir": integer or null} - digits only, no separators."""
STORAGE_PROMPT = """Clash of Clans home village. Read the player's own storage totals at the top-right: \
gold (yellow coin) and elixir (pink drop). Reply with ONLY JSON {"gold": integer or null, "elixir": integer or null}."""
def groq_ask(cfg, frame, prompt, max_tokens=250):
    """Send a downscaled screenshot + prompt to a Groq vision model; returns the JSON dict or None."""
    key, model = cfg.get("groq_api_key"), cfg.get("groq_model")
    if not key or not model:
        return None
    try:
        from groq import Groq
    except ImportError:
        return None
    import base64
    h = int(frame.shape[0] * GROQ_W / frame.shape[1])
    img = cv2.resize(frame, (GROQ_W, h), interpolation=cv2.INTER_AREA)
    try:
        client = _groq.get(key) or _groq.setdefault(key, Groq(api_key=key, timeout=15, max_retries=1))
        b64 = base64.b64encode(cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()).decode()
        r = client.chat.completions.create(
            model=model, temperature=0, max_completion_tokens=max_tokens, response_format={"type": "json_object"},
            messages=[{"role": "user", "content": [
                {"type": "text", "text": prompt.replace("WxH", f"{GROQ_W}x{h}")},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}]}])
        out = json.loads(r.choices[0].message.content)
        return out if isinstance(out, dict) else None
    except Exception as e:
        log_file.warning(f"Groq request failed: {e}")
        _groq["last_error"] = str(e)
        return None


def as_int(v):
    try:
        return int(str(v).replace(",", "").replace(" ", "")) if v is not None else None
    except ValueError:
        return None


_DIGITS = {}


def digit_glyphs():
    """templates/digits.png: the game's own 0-9, 20x28 each, averaged from real screenshots."""
    if "t" not in _DIGITS:
        strip = cv2.imread(tpath("digits"), cv2.IMREAD_GRAYSCALE)
        _DIGITS["t"] = None if strip is None else [strip[:, i * 20:(i + 1) * 20].astype(np.float32) / 255
                                                    for i in range(10)]
    return _DIGITS["t"]


def read_digit_blobs(mask, suffix_ok=False):
    """Keep only digit-shaped blobs sitting in one row (drops icons, bar shine, stray highlights), then
    recognise each blob against the game's own digit shapes. No Tesseract: exact on every storage bar,
    list price and Confirm button tested (25/25, cross-validated), and ~100x faster."""
    n, lab, st, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    H, W = mask.shape
    comps = [i for i in range(1, n) if st[i, 3] >= H * 0.15 and st[i, 2] <= st[i, 3] * 1.3  # digit-shaped
             and st[i, 0] > 0 and st[i, 1] > 0 and st[i, 0] + st[i, 2] < W and st[i, 1] + st[i, 3] < H]
    glyphs = digit_glyphs()
    if not comps or glyphs is None:
        return None
    h = np.median([st[i, 3] for i in comps])
    comps = [i for i in comps if 0.6 * h <= st[i, 3] <= 1.4 * h]
    if not comps:
        return None
    cy = np.median([st[i, 1] + st[i, 3] / 2 for i in comps])  # one text line: drop shapes above/below it
    comps = sorted((i for i in comps if abs(st[i, 1] + st[i, 3] / 2 - cy) <= 0.5 * h), key=lambda i: st[i, 0])
    runs, cur = [], [comps[0]]
    for a, b in zip(comps, comps[1:]):  # split where the gap is wider than a digit (e.g. before the icon)
        if st[b, 0] - (st[a, 0] + st[a, 2]) <= h:
            cur.append(b)
        else:
            runs.append(cur)
            cur = [b]
    runs.append(cur)
    out = ""
    for i in max(runs, key=len):
        x, y, w, hh = st[i, :4]
        g = cv2.resize((lab[y:y + hh, x:x + w] == i).astype(np.float32), (20, 28), interpolation=cv2.INTER_AREA)
        dist = [float(np.abs(g - glyphs[k]).mean()) for k in range(10)]
        if min(dist) > 0.15:  # not a digit (e.g. the 'd'/'H' of a timer): real digits match within ~0.06
            if suffix_ok and out:
                break  # a trailing '%' after the number
            return None
        out += str(dist.index(min(dist)))
    return int(out)


def damage_percent(frame, bbox):
    """'Overall Damage' number: white digits followed by '%'."""
    c = crop(frame, bbox, 0)
    if c is None:
        return None
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    v = read_digit_blobs((hsv[:, :, 2] > hsv[:, :, 2].max() * 0.8) & (hsv[:, :, 1] < 70), suffix_ok=True)
    return v if v is not None and v <= 100 else None


def white_number(frame, bbox):
    """The game's white counter digits (storage bars). Near-white is relative to the brightest pixel, so
    dimmed screens work too. 10/10 on real screenshots."""
    c = crop(frame, bbox, 0)
    if c is None:
        return None
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    return read_digit_blobs((hsv[:, :, 2] > hsv[:, :, 2].max() * 0.8) & (hsv[:, :, 1] < 70))


def panel_box(frame):
    """The builder/lab dropdown list has a pure-white 2px border: columns white for a long vertical run.
    Returns (x0, x1, y0, y1) of its inside, or None if no list is open."""
    g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    cols = np.where((g >= 245).sum(axis=0) > g.shape[0] * 0.3)[0]
    if len(cols) < 2:
        return None
    right = cols[cols - cols[0] >= 250]  # the list's right border is the NEXT long white column, not the last
    if len(right) == 0:                  # (the Builder Base has a white edge at the far right of the screen)
        return None
    x0, x1 = int(cols[0]), int(right[0])
    ys = np.where(g[:, x0] >= 245)[0]
    # the longest unbroken run is the list's own border (a button card below can line up with it)
    runs = np.split(ys, np.where(np.diff(ys) > 3)[0] + 1)
    run = max(runs, key=len)
    return x0 + 3, x1 - 2, int(run[0]) + 3, int(run[-1]) - 2


# Top bar counters sit in fixed places (1920x1080 layout) whatever icon/skin shows (builder, goblin, ...).
TOP_BAR = {"lab": ((662, 61), (680, 30, 820, 95), (600, 20, 680, 105)),
           "builder": ((902, 70), (955, 30, 1060, 95), (840, 20, 920, 105)),  # tap point, counter, icon
           "bb_lab": ((860, 60), (840, 30, 940, 95), (750, 20, 830, 105)),
           "bb_builder": ((1040, 60), (1080, 30, 1185, 95), (990, 20, 1075, 105))}
KIND_NAMES = {"builder": "Builder", "lab": "Lab", "bb_builder": "Builder Base builder", "bb_lab": "Star Lab"}
BB_STARS = (95, 866, 200, 910)  # the 'x/y' daily star counter on the Builder Base Attack button


def top_bar(frame, kind):
    """(tap point, counter box) of the lab or builder counter, scaled to this screen."""
    k = frame.shape[1] / 1920
    (x, y), box, _ = TOP_BAR[kind]
    return (int(x * k), int(y * k)), tuple(int(v * k) for v in box)


def goblin_icon(frame, kind):
    """The game shows the green goblin face on a counter only when the normal builders / researcher are all
    busy and just the goblin (costs gems) is free. Measured: goblin 0.33-0.36 green, normal icons <=0.12."""
    k = frame.shape[1] / 1920
    c = crop(frame, tuple(int(v * k) for v in TOP_BAR[kind][2]), 0)
    if c is None:
        return False
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    return float(((hsv[:, :, 0] >= 30) & (hsv[:, :, 0] <= 80) & (hsv[:, :, 1] > 90) & (hsv[:, :, 2] > 90)).mean()) > 0.22


def read_counter(frame, box):
    """'3/5' -> (3, 5). Anything that isn't a digit (the icon, the '/') splits the groups."""
    glyphs = digit_glyphs()
    c = crop(frame, box, 0)
    if c is None or glyphs is None:
        return None
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    m = ((hsv[:, :, 2] > hsv[:, :, 2].max() * 0.8) & (hsv[:, :, 1] < 70)).astype(np.uint8)
    n, lab, st, _ = cv2.connectedComponentsWithStats(m, 8)
    groups, cur = [], ""
    for i in sorted((i for i in range(1, n) if st[i, 3] >= m.shape[0] * 0.3), key=lambda i: st[i, 0]):
        x, y, w, h = st[i, :4]
        g = cv2.resize((lab[y:y + h, x:x + w] == i).astype(np.float32), (20, 28), interpolation=cv2.INTER_AREA)
        dist = [float(np.abs(g - glyphs[k]).mean()) for k in range(10)]
        if min(dist) <= 0.15:
            cur += str(dist.index(min(dist)))
        elif cur:
            groups.append(cur)
            cur = ""
    if cur:
        groups.append(cur)
    return (int(groups[0]), int(groups[1])) if len(groups) == 2 else None


def troop_bar(frame):
    """Cards on the battle bar, left to right: [(x, y, kind, count)]. Each card is found by its dark
    outline (two tall vertical edges one card-width apart). kind: 'troop' (blue card with a count),
    'spell' (purple card with a count), 'single' (hero / siege / pet: no count), 'used' (greyed x0),
    'empty' (dashed slot)."""
    glyphs = digit_glyphs()
    if glyphs is None:
        return []  # templates/digits.png missing
    H, W = frame.shape[:2]
    ya, yb = int(H * 0.833), int(H * 0.972)
    g = cv2.cvtColor(frame[ya:yb], cv2.COLOR_BGR2GRAY).astype(np.float32)
    col = (np.abs(cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)) > 60).mean(axis=0)
    peaks = []
    for x in range(1, W - 1):
        if col[x] > 0.55 and col[x] >= col[x - 1] and col[x] >= col[x + 1] and (not peaks or x - peaks[-1] >= 8):
            peaks.append(x)
    cw = int(W * 0.0685)  # card width, ~131px at 1920
    cards, end = [], -1
    bar_hsv = cv2.cvtColor(frame[ya:yb], cv2.COLOR_BGR2HSV)
    for a in peaks:
        if a <= end:
            continue
        inside = bar_hsv[:, a + 5:a + 12]
        if inside[:, :, 1].mean() > 100 and inside[:, :, 2].mean() < 110:
            continue  # dark blue bar background, not a card (e.g. a selected card's thick border)
        b = next((p for p in peaks if cw - 6 <= p - a <= cw + 16), None)  # +16: a selected card's white border
        if b:
            cards.append((a, b))
            end = b
    top = int(H * 0.826)

    def number(y0, y1, x0, x1, min_h):
        """White digits in this box (e.g. the 'x16' count, or the level badge), or None."""
        cc = cv2.cvtColor(frame[y0:y1, x0:x1], cv2.COLOR_BGR2HSV)
        cm = ((cc[:, :, 2] > 200) & (cc[:, :, 1] < 60)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(cm, 8)
        digits = ""
        for i in sorted((i for i in range(1, n) if st[i, 3] >= min_h), key=lambda i: st[i, 0]):
            x, y, w, h = st[i, :4]
            gl = cv2.resize((lab[y:y + h, x:x + w] == i).astype(np.float32), (20, 28), interpolation=cv2.INTER_AREA)
            dist = [float(np.abs(gl - glyphs[k]).mean()) for k in range(10)]
            if min(dist) <= 0.15:
                digits += str(dist.index(min(dist)))
            elif digits:
                break
        return int(digits) if digits else None

    def level_badge(a, b):
        """Every real card has a dark level badge bottom-left with white digits (a smaller font than the
        count, so just look for a digit-sized white blob on dark). A dashed empty slot showing the base doesn't."""
        x0, y0 = a + 2, int(H * 0.91)
        cc = cv2.cvtColor(frame[y0:int(H * 0.985), x0:a + (b - a) // 2], cv2.COLOR_BGR2HSV)
        white = ((cc[:, :, 2] > 200) & (cc[:, :, 1] < 60)).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(white, 8)
        for i in range(1, n):
            x, y, w, h = st[i, :4]
            if 0.013 * H <= h <= 0.02 * H and 3 <= w <= 0.016 * W:
                around = cc[max(0, y - 4):y + h + 4, max(0, x - 4):x + w + 4]
                if float(around[:, :, 2][around[:, :, 2] <= 200].mean()) < 110:
                    return True
        return False

    out = []
    for a, b in cards:
        hsv = cv2.cvtColor(frame[ya:yb, a + 4:b - 4], cv2.COLOR_BGR2HSV)
        hue, sat, val = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
        colored = (sat > 90) & (val > 90) & ~((hue >= 30) & (hue <= 85))  # any card colour, not grass
        grey = (sat < 40) & (val > 60)
        cnt = number(top, top + 50, a + (b - a) // 3, b - 4, 18)  # 'x16' at the top-right, after the 'x'
        if cnt is None:  # a selected card sits ~15px higher
            cnt = number(top - int(H * 0.017), top + 35, a + (b - a) // 3, b - 4, 18)
        if grey.mean() > 0.4:
            kind = "used"
        elif colored.mean() < 0.2 or not level_badge(a, b):
            kind = "empty"
        elif cnt is None:
            kind = "single"
        else:
            hh = cv2.cvtColor(frame[top:top + 45, a + 6:a + (b - a) // 3], cv2.COLOR_BGR2HSV)
            hdr = hh[:, :, 0][(hh[:, :, 1] > 90) & (hh[:, :, 2] > 90)]
            hue = np.median(hdr) if len(hdr) else 0  # blue troop ~104, purple spell ~126, red SUPER troop ~179
            kind = "spell" if 115 <= hue <= 150 else "troop"
        out.append(((a + b) // 2, (ya + yb) // 2, kind, cnt))
    return out


def confirm_price(frame, hit):
    """Price on the upgrade window's green Confirm (hit = its 'Confirm' label). White normally; yellow when a
    discount applies - then the old price is printed struck-out underneath, so only the line right under the
    label is read."""
    k = frame.shape[1] / 1920
    v = white_number(frame, (hit[0] - int(160 * k), hit[1] + int(12 * k), hit[0] + int(130 * k), hit[1] + int(75 * k)))
    if v:
        return v
    c = crop(frame, (hit[0] - int(160 * k), hit[1] + int(8 * k), hit[0] + int(130 * k), hit[1] + int(56 * k)), 0)
    if c is None:
        return None
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    return read_digit_blobs((hsv[:, :, 0] >= 18) & (hsv[:, :, 0] <= 35) & (hsv[:, :, 1] > 120) & (hsv[:, :, 2] > 200))


# Built-in categories (lower-case, matched fuzzily). Anything not here is 'other'.
UPGRADE_CATEGORIES = {
    "heroes": ["barbarian king", "archer queen", "grand warden", "royal champion", "minion prince", "dragon duke", "hero hall",
               "battle machine", "battle copter"],
    "guardians": ["longshot", "smasher", "logger"],
    "army": ["laboratory", "army camp", "barracks", "dark barracks", "spell factory", "dark spell factory",
             "clan castle", "workshop", "pet house", "blacksmith", "builder barracks", "star laboratory",
             "reinforcement camp", "healing hut", "clock tower", "ottos outpost"],
    "defences": ["cannon", "archer tower", "mortar", "air defense", "wizard tower", "air sweeper", "hidden tesla",
                 "bomb tower", "x bow", "inferno tower", "eagle artillery", "scattershot", "builders hut",
                 "spell tower", "monolith", "multi archer tower", "ricochet cannon", "firespitter",
                 "multi gear tower", "double cannon", "firecrackers", "crusher", "guard post", "multi mortar",
                 "roaster", "giant cannon", "mega tesla", "lava launcher", "air bombs"],
    "traps": ["bomb", "spring trap", "giant bomb", "giga bomb", "air bomb", "seeking air mine", "skeleton trap", "tornado trap",
              "push trap", "mine", "mega mine"],
    "resources": ["gold mine", "elixir collector", "dark elixir drill", "gold storage", "elixir storage",
                  "dark elixir storage", "gem mine"],
    "town hall": ["town hall", "builder hall"],
}
# Builder Base: without a list of your own, go for the Builder Hall and the 6th builder (O.T.T.O / B.O.B's hut) first
BB_DEFAULT_ROWS = [{"name": n, "base": "builder", "th": ""} for n in
                   ("builder hall", "ottos outpost", "battle machine", "battle copter", "builder barracks",
                    "star laboratory", "clock tower")]


def norm_name(s):
    """'Archer Tower x3' / 'Lonashot&' / 'Builder's Hut (lvl 5)' -> 'archer tower' / 'lonashot' / 'builders hut'."""
    s = re.sub(r"\(.*?\)", " ", str(s).lower())
    s = re.sub(r"(?<=[a-z.])0|0(?=[a-z.'])", "o", s)  # OCR reads O as 0 inside words: '0.T.T.0's' = O.T.T.O's
    s = re.sub(r"\b(x\s*\d+|lvl\s*\d+|level\s*\d+|to\s*\d+|\d+)\b", " ", s)
    s = re.sub(r"[^a-z ]", "", s.replace("-", " ").replace(".", ""))
    return re.sub(r"\s+", " ", s).strip()


def name_match(a, b):
    """Fuzzy: OCR turns Longshot into 'Lonashot&' and Storage into 'Storaae'."""
    import difflib
    a, b = (re.sub(r"^new ", "", norm_name(x)) for x in (a, b))  # the lab list tags fresh items 'New'
    if not a or not b:
        return False
    if a == b:
        return True
    if len(a.split()) != len(b.split()):  # 'dark elixir storage' is not 'elixir storage'
        return False
    if [w[0] for w in a.split()] != [w[0] for w in b.split()]:  # OCR keeps first letters; 'hog rider' != 'hog glider'
        return False
    # short names need a closer match ('miner' is not 'mine'); longer ones survive OCR slips ('scabbershot')
    return difflib.SequenceMatcher(None, a, b).ratio() >= (0.9 if min(len(a), len(b)) <= 6 else 0.8)


def category_of(name):
    exact = next((c for c, ns in UPGRADE_CATEGORIES.items() if norm_name(name) in ns), None)
    if exact:
        return exact
    best = ("other", 0)
    for cat, names in UPGRADE_CATEGORIES.items():
        for n in names:
            if name_match(name, n) and len(n) > best[1]:  # longest match: 'dark elixir storage' over 'elixir storage'
                best = (cat, len(n))
    return best[0]


WIKI_DIR = os.path.join(BASE_DIR, "wiki")
_WIKI = {}
# Hammer Jam (50% off), Gold Pass (up to 20% off), both at once...: every price on one list has the same discount
DISCOUNTS = (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6, 0.55, 0.5, 0.45, 0.4, 0.35, 0.3)


def wiki_data():
    """wiki/buildings.json (made by build_wiki_data.py): per building its levels with cost + required hall level."""
    if "d" not in _WIKI:
        try:
            with open(os.path.join(WIKI_DIR, "buildings.json"), encoding="utf-8") as f:
                _WIKI["d"] = json.load(f)
        except (OSError, ValueError):
            _WIKI["d"] = {}
    return _WIKI["d"]


def wiki_key(name, base):
    """'home:cannon' for an OCR'd list name like 'Cannon x4' (fuzzy), or None."""
    k = (norm_name(name), base)
    if k not in _WIKI:
        import difflib
        hits = [key for key, v in wiki_data().items() if v["base"] == base and name_match(k[0], key.split(":", 1)[1])]
        # several fuzzy hits ('Giaa Bomb': Giga Bomb / Giant Bomb): the closest one
        _WIKI[k] = max(hits, key=lambda key: difflib.SequenceMatcher(None, k[0], key.split(":", 1)[1]).ratio(),
                       default=None)
    return _WIKI[k]


def level_for_price(key, price, factor=1.0):
    """Current level of a building whose next upgrade costs `price` (after `factor` discount), or None."""
    e = wiki_data().get(key)
    if not e or not price:
        return None
    full = price / factor
    hits = [lv["level"] - 1 for lv in e["levels"]
            if lv["cost"] and abs(lv["cost"] - full) <= max(lv["cost"] * 0.012, 600)]
    return hits[0] if len(hits) == 1 else None  # two levels with that price: unknown (the scan then reads it)


def infer_discount(rows, base):
    """The discount that makes the most list prices line up with real wiki prices (smallest discount on a tie)."""
    best = (0, 1.0)
    for f in DISCOUNTS:
        n = sum(1 for nm, p in rows if (k := wiki_key(nm, base)) and level_for_price(k, p, f) is not None)
        if n > best[0]:
            best = (n, f)
    return best[1]


def max_level(key, hall_level, hero_hall=None):
    """Highest level of this building allowed at that Town Hall / Builder Hall (heroes: by Hero Hall level)."""
    e = wiki_data().get(key)
    if not e:
        return None
    hall = e["levels"][-1].get("hall", "town hall")
    if hall == "hero hall":
        if hero_hall is None and hall_level is not None:  # not scanned: the most the Hero Hall can be at this TH
            hero_hall = max_level("home:hero hall", hall_level)
        hall_level = hero_hall
    if hall == "none" or hall_level is None:
        return e["levels"][-1]["level"]
    ok = [lv["level"] for lv in e["levels"] if lv["req"] <= hall_level]
    return max(ok) if ok else 0


NO_WIKI = ("blacksmith", "reinforcement camp")  # real buildings without wiki data: level read from the game


def is_wall(name):
    """'Wall', 'Wall x28', "': Wall x2" - never bought from the builder list (walls have their own routine)."""
    return bool(re.fullmatch(r"walls?( x\w*)?", norm_name(name)))  # 'Wall xI50' = OCR'd 'Wall x150'


def count_of(name):
    m = re.search(r"x\s*(\d+)\s*$", name.strip(), re.I)
    return int(m.group(1)) if m else 1


def time_to_max(sd, base):
    """(builder-seconds of upgrades left until everything is max for this hall, upgrades left, builders) from a
    planner scan. Upgrade times from the wiki data; walls aren't counted, nor what's upgrading right now."""
    hall, hero = sd.get("hall"), sd.get("hero_hall")
    total = n = 0
    for r in sd.get("items", []):
        e = wiki_data().get(r.get("key"))
        mx = max_level(r["key"], hall, hero) if e else None
        if mx is None or r.get("level") is None:
            continue
        for lv in e["levels"]:
            if r["level"] < lv["level"] <= mx:
                total += lv.get("time", 0) * r.get("count", 1)
                n += r.get("count", 1)
    huts = sum(r.get("count", 1) for r in sd.get("items", []) if r.get("key") == "home:builders hut")
    return total, n, (huts or 5) if base == "home" else 3  # Builder Base: its top bar shows x/3


def read_clipboard():
    """Windows clipboard text ('' if none). BlueStacks copies Android's clipboard here."""
    try:
        return subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], capture_output=True,
                              text=True, timeout=15, creationflags=NO_WINDOW).stdout or ""
    except Exception:
        return ""


def settings_drag_start(f):
    """y of a grey section-header band low in More Settings' list (rows are blue), or None."""
    k = f.shape[1] / 1920
    y0 = int(240 * k)
    col = f[y0:int(900 * k), int(500 * k)].astype(int)
    grey = (np.abs(col[:, 0] - col[:, 2]) < 25) & (col.mean(axis=1) > 170)
    runs, start = [], None
    for i, g in enumerate(list(grey) + [False]):
        if g and start is None:
            start = i
        elif not g and start is not None:
            if i - start >= 20 * k:
                runs.append(y0 + (start + i) // 2)
            start = None
    low = [y for y in runs if y >= 300 * k]  # room to drag at least ~280px up
    return max(low) if low else None


def export_items(data):
    """The game's 'Export Village data' JSON -> {'home': items, 'builder': items} in the planner's scan format.
    Exact levels of everything built (an upgrading one counts as its next level, like plan_progress does)."""
    ids = {i: k for k, e in wiki_data().items() for i in e.get("ids", [])}
    out = {}
    for base, parts in (("home", ("buildings", "traps", "heroes", "guardians")),
                        ("builder", ("buildings2", "traps2", "heroes2"))):
        items = {}
        for part in parts:
            for x in data.get(part) or []:
                k = ids.get(x.get("data"))
                if k and isinstance(x.get("lvl"), int):
                    up = "timer" in x
                    r = items.setdefault((k, x["lvl"] + up, up), {"key": k, "name": wiki_data()[k]["name"],
                                                                  "level": x["lvl"] + up, "count": 0, "upgrading": up})
                    r["count"] += x.get("cnt", 1)  # supercharged copies come as separate rows
        out[base] = list(items.values())
    return out


def available_slots(frame):
    """(normal, goblin) free slots from an open builder/lab list's 'Available!' rows. The goblin builder /
    researcher (costs gems) has a green face next to its row; the normal builder/researcher doesn't."""
    box = panel_box(frame)
    if not box or not HAVE_TESS:
        return 0, 0
    x0, x1, y0, y1 = box
    c = frame[y0:y1, x0:x1]
    try:
        d = pytesseract.image_to_data(cv2.cvtColor(c, cv2.COLOR_BGR2GRAY), config="--psm 11",
                                      output_type=pytesseract.Output.DICT, timeout=10)
    except Exception:
        return 0, 0
    normal = goblin = 0
    for i, t in enumerate(d["text"]):
        if t.lower().startswith("availab"):
            x, y, h = d["left"][i], d["top"][i], d["height"][i]
            face = cv2.cvtColor(c[max(0, y - 15):y + h + 15, max(0, x - 80):max(1, x - 8)], cv2.COLOR_BGR2HSV)
            green = ((face[:, :, 0] >= 30) & (face[:, :, 0] <= 85) & (face[:, :, 1] > 80) & (face[:, :, 2] > 80))
            if green.size and green.mean() > 0.15:  # measured: goblin ~0.3, normal faces <0.05
                goblin += 1
            else:
                normal += 1
    return normal, goblin


def list_rows(frame):
    """Rows of an open builder/lab list: [(name, price, affordable, (x, y))]. Prices are white when you can
    afford them and red when you can't; section headers are green; in-progress rows show a timer next to a
    green progress bar and are skipped."""
    box = panel_box(frame)
    if not box or not HAVE_TESS:
        return []
    x0, x1, py0, py1 = box
    c = frame[py0:py1, x0:x1]
    hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
    b, g, r = (c[:, :, i].astype(int) for i in range(3))
    white = (hsv[:, :, 2] > 190) & (hsv[:, :, 1] < 60)
    red = (r > 170) & (r - g > 90) & (r - b > 60)
    green = (g > 170) & (g - r > 40) & (g - b > 60)
    W, H = x1 - x0, py1 - py0
    split = int(W * 0.62)
    prof = (white | red)[:, :split].sum(axis=1)
    out, y = [], 0
    while y < H:
        if prof[y] <= 3:
            y += 1
            continue
        yb = y
        while yb < H and prof[yb] > 3:
            yb += 1
        ya, y = y, yb + 1
        if not 16 <= yb - ya <= 70 or green[ya:yb].sum() > white[ya:yb].sum():  # noise / section header
            continue
        a, bb = max(0, ya - 8), min(H, yb + 8)
        if green[a:bb, split - 60:].sum() > 50:  # upgrade in progress
            continue
        pw, pr = white[a:bb, split - 60:], red[a:bb, split - 60:]
        is_red = pr.sum() > pw.sum()
        price = read_digit_blobs(pr if is_red else pw)
        nm = (white | red)[ya:yb, :split].astype(np.uint8) * 255
        k = 48 / nm.shape[0]
        nm = cv2.resize(nm, None, fx=k, fy=k, interpolation=cv2.INTER_LINEAR)
        try:
            name = pytesseract.image_to_string(cv2.copyMakeBorder(255 - nm, 30, 30, 30, 30, cv2.BORDER_CONSTANT,
                                                                  value=255), config="--psm 7", timeout=5).strip()
        except Exception:
            name = ""
        if price and price >= 1000 and name and ya > 6:  # ya>6: skip the half-cut row at the top edge
            out.append((name, price, not is_red, (x0 + W // 4, py0 + (ya + yb) // 2)))
    return out


def icon_empty(frame, x, y, half=14):
    """A used-up troop slot turns grey: low saturation at its centre."""
    p = frame[max(0, y - half):y + half, max(0, x - half):x + half]
    return p.size > 0 and float(cv2.cvtColor(p, cv2.COLOR_BGR2HSV)[:, :, 1].mean()) < 35


# ---------------------------------------------------------------------------
# The bot (runs on its own thread; never touches Tk - talks to the UI via emit)
# ---------------------------------------------------------------------------
class Abort(Exception):
    pass


class Bot:
    def __init__(self, cfg, adb, emit, mode="farm"):
        self.cfg, self.adb, self.emit, self.mode = cfg, adb, emit, mode  # "farm", or "loot" = attack only
        self.stop_evt = threading.Event()
        self.v = Vision()
        self.stats = dict(attacks=0, skipped=0, walls=0, upgrades=0, switches=0, recoveries=0, errors=0)
        self.hits = {}
        self._last_shot = self._last_preview = 0.0
        self._bank_backoff = {}
        self._upgrade_backoff = {}
        self._busy_until = {}  # kind -> time: 'only the goblin is free' counts as busy for account rotation
        self._bb_next = {}     # account -> time of its next Builder Base visit
        self._acc_idx = -1
        self.loot = {}        # account -> [gold, elixir, dark, attacks] farmed this session
        self._names = list(dict.fromkeys((cfg.get("account_tags") or {}).values()))  # misreads snap to these
        self._saving_home = None  # account farming for its next home plan target: no Builder Base trips meanwhile
        self._last_name = None
        self._cur = self._pre = None  # (account, (gold, elixir, dark)) now / just before the attack
        self._switch_streak = 0
        self._rotate_pause_until = 0.0
        self._n_accounts = 0
        self._emu_restarts = []
        self._unreadable = 0
        self.game_relaunches = 0
        self._last_groq = 0.0
        self._groq_paused_until = 0.0
        self._warned = set()
        if HAVE_TESS:
            pytesseract.pytesseract.tesseract_cmd = cfg["tesseract_path"]

    # --- plumbing ---
    def log(self, msg, level="info"):
        getattr(log_file, {"ok": "info", "warn": "warning", "err": "error"}.get(level, "info"))(msg)
        self.emit("log", (level, msg))

    def bump(self, key, n=1):
        self.stats[key] += n
        self.emit("stats", dict(self.stats))

    def sleep(self, s):
        if self.stop_evt.wait(max(0.0, s)):
            raise Abort()

    def shot(self):
        if self.stop_evt.is_set():
            raise Abort()
        self.sleep(self.cfg["min_screenshot_interval"] - (time.time() - self._last_shot))
        frame = self.adb.screenshot()
        self._last_shot = time.time()
        if self._last_shot - self._last_preview > 1.0:
            self._last_preview = self._last_shot
            self.emit("frame", cv2.resize(frame, (960, int(960 * frame.shape[0] / frame.shape[1])),
                                          interpolation=cv2.INTER_AREA))
        return frame

    def find(self, frame, name):
        hit = self.v.find(frame, name, self.cfg["match_confidence"])
        if hit and name in ("attack_button", "bb_attack_button") and not self.v.undimmed(frame, name, hit):
            return None  # a popup/panel is darkening the village: not really on the home screen
        return hit

    def tap(self, xy, delay=None):
        self.adb.tap(*xy)
        self.sleep(self.cfg["tap_delay"] if delay is None else delay)

    def wait_for(self, name, timeout, poll=0.8):
        end = time.time() + timeout
        while True:
            hit = self.find(self.shot(), name)
            if hit or time.time() >= end:
                return hit
            self.sleep(poll)

    def detect(self, frame):
        if self.find(frame, "wall_okay_button"):  # an Okay/Cancel dialog covers whatever screen is behind it
            return None
        for name, state in STATES:
            hit = self.find(frame, name)
            if hit:
                self.hits[name] = hit
                return state
        return None

    # --- main loop ---
    def run(self):
        self.log("Farming started.", "ok")
        self.emit("stats", dict(self.stats))
        prev, unknown_since, backs, adb_fails, online = "START", None, 0, 0, None
        try:
            while True:
                try:
                    frame = self.shot()
                    if online is not True:
                        online = True
                        self.emit("device", True)
                    adb_fails = 0
                    if frame.shape[:2] != (1080, 1920):
                        self.fix_resolution(frame)
                        continue
                    self.hits = {}
                    state = self.detect(frame)
                    if state != prev:
                        self.emit("state", STATE_LABELS[state])
                    if state is None:
                        unknown_since = unknown_since or time.time()
                        stuck = time.time() - unknown_since
                        # the Supercell logo / loading clouds after a (re)start aren't 'stuck': a slow PC can take
                        # minutes there, and relaunching mid-load would just loop
                        loading = time.time() - getattr(self, "_launched_at", 0) < 180
                        if stuck > (180 if loading else self.cfg["watchdog_attack_timeout"]):
                            self.recover_game(f"stuck on an unrecognised screen for {stuck:.0f}s")
                            unknown_since, backs = None, 0
                        elif self.reload_if_disconnected(frame):
                            unknown_since = None
                        elif self.find(frame, "wall_okay_button"):
                            self.log("Closing an Okay/Cancel dialog with Back (never confirms).")
                            self.adb.back()
                            self.sleep(1.0)
                        elif (self.groq_on("groq_supervisor") and stuck >= 2.5 and time.time() - self._last_groq >= 5
                              and (state := self.groq_rescue(frame))):
                            unknown_since = None
                            getattr(self, "on_" + state.lower())(frame, prev)
                            prev = state
                            continue
                        elif stuck > 10 * (backs + 1) and backs < 3:  # also when Groq is down or unsure
                            self.log("Unrecognised screen - pressing Back to clear any popup.", "warn")
                            self.adb.back()
                            backs += 1
                        self.sleep(1.0)
                    else:
                        unknown_since, backs = None, 0
                        getattr(self, "on_" + state.lower())(frame, prev)
                    prev = state
                except ADBError as e:
                    adb_fails += 1
                    online = False
                    self.emit("device", False)
                    self.log(f"ADB problem: {e}", "warn")
                    prev = "START"
                    try:
                        self.recover_adb(adb_fails)
                    except Abort:
                        raise
                    except Exception as e2:  # recovery must never kill the loop
                        self.log(f"Recovery error: {e2}", "err")
                        self.sleep(10)
                except Abort:
                    raise
                except Exception:
                    self.bump("errors")
                    self.log("Unexpected error, carrying on:\n" + traceback.format_exc(limit=4), "err")
                    self.sleep(3)
        except Abort:
            pass
        self.log("Farming stopped.", "ok")
        self.emit("state", "Stopped")

    # --- screen handlers ---
    def account_name(self, frame):
        """Player name at the top-left of the home screen (e.g. 'GeoCol3'), snapped to names already seen."""
        import difflib
        k = frame.shape[1] / 1920
        c = frame[int(8 * k):int(52 * k), int(140 * k):int(520 * k)]
        hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
        raw = ((hsv[:, :, 2] > 170) & (hsv[:, :, 1] < 60)).astype(np.uint8)
        n, lab, st, _ = cv2.connectedComponentsWithStats(raw, 8)
        # letters: 8-24px tall, not touching the crop edge, sitting on one baseline. Decorations behind the name
        # (cakes, statues, snow) are taller / elsewhere - picking by 'tallest shape' dropped the lower-case letters.
        cand = [i for i in range(1, n) if 8 * k <= st[i, 3] <= 24 * k and st[i, 2] <= 30 * k
                and st[i, 1] > 0 and st[i, 1] + st[i, 3] < c.shape[0]]
        base = np.median([st[i, 1] + st[i, 3] for i in cand]) if cand else 0
        keep = [i for i in cand if abs(st[i, 1] + st[i, 3] - base) <= 3 * k]
        if keep:  # one word: drop shapes far to the right of the name (a gap wider than two letters)
            keep.sort(key=lambda i: st[i, 0])
            word = [keep[0]]
            for i in keep[1:]:
                if st[i, 0] - (st[word[-1], 0] + st[word[-1], 2]) > 30 * k:
                    break
                word.append(i)
            keep = word
        m = cv2.resize(np.isin(lab, keep).astype(np.uint8) * 255, None, fx=3, fy=3)
        try:
            words = pytesseract.image_to_string(cv2.copyMakeBorder(255 - m, 20, 20, 20, 20, cv2.BORDER_CONSTANT,
                                                                   value=255), config="--psm 7", timeout=5).split()
        except Exception:
            words = []
        name = words[0] if words else ""
        digits = re.sub(r"\D", "", name)  # GeoCol2 vs GeoCol3 are different accounts: never snap across digits
        near = difflib.get_close_matches(name, [n for n in self._names if re.sub(r"\D", "", n) == digits], 1, 0.7)
        if near:
            self._last_name = near[0]
        elif len(name) >= 3:
            self._names.append(name)
            self._last_name = name
        # unreadable (e.g. the XP bar animating over it just after a battle): the account hasn't changed
        return self._last_name or "?"

    def track_loot(self, frame, storage):
        """Credit the storage gained since just before the last attack to this account."""
        k = frame.shape[1] / 1920
        dark = white_number(frame, self.cfg["ocr_regions"].get(
            "bank_dark_region", tuple(int(v * k) for v in (1560, 225, 1830, 300))))
        now = (storage.get("gold"), storage.get("elixir"), dark)
        name = self.account_name(frame)
        pre, self._pre = self._pre, None
        if pre and pre[0] == name and None not in now[:2] and None not in pre[1][:2]:
            gain = [max(0, (n or 0) - (p or 0)) if n is not None and p is not None else 0 for n, p in zip(now, pre[1])]
            if any(gain) and max(gain) <= 20_000_000:  # sanity: no single attack brings more
                row = self.loot.setdefault(name, [0, 0, 0, 0])
                for i, g in enumerate(gain):
                    row[i] += g
                row[3] += 1
                self.emit("loot", {a: list(r) for a, r in self.loot.items()})
                self.log(f"{name}: +{gain[0]:,} gold, +{gain[1]:,} elixir, +{gain[2]:,} dark")
        self._cur = (name, now)
        return storage

    def attack_now(self):
        self._pre = self._cur  # storage right before this attack
        self.tap(self.hits["attack_button"])

    def on_home(self, frame, prev):
        self.game_relaunches = 0
        storage = self.track_loot(frame, self.read_storage(frame))
        if self.mode == "loot":  # Loot only: no upgrades, walls or account switching - just attack
            return self.attack_now()
        for kind in ("builder", "lab"):
            if self.cfg[f"{kind}_upgrades_enabled"] and time.time() >= self._upgrade_backoff.get(kind, 0):
                sv = self._saving_home if kind == "builder" else None
                have = {"gold": storage.get("gold"), "elixir": storage.get("elixir"), "dark": self._cur[1][2]}
                if sv and sv[0] == self._last_name and time.time() < sv[3] and have.get(sv[2]) is not None \
                        and have[sv[2]] < sv[1]:
                    continue  # still short of the plan's next target: no need to open the list
                if self.free_slots(frame, kind) == 0:
                    if kind == "builder":
                        self._saving_home = None  # every builder busy: nothing to save up for right now
                    if self.maybe_rescan(kind):  # all busy: still refresh the planner's view now and then
                        return
                    continue  # all busy: no need to open the list
                self.upgrade_from_list(kind)
                return  # screen changed; look again
        if self.cfg["bank_spend_enabled"] and self.spend_bank(storage):
            return  # screen changed while buying; look again
        if self.cfg["builder_base_enabled"] and (self._saving_home or (None,))[0] != self._last_name \
                and self.builder_base():
            return  # back home; look again (one side at a time: not while saving up for a home target)
        if self.account_done(frame) and self.switch_account():
            return
        self.attack_now()

    def on_menu(self, frame, prev):
        self.tap(self.hits["find_match_button"], self.cfg["matchmaking_settle_delay"])

    def on_army(self, frame, prev):
        self.tap(self.hits["confirm_attack_button"], self.cfg["matchmaking_settle_delay"])

    def on_bbend(self, frame, prev):
        self.tap(self.hits["bb_return_home"], 3.0)

    def on_bbhome(self, frame, prev):
        """On the Builder Base outside a visit (a restart, a crash, a stray boat tap): collect and sail home."""
        self.bb_bonus(self._last_name or "?")
        self.bb_collect()
        if not self.take_boat("home"):
            self.log("On the Builder Base and couldn't take the boat home - relaunching the game.", "warn")
            self.recover_game("stuck on the Builder Base")

    def on_end(self, frame, prev):
        self.tap(self.hits["return_home_button"], self.cfg["return_home_delay"])

    def on_battle(self, frame, prev):
        left = [c for c in troop_bar(frame) if c[2] in ("troop", "spell") and c[3]]
        if left:
            # A fresh base shows 'End Battle' a moment before 'Next' appears: give it a few seconds to be scouting.
            nxt = self.wait_for("next_button", 3, poll=0.5)
            if nxt:
                self.hits["next_button"] = nxt
                return self.on_scout(self.shot(), "BATTLE")
            # the scouting timer ran out before we deployed: the battle started with our army unused
            self.log(f"Battle running with {len(left)} unused troop/spell cards - deploying now.", "warn")
            return self.attack(None, None)
        self.log("Found a battle in progress - watching damage.")
        self.finish_battle()

    def on_scout(self, frame, prev):
        c = self.cfg
        if c["loot_force_attack"]:
            return self.attack(None, None)
        if prev != "SCOUT":  # loot numbers count up as the base loads
            self.sleep(c["loot_settle_delay"])
            frame = self.shot()
            if not (self.find(frame, "next_button") or self.hits.get("next_button")):
                return
        gold, elixir = self.read_loot(frame)
        if gold is None or elixir is None:  # one retry on a fresh frame
            self.sleep(0.8)
            gold, elixir = self.read_loot(self.shot())
        self.emit("base", (gold, elixir))
        if gold is None or elixir is None:
            self._unreadable += 1
            if self._unreadable >= 5:
                self.log("Can't read loot on 5 bases in a row - attacking anyway so Next doesn't burn "
                         "gold. Re-capture the loot regions in Setup.", "warn")
                self._unreadable = 0
                return self.attack(gold, elixir)
            self.log(f"Loot unreadable (gold={gold}, elixir={elixir}) - next base.", "warn")
        else:
            self._unreadable = 0
            g_ok, e_ok = gold >= c["loot_gold_threshold"], elixir >= c["loot_elixir_threshold"]
            if (g_ok and e_ok) if c["loot_require_both"] else (g_ok or e_ok):
                return self.attack(gold, elixir)
            self.log(f"Skip: gold {gold:,} / elixir {elixir:,}")
        hit = self.find(self.shot(), "next_button") or self.hits.get("next_button")
        if hit:
            self.bump("skipped")
            self.tap(hit, c["loot_recheck_delay"])

    # --- Groq (AI vision) ---
    def groq_on(self, key):
        return bool(self.cfg.get(key) and self.cfg.get("groq_api_key") and self.cfg.get("groq_model")
                    and time.time() >= self._groq_paused_until)

    def groq(self, frame, prompt, tokens=250):
        self._last_groq = time.time()
        _groq.pop("last_error", None)
        r = groq_ask(self.cfg, frame, prompt, tokens)
        if r is None and "429" in _groq.get("last_error", ""):
            self._groq_paused_until = time.time() + 900
            self.log("Groq's free daily limit is used up - pausing AI for 15 min (the bot keeps farming "
                     "without it).", "warn")
        elif r is None and "groq_fail" not in self._warned:
            self._warned.add("groq_fail")
            self.log("Groq didn't answer (key, model, internet or 'pip install groq'?) - using the "
                     "non-AI fallback. Details in bot.log.", "warn")
        return r

    def groq_rescue(self, frame):
        """Ask Groq what's on screen and act on it. Returns a known state (with its button in
        self.hits) for the normal handler to take over, or None after doing a safe action itself."""
        r = self.groq(frame, RESCUE_PROMPT)
        if not r:
            return None
        k = frame.shape[1] / GROQ_W
        screen, action = str(r.get("screen", "other")).lower(), str(r.get("action", "wait")).lower()
        target, reason = str(r.get("target") or ""), str(r.get("reason") or "")
        b = r.get("button")
        xy = (int(b[0] * k), int(b[1] * k)) if isinstance(b, list) and len(b) == 2 and all(
            isinstance(v, (int, float)) for v in b) else None
        if xy and not (0 <= xy[0] < frame.shape[1] and 0 <= xy[1] < frame.shape[0]):
            xy = None
        self.log(f"Groq: {screen} -> {action} {target!r} ({reason})")
        state = GROQ_SCREENS.get(screen)
        if state and xy:
            name = next(n for n, st in STATES if st == state)
            self.hits[name] = xy
            sc = self.v.score(frame, name)
            if sc and name not in self._warned:
                self._warned.add(name)
                self.log(f"'{name}' image only matched {sc[0]:.2f} here - Groq recognised the screen instead. "
                         f"Re-capture it in Setup if you see this often.", "warn")
            self.emit("state", STATE_LABELS[state] + " (AI)")
            return state
        if action == "tap" and xy:
            if any(w in (target + " " + reason).lower() for w in GROQ_BLOCKED):
                self.log(f"Refused Groq tap on {target!r} - could spend gems. Pressing Back instead.", "warn")
                self.adb.back()
            else:
                self.tap(xy, 1.5)
        elif action == "back":
            self.adb.back()
            self.sleep(1.0)
        elif action == "relaunch":
            self.recover_game(f"Groq: {reason or 'game looks broken'}")
        else:
            self.sleep(2.0)
        return None

    # --- reading numbers ---
    def read(self, frame, region, min_digits=None, thorough=True):
        box = self.cfg["ocr_regions"].get(region)
        if not box:
            return None
        md = self.cfg["ocr_min_digits"] if min_digits is None else min_digits
        v = ocr_number(frame, box, self.cfg["ocr_region_padding_px"], md, thorough)
        return None if v is not None and v > self.cfg["loot_max_plausible"] else v

    def read_pair(self, frame, prompt, groq_key, regions, thorough, cap=None):
        """Local white-text read first (free, ~0.4s); Groq only if that fails (it has a daily limit)."""
        cap = cap or self.cfg["loot_max_plausible"]
        ok = lambda v: v is not None and v <= cap
        boxes = [self.cfg["ocr_regions"].get(r) for r in regions]
        g, e = (white_number(frame, b) if b else None for b in boxes)
        if ok(g) and ok(e):
            return g, e
        if self.groq_on(groq_key):
            r = self.groq(frame, prompt, 60) or {}
            rg, re_ = as_int(r.get("gold")), as_int(r.get("elixir"))
            if ok(rg) and ok(re_):
                return rg, re_
        return tuple(v if ok(v) else self.read(frame, reg, thorough=thorough) for v, reg in zip((g, e), regions))

    def read_loot(self, frame):
        return self.read_pair(frame, LOOT_PROMPT, "use_groq_for_loot",
                              ("loot_gold_region", "loot_elixir_region"), True)

    def read_storage(self, frame):
        g, e = self.read_pair(frame, STORAGE_PROMPT, "use_groq_for_bank",
                              ("bank_gold_region", "bank_elixir_region"), False,
                              cap=99_999_999)  # storages hold far more than any base's loot
        s = {"gold": g, "elixir": e}
        if g is not None or e is not None:
            self.emit("storage", s)
        self.log(f"Storage: gold {g if g is None else f'{g:,}'} / elixir {e if e is None else f'{e:,}'}")
        return s

    # --- attacking ---
    def attack(self, gold, elixir):
        loot = f" - gold {gold:,} / elixir {elixir:,}" if gold is not None and elixir is not None else ""
        self.log(f"Attacking{loot}", "ok")
        self.bump("attacks")
        self.deploy()
        self.finish_battle()

    def deploy(self):
        c = self.cfg
        self._ability_cards = []
        if c.get("deploy_pan", "off") in PAN_DIRS:  # each account/village keeps its own zoom: zoom fully out
            zoom_out(self.adb)                    # first, so the pan lands on the same view every time
        pan_view(self.adb, c.get("deploy_pan", "off"))
        self.sleep(0.5)
        a, b = self.line()
        try:  # what the bot saw + where it will drop: send this file if placement looks wrong
            dbg = self.shot().copy()
            bar = troop_bar(dbg)
            fp = c["fixed_points"]
            cv2.line(dbg, tuple(a), tuple(b), (0, 210, 255), 6)
            if fp.get("spell_point"):
                cv2.line(dbg, tuple(fp["spell_point"]), tuple(fp.get("spell_line_end") or fp["spell_point"]),
                         (255, 80, 220), 6)
            for x, y, kind, n in bar:
                cv2.putText(dbg, f"{kind} {n or ''}", (x - 60, y - 90), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.imwrite(os.path.join(BASE_DIR, "debug_deploy.png"), dbg)
        except Exception as e:
            log_file.info(f"debug_deploy.png: {e}")
        if not self.auto_deploy(a, b):
            self.log("Couldn't read the troop bar - no troops deployed (see debug_deploy.png).", "err")

    def finish_battle(self):
        c = self.cfg
        start, best, streak = time.time(), 0, 0
        last_rise = last_read = start  # troops dead/stuck = damage stops rising while we can still read it
        abilities = list(getattr(self, "_ability_cards", [])) if c["hero_abilities"] else []
        while time.time() - start < c["battle_max_wait"]:
            if abilities and time.time() - start >= c["hero_ability_delay"]:
                for xy in abilities:  # a dead hero's greyed card just ignores the tap
                    self.adb.tap(*xy)
                    self.sleep(0.25)
                self.log(f"Used {len(abilities)} hero abilities.")
                abilities = []
            frame = self.shot()
            if self.pick_reward(frame):
                continue
            if self.find(frame, "return_home_button"):
                self.log("Battle ended on its own.")
                return
            box = c["ocr_regions"].get("damage_percent_region")
            dmg = (damage_percent(frame, box) if box else None)
            if dmg is None or dmg > 100:
                dmg = self.read(frame, "damage_percent_region", min_digits=1, thorough=False)
            if dmg is not None and dmg <= 100 and dmg >= best - 5:  # damage never goes down
                last_read = time.time()
                if dmg > best:
                    last_rise = last_read
                best = max(best, dmg)
                streak = streak + 1 if dmg >= c["surrender_damage_threshold"] else 0
                self.emit("state", f"In battle - {dmg}% damage")
                if streak >= c["damage_confirm_count"]:
                    self.log(f"{dmg}% damage reached - surrendering.", "ok")
                    break
            stalled = time.time() - last_rise
            limit = c["damage_stall_seconds"] if best > 0 else max(45, c["damage_stall_seconds"])  # troops still walking in
            if stalled > limit and time.time() - last_read < 3 * c["timer_poll_interval"] + 2:
                self.log(f"Damage stuck at {best}% for {stalled:.0f}s - troops are done, surrendering.", "ok")
                break
            self.sleep(c["timer_poll_interval"])
        else:
            self.log(f"{c['battle_max_wait']:.0f}s battle limit - surrendering.")
        self.pick_reward(self.shot())
        for name in ("surrender_button", "surrender_confirm_button"):
            hit = self.wait_for(name, 6) or c["coords"].get(name)
            if not hit:
                self.log(f"Couldn't find '{name}'.", "warn")
                return
            self.tap(hit, 0.8)

    def pick_reward(self, frame):
        """'Pick a Reward!' (shown mid-battle at star milestones) covers Surrender until a card is taken. Take the
        resource card (elixir / gold / dark elixir pile), else the middle one. True if it was showing."""
        if not self.v.find(frame, "pick_reward_title", 0.85):
            return False
        k = frame.shape[1] / 1920
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        best, pick = 1500 * k * k, (int(964 * k), int(560 * k))  # a troop card scores ~300 (its level badge)
        for x in (578, 964, 1350):
            c = hsv[int(440 * k):int(600 * k), int((x - 120) * k):int((x + 120) * k)]
            h, s, v = c[:, :, 0], c[:, :, 1], c[:, :, 2]
            n = (((h >= 135) & (h <= 165) & (s > 120) & (v > 150)).sum()  # elixir
                 + ((h >= 18) & (h <= 32) & (s > 150) & (v > 180)).sum()  # gold
                 + ((h >= 120) & (h <= 170) & (s > 80) & (v >= 40) & (v <= 120)).sum())  # dark elixir
            if n > best:
                best, pick = n, (int(x * k), int(560 * k))
        self.tap(pick, 1.0)
        self.log("Picked a battle reward.")
        return True

    # --- walls ---
    def spend_bank(self, storage):
        did = False
        for cur in ("gold", "elixir"):
            val = storage.get(cur)
            if val is None or val < self.cfg["bank_spend_threshold"]:
                continue
            if time.time() < self._bank_backoff.get(cur, 0):
                continue
            self.sleep(0.5)
            again = white_number(self.shot(), self.cfg["ocr_regions"].get(f"bank_{cur}_region") or (0, 0, 1, 1))
            if again != val:  # a one-off misread (e.g. 1.7M read as 17M) must never trigger a purchase
                self.log(f"{cur.title()} read {val:,} then {again} - not sure, skipping this time.", "warn")
                continue
            did = True
            if not self.buy_wall(cur, val):
                self._bank_backoff[cur] = time.time() + 300
                self.log(f"Couldn't buy a {cur} wall - trying again in 5 min.", "warn")
        return did

    def scroll_list(self):
        p = self.cfg["fixed_points"]
        s, e = p.get("list_scroll_start"), p.get("list_scroll_end")
        if not s or not e or abs(s[1] - e[1]) < 100:  # too short a drag registers as a tap
            x, y = s or (960, 540)
            s, e = (x, y + 200), (x, y - 200)
        self.adb.swipe(s[0], s[1], e[0], e[1], self.cfg["bank_scroll_duration_ms"])
        self.sleep(self.cfg["bank_scroll_delay"])

    def find_wall_rows(self, frame):
        """'Wall' rows in the builder list, via Tesseract word search (clean white-on-dark text)."""
        if not HAVE_TESS:
            return []
        h, w = frame.shape[:2]
        x0, y0 = w // 4, h // 12
        gray = cv2.cvtColor(frame[y0:h * 7 // 8, x0:w * 3 // 4], cv2.COLOR_BGR2GRAY)
        try:
            d = pytesseract.image_to_data(gray, config="--psm 11", output_type=pytesseract.Output.DICT, timeout=8)
        except Exception:
            return []
        return [(x0 + d["left"][i] + d["width"][i] // 2, y0 + d["top"][i] + d["height"][i] // 2)
                for i, t in enumerate(d["text"]) if t.strip().lower().startswith("wall")]

    def wall_fail(self, why, frame):
        cv2.imwrite(os.path.join(BASE_DIR, "debug_wall.png"), frame)
        self.log(f"Wall upgrade: {why}. Screen saved as debug_wall.png.", "warn")
        self.close_popups()
        return False

    def bar_price(self, frame, btn):
        """Price printed above an Upgrade button: white when affordable, red when not."""
        k = frame.shape[1] / 1920
        c = crop(frame, (btn[0] - int(100 * k), btn[1] - int(78 * k), btn[0] + int(60 * k), btn[1] - int(33 * k)), 0)
        if c is None:
            return None
        hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
        b, g, r = (c[:, :, i].astype(int) for i in range(3))
        white = (hsv[:, :, 2] > hsv[:, :, 2].max() * 0.8) & (hsv[:, :, 1] < 70)
        red = (r > 170) & (r - g > 70) & (r - b > 50)
        return read_digit_blobs(white | red)

    def upgrade_pair(self, frame):
        """The Upgrade More bar's two double-hammer Upgrade buttons as (gold, elixir) - gold is always the left one.
        Matched on the hammers only, so the price (1,500,000 / 5,000,000 ...) doesn't matter. None if not showing."""
        t = self.v.template("wall_upgrade_hammers")
        if t is None:
            return None
        small = cv2.resize(frame, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_AREA)
        res = cv2.matchTemplate(small, t[2], cv2.TM_CCOEFF_NORMED)
        th, tw = t[1]
        hits = []
        for _ in range(2):
            _, v, _, loc = cv2.minMaxLoc(res)
            if v < 0.8:
                break
            hits.append((int(loc[0] / SCALE + tw / 2), int(loc[1] / SCALE + th / 2)))
            x0, y0 = loc
            res[max(0, y0 - th // 4):y0 + th // 4, max(0, x0 - tw // 2):x0 + tw // 2] = -1  # next peak elsewhere
        if len(hits) != 2 or abs(hits[0][0] - hits[1][0]) < 80 * frame.shape[1] / 1920:
            return None
        return tuple(sorted(hits))

    def select_wall(self):
        """Open the builder list, tap a 'Wall' row once the list has stopped sliding, close the list. Returns
        the Upgrade More button if a wall really is selected (only walls have it), else None."""
        icon = top_bar(self.shot(), "builder")[0]
        frame = self.shot()
        rows = self.find_wall_rows(frame)
        if not rows:  # list not open yet
            self.tap(icon, 1.2)
            prev = None
            for _ in range(10):
                frame = self.shot()
                rows = self.find_wall_rows(frame)
                if rows:
                    break
                roi = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)[150:-150:4, ::4]
                if prev is not None and cv2.absdiff(roi, prev).mean() < 2:
                    break  # list stopped moving: reached the bottom
                prev = roi
                self.scroll_list()
        if not rows:
            return None
        for _ in range(6):  # wait until two reads agree on where the row is (the list glides after a swipe)
            self.sleep(0.4)
            again = self.find_wall_rows(self.shot())
            if again and abs(again[0][1] - rows[0][1]) <= 4:
                rows = again
                break
            rows = again or rows
        self.tap(rows[0], 1.2)
        f = self.shot()
        if panel_box(f):  # the list sometimes stays open over the buttons: its icon closes it
            self.tap(icon, 1.2)
        end = time.time() + 3  # only walls have 'Upgrade More' (greyed/red when it's the only wall of its level)
        while True:
            f = self.shot()
            if self.upgrade_pair(f):  # Upgrade More bar already open: its greyed 'Remove Wall' isn't Upgrade More
                return "open"
            hit = self.find(f, "upgrade_more_button") or self.find(f, "upgrade_more_disabled")
            if hit or time.time() > end:
                return hit
            self.sleep(0.5)

    def buy_wall(self, cur, balance=None):
        """Builder list -> 'Wall' row (selects a wall) -> close list -> Upgrade More -> Upgrade in `cur`
        -> Okay, but only if the dialog really says it upgrades Walls for `cur` (never gems)."""
        region = self.cfg["ocr_regions"].get(f"bank_{cur}_region")
        frame = self.shot()
        if balance is None and region:
            balance = white_number(frame, region)
        for attempt in range(3):  # a slid list can land the tap on the wrong building: just try again
            more = "open" if self.upgrade_pair(frame) else self.select_wall()
            if more:
                break
            self.log(f"Wall upgrade: didn't get a wall selected (attempt {attempt + 1}/3) - retrying.")
        else:
            return self.wall_fail("couldn't select a wall", self.shot())
        f = self.shot()
        lab = f[more[1] + 10:more[1] + 50, more[0] - 70:more[0] + 70].astype(int) if more != "open" else None
        disabled = lab is not None and ((lab[:, :, 2] > 170) & (lab[:, :, 2] - lab[:, :, 1] > 70) & (lab[:, :, 2] - lab[:, :, 0] > 50)).mean() > 0.08
        if disabled:
            # Only one wall of this level: 'Upgrade More' is greyed out (red text), so upgrade this single wall with
            # the bar's own gold (left) / elixir (right) Upgrade button, which sits a fixed distance to the right.
            k = f.shape[1] / 1920
            bx = int(more[0] + (211 if cur == "gold" else 425) * k)
            want = white_number(f, (bx - int(100 * k), more[1] - int(134 * k), bx + int(60 * k), more[1] - int(89 * k)))
            if not want:
                return self.wall_fail("single wall: couldn't read its price (can't afford it?)", f)
            if balance is not None and want > balance:
                return self.wall_fail(f"single wall costs {want:,} but storage is {balance:,} - not buying", f)
            self.log(f"Only one wall at this level - upgrading it on its own ({want:,} {cur}).")
            self.tap((bx, more[1] - int(40 * k)), 1.5)
        else:
            want = None
            if more != "open":
                self.tap(more, 1.2)
            pair, end = None, time.time() + 4
            while not pair and time.time() < end:
                pair = self.upgrade_pair(self.shot())
                if not pair:
                    self.sleep(0.4)
            if not pair:
                return self.wall_fail("Upgrade More bar's Upgrade buttons not found", self.shot())
            btn = pair[0] if cur == "gold" else pair[1]
            for _ in range(20):  # Upgrade More selects a whole row: drop walls (-1) until it's affordable
                f = self.shot()
                cost = self.bar_price(f, btn)
                if cost is None or balance is None or cost <= balance:
                    break
                k = f.shape[1] / 1920  # bar layout is fixed: Remove Wall, Add +10, Add +1, gold Upgrade, elixir Upgrade
                self.tap((pair[0][0] - int(612 * k), pair[0][1] + int(12 * k)), 0.7)
            self.tap(btn, 1.2)
        frame = self.shot()
        ok = self.find(frame, "wall_okay_button")
        if ok:  # 'Upgrade Walls' dialog: check it says Walls + this currency + a price, never gems
            h, w = frame.shape[:2]
            try:
                text = pytesseract.image_to_string(cv2.cvtColor(frame[h * 2 // 5:h * 11 // 20, w // 4:w * 3 // 4],
                                                                cv2.COLOR_BGR2GRAY), timeout=8).lower()
            except Exception:
                text = ""
            price = max((int(x) for x in re.findall(r"\d{4,}", text.replace(" ", "").replace(",", ""))), default=None)
            if "wall" not in text or cur not in text or "gem" in text or price is None:
                return self.wall_fail(f"upgrade dialog didn't check out ({text.strip()[:80]!r})", frame)
        else:  # single wall: the normal upgrade window - the green resource Confirm must show the same price
            ok = self.wait_for("confirm_wall_upgrade_button", 4)
            shown = white_number(self.shot(), (ok[0] - 160, ok[1] + 12, ok[0] + 130, ok[1] + 75)) if ok else None
            if not ok or not want or shown != want:
                return self.wall_fail(f"upgrade window didn't check out (price {shown} vs {want})", self.shot())
            price = want
        if balance is not None and price > balance:
            return self.wall_fail(f"costs {price:,} but storage is {balance:,} - not buying", frame)
        self.tap(ok, self.cfg["bank_post_spend_delay"])
        after = white_number(self.shot(), region) if region else None
        if balance is not None and after is not None and after > balance - price * 0.9:
            self.sleep(1.5)  # the counter animates down; look once more
            after = white_number(self.shot(), region)
        if balance is not None and after is not None and after > balance - price * 0.9:
            return self.wall_fail(f"{cur} didn't drop ({balance:,} -> {after:,}), so it didn't go through",
                                  self.shot())
        self.bump("walls")
        self.log(f"Bought a wall upgrade for {price:,} {cur}"
                 + (f" ({balance:,} -> {after:,})." if balance is not None and after is not None else "."), "ok")
        return True  # the wall bar left open doesn't block Attack

    # --- Builder Base ---
    def at_village(self, f):
        return self.find(f, "attack_button") or self.find(f, "bb_attack_button")

    def matches(self, frame, name, conf=0.8):
        """Every place a (small) template shows, e.g. all collector bubbles."""
        t = self.v.template(name)
        if t is None:
            return []
        small = cv2.resize(frame, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_AREA)
        r = cv2.matchTemplate(small, t[2], cv2.TM_CCOEFF_NORMED)
        h, w = t[1]
        out = []
        for y, x in sorted(zip(*np.where(r >= conf)), key=lambda p: -r[p]):
            xy = (int(x / SCALE + w / 2), int(y / SCALE + h / 2))
            if all(abs(xy[0] - a) > 30 or abs(xy[1] - b) > 30 for a, b in out):
                out.append(xy)
        return out

    def find_any_zoom(self, frame, name, conf=0.75):
        """Like find(), but also at the smaller/larger sizes things have when the camera is zoomed out/in
        (Clash remembers each village's zoom, so it differs between accounts and PCs)."""
        t = self.v.template(name)
        if t is None:
            return None
        small = cv2.resize(frame, None, fx=SCALE, fy=SCALE, interpolation=cv2.INTER_AREA)
        best = (0, None)
        for z in (1.0, 0.85, 0.72, 0.62, 1.15):
            tt = cv2.resize(t[2], None, fx=z, fy=z, interpolation=cv2.INTER_AREA)
            if tt.shape[0] > small.shape[0] or tt.shape[1] > small.shape[1] or min(tt.shape[:2]) < 6:
                continue
            _, v, _, loc = cv2.minMaxLoc(cv2.matchTemplate(small, tt, cv2.TM_CCOEFF_NORMED))
            if v > best[0]:
                best = (v, (int((loc[0] + tt.shape[1] / 2) / SCALE), int((loc[1] + tt.shape[0] / 2) / SCALE)))
        return best[1] if best[0] >= conf else None

    def take_boat(self, to):
        """Home <-> Builder Base: find the boat's sail bubble and tap it. True once the other village shows.
        Where the boat is depends on the village and its level (a big Builder Base has it bottom-right, by the
        Outpost), and the camera may be anywhere after an upgrade - so pan the usual way first, then sweep."""
        want = "bb_attack_button" if to == "builder" else "attack_button"
        L, R, U, D = (600, 500, 1300, 500), (1300, 500, 600, 500), (960, 300, 960, 700), (960, 700, 960, 300)
        usual = [(700, 450, 1300, 250)] * 3 if to == "builder" else [(1300, 300, 800, 600)] * 3
        sweep = usual + [L] * 3 + [U] * 2 + [R] * 5 + [D] * 4 + [L] * 5 + [U] * 2
        for _ in range(2):
            boat = self.find_any_zoom(self.shot(), "bb_boat")
            if not boat:  # zoomed in (e.g. after an upgrade on the far village): zoom out so the sweep covers the map
                zoom_out(self.adb)
                self.sleep(0.8)
                boat = self.find_any_zoom(self.shot(), "bb_boat")
            for mv in sweep:
                if boat:
                    break
                self.adb.swipe(*mv, 450)
                self.sleep(0.9)
                boat = self.find_any_zoom(self.shot(), "bb_boat")
            if boat:
                if to == "home":
                    self.bb_collect()
                    boat = self.find_any_zoom(self.shot(), "bb_boat") or boat
                self.tap(boat, 2.0)
                if self.wait_for(want, 25, poll=1.0):
                    self.sleep(1.0)
                    return True
            self.back_to_village()
        cv2.imwrite(os.path.join(BASE_DIR, "debug_boat.png"), self.shot())
        return False

    def bb_collect(self):
        """Tap every gold / elixir / gem bubble. One of the elixir bubbles is the Elixir Cart: its window gets Collect."""
        f = self.shot()
        H, W = f.shape[:2]
        # only bubbles in the open middle: one sitting over the edge buttons (Season Pass, Attack, Shop, the
        # resource bars...) would tap that button instead. It gets collected another time.
        safe = lambda p: 0.13 * W < p[0] < 0.87 * W and 0.11 * H < p[1] < 0.76 * H
        for xy in filter(safe, self.matches(f, "bb_elixir_bubble", 0.7) + self.matches(f, "bb_gold_bubble", 0.7)
                         + self.matches(f, "bb_gem_bubble", 0.7)):
            self.tap(xy, 1.0)
            f = self.shot()
            if self.find(f, "bb_cart_title"):
                btn = self.find(f, "bb_cart_collect")
                if btn:
                    self.tap(btn, 1.2)
                    self.log("Builder Base: collected the Elixir Cart.", "ok")
                self.adb.back()
                self.sleep(1.0)

    def bb_bonus(self, name):
        """Collect the 'Star Bonus!' popup if it's showing. True if one was collected just now."""
        f = self.shot()
        if not self.find(f, "bb_bonus_title"):
            return False
        ok = self.find(f, "bb_bonus_okay")
        if ok:
            self.tap(ok, 1.5)
        else:  # never a blind tap: Back closes the popup (the bonus is already credited)
            self.adb.back()
            self.sleep(1.5)
        self.log(f"Builder Base: Star Bonus collected ({name}).", "ok")
        return True

    def bb_clock_boost(self):
        """The Clock Tower's free boost (everything 10x faster for ~30 min, free once per 22h). Its bubble only
        shows when it's ready; only a button that says 'Free Boost!' is pressed - never the gem one."""
        bub = self.find(self.shot(), "bb_clock_bubble")
        if not bub:
            return
        self.tap(bub, 1.5)
        free = self.wait_for("bb_free_boost", 3)
        if free:
            self.tap(free, 1.5)
            if self.wait_for("bb_boost_title", 3):
                btn = self.find(self.shot(), "bb_boost_button")
                if btn:
                    self.tap(btn, 1.5)
                    self.log("Builder Base: Clock Tower free boost started (10x speed).", "ok")
        self.back_to_village()

    def bb_stars(self, f):
        """(earned, needed) of today's star bonus from the Builder Base Attack button, or None."""
        return read_counter(f, BB_STARS)

    def bb_attack(self):
        """One Builder Base battle: Attack -> Find Now -> drop every card on the open ground at the left ->
        Battle Machine ability -> wait for Return Home. Builder Base bases can't be skipped; loot isn't read."""
        for _ in range(2):  # the first tap only deselects a building that's still selected
            self.tap(self.find(self.shot(), "bb_attack_button") or (125, 955), 1.5)
            find = self.wait_for("bb_find_now", 4)
            if find:
                break
        if not find:
            return self.log("Builder Base: Find Now didn't show.", "warn")
        self.tap(find, 1.0)
        cards, end = [], time.time() + 40
        while not cards and time.time() < end:  # matchmaking, then the battle screen with the troop bar
            self.sleep(1.5)
            cards = [c for c in troop_bar(self.shot()) if c[2] in ("single", "troop")]
        if not cards:
            return self.log("Builder Base: no battle screen after Find Now.", "warn")
        self.bump("attacks")
        self.bb_deploy(cards)
        self.log(f"Builder Base: attacking with {len(cards)} cards.", "ok")
        end = time.time() + 420  # up to two stages of 3 min each
        while time.time() < end:  # the battle ends by itself once every troop is down (or the timer runs out)
            self.sleep(6)
            f = self.shot()
            done = self.find(f, "bb_return_home")
            if done:
                self.tap(done, 3.0)
                return self.wait_for("bb_attack_button", 15)
            # 100% on stage 1 starts stage 2 with extra cards (+ the survivors): drop everything not used up.
            # Tapping an already-deployed card is harmless (the Battle Machine's = its ability).
            self.bb_deploy([c for c in troop_bar(f) if c[2] in ("single", "troop")])
        self.log("Builder Base: battle didn't finish in time.", "warn")

    def bb_deploy(self, cards):
        a, b = (390, 380), (260, 600)  # left edge of the map: always outside the base, clear of the Boost buttons
        for k, (x, y, kind, n) in enumerate(cards):
            self.tap((x, y), self.cfg["deploy_select_delay"])
            for i in range(n or 1):
                self.adb.tap(*self.along(a, b, (k + i) % len(cards), len(cards)))
            self.sleep(0.2)

    def builder_base(self):
        """Once per account every few hours: boat over, collect (incl. the Elixir Cart), keep the builders and
        Star Lab busy, attack until today's star bonus is complete, collect again, boat back."""
        name = self._last_name
        for _ in range(3):  # just after arriving / switching, popups and the XP bar can hide the name for a moment
            if name and name != "?":
                break
            self.sleep(1.0)
            name = self.account_name(self.shot())
        if time.time() < self._bb_next.get(name, 0):
            return False
        # unreadable name: visit anyway, but only block re-visits briefly (it may be a different account next time)
        self._bb_next[name] = time.time() + (600 if name == "?" else 3600 * self.cfg["bb_visit_hours"])
        self.log(f"Builder Base visit ({name}).", "ok")
        if not self.take_boat("builder"):
            self._bb_next[name] = time.time() + 1800
            self.log("Builder Base: couldn't take the boat - trying again in 30 min (screen: debug_boat.png).", "warn")
            return True
        self.emit("state", f"Builder Base ({name})")
        self.bb_collect()
        self.bb_clock_boost()  # first, while the arrival view shows the whole base (its bubble is on the tower)
        c = self.cfg
        for rnd in range(2):  # upgrades before AND after the attacks (the loot may pay for more)
            for kind in ("bb_builder", "bb_lab"):
                if c["bb_builder_upgrades" if kind == "bb_builder" else "bb_lab_upgrades"]:
                    for _ in range(3):
                        if not self.free_slots(self.shot(), kind):
                            self.maybe_rescan(kind)
                            break
                        n = self.stats["upgrades"]
                        self.upgrade_from_list(kind)
                        if self.stats["upgrades"] == n:
                            break
            if rnd or not c["bb_attacks_enabled"]:
                break
            last = None
            # Star Bonus: a popup each time the counter fills, several times a day. Once they're used up the counter
            # just rolls over with no popup - then this account is done until tomorrow.
            for _ in range(12):
                if self.cfg["bb_bonus_days"].get(name) == time.strftime("%Y-%m-%d"):
                    self.log("Builder Base: no Star Bonus left today - no attacks.")
                    break
                if self.bb_bonus(name):
                    last = None  # the counter restarts after a bonus: that's not a 'rolled over without one'
                f = self.shot()
                if not self.find(f, "bb_attack_button"):  # still in a battle / its end screen: never press Back here
                    done = self.wait_for("bb_return_home", 240, poll=3)
                    if done:
                        self.tap(done, 3.0)
                    if not self.wait_for("bb_attack_button", 15):
                        self.log("Builder Base: not back on the village - stopping the attacks.", "warn")
                        break
                    f = self.shot()
                st = self.bb_stars(f)
                if not st:  # the counter disappears from the Attack button once today's bonuses are all used
                    if name != "?":
                        self.cfg["bb_bonus_days"][name] = time.strftime("%Y-%m-%d")
                        save_config(self.cfg)
                    self.log("Builder Base: all of today's Star Bonuses collected - done until tomorrow.", "ok")
                    break
                if last and st[0] < last[0]:  # rolled over with no popup: today's bonus was already taken
                    if name != "?":
                        self.cfg["bb_bonus_days"][name] = time.strftime("%Y-%m-%d")
                        save_config(self.cfg)
                    self.log("Builder Base: star counter rolled over without a bonus - done for today.", "ok")
                    break
                last = st
                self.log(f"Builder Base: stars {st[0]}/{st[1]} - attacking.")
                self.bb_attack()
            self._upgrade_backoff.pop("bb_builder", None)
            self._upgrade_backoff.pop("bb_lab", None)
        self.take_boat("home")  # the boat view also shows the Elixir Cart: collect it (new defense rewards) first
        return True

    def reload_if_disconnected(self, frame):
        """'Anyone there? You have been disconnected due to inactivity' -> RELOAD GAME, then wait for it to load."""
        hit = self.find(frame, "reload_game")
        if not hit:
            return False
        self.log("Disconnected for inactivity - reloading the game.", "warn")
        self.tap(hit, 2.0)
        self._launched_at = time.time()  # loading screens next: don't count them as stuck
        for _ in range(30):
            if self.at_village(self.shot()):
                break
            self.sleep(2.0)
        return True

    def storage_total(self, frame):
        """Gold + elixir + dark elixir, for checking an upgrade really took the resources."""
        regions = {"bank_dark_region": (1560, 225, 1830, 300), **self.cfg["ocr_regions"]}  # default: 1920x1080 bar
        vals = [white_number(frame, regions[r]) for r in
                ("bank_gold_region", "bank_elixir_region", "bank_dark_region") if r in regions]
        return None if not vals or None in vals else vals

    def back_to_village(self, icon=None):
        """Close windows/lists/dialogs until the plain village shows. Back closes game windows and
        cancels dialogs; a dropdown list is closed by tapping its icon."""
        for _ in range(6):
            f = self.shot()
            if self.reload_if_disconnected(f):
                continue
            if (self.find(f, "wall_okay_button") or self.find(f, "lab_picker_title")
                    or not self.at_village(f)):  # dialogs / game windows (they hide Attack)
                self.adb.back()
            elif panel_box(f) and icon:  # a dropdown list leaves Attack visible; its icon closes it
                self.adb.tap(*icon)
            else:
                return
            self.sleep(0.9)

    def scroll_panel(self, box):
        x0, x1, y0, y1 = box
        cx = (x0 + x1) // 2
        self.adb.swipe(cx, y1 - 90, cx, max(y0 + 90, y1 - 90 - 380), self.cfg["bank_scroll_duration_ms"])
        self.sleep(self.cfg["bank_scroll_delay"])

    def upgrade_from_list(self, kind):
        """Open the builder or lab list, read every row, start the MOST EXPENSIVE upgrade you can afford
        (white price; walls are left to the wall routine), and confirm with the green resource button only.
        Busy builders/lab just produce a gem prompt, which is backed out of - so the attempt is the check."""
        frame = self.shot()
        icon = top_bar(frame, kind)[0]
        before = self.storage_total(frame)
        free_before = self.free_slots(frame, kind)
        if not panel_box(frame):
            self.tap(icon, 1.3)
        self.sleep(0.5)  # let the list finish sliding open before reading its 'Available!' rows
        normal, goblin = available_slots(self.shot())
        # Only refuse when the top bar agrees (goblin face / 0 free): one read of a still-moving list isn't enough.
        if kind.startswith("bb_") and free_before:
            normal = free_before  # the Builder Base list has no 'Available!' rows: trust the top bar counter
        if normal == 0 and not free_before:  # nothing free, or only the goblin (it costs gems) - never use it
            self._busy_until[kind] = self._upgrade_backoff[kind] = time.time() + 1800
            self.back_to_village(icon)
            return self.log(f"{KIND_NAMES[kind]}: " + ("only the goblin is free (costs gems) - not using it"
                                                   if goblin else "no free slot") + ". Checking again in 30 min.")
        if kind == "builder":
            self._saving_home = None
        seen = self.read_list()  # the whole list first: the discount and levels are worked out from all of it
        plan, levels = self.plan_positions(kind, seen)
        if plan:  # this account has a plan: stick to it - the next target in order, or wait (keep farming) for it
            i = min(plan.values())
            rows = [(nm, p, ok) for nm, p, ok in seen if plan.get((nm, p)) == i]
            buy = [r for r in rows if r[2]]
            if not buy:
                it = self.plan_items(kind)[i]
                self._upgrade_backoff[kind] = time.time() + 900
                if kind == "builder":  # farm home until it's bought - the Builder Base waits, the list stays shut
                    self._saving_home = (self._last_name, min(p for _, p, _ in rows),
                                         wiki_data()[it["key"]]["levels"][0]["res"], time.time() + 3600)
                self.back_to_village(icon)
                return self.log(f"{KIND_NAMES[kind]}: next in the plan is {wiki_data()[it['key']]['name']} -> "
                                f"{it['to']} ({min(p for _, p, _ in rows):,}) - farming until it's affordable"
                                + (" (Builder Base visits wait till then)." if kind == "builder" else "."))
        best = None
        for nm, price, ok in seen:
            skip = is_wall(nm) or (  # walls: the wall routine; TH: optional no-rush
                self.cfg.get("skip_town_hall", True) and re.search(r"t[o0]wn\s*ha", nm.lower()))
            if skip or not ok or (plan and plan.get((nm, price)) != min(plan.values())):
                continue
            bb = next((i for i, r in enumerate(BB_DEFAULT_ROWS) if name_match(nm, r["name"])), 99) \
                if kind == "bb_builder" else 99
            rank = (plan.get((nm, price), 10 ** 6), bb, -price)  # 1) your plan 2) 6th builder first 3) priciest
            if not best or rank < best[3]:
                best = (nm, price, 0, rank)
        if not best:
            self._upgrade_backoff[kind] = time.time() + 1200
            self.back_to_village(icon)
            return self.log(f"{KIND_NAMES[kind]}: nothing affordable right now - checking again in 20 min.")
        nm, price = best[:2]
        r = best[3][1:]
        lv = levels.get((nm, price))
        self.log(f"{KIND_NAMES[kind]}: choosing '{nm}'" + (f" (level {lv} -> {lv + 1})" if lv is not None else "")
                 + f" for {price:,} - "
                 + (f"#{best[3][0] + 1} in your upgrade plan." if best[3][0] < 10 ** 6
                    else "Builder Base: Builder Hall / 6th builder (O.T.T.O's Outpost) first." if r[0] < 99
                    else "the most expensive one you can afford."))
        self._started = (kind, nm, price, lv)
        self.tap(icon, 1.0)  # close + reopen = back at the top, then page down until it's on screen
        self.tap(icon, 1.3)  # (scrolling never lands in the same place twice, so search, don't count)
        match, prev = [], None
        for _ in range(8):
            frame = self.shot()
            rows = list_rows(frame)
            # name AND price: two rows can cost the same (Builder Base Cannon / Elixir Storage: 2,500,000)
            match = [r for r in rows if r[1] == price and r[2] and name_match(r[0], nm)]
            box = panel_box(frame)
            if match or not box or rows == prev:
                break
            prev = rows
            self.scroll_panel(box)
        if not match:
            self._upgrade_backoff[kind] = time.time() + 600
            self.back_to_village(icon)
            return self.log(f"{KIND_NAMES[kind]}: lost '{nm}' after re-opening the list - will retry.", "warn")
        self.tap(match[0][3], 1.6)
        if self.find(self.shot(), "guardians_title"):
            f, k = self.shot(), self.shot().shape[1] / 1920
            btn = next(((int(cx * k), int(823 * k)) for cx in (432, 960, 1490)
                        if white_number(f, (int((cx - 130) * k), int(818 * k), int((cx + 95) * k), int(862 * k))) == price),
                       None)
            if not btn:
                self._upgrade_backoff[kind] = time.time() + 1800
                self.back_to_village(icon)
                return self.log(f"{KIND_NAMES[kind]}: no Guardian shows {price:,} - backed out.", "warn")
            self.tap(btn, 1.6)
        hit, shown = self.resource_confirm(kind)  # heroes/research: the window opens directly
        if not hit:  # a building: the row only selected it - close the list, then its own Upgrade button
            if panel_box(self.shot()):
                self.tap(icon, 1.2)
            got, glv = self.selected_label(self.shot())  # is the selected building really the one chosen?
            want = re.sub(r"(?i)\s*x\s*\d+\s*$", "", nm)
            if got and not name_match(got, want):
                self._upgrade_backoff[kind] = time.time() + 600
                self.back_to_village(icon)
                return self.log(f"{KIND_NAMES[kind]}: the list selected '{got}' (level {glv}), not '{want}' - "
                                "backed out.", "warn")
            up = self.wait_for("building_upgrade_button", 4)
            if up:
                self.tap(up, 1.6)
            end = time.time() + 4
            while not hit and time.time() < end:
                hit, shown = self.resource_confirm(kind)  # the GREEN resource Confirm only
        if not hit:
            self._upgrade_backoff[kind] = time.time() + 600
            self.back_to_village(icon)
            return self.log(f"{KIND_NAMES[kind]}: no green Confirm for '{nm}' - backed out.", "warn")
        if shown != price:
            self._upgrade_backoff[kind] = time.time() + 600
            self.back_to_village(icon)
            return self.log(f"{KIND_NAMES[kind]}: Confirm shows {shown}, list said {price:,} - not risking it.", "warn")
        self.tap(hit, 2.0)
        self.back_to_village(icon)  # also cancels any 'all builders busy - use gems?' prompt
        for _ in range(3):  # the counters can lag (or a selected building's bar hides them) for a moment
            frame = self.shot()
            after, free_after = self.storage_total(frame), self.free_slots(frame, kind)
            spent = (before and after and any(b - a >= price * 0.9 for b, a in zip(before, after))) or (
                free_before is not None and free_after is not None and free_after < free_before)
            if spent:
                break
            self.sleep(1.5)
        if spent:
            self.bump("upgrades")
            self.log(f"{'Research' if kind.endswith('lab') else 'Upgrade'} started"
                     f"{' (Builder Base)' if kind.startswith('bb_') else ''}: {nm} for {price:,}.", "ok")
            self._upgrade_backoff[kind] = 0  # another builder may be free - check next time home
            self.plan_progress(*self._started)
        else:
            self._upgrade_backoff[kind] = time.time() + 1800
            self.log(f"{KIND_NAMES[kind]}: '{nm}' didn't start (all {'builders' if kind == 'builder' else 'lab slots'} "
                     f"busy?) - checking again in 30 min.")

    # --- upgrade planner: per-account scans + targets ---
    def read_list(self):
        """Every row of the open builder / lab list, page by page: [(name, price, affordable)]."""
        seen, pages = {}, []
        for _ in range(8):
            frame = self.shot()
            box = panel_box(frame)
            if not box:
                break
            rows = list_rows(frame)
            for nm, price, ok, xy in rows:
                seen.setdefault((nm, price), ok)
            sig = [(nm, p) for nm, p, _, _ in rows]
            if pages and sig == pages[-1]:
                break  # didn't move: bottom of the list
            pages.append(sig)
            self.scroll_panel(box)
        return [(nm, p, ok) for (nm, p), ok in seen.items()]

    def plan_items(self, kind):
        if kind.endswith("lab"):
            return []
        base = "builder" if kind.startswith("bb_") else "home"
        return ((self.cfg.get("plans") or {}).get(self._last_name or "?") or {}).get(base) or []

    def plan_positions(self, kind, seen):
        """Record this account's scan, then ({(name, price): position in its plan}, {(name, price): level})."""
        if kind.endswith("lab"):
            return {}, {}
        base = "builder" if kind.startswith("bb_") else "home"
        sd = ((self.cfg.get("scans") or {}).get(self._last_name) or {}).get(base) or {}
        if sd.get("source") == "export":  # exact levels already known: the list only gives names + prices
            f = infer_discount([(n, p) for n, p, _ in seen], base)
            rows = [{"name": nm, "price": p, "key": wiki_key(nm, base)} for nm, p, _ in seen]
            for r in rows:
                r["level"] = level_for_price(r["key"], r["price"], f) if r["key"] else None
            exact = sd["items"]
        else:
            rows = exact = self.record_scan(base, seen)["items"]
        levels = {(r["name"], r["price"]): r["level"] for r in rows}
        pos = {}
        for i, it in enumerate(self.plan_items(kind)):
            if not any(r["key"] == it["key"] and not r.get("upgrading") and (r["level"] is None or r["level"] < it["to"])
                       for r in exact):
                continue  # every copy is there already (or upgrading to it)
            for r in rows:
                if r["key"] == it["key"] and (r["level"] is None or r["level"] < it["to"]):
                    pos.setdefault((r["name"], r["price"]), i)
        return pos, levels

    def record_scan(self, base, seen):
        """What this account can still upgrade, with levels worked out from the prices (discounts included)."""
        f = infer_discount([(n, p) for n, p, _ in seen], base)
        items, dup = [], {}
        for nm, p, ok in seen:
            k = wiki_key(nm, base)
            if is_wall(nm) or "supercharge" in nm.lower() or (k is None and norm_name(nm) not in NO_WIKI):
                continue  # walls, Supercharges and event items (Cake-A-Pult...) aren't planned
            r = {"name": nm, "price": p, "ok": ok, "key": k, "count": count_of(nm),
                 "level": level_for_price(k, p, f) if k else None}
            same = dup.get((k or norm_name(nm), p))  # the same row read twice across pages ('Storaqe' / 'SGoraqe')
            if same:
                same["count"] = max(same["count"], r["count"])
                continue
            dup[(k or norm_name(nm), p)] = r
            items.append(r)
        hall = next((r["level"] for r in items if r["key"] in ("home:town hall", "builder:builder hall")
                     and r["level"] is not None), None)
        if hall is None:  # no hall row (maxed / upgrading): the list only offers what this hall allows
            reqs = [wiki_data()[r["key"]]["levels"][r["level"]]["req"] for r in items
                    if r["key"] and r["level"] is not None and r["level"] < len(wiki_data()[r["key"]]["levels"])
                    and wiki_data()[r["key"]]["levels"][-1].get("hall") in ("town hall", "builder hall")
                    and wiki_data()[r["key"]]["levels"][0]["level"] == 1]
            hall = max(reqs) if reqs else None
        hero = next((r["level"] for r in items if r["key"] == "home:hero hall" and r["level"] is not None), None)
        acc = self._last_name
        if not acc or acc == "?":  # never file a scan under an unknown account - it'd mix accounts up
            self.log("Upgrade planner: couldn't read the account name - scan not saved.", "warn")
            return {"items": items, "discount": f, "hall": hall}
        scans = self.cfg.setdefault("scans", {}).setdefault(acc, {})
        old = scans.get(base) or {}
        has_row = any(r["key"] in ("home:town hall", "builder:builder hall") for r in items)
        if not has_row:  # estimated: a rushed base's pending upgrades need less than its real hall - and a hall
            hall = max([h for h in (hall, old.get("hall")) if h] or [None])  # never goes down; manual stays
        hero = hero if hero is not None else old.get("hero_hall")
        scan = {"time": time.strftime("%Y-%m-%d %H:%M"), "discount": f, "items": items, "hall": hall,
                "hero_hall": hero}
        scans[base] = scan
        if f < 1:
            self.log(f"Prices are {round((1 - f) * 100)}% off right now (Hammer Jam / Gold Pass) - levels "
                     "worked out with that.")
        self.prune_plan(acc, base)
        save_config(self.cfg)
        self.emit("scan", acc)
        return scan

    def prune_plan(self, acc, base):
        """Drop plan targets this account has reached (every copy seen on the list is at or past the target)."""
        plans = (self.cfg.get("plans") or {}).get(acc) or {}
        items = ((self.cfg.get("scans") or {}).get(acc) or {}).get(base, {}).get("items", [])
        keep = []
        for it in plans.get(base) or []:
            mine = [r for r in items if r["key"] == it["key"]]
            if mine and all(r["level"] is not None and r["level"] >= it["to"] for r in mine):
                self.log(f"Upgrade plan: {wiki_data()[it['key']]['name']} -> level {it['to']} done "
                         f"({acc}).", "ok")
                continue
            keep.append(it)
        if plans.get(base) is not None and len(keep) != len(plans[base]):
            plans[base] = keep

    def plan_progress(self, kind, nm, price, lv):
        """An upgrade just started: one copy of that row moves up a level; targets reached leave the plan."""
        if kind.endswith("lab"):
            return
        base = "builder" if kind.startswith("bb_") else "home"
        acc = self._last_name or "?"
        scan = ((self.cfg.get("scans") or {}).get(acc) or {}).get(base)
        if not scan:
            return
        if scan.get("source") == "export":  # exact rows: the lowest copy of that building (at lv if known)
            k = wiki_key(nm, base)
            mine = sorted((r for r in scan["items"] if r["key"] == k and not r.get("upgrading")),
                          key=lambda r: (lv is not None and r["level"] != lv, r["level"]))
        else:
            mine = [r for r in scan["items"] if r.get("name") == nm and r.get("price") == price]
        for r in mine[:1]:
            r["count"] -= 1
            lv = r["level"] if lv is None and scan.get("source") == "export" else lv
            if lv is not None:
                scan["items"].append({"name": r["name"] if scan.get("source") == "export" else nm + " (upgrading)",
                                      "price": 0, "ok": False, "key": r["key"], "count": 1, "level": lv + 1,
                                      "upgrading": True})
        scan["items"] = [r for r in scan["items"] if r["count"] > 0]
        self.prune_plan(acc, base)
        save_config(self.cfg)
        self.emit("scan", acc)

    def level_label(self, frame):
        """'Cannon (Level 21)' shown under a selected building -> 21, else None."""
        return self.selected_label(frame)[1]

    def selected_label(self, frame):
        """'Cannon (Level 21)' shown under a selected building -> ('Cannon', 21); (None, None) if unreadable."""
        k = frame.shape[1] / 1920
        c = frame[int(640 * k):int(790 * k), int(360 * k):int(1560 * k)]
        hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
        m = cv2.resize(((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 80)).astype(np.uint8) * 255, None, fx=1.5, fy=1.5)
        try:
            t = pytesseract.image_to_string(cv2.copyMakeBorder(255 - m, 20, 20, 20, 20, cv2.BORDER_CONSTANT,
                                                               value=255), config="--psm 6", timeout=6)
        except Exception:
            return None, None
        hit = re.search(r"(?i)([A-Za-z0-9.'& -]*?)\s*\(?\s*l[e3]v[e3]l\s*([0-9]{1,3})", t)
        return (hit.group(1).strip() or None, int(hit.group(2))) if hit else (None, None)

    def resolve_levels(self, kind, base, limit=10):
        """Rows the price didn't settle (duplicate prices, OCR slips, no wiki data): tap each one in the list and
        read 'Name (Level N)' under the building. Saves the scan."""
        acc = self._last_name
        scan = ((self.cfg.get("scans") or {}).get(acc) or {}).get(base)
        todo = [r for r in (scan or {}).get("items", []) if r.get("level") is None and not r.get("upgrading")]
        if not todo:
            return
        icon = top_bar(self.shot(), kind)[0]
        for r in todo[:limit]:
            self.back_to_village(icon)
            self.tap(icon, 1.3)
            hit = None
            for _ in range(8):  # page down until the row shows
                f = self.shot()
                box = panel_box(f)
                if not box:
                    break
                hit = next((xy for nm, p, ok, xy in list_rows(f) if p == r["price"] and name_match(nm, r["name"])),
                           None)
                if hit:
                    break
                self.scroll_panel(box)
            if not hit:
                continue
            self.tap(hit, 1.4)
            if panel_box(self.shot()):
                self.tap(icon, 1.0)
            lv = None
            for _ in range(3):
                lv = self.level_label(self.shot())
                if lv is not None:
                    break
                self.sleep(0.6)
            if lv is not None:
                r["level"] = lv
                self.log(f"Upgrade planner: {r['name']} is level {lv} (read from the game).")
        self.back_to_village(icon)
        self.prune_plan(acc, base)
        save_config(self.cfg)
        self.emit("scan", acc)

    def export_village(self):
        """Settings > More Settings > Data Export 'Copy': the game's own JSON with every level, both villages.
        Read back from the Windows clipboard. None if any step didn't work (then the list is read instead)."""
        self.back_to_village()
        f = self.shot()
        k = f.shape[1] / 1920
        self.tap(self.find(f, "settings_cog_button") or (int(1842 * k), int(765 * k)), 1.5)
        # strict matches only: a looser one takes look-alike green buttons (Credits, Change Name...)
        more = None
        for _ in range(6):
            more = self.v.find(self.shot(), "more_settings_button", 0.9)
            if more:
                break
            self.sleep(0.8)
        data = None
        if more:
            self.tap(more, 2.0)  # let the window finish opening before anything touches it
            hit = None
            for _ in range(10):  # the page opens wherever it was last scrolled to (the top after a game start)
                f = self.shot()
                hit = self.v.find(f, "export_copy_row", 0.9)  # the Change Name row scores ~0.75
                y = None if hit else settings_drag_start(f)
                if y is None:
                    break
                # SLOW drag from a grey section header, never from a row: a touch on a row that doesn't turn
                # into a scroll presses it (flips a setting, opens Change Name...)
                self.adb.swipe(int(500 * k), y, int(500 * k), max(int(20 * k), y - int(400 * k)), 1200)
                self.sleep(1.2)
            if hit:
                self.tap((hit[0] + int(398 * k), hit[1]), 1.0)  # the green Copy button at the row's right
                for _ in range(5):
                    try:
                        d = json.loads(read_clipboard())
                        if d.get("tag") and d.get("buildings") and abs(time.time() - d.get("timestamp", 0)) < 900:
                            data = d
                            break
                    except (ValueError, AttributeError):
                        pass
                    self.sleep(1.0)
        self.back_to_village()
        return data

    def scan_export(self):
        """Fill this account's planner scans (home + Builder Base) from the data export. True if it worked.
        The export carries the player tag, so an account whose name OCR slips ('GeoCo!3') still files right."""
        data = self.export_village()
        if not data:
            self.log("Upgrade planner: the game's data export didn't come through - reading the builder list "
                     "instead.", "warn")
            return False
        tags = self.cfg.setdefault("account_tags", {})
        acc = tags.get(data["tag"]) or (self._last_name if self._last_name not in (None, "?") else None)
        if not acc:
            return False
        tags[data["tag"]] = self._last_name = acc
        scans = self.cfg.setdefault("scans", {}).setdefault(acc, {})
        for base, items in export_items(data).items():
            if not items:
                continue
            hall = next((r["level"] - r["upgrading"] for r in items
                         if r["key"] in ("home:town hall", "builder:builder hall")), None)
            hero = next((r["level"] - r["upgrading"] for r in items if r["key"] == "home:hero hall"), None)
            scans[base] = {"time": time.strftime("%Y-%m-%d %H:%M"), "discount": 1.0, "items": items, "hall": hall,
                           "hero_hall": hero, "source": "export"}
            self.prune_plan(acc, base)
        save_config(self.cfg)
        self.emit("scan", acc)
        self.log(f"Upgrade planner: read {acc}'s exact levels from the game's data export.", "ok")
        return True

    def maybe_rescan(self, kind, hours=6):
        """Builders all busy: read the list anyway if this account's planner scan is missing or old. True if it did."""
        if kind.endswith("lab") or not self._last_name or self._last_name == "?":
            return False
        base = "builder" if kind.startswith("bb_") else "home"
        sd = ((self.cfg.get("scans") or {}).get(self._last_name) or {}).get(base) or {}
        try:
            age = time.time() - time.mktime(time.strptime(sd["time"], "%Y-%m-%d %H:%M"))
        except (KeyError, ValueError):
            age = 10 ** 9
        if age < hours * 3600:
            return False
        if self.scan_export():
            return True
        icon = top_bar(self.shot(), kind)[0]
        self.tap(icon, 1.3)
        self.sleep(0.5)
        seen = self.read_list()
        self.tap(icon, 1.0)
        self.back_to_village(icon)
        if seen:
            self.record_scan(base, seen)
            self.resolve_levels(kind, base)
            self.log(f"Upgrade planner: refreshed {self._last_name}'s {'Builder Base' if base == 'builder' else 'home'}"
                     f" scan ({len(seen)} upgrades left).")
        return True

    def scan_now(self):
        """Planner's 'Scan': open this account's builder list (home or Builder Base, whichever is showing), read it."""
        self.back_to_village()
        f = self.shot()
        if not self.at_village(f):
            raise RuntimeError("the game isn't on the home village or Builder Base")
        kind = "bb_builder" if self.find(f, "bb_attack_button") else "builder"
        self._last_name = None
        for _ in range(4):  # a popup / the XP bar animating can hide the name for a moment
            if self.account_name(f) != "?":
                break
            self.sleep(1.0)
            f = self.shot()
        if self.scan_export():  # exact levels of both villages, straight from the game
            return self._last_name, kind
        if not self._last_name or self._last_name == "?":
            raise RuntimeError("couldn't read the account name at the top-left - close any popup and try again")
        icon = top_bar(f, kind)[0]
        if not panel_box(f):
            self.tap(icon, 1.3)
        self.sleep(0.5)
        seen = self.read_list()
        self.tap(icon, 1.0)
        self.back_to_village(icon)
        if not seen:
            raise RuntimeError("the builder list didn't open")
        base = "builder" if kind.startswith("bb_") else "home"
        self.record_scan(base, seen)
        self.resolve_levels(kind, base)
        return self._last_name, kind

    def resource_confirm(self, kind):
        """(button, price shown on it) of the upgrade window's green resource button, else (None, None).
        Home: the captured Confirm image. Builder Base (purple elixir icon, so the image doesn't match): the big
        green button in the bottom part of the screen. Either way the caller only taps it if the price equals the
        list price, so a gem button can never be pressed."""
        f = self.shot()
        if not kind.startswith("bb_"):
            hit = self.find(f, "confirm_wall_upgrade_button")
            return (hit, confirm_price(f, hit)) if hit else (None, None)
        H = f.shape[0]
        hsv = cv2.cvtColor(f[int(H * 0.6):], cv2.COLOR_BGR2HSV)
        m = ((hsv[:, :, 0] >= 35) & (hsv[:, :, 0] <= 75) & (hsv[:, :, 1] > 150) & (hsv[:, :, 2] > 150)).astype(np.uint8)
        n, _, st, _ = cv2.connectedComponentsWithStats(m, 8)
        big = [i for i in range(1, n) if st[i, 2] > f.shape[1] * 0.12 and st[i, 3] > H * 0.08]
        if not big:
            return None, None
        x, y, w, h = st[max(big, key=lambda i: st[i, 4]), :4]
        y += int(H * 0.6)
        # from the very edge: a 7-digit price starts right at it (x + 10 cut '4 400 000' to '400 000')
        return (x + w // 2, y + h // 2), white_number(f, (x, y + 10, x + int(w * 0.72), y + h - 10))

    def free_slots(self, frame, kind):
        """Normal builders / lab slots free. A goblin icon means only the (gem-costing) goblin is free = 0."""
        if goblin_icon(frame, kind):
            return 0
        c = read_counter(frame, top_bar(frame, kind)[1])
        return c[0] if c else None

    def account_done(self, frame):
        """True when rotation is on and every enabled builder/lab slot is busy on this account."""
        c = self.cfg
        kinds = [k for k in ("builder", "lab") if c[f"{k}_upgrades_enabled"]]
        if not c["rotate_accounts"] or not kinds or time.time() < self._rotate_pause_until:
            return False
        if any(self.free_slots(frame, k) != 0 and time.time() >= self._busy_until.get(k, 0) for k in kinds):
            self._switch_streak = 0  # this account still has work: stay and farm
            return False
        return True

    def list_accounts(self, frame):
        """Account names on the Supercell ID panel: the bold name that has a 'Town Hall' line under it."""
        x0 = frame.shape[1] * 2 // 3
        try:
            d = pytesseract.image_to_data(cv2.cvtColor(frame[:, x0:], cv2.COLOR_BGR2GRAY), config="--psm 11",
                                          output_type=pytesseract.Output.DICT, timeout=10)
        except Exception:
            return []
        words = [(t.strip(), x0 + d["left"][i] + d["width"][i] // 2, d["top"][i] + d["height"][i] // 2)
                 for i, t in enumerate(d["text"]) if t.strip()]
        towns = [(x, y) for t, x, y in words if t.lower() == "town"]
        return [(t, (x, y)) for t, x, y in words
                if any(abs(tx - x) < 90 and 40 < ty - y < 75 for tx, ty in towns)]

    def switch_account(self):
        """Settings cog -> blue switch button -> tap the next account on the Supercell ID list."""
        # _switch_streak = accounts already left because they were busy; this one is busy too
        if self._n_accounts and self._switch_streak + 1 >= self._n_accounts:
            self._switch_streak = 0
            if self.cfg["stop_when_all_busy"]:
                self.log("Every account's builders and lab are busy - nothing left to do, stopping.", "ok")
                self.stop_evt.set()
                raise Abort()
            self._rotate_pause_until = time.time() + 1800
            self.log("Every account's builders/lab are busy - farming here, checking again in 30 min.", "ok")
            return False
        frame = self.shot()
        k = frame.shape[1] / 1920
        cog = self.find(frame, "settings_cog_button") or (int(1842 * k), int(765 * k))
        self.tap(cog, 2.0)
        sw = self.wait_for("switch_account_button", 5)
        if not sw:
            return self.switch_fail("switch-account button not found")
        self.tap(sw, 2.5)
        if not self.wait_for("supercell_id_header", 6):
            return self.switch_fail("Supercell ID list didn't open")
        accounts = self.list_accounts(self.shot())
        wanted = {a.strip().lower() for a in self.cfg["accounts"].split(",") if a.strip()}
        if wanted:
            accounts = [a for a in accounts if a[0].lower() in wanted]
        self._n_accounts = len(accounts)
        if len(accounts) < 2:
            return self.switch_fail(f"need 2+ accounts to rotate, found {[a[0] for a in accounts]}")
        self._acc_idx = (self._acc_idx + 1) % len(accounts)
        if accounts[self._acc_idx][0].lower() == (self._last_name or "").lower():  # that's the one we're on
            self._acc_idx = (self._acc_idx + 1) % len(accounts)
        name, xy = accounts[self._acc_idx]
        self.log(f"Switching account -> {name}", "ok")
        self._last_name, self._pre = None, None  # new account: re-read its name, don't mix its loot with the last
        self.tap(xy, 4.0)
        end, backs = time.time() + 75, 0
        while time.time() < end:
            f = self.shot()
            if self.find(f, "attack_button"):
                self._switch_streak += 1
                self._upgrade_backoff.clear()
                self._bank_backoff.clear()
                self._busy_until.clear()
                self.bump("switches")
                # plans / scans / Star Bonus follow the in-game name (top-left) - the Supercell ID list's name can
                # differ; it's only the fallback when the name can't be read yet
                self._last_name = None
                for _ in range(3):
                    if self.account_name(self.shot()) != "?":
                        break
                    self.sleep(1.0)
                self._last_name = self._last_name or name
                self.emit("state", f"Home village ({name})")
                return True
            if self.find(f, "wall_okay_button") or time.time() > end - 60 + 6 * (backs + 1):
                self.adb.back()  # 'Welcome back' / news popups; Back never confirms anything
                backs += 1
            self.sleep(2.0)
        return self.switch_fail(f"{name} didn't reach its village")

    def switch_fail(self, why):
        self._rotate_pause_until = time.time() + 900
        self.log(f"Account switch: {why} - staying on this account for 15 min.", "warn")
        self.back_to_village()
        return False

    def line(self):
        """Drop line: deploy_point -> deploy_line_end (if captured), else just the one point."""
        pts = self.cfg["fixed_points"]
        a = pts["deploy_point"]
        return a, pts.get("deploy_line_end") or a

    @staticmethod
    def along(a, b, k, n):
        """k-th of n points spread evenly from a to b (inclusive)."""
        f = k / (n - 1) if n > 1 else 0.5
        return int(a[0] + (b[0] - a[0]) * f), int(a[1] + (b[1] - a[1]) * f)

    def drop(self, a, b, n, card):
        """Deploy n of the selected unit as taps spread evenly along a->b. Never drags: a moving touch scrolls the
        view instead of placing troops. With a single spot (a == b) and hold-to-deploy, a still long-press is used."""
        c = self.cfg
        if a == b and c["hold_deploy"] and n >= 3 and card:
            ms = min(n * c["hold_ms_per_troop"], 8000)
            for _ in range(3):
                self.adb.swipe(a[0], a[1], a[0], a[1], ms)
                if icon_empty(self.shot(), *card):
                    break
            return
        pts = [self.along(a, b, k, n) for k in range(n)]
        gap = max(0.0, c["deploy_tap_delay"])
        for i in range(0, n, 20):  # one shell call per 20 taps - fast, no round trip per troop
            chunk = pts[i:i + 20]
            self.adb.shell(f"; sleep {gap}; ".join(f"input tap {x} {y}" for x, y in chunk), timeout=10 + len(chunk))

    def auto_deploy(self, a, b):
        """Read this account's troop bar and deploy it: spells first (if enabled), then every troop card (all of
        it), then every hero / siege machine / pet (one tap each)."""
        cards = troop_bar(self.shot())
        troops = [cd for cd in cards if cd[2] == "troop" and cd[3]]
        spells = [cd for cd in cards if cd[2] == "spell" and cd[3]] if self.cfg["deploy_spells"] else []
        singles = [cd for cd in cards if cd[2] == "single"]
        if not troops and not singles:
            return False
        fp = self.cfg["fixed_points"]
        if fp.get("spell_point"):  # their own line, if set
            sa, sb = fp["spell_point"], fp.get("spell_line_end") or fp["spell_point"]
        else:  # else around the middle of the troop line
            sa, sb = self.along(a, b, 1, 4), self.along(a, b, 2, 4)
        for x, y, _, n in spells:  # spells FIRST, as taps spread along their line (a hold doesn't cast them)
            self.tap((x, y), self.cfg["deploy_select_delay"])
            self.drop(sa, sb, n, None)
        delay = self.cfg["deploy_select_delay"]
        for x, y, _, n in troops:  # every troop on the saved line - the camera pans to the same spot each battle
            self.tap((x, y), delay)
            self.drop(a, b, n, (x, y))
        for k, (x, y, _, _) in enumerate(singles):  # heroes / siege spread evenly along the same line
            self.tap((x, y), delay)
            self.adb.tap(*self.along(a, b, k, len(singles)))
            self.sleep(0.3)
        self._ability_cards = [(x, y) for x, y, _, _ in singles]  # tapping a deployed hero's card = its ability
        self.log(f"Auto-deployed {sum(cd[3] for cd in troops)} troops ({len(troops)} types), "
                 f"{len(singles)} heroes/siege" + (f", {len(spells)} spell types." if spells else "."))
        return True

    def close_popups(self):
        """Only clear things that block the Attack button: an Okay/Cancel dialog (Back = Cancel) or the
        builder list (its icon toggles it). A selected wall's button bar doesn't block Attack, so it stays -
        pressing Back there would open the 'quit the game?' dialog."""
        for _ in range(3):
            f = self.shot()
            if self.find(f, "wall_okay_button"):
                self.adb.back()
            elif self.find_wall_rows(f):
                self.adb.tap(*top_bar(f, "builder")[0])
            else:
                return
            self.sleep(0.8)

    # --- recovery ---
    def emulator_restart_allowed(self):
        return self.cfg["watchdog_enabled"] and os.path.isfile(self.cfg["emulator_exe_path"])

    def launch_game(self):
        pkg, act = self.cfg["coc_package_name"], self.cfg["coc_activity_name"]
        self.adb.shell(f"am force-stop {pkg}")
        self.sleep(2)
        if act:
            self.adb.shell(f"am start -n {pkg}/{act}", timeout=20)
        else:
            self.adb.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", timeout=20)
        self._launched_at = time.time()
        self.sleep(self.cfg["game_launch_wait"])

    def recover_game(self, reason):
        self.bump("recoveries")
        self.game_relaunches += 1
        self.emit("state", "Recovering")
        if self.game_relaunches > 2 and self.emulator_restart_allowed():
            self.log(f"Recovery: {reason}. Game relaunch didn't help twice - restarting the emulator.", "err")
            self.restart_emulator()
            self.game_relaunches = 0
            return
        self.log(f"Recovery: {reason} - relaunching the game.", "err")
        self.launch_game()
        if self.game_relaunches > 2:  # can't escalate: back off so we don't hammer a broken setup
            self.sleep(min(600, 60 * self.game_relaunches))

    def reconnect(self):
        target = self.cfg["auto_connect_target"]
        if target:
            self.log(f"adb connect {target}: {self.adb.connect(target)}")
        devs = self.adb.devices()
        if devs and self.adb.device not in devs:
            self.adb.device = target if target in devs else devs[0]
            self.emit("devices", devs)
        return self.adb.ready()

    def emulator_crashed(self):
        """BlueStacks' window process is gone (crashed or closed). Restart it; two crashes within 30 min mean this
        graphics setup doesn't work on this PC, so the next one is tried (and kept once it stops crashing)."""
        now = time.time()
        self._crashes = [t for t in getattr(self, "_crashes", []) if now - t < 1800] + [now]
        edit = None
        idx = int(self.cfg.get("gfx_profile", 0))
        if len(self._crashes) >= 2 and idx + 1 < len(GFX_PROFILES):
            idx += 1
            name, values = GFX_PROFILES[idx]
            self.cfg["gfx_profile"] = idx
            save_config(self.cfg)
            self._crashes = []
            self.log(f"BlueStacks keeps crashing - switching its graphics to '{name}' and restarting.", "err")
            edit = lambda: bs_conf_set(values)
        else:
            self.log(f"BlueStacks crashed or was closed ({len(self._crashes)} in 30 min) - restarting it.", "err")
        self.bump("recoveries")
        self.restart_emulator(while_closed=edit)

    def recover_adb(self, fails):
        self.adb.close_shell()
        if self.emulator_restart_allowed() and not process_running(self.cfg["emulator_exe_path"]):
            return self.emulator_crashed()
        try:
            if fails >= 2:
                self.adb.host("kill-server", timeout=10)
                self.adb.host("start-server", timeout=20)
            if self.reconnect():
                self.log("ADB reconnected.", "ok")
                self.emit("device", True)
                return
        except ADBError as e:
            self.log(f"Reconnect failed: {e}", "warn")
        if fails >= 3 and self.emulator_restart_allowed():
            self.bump("recoveries")
            self.restart_emulator()
            return
        self.sleep(min(120, 3 * fails * fails))  # 3s, 12s, 27s... quick first retry

    def fix_resolution(self, frame):
        """Buttons, drop lines and text boxes are all 1920x1080 pixels. The monitor doesn't matter (screenshots
        come from inside the emulator), but BlueStacks' own resolution does: set it back and restart BlueStacks."""
        h, w = frame.shape[:2]
        conf = r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf"
        if getattr(self, "_res_fixed", False) or not self.emulator_restart_allowed() or not os.path.exists(conf):
            self.log(f"The emulator is {w}x{h} but must be 1920x1080: BlueStacks Settings > Display > "
                     "1920x1080, DPI 240, then restart BlueStacks.", "err")
            self.sleep(60)
            return
        self._res_fixed = True
        self.log(f"The emulator is {w}x{h} - setting BlueStacks to 1920x1080 and restarting it.", "warn")

        self.restart_emulator(while_closed=lambda: bs_conf_set({"fb_width": "1920", "fb_height": "1080", "dpi": "240"}))

    def restart_emulator(self, while_closed=None):
        now = time.time()
        self._emu_restarts = [t for t in self._emu_restarts if now - t < 3600]
        if len(self._emu_restarts) >= 3:
            self.log("3 emulator restarts in the last hour - cooling down 15 min before the next one.", "err")
            self.sleep(900)
        self._emu_restarts.append(time.time())
        exe = self.cfg["emulator_exe_path"]
        self.log("Restarting the emulator...", "err")
        self.adb.close_shell()
        subprocess.run(["taskkill", "/IM", os.path.basename(exe), "/T", "/F"], capture_output=True,
                       timeout=20, creationflags=NO_WINDOW)
        self.sleep(5)
        try:  # BlueStacks rewrites its conf on exit, so settings are changed only while it's closed
            bs_conf_set(GFX_PROFILES[min(int(self.cfg.get("gfx_profile", 0)), len(GFX_PROFILES) - 1)][1])
            if while_closed:
                while_closed()
        except OSError as e:
            self.log(f"Couldn't change BlueStacks' settings: {e}", "err")
        subprocess.Popen([exe, *self.cfg["emulator_launch_args"].split()], creationflags=0x00000008)  # detached
        self.sleep(self.cfg["emulator_boot_wait"])
        end = time.time() + self.cfg["adb_ready_timeout"]
        while time.time() < end:
            try:
                if self.reconnect():
                    break
            except ADBError:
                pass
            self.sleep(5)
        else:
            self.log("Emulator didn't come back on ADB in time; will keep trying.", "warn")
            return
        self.emit("device", True)
        for i in range(4):  # Android can still be starting up for a while after ADB answers
            try:
                return self.launch_game()
            except ADBError as e:
                self.log(f"Game launch after the emulator restart failed ({e}) - retrying.", "warn")
                self.sleep(15)
                self.reconnect()


# ---------------------------------------------------------------------------
# Phone view: read-only web page (live screen + stats) for any browser on the same Wi-Fi
# ---------------------------------------------------------------------------
PHONE_PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Loot Farmer</title><style>
body{margin:0;background:#1c1c1c;color:#e6e6e6;font:15px system-ui,sans-serif;padding:14px}
h1{font-size:20px;margin:0 0 2px}#state{color:#4cc38a;font-weight:600;margin-bottom:10px}
img{width:100%;border-radius:10px;background:#141414;min-height:120px}
.g{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:12px 0}
.c{background:#2b2b2b;border-radius:10px;padding:10px}.c b{display:block;font-size:20px}
.c span{color:#9a9a9a;font-size:12px;text-transform:uppercase}
.gold{color:#f5c542}.elixir{color:#d77bff}
#loot table{width:100%;border-collapse:collapse;margin-bottom:12px;font-size:13px}
#loot td,#loot th{padding:6px;text-align:right;border-bottom:1px solid #333}#loot td:first-child,#loot th:first-child{text-align:left}
#log{background:#141414;border-radius:10px;padding:10px;font:12px ui-monospace,monospace;white-space:pre-wrap}
</style></head><body><h1>&#9876; Loot Farmer</h1><div id="state">&hellip;</div><img id="f">
<div class="g" id="g"></div><div id="loot"></div><div id="log"></div><script>
const T=[["battery","Battery"],["runtime","Runtime"],["attacks","Attacks"],["skipped","Skipped"],["walls","Walls"],
["recoveries","Recoveries"],["errors","Errors"],["s_gold","Your gold","gold"],["s_elixir","Your elixir","elixir"],
["upgrades","Upgrades"],["switches","Switches"],["b_gold","Base gold","gold"],["b_elixir","Base elixir","elixir"]];
async function tick(){try{const s=await (await fetch("status"+location.search,{cache:"no-store"})).json();
document.getElementById("state").textContent=s.state;
document.getElementById("g").innerHTML=T.map(([k,l,c])=>`<div class="c"><span>${l}</span><b class="${c||""}">${s[k]??"-"}</b></div>`).join("");
document.getElementById("log").textContent=s.log.join("\\n");
const L=s.loot||[];document.getElementById("loot").innerHTML=L.length?"<table><tr><th>Account</th><th>Gold</th><th>/hr</th><th>Elixir</th><th>/hr</th><th>Dark</th><th>/hr</th></tr>"+L.map(r=>`<tr><td>${r[0]}</td><td class="gold">${r[1]}</td><td class="gold">${r[2]}</td><td class="elixir">${r[3]}</td><td class="elixir">${r[4]}</td><td>${r[5]}</td><td>${r[6]}</td></tr>`).join("")+"</table>":"";
document.getElementById("f").src="frame.jpg"+location.search+"&t="+Date.now();}catch(e){document.getElementById("state").textContent="PC not reachable";}}
tick();setInterval(tick,1500);</script></body></html>"""


class PhoneView:
    """Serves PHONE_PAGE, the latest frame and a status JSON. Read-only: nothing on it controls the bot."""

    def __init__(self, port, key):
        self.jpeg, self.status = b"", {"state": "Idle", "log": []}
        view = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                path, _, query = self.path.partition("?")
                if f"k={key}" not in query.split("&"):
                    self.send_error(403, "Missing or wrong access key - use the full link shown in the app.")
                    return
                if path == "/frame.jpg":
                    body, ctype = view.jpeg, "image/jpeg"
                elif path == "/status":
                    body, ctype = json.dumps(view.status).encode(), "application/json"
                else:
                    body, ctype = PHONE_PAGE.encode(), "text/html; charset=utf-8"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        self.server = http.server.ThreadingHTTPServer(("0.0.0.0", port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


def _pid_image(pid):
    """Full exe path of a running process, or None if it isn't running (Windows)."""
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        code = ctypes.c_ulong()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return None
        buf, n = ctypes.create_unicode_buffer(1024), ctypes.c_ulong(1024)
        return buf.value if k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)) else None
    finally:
        k32.CloseHandle(h)


class Tunnel:
    """Cloudflare quick tunnel: a public https://....trycloudflare.com link to the phone view, no account.
    cloudflared runs DETACHED and is reused by the next app start (tunnel.json), so restarting the app keeps
    the same link. It only changes after a PC reboot or if the tunnel drops (then a new one starts)."""
    STATE = os.path.join(BASE_DIR, "tunnel.json")
    LOG = os.path.join(BASE_DIR, "cloudflared.log")

    def __init__(self, exe, port, on_url):
        self.exe, self.port, self.on_url = exe, port, on_url
        threading.Thread(target=self._run, daemon=True).start()

    def _alive(self, st):
        img = _pid_image(st.get("pid", 0)) if st else None
        return bool(img and img.lower().endswith("cloudflared.exe") and st.get("port") == self.port)

    def _start(self):
        with open(self.LOG, "w") as lf:
            proc = subprocess.Popen([self.exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.port}"],
                                    stdout=lf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                    creationflags=0x00000008 | 0x00000200)  # DETACHED | NEW_PROCESS_GROUP
        for _ in range(60):
            time.sleep(1)
            try:
                m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", open(self.LOG, errors="replace").read())
            except OSError:
                m = None
            if m:
                st = {"pid": proc.pid, "url": m.group(), "port": self.port}
                with open(self.STATE, "w") as f:
                    json.dump(st, f)
                return st
            if proc.poll() is not None:
                break
        log_file.warning("cloudflared didn't give a link: " + open(self.LOG, errors="replace").read()[-400:])
        proc.kill()
        return None

    def _run(self):
        try:
            st = json.load(open(self.STATE))
        except Exception:
            st = None
        while True:
            try:
                if self._alive(st) and self._link_works(st["url"]) is False:
                    # e.g. a saved tunnel Cloudflare has deleted: cloudflared keeps running ('Tunnel not found')
                    log_file.warning(f"Anywhere link {st['url']} is dead - starting a new one.")
                    self._kill(st)
                if not self._alive(st):
                    self.on_url(None)
                    try:
                        st = self._start()
                    except OSError as e:
                        log_file.warning(f"cloudflared failed: {e}")
                        st = None
                    # a brand-new name takes a few seconds to exist; opening it before that gets 'site can't be
                    # reached' cached in the browser, so only hand the link out once it answers
                    if st:
                        for _ in range(18):  # up to ~90s
                            if self._link_works(st["url"]):
                                break
                            time.sleep(5)
                        else:  # can't confirm it (our own check may be what's failing): hand it out anyway
                            log_file.warning(f"New Anywhere link {st['url']} didn't answer a check yet.")
                    if not st:
                        time.sleep(30)
                        continue
                self.on_url(st["url"])
                checked = time.time()
                while self._alive(st):
                    time.sleep(10)
                    if time.time() - checked > 60:
                        checked = time.time()
                        if self._link_works(st["url"]) is False and self._link_works(st["url"]) is False:
                            log_file.warning(f"Anywhere link {st['url']} stopped working - starting a new one.")
                            self._kill(st)
                            break
            except Exception as e:  # the tunnel thread must never die
                log_file.warning(f"Tunnel: {e}")
                st = None
                time.sleep(30)

    @staticmethod
    def _kill(st):
        subprocess.run(["taskkill", "/PID", str(st["pid"]), "/F"], capture_output=True, creationflags=NO_WINDOW)
        time.sleep(1)

    @staticmethod
    def _link_works(url):
        """True: our server answers through the tunnel. False: Cloudflare says it's gone (name doesn't exist /
        530). None: can't tell (no internet, VPN reconnecting...) - never a reason to throw a working link away."""
        import urllib.error
        import urllib.request
        host = url.split("//", 1)[-1].split("/")[0]
        try:  # ask Cloudflare's DNS directly: a lookup through Windows would cache 'no such name' for minutes
            r = json.load(urllib.request.urlopen(urllib.request.Request(
                f"https://cloudflare-dns.com/dns-query?name={host}&type=A",
                headers={"accept": "application/dns-json"}), timeout=10))
            if r.get("Status") == 3 or (r.get("Status") == 0 and not r.get("Answer")):  # 3 = no such name
                return False
        except Exception:
            return None
        try:
            urllib.request.urlopen(url + "/status", timeout=15).close()
            return True
        except urllib.error.HTTPError as e:
            return e.code not in (502, 530, 1033)
        except Exception:
            return None

    @staticmethod
    def kill_saved():
        """Stop a background tunnel left running (used when the public link is turned off)."""
        try:
            st = json.load(open(Tunnel.STATE))
            if (_pid_image(st["pid"]) or "").lower().endswith("cloudflared.exe"):
                subprocess.run(["taskkill", "/PID", str(st["pid"]), "/F"], capture_output=True,
                               creationflags=NO_WINDOW)
        except Exception:
            pass


PAN_DIRS = {"top-left": (1, 1), "top-right": (-1, 1), "bottom-left": (1, -1), "bottom-right": (-1, -1),
            "left": (1, 0), "right": (-1, 0), "top": (0, 1), "bottom": (0, -1)}


def pan_view(adb, where, frame_w=1920, frame_h=1080):
    """Drag the battle camera as far as it goes towards `where` (e.g. 'top-left' shows the map's top-left edge).
    The game stops at the map edge, so the view is identical every attack and saved drop points line up.
    Slow drags on the open map area (away from the buttons and troop bar) so nothing gets tapped or flung."""
    d = PAN_DIRS.get(where)
    if not d:
        return
    k = frame_w / 1920
    cx, cy, dx, dy = 960 * k, 460 * k, 330 * k * d[0], 200 * k * d[1]
    for _ in range(3):
        adb.swipe(cx - dx, cy - dy, cx + dx, cy + dy, 450)
        time.sleep(0.25)


BS_CONF = r"C:\ProgramData\BlueStacks_nxt\bluestacks.conf"
# Graphics setups to try, in order, when BlueStacks keeps crashing (typically on the game's loading clouds - a
# graphics-driver crash, e.g. NVIDIA + Vulkan). 0 = leave BlueStacks as the user set it.
GFX_PROFILES = [
    ("as set in BlueStacks", {}),
    ("OpenGL", {"graphics_renderer": "gl", "graphics_engine": "aga", "max_fps": "30"}),
    ("OpenGL + Compatibility", {"graphics_renderer": "gl", "graphics_engine": "legacy", "max_fps": "30"}),
    ("Vulkan + Compatibility", {"graphics_renderer": "vlcn", "graphics_engine": "legacy", "max_fps": "30"}),
]


def bs_conf_set(values):
    """Set bst.instance.<every instance>.<key> in bluestacks.conf. Only while BlueStacks is closed: it rewrites
    the file when it exits. A backup of the first original is kept next to it."""
    if not values or not os.path.exists(BS_CONF):
        return
    if not os.path.exists(BS_CONF + ".lootfarmer-original"):
        shutil.copy2(BS_CONF, BS_CONF + ".lootfarmer-original")
    s = open(BS_CONF, encoding="utf-8").read()
    for key, val in values.items():
        s = re.sub(rf'(bst\.instance\.[^.\n]+\.{key})="[^"]*"', rf'\1="{val}"', s)
    with open(BS_CONF, "w", encoding="utf-8", newline="") as f:
        f.write(s)


def process_running(exe):
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {os.path.basename(exe)}", "/NH"], capture_output=True,
                             text=True, timeout=15, creationflags=NO_WINDOW).stdout
    except Exception:
        return True  # can't tell: assume it's running rather than kill a working emulator
    return os.path.basename(exe).lower() in out.lower()


def post_discord(cfg, text, files=()):
    """Best effort: a failed post is logged, never raised. files: [(filename, bytes)], up to 10."""
    import base64
    import urllib.request
    hook = cfg.get("discord_webhook") or ""
    if not hook.startswith("http"):
        try:
            hook = base64.b64decode(hook).decode()
        except Exception:
            return
    if not hook.startswith("https://discord.com/api/webhooks/"):
        return
    payload = json.dumps({"content": text[:1900]})
    if files:  # multipart: the message as payload_json + each file as files[i]
        b = "----LootFarmer" + secrets.token_hex(8)
        nl = "\r\n"
        parts = [f'--{b}{nl}Content-Disposition: form-data; name="payload_json"{nl}'
                 f'Content-Type: application/json{nl}{nl}{payload}{nl}'.encode()]
        for i, (name, data) in enumerate(files[:10]):
            parts += [f'--{b}{nl}Content-Disposition: form-data; name="files[{i}]"; filename="{name}"{nl}'
                      f'Content-Type: application/octet-stream{nl}{nl}'.encode(), data, nl.encode()]
        body, ctype = b"".join(parts) + f"--{b}--{nl}".encode(), f"multipart/form-data; boundary={b}"
    else:
        body, ctype = payload.encode(), "application/json"
    import urllib.error
    for i in range(5):  # at start-up the network may not be up yet; Discord rate-limits with 429 + retry_after
        try:
            req = urllib.request.Request(hook, body, method="POST",
                                         headers={"Content-Type": ctype, "User-Agent": "LootFarmer"})
            urllib.request.urlopen(req, timeout=60).close()
            return True
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 504):
                log_file.warning(f"Discord post refused: {e}")
                return
            wait = 5 * (i + 1)
            try:
                wait = float(json.loads(e.read()).get("retry_after", wait)) + 1
            except Exception:
                pass
        except Exception as e:
            log_file.info(f"Discord post failed (try {i + 1}): {e}")
            wait = 10 * (i + 1)
        time.sleep(min(wait, 60))
    log_file.warning("Discord post failed 5 times - giving up on this message.")


def single_instance():
    """A machine-wide lock so only one Loot Farmer drives the emulator. Waits a few seconds first, so the
    Restart / Update buttons (new copy starts while the old one closes) still work. None = another is running."""
    if os.name != "nt":
        return True
    import ctypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    for _ in range(20):
        h = k32.CreateMutexW(None, False, "Local\\LootFarmerBot")
        if h and ctypes.get_last_error() != 183:  # 183 = ERROR_ALREADY_EXISTS
            return h
        if h:
            k32.CloseHandle(h)
        time.sleep(0.5)
    return None


_TOUCH = {}


def touch_device(adb):
    """(path, max_x, max_y) of the emulator's multi-touch input device (BlueStacks: 'Virtual Touch'), or None."""
    if "dev" not in _TOUCH:
        dev = None
        try:
            out = adb.shell("getevent -p", timeout=10)
        except ADBError:
            return None  # try again next time
        for block in out.split("add device")[1:]:
            mx = re.search(r"0035\s*:.*?max (\d+)", block)
            my = re.search(r"0036\s*:.*?max (\d+)", block)
            if mx and my:
                dev = (block.split(":", 1)[1].split()[0], int(mx.group(1)), int(my.group(1)))
                break
        _TOUCH["dev"] = dev
    return _TOUCH["dev"]


def zoom_out(adb, times=3, W=1920, H=1080, spread=((460, 900), (1460, 1020))):
    """Two-finger pinch (fingers moving together) = the game's zoom-out, sent straight to the touch device.
    Stops at the game's limit, so extra pinches are harmless. Raw input_event structs are written in one shell
    call per pinch (sendevent costs ~50 ms per event: 3 pinches took 16 s, longer than half the scout timer).
    spread: each finger's start -> end x (swap them to zoom in)."""
    import base64
    d = touch_device(adb)
    if not d:
        return False
    dev, mx, my = d
    size = 24  # struct input_event on the 64-bit Android image: timeval 16 + 2 + 2 + 4
    ev = lambda t, c, v: struct.pack("<qqHHI", 0, 0, t, c, v & 0xFFFFFFFF)
    assert len(ev(0, 0, 0)) == size
    for _ in range(times):
        steps = []
        for i in range(9):
            f, b = i / 8, b""
            for slot, (x0, x1) in enumerate(spread):
                b += ev(3, 47, slot) + (ev(3, 57, 100 + slot) if i == 0 else b"")
                b += ev(3, 53, int((x0 + (x1 - x0) * f) / W * mx)) + ev(3, 54, int(480 / H * my))
            b += (ev(1, 330, 1) if i == 0 else b"") + ev(0, 0, 0)  # BTN_TOUCH down with the first frame
            steps.append(b)
        steps.append(ev(3, 47, 0) + ev(3, 57, -1) + ev(3, 47, 1) + ev(3, 57, -1) + ev(1, 330, 0) + ev(0, 0, 0))
        adb.shell("; sleep 0.02; ".join(f"echo {base64.b64encode(s).decode()} | base64 -d > {dev}" for s in steps),
                  timeout=30)
        time.sleep(0.3)
    return True


def battery():
    """(percent, plugged_in) from Windows, or None on a PC without a battery."""
    import ctypes

    class SPS(ctypes.Structure):
        _fields_ = [("ac", ctypes.c_ubyte), ("flag", ctypes.c_ubyte), ("pct", ctypes.c_ubyte),
                    ("saver", ctypes.c_ubyte), ("life", ctypes.c_ulong), ("full", ctypes.c_ulong)]
    sps = SPS()
    if not ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(sps)) or sps.pct == 255 or sps.flag & 128:
        return None
    return sps.pct, sps.ac == 1


def lan_ip():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))  # no packet is sent; just picks the Wi-Fi/LAN interface
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------
BG, CARD, MUTED, TEXT = "#1c1c1c", "#2b2b2b", "#9a9a9a", "#e6e6e6"
GREEN, AMBER, RED, BLUE, GOLD, PINK = "#4cc38a", "#e5b454", "#ff6b6b", "#57a6ff", "#f5c542", "#d77bff"
LEVEL_COLORS = {"info": TEXT, "ok": GREEN, "warn": AMBER, "err": RED}
UI_SCALE = 1.0  # set from the real DPI at startup so the layout isn't tiny on 150-200% displays


def S(*v):
    r = tuple(int(round(x * UI_SCALE)) for x in v)
    return r if len(r) > 1 else r[0]



class DropLinePicker(tk.Toplevel):
    """Click to pin-point the troop drop line on a live screenshot. 1st click = start, 2nd = end, further clicks
    move whichever marker is nearer. Refresh grabs a new screenshot (e.g. once you're on the scouting screen)."""

    def __init__(self, app, frame, keys=("deploy_point", "deploy_line_end"), what="Troop"):
        super().__init__(app)
        self.app, self.keys, self.what = app, keys, what
        self.title(f"{what} drop line")
        self.configure(bg=BG)
        fp = app.cfg["fixed_points"]
        self.pts = [fp.get(keys[0]), fp.get(keys[1])]
        self.pts = [list(p) if p else None for p in self.pts]
        bar = ttk.Frame(self, padding=S(12, 10))
        bar.pack(fill="x")
        ttk.Label(bar, text="On the scouting screen: 'Zoom out + pan', then click where the line STARTS and ENDS "
                            "(grass outside the red border).").pack(side="left")
        ttk.Button(bar, text="Save", style="Accent.TButton", command=self.save).pack(side="right")
        ttk.Button(bar, text="Refresh screenshot", command=self.refresh).pack(side="right", padx=S(8))
        ttk.Button(bar, text="Zoom out + pan + refresh", command=self.pan).pack(side="right")
        ttk.Button(bar, text="Clear", command=self.clear).pack(side="right")
        self.info = ttk.Label(self, style="Muted.TLabel", padding=S(12, 0, 12, 6))
        self.info.pack(anchor="w")
        self.cv = tk.Canvas(self, highlightthickness=0, bg=BG, cursor="crosshair")
        self.cv.pack(padx=S(12), pady=S(0, 12))
        self.cv.bind("<Button-1>", self.click)
        self.bind("<Escape>", lambda e: self.destroy())
        self.show(frame)
        self.transient(app)
        self.focus_set()

    def show(self, frame):
        h, w = frame.shape[:2]
        self.scale = min(S(1200) / w, S(680) / h, 1.0)
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
            (int(w * self.scale), int(h * self.scale)), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)
        self.cv.config(width=img.width, height=img.height)
        self.draw()

    def draw(self):
        self.cv.delete("all")
        self.cv.create_image(0, 0, anchor="nw", image=self.photo)
        k = self.scale
        a, b = self.pts
        if a and b:
            for w, col in ((S(8), "black"), (S(4), "#ffd21f")):  # dark outline so it shows on grass
                self.cv.create_line(a[0] * k, a[1] * k, b[0] * k, b[1] * k, fill=col, width=w, capstyle="round")
        r = S(11)
        for p, label, col in ((a, "START", GREEN), (b, "END", BLUE)):
            if p:
                x, y = p[0] * k, p[1] * k
                self.cv.create_oval(x - r, y - r, x + r, y + r, outline="white", width=S(3), fill=col)
                for dx, dy, c in ((2, 2, "black"), (0, 0, "white")):
                    self.cv.create_text(x + r + 6 + dx, y - r - 6 + dy, text=label, fill=c, anchor="w",
                                        font=("Segoe UI", 12, "bold"))
        self.info.config(text=f"Start: {tuple(a) if a else '-'}     End: {tuple(b) if b else '- (one spot only)'}")

    def click(self, e):
        p = [int(e.x / self.scale), int(e.y / self.scale)]
        a, b = self.pts
        if not a:
            self.pts[0] = p
        elif not b:
            self.pts[1] = p
        else:  # move the nearer marker
            near = min((0, 1), key=lambda i: (self.pts[i][0] - p[0]) ** 2 + (self.pts[i][1] - p[1]) ** 2)
            self.pts[near] = p
        self.draw()

    def clear(self):
        self.pts = [None, None]
        self.draw()

    def refresh(self):
        self.app.with_screenshot(lambda f: self.winfo_exists() and self.show(f))

    def pan(self):
        """Pan exactly like the bot does before deploying, so the points are picked on the same view."""
        where = self.app.cfg.get("deploy_pan", "off")
        if where not in PAN_DIRS:
            return messagebox.showinfo("Pan", "Set 'Pan view before deploying' in Settings > Battle first.", parent=self)
        self.app.bg(lambda: (zoom_out(self.app.adb), pan_view(self.app.adb, where), time.sleep(0.6),
                             self.app.ui(self.refresh)))

    def save(self):
        a, b = self.pts
        if not a:
            return messagebox.showinfo("Drop line", "Click at least the start point.", parent=self)
        fp = self.app.cfg["fixed_points"]
        fp[self.keys[0]] = a
        if b:
            fp[self.keys[1]] = b
        else:
            fp.pop(self.keys[1], None)
        save_config(self.app.cfg)
        self.app.log(f"{self.what} drop line saved: {tuple(a)} -> {tuple(b) if b else 'single spot'}.", "ok")
        self.app.refresh_setup()
        self.destroy()


PRETTY = {"ottos outpost": "O.T.T.O's Outpost", "x bow": "X-Bow", "builders hut": "Builder's Hut",
          "multi archer tower": "Multi-Archer Tower", "multi gear tower": "Multi-Gear Tower"}
pretty = lambda n: PRETTY.get(n, n.title())
PLAN_CATS = ("All", "Heroes", "Army", "Defences", "Guardians", "Traps", "Resources", "Other")


class PlannerWindow(tk.Toplevel):
    """Per-account upgrade planner: every upgrade the account has left (from its last scan) as a card with its picture,
    current level and the max for its Town Hall; click a level to queue it. The queue is this account's order."""

    def __init__(self, app, account=None):
        super().__init__(app)
        self.app, self.cfg = app, app.cfg
        self.title("Upgrade planner")
        self.configure(bg=BG)
        self.geometry(f"{S(1240)}x{S(780)}")
        self._img = {}
        top = ttk.Frame(self, padding=S(14, 12, 14, 6))
        top.pack(fill="x")
        ttk.Label(top, text="Account", style="CardTitle.TLabel").pack(side="left")
        self.acc = ttk.Combobox(top, state="readonly", width=16)
        self.acc.pack(side="left", padx=S(6, 14))
        self.acc.bind("<<ComboboxSelected>>", lambda e: self.refresh())
        self.base = tk.StringVar(value="home")
        for v, t in (("home", "Home village"), ("builder", "Builder Base")):
            ttk.Radiobutton(top, text=t, value=v, variable=self.base, command=self.refresh).pack(side="left",
                                                                                                padx=S(0, 8))
        ttk.Label(top, text="Hall level", style="CardTitle.TLabel").pack(side="left", padx=S(10, 4))
        self.hall = ttk.Spinbox(top, from_=1, to=18, width=4, command=self.set_hall)
        self.hall.pack(side="left")
        self.hall.bind("<Return>", lambda e: self.set_hall())
        ttk.Button(top, text="🔍  Scan the account in the game now", style="Accent.TButton",
                   command=self.scan).pack(side="right")
        self.info = ttk.Label(self, style="Sub.TLabel", padding=S(14, 0, 14, 6))
        self.info.pack(fill="x")

        body = ttk.Frame(self, padding=S(14, 0, 14, 14))
        body.pack(fill="both", expand=True)
        # left: this account's queue
        q = ttk.Frame(body, style="Card.TFrame", padding=S(10))
        q.pack(side="left", fill="y")
        self.qtitle = ttk.Label(q, style="CardTitle.TLabel")
        self.qtitle.pack(anchor="w")
        ttk.Label(q, text="Top = upgraded first. Done targets drop off\nby themselves.", style="Sub.TLabel").pack(
            anchor="w", pady=S(2, 6))
        self.qbox = self._scroller(q, S(330))
        ttk.Button(q, text="Clear this account's plan", command=self.clear).pack(anchor="w", pady=S(8, 0))
        # right: cards
        r = ttk.Frame(body)
        r.pack(side="left", fill="both", expand=True, padx=S(12, 0))
        bar = ttk.Frame(r)
        bar.pack(fill="x", pady=S(0, 6))
        self.cat = tk.StringVar(value="All")
        for c in PLAN_CATS:
            ttk.Radiobutton(bar, text=c, value=c, variable=self.cat, command=self.draw_cards).pack(
                side="left", padx=S(0, 6))
        self.search = ttk.Entry(bar, width=16)
        self.search.pack(side="right")
        self.search.bind("<KeyRelease>", lambda e: self.draw_cards())
        ttk.Label(bar, text="🔎", style="Sub.TLabel").pack(side="right")
        self.cards = self._scroller(r, None)
        self.transient(app)
        self.accounts()
        self.refresh()

    # --- helpers ---
    def _scroller(self, parent, width):
        wrap = ttk.Frame(parent)
        wrap.pack(fill="both", expand=True)
        cv = tk.Canvas(wrap, bg=CARD if width else BG, highlightthickness=0, **({"width": width} if width else {}))
        sb = ttk.Scrollbar(wrap, command=cv.yview)
        inner = ttk.Frame(cv, style="Card.TFrame" if width else "TFrame")
        inner.bind("<Configure>", lambda e: cv.configure(scrollregion=cv.bbox("all")))
        win = cv.create_window(0, 0, anchor="nw", window=inner)
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        cv.configure(yscrollcommand=sb.set)
        cv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        wheel = lambda e: cv.yview_scroll(int(-e.delta / 120), "units")
        cv.bind("<Enter>", lambda e: cv.bind_all("<MouseWheel>", wheel))
        cv.bind("<Leave>", lambda e: cv.unbind_all("<MouseWheel>"))
        inner.canvas = cv
        return inner

    def icon(self, key, size):
        if (key, size) not in self._img:
            e = wiki_data().get(key) or {}
            try:
                im = Image.open(os.path.join(WIKI_DIR, e["icon"])).convert("RGBA")
                im.thumbnail((size, size))
                self._img[(key, size)] = ImageTk.PhotoImage(im)
            except Exception:
                self._img[(key, size)] = None
        return self._img[(key, size)]

    def accounts(self):
        names = sorted(set((self.cfg.get("scans") or {})) | set((self.cfg.get("plans") or {})) - {"?"})
        cur = getattr(getattr(self.app, "bot", None), "_last_name", None)
        self.acc["values"] = names
        pick = cur if cur in names else self.acc.get() if self.acc.get() in names else (names[0] if names else "")
        self.acc.set(pick)

    def scan_data(self):
        return ((self.cfg.get("scans") or {}).get(self.acc.get()) or {}).get(self.base.get()) or {}

    def plan(self):
        p = self.cfg.setdefault("plans", {}).setdefault(self.acc.get(), {})
        return p.setdefault(self.base.get(), [])

    def hall_level(self):
        sd = self.scan_data()
        if sd.get("hall"):
            return sd["hall"]
        # not on the list (maxed, or upgrading): at least what the list's next levels need
        reqs = [wiki_data()[r["key"]]["levels"][r["level"]]["req"] for r in sd.get("items", [])
                if r.get("key") and r.get("level") is not None and not r.get("upgrading")
                and r["level"] < len(wiki_data()[r["key"]]["levels"])
                and wiki_data()[r["key"]]["levels"][-1].get("hall") in ("town hall", "builder hall")]
        return max(reqs) if reqs else None

    def set_hall(self):
        try:
            v = int(self.hall.get())
        except ValueError:
            return
        sd = self.cfg.setdefault("scans", {}).setdefault(self.acc.get(), {}).setdefault(self.base.get(), {})
        sd["hall"] = v
        save_config(self.cfg)
        self.refresh()

    def groups(self):
        """{key: [(level, count), ...]} of what's left, from the scan (several levels per building possible)."""
        g = {}
        for r in self.scan_data().get("items", []):
            if r.get("key"):
                g.setdefault(r["key"], []).append((r.get("level"), r.get("count", 1), r.get("upgrading", False)))
        return g

    # --- drawing ---
    def refresh(self):
        if not self.winfo_exists():
            return
        self.accounts()
        sd, acc = self.scan_data(), self.acc.get()
        hall = self.hall_level()
        self.hall.set(hall or "")
        what = "Town Hall" if self.base.get() == "home" else "Builder Hall"
        if not acc:
            self.info.config(text="No account scanned yet - open the game on an account and click 'Scan the account"
                                  " in the game now' (farming also scans each account automatically).")
        elif not sd:
            self.info.config(text=f"{acc}: no {'Builder Base' if self.base.get() == 'builder' else 'home'} scan yet"
                                  " - click Scan while that village is showing in the game.")
        else:
            d = sd.get("discount", 1)
            self.info.config(text=f"{acc} · {what} {hall or '?'} · scanned {sd.get('time', '?')}"
                                  + (" · exact levels from the game's data export" if sd.get("source") == "export"
                                     else "")
                                  + (f" · prices were {round((1 - d) * 100)}% off (Hammer Jam / Gold Pass) - "
                                     "levels worked out with that" if d < 1 else ""))
        self.qtitle.config(text=f"Upgrade order - {acc or 'no account'}")
        self.build_cards()
        self.draw_queue()

    # plain tk widgets inside the lists: themed ttk ones made each redraw take seconds
    def _lbl(self, parent, text="", bold=False, muted=False, size=10, **kw):
        return tk.Label(parent, text=text, bg=kw.pop("bg", CARD), fg=MUTED if muted else TEXT, anchor="w",
                        font=("Segoe UI", size, "bold" if bold else "normal"), justify="left", **kw)

    def _chip(self, parent, text, cmd):
        c = tk.Label(parent, text=text, bg="#3a3a3a", fg=TEXT, font=("Segoe UI", 9, "bold"), padx=S(7), pady=S(3),
                     cursor="hand2")
        c.bind("<Button-1>", lambda e: cmd())
        return c

    def draw_queue(self):
        for w in self.qbox.winfo_children():
            w.destroy()
        plan, g = self.plan(), self.groups()
        if not plan:
            self._lbl(self.qbox, "Nothing queued yet.\nClick a level on a building →", muted=True).pack(
                anchor="w", pady=S(6))
        for i, it in enumerate(plan):
            e = wiki_data().get(it["key"], {})
            left = sum(c for lv, c, up in g.get(it["key"], []) if lv is not None and lv < it["to"] and not up)
            row = tk.Frame(self.qbox, bg=CARD, pady=S(3))
            row.pack(fill="x")
            self._lbl(row, f"{i + 1}.", muted=True, width=3).pack(side="left")
            ic = self.icon(it["key"], S(34))
            if ic:
                tk.Label(row, image=ic, bg=CARD).pack(side="left")
            txt = tk.Frame(row, bg=CARD)
            txt.pack(side="left", fill="x", expand=True, padx=S(6, 0))
            self._lbl(txt, f"{e.get('name', it['key'])} → {it['to']}", bold=True).pack(anchor="w")
            self._lbl(txt, f"{left} to upgrade" if left else "✓ reached (or upgrading)", muted=True,
                      size=9).pack(anchor="w")
            for sym, cmd in (("✕", lambda i=i: self.remove(i)), ("▼", lambda i=i: self.move(i, 1)),
                             ("▲", lambda i=i: self.move(i, -1))):
                self._chip(row, sym, cmd).pack(side="right", padx=S(1))

    def build_cards(self):
        """One card per building this account can still upgrade - built once per account / village / scan."""
        g, hall = self.groups(), self.hall_level()
        hero_hall = self.scan_data().get("hero_hall")
        sig = (self.acc.get(), self.base.get(), self.scan_data().get("time"), hall, hero_hall,
               tuple(sorted((k, tuple(v)) for k, v in g.items())))
        if sig == getattr(self, "_sig", None):
            return self.draw_cards()
        self._sig = sig
        for w in self.cards.winfo_children():
            w.destroy()
        self._cards = {}
        self._empty = self._lbl(self.cards, "Nothing to show - scan the account first (or change the filter).",
                                muted=True, bg=BG)
        for c in range(3):
            self.cards.columnconfigure(c, weight=1, uniform="card")
        for k in g:
            e = wiki_data()[k]
            levels = sorted(((lv, c, up) for lv, c, up in g[k] if lv is not None), key=lambda x: x[0])
            cur = min((lv for lv, c, up in levels), default=None)
            mx = max_level(k, hall, hero_hall)
            have = ", ".join(("not built yet" if lv == 0 else f"lvl {lv}") + (f" ×{c}" if c > 1 else "")
                             + (" ⏳" if up else "") for lv, c, up in levels) or "level unknown"
            self._cards[k] = self._card(k, e, have, cur, mx)
        self.draw_cards()

    def draw_cards(self):
        """Filter / search: just show or hide the cards already built."""
        cards = getattr(self, "_cards", {})
        for c in cards.values():
            c["frame"].grid_remove()
        self._empty.grid_remove()
        cat, q = self.cat.get().lower(), self.search.get().strip().lower()
        order = [c.lower() for c in PLAN_CATS[1:]]
        keys = sorted((k for k, c in cards.items() if (cat == "all" or c["cat"] == cat) and (not q or q in c["name"])),
                      key=lambda k: (order.index(cards[k]["cat"]) if cards[k]["cat"] in order else 99,
                                     cards[k]["name"]))
        for n, k in enumerate(keys):
            cards[k]["frame"].grid(row=n // 3, column=n % 3, sticky="nsew", padx=S(4), pady=S(4))
        if not keys:
            self._empty.grid(row=0, column=0, columnspan=3, sticky="w", pady=S(10))
        self.update_chips()
        self.cards.canvas.yview_moveto(0)

    def _card(self, k, e, have, cur, mx):
        """One card = ONE canvas (icon, text and level chips are canvas items): a widget per label made the
        window take seconds to show 50 cards."""
        cv = tk.Canvas(self.cards, bg=CARD, highlightthickness=0, width=S(300), height=S(10))
        pad, right = S(10), S(290)
        ic = self.icon(k, S(56))
        if ic:
            cv.create_image(pad, pad, image=ic, anchor="nw")
        tx = pad + S(64)
        y = cv.bbox(cv.create_text(tx, pad, text=e["name"], anchor="nw", fill=TEXT,
                                   font=("Segoe UI", 11, "bold"), width=right - tx))[3]
        y = cv.bbox(cv.create_text(tx, y + S(2), text=have, anchor="nw", fill=MUTED, font=("Segoe UI", 9),
                                   width=right - tx))[3]
        if mx:
            y = cv.bbox(cv.create_text(tx, y + S(2), text=f"max {mx} at this hall", anchor="nw", fill=MUTED,
                                       font=("Segoe UI", 9)))[3]
        y = max(y, pad + S(56)) + S(8)
        info = {"frame": cv, "chips": [], "x": None, "name": e["name"].lower(), "cat": category_of(k.split(":", 1)[1])}
        if cur is None or mx is None or cur >= mx:
            y = cv.bbox(cv.create_text(pad, y, anchor="nw", fill=MUTED, font=("Segoe UI", 9), text=(
                "✓ maxed for this hall" if cur is not None and mx and cur >= mx else "(level couldn't be read)")))[3]
            cv.configure(height=y + pad)
            return info
        x = [pad]

        def chip(text, cmd, line):
            t = cv.create_text(x[0] + S(7), line + S(3), text=text, anchor="nw", fill=TEXT,
                               font=("Segoe UI", 9, "bold"))
            x0, y0, x1, y1 = cv.bbox(t)
            r = cv.create_rectangle(x0 - S(7), y0 - S(3), x1 + S(7), y1 + S(3), fill="#3a3a3a", width=0)
            cv.tag_lower(r, t)
            for item in (t, r):
                cv.tag_bind(item, "<Button-1>", lambda ev: cmd())
                cv.tag_bind(item, "<Enter>", lambda ev: cv.configure(cursor="hand2"))
                cv.tag_bind(item, "<Leave>", lambda ev: cv.configure(cursor=""))
            x[0] = x1 + S(11)
            return r, t
        line = y
        x[0] = cv.bbox(cv.create_text(pad, line + S(3), text="Up to:", anchor="nw", fill=MUTED,
                                      font=("Segoe UI", 9)))[2] + S(6)
        targets = list(range(cur + 1, mx + 1))
        many = len(targets) > 6  # heroes have dozens of levels: next few + max, plus a picker for any level
        if many:
            targets = targets[:4] + [mx]
        for lv in targets:
            info["chips"].append((lv, chip(("MAX " if lv == mx else "") + str(lv), lambda lv=lv: self.add(k, lv),
                                           line)))
        info["x"] = chip("✕", lambda: self.remove_key(k), line)
        y = cv.bbox("all")[3] + S(8)
        if many:
            x[0] = cv.bbox(cv.create_text(pad, y + S(3), text="or level", anchor="nw", fill=MUTED,
                                          font=("Segoe UI", 9)))[2] + S(6)
            sp = tk.Spinbox(cv, from_=cur + 1, to=mx, width=5, bg="#3a3a3a", fg=TEXT, buttonbackground=CARD,
                            relief="flat", insertbackground=TEXT)
            x[0] = cv.bbox(cv.create_window(x[0], y, window=sp, anchor="nw"))[2] + S(6)
            chip("Set", lambda: self.add(k, min(mx, max(cur + 1, int(sp.get())))) if sp.get().isdigit() else None, y)
            y = cv.bbox("all")[3] + S(4)
        cv.configure(height=y + pad)
        return info

    def update_chips(self, keys=None):
        """Highlight the chosen target levels (only the cards that changed)."""
        want = {it["key"]: it["to"] for it in self.plan()}
        cards = getattr(self, "_cards", {})
        for k in (keys if keys is not None else cards):
            c = cards.get(k)
            if not c:
                continue
            for lv, (r, t) in c["chips"]:
                on = want.get(k, 0) >= lv
                c["frame"].itemconfigure(r, fill=BLUE if on else "#3a3a3a")
                c["frame"].itemconfigure(t, fill="#0b1a2b" if on else TEXT)
            if c["x"] is not None:
                for item in c["x"]:
                    c["frame"].itemconfigure(item, state="normal" if k in want else "hidden")

    # --- editing (saved straight away; the bot reads it on its next builder check) ---
    def add(self, key, to):
        plan = self.plan()
        it = next((x for x in plan if x["key"] == key), None)
        if it:
            it["to"] = to
        else:
            plan.append({"key": key, "to": to})
        self.save([key])

    def remove(self, i):
        plan = self.plan()
        if 0 <= i < len(plan):
            key = plan.pop(i)["key"]
            self.save([key])

    def remove_key(self, key):
        plan = self.plan()
        plan[:] = [x for x in plan if x["key"] != key]
        self.save([key])

    def move(self, i, d):
        plan = self.plan()
        if 0 <= i + d < len(plan):
            plan[i], plan[i + d] = plan[i + d], plan[i]
            self.save([])

    def clear(self):
        if self.plan() and messagebox.askyesno("Clear", f"Remove every target for {self.acc.get()} "
                                                        f"({'Builder Base' if self.base.get() == 'builder' else 'home'})?",
                                               parent=self):
            self.plan().clear()
            self.save(None)

    def save(self, keys=None):
        save_config(self.cfg)
        self.draw_queue()
        self.update_chips(keys)

    def scan(self):
        if self.app.thread:
            return messagebox.showinfo("Scan", "Farming is running - the bot scans each account by itself whenever "
                                               "it checks the builders. Stop farming to scan right now.", parent=self)
        self.info.config(text="Scanning - reading the builder list in the game…")

        def work():
            try:
                acc, kind = Bot(self.cfg, self.app.adb, self.app.emit).scan_now()
            except Exception as e:
                msg = f"Scan failed: {e}"
                return self.app.ui(lambda: self.info.config(text=msg))

            def done():
                if self.winfo_exists():
                    self.accounts()
                    self.acc.set(acc)
                    self.base.set("builder" if kind.startswith("bb_") else "home")
                    self.refresh()
            self.app.ui(done)
        self.app.bg(work)


class CaptureWindow(tk.Toplevel):
    """Shows a screenshot; drag a box. Calls on_done(crop, center, bbox) in full-res coords."""

    def __init__(self, parent, frame, prompt, on_done):
        super().__init__(parent)
        self.title("Capture")
        self.configure(bg=BG)
        self.frame, self.on_done = frame, on_done
        h, w = frame.shape[:2]
        self.scale = min(S(1200) / w, S(680) / h, 1.0)
        ttk.Label(self, text=prompt + "   (Esc to cancel)", padding=S(12, 10)).pack(anchor="w")
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
            (int(w * self.scale), int(h * self.scale)), Image.LANCZOS)
        self.photo = ImageTk.PhotoImage(img)
        self.cv = tk.Canvas(self, width=img.width, height=img.height, cursor="crosshair",
                            highlightthickness=0, bg=BG)
        self.cv.pack(padx=S(12), pady=S(0, 12))
        self.cv.create_image(0, 0, anchor="nw", image=self.photo)
        self.start, self.rect = None, None
        self.cv.bind("<ButtonPress-1>", self._press)
        self.cv.bind("<B1-Motion>", self._drag)
        self.cv.bind("<ButtonRelease-1>", self._release)
        self.bind("<Escape>", lambda e: self.destroy())
        self.transient(parent)
        self.grab_set()
        self.focus_set()

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.cv.delete(self.rect)
        self.rect = self.cv.create_rectangle(e.x, e.y, e.x, e.y, outline=GREEN, width=2)

    def _drag(self, e):
        if self.start:
            self.cv.coords(self.rect, *self.start, e.x, e.y)

    def _release(self, e):
        if not self.start:
            return
        (x0, x1), (y0, y1) = sorted((self.start[0], e.x)), sorted((self.start[1], e.y))
        self.destroy()
        if x1 - x0 < 4 or y1 - y0 < 4:
            return
        b = [int(v / self.scale) for v in (x0, y0, x1, y1)]
        self.on_done(self.frame[b[1]:b[3], b[0]:b[2]].copy(), ((b[0] + b[2]) // 2, (b[1] + b[3]) // 2), b)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        global UI_SCALE
        UI_SCALE = self.winfo_fpixels("1i") / 96
        self.title("Loot Farmer")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        self.geometry(f"{min(S(1240), int(sw * .92))}x{min(S(800), int(sh * .85))}+{int(sw * .04)}+{int(sh * .03)}")
        self.minsize(min(S(1060), int(sw * .8)), min(S(680), int(sh * .7)))
        sv_ttk.set_theme("dark")
        import tkinter.font as tkfont
        for name in tkfont.names(self):  # the theme sizes fonts in pixels; scale them for high-DPI screens
            f = tkfont.nametofont(name, self)
            if name.startswith("SunValley") and f.cget("size") < 0:
                f.configure(size=int(f.cget("size") * UI_SCALE))
        self.configure(bg=BG)
        self.cfg, warn = load_config()
        self.adb = ADB(self.cfg["adb_path"], self.cfg["device"])
        self.q = queue.Queue()
        self.bot = self.thread = None
        self.started_at = None
        self.vision = Vision()
        self.recent = collections.deque(maxlen=10)
        self.phone, self.phone_url, self.tunnel = None, "", None
        if not self.cfg["phone_view_key"]:
            self.cfg["phone_view_key"] = secrets.token_urlsafe(9)
            save_config(self.cfg)
        if self.cfg["phone_view_enabled"]:
            key, port = self.cfg["phone_view_key"], self.cfg["phone_view_port"]
            try:
                self.phone = PhoneView(port, key)
                self.phone_url = f"http://{lan_ip()}:{port}/?k={key}"
                exe = os.path.join(BASE_DIR, "cloudflared.exe")
                if self.cfg["public_link_enabled"] and os.path.isfile(exe):
                    self.tunnel = Tunnel(exe, port, lambda url: self.ui(lambda: self._public_url(url)))
                else:
                    Tunnel.kill_saved()
            except OSError as e:
                warn = f"Phone view couldn't start on port {self.cfg['phone_view_port']} ({e})."
        self._styles()
        self._build()
        self.refresh_setup()
        self.after(100, self._pump)
        self.protocol("WM_DELETE_WINDOW", self._close)
        if warn:
            self.log(warn, "warn")
        self.bg(self._initial_connect)
        self.after(3000, self._update_tick)

    # --- helpers ---
    def emit(self, kind, data):
        self.q.put((kind, data))

    def bg(self, fn, *a):
        """Run fn on a worker thread; exceptions go to the log instead of freezing the UI."""
        def wrap():
            try:
                fn(*a)
            except Exception as e:
                self.emit("log", ("err", f"{getattr(fn, '__name__', 'task')}: {e}"))
        threading.Thread(target=wrap, daemon=True).start()

    def ui(self, fn):
        self.q.put(("call", fn))

    def log(self, msg, level="info"):
        self.emit("log", (level, msg))

    def _styles(self):
        s = ttk.Style(self)
        f = "Segoe UI Variable Display" if "Segoe UI Variable Display" in self.tk.call("font", "families") else "Segoe UI"
        s.configure("Title.TLabel", font=(f, 20, "bold"))
        s.configure("Sub.TLabel", font=("Segoe UI", 10), foreground=MUTED)
        s.configure("CardTitle.TLabel", font=("Segoe UI", 9, "bold"), foreground=MUTED)
        s.configure("CardValue.TLabel", font=(f, 22, "bold"))
        s.configure("Big.TLabel", font=(f, 15, "bold"))
        s.configure("Res.TLabel", font=(f, 17, "bold"))
        s.configure("Muted.TLabel", foreground=MUTED)
        s.configure("Pill.TLabel", font=("Segoe UI", 10, "bold"))
        s.configure("Start.Accent.TButton", font=("Segoe UI", 11, "bold"), padding=S(22, 9))
        s.configure("Treeview", rowheight=S(30))

    def card(self, parent, title, **grid):
        f = ttk.Frame(parent, style="Card.TFrame", padding=S(16, 12, 16, 14))
        f.grid(**grid, sticky="nsew")
        if title:
            ttk.Label(f, text=title.upper(), style="CardTitle.TLabel").pack(anchor="w", pady=S(0, 6))
        return f

    def text_widget(self, parent, height):
        t = tk.Text(parent, height=height, bg="#141414", fg=TEXT, insertbackground=TEXT, relief="flat",
                    font=("Cascadia Mono", 9) if "Cascadia Mono" in self.tk.call("font", "families")
                    else ("Consolas", 9), padx=S(10), pady=S(8), wrap="word", borderwidth=0, highlightthickness=0)
        for lvl, col in LEVEL_COLORS.items():
            t.tag_configure(lvl, foreground=col)
        t.tag_configure("ts", foreground="#6b6b6b")
        t.configure(state="disabled")
        return t

    # --- layout ---
    def _build(self):
        head = ttk.Frame(self, padding=S(24, 18, 24, 6))
        head.pack(fill="x")
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text="⚔  Loot Farmer", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text="Clash of Clans  ·  unattended resource farming", style="Sub.TLabel").pack(anchor="w")
        if self.phone_url:
            link = ttk.Label(left, text="📱  Home Wi-Fi link  (click to copy)", style="Sub.TLabel",
                             foreground=BLUE, cursor="hand2")
            link.pack(anchor="w", pady=S(4, 0))
            link.bind("<Button-1>", lambda e: self._copy(self.phone_url, "Home Wi-Fi link"))
            self.public_url = ""
            self.public_label = ttk.Label(left, text="🌍  Anywhere link: starting…" if self.tunnel else
                                          "🌍  Anywhere link: add cloudflared.exe next to bot.py",
                                          style="Sub.TLabel", foreground=BLUE if self.tunnel else MUTED,
                                          cursor="hand2")
            self.public_label.pack(anchor="w")
            self.public_label.bind("<Button-1>", lambda e: self.public_url and self._copy(
                self.public_url, "Anywhere link"))
        rep = ttk.Label(left, text="🐞  Send debug report to Discord", style="Sub.TLabel", foreground=BLUE,
                        cursor="hand2")
        rep.pack(anchor="w")
        rep.bind("<Button-1>", lambda e: self.send_report("sent by hand"))
        ver = ttk.Label(left, text=f"🕘  Version {APP_VERSION} - switch version", style="Sub.TLabel", foreground=BLUE,
                        cursor="hand2")
        ver.pack(anchor="w")
        ver.bind("<Button-1>", lambda e: self.pick_version())
        pln = ttk.Label(left, text="🏗  Upgrade planner (per account)", style="Sub.TLabel", foreground=BLUE,
                        cursor="hand2")
        pln.pack(anchor="w")
        pln.bind("<Button-1>", lambda e: self.open_planner())

        right = ttk.Frame(head)
        right.pack(side="right")
        self.start_btn = ttk.Button(right, text="▶  Start farming", style="Start.Accent.TButton",
                                    command=self.toggle, width=18)
        self.start_btn.pack(side="right", padx=S(14, 0))
        ttk.Button(right, text="⟳  Restart app", command=self.restart_app).pack(side="right", padx=S(10, 0))
        self.update_btn = ttk.Button(left, text="⬆  Update available - click to update", style="Accent.TButton",
                                     command=self.do_update)  # under the title: the right-hand row is full
        self._update = None  # shown only when GitHub has a newer version
        self.loot_btn = ttk.Button(right, text="💰  Loot only", command=lambda: self.toggle("loot"), width=14)
        self.loot_btn.pack(side="right", padx=S(10, 0))
        self.pill = ttk.Label(right, text="●  Connecting…", style="Pill.TLabel", foreground=AMBER)
        self.batt_label = ttk.Label(right, text="", style="Pill.TLabel", foreground=MUTED)
        self.batt_label.pack(side="right", padx=S(4, 10))
        self._batt_t, self._batt_warned = 0.0, False
        self.pill.pack(side="right", padx=S(14))
        ttk.Button(right, text="↻", width=3, command=lambda: self.bg(self._initial_connect)).pack(side="right")
        self.dev_combo = ttk.Combobox(right, width=15, state="readonly")
        self.dev_combo.pack(side="right", padx=S(6))
        self.dev_combo.bind("<<ComboboxSelected>>", self._device_chosen)

        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=S(20), pady=S(8, 18))
        tabs = {}
        for name in ("Dashboard", "Setup", "Settings", "Log"):
            tabs[name] = ttk.Frame(nb, padding=S(14))
            nb.add(tabs[name], text=f"  {name}  ")
        self._build_dashboard(tabs["Dashboard"])
        self._build_setup(tabs["Setup"])
        self._build_settings(tabs["Settings"])
        self._build_log(tabs["Log"])

    def _build_dashboard(self, tab):
        tab.columnconfigure(0, weight=3)
        tab.columnconfigure(1, weight=2)
        tab.rowconfigure(1, weight=1)
        stats = ttk.Frame(tab)
        stats.grid(row=0, column=0, columnspan=2, sticky="ew", pady=S(0, 12))
        self.stat_labels = {}
        for i, (key, title) in enumerate([("runtime", "Runtime"), ("attacks", "Attacks"),
                                          ("skipped", "Bases skipped"), ("walls", "Walls bought"),
                                          ("upgrades", "Upgrades"),
                                          ("switches", "Switches"), ("recoveries", "Recoveries"),
                                          ("errors", "Errors")]):
            stats.columnconfigure(i, weight=1, uniform="s")
            c = self.card(stats, title, row=0, column=i, padx=S(0 if i == 0 else 6, 0))
            self.stat_labels[key] = ttk.Label(c, text="0" if key != "runtime" else "—", style="CardValue.TLabel")
            self.stat_labels[key].pack(anchor="w")

        live = self.card(tab, "Live view", row=1, column=0, padx=S(0, 6))
        top = ttk.Frame(live, style="Card.TFrame")
        top.pack(fill="x", pady=S(0, 8))
        self.state_label = ttk.Label(top, text="Idle", style="Big.TLabel", foreground=MUTED)
        self.state_label.pack(side="left")
        self.bb_var = tk.BooleanVar(value=bool(self.cfg["builder_base_enabled"]))

        def bb_toggled():
            self.cfg["builder_base_enabled"] = self.bb_var.get()  # the running bot shares this dict: live
            if "builder_base_enabled" in getattr(self, "svars", {}):
                self.svars["builder_base_enabled"][0].set(self.bb_var.get())
            save_config(self.cfg)
            self.log("Builder Base " + ("on: each account gets a visit (collect, cart, upgrades, daily stars)."
                                        if self.bb_var.get() else "off."), "ok")
        ttk.Checkbutton(top, text="🏗  Builder Base", variable=self.bb_var, command=bb_toggled,
                        style="Switch.TCheckbutton").pack(side="right")
        self.preview = tk.Canvas(live, bg="#141414", highlightthickness=0, height=S(240))
        self.preview.pack(fill="both", expand=True)
        self.preview.create_text(10, 10, anchor="nw", text="The emulator screen appears here while farming.",
                                 fill=MUTED, font=("Segoe UI", 10), tags="hint")
        self._photo = None

        side = ttk.Frame(tab)
        side.grid(row=1, column=1, rowspan=2, sticky="nsew", padx=S(6, 0))
        side.columnconfigure(0, weight=1)
        side.rowconfigure(1, weight=1)
        res = self.card(side, "Resources", row=0, column=0, pady=S(0, 12))
        grid = ttk.Frame(res)
        grid.pack(fill="x")
        grid.columnconfigure(0, weight=1, uniform="r")
        grid.columnconfigure(1, weight=1, uniform="r")
        self.res_labels = {}
        for i, (key, label, col) in enumerate([("s_gold", "Your gold", GOLD), ("s_elixir", "Your elixir", PINK),
                                               ("b_gold", "Last base gold", GOLD),
                                               ("b_elixir", "Last base elixir", PINK)]):
            cell = ttk.Frame(grid)
            cell.grid(row=i // 2, column=i % 2, sticky="ew", pady=S(4))
            ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
            self.res_labels[key] = ttk.Label(cell, text="—", foreground=col, style="Res.TLabel")
            self.res_labels[key].pack(anchor="w")
        lc = self.card(side, "Loot this session (per account)", row=1, column=0, pady=S(0, 12))
        self.loot_tree = self._tree(lc, ("Account", "Gold", "Gold/hr", "Elixir", "Elixir/hr", "Dark", "Dark/hr", "Att."),
                                    (84, 56, 58, 56, 62, 46, 56, 34), 4)
        self.loot_tree.pack(fill="both", expand=True)
        mc = self.card(side, "Time to max (this hall, builders only)", row=2, column=0, pady=S(0, 12))
        self.max_tree = self._tree(mc, ("Account", "Village", "Hall", "Upgrades", "Builder time", "Days"),
                                   (84, 70, 40, 64, 84, 56), 4)
        self.max_tree.pack(fill="both", expand=True)
        self.render_max()
        act = self.card(tab, "Activity", row=2, column=0, padx=S(0, 6), pady=S(12, 0))
        self.mini_log = self.text_widget(act, 5)
        self.mini_log.pack(fill="both", expand=True)

    def _tree(self, parent, cols, widths, height):
        t = ttk.Treeview(parent, columns=cols, show="headings", height=height, selectmode="browse")
        for c, w in zip(cols, widths):
            t.heading(c, text=c)
            t.column(c, width=S(w), anchor="w", stretch=c == cols[0])
        t.tag_configure("ok", foreground=GREEN)
        t.tag_configure("missing", foreground=RED)
        t.tag_configure("optional", foreground=MUTED)
        return t

    def _build_setup(self, tab):
        tab.columnconfigure(0, weight=1)
        tab.columnconfigure(1, weight=1)
        tab.rowconfigure(1, weight=1)
        bar = ttk.Frame(tab)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=S(0, 10))
        ttk.Label(bar, text="Put the game on the right screen, select a row, then Capture.",
                  style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Run setup check", command=lambda: self.bg(self.setup_check)).pack(side="right")
        ttk.Button(bar, text="Ask Groq", command=self.ask_groq).pack(side="right", padx=S(8, 0))
        ttk.Button(bar, text="Test wall (elixir)", command=lambda: self.test_wall("elixir")).pack(
            side="right", padx=S(8, 0))
        ttk.Button(bar, text="Read troop bar", command=self.read_bar).pack(side="right", padx=S(8, 0))
        ttk.Button(bar, text="Test switch", command=self.test_switch).pack(side="right", padx=S(8, 0))
        ttk.Button(bar, text="Test lab", command=lambda: self.test_upgrade("lab")).pack(side="right", padx=S(8, 0))
        ttk.Button(bar, text="Test builder", command=lambda: self.test_upgrade("builder")).pack(
            side="right", padx=S(8, 0))
        ttk.Button(bar, text="Test wall (gold)", command=lambda: self.test_wall("gold")).pack(
            side="right", padx=S(8, 0))
        ttk.Button(bar, text="Preview screen", command=self.preview_screen).pack(side="right", padx=S(8))

        body = ttk.Frame(tab)
        body.grid(row=1, column=0, columnspan=2, sticky="nsew")
        area = self._scroll_area(body)
        area.columnconfigure(0, weight=1, uniform="c")
        area.columnconfigure(1, weight=1, uniform="c")
        lcol = ttk.Frame(area)
        lcol.grid(row=0, column=0, sticky="new", padx=S(0, 6))
        rcol = ttk.Frame(area)
        rcol.grid(row=0, column=1, sticky="new", padx=S(6, 12))
        for col in (lcol, rcol):
            col.columnconfigure(0, weight=1)

        c = self.card(lcol, "Buttons", row=0, column=0, pady=S(0, 12))
        self.btn_tree = self._tree(c, ("Button", "Status", "Match"), (250, 110, 70), len(BUTTONS))
        self.btn_tree.pack(fill="x")
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=S(10, 0))
        ttk.Button(row, text="Capture", style="Accent.TButton", command=self.capture_button).pack(side="left")
        ttk.Button(row, text="Test match", command=self.test_button).pack(side="left", padx=S(8))

        c = self.card(rcol, "Text regions (OCR)", row=0, column=0, pady=S(0, 12))
        self.ocr_tree = self._tree(c, ("Region", "Status", "Last read"), (210, 100, 110), len(OCR_REGIONS))
        self.ocr_tree.pack(fill="x")
        row = ttk.Frame(c, style="Card.TFrame")
        row.pack(fill="x", pady=S(10, 0))
        ttk.Button(row, text="Capture", style="Accent.TButton", command=self.capture_region).pack(side="left")
        ttk.Button(row, text="Test read", command=self.test_region).pack(side="left", padx=S(8))

        c = self.card(rcol, "Screen points", row=1, column=0, pady=S(0, 12))
        self.pt_tree = self._tree(c, ("Point", "Position"), (230, 170), len(POINTS))
        self.pt_tree.pack(fill="x")
        row = ttk.Frame(c)
        row.pack(fill="x", pady=S(10, 0))
        ttk.Button(row, text="Capture", style="Accent.TButton", command=self.capture_point).pack(side="left")
        ttk.Button(row, text="Set troop line",
                   command=lambda: self.with_screenshot(lambda f: DropLinePicker(self, f))).pack(side="left", padx=S(8))
        ttk.Button(row, text="Set spell line", command=lambda: self.with_screenshot(lambda f: DropLinePicker(
            self, f, ("spell_point", "spell_line_end"), "Spell"))).pack(side="left")


    def _scroll_area(self, parent):
        canvas = tk.Canvas(parent, highlightthickness=0, bg=BG)
        sb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        win = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        canvas.configure(yscrollcommand=sb.set)
        wheel = lambda e: canvas.yview_scroll(-int(e.delta / 120), "units")
        canvas.bind("<Enter>", lambda e: canvas.bind_all("<MouseWheel>", wheel))
        canvas.bind("<Leave>", lambda e: canvas.unbind_all("<MouseWheel>"))
        sb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        return inner

    def _build_settings(self, tab):
        foot = ttk.Frame(tab)
        foot.pack(side="bottom", fill="x", pady=S(10, 0))
        ttk.Button(foot, text="Save settings", style="Accent.TButton", command=self.save_settings).pack(side="right")
        ttk.Button(foot, text="Discard changes", command=self.load_settings_vars).pack(side="right", padx=S(8))
        ttk.Label(foot, text="Changes apply immediately, even while farming.", style="Muted.TLabel").pack(side="left")
        area = self._scroll_area(tab)
        cols = [ttk.Frame(area), ttk.Frame(area)]
        for i, col in enumerate(cols):
            area.columnconfigure(i, weight=1, uniform="g")
            col.grid(row=0, column=i, sticky="new")
        self.svars = {}
        heights = [0, 0]
        for group, blurb, fields in SETTINGS:
            col = heights.index(min(heights))
            heights[col] += len(fields) + 2
            c = ttk.Frame(cols[col], style="Card.TFrame", padding=S(16, 12, 16, 14))
            c.pack(fill="x", padx=S(6), pady=S(6))
            c.columnconfigure(1, weight=1)
            ttk.Label(c, text=group, style="Big.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
            ttk.Label(c, text=blurb, style="Muted.TLabel", wraplength=S(470)).grid(row=1, column=0, columnspan=2, sticky="w",
                                                                pady=S(0, 8))
            for r, (key, label, kind) in enumerate(fields, start=2):
                if kind is bool:
                    v = tk.BooleanVar()
                    ttk.Checkbutton(c, text=label, variable=v, style="Switch.TCheckbutton").grid(
                        row=r, column=0, columnspan=2, sticky="w", pady=S(3))
                else:
                    v = tk.StringVar()
                    ttk.Label(c, text=label).grid(row=r, column=0, sticky="w", pady=S(3), padx=S(0, 12))
                    ttk.Entry(c, textvariable=v, show="•" if kind == "secret" else "").grid(
                        row=r, column=1, sticky="ew", pady=S(3))
                self.svars[key] = (v, kind)
        self.load_settings_vars()

    def _build_log(self, tab):
        bar = ttk.Frame(tab)
        bar.pack(fill="x", pady=S(0, 10))
        ttk.Label(bar, text=f"Also saved to {LOG_FILE}", style="Muted.TLabel").pack(side="left")
        ttk.Button(bar, text="Open log file", command=lambda: os.startfile(LOG_FILE)).pack(side="right")
        ttk.Button(bar, text="Clear", command=self._clear_log).pack(side="right", padx=S(8))
        self.full_log = self.text_widget(tab, 20)
        sb = ttk.Scrollbar(tab, command=self.full_log.yview)
        self.full_log.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.full_log.pack(fill="both", expand=True)

    # --- event pump (the only place the UI is updated from bot/background work) ---
    def report_callback_exception(self, exc, val, tb):
        """Tk errors would go to a console pythonw doesn't have: log them and keep the window alive."""
        log_file.error("UI error:\n" + "".join(traceback.format_exception(exc, val, tb)))

    def _pump(self):
        self.after(100, self._pump)  # re-arm first: one bad message must never freeze the window for good
        frame = None
        try:
            for _ in range(500):
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self._append_log(*data)
                    if data[0] == "err" and time.time() - getattr(self, "_report_t", 0) > 900:
                        self.send_report(f"error: {data[1][:300]}", auto=True)
                elif kind == "state":
                    col = RED if data == "Recovering" else MUTED if data in ("Stopped", "Idle") else GREEN
                    self.state_label.config(text=data, foreground=col)
                elif kind == "stats":
                    for k, v in data.items():
                        self.stat_labels[k].config(text=f"{v:,}")
                elif kind == "frame":
                    frame = data
                elif kind == "device":
                    self._set_pill(data)
                elif kind == "devices":
                    self.dev_combo["values"] = data
                    if self.adb.device in data:
                        self.dev_combo.set(self.adb.device)
                elif kind == "storage":
                    for k, v in data.items():
                        if v is not None:
                            self.res_labels["s_" + k].config(text=f"{v:,}")
                elif kind == "loot":
                    self.loot_rows = data
                    self.render_loot()
                elif kind == "base":
                    for k, v in zip(("b_gold", "b_elixir"), data):
                        self.res_labels[k].config(text="?" if v is None else f"{v:,}")
                elif kind == "call":
                    data()
                elif kind == "scan":
                    self.render_max()
                    p = getattr(self, "planner", None)
                    if p is not None and p.winfo_exists():
                        p.refresh()
        except queue.Empty:
            pass
        if frame is not None:
            self._show_frame(frame)
            if self.phone:
                self.phone.jpeg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()
        if time.time() - self._batt_t > 15:
            self._batt_t = time.time()
            b = battery()
            if b:
                pct, plugged = b
                self.batt_label.config(text=f"🔋 {pct}%" + (" ⚡" if plugged else ""),
                                       foreground=RED if pct <= 20 and not plugged else
                                       AMBER if not plugged else GREEN)
                if not plugged and pct <= 20 and not self._batt_warned:
                    self.log(f"Laptop battery at {pct}% and unplugged - plug it in or farming will stop.", "err")
                self._batt_warned = not plugged and pct <= 20
        if self.started_at and time.time() - getattr(self, "_loot_t", 0) > 5:  # keep the /hr rates current
            self._loot_t = time.time()
            self.render_loot()
        if self.phone:
            st = {k: lbl.cget("text") for k, lbl in {**self.stat_labels, **self.res_labels}.items()}
            st["battery"] = self.batt_label.cget("text").replace("🔋 ", "") or "-"
            st["loot"] = getattr(self, "loot_view", [])
            st.update(state=self.state_label.cget("text"), log=list(self.recent))
            self.phone.status = st
        if self.started_at:
            s = int(time.time() - self.started_at)
            self.stat_labels["runtime"].config(text=f"{s // 3600}h {s // 60 % 60:02d}m")
        if self.thread and not self.thread.is_alive():
            self.thread = self.bot = None
            self.started_at = None
            self.start_btn.config(text="▶  Start farming", state="normal")
            self.loot_btn.config(text="💰  Loot only", state="normal")

    def _append_log(self, level, msg):
        ts = time.strftime("%H:%M:%S ")
        self.recent.append(ts + msg.splitlines()[0])
        for t, cap in ((self.full_log, 3000), (self.mini_log, 200)):
            t.configure(state="normal")
            t.insert("end", ts, "ts")
            t.insert("end", msg + "\n", level)
            n = int(t.index("end-1c").split(".")[0])
            if n > cap:
                t.delete("1.0", f"{n - cap}.0")
            t.see("end")
            t.configure(state="disabled")

    def _clear_log(self):
        self.full_log.configure(state="normal")
        self.full_log.delete("1.0", "end")
        self.full_log.configure(state="disabled")

    def _show_frame(self, frame):
        cw, ch = max(self.preview.winfo_width(), 50), max(self.preview.winfo_height(), 50)
        h, w = frame.shape[:2]
        s = min(cw / w, ch / h)
        img = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
            (max(1, int(w * s)), max(1, int(h * s))), Image.BILINEAR)
        self._photo = ImageTk.PhotoImage(img)
        self.preview.delete("all")
        self.preview.create_image(cw // 2, ch // 2, image=self._photo)

    def _copy(self, text, what):
        self.clipboard_clear()
        self.clipboard_append(text)
        self.log(f"{what} copied - paste it to yourself and open it on your phone: {text}", "ok")

    def _public_url(self, url):
        if url:
            new = f"{url}/?k={self.cfg['phone_view_key']}"
            if new != self.public_url:
                self.log(f"Anywhere link ready: {new}", "ok")
                self.bg(lambda: post_discord(self.cfg, f"🟢 **Loot Farmer v{APP_VERSION}** is running on "
                                                       f"**{os.environ.get('COMPUTERNAME', 'a PC')}**\n{new}"))
            self.public_url = new
            self.public_label.config(text="🌍  Anywhere link  (click to copy)", foreground=BLUE)
        else:
            self.public_url = ""
            self.public_label.config(text="🌍  Anywhere link: reconnecting…", foreground=AMBER)

    def _set_pill(self, ok):
        self.pill.config(text="●  Connected" if ok else "●  Offline", foreground=GREEN if ok else RED)

    # --- device ---
    def _initial_connect(self):
        devs = self.adb.devices()
        target = self.cfg["auto_connect_target"]
        if not devs and target:
            self.log(f"adb connect {target}: {self.adb.connect(target)}")
            devs = self.adb.devices()
        if devs and self.adb.device not in devs:
            self.adb.device = self.cfg["device"] if self.cfg["device"] in devs else devs[0]
        self.emit("devices", devs)
        self.emit("device", bool(devs) and self.adb.ready())
        self.log(f"Devices: {', '.join(devs) or 'none - is the emulator running with ADB enabled?'}",
                 "ok" if devs else "warn")

    def _device_chosen(self, _e=None):
        self.adb.device = self.cfg["device"] = self.dev_combo.get()
        self.adb.close_shell()
        save_config(self.cfg)
        self.bg(lambda: self.emit("device", self.adb.ready()))

    # --- start / stop ---
    def missing_setup(self):
        c = self.cfg
        miss = [f"Button: {d}" for n, d in BUTTONS if n in REQUIRED_BUTTONS and not os.path.exists(tpath(n))]
        need = ["damage_percent_region"] + ([] if c["loot_force_attack"] else ["loot_gold_region",
                                                                              "loot_elixir_region"])
        miss += [f"Text region: {d}" for n, d in OCR_REGIONS if n in need and n not in c["ocr_regions"]]
        if "deploy_point" not in c["fixed_points"]:
            miss.append("Screen point: troop drop point")
        return miss

    def toggle(self, mode="farm"):
        mine, other = (self.start_btn, self.loot_btn) if mode == "farm" else (self.loot_btn, self.start_btn)
        if self.thread:
            self.bot.stop_evt.set()
            self.start_btn.config(text="Stopping…", state="disabled")
            self.loot_btn.config(state="disabled")
            return
        miss = self.missing_setup()
        if miss:
            messagebox.showwarning("Setup not finished", "Finish these on the Setup tab first:\n\n"
                                   + "\n".join("•  " + m for m in miss))
            return
        self.reset_dashboard()
        self.bot = Bot(self.cfg, self.adb, self.emit, mode)
        self.thread = threading.Thread(target=self.bot.run, daemon=True)
        self.thread.start()
        self.started_at = time.time()
        if self.public_url:  # the start-up post can be missed (PC asleep, network not up yet): send it again
            url = self.public_url
            pc = os.environ.get("COMPUTERNAME", "a PC")
            self.bg(lambda: post_discord(self.cfg, f"▶️ Farming started on **{pc}**\n{url}"))
        mine.config(text="■  Stop farming" if mode == "farm" else "■  Stop looting")
        other.config(state="disabled")

    def render_loot(self):
        """Per-account session loot + per-hour rates (loot / time since Start), with an all-accounts total."""
        rows = getattr(self, "loot_rows", {})
        hrs = max((time.time() - self.started_at) / 3600, 1 / 60) if self.started_at else None
        short = lambda v: f"{v / 1e6:.2f}M" if v >= 1e6 else f"{v / 1e3:.0f}k" if v >= 1e3 else str(int(v))
        rate = lambda v: short(v / hrs) if hrs else "-"
        items = sorted(rows.items())
        if len(items) > 1:
            items.append(("All accounts", [sum(r[i] for _, r in items) for i in range(4)]))
        self.loot_view = [[acc, short(g), rate(g), short(e), rate(e), short(dk), rate(dk), n]
                          for acc, (g, e, dk, n) in items]
        self.loot_tree.delete(*self.loot_tree.get_children())
        for row in self.loot_view:
            self.loot_tree.insert("", "end", values=row)

    def render_max(self):
        """Per account + village: upgrade time left to max this hall, and the calendar days that is with every
        builder kept busy (no boosts / Builder Potions)."""
        self.max_tree.delete(*self.max_tree.get_children())
        tagged = set((self.cfg.get("account_tags") or {}).values())
        for acc, bases in sorted((self.cfg.get("scans") or {}).items()):
            if tagged and acc not in tagged and difflib.get_close_matches(acc, tagged, 1, 0.75):
                continue  # a misread name of a known account
            for base in ("home", "builder"):
                sd = bases.get(base)
                if not sd:
                    continue
                secs, n, builders = time_to_max(sd, base)
                days = secs / 86400
                self.max_tree.insert("", "end", values=(
                    acc, "Home" if base == "home" else "Builder", sd.get("hall") or "?", n,
                    f"{days:,.0f} d" if days >= 2 else f"{secs / 3600:,.0f} h",
                    f"{days / builders:,.1f}" if n else "✓ max"))

    def reset_dashboard(self):
        for k, lbl in self.stat_labels.items():
            lbl.config(text="0" if k != "runtime" else "0h 00m")
        for lbl in self.res_labels.values():
            lbl.config(text="—")
        self.loot_tree.delete(*self.loot_tree.get_children())
        self.loot_rows, self.loot_view = {}, []

    def _update_tick(self):
        """Check GitHub for a newer version now and every 30 minutes."""
        def work():
            man = check_update()
            if man:
                self.ui(lambda: self._show_update(man))
        self.bg(work)
        self.after(30 * 60 * 1000, self._update_tick)

    def _show_update(self, man):
        if not self._update:
            self.log(f"Update available: version {man['version']} (you have {APP_VERSION}). Click ⬆ Update.", "ok")
            self.update_btn.pack(anchor="w", pady=S(6, 0))
        self._update = man

    def do_update(self):
        if not self._update:
            return
        msg = "Download the update and restart Loot Farmer?"
        if self.thread:
            msg = "Farming is running - stop it, download the update and restart?"
        if not messagebox.askyesno("Update", msg + "\n\nYour settings and drop lines are kept."):
            return
        if self.thread:
            self.bot.stop_evt.set()
        self.update_btn.config(text="Updating…", state="disabled")

        def work():
            try:
                n = apply_update(self._update)
            except Exception as e:
                self.log(f"Update failed: {e}", "err")
                return self.ui(lambda: self.update_btn.config(text="⬆  Update available - click to update",
                                                              state="normal"))
            self.log(f"Updated {n} files to version {self._update['version']} - restarting.", "ok")
            self.ui(lambda: self.after(800, self._restart_now))
        self.bg(work)

    def open_planner(self):
        p = getattr(self, "planner", None)
        if p is not None and p.winfo_exists():
            return p.lift()
        self.planner = PlannerWindow(self)

    def pick_version(self):
        """List the published versions and install the chosen one (older or newer). Settings are kept."""
        self.log("Loading the version list from GitHub…")

        def work():
            try:
                vs = list_versions()
            except Exception as e:
                return self.log(f"Couldn't load the version list: {e}", "err")
            self.ui(lambda: self._version_window(vs))
        self.bg(work)

    def _version_window(self, vs):
        if not vs:
            return self.log("No published versions found.", "warn")
        w = tk.Toplevel(self)
        w.title("Switch version")
        w.configure(bg=BG)
        w.transient(self)
        f = ttk.Frame(w, padding=S(14))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text=f"You have version {APP_VERSION}. Pick one to install - your settings, drop lines and "
                          "accounts are kept.", style="Sub.TLabel").pack(anchor="w", pady=S(0, 8))
        tree = self._tree(f, ("Version", "Published", "Notes"), (80, 110, 360), min(len(vs), 12))
        tree.pack(fill="both", expand=True)
        for v, d, m, sha in vs:
            tree.insert("", "end", iid=sha, values=(f"v{v}" + ("  (current)" if v == APP_VERSION else ""), d, m))

        def go():
            sel = tree.selection()
            if not sel:
                return
            v = next(x[0] for x in vs if x[3] == sel[0])
            if not messagebox.askyesno("Switch version", f"Install version {v} and restart Loot Farmer?"
                                       + ("\n\nFarming will be stopped." if self.thread else "")
                                       + "\n\nAn 'Update available' button will offer the newest again."
                                       + ("\n\nWARNING: this is your publishing folder - changes you haven't "
                                          "published yet will be overwritten."
                                          if os.path.isdir(os.path.join(BASE_DIR, ".git")) else ""), parent=w):
                return
            if self.thread:
                self.bot.stop_evt.set()
            w.destroy()

            def work():
                try:
                    n = install_version(sel[0])
                except Exception as e:
                    return self.log(f"Switching version failed: {e}", "err")
                self.log(f"Installed version {v} ({n} files) - restarting.", "ok")
                self.ui(lambda: self.after(800, self._restart_now))
            self.bg(work)
        ttk.Button(f, text="Install selected version", style="Accent.TButton", command=go).pack(anchor="e", pady=S(10, 0))

    def _restart_now(self):
        self._close()
        exe = sys.executable
        if os.name == "nt" and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"
        subprocess.Popen([exe, os.path.abspath(__file__)], cwd=BASE_DIR, creationflags=0x00000008)

    def send_report(self, reason, auto=False):
        """bot.log, the debug screenshots, the live screen and the non-secret settings to the Discord webhook.
        Automatic ones (on errors) at most every 15 min so a repeating error can't flood the channel."""
        if not self.cfg.get("discord_webhook"):
            if not auto:
                self.log("No Discord webhook set (discord_webhook in bot.py / config.json).", "warn")
            return
        self._report_t = time.time()

        def work():
            files, hide = [], re.compile(r"k=[\w-]+")
            try:
                with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
                    files.append(("bot.log", hide.sub("k=<hidden>", "".join(f.readlines()[-400:])).encode()))
            except OSError:
                pass
            for name in ("debug_deploy.png", "debug_wall.png", "debug_boat.png"):
                path = os.path.join(BASE_DIR, name)
                if os.path.exists(path) and time.time() - os.path.getmtime(path) < 6 * 3600:
                    img = cv2.imread(path)
                    if img is not None:
                        files.append((name.replace(".png", ".jpg"), cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()))
            try:
                files.append(("screen_now.jpg", cv2.imencode(".jpg", self.adb.screenshot(),
                                                             [cv2.IMWRITE_JPEG_QUALITY, 85])[1].tobytes()))
            except Exception:
                pass
            secret = ("groq_api_key", "phone_view_key", "discord_webhook")
            files.append(("settings.json", json.dumps({k: v for k, v in self.cfg.items() if k not in secret},
                                                      indent=1).encode()))
            text = (f"🐞 **Debug report** from **{os.environ.get('COMPUTERNAME', 'a PC')}** (v{APP_VERSION}) - "
                    f"{reason}\nState: {self.state_label.cget('text')}")
            if post_discord(self.cfg, text, files):
                self.log(f"Debug report sent to Discord ({len(files)} files).", "ok")
            elif not auto:
                self.log("Couldn't send the debug report - details in bot.log.", "warn")
        self.bg(work)

    def restart_app(self):
        """Close and reopen the app so it picks up code changes (stops any farming first)."""
        if self.thread and not messagebox.askyesno("Restart", "Farming is running - stop it and restart the app?"):
            return
        self.log("Restarting…")
        self._close()
        exe = sys.executable
        if os.name == "nt" and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
            exe = exe[:-10] + "pythonw.exe"  # no console window
        subprocess.Popen([exe, os.path.abspath(__file__)], cwd=BASE_DIR, creationflags=0x00000008)  # detached

    def _close(self):
        if self.bot:
            self.bot.stop_evt.set()
            self.thread.join(timeout=3)
        self.adb.close_shell()
        if self.phone:
            self.phone.server.shutdown()
        # the Cloudflare tunnel is left running on purpose, so the next start reuses the same link
        self.destroy()

    # --- settings ---
    def load_settings_vars(self):
        for key, (v, kind) in self.svars.items():
            val = self.cfg.get(key, DEFAULTS.get(key))
            if kind is bool:
                v.set(bool(val))
            elif kind is int:
                v.set(str(int(float(val or 0))))
            else:
                v.set("" if val is None else str(val))

    def save_settings(self):
        new = {}
        for key, (v, kind) in self.svars.items():
            try:
                raw = v.get()
                new[key] = (bool(raw) if kind is bool else int(float(raw)) if kind is int
                            else float(raw) if kind is float else raw.strip())
            except (ValueError, tk.TclError):
                label = next(lbl for _, _, fs in SETTINGS for k, lbl, _ in fs if k == key)
                messagebox.showerror("Invalid value", f"'{label}' needs a number.")
                return
        self.cfg.update(new)
        if hasattr(self, "bb_var"):
            self.bb_var.set(bool(self.cfg["builder_base_enabled"]))
        self.adb.path = self.cfg["adb_path"]
        if HAVE_TESS:
            pytesseract.pytesseract.tesseract_cmd = self.cfg["tesseract_path"]
        save_config(self.cfg)
        self.log("Settings saved.", "ok")

    # --- setup tab ---
    def refresh_setup(self):
        for t in (self.btn_tree, self.ocr_tree, self.pt_tree):
            keep = {i: t.set(i) for i in t.get_children()}
            t.delete(*t.get_children())
            t.keep = keep
        for n, d in BUTTONS:
            ok = os.path.exists(tpath(n))
            tag = "ok" if ok else "missing" if n in REQUIRED_BUTTONS else "optional"
            self.btn_tree.insert("", "end", iid=n, tags=(tag,),
                                 values=(d, "✓ Ready" if ok else "✗ Missing" if tag == "missing" else "—  Not set",
                                         self.btn_tree.keep.get(n, {}).get("Match", "")))
        for n, d in OCR_REGIONS:
            ok = n in self.cfg["ocr_regions"]
            self.ocr_tree.insert("", "end", iid=n, tags=("ok" if ok else "optional",),
                                 values=(d, "✓ Set" if ok else "—  Not set",
                                         self.ocr_tree.keep.get(n, {}).get("Last read", "")))
        for n, d in POINTS:
            p = self.cfg["fixed_points"].get(n)
            self.pt_tree.insert("", "end", iid=n, tags=("ok" if p else "optional",),
                                values=(d, f"({p[0]}, {p[1]})" if p else "—  Not set"))

    def selected(self, tree, what):
        sel = tree.selection()
        if not sel:
            messagebox.showinfo("Select a row", f"Select a {what} in the list first.")
        return sel[0] if sel else None

    def with_screenshot(self, fn):
        """Screenshot off the UI thread, then fn(frame) back on it."""
        def work():
            try:
                frame = self.adb.screenshot()
            except ADBError as e:
                self.log(f"Screenshot failed: {e}", "err")
                return
            self.ui(lambda: fn(frame))
        self.bg(work)

    def capture_button(self):
        n = self.selected(self.btn_tree, "button")
        if n:
            self.with_screenshot(lambda f: CaptureWindow(
                self, f, f"Drag a tight box around: {dict(BUTTONS)[n]}", lambda c, ctr, b: self._save_button(n, c, ctr)))

    def _save_button(self, n, img, center):
        cv2.imwrite(tpath(n), img)
        self.cfg["coords"][n] = list(center)
        save_config(self.cfg)
        self.log(f"Saved button '{n}'.", "ok")
        self.refresh_setup()

    def test_button(self):
        n = self.selected(self.btn_tree, "button")
        if not n:
            return

        def done(frame):
            r = self.vision.score(frame, n)
            if r is None:
                return self.log(f"'{n}' has no image yet.", "warn")
            ok = r[0] >= self.cfg["match_confidence"]
            self.btn_tree.set(n, "Match", f"{r[0]:.2f}")
            self.log(f"'{n}': best match {r[0]:.2f} at ({r[1]}, {r[2]}) - "
                     + ("would be tapped." if ok else "below confidence, not on this screen?"),
                     "ok" if ok else "warn")
        self.with_screenshot(done)

    def capture_region(self):
        n = self.selected(self.ocr_tree, "region")
        if n:
            self.with_screenshot(lambda f: CaptureWindow(
                self, f, f"Drag a box around just the digits: {dict(OCR_REGIONS)[n]}",
                lambda c, ctr, b: self._save_region(n, b, f)))

    def _save_region(self, n, box, frame):
        self.cfg["ocr_regions"][n] = box
        save_config(self.cfg)
        self.refresh_setup()
        self._read_region(n, frame)

    def _read_region(self, n, frame):
        md = 1 if n == "damage_percent_region" else self.cfg["ocr_min_digits"]
        v = ocr_number(frame, self.cfg["ocr_regions"][n], self.cfg["ocr_region_padding_px"], md)
        self.ocr_tree.set(n, "Last read", "unreadable" if v is None else f"{v:,}")
        self.log(f"'{n}' reads: {'unreadable' if v is None else f'{v:,}'}", "warn" if v is None else "ok")

    def test_region(self):
        n = self.selected(self.ocr_tree, "region")
        if n and n not in self.cfg["ocr_regions"]:
            return self.log(f"Capture '{n}' first.", "warn")
        if n:
            self.with_screenshot(lambda f: self._read_region(n, f))

    def capture_point(self):
        n = self.selected(self.pt_tree, "point")
        if n:
            self.with_screenshot(lambda f: CaptureWindow(
                self, f, f"Drag a small box centred on: {dict(POINTS)[n]}",
                lambda c, ctr, b: self._save_point(n, ctr)))

    def _save_point(self, n, center):
        self.cfg["fixed_points"][n] = list(center)
        save_config(self.cfg)
        self.log(f"Saved point '{n}' at {center}.", "ok")
        self.refresh_setup()

    def preview_screen(self):
        def show(frame):
            win = tk.Toplevel(self)
            win.title("Emulator screen")
            win.configure(bg=BG)
            h, w = frame.shape[:2]
            s = min(S(1100) / w, S(640) / h, 1.0)
            win.photo = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).resize(
                (int(w * s), int(h * s)), Image.LANCZOS))
            ttk.Label(win, image=win.photo).pack(padx=S(12), pady=S(12))
            ttk.Label(win, text=f"{w} x {h}", style="Muted.TLabel").pack(pady=S(0, 10))
        self.with_screenshot(show)

    def read_bar(self):
        """Log what auto-deploy would do with the troop bar on screen (start a search first)."""
        def work():
            cards = troop_bar(self.adb.screenshot())
            if not cards:
                return self.log("No troop bar on screen - start a search (scouting screen) first.", "warn")
            for x, y, kind, n in cards:
                self.log(f"  card at x={x}: {kind}" + (f" x{n}" if n is not None else ""))
        self.bg(work)

    def test_switch(self):
        if self.thread:
            return messagebox.showinfo("Farming is running", "Stop farming first, then test.")
        if not messagebox.askyesno("Test account switch", "Switch to the next account now?\n\nStart from the "
                                   "home village."):
            return

        def work():
            b = Bot(self.cfg, self.adb, self.emit)
            b._acc_idx = self.cfg.get("_last_account_idx", -1)
            try:
                ok = b.switch_account()
            except (Abort, ADBError) as e:
                return self.log(f"Switch test stopped: {e}", "warn")
            self.cfg["_last_account_idx"] = b._acc_idx
            self.log("Switch test: done." if ok else "Switch test: failed (see above).", "ok" if ok else "warn")
        self.bg(work)

    def test_upgrade(self, kind):
        """Start one builder/lab upgrade now (most expensive affordable), ignoring the on/off switches."""
        if self.thread:
            return messagebox.showinfo("Farming is running", "Stop farming first, then test.")
        if not messagebox.askyesno("Test upgrade", f"Start the most expensive {kind} upgrade you can afford "
                                   "now?\n\nStart from the home village. Gems and magic items are never used."):
            return

        def work():
            self.log(f"Testing a {kind} upgrade…")
            try:
                Bot(self.cfg, self.adb, self.emit).upgrade_from_list(kind)
            except (Abort, ADBError) as e:
                self.log(f"Upgrade test stopped: {e}", "warn")
        self.bg(work)

    def test_wall(self, cur):
        """Buy exactly one wall upgrade now, ignoring the storage threshold."""
        if self.thread:
            return messagebox.showinfo("Farming is running", "Stop farming first, then test the wall upgrade.")
        if not messagebox.askyesno("Test wall upgrade", f"Buy ONE wall upgrade with {cur} now?\n\n"
                                   "Start from the home village. Gems are never spent."):
            return

        def work():
            self.log(f"Testing a {cur} wall upgrade…")
            try:
                Bot(self.cfg, self.adb, self.emit).buy_wall(cur)
            except (Abort, ADBError) as e:
                self.log(f"Wall test stopped: {e}", "warn")
        self.bg(work)

    def ask_groq(self):
        """Shows what the AI supervisor would decide on the current screen (it doesn't tap)."""
        def work():
            if not self.cfg.get("groq_api_key"):
                return self.log("Add your Groq API key in Settings > Engine first.", "warn")
            self.log("Asking Groq about the current screen…")
            t = time.time()
            r = groq_ask(self.cfg, self.adb.screenshot(), RESCUE_PROMPT)
            if r is None:
                return self.log("Groq didn't answer - check the key/model in Settings and bot.log.", "err")
            self.log(f"Groq ({time.time() - t:.1f}s): screen={r.get('screen')}, action={r.get('action')} "
                     f"{r.get('target')!r}, button={r.get('button')} - {r.get('reason')}", "ok")
        self.bg(work)

    def setup_check(self):
        self.log("Setup check…")
        for m in self.missing_setup():
            self.log(f"Missing - {m}", "warn")
        frame = self.adb.screenshot()
        h, w = frame.shape[:2]
        bad = [f"{n} {tuple(b)}" for n, b in self.cfg["ocr_regions"].items() if b[2] > w or b[3] > h]
        bad += [f"{n} {tuple(p)}" for n, p in self.cfg["fixed_points"].items() if p[0] > w or p[1] > h]
        for b in bad:
            self.log(f"Outside the current {w}x{h} screen (resolution changed?): {b}", "warn")
        state = next((s for n, s in STATES if self.vision.find(frame, n, self.cfg["match_confidence"])), None)
        self.log(f"Screen is {w}x{h}; the bot thinks it's on: {STATE_LABELS[state]}.", "ok")
        if not HAVE_TESS or not os.path.exists(self.cfg["tesseract_path"]):
            self.log("Tesseract not found - install it or fix its path in Settings > Engine.", "err")
        if not self.missing_setup() and not bad:
            self.log("Everything required is set up.", "ok")


# ---------------------------------------------------------------------------
# Self-check: python bot.py --selftest
# ---------------------------------------------------------------------------
def selftest():
    px = bytes(range(24)) * 2  # 4x3 RGBA
    for extra in (b"", b"\0\0\0\0"):
        img = decode_raw(struct.pack("<III", 4, 3, 1) + extra + px)
        assert img.shape == (3, 4, 3) and tuple(img[0, 0]) == (2, 1, 0), "raw screencap decode"
    assert decode_raw(b"\x89PNG not raw at all") is None
    assert panel_box(np.zeros((1080, 1920, 3), np.uint8)) is None, "no list open"
    # names as OCR reads them, and look-alikes that must not match
    assert name_match("Lonashot&", "Longshot") and name_match("New Hog Glider", "Hog Glider")
    assert name_match("0.T.T.0's Ouboost", "O.T.T.O's Outpost") and name_match("Scabbershot x2", "Scattershot")
    assert not name_match("Ricochet Cannon", "Cannon") and not name_match("Dark Elixir Storage", "Elixir Storage")
    assert not name_match("Hog Rider", "Hog Glider") and not name_match("Miner", "Mine")
    assert all(map(is_wall, ("Wall", "Wall x28", "': Wall x2", "Wall xI50"))) and not is_wall("Wall Wrecker")
    if wiki_data():  # price -> level, with the discount worked out from the whole list (Hammer Jam / Gold Pass)
        rows = [("Cannon x4", 3000000), ("Mortar x3", 21000000), ("Archer Queen", 380000), ("Laboratoru", 13000000)]
        for f in (1, 0.8, 0.5):
            got = [(n, int(p * f)) for n, p in rows]
            assert infer_discount(got, "home") == f, f
            assert level_for_price(wiki_key("Cannon x4", "home"), 3000000 * f, f) == 20

    frame = np.random.default_rng(0).integers(0, 255, (1080, 1920, 3), dtype=np.uint8)
    frame = cv2.GaussianBlur(frame, (5, 5), 0)
    d = tempfile.mkdtemp()
    try:
        cv2.imwrite(os.path.join(d, "btn.png"), frame[500:560, 800:900])
        v = Vision(d)
        sc, x, y = v.score(frame, "btn")
        assert sc > 0.9 and abs(x - 850) <= 2 and abs(y - 530) <= 2, f"template match {sc, x, y}"
        assert v.find(frame, "nope", 0.8) is None
    finally:
        shutil.rmtree(d)

    g = digit_glyphs()
    assert g is not None, "templates/digits.png missing"
    canvas = np.zeros((40, 12 + 26 * 7), bool)
    for j, ch in enumerate("9524837"):
        canvas[6:34, 6 + j * 26:26 + j * 26] = g[int(ch)] > 0.5
    assert read_digit_blobs(canvas) == 9524837, "digit reader"

    grey = np.full((60, 60, 3), 120, np.uint8)
    red = grey.copy()
    red[:, :] = (30, 30, 220)
    assert icon_empty(grey, 30, 30) and not icon_empty(red, 30, 30), "grey slot detection"

    # the game's data export: ids -> buildings, upgrading = next level, supercharged copies merged
    ex = export_items({"buildings": [{"data": 1000013, "lvl": 17, "timer": 5}, {"data": 1000002, "lvl": 17, "cnt": 2},
                                     {"data": 1000002, "lvl": 17, "cnt": 4, "supercharge": 1}, {"data": 1000010, "lvl": 9}],
                       "heroes2": [{"data": 28000003, "lvl": 35}]})
    assert sorted((r["key"], r["level"], r["count"], r["upgrading"]) for r in ex["home"]) == [
        ("home:elixir collector", 17, 6, False), ("home:mortar", 18, 1, True)], ex
    assert [(r["key"], r["level"]) for r in ex["builder"]] == [("builder:battle machine", 35)], ex

    if HAVE_TESS and os.path.exists(DEFAULTS["tesseract_path"]):
        pytesseract.pytesseract.tesseract_cmd = DEFAULTS["tesseract_path"]
        img = np.zeros((80, 420, 3), np.uint8)
        cv2.putText(img, "1234567", (10, 60), cv2.FONT_HERSHEY_SIMPLEX, 2, (255, 255, 255), 4)
        got = ocr_number(img, (0, 0, 420, 80), 0, 3)
        assert got == 1234567, f"OCR read {got}"
    print("selftest ok")


def make_package():
    """LootFarmer_share.zip for a friend: bot + templates + setup, with a config stripped of anything personal
    (API key, phone-link secret, device, paths, account names). Setup.bat fills the paths in on their PC."""
    import zipfile
    cfg, _ = load_config()
    shared = {k: cfg.get(k, v) for k, v in DEFAULTS.items()}  # drops leftovers from the old bot
    shared.update(groq_api_key="", phone_view_key="", device="", accounts="", emulator_exe_path="",
                  adb_path="", tesseract_path="",  # -> the bundled copies in the folder
                  auto_connect_target=DEFAULTS["auto_connect_target"], watchdog_enabled=False)
    out = os.path.join(BASE_DIR, "LootFarmer_share.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("bot.py", "setup.ps1", "Setup.bat", "README.txt"):
            z.write(os.path.join(BASE_DIR, f), f"LootFarmer/{f}")
        for folder in ("Tesseract-OCR", "platform-tools"):  # bundled tools: nothing to install
            for root, _, files in os.walk(os.path.join(BASE_DIR, folder)):
                for f in files:
                    full = os.path.join(root, f)
                    z.write(full, "LootFarmer/" + os.path.relpath(full, BASE_DIR).replace(os.sep, "/"))
        for f in sorted(os.listdir(TEMPLATE_DIR)):
            if f.endswith(".png") and not f.startswith("old_"):
                z.write(os.path.join(TEMPLATE_DIR, f), f"LootFarmer/templates/{f}")
        for f in published_files():
            if f.startswith("wiki/"):
                z.write(os.path.join(BASE_DIR, f), f"LootFarmer/{f}")
        z.writestr("LootFarmer/config.json", json.dumps(shared, indent=2))
    print(f"Created {out}")


PUBLISHED = ("bot.py", "setup.ps1", "Setup.bat", "README.txt", ".gitignore", ".gitattributes")


def published_files():
    """Everything an update carries: the code, setup files and templates. Never config.json / logs / tools."""
    files = [f for f in PUBLISHED if os.path.exists(os.path.join(BASE_DIR, f))]
    files += sorted(f"templates/{f}" for f in os.listdir(TEMPLATE_DIR) if f.endswith(".png"))
    if os.path.isdir(WIKI_DIR):  # the upgrade planner's wiki data + building pictures
        files += ["wiki/buildings.json"] * os.path.exists(os.path.join(WIKI_DIR, "buildings.json"))
        icons = os.path.join(WIKI_DIR, "icons")
        files += sorted(f"wiki/icons/{f}" for f in (os.listdir(icons) if os.path.isdir(icons) else [])
                        if f.endswith(".png"))
    return files


def file_hash(path):
    import hashlib
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def fetch(path, timeout=20, ref="main"):
    import urllib.request
    url = f"https://raw.githubusercontent.com/{UPDATE_REPO}/{ref}/{path}?t={int(time.time())}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Cache-Control": "no-cache"}),
                                timeout=timeout) as r:
        return r.read()


def check_update():
    """The published manifest if GitHub has a newer version than this copy, else None."""
    try:
        man = json.loads(fetch("manifest.json"))
        return man if int(man.get("version", 0)) > APP_VERSION else None
    except Exception as e:
        log_file.info(f"Update check failed: {e}")
        return None


def apply_update(man):
    """Download only the files whose hash differs, check each against the manifest, then swap them in.
    config.json and anything not in the manifest are left alone. Returns the number of files updated."""
    changed = {}
    for path, digest in man["files"].items():
        if ".." in path or path.startswith(("/", "\\")) or path == "config.json":
            continue  # never write outside the bot folder or over the user's settings
        local = os.path.join(BASE_DIR, path)
        if os.path.exists(local) and file_hash(local) == digest:
            continue
        import hashlib
        data = fetch(path.replace(" ", "%20"), ref=man.get("ref", "main"))
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError(f"{path}: download didn't match the published file - try again")
        changed[path] = data
    for path, data in changed.items():  # all downloaded and verified first, then written
        local = os.path.join(BASE_DIR, path)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        tmp = local + ".new"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, local)
    return len(changed)


def list_versions(limit=30):
    """Published versions, newest first: [(version, date, message, commit)]. Every publish commits manifest.json,
    so its history is the version history."""
    import urllib.request
    url = f"https://api.github.com/repos/{UPDATE_REPO}/commits?path=manifest.json&per_page={limit}"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "LootFarmer"}), timeout=20) as r:
        commits = json.load(r)
    out = []
    for c in commits:
        try:
            man = json.loads(fetch("manifest.json", ref=c["sha"]))
        except Exception:
            continue
        out.append((int(man.get("version", 0)), c["commit"]["committer"]["date"][:10],
                    c["commit"]["message"].splitlines()[0][:60], c["sha"]))
    return out


def install_version(sha):
    """Put this folder's files back to a published snapshot (settings untouched). Returns files changed."""
    man = json.loads(fetch("manifest.json", ref=sha))
    man["ref"] = sha
    return apply_update(man)


def publish(message):
    """Bump APP_VERSION, write manifest.json and push the published files to GitHub (git must be signed in)."""
    src = open(__file__, encoding="utf-8").read()
    new_version = APP_VERSION + 1
    src = re.sub(r"^APP_VERSION = \d+", f"APP_VERSION = {new_version}", src, count=1, flags=re.M)
    with open(__file__, "w", encoding="utf-8") as f:
        f.write(src)
    man = {"version": new_version, "files": {p: file_hash(os.path.join(BASE_DIR, p)) for p in published_files()}}
    with open(os.path.join(BASE_DIR, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(man, f, indent=1)
    git = lambda *a: subprocess.run(["git", *a], cwd=BASE_DIR, check=True)
    if not os.path.isdir(os.path.join(BASE_DIR, ".git")):
        git("init", "-b", "main")
        git("remote", "add", "origin", f"https://github.com/{UPDATE_REPO}.git")
    git("add", "manifest.json", *published_files())
    git("commit", "-m", message or f"Update to version {new_version}")
    git("push", "-u", "origin", "main")
    print(f"Published version {new_version} ({len(man['files'])} files). Friends will see an Update button.")


def make_update():
    """LootFarmer_update.zip for a friend who's already set up: only the code + templates. No config.json (keeps
    their drop lines, settings and account setup; new settings get their defaults) and no bundled tools."""
    import zipfile
    out = os.path.join(BASE_DIR, "LootFarmer_update.zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in ("bot.py", "setup.ps1", "Setup.bat", "README.txt"):
            z.write(os.path.join(BASE_DIR, f), f"LootFarmer/{f}")
        for f in sorted(os.listdir(TEMPLATE_DIR)):
            if f.endswith(".png") and not f.startswith("old_"):
                z.write(os.path.join(TEMPLATE_DIR, f), f"LootFarmer/templates/{f}")
        for f in published_files():
            if f.startswith("wiki/"):
                z.write(os.path.join(BASE_DIR, f), f"LootFarmer/{f}")
        z.writestr("LootFarmer/HOW TO UPDATE.txt", "\r\n".join([
            "1. Close Loot Farmer.",
            "2. Copy everything in this LootFarmer folder into your existing LootFarmer folder, and choose",
            "   'Replace the files in the destination'.",
            "   Your settings, drop lines and accounts (config.json) are NOT in this update, so they're kept.",
            "3. Open Loot Farmer again. New settings appear with sensible defaults - check the Settings tab.", ""]))
    print(f"Created {out}")


if __name__ == "__main__":
    if "--publish" in sys.argv:
        i = sys.argv.index("--publish")
        publish(" ".join(sys.argv[i + 1:]).strip())
    elif "--update" in sys.argv:
        make_update()
    elif "--package" in sys.argv:
        make_package()
    elif "--selftest" in sys.argv:
        selftest()
    else:
        try:  # crisp text on scaled (125-200%) Windows displays
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
        _instance = single_instance()
        if _instance is None:
            tk.Tk().withdraw()
            messagebox.showinfo("Loot Farmer", "Loot Farmer is already running (maybe from another folder).\n\n"
                                               "Two copies would fight over the same emulator - close the other one.")
            sys.exit()
        try:
            App().mainloop()
        except Exception:
            log_file.error("Loot Farmer crashed:\n" + traceback.format_exc())
            tk.Tk().withdraw()
            messagebox.showerror("Loot Farmer", "Loot Farmer hit an error and closed - details are in bot.log.\n\n"
                                                + traceback.format_exc(limit=2))
