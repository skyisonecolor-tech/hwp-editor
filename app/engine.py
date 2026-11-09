# -*- coding: utf-8 -*-
"""
한글(HWP/HWPX) COM 자동화 엔진.

한글 파일에는 '쪽' 정보가 저장되어 있지 않으므로, 쪽 계산은 한글 프로그램에 맡긴다.
 - 쪽 경계 : Goto(쪽 이동) → MovePageBegin → 최상위 문단 위치(GetPos)로 정규화
 - 쪽 추출 : 원본을 열어 필요한 범위 밖을 지운 뒤 임시 파일로 저장 (원본은 절대 저장하지 않음)
 - 통합    : 첫 조각을 열어 결과 파일로 저장 → 나머지 조각을 InsertFile(구역 유지)로 끝에 삽입
 - 썸네일  : CreatePageImage

모든 메서드는 같은 스레드(작업 스레드)에서만 호출해야 한다.
"""
import os
import time
import shutil
import logging

log = logging.getLogger("hwptool.engine")

# 앞쪽을 지울 때 남겨 두어야 하는 '문서 전체 설정' 컨트롤
#  secd: 구역 정의, cold: 단 정의, head/foot: 머리말/꼬리말, pgnp: 쪽 번호 위치,
#  pghd: 감추기, nwno: 새 번호
KEEP_CTRL_IDS = {"secd", "cold", "head", "foot", "pgnp", "pghd", "nwno"}

SAVE_FORMATS = {"hwp": "HWP", "hwpx": "HWPX", "pdf": "PDF"}


class EngineError(Exception):
    pass


