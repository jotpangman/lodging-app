import io
import os
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests
import streamlit as st
from dotenv import load_dotenv

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)


# =========================================================
# 기본 설정
# =========================================================

load_dotenv()

def get_secret(name):
    """로컬에서는 .env, 배포 환경에서는 Streamlit Secrets를 읽습니다."""
    local_value = os.getenv(name, "").strip()

    if local_value:
        return local_value

    try:
        return str(st.secrets[name]).strip()
    except (KeyError, FileNotFoundError):
        return ""


NAVER_GEOCODING_CLIENT_ID = get_secret(
    "NAVER_GEOCODING_CLIENT_ID"
)

NAVER_GEOCODING_CLIENT_SECRET = get_secret(
    "NAVER_GEOCODING_CLIENT_SECRET"
)

NAVER_DIRECTIONS_CLIENT_ID = get_secret(
    "NAVER_DIRECTIONS_CLIENT_ID"
)

NAVER_DIRECTIONS_CLIENT_SECRET = get_secret(
    "NAVER_DIRECTIONS_CLIENT_SECRET"
)

# 현재 사용 중인 NAVER Cloud Maps 환경에 따라 URL이 다를 수 있으므로
# 필요하면 이 값만 변경하면 됩니다.
GEOCODING_URL = (
    "https://maps.apigw.ntruss.com/"
    "map-geocode/v2/geocode"
)

DIRECTIONS_URL = (
    "https://maps.apigw.ntruss.com/"
    "map-direction/v1/driving"
)

DISTANCE_STANDARD_KM = 30
TRANSIT_STANDARD_MINUTES = 90


# =========================================================
# 공통 함수
# =========================================================

def get_geocoding_headers():
    """Geocoding API 인증 헤더를 반환합니다."""
    if (
        not NAVER_GEOCODING_CLIENT_ID
        or not NAVER_GEOCODING_CLIENT_SECRET
    ):
        raise ValueError(
            ".env 파일에 Geocoding 인증정보를 입력해 주세요."
        )

    return {
        "X-NCP-APIGW-API-KEY-ID": NAVER_GEOCODING_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": NAVER_GEOCODING_CLIENT_SECRET,
    }


def get_directions_headers():
    """Directions 5 API 인증 헤더를 반환합니다."""
    if (
        not NAVER_DIRECTIONS_CLIENT_ID
        or not NAVER_DIRECTIONS_CLIENT_SECRET
    ):
        raise ValueError(
            ".env 파일에 Directions 5 인증정보를 입력해 주세요."
        )

    return {
        "X-NCP-APIGW-API-KEY-ID": NAVER_DIRECTIONS_CLIENT_ID,
        "X-NCP-APIGW-API-KEY": NAVER_DIRECTIONS_CLIENT_SECRET,
    }


def geocode_candidates(address):
    """주소 검색 결과를 사용자가 선택할 수 있도록 후보 목록으로 반환합니다."""
    address = address.strip()

    if not address:
        raise ValueError("주소가 입력되지 않았습니다.")

    response = requests.get(
        GEOCODING_URL,
        headers=get_geocoding_headers(),
        params={"query": address, "count": 10},
        timeout=20,
    )
    response.raise_for_status()
    addresses = response.json().get("addresses", [])

    if not addresses:
        raise ValueError(
            f"주소를 찾지 못했습니다: {address}\n"
            "시·도, 시·군·구, 도로명과 건물번호까지 입력해 주세요."
        )

    candidates = []
    seen = set()

    for item in addresses:
        road_address = item.get("roadAddress", "").strip()
        jibun_address = item.get("jibunAddress", "").strip()
        label = road_address or jibun_address

        if not label or label in seen:
            continue
        seen.add(label)

        sido = ""
        sigungu = ""
        for element in item.get("addressElements", []):
            types = element.get("types", [])
            if "SIDO" in types:
                sido = element.get("longName", "")
            if "SIGUGUN" in types:
                sigungu = element.get("longName", "")

        if not sido or not sigungu:
            parts = label.split()
            sido = sido or (parts[0] if parts else "")
            sigungu = sigungu or (parts[1] if len(parts) > 1 else "")

        display = label
        if road_address and jibun_address and road_address != jibun_address:
            display = f"{road_address}  (지번: {jibun_address})"

        candidates.append({
            "label": display,
            "confirmed_address": label,
            "x": item["x"],
            "y": item["y"],
            "road_address": road_address,
            "jibun_address": jibun_address,
            "sido": sido,
            "sigungu": sigungu,
        })

    if not candidates:
        raise ValueError("사용할 수 있는 주소 검색 결과가 없습니다.")

    return candidates


