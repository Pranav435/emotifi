#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emotifi — menubar Emoji/GIF/Sticker picker for macOS (single-file, Python)

This build adds proper animated GIF/MP4 pasting support:
  • Tries to paste raw GIF/MP4 bytes to NSPasteboard with correct UTIs (keeps animation).
  • Falls back to raster image paste (PNG/TIFF) if the target app doesn’t accept movie/GIF UTIs.
  • Finally falls back to inserting a link if binary pastes are not accepted.
  • Improves GIPHY URL selection (prefers .gif/.mp4 when available).

Other features preserved from the original:
  • Emoji/GIF/Sticker palette with fuzzy emoji search + synonyms.
  • ⭐ My Stickers — import images, auto-convert to ≤512px PNG, searchable & paste-ready.
  • Inline '::' capture via CGEvent tap (preferred) or NSEvent fallback.
  • Backspace cleanup around inline insertion (no stray colons).
  • Global hotkey (⌘⇧E by default), thread-safe dispatch to main.
  • TTS on selection (configurable): Inline only, All capture, or None.
  • Preferences (persisted): launch at login, inline capture toggle, hotkey record/clear, TTS mode.
  • Welcome / Onboarding screen for Accessibility/Input Monitoring.
  • UI polish: banner tips, clean layout, clear empty states.
  • First open after startup always defaults to the Emoji tab.
  • GIPHY key auto-resolves from env/Info.plist/optional secrets.json when packaged.
"""

import os, sys, json, threading, time, difflib, re, subprocess, uuid
import requests
import rumps
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict

# third-party for emoji dataset
import emoji  # pip install emoji

# ---- macOS frameworks (PyObjC) ----
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSPanel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowCollectionBehaviorCanJoinAllSpaces,
    NSFloatingWindowLevel,
    NSSegmentedControl,
    NSSearchField,
    NSScrollView,
    NSTableView,
    NSTableColumn,
    NSTextField,
    NSImageView,
    NSImage,
    NSPasteboard,
    NSScreen,
    NSMakeRect,
    NSEvent,
    NSEventMaskKeyDown,
    NSButton,
    NSWorkspace,
    NSEventModifierFlagCommand,
    NSEventModifierFlagShift,
    NSEventModifierFlagControl,
    NSEventModifierFlagOption,
    NSPasteboardTypeString,
    NSPasteboardTypePNG,
    NSPasteboardTypeTIFF,
    NSNotificationCenter,
    NSControlTextDidChangeNotification,
    NSSpeechSynthesizer,
    NSPopUpButton,
    NSBox,
    NSBezelStyleRounded,
    NSOpenPanel,
    NSBitmapImageFileTypePNG,   # correct PNG enum in PyObjC
    NSAlert,
)
from Foundation import NSObject, NSURL, NSData, NSIndexSet, NSOperationQueue, NSTimer
from objc import super as objc_super
import Quartz
from Quartz import (
    kCGHIDEventTap,
    kCGSessionEventTap,
    kCGHeadInsertEventTap,
    kCGEventKeyDown,
    kCGEventSourceStateHIDSystemState,
    kCGEventFlagMaskCommand,
    kCGEventFlagMaskShift,
    kCGKeyboardEventKeycode,
    CGEventMaskBit,
    CGEventTapCreate,
    CGEventTapEnable,
    CGEventGetFlags,
    CGEventKeyboardGetUnicodeString,
    CGEventGetIntegerValueField,
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CFRunLoopRun,
    CGEventSourceCreate,
    CGEventCreateKeyboardEvent,
    CGEventPost,
)

# --- Accessibility trust (and deep links to settings) ---
AX_PROMPT_AVAILABLE = False
try:
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt  # type: ignore
    AX_PROMPT_AVAILABLE = True
except Exception:
    try:
        from Quartz import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt  # type: ignore
        AX_PROMPT_AVAILABLE = True
    except Exception:
        AX_PROMPT_AVAILABLE = False

def is_accessibility_trusted(prompt: bool = False) -> bool:
    if not AX_PROMPT_AVAILABLE:
        return False
    try:
        opts = {kAXTrustedCheckOptionPrompt: bool(prompt)}
        return bool(AXIsProcessTrustedWithOptions(opts))
    except Exception:
        return False

def open_accessibility_settings():
    try:
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility")
        )
    except Exception:
        pass

def open_inputmonitor_settings():
    try:
        NSWorkspace.sharedWorkspace().openURL_(
            NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent")
        )
    except Exception:
        pass


# --- Build-time / runtime API key resolution ---
def _get_giphy_api_key() -> str:
    # 1) Shell env (dev, or if user set launchctl env)
    k = (os.environ.get("GIPHY_API_KEY") or "").strip()
    if k:
        return k
    # 2) Packaged app’s Info.plist (py2app injected)
    try:
        from Foundation import NSBundle
        info = (NSBundle.mainBundle().infoDictionary() or {})
        k = (info.get("GIPHYApiKey") or
             (info.get("LSEnvironment") or {}).get("GIPHY_API_KEY") or "")
        if k:
            return str(k).strip()
    except Exception:
        pass
    # 3) Optional secrets.json bundled as a resource
    try:
        from Foundation import NSBundle
        bundle = NSBundle.mainBundle()
        rpath = bundle.resourcePath()
        spath = os.path.join(rpath, "secrets.json")
        if os.path.exists(spath):
            import json as _json
            with open(spath, "r", encoding="utf-8") as f:
                kk = (_json.load(f).get("GIPHY_API_KEY") or "").strip()
                if kk:
                    return kk
    except Exception:
        pass
    return ""

GIPHY_API_KEY = _get_giphy_api_key()
TRIGGER_TOKEN = "::"  # inline trigger

# ---- Shared HTTP session + caches ----
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Emotifi/3.3"})
IMG_CACHE: Dict[str, bytes] = {}     # url -> bytes

# ========= Preferences (persisted) =========
APP_ID = "com.emotifi.app"
APP_SUPPORT_DIR = os.path.join(os.path.expanduser("~/Library/Application Support"), "Emotifi")
PREFS_PATH = os.path.join(APP_SUPPORT_DIR, "prefs.json")
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LAUNCH_PLIST = os.path.join(LAUNCH_AGENTS_DIR, f"{APP_ID}.plist")
STICKERS_DIR = os.path.join(APP_SUPPORT_DIR, "Stickers")

DEFAULT_PREFS = {
    "launch_at_login": False,
    "enable_inline": True,
    "enable_hotkey": True,
    "hotkey": "CMD+SHIFT+E",
    "tts_mode": "inline",     # "inline" | "all" | "none"
    "onboard_done": False,    # show welcome screen on first run / until granted
    # New toggle: prefer animated paste (GIF/MP4) when possible
    "prefer_animated": True,
}

def _ensure_dir(p):
    try: os.makedirs(p, exist_ok=True)
    except Exception: pass

def _read_json(p, default):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def _write_json(p, obj):
    tmp = p + ".tmp"
    _ensure_dir(os.path.dirname(p))
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, p)

class Prefs:
    def __init__(self):
        _ensure_dir(APP_SUPPORT_DIR)
        _ensure_dir(STICKERS_DIR)
        self._data = _read_json(PREFS_PATH, DEFAULT_PREFS.copy())

    def get(self, k):
        return self._data.get(k, DEFAULT_PREFS.get(k))

    def set(self, k, v):
        self._data[k] = v
        _write_json(PREFS_PATH, self._data)

    @property
    def launch_at_login(self): return bool(self.get("launch_at_login"))
    @property
    def enable_inline(self): return bool(self.get("enable_inline"))
    @property
    def enable_hotkey(self): return bool(self.get("enable_hotkey"))
    @property
    def hotkey(self): return str(self.get("hotkey") or "CMD+SHIFT+E")
    @property
    def tts_mode(self):
        v = str(self.get("tts_mode") or "inline")
        return v if v in ("inline","all","none") else "inline"
    @property
    def onboard_done(self): return bool(self.get("onboard_done"))
    @property
    def prefer_animated(self): return bool(self.get("prefer_animated"))

PREFS = Prefs()

def _human_hotkey_to_parts(spec: str) -> Tuple[str, int]:
    spec = (spec or "").strip().upper().replace(" ", "")
    if not spec:
        spec = DEFAULT_PREFS["hotkey"]
    parts = [p for p in spec.split("+") if p]
    mods = 0
    key = None
    for p in parts:
        if p in ("CMD", "COMMAND"): mods |= (NSEventModifierFlagCommand)
        elif p in ("SHIFT", "SHF"): mods |= (NSEventModifierFlagShift)
        elif p in ("CTRL", "CONTROL", "CTL"): mods |= (NSEventModifierFlagControl)
        elif p in ("OPT", "OPTION", "ALT"): mods |= (NSEventModifierFlagOption)
        elif p == "SPACE": key = " "
        elif len(p) == 1: key = p.lower()
        elif p in [";", "'", ",", ".", "/", "\\", "[", "]", "-", "="]: key = p
    if not key: key = "e"
    if mods == 0: mods = (NSEventModifierFlagCommand | NSEventModifierFlagShift)
    return key, mods

def _parts_to_human(key: str, mods: int) -> str:
    pieces = []
    if mods & NSEventModifierFlagCommand: pieces.append("CMD")
    if mods & NSEventModifierFlagShift: pieces.append("SHIFT")
    if mods & NSEventModifierFlagOption: pieces.append("OPT")
    if mods & NSEventModifierFlagControl: pieces.append("CTRL")
    if key == " ": pieces.append("SPACE")
    else: pieces.append(key.upper())
    return "+".join(pieces)

def _enable_launch_at_login(enable: bool):
    """Create/remove LaunchAgent to run this script/app on login."""
    try:
        _ensure_dir(LAUNCH_AGENTS_DIR)
        if enable:
            is_frozen = getattr(sys, 'frozen', False)
            if is_frozen:
                program_args = [sys.executable]  # .../Emotifi.app/Contents/MacOS/Emotifi
            else:
                program_args = [sys.executable, os.path.abspath(__file__)]
            args_xml = "".join(f"<string>{a}</string>" for a in program_args)
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>{APP_ID}</string>
    <key>ProgramArguments</key><array>{args_xml}</array>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>{APP_SUPPORT_DIR}/emotifi.log</string>
    <key>StandardErrorPath</key><string>{APP_SUPPORT_DIR}/emotifi.err</string>
    <key>EnvironmentVariables</key><dict><key>GIPHY_API_KEY</key><string>{GIPHY_API_KEY}</string></dict>
</dict></plist>"""
            _ensure_dir(APP_SUPPORT_DIR)
            with open(LAUNCH_PLIST, "w", encoding="utf-8") as f:
                f.write(plist)
            try:
                subprocess.run(["launchctl", "unload", LAUNCH_PLIST], check=False)
                subprocess.run(["launchctl", "load", LAUNCH_PLIST], check=False)
            except Exception:
                pass
        else:
            try:
                subprocess.run(["launchctl", "unload", LAUNCH_PLIST], check=False)
            except Exception:
                pass
            if os.path.exists(LAUNCH_PLIST): os.remove(LAUNCH_PLIST)
        return True
    except Exception:
        return False

