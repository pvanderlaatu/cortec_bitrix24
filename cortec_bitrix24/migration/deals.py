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
Fase 7 — Negociaciones → CRM Deal.

Los contactos de cada negociación viven en un método aparte
(`crm.deal.contact.items.get`, uno por negociación). Pedirlos de uno en
uno a 2 peticiones por segundo son más de cinco horas para 40 000
negociaciones. Por eso esta fase recorre la lista en bloques de 50 y
pide los contactos de las 50 en un solo `batch`: la misma información en
1/50 de los viajes.

La etapa se resuelve por el par (CATEGORY_ID, STAGE_ID), porque dos
embudos distintos pueden tener etapas con el mismo nombre — la fase de
diccionarios ya las separó con el nombre del embudo como prefijo.
"""

from itertools import islice

import frappe

from cortec_bitrix24.migration import dicts, idmap
from cortec_bitrix24.migration.userfields import build_uf_values
from cortec_bitrix24.migration.users import resolve_owner
from cortec_bitrix24.utils import (
    as_float,
    bbcode_to_text,
    parse_date_str,
    parse_datetime_str,
    to_bitrix_datetime,
)

SELECT_FIELDS = ["*", "UF_*"]


def run(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">DATE_MODIFY": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("negociaciones_en_bitrix", ctx.client.count("crm.deal.list"))

    fallback_status = _fallback_status(ctx)
    rows = ctx.client.list_all("crm.deal.list", params, start_after_id=ctx.cursor())

    for chunk in _chunks(rows, ctx.batch_size):
        contacts_by_deal = _fetch_deal_contacts(ctx, [r.get("ID") for r in chunk])

        for row in chunk:
            bitrix_id = row.get("ID")
            try:
                _sync_deal(
                    ctx, row, contacts_by_deal.get(str(bitrix_id), []), fallback_status
                )
            except Exception as e:
                ctx.log(
                    "error",
                    f"No se pudo migrar la negociación {bitrix_id}: {e}",
                    bitrix_id=bitrix_id,
                )
            ctx.set_cursor(int(bitrix_id))

        ctx.checkpoint()

    ctx.checkpoint()


def _sync_deal(ctx, row: dict, contact_ids: list, fallback_status) -> None:
    bitrix_id = row.get("ID")

    status = dicts.resolve_status(ctx, "Deal", row.get("STAGE_ID"), fallback_status)
    status_type = _status_type(status)
    comentarios = bbcode_to_text(row.get("COMMENTS"))

    organization = idmap.get_target_name(
        idmap.ENTITY_COMPANY, row.get("COMPANY_ID")
    )
    lead = idmap.get_target_name(idmap.ENTITY_LEAD, row.get("LEAD_ID"))

    contacts = _build_contacts_table(ctx, contact_ids)
    primary = contacts[0] if contacts else {}

    values = {
        "organization": organization,
        "lead": lead,
        "status": status,
        "deal_owner": resolve_owner(ctx, row.get("ASSIGNED_BY_ID")),
        "deal_value": as_float(row.get("OPPORTUNITY")) or None,
        "currency": _resolve_currency(row.get("CURRENCY_ID")),
        "probability": as_float(row.get("PROBABILITY")) or None,
        "source": dicts.resolve_catalog(
            ctx, "SOURCE", row.get("SOURCE_ID"), "CRM Lead Source"
        ),
        "expected_closure_date": parse_date_str(row.get("CLOSEDATE")),
        "closed_date": parse_date_str(row.get("CLOSEDATE"))
        if status_type in ("Won", "Lost")
        else None,
        # CRM Deal muestra estos campos en la cabecera; se copian del
        # contacto principal para que la negociación sea legible aunque
        # el contacto no llegara a migrarse.
        "email": primary.get("email"),
        "mobile_no": primary.get("mobile_no"),
        "contact": primary.get("contact"),
        "organization_name": (row.get("TITLE") or "").strip() or None,
    }

    if status_type == "Lost" and comentarios:
        # El motivo de pérdida de Bitrix24 no tiene campo propio: lo que
        # hay es el texto de comentarios, y en una negociación perdida es
        # justo donde el agente lo escribió.
        values["lost_notes"] = comentarios

    values.update(build_uf_values(ctx, "Deal", row))

    idmap.upsert(
        ctx,
        idmap.ENTITY_DEAL,
        bitrix_id,
        "CRM Deal",
        values,
        child_tables={"contacts": contacts} if contacts else None,
        created_on=parse_datetime_str(row.get("DATE_CREATE")),
        modified_on=parse_datetime_str(row.get("DATE_MODIFY")),
    )


# ---------------------------------------------------------------------------
# Contactos de la negociación
# ---------------------------------------------------------------------------


def _fetch_deal_contacts(ctx, deal_ids: list) -> dict[str, list]:
    """
    {deal_id: [ítems de contacto]} para un bloque de negociaciones, en
    un solo `batch`.
    """
    if not deal_ids:
        return {}

    commands = {
        str(deal_id): ("crm.deal.contact.items.get", {"id": deal_id})
        for deal_id in deal_ids
        if deal_id
    }

    try:
        results = ctx.client.batch(commands)
    except Exception as e:
        ctx.log(
            "warning",
            f"No se pudieron leer los contactos de un bloque de negociaciones "
            f"({e}). Las negociaciones se migran sin su tabla de contactos.",
        )
        return {}

    return {k: (v or []) for k, v in results.items()}


def _build_contacts_table(ctx, items: list) -> list[dict]:
    """
    Filas de la tabla hija `contacts` (CRM Contacts).

    Se omiten los contactos que no llegaron a migrarse: una fila con un
    Link roto impide guardar la negociación entera.
    """
    rows = []

    ordered = sorted(
        items or [], key=lambda x: (0 if _is_primary(x) else 1, int(x.get("SORT") or 0))
    )

    for item in ordered:
        contact_name = idmap.get_target_name(
            idmap.ENTITY_CONTACT, item.get("CONTACT_ID")
        )
        if not contact_name:
            continue

        contact = frappe.db.get_value(
            "Contact",
            contact_name,
            ["first_name", "last_name", "email_id", "mobile_no", "phone"],
            as_dict=True,
        )
        if not contact:
            continue

        rows.append(
            {
                "contact": contact_name,
                "full_name": " ".join(
                    x for x in (contact.first_name, contact.last_name) if x
                ).strip(),
                "email": contact.email_id,
                "mobile_no": contact.mobile_no,
                "phone": contact.phone,
                "is_primary": 1 if not rows else 0,
            }
        )

    return rows


def _is_primary(item: dict) -> bool:
    from cortec_bitrix24.utils import is_truthy

    return is_truthy(item.get("IS_PRIMARY"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunks(iterable, size: int):
    """Trocea un generador en listas de `size` elementos."""
    iterator = iter(iterable)
    while True:
        chunk = list(islice(iterator, size))
        if not chunk:
            return
        yield chunk


def _status_type(status: str | None) -> str | None:
    if not status:
        return None
    return frappe.db.get_value("CRM Deal Status", status, "type")


def _resolve_currency(value: str | None) -> str | None:
    if not value:
        return None
    return value if frappe.db.exists("Currency", value) else None


def _fallback_status(ctx) -> str | None:
    status = frappe.get_all(
        "CRM Deal Status",
        filters={"type": "Open"},
        fields=["name"],
        order_by="position asc",
        limit=1,
    )
    if status:
        return status[0].name

    ctx.log(
        "warning",
        "No hay ningún CRM Deal Status de tipo 'Open'. Las negociaciones sin "
        "etapa mapeada quedarán sin estado.",
    )
    return None
