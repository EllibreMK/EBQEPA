from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsVectorLayer,
    QgsRasterLayer,
)
from qgis.gui import QgisInterface
from qgis.PyQt.QtWidgets import (
    QCheckBox,
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
from qgis_epanet_integrator.elevation_source import (
    ElevationLookupError,
    gugik_terrain_height,
    raster_terrain_height,
)


@dataclass
class ImportOptions:
    fragments_layer: QgsVectorLayer
    source_gis_layer: QgsVectorLayer | None
    diameter_field: str | None
    default_diameter: float
    default_roughness: float
    default_elevation: float
    elevation_source: str
    dem_raster: QgsRasterLayer | None
    burial_depth: float
    node_tolerance: float
    pipe_tolerance: float
    split_existing_pipes: bool
    create_missing_nodes: bool


def _configured_layer(model_layer: ModelLayer) -> QgsVectorLayer | None:
    saved = ProjectSettings().get(SettingKey.MODEL_LAYERS, {})
    layer_id = saved.get(model_layer.name)
    layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
    return layer if isinstance(layer, QgsVectorLayer) else None


def _configured_node_layers() -> list[QgsVectorLayer]:
    result: list[QgsVectorLayer] = []
    for model_layer in (ModelLayer.JUNCTIONS, ModelLayer.RESERVOIRS, ModelLayer.TANKS):
        layer = _configured_layer(model_layer)
        if layer is not None:
            result.append(layer)
    return result


def _is_fragments_layer(layer: QgsVectorLayer) -> bool:
    names = set(layer.fields().names())
    return layer.geometryType() == Qgis.GeometryType.Line and {
        "status", "decyzja", "gis_fid", "fragment"
    }.issubset(names)


class AddFragmentsDialog(QDialog):
    def __init__(self, iface: QgisInterface, fragments_layer: QgsVectorLayer):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.fragments_layer = fragments_layer
        self.setWindowTitle("Dodaj fragmenty GIS do modelu")
        self.resize(590, 390)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Narzędzie dodaje wybrane nowe fragmenty do warstwy przewodów modelu. "
            "Końcówki są najpierw dopasowywane do istniejących węzłów. Jeśli "
            "końcówka trafia w środek istniejącego przewodu, może zostać utworzony "
            "nowy węzeł i przewód modelu zostanie podzielony. Operacja modyfikuje "
            "warstwy modelu — przed pierwszym użyciem warto wykonać kopię projektu."
        )
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        layout.addLayout(form)

        self.source_combo = QComboBox()
        self.source_layers: list[QgsVectorLayer | None] = [None]
        self.source_combo.addItem("— bez warstwy źródłowej —", None)
        preferred_layer_id = self._preferred_source_layer_id()
        preferred_idx = 0
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.geometryType() != Qgis.GeometryType.Line:
                continue
            if layer.id() == fragments_layer.id():
                continue
            self.source_layers.append(layer)
            self.source_combo.addItem(layer.name(), layer.id())
            if preferred_layer_id and layer.id() == preferred_layer_id:
                preferred_idx = self.source_combo.count() - 1
        self.source_combo.setCurrentIndex(preferred_idx)
        self.source_combo.currentIndexChanged.connect(self._refresh_source_fields)
        form.addRow("Warstwa źródłowa GIS:", self.source_combo)

        self.diameter_field = QComboBox()
        form.addRow("Pole średnicy wewnętrznej GIS:", self.diameter_field)

        self.default_diameter = QDoubleSpinBox()
        self.default_diameter.setDecimals(2)
        self.default_diameter.setRange(0.01, 100000.0)
        self.default_diameter.setValue(110.0)
        self.default_diameter.setSuffix(" mm")
        self.default_diameter.setToolTip(
            "Wartość awaryjna używana tylko wtedy, gdy wybrane pole średnicy wewnętrznej "
            "jest puste, niepoprawne albo nie wskazano pola. EPANET wymaga średnicy "
            "hydraulicznej (wewnętrznej), nie średnicy zewnętrznej rury."
        )
        form.addRow("Średnica wewnętrzna domyślna:", self.default_diameter)

        self.default_roughness = QDoubleSpinBox()
        self.default_roughness.setDecimals(3)
        self.default_roughness.setRange(0.001, 100000.0)
        self.default_roughness.setValue(100.0)
        form.addRow("Szorstkość domyślna:", self.default_roughness)

        self.elevation_source = QComboBox()
        self.elevation_source.addItem("GUGiK NMT online", "gugik")
        self.elevation_source.addItem("Raster NMT z projektu", "raster")
        self.elevation_source.addItem("Wartość domyślna", "default")
        self.elevation_source.currentIndexChanged.connect(self._refresh_elevation_controls)
        form.addRow("Źródło rzędnej nowych węzłów:", self.elevation_source)

        self.dem_raster = QComboBox()
        self.dem_raster.addItem("— wybierz raster —", None)
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsRasterLayer) and layer.isValid():
                self.dem_raster.addItem(layer.name(), layer.id())
        form.addRow("Raster NMT:", self.dem_raster)

        self.burial_depth = QDoubleSpinBox()
        self.burial_depth.setDecimals(2)
        self.burial_depth.setRange(0.0, 20.0)
        self.burial_depth.setValue(1.50)
        self.burial_depth.setSuffix(" m")
        self.burial_depth.setToolTip(
            "Dla nowych, wolnych węzłów: rzędna EPANET = wysokość terenu NMT - głębokość."
        )
        form.addRow("Domyślna głębokość przewodu:", self.burial_depth)

        self.default_elevation = QDoubleSpinBox()
        self.default_elevation.setDecimals(3)
        self.default_elevation.setRange(-10000.0, 10000.0)
        self.default_elevation.setValue(0.0)
        self.default_elevation.setSuffix(" m")
        self.default_elevation.setToolTip(
            "Używana tylko po wybraniu źródła 'Wartość domyślna'."
        )
        form.addRow("Rzędna domyślna:", self.default_elevation)

        self.node_tolerance = QDoubleSpinBox()
        self.node_tolerance.setDecimals(3)
        self.node_tolerance.setRange(0.001, 1000.0)
        self.node_tolerance.setValue(self._suggested_value("tol_wezla_m", 0.50))
        self.node_tolerance.setSuffix(" m")
        form.addRow("Tolerancja istniejącego węzła:", self.node_tolerance)

        self.pipe_tolerance = QDoubleSpinBox()
        self.pipe_tolerance.setDecimals(3)
        self.pipe_tolerance.setRange(0.001, 1000.0)
        self.pipe_tolerance.setValue(self._suggested_value("tolerancja_m", 0.30))
        self.pipe_tolerance.setSuffix(" m")
        form.addRow("Tolerancja przewodu:", self.pipe_tolerance)

        self.split_pipes = QCheckBox("Dziel istniejący przewód, gdy nowe połączenie trafia w jego środek")
        self.split_pipes.setChecked(True)
        layout.addWidget(self.split_pipes)

        self.create_nodes = QCheckBox("Twórz brakujące węzły na końcach nowych fragmentów")
        self.create_nodes.setChecked(True)
        layout.addWidget(self.create_nodes)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._refresh_source_fields()
        self._refresh_elevation_controls()

    def _refresh_elevation_controls(self) -> None:
        mode = self.elevation_source.currentData()
        self.dem_raster.setEnabled(mode == "raster")
        self.burial_depth.setEnabled(mode in {"gugik", "raster"})
        self.default_elevation.setEnabled(mode == "default")

    def _current_dem_raster(self) -> QgsRasterLayer | None:
        layer_id = self.dem_raster.currentData()
        layer = QgsProject.instance().mapLayer(layer_id) if layer_id else None
        return layer if isinstance(layer, QgsRasterLayer) else None

    def _preferred_source_layer_id(self) -> str | None:
        if "gis_layer_id" not in self.fragments_layer.fields().names():
            return None
        values = set()
        for feature in self.fragments_layer.getFeatures():
            value = feature["gis_layer_id"]
            if value:
                values.add(str(value))
            if len(values) > 1:
                return None
        return next(iter(values), None)

    def _suggested_value(self, field_name: str, default: float) -> float:
        if field_name not in self.fragments_layer.fields().names():
            return default
        vals = []
        for feature in self.fragments_layer.getFeatures():
            try:
                vals.append(float(feature[field_name]))
            except (TypeError, ValueError):
                pass
            if len(vals) >= 50:
                break
        if not vals:
            return default
        vals.sort()
        return vals[len(vals) // 2]

    def current_source_layer(self) -> QgsVectorLayer | None:
        idx = self.source_combo.currentIndex()
        if idx <= 0 or idx >= len(self.source_layers):
            return None
        return self.source_layers[idx]

    @staticmethod
    def _normalized_field_name(name: str) -> str:
        text = unicodedata.normalize("NFKD", str(name or ""))
        text = "".join(ch for ch in text if not unicodedata.combining(ch))
        return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")

    def _refresh_source_fields(self) -> None:
        self.diameter_field.clear()
        self.diameter_field.addItem("— użyj wartości domyślnej —", None)
        self.diameter_field.setToolTip(
            "Wybierz pole zawierające gotową średnicę WEWNĘTRZNĄ rury w milimetrach. "
            "Wartość jest kopiowana 1:1 do pola diameter modelu EPANET — wtyczka nie "
            "przelicza średnicy zewnętrznej ani SDR."
        )
        layer = self.current_source_layer()
        if layer is None:
            return

        # Prefer a field that explicitly says it contains INTERNAL diameter.
        # This avoids accidentally feeding EPANET the outside/nominal diameter.
        preferred = 0
        fallback = 0
        for i, field in enumerate(layer.fields(), start=1):
            self.diameter_field.addItem(field.name(), field.name())
            normalized = self._normalized_field_name(field.name())

            internal_tokens = (
                "srednica_wewnetrzna", "srednica_wew", "wewnetrzna_srednica",
                "diameter_internal", "internal_diameter", "inside_diameter",
                "diameter_wew", "dn_wew", "di",
            )
            if any(token == normalized or token in normalized for token in internal_tokens):
                preferred = i
            elif normalized in {"srednica", "diameter", "dn"}:
                fallback = i

        self.diameter_field.setCurrentIndex(preferred or fallback)

    def options(self) -> ImportOptions:
        return ImportOptions(
            fragments_layer=self.fragments_layer,
            source_gis_layer=self.current_source_layer(),
            diameter_field=self.diameter_field.currentData(),
            default_diameter=float(self.default_diameter.value()),
            default_roughness=float(self.default_roughness.value()),
            default_elevation=float(self.default_elevation.value()),
            elevation_source=str(self.elevation_source.currentData() or "gugik"),
            dem_raster=self._current_dem_raster(),
            burial_depth=float(self.burial_depth.value()),
            node_tolerance=float(self.node_tolerance.value()),
            pipe_tolerance=float(self.pipe_tolerance.value()),
            split_existing_pipes=self.split_pipes.isChecked(),
            create_missing_nodes=self.create_nodes.isChecked(),
        )


def _selected_fragment_features(layer: QgsVectorLayer) -> list[QgsFeature]:
    selected = list(layer.selectedFeatures())
    if selected:
        return selected
    if "decyzja" in layer.fields().names():
        return [f for f in layer.getFeatures() if str(f["decyzja"] or "") == "DO_DODANIA"]
    return []


def _field_index(layer: QgsVectorLayer, field_name: str) -> int:
    return layer.fields().indexFromName(field_name)


def _set_if_present(feature: QgsFeature, field_name: str, value) -> None:
    idx = feature.fields().indexFromName(field_name)
    if idx >= 0:
        feature.setAttribute(idx, value)


def _existing_names(layer: QgsVectorLayer) -> set[str]:
    if "name" not in layer.fields().names():
        return set()
    return {str(f["name"]) for f in layer.getFeatures() if f["name"] not in (None, "")}


def _safe_token(value: object) -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^0-9A-Za-z_\-]+", "_", text)
    return text.strip("_")[:40]


