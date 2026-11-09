# -*- coding: utf-8 -*-
import os
import sys
import itertools

from PySide6.QtCore import Qt, QSize, QRect, Signal, QUrl
from PySide6.QtGui import (QPixmap, QPainter, QColor, QPen, QFont, QKeySequence, QAction,
                           QDesktopServices, QIcon, QShortcut)
from PySide6.QtWidgets import (
    QMainWindow, QWidget, QListWidget, QListWidgetItem, QListView, QAbstractItemView,
    QStyledItemDelegate, QStyle, QSplitter, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QComboBox, QRadioButton, QButtonGroup, QProgressBar, QFileDialog, QMessageBox,
    QInputDialog, QDialog, QScrollArea, QSlider, QGroupBox, QToolButton, QMenu, QFrame)

from . import planner
from .worker import Worker, app_data_dir

APP_NAME = "한글 쪽 편집기"
VERSION = "1.0.0"
THUMB_DPI = 40
HWP_EXTS = (".hwp", ".hwpx")
ROLE_KEY = Qt.UserRole + 1     # (file_id, page)

PALETTE = ["#2f6fdf", "#e0592a", "#1d9a6c", "#a64bd6", "#d4a017", "#d6336c",
           "#0f8fa8", "#6b7a1f", "#8c5a3c", "#4f5bd5"]


class FileEntry:
    def __init__(self, fid, path, color):
        self.id = fid
        self.path = path
        self.name = os.path.basename(path)
        self.color = color
        self.pages = None
        self.error = None
        self.thumbs = {}          # page → QPixmap


# ====================================================================== 위젯
class PageDelegate(QStyledItemDelegate):
    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def sizeHint(self, option, index):
        w = self.win.thumb_w
        return QSize(w + 16, int(w * 1.414) + 40)

    def paint(self, p, option, index):
        fid, page = index.data(ROLE_KEY)
        entry = self.win.files.get(fid)
        r = option.rect.adjusted(4, 4, -4, -4)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        selected = option.state & QStyle.State_Selected
        if selected:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor("#d6e6ff"))
            p.drawRoundedRect(option.rect.adjusted(1, 1, -1, -1), 6, 6)
        tw = self.win.thumb_w
        th = int(tw * 1.414)
        box = QRect(r.x() + (r.width() - tw) // 2, r.y() + 2, tw, th)
        pm = entry.thumbs.get(page) if entry else None
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white"))
        p.drawRect(box)
        if pm is not None and not pm.isNull():
            spm = pm.scaled(box.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
            x = box.x() + (box.width() - spm.width()) // 2
            y = box.y() + (box.height() - spm.height()) // 2
            p.drawPixmap(x, y, spm)
        else:
            p.setPen(QColor("#b0b0b0"))
            f = QFont(p.font())
            f.setPixelSize(max(14, tw // 5))
            f.setBold(True)
            p.setFont(f)
            p.drawText(box, Qt.AlignCenter, "%d" % page)
        color = QColor(entry.color if entry else "#888")
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(color, 3 if selected else 2))
        p.drawRect(box)
        # 순번 배지
        num = index.row() + 1
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, 150))
        badge = QRect(box.x() + 3, box.y() + 3, 26 if num < 100 else 34, 18)
        p.drawRoundedRect(badge, 4, 4)
        p.setPen(QColor("white"))
        f = QFont(p.font())
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        p.drawText(badge, Qt.AlignCenter, str(num))
        # 설명
        cap = QRect(r.x(), box.bottom() + 4, r.width(), 32)
        p.setPen(color)
        f.setPixelSize(11)
        f.setBold(True)
        p.setFont(f)
        name = entry.name if entry else "?"
        fm = p.fontMetrics()
        p.drawText(cap.adjusted(0, 0, 0, -16), Qt.AlignHCenter | Qt.AlignTop,
                   fm.elidedText(os.path.splitext(name)[0], Qt.ElideMiddle, cap.width()))
        p.setPen(QColor("#444"))
        f.setBold(False)
        p.setFont(f)
        p.drawText(cap.adjusted(0, 15, 0, 0), Qt.AlignHCenter | Qt.AlignTop, "%d쪽" % page)
        p.restore()


class DropListWidget(QListWidget):
    """탐색기에서 끌어온 한글 파일을 받는 목록(내부 끌어 놓기 순서 변경도 지원)."""
    filesDropped = Signal(list)

    def _hwp_urls(self, md):
        if not md.hasUrls():
            return []
        out = []
        for u in md.urls():
            f = u.toLocalFile()
            if os.path.isdir(f):
                for root, _, names in os.walk(f):
                    out += [os.path.join(root, n) for n in sorted(names) if n.lower().endswith(HWP_EXTS)]
            elif f.lower().endswith(HWP_EXTS):
                out.append(f)
        return out

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e):
        if e.mimeData().hasUrls():
            files = self._hwp_urls(e.mimeData())
            e.acceptProposedAction()
            if files:
                self.filesDropped.emit(files)
            return
        super().dropEvent(e)
        self.window().on_internal_move(self)