def is_different_district(home_geo, site_geo):
    """자택과 현장의 시·군·구가 다른지 자동으로 판정합니다."""
    home_region = (home_geo.get("sido", ""), home_geo.get("sigungu", ""))
    site_region = (site_geo.get("sido", ""), site_geo.get("sigungu", ""))
    return home_region != site_region

def build_naver_transit_url(home_geo, site_geo):
    """확정된 출발지와 도착지를 넣은 네이버 대중교통 길찾기 URL을 만듭니다."""
    start_name = quote(home_geo["confirmed_address"], safe="")
    goal_name = quote(site_geo["confirmed_address"], safe="")

    start = f"{home_geo['x']},{home_geo['y']},{start_name},PLACE_POI"
    goal = f"{site_geo['x']},{site_geo['y']},{goal_name},PLACE_POI"

    return f"https://map.naver.com/p/directions/{start}/{goal}/-/transit"

def get_driving_route(start_coordinate, goal_coordinate):
    """
    출발지와 도착지 좌표를 이용해 자동차 거리와 시간을 조회합니다.

    start_coordinate, goal_coordinate 형식:
    경도,위도
    """
    response = requests.get(
        DIRECTIONS_URL,
        headers=get_directions_headers(),
        params={
            "start": start_coordinate,
            "goal": goal_coordinate,
            "option": "traoptimal",
        },
        timeout=30,
    )

    response.raise_for_status()
    data = response.json()

    if data.get("code") != 0:
        message = data.get("message", "경로 조회 실패")
        raise ValueError(f"NAVER 길찾기 오류: {message}")

    route_list = data.get("route", {}).get("traoptimal", [])

    if not route_list:
        raise ValueError(
            "자동차 경로를 찾지 못했습니다. "
            "입력한 주소 또는 좌표를 확인해 주세요."
        )

    summary = route_list[0].get("summary", {})

    distance_meter = summary.get("distance")
    duration_millisecond = summary.get("duration")

    if distance_meter is None or duration_millisecond is None:
        raise ValueError("길찾기 응답에서 거리 또는 시간을 찾지 못했습니다.")

    distance_km = round(distance_meter / 1000, 1)
    duration_minutes = round(duration_millisecond / 60000)

    return {
        "distance_km": distance_km,
        "duration_minutes": duration_minutes,
    }


def calculate_months(start_date, end_date):
    """두 날짜 사이의 사용 개월 수를 대략적으로 계산합니다."""
    if end_date < start_date:
        raise ValueError("종료일은 시작일보다 빠를 수 없습니다.")

    months = (
        (end_date.year - start_date.year) * 12
        + end_date.month
        - start_date.month
    )

    if end_date.day >= start_date.day:
        months += 1

    return max(months, 1)


def format_date_short(value):
    """날짜를 ’26.10.13 형식으로 변환합니다."""
    return f"’{value.strftime('%y.%m.%d')}"


def format_minutes(total_minutes):
    """분을 '2시간 15분' 형식으로 바꿉니다."""
    hours = total_minutes // 60
    minutes = total_minutes % 60

    if hours and minutes:
        return f"{hours}시간 {minutes}분"
    if hours:
        return f"{hours}시간"
    return f"{minutes}분"