def _unique_name(existing: set[str], preferred: str, prefix: str) -> str:
    base = _safe_token(preferred) or prefix
    if base not in existing:
        existing.add(base)
        return base
    i = 2
    while f"{base}_{i}" in existing:
        i += 1
    name = f"{base}_{i}"
    existing.add(name)
    return name


def _single_polylines(geometry: QgsGeometry) -> list[list[QgsPointXY]]:
    try:
        line = geometry.asPolyline()
    except Exception:
        line = []
    if len(line) >= 2:
        return [[QgsPointXY(p) for p in line]]
    try:
        multi = geometry.asMultiPolyline()
    except Exception:
        multi = []
    return [[QgsPointXY(p) for p in part] for part in multi if len(part) >= 2]


def _order_fragment_line_tasks(tasks: list[tuple[QgsFeature, int, list[QgsPointXY]]], tolerance: float):
    """Order a batch so parent/new trunk lines are added before branches.

    If an endpoint of task A lands on the *interior* of task B, A depends on B.
    Processing B first means it is already present in the model when A is handled,
    so the normal pipe-splitting logic can create a real hydraulic T-junction.

    This makes batch imports independent of feature/FID order.  Cycles or ambiguous
    groups are left in their original order and are still handled by the normal
    endpoint logic.
    """
    if len(tasks) < 2:
        return tasks

    geometries: dict[int, QgsGeometry] = {}
    index = QgsSpatialIndex()
    for task_id, (_, _, points) in enumerate(tasks, start=1):
        geom = QgsGeometry.fromPolylineXY(points)
        geometries[task_id] = geom
        f = QgsFeature()
        f.setId(task_id)
        f.setGeometry(geom)
        index.addFeature(f)

    dependencies: dict[int, set[int]] = {i: set() for i in geometries}
    for task_id, geom in geometries.items():
        parts = _single_polylines(geom)
        if len(parts) != 1 or len(parts[0]) < 2:
            continue
        for endpoint in (parts[0][0], parts[0][-1]):
            rect = QgsRectangle(
                endpoint.x() - tolerance, endpoint.y() - tolerance,
                endpoint.x() + tolerance, endpoint.y() + tolerance,
            )
            pgeom = QgsGeometry.fromPointXY(endpoint)
            best_target = None
            best_distance = float("inf")
            for candidate_id in index.intersects(rect):
                if candidate_id == task_id:
                    continue
                candidate = geometries.get(candidate_id)
                if candidate is None:
                    continue
                try:
                    nearest = candidate.nearestPoint(pgeom)
                    distance = float(pgeom.distance(nearest))
                    target = QgsPointXY(nearest.asPoint())
                except Exception:
                    continue
                if distance > tolerance:
                    continue
                # A connection to the endpoint of another pending line does not
                # require splitting that line, so it is not a processing dependency.
                if _is_near_line_endpoint(candidate, target, 1e-5):
                    continue
                if distance < best_distance:
                    best_target = candidate_id
                    best_distance = distance
            if best_target is not None:
                dependencies[task_id].add(best_target)

    # Stable topological sort: retain the user's/original order whenever possible.
    remaining = set(dependencies)
    ordered_ids: list[int] = []
    while remaining:
        ready = [i for i in sorted(remaining) if not (dependencies[i] & remaining)]
        if not ready:
            # Cyclic/ambiguous geometry. Do not guess: preserve original order.
            ordered_ids.extend(sorted(remaining))
            break
        ordered_ids.extend(ready)
        remaining.difference_update(ready)

    return [tasks[i - 1] for i in ordered_ids]


