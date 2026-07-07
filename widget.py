# -*- coding: utf-8 -*-
"""
Taskbar Spotify gostergesi — resmi Spotify gorunumu:
  - Sadece Spotify ACIKKEN (bir sarki varken) taskbar bandinda belirir.
  - Album kapagindan turetilen arka plan + Spotify yesili spektrum + sarki/sanatci
    + ilerleme cubugu. Spotify kapaninca kaybolur, tam ekranda gizlenir.
  - Spektrum SADECE Spotify ses cikardiginda oynar (pycaw ile Spotify oturumu).
Sag tik: Kapat.
"""
import sys, os, io, asyncio, threading, time, winreg
import numpy as np
import psutil

BASE = os.path.dirname(os.path.abspath(__file__))
WIDTH   = 330
BARS    = 20
BAR_ZONE = 108           # sag taraftaki spektrum bolgesi genisligi
GAP_FROM_TRAY = 12
SPECTRUM = True
SR, NFFT = 48000, 2048

# ---- Spotify paleti (on plan hep Spotify; arka plan taskbar'a uyar) ----
SPOT_GREEN = "#1ed760"   # resmi Spotify yesili
WHITE      = "#ffffff"
GREY       = "#b3b3b3"   # Spotify ikincil metin grisi
BASE_BG    = "#121212"   # Spotify koyu zemini
TRACK_GREY = "#4d4d4d"   # ilerleme cubugu bos kismi

# ---------------- Paylasilan durum ----------------
media_state = {"name": "", "title": "", "artist": "", "album": "",
               "pos": 0, "dur": 0, "playing": False, "present": False}
cover = {"pil": None, "token": None}
spectrum = np.zeros(BARS)
gate = {"v": 0.0}

# ---------------- Spotify (winsdk) ----------------
async def _fetch_media(last_title):
    from winsdk.windows.media.control import \
        GlobalSystemMediaTransportControlsSessionManager as M
    from winsdk.windows.storage.streams import DataReader, Buffer, InputStreamOptions
    mgr = await M.request_async()
    sess = None
    for s in mgr.get_sessions():
        try:
            if "spotify" in (s.source_app_user_model_id or "").lower():
                sess = s; break
        except Exception:
            pass
    if sess is None:
        return None
    p = await sess.try_get_media_properties_async()
    tl = sess.get_timeline_properties()
    pos = int(tl.position.total_seconds()) if tl.position else 0
    dur = int(tl.end_time.total_seconds()) if tl.end_time else 0
    try:
        playing = sess.get_playback_info().playback_status == 4
    except Exception:
        playing = True
    artist = (p.artist or "").strip(); title = (p.title or "").strip()
    info = {"name": f"{artist} - {title}" if artist else title,
            "title": title, "artist": artist, "album": (p.album_title or "").strip(),
            "pos": pos, "dur": dur, "playing": playing, "present": True, "cover_bytes": None}
    if title and title != last_title and p.thumbnail:
        try:
            st = await p.thumbnail.open_read_async()
            buf = Buffer(st.size)
            await st.read_async(buf, st.size, InputStreamOptions.NONE)
            rd = DataReader.from_buffer(buf)
            out = bytearray(buf.length); rd.read_bytes(out)
            info["cover_bytes"] = bytes(out)
        except Exception:
            pass
    return info

def media_loop():
    from PIL import Image
    last_title = None
    while True:
        try:
            info = asyncio.run(_fetch_media(last_title))
        except Exception:
            info = None
        if info and info["present"]:
            media_state.update({k: info[k] for k in
                ("name","title","artist","album","pos","dur","playing","present")})
            if info["cover_bytes"]:
                try:
                    img = Image.open(io.BytesIO(info["cover_bytes"])).convert("RGB")
                    cover["pil"] = img.resize((96, 96), Image.LANCZOS)
                    cover["token"] = info["title"]
                except Exception:
                    pass
            last_title = info["title"]
        else:
            media_state.update(present=False, playing=False)
            last_title = None
        time.sleep(1)

# ---------------- Spotify kontrol (GSMTC komutlari) ----------------
async def _toggle():
    from winsdk.windows.media.control import \
        GlobalSystemMediaTransportControlsSessionManager as M
    mgr = await M.request_async()
    for s in mgr.get_sessions():
        try:
            if "spotify" in (s.source_app_user_model_id or "").lower():
                await s.try_toggle_play_pause_async(); return
        except Exception:
            pass

def send_cmd(cmd):
    # play/pause: Spotify oturumuna hedefli SMTC toggle.
    # next/prev: WM_APPCOMMAND dogrudan Spotify penceresine (durakli/calar farketmez,
    #            global medya tusu yonlendirmesine bagli degil, guvenilir).
    def run():
        try:
            if cmd == "toggle":
                asyncio.run(_toggle())
            elif cmd == "next":
                _appcmd(11)   # APPCOMMAND_MEDIA_NEXTTRACK
            elif cmd == "prev":
                _appcmd(12)   # APPCOMMAND_MEDIA_PREVIOUSTRACK
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()