# Expose the active input manager to the palette for cleanup
ACTIVE_INPUT = None

# ---------- Models ----------
@dataclass
class ResultItem:
    kind: str        # "emoji" | "gif" | "sticker" | "mystick"
    display: str
    detail: str
    insert_text: str
    thumb_url: Optional[str] = None
    media_url: Optional[str] = None  # may be http(s) URL, file:// URL, or absolute path
    alt_media_url: Optional[str] = None  # e.g., MP4 alternative to GIF

# ---------- Emoji search (improved fuzzy matching + synonyms) ----------
class EmojiSearch:
    _SYNONYMS: Dict[str, str] = {
        "tasty": "yum yummy delicious hungry food drool savoring snack dessert sweet tasty",
        "yummy": "yum tasty delicious drool savoring",
        "hungry": "food eat meal fork knife burger pizza noodles hungry",
        "love": "heart hearts like affection kiss romance",
        "money": "cash dollar bank coin rich bag",
        "laugh": "lol rofl joy tears happy funny",
        "music": "note melody song headphones guitar piano",
        "work": "laptop briefcase office tie chart",
        "travel": "plane flight passport globe beach suitcase",
        "rain": "umbrella cloud drop storm thunder",
        "food": "pizza burger fries noodles taco ramen cake cookie chocolate",
        "party": "tada balloon confetti birthday cake party",
        "happy": "smile grin joy blush sunshine",
        "sad": "cry frown disappointed",
        "angry": "mad rage angry pouting",
        "sick": "mask thermometer sneeze vomit",
        "sport": "soccer football basketball cricket tennis run",
        "cricket": "cricket bat ball",
        "run": "running runner sprint shoe",
        "chai": "tea cup chai hot drink",
        "biryani": "rice food meal spicy",
        "pani": "pani puri golgappa chaat"
    }
    def __init__(self):
        self.rows: List[Tuple[str, str, str, List[str]]] = []  # (char, name, terms_str, tokens)
        for ch, meta in emoji.EMOJI_DATA.items():
            name = (meta.get("en") or meta.get("name") or "").replace(":", " ").strip()
            aliases = meta.get("alias", []) or meta.get("aliases", []) or []
            kw = meta.get("keywords", []) or meta.get("kw", []) or meta.get("tags", []) or []
            pieces: List[str] = []
            if name: pieces.append(name)
            pieces += [a.replace("_", " ") for a in aliases]
            pieces += [k.replace("_", " ") for k in kw]
            terms = " ".join(pieces).lower().strip()
            tokens = self._tokenize(terms)
            if terms:
                self.rows.append((ch, name or ch, terms, tokens))
        # Dedup
        seen = set(); uniq = []
        for ch, name, terms, tokens in self.rows:
            if ch in seen: continue
            seen.add(ch); uniq.append((ch, name, terms, tokens))
        self.rows = uniq

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        return [t for t in re.split(r"[^a-z0-9+]+", text.lower()) if t]

    def _expand_query(self, q: str) -> List[str]:
        base = self._tokenize(q)
        extra: List[str] = []
        for w in base:
            if w in self._SYNONYMS:
                extra += self._tokenize(self._SYNONYMS[w])
        seen = set(); out = []
        for w in base + extra:
            if w not in seen:
                seen.add(w); out.append(w)
        return out

    def search(self, q: str, limit: int = 120) -> List[ResultItem]:
        q = (q or "").strip().lower()
        if not q:
            q = "heart"
        qwords = self._expand_query(q)
        qjoined = " ".join(qwords)
        hits: List[Tuple[float, str, str]] = []
        for ch, name, terms, tokens in self.rows:
            score = 0.0
            if q in terms: score += 6.0
            for w in qwords:
                if w in terms: score += 3.0
                if any(tok.startswith(w) for tok in tokens): score += 2.5
            try:
                s1 = difflib.SequenceMatcher(a=qjoined, b=terms).ratio()
                if s1 >= 0.55: score += (s1 - 0.5) * 6.0
                s2 = difflib.SequenceMatcher(a=" ".join(qwords), b=name.lower()).ratio()
                if s2 >= 0.55: score += (s2 - 0.5) * 4.0
            except Exception: pass
            score += max(0.0, 1.5 - 0.2 * len(name.split()))
            if score > 0:
                hits.append((score, name, ch))
        if not hits and any(w in ("tasty","yum","yummy","food","hungry","snack") for w in qwords):
            for ch, name, terms, tokens in self.rows:
                if any(w in tokens for w in ["yum","yummy","food","snack","pizza","burger","fries","noodles","cookie","chocolate","drooling","savoring"]):
                    hits.append((1.0, name, ch))
        hits.sort(key=lambda x: (-x[0], x[1]))
        return [ResultItem("emoji", f"{ch}  {name}", name, ch) for _, name, ch in hits[:limit]]

# ---------- GIPHY search ----------
class GiphySearch:
    def __init__(self, kind: str):
        self.kind = kind  # 'gif' or 'sticker'
        self.api_key = GIPHY_API_KEY
        self.last_status = None
        self.last_error = None

    def search(self, q: str, limit: int = 25) -> List[ResultItem]:
        self.last_status = None
        self.last_error = None
        if not self.api_key:
            self.last_error = "Missing GIPHY_API_KEY"
            print("[Giphy] No API key set. Provide one via env/Info.plist/secrets.json.")
            return []
        endpoint = "https://api.giphy.com/v1/gifs/search"
        if self.kind == "sticker":
            endpoint = "https://api.giphy.com/v1/stickers/search"
        params = {"api_key": self.api_key, "q": (q or "trending"), "limit": str(limit), "rating": "pg-13", "lang": "en"}
        try:
            r = HTTP.get(endpoint, params=params, timeout=7)
            self.last_status = r.status_code
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            self.last_error = str(e)
            print(f"[Giphy {self.kind}] error: {self.last_error} (status={self.last_status})")
            return []
        out: List[ResultItem] = []
        for item in data.get("data", [])[:limit]:
            title = item.get("title") or self.kind.upper()
            images = item.get("images", {}) or {}
            # Prefer true GIF and MP4 originals when available
            gif_url = (images.get("original", {}) or {}).get("url")
            mp4_url = (images.get("original_mp4", {}) or {}).get("mp4")
            # Nice small preview for thumbnails / quick loads
            preview = images.get("fixed_height_small") or images.get("preview_gif") or images.get("downsized_small") or {}
            thumb_url = preview.get("url")
            media_url = gif_url or mp4_url or thumb_url
            share = item.get("url") or media_url or thumb_url or ""
            out.append(ResultItem(self.kind, title, self.kind.upper(), share, thumb_url, media_url, alt_media_url=mp4_url if gif_url else gif_url))
        if not out:
            print(f"[Giphy {self.kind}] empty results for q='{q}'. status={self.last_status}")
        return out

