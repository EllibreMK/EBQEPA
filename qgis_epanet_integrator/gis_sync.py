from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from qgis.core import QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface
from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QLabel,
    QPushButton,
    QVBoxLayout,
)


@dataclass
class SyncCallbacks:
    find_mains: Callable[[], object]
    add_mains: Callable[[], object]
    find_services: Callable[[], object]
    add_services: Callable[[], object]
    validate_model: Callable[[], object]
    refresh_mapping: Callable[[], object]
    show_mapping: Callable[[], object]


def _layer_by_kind(kind: str) -> QgsVectorLayer | None:
    candidates: list[QgsVectorLayer] = []
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if str(layer.customProperty("qgis_epanet_integrator/source_kind", "")) == kind:
            candidates.append(layer)
    if not candidates:
        return None
    # Prefer the newest layer added to the project.
    return candidates[-1]


def _selection_text(layer: QgsVectorLayer | None, group_field: str | None = None) -> str:
    if layer is None:
        return "brak warstwy roboczej"
    count = int(layer.featureCount())
    selected = list(layer.selectedFeatures())
    if group_field and group_field in layer.fields().names():
        groups = {
            str(f[group_field] or "").strip()
            for f in selected
            if str(f[group_field] or "").strip()
        }
        return f"{count} fragmentów; zaznaczono {len(groups)} grup"
    return f"{count} fragmentów; zaznaczono {len(selected)}"


class GisSyncDialog(QDialog):
    """One place for the incremental GIS -> EPANET synchronization workflow."""

    def __init__(self, iface: QgisInterface, callbacks: SyncCallbacks, parent=None):
        super().__init__(parent or iface.mainWindow())
        self.iface = iface
        self.callbacks = callbacks
        self.setWindowTitle("Synchronizacja GIS → model EPANET")
        self.setMinimumWidth(620)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)

        root = QVBoxLayout(self)

        intro = QLabel(
            "Kolejność pracy: znajdź zmiany GIS → zaznacz tylko to, co chcesz dodać → "
            "dodaj do modelu → sprawdź topologię/model. Pozostałych obiektów z warstw "
            "tymczasowych nie trzeba usuwać."
        )
        intro.setWordWrap(True)
        root.addWidget(intro)

        self.main_status = QLabel()
        self.service_status = QLabel()
        self.topology_status = QLabel()
        self.mapping_status = QLabel()

        mains = QGroupBox("1. Sieć główna")
        mains_grid = QGridLayout(mains)
        b_find_mains = QPushButton("Znajdź nowe fragmenty GIS")
        b_add_mains = QPushButton("Dodaj zaznaczone fragmenty")
        mains_grid.addWidget(b_find_mains, 0, 0)
        mains_grid.addWidget(b_add_mains, 0, 1)
        mains_grid.addWidget(self.main_status, 1, 0, 1, 2)
        root.addWidget(mains)

        services = QGroupBox("2. Przyłącza")
        services_grid = QGridLayout(services)
        b_find_services = QPushButton("Znajdź i pogrupuj nowe przyłącza")
        b_add_services = QPushButton("Dodaj zaznaczone grupy przyłączy")
        services_grid.addWidget(b_find_services, 0, 0)
        services_grid.addWidget(b_add_services, 0, 1)
        services_grid.addWidget(self.service_status, 1, 0, 1, 2)
        root.addWidget(services)

        check = QGroupBox("3. Kontrola")
        check_grid = QGridLayout(check)
        b_validate = QPushButton("Sprawdź model")
        check_grid.addWidget(b_validate, 0, 0)
        check_grid.addWidget(self.topology_status, 1, 0)
        root.addWidget(check)

        billing = QGroupBox("4. Trwałe mapowanie / przyszły billing")
        billing_grid = QGridLayout(billing)
        billing_label = QLabel(
            "Po dodaniu przyłączy Integrator zapisuje trwałą relację GIS przyłącza → node_id. "
            "Pole CID jest już przygotowane, ale pozostaje puste do czasu integracji z billingiem. "
            "Odświeżanie mapowania nie kasuje ręcznie przypisanego CID."
        )
        billing_label.setWordWrap(True)
        b_refresh_mapping = QPushButton("Odśwież mapowanie przyłącza → node_id")
        b_show_mapping = QPushButton("Pokaż tabelę mapowania")
        billing_grid.addWidget(billing_label, 0, 0, 1, 2)
        billing_grid.addWidget(b_refresh_mapping, 1, 0)
        billing_grid.addWidget(b_show_mapping, 1, 1)
        billing_grid.addWidget(self.mapping_status, 2, 0, 1, 2)
        root.addWidget(billing)

        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)
        root.addWidget(line)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        b_find_mains.clicked.connect(lambda: self._run(self.callbacks.find_mains))
        b_add_mains.clicked.connect(lambda: self._run(self.callbacks.add_mains))
        b_find_services.clicked.connect(lambda: self._run(self.callbacks.find_services))
        b_add_services.clicked.connect(lambda: self._run(self.callbacks.add_services))
        b_validate.clicked.connect(lambda: self._run(self.callbacks.validate_model))
        b_refresh_mapping.clicked.connect(lambda: self._run(self.callbacks.refresh_mapping))
        b_show_mapping.clicked.connect(lambda: self._run(self.callbacks.show_mapping))

        self.refresh_status()

    def _run(self, callback: Callable[[], object]) -> None:
        callback()
        self.refresh_status()

    def refresh_status(self) -> None:
        network = _layer_by_kind("network")
        services = _layer_by_kind("service_connections")
        issues = _layer_by_kind("service_topology_issues")
        endpoints = _layer_by_kind("service_connection_endpoints")

        self.main_status.setText("Stan: " + _selection_text(network))
        self.service_status.setText("Stan: " + _selection_text(services, "grupa"))

        if issues is not None and issues.featureCount() > 0:
            self.topology_status.setText(
                f"Ostatnia kontrola przyłączy: {issues.featureCount()} problemów do sprawdzenia."
            )
        elif endpoints is not None:
            linked = 0
            if "node_id" in endpoints.fields().names():
                linked = sum(
                    1
                    for f in endpoints.getFeatures()
                    if str(f["node_id"] or "").strip()
                )
            self.topology_status.setText(
                f"Końcówki przyłączy powiązane z węzłami: {linked}. Kontrolę modelu można uruchomić poniżej."
            )
        else:
            self.topology_status.setText("Kontrola będzie dostępna po dodaniu zmian do modelu.")

        try:
            from qgis_epanet_integrator.service_mapping import mapping_counts
            counts = mapping_counts()
            if counts["total"]:
                self.mapping_status.setText(
                    f"Mapowanie trwałe: {counts['total']} rekordów; "
                    f"z node_id: {counts['with_node']}; z CID: {counts['with_cid']}; "
                    f"oczekuje na CID: {counts['without_cid']}."
                )
            else:
                self.mapping_status.setText("Mapowanie trwałe: jeszcze brak rekordów.")
        except Exception as exc:
            self.mapping_status.setText(f"Mapowanie trwałe: nie udało się odczytać stanu ({exc}).")


def show_gis_sync_dialog(
    iface: QgisInterface,
    callbacks: SyncCallbacks,
) -> GisSyncDialog:
    dialog = GisSyncDialog(iface, callbacks)
    dialog.show()
    dialog.raise_()
    dialog.activateWindow()
    return dialog
