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

    fig = fc.build_plan_figure(plan, selected_guid=selected_guid)
    event = st.plotly_chart(
        fig, key=f'{session_prefix}_plot', on_select='rerun',
        selection_mode=('points',), use_container_width=True,
    )

    if event and event.get('selection', {}).get('points'):
        guid = _extract_customdata_guid(event['selection']['points'][0])
        if guid:
            st.session_state[selected_key] = guid
            selected_guid = guid

    if selected_guid:
        sp_entry = next((s for s in plan['spaces'] if s['guid'] == selected_guid), None)
        if sp_entry is None:
            st.warning('선택된 공간을 이 층에서 찾을 수 없습니다 (층이 바뀌었을 수 있음).')
        else:
            detail = fc.build_space_detail(data['ifc_file'], data['wall_classification'], sp_entry['entity'])
            _render_space_detail(detail)
    else:
        st.caption('평면도에서 공간을 클릭하면 상세 정보가 여기 표시됩니다.')


def _render_space_detail(detail):
    st.markdown(f"### 📍 {detail['name']}" + (f" ({detail['long_name']})" if detail['long_name'] else ''))
    c1, c2 = st.columns(2)
    with c1:
        st.metric('공간 면적(㎡)', detail['area'] if detail['area'] is not None else 'N/A')
        st.caption(f"산출방식: {detail['area_method']}")
    with c2:
        st.caption(f"GlobalId: `{detail['guid']}`")

    st.markdown('**접한 구조재 개수**')
    if detail['class_counts']:
        st.table({
            '클래스': list(detail['class_counts'].keys()),
            '개수': list(detail['class_counts'].values()),
        })
    else:
        st.caption('(RelSpaceBoundary로 연결된 구조재 없음)')

    if detail['wall_class_counts']:
        st.markdown('**벽 내/외벽 구분**')
        st.table({
            '판정': list(detail['wall_class_counts'].keys()),
            '개수': list(detail['wall_class_counts'].values()),
            '합산면적(㎡)': [detail['wall_area_by_class'].get(k, 0) for k in detail['wall_class_counts'].keys()],
        })

    if detail['area_by_class']:
        st.markdown('**유형별 합산 면적(㎡)**')
        st.table({
            '클래스': list(detail['area_by_class'].keys()),
            '면적(㎡)': list(detail['area_by_class'].values()),
        })


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

    mapping, offset = fc.match_storeys(data_a['storeys'], data_b['storeys'])

    storey_names_a = [s['Name'] for s in data_a['storeys']]
    selected_a_name = st.selectbox('비교할 층 선택 (전문가 IFC 기준)', storey_names_a, key='storey_select')
    selected_b_name = mapping.get(selected_a_name)

    st.caption(
        f"고도 기준 자동 매핑 → AI측 층: **{selected_b_name or '매핑 안됨'}** "
        f"(추정 오프셋 {offset:.0f}mm). 층 이름 체계가 서로 달라 고도로 자동 매칭한 결과이니, "
        f"평면도 형태를 보고 실제로 같은 층이 맞는지 육안으로도 확인해주세요."
    )

    # 층이 바뀌면 이전 선택된 공간 정보는 초기화
    if st.session_state.get('_last_storey') != selected_a_name:
        st.session_state.pop('left_selected_guid', None)
        st.session_state.pop('right_selected_guid', None)
        st.session_state['_last_storey'] = selected_a_name

    col_left, col_right = st.columns(2)
    with col_left:
        _render_side('전문가 IFC', data_a, selected_a_name, 'left')
    with col_right:
        _render_side('AI IFC', data_b, selected_b_name, 'right')
else:
    st.info('좌측/우측에 전문가 IFC와 AI 생성 IFC를 각각 업로드해주세요.')
