from __future__ import annotations

import math

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsNetworkAccessManager,
    QgsPointXY,
    QgsProject,
    QgsRasterLayer,
)
from qgis.PyQt.QtCore import QEventLoop, QTimer, QUrl, QUrlQuery
from qgis.PyQt.QtNetwork import QNetworkReply, QNetworkRequest


GUGIK_NMT_URLS = (
    "https://services.gugik.gov.pl/nmt/",
    "https://integracja.gugik.gov.pl/nmt/",
)
GUGIK_NMT_CRS = QgsCoordinateReferenceSystem("EPSG:2180")


class ElevationLookupError(RuntimeError):
    pass


def _parse_height(text: str) -> float:
    value = (text or "").strip().replace(",", ".")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ElevationLookupError(f"Nieprawidłowa odpowiedź NMT GUGiK: {text!r}") from exc
    if not math.isfinite(result):
        raise ElevationLookupError(f"Nieprawidłowa wysokość NMT GUGiK: {text!r}")
    return result


def _gugik_request_height(base_url: str, p92: QgsPointXY, timeout_ms: int) -> float:
    url = QUrl(base_url)
    query = QUrlQuery()
    query.addQueryItem("request", "GetHByXY")
    query.addQueryItem("x", str(p92.y()))
    query.addQueryItem("y", str(p92.x()))
    url.setQuery(query)

    request = QNetworkRequest(url)
    try:
        if hasattr(QNetworkRequest, "KnownHeaders"):
            header = QNetworkRequest.KnownHeaders.UserAgentHeader
        else:
            header = QNetworkRequest.UserAgentHeader
        request.setHeader(header, "QGIS-Plugin-Integrator-QGIS-EPANET")
    except Exception:
        pass

    reply = QgsNetworkAccessManager.instance().get(request)
    loop = QEventLoop()
    timer = QTimer()
    timer.setSingleShot(True)
    timer.timeout.connect(loop.quit)
    reply.finished.connect(loop.quit)
    timer.start(timeout_ms)
    loop.exec()

    if not reply.isFinished():
        reply.abort()
        reply.deleteLater()
        raise ElevationLookupError("przekroczono czas oczekiwania")

    if hasattr(QNetworkReply, "NetworkError"):
        no_error = QNetworkReply.NetworkError.NoError
    else:
        no_error = QNetworkReply.NoError
    if reply.error() != no_error:
        message = reply.errorString()
        reply.deleteLater()
        raise ElevationLookupError(message)

    text = bytes(reply.readAll()).decode("utf-8", errors="replace").strip()
    reply.deleteLater()
    return _parse_height(text)


def gugik_terrain_height(point: QgsPointXY, source_crs, timeout_ms: int = 12000) -> float:
    """Return NMT terrain elevation from GUGiK GetHByXY.

    The public service expects PUWG 1992 (EPSG:2180). Its x/y convention is
    geodetic: x=northing, y=easting, hence the deliberate swap relative to
    QgsPointXY(x=easting, y=northing). Temporary HTTP 502/503 errors are common
    enough that the lookup retries and also tries the GUGiK integration host.
    """
    transform = QgsCoordinateTransform(source_crs, GUGIK_NMT_CRS, QgsProject.instance())
    p92 = transform.transform(point)

    errors = []
    # Two attempts on each official GUGiK host. A single 502 must not abort an
    # otherwise valid model update.
    for base_url in GUGIK_NMT_URLS:
        for attempt in range(2):
            try:
                return _gugik_request_height(base_url, p92, timeout_ms)
            except ElevationLookupError as exc:
                errors.append(f"{base_url} próba {attempt + 1}: {exc}")
                # Short event-loop delay before retry, without blocking QGIS UI.
                if attempt == 0:
                    wait_loop = QEventLoop()
                    QTimer.singleShot(700, wait_loop.quit)
                    wait_loop.exec()

    detail = "; ".join(errors[-4:])
    raise ElevationLookupError(
        "NMT GUGiK jest chwilowo niedostępne po ponowieniach. "
        f"Szczegóły: {detail}"
    )


def raster_terrain_height(point: QgsPointXY, source_crs, raster: QgsRasterLayer) -> float:
    if raster is None or not raster.isValid():
        raise ElevationLookupError("Wybrana warstwa NMT nie jest prawidłowym rastrem.")
    transform = QgsCoordinateTransform(source_crs, raster.crs(), QgsProject.instance())
    rp = transform.transform(point)
    value, ok = raster.dataProvider().sample(rp, 1)
    if not ok or value is None:
        raise ElevationLookupError("Brak wartości NMT w miejscu nowego węzła.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ElevationLookupError("Nie można odczytać wysokości z rastra NMT.") from exc
    if not math.isfinite(result):
        raise ElevationLookupError("Raster NMT zwrócił nieprawidłową wysokość.")
    return result
