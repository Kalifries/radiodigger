#!/usr/bin/env python3
import curses
import os
import json
import time
import threading
import requests
import shutil
import subprocess

import vlc

# Optional: pulsectl for better "real-ish" VU meter
try:
    import pulsectl  # type: ignore
except Exception:
    pulsectl = None

API = "https://de1.api.radio-browser.info/json/stations/search"

CONFIG_DIR = os.path.expanduser("~/.config/radiodigger")
CACHE_DIR = os.path.expanduser("~/.cache/radiodigger")
FAV_FILE = os.path.join(CONFIG_DIR, "favorites.json")   # slot -> stationuuid
STATE_FILE = os.path.join(CONFIG_DIR, "state.json")     # last played, UI prefs
VOL_FILE = os.path.join(CONFIG_DIR, "volumes.json")     # stationuuid -> 0..100
NOWPLAY_FILE = os.path.join(CACHE_DIR, "now_playing.txt")

BANNER = [
    "██████╗  █████╗ ██████╗ ██╗ ██████╗ ",
    "██╔══██╗██╔══██╗██╔══██╗██║██╔════╝ ",
    "██████╔╝███████║██║  ██║██║██║  ███╗",
    "██╔══██╗██╔══██║██║  ██║██║██║   ██║",
    "██║  ██║██║  ██║██████╔╝██║╚██████╔╝",
    "╚═╝  ╚═╝╚═╝  ╚═╝╚═════╝ ╚═╝ ╚═════╝ ",
    "        R A D I O   D I G G E R      "
]

# -----------------------------
# Helpers: config/state storage
# -----------------------------
def ensure_dirs():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    os.makedirs(CACHE_DIR, exist_ok=True)
    for path, default in [
        (FAV_FILE, {}),
        (STATE_FILE, {"last_stationuuid": None, "show_history": True}),
        (VOL_FILE, {}),
    ]:
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump(default, f, indent=2)

def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default

def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

# -----------------------------
# Radio Browser search
# -----------------------------
def search_stations(query):
    r = requests.get(API, params={
        "name": query,
        "limit": 150,
        "hidebroken": "true"
    }, timeout=10)
    r.raise_for_status()
    return r.json()

# -----------------------------
# UI: colors + primitives
# -----------------------------
def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # banner
    curses.init_pair(2, curses.COLOR_YELLOW, -1)   # headings/metadata
    curses.init_pair(3, curses.COLOR_GREEN, -1)    # good/active
    curses.init_pair(4, curses.COLOR_RED, -1)      # stop/error-ish
    curses.init_pair(5, curses.COLOR_MAGENTA, -1)  # selection highlight
    curses.init_pair(6, curses.COLOR_BLUE, -1)     # history box

def draw_banner(stdscr):
    for i, line in enumerate(BANNER):
        stdscr.addstr(i, 2, line, curses.color_pair(1) | curses.A_BOLD)

def status_bar(stdscr, text):
    h, w = stdscr.getmaxyx()

    # If terminal is too small, don't draw
    if h < 1 or w < 2:
        return

    line = text[: max(0, w - 1)]

    try:
        stdscr.attron(curses.A_REVERSE)
        stdscr.addstr(h - 1, 0, line.ljust(w - 1))
        stdscr.attroff(curses.A_REVERSE)
    except curses.error:
        # Terminal too small or resized mid-draw — ignore safely
        pass


def clamp(n, lo, hi):
    return max(lo, min(hi, n))

def safe_addstr(stdscr, y, x, s, attr=0):
    try:
        stdscr.addstr(y, x, s, attr)
    except curses.error:
        pass

