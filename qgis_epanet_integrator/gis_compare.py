from __future__ import annotations

from dataclasses import dataclass

from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsProject,
    QgsPointXY,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
)
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import QVariant
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from qgis_epanet_integrator.elements import ModelLayer
from qgis_epanet_integrator.settings import ProjectSettings, SettingKey


@dataclass
class CompareOptions:
    gis_layer: QgsVectorLayer
    gis_id_field: str | None
    tolerance: float
    minimum_fragment_length: float
    node_tolerance: float


def _configured_model_node_layers() -> list[QgsVectorLayer]:
    saved = ProjectSettings().get(SettingKey.MODEL_LAYERS, {})
    result: list[QgsVectorLayer] = []
    for model_layer in (
        ModelLayer.JUNCTIONS,
        ModelLayer.RESERVOIRS,
        ModelLayer.TANKS,
    ):
        layer_id = saved.get(model_layer.name)
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        if isinstance(layer, QgsVectorLayer):
            result.append(layer)
    return result


def _configured_model_pipe_layer() -> QgsVectorLayer | None:
    saved = ProjectSettings().get(SettingKey.MODEL_LAYERS, {})
    layer_id = saved.get(ModelLayer.PIPES.name)
    layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
    return layer if isinstance(layer, QgsVectorLayer) else None


