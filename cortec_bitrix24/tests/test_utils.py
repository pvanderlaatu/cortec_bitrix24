# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
Tests de las conversiones Bitrix24 → Frappe.

Son las funciones que deciden si el mismo cliente acaba siendo un
contacto o tres, y si el historial queda en orden cronológico. No tocan
la base de datos, así que se pueden ejecutar sueltas:

    bench --site <sitio> run-tests --app cortec_bitrix24 \\
        --module cortec_bitrix24.tests.test_utils
"""

import unittest

from cortec_bitrix24.utils import (
    bbcode_to_text,
    first_value,
    html_to_text,
    is_truthy,
    multifield_pairs,
    multifield_values,
    normalize_phone,
    phone_matches,
    truncate,
)


class TestNormalizePhone(unittest.TestCase):
    def test_numero_nacional_recibe_codigo_de_pais(self):
        self.assertEqual(normalize_phone("8888-7777"), "+50688887777")
        self.assertEqual(normalize_phone("2222 3333"), "+50622223333")

    def test_formatos_distintos_del_mismo_numero_convergen(self):
        # El motivo de existir de esta función: Bitrix24 guarda el
        # teléfono tal como lo tecleó cada agente.
        variantes = [
            "+506 8888-7777",
            "(506) 8888 7777",
            "50688887777",
            "00506 88887777",
            "8888.7777",
        ]
        normalizados = {normalize_phone(v) for v in variantes}
        self.assertEqual(normalizados, {"+50688887777"})

    def test_numero_internacional_se_respeta(self):
        self.assertEqual(normalize_phone("+1 305 555 0142"), "+13055550142")

    def test_vacio_devuelve_cadena_vacia(self):
        self.assertEqual(normalize_phone(None), "")
        self.assertEqual(normalize_phone("   "), "")
        self.assertEqual(normalize_phone("sin número"), "")

    def test_numero_ininteligible_se_conserva_en_vez_de_perderse(self):
        # Preferimos un dato raro a ninguno: el agente puede corregirlo.
        self.assertEqual(normalize_phone("123"), "+123")

    def test_otro_codigo_de_pais(self):
        self.assertEqual(normalize_phone("612345678", country_code="34"), "+34612345678")


class TestPhoneMatches(unittest.TestCase):
    def test_compara_por_la_parte_nacional(self):
        self.assertTrue(phone_matches("+50688887777", "8888-7777"))
        self.assertTrue(phone_matches("00506 88887777", "(506) 8888 7777"))

    def test_numeros_distintos_no_casan(self):
        self.assertFalse(phone_matches("+50688887777", "+50688887778"))

    def test_vacio_nunca_casa(self):
        self.assertFalse(phone_matches("", ""))
        self.assertFalse(phone_matches(None, "+50688887777"))


class TestMultifield(unittest.TestCase):
    CAMPO = [
        {"ID": "1", "VALUE": "a@cortec.cr", "VALUE_TYPE": "WORK"},
        {"ID": "2", "VALUE": "b@cortec.cr", "VALUE_TYPE": "HOME"},
        {"ID": "3", "VALUE": "a@cortec.cr", "VALUE_TYPE": "OTHER"},
    ]

    def test_extrae_valores_sin_duplicados(self):
        self.assertEqual(
            multifield_values(self.CAMPO), ["a@cortec.cr", "b@cortec.cr"]
        )

    def test_conserva_el_tipo(self):
        self.assertEqual(
            multifield_pairs(self.CAMPO)[0], ("a@cortec.cr", "WORK")
        )

    def test_tolera_las_formas_vacias_de_bitrix(self):
        # Bitrix24 devuelve False, lista vacía o nada según el método.
        for vacio in (None, [], False, ""):
            self.assertEqual(multifield_values(vacio), [])
            self.assertEqual(multifield_pairs(vacio), [])
            self.assertEqual(first_value(vacio), "")


class TestTexto(unittest.TestCase):
    def test_bbcode_se_convierte_a_texto_plano(self):
        crudo = "[B]Urgente[/B][BR]Llamar al [URL=tel:88887777]cliente[/URL]"
        self.assertEqual(bbcode_to_text(crudo), "Urgente\nLlamar al cliente")

    def test_html_a_texto_colapsa_espacios(self):
        self.assertEqual(
            html_to_text("<p>Hola</p>\n<p>  mundo </p>"), "Hola mundo"
        )

    def test_truncate_respeta_el_limite(self):
        self.assertEqual(truncate("abcdef", 4), "abc…")
        self.assertEqual(truncate("abc", 10), "abc")
        self.assertEqual(truncate(None), "")


class TestBooleanos(unittest.TestCase):
    def test_convenciones_de_bitrix(self):
        for verdadero in ("Y", "y", 1, "1", True, "true"):
            self.assertTrue(is_truthy(verdadero), verdadero)
        for falso in ("N", 0, "0", False, None, ""):
            self.assertFalse(is_truthy(falso), falso)