# ---------- My Stickers search ----------
class MyStickerSearch:
    def __init__(self, folder: str):
        self.folder = folder
        _ensure_dir(self.folder)

    def _all_files(self) -> List[str]:
        try:
            names = [n for n in os.listdir(self.folder) if not n.startswith(".")]
            names.sort(key=lambda n: (0 if n.lower().endswith(".png") else 1, n.lower()))
            return [os.path.join(self.folder, n) for n in names]
        except Exception:
            return []

    def search(self, q: str, limit: int = 120) -> List[ResultItem]:
        q = (q or "").strip().lower()
        files = self._all_files()
        out: List[ResultItem] = []
        for path in files:
            name = os.path.basename(path)
            if q and q not in name.lower():
                continue
            file_url = "file://" + os.path.abspath(path)
            display = os.path.splitext(name)[0]
            out.append(ResultItem("mystick", display, "My Sticker", insert_text=file_url, thumb_url=file_url, media_url=file_url))
            if len(out) >= limit: break
        return out

# ---------- Image & paste helpers ----------

def _nsimage_from_any(uri_or_path: str) -> Optional[NSImage]:
    try:
        if uri_or_path.startswith("file://"):
            path = uri_or_path[7:]
            return NSImage.alloc().initWithContentsOfFile_(path)
        if uri_or_path.startswith("http://") or uri_or_path.startswith("https://"):
            data = IMG_CACHE.get(uri_or_path)
            if data is None:
                r = HTTP.get(uri_or_path, timeout=8); r.raise_for_status(); data = r.content; IMG_CACHE[uri_or_path] = data
            nsdata = NSData.dataWithBytes_length_(data, len(data))
            return NSImage.alloc().initWithData_(nsdata)
        if os.path.exists(uri_or_path):
            return NSImage.alloc().initWithContentsOfFile_(uri_or_path)
    except Exception:
        return None
    return None

def _activate_app_and_sleep(prev_app):
    try:
        if prev_app:
            prev_app.activateWithOptions_(1)  # NSApplicationActivateIgnoringOtherApps
    except Exception:
        pass
    time.sleep(0.08)

