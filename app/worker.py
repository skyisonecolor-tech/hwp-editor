# -*- coding: utf-8 -*-
"""한글 COM 작업을 한 스레드에서 차례대로 처리한다(화면이 멈추지 않도록)."""
import os
import sys
import json
import queue
import hashlib
import itertools
import logging
import tempfile
import shutil
import traceback

from PySide6.QtCore import QThread, Signal

from . import planner
from .engine import HwpEngine, FakeEngine

log = logging.getLogger("hwptool.worker")

PRI_RUN, PRI_PREVIEW, PRI_INFO, PRI_STOP = 0, 1, 2, 9


def app_data_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.local/share")
    d = os.path.join(base, "HwpPageTool")
    os.makedirs(d, exist_ok=True)
    return d


def cache_dir_for(path):
    st = os.stat(path)
    key = "%s|%d|%d" % (os.path.abspath(path).lower(), st.st_size, int(st.st_mtime))
    h = hashlib.md5(key.encode("utf-8")).hexdigest()[:16]
    return os.path.join(app_data_dir(), "cache", h)


class Worker(QThread):
    fileInfo = Signal(int, int)          # file_id, page_count
    fileError = Signal(int, str)         # file_id, message
    thumbReady = Signal(int, int, str)   # file_id, page, image path
    previewReady = Signal(int, int, str)
    jobProgress = Signal(str, int, int)  # message, i, n
    jobDone = Signal(list)               # 만든 파일 목록
    jobFailed = Signal(str)
    engineError = Signal(str)
    diagDone = Signal(str)

    def __init__(self, fake=False, visible=False):
        super().__init__()
        self.fake = fake
        self.visible = visible
        self.q = queue.PriorityQueue()
        self._seq = itertools.count()
        self.engine = None
        self._engine_failed = None

    # -------------------------------------------------------------- 요청
    def _put(self, pri, kind, *args):
        self.q.put((pri, next(self._seq), kind, args))

    def request_info(self, fid, path, dpi):
        self._put(PRI_INFO, "info", fid, path, dpi)

    def request_preview(self, fid, path, page, dpi=110):
        self._put(PRI_PREVIEW, "preview", fid, path, page, dpi)

    def request_run(self, items, files, mode, fmt, dst):
        self._put(PRI_RUN, "run", items, files, mode, fmt, dst)

    def request_diag(self, path):
        self._put(PRI_RUN, "diag", path)

    def request_visible(self, visible):
        self.visible = visible
        self._put(PRI_RUN, "visible", visible)

    def stop(self):
        self._put(PRI_STOP, "stop")

    # -------------------------------------------------------------- 실행
    def _get_engine(self):
        if self.engine is not None:
            return self.engine
        if self._engine_failed:
            raise RuntimeError(self._engine_failed)
        eng = FakeEngine() if self.fake else HwpEngine(visible=self.visible)
        try:
            eng.start()
        except Exception as e:
            self._engine_failed = str(e)
            self.engineError.emit(str(e))
            raise
        self.engine = eng
        return eng

    def run(self):
        com = False
        if sys.platform == "win32" and not self.fake:
            import pythoncom
            pythoncom.CoInitialize()
            com = True
        try:
            while True:
                pri, _, kind, args = self.q.get()
                if kind == "stop":
                    break
                try:
                    getattr(self, "_do_" + kind)(*args)
                except Exception as e:
                    log.error("%s 작업 실패: %s\n%s", kind, e, traceback.format_exc())
                    if kind == "info":
                        self.fileError.emit(args[0], str(e))
                    elif kind == "run":
                        self.jobFailed.emit(str(e))
        finally:
            if self.engine is not None:
                self.engine.quit()
            if com:
                import pythoncom
                pythoncom.CoUninitialize()

    def _do_diag(self, path):
        try:
            eng = self._get_engine()
        except Exception as e:
            self.diagDone.emit("✘ 한글 연결 실패: %s" % e)
            return
        text = eng.diagnose(path)
        log.info("연결 시험 결과\n%s", text)
        self.diagDone.emit(text)

    def _do_visible(self, visible):
        if self.engine is not None:
            self.engine.set_visible(visible)

    def _do_info(self, fid, path, dpi):
        cdir = cache_dir_for(path)
        meta = os.path.join(cdir, "meta.json")
        if os.path.exists(meta):
            try:
                with open(meta, encoding="utf-8") as f:
                    n = int(json.load(f)["pages"])
                imgs = [os.path.join(cdir, "p%d_%d.bmp" % (p, dpi)) for p in range(1, n + 1)]
                if all(os.path.exists(i) for i in imgs):
                    self.fileInfo.emit(fid, n)
                    for p, i in enumerate(imgs, 1):
                        self.thumbReady.emit(fid, p, i)
                    return
            except Exception:
                pass
        eng = self._get_engine()
        count = {}

        def on_count(n):
            count["n"] = n
            self.fileInfo.emit(fid, n)

        def on_img(p, f):
            if f:
                self.thumbReady.emit(fid, p, f)

        eng.render_pages(path, cdir, dpi=dpi, callback=on_img, on_count=on_count)
        if "n" in count:
            with open(meta, "w", encoding="utf-8") as f:
                json.dump({"pages": count["n"], "path": path}, f, ensure_ascii=False)

    def _do_preview(self, fid, path, page, dpi):
        cdir = cache_dir_for(path)
        f = os.path.join(cdir, "p%d_%d.bmp" % (page, dpi))
        if not os.path.exists(f):
            self._get_engine().render_pages(path, cdir, dpi=dpi, pages=[page])
        if os.path.exists(f):
            self.previewReady.emit(fid, page, f)

    def _do_run(self, items, files, mode, fmt, dst):
        eng = self._get_engine()
        tmp = tempfile.mkdtemp(prefix="hwptool_")
        try:
            outs = planner.execute(eng, items, files, mode, fmt, dst, tmp,
                                   progress=lambda m, i, n: self.jobProgress.emit(m, i, n))
            self.jobDone.emit(outs)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
