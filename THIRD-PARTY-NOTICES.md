# Third-party components

SmartOps bundles Python and packages installed from their upstream distributions. Their bundled license notices remain under `_internal` where included by the packaging tools.

- Python: Python Software Foundation License.
- PySide6 / Qt: LGPLv3 / GPLv3 / commercial options. This package uses dynamically loaded, unmodified Qt libraries. Upstream source and license information: https://www.qt.io/licensing/ and https://code.qt.io/pyside/pyside-setup.git/ . Users may replace the corresponding compatible shared libraries and reverse-engineer for debugging modifications as permitted by the LGPL.
- Playwright: Apache License 2.0. https://github.com/microsoft/playwright-python
- openpyxl: MIT. https://openpyxl.readthedocs.io/
- PyYAML: MIT. https://github.com/yaml/pyyaml
- pywin32: Python Software Foundation / project license. https://github.com/mhammond/pywin32
- uiautomation: Apache License 2.0. https://pypi.org/project/uiautomation/
- pynput: LGPLv3. https://pypi.org/project/pynput/
- Pillow: MIT-CMU. https://pypi.org/project/pillow/
- PyInstaller: GPL with bootloader exception allowing distribution of bundled applications. https://pyinstaller.org/

No Chrome binary, account credentials, browser profile or corporate report is distributed in this package. Chrome remains a separately installed application.