class GisCompareDialog(QDialog):
    def __init__(self, iface: QgisInterface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Znajdź nowe elementy GIS")
        self.resize(580, 280)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Narzędzie szuka fragmentów wybranej warstwy GIS, których nie ma "
            "jeszcze w modelu EPANET. Istniejąca geometria modelu pozostaje bez "
            "zmian. Niewielkie przesunięcia inwentaryzacyjne są ignorowane w "
            "ramach zadanej tolerancji. Wynikiem jest wyłącznie tymczasowa "
            "warstwa nowych fragmentów do dalszej weryfikacji."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        layout.addLayout(form)

        self.layer_combo = QComboBox()
        self._layers: list[QgsVectorLayer] = []
        model = _configured_model_pipe_layer()
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.geometryType() != Qgis.GeometryType.Line:
                continue
            if model is not None and layer.id() == model.id():
                continue
            self._layers.append(layer)
            self.layer_combo.addItem(layer.name(), layer.id())
        self.layer_combo.currentIndexChanged.connect(self._refresh_fields)
        form.addRow("Warstwa przewodów GIS:", self.layer_combo)

        self.gis_id_combo = QComboBox()
        form.addRow("ID GIS (tylko informacyjnie):", self.gis_id_combo)

        self.tolerance = QDoubleSpinBox()
        self.tolerance.setDecimals(3)
        self.tolerance.setRange(0.001, 1000.0)
        self.tolerance.setValue(0.50)
        self.tolerance.setSuffix(" m")
        self.tolerance.setToolTip(
            "Przesunięcie geometrii mniejsze od tej wartości traktowane jest "
            "jako ten sam przebieg sieci."
        )
        form.addRow("Tolerancja istniejącej sieci:", self.tolerance)

        self.node_tolerance = QDoubleSpinBox()
        self.node_tolerance.setDecimals(3)
        self.node_tolerance.setRange(0.001, 1000.0)
        self.node_tolerance.setValue(0.50)
        self.node_tolerance.setSuffix(" m")
        self.node_tolerance.setToolTip(
            "Jeśli koniec nowego fragmentu znajduje się w tej odległości od "
            "istniejącego węzła modelu, węzeł ma pierwszeństwo przed snapem "
            "do przewodu."
        )
        form.addRow("Tolerancja węzła modelu:", self.node_tolerance)

        self.minimum_fragment_length = QDoubleSpinBox()
        self.minimum_fragment_length.setDecimals(2)
        self.minimum_fragment_length.setRange(0.0, 10000.0)
        self.minimum_fragment_length.setValue(0.50)
        self.minimum_fragment_length.setSuffix(" m")
        self.minimum_fragment_length.setToolTip(
            "Krótsze fragmenty powstałe przez drobne różnice geometrii są pomijane."
        )
        form.addRow("Minimalna długość nowego fragmentu:", self.minimum_fragment_length)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh_fields()

    def _refresh_fields(self) -> None:
        self.gis_id_combo.clear()
        self.gis_id_combo.addItem("— bez pola ID —", None)
        layer = self.current_layer()
        if layer is None:
            return
        preferred = 0
        for i, field in enumerate(layer.fields(), start=1):
            self.gis_id_combo.addItem(field.name(), field.name())
            if field.name().lower() in {
                "id",
                "fid",
                "objectid",
                "id_przewodu",
                "idprzewodu",
            }:
                preferred = i
        self.gis_id_combo.setCurrentIndex(preferred)

    def current_layer(self) -> QgsVectorLayer | None:
        idx = self.layer_combo.currentIndex()
        if idx < 0 or idx >= len(self._layers):
            return None
        return self._layers[idx]

    def options(self) -> CompareOptions | None:
        gis_layer = self.current_layer()
        model = _configured_model_pipe_layer()
        if gis_layer is None or model is None:
            return None
        return CompareOptions(
            gis_layer=gis_layer,
            gis_id_field=self.gis_id_combo.currentData(),
            tolerance=float(self.tolerance.value()),
            minimum_fragment_length=float(self.minimum_fragment_length.value()),
            node_tolerance=float(self.node_tolerance.value()),
        )


def _candidate_ids(
    index: QgsSpatialIndex,
    geometry: QgsGeometry,
    tolerance: float,
) -> list[int]:
    rect = geometry.boundingBox()
    rect = QgsRectangle(
        rect.xMinimum() - tolerance,
        rect.yMinimum() - tolerance,
        rect.xMaximum() + tolerance,
        rect.yMaximum() + tolerance,
    )
    return index.intersects(rect)


def _safe_length(geometry: QgsGeometry) -> float:
    try:
        return max(0.0, float(geometry.length()))
    except Exception:
        return 0.0


def _local_model_union(
    candidate_ids: list[int],
    model_features: dict[int, QgsFeature],
) -> QgsGeometry | None:
    geometries: list[QgsGeometry] = []
    for fid in candidate_ids:
        feature = model_features.get(fid)
        if feature is None or not feature.hasGeometry():
            continue
        geom = feature.geometry()
        if geom.isEmpty():
            continue
        geometries.append(QgsGeometry(geom))
    if not geometries:
        return None
    if len(geometries) == 1:
        return geometries[0]
    try:
        return QgsGeometry.unaryUnion(geometries)
    except Exception:
        # collectGeometry jest bezpiecznym fallbackiem — do bufora nie potrzebujemy
        # rozpuszczonych granic pomiędzy liniami.
        return QgsGeometry.collectGeometry(geometries)


def _line_parts(geometry: QgsGeometry) -> list[QgsGeometry]:
    if geometry is None or geometry.isEmpty():
        return []
    try:
        parts = geometry.asGeometryCollection()
    except Exception:
        parts = []
    if not parts:
        parts = [geometry]
    result: list[QgsGeometry] = []
    for part in parts:
        if part is None or part.isEmpty():
            continue
        if part.type() != Qgis.GeometryType.Line:
            continue
        result.append(QgsGeometry(part))
    return result





def _build_model_node_index(
    model_crs,
) -> tuple[QgsSpatialIndex, dict[int, QgsPointXY]]:
    """Buduje wspólny indeks junctionów, rezerwuarów i zbiorników.

    Poszczególne warstwy mogą mieć te same FID-y, dlatego indeks dostaje
    własne syntetyczne identyfikatory. Wszystkie punkty są transformowane do
    CRS warstwy przewodów modelu.
    """
    project = QgsProject.instance()
    index = QgsSpatialIndex()
    points: dict[int, QgsPointXY] = {}
    synthetic_id = 1

    for layer in _configured_model_node_layers():
        transform = None
        if layer.crs() != model_crs:
            transform = QgsCoordinateTransform(
                layer.crs(),
                model_crs,
                project.transformContext(),
            )
        for feature in layer.getFeatures():
            if not feature.hasGeometry() or feature.geometry().isEmpty():
                continue
            geom = QgsGeometry(feature.geometry())
            try:
                if transform is not None:
                    geom.transform(transform)
                point = QgsPointXY(geom.asPoint())
            except Exception:
                continue
            indexed = QgsFeature()
            indexed.setId(synthetic_id)
            indexed.setGeometry(QgsGeometry.fromPointXY(point))
            index.addFeature(indexed)
            points[synthetic_id] = point
            synthetic_id += 1

    return index, points


def _best_node_snap_target(
    endpoint: QgsPointXY,
    interior_point: QgsPointXY,
    node_index: QgsSpatialIndex,
    node_points: dict[int, QgsPointXY],
    tolerance: float,
) -> QgsPointXY | None:
    """Wybiera istniejący węzeł modelu w pobliżu końcówki.

    Węzły mają pierwszeństwo przed przewodami. Gdy kilka węzłów leży w
    tolerancji, preferowany jest węzeł znajdujący się w naturalnym kierunku
    przedłużenia końcowego segmentu, a następnie najbliższy.
    """
    if tolerance <= 0:
        return None

    dx = float(endpoint.x() - interior_point.x())
    dy = float(endpoint.y() - interior_point.y())
    norm = (dx * dx + dy * dy) ** 0.5
    if norm <= 1e-12:
        return None
    ux, uy = dx / norm, dy / norm

    rect = QgsRectangle(
        endpoint.x() - tolerance,
        endpoint.y() - tolerance,
        endpoint.x() + tolerance,
        endpoint.y() + tolerance,
    )
    candidate_ids = node_index.intersects(rect)
    scored: list[tuple[float, float, QgsPointXY]] = []

    for node_id in candidate_ids:
        point = node_points.get(node_id)
        if point is None:
            continue
        vx = float(point.x() - endpoint.x())
        vy = float(point.y() - endpoint.y())
        distance = (vx * vx + vy * vy) ** 0.5
        if distance > tolerance + 1e-9:
            continue
        if distance <= 1e-12:
            return QgsPointXY(point)

        cosang = max(-1.0, min(1.0, (vx * ux + vy * uy) / distance))
        # Węzeł przed końcówką jest preferowany. Węzeł z boku nadal może
        # wygrać, jeśli jest bardzo blisko; węzeł "za" końcówką dostaje dużą
        # karę, by uniknąć cofania nowego przewodu.
        angle_penalty = 1.0 - cosang
        score = distance + tolerance * 0.65 * angle_penalty
        scored.append((score, distance, point))

    if not scored:
        return None
    scored.sort(key=lambda item: (item[0], item[1]))
    return QgsPointXY(scored[0][2])

def _candidate_diameter(feature: QgsFeature) -> float:
    """Zwraca średnicę przewodu modelowego, jeśli pole jest dostępne."""
    try:
        fields = feature.fields().names()
        if "diameter" not in fields:
            return 0.0
        value = feature["diameter"]
        return max(0.0, float(value))
    except Exception:
        return 0.0


def _nearest_intersection_point(
    ray: QgsGeometry,
    endpoint: QgsPointXY,
    candidate: QgsGeometry,
) -> tuple[QgsPointXY | None, float]:
    """Znajduje pierwsze przecięcie kandydata z przedłużeniem końcówki."""
    try:
        intersection = ray.intersection(candidate)
    except Exception:
        return None, float("inf")
    if intersection is None or intersection.isEmpty():
        return None, float("inf")

    endpoint_geom = QgsGeometry.fromPointXY(endpoint)
    try:
        nearest = intersection.nearestPoint(endpoint_geom)
        if nearest is None or nearest.isEmpty():
            return None, float("inf")
        point = nearest.asPoint()
        return QgsPointXY(point), float(endpoint_geom.distance(nearest))
    except Exception:
        return None, float("inf")


def _best_snap_target(
    endpoint: QgsPointXY,
    interior_point: QgsPointXY,
    candidate_ids: list[int],
    model_features: dict[int, QgsFeature],
    tolerance: float,
) -> QgsPointXY | None:
    """Wybiera właściwy przewód modelu dla końcówki nowego fragmentu.

    Nie wybieramy już po prostu najbliższej geometrii z unii modelu. Przy
    gęstej sieci taki wybór potrafił dociągać nowy odcinek do przyłącza, które
    przebiegało odrobinę bliżej niż właściwa sieć główna.

    Najpierw przedłużamy końcowy segment nowego fragmentu w jego naturalnym
    kierunku. Pierwszeństwo dostają przewody, które przecina to przedłużenie.
    Dopiero gdy nie ma takiego przecięcia, stosowany jest wybór po najbliższym
    punkcie z karą za odchylenie od kierunku przewodu. Średnica jest używana
    wyłącznie jako rozstrzygnięcie pomiędzy prawie równorzędnymi kandydatami.
    """
    dx = float(endpoint.x() - interior_point.x())
    dy = float(endpoint.y() - interior_point.y())
    norm = (dx * dx + dy * dy) ** 0.5
    if norm <= 1e-12:
        return None
    ux, uy = dx / norm, dy / norm

    # Końcówka po difference(buffer) znajduje się zwykle na granicy bufora.
    # Przedłużenie 2.5*tolerancji daje zapas na drobne przesunięcia danych.
    ray_length = max(tolerance * 2.5, 0.05)
    ray_end = QgsPointXY(
        endpoint.x() + ux * ray_length,
        endpoint.y() + uy * ray_length,
    )
    ray = QgsGeometry.fromPolylineXY([endpoint, ray_end])
    endpoint_geom = QgsGeometry.fromPointXY(endpoint)

    ray_hits: list[tuple[float, float, QgsPointXY]] = []
    fallback: list[tuple[float, float, float, QgsPointXY]] = []
    snap_limit = tolerance * 1.25 + 1e-9

    for fid in candidate_ids:
        feature = model_features.get(fid)
        if feature is None or not feature.hasGeometry():
            continue
        geom = feature.geometry()
        if geom is None or geom.isEmpty():
            continue

        diameter = _candidate_diameter(feature)

        hit_point, hit_distance = _nearest_intersection_point(
            ray, endpoint, geom
        )
        if hit_point is not None and hit_distance <= ray_length + 1e-9:
            ray_hits.append((hit_distance, diameter, hit_point))

        try:
            nearest = geom.nearestPoint(endpoint_geom)
            if nearest is None or nearest.isEmpty():
                continue
            distance = float(endpoint_geom.distance(nearest))
            if distance > snap_limit:
                continue
            nearest_point = QgsPointXY(nearest.asPoint())
        except Exception:
            continue

        vx = float(nearest_point.x() - endpoint.x())
        vy = float(nearest_point.y() - endpoint.y())
        vnorm = (vx * vx + vy * vy) ** 0.5
        if vnorm <= 1e-12:
            angle_penalty = 0.0
        else:
            cosang = max(-1.0, min(1.0, (vx * ux + vy * uy) / vnorm))
            # 0 = dokładnie przed końcówką, 1 = 90 stopni, 2 = za końcówką.
            angle_penalty = 1.0 - cosang
        score = distance + tolerance * 0.75 * angle_penalty
        fallback.append((score, distance, diameter, nearest_point))

    if ray_hits:
        ray_hits.sort(key=lambda item: item[0])
        best_distance = ray_hits[0][0]
        # Jeżeli kilka linii przecina naturalne przedłużenie praktycznie w tym
        # samym miejscu, preferujemy większą średnicę. To ogranicza snap do
        # przyłączy bez wymuszania sztywnego progu średnicy.
        close = [
            item for item in ray_hits
            if item[0] <= best_distance + max(0.05, tolerance * 0.20)
        ]
        close.sort(key=lambda item: (-item[1], item[0]))
        return close[0][2]

    if not fallback:
        return None

    fallback.sort(key=lambda item: item[0])
    best_score = fallback[0][0]
    close = [
        item for item in fallback
        if item[0] <= best_score + max(0.03, tolerance * 0.15)
    ]
    close.sort(key=lambda item: (-item[2], item[0], item[1]))
    return close[0][3]


def _snap_fragment_endpoints_to_model(
    fragment: QgsGeometry,
    candidate_ids: list[int],
    model_features: dict[int, QgsFeature],
    tolerance: float,
    node_index: QgsSpatialIndex,
    node_points: dict[int, QgsPointXY],
    node_tolerance: float,
) -> QgsGeometry:
    """Dociąga końce fragmentu do istniejącego węzła lub przewodu modelu.

    Istniejący węzeł ma pierwszeństwo. Dopiero gdy w promieniu tolerancji nie
    ma sensownego węzła, stosowany jest kierunkowy snap do przewodu.
    """
    if fragment is None or fragment.isEmpty() or tolerance <= 0:
        return QgsGeometry(fragment)

    try:
        polyline = fragment.asPolyline()
    except Exception:
        polyline = []

    if len(polyline) < 2:
        try:
            multi = fragment.asMultiPolyline()
        except Exception:
            multi = []
        if len(multi) != 1 or len(multi[0]) < 2:
            return QgsGeometry(fragment)
        polyline = multi[0]

    points = [QgsPointXY(point) for point in polyline]
    endpoint_pairs = (
        (0, 1),
        (len(points) - 1, len(points) - 2),
    )

    for endpoint_idx, interior_idx in endpoint_pairs:
        target = _best_node_snap_target(
            points[endpoint_idx],
            points[interior_idx],
            node_index,
            node_points,
            node_tolerance,
        )
        if target is None:
            target = _best_snap_target(
                points[endpoint_idx],
                points[interior_idx],
                candidate_ids,
                model_features,
                tolerance,
            )
        if target is not None:
            points[endpoint_idx] = target

    try:
        return QgsGeometry.fromPolylineXY(points)
    except Exception:
        return QgsGeometry(fragment)


def _new_fragments(
    gis_geometry: QgsGeometry,
    model_reference: QgsGeometry | None,
    tolerance: float,
    minimum_fragment_length: float,
) -> tuple[list[QgsGeometry], float]:
    """Zwraca nowe fragmenty GIS oraz procent długości już istniejącej w modelu."""
    total_length = _safe_length(gis_geometry)
    if total_length <= 0:
        return [], 0.0

    if model_reference is None or model_reference.isEmpty():
        raw_new = QgsGeometry(gis_geometry)
        coverage = 0.0
    else:
        try:
            corridor = model_reference.buffer(tolerance, 8)
            if corridor.isEmpty():
                raw_new = QgsGeometry(gis_geometry)
                coverage = 0.0
            else:
                covered = gis_geometry.intersection(corridor)
                covered_length = _safe_length(covered)
                coverage = max(
                    0.0,
                    min(100.0, 100.0 * covered_length / total_length),
                )
                raw_new = gis_geometry.difference(corridor)
        except Exception:
            # W razie problemu z operacją geometryczną nie uznajemy przewodu za
            # istniejący na siłę — bezpieczniej pokazać go do ręcznej weryfikacji.
            raw_new = QgsGeometry(gis_geometry)
            coverage = 0.0

    fragments = [
        part
        for part in _line_parts(raw_new)
        if _safe_length(part) >= minimum_fragment_length
    ]
    return fragments, coverage


def find_new_gis_fragments(
    options: CompareOptions,
    output_name: str = "Nowe fragmenty GIS do modelu EPANET",
    source_kind: str = "network",
) -> QgsVectorLayer:
    model = _configured_model_pipe_layer()
    if model is None:
        raise RuntimeError("Nie ustawiono warstwy przewodów modelu.")

    gis = options.gis_layer
    project = QgsProject.instance()
    transform = None
    if gis.crs() != model.crs():
        transform = QgsCoordinateTransform(
            gis.crs(),
            model.crs(),
            project.transformContext(),
        )

    model_features = {f.id(): f for f in model.getFeatures()}
    model_index = QgsSpatialIndex()
    for feature in model_features.values():
        if feature.hasGeometry() and not feature.geometry().isEmpty():
            model_index.addFeature(feature)

    node_index, node_points = _build_model_node_index(model.crs())

    output = QgsVectorLayer(
        f"MultiLineString?crs={model.crs().authid()}",
        output_name,
        "memory",
    )
    output.setCustomProperty("qgis_epanet_integrator/source_kind", source_kind)
    output.setCustomProperty("qgis_epanet_integrator/source_layer_id", gis.id())
    provider = output.dataProvider()
    provider.addAttributes(
        [
            QgsField("status", QVariant.String),
            QgsField("decyzja", QVariant.String),
            QgsField("komentarz", QVariant.String),
            QgsField("gis_id", QVariant.String),
            QgsField("gis_layer_id", QVariant.String),
            QgsField("gis_fid", QVariant.LongLong),
            QgsField("fragment", QVariant.Int),
            QgsField("pokrycie_pct", QVariant.Double),
            QgsField("dl_gis_m", QVariant.Double),
            QgsField("dl_nowa_m", QVariant.Double),
            QgsField("tolerancja_m", QVariant.Double),
            QgsField("tol_wezla_m", QVariant.Double),
            QgsField("uwaga", QVariant.String),
        ]
    )
    output.updateFields()

    out_features: list[QgsFeature] = []

    for gis_feature in gis.getFeatures():
        if not gis_feature.hasGeometry() or gis_feature.geometry().isEmpty():
            continue

        geom = QgsGeometry(gis_feature.geometry())
        if transform is not None:
            geom.transform(transform)
        if geom.type() != Qgis.GeometryType.Line:
            continue

        gis_id = ""
        if options.gis_id_field:
            raw = gis_feature[options.gis_id_field]
            if raw not in (None, ""):
                gis_id = str(raw)

        candidate_ids = _candidate_ids(model_index, geom, options.tolerance)
        model_reference = _local_model_union(candidate_ids, model_features)
        fragments, coverage = _new_fragments(
            geom,
            model_reference,
            options.tolerance,
            options.minimum_fragment_length,
        )
        if not fragments:
            # Przebieg już istnieje w modelu albo różnice są tylko drobnym szumem
            # geometrii. Nie dodajemy go do warstwy wynikowej.
            continue

        total_length = _safe_length(geom)
        total_new_length = sum(_safe_length(part) for part in fragments)

        if coverage <= 10.0:
            status = "NOWY"
            note = "Przebieg praktycznie nie występuje w istniejącym modelu"
        else:
            status = "CZĘŚCIOWO_NOWY"
            note = (
                "Część przebiegu jest już w modelu; pokazano wyłącznie fragment "
                "wychodzący poza istniejącą sieć. Końce fragmentu są najpierw "
                "dociągane do istniejących węzłów modelu, a dopiero potem do "
                "osi przewodów."
            )

        for part_no, fragment in enumerate(fragments, start=1):
            fragment = _snap_fragment_endpoints_to_model(
                fragment,
                candidate_ids,
                model_features,
                options.tolerance,
                node_index,
                node_points,
                options.node_tolerance,
            )
            fragment.convertToMultiType()
            out = QgsFeature(output.fields())
            out.setGeometry(fragment)
            out.setAttributes(
                [
                    status,
                    "DO_WERYFIKACJI",
                    "",
                    gis_id,
                    gis.id(),
                    int(gis_feature.id()),
                    part_no,
                    coverage,
                    total_length,
                    _safe_length(fragment),
                    options.tolerance,
                    options.node_tolerance,
                    note,
                ]
            )
            out_features.append(out)

    provider.addFeatures(out_features)
    output.updateExtents()
    project.addMapLayer(output)
    return output


# Zachowujemy nazwę funkcji używaną przez plugin.py, aby aktualizacja nie
# naruszała reszty interfejsu.
def compare_model_with_gis(options: CompareOptions) -> QgsVectorLayer:
    return find_new_gis_fragments(options)


def show_gis_compare(iface: QgisInterface) -> QgsVectorLayer | None:
    if _configured_model_pipe_layer() is None:
        QMessageBox.warning(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Najpierw ustaw warstwę przewodów modelu.",
        )
        return None

    dialog = GisCompareDialog(iface)
    if not dialog.exec():
        return None
    options = dialog.options()
    if options is None:
        return None
    try:
        layer = find_new_gis_fragments(options)
    except Exception as exc:
        QMessageBox.critical(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            f"Nie udało się przeanalizować warstwy GIS:\n{exc}",
        )
        return None

    iface.setActiveLayer(layer)
    if layer.featureCount() > 0:
        iface.zoomToActiveLayer()
    iface.messageBar().pushMessage(
        "Integrator QGIS-EPANET",
        (
            "Analiza zakończona. Znaleziono "
            f"{layer.featureCount()} nowych fragmentów GIS do weryfikacji."
        ),
        level=Qgis.MessageLevel.Success,
        duration=7,
    )
    return layer
