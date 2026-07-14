# -*- coding: utf-8 -*-
"""
floorplan_app.py
------------------
전문가 IFC와 AI 생성 IFC를 업로드해 층별 평면도를 좌/우로 비교하는 Streamlit 앱.

핵심 기능:
- 층별 평면도 좌/우 비교, 공간(Space) 클릭시 접한 구조재/내외벽/면적/설비 정보 표시
- 공간 클릭시 내부(파랑)/외부(주황)/기타관련부재(보라)/설비(노랑) 색상 하이라이트
- (선택) 공간 자동 매핑: 면적+centroid 좌표 오차 임계값 기준으로 한쪽 클릭시 반대편도 자동 선택
- 비교 테이블(구조재개수/내외부구분/유형별면적/설비개수)은 두 IFC의 합집합 키로 통일해 표시

실행:
    streamlit run floorplan_app.py

요구사항: streamlit>=1.35 (st.plotly_chart의 on_select 기능 필요), plotly, shapely,
ifcopenshell, numpy, pandas, openpyxl (ifc_to_excel.py 의존성)
"""
import os
import tempfile
import hashlib

import streamlit as st

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



_SESSION_CACHE_PREFIX = '_cache__'


def _session_cache(key, compute_fn):
    """이 세션(session_state)에만 저장되는 캐시. st.cache_resource와 달리 다른 사용자의
    세션에는 전혀 영향을 주지 않는다 - session_state 자체가 세션별로 격리되어 있기 때문.
    key가 이미 있으면 재계산 없이 그대로 반환, 없으면 compute_fn()을 실행해 저장 후 반환."""
    full_key = _SESSION_CACHE_PREFIX + key
    if full_key not in st.session_state:
        st.session_state[full_key] = compute_fn()
    return st.session_state[full_key]


def _load_ifc_cached(file_bytes, filename, file_hash):
    """업로드된 IFC를 임시파일로 저장 후 파싱. 이 세션 안에서 같은 파일(해시로 식별)에 대해
    1회만 실행되도록 session_state에 저장."""
    def _compute():
        suffix = os.path.splitext(filename)[1] or '.ifc'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(file_bytes)
            path = tmp.name
        return fc.load_ifc(path)
    with st.spinner('IFC 파일 파싱 중... (지붕/천장 면적 등 지오메트리 계산 포함, 수 초~수십 초 소요될 수 있음)'):
        return _session_cache(f'ifc_{file_hash}', _compute)


def _build_plan_cached(storeys, storey_name, cache_tag):
    """층별 평면 지오메트리를 이 세션 안에서만 캐싱.
    cache_tag: 파일해시 등을 포함한 문자열로, 같은 층 이름이라도 파일이 다르면 구분되게 한다."""
    def _compute():
        storey = next(s for s in storeys if s['Name'] == storey_name)
        return fc.build_storey_plan_data(storey)
    with st.spinner('해당 층 도면 지오메트리 계산 중...'):
        return _session_cache(f'plan_{cache_tag}_{storey_name}', _compute)


def _match_spaces_cached(spaces_a, spaces_b, area_thresh, centroid_thresh, cache_tag):
    """공간 자동 매핑 결과를 이 세션 안에서만 캐싱."""
    def _compute():
        return fc.match_spaces(spaces_a, spaces_b, area_thresh=area_thresh, centroid_thresh=centroid_thresh)
    with st.spinner('공간 자동 매핑 계산 중... (면적으로 좌표계 오프셋 추정 후 centroid 매칭)'):
        return _session_cache(f'spacematch_{cache_tag}_{area_thresh}_{centroid_thresh}', _compute)


def _clear_all_caches():
    """이 세션의 캐시(IFC 파싱/평면 지오메트리/공간매칭 결과)와 선택 상태를 전부 비운다.
    session_state 기반이라 다른 사용자의 세션에는 전혀 영향을 주지 않는다."""
    for key in list(st.session_state.keys()):
        if key.startswith((_SESSION_CACHE_PREFIX, 'left_', 'right_', '_last_storey_pair', '_file_hash_')):
            del st.session_state[key]


