from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone

from qgis.core import Qgis, QgsProject, QgsVectorLayer
from qgis.gui import QgisInterface
from qgis.PyQt.QtWidgets import QMessageBox

from qgis_epanet_integrator.gis_compare import _configured_model_pipe_layer

TABLE_NAME = "integrator_service_node_mapping"
LAYER_NAME = "Mapowanie przyłączy → węzły"


def _model_gpkg_path() -> str | None:
    layer = _configured_model_pipe_layer()
    if layer is None:
        return None
    source = str(layer.source() or "")
    path = source.split("|", 1)[0]
    if path.lower().endswith(".gpkg") and os.path.isfile(path):
        return path
    return None


def _mapping_db_path() -> str | None:
    # Prefer the hydraulic model GeoPackage, so the mapping travels together
    # with the model. If the model is not a GPKG, use a sidecar GPKG next to the
    # saved QGIS project.
    path = _model_gpkg_path()
    if path:
        return path
    project = QgsProject.instance()
    home = str(project.homePath() or "").strip()
    if home and os.path.isdir(home):
        return os.path.join(home, "integrator_epanet_mapping.gpkg")
    return None


def _ensure_table(path: str) -> None:
    conn = sqlite3.connect(path)
    try:
        conn.execute(
            f'''CREATE TABLE IF NOT EXISTS "{TABLE_NAME}" (
                mapping_id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_layer TEXT NOT NULL DEFAULT '',
                source_layer_id TEXT NOT NULL DEFAULT '',
                source_key TEXT NOT NULL DEFAULT '',
                gis_id TEXT NOT NULL DEFAULT '',
                gis_fid INTEGER,
                group_id TEXT NOT NULL DEFAULT '',
                role TEXT NOT NULL DEFAULT '',
                node_id TEXT NOT NULL DEFAULT '',
                cid TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'BEZ_CID',
                created_at TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL DEFAULT '',
                UNIQUE(source_layer_id, source_key, role)
            )'''
        )
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{TABLE_NAME}_node" ON "{TABLE_NAME}"(node_id)'
        )
        conn.execute(
            f'CREATE INDEX IF NOT EXISTS "idx_{TABLE_NAME}_cid" ON "{TABLE_NAME}"(cid)'
        )
        # Register as a GeoPackage attribute table when possible. This is safe
        # for an existing GPKG and makes the table visible to QGIS/OGR.
        try:
            row = conn.execute(
                "SELECT 1 FROM gpkg_contents WHERE table_name=?", (TABLE_NAME,)
            ).fetchone()
            if row is None:
                now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
                conn.execute(
                    "INSERT INTO gpkg_contents (table_name, data_type, identifier, description, last_change) "
                    "VALUES (?, 'attributes', ?, ?, ?)",
                    (TABLE_NAME, LAYER_NAME, "Trwałe mapowanie GIS przyłączy do węzłów EPANET i przyszłych CID.", now),
                )
        except sqlite3.Error:
            pass
        conn.commit()
    finally:
        conn.close()


def _value(feature, field: str) -> str:
    if field not in feature.fields().names():
        return ""
    return str(feature[field] or "").strip()


def save_endpoint_mapping(
    endpoints: QgsVectorLayer,
    fragments: QgsVectorLayer | None = None,
    group_ids: list[str] | None = None,
) -> dict[str, int | str]:
    path = _mapping_db_path()
    if not path:
        raise RuntimeError(
            "Nie można ustalić trwałego miejsca zapisu mapowania. Model nie jest w GeoPackage, "
            "a projekt QGIS nie ma zapisanego katalogu."
        )
    _ensure_table(path)

    source_layer_id = str(endpoints.customProperty("qgis_epanet_integrator/source_layer_id", "") or "")
    source_layer_name = ""
    source_layer = QgsProject.instance().mapLayer(source_layer_id) if source_layer_id else None
    if isinstance(source_layer, QgsVectorLayer):
        source_layer_name = source_layer.name()

    # Fall back to the fragment source data if the endpoint layer predates
    # source-name propagation.
    if fragments is not None:
        source_layer_id = source_layer_id or str(
            fragments.customProperty("qgis_epanet_integrator/source_layer_id", "") or ""
        )
        source_layer = QgsProject.instance().mapLayer(source_layer_id) if source_layer_id else None
        if isinstance(source_layer, QgsVectorLayer):
            source_layer_name = source_layer.name()

    wanted = set(group_ids or [])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    inserted = 0
    updated = 0
    skipped = 0

    conn = sqlite3.connect(path)
    try:
        for feature in endpoints.getFeatures():
            group_id = _value(feature, "grupa")
            if wanted and group_id not in wanted:
                continue
            node_id = _value(feature, "node_id")
            role = _value(feature, "rola")
            gis_id = _value(feature, "gis_id")
            gis_fid_txt = _value(feature, "gis_fid")
            try:
                gis_fid = int(gis_fid_txt) if gis_fid_txt else None
            except ValueError:
                gis_fid = None
            source_key = gis_id or (f"FID:{gis_fid}" if gis_fid is not None else "")
            if not source_key:
                # A group/point without any durable GIS identity is not safe to
                # persist as a long-term mapping.
                skipped += 1
                continue

            old = conn.execute(
                f'SELECT mapping_id, cid, created_at FROM "{TABLE_NAME}" '
                "WHERE source_layer_id=? AND source_key=? AND role=?",
                (source_layer_id, source_key, role),
            ).fetchone()
            status = "BEZ_CID" if node_id else "BRAK_NODE_ID"
            if old is None:
                conn.execute(
                    f'''INSERT INTO "{TABLE_NAME}"
                    (source_layer, source_layer_id, source_key, gis_id, gis_fid, group_id, role,
                     node_id, cid, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?, ?)''',
                    (
                        source_layer_name, source_layer_id, source_key, gis_id, gis_fid,
                        group_id, role, node_id, status, now, now,
                    ),
                )
                inserted += 1
            else:
                cid = str(old[1] or "").strip()
                # Never erase a CID already assigned by the user/future billing
                # workflow when refreshing geometry/node links.
                status = "POWIAZANY_CID" if cid else status
                conn.execute(
                    f'''UPDATE "{TABLE_NAME}" SET
                    source_layer=?, gis_id=?, gis_fid=?, group_id=?, node_id=?,
                    status=?, updated_at=? WHERE mapping_id=?''',
                    (
                        source_layer_name, gis_id, gis_fid, group_id, node_id,
                        status, now, int(old[0]),
                    ),
                )
                updated += 1
        conn.commit()
    finally:
        conn.close()

    return {"inserted": inserted, "updated": updated, "skipped": skipped, "path": path}


