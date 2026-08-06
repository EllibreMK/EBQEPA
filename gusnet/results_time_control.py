from __future__ import annotations

import contextlib
from collections.abc import Sequence

from qgis.core import (
    Qgis,
    QgsDateTimeRange,
    QgsInterval,
    QgsProject,
    QgsTemporalNavigationObject,
    QgsVectorLayer,
)
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QWidget,
)

from gusnet.elements import ResultLayer
from gusnet.i18n import tr
from gusnet.settings import ProjectSettings, SettingKey
from gusnet.style import apply_qev_result_style


class ResultsTimeControl(QWidget):
    """EPANET-style control for QGIS temporal simulation results."""

    def __init__(
        self,
        iface: QgisInterface,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.iface = iface
        self.controller: QgsTemporalNavigationObject = (
            iface.mapCanvas().temporalController()
        )
        self._updating_temporal_range = False

        self.first_button = self._create_button(
            "|<", tr("First simulation time step")
        )
        self.previous_button = self._create_button(
            "<", tr("Previous simulation time step")
        )
        self.next_button = self._create_button(
            ">", tr("Next simulation time step")
        )
        self.last_button = self._create_button(
            ">|", tr("Last simulation time step")
        )

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setMinimumWidth(180)
        # Render only after releasing the slider handle.
        self.slider.setTracking(False)

        self.time_combo = QComboBox(self)
        self.time_combo.setMinimumWidth(86)
        self.time_combo.setToolTip(tr("Select an exact simulation time"))

        self.node_result_combo = QComboBox(self)
        self.node_result_combo.setMinimumWidth(135)
        self.node_result_combo.setToolTip(
            tr("Select the node result displayed on the map")
        )
        self.node_result_combo.addItem(tr("Pressure"), "pressure")
        self.node_result_combo.addItem(tr("Head"), "head")
        self.node_result_combo.addItem(tr("Demand"), "demand")
        self.node_result_combo.addItem(tr("Quality"), "quality")

        self.link_result_combo = QComboBox(self)
        self.link_result_combo.setMinimumWidth(145)
        self.link_result_combo.setToolTip(
            tr("Select the link result displayed on the map")
        )
        self.link_result_combo.addItem(tr("Flow rate"), "flowrate")
        self.link_result_combo.addItem(tr("Velocity"), "velocity")
        self.link_result_combo.addItem(tr("Headloss"), "headloss")
        self.link_result_combo.addItem(tr("Status"), "status")
        self.link_result_combo.addItem(tr("Setting"), "setting")
        self.link_result_combo.addItem(tr("Friction factor"), "friction_factor")
        self.link_result_combo.addItem(tr("Reaction rate"), "reaction_rate")
        self.link_result_combo.addItem(tr("Quality"), "quality")

        self.time_label = QLabel(self)
        self.time_label.setMinimumWidth(105)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 0, 2, 0)
        layout.setSpacing(3)
        layout.addWidget(self.first_button)
        layout.addWidget(self.previous_button)
        layout.addWidget(self.slider)
        layout.addWidget(self.next_button)
        layout.addWidget(self.last_button)
        layout.addWidget(self.time_combo)
        layout.addWidget(self.time_label)
        layout.addWidget(QLabel(tr("Nodes:"), self))
        layout.addWidget(self.node_result_combo)
        layout.addWidget(QLabel(tr("Links:"), self))
        layout.addWidget(self.link_result_combo)

        self.first_button.clicked.connect(self.go_to_first_frame)
        self.previous_button.clicked.connect(lambda: self.change_frame(-1))
        self.next_button.clicked.connect(lambda: self.change_frame(1))
        self.last_button.clicked.connect(self.go_to_last_frame)
        self.slider.valueChanged.connect(self.set_frame)
        self.time_combo.currentIndexChanged.connect(self.set_frame)
        self.node_result_combo.currentIndexChanged.connect(
            self.apply_selected_node_style
        )
        self.link_result_combo.currentIndexChanged.connect(
            self.apply_selected_link_style
        )

        self.controller.temporalExtentsChanged.connect(self.update_from_controller)
        self.controller.temporalFrameDurationChanged.connect(self.update_from_controller)
        QgsProject.instance().layersAdded.connect(self._results_layers_added)

        self.update_from_controller()

    def _create_button(self, text: str, tooltip: str) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        return button

    @staticmethod
    def _sequence_length(value) -> int:
        if value is None or isinstance(value, (str, bytes)):
            return 0
        if isinstance(value, Sequence):
            return len(value)
        return 0

    def result_frame_count(self) -> int:
        """Return the number of real result samples stored in result layers."""
        candidate_fields = (
            "pressure",
            "head",
            "demand",
            "flowrate",
            "velocity",
            "headloss",
        )
        best_count = 0
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            available = [name for name in candidate_fields if layer.fields().indexFromName(name) >= 0]
            if not available:
                continue
            feature = next(layer.getFeatures(), None)
            if feature is None:
                continue
            for field_name in available:
                best_count = max(best_count, self._sequence_length(feature[field_name]))
        return best_count

    def configured_duration_hours(self) -> int:
        duration = ProjectSettings().get(SettingKey.SIMULATION_DURATION, 0)
        try:
            return max(0, int(duration))
        except (TypeError, ValueError):
            return 0

    def frame_count(self) -> int:
        # Prefer actual WNTR result arrays. Project settings are only a fallback
        # used before result layers have been created.
        result_count = self.result_frame_count()
        if result_count > 0:
            return result_count
        return max(1, self.configured_duration_hours() + 1)

    def simulation_duration_hours(self) -> int:
        return max(0, self.frame_count() - 1)

    def _results_layers_added(self, layers) -> None:
        self.update_from_controller()

        # Processing can still apply Gusnet's default renderer after layersAdded.
        # Apply both selected result styles once the event loop has finished
        # registering and styling the output layers.
        from qgis.PyQt.QtCore import QTimer

        QTimer.singleShot(0, self.apply_selected_styles)
        QTimer.singleShot(300, self.apply_selected_styles)

    def current_frame(self) -> int:
        maximum = self.frame_count() - 1
        return max(0, min(int(self.controller.currentFrameNumber()), maximum))

    @staticmethod
    def format_hour(frame: int) -> str:
        return f"{max(0, int(frame)):02d}:00"

    def rebuild_time_combo(self, maximum: int) -> None:
        expected_count = maximum + 1
        if self.time_combo.count() == expected_count:
            return

        self.time_combo.blockSignals(True)
        try:
            self.time_combo.clear()
            for frame in range(expected_count):
                self.time_combo.addItem(self.format_hour(frame), frame)
        finally:
            self.time_combo.blockSignals(False)

    def set_frame(self, frame: int) -> None:
        maximum = self.frame_count() - 1
        frame = max(0, min(int(frame), maximum))

        if self.controller.currentFrameNumber() != frame:
            self.controller.setCurrentFrameNumber(frame)

        self.update_from_controller()

    def change_frame(self, difference: int) -> None:
        self.set_frame(self.current_frame() + difference)

    def go_to_first_frame(self) -> None:
        self.set_frame(0)

    def go_to_last_frame(self) -> None:
        self.set_frame(self.frame_count() - 1)

    def update_from_controller(self, *args) -> None:
        del args
        self.update_controller_duration()

        duration_hours = self.simulation_duration_hours()
        maximum = self.frame_count() - 1
        current = self.current_frame()

        self.rebuild_time_combo(maximum)

        self.slider.blockSignals(True)
        self.slider.setRange(0, maximum)
        self.slider.setValue(current)
        self.slider.blockSignals(False)

        self.time_combo.blockSignals(True)
        self.time_combo.setCurrentIndex(current)
        self.time_combo.blockSignals(False)

        self.time_label.setText(
            f"{self.format_hour(current)} / {self.format_hour(duration_hours)}"
        )

        self.first_button.setEnabled(current > 0)
        self.previous_button.setEnabled(current > 0)
        self.next_button.setEnabled(current < maximum)
        self.last_button.setEnabled(current < maximum)

    def update_controller_duration(self) -> None:
        if self._updating_temporal_range:
            return

        duration_hours = self.simulation_duration_hours()
        current_extents = self.controller.temporalExtents()
        start_time = current_extents.begin()

        # QGIS treats the temporal end as exclusive. Add one report step so
        # the final EPANET time (e.g. 48:00) becomes an independently selectable
        # frame instead of ending at 47:00.
        desired_end_time = start_time.addSecs((duration_hours + 1) * 3600)

        self._updating_temporal_range = True
        try:
            self.controller.setFrameDuration(
                QgsInterval(1, Qgis.TemporalUnit.Hours)
            )
            if current_extents.end() != desired_end_time:
                self.controller.setTemporalExtents(
                    QgsDateTimeRange(start_time, desired_end_time)
                )
        finally:
            self._updating_temporal_range = False

    def _apply_style_to_result_layers(
        self, layer_type: ResultLayer, field_name: str
    ) -> bool:
        styled_any = False
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            if layer.fields().indexFromName(field_name) < 0:
                continue

            # Avoid applying a node field to a link layer (or vice versa) when
            # both happen to contain a field with the same name, e.g. quality.
            geometry_type = layer.geometryType()
            if layer_type is ResultLayer.NODES and geometry_type != Qgis.GeometryType.Point:
                continue
            if layer_type is ResultLayer.LINKS and geometry_type != Qgis.GeometryType.Line:
                continue

            try:
                apply_qev_result_style(layer, layer_type, field_name)
            except (KeyError, ValueError):
                continue
            styled_any = True
        return styled_any

    def apply_selected_node_style(self, *_args) -> None:
        field_name = self.node_result_combo.currentData()
        if field_name and self._apply_style_to_result_layers(
            ResultLayer.NODES, field_name
        ):
            self.iface.mapCanvas().refresh()

    def apply_selected_link_style(self, *_args) -> None:
        field_name = self.link_result_combo.currentData()
        if field_name and self._apply_style_to_result_layers(
            ResultLayer.LINKS, field_name
        ):
            self.iface.mapCanvas().refresh()

    def apply_selected_styles(self) -> None:
        self.apply_selected_node_style()
        self.apply_selected_link_style()

    def destroy(self) -> None:
        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.temporalExtentsChanged.disconnect(self.update_from_controller)
        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.temporalFrameDurationChanged.disconnect(self.update_from_controller)
        with contextlib.suppress(TypeError, RuntimeError):
            QgsProject.instance().layersAdded.disconnect(self._results_layers_added)
