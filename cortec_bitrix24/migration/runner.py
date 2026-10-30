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
cortec_bitrix24.migration.runner
=================================
Orquesta la corrida: contexto compartido, orden de fases, checkpoints y
el silenciado de los avisos de cortec_helpdesk.

El orden de las fases no es negociable — cada una necesita las
correspondencias que dejó la anterior:

    diccionarios → usuarios → campos UF → empresas → contactos →
    leads → negociaciones → actividades → tareas → WhatsApp →
    adjuntos → formularios

Cada fase guarda su cursor en el Bitrix24 Migration Run, así que una
corrida interrumpida (worker reiniciado, portal caído) se reanuda donde
quedó en vez de volver a empezar.
"""

import importlib
import importlib.util
import json
import time
from contextlib import contextmanager

import frappe
from frappe.utils import now_datetime

from cortec_bitrix24.bitrix.client import get_client
from cortec_bitrix24.bitrix.errors import B24AuthError, B24FatalError
from cortec_bitrix24.cortec_bitrix24.doctype.bitrix24_migration_settings.bitrix24_migration_settings import (
    get_settings,
)

# Cada cuántos documentos se hace commit. Suficientemente alto para que
# el coste de la transacción no domine, y suficientemente bajo para que
# una caída no tire por la borda media hora de trabajo.
COMMIT_EVERY = 200

# Además, como mucho cada tantos segundos. Una fase que recorre el portal
# sin crear nada (las actividades en seco, por ejemplo) nunca llega a
# COMMIT_EVERY, y sin esto el formulario de la corrida parece congelado
# hasta que la fase termina.
CHECKPOINT_SECONDS = 30


class MigrationContext:
    """
    Estado compartido por todas las fases.

    Lleva el cliente REST, los contadores, el log y los cursores. Se
    pasa como primer argumento a cada fase en vez de usar variables de
    módulo, para que los tests puedan construir uno de mentira.
    """

    def __init__(self, run_name: str):
        self.run_name = run_name
        self.run = frappe.get_doc("Bitrix24 Migration Run", run_name)
        self.settings = get_settings()

        self.dry_run = self.run.mode == "Dry Run"
        self.preserve_timestamps = bool(self.settings.preserve_timestamps)
        self.country_code = self.settings.country_code or "506"
        self.default_owner = self.settings.default_owner
        self.batch_size = min(int(self.settings.batch_size or 50), 50)
        self.delta_since = self.run.delta_since

        self.client = get_client(logger=self._client_logger)
        self.client.on_request = self._on_request

        self.phase = ""
        self.counters = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
        self.phase_state = json.loads(self.run.phase_state or "{}")
        self.summary = json.loads(self.run.summary or "{}")

        self._since_commit = 0
        self._last_checkpoint = time.monotonic()

    # -----------------------------------------------------------------
    # Contadores y checkpoints
    # -----------------------------------------------------------------

    def _on_request(self) -> None:
        """Tras cada petición al portal: guarda el avance si toca por tiempo."""
        if time.monotonic() - self._last_checkpoint >= CHECKPOINT_SECONDS:
            self.checkpoint()

    def count(self, key: str, n: int = 1) -> None:
        self.counters[key] = self.counters.get(key, 0) + n
        self._since_commit += n
        if self._since_commit >= COMMIT_EVERY:
            self.checkpoint()

    def checkpoint(self) -> None:
        """Persiste contadores y cursores, y cierra la transacción."""
        self._since_commit = 0
        self._last_checkpoint = time.monotonic()
        self._flush_run()
        # También en seco: las fases no escriben nada en el CRM, pero el
        # avance y el log de la corrida solo se ven desde fuera si se
        # confirman. Sin esto, una corrida en seco atascada muestra
        # «Running» sin fase ni incidencias, y al detenerla se pierde
        # todo lo que había anotado.
        frappe.db.commit()

    def _flush_run(self) -> None:
        frappe.db.set_value(
            "Bitrix24 Migration Run",
            self.run_name,
            {
                "current_phase": self.phase,
                "created_count": self.counters["created"],
                "updated_count": self.counters["updated"],
                "skipped_count": self.counters["skipped"],
                "error_count": self.counters["errors"],
                "request_count": self.client.request_count,
                "throttled_seconds": round(self.client.throttled_seconds, 1),
                "phase_state": json.dumps(self.phase_state, ensure_ascii=False),
                "summary": json.dumps(
                    self.summary, ensure_ascii=False, indent=1, default=str
                ),
            },
            update_modified=False,
        )

    # -----------------------------------------------------------------
    # Cursores de fase
    # -----------------------------------------------------------------

    def cursor(self, key: str = "last_id") -> int:
        return int((self.phase_state.get(self.phase) or {}).get(key) or 0)

    def set_cursor(self, value, key: str = "last_id") -> None:
        self.phase_state.setdefault(self.phase, {})[key] = value

    def is_phase_done(self, phase: str) -> bool:
        return bool((self.phase_state.get(phase) or {}).get("done"))

    def mark_phase_done(self) -> None:
        self.phase_state.setdefault(self.phase, {})["done"] = True
        self.phase_state[self.phase]["finished_on"] = str(now_datetime())

    # -----------------------------------------------------------------
    # Log
    # -----------------------------------------------------------------

    def log(
        self,
        level: str,
        message: str,
        bitrix_id=None,
        target_doctype: str | None = None,
        target_name: str | None = None,
    ) -> None:
        """
        Anota una incidencia en Bitrix24 Migration Log.

        Los mensajes Info se descartan salvo que sean hitos de fase
        (los emite el propio runner): una corrida grande generaría
        cientos de miles de filas inútiles.
        """
        if level == "error":
            self.counters["errors"] = self.counters.get("errors", 0) + 1

        try:
            frappe.get_doc(
                {
                    "doctype": "Bitrix24 Migration Log",
                    "run": self.run_name,
                    "level": level.capitalize(),
                    "phase": self.phase,
                    "bitrix_id": str(bitrix_id) if bitrix_id is not None else None,
                    "target_doctype": target_doctype,
                    "target_name": target_name,
                    "message": message,
                }
            ).insert(ignore_permissions=True)
        except Exception:
            # Que falle el log nunca debe tumbar la migración.
            frappe.log_error(
                title="CORTEC Bitrix24: no se pudo escribir el log de migración",
                message=f"{self.phase}: {message}",
            )

    def _client_logger(self, level: str, message: str) -> None:
        """Puente entre los avisos del cliente REST y el log de la corrida."""
        self.log(level, message)

    def note(self, key: str, value) -> None:
        """Anota un dato en el informe de la corrida (`summary`)."""
        self.summary.setdefault(self.phase, {})[key] = value

    def add_to_note_list(self, key: str, value) -> None:
        bucket = self.summary.setdefault(self.phase, {}).setdefault(key, [])
        if value not in bucket:
            bucket.append(value)


# ---------------------------------------------------------------------------
# Fases
# ---------------------------------------------------------------------------


def get_phases() -> list[tuple[str, str, str, str]]:
    """
    (clave, campo del Settings que la habilita, etiqueta, módulo).

    El módulo se importa tarde, dentro de run_migration(), para que un
    error de sintaxis en una fase no impida ni abrir el doctype de
    Settings.
    """
    return [
        ("dicts", "migrate_dicts", "Diccionarios", "dicts"),
        ("users", "migrate_users", "Usuarios", "users"),
        ("userfields", "migrate_userfields", "Campos personalizados", "userfields"),
        ("companies", "migrate_companies", "Empresas", "companies"),
        ("contacts", "migrate_contacts", "Contactos", "contacts"),
        ("leads", "migrate_leads", "Leads", "leads"),
        ("deals", "migrate_deals", "Negociaciones", "deals"),
        ("activities", "migrate_activities", "Actividades y timeline", "activities"),
        ("tasks", "migrate_tasks", "Tareas y proyectos", "tasks"),
        ("openlines", "migrate_openlines", "WhatsApp (Open Channels)", "openlines"),
        ("files", "migrate_files", "Adjuntos", "files"),
        ("webforms", "migrate_webforms", "Formularios web", "webforms"),
    ]


def run_migration(run_name: str) -> None:
    """
    Punto de entrada del job en cola. Ejecuta las fases habilitadas y
    deja el Bitrix24 Migration Run cerrado pase lo que pase.
    """
    started = time.monotonic()
    ctx = MigrationContext(run_name)

    frappe.db.set_value(
        "Bitrix24 Migration Run",
        run_name,
        {"status": "Running", "started_on": now_datetime()},
        update_modified=False,
    )
    frappe.db.commit()

    status = "Completed"

    try:
        with silence_cortec_alerts(ctx), migration_flags(), quiet_notifications(ctx):
            for key, flag, label, module_name in get_phases():
                if not ctx.settings.get(flag):
                    continue

                if ctx.is_phase_done(key):
                    ctx.log("info", f"Fase '{label}' ya completada; se omite.")
                    continue

                ctx.phase = key
                ctx.log("info", f"Inicio de la fase '{label}'.")
                ctx.checkpoint()

                module = importlib.import_module(
                    f"cortec_bitrix24.migration.{module_name}"
                )
                try:
                    module.run(ctx)
                except B24AuthError as e:
                    # Un permiso del webhook que solo usa esta fase no
                    # debe tumbar la corrida entera. El CRM sí.
                    if key not in OPTIONAL_PHASES:
                        raise
                    ctx.log("error", f"Fase '{label}' omitida: {e}")
                    ctx.summary.setdefault("fases_omitidas", {})[key] = str(e)
                    ctx.checkpoint()
                    continue

                ctx.mark_phase_done()
                ctx.checkpoint()
                ctx.log("info", f"Fin de la fase '{label}'.")

    except B24FatalError as e:
        status = "Failed"
        ctx.log("error", f"Corrida abortada: {e}")
        frappe.log_error(
            title="CORTEC Bitrix24: migración abortada", message=str(e)
        )
    except Exception as e:
        status = "Failed"
        ctx.log("error", f"Corrida abortada por un error inesperado: {e}")
        frappe.log_error(
            title="CORTEC Bitrix24: migración abortada",
            message=frappe.get_traceback(),
        )
    finally:
        ctx.phase = ""
        ctx.summary["duracion_segundos"] = round(time.monotonic() - started, 1)
        ctx._flush_run()
        frappe.db.set_value(
            "Bitrix24 Migration Run",
            run_name,
            {"status": status, "finished_on": now_datetime(), "current_phase": ""},
            update_modified=False,
        )
        frappe.db.set_value(
            "Bitrix24 Migration Settings",
            "Bitrix24 Migration Settings",
            {"last_run": run_name},
            update_modified=False,
        )
        if status == "Completed" and not ctx.dry_run:
            # Punto de partida de la siguiente pasada delta.
            frappe.db.set_value(
                "Bitrix24 Migration Settings",
                "Bitrix24 Migration Settings",
                {"last_completed_run_end": now_datetime()},
                update_modified=False,
            )
        frappe.db.commit()


# ---------------------------------------------------------------------------
# Convivencia con el resto del sitio
# ---------------------------------------------------------------------------


@contextmanager
def migration_flags():
    """
    Marca el hilo como "estoy migrando".

    `in_bitrix24_migration` la consultan los avisos de cortec_helpdesk
    (1.0.14 en adelante) para no avisar por documentos históricos ni
    crear Leads por cada WhatsApp antiguo. silence_cortec_alerts() queda
    como respaldo para versiones anteriores.

    `in_import` solo frena las alertas del doctype Notification
    disparadas por evento. Las notificaciones de asignación, las reglas
    de asignación y las plantillas de WhatsApp NO lo consultan: de eso
    se encarga quiet_notifications().
    """
    previous_migration = frappe.flags.get("in_bitrix24_migration")
    previous_import = frappe.flags.get("in_import")

    frappe.flags.in_bitrix24_migration = True
    frappe.flags.in_import = True

    try:
        yield
    finally:
        frappe.flags.in_bitrix24_migration = previous_migration
        frappe.flags.in_import = previous_import


# Fases que dependen de un permiso del webhook distinto del CRM. Si el
# portal la rechaza por permisos, se omite y la corrida sigue.
OPTIONAL_PHASES = {"users", "tasks", "openlines", "files", "webforms"}

# Campos de CORTEC Helpdesk Settings que se apagan durante la corrida.
CORTEC_ALERT_FIELDS = ("telegram_enabled", "browser_alerts_enabled", "raven_enabled")


@contextmanager
def silence_cortec_alerts(ctx):
    """
    Apaga temporalmente los avisos de cortec_helpdesk.

    Sin esto, migrar el histórico dispara `telegram.on_communication` y
    `whatsapp.route_unclaimed_message` por cada Communication y cada
    WhatsApp Message insertados: miles de mensajes de Telegram a los
    agentes por conversaciones de hace años, y un CRM Lead espurio por
    cada mensaje de WhatsApp sin vínculo abierto.

    Los valores se restauran en el `finally`, también si la corrida
    aborta. Si el proceso muere de golpe (kill -9 del worker) quedarían
    apagados: el informe de la corrida lo deja anotado para que se
    revise.
    """
    if ctx.dry_run or not ctx.settings.silence_cortec_alerts:
        yield
        return

    if not frappe.db.exists("DocType", "CORTEC Helpdesk Settings"):
        yield
        return

    meta = frappe.get_meta("CORTEC Helpdesk Settings")
    previous = {
        field: frappe.db.get_single_value("CORTEC Helpdesk Settings", field)
        for field in CORTEC_ALERT_FIELDS
        # raven_enabled solo existe desde cortec_helpdesk 1.0.11.
        if meta.has_field(field)
    }

    for field in previous:
        frappe.db.set_single_value("CORTEC Helpdesk Settings", field, 0)
    frappe.db.commit()

    ctx.log(
        "info",
        "Avisos de CORTEC Helpdesk desactivados durante la corrida "
        f"(valores previos: {previous}). Se restauran al terminar.",
    )

    try:
        yield
    finally:
        for field, value in previous.items():
            frappe.db.set_single_value("CORTEC Helpdesk Settings", field, value)
        frappe.db.commit()
        frappe.clear_cache(doctype="CORTEC Helpdesk Settings")
        ctx.log("info", f"Avisos de CORTEC Helpdesk restaurados: {previous}.")


# ---------------------------------------------------------------------------
# Ni un aviso a agentes ni a clientes
# ---------------------------------------------------------------------------

# Funciones que notifican o actúan al crear un documento y que NO
# consultan `in_import`: (módulo, atributo, qué evita, app que lo
# aporta, opcional). Un atributo "Clase.metodo" se sustituye en la
# clase. «Opcional» = el módulo no existe en todas las versiones de la
# app; si no está, no hay nada que silenciar.
#
#   notify_assignment    — CRM Lead, Deal y Task asignan a su responsable
#                          en after_insert; cada asignación crea una
#                          Notification Log y un correo al agente.
#   notify_assigned_user — la misma asignación, en la campana de /crm.
#   add_assign_comment   — «X asignó a Y» en el timeline, con la fecha
#                          de la migración.
#   apply                — reglas de asignación (round-robin) sobre el
#                          histórico.
#   notify_agent         — aviso en /crm por cada WhatsApp entrante.
#   _enabled             — automatizaciones de Frappe CRM, que reaccionan
#                          a correos y WhatsApp nuevos (solo se apagan
#                          solas en install/migrate).
#   run_server_script_for_doc_event — plantillas de WhatsApp que
#                          frappe_whatsapp envía al CLIENTE en eventos de
#                          documento (p. ej. «al crear un Lead»).
QUIET_TARGETS = (
    ("frappe.desk.form.assign_to", "notify_assignment", "correo de asignación", "frappe", False),
    ("crm.api.todo", "notify_assigned_user", "aviso de asignación en /crm", "crm", False),
    (
        "frappe.desk.doctype.todo.todo",
        "ToDo.add_assign_comment",
        "comentario de asignación",
        "frappe",
        False,
    ),
    (
        "frappe.automation.doctype.assignment_rule.assignment_rule",
        "apply",
        "reglas de asignación",
        "frappe",
        False,
    ),
    ("crm.api.whatsapp", "notify_agent", "aviso de WhatsApp en /crm", "crm", False),
    ("crm.automation.events", "_enabled", "automatizaciones de Frappe CRM", "crm", True),
    (
        "frappe_whatsapp.utils",
        "run_server_script_for_doc_event",
        "plantillas automáticas de WhatsApp",
        "frappe_whatsapp",
        False,
    ),
)

# Doctypes que escribe la migración: los que pueden disparar una alerta
# programada («Days Before/After») sobre un documento histórico.
MIGRATED_DOCTYPES = (
    "CRM Lead",
    "CRM Deal",
    "CRM Task",
    "CRM Organization",
    "Contact",
    "Communication",
    "WhatsApp Message",
    "FCRM Note",
)


def _quiet(*args, **kwargs):
    # None también sirve de «no» para los interruptores (_enabled).
    return None


@contextmanager
def quiet_notifications(ctx):
    """
    Silencia, SOLO en el proceso de este worker, todo lo que notificaría
    a un agente o a un cliente por un documento migrado.

    Las asignaciones se crean igual (ToDo, `_assign`, DocShare): /crm
    muestra a cada agente lo suyo; lo que se omite es el aviso. El resto
    del sitio —web, otros workers— sigue notificando con normalidad.

    Si alguno de los atributos ya no existe (una versión nueva de
    Frappe, CRM o frappe_whatsapp lo renombró), la corrida real se
    aborta antes de escribir nada: un silenciado incompleto significa
    miles de correos a agentes o de WhatsApp a clientes.
    """
    installed = set(frappe.get_installed_apps())
    targets, missing = [], []

    for module_name, attribute, label, app, optional in QUIET_TARGETS:
        if app not in installed:
            continue
        if optional and not _module_exists(module_name):
            continue
        owner, name = _resolve_target(module_name, attribute)
        if owner is None:
            missing.append(f"{module_name}.{attribute} ({label})")
        else:
            targets.append((owner, name, label))

    _report_scheduled_alerts(ctx)

    if missing:
        detail = "; ".join(missing)
        if not ctx.dry_run:
            raise B24FatalError(
                "QUIET_NOTIFICATIONS",
                "No se pueden silenciar las notificaciones: falta " + detail + ". "
                "Revise quiet_notifications() en cortec_bitrix24/migration/runner.py.",
            )
        _note_before(ctx, "notificaciones_sin_silenciar", missing)
        ctx.log("warning", f"La corrida real se abortaría: falta {detail}.")

    if ctx.dry_run:
        # El dry-run no escribe nada: solo comprueba y avisa.
        yield
        return

    originals = [(owner, name, getattr(owner, name)) for owner, name, _label in targets]
    for owner, name, _label in targets:
        setattr(owner, name, _quiet)
    ctx.log(
        "info",
        "Notificaciones silenciadas durante la corrida: "
        + ", ".join(label for _owner, _name, label in targets)
        + ".",
    )

    try:
        yield
    finally:
        for owner, name, original in originals:
            setattr(owner, name, original)
        ctx.log("info", "Notificaciones restauradas.")


def _module_exists(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except ModuleNotFoundError:
        # Tampoco existe el paquete padre (p. ej. crm.automation).
        return False


def _resolve_target(module_name: str, attribute: str):
    """(objeto dueño, nombre del atributo), o (None, None) si no existe."""
    try:
        owner = importlib.import_module(module_name)
    except ImportError:
        return None, None

    *path, name = attribute.split(".")
    for part in path:
        owner = getattr(owner, part, None)
        if owner is None:
            return None, None

    return (owner, name) if callable(getattr(owner, name, None)) else (None, None)


def _report_scheduled_alerts(ctx) -> None:
    """
    Alertas programadas activas sobre doctypes migrados.

    Las ejecuta el scheduler, fuera de este proceso, así que no se
    pueden silenciar desde aquí: una alerta «1 día antes del
    vencimiento» avisaría por cada tarea migrada que venza mañana. Se
    listan para desactivarlas a mano antes de la corrida real.
    """
    found = []

    if frappe.db.exists("DocType", "Notification"):
        for row in frappe.get_all(
            "Notification",
            filters={
                "enabled": 1,
                "event": ["in", ("Days Before", "Days After")],
                "document_type": ["in", MIGRATED_DOCTYPES],
            },
            fields=["name", "document_type", "event"],
        ):
            found.append(f"Notification «{row.name}» ({row.document_type}, {row.event})")

    if frappe.db.exists("DocType", "WhatsApp Notification"):
        for row in frappe.get_all("WhatsApp Notification", fields=["*"]):
            if row.get("disabled"):
                continue
            if row.get("reference_doctype") not in MIGRATED_DOCTYPES:
                continue
            if (row.get("notification_type") or "") == "DocType Event":
                # Estas sí las silencia quiet_notifications().
                continue
            found.append(f"WhatsApp Notification «{row.name}» ({row.get('reference_doctype')})")

    if found:
        _note_before(ctx, "alertas_programadas_a_desactivar", found)
        ctx.log(
            "warning",
            "Alertas programadas activas sobre doctypes migrados (desactívelas antes "
            "de la corrida real y reactívelas al día siguiente): " + "; ".join(found),
        )


def _note_before(ctx, key: str, value) -> None:
    """Anota en el informe, fuera de cualquier fase."""
    ctx.summary.setdefault("antes_de_empezar", {})[key] = value