def _build_node_index(node_layers: list[QgsVectorLayer]) -> tuple[QgsSpatialIndex, dict[int, QgsPointXY]]:
    index = QgsSpatialIndex()
    points: dict[int, QgsPointXY] = {}
    sid = 1
    for layer in node_layers:
        for f in layer.getFeatures():
            if not f.hasGeometry() or f.geometry().isEmpty():
                continue
            try:
                p = QgsPointXY(f.geometry().asPoint())
            except Exception:
                continue
            tmp = QgsFeature()
            tmp.setId(sid)
            tmp.setGeometry(QgsGeometry.fromPointXY(p))
            index.addFeature(tmp)
            points[sid] = p
            sid += 1
    return index, points


def _nearest_node(point: QgsPointXY, index: QgsSpatialIndex, points: dict[int, QgsPointXY], tolerance: float) -> QgsPointXY | None:
    rect = QgsRectangle(point.x() - tolerance, point.y() - tolerance, point.x() + tolerance, point.y() + tolerance)
    best = None
    best_d = float("inf")
    for fid in index.intersects(rect):
        p = points.get(fid)
        if p is None:
            continue
        d = math.hypot(p.x() - point.x(), p.y() - point.y())
        if d <= tolerance and d < best_d:
            best = p
            best_d = d
    return QgsPointXY(best) if best is not None else None


