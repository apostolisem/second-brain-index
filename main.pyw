from __future__ import annotations

import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from second_brain_index.config import AppConfig
from second_brain_index.db import ManualLinkStore
from second_brain_index.resources import DESKTOP_FILE_NAME, app_icon
from second_brain_index.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setOrganizationName("SecondBrainHub")
    app.setApplicationName("SecondBrainIndex")
    # Matches second-brain-hub.desktop so the taskbar/dock groups the window
    # with its launcher instead of showing a generic placeholder icon.
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    app.setWindowIcon(app_icon())
    config = AppConfig.load(Path(".env"))
    store = ManualLinkStore(config.db_path)
    app.aboutToQuit.connect(store.close)

    window = MainWindow(config, store)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