# -----------------------------
# Now-playing + metadata history
# -----------------------------
class NowPlaying:
    def __init__(self):
        self.lock = threading.Lock()
        self.station_name = "—"
        self.stationuuid = None
        self.line = "—"
        self.history = []  # newest first
        self.max_history = 200

    def set_station(self, station_name, stationuuid):
        with self.lock:
            self.station_name = station_name or "Unknown"
            self.stationuuid = stationuuid
            # Keep line, but station changed
            self._write_nowplay()

    def update_line(self, line):
        line = (line or "").strip()
        if not line:
            return
        with self.lock:
            if line != self.line:
                self.line = line
                self.history.insert(0, line)
                self.history = self.history[:self.max_history]
                self._write_nowplay()

    def snapshot(self):
        with self.lock:
            return self.station_name, self.line, list(self.history)

    def _write_nowplay(self):
        os.makedirs(CACHE_DIR, exist_ok=True)
        out = f"{self.station_name} :: {self.line}".strip()
        try:
            with open(NOWPLAY_FILE, "w") as f:
                f.write(out + "\n")
        except Exception:
            pass

        # tmux integration (optional)
        if "TMUX" in os.environ and shutil.which("tmux"):
            # User option for tmux: #{@radiodigger_now_playing}
            try:
                subprocess.run(
                    ["tmux", "set", "-gq", "@radiodigger_now_playing", out[:200]],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False
                )
            except Exception:
                pass

# -----------------------------
# Volume per station + player control
# -----------------------------
class Player:
    def __init__(self, vlc_instance, nowplaying: NowPlaying):
        self.vlc_instance = vlc_instance
        self.player = vlc_instance.media_player_new()
        self.now = nowplaying
        self.volumes = load_json(VOL_FILE, {})  # stationuuid -> 0..100
        self.state = load_json(STATE_FILE, {"last_stationuuid": None, "show_history": True})

        self._stop_flag = False
        self._meta_thread = threading.Thread(target=self._meta_loop, daemon=True)
        self._meta_thread.start()

    def play_station(self, station):
        url = station.get("url_resolved")
        if not url:
            return False

        name = station.get("name", "Unknown")
        stationuuid = station.get("stationuuid")

        self.stop()

        self.now.set_station(name, stationuuid)

        media = self.vlc_instance.media_new(url)
        self.player.set_media(media)
        self.player.play()

        # Apply per-station volume if known
        if stationuuid and stationuuid in self.volumes:
            self.set_volume(int(self.volumes[stationuuid]))
        else:
            # Default volume if none saved
            if self.get_volume() <= 0:
                self.set_volume(70)

        # Persist last played
        self.state["last_stationuuid"] = stationuuid
        save_json(STATE_FILE, self.state)

        return True

    def stop(self):
        try:
            self.player.stop()
        except Exception:
            pass

    def is_playing(self):
        try:
            return bool(self.player.is_playing())
        except Exception:
            return False

    def get_volume(self):
        try:
            return int(self.player.audio_get_volume())
        except Exception:
            return 0

    def set_volume(self, vol):
        vol = clamp(int(vol), 0, 100)
        try:
            self.player.audio_set_volume(vol)
        except Exception:
            pass

    def save_station_volume(self, stationuuid):
        if not stationuuid:
            return
        self.volumes[stationuuid] = self.get_volume()
        save_json(VOL_FILE, self.volumes)

    def toggle_mute(self):
        try:
            self.player.audio_toggle_mute()
        except Exception:
            pass

    def shutdown(self):
        self._stop_flag = True
        self.stop()

    def _meta_loop(self):
        """
        Poll metadata occasionally. Some streams update ICY metadata on interval.
        """
        while not self._stop_flag:
            try:
                media = self.player.get_media()
                if media:
                    # network parsing helps fetch metadata
                    media.parse_with_options(vlc.MediaParseFlag.network, timeout=200)

                    artist = media.get_meta(vlc.Meta.Artist) or ""
                    title = media.get_meta(vlc.Meta.Title) or ""
                    now = ""

                    if artist and title:
                        now = f"{artist} — {title}"
                    elif title:
                        now = title
                    else:
                        # Some stations put everything in "NowPlaying" or "Description"
                        np = media.get_meta(vlc.Meta.NowPlaying) or ""
                        desc = media.get_meta(vlc.Meta.Description) or ""
                        now = np or desc or "Stream (no metadata)"

                    self.now.update_line(now)
            except Exception:
                pass
            time.sleep(1.5)