def insert_text_via_keystroke_paste(text: str, prev_app=None):
    _activate_app_and_sleep(prev_app)
    pb = NSPasteboard.generalPasteboard()
    pb.clearContents()
    pb.setString_forType_(text, NSPasteboardTypeString)
    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    cmd_down = CGEventCreateKeyboardEvent(src, 0x37, True)  # Cmd
    v_down = CGEventCreateKeyboardEvent(src, 0x09, True)    # v
    v_up = CGEventCreateKeyboardEvent(src, 0x09, False)
    cmd_up = CGEventCreateKeyboardEvent(src, 0x37, False)
    Quartz.CGEventSetFlags(v_down, kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(v_up, kCGEventFlagMaskCommand)
    CGEventPost(0, cmd_down); CGEventPost(0, v_down); CGEventPost(0, v_up); CGEventPost(0, cmd_up)

def paste_image_from_url_or_fallback(url_or_path: Optional[str], prev_app=None) -> bool:
    if not url_or_path: return False
    try:
        img = _nsimage_from_any(url_or_path)
        if not img:
            insert_text_via_keystroke_paste(url_or_path, prev_app=prev_app); return True
        tiff = img.TIFFRepresentation()
        png = None
        try:
            from AppKit import NSBitmapImageRep
            rep = NSBitmapImageRep.imageRepWithData_(tiff)
            png = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        except Exception:
            pass
        _activate_app_and_sleep(prev_app)
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        wrote = False
        if png: wrote = pb.setData_forType_(png, NSPasteboardTypePNG) or wrote
        if tiff: wrote = pb.setData_forType_(tiff, NSPasteboardTypeTIFF) or wrote
        src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        cmd_down = CGEventCreateKeyboardEvent(src, 0x37, True)
        v_down = CGEventCreateKeyboardEvent(src, 0x09, True)
        v_up = CGEventCreateKeyboardEvent(src, 0x09, False)
        cmd_up = CGEventCreateKeyboardEvent(src, 0x37, False)
        Quartz.CGEventSetFlags(v_down, kCGEventFlagMaskCommand)
        Quartz.CGEventSetFlags(v_up, kCGEventFlagMaskCommand)
        CGEventPost(0, cmd_down); CGEventPost(0, v_down); CGEventPost(0, v_up); CGEventPost(0, cmd_up)
        if not wrote:
            insert_text_via_keystroke_paste(url_or_path, prev_app=prev_app)
        return True
    except Exception:
        insert_text_via_keystroke_paste(url_or_path, prev_app=prev_app)
        return True

# --- Animated paste helpers (GIF/MP4) ---
GIF_UTI = "com.compuserve.gif"      # legacy UTI works widely
MP4_UTI = "public.mpeg-4"           # MPEG-4 movie UTI

def _pb_cmd_v(prev_app=None):
    _activate_app_and_sleep(prev_app)
    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    cmd_down = CGEventCreateKeyboardEvent(src, 0x37, True)  # Cmd
    v_down = CGEventCreateKeyboardEvent(src, 0x09, True)    # v
    v_up   = CGEventCreateKeyboardEvent(src, 0x09, False)
    cmd_up = CGEventCreateKeyboardEvent(src, 0x37, False)
    Quartz.CGEventSetFlags(v_down, kCGEventFlagMaskCommand)
    Quartz.CGEventSetFlags(v_up,   kCGEventFlagMaskCommand)
    CGEventPost(0, cmd_down); CGEventPost(0, v_down); CGEventPost(0, v_up); CGEventPost(0, cmd_up)

def paste_raw_bytes(data: bytes, type_identifiers: List[str], prev_app=None) -> bool:
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        wrote_any = False
        # Offer multiple types; some apps sniff one or another.
        for t in type_identifiers:
            try:
                wrote_any = pb.setData_forType_(nsdata, t) or wrote_any
            except Exception:
                pass
        _pb_cmd_v(prev_app)
        return bool(wrote_any)
    except Exception:
        return False

def _download_bytes(url: str) -> Optional[bytes]:
    try:
        data = IMG_CACHE.get(url)
        if data is None:
            r = HTTP.get(url, timeout=12)
            r.raise_for_status()
            data = r.content
            IMG_CACHE[url] = data
        return data
    except Exception:
        return None

def paste_animated_from_url(url: str, prev_app=None) -> bool:
    if not url:
        return False
    lower = url.lower()
    data = _download_bytes(url)
    if not data:
        return False
    # Heuristic by extension/signature
    if lower.endswith(".gif") or data[:6] in (b"GIF89a", b"GIF87a"):
        return paste_raw_bytes(data, [GIF_UTI, "public.data"], prev_app=prev_app)
    if lower.endswith(".mp4"):
        return paste_raw_bytes(data, [MP4_UTI, "public.movie", "public.data"], prev_app=prev_app)
    # Some GIPHY "url" is CDN without extension; try GIF signature first.
    if data[:6] in (b"GIF89a", b"GIF87a"):
        return paste_raw_bytes(data, [GIF_UTI, "public.data"], prev_app=prev_app)
    return False

def backspace(n=1):
    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    for _ in range(n):
        bs_down = CGEventCreateKeyboardEvent(src, 0x33, True)
        bs_up = CGEventCreateKeyboardEvent(src, 0x33, False)
        CGEventPost(0, bs_down); CGEventPost(0, bs_up)

# --- robust PNG resizing/saving ---
def _best_bitmap_rep(nsimg: NSImage):
    try:
        tiff = nsimg.TIFFRepresentation()
        if not tiff:
            return None
        from AppKit import NSBitmapImageRep
        rep = NSBitmapImageRep.imageRepWithData_(tiff)
        return rep
    except Exception:
        return None

def _resize_png_bytes(nsimg: NSImage, max_dim: int = 512) -> Optional[bytes]:
    try:
        rep = _best_bitmap_rep(nsimg)
        if not rep:
            return None
        w = float(rep.pixelsWide())
        h = float(rep.pixelsHigh())
        if w <= 0 or h <= 0:
            return None
        scale = min(max_dim / w, max_dim / h, 1.0)  # shrink only
        new_w = int(round(w * scale))
        new_h = int(round(h * scale))

        from AppKit import NSBitmapImageRep, NSCalibratedRGBColorSpace
        new_rep = NSBitmapImageRep.alloc().initWithBitmapDataPlanes_pixelsWide_pixelsHigh_bitsPerSample_samplesPerPixel_hasAlpha_isPlanar_colorSpaceName_bytesPerRow_bitsPerPixel_(
            None, new_w, new_h, 8, 4, True, False, NSCalibratedRGBColorSpace, 0, 0
        )
        if not new_rep:
            return None

        from AppKit import NSGraphicsContext
        NSGraphicsContext.saveGraphicsState()
        ctx = NSGraphicsContext.graphicsContextWithBitmapImageRep_(new_rep)
        NSGraphicsContext.setCurrentContext_(ctx)
        src_img = NSImage.alloc().initWithSize_((w, h))
        src_img.addRepresentation_(rep)
        src_img.drawInRect_(((0, 0), (new_w, new_h)))
        NSGraphicsContext.restoreGraphicsState()

        png = new_rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
        if not png:
            return None
        return bytes(png)
    except Exception:
        return None

def import_image_as_sticker() -> Optional[str]:
    """Open file dialog, import image, convert to PNG ≤512px, save to STICKERS_DIR."""
    try:
        panel = NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setAllowedFileTypes_(["png","jpg","jpeg","gif","tiff","heic","webp"])
        if panel.runModal() != 1:
            print("[Stickers] Import canceled.")
            return None
        url = panel.URL()
        if not url:
            print("[Stickers] No URL from panel.")
            return None
        path = url.path()
        print(f"[Stickers] Selected: {path}")

        img = _nsimage_from_any(path)
        if not img:
            print("[Stickers] Could not load image via NSImage.")
            return None

        _ensure_dir(STICKERS_DIR)

        base = os.path.splitext(os.path.basename(path))[0]
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_") or "sticker"
        name = f"{safe}_{uuid.uuid4().hex[:6]}.png"
        dest = os.path.join(STICKERS_DIR, name)

        png_bytes = _resize_png_bytes(img, 512)
        if png_bytes:
            with open(dest, "wb") as f:
                f.write(png_bytes)
            print(f"[Stickers] Wrote PNG: {dest}")
            return dest

        rep = _best_bitmap_rep(img)
        if rep:
            from AppKit import NSBitmapImageRep
            png2 = rep.representationUsingType_properties_(NSBitmapImageFileTypePNG, None)
            if png2:
                with open(dest, "wb") as f:
                    f.write(bytes(png2))
                print(f"[Stickers] Wrote PNG (no-resize fallback): {dest}")
                return dest

        print("[Stickers] Failed to produce PNG.")
        return None
    except Exception as e:
        print(f"[Stickers] Import failed: {e}")
        return None

# ---------- Welcome / Onboarding Panel ----------
class WelcomePanel(NSObject):
    """
    First-run screen:
      • Welcomes the user with a slogan
      • Shows permission status (Accessibility, Input Monitoring)
      • Buttons to request/open the right System Settings panes
      • Continue button to proceed (keeps showing until Accessibility granted)
    """
    def initWithOwner_(self, owner):
        self = objc_super(WelcomePanel, self).init()
        if self is None: return None
        self.owner = owner
        self._build_panel()
        return self

    def _build_panel(self):
        w, h = 620, 420
        frame = NSScreen.mainScreen().frame()
        x = frame.size.width / 2 - w / 2
        y = frame.size.height / 2 - h / 2
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            2, True,
        )
        self.panel.setTitle_("Welcome to Emotifi")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        content = self.panel.contentView()

        def label(text, x, y, w_, h_, size=14, bold=False):
            t = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w_, h_))
            t.setStringValue_(text)
            t.setBezeled_(False); t.setDrawsBackground_(False)
            t.setEditable_(False); t.setSelectable_(False)
            try:
                font = t.font()
                if bold:
                    from AppKit import NSFontManager, NSFont
                    fm = NSFontManager.sharedFontManager()
                    font = fm.convertFont_toHaveTrait_(font, 2)  # NSBoldFontMask
                if size != 14:
                    from AppKit import NSFont
                    font = NSFont.systemFontOfSize_(size)
                t.setFont_(font)
            except Exception: pass
            content.addSubview_(t)
            return t

        label("✨ Emotifi", 24, h-64, 400, 28, size=24, bold=True)
        label("Emoji • GIF • Sticker picker for your Mac", 24, h-92, 400, 22, size=14, bold=False)
        label("Set up permissions so the hotkey and inline capture work everywhere.", 24, h-118, 560, 20)

        # Status lines
        self.lbl_ax = label("Accessibility: Checking…", 40, h-170, 480, 20)
        self.lbl_im = label("Input Monitoring: Enable in System Settings → Privacy & Security → Input Monitoring", 40, h-196, 560, 20)

        # Buttons
        self.btn_req_ax = NSButton.alloc().initWithFrame_(NSMakeRect(40, h-238, 200, 30))
        self.btn_req_ax.setTitle_("Request Accessibility")
        self.btn_req_ax.setBezelStyle_(NSBezelStyleRounded)
        self.btn_req_ax.setTarget_(self); self.btn_req_ax.setAction_("requestAX:")
        content.addSubview_(self.btn_req_ax)

        self.btn_open_ax = NSButton.alloc().initWithFrame_(NSMakeRect(250, h-238, 220, 30))
        self.btn_open_ax.setTitle_("Open Accessibility Settings")
        self.btn_open_ax.setBezelStyle_(NSBezelStyleRounded)
        self.btn_open_ax.setTarget_(self); self.btn_open_ax.setAction_("openAX:")
        content.addSubview_(self.btn_open_ax)

        self.btn_open_im = NSButton.alloc().initWithFrame_(NSMakeRect(40, h-276, 200, 30))
        self.btn_open_im.setTitle_("Open Input Monitoring")
        self.btn_open_im.setBezelStyle_(NSBezelStyleRounded)
        self.btn_open_im.setTarget_(self); self.btn_open_im.setAction_("openIM:")
        content.addSubview_(self.btn_open_im)

        # Continue
        self.btn_continue = NSButton.alloc().initWithFrame_(NSMakeRect(w-140, 18, 110, 32))
        self.btn_continue.setTitle_("Continue")
        self.btn_continue.setBezelStyle_(NSBezelStyleRounded)
        self.btn_continue.setTarget_(self); self.btn_continue.setAction_("continue:")
        content.addSubview_(self.btn_continue)

        # Divider
        box = NSBox.alloc().initWithFrame_(NSMakeRect(20, 56, w-40, 1))
        box.setBoxType_(2); content.addSubview_(box)

        self._refresh_status_labels()

    def _refresh_status_labels(self):
        ax_ok = is_accessibility_trusted(False)
        self.lbl_ax.setStringValue_("Accessibility: " + ("✅ Granted" if ax_ok else "❌ Not granted"))

    def requestAX_(self, _):
        _ = is_accessibility_trusted(True)  # prompts if possible
        open_accessibility_settings()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(1.0, self, "refreshLater:", None, False)

    def refreshLater_(self, _):
        self._refresh_status_labels()

    def openAX_(self, _):
        open_accessibility_settings()

    def openIM_(self, _):
        open_inputmonitor_settings()

    def continue_(self, _):
        if is_accessibility_trusted(False):
            PREFS.set("onboard_done", True)
            self.panel.orderOut_(None)
            try:
                self.owner.start_inputs_after_onboarding()
            except Exception:
                pass
        else:
            try:
                alert = NSAlert.alloc().init()
                alert.setMessageText_("Accessibility not granted")
                alert.setInformativeText_("Please grant Accessibility to Emotifi in System Settings to continue. This enables the hotkey and inline capture.")
                alert.runModal()
            except Exception:
                pass
            self._refresh_status_labels()

    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self._refresh_status_labels()

