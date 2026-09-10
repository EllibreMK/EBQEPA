from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qgis.core import (
    QgsFeature,
    QgsFeatureRequest,
    QgsGeometry,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
)
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from qgis_epanet_integrator.elements import Field, ModelLayer
from qgis_epanet_integrator.i18n import tr
from qgis_epanet_integrator.settings import ProjectSettings, SettingKey


@dataclass(frozen=True)
class ValidationIssue:
    severity: str
    message: str
    layer_id: str = ""
    feature_id: int = -1
    element_id: str = ""


@dataclass(frozen=True)
class ValidationOptions:
    pumps: bool = True
    tanks: bool = True
    valves: bool = True
    source_connectivity: bool = True
    demands: bool = False
    pipe_parameters: bool = False


class ValidationPreset(Enum):
    BASIC = "basic"
    STANDARD = "standard"
    FULL = "full"
    CUSTOM = "custom"


def _model_layers():
    result = {}
    configured = ProjectSettings().get(SettingKey.MODEL_LAYERS, {}) or {}
    project = QgsProject.instance()
    for key, layer_id in configured.items():
        try:
            model_layer = ModelLayer[key]
        except (KeyError, TypeError):
            continue
        layer = project.mapLayer(layer_id)
        if layer is not None and layer.isValid():
            result[model_layer] = layer
    return result


def _feature_name(feature) -> str:
    try:
        value = feature[Field.NAME.value]
    except (KeyError, IndexError):
        return str(feature.id())
    return str(value) if value not in (None, "") else str(feature.id())


def _number(feature, field_name: str):
    try:
        value = feature[field_name]
        if value in (None, ""):
            return None
        return float(value)
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def _text(feature, field_name: str) -> str:
    try:
        value = feature[field_name]
    except (KeyError, IndexError):
        return ""
    return "" if value is None else str(value).strip()


def _line_endpoints(geometry: QgsGeometry):
    line = geometry.asPolyline()
    if not line:
        multi = geometry.asMultiPolyline()
        line = multi[0] if multi else []
    if len(line) < 2:
        return None
    return line[0], line[-1]


def _validation_tolerance(layers) -> float:
    extent = QgsRectangle()
    has_extent = False
    for layer in layers.values():
        if layer.featureCount() <= 0:
            continue
        if not has_extent:
            extent = QgsRectangle(layer.extent())
            has_extent = True
        else:
            extent.combineExtentWith(layer.extent())
    span = max(extent.width(), extent.height()) if has_extent else 0
    return max(span * 1e-8, 1e-7)


@dataclass
class _Topology:
    index: QgsSpatialIndex
    nodes: dict[int, tuple[ModelLayer, object, str]]
    connected_node_ids: set[int]
    adjacency: dict[int, set[int]]
    tolerance: float


