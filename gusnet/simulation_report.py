from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from gusnet.i18n import tr


@dataclass(frozen=True)
class ExtremeResult:
    label: str
    field_name: str
    value: float
    time_index: int
    layer_id: str
    feature_id: int
    element_name: str


METRICS = (
    ("Minimalne ciśnienie", "pressure", "min"),
    ("Maksymalne ciśnienie", "pressure", "max"),
    ("Maksymalna prędkość", "velocity", "max_abs"),
    ("Maksymalny przepływ bezwzględny", "flowrate", "max_abs"),
    ("Maksymalna strata wysokości", "headloss", "max_abs"),
)


def _numeric_sequence(value) -> list[float]:
    if value is None or isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        return []
    converted: list[float] = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError):
            return []
        if math.isfinite(number):
            converted.append(number)
        else:
            converted.append(float("nan"))
    return converted


def _feature_name(feature) -> str:
    names = feature.fields().names()
    for candidate in ("name", "id", "ID"):
        if candidate in names:
            value = feature[candidate]
            if value not in (None, ""):
                return str(value)
    return str(feature.id())


def collect_extremes() -> list[ExtremeResult]:
    project = QgsProject.instance()
    found: list[ExtremeResult] = []

    for label, field_name, mode in METRICS:
        best: ExtremeResult | None = None
        best_comparison: float | None = None

        for layer in project.mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.fields().indexFromName(field_name) < 0:
                continue

            for feature in layer.getFeatures():
                values = _numeric_sequence(feature[field_name])
                for index, value in enumerate(values):
                    if not math.isfinite(value):
                        continue
                    comparison = abs(value) if mode == "max_abs" else value
                    is_better = (
                        best_comparison is None
                        or (mode == "min" and comparison < best_comparison)
                        or (mode != "min" and comparison > best_comparison)
                    )
                    if not is_better:
                        continue
                    best_comparison = comparison
                    best = ExtremeResult(
                        label=tr(label),
                        field_name=field_name,
                        value=value,
                        time_index=index,
                        layer_id=layer.id(),
                        feature_id=feature.id(),
                        element_name=_feature_name(feature),
                    )

        if best is not None:
            found.append(best)

    return found


class SimulationReportDialog(QDialog):
    def __init__(self, iface: QgisInterface, log_text: str = "", parent=None) -> None:
        super().__init__(parent)
        self.iface = iface
        self.results = collect_extremes()

        self.setWindowTitle(tr("Raport symulacji"))
        self.resize(830, 520)

        tabs = QTabWidget(self)
        tabs.addTab(self._summary_tab(), tr("Podsumowanie"))
        tabs.addTab(self._log_tab(log_text), tr("Raport EPANET / WNTR"))

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(buttons)

    def _summary_tab(self) -> QWidget:
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        explanation = QLabel(
            tr("Dwukrotne kliknięcie wyniku zaznacza element i przybliża mapę do jego położenia."),
            widget,
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)

        self.table = QTableWidget(0, 5, widget)
        self.table.setHorizontalHeaderLabels(
            [tr("Wynik"), tr("Wartość"), tr("Element"), tr("Czas"), tr("Pole")]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)

        for result in self.results:
            row = self.table.rowCount()
            self.table.insertRow(row)
            values = (
                result.label,
                f"{result.value:.4g}",
                result.element_name,
                f"{result.time_index:02d}:00",
                result.field_name,
            )
            for column, text in enumerate(values):
                item = QTableWidgetItem(text)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, (result.layer_id, result.feature_id))
                self.table.setItem(row, column, item)

        self.table.cellDoubleClicked.connect(self._zoom_to_row)
        layout.addWidget(self.table, 1)

        zoom_button = QPushButton(tr("Pokaż zaznaczony element na mapie"), widget)
        zoom_button.clicked.connect(self._zoom_to_selected)
        layout.addWidget(zoom_button)

        if not self.results:
            self.table.setEnabled(False)
            zoom_button.setEnabled(False)
            layout.insertWidget(1, QLabel(tr("Nie znaleziono warstw z wynikami czasowymi."), widget))

        return widget

    def _log_tab(self, log_text: str) -> QWidget:
        widget = QWidget(self)
        layout = QVBoxLayout(widget)
        editor = QTextEdit(widget)
        editor.setReadOnly(True)
        editor.setPlainText(log_text.strip() or tr("Brak zapisanego raportu z ostatniej symulacji."))
        layout.addWidget(editor)
        return widget

    def _zoom_to_selected(self) -> None:
        row = self.table.currentRow()
        if row >= 0:
            self._zoom_to_row(row, 0)

    def _zoom_to_row(self, row: int, column: int) -> None:
        del column
        item = self.table.item(row, 0)
        if item is None:
            return
        reference = item.data(Qt.ItemDataRole.UserRole)
        if not reference:
            return
        layer_id, feature_id = reference
        layer = QgsProject.instance().mapLayer(layer_id)
        if not isinstance(layer, QgsVectorLayer):
            return

        layer.removeSelection()
        layer.selectByIds([int(feature_id)])
        self.iface.setActiveLayer(layer)
        self.iface.mapCanvas().zoomToSelected(layer)
        self.iface.mapCanvas().refresh()


def show_simulation_report(iface: QgisInterface, log_text: str = "") -> SimulationReportDialog | None:
    if not collect_extremes():
        QMessageBox.information(
            iface.mainWindow(),
            tr("Raport symulacji"),
            tr("Najpierw uruchom symulację i utwórz warstwy wynikowe."),
        )
        return None
    dialog = SimulationReportDialog(iface, log_text, iface.mainWindow())
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
