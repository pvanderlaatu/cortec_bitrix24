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
Fase 6 — Leads → CRM Lead.

Un detalle de modelo que conviene tener presente: en Bitrix24 el lead
lleva un importe (`OPPORTUNITY`) y en Frappe CRM el CRM Lead no tiene
campo de valor — el importe aparece solo al convertirlo en CRM Deal. En
vez de forzarlo dentro de `annual_revenue`, que significa otra cosa (la
facturación anual de la empresa) y falsearía los informes, el importe se
anota en el texto de comentarios. Los leads que en Bitrix24 ya estaban
convertidos sí conservan su importe, porque viaja con la negociación.
"""

import frappe

from cortec_bitrix24.migration import dicts, idmap
from cortec_bitrix24.migration.userfields import build_uf_values
from cortec_bitrix24.migration.users import resolve_owner
from cortec_bitrix24.utils import (
    as_float,
    first_value,
    normalize_phone,
    parse_datetime_str,
    to_bitrix_datetime,
)

SELECT_FIELDS = ["*", "UF_*"]

# STATUS_ID que Bitrix24 usa para los leads ya convertidos.
CONVERTED_STATUS = "CONVERTED"
JUNK_STATUS = "JUNK"


def run(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">DATE_MODIFY": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("leads_en_bitrix", ctx.client.count("crm.lead.list"))

    fallback_status = _fallback_status(ctx)
    has_comentarios = _has_field("CRM Lead", "custom_comentarios")

    for row in ctx.client.list_all(
        "crm.lead.list", params, start_after_id=ctx.cursor()
    ):
        bitrix_id = row.get("ID")
        try:
            _sync_lead(ctx, row, fallback_status, has_comentarios)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar el lead {bitrix_id}: {e}",
                bitrix_id=bitrix_id,
            )
        ctx.set_cursor(int(bitrix_id))

    ctx.checkpoint()


def _sync_lead(ctx, row: dict, fallback_status, has_comentarios: bool) -> None:
    bitrix_id = row.get("ID")

    first_name = (row.get("NAME") or "").strip()
    last_name = (row.get("LAST_NAME") or "").strip()
    title = (row.get("TITLE") or "").strip()

    if not first_name and not last_name:
        # Sin nombre de persona, el título del lead es lo más
        # informativo que hay; si tampoco está, el teléfono o el correo.
        first_name = (
            title
            or first_value(row.get("PHONE"))
            or first_value(row.get("EMAIL"))
            or f"Lead B24 {bitrix_id}"
        )

    status_id = row.get("STATUS_ID")

    values = {
        "first_name": first_name,
        "middle_name": (row.get("SECOND_NAME") or "").strip() or None,
        "last_name": last_name or None,
        "lead_name": title or None,
        "job_title": (row.get("POST") or "").strip() or None,
        "organization": (row.get("COMPANY_TITLE") or "").strip() or None,
        "email": first_value(row.get("EMAIL")) or None,
        "mobile_no": normalize_phone(
            first_value(row.get("PHONE")), ctx.country_code
        )
        or None,
        "website": first_value(row.get("WEB")) or None,
        "status": dicts.resolve_status(ctx, "Lead", status_id, fallback_status),
        "source": dicts.resolve_catalog(
            ctx, "SOURCE", row.get("SOURCE_ID"), "CRM Lead Source"
        ),
        "lead_owner": resolve_owner(ctx, row.get("ASSIGNED_BY_ID")),
        "converted": 1 if str(status_id) == CONVERTED_STATUS else 0,
    }

    if has_comentarios:
        values["custom_comentarios"] = _comentarios(row)

    values.update(build_uf_values(ctx, "Lead", row))

    idmap.upsert(
        ctx,
        idmap.ENTITY_LEAD,
        bitrix_id,
        "CRM Lead",
        values,
        created_on=parse_datetime_str(row.get("DATE_CREATE")),
        modified_on=parse_datetime_str(row.get("DATE_MODIFY")),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _comentarios(row: dict) -> str | None:
    """
    Texto de `custom_comentarios`: los comentarios del lead más el
    importe y la descripción de la fuente, que en Frappe CRM no tienen
    campo propio y se perderían.
    """
    from cortec_bitrix24.utils import bbcode_to_text

    partes = []

    comentarios = bbcode_to_text(row.get("COMMENTS"))
    if comentarios:
        partes.append(comentarios)

    descripcion = (row.get("SOURCE_DESCRIPTION") or "").strip()
    if descripcion:
        partes.append(f"Origen (Bitrix24): {descripcion}")

    importe = as_float(row.get("OPPORTUNITY"))
    if importe:
        moneda = (row.get("CURRENCY_ID") or "").strip()
        partes.append(f"Oportunidad en Bitrix24: {importe:,.2f} {moneda}".strip())

    return "\n\n".join(partes) or None


def _fallback_status(ctx) -> str | None:
    """
    Estado de respaldo para los leads cuyo STATUS_ID no quedó mapeado.

    Se elige el CRM Lead Status abierto de menor `position`, que es el
    primero del embudo: dejar el campo vacío haría que el Lead no
    apareciera en ninguna columna del tablero.
    """
    status = frappe.get_all(
        "CRM Lead Status",
        filters={"type": "Open"},
        fields=["name"],
        order_by="position asc",
        limit=1,
    )
    if status:
        return status[0].name

    ctx.log(
        "warning",
        "No hay ningún CRM Lead Status de tipo 'Open'. Los leads sin estado "
        "mapeado quedarán sin estado.",
    )
    return None


def _has_field(doctype: str, fieldname: str) -> bool:
    try:
        return bool(frappe.get_meta(doctype).has_field(fieldname))
    except Exception:
        return False
