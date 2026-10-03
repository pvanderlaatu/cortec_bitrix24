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

import sys
import types
import unittest
from unittest import mock

from cortec_bitrix24.bitrix.errors import B24FatalError
from cortec_bitrix24.migration import runner


def notify(*args, **kwargs):
    return "enviado"


class Assigner:
    def add_assign_comment(self, **kwargs):
        return "comentado"


class FakeCtx:
    def __init__(self, dry_run=False):
        self.dry_run = dry_run
        self.summary = {}
        self.logs = []

    def log(self, level, message, **kwargs):
        self.logs.append((level, message))


class TestQuietNotifications(unittest.TestCase):
    """
    Ni agentes ni clientes deben recibir avisos por la migración. Estos
    tests fijan el comportamiento del silenciado, no el de Frappe: que
    las funciones de Frappe siguen existiendo con esos nombres lo
    comprueba cada corrida (y la aborta si no).
    """

    def setUp(self):
        self.module = types.ModuleType("fake_notify")
        self.module.notify = notify
        self.module.Assigner = Assigner
        sys.modules["fake_notify"] = self.module
        self.addCleanup(sys.modules.pop, "fake_notify", None)

        self.targets = [
            ("fake_notify", "notify", "correo", "frappe", False),
            ("fake_notify", "Assigner.add_assign_comment", "comentario", "frappe", False),
        ]
        patches = [
            mock.patch.object(runner, "QUIET_TARGETS", self.targets),
            mock.patch.object(runner, "frappe"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

        self.frappe = mocks[1]
        self.frappe.get_installed_apps.return_value = ["frappe", "crm"]
        # Sin alertas programadas.
        self.frappe.db.exists.return_value = False

    def test_silencia_dentro_y_restaura_al_salir(self):
        with runner.quiet_notifications(FakeCtx()):
            self.assertIsNone(self.module.notify())
            self.assertIsNone(Assigner().add_assign_comment(x=1))

        self.assertEqual(self.module.notify(), "enviado")
        self.assertEqual(Assigner().add_assign_comment(x=1), "comentado")

    def test_restaura_aunque_falle_la_corrida(self):
        with self.assertRaises(ValueError):
            with runner.quiet_notifications(FakeCtx()):
                raise ValueError("fase rota")
        self.assertEqual(self.module.notify(), "enviado")

    def test_atributo_faltante_aborta_la_corrida_real(self):
        self.targets.append(("fake_notify", "renombrado", "plantillas", "frappe", False))

        with self.assertRaises(B24FatalError):
            with runner.quiet_notifications(FakeCtx()):
                self.fail("no debe empezar ninguna fase")

        # No quedó nada a medio parchear.
        self.assertEqual(self.module.notify(), "enviado")

    def test_atributo_faltante_en_dry_run_solo_avisa(self):
        self.targets.append(("fake_notify", "renombrado", "plantillas", "frappe", False))
        ctx = FakeCtx(dry_run=True)

        with runner.quiet_notifications(ctx):
            pass

        self.assertIn("notificaciones_sin_silenciar", ctx.summary["antes_de_empezar"])
        self.assertEqual(self.module.notify(), "enviado")

    def test_app_no_instalada_no_es_un_error(self):
        self.targets.append(("frappe_whatsapp.utils", "run", "plantillas", "frappe_whatsapp", False))
        with runner.quiet_notifications(FakeCtx()):
            pass

    def test_modulo_opcional_ausente_no_es_un_error(self):
        # Versiones de Frappe CRM sin motor de automatizaciones.
        self.targets.append(("crm_sin_automation.events", "_enabled", "automatizaciones", "crm", True))
        with runner.quiet_notifications(FakeCtx()):
            pass

    def test_lista_alertas_programadas(self):
        self.frappe.db.exists.side_effect = lambda doctype, name: name == "Notification"
        self.frappe.get_all.return_value = [
            types.SimpleNamespace(name="Recordatorio", document_type="CRM Task", event="Days Before")
        ]
        ctx = FakeCtx(dry_run=True)

        with runner.quiet_notifications(ctx):
            pass

        alertas = ctx.summary["antes_de_empezar"]["alertas_programadas_a_desactivar"]
        self.assertEqual(len(alertas), 1)
        self.assertIn("Recordatorio", alertas[0])


class TestAvisosDeCortecHelpdesk(unittest.TestCase):
    def test_raven_se_apaga_durante_la_corrida(self):
        self.assertIn("raven_enabled", runner.CORTEC_ALERT_FIELDS)

    def test_los_objetivos_reales_estan_completos(self):
        silenciados = {attribute for _m, attribute, _l, _a, _o in runner.QUIET_TARGETS}
        self.assertTrue(
            {
                "notify_assignment",
                "notify_assigned_user",
                "ToDo.add_assign_comment",
                "apply",
                "notify_agent",
                "_enabled",
                "run_server_script_for_doc_event",
            }
            <= silenciados
        )


class TestFasesOpcionales(unittest.TestCase):
    def test_un_permiso_que_falta_no_tumba_el_crm(self):
        # Las fases que dependen de un permiso distinto del CRM se
        # omiten; las del CRM no pueden omitirse.
        self.assertTrue({"users", "tasks", "openlines", "files", "webforms"} <= runner.OPTIONAL_PHASES)
        for fase in ("dicts", "userfields", "companies", "contacts", "leads", "deals", "activities"):
            self.assertNotIn(fase, runner.OPTIONAL_PHASES)
