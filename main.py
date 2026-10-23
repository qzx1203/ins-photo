"""Desktop application entry point."""

import sys
from pathlib import Path

from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import QApplication

from ins_saver import __version__
from ins_saver.ui import MainWindow


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("拾光")
    app.setOrganizationName("Shiguang")
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    assets = Path(getattr(sys, "_MEIPASS", Path(__file__).parent)) / "assets"
    app.setWindowIcon(QIcon(str(assets / "app.ico")))
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
