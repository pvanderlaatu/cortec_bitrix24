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
cortec_bitrix24.api
====================
Puntos de entrada de la migración: lanzar una corrida, seguirla, cotejar
los conteos contra el portal, exportar el mapeo de formularios e
importar los consentimientos de los formularios.

Todos exigen System Manager. La migración escribe en el CRM entero y
puede tardar horas; no es una operación que deba poder disparar un
agente desde la interfaz.
"""

import json

import frappe
from frappe import _
from frappe.utils import cint

# Los jobs de la cola `long` no tienen el límite de tiempo de `default`.
# Una migración de decenas de miles de registros contra un portal que
# admite dos peticiones por segundo dura horas.
QUEUE = "long"
JOB_TIMEOUT = 12 * 60 * 60


@frappe.whitelist()
def start_migration(dry_run: int = 1, delta_since: str | None = None) -> dict:
    """
    Crea un Bitrix24 Migration Run y lo encola.

    `delta_since` limita la corrida a lo modificado después de esa
    fecha; vacío usa el fin de la última corrida completa, que es lo
    que se quiere para la pasada del día del corte.
    """
    frappe.only_for("System Manager")

    _assert_no_run_in_progress()

    settings = frappe.get_single("Bitrix24 Migration Settings")

    if not settings.get_password("webhook_url", raise_exception=False):
        frappe.throw(_("Configure primero la URL del webhook de Bitrix24."))

    if not settings.default_owner:
        frappe.throw(
            _(
                "Configure el usuario de respaldo: es quien recibe los Leads y "
                "Deals cuyo responsable de Bitrix24 no existe en Frappe."
            )
        )

    dry_run = cint(dry_run)

    if not dry_run and not _has_completed_dry_run():
        frappe.throw(
            _(
                "No hay ninguna corrida en seco completada. Ejecute primero un "
                "dry-run y revise su informe: es la única forma de ver qué "
                "usuarios, estados y campos quedan sin mapear antes de escribir "
                "en el CRM."
            )
        )

    run = frappe.get_doc(
        {
            "doctype": "Bitrix24 Migration Run",
            "mode": "Dry Run" if dry_run else "Real",
            "status": "Queued",
            "delta_since": delta_since or settings.last_completed_run_end,
        }
    ).insert(ignore_permissions=True)

    # La corrida en seco no debe heredar el dry_run del Settings ni al
    # revés: el modo lo fija el Run, y el Settings solo da el valor
    # inicial del botón.
    frappe.db.set_single_value("Bitrix24 Migration Settings", "dry_run", dry_run)

    frappe.enqueue(
        "cortec_bitrix24.migration.runner.run_migration",
        queue=QUEUE,
        timeout=JOB_TIMEOUT,
        run_name=run.name,
        job_name=f"bitrix24-migration-{run.name}",
    )

    return {"run": run.name, "mode": run.mode, "delta_since": run.delta_since}


@frappe.whitelist()
def migration_status(run: str | None = None) -> dict:
    """Estado de una corrida (por defecto, la última)."""
    frappe.only_for("System Manager")

    if not run:
        run = frappe.db.get_single_value("Bitrix24 Migration Settings", "last_run")

    if not run:
        return {"run": None}

    doc = frappe.db.get_value(
        "Bitrix24 Migration Run",
        run,
        [
            "name", "mode", "status", "current_phase", "started_on", "finished_on",
            "created_count", "updated_count", "skipped_count", "error_count",
            "request_count", "summary",
        ],
        as_dict=True,
    )

    if doc and doc.summary:
        try:
            doc.summary = json.loads(doc.summary)
        except ValueError:
            pass

    return doc or {"run": None}


@frappe.whitelist()
def verify_counts() -> dict:
    """
    Coteja los conteos del portal con lo que hay en Frappe.

    Es la comprobación que cierra la migración: si las cifras no cuadran,
    el detalle de por qué está en Bitrix24 Migration Log.
    """
    frappe.only_for("System Manager")

    from cortec_bitrix24.bitrix.client import get_client
    from cortec_bitrix24.bitrix.errors import B24Error
    from cortec_bitrix24.migration import idmap

    client = get_client()

    comparacion = []
    for label, method, entity_type, doctype in (
        ("Empresas", "crm.company.list", idmap.ENTITY_COMPANY, "CRM Organization"),
        ("Contactos", "crm.contact.list", idmap.ENTITY_CONTACT, "Contact"),
        ("Leads", "crm.lead.list", idmap.ENTITY_LEAD, "CRM Lead"),
        ("Negociaciones", "crm.deal.list", idmap.ENTITY_DEAL, "CRM Deal"),
    ):
        try:
            en_bitrix = client.count(method)
        except B24Error as e:
            en_bitrix = f"error: {e.code}"

        migrados = frappe.db.count("Bitrix24 ID Map", {"entity_type": entity_type})
        en_frappe = frappe.db.count(doctype)

        comparacion.append(
            {
                "entidad": label,
                "en_bitrix24": en_bitrix,
                "migrados": migrados,
                "total_en_frappe": en_frappe,
                "cuadra": en_bitrix == migrados,
            }
        )

    return {
        "comparacion": comparacion,
        "errores_ultima_corrida": frappe.db.count(
            "Bitrix24 Migration Log", {"level": "Error"}
        ),
    }


@frappe.whitelist()
def export_webform_mapping() -> dict:
    """
    Mapeo de todos los formularios web, listo para configurar el Worker
    `leads-intake` que sustituye al formulario de Bitrix24.
    """
    frappe.only_for("System Manager")

    forms = frappe.get_all(
        "Bitrix24 Web Form",
        fields=["bitrix_form_id", "form_name", "is_active", "mapping_json"],
        order_by="form_name asc",
    )

    salida = []
    for form in forms:
        try:
            mapping = json.loads(form.mapping_json) if form.mapping_json else {}
        except ValueError:
            mapping = {}

        salida.append(
            {
                "bitrix_form_id": form.bitrix_form_id,
                "nombre": form.form_name,
                "activo": bool(form.is_active),
                "campos": mapping,
                "sin_destino": [
                    code
                    for code, cfg in mapping.items()
                    if isinstance(cfg, dict) and not cfg.get("destino_frappe")
                ],
            }
        )

    return {"formularios": salida, "endpoint_destino": "cortec_helpdesk.api"}


@frappe.whitelist()
def import_webform_consents(
    file_url: str, agreement: str = "promociones", dry_run: int = 1, subscribe: int = 0
) -> dict:
    """
    Importa un CSV de la pantalla «Consentimiento del usuario» de
    Bitrix24 como CORTEC Consent Record (ver migration/consents.py).

    `agreement` — código del CORTEC User Agreement de destino:
                  «promociones» o «politica-privacidad».
    `subscribe` — 1 para suscribir a la lista promocional a quien se le
                  importe un consentimiento publicitario. Por defecto no:
                  suscribir de golpe cientos de correos es una decisión
                  aparte.

    Se ejecuta después de la corrida real: necesita el ID Map para
    llegar de la actividad de Bitrix24 al Lead o Contacto migrado.
    """
    frappe.only_for("System Manager")

    if "cortec_helpdesk" not in frappe.get_installed_apps():
        frappe.throw(_("cortec_helpdesk no está instalado: no hay dónde registrar los consentimientos."))

    if not frappe.db.exists("Bitrix24 ID Map", {"target_doctype": ["in", ("CRM Lead", "Contact")]}):
        frappe.throw(
            _("No hay Leads ni Contactos migrados todavía. Importe los consentimientos después de la corrida real.")
        )

    from cortec_bitrix24.bitrix.client import get_client
    from cortec_bitrix24.migration.consents import import_consents

    content = frappe.get_doc("File", {"file_url": file_url}).get_content()

    return import_consents(
        get_client(),
        content,
        agreement,
        dry_run=bool(cint(dry_run)),
        subscribe=bool(cint(subscribe)),
    )


@frappe.whitelist()
def abort_migration(run: str | None = None) -> dict:
    """
    Marca una corrida como abortada.

    No mata el job: solo deja el Run en un estado coherente para que se
    pueda lanzar otro. Si el worker sigue vivo, pare primero el job
    desde la cola (`bench --site <sitio> doctor` / RQ dashboard) y
    después llame a esto.
    """
    frappe.only_for("System Manager")

    if not run:
        run = frappe.db.get_single_value("Bitrix24 Migration Settings", "last_run")

    if not run:
        frappe.throw(_("No hay ninguna corrida que abortar."))

    frappe.db.set_value("Bitrix24 Migration Run", run, "status", "Aborted")
    return {"run": run, "status": "Aborted"}


# ---------------------------------------------------------------------------
# Internos
# ---------------------------------------------------------------------------


def _assert_no_run_in_progress() -> None:
    """
    Dos corridas a la vez se pisarían los cursores y podrían duplicar
    documentos pese al ID Map, porque ambas leerían el mismo estado
    "no migrado" antes de que ninguna escribiera.
    """
    en_curso = frappe.db.get_value(
        "Bitrix24 Migration Run", {"status": ["in", ("Queued", "Running")]}, "name"
    )
    if en_curso:
        frappe.throw(
            _(
                "La corrida {0} sigue en curso. Espere a que termine, o márquela "
                "como abortada desde su propio formulario."
            ).format(en_curso)
        )


def _has_completed_dry_run() -> bool:
    return bool(
        frappe.db.exists(
            "Bitrix24 Migration Run", {"mode": "Dry Run", "status": "Completed"}
        )
    )
