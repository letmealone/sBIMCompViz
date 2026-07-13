# -*- coding: utf-8 -*-
"""
floorplan_core.py
------------------
Streamlit 평면도 비교 앱의 데이터/지오메트리 처리 로직.
ifc_to_excel.py의 함수(내외벽 판정, 면적 계산 등)를 최대한 재사용한다.
Streamlit에 의존하지 않으므로 단독으로 import/테스트 가능하다.
"""
import statistics
from collections import Counter, defaultdict

import numpy as np
import ifcopenshell
import ifcopenshell.geom as geom
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union

import ifc_to_excel as ite  # 내외벽 판정(_determine_wall_classification), 면적계산(_area_columns) 등 재사용

_SETTINGS = geom.settings()
_SETTINGS.set('use-world-coords', True)

# 평면도에 그릴 대상 클래스. Space는 클릭 가능(색상 채움), 나머지는 참고용 윤곽선만 표시.
PLAN_STRUCTURAL_CLASSES = (
    'IfcWall', 'IfcWallStandardCase', 'IfcColumn', 'IfcBeam',
    'IfcSlab', 'IfcCurtainWall', 'IfcDoor', 'IfcWindow',
)


# ===================================================================
# 1. IFC 로딩 및 층 매핑
# ===================================================================

def load_ifc(path):
    """IFC 파일을 열고 앱에서 바로 쓸 수 있는 형태로 구조화."""
    ifc_file = ifcopenshell.open(path)

    storeys = []
    for s in ifc_file.by_type('IfcBuildingStorey'):
        storeys.append({'Name': s.Name, 'Elevation': s.Elevation, 'entity': s})
    storeys.sort(key=lambda x: (x['Elevation'] is None, x['Elevation']))

    wall_classification = ite._determine_wall_classification(ifc_file)

    return {
        'ifc_file': ifc_file,
        'storeys': storeys,
        'wall_classification': wall_classification,
    }


def match_storeys(storeys_a, storeys_b, gap_cost=1000.0):
    """고도(Elevation) 기반 층 매핑 (ifc_compare_core.py의 build_floor_mapping과 동일한 방식,
    이 앱은 openai/httpx 등 무거운 의존성 없이 독립 실행되도록 여기서 간단히 재구현).
    반환: A_Name -> B_Name 매핑 dict, 오프셋(mm)."""
    valid_a = [s for s in storeys_a if s['Elevation'] is not None]
    valid_b = [s for s in storeys_b if s['Elevation'] is not None]
    if not valid_a or not valid_b:
        return {}, 0.0

    naive_diffs = []
    for a in valid_a:
        nearest = min(valid_b, key=lambda x: abs(x['Elevation'] - a['Elevation']))
        naive_diffs.append(a['Elevation'] - nearest['Elevation'])
    offset = statistics.median(naive_diffs)

    seq_a = [a['Elevation'] - offset for a in valid_a]
    seq_b = [b['Elevation'] for b in valid_b]
    n, m = len(seq_a), len(seq_b)

    dp = [[0.0] * (m + 1) for _ in range(n + 1)]
    for i in range(1, n + 1):
        dp[i][0] = dp[i - 1][0] + gap_cost
    for j in range(1, m + 1):
        dp[0][j] = dp[0][j - 1] + gap_cost
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            match_cost = abs(seq_a[i - 1] - seq_b[j - 1])
            dp[i][j] = min(
                dp[i - 1][j - 1] + match_cost,
                dp[i - 1][j] + gap_cost,
                dp[i][j - 1] + gap_cost,
            )

    i, j = n, m
    pairs = []
    while i > 0 or j > 0:
        if i > 0 and j > 0 and abs(dp[i][j] - (dp[i - 1][j - 1] + abs(seq_a[i - 1] - seq_b[j - 1]))) < 1e-6:
            pairs.append((i - 1, j - 1)); i -= 1; j -= 1
        elif i > 0 and abs(dp[i][j] - (dp[i - 1][j] + gap_cost)) < 1e-6:
            pairs.append((i - 1, None)); i -= 1
        else:
            pairs.append((None, j - 1)); j -= 1
    pairs.reverse()

    mapping = {}
    for a_idx, b_idx in pairs:
        if a_idx is None:
            continue
        a_name = valid_a[a_idx]['Name']
        mapping[a_name] = valid_b[b_idx]['Name'] if b_idx is not None else None

    return mapping, offset


