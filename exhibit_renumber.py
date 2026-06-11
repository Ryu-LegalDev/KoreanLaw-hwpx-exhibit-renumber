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

# 갑호증 (번호 없음) — "갑 제호증" 패턴 (제와 호증 사이에 숫자 없음)
EXHIBIT_A_NONUM_RE = re.compile(r"갑\s*제호증")

# 을호증 (번호 없음) — "을 제호증" 패턴 (제와 호증 사이에 숫자 없음)
EXHIBIT_B_NONUM_RE = re.compile(r"을\s*제호증")

# 참고자료
REFERENCE_RE = re.compile(r"참고자료\s*(\d+)")
REFERENCE_LIST_RE = re.compile(r"^(?:\d+[\.\s]+)?참고자료\s*\d+")

# 마무리 섹션 헤더 키워드 (공백 제거 후 비교)
SECTION_KEYWORDS = {"입증방법", "소명방법", "참고자료", "첨부서류", "첨부자료", "참고자료목록"}


# ═══════════════════════════════════════════════════════════════
# 전처리: 번호 없는 증거에 임시 번호 부여
# ═══════════════════════════════════════════════════════════════
def preprocess_all_unnumbered(paragraphs):
    """
    번호 없는 '갑 제호증', '을 제호증'에 임시 번호를 부여.
    기존 최대 번호 + 1부터 순차 부여하여, 이후 정렬 로직이 통일 처리 가능.
    """
    for numbered_re, unnumbered_re, label in [
        (EXHIBIT_A_RE, EXHIBIT_A_NONUM_RE, "갑호증"),
        (EXHIBIT_B_RE, EXHIBIT_B_NONUM_RE, "을호증"),
    ]:
        # 현재 최대 번호 확인
        max_n = 0
        has_unnumbered = False
        for para_el in paragraphs:
            text = get_para_texts(para_el)
            for m in numbered_re.finditer(text):
                max_n = max(max_n, int(m.group(1)))
            # 이미 번호가 붙은 부분을 제거한 뒤 번호 없는 패턴 확인
            remaining = numbered_re.sub("", text)
            if unnumbered_re.search(remaining):
                has_unnumbered = True

        if not has_unnumbered:
            continue

        # 임시 번호 부여 (max + 1부터)
        counter = [max_n]
        for para_el in paragraphs:
            original = get_para_texts(para_el)

            def replacer(m):
                counter[0] += 1
                return m.group(0).replace("제호증", f"제{counter[0]}호증")

            replaced = unnumbered_re.sub(replacer, original)
            if replaced != original:
                set_para_texts(para_el, replaced)

        assigned = counter[0] - max_n
        print(f"[자동] 번호 없는 {label} {assigned}개에 임시 번호 부여 (제{max_n + 1}~제{counter[0]}호증)")


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
        "format_line": lambda n, name: f"갑 제{n}호증 {name}",
        "needs_seq_prefix": True,   # 입증방법에 "1. 갑 제1호증 ..."
        "folder_suffix": "_입증방법",
    },
    "exhibit_b": {
        "label": "을호증",
        "pattern": EXHIBIT_B_RE,
        "list_start": EXHIBIT_B_LIST_RE,
        "format_name": lambda n: f"을 제{n}호증",
        "format_line": lambda n, name: f"을 제{n}호증 {name}",
        "needs_seq_prefix": True,   # 입증방법에 "1. 을 제1호증 ..."
        "folder_suffix": "_입증방법",
    },
    "exhibit_b_nonum": {
        "label": "을호증 (번호 미기재)",
        "pattern": EXHIBIT_B_NONUM_RE,
        "list_start": EXHIBIT_B_LIST_RE,
        "format_name": lambda n: f"을 제{n}호증",
        "format_line": lambda n, name: f"을 제{n}호증 {name}",
        "needs_seq_prefix": True,
        "folder_suffix": "_입증방법",
    },
    "reference": {
        "label": "참고자료",
        "pattern": REFERENCE_RE,
        "list_start": REFERENCE_LIST_RE,
        "format_name": lambda n: f"참고자료 {n}",
        "format_line": lambda n, name: f"참고자료 {n}. {name}",
        "needs_seq_prefix": False,  # "참고자료 1. 이름"에 이미 번호 포함
        "folder_suffix": "_참고자료",
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


def _is_citation_line(text, match):
    """증거 인용 줄인지 판별 (본문 문장 속 언급과 구분).

    인용 줄: 줄 시작이 비어있거나 대시/불릿 뒤에 바로 증거번호가 오는 형태
      예) "- 갑 제8호증 녹취록"  /  "갑 제3호증 매매계약서"
    본문:  앞에 한글 텍스트가 있는 형태
      예) "또한 다음 갑 제8호증 녹취록의 내용을 보면, ..."
    """
    before = text[:match.start()].strip()
    # 대시·불릿·공백만 남으면 인용 줄
    before_clean = re.sub(r'^[-·•\-\s\d.]+$', '', before)
    return len(before_clean) == 0


def build_registry(paragraphs, pattern):
    """본문에서 증거 최초 등장 순서 기록. 반환: (order, registry)

    이름은 '인용 줄'(- 갑 제N호증 이름)에서 우선적으로 가져오고,
    본문 문장 속 언급("갑 제N호증 녹취록의 내용을 보면, ...")은 이름으로 채택하지 않는다.
    """
    registry = {}          # { n: name }
    registry_is_cite = {}  # { n: True/False } — 인용 줄에서 가져온 이름인지
    order = []

    for para_el in paragraphs:
        text = get_para_texts(para_el)
        for m in pattern.finditer(text):
            n = int(m.group(1))
            is_cite = _is_citation_line(text, m)
            name_after = text[m.end():].strip()
            name_after = re.sub(r"^[\.\s]+", "", name_after)

            if n not in registry:
                # 첫 등장: 순서 기록 + 이름 임시 저장
                registry[n] = name_after
                registry_is_cite[n] = is_cite
                order.append(n)
            elif is_cite and not registry_is_cite.get(n, False):
                # 이전에 본문 문장에서 가져온 이름 → 인용 줄 이름으로 교체
                registry[n] = name_after
                registry_is_cite[n] = True

    return order, registry


def deduplicate_registry(order, registry, cfg):
    """같은 이름의 증거를 하나로 병합.

    예) 갑 제3호증 '공급계약서', 갑 제6호증 '공급계약서'
        → 갑 제6호증을 갑 제3호증으로 통합, order에서 제거
    반환: (new_order, new_registry, merge_map)
      merge_map: { 6: 3 } — 나중 번호 → 먼저 등장한 번호
    """
    name_to_first = {}   # { name: first_old_n }
    merge_map = {}       # { later_n: first_n }
    new_order = []

    for old_n in order:
        name = registry.get(old_n, "").strip()
        if not name:
            new_order.append(old_n)
            continue

        if name in name_to_first:
            first_n = name_to_first[name]
            merge_map[old_n] = first_n
            print(f"[병합] {cfg['format_name'](old_n)} → {cfg['format_name'](first_n)}  (동일 증거: {name[:40]})")
        else:
            name_to_first[name] = old_n
            new_order.append(old_n)

    # 병합된 번호는 registry에서 제거
    new_registry = {n: v for n, v in registry.items() if n not in merge_map}

    return new_order, new_registry, merge_map


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
                if not name.strip():
                    # 이름 없는 입증방법 항목은 자리표시자이므로 무시
                    # (예: "갑 제1호증" 만 적혀있고 증거 이름이 없는 경우)
                    continue
                print(f"[경고] {cfg['format_name'](old_n)}이(가) 마무리 목록에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    # 동일 이름 증거 병합 (갑 제3호증, 갑 제6호증 둘 다 '공급계약서' → 하나로)
    order, registry, merge_map = deduplicate_registry(order, registry, cfg)

    mapping = build_mapping(order)

    # merge_map의 나중 번호도 mapping에 반영 (6→3의 새 번호로)
    for later_n, first_n in merge_map.items():
        mapping[later_n] = mapping[first_n]

    print("=" * 60)
    print(f"[확인] {cfg['label']} 번호 재매핑")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        print(f"  {cfg['format_name'](old_n)} → {cfg['format_name'](new_n)}  ({registry[old_n][:40]})")
    if merge_map:
        print("-" * 60)
        for later_n, first_n in merge_map.items():
            print(f"  {cfg['format_name'](later_n)} → {cfg['format_name'](mapping[later_n])}  (동일 증거 병합)")
    print("=" * 60)

    # 본문 치환 (병합 포함)
    for para_el in body_paragraphs:
        original = get_para_texts(para_el)
        replaced = replace_numbers(original, mapping, pattern)
        if replaced != original:
            set_para_texts(para_el, replaced)

    # 마무리 목록 재생성 (병합으로 줄어든 order 기준)
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
    # 자동번호 스타일 템플릿 우선 선택, 없으면 첫 항목 사용
    tmpl_is_auto = False
    auto_tmpl = list_items[0][0]
    for p, is_auto, _, _ in list_items:
        if is_auto:
            auto_tmpl = p
            tmpl_is_auto = True
            break

    root_children = list(root)
    first_para = list_items[0][0]
    last_para = list_items[-1][0]
    start_pos = root_children.index(first_para)
    end_pos = root_children.index(last_para)

    new_lines = []
    for new_n, old_n in enumerate(order, start=1):
        name = registry[old_n]
        line = cfg["format_line"](new_n, name)
        # 자동번호 스타일이 아니고 순번 접두사가 필요한 모드만 "1. " 추가
        # (참고자료는 "참고자료 1. 이름"에 이미 번호가 포함되므로 제외)
        if not tmpl_is_auto and cfg.get("needs_seq_prefix", True):
            line = f"{new_n}. {line}"
        new_lines.append(line)

    for i in range(end_pos, start_pos - 1, -1):
        root.remove(root_children[i])

    for line in reversed(new_lines):
        new_para = clone_para_with_text(auto_tmpl, line)
        root.insert(start_pos, new_para)

    print(f"\n[완료] 마무리 목록 재생성 완료 ({len(new_lines)}개 항목)")


# ═══════════════════════════════════════════════════════════════
# 증거 파일 이름 변경
# ═══════════════════════════════════════════════════════════════
def extract_evidence_names(hwpx_path):
    """처리된 HWPX 파일에서 최종 증거 번호-이름 매핑을 추출."""
    with zipfile.ZipFile(hwpx_path, "r") as z:
        xml_bytes = z.read("Contents/section0.xml")
        header_bytes = z.read("Contents/header.xml")

    root = ET.fromstring(xml_bytes)
    header_root = ET.fromstring(header_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    mode = detect_mode(paragraphs)
    cfg = MODE_CONFIGS[mode]
    # 출력 파일에서는 을호증에 이미 번호가 부여되었으므로 exhibit_b로 처리
    if mode == "exhibit_b_nonum":
        mode = "exhibit_b"
        cfg = MODE_CONFIGS[mode]
    pattern = cfg["pattern"]
    list_start = cfg["list_start"]

    section_idx = find_section_idx(paragraphs, list_start)
    evidence = {}

    # 입증방법 섹션에서 이름 추출 (가장 깔끔한 소스)
    if section_idx is not None:
        list_items = collect_list_items(paragraphs, section_idx, header_root, pattern)
        for _, _, n, name in list_items:
            if name.strip():
                evidence[n] = name.strip()

    # 입증방법에서 못 찾으면 본문에서 추출
    if not evidence:
        body = paragraphs[:section_idx] if section_idx else paragraphs
        _, reg = build_registry(body, pattern)
        evidence = {n: name.strip() for n, name in reg.items() if name.strip()}

    return evidence, cfg


def _normalize_name(s):
    """증거 이름 비교용 정규화: 선행 0 제거, 공백·구두점 통일."""
    s = re.sub(r"\.\s*0+(\d)", r". \1", s)   # ". 04." → ". 4."
    s = re.sub(r"\b0+(\d)", r"\1", s)         # "02.53" → "2.53"
    s = re.sub(r"\s+", " ", s).strip()        # 연속 공백 → 단일 공백
    return s


def _core_name(s):
    """증거 이름에서 핵심 키워드만 추출 (공백·특수문자·시간표기 차이 무시)."""
    s = _normalize_name(s)
    s = re.sub(r"[()（）\[\]~～:：.,\s]", "", s)  # 구두점·공백 모두 제거
    return s


def _strip_exhibit_prefix(fname_no_ext):
    """파일명에서 증거 접두사(갑 제N호증 등)를 제거하고 (clean, old_n)을 반환."""
    m = re.match(r"^(갑|을)\s*제(\d+)호증\s*", fname_no_ext)
    if m:
        old_n = int(m.group(2))
        clean = fname_no_ext[m.end():].strip()
        return clean, old_n

    m2 = re.match(r"^참고자료\s*(\d+)\s*\.?\s*", fname_no_ext)
    if m2:
        old_n = int(m2.group(1))
        clean = fname_no_ext[m2.end():].strip()
        return clean, old_n

    return fname_no_ext.strip(), None


def _safe_startswith(longer, shorter):
    """shorter로 시작하되, 바로 뒤에 숫자가 오면 False.

    '카카오톡 내역'.startswith('카카오톡 내역') → True
    '카카오톡 내역1'.startswith('카카오톡 내역') → False (뒤에 '1'이 바로 이어짐)
    '카카오톡 내역 추가분'.startswith('카카오톡 내역') → True (공백 분리)
    """
    if not longer.startswith(shorter):
        return False
    remainder = longer[len(shorter):]
    if not remainder:
        return True  # 완전 일치
    # 나머지가 숫자로 시작하면 다른 자료 (카카오톡 내역1 ≠ 카카오톡 내역)
    if remainder[0].isdigit():
        return False
    return True


def _names_match(file_clean, ev_name):
    """파일 이름과 증거 이름이 일치하는지 정규화 비교."""
    fc = _normalize_name(file_clean)
    en = _normalize_name(ev_name)
    if not fc or len(fc) < 2:
        return False
    # 정확 일치
    if fc == en:
        return True
    # 한쪽이 다른 쪽으로 시작 (숫자 접미사 보호)
    if _safe_startswith(en, fc) or _safe_startswith(fc, en):
        return True
    # 핵심 키워드 비교 (공백·괄호·콜론 등 차이 무시)
    fc_core = _core_name(file_clean)
    en_core = _core_name(ev_name)
    if fc_core and en_core and (fc_core == en_core
                                 or _safe_startswith(en_core, fc_core)
                                 or _safe_startswith(fc_core, en_core)):
        return True
    return False


def rename_evidence_files(input_hwpx, output_hwpx):
    """같은 폴더의 증거 파일명에 증거 번호를 자동 부여."""
    evidence, cfg = extract_evidence_names(output_hwpx)
    if not evidence:
        return

    # 원본에서도 증거 이름 추출 (번호 매핑용)
    old_evidence, _ = extract_evidence_names(input_hwpx)

    folder = os.path.dirname(os.path.abspath(input_hwpx))
    exclude = {os.path.basename(input_hwpx), os.path.basename(output_hwpx)}

    # 자료 폴더 경로 (문서 유형에 따라 접미사 결정)
    input_basename = os.path.splitext(os.path.basename(input_hwpx))[0]
    folder_suffix = cfg.get("folder_suffix", "_입증방법")
    data_folder = os.path.join(folder, f"{input_basename}{folder_suffix}")
    # 이전 버전 호환: _자료 폴더도 탐색 대상에 포함
    legacy_folder = os.path.join(folder, f"{input_basename}_자료")

    # 후보 파일 수집 (입력/출력 hwpx, .py 제외)
    # 루트 폴더 + 기존 자료 폴더 모두 탐색
    candidates = []       # (파일명, 원본경로) 쌍
    for f in os.listdir(folder):
        full = os.path.join(folder, f)
        if not os.path.isfile(full):
            continue
        if f in exclude or f.endswith('.py'):
            continue
        candidates.append((f, full))

    # 자료 폴더가 이미 있으면 그 안의 파일도 포함 (현재 접미사 + 이전 _자료)
    for search_folder in [data_folder, legacy_folder]:
        if not os.path.isdir(search_folder):
            continue
        for f in os.listdir(search_folder):
            full = os.path.join(search_folder, f)
            if not os.path.isfile(full):
                continue
            if f.endswith('.py'):
                continue
            if full not in {fp for _, fp in candidates}:
                candidates.append((f, full))

    if not candidates:
        return

    # 원본 번호 → 신규 번호 매핑 (이름 기준 대조)
    old_to_new = {}
    for old_n, old_name in old_evidence.items():
        for new_n, new_name in evidence.items():
            if _names_match(old_name, new_name):
                old_to_new[old_n] = new_n
                break

    # ── 1차: 이름 기반 매칭 (서면 증거 이름으로 파일명 통일) ──
    renames = []          # [(원본경로, 새파일명)]
    used_paths = set()
    used_evidence = set()

    for n in sorted(evidence.keys()):
        ev_name = evidence[n]
        prefix = cfg["format_name"](n)

        for fname, fpath in candidates:
            if fpath in used_paths:
                continue
            fname_no_ext = os.path.splitext(fname)[0]
            ext = os.path.splitext(fname)[1]
            clean, _ = _strip_exhibit_prefix(fname_no_ext)

            if clean and _names_match(clean, ev_name):
                new_name = f"{prefix} {ev_name}{ext}"
                renames.append((fpath, new_name))
                used_paths.add(fpath)
                used_evidence.add(n)
                break

    # ── 2차: 번호 기반 매칭 (1차에서 매칭 안 된 파일) ──
    for fname, fpath in candidates:
        if fpath in used_paths:
            continue
        fname_no_ext = os.path.splitext(fname)[0]
        ext = os.path.splitext(fname)[1]
        clean, file_old_n = _strip_exhibit_prefix(fname_no_ext)

        if file_old_n is None or not clean:
            continue

        new_n = old_to_new.get(file_old_n)
        if new_n and new_n not in used_evidence:
            prefix = cfg["format_name"](new_n)
            ev_name = evidence.get(new_n, clean)
            new_name = f"{prefix} {ev_name}{ext}"
            renames.append((fpath, new_name))
            used_paths.add(fpath)
            used_evidence.add(new_n)

    if not renames:
        print("\n[참고] 증거 번호를 붙일 파일을 찾지 못했습니다.")
        return

    # 자료 폴더 생성: {서면이름}_{입증방법|참고자료}
    data_folder_name = f"{input_basename}{folder_suffix}"
    os.makedirs(data_folder, exist_ok=True)

    print(f"\n{'=' * 60}")
    print(f"[증거 파일 → {data_folder_name} 폴더]")
    print(f"{'-' * 60}")
    for old_path, new_name in renames:
        old_display = os.path.basename(old_path)
        # 이미 자료 폴더 안에 있는 파일은 경로 표시
        src_dir = os.path.dirname(old_path)
        if src_dir in (data_folder, legacy_folder):
            old_display = f"{os.path.basename(src_dir)}/{old_display}"
        print(f"  {old_display}")
        print(f"    → {data_folder_name}/{new_name}")
    print(f"{'=' * 60}")

    # 2단계 이동+이름 변경 (충돌 방지: 먼저 임시 이름 → 최종 이름)
    # 1단계: 모든 파일을 자료 폴더 내 임시 이름으로 이동
    temp_renames = []
    for src_path, new_name in renames:
        temp_name = f"__exhibit_temp__{os.path.basename(src_path)}"
        temp_path = os.path.join(data_folder, temp_name)
        shutil.move(src_path, temp_path)
        temp_renames.append((temp_name, new_name))

    # 2단계: 임시 이름을 최종 이름으로 변경
    renamed_count = 0
    for temp_name, new_name in temp_renames:
        temp_path = os.path.join(data_folder, temp_name)
        new_path = os.path.join(data_folder, new_name)
        os.rename(temp_path, new_path)
        renamed_count += 1

    print(f"\n[완료] {renamed_count}개 증거 파일을 {data_folder_name} 폴더로 이동 완료")


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

    # 전처리: 번호 없는 증거(갑 제호증, 을 제호증)에 임시 번호 부여
    preprocess_all_unnumbered(paragraphs)

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

    # 같은 폴더의 증거 파일명에 증거 번호 자동 부여
    rename_evidence_files(input_path, output_path)


def preview_only(input_path):
    """HWPX를 수정하지 않고 재매핑 결과만 출력."""
    with zipfile.ZipFile(input_path, "r") as z:
        xml_bytes = z.read("Contents/section0.xml")
        header_bytes = z.read("Contents/header.xml")

    root = ET.fromstring(xml_bytes)
    header_root = ET.fromstring(header_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    # 전처리: 번호 없는 증거에 임시 번호 부여 (미리보기용, 원본 미변경)
    preprocess_all_unnumbered(paragraphs)

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
                if not name.strip():
                    continue
                print(f"[경고] {cfg['format_name'](old_n)}이(가) 마무리 목록에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    # 동일 이름 증거 병합
    order, registry, merge_map = deduplicate_registry(order, registry, cfg)

    mapping = build_mapping(order)
    for later_n, first_n in merge_map.items():
        mapping[later_n] = mapping[first_n]

    print("=" * 60)
    print(f"[미리보기] {cfg['label']} 번호 재매핑 결과")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        arrow = "→" if old_n != new_n else "="
        print(f"  {cfg['format_name'](old_n)} {arrow} {cfg['format_name'](new_n)}  {registry[old_n][:50]}")
    if merge_map:
        print("-" * 60)
        for later_n, first_n in merge_map.items():
            print(f"  {cfg['format_name'](later_n)} → {cfg['format_name'](mapping[later_n])}  (동일 증거 병합)")
    print("-" * 60)
    print(f"\n[확인] 재생성될 마무리 목록:")
    for new_n, old_n in enumerate(order, start=1):
        line = cfg['format_line'](new_n, registry[old_n])
        if cfg.get("needs_seq_prefix", True):
            line = f"{new_n}. {line}"
        print(f"  {line}")
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
        line = cfg['format_line'](new_n, registry[old_n])
        if cfg.get("needs_seq_prefix", True):
            line = f"{new_n}. {line}"
        print(f"  {line}")
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