# -----------------------------
# Better VU meter via Pulse/PipeWire (optional)
# -----------------------------
class PulseVU:
    """
    Attempts to find the sink-input created by VLC and estimate a "level".
    This is not FFT, but it tracks real system volume + playback state more honestly.
    """
    def __init__(self):
        self.ok = False
        self._pulse = None
        self._last_level = 0
        if pulsectl is None:
            return
        try:
            self._pulse = pulsectl.Pulse("radiodigger")
            self.ok = True
        except Exception:
            self.ok = False

    def close(self):
        try:
            if self._pulse:
                self._pulse.close()
        except Exception:
            pass

    def level(self):
        if not self.ok:
            return None

        try:
            inputs = self._pulse.sink_input_list()
            # Look for VLC-like apps
            target = None
            for si in inputs:
                app = (si.proplist.get("application.name") or "").lower()
                binname = (si.proplist.get("application.process.binary") or "").lower()
                if "vlc" in app or "vlc" in binname or "libvlc" in app:
                    target = si
                    break

            if not target:
                return None

            # volume values are 0..1+ per channel; take max, clamp to 0..1
            vols = list(target.volume.values) if hasattr(target.volume, "values") else [target.volume.value_flat]
            v = max(vols) if vols else 0.0
            v = clamp(v, 0.0, 1.0)
            # Smooth a bit
            lvl = int(v * 30)
            self._last_level = int((self._last_level * 0.6) + (lvl * 0.4))
            return self._last_level
        except Exception:
            return None

# -----------------------------
# UI screens
# -----------------------------
def search_screen(stdscr, default_query=""):
    stdscr.clear()
    draw_banner(stdscr)
    safe_addstr(stdscr, len(BANNER) + 2, 2, "Search stations:", curses.color_pair(2) | curses.A_BOLD)
    safe_addstr(stdscr, len(BANNER) + 3, 2, default_query)
    curses.echo()
    try:
        q = stdscr.getstr(len(BANNER) + 3, 2).decode()
    finally:
        curses.noecho()
    return (q or "").strip()

def draw_history_box(stdscr, history, selected, box_y, box_h, box_w, scroll):
    # box border
    safe_addstr(stdscr, box_y, 2, "┌" + "─" * (box_w - 2) + "┐", curses.color_pair(6))
    for i in range(1, box_h - 1):
        safe_addstr(stdscr, box_y + i, 2, "│" + " " * (box_w - 2) + "│", curses.color_pair(6))
    safe_addstr(stdscr, box_y + box_h - 1, 2, "└" + "─" * (box_w - 2) + "┘", curses.color_pair(6))
    safe_addstr(stdscr, box_y, 4, "Metadata History", curses.color_pair(6) | curses.A_BOLD)

    inner_h = box_h - 2
    view = history[scroll:scroll + inner_h]
    for i, line in enumerate(view):
        y = box_y + 1 + i
        s = line[:box_w - 4]
        if (scroll + i) == selected:
            safe_addstr(stdscr, y, 4, s.ljust(box_w - 4), curses.A_REVERSE | curses.color_pair(6))
        else:
            safe_addstr(stdscr, y, 4, s.ljust(box_w - 4), curses.color_pair(6))

