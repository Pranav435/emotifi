#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Emotifi — menubar Emoji/GIF/Sticker picker for macOS (single-file, Python)

This build:
  • Massive emoji dataset + smarter matching via `emoji` package.
  • Focus always lands on Search (immediate + delayed first-responder fix).
  • Inline '::' capture with CGEvent tap (preferred) or NSEvent fallback.
  • Guaranteed cleanup of stray ':' or leaked '::query' on insert.
  • NEW: If palette was opened via '::', press Backspace once right before pasting
         for emoji, GIFs, and stickers (fixes lingering ':' in some apps).
  • Arrow keys navigate even when the search field is focused.
  • System TTS via NSSpeechSynthesizer (inline mode only).
  • Click-to-insert with true paste; GIF/Sticker preview + caching.
  • FIX: Global hotkey (⌘⇧E) dispatches UI to main thread.
  • Emoji search: fuzzier matching + synonyms mapping (e.g., 'tasty' finds 😋 🤤 🍕).

Setup
  pip install --upgrade pyobjc rumps requests emoji
  export GIPHY_API_KEY="YOUR_KEY"
  # optional: export EMOTIFI_HOTKEY="CMD+SHIFT+E"
  python emotifi.py
"""

import os, threading, time, difflib, re
import requests
import rumps
from collections import deque
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Iterable

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

# --- Accessibility trust (optional; hotkey works regardless) ---
AX_PROMPT_FN = None
try:
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt  # type: ignore
    AX_PROMPT_FN = lambda: AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
except Exception:
    try:
        from Quartz import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt  # type: ignore
        AX_PROMPT_FN = lambda: AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    except Exception:
        AX_PROMPT_FN = None

GIPHY_API_KEY = os.environ.get("GIPHY_API_KEY", "").strip()
TRIGGER_TOKEN = "::"  # inline trigger

# ---- Shared HTTP session + caches ----
HTTP = requests.Session()
HTTP.headers.update({"User-Agent": "Emotifi/2.3"})
IMG_CACHE: Dict[str, bytes] = {}     # url -> bytes

# Expose the active input manager to the palette for cleanup
ACTIVE_INPUT = None

# ---------- Hotkey config helper ----------
def _hotkey_from_env() -> Tuple[str, int]:
    spec = os.environ.get("EMOTIFI_HOTKEY", "").strip()
    if not spec:
        return "e", (NSEventModifierFlagCommand | NSEventModifierFlagShift)
    spec = spec.replace(" ", "").upper()
    parts = [p for p in spec.split("+") if p]
    mods = 0
    key = None
    for p in parts:
        if p in ("CMD", "COMMAND"): mods |= NSEventModifierFlagCommand
        elif p in ("SHIFT", "SHF"): mods |= NSEventModifierFlagShift
        elif p in ("CTRL", "CONTROL", "CTL"): mods |= NSEventModifierFlagControl
        elif p in ("OPT", "OPTION", "ALT"): mods |= NSEventModifierFlagOption
        elif p == "SPACE": key = " "
        elif len(p) == 1: key = p.lower()
        elif p in [";", "'", ",", ".", "/", "\\", "[", "]", "-", "="]: key = p
    if not key: key = "e"
    if mods == 0: mods = (NSEventModifierFlagCommand | NSEventModifierFlagShift)
    return key, mods


# ---------- Models ----------
@dataclass
class ResultItem:
    kind: str        # "emoji" | "gif" | "sticker"
    display: str
    detail: str
    insert_text: str
    thumb_url: Optional[str] = None
    media_url: Optional[str] = None


# ---------- Emoji search (improved fuzzy matching + synonyms) ----------
class EmojiSearch:
    """
    Builds a lightweight search index over emoji.EMOJI_DATA with:
      - tokenized name/aliases/keywords
      - partial matches ('in' and startswith)
      - fuzzy similarity via difflib
      - simple synonyms/expansions (e.g., 'tasty' -> 'yum delicious hungry drool savoring food')
    """
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
        "run": "running runner sprint shoe"
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

        # Deduplicate by emoji char (keep first)
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
        # Unique, keep order
        seen = set()
        out = []
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

        hits: List[Tuple[float, str, str]] = []  # (score, name, ch)
        for ch, name, terms, tokens in self.rows:
            score = 0.0

            # Exact/substring match boosts
            if q in terms:
                score += 6.0
            for w in qwords:
                if w in terms:
                    score += 3.0
                # prefix boosts
                if any(tok.startswith(w) for tok in tokens):
                    score += 2.5

            # Fuzzy similarity between query and name/terms (cheap)
            try:
                s1 = difflib.SequenceMatcher(a=qjoined, b=terms).ratio()
                if s1 >= 0.55:
                    score += (s1 - 0.5) * 6.0  # up to ~+3
                s2 = difflib.SequenceMatcher(a=" ".join(qwords), b=name.lower()).ratio()
                if s2 >= 0.55:
                    score += (s2 - 0.5) * 4.0
            except Exception:
                pass

            # Short, iconic names (e.g., 'pizza') get a tiny bump
            score += max(0.0, 1.5 - 0.2 * len(name.split()))

            if score > 0:
                hits.append((score, name, ch))

        # If nothing matched, try falling back to food/music/sport themed packs for some common words
        if not hits and any(w in ("tasty", "yum", "yummy", "food", "hungry", "snack") for w in qwords):
            for ch, name, terms, tokens in self.rows:
                if any(w in tokens for w in ["yum", "yummy", "food", "snack", "pizza", "burger", "fries", "noodles", "cookie", "chocolate", "drooling", "savoring"]):
                    hits.append((1.0, name, ch))

        hits.sort(key=lambda x: (-x[0], x[1]))
        items = [ResultItem("emoji", f"{ch}  {name}", name, ch) for _, name, ch in hits[:limit]]
        return items


# ---------- GIPHY search ----------
class GiphySearch:
    def __init__(self, kind: str):
        self.kind = kind  # 'gif' or 'sticker'
        self.api_key = os.environ.get("GIPHY_API_KEY", "").strip()
        self.last_status = None
        self.last_error = None

    def search(self, q: str, limit: int = 25) -> List[ResultItem]:
        self.last_status = None
        self.last_error = None
        if not self.api_key:
            self.last_error = "Missing GIPHY_API_KEY"
            print("[Giphy] No API key set. Export GIPHY_API_KEY first.")
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
            preview = images.get("fixed_height_small") or images.get("preview_gif") or images.get("downsized_small") or {}
            thumb_url = preview.get("url")
            media_url = (images.get("original", {}) or {}).get("url") or thumb_url
            share = item.get("url") or media_url or thumb_url or ""
            out.append(ResultItem(self.kind, title, self.kind.upper(), share, thumb_url, media_url))
        if not out:
            print(f"[Giphy {self.kind}] empty results for q='{q}'. status={self.last_status}")
        return out


# ---------- Paste helpers ----------
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

def paste_image_from_url_or_fallback(url: Optional[str], prev_app=None) -> bool:
    if not url: return False
    try:
        data = IMG_CACHE.get(url)
        if data is None:
            r = HTTP.get(url, timeout=8); r.raise_for_status(); data = r.content; IMG_CACHE[url] = data
        _activate_app_and_sleep(prev_app)
        nsdata = NSData.dataWithBytes_length_(data, len(data))
        img = NSImage.alloc().initWithData_(nsdata)
        if not img:
            insert_text_via_keystroke_paste(url, prev_app=prev_app); return True
        tiff = img.TIFFRepresentation()
        png = None
        try:
            from AppKit import NSBitmapImageRep
            rep = NSBitmapImageRep.imageRepWithData_(tiff)
            png = rep.representationUsingType_properties_(NSBitmapImageRep.NSPNGFileType, None)
        except Exception:
            pass
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        wrote = False
        if png: wrote = pb.setData_forType_(png, NSPasteboardTypePNG) or wrote
        if tiff: wrote = pb.setData_forType_(tiff, NSPasteboardTypeTIFF) or wrote
        # paste
        src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
        cmd_down = CGEventCreateKeyboardEvent(src, 0x37, True)
        v_down = CGEventCreateKeyboardEvent(src, 0x09, True)
        v_up = CGEventCreateKeyboardEvent(src, 0x09, False)
        cmd_up = CGEventCreateKeyboardEvent(src, 0x37, False)
        Quartz.CGEventSetFlags(v_down, kCGEventFlagMaskCommand)
        Quartz.CGEventSetFlags(v_up, kCGEventFlagMaskCommand)
        CGEventPost(0, cmd_down); CGEventPost(0, v_down); CGEventPost(0, v_up); CGEventPost(0, cmd_up)
        if not wrote:
            insert_text_via_keystroke_paste(url, prev_app=prev_app)
        return True
    except Exception:
        insert_text_via_keystroke_paste(url, prev_app=prev_app)
        return True

def backspace(n=1):
    src = CGEventSourceCreate(kCGEventSourceStateHIDSystemState)
    for _ in range(n):
        bs_down = CGEventCreateKeyboardEvent(src, 0x33, True)
        bs_up = CGEventCreateKeyboardEvent(src, 0x33, False)
        CGEventPost(0, bs_down); CGEventPost(0, bs_up)


# ---------- Palette window ----------
class PaletteWindow(NSObject):
    def init(self):
        self = objc_super(PaletteWindow, self).init()
        if self is None: return None
        self.current_items: List[ResultItem] = []
        self._tts_enabled = False
        self._search_generation = 0
        self._debounce_timer: Optional[threading.Timer] = None
        self._prev_app = None
        # System default TTS
        self.synth = NSSpeechSynthesizer.alloc().init()
        self.emoji_engine = EmojiSearch()
        self.gif_engine = GiphySearch("gif")
        self.sticker_engine = GiphySearch("sticker")
        self._build_ui()
        self._install_key_monitor_for_panel()
        return self

    # --- UI ---
    def _build_ui(self):
        frame = NSScreen.mainScreen().frame()
        width, height = 560, 440
        x = frame.size.width / 2 - width / 2
        y = frame.size.height / 2 - height / 2
        self.panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, width, height),
            NSWindowStyleMaskTitled | NSWindowStyleMaskClosable | NSWindowStyleMaskMiniaturizable,
            2, True,
        )
        self.panel.setTitle_("Emoji • GIF • Sticker")
        self.panel.setLevel_(NSFloatingWindowLevel)
        self.panel.setCollectionBehavior_(NSWindowCollectionBehaviorCanJoinAllSpaces)

        content = self.panel.contentView()

        self.mode_seg = NSSegmentedControl.alloc().initWithFrame_(NSMakeRect(12, height - 44, 260, 24))
        self.mode_seg.setSegmentCount_(3)
        self.mode_seg.setLabel_forSegment_("Emoji", 0)
        self.mode_seg.setLabel_forSegment_("GIFs", 1)
        self.mode_seg.setLabel_forSegment_("Stickers", 2)
        try: self.mode_seg.setTrackingMode_(0)
        except Exception: pass
        try: self.mode_seg.setSelectedSegment_(0)
        except Exception: self.mode_seg.setSelected_forSegment_(True, 0)
        self.mode_seg.setTarget_(self); self.mode_seg.setAction_("modeChanged:")
        content.addSubview_(self.mode_seg)

        self.search = NSSearchField.alloc().initWithFrame_(NSMakeRect(280, height - 46, width - 292, 28))
        self.search.setPlaceholderString_("Search…  ↑/↓: navigate   ⏎: insert   ⌥⏎: link   Esc: close")
        try: self.search.setContinuous_(True)
        except Exception: pass
        self.search.setTarget_(self); self.search.setAction_("searchFieldChanged:")
        self.search.setDelegate_(self)
        NSNotificationCenter.defaultCenter().addObserver_selector_name_object_(
            self, "controlTextDidChange:", NSControlTextDidChangeNotification, self.search
        )
        content.addSubview_(self.search)

        self.scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 40, width - 220, height - 100))
        self.scroll.setHasVerticalScroller_(True)
        self.table = NSTableView.alloc().initWithFrame_(self.scroll.bounds())
        col = NSTableColumn.alloc().initWithIdentifier_("main")
        col.setWidth_(self.scroll.frame().size.width - 4); col.setTitle_("Results")
        self.table.addTableColumn_(col)
        self.table.setDelegate_(self); self.table.setDataSource_(self)
        self.table.setTarget_(self); self.table.setAction_("rowClicked:")
        self.scroll.setDocumentView_(self.table)
        content.addSubview_(self.scroll)

        self.preview = NSImageView.alloc().initWithFrame_(NSMakeRect(width - 200, 140, 180, 180))
        content.addSubview_(self.preview)
        self.info = NSTextField.alloc().initWithFrame_(NSMakeRect(width - 200, 12, 180, 120))
        self.info.setBezeled_(False); self.info.setDrawsBackground_(False)
        self.info.setEditable_(False); self.info.setSelectable_(False)
        content.addSubview_(self.info)

        self.btn_access = NSButton.alloc().initWithFrame_(NSMakeRect(width - 200, height - 78, 180, 24))
        self.btn_access.setTitle_("Open Accessibility"); self.btn_access.setTarget_(self)
        self.btn_access.setAction_("openAccessibility:"); content.addSubview_(self.btn_access)

        self.btn_input = NSButton.alloc().initWithFrame_(NSMakeRect(width - 200, height - 110, 180, 24))
        self.btn_input.setTitle_("Open Input Monitoring"); self.btn_input.setTarget_(self)
        self.btn_input.setAction_("openInputMon:"); content.addSubview_(self.btn_input)

        # Ensure search gets focus immediately and stays there
        self.panel.setInitialFirstResponder_(self.search)
        self.panel.makeFirstResponder_(self.search)

    # Settings deeplinks
    def openAccessibility_(self, _):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))
    def openInputMon_(self, _):
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_("x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"))

    # Actions
    def modeChanged_(self, _): self.performSearch()
    def searchFieldChanged_(self, _): self.performSearch()
    def controlTextDidChange_(self, _): self.performSearch()

    # Arrow keys + Enter/Esc regardless of focus
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

    # Inline capture API — called on main thread
    def inline_begin(self):
        self._tts_enabled = True
        self._prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        NSApp.activateIgnoringOtherApps_(True)
        self.panel.makeKeyAndOrderFront_(None)
        self.search.setStringValue_("")
        self.panel.makeFirstResponder_(self.search)
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(0.02, self, "refocusTimer:", None, False)
        self.performSearch()

    def refocusTimer_(self, _):  # called by NSTimer
        try: self.panel.makeFirstResponder_(self.search)
        except Exception: pass

    def inline_append(self, ch: str):
        s = self.search.stringValue() or ""
        self.search.setStringValue_(s + ch); self.performSearch()

    def inline_backspace(self):
        s = self.search.stringValue() or ""
        if s:
            self.search.setStringValue_(s[:-1]); self.performSearch()

    # Table clicks insert immediately
    def rowClicked_(self, _):
        row = self.table.clickedRow()
        if row >= 0:
            self.table.selectRowIndexes_byExtendingSelection_(NSIndexSet.indexSetWithIndex_(row), False)
            self.insert_current(link_mode=False)

    def _current_mode(self) -> str:
        try:
            idx = int(self.mode_seg.selectedSegment())
            return ["emoji", "gif", "sticker"][max(0, min(2, idx))]
        except Exception:
            return "emoji"

    # Search logic (debounced for network kinds)
    def performSearch(self):
        q = self.search.stringValue()
        mode = self._current_mode()

        if mode == "emoji":
            items = self.emoji_engine.search(q)
            self._apply_results_on_main(items, hint=("No emoji found." if not items else ""))
            return

        if self._debounce_timer: self._debounce_timer.cancel()
        self._search_generation += 1
        gen = self._search_generation

        def fire():
            engine = self.gif_engine if mode == "gif" else self.sticker_engine
            items = engine.search(q)
            hint = self._giphy_hint(engine, q) if not items else ""
            def apply():
                if gen == self._search_generation:
                    self._apply_results(items, hint)
            NSOperationQueue.mainQueue().addOperationWithBlock_(apply)

        self._debounce_timer = threading.Timer(0.22, fire)
        self._debounce_timer.daemon = True
        self._debounce_timer.start()

    def _giphy_hint(self, engine: "GiphySearch", q: str) -> str:
        if engine.last_status in (401, 403): return "Giphy: Unauthorized/Forbidden. Check GIPHY_API_KEY."
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

    # TTS (system default voice), inline only
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

    # Preview (cached)
    def _update_preview(self, row: int):
        try:
            it = self.current_items[row]
            self.info.setStringValue_(f"{it.kind.upper()}\n{it.display}\n\nEnter: insert   ⌥Enter: link   Esc: close")
            if it.kind == "emoji":
                self.preview.setImage_(None); return
            url = it.thumb_url or it.media_url
            if not url: self.preview.setImage_(None); return
            data = IMG_CACHE.get(url)
            if data is None:
                def load():
                    try:
                        r = HTTP.get(url, timeout=6); r.raise_for_status(); data2 = r.content; IMG_CACHE[url] = data2
                    except Exception: data2 = None
                    def apply():
                        if data2:
                            nsdata = NSData.dataWithBytes_length_(data2, len(data2))
                            img = NSImage.alloc().initWithData_(nsdata)
                            self.preview.setImage_(img)
                        else:
                            self.preview.setImage_(None)
                    NSOperationQueue.mainQueue().addOperationWithBlock_(apply)
                threading.Thread(target=load, daemon=True).start()
            else:
                nsdata = NSData.dataWithBytes_length_(data, len(data))
                img = NSImage.alloc().initWithData_(nsdata)
                self.preview.setImage_(img)
        except Exception: pass

    def hide(self):
        self.panel.orderOut_(None)

    def insert_current(self, link_mode: bool = False):
        row = self.table.selectedRow()
        if 0 <= row < len(self.current_items):
            it = self.current_items[row]
            prev = self._prev_app
            self.hide()

            # Clean up inline text (stray ':' or '::query')
            try:
                if ACTIVE_INPUT:
                    ACTIVE_INPUT.inline_cleanup_on_insert()
            except Exception:
                pass

            # NEW: Always backspace once right before paste if palette was opened via '::'
            try:
                if ACTIVE_INPUT and getattr(ACTIVE_INPUT, "last_triggered_inline", False):
                    backspace(1)
            except Exception:
                pass

            def do_paste():
                if it.kind == "emoji":
                    insert_text_via_keystroke_paste(it.insert_text, prev_app=prev)
                else:
                    if link_mode:
                        insert_text_via_keystroke_paste(it.media_url or it.insert_text, prev_app=prev)
                    else:
                        ok = paste_image_from_url_or_fallback(it.media_url or it.thumb_url or it.insert_text, prev_app=prev)
                        if not ok:
                            insert_text_via_keystroke_paste(it.media_url or it.insert_text, prev_app=prev)
                # reset inline flag after the insert
                try:
                    if ACTIVE_INPUT:
                        ACTIVE_INPUT.last_triggered_inline = False
                except Exception:
                    pass
            threading.Thread(target=do_paste, daemon=True).start()


# ---------- Global input (hotkey + inline capture) ----------
class GlobalInput:
    """
    A) Global hotkey (default ⌘⇧E) via Cocoa monitors (global + local)
    B) Inline '::' capture via CGEvent tap (preferred) or NSEvent fallback.
       UI calls are dispatched to the main thread. Adds colon clean-up.
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
        self._wanted_key, self._wanted_mods = _hotkey_from_env()
        self.using_tap = False
        self.capturing = False
        self.capture_len = 0
        self.fallback_cleanup = False
        # NEW: track whether this session was triggered by inline '::'
        self.last_triggered_inline = False

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

    def start_hotkey(self):
        """Start global+local monitors; always dispatch on_hotkey to main thread."""
        def fire_on_main():
            try:
                NSOperationQueue.mainQueue().addOperationWithBlock_(self.on_hotkey)
            except Exception:
                pass

        def handler_global(ns_event):
            try:
                if self._mods_match(int(ns_event.modifierFlags())) and self._key_match(ns_event):
                    fire_on_main()   # ensure UI on main thread
            except Exception:
                pass

        self._hotkey_monitor_global = NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handler_global
        )

        def handler_local(ns_event):
            try:
                if self._mods_match(int(ns_event.modifierFlags())) and self._key_match(ns_event):
                    fire_on_main()   # ensure UI on main thread
                    return None       # swallow locally
            except Exception:
                pass
            return ns_event

        self._hotkey_monitor_local = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
            NSEventMaskKeyDown, handler_local
        )

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
            if int(keycode) == 41:  # semicolon
                return ":" if (flags & kCGEventFlagMaskShift) else ";"
        except Exception:
            pass
        return ""

    def _start_capture(self, erase_two=True, via_tap=False):
        self.using_tap = via_tap
        self.capturing = True
        self.capture_len = 0
        self.fallback_cleanup = not via_tap
        self.last_triggered_inline = True  # mark inline session
        if erase_two:
            backspace(2)         # erase '::'
            threading.Timer(0.02, lambda: backspace(1)).start()  # extra guard
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
        try:
            if AX_PROMPT_FN: AX_PROMPT_FN()
        except Exception:
            pass

        # start hotkey monitors (now thread-safe)
        self.start_hotkey()

        def run_tap():
            ok = False
            try:
                ok = self._start_cgeventtap()
            except Exception:
                ok = False
            if not ok:
                print("[WARN] CGEventTap unavailable. Enable Accessibility & Input Monitoring for your venv Python and Terminal/VSCode.")
                try:
                    self._start_nsevent_monitor()
                except Exception:
                    print("[ERROR] NSEvent fallback failed. Permissions needed.")
        threading.Thread(target=run_tap, daemon=True).start()