class HwpEngine:
    def __init__(self, visible=False):
        self.visible = visible
        self.hwp = None
        self._img_base = None  # CreatePageImage 쪽 번호 기준(0 또는 1)

    # ------------------------------------------------------------------ 연결
    def start(self):
        import pythoncom  # noqa: F401  (작업 스레드에서 CoInitialize 됨)
        import win32com.client as win32
        try:
            self.hwp = win32.Dispatch("HWPFrame.HwpObject")
        except Exception as e:
            raise EngineError("한글 프로그램을 실행할 수 없습니다. 한글(한컴오피스)이 설치되어 있는지 확인하세요.\n" + str(e))
        # 파일 접근 보안 승인 창 억제(보안 모듈이 등록된 경우에만 효과)
        for name in ("FilePathCheckerModule", "FilePathCheckerModuleExample", "SecurityModule"):
            try:
                if self.hwp.RegisterModule("FilePathCheckDLL", name):
                    log.info("보안 모듈 등록: %s", name)
                    break
            except Exception:
                pass
        try:
            self.hwp.XHwpWindows.Item(0).Visible = bool(self.visible)
        except Exception:
            log.warning("한글 창 표시 여부 설정 실패")
        try:
            log.info("한글 버전: %s", self.hwp.Version)
        except Exception:
            pass

    def set_visible(self, visible):
        self.visible = visible
        if self.hwp is not None:
            try:
                self.hwp.XHwpWindows.Item(0).Visible = bool(visible)
            except Exception:
                pass

    def quit(self):
        if self.hwp is None:
            return
        try:
            self.hwp.Clear(1)
        except Exception:
            pass
        try:
            self.hwp.Quit()
        except Exception:
            pass
        self.hwp = None

    # ------------------------------------------------------------- 기본 동작
    def _open(self, path):
        path = os.path.abspath(path)
        if not os.path.exists(path):
            raise EngineError("파일이 없습니다: " + path)
        self.hwp.Clear(1)  # 열려 있던 문서는 저장하지 않고 닫기
        ok = self.hwp.Open(path, "", "forceopen:true")
        if not ok:
            raise EngineError("파일을 열 수 없습니다: " + path)
        return self.hwp.PageCount

    def _close(self):
        try:
            self.hwp.Clear(1)
        except Exception:
            pass

    def _save_as(self, path, fmt):
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.exists(path):
            os.remove(path)
        ok = self.hwp.SaveAs(path, SAVE_FORMATS[fmt], "")
        if not ok or not os.path.exists(path):
            raise EngineError("저장에 실패했습니다: " + path)

    def _run(self, action):
        return self.hwp.Run(action)

    def _get_pos(self):
        lst, para, pos = self.hwp.GetPos()
        return int(lst), int(para), int(pos)

    def _set_pos(self, para, pos, lst=0):
        return self.hwp.SetPos(lst, para, pos)

    def _current_page(self):
        """물리적 쪽 번호(1부터). 새 쪽 번호 설정과 무관."""
        try:
            return int(self.hwp.XHwpDocuments.Active_XHwpDocument.XHwpDocumentInfo.CurrentPage) + 1
        except Exception:
            return None

    def _to_top_level(self):
        """표·글상자 안에 있으면 바깥 본문으로 나온다."""
        for _ in range(20):
            lst, para, pos = self._get_pos()
            if lst == 0:
                return para, pos
            self._run("MoveParentList")
        lst, para, pos = self._get_pos()
        if lst != 0:
            raise EngineError("본문 위치를 찾지 못했습니다.")
        return para, pos

    def _goto_page(self, page):
        hwp = self.hwp
        pset = hwp.HParameterSet.HGotoE
        hwp.HAction.GetDefault("Goto", pset.HSet)
        pset.HSet.SetItem("DialogResult", int(page))
        pset.SetSelectionIndex = 1
        hwp.HAction.Execute("Goto", pset.HSet)
        self._run("MovePageBegin")
        cur = self._current_page()
        if cur is not None and cur != page:
            # Goto가 '인쇄 쪽 번호' 기준으로 동작한 경우 등 → 처음부터 한 쪽씩 이동
            log.info("Goto 결과 불일치(요청 %s, 실제 %s) → 순차 이동", page, cur)
            self._run("MoveDocBegin")
            for _ in range(page * 3 + 5):
                cur = self._current_page()
                if cur is None or cur >= page:
                    break
                self._run("MovePageDown")
                self._run("MovePageBegin")
            if cur != page:
                raise EngineError("%d쪽으로 이동하지 못했습니다." % page)

    def _doc_end(self):
        self._run("MoveDocEnd")
        return self._to_top_level()

    def page_starts(self, page_count):
        """각 쪽 시작의 최상위 (문단, 위치). 인덱스 0 = 1쪽."""
        starts = [(0, 0)]
        for p in range(2, page_count + 1):
            self._goto_page(p)
            starts.append(self._to_top_level())
        return starts

    def _delete_between(self, s, e):
        """최상위 본문의 (문단,위치) s 이상 e 미만을 지운다."""
        if s >= e:
            return
        self._set_pos(s[0], s[1])
        self.hwp.SelectText(s[0], s[1], e[0], e[1])
        self._run("Delete")
        self._run("Cancel")

    def _para_end(self, para):
        self._set_pos(para, 0)
        self._run("MoveParaEnd")
        return self._to_top_level()

    def _keep_position(self, limit):
        """앞쪽을 지울 때 보존할 문서 설정 컨트롤(머리말 등) 바로 뒤 위치.
        모두 첫 문단에 있을 때만 보존하고, 그 외에는 (0,0)."""
        best = None
        try:
            ctrl = self.hwp.HeadCtrl
            while ctrl is not None:
                cid = str(ctrl.CtrlID).strip()
                if cid in KEEP_CTRL_IDS:
                    a = ctrl.GetAnchorPos(0)
                    lst, para, pos = int(a.Item("List")), int(a.Item("Para")), int(a.Item("Pos"))
                    if lst == 0 and (para, pos) < limit:
                        if para != 0:
                            return (0, 0)
                        # 컨트롤 바로 뒤 위치 구하기
                        self._set_pos(0, pos)
                        self._run("MoveRight")
                        l2, p2, q2 = self._get_pos()
                        after = (p2, q2) if (l2 == 0 and p2 == 0) else self._para_end(0)
                        if best is None or after > best:
                            best = after
                ctrl = ctrl.Next
        except Exception as ex:
            log.warning("컨트롤 탐색 실패: %s", ex)
            return (0, 0)
        if best is None or best >= limit:
            return (0, 0)
        return best

    def _read_para_shape(self):
        try:
            act = self.hwp.CreateAction("ParagraphShape")
            pset = act.CreateSet()
            act.GetDefault(pset)
            return pset
        except Exception:
            return None

    def _apply_para_shape(self, pset):
        if pset is None:
            return
        try:
            self.hwp.CreateAction("ParagraphShape").Execute(pset)
        except Exception as ex:
            log.warning("문단 모양 복원 실패: %s", ex)

    # ------------------------------------------------------------- 공개 API
    def page_count(self, path):
        n = self._open(path)
        self._close()
        return int(n)

    def render_pages(self, path, out_dir, dpi=40, pages=None, callback=None, on_count=None):
        """쪽 이미지를 out_dir/p{n}_{dpi}.bmp 로 저장.
        on_count(n): 문서를 연 직후 쪽 수 전달, callback(page, file_path_or_None)."""
        n = int(self._open(path))
        if on_count:
            on_count(n)
        os.makedirs(out_dir, exist_ok=True)
        targets = pages or list(range(1, n + 1))
        fails = 0
        try:
            for i, p in enumerate(targets):
                if p < 1 or p > n:
                    continue
                f = os.path.join(out_dir, "p%d_%d.bmp" % (p, dpi))
                ok = self._page_image(f, p, dpi)
                fails = 0 if ok else fails + 1
                if callback:
                    callback(p, f if ok else None)
                if fails >= 2 and i <= 1:
                    log.warning("쪽 이미지 생성이 지원되지 않는 것 같아 미리보기를 건너뜁니다: %s", path)
                    break
        finally:
            self._close()
        return n

    def _page_image(self, f, page, dpi):
        bases = [self._img_base] if self._img_base is not None else [0, 1]
        for base in bases:
            try:
                if os.path.exists(f):
                    os.remove(f)
                ok = self.hwp.CreatePageImage(f, page - 1 + base, dpi, 24, "bmp")
                if ok and os.path.exists(f) and os.path.getsize(f) > 0:
                    self._img_base = base
                    return True
            except Exception as ex:
                log.warning("쪽 이미지 생성 실패(%s쪽, 기준 %s): %s", page, base, ex)
        return False

    def extract_range(self, src, first, last, dst, fmt="hwp"):
        """src의 first~last 쪽만 남긴 문서를 dst에 저장한다. 원본은 저장하지 않는다."""
        n = int(self._open(src))
        try:
            first = max(1, first)
            last = min(n, last)
            if first == 1 and last == n:
                self._save_as(dst, fmt)
                return n
            starts = self.page_starts(n)
            # 1) 뒤쪽 삭제
            if last < n:
                e = starts[last]  # (last+1)쪽 시작
                if e[1] == 0 and e[0] > 0:
                    e = self._para_end(e[0] - 1)  # 빈 문단이 남지 않도록 이전 문단 끝부터
                end = self._doc_end()
                self._delete_between(e, end)
            # 2) 앞쪽 삭제
            if first > 1:
                s = starts[first - 1]
                if s > (0, 0):
                    self._set_pos(s[0], s[1])
                    shape = self._read_para_shape()
                    keep = self._keep_position(s)
                    self._delete_between(keep, s)
                    self._set_pos(keep[0], keep[1])
                    if keep != (0, 0) or s[1] == 0:
                        self._apply_para_shape(shape)
            got = int(self.hwp.PageCount)
            want = last - first + 1
            if got != want:
                log.warning("추출 쪽 수 차이: %s %d~%d쪽 → %d쪽(예상 %d)", os.path.basename(src), first, last, got, want)
            self._save_as(dst, fmt)
            return got
        finally:
            self._close()

    def _insert_file(self, path):
        hwp = self.hwp
        self._run("MoveDocEnd")
        pset = hwp.HParameterSet.HInsertFile
        hwp.HAction.GetDefault("InsertFile", pset.HSet)
        pset.filename = os.path.abspath(path)
        pset.KeepSection = 1
        pset.KeepCharshape = 1
        pset.KeepParashape = 1
        pset.KeepStyle = 1
        if not hwp.HAction.Execute("InsertFile", pset.HSet):
            raise EngineError("파일 끼워 넣기에 실패했습니다: " + path)

    def assemble(self, pieces, dst, fmt="hwp", tmp_dir=None, progress=None):
        """pieces: 순서대로 합칠 파일 경로 목록. 첫 조각을 열고 나머지를 끝에 끼워 넣은 뒤
        dst에 fmt 형식으로 한 번만 저장한다(첫 조각 원본은 저장하지 않음)."""
        if not pieces:
            raise EngineError("합칠 내용이 없습니다.")
        self._open(pieces[0])
        try:
            for i, p in enumerate(pieces[1:], start=2):
                if progress:
                    progress(i, len(pieces))
                self._insert_file(p)
            pages = int(self.hwp.PageCount)
            self._save_as(dst, fmt)
        finally:
            self._close()
        return pages

    def diagnose(self, path):
        """실제 PC에서 각 COM 기능이 동작하는지 점검하고 결과 문장을 돌려준다."""
        lines = []

        def ok(name, fn):
            try:
                r = fn()
                lines.append("✔ %s: %s" % (name, r))
                return r
            except Exception as e:
                lines.append("✘ %s: %s" % (name, e))
                return None

        ok("한글 버전", lambda: self.hwp.Version)
        n = ok("파일 열기 / 쪽 수", lambda: int(self._open(path)))
        if not n:
            return "\n".join(lines)
        ok("현재 쪽 읽기", lambda: self._current_page())
        starts = ok("쪽 경계 계산", lambda: self.page_starts(n))
        tmp = os.path.join(os.path.dirname(os.path.abspath(path)), "~hwptool_test.bmp")
        ok("쪽 이미지 만들기", lambda: "%s (기준 %s)" % (self._page_image(tmp, 1, 30), self._img_base))
        if os.path.exists(tmp):
            os.remove(tmp)
        self._close()
        if n >= 2 and starts:
            import tempfile
            d = tempfile.mkdtemp(prefix="hwptool_diag_")
            a = os.path.join(d, "p2.hwp")
            ok("2쪽만 추출", lambda: "%s쪽 문서 생성" % self.extract_range(path, 2, 2, a))
            if os.path.exists(a):
                ok("파일 끼워 넣기(통합)", lambda: "%s쪽 문서 생성" % self.assemble([a, a], os.path.join(d, "m.hwp")))
                ok("PDF 저장", lambda: (self.convert(a, os.path.join(d, "p2.pdf"), "pdf"), "완료")[1])
            shutil.rmtree(d, ignore_errors=True)
        return "\n".join(lines)

    def convert(self, src, dst, fmt):
        self._open(src)
        try:
            self._save_as(dst, fmt)
        finally:
            self._close()