def station_browser(stdscr, stations, player: Player, vu: PulseVU):
    favs = load_json(FAV_FILE, {})     # slot "1".."9" -> stationuuid
    volumes = load_json(VOL_FILE, {})  # stationuuid -> 0..100
    state = load_json(STATE_FILE, {"last_stationuuid": None, "show_history": True})

    selected = 0
    offset = 0

    show_history = bool(state.get("show_history", True))
    hist_sel = 0
    hist_scroll = 0

    while True:
        stdscr.clear()
        draw_banner(stdscr)
        h, w = stdscr.getmaxyx()

        top = len(BANNER) + 1

        # Panel sizing
        history_h = 10 if show_history else 0
        list_h = h - top - 6 - history_h
        list_h = max(5, list_h)

        # Station list
        for i, st in enumerate(stations[offset:offset + list_h]):
            idx = offset + i
            name = st.get("name", "Unknown")
            country = st.get("country", "")
            su = st.get("stationuuid")
            is_fav = su in favs.values()
            mark = "★" if is_fav else " "

            volmark = ""
            if su and su in volumes:
                volmark = f" (vol {int(volumes[su])}%)"

            line = f"{mark} {name} [{country}]{volmark}"
            if idx == selected:
                safe_addstr(stdscr, top + i, 2, line[:w - 4], curses.color_pair(5) | curses.A_REVERSE)
            else:
                safe_addstr(stdscr, top + i, 2, line[:w - 4])

        # Now playing + VU + volume
        station_name, np_line, history = player.now.snapshot()

        safe_addstr(stdscr, h - 5 - history_h, 2, f"Station: {station_name}"[:w - 4],
                    curses.color_pair(2) | curses.A_BOLD)
        safe_addstr(stdscr, h - 4 - history_h, 2, f"Now: {np_line}"[:w - 4],
                    curses.color_pair(2))

        # VU meter line
        vu_width = min(30, max(10, w - 20))
        lvl = vu.level() if vu else None
        if lvl is None:
            # fallback: animate based on playing state + volume (non-random-ish)
            base = int((player.get_volume() / 100) * vu_width)
            lvl = base if player.is_playing() else 0

        lvl = clamp(lvl, 0, vu_width)
        bar = "█" * lvl
        safe_addstr(stdscr, h - 3 - history_h, 2, "VU: ", curses.color_pair(3) | curses.A_BOLD)
        safe_addstr(stdscr, h - 3 - history_h, 6, bar.ljust(vu_width),
                    curses.color_pair(3 if player.is_playing() else 4))
        safe_addstr(stdscr, h - 3 - history_h, 7 + vu_width, f" Vol {player.get_volume():3d}%",
                    curses.color_pair(2))

        # Metadata history box
        if show_history:
            box_w = min(w - 4, 80)
            box_h = min(history_h, h - 2)
            box_y = h - history_h - 2
            hist_sel = clamp(hist_sel, 0, max(0, len(history) - 1))
            hist_scroll = clamp(hist_scroll, 0, max(0, len(history) - (box_h - 2)))
            draw_history_box(stdscr, history, hist_sel, box_y, box_h, box_w, hist_scroll)

        # Help/status
        status_bar(
            stdscr,
            "↑↓ navigate | ENTER play | +/- vol | m mute | s stop | h history | 1-9 save fav | g goto fav | b back | q quit"
        )

        stdscr.refresh()
        key = stdscr.getch()

        # Navigation in station list
        if key == curses.KEY_UP and selected > 0:
            selected -= 1
            if selected < offset:
                offset -= 1

        elif key == curses.KEY_DOWN and selected < len(stations) - 1:
            selected += 1
            if selected >= offset + list_h:
                offset += 1

        # Play
        elif key in (10, 13):
            st = stations[selected]
            player.play_station(st)

        # Stop
        elif key in (ord("s"), ord("S")):
            player.stop()

        # Mute
        elif key in (ord("m"), ord("M")):
            player.toggle_mute()

        # Volume up/down (and save per-station on change)
        elif key in (ord("+"), ord("=")):
            player.set_volume(player.get_volume() + 5)
            st = stations[selected]
            player.save_station_volume(st.get("stationuuid"))
            volumes = load_json(VOL_FILE, {})

        elif key in (ord("-"), ord("_")):
            player.set_volume(player.get_volume() - 5)
            st = stations[selected]
            player.save_station_volume(st.get("stationuuid"))
            volumes = load_json(VOL_FILE, {})

        # Toggle history
        elif key in (ord("h"), ord("H")):
            show_history = not show_history
            state["show_history"] = show_history
            save_json(STATE_FILE, state)

        # Scroll history (PgUp/PgDn)
        elif show_history and key == curses.KEY_PPAGE:
            hist_sel = clamp(hist_sel + 5, 0, max(0, len(history) - 1))
            hist_scroll = clamp(hist_scroll + 5, 0, max(0, len(history) - 1))

        elif show_history and key == curses.KEY_NPAGE:
            hist_sel = clamp(hist_sel - 5, 0, max(0, len(history) - 1))
            hist_scroll = clamp(hist_scroll - 5, 0, max(0, len(history) - 1))

        # Save favorites: 1-9 bind to CURRENT station
        elif key in range(ord("1"), ord("9") + 1):
            st = stations[selected]
            su = st.get("stationuuid")
            if su:
                favs[chr(key)] = su
                save_json(FAV_FILE, favs)

        # Go to favorite (press g then 1-9)
        elif key in (ord("g"), ord("G")):
            status_bar(stdscr, "Goto favorite: press 1-9 (or any other key to cancel)")
            stdscr.refresh()
            k2 = stdscr.getch()
            if k2 in range(ord("1"), ord("9") + 1):
                slot = chr(k2)
                target_su = favs.get(slot)
                if target_su:
                    # find station in current list
                    for i, st in enumerate(stations):
                        if st.get("stationuuid") == target_su:
                            selected = i
                            offset = max(0, selected - 3)
                            break

        # Back / Quit
        elif key in (ord("b"), ord("B")):
            player.stop()
            return

        elif key in (ord("q"), ord("Q")):
            raise SystemExit

