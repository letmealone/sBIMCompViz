# -*- coding: utf-8 -*-
"""
floorplan_app.py
------------------
전문가 IFC와 AI 생성 IFC를 업로드해 층별 평면도를 좌/우로 비교하는 Streamlit 앱.

핵심 기능:
- 층별 평면도 좌/우 비교, 공간(Space) 클릭시 접한 구조재/내외벽/면적/설비 정보 표시
- 공간 클릭시 내부(파랑)/외부(주황)/기타관련부재(보라)/설비(노랑) 색상 하이라이트
- (선택) 공간 자동 매핑: 면적+centroid 좌표 오차 임계값 기준. 조닝이 달라 1:1로 안 맞으면
  인접 공간을 순차 병합해 면적을 맞춰보는 고도화된 매칭까지 지원
- 비교 테이블(구조재개수/내외부구분/유형별면적/설비개수)은 두 IFC의 합집합 키로 통일해 표시

실행:
    streamlit run floorplan_app.py

요구사항: streamlit>=1.35 (st.plotly_chart의 on_select 기능 필요), plotly, shapely,
ifcopenshell, numpy, pandas, openpyxl (ifc_to_excel.py 의존성)
"""
import os
import tempfile

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


@st.cache_resource(show_spinner='공간 자동 매핑 계산 중... (면적으로 좌표계 오프셋 추정 후 매칭, 조닝 다르면 인접공간 병합 시도)')
def _match_spaces_cached(_spaces_a, _spaces_b, area_thresh, centroid_thresh, adjacency_tol, max_group, cache_tag):
    """_spaces_a/_spaces_b: 언더스코어 접두사라 해싱 제외(폴리곤 객체 포함이라 해시 불가/불안정).
    나머지 인자는 해시 가능 -> 임계값/층조합이 바뀌면 캐시도 갱신됨."""
    return fc.match_spaces(_spaces_a, _spaces_b, area_thresh=area_thresh, centroid_thresh=centroid_thresh,
                            adjacency_tol=adjacency_tol, max_group=max_group)


def _get_selected_entries(plan, selected_guids):
    """plan['spaces']에서 선택된 guid(들)에 해당하는 항목들 반환."""
    if not selected_guids:
        return []
    guid_set = set(selected_guids) if isinstance(selected_guids, (list, tuple, set)) else {selected_guids}
    return [s for s in plan['spaces'] if s['guid'] in guid_set]


def _render_plot_and_get_detail(label, data, storey_name, plan, session_prefix):
    """평면도 렌더링 + 클릭 이벤트 처리. (선택된 공간(그룹)의 detail, 새로 클릭된 단일 guid) 반환.
    새로 클릭된 guid는 항상 사용자가 실제로 클릭한 '단일' 공간이다(조닝 병합은 반대편에서만 적용됨)."""
    st.subheader(label)
    if storey_name is None or plan is None:
        st.info('이 층에 대응하는 층을 찾지 못했습니다 (층 매핑 없음).')
        return None, None

    st.caption(f"공간 {len(plan['spaces'])}개 · 구조요소 {len(plan['structural'])}개  (층: {storey_name})")

    selected_key = f'{session_prefix}_selected_guids'
    selected_guids = st.session_state.get(selected_key) or []

    entries = _get_selected_entries(plan, selected_guids)
    detail = None
    if entries:
        detail = fc.build_space_group_detail(data['ifc_file'], data['wall_classification'], entries)

    equipment_entities = None
    highlight_map = None
    if detail is not None:
        highlight_map = detail['highlight_map']
        equipment_entities = fc.get_group_contained_equipment(data['ifc_file'], [e['entity'] for e in entries])

    fig = fc.build_plan_figure(
        plan, selected_guid=selected_guids,
        highlight_map=highlight_map, equipment_entities=equipment_entities,
    )
    event = st.plotly_chart(
        fig, key=f'{session_prefix}_plot', on_select='rerun',
        selection_mode=('points',), width='stretch',
    )

    new_guid = None
    if event and event.get('selection', {}).get('points'):
        g = _extract_customdata_guid(event['selection']['points'][0])
        if g and [g] != selected_guids:
            new_guid = g

    if detail is not None:
        _render_legend()
        title = f"**📍 {detail['name']}**"
        if detail['is_group']:
            title += f" _(자동매핑: {len(detail['guids'])}개 공간 병합)_"
        st.markdown(title)
        c1, c2 = st.columns(2)
        with c1:
            st.metric('공간 면적(㎡)', detail['area'] if detail['area'] is not None else 'N/A')
            st.caption(f"산출방식: {detail['area_method']}")
        with c2:
            if detail['is_group']:
                st.caption("GlobalId: 여러 개 (병합그룹)")
            else:
                st.caption(f"GlobalId: `{detail['guid']}`")
    elif selected_guids:
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
    """선택된 두 공간(좌/우, 그룹 가능)의 지표를 합집합 기준 통일 테이블로 나란히 비교."""
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
# 사이드바: 공간 자동 매핑 설정
# ===================================================================

