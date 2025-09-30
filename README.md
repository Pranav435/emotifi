# Emotifi 🎉

*The emoji • GIF • sticker palette Apple forgot — built by a broke college student who refused to pay $20 for paste.*

---

## The Backstory 📖

Hi, I’m Pranav. I’m a college student, which means:

* Empty wallet ✔️
* Expensive laptop ✔️
* macOS “character viewer” that only does basic emojis ❌

But what about **GIFs**? **Stickers**? Actual fun? Apple never added them, and the apps that exist charge too much.

So I hacked together **Emotifi** — a tiny menubar app that gives you a **searchable palette** of emojis, GIFs, stickers, and even your own personal sticker pack. It’s scrappy, it’s open-source, and it makes my Mac (and hopefully yours) less boring.

---

## What Emotifi Does 🚀

* **Global palette** with **CMD+SHIFT+E**
* **Inline picker** with **`::`** anywhere you’re typing
* **Emoji search** with fuzzy matching + synonyms (try: tasty, chai, cricket)
* **GIFs & Stickers** powered by [GIPHY Developers](https://developers.giphy.com)
* **My Stickers** — import any image, auto-converts to PNG ≤512px
* **Paste anywhere**: emojis as text, GIFs/stickers as images, or Option+Enter for links
* **Preferences** to set hotkeys, launch at login, toggle inline capture, and speech mode
* **Onboarding screen** that walks you through Accessibility & Input Monitoring

---

## Install

### Option A — Prebuilt app (easy)

👉 **[Download the latest release](https://github.com/Pranav435/emotifi/releases/latest)**

⚠️ This build is **unsigned** (Apple wants $100/year). The first launch needs a nudge:

1. Move `Emotifi.app` into **Applications**
2. Double-click → macOS complains
3. Go to **System Settings → Privacy & Security → Open Anyway**
4. Relaunch once, then you’re good

Still stuck? Run this in Terminal:

```bash
xattr -dr com.apple.quarantine /Applications/Emotifi.app
```

---

### Option B — From source (for tinkerers)

```bash
git clone https://github.com/Pranav435/emotifi.git
cd emotifi
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

To unlock GIFs & stickers:

* Grab a free API key at [GIPHY Developers](https://developers.giphy.com)
* Add it in one of these ways:

  * `.env` → `GIPHY_API_KEY=your_key`
  * `Info.plist` → add `GIPHYApiKey` when packaged
  * `secrets.json` in resources
  * Environment variable

Run it:

```bash
python emotifi.py
```

---

## Using Emotifi ⌨️

| Action             | Keys            | Notes                                  |
| ------------------ | --------------- | -------------------------------------- |
| Open palette       | **CMD+SHIFT+E** | Changeable in Preferences              |
| Inline summon      | Type **`::`**   | Instant inline search                  |
| Search             | Just type       | Works inline or in palette             |
| Move               | ↑ / ↓           | Speaks selection if TTS is on          |
| Insert             | Enter           | Emoji as text; GIF/sticker as image    |
| Insert as link     | Option+Enter    | For GIFs/stickers                      |
| Close              | Esc             | Cancels palette or inline              |
| Backspace (inline) | Backspace       | Emotifi cleans `::query` automatically |

---

## My Stickers ⭐

Add your own memes, logos, reaction shots:

* Menubar → **Add Sticker…**
* Images get converted & resized to 512px max
* Stored at `~/Library/Application Support/Emotifi/Stickers`
* Shows up in the **⭐ My** tab, searchable by filename

---

## Preferences ⚙️

* **Launch at login** → adds a LaunchAgent
* **Enable inline capture** → toggles `::` trigger
* **Enable global shortcut** → turns hotkey on/off
* **Shortcut record/clear** → set your own combo
* **Speech mode** → Inline only / Always / None

---

## Behind the Curtain 🧑‍💻

* **Python** + `rumps` for menubar magic
* **PyObjC** for native macOS UI
* **emoji** library + synonyms for better search
* **GIPHY API** for GIFs/stickers
* **NSPasteboard** trickery for pasting text/images
* **CGEvent tap** for inline capture (fallback if needed)
* **Prefs** stored at `~/Library/Application Support/Emotifi/prefs.json`

---

## Donate 💸 (make Gatekeeper stop yelling)

Right now, Emotifi is unsigned. macOS yells the first time you open it. To fix that, I need an **Apple Developer ID**: **$100/year**.

If you like Emotifi and want to help me ship a smoother version, you can donate here:

👉 [**Donate via PayPal**](https://paypal.me/theblindiephoenix)

* 🎯 Goal: $100
* ✅ Current: (I’ll update this as donations come in)

Even a small donation helps me as a college student keep this free and fun for everyone.

---

## License 📜

This project is licensed under the [MIT License](./LICENSE).

---

## Contact 👋

Built by **Pranav** (between classes and caffeine).

* YouTube → [Blindie Phoenix](https://youtube.com/@imtheblindiephoenix)
* GitHub → [yourusername](https://github.com/Pranav435)