def mapping_counts() -> dict[str, int | str]:
    path = _mapping_db_path()
    if not path or not os.path.isfile(path):
        return {"total": 0, "with_node": 0, "with_cid": 0, "without_cid": 0, "path": path or ""}
    _ensure_table(path)
    conn = sqlite3.connect(path)
    try:
        total = int(conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}"').fetchone()[0])
        with_node = int(conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}" WHERE TRIM(node_id)<>\'\'').fetchone()[0])
        with_cid = int(conn.execute(f'SELECT COUNT(*) FROM "{TABLE_NAME}" WHERE TRIM(cid)<>\'\'').fetchone()[0])
    finally:
        conn.close()
    return {
        "total": total,
        "with_node": with_node,
        "with_cid": with_cid,
        "without_cid": max(0, total - with_cid),
        "path": path,
    }


def load_mapping_layer(iface: QgisInterface, show_message: bool = True) -> QgsVectorLayer | None:
    path = _mapping_db_path()
    if not path:
        if show_message:
            QMessageBox.warning(iface.mainWindow(), "Integrator QGIS-EPANET", "Brak trwałego miejsca zapisu mapowania.")
        return None
    _ensure_table(path)

    # Reuse an already loaded mapping table.
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        if str(layer.customProperty("qgis_epanet_integrator/source_kind", "")) == "service_node_mapping":
            iface.setActiveLayer(layer)
            return layer

    layer = QgsVectorLayer(f"{path}|layername={TABLE_NAME}", LAYER_NAME, "ogr")
    if not layer.isValid():
        if show_message:
            QMessageBox.warning(
                iface.mainWindow(), "Integrator QGIS-EPANET",
                f"Tabela mapowania istnieje, ale QGIS nie mógł jej otworzyć:\n{path}",
            )
        return None
    layer.setCustomProperty("qgis_epanet_integrator/source_kind", "service_node_mapping")
    QgsProject.instance().addMapLayer(layer)
    iface.setActiveLayer(layer)
    if show_message:
        iface.messageBar().pushMessage(
            "Integrator QGIS-EPANET",
            "Otworzono trwałą tabelę mapowania. Pole CID można później uzupełniać bez utraty przy odświeżaniu mapowania.",
            level=Qgis.MessageLevel.Info,
            duration=8,
        )
    return layer


def refresh_mapping_from_project(iface: QgisInterface) -> dict[str, int | str] | None:
    endpoints = None
    fragments = None
    for layer in QgsProject.instance().mapLayers().values():
        if not isinstance(layer, QgsVectorLayer):
            continue
        kind = str(layer.customProperty("qgis_epanet_integrator/source_kind", ""))
        if kind == "service_connection_endpoints":
            endpoints = layer
        elif kind == "service_connections":
            fragments = layer
    if endpoints is None:
        QMessageBox.information(
            iface.mainWindow(),
            "Integrator QGIS-EPANET",
            "Brak warstwy „Końcówki nowych przyłączy GIS”. Najpierw wyszukaj przyłącza.",
        )
        return None
    result = save_endpoint_mapping(endpoints, fragments, None)
    iface.messageBar().pushMessage(
        "Integrator QGIS-EPANET",
        (
            f"Mapowanie przyłączy → węzły: dodano {result['inserted']}, "
            f"odświeżono {result['updated']}, pominięto {result['skipped']} bez trwałego ID GIS."
        ),
        level=Qgis.MessageLevel.Success,
        duration=9,
    )
    return result
