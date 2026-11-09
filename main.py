# -*- coding: utf-8 -*-
"""한글 쪽 편집기 — 실행 파일 진입점.

  python main.py          : 실제 한글(COM) 사용
  python main.py --fake   : 한글 없이 화면만 시험
"""
import os
import sys
import logging
from logging.handlers import RotatingFileHandler


def setup_logging():
    from app.worker import app_data_dir
    d = os.path.join(app_data_dir(), "logs")
    os.makedirs(d, exist_ok=True)
    h = RotatingFileHandler(os.path.join(d, "hwptool.log"), maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(h)
    logging.getLogger("hwptool").info("===== 시작 =====")


def main():
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QFont
    setup_logging()
    fake = "--fake" in sys.argv or os.environ.get("HWPTOOL_FAKE") == "1"
    app = QApplication(sys.argv)
    app.setApplicationName("한글 쪽 편집기")
    f = QFont("맑은 고딕" if sys.platform == "win32" else app.font().family(), 10)
    app.setFont(f)
    from app.gui import MainWindow
    w = MainWindow(fake=fake)
    files = [a for a in sys.argv[1:] if a.lower().endswith((".hwp", ".hwpx")) and os.path.exists(a)]
    if files:
        w.add_files(files)   # 파일을 exe 아이콘에 끌어다 놓아 실행한 경우
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
