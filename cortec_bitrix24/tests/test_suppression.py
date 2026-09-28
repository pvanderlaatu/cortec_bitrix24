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
from unittest import mock

from cortec_bitrix24.migration import idmap


class FakeCtx:
    dry_run = False

    def __init__(self):
        self.counts = {}
        self.logs = []

    def count(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    def log(self, level, message, **kwargs):
        self.logs.append((level, message))


class TestUpsertRespetaSupresiones(unittest.TestCase):
    """
    Una corrida delta no puede volver a escribir los datos de un titular
    que pidió la supresión (cortec_helpdesk.privacy).
    """

    def setUp(self):
        self.suppressed = {("CRM Lead", "CRM-LEAD-1")}
        patches = [
            mock.patch.object(
                idmap, "is_suppressed", side_effect=lambda dt, name: (dt, name) in self.suppressed
            ),
            mock.patch.object(idmap, "frappe"),
        ]
        mocks = [p.start() for p in patches]
        for p in patches:
            self.addCleanup(p.stop)

        self.frappe = mocks[1]
        self.frappe.db.get_value.return_value = mock.Mock(
            target_doctype="CRM Lead", target_name="CRM-LEAD-1", payload_hash="viejo"
        )
        self.frappe.db.exists.return_value = True

    def test_no_actualiza_un_titular_suprimido(self):
        ctx = FakeCtx()
        name = idmap.upsert(ctx, idmap.ENTITY_LEAD, 10, "CRM Lead", {"first_name": "Ana"})

        self.assertEqual(name, "CRM-LEAD-1")
        self.assertEqual(ctx.counts, {"skipped": 1})
        self.frappe.get_doc.assert_not_called()
        self.frappe.new_doc.assert_not_called()

    def test_no_crea_notas_colgadas_de_un_titular_suprimido(self):
        self.frappe.db.get_value.return_value = None
        ctx = FakeCtx()
        idmap.upsert(
            ctx,
            idmap.ENTITY_COMMENT,
            99,
            "FCRM Note",
            {"content": "texto", "reference_doctype": "CRM Lead", "reference_docname": "CRM-LEAD-1"},
        )

        self.assertEqual(ctx.counts, {"skipped": 1})
        self.frappe.new_doc.assert_not_called()

    def test_otros_documentos_se_escriben(self):
        self.frappe.db.get_value.return_value = None
        ctx = FakeCtx()
        with mock.patch.object(idmap, "_save"), mock.patch.object(idmap, "record"), mock.patch.object(
            idmap, "_apply_timestamps"
        ):
            idmap.upsert(ctx, idmap.ENTITY_LEAD, 11, "CRM Lead", {"first_name": "Beto"})

        self.assertEqual(ctx.counts, {"created": 1})