def _build_topology(layers, issues: list[ValidationIssue]) -> _Topology:
    tolerance = _validation_tolerance(layers)
    index = QgsSpatialIndex()
    nodes: dict[int, tuple[ModelLayer, object, str]] = {}
    connected_node_ids: set[int] = set()
    adjacency: dict[int, set[int]] = {}
    coordinate_buckets: dict[tuple[int, int], tuple[str, str, int]] = {}

    synthetic_id = 1
    for model_layer in (ModelLayer.JUNCTIONS, ModelLayer.RESERVOIRS, ModelLayer.TANKS):
        layer = layers.get(model_layer)
        if layer is None:
            continue
        for feature in layer.getFeatures():
            name = _feature_name(feature)
            geom = feature.geometry()
            if geom.isNull() or geom.isEmpty():
                issues.append(ValidationIssue(
                    "Błąd", tr("Węzeł nie ma geometrii."), layer.id(), feature.id(), name,
                ))
                continue
            if not geom.isGeosValid():
                issues.append(ValidationIssue(
                    "Błąd", tr("Geometria węzła jest nieprawidłowa."), layer.id(), feature.id(), name,
                ))
            point = geom.asPoint()
            indexed = QgsFeature(feature)
            indexed.setId(synthetic_id)
            index.addFeature(indexed)
            nodes[synthetic_id] = (model_layer, feature, layer.id())
            adjacency[synthetic_id] = set()

            bucket = (round(point.x() / tolerance), round(point.y() / tolerance))
            previous = coordinate_buckets.get(bucket)
            if previous is not None:
                previous_name, _, _ = previous
                issues.append(ValidationIssue(
                    "Błąd",
                    tr("Węzeł nakłada się na inny węzeł: {other}.").format(other=previous_name),
                    layer.id(), feature.id(), name,
                ))
            else:
                coordinate_buckets[bucket] = (name, layer.id(), feature.id())
            synthetic_id += 1

    geometry_seen: dict[bytes, tuple[str, str, int]] = {}
    for model_layer in (ModelLayer.PIPES, ModelLayer.PUMPS, ModelLayer.VALVES):
        layer = layers.get(model_layer)
        if layer is None:
            continue
        for feature in layer.getFeatures():
            name = _feature_name(feature)
            geom = feature.geometry()
            if geom.isNull() or geom.isEmpty():
                issues.append(ValidationIssue(
                    "Błąd", tr("Połączenie nie ma geometrii."), layer.id(), feature.id(), name,
                ))
                continue
            if not geom.isGeosValid():
                issues.append(ValidationIssue(
                    "Błąd", tr("Geometria połączenia jest nieprawidłowa."), layer.id(), feature.id(), name,
                ))
            if geom.length() <= 0:
                issues.append(ValidationIssue(
                    "Błąd", tr("Połączenie ma zerową długość geometryczną."), layer.id(), feature.id(), name,
                ))

            key = bytes(geom.asWkb())
            previous = geometry_seen.get(key)
            if previous is not None:
                issues.append(ValidationIssue(
                    "Ostrzeżenie",
                    tr("Geometria połączenia jest identyczna z elementem {other}.").format(other=previous[0]),
                    layer.id(), feature.id(), name,
                ))
            else:
                geometry_seen[key] = (name, layer.id(), feature.id())

            endpoints = _line_endpoints(geom)
            if endpoints is None:
                issues.append(ValidationIssue(
                    "Błąd", tr("Nie można odczytać końców połączenia."), layer.id(), feature.id(), name,
                ))
                continue
            endpoint_nodes: list[int | None] = []
            for point in endpoints:
                nearest = index.nearestNeighbor(point, 1, tolerance)
                endpoint_nodes.append(nearest[0] if nearest else None)
            missing = sum(node_id is None for node_id in endpoint_nodes)
            if missing:
                issues.append(ValidationIssue(
                    "Błąd",
                    tr("Końcówka połączenia nie jest połączona z węzłem ({count}).").format(count=missing),
                    layer.id(), feature.id(), name,
                ))
                continue
            first, second = endpoint_nodes
            assert first is not None and second is not None
            connected_node_ids.update((first, second))
            adjacency[first].add(second)
            adjacency[second].add(first)
            if first == second:
                issues.append(ValidationIssue(
                    "Błąd",
                    tr("Obie końcówki połączenia są podłączone do tego samego węzła."),
                    layer.id(), feature.id(), name,
                ))

    for node_id, (_, feature, layer_id) in nodes.items():
        if node_id not in connected_node_ids:
            issues.append(ValidationIssue(
                "Ostrzeżenie", tr("Węzeł nie jest połączony z żadnym elementem sieci."),
                layer_id, feature.id(), _feature_name(feature),
            ))

    return _Topology(index, nodes, connected_node_ids, adjacency, tolerance)


def _check_duplicates(layers, issues):
    for group in (
        (ModelLayer.JUNCTIONS, ModelLayer.RESERVOIRS, ModelLayer.TANKS),
        (ModelLayer.PIPES, ModelLayer.PUMPS, ModelLayer.VALVES),
    ):
        seen: dict[str, tuple[str, int]] = {}
        for model_layer in group:
            layer = layers.get(model_layer)
            if layer is None or Field.NAME.value not in layer.fields().names():
                continue
            for feature in layer.getFeatures():
                name = _feature_name(feature)
                if name in seen:
                    issues.append(ValidationIssue(
                        "Błąd", tr("Zduplikowany identyfikator: {name}").format(name=name),
                        layer.id(), feature.id(), name,
                    ))
                else:
                    seen[name] = (layer.id(), feature.id())


def _check_required_pipe_data(layers, issues):
    pipes = layers.get(ModelLayer.PIPES)
    if pipes is None:
        return
    fields = pipes.fields().names()
    for feature in pipes.getFeatures():
        name = _feature_name(feature)
        if Field.DIAMETER.value in fields:
            diameter = _number(feature, Field.DIAMETER.value)
            if diameter is None or diameter <= 0:
                issues.append(ValidationIssue(
                    "Błąd", tr("Średnica przewodu musi być większa od zera."),
                    pipes.id(), feature.id(), name,
                ))
        if Field.LENGTH.value in fields:
            length = _number(feature, Field.LENGTH.value)
            if length is None or length <= 0:
                issues.append(ValidationIssue(
                    "Błąd", tr("Długość przewodu musi być większa od zera."),
                    pipes.id(), feature.id(), name,
                ))


