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
Fase 8 — Actividades y timeline.

Es la fase que convierte el contact center en historial consultable. El
timeline de Bitrix24 mete en una sola tabla cosas que en Frappe viven en
doctypes distintos, así que se ramifica por `PROVIDER_ID`:

    correo          → Communication   (aparece en el hilo del Lead/Deal)
    llamada         → CRM Call Log
    tarea / to-do   → CRM Task
    Open Channels   → se omite aquí; es la fase 9 (openlines.py)
    lo demás        → FCRM Note

Los comentarios del timeline (`crm.timeline.comment.list`) son un método
aparte que solo acepta una entidad por llamada, así que se piden en
bloques de 50 con `batch` sobre los Leads y Deals ya migrados.

Los adjuntos de cada actividad se bajan en el momento, junto con ella
(ver `files.attach`): un adjunto separado de su correo no le sirve a
nadie, y recorrer el timeline una segunda vez solo para los ficheros
duplicaría el gasto de peticiones.
"""

import frappe

from cortec_bitrix24.migration import files, idmap
from cortec_bitrix24.migration.users import resolve_user
from cortec_bitrix24.utils import (
    as_int,
    bbcode_to_text,
    html_to_text,
    normalize_phone,
    parse_datetime_str,
    to_bitrix_datetime,
    truncate,
)

# OWNER_TYPE_ID de Bitrix24 → (tipo en el ID Map, doctype de Frappe).
OWNER_TYPES = {
    1: (idmap.ENTITY_LEAD, "CRM Lead"),
    2: (idmap.ENTITY_DEAL, "CRM Deal"),
    3: (idmap.ENTITY_CONTACT, "Contact"),
    4: (idmap.ENTITY_COMPANY, "CRM Organization"),
}

# PROVIDER_ID → categoría de destino.
EMAIL_PROVIDERS = {"CRM_EMAIL", "EMAIL"}
CALL_PROVIDERS = {"VOXIMPLANT_CALL", "CRM_CALL", "CALL", "REST_APP_CALL"}
TASK_PROVIDERS = {"CRM_TODO", "TASK", "CRM_REMINDER"}

# Los mensajes de Open Channels (WhatsApp) aparecen también como
# actividad. Se ignoran aquí para no duplicarlos: la fase 9 los migra
# con su conversación completa, que es donde tienen sentido.
OPENLINES_PROVIDERS = {"IMOPENLINES_SESSION", "IMOPENLINES", "CRM_IMOPENLINES"}

# TYPE_ID, por si falta PROVIDER_ID (actividades antiguas).
TYPE_MEETING, TYPE_CALL, TYPE_TASK, TYPE_EMAIL = 1, 2, 3, 4

# DIRECTION de Bitrix24.
DIRECTION_INCOMING = 1

SELECT_FIELDS = [
    "ID", "OWNER_ID", "OWNER_TYPE_ID", "TYPE_ID", "PROVIDER_ID",
    "PROVIDER_TYPE_ID", "SUBJECT", "DESCRIPTION", "DESCRIPTION_TYPE",
    "DIRECTION", "COMPLETED", "RESPONSIBLE_ID", "AUTHOR_ID", "CREATED",
    "LAST_UPDATED", "START_TIME", "END_TIME", "DEADLINE", "COMMUNICATIONS",
    "FILES", "SETTINGS",
]


def run(ctx) -> None:
    _migrate_activities(ctx)
    _migrate_timeline_comments(ctx)
    ctx.checkpoint()


# ---------------------------------------------------------------------------
# Actividades
# ---------------------------------------------------------------------------


def _migrate_activities(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">LAST_UPDATED": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("actividades_en_bitrix", ctx.client.count("crm.activity.list"))

    por_tipo = {"correo": 0, "llamada": 0, "tarea": 0, "nota": 0, "omitidas": 0}

    for row in ctx.client.list_all(
        "crm.activity.list", params, start_after_id=ctx.cursor("activity_id")
    ):
        bitrix_id = row.get("ID")
        try:
            kind = _sync_activity(ctx, row)
            por_tipo[kind] = por_tipo.get(kind, 0) + 1
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar la actividad {bitrix_id}: {e}",
                bitrix_id=bitrix_id,
            )
        ctx.set_cursor(int(bitrix_id), "activity_id")

    ctx.note("actividades_por_tipo", por_tipo)


def _sync_activity(ctx, row: dict) -> str:
    provider = (row.get("PROVIDER_ID") or "").strip().upper()
    type_id = as_int(row.get("TYPE_ID"))

    if provider in OPENLINES_PROVIDERS:
        return "omitidas"

    reference = _resolve_reference(ctx, row)
    if not reference:
        # La entidad dueña no se migró (fase apagada, o un tipo de
        # entidad que esta migración no cubre). Sin documento al que
        # colgar la actividad, guardarla sería dejar huérfano un
        # registro que nadie va a encontrar.
        return "omitidas"

    ref_doctype, ref_name = reference

    if provider in EMAIL_PROVIDERS or type_id == TYPE_EMAIL:
        _sync_email(ctx, row, ref_doctype, ref_name)
        return "correo"

    if provider in CALL_PROVIDERS or type_id == TYPE_CALL:
        _sync_call(ctx, row, ref_doctype, ref_name)
        return "llamada"

    if provider in TASK_PROVIDERS or type_id == TYPE_TASK:
        _sync_task(ctx, row, ref_doctype, ref_name)
        return "tarea"

    _sync_note(ctx, row, ref_doctype, ref_name)
    return "nota"


# ---------------------------------------------------------------------------
# Correos
# ---------------------------------------------------------------------------


def _sync_email(ctx, row: dict, ref_doctype: str, ref_name: str) -> None:
    """
    Una actividad de correo → Communication.

    `communication_type` queda en "Communication" y `status` en "Linked",
    que es lo que hace que el correo salga en el hilo del Lead/Deal en
    /crm y en el timeline del documento en el escritorio.
    """
    bitrix_id = row.get("ID")
    incoming = as_int(row.get("DIRECTION")) == DIRECTION_INCOMING

    sender, recipients = _email_parties(row, incoming)
    content = _description_html(row)

    values = {
        "communication_type": "Communication",
        "communication_medium": "Email",
        "sent_or_received": "Received" if incoming else "Sent",
        "subject": truncate(row.get("SUBJECT") or "(sin asunto)", 140),
        "content": content,
        "text_content": html_to_text(content),
        "sender": sender or None,
        "recipients": recipients or None,
        "reference_doctype": ref_doctype,
        "reference_name": ref_name,
        "communication_date": parse_datetime_str(
            row.get("START_TIME") or row.get("CREATED"), fallback_now=True
        ),
        "status": "Linked",
        "seen": 1,
        "user": resolve_user(ctx, row.get("AUTHOR_ID"), fallback=None),
    }

    name = idmap.upsert(
        ctx,
        idmap.ENTITY_ACTIVITY,
        bitrix_id,
        "Communication",
        values,
        created_on=parse_datetime_str(row.get("CREATED")),
        modified_on=parse_datetime_str(row.get("LAST_UPDATED")),
    )

    if name:
        files.attach(ctx, row.get("FILES"), "Communication", name)


def _email_parties(row: dict, incoming: bool) -> tuple[str, str]:
    """
    Remitente y destinatarios a partir de COMMUNICATIONS.

    Bitrix24 guarda en COMMUNICATIONS la dirección del interlocutor (el
    cliente), no la del agente. En un correo entrante esa dirección es
    el remitente; en uno saliente, el destinatario. La otra punta no
    viaja en la actividad, así que se deja vacía en vez de inventarla.
    """
    direcciones = []
    for item in row.get("COMMUNICATIONS") or []:
        if not isinstance(item, dict):
            continue
        value = (item.get("VALUE") or "").strip()
        if value and value not in direcciones:
            direcciones.append(value)

    interlocutor = ", ".join(direcciones)

    if incoming:
        return (direcciones[0] if direcciones else ""), ""
    return "", interlocutor


# ---------------------------------------------------------------------------
# Llamadas
# ---------------------------------------------------------------------------


def _sync_call(ctx, row: dict, ref_doctype: str, ref_name: str) -> None:
    bitrix_id = row.get("ID")
    incoming = as_int(row.get("DIRECTION")) == DIRECTION_INCOMING

    numero = ""
    for item in row.get("COMMUNICATIONS") or []:
        if isinstance(item, dict) and item.get("VALUE"):
            numero = normalize_phone(item["VALUE"], ctx.country_code)
            break

    start = parse_datetime_str(row.get("START_TIME") or row.get("CREATED"))
    end = parse_datetime_str(row.get("END_TIME"))

    values = {
        "id": f"B24-{bitrix_id}",
        "type": "Incoming" if incoming else "Outgoing",
        "status": "Completed" if row.get("COMPLETED") in ("Y", True, 1) else "No Answer",
        "from": numero if incoming else None,
        "to": None if incoming else numero,
        "start_time": start,
        "end_time": end,
        "duration": _duration(start, end),
        "telephony_medium": "Manual",
        "medium": "Bitrix24",
        "reference_doctype": ref_doctype,
        "reference_docname": ref_name,
        "caller": resolve_user(ctx, row.get("RESPONSIBLE_ID"), fallback=None),
    }

    idmap.upsert(
        ctx,
        idmap.ENTITY_ACTIVITY,
        bitrix_id,
        "CRM Call Log",
        values,
        child_tables={
            "links": [{"link_doctype": ref_doctype, "link_name": ref_name}]
        },
        created_on=parse_datetime_str(row.get("CREATED")),
        modified_on=parse_datetime_str(row.get("LAST_UPDATED")),
    )


def _duration(start: str | None, end: str | None) -> int | None:
    if not start or not end:
        return None
    from frappe.utils import get_datetime, time_diff_in_seconds

    try:
        seconds = int(time_diff_in_seconds(get_datetime(end), get_datetime(start)))
    except Exception:
        return None
    return seconds if seconds > 0 else None


# ---------------------------------------------------------------------------
# Tareas
# ---------------------------------------------------------------------------


def _sync_task(ctx, row: dict, ref_doctype: str, ref_name: str) -> None:
    bitrix_id = row.get("ID")
    completada = row.get("COMPLETED") in ("Y", True, 1)

    values = {
        "title": truncate(row.get("SUBJECT") or f"Tarea B24 {bitrix_id}", 140),
        "description": _description_html(row),
        "status": "Done" if completada else "Todo",
        "due_date": parse_datetime_str(row.get("DEADLINE") or row.get("END_TIME")),
        "assigned_to": resolve_user(ctx, row.get("RESPONSIBLE_ID"), fallback=None),
        "reference_doctype": ref_doctype,
        "reference_docname": ref_name,
    }

    idmap.upsert(
        ctx,
        idmap.ENTITY_ACTIVITY,
        bitrix_id,
        "CRM Task",
        values,
        created_on=parse_datetime_str(row.get("CREATED")),
        modified_on=parse_datetime_str(row.get("LAST_UPDATED")),
    )


# ---------------------------------------------------------------------------
# Resto del timeline → nota
# ---------------------------------------------------------------------------


def _sync_note(ctx, row: dict, ref_doctype: str, ref_name: str) -> None:
    bitrix_id = row.get("ID")
    provider = (row.get("PROVIDER_ID") or "").strip()

    values = {
        "title": truncate(
            row.get("SUBJECT") or f"{provider or 'Actividad'} B24 {bitrix_id}", 140
        ),
        "content": _description_html(row),
        "reference_doctype": ref_doctype,
        "reference_docname": ref_name,
    }

    name = idmap.upsert(
        ctx,
        idmap.ENTITY_ACTIVITY,
        bitrix_id,
        "FCRM Note",
        values,
        created_on=parse_datetime_str(row.get("CREATED")),
        modified_on=parse_datetime_str(row.get("LAST_UPDATED")),
    )

    if name:
        files.attach(ctx, row.get("FILES"), "FCRM Note", name)


# ---------------------------------------------------------------------------
# Comentarios del timeline
# ---------------------------------------------------------------------------


def _migrate_timeline_comments(ctx) -> None:
    """
    Comentarios que los agentes escribieron en el timeline.

    `crm.timeline.comment.list` solo acepta una entidad por llamada, así
    que se recorren los Leads y Deals ya migrados en bloques de 50 y se
    piden con `batch`. El cursor avanza por nombre de fila del ID Map,
    para poder reanudar.
    """
    for entity_type, entity_type_id, doctype in (
        (idmap.ENTITY_LEAD, 1, "CRM Lead"),
        (idmap.ENTITY_DEAL, 2, "CRM Deal"),
    ):
        cursor_key = f"comments_{entity_type}"
        last = ctx.cursor(cursor_key)

        while True:
            rows = idmap.iter_migrated(entity_type, last, ctx.batch_size)
            if not rows:
                break

            commands = {
                r.bitrix_id: (
                    "crm.timeline.comment.list",
                    {
                        "filter": {
                            "ENTITY_ID": r.bitrix_id,
                            "ENTITY_TYPE": "deal" if entity_type_id == 2 else "lead",
                        }
                    },
                )
                for r in rows
            }

            try:
                results = ctx.client.batch(commands)
            except Exception as e:
                ctx.log("warning", f"No se pudieron leer comentarios: {e}")
                results = {}

            by_id = {r.bitrix_id: r.target_name for r in rows}

            for bitrix_id, comments in results.items():
                target = by_id.get(bitrix_id)
                if not target:
                    continue
                for comment in _as_list(comments):
                    try:
                        _sync_comment(ctx, comment, doctype, target)
                    except Exception as e:
                        ctx.log(
                            "error",
                            f"No se pudo migrar el comentario "
                            f"{comment.get('ID')}: {e}",
                            bitrix_id=comment.get("ID"),
                        )

            last = rows[-1].bitrix_id
            ctx.set_cursor(last, cursor_key)
            ctx.checkpoint()


def _sync_comment(ctx, comment: dict, ref_doctype: str, ref_name: str) -> None:
    bitrix_id = comment.get("ID")
    texto = bbcode_to_text(comment.get("COMMENT"))
    if not texto:
        return

    autor = resolve_user(ctx, comment.get("AUTHOR_ID"), fallback=None)

    values = {
        "title": truncate(texto, 80) or f"Comentario B24 {bitrix_id}",
        "content": frappe.utils.escape_html(texto).replace("\n", "<br>"),
        "reference_doctype": ref_doctype,
        "reference_docname": ref_name,
    }

    name = idmap.upsert(
        ctx,
        idmap.ENTITY_COMMENT,
        bitrix_id,
        "FCRM Note",
        values,
        created_on=parse_datetime_str(comment.get("CREATED")),
    )

    # El autor del comentario se conserva como propietario de la nota:
    # es el único sitio donde Frappe lo muestra.
    if name and autor and not ctx.dry_run:
        frappe.db.set_value("FCRM Note", name, "owner", autor, update_modified=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_reference(ctx, row: dict) -> tuple[str, str] | None:
    """Documento de Frappe dueño de la actividad, o None si no se migró."""
    owner_type = as_int(row.get("OWNER_TYPE_ID"))
    mapping = OWNER_TYPES.get(owner_type)
    if not mapping:
        return None

    entity_type, doctype = mapping
    name = idmap.get_target_name(entity_type, row.get("OWNER_ID"))
    return (doctype, name) if name else None


def _description_html(row: dict) -> str:
    """
    Cuerpo de la actividad como HTML.

    `DESCRIPTION_TYPE` vale 1 (texto plano), 2 (BBCode) o 3 (HTML). El
    HTML se deja tal cual; lo demás se escapa antes de convertir los
    saltos de línea, para que un `<` de un correo antiguo no rompa la
    vista del hilo.
    """
    description = row.get("DESCRIPTION") or ""
    description_type = as_int(row.get("DESCRIPTION_TYPE"), 1)

    if description_type == 3:
        return description

    if description_type == 2:
        description = bbcode_to_text(description)

    return frappe.utils.escape_html(description).replace("\n", "<br>")


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return list(value.values())
    return []