class PreviewDialog(QDialog):
    def __init__(self, parent, title, pm):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 920)
        lay = QVBoxLayout(self)
        self.area = QScrollArea()
        self.area.setAlignment(Qt.AlignCenter)
        self.lbl = QLabel()
        self.lbl.setAlignment(Qt.AlignCenter)
        self.area.setWidget(self.lbl)
        self.area.setWidgetResizable(True)
        lay.addWidget(self.area)
        self.set_pixmap(pm)

    def set_pixmap(self, pm):
        if pm is None or pm.isNull():
            self.lbl.setText("미리보기를 만드는 중입니다…")
            return
        w = max(300, self.area.viewport().width() - 20)
        self.lbl.setPixmap(pm.scaledToWidth(min(w, pm.width() * 2), Qt.SmoothTransformation))


# ================================================================== 메인 창
class MainWindow(QMainWindow):
    def __init__(self, fake=False):
        super().__init__()
        self.fake = fake
        self.files = {}               # id → FileEntry
        self._ids = itertools.count(1)
        self.thumb_w = 120
        self.busy = False
        self.preview_dlg = None
        self.preview_key = None

        self.worker = Worker(fake=fake)
        self.worker.fileInfo.connect(self.on_file_info)
        self.worker.fileError.connect(self.on_file_error)
        self.worker.thumbReady.connect(self.on_thumb)
        self.worker.previewReady.connect(self.on_preview)
        self.worker.jobProgress.connect(self.on_progress)
        self.worker.jobDone.connect(self.on_done)
        self.worker.jobFailed.connect(self.on_failed)
        self.worker.engineError.connect(self.on_engine_error)
        self.worker.diagDone.connect(self.on_diag)
        self.worker.start()

        self.setWindowTitle("%s %s%s" % (APP_NAME, VERSION, " [시험 모드]" if fake else ""))
        self.resize(1280, 820)
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_menu()
        self.refresh()

    # ---------------------------------------------------------------- 화면
    def _btn(self, text, slot, tip=None):
        b = QPushButton(text)
        b.clicked.connect(slot)
        if tip:
            b.setToolTip(tip)
        return b

    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(10, 10, 10, 8)

        split = QSplitter(Qt.Horizontal)
        outer.addWidget(split, 1)

        # ---- 왼쪽: 파일 목록
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 6, 0)
        t = QLabel("<b>① 원본 파일</b>  <span style='color:#777'>(끌어서 순서 변경)</span>")
        ll.addWidget(t)
        row = QHBoxLayout()
        row.addWidget(self._btn("＋ 파일 추가", self.add_files_dialog))
        row.addWidget(self._btn("＋ 폴더", self.add_folder_dialog, "폴더 안의 한글 파일을 모두 추가"))
        ll.addLayout(row)
        self.file_list = DropListWidget()
        self.file_list.setDragDropMode(QAbstractItemView.InternalMove)
        self.file_list.setDefaultDropAction(Qt.MoveAction)
        self.file_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.file_list.setAcceptDrops(True)
        self.file_list.filesDropped.connect(self.add_files)
        self.file_list.itemDoubleClicked.connect(lambda it: self.add_range_for(it.data(ROLE_KEY)))
        self.file_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.file_list.customContextMenuRequested.connect(self.file_menu)
        self.file_list.setStyleSheet("QListWidget::item{padding:6px 4px;}")
        ll.addWidget(self.file_list, 1)
        g = QHBoxLayout()
        g.addWidget(self._btn("▲", lambda: self.move_files(-1), "선택 파일을 위로"))
        g.addWidget(self._btn("▼", lambda: self.move_files(1), "선택 파일을 아래로"))
        g.addWidget(self._btn("이름순", self.sort_files_by_name, "파일 이름순으로 정렬"))
        g.addWidget(self._btn("제거", self.remove_files, "선택 파일과 그 쪽들을 목록에서 제거"))
        ll.addLayout(g)
        g2 = QHBoxLayout()
        g2.addWidget(self._btn("쪽 범위로 추가…", lambda: self.add_range_for(self.current_file_id()),
                               "선택 파일에서 원하는 쪽(예: 1-3, 5)만 결과에 추가"))
        g2.addWidget(self._btn("전체 다시 추가", self.readd_selected_files, "선택 파일의 모든 쪽을 결과 끝에 추가"))
        ll.addLayout(g2)
        split.addWidget(left)

        # ---- 오른쪽: 결과 쪽 목록
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)
        top = QHBoxLayout()
        self.result_title = QLabel()
        top.addWidget(self.result_title, 1)
        top.addWidget(QLabel("크기"))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(70, 240)
        self.size_slider.setValue(self.thumb_w)
        self.size_slider.setFixedWidth(140)
        self.size_slider.valueChanged.connect(self.set_thumb_size)
        top.addWidget(self.size_slider)
        rl.addLayout(top)

        tools = QHBoxLayout()
        tools.addWidget(self._btn("선택 쪽 빼기", self.remove_pages, "결과에서 제외 (Delete 키)"))
        tools.addWidget(self._btn("선택 쪽만 남기기", self.keep_selected, "선택한 쪽만 남기고 나머지 제외 = 추출"))
        tools.addWidget(self._btn("복제", self.duplicate_pages, "선택 쪽을 바로 뒤에 한 번 더"))
        tools.addWidget(self._btn("맨 앞으로", lambda: self.move_pages_edge(True)))
        tools.addWidget(self._btn("맨 뒤로", lambda: self.move_pages_edge(False)))
        more = QToolButton()
        more.setText("더보기 ▾")
        more.setPopupMode(QToolButton.InstantPopup)
        m = QMenu(more)
        m.addAction("선택 쪽 순서 뒤집기 (선택 없으면 전체)", self.reverse_pages)
        m.addAction("파일 목록 순서대로 다시 정렬", self.regroup_by_files)
        m.addAction("원래대로 (모든 파일의 모든 쪽)", self.reset_pages)
        m.addSeparator()
        m.addAction("모두 선택  (Ctrl+A)", lambda: self.grid.selectAll())
        m.addAction("결과 비우기", self.clear_pages)
        more.setMenu(m)
        tools.addWidget(more)
        tools.addStretch(1)
        rl.addLayout(tools)

        self.grid = DropListWidget()
        self.grid.setViewMode(QListView.ListMode)
        self.grid.setFlow(QListView.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSpacing(4)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setDragDropMode(QAbstractItemView.InternalMove)
        self.grid.setDefaultDropAction(Qt.MoveAction)
        self.grid.setDropIndicatorShown(True)
        self.grid.setAcceptDrops(True)
        self.grid.setItemDelegate(PageDelegate(self))
        self.grid.setStyleSheet("QListWidget{background:#eef0f3;}")
        self.grid.filesDropped.connect(self.add_files)
        self.grid.itemDoubleClicked.connect(self.open_preview)
        self.grid.itemSelectionChanged.connect(self.refresh_title)
        self.grid.setContextMenuPolicy(Qt.CustomContextMenu)
        self.grid.customContextMenuRequested.connect(self.page_menu)
        rl.addWidget(self.grid, 1)
        self.hint = QLabel("한글 파일(.hwp, .hwpx)을 이곳이나 왼쪽 목록으로 끌어다 놓으세요.  "
                           "쪽은 끌어서 순서를 바꾸고, 두 번 누르면 크게 봅니다.")
        self.hint.setStyleSheet("color:#666")
        rl.addWidget(self.hint)
        split.addWidget(right)
        split.setSizes([300, 980])

        QShortcut(QKeySequence.Delete, self.grid, activated=self.remove_pages)

        # ---- 아래: 저장
        box = QGroupBox("② 저장")
        bl = QHBoxLayout(box)
        bl.addWidget(QLabel("방식"))
        self.mode_merge = QRadioButton("하나의 파일로 (통합·추출·순서 변경)")
        self.mode_split = QRadioButton("쪽마다 따로 (분할)")
        self.mode_merge.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.mode_merge)
        grp.addButton(self.mode_split)
        bl.addWidget(self.mode_merge)
        bl.addWidget(self.mode_split)
        bl.addSpacing(20)
        bl.addWidget(QLabel("형식"))
        self.fmt = QComboBox()
        self.fmt.addItem("HWP (.hwp)", "hwp")
        self.fmt.addItem("HWPX (.hwpx)", "hwpx")
        self.fmt.addItem("PDF (.pdf)", "pdf")
        bl.addWidget(self.fmt)
        bl.addStretch(1)
        self.progress = QProgressBar()
        self.progress.setFixedWidth(240)
        self.progress.setVisible(False)
        bl.addWidget(self.progress)
        self.save_btn = QPushButton("  저장하기  ")
        self.save_btn.setMinimumHeight(34)
        self.save_btn.setStyleSheet("QPushButton{background:#2f6fdf;color:white;font-weight:bold;border-radius:5px;padding:0 18px;}"
                                    "QPushButton:disabled{background:#9db5e3;}")
        self.save_btn.clicked.connect(self.save)
        bl.addWidget(self.save_btn)
        outer.addWidget(box)
        self.statusBar().showMessage("준비")

    def _build_menu(self):
        mb = self.menuBar()
        fm = mb.addMenu("파일")
        a = QAction("파일 추가…", self, shortcut=QKeySequence.Open, triggered=self.add_files_dialog)
        fm.addAction(a)
        fm.addAction(QAction("폴더 추가…", self, triggered=self.add_folder_dialog))
        fm.addSeparator()
        fm.addAction(QAction("모두 지우기", self, triggered=self.clear_all))
        fm.addSeparator()
        fm.addAction(QAction("끝내기", self, triggered=self.close))
        sm = mb.addMenu("설정")
        self.act_visible = QAction("작업 중 한글 창 보이기 (문제 해결용)", self, checkable=True)
        self.act_visible.toggled.connect(self.worker.request_visible)
        sm.addAction(self.act_visible)
        sm.addAction(QAction("한글 연결 시험…", self, triggered=self.run_diag))
        sm.addSeparator()
        sm.addAction(QAction("미리보기 저장소 비우기", self, triggered=self.clear_cache))
        sm.addAction(QAction("기록(로그) 폴더 열기", self, triggered=lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.path.join(app_data_dir(), "logs")))))
        hm = mb.addMenu("도움말")
        hm.addAction(QAction("사용 방법", self, triggered=self.show_help))
        hm.addAction(QAction("프로그램 정보", self, triggered=lambda: QMessageBox.about(
            self, APP_NAME, "%s %s\n한글(HWP·HWPX) 파일 통합 · 쪽 추출 · 순서 변경 · 분할\n\n"
                            "한컴오피스 한글이 설치된 Windows PC에서 동작합니다." % (APP_NAME, VERSION))))

    # ------------------------------------------------------------ 파일 관리
    def add_files_dialog(self):
        fs, _ = QFileDialog.getOpenFileNames(self, "한글 파일 선택", "", "한글 문서 (*.hwp *.hwpx)")
        if fs:
            self.add_files(fs)

    def add_folder_dialog(self):
        d = QFileDialog.getExistingDirectory(self, "폴더 선택")
        if d:
            fs = sorted(os.path.join(d, n) for n in os.listdir(d) if n.lower().endswith(HWP_EXTS))
            if not fs:
                QMessageBox.information(self, APP_NAME, "폴더에 한글 파일이 없습니다.")
            self.add_files(fs)

    def add_files(self, paths):
        existing = {os.path.normcase(os.path.abspath(e.path)) for e in self.files.values()}
        skipped = 0
        for p in paths:
            ap = os.path.abspath(p)
            if os.path.basename(ap).startswith("~$"):
                continue
            if os.path.normcase(ap) in existing:
                skipped += 1
                continue
            existing.add(os.path.normcase(ap))
            fid = next(self._ids)
            e = FileEntry(fid, ap, PALETTE[(fid - 1) % len(PALETTE)])
            self.files[fid] = e
            it = QListWidgetItem()
            it.setData(ROLE_KEY, fid)
            it.setToolTip(ap)
            self.file_list.addItem(it)
            self._update_file_item(it)
            self.worker.request_info(fid, ap, THUMB_DPI)
        if skipped:
            self.statusBar().showMessage("이미 목록에 있는 파일 %d개는 건너뛰었습니다." % skipped, 5000)
        self.refresh()

    def _file_item(self, fid):
        for i in range(self.file_list.count()):
            it = self.file_list.item(i)
            if it.data(ROLE_KEY) == fid:
                return it
        return None

    def _dot_icon(self, color):
        pm = QPixmap(14, 14)
        pm.fill(Qt.transparent)
        qp = QPainter(pm)
        qp.setRenderHint(QPainter.Antialiasing)
        qp.setBrush(QColor(color))
        qp.setPen(Qt.NoPen)
        qp.drawEllipse(1, 1, 12, 12)
        qp.end()
        return QIcon(pm)

    def _update_file_item(self, it):
        e = self.files[it.data(ROLE_KEY)]
        if e.error:
            info = "열기 실패"
        elif e.pages is None:
            info = "읽는 중…"
        else:
            used = sum(1 for fid, _ in self.items() if fid == e.id)
            info = "%d쪽" % e.pages + ("" if used == e.pages else " · 결과에 %d쪽" % used)
        it.setText("%s\n   %s" % (e.name, info))
        it.setIcon(self._dot_icon(e.color))
        it.setForeground(QColor("#c0392b") if e.error else QColor("#222"))

    def current_file_id(self):
        it = self.file_list.currentItem()
        return it.data(ROLE_KEY) if it else None

    def selected_file_ids(self):
        return [it.data(ROLE_KEY) for it in self.file_list.selectedItems()]

    def file_order(self):
        return [self.file_list.item(i).data(ROLE_KEY) for i in range(self.file_list.count())]

    def move_files(self, d):
        rows = sorted(self.file_list.row(it) for it in self.file_list.selectedItems())
        if not rows:
            return
        if (d < 0 and rows[0] == 0) or (d > 0 and rows[-1] == self.file_list.count() - 1):
            return
        for r in (rows if d < 0 else reversed(rows)):
            it = self.file_list.takeItem(r)
            self.file_list.insertItem(r + d, it)
            it.setSelected(True)
        self.on_internal_move(self.file_list)

    def sort_files_by_name(self):
        its = [self.file_list.takeItem(0) for _ in range(self.file_list.count())]
        its.sort(key=lambda it: self.files[it.data(ROLE_KEY)].name.lower())
        for it in its:
            self.file_list.addItem(it)
        self.on_internal_move(self.file_list)

    def remove_files(self):
        ids = set(self.selected_file_ids())
        if not ids:
            return
        for fid in ids:
            it = self._file_item(fid)
            self.file_list.takeItem(self.file_list.row(it))
            self.files.pop(fid, None)
        self.set_items([k for k in self.items() if k[0] not in ids])

    def clear_all(self):
        self.file_list.clear()
        self.grid.clear()
        self.files.clear()
        self.refresh()

    def file_menu(self, pos):
        it = self.file_list.itemAt(pos)
        if not it:
            return
        fid = it.data(ROLE_KEY)
        m = QMenu(self)
        m.addAction("쪽 범위로 추가…", lambda: self.add_range_for(fid))
        m.addAction("모든 쪽 다시 추가", lambda: self.append_pages(fid, None))
        m.addAction("결과에서 이 파일의 쪽 모두 빼기", lambda: self.set_items([k for k in self.items() if k[0] != fid]))
        m.addSeparator()
        m.addAction("폴더 열기", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(self.files[fid].path))))
        m.addAction("목록에서 제거", self.remove_files)
        m.exec(self.file_list.mapToGlobal(pos))

    def readd_selected_files(self):
        for fid in self.selected_file_ids():
            self.append_pages(fid, None)

    def add_range_for(self, fid):
        if fid is None:
            QMessageBox.information(self, APP_NAME, "왼쪽 목록에서 파일을 먼저 선택하세요.")
            return
        e = self.files[fid]
        if not e.pages:
            QMessageBox.information(self, APP_NAME, "아직 파일을 읽는 중입니다.")
            return
        text, ok = QInputDialog.getText(
            self, "쪽 범위로 추가",
            "%s (전체 %d쪽)\n결과 끝에 추가할 쪽을 입력하세요.\n예) 1-3, 5, 8-   (8-는 8쪽부터 끝까지)" % (e.name, e.pages))
        if not ok:
            return
        try:
            pages = planner.parse_ranges(text, e.pages)
        except ValueError as ex:
            QMessageBox.warning(self, APP_NAME, str(ex))
            return
        self.append_pages(fid, pages)

    # ------------------------------------------------------------- 쪽 목록
    def items(self):
        return [self.grid.item(i).data(ROLE_KEY) for i in range(self.grid.count())]

    def _make_page_item(self, key):
        it = QListWidgetItem()
        it.setData(ROLE_KEY, key)
        it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled)
        e = self.files.get(key[0])
        if e:
            it.setToolTip("%s — %d쪽" % (e.name, key[1]))
        return it

    def set_items(self, keys, select_rows=None):
        self.grid.setUpdatesEnabled(False)
        self.grid.clear()
        for k in keys:
            self.grid.addItem(self._make_page_item(k))
        if select_rows:
            for r in select_rows:
                if 0 <= r < self.grid.count():
                    self.grid.item(r).setSelected(True)
            self.grid.scrollToItem(self.grid.item(min(select_rows)))
        self.grid.setUpdatesEnabled(True)
        self.refresh()

    def append_pages(self, fid, pages):
        e = self.files.get(fid)
        if not e or not e.pages:
            return
        pages = pages or list(range(1, e.pages + 1))
        start = self.grid.count()
        for p in pages:
            self.grid.addItem(self._make_page_item((fid, p)))
        self.grid.clearSelection()
        for r in range(start, self.grid.count()):
            self.grid.item(r).setSelected(True)
        self.grid.scrollToItem(self.grid.item(self.grid.count() - 1))
        self.refresh()

    def selected_rows(self):
        return sorted(self.grid.row(it) for it in self.grid.selectedItems())

    def remove_pages(self):
        rows = set(self.selected_rows())
        if rows:
            self.set_items([k for i, k in enumerate(self.items()) if i not in rows])

    def keep_selected(self):
        rows = set(self.selected_rows())
        if not rows:
            QMessageBox.information(self, APP_NAME, "남길 쪽을 먼저 선택하세요. (Ctrl·Shift를 누른 채 누르면 여러 쪽 선택)")
            return
        self.set_items([k for i, k in enumerate(self.items()) if i in rows])

    def duplicate_pages(self):
        rows = set(self.selected_rows())
        if not rows:
            return
        out, sel = [], []
        for i, k in enumerate(self.items()):
            out.append(k)
            if i in rows:
                out.append(k)
                sel.append(len(out) - 1)
        self.set_items(out, sel)

    def move_pages_edge(self, front):
        rows = self.selected_rows()
        if not rows:
            return
        keys = self.items()
        chosen = [keys[r] for r in rows]
        rest = [k for i, k in enumerate(keys) if i not in set(rows)]
        if front:
            self.set_items(chosen + rest, list(range(len(chosen))))
        else:
            self.set_items(rest + chosen, list(range(len(rest), len(keys))))

    def reverse_pages(self):
        keys = self.items()
        rows = self.selected_rows() or list(range(len(keys)))
        vals = [keys[r] for r in rows][::-1]
        for r, v in zip(rows, vals):
            keys[r] = v
        self.set_items(keys, rows if self.grid.selectedItems() else None)

    def regroup_by_files(self, ask=False):
        keys = self.items()
        order = {fid: i for i, fid in enumerate(self.file_order())}
        if ask and not self._is_grouped(keys):
            r = QMessageBox.question(
                self, APP_NAME, "결과의 쪽들이 파일별로 섞여 있습니다.\n바뀐 파일 순서대로 쪽을 다시 묶을까요?\n"
                                "(각 파일 안의 쪽 순서는 그대로 유지됩니다)")
            if r != QMessageBox.Yes:
                return
        # 파일별로 묶고, 파일 안에서는 현재 순서 유지(안정 정렬)
        keys.sort(key=lambda k: order.get(k[0], 1e9))
        self.set_items(keys)

    @staticmethod
    def _is_grouped(keys):
        seen, last = set(), None
        for fid, _ in keys:
            if fid != last:
                if fid in seen:
                    return False
                seen.add(fid)
                last = fid
        return True

    def reset_pages(self):
        keys = []
        for fid in self.file_order():
            e = self.files[fid]
            if e.pages:
                keys += [(fid, p) for p in range(1, e.pages + 1)]
        self.set_items(keys)

    def clear_pages(self):
        self.set_items([])

    def page_menu(self, pos):
        it = self.grid.itemAt(pos)
        m = QMenu(self)
        if it:
            if not it.isSelected():
                self.grid.clearSelection()
                it.setSelected(True)
            m.addAction("크게 보기", lambda: self.open_preview(it))
            m.addSeparator()
            m.addAction("선택 쪽 빼기  (Delete)", self.remove_pages)
            m.addAction("선택 쪽만 남기기", self.keep_selected)
            m.addAction("복제", self.duplicate_pages)
            m.addAction("맨 앞으로", lambda: self.move_pages_edge(True))
            m.addAction("맨 뒤로", lambda: self.move_pages_edge(False))
            m.addAction("선택 쪽 순서 뒤집기", self.reverse_pages)
            m.addSeparator()
            m.addAction("선택 쪽만 바로 저장…", self.save_selected_only)
        else:
            m.addAction("원래대로 (모든 파일의 모든 쪽)", self.reset_pages)
        m.exec(self.grid.mapToGlobal(pos))

    def on_internal_move(self, widget):
        if widget is self.file_list:
            self.regroup_by_files(ask=True)
        self.refresh()

    def set_thumb_size(self, v):
        self.thumb_w = v
        self.grid.setItemDelegate(PageDelegate(self))   # 크기 다시 계산
        self.grid.doItemsLayout()

    # -------------------------------------------------------- 작업 스레드
    def on_file_info(self, fid, n):
        e = self.files.get(fid)
        if not e:
            return
        e.pages = n
        self.append_pages(fid, None)
        self.grid.clearSelection()

    def on_file_error(self, fid, msg):
        e = self.files.get(fid)
        if not e:
            return
        e.error = msg
        self.refresh()
        self.statusBar().showMessage("%s 열기 실패: %s" % (e.name, msg), 8000)

    def on_thumb(self, fid, page, path):
        e = self.files.get(fid)
        if not e:
            return
        pm = QPixmap(path)
        if not pm.isNull():
            e.thumbs[page] = pm.scaledToWidth(min(pm.width(), 260), Qt.SmoothTransformation)
            self.grid.viewport().update()

    def open_preview(self, it):
        fid, page = it.data(ROLE_KEY)
        e = self.files.get(fid)
        if not e:
            return
        self.preview_key = (fid, page)
        self.preview_dlg = PreviewDialog(self, "%s — %d쪽" % (e.name, page), None)
        self.preview_dlg.show()
        self.worker.request_preview(fid, e.path, page)

    def on_preview(self, fid, page, path):
        if self.preview_dlg and self.preview_key == (fid, page) and self.preview_dlg.isVisible():
            self.preview_dlg.set_pixmap(QPixmap(path))

    def run_diag(self):
        fid = self.current_file_id()
        path = self.files[fid].path if fid else None
        if not path:
            path, _ = QFileDialog.getOpenFileName(self, "시험할 한글 파일 (2쪽 이상 권장)", "", "한글 문서 (*.hwp *.hwpx)")
        if path:
            self.statusBar().showMessage("한글 연결 시험 중… (" + os.path.basename(path) + ")")
            self.worker.request_diag(path)

    def on_diag(self, text):
        self.statusBar().showMessage("연결 시험 완료", 5000)
        QMessageBox.information(self, "한글 연결 시험 결과", text + "\n\n(이 결과는 기록(로그) 파일에도 저장되었습니다)")

    def on_engine_error(self, msg):
        QMessageBox.critical(self, APP_NAME, msg)

    # ----------------------------------------------------------------- 저장
    def save_selected_only(self):
        rows = self.selected_rows()
        if rows:
            keys = self.items()
            self._save([keys[r] for r in rows])

    def save(self):
        self._save(self.items())

    def _save(self, keys):
        if self.busy:
            return
        if not keys:
            QMessageBox.information(self, APP_NAME, "저장할 쪽이 없습니다. 한글 파일을 먼저 추가하세요.")
            return
        if any(e.pages is None and not e.error for e in self.files.values()):
            QMessageBox.information(self, APP_NAME, "아직 읽는 중인 파일이 있습니다. 잠시 후 다시 눌러 주세요.")
            return
        fmt = self.fmt.currentData()
        mode = "split" if self.mode_split.isChecked() else "merge"
        first = self.files[keys[0][0]]
        start_dir = os.path.dirname(first.path)
        src_paths = {os.path.normcase(e.path) for e in self.files.values()}
        if mode == "merge":
            stem = os.path.splitext(first.name)[0]
            sug = os.path.join(start_dir, ("%s_편집" % stem if len({k[0] for k in keys}) == 1 else "합친문서") + planner.EXT[fmt])
            filt = {"hwp": "한글 문서 (*.hwp)", "hwpx": "한글 문서 (*.hwpx)", "pdf": "PDF (*.pdf)"}[fmt]
            dst, _ = QFileDialog.getSaveFileName(self, "저장할 파일", sug, filt)
            if not dst:
                return
            if not dst.lower().endswith(planner.EXT[fmt]):
                dst += planner.EXT[fmt]
            dst = os.path.abspath(dst)
            if os.path.normcase(dst) in src_paths:
                QMessageBox.warning(self, APP_NAME, "원본 파일 위에 덮어쓸 수 없습니다. 다른 이름을 지정하세요.")
                return
        else:
            dst = QFileDialog.getExistingDirectory(self, "쪽 파일을 저장할 폴더", start_dir)
            if not dst:
                return
            dst = os.path.abspath(dst)
        files = {fid: (e.path, e.pages) for fid, e in self.files.items() if e.pages}
        self.busy = True
        self.save_btn.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, 0)
        self.statusBar().showMessage("작업 준비 중…")
        self.worker.request_run(list(keys), files, mode, fmt, dst)

    def on_progress(self, msg, i, n):
        self.progress.setRange(0, n)
        self.progress.setValue(i)
        self.statusBar().showMessage(msg)

    def _finish(self):
        self.busy = False
        self.save_btn.setEnabled(True)
        self.progress.setVisible(False)

    def on_done(self, outs):
        self._finish()
        self.statusBar().showMessage("완료: %d개 파일 저장" % len(outs), 10000)
        box = QMessageBox(self)
        box.setWindowTitle(APP_NAME)
        box.setIcon(QMessageBox.Information)
        if len(outs) == 1:
            box.setText("저장했습니다.\n\n%s" % outs[0])
        else:
            box.setText("%d개 파일을 저장했습니다.\n\n%s" % (len(outs), os.path.dirname(outs[0]) if outs else ""))
        open_file = box.addButton("파일 열기", QMessageBox.AcceptRole) if len(outs) == 1 else None
        open_dir = box.addButton("폴더 열기", QMessageBox.ActionRole)
        box.addButton("닫기", QMessageBox.RejectRole)
        box.exec()
        if outs and box.clickedButton() is open_dir:
            QDesktopServices.openUrl(QUrl.fromLocalFile(os.path.dirname(outs[0])))
        elif outs and open_file is not None and box.clickedButton() is open_file:
            QDesktopServices.openUrl(QUrl.fromLocalFile(outs[0]))

    def on_failed(self, msg):
        self._finish()
        self.statusBar().showMessage("실패: " + msg, 10000)
        QMessageBox.critical(self, APP_NAME, "작업 중 문제가 생겼습니다.\n\n%s\n\n"
                             "‘설정 → 기록(로그) 폴더 열기’의 hwptool.log 파일을 보내 주시면 원인을 찾을 수 있습니다." % msg)

    # ----------------------------------------------------------------- 기타
    def refresh_title(self):
        keys = self.items()
        nf = len({k[0] for k in keys})
        sel = len(self.grid.selectedItems())
        self.result_title.setText(
            "<b>결과 문서</b> &nbsp; %d쪽 · 원본 %d개%s" % (len(keys), nf, (" &nbsp;|&nbsp; %d쪽 선택" % sel) if sel else ""))

    def refresh(self):
        for i in range(self.file_list.count()):
            self._update_file_item(self.file_list.item(i))
        self.refresh_title()
        self.hint.setVisible(True)

    def clear_cache(self):
        import shutil
        shutil.rmtree(os.path.join(app_data_dir(), "cache"), ignore_errors=True)
        self.statusBar().showMessage("미리보기 저장소를 비웠습니다.", 4000)

    def show_help(self):
        QMessageBox.information(self, "사용 방법", (
            "1. 한글 파일을 끌어다 놓거나 ‘파일 추가’로 불러옵니다.\n"
            "   → 모든 파일의 모든 쪽이 오른쪽 ‘결과 문서’에 순서대로 들어갑니다.\n\n"
            "2. 결과 문서를 원하는 모양으로 만듭니다.\n"
            "   • 통합: 그대로 저장\n"
            "   • 파일 순서 변경: 왼쪽 목록에서 파일을 끌어 옮기기(▲▼)\n"
            "   • 쪽 순서 변경: 오른쪽에서 쪽을 끌어 옮기기, 맨 앞/맨 뒤로\n"
            "   • 추출: 쪽을 선택하고 ‘선택 쪽만 남기기’ 또는 ‘쪽 범위로 추가’\n"
            "   • 빼기: 쪽을 선택하고 Delete\n\n"
            "3. 저장 방식(하나로 / 쪽마다)과 형식(HWP·HWPX·PDF)을 고르고 ‘저장하기’.\n\n"
            "※ 원본 파일은 바뀌지 않습니다.\n"
            "※ 한글 파일의 쪽은 한글 프로그램이 계산합니다. 여러 쪽에 걸친 표는 중간에서 나눌 수 없어\n"
            "   표가 이어지는 다음 쪽에 함께 들어갈 수 있습니다."))

    # ------------------------------------------------------------ 창 닫기
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        fs = self.file_list._hwp_urls(e.mimeData())
        if fs:
            self.add_files(fs)

    def closeEvent(self, e):
        if self.busy:
            r = QMessageBox.question(self, APP_NAME, "저장 작업이 진행 중입니다. 그래도 끝낼까요?")
            if r != QMessageBox.Yes:
                e.ignore()
                return
        self.worker.stop()
        self.worker.wait(8000)
        e.accept()