def _render_plot_and_get_detail(label, data, storey_name, plan, session_prefix):
    """평면도 렌더링 + 클릭 이벤트 처리. (선택된 공간의 detail, 새로 클릭된 guid) 반환."""
    st.subheader(label)
    if storey_name is None or plan is None:
        st.info('이 층에 대응하는 층을 찾지 못했습니다 (층 매핑 없음).')
        return None, None

    st.caption(f"공간 {len(plan['spaces'])}개 · 구조요소 {len(plan['structural'])}개  (층: {storey_name})")

    selected_key = f'{session_prefix}_selected_guid'
    selected_guid = st.session_state.get(selected_key)

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

    new_guid = None
    if event and event.get('selection', {}).get('points'):
        g = _extract_customdata_guid(event['selection']['points'][0])
        if g and g != selected_guid:
            new_guid = g

    if detail is not None:
        _render_legend()
        st.markdown(f"**📍 {detail['name']}**" + (f" ({detail['long_name']})" if detail['long_name'] else ''))
        c1, c2 = st.columns(2)
        with c1:
            st.metric('공간 면적(㎡)', detail['area'] if detail['area'] is not None else 'N/A')
            st.caption(f"산출방식: {detail['area_method']}")
        with c2:
            st.caption(f"GlobalId: `{detail['guid']}`")
    elif selected_guid:
        st.warning('선택된 공간을 이 층에서 찾을 수 없습니다 (층이 바뀌었을 수 있음).')
    else:
        st.caption('평면도에서 공간을 클릭하면 상세 정보가 여기 표시됩니다.')

    return detail, new_guid


def _render_legend():
    st.caption(
        '🟦 내부(내벽) · 🟧 외부(외벽, 판정불가 포함) · 🟪 벽 이외 관련부재(기둥/문/창/바닥 등) · '
        '🟨 설비(조명·센서·소방장치) · ⬜ 선택된 공간과 무관한 배경 요소'
    )


def _render_union_table(title, left_d, right_d, label_left, label_right,
                         extra_left=None, extra_right=None, extra_label=None):
    """left_d/right_d(dict) 키의 합집합을 행으로 하는 통일된 비교 테이블 렌더링.
    (한쪽에만 있는 클래스도 다른 쪽엔 0으로 채워져 두 IFC 표의 행 구성이 항상 동일해진다)"""
    if not left_d and not right_d:
        return
    keys = list(dict.fromkeys(list(left_d.keys()) + list(right_d.keys())))
    st.markdown(f'**{title}**')
    table = {
        '구분': keys,
        label_left: [left_d.get(k, 0) for k in keys],
        label_right: [right_d.get(k, 0) for k in keys],
    }
    if extra_left is not None and extra_right is not None:
        table[f'{label_left}·{extra_label}'] = [extra_left.get(k, 0) for k in keys]
        table[f'{label_right}·{extra_label}'] = [extra_right.get(k, 0) for k in keys]
    st.table(table)


