"""
exhibit_renumber.py
====================
HWPX 파일에서 증거 번호를 본문 등장 순서 기준으로 자동 재번호매김합니다.

지원 형식 (자동 판별):
    - 소장/준비서면(원고): '갑 제N호증'
    - 답변서/준비서면(피고): '을 제N호증' 또는 '을 제호증' (번호 미기재 시 자동 부여)
    - 변호인의견서: '참고자료 N'

사용법:
    python exhibit_renumber.py input.hwpx               (자동 출력 파일명)
    python exhibit_renumber.py input.hwpx output.hwpx    (출력 파일명 지정)
    python exhibit_renumber.py --preview input.hwpx      (미리보기만)

동작:
    1. HWPX에서 section0.xml 파싱
    2. 문서 유형 자동 판별 (갑호증 / 을호증 / 참고자료)
    3. 본문에서 증거가 최초 등장하는 순서대로 새 번호 매핑 생성
       - 번호가 없는 경우(을 제호증): 등장 순서대로 번호 자동 부여
       - 이미 등장한 증거가 다시 나오면 기존 번호 유지 (고정)
    4. 마무리 섹션(입증방법/참고자료 등) 이하의 번호 목록도 재생성
    5. 수정된 XML로 새 HWPX 파일 출력
"""

import re
import sys
import zipfile
import shutil
import os
from xml.etree import ElementTree as ET
from copy import deepcopy

# ── 네임스페이스 ────────────────────────────────────────────────
HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"
HC = "http://www.hancom.co.kr/hwpml/2011/core"

ET.register_namespace("hp", HP)
ET.register_namespace("hs", HS)
ET.register_namespace("hc", HC)

# ── 정규식 ──────────────────────────────────────────────────────
# 갑호증 (번호 있음)
EXHIBIT_A_RE = re.compile(r"갑\s*제(\d+)호증")
EXHIBIT_A_LIST_RE = re.compile(r"^(?:\d+[\.\s]+)?갑\s*제\d+호증")

# 을호증 (번호 있음)
EXHIBIT_B_RE = re.compile(r"을\s*제(\d+)호증")
EXHIBIT_B_LIST_RE = re.compile(r"^(?:\d+[\.\s]+)?을\s*제\d+호증")

# 을호증 (번호 없음) — "을 제호증" 패턴 (제와 호증 사이에 숫자 없음)
EXHIBIT_B_NONUM_RE = re.compile(r"을\s*제호증")

# 참고자료
REFERENCE_RE = re.compile(r"참고자료\s*(\d+)")
REFERENCE_LIST_RE = re.compile(r"^(?:\d+[\.\s]+)?참고자료\s*\d+")

# 마무리 섹션 헤더 키워드 (공백 제거 후 비교)
SECTION_KEYWORDS = {"입증방법", "소명방법", "참고자료", "첨부서류", "첨부자료", "참고자료목록"}


# ═══════════════════════════════════════════════════════════════
# 공통 유틸
# ═══════════════════════════════════════════════════════════════
def get_para_texts(para_el):
    """단락 요소에서 <hp:t> 텍스트를 모두 이어붙여 반환."""
    parts = []
    for t in para_el.iter(f"{{{HP}}}t"):
        if t.text:
            parts.append(t.text)
    return "".join(parts)


def set_para_texts(para_el, new_text):
    """단락 요소의 첫 번째 <hp:t>에 텍스트를 쓰고 나머지 <hp:t>를 비운다."""
    t_nodes = list(para_el.iter(f"{{{HP}}}t"))
    if not t_nodes:
        return
    t_nodes[0].text = new_text
    for t in t_nodes[1:]:
        t.text = ""


def is_auto_numbered_para(para_el, header_root):
    """단락의 paraPrIDRef가 자동번호 스타일인지 확인."""
    para_pr_id = para_el.get("paraPrIDRef")
    if para_pr_id is None:
        return False
    HH = "http://www.hancom.co.kr/hwpml/2011/head"
    for el in header_root.iter(f"{{{HH}}}paraPr"):
        if el.get("id") == para_pr_id:
            heading = el.find(f"{{{HH}}}heading")
            if heading is not None and heading.get("type") == "NUMBER":
                return True
    return False


