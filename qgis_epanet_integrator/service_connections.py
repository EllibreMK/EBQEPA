from __future__ import annotations

from dataclasses import dataclass

from qgis.core import (
    Qgis,
    QgsFeature,
    QgsField,
    QgsGeometry,
    QgsPointXY,
    QgsProject,
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

from qgis_epanet_integrator.gis_compare import (
    CompareOptions,
    _configured_model_pipe_layer,
    find_new_gis_fragments,
)
from qgis_epanet_integrator.gis_import import (
    AddFragmentsDialog,
    _configured_node_layers,
    add_fragments_to_model,
)
from qgis_epanet_integrator.service_mapping import save_endpoint_mapping


@dataclass
class ServiceConnectionOptions:
    compare: CompareOptions
    grouping_tolerance: float


class ServiceConnectionsDialog(QDialog):
    """Analyze only service-connection fragments missing from the model.

    New fragments are grouped into small connected components. Their terminal
    points are also exposed in a second temporary layer. This creates a stable
    place for the future billing workflow: customer/consumption records will be
    assigned to hydraulic nodes created at those terminal points, not to pipes.
    """

    def __init__(self, iface: QgisInterface):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.setWindowTitle("Przyłącza GIS – nowe grupy i końcówki")
        self.resize(660, 390)

        layout = QVBoxLayout(self)
        info = QLabel(
            "Wybierz warstwę liniową przyłączy GIS. Narzędzie wyszuka tylko "
            "fragmenty, których nie ma jeszcze w modelu EPANET, a następnie "
            "połączy stykające się segmenty w spójne grupy. Dodatkowo utworzy "
            "warstwę punktową końcówek grup. W przyszłości dane billingowe / "
            "rozbiory będą przypisywane do węzłów hydraulicznych powstałych "
            "właśnie w tych punktach."
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
        form.addRow("Warstwa przyłączy GIS:", self.layer_combo)

        self.id_combo = QComboBox()
        form.addRow("ID przyłącza / obiektu GIS:", self.id_combo)

        self.tolerance = QDoubleSpinBox()
        self.tolerance.setDecimals(3)
        self.tolerance.setRange(0.001, 1000.0)
        self.tolerance.setValue(0.50)
        self.tolerance.setSuffix(" m")
        self.tolerance.setToolTip(
            "Odchylenie geometrii przyłącza od modelu mniejsze od tej wartości "
            "traktowane jest jako istniejący przebieg."
        )
        form.addRow("Tolerancja istniejącego modelu:", self.tolerance)

        self.node_tolerance = QDoubleSpinBox()
        self.node_tolerance.setDecimals(3)
        self.node_tolerance.setRange(0.001, 1000.0)
        self.node_tolerance.setValue(0.50)
        self.node_tolerance.setSuffix(" m")
        form.addRow("Tolerancja węzła modelu:", self.node_tolerance)

        self.grouping_tolerance = QDoubleSpinBox()
        self.grouping_tolerance.setDecimals(3)
        self.grouping_tolerance.setRange(0.001, 10.0)
        self.grouping_tolerance.setValue(0.20)
        self.grouping_tolerance.setSuffix(" m")
        self.grouping_tolerance.setToolTip(
            "Maksymalna przerwa między końcami segmentów, aby uznać je za jedną "
            "grupę przyłącza/podsieci. Celowo mniejsza od tolerancji modelu, aby "
            "nie sklejać równoległych przyłączy."
        )
        form.addRow("Tolerancja grupowania segmentów:", self.grouping_tolerance)

        self.minimum_length = QDoubleSpinBox()
        self.minimum_length.setDecimals(2)
        self.minimum_length.setRange(0.0, 10000.0)
        self.minimum_length.setValue(0.30)
        self.minimum_length.setSuffix(" m")
        self.minimum_length.setToolTip(
            "Krótsze resztki geometrii będą pomijane. Dla przyłączy domyślna "
            "wartość jest nieco mniejsza niż dla sieci głównej."
        )
        form.addRow("Minimalna długość nowego fragmentu:", self.minimum_length)

        hint = QLabel(
            "Warstwa wynikowa fragmentów otrzyma pola grupa, seg_grupy i "
            "dl_grupy_m. Druga warstwa „Końcówki nowych przyłączy GIS” pokaże "
            "punkty wpięcia do modelu oraz wolne końce odbiorców. Wolny koniec "
            "jest przyszłym kandydatem na węzeł z rozbiorem."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self._refresh_fields()

    def current_layer(self) -> QgsVectorLayer | None:
        idx = self.layer_combo.currentIndex()
        if idx < 0 or idx >= len(self._layers):
            return None
        return self._layers[idx]

    def _refresh_fields(self) -> None:
        self.id_combo.clear()
        self.id_combo.addItem("— bez pola ID —", None)
        layer = self.current_layer()
        if layer is None:
            return
        preferred = 0
        for i, field in enumerate(layer.fields(), start=1):
            name = field.name()
            self.id_combo.addItem(name, name)
            if name.lower() in {
                "id", "fid", "objectid", "id_przylacza", "idprzylacza",
                "nr_przylacza", "numer_przylacza",
            }:
                preferred = i
        self.id_combo.setCurrentIndex(preferred)

    def options(self) -> ServiceConnectionOptions | None:
        layer = self.current_layer()
        if layer is None:
            return None
        return ServiceConnectionOptions(
            compare=CompareOptions(
                gis_layer=layer,
                gis_id_field=self.id_combo.currentData(),
                tolerance=float(self.tolerance.value()),
                minimum_fragment_length=float(self.minimum_length.value()),
                node_tolerance=float(self.node_tolerance.value()),
            ),
            grouping_tolerance=float(self.grouping_tolerance.value()),
        )


def _endpoints(geometry: QgsGeometry) -> tuple[QgsPointXY, QgsPointXY] | None:
    if geometry is None or geometry.isEmpty():
        return None
    try:
        if geometry.isMultipart():
            parts = geometry.asMultiPolyline()
            if not parts:
                return None
            # New-fragment output normally contains one part. If a provider
            # preserved several, use the longest one to get stable terminals.
            part = max(parts, key=lambda pts: len(pts))
        else:
            part = geometry.asPolyline()
        if len(part) < 2:
            return None
        return QgsPointXY(part[0]), QgsPointXY(part[-1])
    except Exception:
        return None


def _feature_components(layer: QgsVectorLayer, tolerance: float) -> list[list[int]]:
    """Return connected feature-id groups for new service fragments."""
    features = {f.id(): QgsFeature(f) for f in layer.getFeatures()}
    index = QgsSpatialIndex()
    for feature in features.values():
        if feature.hasGeometry() and not feature.geometry().isEmpty():
            index.addFeature(feature)

    adjacency: dict[int, set[int]] = {fid: set() for fid in features}
    for fid, feature in features.items():
        geom = feature.geometry()
        box = geom.boundingBox()
        box.grow(tolerance)
        for other_id in index.intersects(box):
            if other_id == fid or other_id not in features:
                continue
            other = features[other_id]
            try:
                if geom.distance(other.geometry()) <= tolerance:
                    adjacency[fid].add(other_id)
                    adjacency[other_id].add(fid)
            except Exception:
                continue

    components: list[list[int]] = []
    unseen = set(features)
    while unseen:
        start = unseen.pop()
        stack = [start]
        component = [start]
        while stack:
            current = stack.pop()
            for neighbor in adjacency[current]:
                if neighbor in unseen:
                    unseen.remove(neighbor)
                    stack.append(neighbor)
                    component.append(neighbor)
        components.append(sorted(component))
    return components


def _model_index(model: QgsVectorLayer) -> tuple[QgsSpatialIndex, dict[int, QgsFeature]]:
    features = {f.id(): QgsFeature(f) for f in model.getFeatures()}
    index = QgsSpatialIndex()
    for feature in features.values():
        if feature.hasGeometry() and not feature.geometry().isEmpty():
            index.addFeature(feature)
    return index, features


def _point_near_model(
    point: QgsPointXY,
    index: QgsSpatialIndex,
    features: dict[int, QgsFeature],
    tolerance: float,
) -> bool:
    geom = QgsGeometry.fromPointXY(point)
    box = geom.boundingBox()
    box.grow(tolerance)
    for fid in index.intersects(box):
        feature = features.get(fid)
        if feature is None:
            continue
        try:
            if geom.distance(feature.geometry()) <= tolerance:
                return True
        except Exception:
            continue
    return False


def _cluster_endpoints(
    layer: QgsVectorLayer,
    feature_ids: list[int],
    tolerance: float,
) -> list[dict]:
    """Cluster endpoints inside one connected group.

    Each cluster stores its representative point and the number of incident
    fragment ends. One incident end means a true terminal candidate; two or
    more normally means an internal connection/branch junction.
    """
    clusters: list[dict] = []
    for fid in feature_ids:
        feature = layer.getFeature(fid)
        endpoints = _endpoints(feature.geometry())
        if endpoints is None:
            continue
        for point in endpoints:
            matched = None
            for cluster in clusters:
                if QgsGeometry.fromPointXY(point).distance(
                    QgsGeometry.fromPointXY(cluster["point"])
                ) <= tolerance:
                    matched = cluster
                    break
            if matched is None:
                clusters.append({"point": point, "incidence": 1, "fids": {fid}})
            else:
                matched["incidence"] += 1
                matched["fids"].add(fid)
    return clusters


def _postprocess_service_connections(
    fragments: QgsVectorLayer,
    options: ServiceConnectionOptions,
) -> QgsVectorLayer:
    project = QgsProject.instance()
    model = _configured_model_pipe_layer()
    if model is None:
        return fragments

    provider = fragments.dataProvider()
    existing_names = {field.name() for field in fragments.fields()}
    new_fields = []
    for field in (
        QgsField("grupa", QVariant.String),
        QgsField("seg_grupy", QVariant.Int),
        QgsField("dl_grupy_m", QVariant.Double),
        QgsField("koncowki", QVariant.Int),
        QgsField("wpięcia", QVariant.Int),
    ):
        if field.name() not in existing_names:
            new_fields.append(field)
    if new_fields:
        provider.addAttributes(new_fields)
        fragments.updateFields()

    components = _feature_components(fragments, options.grouping_tolerance)
    model_idx, model_features = _model_index(model)

    endpoint_layer = QgsVectorLayer(
        f"Point?crs={fragments.crs().authid()}",
        "Końcówki nowych przyłączy GIS",
        "memory",
    )
    endpoint_layer.setCustomProperty(
        "qgis_epanet_integrator/source_kind", "service_connection_endpoints"
    )
    endpoint_layer.setCustomProperty(
        "qgis_epanet_integrator/source_layer_id",
        options.compare.gis_layer.id(),
    )
    ep_provider = endpoint_layer.dataProvider()
    ep_provider.addAttributes(
        [
            QgsField("grupa", QVariant.String),
            QgsField("rola", QVariant.String),
            QgsField("incydencja", QVariant.Int),
            QgsField("gis_id", QVariant.String),
            QgsField("gis_fid", QVariant.LongLong),
            QgsField("node_id", QVariant.String),
            QgsField("CID", QVariant.String),
            QgsField("uwaga", QVariant.String),
        ]
    )
    endpoint_layer.updateFields()

    changes: dict[int, dict[int, object]] = {}
    endpoint_features: list[QgsFeature] = []

    idx_group = fragments.fields().indexFromName("grupa")
    idx_count = fragments.fields().indexFromName("seg_grupy")
    idx_length = fragments.fields().indexFromName("dl_grupy_m")
    idx_ends = fragments.fields().indexFromName("koncowki")
    idx_connections = fragments.fields().indexFromName("wpięcia")

    for group_no, fids in enumerate(components, start=1):
        group_id = f"PRZ_{group_no:04d}"
        total_length = 0.0
        for fid in fids:
            feature = fragments.getFeature(fid)
            if feature.hasGeometry():
                total_length += feature.geometry().length()

        clusters = _cluster_endpoints(
            fragments,
            fids,
            options.grouping_tolerance,
        )
        terminal_count = sum(1 for c in clusters if c["incidence"] == 1)
        model_connection_count = 0

        for cluster in clusters:
            point = cluster["point"]
            near_model = _point_near_model(
                point,
                model_idx,
                model_features,
                options.compare.tolerance,
            )
            if near_model:
                role = "WPIECIE_DO_MODELU"
                model_connection_count += 1
                note = "Punkt grupy leży przy istniejącym przewodzie modelu."
            elif cluster["incidence"] == 1:
                role = "KONIEC_ODBIORCY"
                note = (
                    "Wolny koniec nowej grupy. Po dodaniu do modelu powstały "
                    "tu węzeł jest kandydatem do przypisania rozbioru/billingu."
                )
            else:
                role = "WEZEL_WEWNETRZNY"
                note = "Połączenie segmentów wewnątrz nowej grupy przyłączy."

            source_gis_ids: list[str] = []
            source_gis_fids: list[int] = []
            for source_fid in sorted(cluster["fids"]):
                source_feature = fragments.getFeature(source_fid)
                if "gis_id" in source_feature.fields().names():
                    value = str(source_feature["gis_id"] or "").strip()
                    if value and value not in source_gis_ids:
                        source_gis_ids.append(value)
                if "gis_fid" in source_feature.fields().names():
                    try:
                        value = int(source_feature["gis_fid"])
                        if value not in source_gis_fids:
                            source_gis_fids.append(value)
                    except Exception:
                        pass

            # A true terminal normally belongs to one source GIS feature. For
            # internal junctions several IDs may meet, so keep a readable list.
            gis_id_value = ";".join(source_gis_ids)
            gis_fid_value = source_gis_fids[0] if len(source_gis_fids) == 1 else None

            ep = QgsFeature(endpoint_layer.fields())
            ep.setGeometry(QgsGeometry.fromPointXY(point))
            ep.setAttributes(
                [
                    group_id,
                    role,
                    int(cluster["incidence"]),
                    gis_id_value,
                    gis_fid_value,
                    "",
                    "",
                    note,
                ]
            )
            endpoint_features.append(ep)

        for fid in fids:
            changes[fid] = {
                idx_group: group_id,
                idx_count: len(fids),
                idx_length: total_length,
                idx_ends: terminal_count,
                idx_connections: model_connection_count,
            }

    if changes:
        provider.changeAttributeValues(changes)
    fragments.updateExtents()

    ep_provider.addFeatures(endpoint_features)
    endpoint_layer.updateExtents()
    project.addMapLayer(endpoint_layer)

    fragments.setCustomProperty(
        "qgis_epanet_integrator/service_endpoint_layer_id",
        endpoint_layer.id(),
    )
    endpoint_layer.setCustomProperty(
        "qgis_epanet_integrator/service_fragment_layer_id",
        fragments.id(),
    )
    return fragments


def show_service_connections_foundation(iface: QgisInterface) -> QgsVectorLayer | None:
    if _configured_model_pipe_layer() is None:
        QMessageBox.warning(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Najpierw ustaw warstwę przewodów modelu.",
        )
        return None

    dialog = ServiceConnectionsDialog(iface)
    if not dialog.exec():
        return None
    options = dialog.options()
    if options is None:
        return None

    try:
        layer = find_new_gis_fragments(
            options.compare,
            output_name="Nowe fragmenty przyłączy GIS",
            source_kind="service_connections",
        )
        layer = _postprocess_service_connections(layer, options)
        iface.setActiveLayer(layer)
        if layer.featureCount() > 0:
            iface.zoomToActiveLayer()
        iface.messageBar().pushMessage(
            "Integrator QGIS-EPANET",
            (
                f"Znaleziono {layer.featureCount()} nowych fragmentów przyłączy. "
                "Utworzono grupy oraz warstwę punktową końcówek pod przyszłe "
                "przypisanie rozbiorów do węzłów."
            ),
            level=Qgis.MessageLevel.Success,
            duration=8,
        )
        return layer
    except Exception as exc:
        QMessageBox.critical(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            f"Nie udało się przeanalizować warstwy przyłączy GIS:\n{exc}",
        )
        return None



def _is_service_fragments_layer(layer: QgsVectorLayer) -> bool:
    return (
        isinstance(layer, QgsVectorLayer)
        and layer.geometryType() == Qgis.GeometryType.Line
        and str(layer.customProperty("qgis_epanet_integrator/source_kind", "")) == "service_connections"
        and "grupa" in layer.fields().names()
    )


def _service_fragments_layer(iface: QgisInterface) -> QgsVectorLayer | None:
    active = iface.activeLayer()
    if isinstance(active, QgsVectorLayer) and _is_service_fragments_layer(active):
        return active
    candidates = [
        layer for layer in QgsProject.instance().mapLayers().values()
        if isinstance(layer, QgsVectorLayer) and _is_service_fragments_layer(layer)
    ]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _selected_service_groups(layer: QgsVectorLayer) -> list[str]:
    groups: set[str] = set()
    selected = list(layer.selectedFeatures())
    if selected:
        for feature in selected:
            value = str(feature["grupa"] or "").strip()
            if value:
                groups.add(value)
        return sorted(groups)

    if "decyzja" in layer.fields().names():
        for feature in layer.getFeatures():
            if str(feature["decyzja"] or "") != "DO_DODANIA":
                continue
            value = str(feature["grupa"] or "").strip()
            if value:
                groups.add(value)
    return sorted(groups)


def _select_whole_service_groups(layer: QgsVectorLayer, group_ids: list[str]) -> int:
    wanted = set(group_ids)
    fids = [
        feature.id()
        for feature in layer.getFeatures()
        if str(feature["grupa"] or "").strip() in wanted
    ]
    layer.selectByIds(fids)
    return len(fids)


def _endpoint_layer_for_fragments(fragments: QgsVectorLayer) -> QgsVectorLayer | None:
    layer_id = str(
        fragments.customProperty(
            "qgis_epanet_integrator/service_endpoint_layer_id", ""
        )
        or ""
    )
    if layer_id:
        layer = QgsProject.instance().mapLayer(layer_id)
        if isinstance(layer, QgsVectorLayer):
            return layer

    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if str(layer.customProperty("qgis_epanet_integrator/source_kind", "")) != "service_connection_endpoints":
            continue
        if str(layer.customProperty("qgis_epanet_integrator/service_fragment_layer_id", "")) == fragments.id():
            return layer
    return None


def _nearest_model_node_name(point: QgsPointXY, tolerance: float) -> str | None:
    pgeom = QgsGeometry.fromPointXY(point)
    best_name = None
    best_distance = float("inf")
    for layer in _configured_node_layers():
        for feature in layer.getFeatures():
            if not feature.hasGeometry() or feature.geometry().isEmpty():
                continue
            try:
                distance = float(pgeom.distance(feature.geometry()))
            except Exception:
                continue
            if distance > tolerance or distance >= best_distance:
                continue
            name = ""
            if "name" in feature.fields().names():
                name = str(feature["name"] or "").strip()
            if name:
                best_name = name
                best_distance = distance
    return best_name


def _update_service_endpoint_node_ids(
    fragments: QgsVectorLayer,
    group_ids: list[str],
    tolerance: float,
) -> int:
    endpoint_layer = _endpoint_layer_for_fragments(fragments)
    if endpoint_layer is None or "node_id" not in endpoint_layer.fields().names():
        return 0

    wanted = set(group_ids)
    idx_node = endpoint_layer.fields().indexFromName("node_id")
    idx_note = endpoint_layer.fields().indexFromName("uwaga")
    changes: dict[int, dict[int, object]] = {}
    updated = 0

    for feature in endpoint_layer.getFeatures():
        if str(feature["grupa"] or "").strip() not in wanted:
            continue
        if not feature.hasGeometry() or feature.geometry().isEmpty():
            continue
        try:
            point = QgsPointXY(feature.geometry().asPoint())
        except Exception:
            continue
        node_name = _nearest_model_node_name(point, tolerance)
        if not node_name:
            continue
        values: dict[int, object] = {idx_node: node_name}
        if idx_note >= 0:
            current = str(feature["uwaga"] or "")
            suffix = f"Węzeł modelu: {node_name}."
            values[idx_note] = f"{current} {suffix}".strip()
        changes[feature.id()] = values
        updated += 1

    if changes:
        endpoint_layer.dataProvider().changeAttributeValues(changes)
        endpoint_layer.triggerRepaint()
    return updated




def _feature_name(feature: QgsFeature) -> str:
    if "name" not in feature.fields().names():
        return ""
    return str(feature["name"] or "").strip()


def _current_model_names() -> tuple[set[str], set[str]]:
    """Return current pipe and node names from configured hydraulic layers."""
    pipe_names: set[str] = set()
    model = _configured_model_pipe_layer()
    if model is not None:
        for feature in model.getFeatures():
            name = _feature_name(feature)
            if name:
                pipe_names.add(name)

    node_names: set[str] = set()
    for node_layer in _configured_node_layers():
        for feature in node_layer.getFeatures():
            name = _feature_name(feature)
            if name:
                node_names.add(name)
    return pipe_names, node_names


def _line_endpoints(geometry: QgsGeometry) -> tuple[QgsPointXY, QgsPointXY] | None:
    """Return first/last point of a model line geometry."""
    return _endpoints(geometry)


def _new_node_on_pipe_interior_issues(
    new_node_names: set[str],
    tolerance: float,
) -> list[dict]:
    """Find new nodes that visually lie on a pipe but are not its endpoint.

    This catches the dangerous case where a junction is placed on a bend or in
    the middle of a pipe geometry without actually splitting that pipe.  Such a
    model can look connected on the map while EPANET sees two separate graphs.
    """
    model = _configured_model_pipe_layer()
    if model is None or not new_node_names:
        return []

    pipe_index, pipe_features = _model_index(model)
    audit_tol = max(0.002, min(0.02, float(tolerance) * 0.05))
    issues: list[dict] = []

    for node_layer in _configured_node_layers():
        for node in node_layer.getFeatures():
            node_name = _feature_name(node)
            if node_name not in new_node_names:
                continue
            if not node.hasGeometry() or node.geometry().isEmpty():
                continue
            try:
                point = QgsPointXY(node.geometry().asPoint())
            except Exception:
                continue
            pgeom = QgsGeometry.fromPointXY(point)
            box = pgeom.boundingBox()
            box.grow(audit_tol)

            for pipe_fid in pipe_index.intersects(box):
                pipe = pipe_features.get(pipe_fid)
                if pipe is None or not pipe.hasGeometry() or pipe.geometry().isEmpty():
                    continue
                try:
                    if pgeom.distance(pipe.geometry()) > audit_tol:
                        continue
                except Exception:
                    continue

                ends = _line_endpoints(pipe.geometry())
                if ends is None:
                    continue
                e1 = QgsGeometry.fromPointXY(ends[0])
                e2 = QgsGeometry.fromPointXY(ends[1])
                if min(pgeom.distance(e1), pgeom.distance(e2)) <= audit_tol:
                    continue

                pipe_name = _feature_name(pipe) or f"FID {pipe.id()}"
                issues.append(
                    {
                        "typ": "WEZEL_NA_SRODKU_PRZEWODU",
                        "node_id": node_name,
                        "pipe_id": pipe_name,
                        "point": point,
                        "uwaga": (
                            "Nowy węzeł leży na przebiegu przewodu, ale przewód nie kończy się "
                            "w tym węźle. Wizualnie wygląda jak połączenie, lecz hydraulicznie "
                            "przewód prawdopodobnie nie został podzielony."
                        ),
                    }
                )
    return issues


def _disconnected_new_pipe_component_issues(
    previous_pipe_names: set[str],
    new_pipe_names: set[str],
    tolerance: float,
) -> list[dict]:
    """Find components made only of newly added pipes.

    Pipe endpoints are clustered geometrically.  A new component is considered
    connected when it reaches at least one pipe that existed before the import.
    A correctly split old main keeps one old pipe name at the split point, so a
    service branch should reach a pre-existing component immediately.
    """
    model = _configured_model_pipe_layer()
    if model is None or not new_pipe_names:
        return []

    records: list[dict] = []
    synthetic_features: list[QgsFeature] = []
    pipe_endpoint_ids: dict[str, tuple[int, int]] = {}
    audit_tol = max(0.002, min(0.02, float(tolerance) * 0.05))

    for pipe in model.getFeatures():
        name = _feature_name(pipe)
        if not name or not pipe.hasGeometry() or pipe.geometry().isEmpty():
            continue
        ends = _line_endpoints(pipe.geometry())
        if ends is None:
            continue
        ids = []
        for point in ends:
            rec_id = len(records)
            records.append(
                {
                    "pipe": name,
                    "old": name in previous_pipe_names,
                    "new": name in new_pipe_names,
                    "point": point,
                }
            )
            feature = QgsFeature()
            feature.setId(rec_id)
            feature.setGeometry(QgsGeometry.fromPointXY(point))
            synthetic_features.append(feature)
            ids.append(rec_id)
        pipe_endpoint_ids[name] = (ids[0], ids[1])

    if not records:
        return []

    parent = list(range(len(records)))
    rank = [0] * len(records)

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra == rb:
            return
        if rank[ra] < rank[rb]:
            ra, rb = rb, ra
        parent[rb] = ra
        if rank[ra] == rank[rb]:
            rank[ra] += 1

    # The two ends of one pipe belong to one hydraulic component.
    for a, b in pipe_endpoint_ids.values():
        union(a, b)

    index = QgsSpatialIndex()
    for feature in synthetic_features:
        index.addFeature(feature)

    # Merge pipe ends which meet at the same hydraulic node.
    for i, record in enumerate(records):
        point = record["point"]
        pgeom = QgsGeometry.fromPointXY(point)
        box = pgeom.boundingBox()
        box.grow(audit_tol)
        for j in index.intersects(box):
            if j <= i or j >= len(records):
                continue
            try:
                other = QgsGeometry.fromPointXY(records[j]["point"])
                if pgeom.distance(other) <= audit_tol:
                    union(i, j)
            except Exception:
                continue

    components: dict[int, dict] = {}
    for i, record in enumerate(records):
        root = find(i)
        comp = components.setdefault(
            root,
            {"has_old": False, "new_pipes": set(), "all_pipes": set(), "point": record["point"]},
        )
        comp["has_old"] = bool(comp["has_old"] or record["old"])
        comp["all_pipes"].add(record["pipe"])
        if record["new"]:
            comp["new_pipes"].add(record["pipe"])

    issues: list[dict] = []
    for comp in components.values():
        if not comp["new_pipes"] or comp["has_old"]:
            continue
        names = sorted(comp["new_pipes"])
        preview = ", ".join(names[:6])
        if len(names) > 6:
            preview += f" (+{len(names) - 6})"
        issues.append(
            {
                "typ": "ODCIETA_NOWA_GRUPA",
                "node_id": "",
                "pipe_id": preview,
                "point": comp["point"],
                "uwaga": (
                    "Nowo dodane przewody tworzą komponent, który nie łączy się końcem z żadnym "
                    "przewodem istniejącym przed importem. Sprawdź punkt wpięcia i podział przewodu głównego."
                ),
            }
        )
    return issues


def _create_topology_issue_layer(issues: list[dict], crs_authid: str) -> QgsVectorLayer | None:
    if not issues:
        return None
    layer = QgsVectorLayer(
        f"Point?crs={crs_authid}",
        "Problemy topologii po imporcie przyłączy",
        "memory",
    )
    provider = layer.dataProvider()
    provider.addAttributes(
        [
            QgsField("typ", QVariant.String),
            QgsField("node_id", QVariant.String),
            QgsField("pipe_id", QVariant.String),
            QgsField("uwaga", QVariant.String),
        ]
    )
    layer.updateFields()

    features: list[QgsFeature] = []
    for issue in issues:
        feature = QgsFeature(layer.fields())
        feature.setGeometry(QgsGeometry.fromPointXY(issue["point"]))
        feature.setAttributes(
            [issue["typ"], issue["node_id"], issue["pipe_id"], issue["uwaga"]]
        )
        features.append(feature)
    provider.addFeatures(features)
    layer.updateExtents()
    layer.setCustomProperty("qgis_epanet_integrator/source_kind", "service_topology_issues")
    QgsProject.instance().addMapLayer(layer)
    return layer


def _audit_service_import_topology(
    previous_pipe_names: set[str],
    previous_node_names: set[str],
    tolerance: float,
) -> tuple[list[dict], QgsVectorLayer | None]:
    current_pipe_names, current_node_names = _current_model_names()
    new_pipe_names = current_pipe_names - previous_pipe_names
    new_node_names = current_node_names - previous_node_names

    issues = _new_node_on_pipe_interior_issues(new_node_names, tolerance)
    issues.extend(
        _disconnected_new_pipe_component_issues(
            previous_pipe_names,
            new_pipe_names,
            tolerance,
        )
    )

    # De-duplicate identical diagnostics which can arise from multipart/provider quirks.
    unique: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for issue in issues:
        key = (str(issue["typ"]), str(issue["node_id"]), str(issue["pipe_id"]))
        if key in seen:
            continue
        seen.add(key)
        unique.append(issue)

    model = _configured_model_pipe_layer()
    issue_layer = None
    if model is not None and unique:
        issue_layer = _create_topology_issue_layer(unique, model.crs().authid())
    return unique, issue_layer


def show_add_service_groups_to_model(iface: QgisInterface) -> dict[str, int] | None:
    """Add complete selected service-connection groups to the hydraulic model.

    Selecting even one segment means selecting its whole ``grupa``. This avoids
    importing only the Ø90 trunk while accidentally leaving its Ø40/Ø32 branches
    behind. After a successful import, terminal/helper points are linked to the
    nearest real EPANET node by filling ``node_id``.
    """
    layer = _service_fragments_layer(iface)
    if layer is None:
        QMessageBox.warning(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Ustaw jako aktywną warstwę „Nowe fragmenty przyłączy GIS”.",
        )
        return None

    groups = _selected_service_groups(layer)
    if not groups:
        QMessageBox.information(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Zaznacz co najmniej jeden fragment grupy przyłącza albo ustaw decyzję DO_DODANIA.",
        )
        return None

    segment_count = _select_whole_service_groups(layer, groups)
    dialog = AddFragmentsDialog(iface, layer)
    dialog.setWindowTitle("Dodaj grupy przyłączy GIS do modelu")
    if not dialog.exec():
        return None
    options = dialog.options()

    answer = QMessageBox.question(
        iface.mainWindow(),
        "Dodaj grupy przyłączy do modelu",
        (
            f"Wybrano {len(groups)} grup ({segment_count} fragmentów). "
            "Do modelu zostaną dodane CAŁE grupy, razem z odgałęzieniami. "
            "Operacja może tworzyć węzły i dzielić istniejące przewody. Kontynuować?"
        ),
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        return None

    previous_pipe_names, previous_node_names = _current_model_names()

    try:
        result = add_fragments_to_model(iface, options)
        mapped_nodes = _update_service_endpoint_node_ids(
            layer,
            groups,
            max(float(options.node_tolerance), 0.05),
        )
        result["groups"] = len(groups)
        result["mapped_nodes"] = mapped_nodes
        endpoint_layer = _endpoint_layer_for_fragments(layer)
        mapping_result = None
        if endpoint_layer is not None:
            mapping_result = save_endpoint_mapping(endpoint_layer, layer, groups)
            result["mapping_inserted"] = int(mapping_result["inserted"])
            result["mapping_updated"] = int(mapping_result["updated"])
            result["mapping_skipped"] = int(mapping_result["skipped"])
        topology_issues, issue_layer = _audit_service_import_topology(
            previous_pipe_names,
            previous_node_names,
            max(float(options.node_tolerance), float(options.pipe_tolerance)),
        )
        result["topology_issues"] = len(topology_issues)
    except Exception as exc:
        QMessageBox.critical(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            f"Nie udało się dodać grup przyłączy do modelu:\n{exc}",
        )
        return None

    if result.get("topology_issues", 0):
        count = int(result["topology_issues"])
        if issue_layer is not None:
            iface.setActiveLayer(issue_layer)
            iface.zoomToActiveLayer()
        preview_lines = []
        for issue in topology_issues[:8]:
            ident = issue["node_id"] or issue["pipe_id"]
            preview_lines.append(f"• {issue['typ']}: {ident}")
        if len(topology_issues) > 8:
            preview_lines.append(f"• ... oraz {len(topology_issues) - 8} kolejnych")
        QMessageBox.warning(
            iface.mainWindow(),
            "Kontrola topologii po imporcie przyłączy",
            (
                f"Przyłącza zostały dodane, ale wykryto {count} potencjalnych problemów topologii.\n\n"
                + "\n".join(preview_lines)
                + "\n\nUtworzono warstwę „Problemy topologii po imporcie przyłączy”. "
                  "Sprawdź wskazane miejsca przed uruchomieniem symulacji EPANET."
            ),
        )
        iface.messageBar().pushMessage(
            "Integrator QGIS-EPANET",
            f"Dodano grupy, ale kontrola topologii wykryła {count} problemów. Sprawdź warstwę diagnostyczną.",
            level=Qgis.MessageLevel.Warning,
            duration=12,
        )
    else:
        iface.messageBar().pushMessage(
            "Integrator QGIS-EPANET",
            (
                f"Dodano {result['groups']} grup: {result['pipes']} przewodów, "
                f"{result['nodes']} nowych węzłów, {result['splits']} podziałów. "
                f"Powiązano {result['mapped_nodes']} punktów końcowych z node_id. "
                f"Mapowanie trwałe: +{result.get('mapping_inserted', 0)} / "
                f"odświeżono {result.get('mapping_updated', 0)}. "
                "Kontrola topologii: OK."
            ),
            level=Qgis.MessageLevel.Success,
            duration=10,
        )
    return result