def _build_pipe_index(pipe_layer: QgsVectorLayer) -> tuple[QgsSpatialIndex, dict[int, QgsFeature]]:
    features = {f.id(): f for f in pipe_layer.getFeatures()}
    index = QgsSpatialIndex()
    for f in features.values():
        if f.hasGeometry() and not f.geometry().isEmpty():
            index.addFeature(f)
    return index, features


def _nearest_pipe(point: QgsPointXY, pipe_layer: QgsVectorLayer, tolerance: float) -> tuple[QgsFeature | None, QgsPointXY | None, float]:
    index, features = _build_pipe_index(pipe_layer)
    rect = QgsRectangle(point.x() - tolerance, point.y() - tolerance, point.x() + tolerance, point.y() + tolerance)
    pgeom = QgsGeometry.fromPointXY(point)
    best_f = None
    best_p = None
    best_d = float("inf")
    for fid in index.intersects(rect):
        f = features.get(fid)
        if f is None:
            continue
        try:
            nearest = f.geometry().nearestPoint(pgeom)
            d = float(pgeom.distance(nearest))
            np = QgsPointXY(nearest.asPoint())
        except Exception:
            continue
        if d <= tolerance and d < best_d:
            best_f, best_p, best_d = f, np, d
    return best_f, best_p, best_d


def _split_polyline_at_point(geometry: QgsGeometry, point: QgsPointXY) -> tuple[QgsGeometry, QgsGeometry] | None:
    parts = _single_polylines(geometry)
    if len(parts) != 1:
        return None
    pts = parts[0]
    pgeom = QgsGeometry.fromPointXY(point)
    try:
        _, closest, after_vertex, _ = geometry.closestSegmentWithContext(point)
        split = QgsPointXY(closest)
    except Exception:
        return None
    if after_vertex <= 0 or after_vertex >= len(pts):
        return None

    eps = 1e-8
    if math.hypot(split.x() - pts[after_vertex - 1].x(), split.y() - pts[after_vertex - 1].y()) <= eps:
        idx = after_vertex - 1
        if idx <= 0 or idx >= len(pts) - 1:
            return None
        first = pts[: idx + 1]
        second = pts[idx:]
    elif math.hypot(split.x() - pts[after_vertex].x(), split.y() - pts[after_vertex].y()) <= eps:
        idx = after_vertex
        if idx <= 0 or idx >= len(pts) - 1:
            return None
        first = pts[: idx + 1]
        second = pts[idx:]
    else:
        first = pts[:after_vertex] + [split]
        second = [split] + pts[after_vertex:]

    if len(first) < 2 or len(second) < 2:
        return None
    return QgsGeometry.fromPolylineXY(first), QgsGeometry.fromPolylineXY(second)


def _is_near_line_endpoint(geometry: QgsGeometry, point: QgsPointXY, tolerance: float) -> bool:
    parts = _single_polylines(geometry)
    if len(parts) != 1:
        return False
    pts = parts[0]
    return min(
        math.hypot(point.x() - pts[0].x(), point.y() - pts[0].y()),
        math.hypot(point.x() - pts[-1].x(), point.y() - pts[-1].y()),
    ) <= tolerance




def _clear_primary_key_attributes(layer: QgsVectorLayer, feature: QgsFeature) -> None:
    """Leave provider-managed primary keys unset for newly created features.

    GeoPackage exposes its ``fid`` primary key as a normal QGIS attribute.
    Copying attributes from an existing feature (especially while the layer is
    in edit mode) can therefore copy a positive or temporary negative FID into
    a new feature.  On commit OGR then tries to INSERT that value and violates
    the UNIQUE constraint on ``fid``.
    """
    indexes = set(layer.dataProvider().pkAttributeIndexes())
    # Defensive fallback for providers which expose the GPKG key but do not
    # report it through pkAttributeIndexes().
    fid_idx = layer.fields().indexFromName("fid")
    if fid_idx >= 0:
        indexes.add(fid_idx)
    for idx in indexes:
        feature.setAttribute(idx, None)


