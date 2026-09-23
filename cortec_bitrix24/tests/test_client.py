# Copyright (C) 2026 Corporación de Tecnología CORTEC S.R.L.
# SPDX-License-Identifier: AGPL-3.0-or-later
from __future__ import annotations

"""
Tests del cliente REST.

Cubren las tres cosas que, si fallan, arruinan una migración entera sin
dar ningún error visible:

  * la paginación por ID — si el cursor no avanza, el bucle repite la
    primera página para siempre; si salta, se pierden registros en
    silencio;
  * la serialización de los comandos de `batch`, que viajan como query
    string y no como JSON;
  * que la URL del webhook (que lleva el token) nunca salga en un log.
"""

import unittest

from cortec_bitrix24.bitrix.client import PAGE_SIZE, B24Client, _flatten, _safe_url
from cortec_bitrix24.bitrix.errors import B24AuthError

WEBHOOK = "https://cortec.bitrix24.es/rest/1/s3cr3tt0k3n/"


def _client() -> B24Client:
    # throttle=0 para que los tests no duerman.
    return B24Client(WEBHOOK, throttle=0)


class TestConstruccion(unittest.TestCase):
    def test_sin_webhook_falla_de_inmediato(self):
        with self.assertRaises(B24AuthError):
            B24Client("")

    def test_normaliza_la_barra_final(self):
        self.assertEqual(
            B24Client("https://x.bitrix24.es/rest/1/tok", throttle=0).base_url,
            "https://x.bitrix24.es/rest/1/tok/",
        )


class TestSafeUrl(unittest.TestCase):
    def test_oculta_el_token(self):
        seguro = _safe_url(WEBHOOK)
        self.assertNotIn("s3cr3tt0k3n", seguro)
        self.assertEqual(seguro, "https://cortec.bitrix24.es/rest/1/***/")

    def test_url_inesperada_no_filtra_nada(self):
        self.assertEqual(_safe_url("https://algo/raro"), "***")


class TestListAll(unittest.TestCase):
    def setUp(self):
        self.client = _client()
        self.llamadas = []

    def _servir(self, registros: list[dict]):
        """Sustituye call() por un portal de mentira paginado por ID."""

        def fake_call(method, params=None):
            params = params or {}
            self.llamadas.append(params)
            desde = int(params["filter"][">ID"])
            posteriores = [r for r in registros if int(r["ID"]) > desde]
            return posteriores[:PAGE_SIZE]

        self.client.call = fake_call

    def test_recorre_todas_las_paginas_sin_repetir(self):
        registros = [{"ID": str(i)} for i in range(1, 121)]
        self._servir(registros)

        obtenidos = [r["ID"] for r in self.client.list_all("crm.lead.list")]

        self.assertEqual(len(obtenidos), 120)
        self.assertEqual(len(set(obtenidos)), 120, "hay registros repetidos")
        self.assertEqual(obtenidos[0], "1")
        self.assertEqual(obtenidos[-1], "120")

    def test_el_cursor_avanza_en_cada_pagina(self):
        self._servir([{"ID": str(i)} for i in range(1, 121)])
        list(self.client.list_all("crm.lead.list"))

        cursores = [int(p["filter"][">ID"]) for p in self.llamadas]
        self.assertEqual(cursores, [0, 50, 100])

    def test_desactiva_el_conteo_de_total(self):
        # start=-1 es lo que evita que el portal cuente el total en cada
        # página, que es el coste que degrada la paginación por offset.
        self._servir([{"ID": "1"}])
        list(self.client.list_all("crm.lead.list"))
        self.assertEqual(self.llamadas[0]["start"], -1)

    def test_respeta_el_filtro_del_llamador(self):
        self._servir([{"ID": "1"}])
        list(
            self.client.list_all(
                "crm.lead.list", {"filter": {">DATE_MODIFY": "2026-01-01T00:00:00"}}
            )
        )
        self.assertEqual(
            self.llamadas[0]["filter"][">DATE_MODIFY"], "2026-01-01T00:00:00"
        )

    def test_reanuda_desde_un_id(self):
        self._servir([{"ID": str(i)} for i in range(1, 61)])
        obtenidos = [
            r["ID"] for r in self.client.list_all("crm.lead.list", start_after_id=55)
        ]
        self.assertEqual(obtenidos, ["56", "57", "58", "59", "60"])

    def test_coleccion_vacia(self):
        self._servir([])
        self.assertEqual(list(self.client.list_all("crm.lead.list")), [])

    def test_sin_campo_id_se_detiene_en_vez_de_repetir(self):
        # Un método que no devuelve ID haría bucle infinito: el cliente
        # corta y lo anota.
        avisos = []
        client = B24Client(WEBHOOK, throttle=0, logger=lambda l, m: avisos.append(m))
        client.call = lambda method, params=None: [{"TITLE": "sin id"}] * PAGE_SIZE

        self.assertEqual(len(list(client.list_all("crm.lead.list"))), PAGE_SIZE)
        self.assertTrue(any("paginación" in a for a in avisos))


class TestBatchEncoding(unittest.TestCase):
    def test_aplana_filtros_anidados(self):
        pares: list[str] = []
        _flatten({"filter": {">ID": 5}, "select": ["ID", "TITLE"]}, "", pares)

        self.assertIn("filter[>ID]=5", pares)
        self.assertIn("select[0]=ID", pares)
        self.assertIn("select[1]=TITLE", pares)

    def test_booleanos_como_1_y_0(self):
        pares: list[str] = []
        _flatten({"a": True, "b": False}, "", pares)
        self.assertEqual(pares, ["a=1", "b=0"])

    def test_comando_completo(self):
        comando = _client()._encode_cmd(
            "crm.deal.contact.items.get", {"id": 42}
        )
        self.assertEqual(comando, "crm.deal.contact.items.get?id=42")

    def test_mas_de_50_comandos_se_rechaza(self):
        comandos = {str(i): ("crm.lead.get", {"id": i}) for i in range(51)}
        with self.assertRaises(ValueError):
            _client().batch(comandos)

    def test_batch_vacio_no_llama_al_portal(self):
        self.assertEqual(_client().batch({}), {})