def _check_pumps(layers, issues):
    layer = layers.get(ModelLayer.PUMPS)
    if layer is None:
        return
    fields = layer.fields().names()
    for feature in layer.getFeatures():
        curve = _text(feature, Field.PUMP_CURVE.value) if Field.PUMP_CURVE.value in fields else ""
        power = _number(feature, Field.POWER.value) if Field.POWER.value in fields else None
        if not curve and (power is None or power <= 0):
            issues.append(ValidationIssue(
                "Ostrzeżenie", tr("Pompa nie ma krzywej ani dodatniej mocy."),
                layer.id(), feature.id(), _feature_name(feature),
            ))


def _check_tanks(layers, issues):
    layer = layers.get(ModelLayer.TANKS)
    if layer is None:
        return
    fields = layer.fields().names()
    for feature in layer.getFeatures():
        name = _feature_name(feature)
        minimum = _number(feature, Field.MIN_LEVEL.value) if Field.MIN_LEVEL.value in fields else None
        initial = _number(feature, Field.INIT_LEVEL.value) if Field.INIT_LEVEL.value in fields else None
        maximum = _number(feature, Field.MAX_LEVEL.value) if Field.MAX_LEVEL.value in fields else None
        if None not in (minimum, initial, maximum) and not (minimum <= initial <= maximum):
            issues.append(ValidationIssue(
                "Błąd", tr("Poziomy zbiornika nie spełniają warunku: minimum ≤ początkowy ≤ maksimum."),
                layer.id(), feature.id(), name,
            ))
        diameter = _number(feature, Field.TANK_DIAMETER.value) if Field.TANK_DIAMETER.value in fields else None
        if diameter is not None and diameter <= 0:
            issues.append(ValidationIssue(
                "Błąd", tr("Średnica zbiornika musi być większa od zera."),
                layer.id(), feature.id(), name,
            ))


def _check_valves(layers, issues):
    layer = layers.get(ModelLayer.VALVES)
    if layer is None:
        return
    fields = layer.fields().names()
    for feature in layer.getFeatures():
        name = _feature_name(feature)
        valve_type = _text(feature, Field.VALVE_TYPE.value).upper()
        required_field = None
        if valve_type in {"PRV", "PSV", "PBV"}:
            required_field = Field.PRESSURE_SETTING.value
        elif valve_type == "FCV":
            required_field = Field.FLOW_SETTING.value
        elif valve_type == "TCV":
            required_field = Field.THROTTLE_SETTING.value
        elif valve_type == "GPV":
            required_field = Field.HEADLOSS_CURVE.value
        if required_field and required_field in fields:
            value = _text(feature, required_field)
            if value == "":
                issues.append(ValidationIssue(
                    "Ostrzeżenie",
                    tr("Zawór typu {type} nie ma wymaganego ustawienia.").format(type=valve_type),
                    layer.id(), feature.id(), name,
                ))


def _check_source_connectivity(layers, topology: _Topology, issues):
    sources = {
        node_id for node_id, (model_layer, _, _) in topology.nodes.items()
        if model_layer in (ModelLayer.RESERVOIRS, ModelLayer.TANKS)
    }
    if not sources:
        issues.append(ValidationIssue(
            "Ostrzeżenie", tr("Model nie zawiera rezerwuaru ani zbiornika będącego źródłem zasilania."),
        ))
        return
    reachable = set(sources)
    stack = list(sources)
    while stack:
        current = stack.pop()
        for neighbor in topology.adjacency.get(current, ()):
            if neighbor not in reachable:
                reachable.add(neighbor)
                stack.append(neighbor)
    for node_id, (model_layer, feature, layer_id) in topology.nodes.items():
        if model_layer is ModelLayer.JUNCTIONS and node_id not in reachable:
            issues.append(ValidationIssue(
                "Ostrzeżenie", tr("Węzeł nie ma połączenia ze źródłem zasilania."),
                layer_id, feature.id(), _feature_name(feature),
            ))


def _check_demands(layers, issues):
    layer = layers.get(ModelLayer.JUNCTIONS)
    if layer is None or Field.BASE_DEMAND.value not in layer.fields().names():
        return
    for feature in layer.getFeatures():
        demand = _number(feature, Field.BASE_DEMAND.value)
        if demand is not None and demand < 0:
            issues.append(ValidationIssue(
                "Informacja", tr("Węzeł ma ujemne zapotrzebowanie; może to być zamierzone zasilanie."),
                layer.id(), feature.id(), _feature_name(feature),
            ))


