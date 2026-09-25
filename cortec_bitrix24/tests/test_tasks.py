# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
Tests de la fase de Tareas y proyectos.

Cubren los tres puntos donde un error no da excepción, solo datos mal
migrados que nadie mira hasta meses después:

  * el prefijo de `ufCrmTask` — confundir `CO_` con `C_` cuelga la tarea
    de un contacto que no tiene nada que ver;
  * las dos escalas de prioridad distintas que conviven en el mismo
    portal (0/1/2 en tareas, 1/2/3 en actividades del CRM);
  * el mapeo de estados, donde el 4 de Bitrix24 no es evidente.
"""

import unittest

from cortec_bitrix24.migration.activities import ACTIVITY_PRIORITY_MAP
from cortec_bitrix24.migration.tasks import (
    CRM_BINDINGS,
    PRIORITY_MAP,
    STATUS_MAP,
    _as_list,
    _horas,
)


class TestMapeoDeEstados(unittest.TestCase):
    def test_los_siete_estados_de_bitrix_tienen_destino(self):
        for code in range(1, 8):
            self.assertIn(code, STATUS_MAP, f"falta el estado {code}")

    def test_solo_son_validos_los_estados_de_crm_task(self):
        validos = {"Backlog", "Todo", "In Progress", "Done", "Canceled"}
        self.assertTrue(set(STATUS_MAP.values()) <= validos)

    def test_completada_y_supuestamente_completada_cuentan_como_hechas(self):
        # En un portal que se retira, nadie va a "aceptar" ya el 4.
        self.assertEqual(STATUS_MAP[4], "Done")
        self.assertEqual(STATUS_MAP[5], "Done")

    def test_rechazada_no_es_lo_mismo_que_aplazada(self):
        self.assertEqual(STATUS_MAP[7], "Canceled")
        self.assertEqual(STATUS_MAP[6], "Backlog")


class TestPrioridades(unittest.TestCase):
    def test_las_dos_escalas_del_portal_son_distintas(self):
        # Tareas: 0/1/2. Actividades del CRM: 1/2/3. Aplicar una a la
        # otra desplaza todas las prioridades un escalón.
        self.assertEqual(PRIORITY_MAP, {0: "Low", 1: "Medium", 2: "High"})
        self.assertEqual(ACTIVITY_PRIORITY_MAP, {1: "Low", 2: "Medium", 3: "High"})
        self.assertNotEqual(PRIORITY_MAP.get(1), ACTIVITY_PRIORITY_MAP.get(1))

    def test_solo_valores_admitidos_por_crm_task(self):
        validos = {"Low", "Medium", "High"}
        self.assertTrue(set(PRIORITY_MAP.values()) <= validos)
        self.assertTrue(set(ACTIVITY_PRIORITY_MAP.values()) <= validos)


class TestVinculoCrm(unittest.TestCase):
    def test_co_se_evalua_antes_que_c(self):
        # "CO_7" empieza por "C_"? No, pero sí por "CO_". El orden de la
        # tupla es lo que garantiza que una empresa no se lea como
        # contacto; si alguien la reordena, este test lo detecta.
        prefijos = [p for p, _, _ in CRM_BINDINGS]
        self.assertLess(prefijos.index("CO_"), prefijos.index("C_"))

    def test_cada_prefijo_apunta_a_un_doctype_distinto(self):
        doctypes = [d for _, _, d in CRM_BINDINGS]
        self.assertEqual(len(doctypes), len(set(doctypes)))

    def test_los_cuatro_prefijos_de_bitrix_estan_cubiertos(self):
        self.assertEqual(
            {p for p, _, _ in CRM_BINDINGS}, {"L_", "D_", "C_", "CO_"}
        )


class TestHelpers(unittest.TestCase):
    def test_horas_legibles(self):
        self.assertEqual(_horas(3600), "1 h")
        self.assertEqual(_horas(5400), "1 h 30 min")
        self.assertEqual(_horas(900), "15 min")
        self.assertEqual(_horas(0), "0 min")

    def test_as_list_tolera_las_formas_de_bitrix(self):
        self.assertEqual(_as_list([{"a": 1}]), [{"a": 1}])
        self.assertEqual(_as_list({"0": {"a": 1}}), [{"a": 1}])
        for vacio in (None, "", 0, []):
            self.assertEqual(_as_list(vacio), [])
