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

from cortec_bitrix24.migration.dicts import fetch_all_statuses

ESTADOS = [{"ID": str(i), "ENTITY_ID": "STATUS"} for i in range(1, 121)]


class PortalQueIgnoraStart:
    """Devuelve la lista entera en cada llamada, sin `next`."""

    def __init__(self):
        self.llamadas = 0

    def call_raw(self, method, params=None):
        self.llamadas += 1
        if self.llamadas > 10:
            raise AssertionError("bucle infinito: el portal ignora `start`")
        return {"result": list(ESTADOS)}


class PortalQueIgnoraStartConNext(PortalQueIgnoraStart):
    """Lo mismo, pero anunciando `next` como si paginara."""

    def call_raw(self, method, params=None):
        raw = super().call_raw(method, params)
        raw["next"] = (params or {}).get("start", 0) + 50
        return raw


class PortalPaginado:
    """Pagina de verdad, de 50 en 50, con `next`."""

    def __init__(self):
        self.starts = []

    def call_raw(self, method, params=None):
        start = (params or {}).get("start", 0)
        self.starts.append(start)
        raw = {"result": ESTADOS[start : start + 50]}
        if start + 50 < len(ESTADOS):
            raw["next"] = start + 50
        return raw


class TestEstadosDelPortal(unittest.TestCase):
    """
    crm.status.list no admite `>ID`. La versión anterior pedía páginas
    mientras la lista acumulada tuviera 50 filas o más, y en un portal
    que ignora `start` no terminaba nunca: la corrida en seco se quedaba
    horas en «Diccionarios».
    """

    def test_portal_que_ignora_start(self):
        client = PortalQueIgnoraStart()
        filas = fetch_all_statuses(client)
        self.assertEqual(len(filas), 120)
        self.assertLessEqual(client.llamadas, 2)

    def test_portal_que_ignora_start_pero_anuncia_next(self):
        client = PortalQueIgnoraStartConNext()
        filas = fetch_all_statuses(client)
        self.assertEqual(len(filas), 120)
        self.assertLessEqual(client.llamadas, 2)

    def test_portal_que_pagina(self):
        client = PortalPaginado()
        filas = fetch_all_statuses(client)
        self.assertEqual([f["ID"] for f in filas], [e["ID"] for e in ESTADOS])
        self.assertEqual(client.starts, [0, 50, 100])

    def test_portal_sin_estados(self):
        class Vacio:
            def call_raw(self, method, params=None):
                return {"result": []}

        self.assertEqual(fetch_all_statuses(Vacio()), [])