def _check_pipe_parameters(layers, issues):
    layer = layers.get(ModelLayer.PIPES)
    if layer is None:
        return
    fields = layer.fields().names()
    for feature in layer.getFeatures():
        name = _feature_name(feature)
        diameter = _number(feature, Field.DIAMETER.value) if Field.DIAMETER.value in fields else None
        if diameter is not None and (diameter < 20 or diameter > 2000):
            issues.append(ValidationIssue(
                "Informacja", tr("Nietypowa średnica przewodu: {value:g}.").format(value=diameter),
                layer.id(), feature.id(), name,
            ))
        roughness = _number(feature, Field.ROUGHNESS.value) if Field.ROUGHNESS.value in fields else None
        if roughness is not None and roughness <= 0:
            issues.append(ValidationIssue(
                "Ostrzeżenie", tr("Współczynnik szorstkości powinien być większy od zera."),
                layer.id(), feature.id(), name,
            ))


def validate_model(options: ValidationOptions | None = None) -> list[ValidationIssue]:
    options = options or ValidationOptions()
    layers = _model_layers()
    issues: list[ValidationIssue] = []
    if not layers:
        return [ValidationIssue("Błąd", tr("Nie skonfigurowano warstw modelu."))]

    # Kontrole geometrii i integralności danych są wykonywane zawsze.
    _check_duplicates(layers, issues)
    _check_required_pipe_data(layers, issues)
    topology = _build_topology(layers, issues)

    # Kontrole hydrauliczne są opcjonalne.
    if options.pumps:
        _check_pumps(layers, issues)
    if options.tanks:
        _check_tanks(layers, issues)
    if options.valves:
        _check_valves(layers, issues)
    if options.source_connectivity:
        _check_source_connectivity(layers, topology, issues)
    if options.demands:
        _check_demands(layers, issues)
    if options.pipe_parameters:
        _check_pipe_parameters(layers, issues)

    return issues


class ValidationOptionsDialog(QDialog):
    def __init__(self, iface: QgisInterface):
        super().__init__(iface.mainWindow())
        self.setWindowTitle(tr("Zakres sprawdzenia modelu"))
        self.resize(520, 430)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(tr(
            "Kontrole geometrii i integralności danych są wykonywane zawsze. "
            "Wybierz dodatkowe kontrole hydrauliczne."
        )))

        preset_form = QFormLayout()
        self.preset_combo = QComboBox(self)
        self.preset_combo.addItem(tr("Podstawowe — tylko geometria i dane wymagane"), ValidationPreset.BASIC)
        self.preset_combo.addItem(tr("Standardowe — najważniejsze kontrole hydrauliczne"), ValidationPreset.STANDARD)
        self.preset_combo.addItem(tr("Pełne — wszystkie kontrole"), ValidationPreset.FULL)
        self.preset_combo.addItem(tr("Własne"), ValidationPreset.CUSTOM)
        self.preset_combo.setCurrentIndex(1)
        preset_form.addRow(tr("Tryb:"), self.preset_combo)
        layout.addLayout(preset_form)

        geometry_group = QGroupBox(tr("Kontrole obowiązkowe"), self)
        geometry_layout = QVBoxLayout(geometry_group)
        mandatory = QCheckBox(tr(
            "Geometria, duplikaty identyfikatorów, nakładające się i osierocone węzły, "
            "końcówki połączeń, długości i średnice"
        ), geometry_group)
        mandatory.setChecked(True)
        mandatory.setEnabled(False)
        geometry_layout.addWidget(mandatory)
        layout.addWidget(geometry_group)

        hydraulic_group = QGroupBox(tr("Opcjonalne kontrole hydrauliczne"), self)
        hydraulic_layout = QVBoxLayout(hydraulic_group)
        self.pumps_check = QCheckBox(tr("Pompy bez krzywej lub mocy"), hydraulic_group)
        self.tanks_check = QCheckBox(tr("Poziomy i średnice zbiorników"), hydraulic_group)
        self.valves_check = QCheckBox(tr("Wymagane ustawienia zaworów"), hydraulic_group)
        self.source_check = QCheckBox(tr("Połączenie węzłów ze źródłem zasilania"), hydraulic_group)
        self.demands_check = QCheckBox(tr("Ujemne zapotrzebowania"), hydraulic_group)
        self.parameters_check = QCheckBox(tr("Nietypowe średnice i szorstkości przewodów"), hydraulic_group)
        for checkbox in (
            self.pumps_check, self.tanks_check, self.valves_check,
            self.source_check, self.demands_check, self.parameters_check,
        ):
            hydraulic_layout.addWidget(checkbox)
            checkbox.toggled.connect(self._mark_custom)
        layout.addWidget(hydraulic_group)

        self.preset_combo.currentIndexChanged.connect(self._apply_preset)
        self._applying_preset = False
        self._apply_preset()

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply_preset(self, *args) -> None:
        del args
        preset = self.preset_combo.currentData()
        if preset is ValidationPreset.CUSTOM:
            return
        self._applying_preset = True
        try:
            if preset is ValidationPreset.BASIC:
                values = (False, False, False, False, False, False)
            elif preset is ValidationPreset.STANDARD:
                values = (True, True, True, True, False, False)
            else:
                values = (True, True, True, True, True, True)
            for checkbox, value in zip((
                self.pumps_check, self.tanks_check, self.valves_check,
                self.source_check, self.demands_check, self.parameters_check,
            ), values):
                checkbox.setChecked(value)
        finally:
            self._applying_preset = False

    def _mark_custom(self, *args) -> None:
        del args
        if self._applying_preset:
            return
        custom_index = self.preset_combo.findData(ValidationPreset.CUSTOM)
        if custom_index >= 0:
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(custom_index)
            self.preset_combo.blockSignals(False)

    def options(self) -> ValidationOptions:
        return ValidationOptions(
            pumps=self.pumps_check.isChecked(),
            tanks=self.tanks_check.isChecked(),
            valves=self.valves_check.isChecked(),
            source_connectivity=self.source_check.isChecked(),
            demands=self.demands_check.isChecked(),
            pipe_parameters=self.parameters_check.isChecked(),
        )