# ---------- Menubar app ----------
class MenuApp(rumps.App):
    def __init__(self):
        super().__init__("🎛️", title="")
        self.palette = PaletteWindow.alloc().init()
        self.menu = [
            rumps.MenuItem("Open Palette (⌘⇧E or '::')", callback=self.open_palette_hotkey),
            rumps.MenuItem("Open Accessibility Settings", callback=lambda _: self.palette.openAccessibility_(None)),
            rumps.MenuItem("Open Input Monitoring", callback=lambda _: self.palette.openInputMon_(None)),
            rumps.MenuItem("Quit", callback=self.quit_app),
        ]
        self.input = GlobalInput(self.open_palette_hotkey, self.palette)
        self.input.start()

        # Expose input manager globally so the palette can call cleanup on insert
        global ACTIVE_INPUT
        ACTIVE_INPUT = self.input

    def open_palette_hotkey(self, *_):
        # Always hop to main thread; hotkey may come from a non-UI thread.
        NSOperationQueue.mainQueue().addOperationWithBlock_(self._open_palette_main)

    def _open_palette_main(self):
        # Show the palette and focus search. No return value (void) to satisfy PyObjC block expectations.
        self.palette._tts_enabled = False
        self.palette._prev_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        NSApp.activateIgnoringOtherApps_(True)
        self.palette.panel.makeKeyAndOrderFront_(None)
        self._focus_search_delayed()
        self.palette.performSearch()

    def _focus_search_delayed(self):
        # Put focus immediately and then once more on the main queue shortly after.
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

    def quit_app(self, *_):
        rumps.quit_application()


# ---------- Bootstrap ----------
if __name__ == "__main__":
    app = MenuApp()
    NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    app.run()
