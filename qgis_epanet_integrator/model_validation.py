from __future__ import annotations

from dataclasses import dataclass

from qgis.core import QgsFeature, QgsFeatureRequest, QgsProject, QgsSpatialIndex, QgsWkbTypes
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
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


def validate_model() -> list[ValidationIssue]:
    layers = _model_layers()
    issues: list[ValidationIssue] = []
    if not layers:
        return [ValidationIssue("Błąd", tr("Nie skonfigurowano warstw modelu."))]

    # Duplicate node IDs and duplicate link IDs are checked across their respective groups.
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
                        "Błąd",
                        tr("Zduplikowany identyfikator: {name}").format(name=name),
                        layer.id(), feature.id(), name,
                    ))
                else:
                    seen[name] = (layer.id(), feature.id())

    pipes = layers.get(ModelLayer.PIPES)
    if pipes is not None:
        field_names = pipes.fields().names()
        for feature in pipes.getFeatures():
            name = _feature_name(feature)
            if feature.geometry().isNull() or feature.geometry().isEmpty():
                issues.append(ValidationIssue("Błąd", tr("Przewód nie ma geometrii."), pipes.id(), feature.id(), name))
                continue
            if Field.DIAMETER.value in field_names:
                value = feature[Field.DIAMETER.value]
                try:
                    invalid = value is None or float(value) <= 0
                except (TypeError, ValueError):
                    invalid = True
                if invalid:
                    issues.append(ValidationIssue("Błąd", tr("Średnica przewodu musi być większa od zera."), pipes.id(), feature.id(), name))
            if Field.LENGTH.value in field_names:
                value = feature[Field.LENGTH.value]
                try:
                    invalid = value is None or float(value) <= 0
                except (TypeError, ValueError):
                    invalid = True
                if invalid:
                    issues.append(ValidationIssue("Błąd", tr("Długość przewodu musi być większa od zera."), pipes.id(), feature.id(), name))

        # Check whether both ends of every pipe touch any configured node.
        node_features = []
        for model_layer in (ModelLayer.JUNCTIONS, ModelLayer.RESERVOIRS, ModelLayer.TANKS):
            layer = layers.get(model_layer)
            if layer is not None:
                node_features.extend(list(layer.getFeatures()))
        if node_features:
            # QGIS 3.44 does not accept a plain Python list in the
            # QgsSpatialIndex constructor. Build the index explicitly.
            # Assign synthetic feature IDs because node layers can reuse the
            # same provider feature IDs (e.g. fid=1 in several layers).
            index = QgsSpatialIndex()
            for index_id, node_feature in enumerate(node_features, start=1):
                indexed_feature = QgsFeature(node_feature)
                indexed_feature.setId(index_id)
                index.addFeature(indexed_feature)

            tolerance = max(pipes.extent().width(), pipes.extent().height()) * 1e-8
            tolerance = max(tolerance, 1e-7)
            for feature in pipes.getFeatures():
                geom = feature.geometry()
                if geom.isNull() or geom.isEmpty():
                    continue
                line = geom.asPolyline()
                if not line:
                    multi = geom.asMultiPolyline()
                    line = multi[0] if multi else []
                if len(line) < 2:
                    continue
                missing = 0
                for point in (line[0], line[-1]):
                    nearest = index.nearestNeighbor(point, 1, tolerance)
                    if not nearest:
                        missing += 1
                if missing:
                    issues.append(ValidationIssue(
                        "Błąd",
                        tr("Końcówka przewodu nie jest połączona z węzłem ({count}).").format(count=missing),
                        pipes.id(), feature.id(), _feature_name(feature),
                    ))

    return issues


class ModelValidationDialog(QDialog):
    def __init__(self, iface: QgisInterface, issues: list[ValidationIssue]):
        super().__init__(iface.mainWindow())
        self.iface = iface
        self.issues = issues
        self.setWindowTitle(tr("Sprawdzenie modelu"))
        self.resize(850, 480)

        layout = QVBoxLayout(self)
        summary = tr("Nie znaleziono problemów.") if not issues else tr("Znaleziono problemy: {count}").format(count=len(issues))
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
    dialog = ModelValidationDialog(iface, validate_model())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
