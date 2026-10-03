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

Etapas: asignar o crear
-----------------------
Según «Etapas de Bitrix24» en Settings:

- **Asignar a los estados de Frappe CRM** (por defecto): no se crea
  ningún estado. Cada etapa se propone contra los que ya existen
  (propose_targets): ganadas → tipo Won, perdidas → tipo Lost (las de
  basura a «Junk» si existe), en curso → repartidas en orden sobre los
  Open/Ongoing.
- **Crear como estados nuevos**: un estado por etapa, con su nombre de
  Bitrix24. `SEMANTICS` "S" → Won, "F" → Lost, nada → Open.

En los dos casos el resultado queda en Bitrix24 Status Map, **también en
la corrida en seco**: es configuración, no datos del CRM, y es lo que se
revisa y corrige a mano antes de la corrida real. Una fila corregida a
mano no se vuelve a pisar.
"""

import frappe

from cortec_bitrix24.utils import as_int, local_cache

# Valores del campo «Etapas de Bitrix24» de Settings.
STRATEGY_ASSIGN = "Asignar a los estados de Frappe CRM"
STRATEGY_CREATE = "Crear como estados nuevos"

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

    lead_stages = [r for r in statuses if (r.get("ENTITY_ID") or "").strip() == LEAD_STATUS_ENTITY]
    deal_stages: dict[str, list[dict]] = {}
    for row in statuses:
        entity = (row.get("ENTITY_ID") or "").strip()
        if entity.startswith(DEAL_STAGE_PREFIX):
            deal_stages.setdefault(entity, []).append(row)

    buckets["lead_status"] = len(lead_stages)
    buckets["deal_status"] = sum(len(v) for v in deal_stages.values())

    if (ctx.settings.get("status_strategy") or STRATEGY_ASSIGN) == STRATEGY_ASSIGN:
        _assign_stages(ctx, "Lead", "CRM Lead Status", {LEAD_STATUS_ENTITY: lead_stages})
        _assign_stages(ctx, "Deal", "CRM Deal Status", deal_stages)
    else:
        for row in lead_stages:
            _sync_lead_status(ctx, row)
        for entity, rows in deal_stages.items():
            for row in rows:
                _sync_deal_status(ctx, row, entity, categories)

    for row in statuses:
        entity = (row.get("ENTITY_ID") or "").strip()

        if entity == SOURCE_ENTITY:
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
# Asignar las etapas a los estados de Frappe CRM
# ---------------------------------------------------------------------------


def _assign_stages(ctx, entity: str, status_doctype: str, stages_by_entity: dict) -> None:
    """
    Propone, para cada etapa de Bitrix24, uno de los estados que ya
    existen en Frappe CRM, y lo deja en Bitrix24 Status Map. No crea
    estados.
    """
    targets = frappe.get_all(
        status_doctype, fields=["name", "type"], order_by="position asc, creation asc"
    )
    types = {t.name: t.type for t in targets}

    if not targets:
        ctx.log(
            "warning",
            f"No hay ningún {status_doctype} en Frappe CRM: las etapas de {entity} "
            "quedan sin asignar en Bitrix24 Status Map.",
        )

    for bitrix_entity, stages in stages_by_entity.items():
        category_id = bitrix_entity[len(DEAL_STAGE_PREFIX) :].lstrip("_") or "0"
        if entity == "Lead":
            category_id = ""

        for status_id, target in propose_targets(stages, targets).items():
            stage = next(s for s in stages if str(s.get("STATUS_ID")) == status_id)
            label = (stage.get("NAME") or status_id).strip()
            _upsert_status_map(
                ctx,
                entity=entity,
                bitrix_status_id=status_id,
                bitrix_label=label,
                category_id=category_id,
                status_doctype=status_doctype,
                crm_status=target,
                status_type=types.get(target) or _status_type(stage),
            )
            ctx.add_to_note_list(
                "etapas_propuestas", f"{entity}: {label} → {target or '(sin asignar)'}"
            )


def propose_targets(stages: list[dict], targets: list) -> dict[str, str | None]:
    """
    {STATUS_ID de Bitrix24: nombre del estado de Frappe CRM propuesto}.

    `targets` son los estados existentes, en orden de posición, con
    `name` y `type` (Open, Ongoing, Won, Lost).

    - Ganada (SEMANTICS "S") → el primer estado Won. Si no hay ninguno
      (en Frappe CRM los estados de Lead no tienen Won), el último de
      los en curso, que suele ser el más avanzado (p. ej. «Qualified»).
    - Perdida (SEMANTICS "F") → «Junk» si la etapa es de basura y ese
      estado existe; si no, el primer Lost que no sea «Junk».
    - En curso → se reparten en orden sobre los Open/Ongoing: la
      primera etapa al primer estado, la última al último.
    """

    def get(t, key):
        return t.get(key) if isinstance(t, dict) else getattr(t, key, None)

    open_ = [get(t, "name") for t in targets if get(t, "type") in ("Open", "Ongoing")]
    won = [get(t, "name") for t in targets if get(t, "type") == "Won"]
    lost = [get(t, "name") for t in targets if get(t, "type") == "Lost"]
    junk = next((n for n in lost if (n or "").strip().lower() == "junk"), None)
    lost_other = [n for n in lost if n != junk]

    in_progress = sorted(
        (s for s in stages if _status_type(s) == "Open"), key=lambda s: as_int(s.get("SORT"))
    )

    result: dict[str, str | None] = {}
    for stage in stages:
        status_id = str(stage.get("STATUS_ID"))
        kind = _status_type(stage)

        if kind == "Won":
            target = won[0] if won else (open_[-1] if open_ else None)
        elif kind == "Lost":
            text = f"{status_id} {stage.get('NAME') or ''}".lower()
            if junk and "junk" in text:
                target = junk
            else:
                target = (lost_other or lost or [None])[0]
        else:
            if not open_:
                target = None
            else:
                index = in_progress.index(stage)
                target = open_[index * len(open_) // max(len(in_progress), 1)]

        result[status_id] = target

    return result


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
    un `crm_status`, se deja como está. El mapa es precisamente el sitio
    previsto para corregir a mano una correspondencia que la heurística
    no acierta.

    Se escribe también en seco. Un `crm_status` que todavía no existe
    (estado que se creará en la corrida real) se deja vacío y se rellena
    en la corrida real.
    """
    name = f"{entity}::{bitrix_status_id}"
    target = crm_status if crm_status and frappe.db.exists(status_doctype, crm_status) else None

    if frappe.db.exists("Bitrix24 Status Map", name):
        values = {"bitrix_label": bitrix_label, "category_id": category_id}
        if target and not frappe.db.get_value("Bitrix24 Status Map", name, "crm_status"):
            values.update({"crm_status": target, "status_type": status_type})
        frappe.db.set_value("Bitrix24 Status Map", name, values, update_modified=False)
        return

    frappe.get_doc(
        {
            "doctype": "Bitrix24 Status Map",
            "entity": entity,
            "bitrix_status_id": str(bitrix_status_id),
            "bitrix_label": bitrix_label,
            "category_id": category_id,
            "status_doctype": status_doctype,
            "crm_status": target,
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

    # En seco la fase de diccionarios no crea los catálogos, solo los
    # anota en «crear_<doctype>»: los que vaya a crear cuentan como
    # existentes, para no avisar de un problema que no lo es.
    if ctx.dry_run and label in (ctx.summary.get("dicts") or {}).get(f"crear_{doctype}", []):
        return label

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
