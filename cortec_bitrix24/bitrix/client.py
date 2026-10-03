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
cortec_bitrix24.bitrix.client
==============================
Cliente REST de Bitrix24 sobre un webhook entrante.

Concentra toda la fricción con el portal, de modo que los módulos de
mapeo (migration/*.py) solo vean diccionarios de Python:

call()      — una llamada, con control de ritmo y reintentos.
batch()     — hasta 50 sub-llamadas independientes en un solo request.
             Es la diferencia entre 50 viajes y uno; a 2 req/s eso es
             25 segundos contra medio segundo.
list_all()  — itera una colección completa con paginación por ID.

Sobre la paginación
-------------------
`crm.*.list` devuelve como máximo 50 registros y acepta `start` como
offset. Ese offset obliga al portal a descartar N filas en cada página,
y más allá de unos 10 000 registros la consulta se degrada hasta dar
timeout. Bitrix24 documenta un modo rápido para recorrer colecciones
grandes:

    order:  {"ID": "ASC"}
    filter: {">ID": <último ID recibido>}
    start:  -1            # desactiva el conteo de total, que es lo caro

Es el que usa list_all(). Como efecto secundario, `total` no viene en la
respuesta: para contar se usa count(), que sí paga el conteo una vez.

Sobre el ritmo
--------------
El portal admite del orden de 2 peticiones por segundo y responde
QUERY_LIMIT_EXCEEDED al pasarse. El cliente espera `throttle` segundos
entre peticiones (medido con un reloj monotónico, no con sleep ciego) y
aplica backoff exponencial ante los códigos recuperables.
"""

import time
from typing import Any, Callable, Iterator
from urllib.parse import quote

import frappe
import requests

from cortec_bitrix24.bitrix.errors import (
    AUTH_ERROR_CODES,
    RETRYABLE_ERROR_CODES,
    B24AuthError,
    B24Error,
    B24FatalError,
)

# Tope duro del método `batch` de Bitrix24.
MAX_BATCH_COMMANDS = 50

# Tope duro de registros por página en los métodos *.list.
PAGE_SIZE = 50


class B24Client:
    """
    Cliente contra un webhook entrante de Bitrix24.

    La URL del webhook incluye el token, así que **nunca** se registra
    en logs ni en mensajes de error: _safe_url() la recorta antes.
    """

    def __init__(
        self,
        webhook_url: str,
        throttle: float = 0.5,
        timeout: int = 60,
        max_retries: int = 5,
        logger: Callable[[str, str], None] | None = None,
    ):
        if not webhook_url:
            raise B24AuthError(
                "NO_WEBHOOK",
                "No hay URL de webhook configurada en Bitrix24 Migration Settings.",
            )

        self.base_url = webhook_url.strip().rstrip("/") + "/"
        self.throttle = max(float(throttle or 0), 0.0)
        self.timeout = int(timeout or 60)
        self.max_retries = int(max_retries or 5)
        self._log = logger or (lambda level, msg: None)

        self._session = requests.Session()
        self._last_call_at = 0.0

        # Diagnóstico de la corrida: cuántas peticiones HTTP se gastaron
        # y cuánto tiempo estuvimos esperando por el rate limit.
        self.request_count = 0
        self.throttled_seconds = 0.0

    # -----------------------------------------------------------------
    # API pública
    # -----------------------------------------------------------------

    def call(self, method: str, params: dict | None = None) -> Any:
        """
        Ejecuta un método REST y devuelve su `result`.

        Lanza B24FatalError ante problemas de autenticación o cuando se
        agotan los reintentos; B24Error ante un fallo propio del método.
        """
        payload = self._unwrap(self._request(method, params or {}), method)
        return payload.get("result")

    def call_raw(self, method: str, params: dict | None = None) -> dict:
        """
        Como call(), pero devuelve la respuesta completa (`result`,
        `total`, `next`, `time`). Lo usan count() y las llamadas que
        necesitan el cursor `next`.
        """
        return self._unwrap(self._request(method, params or {}), method)

    def batch(
        self,
        commands: dict[str, tuple[str, dict]],
        halt: bool = False,
    ) -> dict[str, Any]:
        """
        Ejecuta hasta 50 sub-llamadas independientes en un solo request.

        `commands` es {clave: (método, params)}. Devuelve
        {clave: result}. Las sub-llamadas que fallaron **no aparecen**
        en el resultado: su error se registra vía el logger y la clave
        se omite, para que el llamador pueda distinguir "sin datos" de
        "falló" comparando claves.

        Con halt=False (el valor por defecto) un error en una
        sub-llamada no detiene a las demás: en una migración interesa
        más avanzar y anotar el fallo que abortar el lote entero.
        """
        if not commands:
            return {}

        if len(commands) > MAX_BATCH_COMMANDS:
            raise ValueError(
                f"batch() admite {MAX_BATCH_COMMANDS} comandos como máximo, "
                f"se pasaron {len(commands)}."
            )

        cmd = {key: self._encode_cmd(m, p) for key, (m, p) in commands.items()}
        raw = self._unwrap(
            self._request("batch", {"halt": 1 if halt else 0, "cmd": cmd}),
            "batch",
        )

        result = raw.get("result") or {}
        errors = result.get("result_error") or {}

        for key, err in (errors.items() if isinstance(errors, dict) else []):
            method = commands[key][0] if key in commands else "?"
            self._log(
                "error",
                f"batch/{method} [{key}]: "
                f"{_error_code(err)} — {_error_description(err)}",
            )

        payload = result.get("result") or {}
        return {k: v for k, v in payload.items() if k not in errors}

    def list_all(
        self,
        method: str,
        params: dict | None = None,
        start_after_id: int = 0,
        id_field: str = "ID",
        row_id_field: str | None = None,
        disable_total: bool = True,
    ) -> Iterator[dict]:
        """
        Itera TODOS los registros de una colección con paginación por ID.

        `start_after_id` permite reanudar una fase interrumpida desde el
        último ID procesado, sin volver a recorrer lo ya migrado.

        Requiere que el método admita `order` y un filtro `>ID` — lo
        cumplen crm.lead.list, crm.deal.list, crm.contact.list,
        crm.company.list, crm.activity.list y tasks.task.list. Para los
        métodos que no (p. ej. crm.status.list, que devuelve pocas filas
        de una vez), usar call() directamente.

        `id_field` es el nombre del campo al FILTRAR y ORDENAR;
        `row_id_field` el que trae la respuesta. Normalmente coinciden,
        pero tasks.task.list filtra por `ID` en mayúsculas y devuelve
        `id` en camelCase, y confundirlos deja el cursor clavado.

        `disable_total=False` conserva el conteo de total (`start: 0`).
        Hace falta en los métodos que no entienden el `start: -1` de la
        familia crm.*; se sigue paginando por ID, solo que pagando el
        conteo en cada página.
        """
        params = dict(params or {})
        base_filter = dict(params.pop("filter", {}) or {})
        last_id = int(start_after_id or 0)
        row_id_field = row_id_field or id_field

        while True:
            page_params = dict(params)
            page_params["order"] = {id_field: "ASC"}
            page_params["filter"] = {**base_filter, f">{id_field}": last_id}
            # start: -1 desactiva el conteo de total, que es lo caro.
            page_params["start"] = -1 if disable_total else 0

            rows = self.call(method, page_params) or []

            # Algunos métodos envuelven la lista en una clave
            # (p. ej. crm.item.list → {"items": [...]}).
            if isinstance(rows, dict):
                rows = _first_list_value(rows)

            if not rows:
                return

            page_last = rows[-1].get(row_id_field)
            if page_last is not None and int(page_last) <= last_id:
                # El portal ignoró el filtro `>ID` o el orden: esta
                # página ya se entregó, y la siguiente sería la misma
                # una y otra vez.
                self._log(
                    "error",
                    f"{method}: el cursor no avanza (último {row_id_field}="
                    f"{page_last}, cursor {last_id}); se interrumpe la "
                    "paginación. Puede que falten registros de esta colección.",
                )
                return

            for row in rows:
                yield row

            last_seen = rows[-1].get(row_id_field)
            if last_seen is None:
                # Sin ID no podemos avanzar el cursor: parar es mejor
                # que entrar en un bucle infinito sobre la misma página.
                self._log(
                    "error",
                    f"{method}: la respuesta no trae '{row_id_field}'; "
                    "se interrumpe la paginación.",
                )
                return

            last_id = int(last_seen)

            if len(rows) < PAGE_SIZE:
                return

    def count(self, method: str, params: dict | None = None) -> int:
        """
        Total de registros de una colección.

        Paga una vez el conteo caro que list_all() evita. Se usa para
        los informes del dry-run y para el cotejo posterior.
        """
        params = dict(params or {})
        params["start"] = 0
        params.setdefault("select", ["ID"])
        raw = self.call_raw(method, params)
        return int(raw.get("total") or 0)

    def profile(self) -> dict:
        """Perfil del usuario dueño del webhook. Sirve de prueba de vida."""
        return self.call("profile") or {}

    # -----------------------------------------------------------------
    # Transporte
    # -----------------------------------------------------------------

    def _request(self, method: str, params: dict) -> dict:
        """
        POST al portal con control de ritmo y backoff.

        Bitrix24 acepta el cuerpo como JSON, lo que evita tener que
        serializar los filtros anidados al formato de arrays de PHP.
        """
        url = self.base_url + method + ".json"
        delay = 1.0
        last_error: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            self._respect_throttle()

            try:
                response = self._session.post(
                    url, json=params, timeout=self.timeout
                )
            except requests.RequestException as e:
                last_error = e
                self._log(
                    "warning",
                    f"{method}: fallo de red ({e}); "
                    f"reintento {attempt}/{self.max_retries} en {delay:.0f}s",
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            self.request_count += 1

            if response.status_code in (401, 403):
                raise B24AuthError(
                    "ACCESS_DENIED",
                    f"Bitrix24 rechazó la llamada (HTTP {response.status_code}). "
                    + permission_hint(method),
                    method,
                )

            if response.status_code >= 500 or response.status_code == 429:
                last_error = B24Error(
                    f"HTTP_{response.status_code}", response.text[:200], method
                )
                self._log(
                    "warning",
                    f"{method}: HTTP {response.status_code}; "
                    f"reintento {attempt}/{self.max_retries} en {delay:.0f}s",
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            try:
                data = response.json()
            except ValueError:
                raise B24FatalError(
                    "INVALID_RESPONSE",
                    f"El portal devolvió algo que no es JSON: "
                    f"{response.text[:200]}",
                    method,
                )

            code = _error_code(data)
            if code and code in RETRYABLE_ERROR_CODES:
                last_error = B24Error(code, _error_description(data), method)
                self._log(
                    "warning",
                    f"{method}: {code}; "
                    f"reintento {attempt}/{self.max_retries} en {delay:.0f}s",
                )
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue

            return data

        raise B24FatalError(
            "RETRIES_EXHAUSTED",
            f"Se agotaron los {self.max_retries} reintentos de {method}. "
            f"Último error: {last_error}",
            method,
        )

    def _respect_throttle(self) -> None:
        """
        Espera lo que falte para cumplir el intervalo mínimo entre
        peticiones. Mide contra la hora de la última llamada real, así
        que el tiempo que tardó el portal en responder ya cuenta como
        espera y no se duerme de más.
        """
        if not self.throttle:
            return

        elapsed = time.monotonic() - self._last_call_at
        pending = self.throttle - elapsed
        if pending > 0:
            time.sleep(pending)
            self.throttled_seconds += pending

        self._last_call_at = time.monotonic()

    def _unwrap(self, data: dict, method: str) -> dict:
        """Convierte un `error` de la respuesta en la excepción que toca."""
        code = _error_code(data)
        if not code:
            return data

        description = _error_description(data)

        if code in AUTH_ERROR_CODES:
            raise B24AuthError(code, f"{description}. {permission_hint(method)}", method)

        if code in ("ERROR_METHOD_NOT_FOUND", "METHOD_NOT_FOUND"):
            raise B24FatalError(
                code,
                f"El portal no expone el método '{method}'. "
                "Puede depender del plan contratado o de un módulo "
                "no instalado (p. ej. Open Channels).",
                method,
            )

        raise B24Error(code, description, method)

    def _encode_cmd(self, method: str, params: dict) -> str:
        """
        Serializa una sub-llamada de `batch` al formato
        `metodo?clave=valor&anidado[a][b]=valor`.

        Aquí sí hace falta el formato de arrays de PHP: los comandos del
        batch viajan como cadenas de query, no como JSON.
        """
        pairs: list[str] = []
        _flatten(params, "", pairs)
        if not pairs:
            return method
        return method + "?" + "&".join(pairs)

    def safe_url(self) -> str:
        """URL del portal sin el token, apta para logs."""
        return _safe_url(self.base_url)


# ---------------------------------------------------------------------------
# Helpers de módulo
# ---------------------------------------------------------------------------


def _flatten(value: Any, prefix: str, out: list[str]) -> None:
    """
    Aplana un valor anidado a pares `clave=valor` con notación de
    corchetes, que es lo que entiende el `batch` de Bitrix24.

        {"filter": {">ID": 5}}  →  filter[>ID]=5
        {"select": ["ID", "TITLE"]}  →  select[0]=ID&select[1]=TITLE
    """
    if isinstance(value, dict):
        for k, v in value.items():
            key = f"{prefix}[{k}]" if prefix else str(k)
            _flatten(v, key, out)
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            key = f"{prefix}[{i}]" if prefix else str(i)
            _flatten(v, key, out)
    elif value is None:
        out.append(f"{quote(prefix, safe='[]<>=')}=")
    elif isinstance(value, bool):
        out.append(f"{quote(prefix, safe='[]<>=')}={1 if value else 0}")
    else:
        out.append(f"{quote(prefix, safe='[]<>=')}={quote(str(value), safe='')}")


def _error_code(data: Any) -> str:
    """Extrae el código de error de una respuesta o sub-respuesta."""
    if isinstance(data, dict):
        return data.get("error") or ""
    if isinstance(data, str):
        return data
    return ""


def _error_description(data: Any) -> str:
    if isinstance(data, dict):
        return data.get("error_description") or ""
    return ""


def _first_list_value(payload: dict) -> list:
    """
    Devuelve la primera lista que encuentre dentro de un `result` que
    viene envuelto en una clave (crm.item.list → {"items": [...]}).
    """
    for value in payload.values():
        if isinstance(value, list):
            return value
    return []


def _safe_url(url: str) -> str:
    """
    Recorta el token del webhook: de
    `https://portal.bitrix24.es/rest/1/abc123xyz/` deja
    `https://portal.bitrix24.es/rest/1/***/`.
    """
    parts = (url or "").rstrip("/").split("/")
    if len(parts) >= 2 and parts[-2].isdigit():
        parts[-1] = "***"
        return "/".join(parts) + "/"
    return "***"


# Permiso del webhook que exige cada familia de métodos. Más específico
# primero: «imopenlines.» antes que «im.».
PERMISSIONS = (
    (("tasks.", "task."), "Tareas", "task"),
    (("sonet_group.",), "Grupos de trabajo", "sonet_group"),
    (("imopenlines.",), "Canales abiertos", "imopenlines"),
    (("im.",), "Chat y notificaciones", "im"),
    (("disk.",), "Drive", "disk"),
    (("user.",), "Usuarios", "user"),
    (("crm.",), "CRM", "crm"),
)


def permission_hint(method: str) -> str:
    """
    Qué hacer ante un rechazo del portal, nombrando el permiso del
    webhook que exige `method`.

    Bitrix24 responde 401 tanto si el token ya no vale como si el
    webhook no tiene permiso sobre el módulo del método. Lo segundo es
    lo habitual al añadir fases (p. ej. Tareas): decirlo con el nombre
    del permiso ahorra adivinar.
    """
    method = (method or "").lower()
    for prefixes, label, scope in PERMISSIONS:
        if method.startswith(prefixes):
            return (
                f"Al webhook le falta el permiso «{label}» ({scope}) que exige "
                f"{method}, o su token ya no es válido. Añádalo en Bitrix24 → "
                "Recursos para desarrolladores → Integraciones → editar el webhook."
            )
    return "Verifique que el token del webhook siga vigente y sus permisos."


def get_client(logger: Callable[[str, str], None] | None = None) -> B24Client:
    """
    Construye un B24Client con lo configurado en
    Bitrix24 Migration Settings. Es el punto de entrada que usan todas
    las fases de la migración.
    """
    settings = frappe.get_single("Bitrix24 Migration Settings")

    return B24Client(
        webhook_url=settings.get_password("webhook_url", raise_exception=False),
        throttle=settings.throttle_seconds,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
        logger=logger,
    )
