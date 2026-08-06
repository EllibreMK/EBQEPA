from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from qgis_epanet_integrator.gusnet_processing.empty_model import TemplateLayers
from qgis_epanet_integrator.gusnet_processing.import_inp import ImportInp
from qgis_epanet_integrator.gusnet_processing.run_simulation import ExportInpFile, RunSimulation


class Provider(QgsProcessingProvider):
    def id(self) -> str:
        return "qgis_epanet_integrator"

    def name(self) -> str:
        return "Integrator QGIS-EPANET"

    def icon(self):
        return QIcon("qgis_epanet_integrator:logo.svg")

    def loadAlgorithms(self) -> None:  # noqa N802
        self.addAlgorithm(RunSimulation())
        self.addAlgorithm(ImportInp())
        self.addAlgorithm(TemplateLayers())
        self.addAlgorithm(ExportInpFile())
