import argparse
import platform
import sys
import traceback
from pathlib import Path

from PyQt6 import QtWidgets

from .window import MainWindow


def main():
    if platform.system() != "Windows":
        print("DeskPets can only run on Windows.")
        sys.exit(1)

    parser = argparse.ArgumentParser()
    parser.add_argument("--me", default="", help="Your user id (e.g., jinho)")
    parser.add_argument("--partner", default="", help="Partner user id (e.g., sanga)")
    parser.add_argument("--shared", default="", help="Shared local folder path")
    args, _ = parser.parse_known_args()

    shared_dir = Path(args.shared).expanduser().resolve() if args.shared else None
    me = (args.me or "").strip() or None
    partner = (args.partner or "").strip() or None

    try:
        app = QtWidgets.QApplication(sys.argv)
        window = MainWindow(app, me=me, partner=partner, shared_dir=shared_dir)
        window.hide()
        window.start_refresh()
        sys.exit(app.exec())
    except Exception as e:
        print(e)
        traceback.print_exc()