class FakeEngine:
    """한글이 없는 환경에서 화면·흐름을 시험하기 위한 가짜 엔진."""

    def __init__(self, visible=False):
        self.calls = []
        self._docs = {}

    def start(self):
        pass

    def set_visible(self, v):
        pass

    def quit(self):
        pass

    def _n(self, path):
        if path in self._docs:
            return self._docs[path]
        try:
            with open(path, "rb") as f:
                head = f.read(64).decode("utf-8", "ignore")
            if head.startswith("PAGES="):
                return int(head.split("=")[1].split()[0])
        except Exception:
            pass
        return 3 + (len(os.path.basename(path)) % 5)

    def page_count(self, path):
        time.sleep(0.05)
        return self._n(path)

    def render_pages(self, path, out_dir, dpi=40, pages=None, callback=None, on_count=None):
        from PySide6.QtGui import QImage, QPainter, QColor, QFont
        from PySide6.QtCore import Qt
        n = self._n(path)
        if on_count:
            on_count(n)
        os.makedirs(out_dir, exist_ok=True)
        for p in pages or range(1, n + 1):
            w, h = int(8.27 * dpi), int(11.69 * dpi)
            img = QImage(w, h, QImage.Format_RGB32)
            img.fill(QColor("white"))
            qp = QPainter(img)
            qp.setPen(QColor("#999"))
            for y in range(int(h * 0.25), int(h * 0.9), max(6, h // 30)):
                qp.drawLine(int(w * 0.12), y, int(w * 0.88), y)
            qp.setPen(QColor("#333"))
            f = QFont()
            f.setPixelSize(max(12, h // 10))
            f.setBold(True)
            qp.setFont(f)
            qp.drawText(img.rect().adjusted(0, int(h * 0.05), 0, -int(h * 0.75)), Qt.AlignCenter,
                        "%s\n%d" % (os.path.splitext(os.path.basename(path))[0][:6], p))
            qp.end()
            fp = os.path.join(out_dir, "p%d_%d.bmp" % (p, dpi))
            img.save(fp, "BMP")
            time.sleep(0.02)
            if callback:
                callback(p, fp)
        return n

    def extract_range(self, src, first, last, dst, fmt="hwp"):
        self.calls.append(("extract", os.path.basename(src), first, last, os.path.basename(dst), fmt))
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write("PAGES=%d\nfrom %s %d-%d\n" % (last - first + 1, src, first, last))
        self._docs[dst] = last - first + 1
        return last - first + 1

    def assemble(self, pieces, dst, fmt="hwp", tmp_dir=None, progress=None):
        self.calls.append(("assemble", [os.path.basename(p) for p in pieces], os.path.basename(dst), fmt))
        total = sum(self._n(p) for p in pieces)
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write("PAGES=%d\n" % total + "\n".join(pieces))
        return total

    def convert(self, src, dst, fmt):
        self.calls.append(("convert", os.path.basename(src), os.path.basename(dst), fmt))
        os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
        shutil.copyfile(src, dst)

    def diagnose(self, path):
        return "✔ 시험 모드(가짜 엔진): %s, %d쪽" % (os.path.basename(path), self._n(path))
