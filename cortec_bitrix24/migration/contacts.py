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
Fase 5 — Contactos → Contact.

Se migran al `Contact` del núcleo de Frappe, no a un doctype propio del
CRM: es el que usan a la vez Frappe CRM (tabla `contacts` de CRM Deal),
Helpdesk y el correo entrante, y es donde cortec_helpdesk busca el
`custom_account_manager` para asignar tickets.

Dos cosas que esta fase hace y conviene no perder de vista:

1. **Teléfonos y correos multivalor.** Bitrix24 guarda varios de cada
   uno por contacto. Se migran todos a las tablas hijas Contact Email /
   Contact Phone, normalizando los teléfonos a E.164. Sin esa
   normalización, el historial de WhatsApp no casa después con el
   contacto.

2. **`custom_account_manager`.** Si cortec_helpdesk está instalado, se
   escribe el responsable de Bitrix24 en ese campo. Es lo que hace que
   su asignación automática de tickets funcione desde el primer correo
   que entre tras el corte, sin tener que rellenarlo a mano para miles
   de contactos.

El consentimiento publicitario (`custom_acepta_promociones`) **no** se
escribe aquí. En cortec_helpdesk es un reflejo del registro de
consentimientos, y la evidencia de Bitrix24 (fecha, IP, URL de cada
envío del formulario) se importa aparte con
`api.import_webform_consents` (ver migration/consents.py).
"""

import frappe

from cortec_bitrix24.migration import idmap
from cortec_bitrix24.migration.userfields import build_uf_values
from cortec_bitrix24.utils import (
    multifield_pairs,
    normalize_phone,
    parse_datetime_str,
    to_bitrix_datetime,
)

SELECT_FIELDS = ["*", "UF_*"]

# VALUE_TYPE de Bitrix24 que se consideran móviles. El resto va a
# `phone` y solo el primero se marca como principal.
MOBILE_TYPES = {"MOBILE", "CELL"}


def run(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">DATE_MODIFY": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("contactos_en_bitrix", ctx.client.count("crm.contact.list"))

    has_account_manager = _has_field("Contact", "custom_account_manager")
    if not has_account_manager:
        ctx.log(
            "info",
            "El campo Contact.custom_account_manager no existe (cortec_helpdesk "
            "no está instalado). Se migra el responsable solo como propietario "
            "del documento.",
        )

    for row in ctx.client.list_all(
        "crm.contact.list", params, start_after_id=ctx.cursor()
    ):
        bitrix_id = row.get("ID")
        try:
            _sync_contact(ctx, row, has_account_manager)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar el contacto {bitrix_id}: {e}",
                bitrix_id=bitrix_id,
            )
        ctx.set_cursor(int(bitrix_id))

    ctx.checkpoint()


def _sync_contact(ctx, row: dict, has_account_manager: bool) -> None:
    from cortec_bitrix24.migration.users import resolve_user

    bitrix_id = row.get("ID")

    first_name = (row.get("NAME") or "").strip()
    last_name = (row.get("LAST_NAME") or "").strip()

    if not first_name and not last_name:
        # Frappe exige al menos el nombre; un contacto sin nombre en
        # Bitrix24 suele ser uno creado desde un WhatsApp suelto.
        first_name = _fallback_name(row, bitrix_id)

    emails, phones = _build_contact_channels(ctx, row)

    values = {
        "first_name": first_name,
        "middle_name": (row.get("SECOND_NAME") or "").strip() or None,
        "last_name": last_name or None,
        "salutation": _resolve_salutation(row.get("HONORIFIC")),
        "designation": (row.get("POST") or "").strip() or None,
        "company_name": _organization_name(ctx, row.get("COMPANY_ID")),
    }

    if has_account_manager:
        values["custom_account_manager"] = resolve_user(
            ctx, row.get("ASSIGNED_BY_ID"), fallback=None
        )

    values.update(build_uf_values(ctx, "Contact", row))

    child_tables = {"email_ids": emails, "phone_nos": phones}

    links = _organization_link(ctx, row.get("COMPANY_ID"))
    if links:
        child_tables["links"] = links

    idmap.upsert(
        ctx,
        idmap.ENTITY_CONTACT,
        bitrix_id,
        "Contact",
        values,
        child_tables=child_tables,
        created_on=parse_datetime_str(row.get("DATE_CREATE")),
        modified_on=parse_datetime_str(row.get("DATE_MODIFY")),
    )


# ---------------------------------------------------------------------------
# Canales de contacto
# ---------------------------------------------------------------------------


def _build_contact_channels(ctx, row: dict) -> tuple[list[dict], list[dict]]:
    """
    Filas de Contact Email y Contact Phone a partir de los campos
    multivalor de Bitrix24.

    El primer correo y el primer móvil quedan marcados como principales:
    son los que Frappe copia a `email_id` / `mobile_no`, y de los que
    depende que un correo entrante se vincule al contacto correcto.
    """
    emails = []
    for value, _kind in multifield_pairs(row.get("EMAIL")):
        if value.lower() in {e["email_id"].lower() for e in emails}:
            continue
        emails.append({"email_id": value, "is_primary": 1 if not emails else 0})

    phones: list[dict] = []
    mobile_marked = False
    phone_marked = False

    for value, kind in multifield_pairs(row.get("PHONE")):
        normalized = normalize_phone(value, ctx.country_code)
        if not normalized or normalized in {p["phone"] for p in phones}:
            continue

        entry = {"phone": normalized}

        if kind in MOBILE_TYPES and not mobile_marked:
            entry["is_primary_mobile_no"] = 1
            mobile_marked = True
        elif kind not in MOBILE_TYPES and not phone_marked:
            entry["is_primary_phone"] = 1
            phone_marked = True

        phones.append(entry)

    # Un contacto cuyo único número Bitrix24 etiquetó como "WORK" se
    # quedaría sin móvil principal, y el enrutado de WhatsApp de
    # cortec_helpdesk busca por móvil. Se marca el primero.
    if phones and not mobile_marked:
        phones[0]["is_primary_mobile_no"] = 1

    return emails, phones


def _fallback_name(row: dict, bitrix_id) -> str:
    """Nombre de emergencia: el primer teléfono o correo que haya."""
    from cortec_bitrix24.utils import first_value

    return (
        first_value(row.get("PHONE"))
        or first_value(row.get("EMAIL"))
        or f"Contacto B24 {bitrix_id}"
    )


# ---------------------------------------------------------------------------
# Vínculo con la empresa
# ---------------------------------------------------------------------------


def _organization_name(ctx, company_id) -> str | None:
    if not company_id or str(company_id) == "0":
        return None
    return idmap.get_target_name(idmap.ENTITY_COMPANY, company_id)


def _organization_link(ctx, company_id) -> list[dict]:
    """
    Fila de Dynamic Link Contact → CRM Organization.

    Es el vínculo que consulta Frappe CRM para mostrar los contactos de
    una organización. Si la empresa no llegó a migrarse (fase apagada o
    error), se deja sin vincular en vez de escribir un Link roto.
    """
    name = _organization_name(ctx, company_id)
    if not name:
        return []

    return [{"link_doctype": "CRM Organization", "link_name": name}]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_salutation(value: str | None) -> str | None:
    """
    HONORIFIC de Bitrix24 (`HNR_ES_1`…) no casa con las Salutation de
    Frappe, y crear Salutation nuevas por un código opaco ensucia un
    catálogo compartido. Solo se usa si ya existe una con ese nombre.
    """
    if not value:
        return None
    return value if frappe.db.exists("Salutation", value) else None


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False
