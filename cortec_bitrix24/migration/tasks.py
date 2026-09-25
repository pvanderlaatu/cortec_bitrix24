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
Fase 9 — Módulo Tareas y proyectos → CRM Task.

Ojo con la ambigüedad del nombre: esto NO son los "to-do" del timeline
del CRM (`CRM_TODO`), que ya migra la fase 8 desde `crm.activity.list`.
Esto es el módulo aparte de Bitrix24 (`tasks.task.*`), con sus proyectos,
checklists, comentarios, co-ejecutores y seguimiento de tiempo.

Se migran **todas** las tareas del portal, no solo las ligadas al CRM.
Las que tienen vínculo (`ufCrmTask`) quedan colgando de su Lead, Deal,
Contacto o Empresa; las internas se crean sin referencia, que en
CRM Task es válido — el único campo obligatorio es `title`.

Qué se pierde y dónde acaba
---------------------------
CRM Task tiene ocho campos y una tarea de Bitrix24 tiene muchos más. Sin
inventar Custom Fields, lo que no tiene hueco propio se escribe como una
cabecera dentro de `description`, que es un Text Editor:

    proyecto, creador, co-ejecutores, observadores, tiempo estimado e
    invertido, el checklist como lista de casillas y los comentarios.

El resto (dependencias entre tareas, sprints, plantillas, historial de
cambios) no se migra. Una tarea archivada no necesita sus dependencias:
ya no se va a replanificar nada.

Peculiaridades de la API que condicionan el código
--------------------------------------------------
1. `tasks.task.list` FILTRA y ORDENA en MAYÚSCULAS (`ID`, `CREATED_DATE`)
   pero DEVUELVE en camelCase (`id`, `title`). Confundirlos deja el
   cursor clavado en la primera página para siempre.
2. No entiende el `start: -1` de la familia `crm.*`, así que aquí se
   pagina por ID pagando el conteo (`disable_total=False`).
3. `task.commentitem.getlist` está descontinuado desde tasks 25.700.0 y
   no funciona con la ficha nueva de tarea. Se intenta igual, y si el
   portal ya no lo expone se anota en el informe en vez de dejar creer
   que las tareas no tenían comentarios.