def evaluate_support(
    distance_km,
    transit_minutes,
    different_district,
):
    """숙소지원 기준 충족 여부를 판정합니다."""
    distance_pass = distance_km >= DISTANCE_STANDARD_KM
    transit_pass = transit_minutes >= TRANSIT_STANDARD_MINUTES
    location_pass = different_district

    # 현재 양식상 세 조건을 각각 표시합니다.
    # 최종 판정 기준은 사내 규정에 맞게 변경할 수 있습니다.
    all_pass = distance_pass and transit_pass and location_pass

    if all_pass:
        final_result = "숙소지원 대상"
    else:
        final_result = "추가 검토 필요"

    reasons = []

    reasons.append(
        f"현장과 자택 간 자동차 이동거리 "
        f"약 {distance_km:,.1f}km "
        f"({'30km 이상 충족' if distance_pass else '30km 이상 미충족'})"
    )

    reasons.append(
        f"대중교통 소요시간 "
        f"{format_minutes(transit_minutes)} "
        f"({'1시간 30분 이상 충족' if transit_pass else '1시간 30분 이상 미충족'})"
    )

    reasons.append(
        "현장 행정구역 외 위치 기준 "
        f"({'충족' if location_pass else '미충족'})"
    )

    return {
        "distance_pass": distance_pass,
        "transit_pass": transit_pass,
        "location_pass": location_pass,
        "final_result": final_result,
        "reasons": reasons,
    }


# =========================================================
# PDF 관련 함수
# =========================================================

def register_korean_font():
    """프로젝트에 포함된 한글 글꼴을 등록합니다."""
    base_dir = Path(__file__).resolve().parent
    font_path = base_dir / "NotoSansKR.ttf"

    if not font_path.exists():
        raise FileNotFoundError(
            "NotoSansKR.ttf 파일을 app.py와 같은 폴더에 넣어 주세요."
        )

    pdfmetrics.registerFont(
        TTFont("KoreanFont", str(font_path))
    )

    return "KoreanFont"


def make_paragraph(text, style):
    """개행 문자가 포함된 텍스트를 PDF 문단으로 변환합니다."""
    safe_text = str(text).replace("\n", "<br/>")
    return Paragraph(safe_text, style)


