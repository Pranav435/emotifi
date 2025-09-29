import os
from setuptools import setup

APP = ['emotifi.py']
DATA_FILES = []  # add assets like 'secrets.json' here if you prefer file-based
OPTIONS = {
    'argv_emulation': False,
    'includes': ['rumps', 'emoji', 'AppKit', 'Quartz', 'Foundation'],
    'plist': {
        'CFBundleName': 'Emotifi',
        'CFBundleDisplayName': 'Emotifi',
        'CFBundleIdentifier': 'com.emotifi.app',
        'CFBundleShortVersionString': '0.9.0',
        'CFBundleVersion': '0.9.0',
        'LSUIElement': True,  # menubar-only app (no Dock icon)
        'NSAppleEventsUsageDescription': 'Emotifi pastes content into other apps when you choose an item.',

        # 🔑 Bundle the GIPHY API key at build time
        'GIPHYApiKey': os.environ.get('GIPHY_API_KEY', ''),
        'LSEnvironment': {
            'GIPHY_API_KEY': os.environ.get('GIPHY_API_KEY', '')
        },
    },
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