# ---------------- Spotify ses kapisi (pycaw) ----------------
def gate_loop():
    from pycaw.pycaw import AudioUtilities, IAudioMeterInformation
    try:
        import comtypes; comtypes.CoInitialize()
    except Exception:
        pass
    meters = []; i = 0
    while True:
        i += 1
        try:
            if i % 33 == 1:
                meters = []
                for s in AudioUtilities.GetAllSessions():
                    try:
                        if s.Process and s.Process.name().lower() == "spotify.exe":
                            meters.append(s._ctl.QueryInterface(IAudioMeterInformation))
                    except Exception:
                        pass
            peak = 0.0
            for m in meters:
                try: peak = max(peak, m.GetPeakValue())
                except Exception: meters = []
            if meters:
                target = 1.0 if peak > 0.003 else 0.0
            else:
                target = 1.0 if media_state.get("playing") else 0.0
        except Exception:
            target = 1.0 if media_state.get("playing") else 0.0
        g = gate["v"]
        gate["v"] = g + (target - g) * (0.6 if target > g else 0.06)
        time.sleep(0.03)

# ---------------- Ses spektrumu (loopback) ----------------
def get_loopback():
    import soundcard as sc
    try:
        return sc.get_microphone(str(sc.default_speaker().name), include_loopback=True)
    except Exception:
        for m in sc.all_microphones(include_loopback=True):
            if getattr(m, "isloopback", False):
                return m
    return None

def audio_loop():
    global spectrum
    import warnings; warnings.filterwarnings("ignore")
    freqs = np.fft.rfftfreq(NFFT, 1 / SR)
    edges = np.logspace(np.log10(55), np.log10(16000), BARS + 1)
    masks = [(freqs >= edges[i]) & (freqs < edges[i + 1]) for i in range(BARS)]
    win = np.hanning(NFFT); peak = 1e-6
    while True:
        if not media_state["present"]:
            spectrum = spectrum * 0.0; time.sleep(0.5); continue
        try:
            lb = get_loopback()
            if lb is None:
                time.sleep(3); continue
            ring = np.zeros(NFFT)
            with lb.recorder(samplerate=SR, channels=2, blocksize=512) as rec:
                while media_state["present"]:
                    block = rec.record(numframes=1024)
                    mono = block.mean(axis=1); n = len(mono)
                    ring = np.roll(ring, -n); ring[-n:] = mono
                    spec = np.abs(np.fft.rfft(ring * win))
                    raw = np.array([spec[m].mean() if m.any() else 0.0 for m in masks])
                    raw = np.log1p(raw * 8.0)
                    peak = max(peak * 0.995, raw.max())
                    norm = np.clip(raw / (peak + 1e-9), 0, 1) * gate["v"]
                    spectrum = np.maximum(norm, spectrum * 0.78)
        except Exception:
            spectrum = spectrum * 0.0; time.sleep(2)

def fmt_secs(s):
    return f"{s//60}:{s%60:02d}"

# ---------------- Test modu ----------------
if "--test" in sys.argv:
    threading.Thread(target=media_loop, daemon=True).start()
    if SPECTRUM:
        threading.Thread(target=audio_loop, daemon=True).start()
        threading.Thread(target=gate_loop, daemon=True).start()
    time.sleep(3)
    print("MEDIA:", {k: media_state[k] for k in ("title","artist","album","dur","playing","present")})
    print("gate:", round(gate["v"], 3), "spectrum max:", round(float(spectrum.max()), 3))
    sys.exit(0)

# ---------------- Windows / DPI ----------------
import ctypes
from ctypes import wintypes
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try: ctypes.windll.user32.SetProcessDPIAware()
    except Exception: pass

u = ctypes.windll.user32
u.FindWindowW.restype = wintypes.HWND
u.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
u.FindWindowExW.restype = wintypes.HWND
u.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
u.GetAncestor.restype = wintypes.HWND
u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
u.IsWindow.argtypes = [wintypes.HWND]
u.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
u.GetForegroundWindow.restype = wintypes.HWND
u.MonitorFromWindow.restype = wintypes.HANDLE
u.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
u.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]

class RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wintypes.DWORD)]

def rect_of(h):
    r = RECT(); u.GetWindowRect(h, ctypes.byref(r)); return r
def classname(h):
    b = ctypes.create_unicode_buffer(256); u.GetClassNameW(h, b, 256); return b.value
def find_tray():
    return u.FindWindowW("Shell_TrayWnd", None)