def _commit_layer_or_raise(layer: QgsVectorLayer, action: str) -> None:
    if not layer.commitChanges():
        errors = "; ".join(layer.commitErrors())
        # QGIS/OGR can partially apply a commit. Do not hide that fact.
        raise RuntimeError(f"{action}: {errors}")


def _start_clean_edit(layer: QgsVectorLayer, action: str) -> None:
    if layer.isEditable():
        raise RuntimeError(
            f"Warstwa {layer.name()} jest już w trybie edycji. Zapisz albo wycofaj "
            "własne zmiany przed użyciem narzędzia — integrator nie będzie ich automatycznie zatwierdzał."
        )
    if not layer.startEditing():
        raise RuntimeError(f"Nie można rozpocząć edycji warstwy {layer.name()} ({action}).")



def _node_feature_elevation(feature: QgsFeature) -> float | None:
    for field_name in ("elevation",):
        if field_name in feature.fields().names():
            try:
                value = float(feature[field_name])
                if math.isfinite(value):
                    return value
            except (TypeError, ValueError):
                pass
    return None


def _elevation_at_existing_node(point: QgsPointXY, tolerance: float = 0.10) -> float | None:
    """Find an EPANET node elevation close to point. Reservoir base_head is
    intentionally not used as terrain/pipe elevation."""
    rect = QgsRectangle(point.x() - tolerance, point.y() - tolerance, point.x() + tolerance, point.y() + tolerance)
    best_value = None
    best_distance = float("inf")
    for model_layer in (ModelLayer.JUNCTIONS, ModelLayer.TANKS):
        layer = _configured_layer(model_layer)
        if layer is None:
            continue
        request = QgsFeatureRequest().setFilterRect(rect)
        for feature in layer.getFeatures(request):
            if not feature.hasGeometry() or feature.geometry().isEmpty():
                continue
            try:
                p = QgsPointXY(feature.geometry().asPoint())
            except Exception:
                continue
            d = math.hypot(p.x() - point.x(), p.y() - point.y())
            if d > tolerance or d >= best_distance:
                continue
            value = _node_feature_elevation(feature)
            if value is not None:
                best_value = value
                best_distance = d
    return best_value


def _interpolated_split_elevation(pipe: QgsFeature, target: QgsPointXY) -> float | None:
    parts = _single_polylines(pipe.geometry())
    if len(parts) != 1 or len(parts[0]) < 2:
        return None
    points = parts[0]
    start_elev = _elevation_at_existing_node(points[0])
    end_elev = _elevation_at_existing_node(points[-1])
    if start_elev is None or end_elev is None:
        return None
    total = float(pipe.geometry().length())
    if total <= 0:
        return None
    try:
        along = float(pipe.geometry().lineLocatePoint(QgsGeometry.fromPointXY(target)))
    except Exception:
        return None
    ratio = min(1.0, max(0.0, along / total))
    return start_elev + (end_elev - start_elev) * ratio


def _new_node_elevation(options: ImportOptions, point: QgsPointXY, node_layer: QgsVectorLayer,
                        split_pipe: QgsFeature | None = None) -> tuple[float, str]:
    if split_pipe is not None:
        interpolated = _interpolated_split_elevation(split_pipe, point)
        if interpolated is not None:
            return float(interpolated), "interpolacja modelu"

    if options.elevation_source == "gugik":
        terrain = gugik_terrain_height(point, node_layer.crs())
        return float(terrain - options.burial_depth), "GUGiK NMT"
    if options.elevation_source == "raster":
        if options.dem_raster is None:
            raise ElevationLookupError("Wybierz warstwę rastrową NMT z projektu.")
        terrain = raster_terrain_height(point, node_layer.crs(), options.dem_raster)
        return float(terrain - options.burial_depth), f"raster {options.dem_raster.name()}"
    return float(options.default_elevation), "wartość domyślna"

def _create_junction(layer: QgsVectorLayer, point: QgsPointXY, name: str, elevation: float) -> QgsFeature:
    """Create and immediately commit one junction.

    Committing each critical step separately is deliberate.  It prevents a later
    failed pipe insert from leaving a half-applied edit buffer containing geometry
    changes to existing pipes.
    """
    _start_clean_edit(layer, f"tworzenie węzła {name}")
    f = QgsFeature(layer.fields())
    f.setGeometry(QgsGeometry.fromPointXY(point))
    _set_if_present(f, "name", name)
    _set_if_present(f, "elevation", elevation)
    _set_if_present(f, "base_demand", 0.0)
    _clear_primary_key_attributes(layer, f)
    if not layer.addFeature(f):
        layer.rollBack()
        raise RuntimeError(f"Nie udało się utworzyć węzła {name}.")
    try:
        _commit_layer_or_raise(layer, f"Nie udało się zapisać węzła {name}")
    except Exception:
        if layer.isEditable():
            layer.rollBack()
        raise
    return f