def build_mapping(order):
    """등장 순서 → 새 번호 매핑 { old_n: new_n }"""
    return {old_n: new_n for new_n, old_n in enumerate(order, start=1)}


def find_section_idx(paragraphs, list_start_re):
    """마무리 섹션 헤더 단락의 인덱스 반환. 없으면 None."""
    for i, para_el in enumerate(paragraphs):
        text = get_para_texts(para_el)
        cleaned = re.sub(r"\s", "", text.strip())

        if cleaned not in SECTION_KEYWORDS:
            continue

        for j in range(i + 1, min(i + 5, len(paragraphs))):
            next_text = get_para_texts(paragraphs[j]).strip()
            if next_text == "":
                continue
            if list_start_re.match(next_text):
                return i
            break

    return None


def clone_para_with_text(template_para, new_text):
    """template_para의 서식을 유지하면서 텍스트만 교체한 새 단락 반환."""
    new_para = deepcopy(template_para)
    set_para_texts(new_para, new_text)
    return new_para


# ═══════════════════════════════════════════════════════════════
# 문서 유형 자동 판별
# ═══════════════════════════════════════════════════════════════
def detect_mode(paragraphs):
    """
    문서 전체를 분석하여 유형 판별.
    반환: "exhibit_a" | "exhibit_b" | "exhibit_b_nonum" | "reference"
    """
    a_count = 0
    b_count = 0
    b_nonum_count = 0
    ref_count = 0

    for para_el in paragraphs:
        text = get_para_texts(para_el)
        a_count += len(EXHIBIT_A_RE.findall(text))
        b_count += len(EXHIBIT_B_RE.findall(text))
        ref_count += len(REFERENCE_RE.findall(text))
        # 을 제호증 (번호 없음) — 을 제N호증으로 이미 매칭된 부분 제외
        remaining = EXHIBIT_B_RE.sub("", text)
        b_nonum_count += len(EXHIBIT_B_NONUM_RE.findall(remaining))

    # 번호 없는 을호증이 있으면 우선
    if b_nonum_count > 0:
        return "exhibit_b_nonum"
    # 나머지는 개수 비교
    counts = {"exhibit_a": a_count, "exhibit_b": b_count, "reference": ref_count}
    best = max(counts, key=counts.get)
    if counts[best] == 0:
        return "exhibit_a"  # 기본값
    return best


# ═══════════════════════════════════════════════════════════════
# 모드별 설정
# ═══════════════════════════════════════════════════════════════
MODE_CONFIGS = {
    "exhibit_a": {
        "label": "갑호증",
        "pattern": EXHIBIT_A_RE,
        "list_start": EXHIBIT_A_LIST_RE,
        "format_name": lambda n: f"갑 제{n}호증",
        "format_line": lambda n, name: f"갑 제{n}호증  {name}",
    },
    "exhibit_b": {
        "label": "을호증",
        "pattern": EXHIBIT_B_RE,
        "list_start": EXHIBIT_B_LIST_RE,
        "format_name": lambda n: f"을 제{n}호증",
        "format_line": lambda n, name: f"을 제{n}호증  {name}",
    },
    "exhibit_b_nonum": {
        "label": "을호증 (번호 미기재)",
        "pattern": EXHIBIT_B_NONUM_RE,  # 번호 없는 패턴
        "list_start": EXHIBIT_B_LIST_RE,  # 입증방법 목록에는 번호가 있을 수 있음
        "format_name": lambda n: f"을 제{n}호증",
        "format_line": lambda n, name: f"을 제{n}호증  {name}",
    },
    "reference": {
        "label": "참고자료",
        "pattern": REFERENCE_RE,
        "list_start": REFERENCE_LIST_RE,
        "format_name": lambda n: f"참고자료 {n}",
        "format_line": lambda n, name: f"참고자료 {n}  {name}",
    },
}