# ===================================================================
# 2. 지오메트리 (실제 footprint 폴리곤 추출)
# ===================================================================

def get_footprint_polygon(ent, tol=0.05):
    """엔티티의 바닥면(최저 Z 근처) 삼각형들을 shapely로 합쳐 실제 footprint 폴리곤 반환.
    형상이 없거나 계산 실패시 None. tol: 바닥면으로 간주할 Z 허용오차(m)."""
    try:
        shape = geom.create_shape(_SETTINGS, ent)
    except Exception:
        return None
    verts = np.array(shape.geometry.verts).reshape(-1, 3)
    faces = np.array(shape.geometry.faces).reshape(-1, 3)
    if len(verts) == 0 or len(faces) == 0:
        return None
    zmin = verts[:, 2].min()
    polys = []
    for tri in faces:
        p = verts[tri]
        if np.all(p[:, 2] <= zmin + tol):
            try:
                poly = Polygon(p[:, :2])
                if poly.is_valid and poly.area > 1e-9:
                    polys.append(poly)
            except Exception:
                continue
    if not polys:
        return None
    try:
        u = unary_union(polys)
    except Exception:
        return None
    if u.is_empty:
        return None
    return u


def _grid_points_in_polygon(poly, spacing=0.5):
    """폴리곤 내부를 spacing(m) 간격 격자로 채운 점 목록. Plotly 클릭 히트영역용
    (폴리곤 전체를 '클릭 가능 영역'으로 만들기 위해 보이지 않는 마커를 촘촘히 깔아둔다)."""
    minx, miny, maxx, maxy = poly.bounds
    xs = np.arange(minx, maxx + spacing, spacing)
    ys = np.arange(miny, maxy + spacing, spacing)
    pts = []
    for x in xs:
        for y in ys:
            if poly.contains(Point(x, y)):
                pts.append((float(x), float(y)))
    if not pts:
        c = poly.centroid
        pts.append((float(c.x), float(c.y)))
    return pts


def _polygon_xy_lists(poly):
    """shapely (Multi)Polygon -> Plotly에 그릴 (x리스트, y리스트) 반환.
    여러 폴리곤/구멍은 None으로 구분해 하나의 트레이스에 이어붙인다."""
    xs, ys = [], []

    def _add_ring(coords):
        cx, cy = zip(*coords)
        xs.extend(cx); xs.append(None)
        ys.extend(cy); ys.append(None)

    geoms = poly.geoms if poly.geom_type == 'MultiPolygon' else [poly]
    for g in geoms:
        _add_ring(list(g.exterior.coords))
    return xs, ys


# ===================================================================
# 3. 층별 요소 수집
# ===================================================================

def get_elements_for_storey(storey_entity, classes=None):
    """해당 층에 속한 요소 목록.
    storey_entity: load_ifc()가 반환한 storeys 리스트의 원소(dict, 'entity' 키에 실제 IfcBuildingStorey).
    구조부재(벽/기둥 등)는 IfcRelContainedInSpatialStructure(ContainsElements)로,
    IfcSpace는 대개 IfcRelAggregates(IsDecomposedBy)로 층에 연결되므로 둘 다 확인한다."""
    storey_ifc = storey_entity['entity'] if isinstance(storey_entity, dict) else storey_entity
    elements = []
    for rel in (storey_ifc.ContainsElements or []):
        for el in rel.RelatedElements:
            if classes is None or el.is_a() in classes:
                elements.append(el)
    for rel in (storey_ifc.IsDecomposedBy or []):
        for el in rel.RelatedObjects:
            if classes is None or el.is_a() in classes:
                elements.append(el)
    return elements