def autostart_last(player: Player):
    """
    On startup, try to autostart the last played station if it exists in favorites slots
    (or just keep last_stationuuid and rely on the next search to find it).
    """
    state = load_json(STATE_FILE, {"last_stationuuid": None})
    last = state.get("last_stationuuid")
    return last

def main(stdscr):
    curses.curs_set(0)
    init_colors()
    ensure_dirs()

    now = NowPlaying()
    vlc_instance = vlc.Instance("--no-video")
    player = Player(vlc_instance, now)
    vu = PulseVU()

    last_stationuuid = autostart_last(player)

    # Hint in cache file so tmux can show something immediately
    now.update_line("Ready")

    default_query = ""
    while True:
        q = search_screen(stdscr, default_query=default_query)
        if not q:
            continue

        try:
            stations = search_stations(q)
        except Exception as e:
            stdscr.clear()
            draw_banner(stdscr)
            safe_addstr(stdscr, len(BANNER) + 2, 2, f"Search error: {e}", curses.color_pair(4) | curses.A_BOLD)
            safe_addstr(stdscr, len(BANNER) + 4, 2, "Press any key…")
            stdscr.getch()
            continue

        if not stations:
            stdscr.clear()
            draw_banner(stdscr)
            safe_addstr(stdscr, len(BANNER) + 2, 2, "No stations found.", curses.color_pair(4) | curses.A_BOLD)
            safe_addstr(stdscr, len(BANNER) + 4, 2, "Press any key…")
            stdscr.getch()
            continue

        # Autostart: if the last station is in THIS results list, start it
        if last_stationuuid:
            for st in stations:
                if st.get("stationuuid") == last_stationuuid:
                    player.play_station(st)
                    break
            last_stationuuid = None  # only try once per run

        try:
            station_browser(stdscr, stations, player, vu)
        except SystemExit:
            break

        default_query = q

    player.shutdown()
    if vu:
        vu.close()

if __name__ == "__main__":
    curses.wrapper(main)