# ═══════════════════════════════════════════════════════════════
# 번호 있는 모드 공통 로직 (갑호증 / 을호증(번호) / 참고자료)
# ═══════════════════════════════════════════════════════════════
def replace_numbers(text, mapping, pattern):
    """텍스트 내 증거 번호를 mapping에 따라 치환."""
    def replacer(m):
        old_n = int(m.group(1))
        new_n = mapping.get(old_n, old_n)
        original = m.group(0)
        return re.sub(r"\d+", str(new_n), original, count=1)
    return pattern.sub(replacer, text)


def build_registry(paragraphs, pattern):
    """본문에서 증거 최초 등장 순서 기록. 반환: (order, registry)"""
    registry = {}
    order = []

    for para_el in paragraphs:
        text = get_para_texts(para_el)
        for m in pattern.finditer(text):
            n = int(m.group(1))
            if n not in registry:
                name_after = text[m.end():].strip()
                name_after = re.sub(r"^[\.\s]+", "", name_after)
                name_after = re.sub(r"^\d+\.\s*", "", name_after)
                registry[n] = name_after
                order.append(n)

    return order, registry


def collect_list_items(paragraphs, section_idx, header_root, pattern):
    """마무리 목록 단락 수집."""
    items = []
    for i in range(section_idx + 1, len(paragraphs)):
        text = get_para_texts(paragraphs[i]).strip()
        if not text:
            continue
        m = pattern.search(text)
        if not m:
            break
        old_n = int(m.group(1))
        name = text[m.end():].strip()
        name = re.sub(r"^[\.\s]+", "", name)
        auto_num = is_auto_numbered_para(paragraphs[i], header_root)
        items.append((paragraphs[i], auto_num, old_n, name))
    return items


# ═══════════════════════════════════════════════════════════════
# 번호 없는 을호증 전용 로직
# ═══════════════════════════════════════════════════════════════
def build_registry_nonum(paragraphs, pattern):
    """
    번호 없는 '을 제호증'의 등장 순서를 기록.
    각 '을 제호증'에 순차적으로 1, 2, 3... 번호를 부여.
    반환: (order, registry)
      order    : [1, 2, 3, ...] 순차 번호
      registry : { n: name }
    """
    registry = {}
    order = []
    seq = 0

    for para_el in paragraphs:
        text = get_para_texts(para_el)
        for m in pattern.finditer(text):
            seq += 1
            name_after = text[m.end():].strip()
            name_after = re.sub(r"^[\.\s]+", "", name_after)
            registry[seq] = name_after
            order.append(seq)

    return order, registry


def replace_nonum_sequential(text, counter, pattern):
    """
    번호 없는 '을 제호증'을 순차 번호로 치환.
    counter는 [현재값]을 담은 리스트 (mutable reference).
    """
    def replacer(m):
        counter[0] += 1
        original = m.group(0)
        # "을 제호증" → "을 제N호증" (공백 패턴 보존)
        return original.replace("제호증", f"제{counter[0]}호증")
    return pattern.sub(replacer, text)


def collect_list_items_for_b(paragraphs, section_idx, header_root):
    """을호증 입증방법 목록 수집 (번호 있는 을 제N호증 패턴으로)."""
    return collect_list_items(paragraphs, section_idx, header_root, EXHIBIT_B_RE)


