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
Consentimientos de los formularios web → CORTEC Consent Record.

No es una fase de la corrida: la API REST de Bitrix24 no expone la lista
de consentimientos (solo `userconsent.agreement.list/text` y
`userconsent.consent.add`). La fuente es el CSV que se exporta a mano de
la pantalla «Consentimiento del usuario» de cada acuerdo, con las
columnas Fecha, Dirección IP, Origen y Dirección.

Cada fila apunta a una «Actividad del CRM #N»: la actividad que dejó el
envío del formulario. Su dueño (Lead, Contacto o Negociación) es el
titular; se resuelve con crm.activity.list y el ID Map de la migración,
así que esto se ejecuta **después** de la corrida real.

Los registros se crean con cortec_helpdesk.consent_log.register_consent,
que es idempotente por referencia de origen: importar dos veces el mismo
CSV no duplica nada.
"""

import csv
import io
import re
import unicodedata
from datetime import datetime

import frappe
from frappe import _

from cortec_bitrix24.migration import idmap
from cortec_bitrix24.migration.activities import OWNER_TYPES
from cortec_bitrix24.utils import as_int

# Encabezado del CSV (sin tildes, en minúsculas) → clave interna.
COLUMNS = {
    "fecha": "date",
    "direccion ip": "ip",
    "origen": "origin",
    "direccion": "url",
}

# Formatos en que llega la fecha: el de la pantalla de Bitrix24 y los
# que deja Excel si reinterpreta la columna al guardar.
DATE_FORMATS = (
    "%d/%m/%Y %H:%M:%S",
    "%d/%m/%Y %H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
)

ACTIVITY_PATTERN = re.compile(r"#\s*(\d+)")

# Máximo de IDs por llamada a crm.activity.list.
CHUNK_SIZE = 50

OWNER_LEAD, OWNER_DEAL, OWNER_CONTACT = 1, 2, 3


# ---------------------------------------------------------------------------
# Punto de entrada
# ---------------------------------------------------------------------------


def import_consents(
    client, content, agreement: str, dry_run: bool = True, subscribe: bool = False
) -> dict:
    from cortec_helpdesk import consent_log

    consent_log.get_agreement(agreement)

    rows = parse_csv(content)
    informe = {
        "acuerdo": agreement,
        "modo": "dry-run" if dry_run else "real",
        "filas": len(rows),
        "sin_actividad": [r["line"] for r in rows if not r["activity_id"]],
        "sin_fecha": [r["line"] for r in rows if r["activity_id"] and not r["date"]],
        "sin_resolver": [],
        "suprimidos": 0,
        "por_doctype": {},
        "ya_presentes": 0,
        "registrados": 0,
    }

    valid = [r for r in rows if r["activity_id"] and r["date"]]
    owners = resolve_owners(client, [r["activity_id"] for r in valid])

    for row in valid:
        subject = owners.get(row["activity_id"])
        if not subject:
            informe["sin_resolver"].append(row["activity_id"])
            continue

        doctype, name, email = subject
        if idmap.is_suppressed(doctype, name):
            informe["suprimidos"] += 1
            continue
        informe["por_doctype"][doctype] = informe["por_doctype"].get(doctype, 0) + 1

        source_reference = source_reference_for(row["activity_id"])
        if frappe.db.exists(
            consent_log.RECORD_DOCTYPE,
            {"agreement": agreement, "source_reference": source_reference},
        ):
            informe["ya_presentes"] += 1
            continue

        if dry_run:
            continue

        consent_log.register_consent(
            agreement,
            consent_log.GRANTED,
            email,
            channel="Bitrix24",
            consent_datetime=row["date"],
            ip=row["ip"],
            page_url=row["url"],
            reference=(doctype, name),
            source_reference=source_reference,
            notes=(
                f"Importado del registro «Consentimiento del usuario» de Bitrix24 "
                f"(origen: {row['origin']}). La fecha es la hora del portal."
            ),
            update_mailing_list=subscribe,
        )
        informe["registrados"] += 1

    return informe


def source_reference_for(activity_id: int) -> str:
    return f"Bitrix24 actividad #{activity_id}"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


def parse_csv(content) -> list[dict]:
    """
    Filas del CSV como dicts {line, date, ip, origin, url, activity_id}.

    Admite UTF-8 (con o sin BOM) y la codificación de Windows que usa
    Excel al guardar como CSV, y separador `;`, `,` o tabulador (esto
    último si se pegó la tabla directamente desde el navegador).
    """
    text = _decode(content)
    if not text.strip():
        return []

    try:
        dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=";,\t")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text), dialect)
    header = [_normalize_header(h) for h in next(reader, [])]

    missing = [h for h in COLUMNS if h not in header]
    if missing:
        frappe.throw(
            _("Al CSV le faltan columnas: {0}. Encabezados leídos: {1}").format(
                ", ".join(missing), ", ".join(header)
            )
        )

    index = {COLUMNS[h]: header.index(h) for h in COLUMNS}

    rows = []
    for line, values in enumerate(reader, start=2):
        if not any(v.strip() for v in values):
            continue

        def cell(key):
            i = index[key]
            return values[i].strip() if i < len(values) else ""

        origin = cell("origin")
        rows.append(
            {
                "line": line,
                "date": parse_date(cell("date")),
                "ip": cell("ip") or None,
                "origin": origin,
                "url": cell("url") or None,
                "activity_id": parse_activity_id(origin),
            }
        )

    return rows


def parse_date(value: str) -> datetime | None:
    value = (value or "").strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def parse_activity_id(origin: str) -> int | None:
    """«Actividad del CRM #81633» → 81633."""
    match = ACTIVITY_PATTERN.search(origin or "")
    return int(match.group(1)) if match else None