with st.sidebar:
    st.header('⚙️ 공간 자동 매핑')
    auto_map_enabled = st.checkbox(
        '면적 + centroid 좌표 오차 기준으로 자동 매핑',
        value=False,
        help='한쪽 평면도에서 공간을 클릭하면, 두 IFC의 좌표계 차이(평행이동)를 면적이 '
             '비슷한 후보들로부터 자동 추정한 뒤, 그 오프셋을 보정한 centroid 거리가 가장 가까운 '
             '공간을 반대편에서 찾습니다. 면적이 바로 안 맞으면(조닝이 다른 경우) 그 공간에 '
             '인접한 공간들을 순차적으로 합쳐 면적이 맞는지 시도합니다.',
    )
    if auto_map_enabled:
        area_thresh = st.number_input('면적 오차 임계값 (㎡)', min_value=0.0, value=2.0, step=0.5)
        centroid_thresh = st.number_input('centroid 좌표 오차 임계값 (m)', min_value=0.0, value=1.0, step=0.1)
        with st.expander('조닝 병합 옵션 (고급)'):
            adjacency_tol = st.number_input('인접 판정 거리 허용오차 (m)', min_value=0.0, value=0.1, step=0.05,
                                             help='두 공간의 경계가 이 거리 이내면 인접한 것으로 보고 병합 후보로 삼습니다.')
            max_group = st.number_input('최대 병합 공간 개수', min_value=1, max_value=15, value=5, step=1)
        st.caption(
            '⚠️ 두 모델의 좌표계가 회전 없이 평행이동만 다르다고 가정합니다. '
            '건물이 회전되어 모델링된 경우 이 방식이 맞지 않을 수 있습니다. '
            '또한 오프셋 자체는 "바로 1:1로 맞는 공간"이 최소 몇 개는 있어야 추정 가능합니다 '
            '(층 전체가 조닝이 다르면 오프셋 추정부터 실패할 수 있음).'
        )
    else:
        area_thresh = centroid_thresh = adjacency_tol = max_group = None


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
        '두 IFC의 층은 각각 독립적으로 선택합니다. 드롭다운에 표시된 고도(mm)를 참고해 '
        '같은 실제 층을 골라 비교해주세요.'
    )

    # 층 선택이 바뀌면 이전 선택된 공간 정보는 초기화
    _cur_key = (selected_a_name, selected_b_name)
    if st.session_state.get('_last_storey_pair') != _cur_key:
        st.session_state.pop('left_selected_guids', None)
        st.session_state.pop('right_selected_guids', None)
        st.session_state['_last_storey_pair'] = _cur_key

    plan_a = _build_plan_cached(data_a['storeys'], selected_a_name, 'left')
    plan_b = _build_plan_cached(data_b['storeys'], selected_b_name, 'right')

    space_a_to_b, space_b_to_a = {}, {}
    if auto_map_enabled:
        space_a_to_b, space_b_to_a, match_offset, match_info = _match_spaces_cached(
            plan_a['spaces'], plan_b['spaces'], area_thresh, centroid_thresh,
            adjacency_tol, max_group, f'{selected_a_name}|{selected_b_name}',
        )
        if match_offset:
            n_merged = sum(1 for m in match_info if m['merged'])
            st.success(
                f"공간 자동 매핑: {len(match_info)}쌍 매칭됨 (그 중 조닝 병합 {n_merged}건) "
                f"· 추정 좌표 오프셋 dx={match_offset[0]:.2f}m, dy={match_offset[1]:.2f}m"
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
        st.session_state['left_selected_guids'] = [new_left]
        changed = True
        if auto_map_enabled and new_left in space_a_to_b:
            st.session_state['right_selected_guids'] = space_a_to_b[new_left]
    if new_right:
        st.session_state['right_selected_guids'] = [new_right]
        changed = True
        if auto_map_enabled and new_right in space_b_to_a:
            st.session_state['left_selected_guids'] = space_b_to_a[new_right]
    if changed:
        st.rerun()  # 하이라이트/자동매핑 반영을 위해 갱신된 session_state로 즉시 재실행

    _render_comparison_tables(detail_left, detail_right, '전문가', 'AI')
else:
    st.info('좌측/우측에 전문가 IFC와 AI 생성 IFC를 각각 업로드해주세요.')
