from __future__ import annotations

import contextlib

from qgis.core import QgsDateTimeRange, QgsTemporalNavigationObject
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QSlider,
    QToolButton,
    QWidget,
)

from gusnet.i18n import tr
from gusnet.settings import ProjectSettings, SettingKey

class ResultsTimeControl(QWidget):
    """EPANET-style control for QGIS temporal simulation results."""

    def __init__(
        self,
        iface: QgisInterface,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)

        self.controller: QgsTemporalNavigationObject = (
            iface.mapCanvas().temporalController()
        )
        self._updating_temporal_range = False

        self.first_button = self._create_button(
            "|<",
            tr("First simulation time step"),
        )
        self.previous_button = self._create_button(
            "<",
            tr("Previous simulation time step"),
        )
        self.next_button = self._create_button(
            ">",
            tr("Next simulation time step"),
        )
        self.last_button = self._create_button(
            ">|",
            tr("Last simulation time step"),
        )

        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setMinimumWidth(180)

        # Zmiana czasu dopiero po puszczeniu suwaka.
        # Zapobiega wielokrotnemu renderowaniu ciężkich warstw.
        self.slider.setTracking(False)

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
        layout.addWidget(self.time_label)

        self.first_button.clicked.connect(self.go_to_first_frame)
        self.previous_button.clicked.connect(
            lambda: self.change_frame(-1)
        )
        self.next_button.clicked.connect(
            lambda: self.change_frame(1)
        )
        self.last_button.clicked.connect(self.go_to_last_frame)
        self.slider.valueChanged.connect(self.set_frame)

        # Nie podłączamy stateChanged. Ten sygnał może być emitowany
        # bardzo często podczas renderowania i odtwarzania.
        self.controller.temporalExtentsChanged.connect(
            self.update_from_controller
        )
        self.controller.temporalFrameDurationChanged.connect(
            self.update_from_controller
        )

        self.update_from_controller()

    def _create_button(
        self,
        text: str,
        tooltip: str,
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tooltip)
        return button

    def frame_count(self) -> int:
        return max(1, int(self.controller.totalFrameCount()))

    def current_frame(self) -> int:
        maximum = self.frame_count() - 1
        return max(
            0,
            min(int(self.controller.currentFrameNumber()), maximum),
        )

    def set_frame(self, frame: int) -> None:
        frame_count = max(
            1,
            self.simulation_duration_hours(),
        )
        maximum = frame_count - 1

        frame = max(
            0,
            min(int(frame), maximum),
        )

        if self.controller.currentFrameNumber() != frame:
            self.controller.setCurrentFrameNumber(frame)

        self.update_from_controller()

    def change_frame(self, difference: int) -> None:
        self.set_frame(self.current_frame() + difference)

    def go_to_first_frame(self) -> None:
        self.set_frame(0)

    def go_to_last_frame(self) -> None:
        frame_count = max(
            1,
            self.simulation_duration_hours(),
        )
        self.set_frame(frame_count - 1)

    def update_from_controller(self, *args) -> None:
        del args

        self.update_controller_duration()

        duration_hours = self.simulation_duration_hours()
        frame_count = max(1, duration_hours)
        maximum = frame_count - 1

        current = max(
            0,
            min(
                int(self.controller.currentFrameNumber()),
                maximum,
            ),
        )

        self.slider.blockSignals(True)
        self.slider.setRange(0, maximum)
        self.slider.setValue(current)
        self.slider.blockSignals(False)

        self.time_label.setText(
            f"{current:02d}:00 / {duration_hours:02d}:00"
        )

        self.first_button.setEnabled(current > 0)
        self.previous_button.setEnabled(current > 0)
        self.next_button.setEnabled(current < maximum)
        self.last_button.setEnabled(current < maximum)

    def destroy(self) -> None:
        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.temporalExtentsChanged.disconnect(
                self.update_from_controller
            )

        with contextlib.suppress(TypeError, RuntimeError):
            self.controller.temporalFrameDurationChanged.disconnect(
                self.update_from_controller
            )

        self.deleteLater()
        
    def simulation_duration_hours(self) -> int:
        duration = ProjectSettings().get(
            SettingKey.SIMULATION_DURATION,
            0,
        )

        try:
            return max(0, int(duration))
        except (TypeError, ValueError):
            return 0   
            
    def update_controller_duration(self) -> None:
        if self._updating_temporal_range:
            return

        duration_hours = max(
            1,
            self.simulation_duration_hours(),
        )

        current_extents = self.controller.temporalExtents()
        start_time = current_extents.begin()
        desired_end_time = start_time.addSecs(
            duration_hours * 3600
        )

        if current_extents.end() == desired_end_time:
            return

        self._updating_temporal_range = True
        try:
            self.controller.setTemporalExtents(
                QgsDateTimeRange(
                    start_time,
                    desired_end_time,
                )
            )
        finally:
            self._updating_temporal_range = False            