def _decode(content) -> str:
    if isinstance(content, str):
        return content.lstrip("﻿")
    try:
        return content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return content.decode("cp1252")


def _normalize_header(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "")
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(value.lower().split())


# ---------------------------------------------------------------------------
# Titulares
# ---------------------------------------------------------------------------


def resolve_owners(client, activity_ids: list[int]) -> dict[int, tuple[str, str, str | None]]:
    """
    {ID de actividad: (doctype, nombre, correo)} de las que se pudieron
    resolver a un CRM Lead o Contact ya migrados.

    Una negociación no es un titular: se usa su contacto principal.
    Una empresa tampoco, y no tiene a quién delegar; queda sin resolver.
    """
    owners = {}
    deal_ids = {}

    ids = sorted(set(activity_ids))
    for start in range(0, len(ids), CHUNK_SIZE):
        chunk = ids[start : start + CHUNK_SIZE]
        for row in client.list_all(
            "crm.activity.list",
            {"filter": {"ID": chunk}, "select": ["ID", "OWNER_TYPE_ID", "OWNER_ID"]},
        ):
            activity_id = as_int(row.get("ID"))
            owner_type = as_int(row.get("OWNER_TYPE_ID"))
            owner_id = row.get("OWNER_ID")

            if owner_type == OWNER_DEAL:
                deal_ids[activity_id] = owner_id
                continue

            subject = _subject(owner_type, owner_id)
            if subject:
                owners[activity_id] = subject

    deal_contacts = {}
    for activity_id, deal_id in deal_ids.items():
        if deal_id not in deal_contacts:
            try:
                deal = client.call("crm.deal.get", {"id": deal_id}) or {}
            except Exception:
                deal = {}
            deal_contacts[deal_id] = deal.get("CONTACT_ID")

        subject = _subject(OWNER_CONTACT, deal_contacts[deal_id])
        if subject:
            owners[activity_id] = subject

    return owners


def _subject(owner_type: int, owner_id) -> tuple[str, str, str | None] | None:
    if owner_type not in (OWNER_LEAD, OWNER_CONTACT) or not as_int(owner_id):
        return None

    entity_type, doctype = OWNER_TYPES[owner_type]
    name = idmap.get_target_name(entity_type, owner_id)
    if not name:
        return None

    email_field = "email" if doctype == "CRM Lead" else "email_id"
    return doctype, name, frappe.db.get_value(doctype, name, email_field)