# ---------- Palette window ----------
class PaletteWindow(NSObject):
    def init(self):
        self = objc_super(PaletteWindow, self).init()
        if self is None: return None
        self.current_items: List[ResultItem] = []
        self._tts_enabled = False
        self._tts_policy = PREFS.tts_mode
        self._search_generation = 0
        self._debounce_timer: Optional[threading.Timer] = None
        self._prev_app = None
        self.synth = NSSpeechSynthesizer.alloc().init()
        self.emoji_engine = EmojiSearch()
        self.gif_engine = GiphySearch("gif")
        self.sticker_engine = GiphySearch("sticker")
        self.mystick_engine = MyStickerSearch(STICKERS_DIR)
        self._build_ui()
        self._install_key_monitor_for_panel()
        return self

    def _set_search_placeholder(self):
        mode = self._current_mode()
        ph = {
            "emoji": "Search emoji… (try: tasty, love, chai)",
            "gif": "Search GIFs… (e.g., happy, facepalm)",
            "sticker": "Search stickers…",
            "mystick": "Search My Stickers by name…",
        }[mode]
        try: self.search.setPlaceholderString_(ph)
        except Exception: pass

    def _build_ui(self):
        frame = NSScreen.mainScreen().frame()
        width, height = 680, 460
        x = frame.size.width / 2 - width / 2
        y = frame.size.height / 2 - height / 2
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
            2, True,
        )
        self.panel.setTitle_("Emotifi — 😀 Emoji • 🎞️ GIF • 🗒️ Sticker • ⭐ My")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)

        content = self.panel.contentView()

        banner = NSTextField.alloc().initWithFrame_(NSMakeRect(12, height - 50, width - 24, 24))
        banner.setStringValue_("✨ Tip: Type to search • ↑/↓ to navigate • ⏎ insert • ⌥⏎ insert link • Esc close • “::” for inline")
        banner.setBezeled_(False); banner.setDrawsBackground_(False)
        banner.setEditable_(False); banner.setSelectable_(False)
        content.addSubview_(banner)

        self.mode_seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(12, height - 78, 420, 26))
        self.mode_seg.setSegmentCount_(4)
        self.mode_seg.setLabel_forSegment_("😀 Emoji", 0)
        self.mode_seg.setLabel_forSegment_("🎞️ GIFs", 1)
        self.mode_seg.setLabel_forSegment_("🗒️ Stickers", 2)
        self.mode_seg.setLabel_forSegment_("⭐ My", 3)
        try: self.mode_seg.setTrackingMode_(0)
        except Exception: pass
        try: self.mode_seg.setSelectedSegment_(0)          # default to Emoji tab in UI
        except Exception: self.mode_seg.setSelected_forSegment_(True, 0)
        self.mode_seg.setTarget_(self); self.mode_seg.setAction_("modeChanged:")
        content.addSubview_(self.mode_seg)

        self.search = NSSearchField.alloc().initWithFrame_(NSMakeRect(450, height - 82, width - 462, 30))
        self._set_search_placeholder()
        try: self.search.setContinuous_(True)
        except Exception: pass
        self.search.setTarget_(self); self.search.setAction_("searchFieldChanged:")
        self.search.setDelegate_(self)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "controlTextDidChange:", NSControlTextDidChangeNotification, self.search
        )
        content.addSubview_(self.search)

        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 48, width - 230, height - 140))
        self.scroll.setHasVerticalScroller_(True)
        self.table = NSTableView.alloc().initWithFrame_(self.scroll.bounds())
        col = NSTableColumn.alloc().initWithIdentifier_("main")
        col.setWidth_(self.scroll.frame().size.width - 4); col.setTitle_("Results")
        self.table.addTableColumn_(col)
        self.table.setDelegate_(self); self.table.setDataSource_(self)
        self.table.setTarget_(self); self.table.setAction_("rowClicked:")
        self.scroll.setDocumentView_(self.table)
        content.addSubview_(self.scroll)

        self.preview = NSImageView.alloc().initWithFrame_(NSMakeRect(width - 206, 164, 190, 170))
        content.addSubview_(self.preview)
        self.info = NSTextField.alloc().initWithFrame_(NSMakeRect(width - 206, 48, 190, 100))
        self.info.setBezeled_(False); self.info.setDrawsBackground_(False)
        self.info.setEditable_(False); self.info.setSelectable_(False)
        content.addSubview_(self.info)

        self.panel.setInitialFirstResponder_(self.search)
        self.panel.makeFirstResponder_(self.search)

    def select_tab(self, name: str):
        """Force a tab selection by name and refresh placeholder."""
        idx_map = {"emoji": 0, "gif": 1, "sticker": 2, "mystick": 3}
        idx = idx_map.get(name, 0)
        try:
            self.mode_seg.setSelectedSegment_(idx)
        except Exception:
            self.mode_seg.setSelected_forSegment_(True, idx)
        self._set_search_placeholder()

    def modeChanged_(self, _):
        self._set_search_placeholder()
        self.performSearch()

    def searchFieldChanged_(self, _): self.performSearch()
    def controlTextDidChange_(self, _): self.performSearch()

    def _install_key_monitor_for_panel(self):
        def handler(event):
            try:
                if not self.panel.isKeyWindow():
                    return event
                keycode = event.keyCode()
                flags = int(event.modifierFlags())
                opt_down = (flags & NSEventModifierFlagOption) == NSEventModifierFlagOption
                if keycode == 126:  # ↑
                    self._move_selection(-1); return None
                if keycode == 125:  # ↓
                    self._move_selection(+1); return None
                if keycode in (36, 76):  # Return
                    self.insert_current(link_mode=opt_down); return None
                if keycode == 53:  # Esc
                    self.hide(); return None
            except Exception:
                pass
            return event
        self._local_key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler)

    def _move_selection(self, delta: int):
        n = len(self.current_items)
        if n == 0: return
        row = self.table.selectedRow()
        if row < 0: row = 0
        row = (row + delta) % n
        self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
        self.table.scrollRowToVisible_(row)
        self._speak_selection(row)
        self._update_preview(row)

    def _apply_tts_policy_for_context(self, context: str):
        mode = PREFS.tts_mode
        if mode == "none":
            self._tts_enabled = False
        elif mode == "all":
            self._tts_enabled = True
        elif mode == "inline":
            self._tts_enabled = (context == "inline")
        else:
            self._tts_enabled = (context == "inline")

    def inline_begin(self):
        self._apply_tts_policy_for_context("inline")
        self._prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.search.setStringValue_("")
        self.panel.makeFirstResponder_(self.search)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.02, self, "refocusTimer:", None, False)
        self.performSearch()

    def refocusTimer_(self, _):
        try: self.panel.makeFirstResponder_(self.search)
        except Exception: pass

    def inline_append(self, ch: str):
        s = self.search.stringValue() or ""
        self.search.setStringValue_(s + ch); self.performSearch()

    def inline_backspace(self):
        s = self.search.stringValue() or ""
        if s:
            self.search.setStringValue_(s[:-1]); self.performSearch()

    def rowClicked_(self, _):
        row = self.table.clickedRow()
        if row >= 0:
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            self.insert_current(link_mode=False)

    def _current_mode(self) -> str:
        try:
            idx = int(self.mode_seg.selectedSegment())
            return ["emoji", "gif", "sticker", "mystick"][max(0, min(3, idx))]
        except Exception:
            return "emoji"

    def _empty_hint(self, mode: str, q: str) -> str:
        if mode == "emoji":
            return "No emoji found."
        if mode == "gif":
            return "No GIFs—try another word (e.g., happy, excited)."
        if mode == "sticker":
            return "No stickers—try a different word."
        if mode == "mystick":
            return "No stickers yet. Use Menu → Add Sticker…"
        return "No results."

    def performSearch(self):
        q = self.search.stringValue()
        mode = self._current_mode()
        if mode == "emoji":
            items = self.emoji_engine.search(q)
            self._apply_results_on_main(items, hint=self._empty_hint(mode, q))
            return

        if mode == "mystick":
            items = self.mystick_engine.search(q)
            self._apply_results_on_main(items, hint=self._empty_hint(mode, q))
            return

        # GIF / Sticker (GIPHY) — debounced
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._search_generation += 1
        gen = self._search_generation

        def fire():
            engine = self.gif_engine if mode == "gif" else self.sticker_engine
            items = engine.search(q)
            hint = self._giphy_hint(engine, q) if not items else ""
            def apply():
                if gen == self._search_generation:
                    self._apply_results(items, hint or self._empty_hint(mode, q))
            NSOperationQueue.mainQueue().addOperationWithBlock_(apply)

        self._debounce_timer = threading.Timer(0.22, fire)
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _giphy_hint(self, engine: "GiphySearch", q: str) -> str:
        if engine.last_status in (401, 403): return "Giphy: Unauthorized/Forbidden. Check GIPHY key."
        if engine.last_status in (429,): return "Giphy: Rate limited. Try later."
        if engine.last_status and 500 <= engine.last_status < 600: return "Giphy: server issue. Try again."
        if engine.last_error: return f"Network/API error: {engine.last_error}"
        return f"No {engine.kind}s found for “{q}”."

    def _apply_results_on_main(self, items: List[ResultItem], hint: str):
        NSOperationQueue.mainQueue().addOperationWithBlock_(lambda: self._apply_results(items, hint))

    def _apply_results(self, items: List[ResultItem], hint: str):
        self.current_items = items
        self.table.reloadData()
        if items:
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(0), False)
            self.table.scrollRowToVisible_(0)
            self._speak_selection(0)
            self._update_preview(0)
        else:
            self.preview.setImage_(None)
            self.info.setStringValue_(hint or "No results.")

    # Table datasource/delegate
    def numberOfRowsInTableView_(self, _): return len(self.current_items)
    def tableView_objectValueForTableColumn_row_(self, _, __, row):
        try:
            it = self.current_items[row]; return f"{it.display} — {it.detail}"
        except Exception: return ""
    def tableViewSelectionDidChange_(self, _):
        row = self.table.selectedRow()
        if row >= 0:
            self._speak_selection(row)
            self._update_preview(row)

    def _speak_selection(self, row: int):
        if not self._tts_enabled: return
        try:
            it = self.current_items[row]
            to_say = f"{it.kind}. {it.display}"
            if self.synth.isSpeaking():
                self.synth.stopSpeaking()
            self.synth.startSpeakingString_(to_say)
        except Exception:
            pass

    def _update_preview(self, row: int):
        try:
            it = self.current_items[row]
            self.info.setStringValue_(f"{it.kind.upper()}\n{it.display}\n\nEnter: insert   ⌥Enter: link   Esc: close")
            if it.kind == "emoji":
                self.preview.setImage_(None); return
            url = it.thumb_url or it.media_url or it.insert_text
            if not url:
                self.preview.setImage_(None); return
            img = _nsimage_from_any(url)
            if img:
                self.preview.setImage_(img)
            else:
                self.preview.setImage_(None)
        except Exception: pass

    def hide(self):
        self.panel.orderOut_(None)

    def insert_current(self, link_mode: bool = False):
        row = self.table.selectedRow()
        if 0 <= row < len(self.current_items):
            it = self.current_items[row]
            prev = self._prev_app
            self.hide()
            # Clean & one extra backspace if inline
            try:
                if ACTIVE_INPUT:
                    ACTIVE_INPUT.inline_cleanup_on_insert()
            except Exception: pass
            try:
                if ACTIVE_INPUT and getattr(ACTIVE_INPUT, "last_triggered_inline", False):
                    backspace(1)
            except Exception: pass

            def do_paste():
                if it.kind == "emoji":
                    insert_text_via_keystroke_paste(it.insert_text, prev_app=prev)
                elif it.kind in ("gif", "sticker", "mystick"):
                    media = it.media_url or it.thumb_url or it.insert_text
                    # ⌥Enter → insert link text only (for GIF/Stickers)
                    if link_mode and it.kind != "mystick":
                        insert_text_via_keystroke_paste(media, prev_app=prev)
                    else:
                        # Paste order is configurable via prefer_animated
                        attempted = False
                        if it.kind != "mystick" and PREFS.prefer_animated:
                            attempted = paste_animated_from_url(media, prev_app=prev)
                            # Try alternative media (mp4 <-> gif) if available and first attempt failed
                            if (not attempted) and it.alt_media_url:
                                attempted = paste_animated_from_url(it.alt_media_url, prev_app=prev)
                        if not attempted:
                            # Rasterize (static) paste
                            raster_ok = paste_image_from_url_or_fallback(media, prev_app=prev)
                            if not raster_ok:
                                # Final fallback: just paste link/text
                                insert_text_via_keystroke_paste(media, prev_app=prev)
                try:
                    if ACTIVE_INPUT:
                        ACTIVE_INPUT.last_triggered_inline = False
                except Exception:
                    pass
            threading.Thread(target=do_paste, daemon=True).start()

    # === helper used by menu after import ===
    def show_my_stickers(self, select_basename: Optional[str] = None):
        """Switch to ⭐ My tab, clear search, refresh, and optionally select a filename."""
        try:
            try:
                self.mode_seg.setSelectedSegment_(3)
            except Exception:
                self.mode_seg.setSelected_forSegment_(True, 3)
            self._set_search_placeholder()
            self.search.setStringValue_("")
            self.performSearch()
            if select_basename:
                name_key = os.path.splitext(os.path.basename(select_basename))[0].lower()
                def _select_row():
                    try:
                        for i, it in enumerate(self.current_items):
                            if (it.kind == "mystick") and (name_key in it.display.lower()):
                                self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(i), False)
                                self.table.scrollRowToVisible_(i)
                                self._update_preview(i)
                                break
                    except Exception:
                        pass
                NSOperationQueue.mainQueue().addOperationWithBlock_(_select_row)
        except Exception:
            pass