class ModelValidationDialog(QDialog):
    def __init__(self, iface: QgisInterface, issues: list[ValidationIssue]):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.issues = issues
        self.setWindowTitle(tr("Sprawdzenie modelu"))
        self.resize(900, 520)

        layout = QVBoxLayout(self)
        if not issues:
            summary = tr("Sprawdzanie modelu przebiegło pomyślnie. Nie znaleziono błędów.")
        else:
            counts = {"Błąd": 0, "Ostrzeżenie": 0, "Informacja": 0}
            for issue in issues:
                counts[issue.severity] = counts.get(issue.severity, 0) + 1
            summary = tr(
                "Znaleziono: {errors} błędów, {warnings} ostrzeżeń i {info} informacji."
            ).format(
                errors=counts.get("Błąd", 0),
                warnings=counts.get("Ostrzeżenie", 0),
                info=counts.get("Informacja", 0),
            )
        layout.addWidget(QLabel(summary))

        self.table = QTableWidget(len(issues), 4, self)
        self.table.setHorizontalHeaderLabels([tr("Poziom"), tr("Element"), tr("Opis"), tr("Warstwa")])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for row, issue in enumerate(issues):
            layer = QgsProject.instance().mapLayer(issue.layer_id)
            values = [issue.severity, issue.element_id, issue.message, layer.name() if layer else ""]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                item.setData(Qt.ItemDataRole.UserRole, row)
                self.table.setItem(row, col, item)
        self.table.doubleClicked.connect(self.zoom_to_selected)
        layout.addWidget(self.table)

        zoom_button = QPushButton(tr("Pokaż zaznaczony element na mapie"), self)
        zoom_button.clicked.connect(self.zoom_to_selected)
        zoom_button.setEnabled(bool(issues))
        layout.addWidget(zoom_button)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.close)
        layout.addWidget(buttons)

    def zoom_to_selected(self, *args) -> None:
        del args
        row = self.table.currentRow()
        if row < 0 or row >= len(self.issues):
            return
        issue = self.issues[row]
        layer = QgsProject.instance().mapLayer(issue.layer_id)
        if layer is None or issue.feature_id < 0:
            return
        layer.selectByIds([issue.feature_id])
        self.iface.setActiveLayer(layer)
        feature = next(layer.getFeatures(QgsFeatureRequest(issue.feature_id)), None)
        if feature and not feature.geometry().isEmpty():
            self.iface.mapCanvas().setExtent(feature.geometry().boundingBox())
            self.iface.mapCanvas().zoomScale(max(self.iface.mapCanvas().scale(), 1000))
            self.iface.mapCanvas().refresh()


def show_model_validation(iface: QgisInterface):
    options_dialog = ValidationOptionsDialog(iface)
    if options_dialog.exec() != QDialog.DialogCode.Accepted:
        return None
    dialog = ModelValidationDialog(iface, validate_model(options_dialog.options()))
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