def build_storey_plan_data(storey_entity, tol=0.05):
    """해당 층의 Space + 구조요소들의 footprint 폴리곤을 미리 계산해 리스트로 반환.
    반환: {'spaces': [{'guid','name','polygon'}...], 'structural': [{'guid','class','name','polygon'}...]}
    (지오메트리 계산은 비용이 있으므로 앱에서 층 변경시에만 1회 호출하도록 캐싱 권장)"""
    spaces_raw = get_elements_for_storey(storey_entity, classes={'IfcSpace'})
    structural_raw = get_elements_for_storey(storey_entity, classes=set(PLAN_STRUCTURAL_CLASSES))

    spaces = []
    for sp in spaces_raw:
        poly = get_footprint_polygon(sp, tol=tol)
        if poly is None:
            continue
        spaces.append({'guid': sp.GlobalId, 'name': sp.Name or '(이름없음)', 'polygon': poly, 'entity': sp})

    structural = []
    for el in structural_raw:
        poly = get_footprint_polygon(el, tol=tol)
        if poly is None:
            continue
        structural.append({'guid': el.GlobalId, 'class': el.is_a(), 'name': el.Name or '', 'polygon': poly})

    return {'spaces': spaces, 'structural': structural}


# ===================================================================
# 4. 클릭된 Space의 상세 정보
# ===================================================================

EQUIPMENT_CLASSES = ('IfcLightFixture', 'IfcSensor', 'IfcFireSuppressionTerminal', 'IfcAlarm')


def get_space_related_elements(ifc_file, space_entity):
    """해당 Space와 RelSpaceBoundary로 연결된 부재 목록 (벽/기둥/문/창/바닥 등 경계형성 요소)."""
    related = []
    for rel in ifc_file.by_type('IfcRelSpaceBoundary'):
        if rel.RelatingSpace == space_entity and rel.RelatedBuildingElement is not None:
            related.append(rel.RelatedBuildingElement)
    return related


def get_space_contained_equipment(ifc_file, space_entity, classes=EQUIPMENT_CLASSES):
    """해당 Space '안에' 배치된 설비(조명/센서/소방장치 등) 목록.
    이런 설비는 RelSpaceBoundary(경계형성)가 아니라 IfcRelContainedInSpatialStructure
    (공간적 포함관계)로 Space와 연결된다 (실측으로 확인한 IFC 구조)."""
    equipment = []
    for rel in ifc_file.by_type('IfcRelContainedInSpatialStructure'):
        if rel.RelatingStructure == space_entity:
            for el in rel.RelatedElements:
                if el.is_a() in classes:
                    equipment.append(el)
    return equipment


def _wall_display_category(result):
    """내/외벽 판정을 좌/우(전문가·AI) 비교가 대칭이 되도록 '내부'/'외부' 이진으로 단순화.
    '판정불가'는 근거 데이터가 없을 뿐 외벽일 가능성을 배제할 수 없고, 무엇보다
    AI 모델처럼 한쪽이 전부 판정불가로 나오는 경우 비교 자체가 안 되므로,
    사용자 요청에 따라 '외부'로 편입하되 괄호로 표시해 원래 판정불가였음을 남긴다."""
    if result == '내벽':
        return '내부', '내벽'
    if result == '판정불가':
        return '외부', '외벽(판정불가)'
    return '외부', result  # '외벽' / '외벽(추정)'


