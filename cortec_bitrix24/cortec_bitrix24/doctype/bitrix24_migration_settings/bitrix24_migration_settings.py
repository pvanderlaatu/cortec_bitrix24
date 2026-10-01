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

"""
Configuración única de la migración.

El validate() de este doctype es la última línea de defensa antes de
pegarle al portal con parámetros que provocarían QUERY_LIMIT_EXCEEDED en
cadena o una corrida imposible de reanudar.
"""

import re

import frappe
from frappe import _
from frappe.model.document import Document

# https://portal.bitrix24.es/rest/<user_id>/<token>/
WEBHOOK_PATTERN = re.compile(
    r"^https://[\w.-]+/rest/\d+/[\w]+/?$", re.IGNORECASE
)

# Tope duro del método `batch` de Bitrix24.
MAX_BATCH_COMMANDS = 50

# Por debajo de esto el portal responde QUERY_LIMIT_EXCEEDED de forma
# sistemática y se pierde más tiempo en reintentos que en la pausa.
MIN_THROTTLE = 0.3


class Bitrix24MigrationSettings(Document):
    def validate(self):
        self._validate_webhook()
        self._normalize_portal_url()
        self._clamp_limits()

    def _validate_webhook(self):
        url = self.get_password("webhook_url", raise_exception=False)
        if not url:
            return

        if not WEBHOOK_PATTERN.match(url.strip()):
            frappe.throw(
                _(
                    "La URL del webhook no tiene el formato esperado. Debe ser "
                    "https://portal.bitrix24.xx/rest/&lt;id&gt;/&lt;token&gt;/ , "
                    "tal como la entrega Bitrix24 en Recursos para "
                    "desarrolladores → Otros → Webhook entrante."
                )
            )

    def _normalize_portal_url(self):
        if not self.portal_url:
            return
        self.portal_url = self.portal_url.strip().rstrip("/")
        if not self.portal_url.startswith("http"):
            self.portal_url = "https://" + self.portal_url

    def _clamp_limits(self):
        if (self.batch_size or 0) < 1 or (self.batch_size or 0) > MAX_BATCH_COMMANDS:
            self.batch_size = MAX_BATCH_COMMANDS

        if (self.throttle_seconds or 0) < MIN_THROTTLE:
            self.throttle_seconds = MIN_THROTTLE

        if (self.max_retries or 0) < 1:
            self.max_retries = 3

        if (self.request_timeout or 0) < 10:
            self.request_timeout = 60

        if (self.max_attachment_mb or 0) < 1:
            self.max_attachment_mb = 25


def get_settings() -> Bitrix24MigrationSettings:
    """
    Atajo para las fases. Se resuelve en cada llamada en vez de
    cachearse: una corrida larga puede pararse a mitad para cambiar el
    usuario de respaldo o apagar una fase, y conviene que lo recoja.
    """
    return frappe.get_single("Bitrix24 Migration Settings")


@frappe.whitelist()
def test_connection() -> dict:
    """
    Prueba de vida contra el portal, sin escribir nada.

    Devuelve el perfil del webhook y el conteo de cada colección, que es
    la cifra contra la que se coteja la migración al terminar.
    """
    frappe.only_for("System Manager")

    from cortec_bitrix24.bitrix.client import get_client
    from cortec_bitrix24.bitrix.errors import B24Error

    try:
        client = get_client()
        profile = client.profile()
    except B24Error as e:
        # Un mensaje, no un traceback: es un problema de configuración
        # que resuelve quien está delante del formulario.
        if e.code == "NO_WEBHOOK":
            frappe.throw(
                _(
                    "No hay ninguna URL de webhook guardada. Péguela en «URL del "
                    "webhook entrante» y guarde el formulario antes de probar."
                ),
                title=_("Falta el webhook"),
            )
        frappe.throw(
            _("Bitrix24 rechazó la conexión: {0}").format(str(e)),
            title=_("No se pudo conectar con Bitrix24"),
        )

    counts = {}
    for label, method in (
        ("Leads", "crm.lead.list"),
        ("Negociaciones", "crm.deal.list"),
        ("Contactos", "crm.contact.list"),
        ("Empresas", "crm.company.list"),
        ("Actividades", "crm.activity.list"),
    ):
        try:
            counts[label] = client.count(method)
        except B24Error as e:
            # Un método sin permiso no debe tumbar el diagnóstico: es
            # justamente lo que el usuario necesita ver.
            counts[label] = f"error: {e.code}"

    return {
        "ok": True,
        "portal": client.safe_url(),
        "usuario": f"{profile.get('NAME', '')} {profile.get('LAST_NAME', '')}".strip()
        or profile.get("ID"),
        "admin": bool(profile.get("ADMIN")),
        "conteos": counts,
    }