def _render_comparison_tables(detail_left, detail_right, label_left='전문가', label_right='AI'):
    """선택된 두 공간(좌/우)의 지표를 합집합 기준 통일 테이블로 나란히 비교."""
    if detail_left is None and detail_right is None:
        return
    st.markdown('---')
    st.markdown('## 🔍 선택된 공간 비교')

    c1, c2 = st.columns(2)
    with c1:
        st.markdown(f"**{label_left}**: " + (f"{detail_left['name']} (면적 {detail_left['area']}㎡)"
                    if detail_left else '선택된 공간 없음'))
    with c2:
        st.markdown(f"**{label_right}**: " + (f"{detail_right['name']} (면적 {detail_right['area']}㎡)"
                    if detail_right else '선택된 공간 없음'))

    dl = detail_left or {}
    dr = detail_right or {}

    _render_union_table('접한 구조재 개수', dl.get('class_counts', {}), dr.get('class_counts', {}),
                         label_left, label_right)

    _render_union_table('벽 내부/외부 구분', dl.get('wall_simple_counts', {}), dr.get('wall_simple_counts', {}),
                         label_left, label_right,
                         extra_left=dl.get('wall_simple_area', {}), extra_right=dr.get('wall_simple_area', {}),
                         extra_label='합산면적(㎡)')

    area_l = {k: v['면적합계(㎡)'] for k, v in dl.get('area_by_class', {}).items()}
    area_r = {k: v['면적합계(㎡)'] for k, v in dr.get('area_by_class', {}).items()}
    _render_union_table('벽 이외 부재 유형별 합산 면적(㎡)', area_l, area_r, label_left, label_right)

    _render_union_table('설비 개수', dl.get('equipment_counts', {}), dr.get('equipment_counts', {}),
                         label_left, label_right)


# ===================================================================
# 사이드바: 초기화 + 공간 자동 매핑 설정
# ===================================================================

with st.sidebar:
    st.header('🔄 초기화')
    if st.button('내 세션 캐시 초기화 (문제 있을 때)', width='stretch'):
        _clear_all_caches()
        st.success('이 세션의 캐시를 초기화했습니다. (다른 사용자에게는 영향 없음)')
        st.rerun()
    st.caption(
        '평면도가 이전에 올린 파일 내용처럼 보이는 등 문제가 있을 때 눌러주세요. '
        '이 캐시는 세션(브라우저 탭)별로 독립되어 있어 다른 사용자의 화면에는 영향을 주지 않습니다. '
        '새 IFC를 업로드하면 자동으로도 초기화됩니다.'
    )
    st.divider()

    st.header('⚙️ 공간 자동 매핑')
    auto_map_enabled = st.checkbox(
        '면적 + centroid 좌표 오차 기준으로 자동 매핑',
        value=False,
        help='한쪽 평면도에서 공간을 클릭하면, 두 IFC의 좌표계 차이(평행이동)를 면적이 '
             '비슷한 후보들로부터 자동 추정한 뒤, 그 오프셋을 보정한 centroid 거리와 면적 오차가 '
             '둘 다 임계값 이내인 공간을 반대편에서 자동으로 찾아 함께 선택합니다.',
    )
    if auto_map_enabled:
        area_thresh = st.number_input('면적 오차 임계값 (㎡)', min_value=0.0, value=2.0, step=0.5)
        centroid_thresh = st.number_input('centroid 좌표 오차 임계값 (m)', min_value=0.0, value=1.0, step=0.1)
        st.caption(
            '⚠️ 두 모델의 좌표계가 회전 없이 평행이동만 다르다고 가정합니다. '
            '건물이 회전되어 모델링된 경우 이 방식이 맞지 않을 수 있습니다.'
        )
    else:
        area_thresh = centroid_thresh = None


# ===================================================================
# 메인 UI
# ===================================================================

col_up1, col_up2 = st.columns(2)
with col_up1:
    file_a = st.file_uploader('전문가 IFC 업로드', type=['ifc'], key='upload_a')
with col_up2:
    file_b = st.file_uploader('AI 생성 IFC 업로드', type=['ifc'], key='upload_b')