def _delete_feature_by_name(layer: QgsVectorLayer, name: str) -> None:
    """Best-effort compensation used only after a failed split update."""
    if "name" not in layer.fields().names():
        return
    matches = [f.id() for f in layer.getFeatures() if str(f["name"] or "") == name]
    if not matches:
        return
    if layer.isEditable():
        layer.rollBack()
    if not layer.startEditing():
        return
    for fid in matches:
        layer.deleteFeature(fid)
    if not layer.commitChanges() and layer.isEditable():
        layer.rollBack()


def _split_existing_pipe(pipe_layer: QgsVectorLayer, pipe: QgsFeature, point: QgsPointXY, existing_pipe_names: set[str]) -> bool:
    """Split an existing pipe without ever shortening it before the new half exists.

    The old implementation changed the original geometry and added the second half
    in one edit buffer.  GeoPackage can partially commit such a buffer: geometry
    changes may be saved even when INSERTs fail.  That is exactly how a piece of an
    existing pipe can disappear.

    Safe order:
      1. add + commit the second half,
      2. only then shorten + commit the original half,
      3. if step 2 fails, remove the newly added duplicate half as compensation.
    """
    split = _split_polyline_at_point(pipe.geometry(), point)
    if split is None:
        return False
    first, second = split

    # Preserve the hydraulic length of the original pipe when it is split.
    # Copying the original `length` attribute to BOTH halves doubles the
    # hydraulic resistance seen by EPANET and can make the solver fail (Error 110).
    # Split an existing hydraulic length proportionally to geometric lengths.
    g1 = float(first.length())
    g2 = float(second.length())
    gsum = g1 + g2
    original_length = None
    if "length" in pipe.fields().names():
        try:
            candidate = float(pipe["length"])
            if math.isfinite(candidate) and candidate > 0:
                original_length = candidate
        except (TypeError, ValueError):
            pass
    if original_length is None:
        original_length = gsum if gsum > 0 else None

    first_length = None
    second_length = None
    if original_length is not None and gsum > 0:
        first_length = original_length * g1 / gsum
        second_length = original_length * g2 / gsum

    new_part = QgsFeature(pipe_layer.fields())
    new_part.setAttributes(pipe.attributes())
    _clear_primary_key_attributes(pipe_layer, new_part)
    new_part.setGeometry(second)
    old_name = str(pipe["name"] or "P") if "name" in pipe.fields().names() else "P"
    new_name = _unique_name(existing_pipe_names, f"{old_name}_B", "P_SPLIT")
    _set_if_present(new_part, "name", new_name)
    if second_length is not None:
        _set_if_present(new_part, "length", second_length)

    # Phase 1: the replacement geometry must exist before touching the original.
    _start_clean_edit(pipe_layer, f"dodawanie drugiej części przewodu {old_name}")
    if not pipe_layer.addFeature(new_part):
        pipe_layer.rollBack()
        raise RuntimeError(f"Nie udało się dodać drugiej części dzielonego przewodu {old_name}.")
    try:
        _commit_layer_or_raise(
            pipe_layer,
            f"Nie udało się zapisać drugiej części dzielonego przewodu {old_name}",
        )
    except Exception:
        if pipe_layer.isEditable():
            pipe_layer.rollBack()
        raise

    # Phase 2: only now shorten the original pipe and update its hydraulic length.
    _start_clean_edit(pipe_layer, f"skracanie pierwszej części przewodu {old_name}")
    if not pipe_layer.changeGeometry(pipe.id(), first):
        pipe_layer.rollBack()
        _delete_feature_by_name(pipe_layer, new_name)
        raise RuntimeError(f"Nie udało się zmienić geometrii pierwszej części przewodu {old_name}.")
    if first_length is not None and "length" in pipe_layer.fields().names():
        length_idx = pipe_layer.fields().indexFromName("length")
        if length_idx >= 0 and not pipe_layer.changeAttributeValue(pipe.id(), length_idx, first_length):
            pipe_layer.rollBack()
            _delete_feature_by_name(pipe_layer, new_name)
            raise RuntimeError(f"Nie udało się zaktualizować długości pierwszej części przewodu {old_name}.")
    try:
        _commit_layer_or_raise(
            pipe_layer,
            f"Nie udało się zapisać pierwszej części dzielonego przewodu {old_name}",
        )
    except Exception:
        if pipe_layer.isEditable():
            pipe_layer.rollBack()
        _delete_feature_by_name(pipe_layer, new_name)
        raise
    return True


def _add_new_pipe_committed(pipe_layer: QgsVectorLayer, feature: QgsFeature, pipe_name: str) -> None:
    _start_clean_edit(pipe_layer, f"dodawanie przewodu {pipe_name}")
    if not pipe_layer.addFeature(feature):
        pipe_layer.rollBack()
        raise RuntimeError(f"Nie udało się dodać przewodu {pipe_name}.")
    try:
        _commit_layer_or_raise(pipe_layer, f"Nie udało się zapisać przewodu {pipe_name}")
    except Exception:
        if pipe_layer.isEditable():
            pipe_layer.rollBack()
        raise

