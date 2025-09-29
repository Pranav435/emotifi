from setuptools import setup

APP = ['emotifi.py']
DATA_FILES = []
OPTIONS = {
    'argv_emulation': False,
    'includes': [
        'rumps', 'emoji',
        'AppKit', 'Quartz', 'Foundation',
    ],
    'plist': {
        'CFBundleName': 'Emotifi',
        'CFBundleDisplayName': 'Emotifi',
        'CFBundleIdentifier': 'com.emotifi.app',
        'CFBundleShortVersionString': '0.9.0',
        'CFBundleVersion': '0.9.0',
        'LSUIElement': True,  # menu bar app (no Dock icon)
        'NSAppleEventsUsageDescription': 'Emotifi pastes content into other apps when you choose an item.',
    },
    # If you add assets later: 'resources': ['icons', 'whatever']
}

setup(
    app=APP,
    data_files=DATA_FILES,
    options={'py2app': OPTIONS},
    setup_requires=['py2app'],
)