# ═══════════════════════════════════════════════════════════════
# 메인: 번호 있는 모드 (갑호증 / 을호증(번호) / 참고자료)
# ═══════════════════════════════════════════════════════════════
def process_numbered(root, paragraphs, header_root, cfg):
    """번호가 있는 증거를 재번호매김."""
    pattern = cfg["pattern"]
    list_start = cfg["list_start"]

    section_idx = find_section_idx(paragraphs, list_start)
    if section_idx is None:
        print(f"[경고] 마무리 섹션을 찾지 못했습니다. 본문 치환만 수행합니다.")
        body_paragraphs = paragraphs
    else:
        body_paragraphs = paragraphs[:section_idx]

    order, registry = build_registry(body_paragraphs, pattern)

    if not order:
        print(f"{cfg['label']}을(를) 발견하지 못했습니다.")
        return False

    # 마무리 목록 수집 및 본문 미등장 증거 경고
    list_items = []
    if section_idx is not None:
        list_items = collect_list_items(paragraphs, section_idx, header_root, pattern)
        for _, _, old_n, name in list_items:
            if old_n not in registry:
                print(f"[경고] {cfg['format_name'](old_n)}이(가) 마무리 목록에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    mapping = build_mapping(order)

    print("=" * 60)
    print(f"[확인] {cfg['label']} 번호 재매핑")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        print(f"  {cfg['format_name'](old_n)} → {cfg['format_name'](new_n)}  ({registry[old_n][:40]})")
    print("=" * 60)

    # 본문 치환
    for para_el in body_paragraphs:
        original = get_para_texts(para_el)
        replaced = replace_numbers(original, mapping, pattern)
        if replaced != original:
            set_para_texts(para_el, replaced)

    # 마무리 목록 재생성
    if section_idx is not None and list_items:
        _regenerate_list(root, list_items, order, registry, cfg)

    return True


# ═══════════════════════════════════════════════════════════════
# 메인: 번호 없는 을호증 모드
# ═══════════════════════════════════════════════════════════════
def process_nonum(root, paragraphs, header_root, cfg):
    """번호 없는 '을 제호증'에 순차 번호를 부여."""
    pattern = cfg["pattern"]  # EXHIBIT_B_NONUM_RE
    list_start = cfg["list_start"]  # EXHIBIT_B_LIST_RE

    section_idx = find_section_idx(paragraphs, list_start)
    if section_idx is None:
        # 입증방법에 을 제N호증이 없을 수도 있으므로, 을 제호증(번호없음) 패턴도 시도
        # "입증방법" 키워드 단독 + 다음 줄에 "을 제호증" 패턴
        section_idx = _find_section_idx_nonum(paragraphs)

    if section_idx is None:
        print(f"[경고] 마무리 섹션을 찾지 못했습니다. 본문 치환만 수행합니다.")
        body_paragraphs = paragraphs
    else:
        body_paragraphs = paragraphs[:section_idx]

    order, registry = build_registry_nonum(body_paragraphs, pattern)

    if not order:
        print(f"{cfg['label']}을(를) 발견하지 못했습니다.")
        return False

    # 입증방법 목록 수집 (번호 있는 을 제N호증 패턴)
    list_items = []
    if section_idx is not None:
        list_items = collect_list_items_for_b(paragraphs, section_idx, header_root)

    print("=" * 60)
    print(f"[확인] {cfg['label']} → 번호 자동 부여")
    print("-" * 60)
    for n in order:
        print(f"  을 제호증 → 을 제{n}호증  ({registry[n][:40]})")
    print("=" * 60)

    # 본문: "을 제호증" → "을 제N호증" 순차 치환
    counter = [0]
    for para_el in body_paragraphs:
        original = get_para_texts(para_el)
        replaced = replace_nonum_sequential(original, counter, pattern)
        if replaced != original:
            set_para_texts(para_el, replaced)

    # 마무리 목록 재생성
    if section_idx is not None and list_items:
        _regenerate_list(root, list_items, order, registry, cfg)
    elif section_idx is not None:
        # 입증방법 헤더는 있지만 목록이 비어있거나 패턴이 다른 경우
        # 기존 목록 항목을 찾아 재생성 시도
        print(f"\n[참고] 입증방법 목록 항목을 재생성할 템플릿을 찾지 못했습니다.")
        print(f"       입증방법 목록을 수동으로 확인해주세요.")

    return True


def _find_section_idx_nonum(paragraphs):
    """번호 없는 을 제호증용 마무리 섹션 탐지 (을 제호증 패턴도 허용)."""
    nonum_list_start = re.compile(r"^(?:\d+[\.\s]+)?을\s*제\d*호증")
    for i, para_el in enumerate(paragraphs):
        text = get_para_texts(para_el)
        cleaned = re.sub(r"\s", "", text.strip())
        if cleaned not in SECTION_KEYWORDS:
            continue
        for j in range(i + 1, min(i + 5, len(paragraphs))):
            next_text = get_para_texts(paragraphs[j]).strip()
            if next_text == "":
                continue
            if nonum_list_start.match(next_text):
                return i
            break
    return None


# ═══════════════════════════════════════════════════════════════
# 공통: 마무리 목록 재생성
# ═══════════════════════════════════════════════════════════════
def _regenerate_list(root, list_items, order, registry, cfg):
    """마무리 목록 단락을 삭제하고 새 목록으로 교체."""
    auto_tmpl = next(
        (p for p, is_auto, _, _ in list_items if is_auto),
        list_items[0][0]
    )

    root_children = list(root)
    first_para = list_items[0][0]
    last_para = list_items[-1][0]
    start_pos = root_children.index(first_para)
    end_pos = root_children.index(last_para)

    new_lines = []
    for new_n, old_n in enumerate(order, start=1):
        name = registry[old_n]
        new_lines.append(cfg["format_line"](new_n, name))

    for i in range(end_pos, start_pos - 1, -1):
        root.remove(root_children[i])

    for line in reversed(new_lines):
        new_para = clone_para_with_text(auto_tmpl, line)
        root.insert(start_pos, new_para)

    print(f"\n[완료] 마무리 목록 재생성 완료 ({len(new_lines)}개 항목)")


# ═══════════════════════════════════════════════════════════════
# 진입점
# ═══════════════════════════════════════════════════════════════
def renumber_hwpx(input_path, output_path):
    shutil.copy2(input_path, output_path)

    with zipfile.ZipFile(input_path, "r") as zin:
        xml_bytes = zin.read("Contents/section0.xml")
        header_bytes = zin.read("Contents/header.xml")

    header_root = ET.fromstring(header_bytes)
    root = ET.fromstring(xml_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    mode = detect_mode(paragraphs)
    cfg = MODE_CONFIGS[mode]
    print(f"[감지] 문서 유형: {cfg['label']}")

    if mode == "exhibit_b_nonum":
        success = process_nonum(root, paragraphs, header_root, cfg)
    else:
        success = process_numbered(root, paragraphs, header_root, cfg)

    if not success:
        return

    # 저장
    new_xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    tmp_path = output_path + ".tmp"
    with zipfile.ZipFile(input_path, "r") as zin, \
         zipfile.ZipFile(tmp_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "Contents/section0.xml":
                zout.writestr(item, new_xml_bytes)
            else:
                zout.writestr(item, zin.read(item.filename))

    os.replace(tmp_path, output_path)
    print(f"\n[저장] 저장 완료: {output_path}")


def preview_only(input_path):
    """HWPX를 수정하지 않고 재매핑 결과만 출력."""
    with zipfile.ZipFile(input_path, "r") as z:
        xml_bytes = z.read("Contents/section0.xml")
        header_bytes = z.read("Contents/header.xml")

    root = ET.fromstring(xml_bytes)
    header_root = ET.fromstring(header_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    mode = detect_mode(paragraphs)
    cfg = MODE_CONFIGS[mode]
    print(f"[감지] 문서 유형: {cfg['label']}")

    if mode == "exhibit_b_nonum":
        _preview_nonum(paragraphs, header_root, cfg)
    else:
        _preview_numbered(paragraphs, header_root, cfg)


def _preview_numbered(paragraphs, header_root, cfg):
    pattern = cfg["pattern"]
    list_start = cfg["list_start"]

    section_idx = find_section_idx(paragraphs, list_start)
    body_paragraphs = paragraphs[:section_idx] if section_idx else paragraphs
    order, registry = build_registry(body_paragraphs, pattern)

    if not order:
        print(f"{cfg['label']}을(를) 발견하지 못했습니다.")
        return

    if section_idx is not None:
        list_items = collect_list_items(paragraphs, section_idx, header_root, pattern)
        for _, _, old_n, name in list_items:
            if old_n not in registry:
                print(f"[경고] {cfg['format_name'](old_n)}이(가) 마무리 목록에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    mapping = build_mapping(order)

    print("=" * 60)
    print(f"[미리보기] {cfg['label']} 번호 재매핑 결과")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        arrow = "→" if old_n != new_n else "="
        print(f"  {cfg['format_name'](old_n)} {arrow} {cfg['format_name'](new_n)}  {registry[old_n][:50]}")
    print("-" * 60)
    print(f"\n[확인] 재생성될 마무리 목록:")
    for new_n, old_n in enumerate(order, start=1):
        print(f"  {new_n}. {cfg['format_line'](new_n, registry[old_n])}")
    print("=" * 60)


def _preview_nonum(paragraphs, header_root, cfg):
    pattern = cfg["pattern"]

    section_idx = find_section_idx(paragraphs, cfg["list_start"])
    if section_idx is None:
        section_idx = _find_section_idx_nonum(paragraphs)

    body_paragraphs = paragraphs[:section_idx] if section_idx else paragraphs
    order, registry = build_registry_nonum(body_paragraphs, pattern)

    if not order:
        print(f"{cfg['label']}을(를) 발견하지 못했습니다.")
        return

    print("=" * 60)
    print(f"[미리보기] {cfg['label']} → 번호 자동 부여 결과")
    print("-" * 60)
    for n in order:
        print(f"  을 제호증 → 을 제{n}호증  ({registry[n][:50]})")
    print("-" * 60)
    print(f"\n[확인] 재생성될 마무리 목록:")
    for new_n, old_n in enumerate(order, start=1):
        print(f"  {new_n}. {cfg['format_line'](new_n, registry[old_n])}")
    print("=" * 60)


# ── CLI ──────────────────────────────────────────────────────────
import glob

if __name__ == "__main__":
    args = sys.argv[1:]

    # ── 인자 없이 더블클릭: 같은 폴더의 .hwpx 자동 처리 ────────
    if not args:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        hwpx_files = glob.glob(os.path.join(script_dir, "*.hwpx"))
        targets = [f for f in hwpx_files if not f.endswith("_숫자정렬완.hwpx")]

        if not targets:
            print("처리할 .hwpx 파일이 없습니다.")
            print(f"이 폴더에 .hwpx 파일을 넣어주세요:\n  {script_dir}")
            input("\n아무 키나 눌러 종료...")
            sys.exit(0)

        print(f"발견된 파일 {len(targets)}개:\n")
        for f in targets:
            print(f"  - {os.path.basename(f)}")
        print()

        for f in targets:
            base, ext = os.path.splitext(f)
            output = base + "_숫자정렬완" + ext
            print(f"{'─' * 60}")
            print(f"▶ {os.path.basename(f)}")
            print(f"{'─' * 60}")
            try:
                renumber_hwpx(f, output)
            except Exception as e:
                print(f"[오류] {e}")
            print()

        print(f"{'━' * 60}")
        print("모든 파일 처리 완료!")
        print(f"{'━' * 60}")
        input("\n아무 키나 눌러 종료...")
        sys.exit(0)

    # ── 인자 있는 경우: 기존 CLI 동작 ───────────────────────────
    if args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if args[0] == "--preview":
        if len(args) < 2:
            print("사용법: python exhibit_renumber.py --preview input.hwpx")
            sys.exit(1)
        preview_only(args[1])

    elif len(args) == 1:
        input_path = args[0]
        base, ext = os.path.splitext(input_path)
        output_path = base + "_숫자정렬완" + ext
        renumber_hwpx(input_path, output_path)

    elif len(args) == 2:
        renumber_hwpx(args[0], args[1])

    else:
        print("사용법: python exhibit_renumber.py input.hwpx")
        print("        python exhibit_renumber.py input.hwpx output.hwpx")
        print("        python exhibit_renumber.py --preview input.hwpx")
        sys.exit(1)
