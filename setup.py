import os
from setuptools import setup

APP = ["emotifi.py"]
DATA_FILES = []

OPTIONS = {
    # Build options
    "argv_emulation": False,
    "optimize": 2,
    "strip": True,

    # Only include what you truly import at runtime
    "includes": [
        "rumps",
        "emoji",
        "AppKit",
        "Quartz",
        "Foundation",
        "requests", "certifi", "urllib3", "idna", "charset_normalizer",
    ],

    # If a whole package directory must be copied verbatim, list it here.
    # (Most of yours don’t need this; requests stack is handled via includes.)
    "packages": [
        "emoji",
        "rumps",
    ],

    # Actively exclude build-time stuff that causes duplicate dist-info folders
    "excludes": [
        # packaging/build tools
        "setuptools",
        "pkg_resources",
        "wheel",
        "distutils",
        "pip",
        # test suites and samples
        "test",
        "tests",
        "unittest",
        "tkinter",
        # rarely-needed stdlib helpers that balloon size
        "lib2to3",
        "pydoc_data",
        "email.mime.application",  # harmless to exclude and avoids extra data
    ],

    "plist": {
        "CFBundleName": "Emotifi",
        "CFBundleDisplayName": "Emotifi",
        "CFBundleIdentifier": "com.emotifi.app",
        "CFBundleShortVersionString": "0.9.0",
        "CFBundleVersion": "0.9.0",
        "LSUIElement": True,
        "NSAppleEventsUsageDescription": "Emotifi pastes content into other apps when you choose an item.",
        # put key at build time
        "GIPHYApiKey": os.environ.get("GIPHY_API_KEY", ""),
        "LSEnvironment": {"GIPHY_API_KEY": os.environ.get("GIPHY_API_KEY", "")},
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={"py2app": OPTIONS},
    setup_requires=["py2app"],
)