"""

import frappe

from cortec_bitrix24.migration import files, idmap
from cortec_bitrix24.migration.users import resolve_user
from cortec_bitrix24.utils import (
    as_int,
    bbcode_to_text,
    is_truthy,
    parse_date_str,
    parse_datetime_str,
    to_bitrix_datetime,
    truncate,
)

# Campos a pedir. La documentación insiste en declararlos siempre: el
# conjunto por defecto cambia entre versiones del portal.
SELECT_FIELDS = [
    "ID", "TITLE", "DESCRIPTION", "STATUS", "PRIORITY", "RESPONSIBLE_ID",
    "CREATED_BY", "CREATED_DATE", "CHANGED_DATE", "CLOSED_DATE", "DEADLINE",
    "START_DATE_PLAN", "END_DATE_PLAN", "GROUP_ID", "PARENT_ID",
    "ACCOMPLICES", "AUDITORS", "TIME_ESTIMATE", "TIME_SPENT_IN_LOGS",
    "UF_CRM_TASK", "UF_TASK_WEBDAV_FILES",
]

# STATUS de Bitrix24 → status de CRM Task.
#
# El 4 ("supuestamente completada": el ejecutor la cerró y falta que el
# creador la acepte) se mapea a Done. En un portal que se va a retirar
# nadie va a aceptar ya nada, así que dejarla "In Progress" mostraría
# como trabajo pendiente lo que está hecho.
STATUS_MAP = {
    1: "Todo",          # Nueva
    2: "Todo",          # Pendiente de ejecución
    3: "In Progress",   # En curso
    4: "Done",          # Supuestamente completada
    5: "Done",          # Completada
    6: "Backlog",       # Aplazada
    7: "Canceled",      # Rechazada
}

# PRIORITY de Bitrix24 → priority de CRM Task.
PRIORITY_MAP = {0: "Low", 1: "Medium", 2: "High"}

# Prefijos de UF_CRM_TASK → (tipo en el ID Map, doctype de Frappe).
# `CO_` debe comprobarse antes que `C_`, o "CO_7" se leería como el
# contacto 7 en vez de la empresa 7.
CRM_BINDINGS = (
    ("CO_", idmap.ENTITY_COMPANY, "CRM Organization"),
    ("L_", idmap.ENTITY_LEAD, "CRM Lead"),
    ("D_", idmap.ENTITY_DEAL, "CRM Deal"),
    ("C_", idmap.ENTITY_CONTACT, "Contact"),
)


def run(ctx) -> None:
    params = {"select": SELECT_FIELDS}
    if ctx.delta_since:
        params["filter"] = {">CHANGED_DATE": to_bitrix_datetime(ctx.delta_since)}

    if ctx.dry_run:
        ctx.note("tareas_en_bitrix", _count_tasks(ctx))

    grupos = _fetch_groups(ctx)
    resumen = {"con_vinculo_crm": 0, "internas": 0, "sin_destino": 0}

    lote: list[dict] = []

    for row in ctx.client.list_all(
        "tasks.task.list",
        params,
        start_after_id=ctx.cursor(),
        id_field="ID",
        row_id_field="id",
        disable_total=False,
    ):
        lote.append(row)

        if len(lote) >= ctx.batch_size:
            _procesar_lote(ctx, lote, grupos, resumen)
            lote = []

    if lote:
        _procesar_lote(ctx, lote, grupos, resumen)

    ctx.note("tareas_por_vinculo", resumen)
    ctx.checkpoint()


def _procesar_lote(ctx, lote: list[dict], grupos: dict, resumen: dict) -> None:
    """
    Migra un bloque de tareas, pidiendo antes checklists y comentarios de
    las 50 de una vez.

    Uno a uno serían tres peticiones por tarea; a dos por segundo, 10 000
    tareas son más de cuatro horas solo en ida y vuelta.
    """
    ids = [str(r.get("id")) for r in lote if r.get("id")]
    checklists = _fetch_checklists(ctx, ids)
    comentarios = _fetch_comments(ctx, ids)

    for row in lote:
        task_id = str(row.get("id") or "")
        if not task_id:
            continue
        try:
            _sync_task(
                ctx,
                row,
                grupos,
                checklists.get(task_id) or [],
                comentarios.get(task_id) or [],
                resumen,
            )
        except Exception as e:
            ctx.log(
                "error",
                f"No se pudo migrar la tarea {task_id}: {e}",
                bitrix_id=task_id,
            )
        ctx.set_cursor(int(task_id))

    ctx.checkpoint()


def _sync_task(ctx, row, grupos, checklist, comentarios, resumen) -> None:
    task_id = str(row.get("id"))

    binding = _resolve_crm_binding(ctx, row.get("ufCrmTask"))
    if binding:
        resumen["con_vinculo_crm"] += 1
    elif row.get("ufCrmTask"):
        # Tenía vínculo en Bitrix24, pero esa entidad no llegó a migrarse.
        resumen["sin_destino"] += 1
    else:
        resumen["internas"] += 1

    ref_doctype, ref_name = binding or (None, None)

    values = {
        "title": truncate(row.get("title") or f"Tarea B24 {task_id}", 140),
        "description": _build_description(ctx, row, grupos, checklist, comentarios),
        "status": STATUS_MAP.get(as_int(row.get("status")), "Todo"),
        "priority": PRIORITY_MAP.get(as_int(row.get("priority")), "Medium"),
        "assigned_to": resolve_user(ctx, row.get("responsibleId"), fallback=None),
        "start_date": parse_date_str(row.get("startDatePlan")),
        "due_date": parse_datetime_str(row.get("deadline") or row.get("endDatePlan")),
        "reference_doctype": ref_doctype,
        "reference_docname": ref_name,
    }

    name = idmap.upsert(
        ctx,
        idmap.ENTITY_TASK,
        task_id,
        "CRM Task",
        values,
        created_on=parse_datetime_str(row.get("createdDate")),
        modified_on=parse_datetime_str(row.get("changedDate")),
    )

    if name:
        files.attach(ctx, row.get("ufTaskWebdavFiles"), "CRM Task", name)


# ---------------------------------------------------------------------------
# Vínculo con el CRM
# ---------------------------------------------------------------------------


def _resolve_crm_binding(ctx, uf_crm_task) -> tuple[str, str] | None:
    """
    Primer vínculo de `ufCrmTask` que apunte a algo ya migrado.

    Bitrix24 lo entrega como lista de cadenas con prefijo: `L_12`
    (lead), `D_34` (negociación), `C_56` (contacto), `CO_78` (empresa).
    Una tarea puede tener varios; CRM Task solo admite una referencia,
    así que se toma el primero resoluble, en el orden de CRM_BINDINGS.
    """
    if not uf_crm_task:
        return None

    if isinstance(uf_crm_task, str):
        uf_crm_task = [uf_crm_task]

    for raw in uf_crm_task:
        texto = str(raw or "").strip().upper()
        if not texto:
            continue

        for prefijo, entity_type, doctype in CRM_BINDINGS:
            if not texto.startswith(prefijo):
                continue

            bitrix_id = texto[len(prefijo):]
            if not bitrix_id.isdigit():
                break

            name = idmap.get_target_name(entity_type, bitrix_id)
            if name:
                return doctype, name
            break

    return None


# ---------------------------------------------------------------------------
# Descripción enriquecida
# ---------------------------------------------------------------------------


def _build_description(ctx, row, grupos, checklist, comentarios) -> str:
    """
    Cuerpo de la CRM Task: la descripción original más lo que no tiene
    campo propio en Frappe.

    Se escribe como HTML porque `description` es un Text Editor. El
    orden pone primero la descripción real: la cabecera de metadatos es
    contexto, no el contenido.
    """
    partes = []

    descripcion = _description_html(row)
    if descripcion:
        partes.append(descripcion)

    metadatos = _metadata_html(ctx, row, grupos)
    if metadatos:
        partes.append(metadatos)

    if checklist:
        partes.append(_checklist_html(checklist))

    if comentarios:
        partes.append(_comments_html(ctx, comentarios))

    return "\n".join(partes)


def _description_html(row) -> str:
    """
    La descripción viene en BBCode salvo que la tarea esté marcada como
    HTML. Se escapa lo que no sea HTML antes de meter los <br>, para que
    un `<` de una tarea antigua no rompa la ficha.
    """
    descripcion = row.get("description") or ""
    if not descripcion:
        return ""

    if is_truthy(row.get("descriptionInBbcode")) or "[" in descripcion:
        texto = bbcode_to_text(descripcion)
        return frappe.utils.escape_html(texto).replace("\n", "<br>")

    return descripcion


def _metadata_html(ctx, row, grupos) -> str:
    lineas = []

    proyecto = grupos.get(str(row.get("groupId") or ""))
    if proyecto:
        lineas.append(f"<b>Proyecto:</b> {frappe.utils.escape_html(proyecto)}")

    creador = resolve_user(ctx, row.get("createdBy"), fallback=None)
    if creador:
        lineas.append(f"<b>Creada por:</b> {frappe.utils.escape_html(creador)}")

    for etiqueta, clave in (
        ("Co-ejecutores", "accomplices"),
        ("Observadores", "auditors"),
    ):
        usuarios = _resolve_user_list(ctx, row.get(clave))
        if usuarios:
            lineas.append(
                f"<b>{etiqueta}:</b> {frappe.utils.escape_html(', '.join(usuarios))}"
            )

    estimado = as_int(row.get("timeEstimate"))
    invertido = as_int(row.get("timeSpentInLogs"))
    if estimado:
        lineas.append(f"<b>Tiempo estimado:</b> {_horas(estimado)}")
    if invertido:
        lineas.append(f"<b>Tiempo invertido:</b> {_horas(invertido)}")

    if not lineas:
        return ""

    return (
        "<hr><p><small>Datos de Bitrix24 sin campo propio en Frappe:<br>"
        + "<br>".join(lineas)
        + "</small></p>"
    )


def _checklist_html(checklist) -> str:
    filas = []
    for item in checklist:
        titulo = (item.get("TITLE") or item.get("title") or "").strip()
        if not titulo:
            continue
        completo = is_truthy(item.get("IS_COMPLETE") or item.get("isComplete"))
        marca = "&#9745;" if completo else "&#9744;"
        filas.append(f"<li>{marca} {frappe.utils.escape_html(titulo)}</li>")

    if not filas:
        return ""

    return "<hr><p><b>Checklist</b></p><ul>" + "".join(filas) + "</ul>"


def _comments_html(ctx, comentarios) -> str:
    """
    Los comentarios van dentro de la descripción y no como FCRM Note
    aparte: una FCRM Note exige `reference_doctype`/`reference_docname`,
    y la mayoría de estas tareas son internas, sin nada del CRM a lo que
    colgarlas.
    """
    filas = []
    for comentario in comentarios:
        texto = bbcode_to_text(
            comentario.get("POST_MESSAGE") or comentario.get("postMessage")
        )
        if not texto:
            continue

        autor = (
            comentario.get("AUTHOR_NAME")
            or comentario.get("authorName")
            or resolve_user(ctx, comentario.get("AUTHOR_ID"), fallback=None)
            or "?"
        )
        fecha = parse_datetime_str(
            comentario.get("POST_DATE") or comentario.get("postDate")
        )

        filas.append(
            "<li><small><b>"
            + frappe.utils.escape_html(str(autor))
            + "</b>"
            + (f" &mdash; {fecha}" if fecha else "")
            + "</small><br>"
            + frappe.utils.escape_html(texto).replace("\n", "<br>")
            + "</li>"
        )

    if not filas:
        return ""

    return "<hr><p><b>Comentarios</b></p><ul>" + "".join(filas) + "</ul>"


# ---------------------------------------------------------------------------
# Lecturas auxiliares
# ---------------------------------------------------------------------------


def _count_tasks(ctx) -> int:
    try:
        raw = ctx.client.call_raw("tasks.task.list", {"select": ["ID"], "start": 0})
        return as_int(raw.get("total"))
    except Exception as e:
        ctx.log("warning", f"No se pudo contar las tareas: {e}")
        return 0


def _fetch_groups(ctx) -> dict[str, str]:
    """
    {GROUP_ID: nombre del proyecto}. Un fallo aquí no es grave: las
    tareas se migran igual, solo sin el nombre del proyecto.
    """
    grupos: dict[str, str] = {}

    try:
        rows = ctx.client.call("sonet_group.get", {"ORDER": {"ID": "ASC"}}) or []
    except Exception as e:
        ctx.log(
            "warning",
            f"No se pudieron leer los proyectos ({e}). Las tareas se migran "
            "sin el nombre del proyecto.",
        )
        return grupos

    for row in rows if isinstance(rows, list) else []:
        gid = str(row.get("ID") or row.get("id") or "")
        nombre = (row.get("NAME") or row.get("name") or "").strip()
        if gid and nombre:
            grupos[gid] = nombre

    return grupos


def _fetch_checklists(ctx, task_ids: list[str]) -> dict[str, list]:
    if not task_ids:
        return {}

    commands = {
        tid: ("task.checklistitem.getlist", {"TASKID": tid}) for tid in task_ids
    }

    try:
        return {k: _as_list(v) for k, v in ctx.client.batch(commands).items()}
    except Exception as e:
        ctx.log("warning", f"No se pudieron leer checklists de un bloque: {e}")
        return {}


def _fetch_comments(ctx, task_ids: list[str]) -> dict[str, list]:
    """
    Comentarios de un bloque de tareas.

    `task.commentitem.getlist` está descontinuado desde tasks 25.700.0 y
    no funciona con la ficha nueva. Si el portal ya no lo expone, se
    anota UNA vez en el informe y se deja de intentar: es importante que
    quede constancia, porque si no, unas tareas sin comentarios parecen
    tareas que nunca los tuvieron.
    """
    if not task_ids or ctx.phase_state.get("_comentarios_no_disponibles"):
        return {}

    commands = {
        tid: ("task.commentitem.getlist", {"TASKID": tid}) for tid in task_ids
    }

    try:
        resultado = ctx.client.batch(commands)
    except Exception as e:
        ctx.phase_state["_comentarios_no_disponibles"] = True
        ctx.note("comentarios_de_tareas", f"no disponibles: {e}")
        ctx.log(
            "warning",
            "El portal no expone task.commentitem.getlist (descontinuado desde "
            f"tasks 25.700.0): {e}. Las tareas se migran SIN sus comentarios. "
            "Para recuperarlos habría que leer el chat de cada tarea con "
            "im.dialog.messages.get.",
        )
        return {}

    if resultado:
        ctx.phase_state["_comentarios_vistos"] = True
    elif not ctx.phase_state.get("_comentarios_vistos"):
        ctx.note(
            "comentarios_de_tareas",
            "el método respondió pero no devolvió comentarios; verifique si el "
            "portal usa ya la ficha nueva de tarea, donde está descontinuado",
        )

    return {k: _as_list(v) for k, v in resultado.items()}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve_user_list(ctx, ids) -> list[str]:
    if not ids:
        return []
    if not isinstance(ids, (list, tuple)):
        ids = [ids]

    usuarios = []
    for uid in ids:
        usuario = resolve_user(ctx, uid, fallback=None)
        if usuario and usuario not in usuarios:
            usuarios.append(usuario)
    return usuarios


def _horas(segundos: int) -> str:
    horas, resto = divmod(int(segundos), 3600)
    minutos = resto // 60
    if horas and minutos:
        return f"{horas} h {minutos} min"
    if horas:
        return f"{horas} h"
    return f"{minutos} min"


def _as_list(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        return [v for v in value.values() if isinstance(v, dict)]
    return []