def _source_diameter(options: ImportOptions, fragment: QgsFeature) -> float:
    """Return the hydraulic/internal pipe diameter in millimetres.

    The selected GIS field is expected to already contain the INTERNAL diameter.
    Its value is copied directly to EPANET's ``diameter`` attribute.  We
    intentionally do not derive it from outside diameter/SDR here because that
    depends on material, pressure class and product series and can have exceptions.
    """
    if options.source_gis_layer is None or not options.diameter_field:
        return options.default_diameter
    if "gis_fid" not in fragment.fields().names():
        return options.default_diameter
    try:
        fid = int(fragment["gis_fid"])
        source = next(options.source_gis_layer.getFeatures(QgsFeatureRequest(fid)), None)
        if source is None:
            return options.default_diameter
        value = float(source[options.diameter_field])
        if math.isfinite(value) and value > 0:
            return value
        return options.default_diameter
    except (TypeError, ValueError, StopIteration):
        return options.default_diameter


def add_fragments_to_model(iface: QgisInterface, options: ImportOptions) -> dict[str, int]:
    pipe_layer = _configured_layer(ModelLayer.PIPES)
    junction_layer = _configured_layer(ModelLayer.JUNCTIONS)
    if pipe_layer is None or junction_layer is None:
        raise RuntimeError("Model musi mieć skonfigurowane warstwy Przewody i Węzły.")

    fragments = _selected_fragment_features(options.fragments_layer)
    if not fragments:
        raise RuntimeError(
            "Zaznacz fragmenty do dodania albo ustaw w polu 'decyzja' wartość DO_DODANIA."
        )

    # Safety rule: never mix our commits with edits the user already has open.
    for layer in (pipe_layer, junction_layer):
        if layer.isEditable():
            raise RuntimeError(
                f"Warstwa {layer.name()} ma niezapisane zmiany. Zapisz albo wycofaj je przed uruchomieniem narzędzia."
            )

    pipe_names = _existing_names(pipe_layer)
    node_names = _existing_names(junction_layer)
    node_layers = _configured_node_layers()
    created_pipes = 0
    created_nodes = 0
    split_pipes = 0
    updated_source = 0

    # Prepare all single-line jobs first.  Their database/FID order is not a
    # reliable construction order.  A later GIS branch may connect to the
    # interior of another line selected in the same batch, so parent lines must
    # be committed before branches that need to split them.
    tasks: list[tuple[QgsFeature, int, list[QgsPointXY]]] = []
    line_counts: dict[int, int] = {}
    for fragment_feature in fragments:
        if not fragment_feature.hasGeometry() or fragment_feature.geometry().isEmpty():
            continue
        lines = _single_polylines(fragment_feature.geometry())
        if not lines:
            continue
        line_counts[fragment_feature.id()] = len(lines)
        for line_no, points in enumerate(lines, start=1):
            if len(points) >= 2:
                tasks.append((fragment_feature, line_no, points))

    tasks = _order_fragment_line_tasks(tasks, options.pipe_tolerance)

    for fragment_feature, line_no, points in tasks:
        adjusted = [QgsPointXY(p) for p in points]

        for endpoint_idx in (0, len(adjusted) - 1):
            endpoint = adjusted[endpoint_idx]

            # Rebuild before EACH endpoint.  A node created while handling the
            # previous endpoint (or a previous line in this batch) must be
            # immediately visible to subsequent snapping decisions.
            node_index, node_points = _build_node_index(node_layers)
            existing_node = _nearest_node(endpoint, node_index, node_points, options.node_tolerance)
            if existing_node is not None:
                adjusted[endpoint_idx] = existing_node
                continue

            # _nearest_pipe builds a fresh index from the committed pipe layer,
            # therefore pipes added/split earlier in this same batch are visible.
            pipe, target, _ = _nearest_pipe(endpoint, pipe_layer, options.pipe_tolerance)
            if pipe is not None and target is not None:
                adjusted[endpoint_idx] = target
                if not _is_near_line_endpoint(pipe.geometry(), target, 1e-5):
                    if not options.split_existing_pipes:
                        raise RuntimeError(
                            "Końcówka nowego fragmentu trafia w środek istniejącego przewodu, "
                            "ale dzielenie przewodów jest wyłączone."
                        )
                    node_name = _unique_name(node_names, "", "J_GIS")
                    node_elevation, _ = _new_node_elevation(options, target, junction_layer, split_pipe=pipe)
                    _create_junction(junction_layer, target, node_name, node_elevation)
                    created_nodes += 1
                    if _split_existing_pipe(pipe_layer, pipe, target, pipe_names):
                        split_pipes += 1
                elif options.create_missing_nodes:
                    # The line endpoint exists geometrically, but EPANET needs a
                    # node there.  Re-check after previous batch operations before
                    # creating one to avoid duplicates.
                    node_index, node_points = _build_node_index(node_layers)
                    existing_node = _nearest_node(target, node_index, node_points, options.node_tolerance)
                    if existing_node is not None:
                        adjusted[endpoint_idx] = existing_node
                    else:
                        node_name = _unique_name(node_names, "", "J_GIS")
                        node_elevation, _ = _new_node_elevation(options, target, junction_layer)
                        _create_junction(junction_layer, target, node_name, node_elevation)
                        created_nodes += 1
                continue

            if options.create_missing_nodes:
                node_name = _unique_name(node_names, "", "J_GIS")
                node_elevation, _ = _new_node_elevation(options, endpoint, junction_layer)
                _create_junction(junction_layer, endpoint, node_name, node_elevation)
                created_nodes += 1
            else:
                raise RuntimeError(
                    "Końcówka nowego fragmentu nie ma w pobliżu istniejącego węzła ani przewodu."
                )

        if math.hypot(adjusted[0].x() - adjusted[-1].x(), adjusted[0].y() - adjusted[-1].y()) <= 1e-9:
            raise RuntimeError("Nowy przewód po dopasowaniu ma ten sam węzeł początkowy i końcowy.")

        new_pipe = QgsFeature(pipe_layer.fields())
        new_geometry = QgsGeometry.fromPolylineXY(adjusted)
        new_pipe.setGeometry(new_geometry)
        gis_id = str(fragment_feature["gis_id"] or "") if "gis_id" in fragment_feature.fields().names() else ""
        preferred = f"GIS_{_safe_token(gis_id)}" if gis_id else ""
        if line_counts.get(fragment_feature.id(), 1) > 1:
            preferred = f"{preferred}_{line_no}" if preferred else ""
        pipe_name = _unique_name(pipe_names, preferred, "P_GIS")
        _set_if_present(new_pipe, "name", pipe_name)
        _set_if_present(new_pipe, "diameter", _source_diameter(options, fragment_feature))
        if new_geometry.length() > 0:
            _set_if_present(new_pipe, "length", float(new_geometry.length()))
        _set_if_present(new_pipe, "roughness", options.default_roughness)
        _set_if_present(new_pipe, "minor_loss", 0.0)
        _set_if_present(new_pipe, "initial_status", "OPEN")
        _set_if_present(new_pipe, "check_valve", False)
        _clear_primary_key_attributes(pipe_layer, new_pipe)
        _add_new_pipe_committed(pipe_layer, new_pipe, pipe_name)
        created_pipes += 1

        # Mark the source work item only after the new model pipe is safely committed.
        if "decyzja" in options.fragments_layer.fields().names():
            idx = options.fragments_layer.fields().indexFromName("decyzja")
            options.fragments_layer.dataProvider().changeAttributeValues(
                {fragment_feature.id(): {idx: "DODANO_DO_MODELU"}}
            )
        if "komentarz" in options.fragments_layer.fields().names():
            idx = options.fragments_layer.fields().indexFromName("komentarz")
            current = str(fragment_feature["komentarz"] or "")
            msg = f"Dodano jako {pipe_name}"
            value = f"{current}; {msg}" if current else msg
            options.fragments_layer.dataProvider().changeAttributeValues(
                {fragment_feature.id(): {idx: value}}
            )
        updated_source += 1

    pipe_layer.updateExtents()
    junction_layer.updateExtents()
    pipe_layer.triggerRepaint()
    junction_layer.triggerRepaint()
    options.fragments_layer.triggerRepaint()
    iface.mapCanvas().refresh()

    return {
        "pipes": created_pipes,
        "nodes": created_nodes,
        "splits": split_pipes,
        "source": updated_source,
    }

