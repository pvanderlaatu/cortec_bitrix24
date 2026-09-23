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
Fase 4 — Empresas → CRM Organization.

Es la primera fase que escribe documentos del CRM, y la que sienta dos
convenciones que repiten las siguientes:

  * el recorrido usa `list_all` con cursor reanudable, y
  * cada registro pasa por `idmap.upsert`, nunca por un insert directo.

Detalle propio de CRM Organization: se llama por su nombre
(`autoname: field:organization_name`), así que dos empresas distintas de
Bitrix24 con el mismo TITLE colisionarían. `_unique_name()` desambigua
añadiendo un sufijo, y el ID Map guarda a cuál corresponde cada una.
"""

import frappe

from cortec_bitrix24.migration import dicts, idmap
from cortec_bitrix24.migration.userfields import build_uf_values
from cortec_bitrix24.utils import (
    as_float,
    first_value,
    parse_datetime_str,
    to_bitrix_datetime,
    truncate,
)

SELECT_FIELDS = ["*", "UF_*"]

# EMPLOYEES de Bitrix24 → opciones de `no_of_employees` en Frappe CRM.
EMPLOYEES_MAP = {
    "EMPLOYEES_1": "1-10",
    "EMPLOYEES_2": "11-50",
    "EMPLOYEES_3": "51-200",
    "EMPLOYEES_4": "201-500",
    "EMPLOYEES_5": "501-1000",
}


def run(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">DATE_MODIFY": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("empresas_en_bitrix", ctx.client.count("crm.company.list"))

    for row in ctx.client.list_all(
        "crm.company.list", params, start_after_id=ctx.cursor()
    ):
        bitrix_id = row.get("ID")
        try:
            _sync_company(ctx, row)
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar la empresa {bitrix_id}: {e}",
                bitrix_id=bitrix_id,
            )
        ctx.set_cursor(int(bitrix_id))

    ctx.checkpoint()


def _sync_company(ctx, row: dict) -> None:
    bitrix_id = row.get("ID")
    title = (row.get("TITLE") or "").strip() or f"Empresa {bitrix_id}"

    values = {
        "organization_name": _unique_name(ctx, bitrix_id, title),
        "website": first_value(row.get("WEB")) or None,
        "industry": dicts.resolve_catalog(
            ctx, "INDUSTRY", row.get("INDUSTRY"), "CRM Industry"
        ),
        "annual_revenue": as_float(row.get("REVENUE")) or None,
        "no_of_employees": EMPLOYEES_MAP.get(row.get("EMPLOYEES") or ""),
        "company_description": truncate(row.get("COMMENTS"), 500) or None,
        "currency": _resolve_currency(row.get("CURRENCY_ID")),
    }

    values.update(build_uf_values(ctx, "Company", row))

    idmap.upsert(
        ctx,
        idmap.ENTITY_COMPANY,
        bitrix_id,
        "CRM Organization",
        values,
        created_on=parse_datetime_str(row.get("DATE_CREATE")),
        modified_on=parse_datetime_str(row.get("DATE_MODIFY")),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _unique_name(ctx, bitrix_id, title: str) -> str:
    """
    Nombre libre para la organización.

    Si ya existe una CRM Organization con ese nombre y **no** es la que
    corresponde a esta empresa de Bitrix24, se añade un sufijo. Fundir
    las dos sería peor: perderíamos las negociaciones de una de ellas.
    """
    existing = idmap.get_target(idmap.ENTITY_COMPANY, bitrix_id)
    if existing and existing[1] == title:
        return title

    if not frappe.db.exists("CRM Organization", title):
        return title

    if existing:
        # Ya la migramos antes con otro nombre: conservarlo evita
        # renombrar el documento en cada pasada.
        return existing[1]

    candidate = f"{title} (B24-{bitrix_id})"
    ctx.log(
        "warning",
        f"Ya existía una CRM Organization llamada '{title}'. La empresa "
        f"{bitrix_id} de Bitrix24 se migra como '{candidate}'. Si son la misma, "
        f"fusiónelas a mano después de la corrida.",
        bitrix_id=bitrix_id,
    )
    return candidate


def _resolve_currency(value: str | None) -> str | None:
    if not value:
        return None
    return value if frappe.db.exists("Currency", value) else None
