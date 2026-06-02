"""
exhibit_renumber_defendant.py
==============================
준비서면 HWPX 파일에서 '을 제N호증'의 순서를 본문 등장 순서 기준으로 자동 재번호매김합니다.
(피고의 소송대리인용)

사용법:
    python exhibit_renumber_defendant.py input.hwpx output.hwpx

동작:
    1. HWPX에서 section0.xml 파싱
    2. 본문에서 '을 제N호증'이 최초 등장하는 순서대로 새 번호 매핑 생성
       - 이미 등장한 호증이 다시 나오면 기존 번호 유지 (고정)
    3. 입증방법 섹션(입 증 방 법 / 입증방법) 이하의 번호 목록도 재생성
    4. 수정된 XML로 새 HWPX 파일 출력

주의:
    - 입증방법 섹션 탐지: 단락 전체가 키워드 단독(입증방법/소명방법 등) AND 다음 단락이 '1. 을 제N호증' 패턴인 경우만 인식
      → 본문 중간에 '입증방법'이라는 단어가 나와도 오탐하지 않음
    - 입증방법 이전 단락: 을호증 번호 치환만 수행
    - 입증방법 이후 단락: 기존 을호증 목록 삭제 후 재생성된 순서로 대체
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
# 본문/입증방법 공통: "을 제N호증" 또는 "을제N호증"
EXHIBIT_RE = re.compile(r"을\s*제(\d+)호증")

# 입증방법 섹션 헤더 키워드 (공백 제거 후 비교)
PROOF_SECTION_KEYWORDS = {"입증방법", "소명방법", "참고자료", "첨부서류"}

# 입증방법 목록 첫 항목 패턴: "을 제N호증" 또는 "1. 을 제N호증" (번호 유무 모두 허용)
PROOF_LIST_START_RE = re.compile(r"^(?:\d+[\.\s]+)?을\s*제\d+호증")


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


def replace_exhibit_numbers(text, mapping):
    """텍스트 내 '을 제N호증'을 mapping에 따라 치환."""
    def replacer(m):
        old_n = int(m.group(1))
        new_n = mapping.get(old_n, old_n)
        original = m.group(0)
        return re.sub(r"\d+", str(new_n), original, count=1)
    return EXHIBIT_RE.sub(replacer, text)


def parse_exhibit_line(text):
    """입증방법 섹션 한 줄에서 (번호, 을호증번호, 단서이름) 추출.
    예) '1. 을 제1호증  계약서' → (1, 1, '계약서')
        '을 제1호증  계약서' → (None, 1, '계약서')
    """
    m = EXHIBIT_RE.search(text)
    if not m:
        return None
    exhibit_n = int(m.group(1))
    name_part = text[m.end():].strip()
    list_num_m = re.match(r"^(\d+)[.\s]+", text.strip())
    list_num = int(list_num_m.group(1)) if list_num_m else None
    return list_num, exhibit_n, name_part


def build_exhibit_registry(paragraphs):
    """
    본문을 순회하며 을호증의 최초 등장 순서를 기록.
    반환: (order, registry)
      order    : 최초 등장 순서의 old_n 리스트
      registry : { old_n: name }
    """
    registry = {}
    order = []

    for para_el in paragraphs:
        text = get_para_texts(para_el)
        for m in EXHIBIT_RE.finditer(text):
            n = int(m.group(1))
            if n not in registry:
                name_after = text[m.end():].strip()
                name_after = re.sub(r"^\d+\.\s*", "", name_after)
                registry[n] = name_after
                order.append(n)

    return order, registry


def is_auto_numbered_para(para_el, header_root):
    """
    단락의 paraPrIDRef가 <heading type="NUMBER" .../>를 가진 스타일인지 확인.
    """
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


def collect_proof_list_items(paragraphs, proof_idx, header_root):
    """
    입증방법 목록 단락을 순회하여 (para_el, is_auto_num, old_n, name) 목록 반환.
    """
    items = []
    for i in range(proof_idx + 1, len(paragraphs)):
        text = get_para_texts(paragraphs[i]).strip()
        if not text:
            continue
        m = EXHIBIT_RE.search(text)
        if not m:
            break
        old_n = int(m.group(1))
        name = text[m.end():].strip()
        auto_num = is_auto_numbered_para(paragraphs[i], header_root)
        items.append((paragraphs[i], auto_num, old_n, name))
    return items


def build_mapping(order):
    """등장 순서 → 새 번호 매핑 { old_n: new_n }"""
    return {old_n: new_n for new_n, old_n in enumerate(order, start=1)}


def find_proof_section_idx(paragraphs):
    """
    입증방법(소명방법 등) 섹션 헤더 단락의 인덱스를 반환. 없으면 None.

    탐지 조건 (AND):
      1. 단락 전체가 키워드 단독 (공백 제거 후 비교)
      2. 바로 다음 단락(빈 줄 건너뜀)이 '1. 을 제N호증'으로 시작

    → 본문 중간에 '입증방법'이라는 단어가 나와도 오탐하지 않음
    """
    for i, para_el in enumerate(paragraphs):
        text = get_para_texts(para_el)
        cleaned = re.sub(r"\s", "", text.strip())

        if cleaned not in PROOF_SECTION_KEYWORDS:
            continue

        for j in range(i + 1, min(i + 5, len(paragraphs))):
            next_text = get_para_texts(paragraphs[j]).strip()
            if next_text == "":
                continue
            if PROOF_LIST_START_RE.match(next_text):
                return i
            break

    return None


def find_proof_body_range(paragraphs, proof_idx):
    """
    입증방법 섹션 이후에서 을호증 목록이 시작~끝나는 인덱스 범위 반환.
    """
    start = None
    end = None
    for i in range(proof_idx + 1, len(paragraphs)):
        text = get_para_texts(paragraphs[i]).strip()
        if EXHIBIT_RE.search(text):
            if start is None:
                start = i
            end = i
        elif start is not None and text == "":
            continue
        elif start is not None:
            break
    return start, end


def clone_para_with_text(template_para, new_text):
    """template_para의 서식을 유지하면서 텍스트만 교체한 새 단락 반환."""
    new_para = deepcopy(template_para)
    set_para_texts(new_para, new_text)
    return new_para


def renumber_hwpx(input_path, output_path):
    # ── 1. HWPX 압축 해제 및 XML 로드 ──────────────────────────
    shutil.copy2(input_path, output_path)

    with zipfile.ZipFile(input_path, "r") as zin:
        xml_bytes = zin.read("Contents/section0.xml")
        header_bytes = zin.read("Contents/header.xml")
        all_files = zin.namelist()

    header_root = ET.fromstring(header_bytes)

    root = ET.fromstring(xml_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    # ── 2. 입증방법 섹션 위치 탐지 ──────────────────────────────
    proof_idx = find_proof_section_idx(paragraphs)
    if proof_idx is None:
        print("[경고] '입증방법' 섹션을 찾지 못했습니다. 본문 치환만 수행합니다.")
        body_paragraphs = paragraphs
        proof_paragraphs = []
    else:
        body_paragraphs = paragraphs[:proof_idx]
        proof_paragraphs = paragraphs[proof_idx:]

    # ── 3. 본문에서 을호증 등장 순서 수집 ───────────────────────
    order, registry = build_exhibit_registry(body_paragraphs)

    if not order:
        print("을호증을 발견하지 못했습니다.")
        return

    # ── 입증방법 목록 수집 및 본문 미등장 호증 경고 ─────────────
    proof_list_items = []
    if proof_idx is not None:
        proof_list_items = collect_proof_list_items(paragraphs, proof_idx, header_root)
        for _, _, old_n, name in proof_list_items:
            if old_n not in registry:
                print(f"[경고] 을 제{old_n}호증이 입증방법에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    mapping = build_mapping(order)

    print("=" * 60)
    print("[확인] 을호증 번호 재매핑")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        print(f"  을 제{old_n}호증 → 을 제{new_n}호증  ({registry[old_n][:40]})")
    print("=" * 60)

    # ── 4. 본문 을호증 번호 치환 ────────────────────────────────
    for para_el in body_paragraphs:
        original = get_para_texts(para_el)
        replaced = replace_exhibit_numbers(original, mapping)
        if replaced != original:
            set_para_texts(para_el, replaced)

    # ── 5. 입증방법 목록 재생성 ─────────────────────────────────
    if proof_idx is not None and proof_list_items:
        auto_tmpl = next(
            (p for p, is_auto, _, _ in proof_list_items if is_auto),
            proof_list_items[0][0]
        )

        root_children = list(root)
        first_para = proof_list_items[0][0]
        last_para = proof_list_items[-1][0]
        start_pos = root_children.index(first_para)
        end_pos = root_children.index(last_para)

        new_lines = []
        for new_n, old_n in enumerate(order, start=1):
            name = registry[old_n]
            new_lines.append(f"을 제{new_n}호증  {name}")

        for i in range(end_pos, start_pos - 1, -1):
            root.remove(root_children[i])

        for line in reversed(new_lines):
            new_para = clone_para_with_text(auto_tmpl, line)
            root.insert(start_pos, new_para)

        print(f"\n[완료] 입증방법 목록 재생성 완료 ({len(new_lines)}개 항목)")

    # ── 6. 수정된 XML → HWPX 파일 저장 ─────────────────────────
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
    """HWPX를 수정하지 않고 재매핑 결과만 출력 (--preview 옵션)."""
    with zipfile.ZipFile(input_path, "r") as z:
        xml_bytes = z.read("Contents/section0.xml")
        header_bytes = z.read("Contents/header.xml")

    root = ET.fromstring(xml_bytes)
    header_root = ET.fromstring(header_bytes)
    paragraphs = root.findall(f"{{{HP}}}p")

    proof_idx = find_proof_section_idx(paragraphs)
    body_paragraphs = paragraphs[:proof_idx] if proof_idx else paragraphs
    order, registry = build_exhibit_registry(body_paragraphs)

    if not order:
        print("을호증을 발견하지 못했습니다.")
        return

    if proof_idx is not None:
        proof_list_items = collect_proof_list_items(paragraphs, proof_idx, header_root)
        for _, _, old_n, name in proof_list_items:
            if old_n not in registry:
                print(f"[경고] 을 제{old_n}호증이 입증방법에는 있으나, 실제 본문에는 없습니다. ({name[:40]})")
                registry[old_n] = name
                order.append(old_n)

    mapping = build_mapping(order)

    print("=" * 60)
    print("[미리보기] 미리보기: 을호증 번호 재매핑 결과")
    print("-" * 60)
    for old_n in order:
        new_n = mapping[old_n]
        arrow = "→" if old_n != new_n else "="
        print(f"  을 제{old_n:2d}호증 {arrow} 을 제{new_n:2d}호증  {registry[old_n][:50]}")
    print("-" * 60)
    print("\n[확인] 재생성될 입증방법 목록:")
    for new_n, old_n in enumerate(order, start=1):
        print(f"  {new_n}. 을 제{new_n}호증  {registry[old_n]}")
    print("=" * 60)


# ── CLI 진입점 ───────────────────────────────────────────────────
if __name__ == "__main__":
    args = sys.argv[1:]

    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    if args[0] == "--preview":
        if len(args) < 2:
            print("사용법: python exhibit_renumber_defendant.py --preview input.hwpx")
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
        print("사용법: python exhibit_renumber_defendant.py input.hwpx")
        print("        python exhibit_renumber_defendant.py input.hwpx output.hwpx")
        print("        python exhibit_renumber_defendant.py --preview input.hwpx")
        sys.exit(1)