def show_add_fragments_to_model(iface: QgisInterface) -> dict[str, int] | None:
    layer = iface.activeLayer()
    if not isinstance(layer, QgsVectorLayer) or not _is_fragments_layer(layer):
        candidates = [
            x for x in QgsProject.instance().mapLayers().values()
            if isinstance(x, QgsVectorLayer) and _is_fragments_layer(x)
        ]
        if len(candidates) == 1:
            layer = candidates[0]
        else:
            QMessageBox.warning(
                iface.mainWindow(),
                "Integrator QGIS-EPANET",
                "Ustaw jako aktywną warstwę 'Nowe fragmenty GIS do modelu EPANET'.",
            )
            return None

    count = len(_selected_fragment_features(layer))
    if count == 0:
        QMessageBox.information(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Najpierw zaznacz fragmenty do dodania lub ustaw im decyzję DO_DODANIA.",
        )
        return None

    dialog = AddFragmentsDialog(iface, layer)
    if not dialog.exec():
        return None

    answer = QMessageBox.question(
        iface.mainWindow(),
        "Dodaj fragmenty do modelu",
        f"Do modelu zostanie dodanych {count} wskazanych fragmentów. Operacja może "
        "tworzyć nowe węzły i dzielić istniejące przewody. Kontynuować?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return None

    try:
        result = add_fragments_to_model(iface, dialog.options())
    except Exception as exc:
        QMessageBox.critical(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            f"Nie udało się dodać fragmentów do modelu:\n{exc}",
        )
        return None

    iface.messageBar().pushMessage(
        "Integrator QGIS-EPANET",
        (
            f"Dodano {result['pipes']} przewodów i {result['nodes']} węzłów; "
            f"podzielono {result['splits']} istniejących przewodów."
        ),
        level=Qgis.MessageLevel.Success,
        duration=8,
    )
    return result