# ---------- Global input (hotkey + inline capture) ----------
class GlobalInput:
    """
    A) Global hotkey via Cocoa monitors (global + local) — configurable.
    B) Inline '::' capture via CGEvent tap (preferred) or NSEvent fallback — configurable.
    """
    def __init__(self, on_hotkey, palette: PaletteWindow):
        self.on_hotkey = on_hotkey
        self.palette = palette
        self.buffer = deque(maxlen=64)
        self.tap = None
        self._runloop_src = None
        self._nsevent_monitor = None
        self._hotkey_monitor_global = None
        self._hotkey_monitor_local = None
        self._wanted_key, self._wanted_mods = _human_hotkey_to_parts(PREFS.hotkey)
        self.using_tap = False
        self.capturing = False
        self.capture_len = 0
        self.fallback_cleanup = False
        self.last_triggered_inline = False
        self._hotkey_enabled = PREFS.enable_hotkey
        self._inline_enabled = PREFS.enable_inline

    # ---- hotkey helpers ----
    def _mods_match(self, flags: int) -> bool:
        mask = (NSEventModifierFlagCommand | NSEventModifierFlagShift |
                NSEventModifierFlagControl | NSEventModifierFlagOption)
        flags = flags & mask
        return (flags & self._wanted_mods) == self._wanted_mods

    def _key_match(self, ns_event) -> bool:
        s = ns_event.charactersIgnoringModifiers() or ""
        if not s: return False
        key = s.lower()
        if self._wanted_key == " ": return key == " "
        return key and key[0] == self._wanted_key

    def _fire_on_main(self):
        try: NSOperationQueue.mainQueue().addOperationWithBlock_(self.on_hotkey)
        except Exception: pass

    def start_hotkey(self):
        if not self._hotkey_enabled:
            return
        def handler_global(ns_event):
            try:
                if self._mods_match(int(ns_event.modifierFlags())) and self._key_match(ns_event):
                    self._fire_on_main()
            except Exception: pass
        self._hotkey_monitor_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler_global)

        def handler_local(ns_event):
            try:
                if self._mods_match(int(ns_event.modifierFlags())) and self._key_match(ns_event):
                    self._fire_on_main()
                    return None
            except Exception: pass
            return ns_event
        self._hotkey_monitor_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler_local)

    def stop_hotkey(self):
        try:
            if self._hotkey_monitor_local:
                NSEvent.removeMonitor_(self._hotkey_monitor_local)
        except Exception: pass
        try:
            if self._hotkey_monitor_global:
                NSEvent.removeMonitor_(self._hotkey_monitor_global)
        except Exception: pass
        self._hotkey_monitor_local = None
        self._hotkey_monitor_global = None

    def reconfigure_hotkey(self, enabled: bool, human_spec: Optional[str]=None):
        self.stop_hotkey()
        self._hotkey_enabled = enabled
        if human_spec:
            self._wanted_key, self._wanted_mods = _human_hotkey_to_parts(human_spec)
        if enabled:
            self.start_hotkey()

    # ---- inline helpers ----
    def _on_main(self, fn):
        NSOperationQueue.mainQueue().addOperationWithBlock_(fn)

    def _chars_from_cgevent(self, event) -> str:
        try:
            length, ustr = CGEventKeyboardGetUnicodeString(event, 8, None, None)
            if length and ustr:
                return ustr
        except TypeError:
            ustr, length = CGEventKeyboardGetUnicodeString(event, 8, None, None)
            if length and ustr:
                return ustr
        try:
            keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
            flags = CGEventGetFlags(event)
            if int(keycode) == 41:  # semicolon key
                return ":" if (flags & kCGEventFlagMaskShift) else ";"
        except Exception:
            pass
        return ""

    def _start_capture(self, erase_two=True, via_tap=False):
        if not self._inline_enabled:
            return
        self.using_tap = via_tap
        self.capturing = True
        self.capture_len = 0
        self.fallback_cleanup = not via_tap
        self.last_triggered_inline = True
        if erase_two:
            backspace(2)
            threading.Timer(0.02, lambda: backspace(1)).start()
        print("[INLINE] capture started (tap=%s)" % via_tap)
        self._on_main(lambda: self.palette.inline_begin())

    def _finish_capture(self, cleanup=False):
        if cleanup and self.fallback_cleanup:
            backspace(2 + self.capture_len)
        self.capturing = False
        self.capture_len = 0
        self.fallback_cleanup = False
        print("[INLINE] capture finished")

    def _handle_captured_char(self, ch: str):
        if ch == "\x1b":  # Esc
            self._on_main(lambda: self.palette.hide())
            self._finish_capture(cleanup=True)
            return None
        if ch in ["\r", "\n"]:
            self._on_main(lambda: self.palette.insert_current(link_mode=False))
            self._finish_capture(cleanup=True)
            return None
        if ch == "\b":
            if self.capture_len > 0:
                self.capture_len -= 1
                self._on_main(lambda: self.palette.inline_backspace())
            return None
        if ch and ch.isprintable():
            self.capture_len += 1
            self._on_main(lambda ch=ch: self.palette.inline_append(ch))
            return None
        return ch

    def _push_and_check(self, ch: str, erase=False, via_tap=False):
        if not ch: return None
        self.buffer.append(ch)
        if self.capturing:
            return self._handle_captured_char(ch)
        if ''.join(list(self.buffer)[-len(TRIGGER_TOKEN):]) == TRIGGER_TOKEN:
            self._start_capture(erase_two=erase, via_tap=via_tap)
            return None
        return ch

    # CGEvent tap path (best)
    def _start_cgeventtap(self):
        if not self._inline_enabled:
            return False
        def callback(_proxy, etype, event, _refcon):
            try:
                if etype == Quartz.kCGEventKeyDown:
                    keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                    if self.capturing and keycode == 0x33:  # backspace
                        return None if (self._handle_captured_char("\b") is None) else event
                    chars = self._chars_from_cgevent(event)
                    for ch in chars:
                        res = self._push_and_check(ch, erase=True, via_tap=True)
                        if res is None:
                            return None  # swallow
            except Exception:
                pass
            return event

        tap = CGEventTapCreate(kCGSessionEventTap, kCGHeadInsertEventTap, 0, CGEventMaskBit(kCGEventKeyDown), callback, None)
        if not tap:
            tap = CGEventTapCreate(kCGHIDEventTap, kCGHeadInsertEventTap, 0, CGEventMaskBit(kCGEventKeyDown), callback, None)
            if not tap:
                return False
        src = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), src, Quartz.kCFRunLoopCommonModes)
        CGEventTapEnable(tap, True)
        self.tap, self._runloop_src = tap, src
        print("[INLINE] CGEventTap active (erase + swallow). Type :: to start capture.")
        CFRunLoopRun()
        return True

    # NSEvent fallback (cannot swallow)
    def _start_nsevent_monitor(self):
        if not self._inline_enabled:
            return
        def handler(ns_event):
            try:
                s = ns_event.characters() or ""
                if not s:
                    raw = ns_event.charactersIgnoringModifiers() or ""
                    if raw == ";" and (int(ns_event.modifierFlags()) & NSEventModifierFlagShift):
                        s = ":"
                for ch in s:
                    self._push_and_check(ch, erase=False, via_tap=False)
            except Exception:
                pass
        self._nsevent_monitor = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler)
        print("[INLINE] NSEvent monitor active (no swallow). Type :: then query; we’ll clean on insert/cancel).")

    def inline_cleanup_on_insert(self):
        """
        Called by the palette right before inserting.
        Ensures any leaked '::query' (fallback) or stray ':' (tap race) is removed.
        """
        if self.capturing:
            if not self.using_tap:
                backspace(2 + self.capture_len)   # NSEvent fallback: remove '::' + query
            else:
                backspace(1)                       # Tap path: defensively remove one stray ':'
            self.capturing = False
            self.capture_len = 0
            self.fallback_cleanup = False

    def start(self):
        # try to nudge AX status check (non-blocking)
        try:
            if AX_PROMPT_AVAILABLE:
                AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: False})
        except Exception:
            pass

        # start hotkey monitors
        self.start_hotkey()

        def run_tap():
            ok = False
            try:
                ok = self._start_cgeventtap()
            except Exception:
                ok = False
            if not ok and self._inline_enabled:
                print("[WARN] CGEventTap unavailable. Enable Accessibility & Input Monitoring for your app.")
                try:
                    self._start_nsevent_monitor()
                except Exception:
                    print("[ERROR] NSEvent fallback failed. Permissions needed.")
        threading.Thread(target=run_tap, daemon=True).start()

    def set_inline_enabled(self, enabled: bool):
        self._inline_enabled = enabled
        if not enabled:
            try:
                if self._nsevent_monitor:
                    NSEvent.removeMonitor_(self._nsevent_monitor)
            except Exception:
                pass
            self._nsevent_monitor = None