def build_space_detail(ifc_file, wall_classification, space_entity):
    """클릭된 Space 1개에 대한 요약 정보:
    - 접한 구조재 개수(전체) + 벽 내/외부 구분(좌우대칭 이진 + 상세근거 병기)
    - 관련된 모든 부재 유형별 합산 면적(계산 가능한 모든 클래스, 벽은 별도 처리)
    - 공간 내 설비(조명/센서/소방장치/경보기) 개수
    """
    related = get_space_related_elements(ifc_file, space_entity)
    equipment = get_space_contained_equipment(ifc_file, space_entity)

    class_counts = Counter(e.is_a() for e in related)

    # 벽 내/외부 구분 (좌우 대칭 이진) + 상세 판정(원래 4분류) 병기
    wall_simple_counts = Counter()
    wall_simple_area = Counter()
    wall_detail_counts = Counter()
    for e in related:
        if not e.is_a('IfcWall'):
            continue
        result, _reason = wall_classification.get(e.GlobalId, ('판정불가', ''))
        simple, detail_label = _wall_display_category(result)
        wall_simple_counts[simple] += 1
        wall_detail_counts[detail_label] += 1
        flat = ite._flatten_psets(e)
        v = flat.get('Qto_WallBaseQuantities.Gross_Side_Area')
        if isinstance(v, (int, float)):
            wall_simple_area[simple] += v

    # 벽 이외 관련 부재: 계산 가능한 모든 클래스에 대해 면적 산정 시도 ("가능한 경우"만 채워짐)
    area_by_class = {}
    non_wall_classes = sorted(set(e.is_a() for e in related if not e.is_a('IfcWall')))
    for cls in non_wall_classes:
        ents = [e for e in related if e.is_a(cls)]
        total, n_ok = 0.0, 0
        for e in ents:
            flat = ite._flatten_psets(e)
            cols = ite._area_columns(e, flat)
            if cols['면적(㎡)'] is not None:
                total += cols['면적(㎡)']
                n_ok += 1
        area_by_class[cls] = {
            '면적합계(㎡)': round(total, 2) if n_ok else None,
            '산출가능/전체': f'{n_ok}/{len(ents)}',
        }

    # 설비 개수 (구조재와 별도 집계)
    equipment_counts = Counter(e.is_a() for e in equipment)

    # Space 자신의 면적
    flat_sp = ite._flatten_psets(space_entity)
    space_area, space_area_method = None, None
    for key in ('Qto_SpaceBaseQuantities.NetFloorArea', 'Qto_SpaceBaseQuantities.GrossFloorArea'):
        if isinstance(flat_sp.get(key), (int, float)):
            space_area, space_area_method = flat_sp[key], key
            break
    if space_area is None:
        cols = ite._area_columns(space_entity, flat_sp)
        space_area, space_area_method = cols['면적(㎡)'], cols['면적산출방식']

    return {
        'name': space_entity.Name or '(이름없음)',
        'long_name': space_entity.LongName,
        'guid': space_entity.GlobalId,
        'area': round(space_area, 2) if space_area is not None else None,
        'area_method': space_area_method,
        'class_counts': dict(class_counts),
        'wall_simple_counts': dict(wall_simple_counts),
        'wall_simple_area': {k: round(v, 2) for k, v in wall_simple_area.items()},
        'wall_detail_counts': dict(wall_detail_counts),
        'area_by_class': area_by_class,
        'equipment_counts': dict(equipment_counts),
        # 평면도 하이라이트용: 각 관련 부재 GlobalId -> 표시 카테고리
        'highlight_map': _build_highlight_map(related, equipment, wall_classification),
    }


