# -*- coding: utf-8 -*-
"""
floorplan_app.py
------------------
전문가 IFC와 AI 생성 IFC를 업로드해 층별 평면도를 좌/우로 비교하고,
평면도 위에서 공간(Space)을 클릭하면 해당 공간에 접한 구조재 개수(내/외벽 구분 포함)와
유형별 면적을 보여주는 Streamlit 앱.

실행:
    streamlit run floorplan_app.py

요구사항: streamlit>=1.35 (st.plotly_chart의 on_select 기능 필요), plotly, shapely,
ifcopenshell, numpy, pandas, openpyxl (ifc_to_excel.py 의존성)
"""
import os
import tempfile

import streamlit as st
import plotly.graph_objects as go

import floorplan_core as fc

st.set_page_config(layout='wide', page_title='IFC 평면도 비교')
st.title('IFC 평면도 비교 (전문가 vs AI)')

MIN_STREAMLIT_VERSION = (1, 35)


def _check_streamlit_version():
    try:
        parts = tuple(int(x) for x in st.__version__.split('.')[:2])
        if parts < MIN_STREAMLIT_VERSION:
            st.warning(
                f"현재 Streamlit 버전 {st.__version__}. 평면도 클릭 선택 기능은 "
                f"Streamlit {'.'.join(map(str, MIN_STREAMLIT_VERSION))} 이상이 필요합니다. "
                f"`pip install --upgrade streamlit`을 권장합니다."
            )
    except Exception:
        pass


_check_streamlit_version()


def _extract_customdata_guid(point):
    """selection point dict에서 customdata(공간 GlobalId) 안전하게 추출."""
    cd = point.get('customdata')
    if cd is None:
        return None
    if isinstance(cd, (list, tuple)):
        return cd[0] if cd else None
    return cd


@st.cache_resource(show_spinner='IFC 파일 파싱 중... (지붕/천장 면적 등 지오메트리 계산 포함, 수 초~수십 초 소요될 수 있음)')
def _load_ifc_cached(file_bytes, filename):
    """업로드된 IFC를 임시파일로 저장 후 파싱. st.cache_resource로 파일당 1회만 실행."""
    suffix = os.path.splitext(filename)[1] or '.ifc'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(file_bytes)
        path = tmp.name
    return fc.load_ifc(path)


@st.cache_resource(show_spinner='해당 층 도면 지오메트리 계산 중...')
def _build_plan_cached(_storeys, storey_name, cache_tag):
    """층별 평면 지오메트리 캐싱.
    _storeys: 언더스코어 접두사라 Streamlit이 해싱을 시도하지 않음
    (ifcopenshell 엔티티가 섞여있어 해싱 불가/불안정하므로 캐시 키 계산에서 제외).
    storey_name, cache_tag: 둘 다 일반 문자열이라 해시 가능 -> 실제 캐시 구분에 사용됨.
    cache_tag로 좌/우 파일을 구분해 서로 다른 파일의 같은 층 이름이 캐시를 덮어쓰지 않게 한다."""
    storey = next(s for s in _storeys if s['Name'] == storey_name)
    return fc.build_storey_plan_data(storey)


def _render_side(label, data, storey_name, session_prefix):
    """한쪽(전문가 또는 AI) 평면도 + 클릭 정보 패널 렌더링."""
    st.subheader(label)
    if storey_name is None:
        st.info('이 층에 대응하는 층을 찾지 못했습니다 (층 매핑 없음).')
        return

    plan = _build_plan_cached(data['storeys'], storey_name, session_prefix)

    st.caption(f"공간 {len(plan['spaces'])}개 · 구조요소 {len(plan['structural'])}개  (층: {storey_name})")

    selected_key = f'{session_prefix}_selected_guid'
    selected_guid = st.session_state.get(selected_key)

    # 선택된 공간이 있으면 상세정보를 먼저 계산해 하이라이트/설비 마커에 반영
    detail = None
    sp_entry = None
    if selected_guid:
        sp_entry = next((s for s in plan['spaces'] if s['guid'] == selected_guid), None)
        if sp_entry is not None:
            detail = fc.build_space_detail(data['ifc_file'], data['wall_classification'], sp_entry['entity'])

    equipment_entities = None
    highlight_map = None
    if detail is not None and sp_entry is not None:
        highlight_map = detail['highlight_map']
        equipment_entities = fc.get_space_contained_equipment(data['ifc_file'], sp_entry['entity'])

    fig = fc.build_plan_figure(
        plan, selected_guid=selected_guid,
        highlight_map=highlight_map, equipment_entities=equipment_entities,
    )
    event = st.plotly_chart(
        fig, key=f'{session_prefix}_plot', on_select='rerun',
        selection_mode=('points',), use_container_width=True,
    )

    if event and event.get('selection', {}).get('points'):
        guid = _extract_customdata_guid(event['selection']['points'][0])
        if guid and guid != selected_guid:
            st.session_state[selected_key] = guid
            st.rerun()  # 새로 클릭된 공간 기준으로 하이라이트/설비를 다시 그리기 위해 즉시 재실행

    if detail is not None:
        _render_legend()
        _render_space_detail(detail)
    elif selected_guid:
        st.warning('선택된 공간을 이 층에서 찾을 수 없습니다 (층이 바뀌었을 수 있음).')
    else:
        st.caption('평면도에서 공간을 클릭하면 상세 정보가 여기 표시됩니다.')