# ---------- Preferences UI ----------
class PreferencesPanel(NSObject):
    def initWithOwner_(self, owner):
        self = objc_super(PreferencesPanel, self).init()
        if self is None: return None
        self.owner = owner  # MenuApp
        self._build_panel()
        self._recording = False
        self._record_monitor = None
        return self

    def _build_panel(self):
        w, h = 580, 420
        frame = NSScreen.mainScreen().frame()
        x = frame.size.width / 2 - w / 2
        y = frame.size.height / 2 - h / 2
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, w, h),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskResizable,
            2, True,
        )
        self.panel.setTitle_("Emotifi Preferences")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)
        content = self.panel.contentView()

        y_cursor = h - 56
        def header(text, y):
            t = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, w-40, 26))
            t.setStringValue_(text)
            t.setBezeled_(False); t.setDrawsBackground_(False)
            t.setEditable_(False); t.setSelectable_(False)
            content.addSubview_(t); return t
        def subtext(text, y):
            t = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y, w-40, 18))
            t.setStringValue_(text)
            t.setBezeled_(False); t.setDrawsBackground_(False)
            t.setEditable_(False); t.setSelectable_(False)
            content.addSubview_(t); return t
        def line(y):
            box = NSBox.alloc().initWithFrame_(NSMakeRect(20, y, w-40, 1))
            box.setBoxType_(2); content.addSubview_(box)

        header("⚙️  General", y_cursor); y_cursor -= 8
        subtext("Tweak capture behavior, launch at login, and speech feedback.", y_cursor-18)
        y_cursor -= 36; line(y_cursor); y_cursor -= 12

        self._pref_map: Dict[int, str] = {}
        self._next_tag = 1
        def checkbox(title, y, key):
            btn = NSButton.alloc().initWithFrame_(NSMakeRect(20, y, 300, 24))
            btn.setButtonType_(3); btn.setTitle_(title)
            btn.setState_(1 if PREFS.get(key) else 0)
            btn.setTarget_(self); btn.setAction_("togglePref:")
            btn.setTag_(self._next_tag); self._pref_map[self._next_tag] = key; self._next_tag += 1
            content.addSubview_(btn); return btn

        self.chk_login  = checkbox("Launch at login",               y_cursor, "launch_at_login"); y_cursor -= 30
        self.chk_inline = checkbox("Enable inline capture (“::”)",  y_cursor, "enable_inline");   y_cursor -= 30
        self.chk_hotkey = checkbox("Enable global shortcut",        y_cursor, "enable_hotkey");   y_cursor -= 30
        self.chk_anim   = checkbox("Prefer animated paste (GIF/MP4) when possible", y_cursor, "prefer_animated"); y_cursor -= 40

        st_label = NSTextField.alloc().initWithFrame_(NSMakeRect(40, y_cursor, 90, 24))
        st_label.setStringValue_("Shortcut:")
        st_label.setBezeled_(False); st_label.setDrawsBackground_(False); st_label.setEditable_(False); st_label.setSelectable_(False)
        content.addSubview_(st_label)

        self.shortcut_field = NSTextField.alloc().initWithFrame_(NSMakeRect(126, y_cursor+1, 190, 24))
        self.shortcut_field.setStringValue_(PREFS.hotkey)
        self.shortcut_field.setEditable_(False); self.shortcut_field.setBezeled_(True)
        content.addSubview_(self.shortcut_field)

        self.btn_record = NSButton.alloc().initWithFrame_(NSMakeRect(320, y_cursor, 110, 26))
        self.btn_record.setBezelStyle_(NSBezelStyleRounded); self.btn_record.setTitle_("Record")
        self.btn_record.setTarget_(self); self.btn_record.setAction_("recordShortcut:")
        content.addSubview_(self.btn_record)

        self.btn_clear = NSButton.alloc().initWithFrame_(NSMakeRect(434, y_cursor, 110, 26))
        self.btn_clear.setBezelStyle_(NSBezelStyleRounded); self.btn_clear.setTitle_("Clear")
        self.btn_clear.setTarget_(self); self.btn_clear.setAction_("clearShortcut:")
        content.addSubview_(self.btn_clear)
        y_cursor -= 46

        header("🗣️  Speech", y_cursor); y_cursor -= 8
        subtext("Choose when Emotifi speaks your current selection.", y_cursor-18)
        y_cursor -= 36; line(y_cursor); y_cursor -= 12

        tts_label = NSTextField.alloc().initWithFrame_(NSMakeRect(20, y_cursor, 110, 24))
        tts_label.setStringValue_("Speech mode:")
        tts_label.setBezeled_(False); tts_label.setDrawsBackground_(False); tts_label.setEditable_(False); tts_label.setSelectable_(False)
        content.addSubview_(tts_label)

        self.popup_tts = NSPopUpButton.alloc().initWithFrame_(NSMakeRect(130, y_cursor-2, 220, 26))
        self.popup_tts.addItemsWithTitles_(["Inline only", "All capture", "None"])
        current = PREFS.tts_mode
        idx = {"inline":0, "all":1, "none":2}.get(current, 0)
        self.popup_tts.selectItemAtIndex_(idx)
        self.popup_tts.setTarget_(self); self.popup_tts.setAction_("changedTTS:")
        content.addSubview_(self.popup_tts)
        y_cursor -= 52

        subtext("Tip: Menu → Add Sticker… to import your images. They appear under the ⭐ My tab.", y_cursor)

    def togglePref_(self, sender):
        try:
            tag = int(sender.tag()); key = self._pref_map.get(tag)
            if not key: return
            val = bool(sender.state())
            PREFS.set(key, val)
            if key == "launch_at_login":
                _enable_launch_at_login(val)
            elif key == "enable_inline":
                self.owner.input.set_inline_enabled(val)
            elif key == "enable_hotkey":
                self.owner.input.reconfigure_hotkey(val, None)
        except Exception:
            pass

    def changedTTS_(self, _):
        idx = int(self.popup_tts.indexOfSelectedItem())
        mode = {0:"inline", 1:"all", 2:"none"}.get(idx, "inline")
        PREFS.set("tts_mode", mode)

    def recordShortcut_(self, _):
        if getattr(self, "_recording", False): return
        self._recording = True
        self.btn_record.setTitle_("Recording…")
        self.shortcut_field.setStringValue_("Press keys…")

        def handler(ns_event):
            try:
                flags = int(ns_event.modifierFlags())
                key = (ns_event.charactersIgnoringModifiers() or "").lower()
                if not key: return None
                if len(key) > 1 and key != " ": key = key[0]
                mods = 0
                if flags & NSEventModifierFlagCommand: mods |= NSEventModifierFlagCommand
                if flags & NSEventModifierFlagShift: mods |= NSEventModifierFlagShift
                if flags & NSEventModifierFlagOption: mods |= NSEventModifierFlagOption
                if flags & NSEventModifierFlagControl: mods |= NSEventModifierFlagControl
                human = _parts_to_human(key, mods)
                self.shortcut_field.setStringValue_(human)
                PREFS.set("hotkey", human)
                if PREFS.enable_hotkey:
                    self.owner.input.reconfigure_hotkey(True, human)
            except Exception:
                pass
            finally:
                try:
                    if self._record_monitor: NSEvent.removeMonitor_(self._record_monitor)
                except Exception: pass
                self._record_monitor = None
                self._recording = False
                self.btn_record.setTitle_("Record")
            return None

        self._record_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(NSEventMaskKeyDown, handler)

    def clearShortcut_(self, _):
        human = DEFAULT_PREFS["hotkey"]
        self.shortcut_field.setStringValue_(human)
        PREFS.set("hotkey", human)
        if PREFS.enable_hotkey:
            self.owner.input.reconfigure_hotkey(True, human)

    def show(self):
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)