def _build_highlight_map(related, equipment, wall_classification):
    """평면도에서 색을 다르게 칠하기 위한 GlobalId -> 카테고리 매핑.
    카테고리: 'wall_internal'(내벽) / 'wall_external'(외벽, 판정불가 포함) /
              'related'(벽 이외 경계 관련 부재) / 'equipment'(조명/센서/소방설비)."""
    hl = {}
    for e in related:
        if e.is_a('IfcWall'):
            result, _ = wall_classification.get(e.GlobalId, ('판정불가', ''))
            simple, _ = _wall_display_category(result)
            hl[e.GlobalId] = 'wall_internal' if simple == '내부' else 'wall_external'
        else:
            hl[e.GlobalId] = 'related'
    for e in equipment:
        hl[e.GlobalId] = 'equipment'
    return hl

    return {
        'name': space_entity.Name or '(이름없음)',
        'long_name': space_entity.LongName,
        'guid': space_entity.GlobalId,
        'area': round(space_area, 2) if space_area is not None else None,
        'area_method': space_area_method,
        'class_counts': dict(class_counts),
        'wall_class_counts': dict(wall_class_counts),
        'wall_area_by_class': {k: round(v, 2) for k, v in wall_area_by_class.items()},
        'area_by_class': area_by_class,
    }


# ===================================================================
# 5. Plotly 평면도 figure 생성
# ===================================================================

_STRUCT_COLORS = {
    'IfcWall': 'rgba(90,90,90,0.85)',
    'IfcWallStandardCase': 'rgba(90,90,90,0.85)',
    'IfcColumn': 'rgba(40,40,40,0.9)',
    'IfcBeam': 'rgba(120,90,60,0.7)',
    'IfcSlab': 'rgba(200,190,170,0.4)',
    'IfcCurtainWall': 'rgba(120,170,220,0.6)',
    'IfcDoor': 'rgba(150,100,50,0.6)',
    'IfcWindow': 'rgba(120,200,230,0.6)',
}
_FADED_COLOR = 'rgba(210,210,210,0.35)'
_FADED_LINE = 'rgba(190,190,190,0.5)'

# 공간 클릭시 하이라이트 색상 (카테고리별로 뚜렷이 구분)
_HIGHLIGHT_COLORS = {
    'wall_internal': ('rgba(30,110,230,0.85)', 'rgba(15,70,160,1.0)'),   # 내벽 = 파랑
    'wall_external': ('rgba(230,90,30,0.85)', 'rgba(170,60,10,1.0)'),    # 외벽(판정불가 포함) = 주황
    'related': ('rgba(160,50,190,0.75)', 'rgba(110,20,140,1.0)'),        # 벽 이외 관련부재 = 보라
}
_EQUIPMENT_COLOR = 'rgba(220,190,20,0.95)'  # 설비(조명/센서/소방) = 노랑 마커

_SPACE_FILL = 'rgba(100,180,120,0.35)'
_SPACE_FILL_SELECTED = 'rgba(230,100,60,0.55)'
_SPACE_LINE = 'rgba(60,140,80,0.9)'
_SPACE_LINE_SELECTED = 'rgba(200,60,20,1.0)'