if file_a and file_b:
    # 실제 파일 내용 기반 식별자 (다른 파일이 우연히 같은 층 이름을 가져도 캐시가 섞이지 않도록,
    # 아래 _build_plan_cached/_match_spaces_cached의 cache_tag에 사용)
    file_hash_a = hashlib.md5(file_a.getvalue()).hexdigest()[:10]
    file_hash_b = hashlib.md5(file_b.getvalue()).hexdigest()[:10]

    # 이전 실행에서 기록해둔 파일 해시와 다르면(=새 IFC로 교체됨) 캐시를 자동으로 비운다
    # (근본 조치: 정합성은 해시 기반 캐시 키로 이미 보장되지만, 이렇게 안 하면 안 쓰는
    # 이전 파일의 캐시 엔트리가 계속 쌓여 메모리를 차지하게 된다)
    prev_hash_a = st.session_state.get('_file_hash_a')
    prev_hash_b = st.session_state.get('_file_hash_b')
    if (prev_hash_a is not None and prev_hash_a != file_hash_a) or \
       (prev_hash_b is not None and prev_hash_b != file_hash_b):
        _clear_all_caches()
        st.toast('새 IFC 파일이 감지되어 이전 캐시를 자동으로 비웠습니다.', icon='🔄')
    st.session_state['_file_hash_a'] = file_hash_a
    st.session_state['_file_hash_b'] = file_hash_b

    data_a = _load_ifc_cached(file_a.getvalue(), file_a.name, file_hash_a)
    data_b = _load_ifc_cached(file_b.getvalue(), file_b.name, file_hash_b)

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
        '두 IFC의 층은 각각 독립적으로 선택합니다. 드롭다운에 표시된 고도(mm)를 참고해 '
        '같은 실제 층을 골라 비교해주세요.'
    )

    # 층 선택이 바뀌면 이전 선택된 공간 정보는 초기화
    _cur_key = (selected_a_name, selected_b_name)
    if st.session_state.get('_last_storey_pair') != _cur_key:
        st.session_state.pop('left_selected_guid', None)
        st.session_state.pop('right_selected_guid', None)
        st.session_state['_last_storey_pair'] = _cur_key

    plan_a = _build_plan_cached(data_a['storeys'], selected_a_name, f'left_{file_hash_a}')
    plan_b = _build_plan_cached(data_b['storeys'], selected_b_name, f'right_{file_hash_b}')

    space_a_to_b, space_b_to_a = {}, {}
    if auto_map_enabled:
        space_a_to_b, space_b_to_a, match_offset, match_info = _match_spaces_cached(
            plan_a['spaces'], plan_b['spaces'], area_thresh, centroid_thresh,
            f'{file_hash_a}_{selected_a_name}|{file_hash_b}_{selected_b_name}',
        )
        if match_offset:
            st.success(
                f"공간 자동 매핑: {len(match_info)}쌍 매칭됨 "
                f"(추정 좌표 오프셋 dx={match_offset[0]:.2f}m, dy={match_offset[1]:.2f}m)"
            )
        else:
            st.warning('공간 자동 매핑: 매칭 후보를 찾지 못했습니다 (면적 임계값을 늘려보세요).')

    col_left, col_right = st.columns(2)
    with col_left:
        detail_left, new_left = _render_plot_and_get_detail(
            '전문가 IFC', data_a, selected_a_name, plan_a, 'left')
    with col_right:
        detail_right, new_right = _render_plot_and_get_detail(
            'AI IFC', data_b, selected_b_name, plan_b, 'right')

    changed = False
    if new_left:
        st.session_state['left_selected_guid'] = new_left
        changed = True
        if auto_map_enabled and new_left in space_a_to_b:
            st.session_state['right_selected_guid'] = space_a_to_b[new_left]
    if new_right:
        st.session_state['right_selected_guid'] = new_right
        changed = True
        if auto_map_enabled and new_right in space_b_to_a:
            st.session_state['left_selected_guid'] = space_b_to_a[new_right]
    if changed:
        st.rerun()  # 하이라이트/자동매핑 반영을 위해 갱신된 session_state로 즉시 재실행

    _render_comparison_tables(detail_left, detail_right, '전문가', 'AI')
else:
    st.info('좌측/우측에 전문가 IFC와 AI 생성 IFC를 각각 업로드해주세요.')