# ---------- Menubar app ----------
class MenuApp(rumps.App):
    def __init__(self):
        super().__init__("🎛️", title="")
        self.quit_button = None  # remove default Quit to avoid duplicate

        self.palette = PaletteWindow.alloc().init()
        # Ensure the palette's default tab is Emoji at app startup
        try:
            self.palette.select_tab("emoji")
        except Exception:
            pass

        self.prefs_ui = PreferencesPanel.alloc().initWithOwner_(self)
        self.welcome = WelcomePanel.alloc().initWithOwner_(self)

        self.menu = [
            rumps.MenuItem("Open Palette (⌘⇧E or '::')", callback=self.open_palette_hotkey),
            rumps.MenuItem("Add Sticker…", callback=self.add_sticker),
            rumps.MenuItem("Open Stickers Folder", callback=self.open_sticker_folder),
            rumps.MenuItem("Preferences…", callback=self.open_prefs),
            None,
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]

        self._first_open_done = False

        # Defer starting input capture until after onboarding if needed
        self.input = GlobalInput(self.open_palette_hotkey, self.palette)
        if PREFS.onboard_done and is_accessibility_trusted(False):
            self.input.start()
        else:
            self.show_welcome_then_inputs()

        if PREFS.launch_at_login and not os.path.exists(LAUNCH_PLIST):
            _enable_launch_at_login(True)

        global ACTIVE_INPUT
        ACTIVE_INPUT = self.input

    # Called by WelcomePanel when user presses Continue successfully
    def start_inputs_after_onboarding(self):
        PREFS.set("onboard_done", True)
        try:
            self.input.start()
        except Exception:
            pass

    def show_welcome_then_inputs(self):
        self.welcome.show()

    def open_prefs(self, *_):
        self.prefs_ui.show()

    def open_palette_hotkey(self, *_):
        NSOperationQueue.mainQueue().addOperationWithBlock_(self._open_palette_main)

    def _open_palette_main(self):
        # FIRST OPEN after startup → force Emoji tab
        if not self._first_open_done:
            try:
                self.palette.select_tab("emoji")
            except Exception:
                pass
            self._first_open_done = True

        self.palette._apply_tts_policy_for_context("hotkey")
        self.palette._prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        NSApp.activateIgnoringOtherApps_(True)
        self.palette.panel.makeKeyAndOrderFront_(None)
        self._focus_search_delayed()
        self.palette.performSearch()

    def _focus_search_delayed(self):
        try:
            self.palette.panel.makeFirstResponder_(self.palette.search)
        except Exception:
            pass
        def _refocus_block():
            try:
                self.palette.panel.makeFirstResponder_(self.palette.search)
            except Exception:
                pass
        NSOperationQueue.mainQueue().addOperationWithBlock_(_refocus_block)

    def add_sticker(self, *_):
        path = import_image_as_sticker()
        if path:
            print(f"[Stickers] Imported: {path}")
            try:
                self.palette.show_my_stickers(select_basename=os.path.basename(path))
            except Exception:
                try:
                    if self.palette._current_mode() == "mystick":
                        self.palette.search.setStringValue_("")
                        self.palette.performSearch()
                except Exception:
                    pass

    def open_sticker_folder(self, *_):
        try:
            NSWorkspace.sharedWorkspace().openURL_(NSURL.fileURLWithPath_(STICKERS_DIR))
        except Exception:
            pass

    def quit_app(self, *_):
        rumps.quit_application()

# ---------- Bootstrap ----------
def _run_app():
    # Use the *global* os imported at top of file
    log_path = os.path.expanduser('~/Library/Application Support/Emotifi/launch_crash.log')
    try:
        app = MenuApp()

        # In a bundled app (py2app), LSUIElement in Info.plist already sets policy;
        # avoid resetting it there. Only set policy in dev runs.
        if not getattr(sys, 'frozen', False):
            from AppKit import NSApplicationActivationPolicyAccessory, NSApp
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)

        app.run()
    except Exception:
        import traceback  # ok to import here; DO NOT import os here
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "w") as f:
            traceback.print_exc(file=f)
        print(f"[Emotifi] Crash at launch. See log: {log_path}")
        raise

if __name__ == "__main__":
    _run_app()
