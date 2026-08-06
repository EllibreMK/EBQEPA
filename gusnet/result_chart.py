from __future__ import annotations

from collections.abc import Sequence

from qgis.core import QgsFeature, QgsVectorLayer
from qgis.gui import QgisInterface
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QMessageBox,
    QVBoxLayout,
)

from gusnet.elements import Field
from gusnet.i18n import tr
from gusnet.settings import ProjectSettings, SettingKey

try:
    from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
except ImportError:  # QGIS builds using the Qt5-specific matplotlib backend
    from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
    from matplotlib.backends.backend_qt5agg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure


RESULT_FIELDS = (
    Field.DEMAND,
    Field.HEAD,
    Field.PRESSURE,
    Field.QUALITY,
    Field.FLOWRATE,
    Field.HEADLOSS,
    Field.UNIT_HEADLOSS,
    Field.VELOCITY,
    Field.REACTION_RATE,
)


class ResultChartDialog(QDialog):
    """Plot time-series values stored on a selected Gusnet result feature."""

    def __init__(self, layer: QgsVectorLayer, feature: QgsFeature, parent=None) -> None:
        super().__init__(parent)
        self.layer = layer
        self.feature = feature
        self.available_fields = self._available_result_fields()

        element_name = str(feature[Field.NAME.value]) if Field.NAME.value in feature.fields().names() else str(feature.id())
        self.setWindowTitle(tr("Simulation result chart") + f" — {element_name}")
        self.resize(760, 500)

        self.element_label = QLabel(element_name, self)
        self.parameter_combo = QComboBox(self)
        for field in self.available_fields:
            self.parameter_combo.addItem(field.friendly_name, field.value)

        form = QFormLayout()
        form.addRow(tr("Element"), self.element_label)
        form.addRow(tr("Parameter"), self.parameter_combo)

        self.figure = Figure(tight_layout=True)
        self.canvas = FigureCanvas(self.figure)
        self.toolbar = NavigationToolbar(self.canvas, self)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self.toolbar)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(buttons)

        self.parameter_combo.currentIndexChanged.connect(self.redraw)
        self.redraw()

    def _available_result_fields(self) -> list[Field]:
        field_names = set(self.layer.fields().names())
        available: list[Field] = []
        for field in RESULT_FIELDS:
            if field.value not in field_names:
                continue
            values = self._as_numeric_sequence(self.feature[field.value])
            if values:
                available.append(field)
        return available

    @staticmethod
    def _as_numeric_sequence(value) -> list[float]:
        if value is None or isinstance(value, (str, bytes)):
            return []
        if not isinstance(value, Sequence):
            return []

        converted: list[float] = []
        for item in value:
            try:
                converted.append(float(item))
            except (TypeError, ValueError):
                return []
        return converted

    def _time_axis(self, count: int) -> list[float]:
        if count <= 1:
            return [0.0] * count

        duration = ProjectSettings().get(SettingKey.SIMULATION_DURATION, count - 1)
        try:
            duration_hours = max(0.0, float(duration))
        except (TypeError, ValueError):
            duration_hours = float(count - 1)

        step = duration_hours / (count - 1)
        return [index * step for index in range(count)]

    def redraw(self) -> None:
        self.figure.clear()
        axes = self.figure.add_subplot(111)

        field_name = self.parameter_combo.currentData()
        if not field_name:
            axes.text(0.5, 0.5, tr("No time-series results"), ha="center", va="center")
            axes.set_axis_off()
            self.canvas.draw_idle()
            return

        values = self._as_numeric_sequence(self.feature[field_name])
        x_values = self._time_axis(len(values))
        field = Field(field_name)

        axes.plot(x_values, values)
        axes.set_xlabel(tr("Simulation time (hours)"))
        axes.set_ylabel(field.friendly_name)
        axes.set_title(field.friendly_name)
        axes.grid(True)
        self.canvas.draw_idle()


def show_selected_result_chart(iface: QgisInterface) -> ResultChartDialog | None:
    layer = iface.activeLayer()
    if not isinstance(layer, QgsVectorLayer):
        QMessageBox.information(
            iface.mainWindow(),
            tr("Simulation result chart"),
            tr("Najpierw wybierz warstwę wynikową Integratora QGIS–EPANET."),
        )
        return None

    selected = layer.selectedFeatures()
    if len(selected) != 1:
        QMessageBox.information(
            iface.mainWindow(),
            tr("Simulation result chart"),
            tr("Select exactly one result feature first."),
        )
        return None

    dialog = ResultChartDialog(layer, selected[0], iface.mainWindow())
    if not dialog.available_fields:
        QMessageBox.information(
            iface.mainWindow(),
            tr("Simulation result chart"),
            tr("The selected feature has no time-series result fields."),
        )
        dialog.deleteLater()
        return None

    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