def find_tray_notify(tray):
    return u.FindWindowExW(tray, None, "TrayNotifyWnd", None)

# --- Spotify penceresine WM_APPCOMMAND (next/prev icin) ---
u.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
u.SendMessageW.restype = wintypes.LPARAM
u.IsWindowVisible.argtypes = [wintypes.HWND]
u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
WM_APPCOMMAND = 0x0319

def _spotify_hwnds():
    res = []
    EP = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, l):
        if u.IsWindowVisible(h):
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            try:
                if psutil.Process(pid.value).name().lower() == "spotify.exe":
                    res.append(h)
            except Exception:
                pass
        return True
    u.EnumWindows(EP(cb), 0)
    return res

def _appcmd(cmd):
    for h in _spotify_hwnds():
        u.SendMessageW(h, WM_APPCOMMAND, h, cmd << 16)

def foreground_is_fullscreen():
    fg = u.GetForegroundWindow()
    if not fg or fg == get_top():
        return False
    if classname(fg) in ("WorkerW", "Progman", "Shell_TrayWnd", ""):
        return False
    wr = rect_of(fg)
    mon = u.MonitorFromWindow(fg, 2)
    mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
    u.GetMonitorInfoW(mon, ctypes.byref(mi))
    m = mi.rcMonitor
    return (wr.left <= m.left and wr.top <= m.top and
            wr.right >= m.right and wr.bottom >= m.bottom)

# ---------------- GUI ----------------
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk, ImageDraw

root = tk.Tk()
root.overrideredirect(True)
root.attributes("-topmost", True)
root.configure(bg=BASE_BG)

_tray = find_tray()
H = (rect_of(_tray).bottom - rect_of(_tray).top) if _tray else 40
sw = root.winfo_screenwidth(); sh = root.winfo_screenheight()
root.geometry(f"{WIDTH}x{H}+{sw - 560}+{sh - H}")

cv = tk.Canvas(root, width=WIDTH, height=H, bg=BASE_BG, highlightthickness=0, bd=0)
cv.place(x=0, y=0, relwidth=1, relheight=1)

FTITLE = tkfont.Font(family="Segoe UI Semibold", size=10)
FSUB   = tkfont.Font(family="Segoe UI", size=8)
FSYM   = tkfont.Font(family="Segoe UI Symbol", size=-int(H * 0.5))

cover_disp = {"img": None, "token": None}      # yuvarlatilmis kapak
theme = {"bg": BASE_BG, "fg": WHITE, "sub": GREY}
shown = {"v": False}
PAD = 4
CLICK_MS = 350                                  # tik sayimi penceresi
click = {"n": 0, "job": None}
flash = {"sym": "", "n": 0}                      # kapak uzeri kisa geri bildirim

def get_top():
    return u.GetAncestor(root.winfo_id(), 2)

def read_theme():
    """Arka plani taskbar rengine uydur (koyu/acik tema veya vurgu rengi)."""
    def rd(path, name):
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, path)
            v, _ = winreg.QueryValueEx(k, name); winreg.CloseKey(k); return v
        except Exception:
            return None
    P = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    D = r"Software\Microsoft\Windows\DWM"
    light = rd(P, "SystemUsesLightTheme"); prev = rd(P, "ColorPrevalence"); acc = rd(D, "AccentColor")
    if prev and acc:                       # taskbar'a vurgu rengi aciksa
        r, g, b = acc & 0xFF, (acc >> 8) & 0xFF, (acc >> 16) & 0xFF
        f = 0.55; bg = (int(r*f), int(g*f), int(b*f))
    else:
        bg = (243, 243, 243) if light == 1 else (30, 30, 30)
    lum = 0.299*bg[0] + 0.587*bg[1] + 0.114*bg[2]
    if lum < 140:
        return "#%02x%02x%02x" % bg, WHITE, GREY          # koyu zemin
    return "#%02x%02x%02x" % bg, "#1a1a1a", "#555555"      # acik zemin

def apply_theme():
    bg, fg, sub = read_theme()
    theme["fg"], theme["sub"] = fg, sub
    if bg != theme["bg"]:
        theme["bg"] = bg
        root.configure(bg=bg); cv.configure(bg=bg)

def sync_cover():
    if cover["token"] != cover_disp["token"] and cover["pil"] is not None:
        cs = H - 2 * PAD
        im = cover["pil"].resize((cs, cs), Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (cs, cs), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, cs-1, cs-1], radius=6, fill=255)
        im.putalpha(mask)                  # yuvarlak kose -> koseler taskbar rengini gosterir
        cover_disp["img"] = ImageTk.PhotoImage(im)
        cover_disp["token"] = cover["token"]