def build_plan_figure(plan_data, click_grid_spacing=0.5, selected_guid=None,
                       highlight_map=None, equipment_entities=None):
    """plan_data(build_storey_plan_data 반환값)로 Plotly Figure 생성.

    Space는 내부에 보이지 않는 마커 격자를 깔아 '폴리곤 내부 아무 곳이나 클릭'해도
    선택되도록 한다(Plotly는 기본적으로 마커/점 단위로만 클릭을 인식하기 때문).

    selected_guid: 강조 표시할 선택된 Space의 GlobalId.
    highlight_map: build_space_detail()이 반환한 {GlobalId: 카테고리} 딕셔너리.
        선택된 Space와 관련된 구조요소를 카테고리별 색상으로 강조하고, 나머지 배경
        구조요소는 흐리게(faded) 처리해 '내부 객체(파랑)/외부 객체(주황)/기타 관련부재(보라)'가
        한눈에 구분되도록 한다. None이면(선택 없음) 기본 클래스별 색상으로 표시.
    equipment_entities: 선택된 Space '안에' 있는 설비(조명/센서/소방장치 등) ifcopenshell 엔티티
        목록. 평면도에 노란 마커로 추가 표시한다 (RelSpaceBoundary 대상이 아니라 별도로 그림).
    """
    import plotly.graph_objects as go
    fig = go.Figure()
    highlight_map = highlight_map or {}

    # 구조요소(벽/기둥/보/바닥 등)
    for el in plan_data['structural']:
        xs, ys = _polygon_xy_lists(el['polygon'])
        cat = highlight_map.get(el['guid'])
        if highlight_map:
            if cat in _HIGHLIGHT_COLORS:
                fill_c, line_c = _HIGHLIGHT_COLORS[cat]
                line_w = 1.5
            else:
                fill_c, line_c, line_w = _FADED_COLOR, _FADED_LINE, 0.5
        else:
            fill_c = _STRUCT_COLORS.get(el['class'], 'rgba(150,150,150,0.5)')
            line_c, line_w = 'rgba(60,60,60,0.6)', 0.5
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode='lines', fill='toself',
            line=dict(width=line_w, color=line_c), fillcolor=fill_c,
            hoverinfo='text', text=f"{el['class']} {el['name']}".strip(),
            showlegend=False,
        ))

    # Space: 시각적 채움(폴리곤) + 클릭 히트영역(격자 마커, 투명)
    for sp in plan_data['spaces']:
        is_sel = (selected_guid is not None and sp['guid'] == selected_guid)
        fill_c = _SPACE_FILL_SELECTED if is_sel else _SPACE_FILL
        line_c = _SPACE_LINE_SELECTED if is_sel else _SPACE_LINE

        xs, ys = _polygon_xy_lists(sp['polygon'])
        fig.add_trace(go.Scatter(
            x=xs, y=ys, mode='lines', fill='toself',
            line=dict(width=1.5 if is_sel else 1.0, color=line_c), fillcolor=fill_c,
            hoverinfo='skip', showlegend=False,
        ))

        pts = _grid_points_in_polygon(sp['polygon'], spacing=click_grid_spacing)
        gx, gy = zip(*pts)
        fig.add_trace(go.Scatter(
            x=list(gx), y=list(gy), mode='markers',
            marker=dict(size=14, opacity=0.0),
            customdata=[sp['guid']] * len(pts),
            hovertemplate=f"{sp['name']}<br>면적 약 {round(sp['polygon'].area,1)}㎡<extra></extra>",
            showlegend=False,
        ))

    # 설비(조명/센서/소방장치): 선택된 Space 안에 있는 것만 노란 마커로 표시
    if equipment_entities:
        ex, ey, etext = [], [], []
        for e in equipment_entities:
            poly = get_footprint_polygon(e)
            if poly is not None and not poly.is_empty:
                c = poly.centroid
                ex.append(c.x); ey.append(c.y)
            else:
                # 형상이 없으면 배치 좌표(ObjectPlacement)라도 사용
                try:
                    import ifcopenshell.util.placement as plc
                    m = plc.get_local_placement(e.ObjectPlacement)
                    ex.append(float(m[0, 3])); ey.append(float(m[1, 3]))
                except Exception:
                    continue
            etext.append(f"{e.is_a()} {e.Name or ''}".strip())
        if ex:
            fig.add_trace(go.Scatter(
                x=ex, y=ey, mode='markers',
                marker=dict(size=11, color=_EQUIPMENT_COLOR, symbol='diamond',
                            line=dict(width=1, color='rgba(120,100,0,1)')),
                text=etext, hoverinfo='text', showlegend=False,
            ))

    fig.update_xaxes(showgrid=False, zeroline=False, visible=False)
    fig.update_yaxes(showgrid=False, zeroline=False, visible=False, scaleanchor='x', scaleratio=1)
    fig.update_layout(
        margin=dict(l=10, r=10, t=30, b=10),
        height=600,
        plot_bgcolor='white',
        clickmode='event+select',
    )
    return fig

