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

from cortec_bitrix24 import utils
from cortec_bitrix24.migration import users
from cortec_bitrix24.utils import clear_local_cache, local_cache


class LocalDeFrappe16:
    """
    Como `frappe.local` en Frappe v16: los atributos viven en un
    almacén aparte y el objeto no tiene `__dict__`.
    """

    __slots__ = ()
    _store: dict = {}

    def __getattribute__(self, name):
        store = object.__getattribute__(self, "_store")
        if name in store:
            return store[name]
        raise AttributeError(name)

    def __setattr__(self, name, value):
        object.__getattribute__(self, "_store")[name] = value


class TestLocalCache(unittest.TestCase):
    def setUp(self):
        LocalDeFrappe16._store = {}
        self.local = LocalDeFrappe16()
        patcher = mock.patch.object(utils.frappe, "local", self.local, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_el_local_de_v16_no_tiene_dict(self):
        # La condición que rompía la fase de usuarios.
        with self.assertRaises(AttributeError):
            self.local.__dict__

    def test_devuelve_siempre_el_mismo_diccionario(self):
        cache = local_cache("_prueba")
        cache["a"] = 1
        self.assertEqual(local_cache("_prueba"), {"a": 1})
        self.assertIs(local_cache("_prueba"), cache)

    def test_claves_distintas_no_se_mezclan(self):
        local_cache("_uno")["x"] = 1
        self.assertEqual(local_cache("_dos"), {})

    def test_limpiar_deja_una_cache_nueva(self):
        local_cache("_prueba")["a"] = 1
        clear_local_cache("_prueba")
        self.assertEqual(local_cache("_prueba"), {})

    def test_limpiar_sin_haber_creado_no_falla(self):
        clear_local_cache("_nunca_creada")

    def test_la_fase_de_usuarios_limpia_su_cache(self):
        # El punto exacto del traceback: users._clear_cache().
        with mock.patch.object(users.frappe, "local", self.local, create=True):
            local_cache(users._CACHE_KEY)["1"] = "agente@example.com"
            users._clear_cache()
            self.assertEqual(local_cache(users._CACHE_KEY), {})