def place_over_taskbar():
    tray = find_tray()
    if not tray or not u.IsWindow(tray):
        return
    tb = rect_of(tray)
    tn = find_tray_notify(tray)
    x = (rect_of(tn).left - WIDTH - GAP_FROM_TRAY) if tn else (tb.right - WIDTH - 220)
    if x < tb.left:
        x = tb.left + 8
    SWP = 0x0001 | 0x0010 | 0x0040
    u.SetWindowPos(get_top(), -1, int(x), int(tb.top), 0, 0, SWP)

def ellipsize(s, font, maxpx):
    if not s: return ""
    if font.measure(s) <= maxpx: return s
    while s and font.measure(s + "…") > maxpx:
        s = s[:-1]
    return s + "…"

def draw():
    cv.delete("all")   # arka plan = canvas'in kendi rengi (taskbar'a uyarli)
    x0 = (PAD + (H - 2*PAD) + 12) if cover_disp["img"] is not None else 12
    if cover_disp["img"] is not None:
        cv.create_image(PAD, PAD, anchor="nw", image=cover_disp["img"])
    # ilerleme cubugu (ust 2px)
    dur = media_state["dur"]; prog = (media_state["pos"] / dur) if dur > 0 else 0
    cv.create_rectangle(0, 0, WIDTH, 2, fill=TRACK_GREY, outline="")
    if prog > 0:
        cv.create_rectangle(0, 0, int(WIDTH * min(prog, 1.0)), 2, fill=SPOT_GREEN, outline="")
    # spektrum bolgesi (sag)
    bars_x0 = WIDTH - 8 - BAR_ZONE
    base = H - 4; maxbar = H - 12
    gap = 1.5
    bw = max(1.0, (BAR_ZONE - (BARS - 1) * gap) / BARS)
    for k in range(BARS):
        val = float(spectrum[k]); x = bars_x0 + k * (bw + gap)
        if val > 0.02:
            cv.create_rectangle(x, base - val * maxbar, x + bw, base,
                                fill=SPOT_GREEN, outline="")
    # sarki + sanatci (kapak ile spektrum arasi)
    text_w = bars_x0 - x0 - 10
    title = media_state["title"] or media_state["name"]
    cv.create_text(x0, H*0.34, anchor="w", fill=theme["fg"], font=FTITLE,
                   text=ellipsize(title, FTITLE, text_w))
    cv.create_text(x0, H*0.68, anchor="w", fill=theme["sub"], font=FSUB,
                   text=ellipsize(media_state["artist"], FSUB, text_w))
    # kapak uzeri kisa geri bildirim (tiklama)
    if flash["n"] > 0 and cover_disp["img"] is not None:
        cx = PAD + (H - 2*PAD) / 2; cy = H / 2
        cv.create_text(cx+1, cy+1, text=flash["sym"], fill="#000000", font=FSYM)
        cv.create_text(cx, cy, text=flash["sym"], fill="#ffffff", font=FSYM)

def tick():
    want = media_state["present"] and not foreground_is_fullscreen()
    if want and not shown["v"]:
        u.ShowWindow(get_top(), 4); shown["v"] = True
    elif not want and shown["v"]:
        u.ShowWindow(get_top(), 0); shown["v"] = False
    if shown["v"]:
        apply_theme(); sync_cover(); place_over_taskbar(); draw()
    root.after(1000, tick)

def animate():
    if shown["v"]:
        if flash["n"] > 0:
            flash["n"] -= 1
        draw()
    root.after(45, animate)

def _on_cover(e):
    return e.x <= PAD + (H - 2 * PAD)            # tik kapak bolgesinde mi

def on_click(e):
    if not _on_cover(e):
        return
    click["n"] += 1
    if click["job"]:
        root.after_cancel(click["job"])
    click["job"] = root.after(CLICK_MS, resolve_click)

def resolve_click():
    n = click["n"]; click["n"] = 0; click["job"] = None
    if n == 1:
        send_cmd("toggle"); flash["sym"] = "⏸" if media_state["playing"] else "▶"
    elif n == 2:
        send_cmd("next"); flash["sym"] = "⏭"
    else:                                        # 3+ tik -> onceki
        send_cmd("prev"); flash["sym"] = "⏮"
    flash["n"] = 12                              # ~0.5 sn goster

def on_right(e):
    m = tk.Menu(root, tearoff=0)
    m.add_command(label="Kapat", command=root.destroy)
    m.tk_popup(e.x_root, e.y_root)

cv.bind("<Button-1>", on_click)
cv.bind("<Button-3>", on_right)

threading.Thread(target=media_loop, daemon=True).start()
if SPECTRUM:
    threading.Thread(target=audio_loop, daemon=True).start()
    threading.Thread(target=gate_loop, daemon=True).start()

apply_theme()   # baslangicta arka plani taskbar rengine uydur
u.ShowWindow(get_top(), 0)
root.after(1000, tick)
root.after(300, animate)
root.mainloop()
