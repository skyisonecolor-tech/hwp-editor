# -*- coding: utf-8 -*-
"""결과 쪽 목록 → 한글 작업 계획.

결과 문서는 (원본 파일, 쪽 번호)의 순서 있는 목록이다.
 - 같은 파일의 연속된 쪽(3,4,5…)은 한 덩어리로 추출해 원래 배치를 최대한 유지한다.
 - 파일 전체가 순서대로 들어가면 추출 없이 원본을 그대로 끼워 넣는다.
"""
import os
import re

EXT = {"hwp": ".hwp", "hwpx": ".hwpx", "pdf": ".pdf"}


def parse_ranges(text, max_page):
    """'1-3, 5, 8-' → [1,2,3,5,8,...max]. 잘못된 입력은 ValueError."""
    pages = []
    text = text.replace("~", "-").replace("–", "-")
    for part in re.split(r"[,\s]+", text.strip()):
        if not part:
            continue
        m = re.fullmatch(r"(\d*)-(\d*)", part)
        if m:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else max_page
        elif part.isdigit():
            a = b = int(part)
        else:
            raise ValueError("이해할 수 없는 범위: " + part)
        if a < 1 or b > max_page or a > b:
            raise ValueError("쪽 범위를 벗어났습니다: %s (전체 %d쪽)" % (part, max_page))
        pages.extend(range(a, b + 1))
    if not pages:
        raise ValueError("쪽 범위를 입력하세요.")
    return pages


def make_runs(items):
    """items: [(file_id, page)] → [(file_id, first, last)] 연속 구간."""
    runs = []
    for fid, page in items:
        if runs and runs[-1][0] == fid and runs[-1][2] + 1 == page:
            runs[-1][2] = page
        else:
            runs.append([fid, page, page])
    return [tuple(r) for r in runs]


def _safe(name):
    return re.sub(r'[\\/:*?"<>|]', "_", name)


def execute(engine, items, files, mode, fmt, dst, tmp_dir, progress=None):
    """
    items : [(file_id, page)]
    files : {file_id: (path, page_count)}
    mode  : 'merge' (하나로) | 'split' (쪽마다)
    dst   : merge → 결과 파일 경로, split → 결과 폴더
    반환  : 만든 파일 경로 목록
    """
    os.makedirs(tmp_dir, exist_ok=True)
    say = progress or (lambda msg, i, n: None)

    if mode == "split":
        outs, used = [], set()
        for i, (fid, page) in enumerate(items, 1):
            path, n = files[fid]
            stem = _safe(os.path.splitext(os.path.basename(path))[0])
            name = "%s_%03d쪽" % (stem, page)
            k, base = 2, name
            while name.lower() in used:
                name = "%s(%d)" % (base, k)
                k += 1
            used.add(name.lower())
            out = os.path.join(dst, name + EXT[fmt])
            say("%s %d쪽 저장 중" % (os.path.basename(path), page), i, len(items))
            if n == 1:
                engine.convert(path, out, fmt)
            else:
                engine.extract_range(path, page, page, out, fmt)
            outs.append(out)
        return outs

    runs = make_runs(items)
    pieces, cache = [], {}
    total = len(runs) + 1
    for i, (fid, a, b) in enumerate(runs, 1):
        path, n = files[fid]
        if a == 1 and b == n:
            pieces.append(path)  # 파일 전체 → 원본 그대로
            continue
        key = (fid, a, b)
        if key not in cache:
            say("%s %d~%d쪽 추출 중" % (os.path.basename(path), a, b), i, total)
            tmp = os.path.join(tmp_dir, "piece_%03d_%d_%d.hwp" % (i, a, b))
            engine.extract_range(path, a, b, tmp, "hwp")
            cache[key] = tmp
        pieces.append(cache[key])

    say("하나의 문서로 합치는 중", total, total)
    if len(pieces) == 1 and pieces[0] in [p for p, _ in files.values()]:
        engine.convert(pieces[0], dst, fmt)
    else:
        engine.assemble(pieces, dst, fmt, tmp_dir=tmp_dir,
                        progress=lambda k, m: say("합치는 중 (%d/%d)" % (k, m), total, total))
    return [dst]
