# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# This file is part of CORTEC Bitrix24.
#
# CORTEC Bitrix24 is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# CORTEC Bitrix24 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with CORTEC Bitrix24. If not, see <https://www.gnu.org/licenses/>.
from __future__ import annotations

import unittest
from datetime import datetime
from unittest import mock

from cortec_bitrix24.migration import consents
from cortec_bitrix24.migration.consents import (
    parse_activity_id,
    parse_csv,
    parse_date,
    parse_unsubscribes_csv,
    resolve_owners,
)

# Tal como sale de copiar la tabla de Bitrix24 a Excel y guardar como
# CSV con la configuración regional de Costa Rica.
CSV_EXCEL = (
    "Fecha;Usuario;Dirección IP;Origen;Items;Dirección\r\n"
    "28/09/2026 09:47:54;;190.113.106.79;Actividad del CRM #81633;;https://abmcr.com/productos/cctv/\r\n"
    "18/09/2026 15:42:26;;186.15.92.114;Actividad del CRM #81579;;https://abmcr.com/productos/control-de-acceso/\r\n"
)


class TestParseCsv(unittest.TestCase):
    def test_csv_de_excel_con_punto_y_coma(self):
        rows = parse_csv(CSV_EXCEL.encode("utf-8-sig"))
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["activity_id"], 81633)
        self.assertEqual(rows[0]["ip"], "190.113.106.79")
        self.assertEqual(rows[0]["url"], "https://abmcr.com/productos/cctv/")
        self.assertEqual(rows[0]["date"], datetime(2026, 9, 28, 9, 47, 54))
        self.assertEqual(rows[0]["line"], 2)

    def test_codificacion_de_windows(self):
        # Excel guarda en cp1252 por defecto: «Dirección» no debe romper
        # la lectura del encabezado.
        rows = parse_csv(CSV_EXCEL.encode("cp1252"))
        self.assertEqual(len(rows), 2)

    def test_separado_por_comas_y_por_tabuladores(self):
        for sep in (",", "\t"):
            rows = parse_csv(CSV_EXCEL.replace(";", sep))
            self.assertEqual(rows[1]["activity_id"], 81579, sep)

    def test_filas_vacias_se_ignoran(self):
        rows = parse_csv(CSV_EXCEL + ";;;;;\r\n\r\n")
        self.assertEqual(len(rows), 2)


class TestParseCampos(unittest.TestCase):
    def test_fechas_que_deja_excel(self):
        self.assertEqual(parse_date("7/9/2026 12:44"), datetime(2026, 9, 7, 12, 44))
        self.assertEqual(parse_date("2026-09-07 12:44:40"), datetime(2026, 9, 7, 12, 44, 40))
        self.assertIsNone(parse_date("ayer"))

    def test_dia_antes_que_mes(self):
        # 07/09 es 7 de septiembre, no 9 de julio.
        self.assertEqual(parse_date("07/09/2026 08:46:31").month, 9)

    def test_id_de_actividad(self):
        self.assertEqual(parse_activity_id("Actividad del CRM #81633"), 81633)
        self.assertIsNone(parse_activity_id("Formulario web"))


class FakeClient:
    def __init__(self, activities, deals=None):
        self.activities = activities
        self.deals = deals or {}
        self.deal_calls = 0

    def list_all(self, method, params):
        ids = set(params["filter"]["ID"])
        return [a for a in self.activities if int(a["ID"]) in ids]

    def call(self, method, params):
        self.deal_calls += 1
        return self.deals.get(params["id"], {})


class TestResolverTitulares(unittest.TestCase):
    def setUp(self):
        targets = {("lead", "10"): "CRM-LEAD-1", ("contact", "20"): "CONTACT-1"}
        patcher = mock.patch.object(
            consents.idmap,
            "get_target_name",
            side_effect=lambda entity, bitrix_id: targets.get((entity, str(bitrix_id))),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        email = mock.patch.object(consents.frappe, "db", create=True)
        db = email.start()
        db.get_value.return_value = "cliente@example.com"
        self.addCleanup(email.stop)

    def test_lead_contacto_negociacion_y_empresa(self):
        client = FakeClient(
            [
                {"ID": "1", "OWNER_TYPE_ID": "1", "OWNER_ID": "10"},
                {"ID": "2", "OWNER_TYPE_ID": "3", "OWNER_ID": "20"},
                {"ID": "3", "OWNER_TYPE_ID": "2", "OWNER_ID": "30"},
                {"ID": "4", "OWNER_TYPE_ID": "2", "OWNER_ID": "30"},
                {"ID": "5", "OWNER_TYPE_ID": "4", "OWNER_ID": "40"},
            ],
            deals={"30": {"CONTACT_ID": "20"}},
        )
        owners = resolve_owners(client, [1, 2, 3, 4, 5, 6])

        self.assertEqual(owners[1][:2], ("CRM Lead", "CRM-LEAD-1"))
        self.assertEqual(owners[2][:2], ("Contact", "CONTACT-1"))
        # La negociación se resuelve por su contacto principal, y una
        # sola vez aunque tenga varios envíos.
        self.assertEqual(owners[3][:2], ("Contact", "CONTACT-1"))
        self.assertEqual(owners[4][:2], ("Contact", "CONTACT-1"))
        self.assertEqual(client.deal_calls, 1)
        # Empresa y actividad inexistente: sin titular.
        self.assertNotIn(5, owners)
        self.assertNotIn(6, owners)


class TestParseBajas(unittest.TestCase):
    """CSV de bajas de las campañas de correo de Bitrix24 (CRM Marketing)."""

    def test_con_fecha(self):
        rows = parse_unsubscribes_csv(
            "E-mail;Fecha\r\nAna@Example.com;15.03.2025 10:20:00\r\n".encode("utf-8")
        )
        self.assertEqual(rows[0]["email"], "ana@example.com")
        self.assertEqual(rows[0]["date"], datetime(2025, 3, 15, 10, 20))

    def test_sin_columna_de_fecha(self):
        rows = parse_unsubscribes_csv("Correo\r\nbeto@example.com\r\n")
        self.assertEqual(rows[0]["email"], "beto@example.com")
        self.assertIsNone(rows[0]["date"])

    def test_encabezados_aceptados(self):
        for encabezado in ("Correo", "Correo electrónico", "E-mail", "Email"):
            rows = parse_unsubscribes_csv(f"{encabezado}\r\nc@example.com\r\n")
            self.assertEqual(rows[0]["email"], "c@example.com", encabezado)

    def test_correo_invalido_queda_marcado(self):
        rows = parse_unsubscribes_csv("Email\r\nno-es-un-correo\r\n")
        self.assertIsNone(rows[0]["email"])
        self.assertEqual(rows[0]["line"], 2)