def _render_legend():
    st.caption(
        '🟦 내부(내벽) · 🟧 외부(외벽, 판정불가 포함) · 🟪 벽 이외 관련부재(기둥/문/창/바닥 등) · '
        '🟨 설비(조명·센서·소방장치) · ⬜ 선택된 공간과 무관한 배경 요소'
    )


def _render_space_detail(detail):
    st.markdown(f"### 📍 {detail['name']}" + (f" ({detail['long_name']})" if detail['long_name'] else ''))
    c1, c2 = st.columns(2)
    with c1:
        st.metric('공간 면적(㎡)', detail['area'] if detail['area'] is not None else 'N/A')
        st.caption(f"산출방식: {detail['area_method']}")
    with c2:
        st.caption(f"GlobalId: `{detail['guid']}`")

    st.markdown('**접한 구조재 개수 (전체)**')
    if detail['class_counts']:
        st.table({
            '클래스': list(detail['class_counts'].keys()),
            '개수': list(detail['class_counts'].values()),
        })
    else:
        st.caption('(RelSpaceBoundary로 연결된 구조재 없음)')

    if detail['wall_simple_counts']:
        st.markdown('**벽 내부/외부 구분** (좌우 비교가 대칭이 되도록 판정불가는 외부로 편입, 괄호로 표기)')
        keys = list(detail['wall_simple_counts'].keys())
        st.table({
            '구분': keys,
            '개수': [detail['wall_simple_counts'][k] for k in keys],
            '합산면적(㎡)': [detail['wall_simple_area'].get(k, 0) for k in keys],
        })
        with st.expander('원 판정 상세 근거 보기'):
            st.table({
                '상세판정': list(detail['wall_detail_counts'].keys()),
                '개수': list(detail['wall_detail_counts'].values()),
            })

    if detail['area_by_class']:
        st.markdown('**벽 이외 부재 유형별 합산 면적** (계산 가능한 경우만 표시됨)')
        keys = list(detail['area_by_class'].keys())
        st.table({
            '클래스': keys,
            '면적합계(㎡)': [detail['area_by_class'][k]['면적합계(㎡)'] for k in keys],
            '산출가능/전체': [detail['area_by_class'][k]['산출가능/전체'] for k in keys],
        })

    if detail['equipment_counts']:
        st.markdown('**설비 개수** (조명/센서/소방장치 - 구조재와 별도 집계)')
        st.table({
            '클래스': list(detail['equipment_counts'].keys()),
            '개수': list(detail['equipment_counts'].values()),
        })
    else:
        st.caption('(이 공간에 배치된 조명/센서/소방장치 없음)')


# ===================================================================
# 메인 UI
# ===================================================================

col_up1, col_up2 = st.columns(2)
with col_up1:
    file_a = st.file_uploader('전문가 IFC 업로드', type=['ifc'], key='upload_a')
with col_up2:
    file_b = st.file_uploader('AI 생성 IFC 업로드', type=['ifc'], key='upload_b')

if file_a and file_b:
    data_a = _load_ifc_cached(file_a.getvalue(), file_a.name)
    data_b = _load_ifc_cached(file_b.getvalue(), file_b.name)

    def _fmt_storey(s):
        elev = s['Elevation']
        return f"{s['Name']} (고도 {elev:.0f}mm)" if elev is not None else s['Name']

    col_sel1, col_sel2 = st.columns(2)
    with col_sel1:
        selected_a_name = st.selectbox(
            '전문가 IFC 층 선택', [s['Name'] for s in data_a['storeys']],
            format_func=lambda n: _fmt_storey(next(s for s in data_a['storeys'] if s['Name'] == n)),
            key='storey_select_a',
        )
    with col_sel2:
        selected_b_name = st.selectbox(
            'AI IFC 층 선택', [s['Name'] for s in data_b['storeys']],
            format_func=lambda n: _fmt_storey(next(s for s in data_b['storeys'] if s['Name'] == n)),
            key='storey_select_b',
        )

    st.caption(
        '두 IFC의 층은 각각 독립적으로 선택합니다 (자동 매핑 미적용). '
        '드롭다운에 표시된 고도(mm)를 참고해 같은 실제 층을 골라 비교해주세요.'
    )

    # 층 선택이 바뀌면 이전 선택된 공간 정보는 초기화
    _cur_key = (selected_a_name, selected_b_name)
    if st.session_state.get('_last_storey_pair') != _cur_key:
        st.session_state.pop('left_selected_guid', None)
        st.session_state.pop('right_selected_guid', None)
        st.session_state['_last_storey_pair'] = _cur_key

    col_left, col_right = st.columns(2)
    with col_left:
        _render_side('전문가 IFC', data_a, selected_a_name, 'left')
    with col_right:
        _render_side('AI IFC', data_b, selected_b_name, 'right')
else:
    st.info('좌측/우측에 전문가 IFC와 AI 생성 IFC를 각각 업로드해주세요.')
