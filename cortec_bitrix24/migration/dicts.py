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
Fase 1 — Diccionarios.

Trae de Bitrix24 los catálogos de los que cuelga todo lo demás:

    STATUS            → CRM Lead Status
    DEAL_STAGE[_n]    → CRM Deal Status (uno por etapa y embudo)
    SOURCE            → CRM Lead Source
    INDUSTRY          → CRM Industry

y deja en Bitrix24 Status Map la correspondencia etapa → estado, que es
lo que consultan las fases de leads y de negociaciones.

Sobre el tipo de estado
-----------------------
`SEMANTICS` de Bitrix24 vale "S" (ganado), "F" (perdido) o nada (en
curso). Las etapas en curso se mapean a **"Open"**, no a "Ongoing",
aunque Frappe CRM ofrezca ambos. El motivo es de integración:
`cortec_helpdesk.overrides.whatsapp._has_open_reference` compara el tipo
con la cadena exacta "Open", y un WhatsApp entrante sobre un Lead
marcado "Ongoing" se tomaría como no atendido y generaría un Lead
duplicado. Quien prefiera el matiz puede reclasificar a mano después:
el mapa es editable.
"""

import frappe

from cortec_bitrix24.utils import as_int, local_cache

# ENTITY_ID de crm.status.list → cómo tratarlo.
LEAD_STATUS_ENTITY = "STATUS"
SOURCE_ENTITY = "SOURCE"
INDUSTRY_ENTITY = "INDUSTRY"
DEAL_STAGE_PREFIX = "DEAL_STAGE"

# Colores de Frappe CRM (CRM Lead Status.color). Bitrix24 entrega un
# hex arbitrario; se aproxima al nombre más cercano por tono, y lo que
# no se reconoce cae en "gray". Es cosmético: no afecta la migración.
HEX_TO_COLOR = {
    "#39a8ef": "blue",
    "#55d0e0": "cyan",
    "#47e4c2": "teal",
    "#7bd500": "green",
    "#a8e050": "green",
    "#ffa900": "orange",
    "#ff5752": "red",
    "#f7526f": "pink",
    "#9985df": "violet",
    "#c2e0ff": "blue",
    "#ffe000": "yellow",
    "#ff9e00": "amber",
}


def run(ctx) -> None:
    categories = _fetch_deal_categories(ctx)
    ctx.note("embudos", {str(k): v for k, v in categories.items()})

    statuses = fetch_all_statuses(ctx.client)

    buckets = {"lead_status": 0, "deal_status": 0, "source": 0, "industry": 0}

    for row in statuses:
        entity = (row.get("ENTITY_ID") or "").strip()

        if entity == LEAD_STATUS_ENTITY:
            _sync_lead_status(ctx, row)
            buckets["lead_status"] += 1
        elif entity.startswith(DEAL_STAGE_PREFIX):
            _sync_deal_status(ctx, row, entity, categories)
            buckets["deal_status"] += 1
        elif entity == SOURCE_ENTITY:
            _sync_simple(ctx, "CRM Lead Source", "source_name", row)
            buckets["source"] += 1
        elif entity == INDUSTRY_ENTITY:
            _sync_simple(ctx, "CRM Industry", "industry", row)
            buckets["industry"] += 1

    ctx.note("catalogos", buckets)
    ctx.checkpoint()


def fetch_all_statuses(client) -> list[dict]:
    """
    Todas las filas de crm.status.list, sin repetir.

    El método no admite el filtro `>ID`, y según el portal pagina con
    `start`/`next` o devuelve la lista entera en cada llamada ignorando
    `start`. Antes se pedía la página siguiente mientras la lista
    acumulada tuviera 50 filas o más: en un portal con más de 50 estados
    que ignora `start`, cada vuelta volvía a añadir la lista entera y el
    bucle no terminaba nunca (la corrida en seco se quedaba horas en
    «Diccionarios»).

    Ahora se sigue `next` mientras traiga filas nuevas, y se para en
    cuanto una página no aporta ninguna.
    """
    rows: list[dict] = []
    seen: set[str] = set()
    start = 0

    while True:
        raw = client.call_raw("crm.status.list", {"start": start}) or {}
        page = raw.get("result") or []

        new = [r for r in page if str(r.get("ID")) not in seen]
        if not new:
            break

        for row in new:
            seen.add(str(row.get("ID")))
        rows.extend(new)

        next_start = raw.get("next")
        if next_start is None or int(next_start) <= start:
            break
        start = int(next_start)

    return rows


# ---------------------------------------------------------------------------
# Embudos
# ---------------------------------------------------------------------------


def _fetch_deal_categories(ctx) -> dict[str, str]:
    """
    {CATEGORY_ID: nombre del embudo}. El embudo principal es el 0 y en
    algunos portales no aparece en la lista, así que se añade a mano.

    Los portales recientes sustituyeron crm.dealcategory.list por
    crm.category.list; se intentan los dos antes de darse por vencido,
    porque sin los nombres las etapas de distintos embudos colisionan.
    """
    categories: dict[str, str] = {"0": "General"}

    for method, params in (
        ("crm.dealcategory.list", {}),
        ("crm.category.list", {"entityTypeId": 2}),
    ):
        try:
            result = ctx.client.call(method, params)
        except Exception as e:
            ctx.log("warning", f"{method} no disponible ({e}).")
            continue

        rows = result if isinstance(result, list) else (result or {}).get("categories")
        if not rows:
            continue

        for row in rows:
            cid = str(row.get("ID") or row.get("id") or "")
            name = (row.get("NAME") or row.get("name") or "").strip()
            if cid and name:
                categories[cid] = name
        return categories

    ctx.log(
        "warning",
        "No se pudieron leer los embudos del portal. Las etapas de embudos "
        "distintos con el mismo nombre se distinguirán solo por su STAGE_ID.",
    )
    return categories


# ---------------------------------------------------------------------------
# Estados
# ---------------------------------------------------------------------------


def _sync_lead_status(ctx, row: dict) -> None:
    label = (row.get("NAME") or row.get("STATUS_ID") or "").strip()
    if not label:
        return

    status_type = _status_type(row)

    _upsert_catalog(
        ctx,
        "CRM Lead Status",
        "lead_status",
        label,
        {
            "position": as_int(row.get("SORT")),
            "type": status_type,
            "color": _color(row.get("COLOR")),
        },
    )

    _upsert_status_map(
        ctx,
        entity="Lead",
        bitrix_status_id=row.get("STATUS_ID"),
        bitrix_label=label,
        category_id="",
        status_doctype="CRM Lead Status",
        crm_status=label,
        status_type=status_type,
    )


def _sync_deal_status(ctx, row: dict, entity: str, categories: dict) -> None:
    """
    Una etapa de negociación. `entity` es `DEAL_STAGE` (embudo
    principal) o `DEAL_STAGE_<id>`.

    El nombre del CRM Deal Status lleva el embudo como prefijo cuando no
    es el principal: `CRM Deal Status` se nombra por su etiqueta, y dos
    embudos con una etapa "Nuevo" se pisarían entre sí, dejando las
    negociaciones de uno con las etapas del otro.
    """
    label = (row.get("NAME") or row.get("STATUS_ID") or "").strip()
    if not label:
        return

    category_id = entity[len(DEAL_STAGE_PREFIX) :].lstrip("_") or "0"
    category_name = categories.get(category_id, "")

    status_name = label
    if category_id != "0" and category_name:
        status_name = f"{category_name} — {label}"

    status_type = _status_type(row)

    _upsert_catalog(
        ctx,
        "CRM Deal Status",
        "deal_status",
        status_name,
        {
            "position": as_int(row.get("SORT")),
            "type": status_type,
            "color": _color(row.get("COLOR")),
        },
    )

    _upsert_status_map(
        ctx,
        entity="Deal",
        bitrix_status_id=row.get("STATUS_ID"),
        bitrix_label=label,
        category_id=category_id,
        status_doctype="CRM Deal Status",
        crm_status=status_name,
        status_type=status_type,
    )


def _status_type(row: dict) -> str:
    semantics = (row.get("SEMANTICS") or "").strip().upper()
    if semantics == "S":
        return "Won"
    if semantics == "F":
        return "Lost"
    return "Open"


def _color(hex_value: str | None) -> str:
    return HEX_TO_COLOR.get((hex_value or "").strip().lower(), "gray")


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------


def _sync_simple(ctx, doctype: str, fieldname: str, row: dict) -> None:
    """Catálogos de un solo campo: fuentes e industrias."""
    label = (row.get("NAME") or "").strip()
    if label:
        _upsert_catalog(ctx, doctype, fieldname, label, {})


def _upsert_catalog(ctx, doctype: str, fieldname: str, label: str, extra: dict) -> None:
    """
    Crea la fila del catálogo si falta.

    Si ya existe **no se toca**: puede ser un estado nativo de Frappe
    CRM que el equipo ya está usando, y pisarle el color o la posición
    desde Bitrix24 sería una sorpresa desagradable.
    """
    if ctx.dry_run:
        if not frappe.db.exists(doctype, label):
            ctx.add_to_note_list(f"crear_{doctype}", label)
        return

    if frappe.db.exists(doctype, label):
        ctx.count("skipped")
        return

    doc = frappe.new_doc(doctype)
    doc.set(fieldname, label)
    doc.update({k: v for k, v in extra.items() if v})
    doc.insert(ignore_permissions=True)
    ctx.count("created")


def _upsert_status_map(
    ctx,
    entity: str,
    bitrix_status_id,
    bitrix_label: str,
    category_id: str,
    status_doctype: str,
    crm_status: str,
    status_type: str,
) -> None:
    """
    Fila de Bitrix24 Status Map.

    Se respeta lo que un humano haya cambiado: si la fila ya existe con
    un `crm_status` distinto del que Bitrix24 sugiere, se deja como
    está. El mapa es precisamente el sitio previsto para corregir a mano
    una correspondencia que la heurística no acierta.
    """
    if ctx.dry_run:
        return

    name = f"{entity}::{bitrix_status_id}"

    if frappe.db.exists("Bitrix24 Status Map", name):
        frappe.db.set_value(
            "Bitrix24 Status Map",
            name,
            {"bitrix_label": bitrix_label, "category_id": category_id},
            update_modified=False,
        )
        return

    frappe.get_doc(
        {
            "doctype": "Bitrix24 Status Map",
            "entity": entity,
            "bitrix_status_id": str(bitrix_status_id),
            "bitrix_label": bitrix_label,
            "category_id": category_id,
            "status_doctype": status_doctype,
            "crm_status": crm_status,
            "status_type": status_type,
        }
    ).insert(ignore_permissions=True)


# ---------------------------------------------------------------------------
# Consulta (la usan leads.py y deals.py)
# ---------------------------------------------------------------------------


_CATALOG_CACHE_KEY = "_b24_catalog_cache"


def resolve_catalog(ctx, entity_id: str, code, doctype: str) -> str | None:
    """
    Código de un catálogo de Bitrix24 → nombre del documento de Frappe.

    Los registros del CRM no guardan la etiqueta del catálogo sino su
    código (`SOURCE_ID = "WEB"`, `INDUSTRY = "IT"`), mientras que la
    fase de diccionarios creó las CRM Lead Source / CRM Industry por su
    etiqueta. Aquí se cierra ese hueco con un `crm.status.list` por
    catálogo y corrida, cacheado en el hilo.

    Devuelve None si el destino no existe, para no dejar un Link roto
    apuntando a un documento inexistente.
    """
    if not code:
        return None

    cache = local_cache(_CATALOG_CACHE_KEY)

    if entity_id not in cache:
        try:
            rows = (
                ctx.client.call(
                    "crm.status.list", {"filter": {"ENTITY_ID": entity_id}}
                )
                or []
            )
        except Exception as e:
            ctx.log("warning", f"No se pudo leer el catálogo {entity_id}: {e}")
            rows = []

        cache[entity_id] = {
            str(r.get("STATUS_ID")): (r.get("NAME") or "").strip()
            for r in rows
            if r.get("STATUS_ID")
        }

    label = cache[entity_id].get(str(code))

    if not label:
        ctx.add_to_note_list("catalogos_sin_mapear", f"{entity_id}: {code}")
        return None

    if not frappe.db.exists(doctype, label):
        ctx.add_to_note_list("catalogos_sin_destino", f"{doctype}: {label}")
        return None

    return label


def resolve_status(ctx, entity: str, bitrix_status_id, fallback: str | None = None):
    """
    Estado de Frappe CRM para una etapa de Bitrix24.

    Un ID sin mapear se anota una sola vez en el informe de la corrida
    (no por cada registro afectado) y cae al `fallback`.
    """
    if not bitrix_status_id:
        return fallback

    name = f"{entity}::{bitrix_status_id}"
    status = frappe.db.get_value("Bitrix24 Status Map", name, "crm_status")

    if not status:
        ctx.add_to_note_list("estados_sin_mapear", f"{entity}: {bitrix_status_id}")
        return fallback

    return status