def build_pdf(data):
    """품의서 본문 PDF를 생성합니다."""
    font_name = register_korean_font()

    pdf_buffer = io.BytesIO()

    document = BaseDocTemplate(
        pdf_buffer,
        pagesize=A4,
        leftMargin=6 * mm,
        rightMargin=6 * mm,
        topMargin=7 * mm,
        bottomMargin=7 * mm,
        title="숙소 사용 요청 품의서",
        author="직원숙소 품의서 자동작성 시스템",
    )

    frame = Frame(
        document.leftMargin,
        document.bottomMargin,
        document.width,
        document.height,
        id="main_frame",
    )

    document.addPageTemplates(
        [PageTemplate(id="main", frames=[frame])]
    )

    normal_style = ParagraphStyle(
        name="NormalKorean",
        fontName=font_name,
        fontSize=9.5,
        leading=14,
        wordWrap="CJK",
    )

    center_style = ParagraphStyle(
        name="CenterKorean",
        parent=normal_style,
        alignment=TA_CENTER,
    )

    title_style = ParagraphStyle(
        name="TitleKorean",
        parent=center_style,
        fontSize=24,
        leading=29,
        spaceAfter=8,
    )

    section_style = ParagraphStyle(
        name="SectionKorean",
        parent=normal_style,
        fontSize=9,
        leading=13,
    )

    story = []

    # -----------------------------------------------------
    # 제목
    # -----------------------------------------------------

    story.append(Paragraph("<u>품 의 서</u>", title_style))
    story.append(Spacer(1, 3 * mm))

    title_table = Table(
        [
            [
                make_paragraph("<b>제 목</b>", center_style),
                make_paragraph(
                    "현장 직원숙소 사용 요청의 건",
                    normal_style,
                ),
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
        rowHeights=[14 * mm],
    )

    title_table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (0, 0), colors.HexColor("#F2F2F2")),
            ]
        )
    )

    story.append(title_table)
    story.append(Spacer(1, 3 * mm))

    # -----------------------------------------------------
    # 1. 사용목적
    # -----------------------------------------------------

    purpose_text = (
        "1) 당 현장과 자택이 원거리로 당사 현장 숙소지원 기준에 "
        "적용되어 직원숙소를 사용하고자 함"
    )

    purpose_table = Table(
        [
            [
                make_paragraph("<b>1. 사용목적</b>", center_style),
                make_paragraph(purpose_text, section_style),
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
        rowHeights=[16 * mm],
    )

    purpose_table.setStyle(common_section_style())
    story.append(purpose_table)

    # -----------------------------------------------------
    # 2. 대상인원
    # -----------------------------------------------------

    applicant_text = (
        f"1) 대상자: {data['applicant_name']}"
        f"(직무: {data['job_title']})"
    )

    applicant_table = Table(
        [
            [
                make_paragraph("<b>2. 대상인원</b>", center_style),
                make_paragraph(applicant_text, section_style),
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
        rowHeights=[16 * mm],
    )

    applicant_table.setStyle(common_section_style())
    story.append(applicant_table)

    # -----------------------------------------------------
    # 3. 사용기준
    # -----------------------------------------------------

    result = data["result"]

    location_status = "대상" if result["location_pass"] else "미충족"
    distance_status = "대상" if result["distance_pass"] else "미충족"
    transit_status = "대상" if result["transit_pass"] else "미충족"

    criteria_inner = Table(
        [
            [
                make_paragraph("", center_style),
                make_paragraph("<b>기준(편도)</b>", center_style),
                make_paragraph("<b>신청자</b>", center_style),
                make_paragraph("<b>비고</b>", center_style),
            ],
            [
                make_paragraph("위치", center_style),
                make_paragraph("현장~자택", center_style),
                make_paragraph(
                    f"현장({data['site_short_name']}) ~ "
                    f"자택({data['home_short_name']})",
                    center_style,
                ),
                make_paragraph(
                    "행정구역 외",
                    center_style,
                ),
            ],
            [
                make_paragraph("거리", center_style),
                make_paragraph("30km 이상", center_style),
                make_paragraph(
                    f"약 {data['distance_km']:,.1f}km",
                    center_style,
                ),
                make_paragraph(distance_status, center_style),
            ],
            [
                make_paragraph("시간", center_style),
                make_paragraph(
                    "대중교통 1시간 30분 이상",
                    center_style,
                ),
                make_paragraph(
                    format_minutes(data["transit_minutes"]),
                    center_style,
                ),
                make_paragraph(transit_status, center_style),
            ],
        ],
        colWidths=[16 * mm, 43 * mm, 65 * mm, 25 * mm],
        rowHeights=[
            10 * mm,
            12 * mm,
            10 * mm,
            12 * mm,
        ],
    )

    criteria_inner.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")),
            ]
        )
    )

    criteria_content = [
        make_paragraph("1) 숙소 대상여부", section_style),
        Spacer(1, 2 * mm),
        criteria_inner,
        Spacer(1, 2 * mm),
        make_paragraph(
            "※ 검색: 네이버 길찾기 – 최적 경로순 기준",
            section_style,
        ),
    ]

    criteria_wrapper = Table(
        [
            [
                make_paragraph("<b>3. 사용기준</b>", center_style),
                criteria_content,
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
    )

    criteria_wrapper.setStyle(common_section_style())
    story.append(criteria_wrapper)

    # -----------------------------------------------------
    # 4. 사용기간
    # -----------------------------------------------------

    period_inner = Table(
        [
            [
                make_paragraph("<b>공사기간</b>", center_style),
                make_paragraph("<b>사용기간</b>", center_style),
                make_paragraph("<b>비고</b>", center_style),
            ],
            [
                make_paragraph(
                    f"{format_date_short(data['construction_start'])} ~ "
                    f"{format_date_short(data['construction_end'])}"
                    f"({data['construction_months']}개월)",
                    center_style,
                ),
                make_paragraph(
                    f"{format_date_short(data['use_start'])} ~ "
                    f"{format_date_short(data['use_end'])}"
                    f"({data['use_months']}개월)",
                    center_style,
                ),
                make_paragraph(
                    f"발령일: {format_date_short(data['appointment_date'])}",
                    center_style,
                ),
            ],
        ],
        colWidths=[54 * mm, 54 * mm, 41 * mm],
        rowHeights=[10 * mm, 14 * mm],
    )

    period_inner.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.6, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#F2F2F2")),
            ]
        )
    )

    period_content = [
        make_paragraph("1) 숙소 사용기간", section_style),
        Spacer(1, 2 * mm),
        period_inner,
    ]

    period_wrapper = Table(
        [
            [
                make_paragraph("<b>4. 사용기간</b>", center_style),
                period_content,
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
    )

    period_wrapper.setStyle(common_section_style())
    story.append(period_wrapper)

    # -----------------------------------------------------
    # 5. 교통수단
    # -----------------------------------------------------

    transport_table = Table(
        [
            [
                make_paragraph("<b>5. 교통수단</b>", center_style),
                make_paragraph(
                    "1) 주요 교통수단: 자동차 및 대중교통",
                    section_style,
                ),
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
        rowHeights=[15 * mm],
    )

    transport_table.setStyle(common_section_style())
    story.append(transport_table)

    # -----------------------------------------------------
    # 6. 특기사항
    # -----------------------------------------------------

    special_text = (
        f"1) [{data['lodging_type']}] 직원숙소 사용 시 파손, 분실 등에 "
        "대한 변제는 사용자 개인이 부담한다.\n"
        "2) [원룸숙소] 직원숙소 보증금/월세 회사부담 외의 비용은 "
        "사용자 개인이 부담한다.\n"
        f"3) 현장 발령일({format_date_short(data['appointment_date'])}) "
        "기준으로 귀가여비를 소급하여 적용한다.\n"
        "4) 현장 숙소사용 기간은 준공일 이후, 전출 발령일까지 "
        "추가사용 할 수 있다."
    )

    special_table = Table(
        [
            [
                make_paragraph("<b>6. 특기사항</b>", center_style),
                make_paragraph(special_text, section_style),
            ]
        ],
        colWidths=[30 * mm, 156 * mm],
        rowHeights=[37 * mm],
    )

    special_table.setStyle(common_section_style())
    story.append(special_table)

    # -----------------------------------------------------
    # 별첨 및 결재라인
    # -----------------------------------------------------

    attachment_text = (
        "별첨:<br/>"
        "① 네이버 거리 지도(자동차 거리)<br/>"
        "② 네이버 거리 지도(대중교통 시간)"
    )

    attachment_table = Table(
        [[make_paragraph(attachment_text, section_style)]],
        colWidths=[186 * mm],
        rowHeights=[25 * mm],
    )

    attachment_table.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.8, colors.black),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )

    story.append(attachment_table)
    story.append(Spacer(1, 3 * mm))

    approval_text = (
        "결재라인: 기안(신청자) – 조정(현장관리) – 결재(현장소장)"
    )

    story.append(make_paragraph(approval_text, section_style))

    document.build(story)
    pdf_buffer.seek(0)

    return pdf_buffer.getvalue()


def common_section_style():
    """품의서 각 구역에 적용할 공통 표 스타일입니다."""
    return TableStyle(
        [
            ("GRID", (0, 0), (-1, -1), 0.8, colors.black),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F2F2F2")),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]
    )


# =========================================================
# Streamlit 화면
# =========================================================

st.set_page_config(
    page_title="숙소 사용 요청 품의서 자동작성",
    page_icon="🏠",
    layout="wide",
)

st.title("🏠 숙소 사용 요청 품의서 자동작성")
st.caption(
    "주소 후보를 확인한 뒤 자동차 이동거리와 시간을 조회하고, "
    "네이버 지도에서 확인한 대중교통 시간을 반영해 품의서 PDF를 생성합니다."
)

if (
    not NAVER_GEOCODING_CLIENT_ID
    or not NAVER_GEOCODING_CLIENT_SECRET
    or not NAVER_DIRECTIONS_CLIENT_ID
    or not NAVER_DIRECTIONS_CLIENT_SECRET
):
    st.warning(
        "Geocoding 및 Directions 5 인증정보를 모두 입력해 주세요."
    )

st.subheader("1. 신청자 정보")
applicant_col1, applicant_col2 = st.columns(2)
with applicant_col1:
    applicant_name = st.text_input("대상자", placeholder="예: 홍길동")
with applicant_col2:
    job_title = st.text_input("직무", placeholder="예: 현장관리")

st.subheader("2. 주소 검색 및 선택")
address_col1, address_col2 = st.columns(2)
with address_col1:
    home_address_query = st.text_input(
        "자택 주소 검색어",
        placeholder="시·도, 시·군·구, 도로명과 건물번호까지 입력",
    )
    home_short_name = st.text_input(
        "품의서 표시용 자택 지역명",
        placeholder="예: 서울 서초구",
    )
with address_col2:
    site_address_query = st.text_input(
        "현장 주소 검색어",
        placeholder="시·도, 시·군·구, 도로명과 건물번호까지 입력",
    )
    site_short_name = st.text_input(
        "품의서 표시용 현장명",
        placeholder="예: 음성 현장",
    )

if st.button("🔎 주소 후보 검색", type="primary", use_container_width=True):
    try:
        with st.spinner("네이버에서 주소 후보를 검색하고 있습니다."):
            st.session_state["home_candidates"] = geocode_candidates(
                home_address_query
            )
            st.session_state["site_candidates"] = geocode_candidates(
                site_address_query
            )
    except requests.HTTPError as error:
        st.error(f"주소 검색 API 오류: HTTP {error.response.status_code}")
    except Exception as error:
        st.error(str(error))

home_candidates = st.session_state.get("home_candidates", [])
site_candidates = st.session_state.get("site_candidates", [])

selected_home = None
selected_site = None

if home_candidates and site_candidates:
    select_col1, select_col2 = st.columns(2)
    with select_col1:
        home_index = st.selectbox(
            "자택 주소 검색 결과",
            range(len(home_candidates)),
            format_func=lambda i: home_candidates[i]["label"],
        )
        selected_home = home_candidates[home_index]
    with select_col2:
        site_index = st.selectbox(
            "현장 주소 검색 결과",
            range(len(site_candidates)),
            format_func=lambda i: site_candidates[i]["label"],
        )
        selected_site = site_candidates[site_index]

    st.info(
        f"확정 자택주소: {selected_home['confirmed_address']}\n\n"
        f"확정 현장주소: {selected_site['confirmed_address']}"
    )

    different_district = is_different_district(selected_home, selected_site)
    st.write(
        "행정구역 판정: "
        + ("**현장 행정구역 외**" if different_district else "**동일 행정구역**")
    )

    naver_transit_url = build_naver_transit_url(
        selected_home,
        selected_site,
    )
    st.link_button(
        "🚌 선택한 주소로 네이버 대중교통 검색",
        naver_transit_url,
        use_container_width=True,
    )
    st.caption(
        "선택한 자택·현장 주소가 네이버 길찾기의 출발지·도착지에 입력된 상태로 열립니다. "
        "네이버 지도에서 평일 오전 05:00 출발 기준을 선택해 시간을 확인하세요."
    )
else:
    different_district = False

st.subheader("3. 대중교통 소요시간")
transit_col1, transit_col2 = st.columns(2)
with transit_col1:
    transit_hours = st.number_input(
        "대중교통 시간",
        min_value=0,
        max_value=24,
        value=1,
        step=1,
    )
with transit_col2:
    transit_minutes_input = st.number_input(
        "대중교통 분",
        min_value=0,
        max_value=59,
        value=30,
        step=1,
    )
st.caption("네이버 길찾기 평일 오전 05:00 출발, 최적 경로순 기준")

st.subheader("4. 기간 정보")
date_col1, date_col2, date_col3 = st.columns(3)
with date_col1:
    construction_start = st.date_input("공사 시작일", value=date.today())
    use_start = st.date_input("숙소 사용 시작일", value=date.today())
with date_col2:
    construction_end = st.date_input("공사 종료일", value=date.today())
    use_end = st.date_input("숙소 사용 종료일", value=date.today())
with date_col3:
    appointment_date = st.date_input("현장 발령일", value=date.today())
    lodging_type = st.selectbox(
        "숙소 유형",
        ["공용/원룸숙소", "공용숙소", "원룸숙소"],
    )

submitted = st.button(
    "거리 조회 및 품의서 생성",
    type="primary",
    use_container_width=True,
)


# =========================================================
# 실행 및 결과 표시
# =========================================================

if submitted:
    try:
        if not selected_home or not selected_site:
            raise ValueError("먼저 주소 후보를 검색한 뒤 정확한 주소를 선택해 주세요.")

        required_fields = {
            "대상자": applicant_name,
            "직무": job_title,
            "자택 지역명": home_short_name,
            "현장명": site_short_name,
        }
        missing_fields = [
            name for name, value in required_fields.items()
            if not value.strip()
        ]
        if missing_fields:
            raise ValueError(
                "다음 항목을 입력해 주세요: " + ", ".join(missing_fields)
            )

        construction_months = calculate_months(
            construction_start, construction_end
        )
        use_months = calculate_months(use_start, use_end)

        home_coordinate = f"{selected_home['x']},{selected_home['y']}"
        site_coordinate = f"{selected_site['x']},{selected_site['y']}"

        with st.spinner("자동차 최적 경로를 조회하고 있습니다."):
            route = get_driving_route(home_coordinate, site_coordinate)

        transit_total_minutes = (
            int(transit_hours) * 60 + int(transit_minutes_input)
        )
        result = evaluate_support(
            distance_km=route["distance_km"],
            transit_minutes=transit_total_minutes,
            different_district=different_district,
        )

        result_col1, result_col2, result_col3 = st.columns(3)
        with result_col1:
            st.metric("자동차 거리", f"{route['distance_km']:,.1f}km")
        with result_col2:
            st.metric(
                "자동차 예상시간",
                format_minutes(route["duration_minutes"]),
            )
        with result_col3:
            st.metric("최종 검토결과", result["final_result"])

        if result["final_result"] == "숙소지원 대상":
            st.success("숙소지원 기준을 충족했습니다.")
        else:
            st.warning("일부 숙소지원 기준을 충족하지 않아 추가 검토가 필요합니다.")

        st.markdown("#### 판단 근거")
        for reason in result["reasons"]:
            st.write(f"- {reason}")

        data = {
            "applicant_name": applicant_name.strip(),
            "job_title": job_title.strip(),
            "home_address": selected_home["confirmed_address"],
            "site_address": selected_site["confirmed_address"],
            "home_short_name": home_short_name.strip(),
            "site_short_name": site_short_name.strip(),
            "construction_start": construction_start,
            "construction_end": construction_end,
            "construction_months": construction_months,
            "use_start": use_start,
            "use_end": use_end,
            "use_months": use_months,
            "appointment_date": appointment_date,
            "lodging_type": lodging_type,
            "distance_km": route["distance_km"],
            "driving_minutes": route["duration_minutes"],
            "transit_minutes": transit_total_minutes,
            "result": result,
        }

        pdf_bytes = build_pdf(data)
        safe_name = applicant_name.strip().replace(" ", "_")
        file_name = f"현장_직원숙소_사용요청_{safe_name}.pdf"

        st.download_button(
            label="📄 품의서 PDF 다운로드",
            data=pdf_bytes,
            file_name=file_name,
            mime="application/pdf",
            type="primary",
            use_container_width=True,
        )

    except requests.HTTPError as error:
        st.error(
            "NAVER API 호출 중 오류가 발생했습니다. "
            f"HTTP 상태코드: {error.response.status_code}"
        )
        with st.expander("오류 상세"):
            st.code(str(error))
    except Exception as error:
        st.error(str(error))
