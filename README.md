# CORTEC Bitrix24

Migración de Bitrix24 a Frappe CRM para Corporación de Tecnología CORTEC S.R.L.

## Licencia

AGPL-3.0-or-later — Ver archivo [LICENSE](LICENSE).

## Qué hace

Trae a Frappe CRM el contenido de un portal de Bitrix24 leyéndolo por su
API REST, de forma **incremental e idempotente**: la migración se puede
correr en seco, repetir tantas veces como haga falta, y rematar con una
pasada delta el día del corte.

| Fase | En Bitrix24 | En Frappe |
| ---- | ----------- | --------- |
| 1 | Etapas, fuentes, industrias, embudos | CRM Lead Status, CRM Deal Status, CRM Lead Source, CRM Industry |
| 2 | Usuarios | Bitrix24 User Map (empareja por correo; **no crea usuarios**) |
| 3 | Campos personalizados `UF_CRM_*` | Custom Field en el doctype destino |
| 4 | Empresas | CRM Organization |
| 5 | Contactos | Contact (+ Contact Email / Contact Phone) |
| 6 | Leads | CRM Lead |
| 7 | Negociaciones | CRM Deal (+ tabla de contactos) |
| 8 | Correos, llamadas, to-dos y comentarios del timeline del CRM | Communication, CRM Call Log, CRM Task, FCRM Note |
| 9 | Módulo Tareas y proyectos (`tasks.task.*`) | CRM Task |
| 10 | WhatsApp de Open Channels | WhatsApp Message |
| 11 | Adjuntos | File (se bajan junto con su correo, mensaje o tarea) |
| 12 | Formularios web | Bitrix24 Web Form (mapeo de campos, para el corte) |

**No** migra: tickets de soporte a Helpdesk (Bitrix24 se usa aquí como CRM),
presupuestos ni facturas.

## Estructura

```
cortec_bitrix24/
├── LICENSE
├── setup.py
├── requirements.txt
├── README.md
└── cortec_bitrix24/
    ├── __init__.py
    ├── hooks.py
    ├── api.py                  # start_migration, verify_counts, export_webform_mapping
    ├── utils.py                # teléfonos E.164, fechas, BBCode
    ├── bitrix/
    │   ├── client.py           # batch, paginación por ID, rate limit, reintentos
    │   └── errors.py
    ├── migration/
    │   ├── runner.py           # fases, checkpoints, silenciado de avisos
    │   ├── idmap.py            # upsert() — el núcleo de la idempotencia
    │   ├── dicts.py            # fase 1        ├── tasks.py       # fase 9
    │   ├── users.py            # fase 2        ├── openlines.py   # fase 10
    │   ├── userfields.py       # fase 3        ├── files.py       # fase 11
    │   ├── companies.py        # fase 4        └── webforms.py    # fase 12
    │   ├── contacts.py         # fase 5
    │   ├── leads.py            # fase 6
    │   ├── deals.py            # fase 7
    │   └── activities.py       # fase 8
    └── tests/
```

## Instalación

```bash
bench get-app cortec_bitrix24 https://github.com/pvanderlaatu/cortec_bitrix24
bench --site sitio.dominio.com install-app cortec_bitrix24
bench --site sitio.dominio.com migrate
bench restart
```

Requiere `crm` (Frappe CRM). `frappe_whatsapp` y `cortec_helpdesk` son
opcionales y se detectan en tiempo de ejecución.

## Configuración

### 1. Webhook entrante en Bitrix24

En el portal: **Recursos para desarrolladores → Otros → Webhook entrante**.
Marcar los permisos **CRM**, **Usuarios**, **Disco** y **Canales abiertos**.
Copiar la URL, que tiene esta forma:

```
https://cortec.bitrix24.es/rest/1/xxxxxxxxxxxxxxxx/
```

### 2. Bitrix24 Migration Settings

| Campo | Valor recomendado |
| ----- | ----------------- |
| URL del portal | `https://cortec.bitrix24.es` |
| URL del webhook | la del paso anterior |
| Pausa entre peticiones | `0.5` s (el portal admite ~2 req/s) |
| Sub-llamadas por batch | `50` (tope de Bitrix24) |
| Usuario de respaldo | el agente que reciba lo que no tenga dueño |
| Código de país | `506` |
| Corrida en seco | ✅ hasta haber revisado el informe |
| Silenciar avisos de CORTEC Helpdesk | ✅ |
| Conservar fechas originales | ✅ |

Pulsar **Probar conexión**: debe devolver el perfil del webhook y el
número de registros de cada colección. Esas cifras son contra las que se
coteja la migración al terminar.

### 3. Corrida en seco

**Migrar → Corrida en seco.** No escribe nada; produce un
`Bitrix24 Migration Run` cuyo campo *Informe* trae:

- conteos previstos por entidad,
- usuarios de Bitrix24 sin `User` equivalente en Frappe,
- etapas sin mapear,
- campos `UF_CRM_*` que se van a crear.

### 4. Revisión manual antes del corte

Con el informe en la mano:

1. **Bitrix24 User Map** — rellenar el campo *Usuario de Frappe* de los
   agentes que deban conservar sus leads. Lo que se ponga a mano no se
   vuelve a pisar en las pasadas siguientes.
2. **Bitrix24 Status Map** — corregir las etapas que la heurística no
   acertó. Ojo al campo *Tipo*: solo `Open` cuenta como "abierto" para
   el enrutado de WhatsApp de `cortec_helpdesk`.
3. **Bitrix24 Field Map** — marcar *Omitir* en los campos personalizados
   que no valga la pena arrastrar.

### 5. Corrida real

**Migrar → Migración real.** Exige que exista una corrida en seco
completada. Se encola en la cola `long` (los workers de esa cola deben
estar activos) y puede tardar horas: a dos peticiones por segundo, 40 000
negociaciones son unas cuantas.

La corrida se puede parar y relanzar: cada fase guarda su cursor y la
siguiente pasada sigue donde quedó.

### 6. Cotejo

```python
import frappe
frappe.call("cortec_bitrix24.api.verify_counts")
```

### 7. Corte

1. Última pasada delta (el propio botón la lanza usando la fecha de la
   última corrida completa).
2. Exportar el mapeo de formularios:
   `frappe.call("cortec_bitrix24.api.export_webform_mapping")`, y usarlo
   para reapuntar el formulario del sitio al Worker `leads-intake`.
3. Poner el portal de Bitrix24 en solo lectura.

## Decisiones que conviene conocer

### El envío de WhatsApp se desactiva durante la fase 10

`WhatsAppMessage.before_insert` de `frappe_whatsapp` llama a
`send_outgoing()`, que **manda el mensaje a Meta** cuando el tipo es
`Outgoing`. Insertar el histórico sin más reenviaría a los clientes, uno
a uno, todos los WhatsApp que los agentes escribieron en años.

`openlines.py` sustituye ese método por un no-op mientras dura la fase y
lo restaura al salir. Si el método no existe (una versión nueva de
`frappe_whatsapp` lo movió), **la fase se aborta** en lugar de seguir.

### Los avisos de CORTEC Helpdesk se apagan durante la corrida

Migrar el histórico dispara `telegram.on_communication` y
`whatsapp.route_unclaimed_message`. Sin protección, eso son miles de
mensajes de Telegram a los agentes y un CRM Lead espurio por cada
WhatsApp antiguo.

`runner.silence_cortec_alerts` pone a 0 `telegram_enabled` y
`browser_alerts_enabled` de `CORTEC Helpdesk Settings` y los restaura en
el `finally`. Si el worker muere de golpe quedarían apagados: **revisar
esos dos checks al terminar** es parte del cierre.

Lo limpio sería un `return` temprano ante
`frappe.flags.in_bitrix24_migration` en los hooks de `cortec_helpdesk`
(la bandera ya se activa durante toda la corrida). Mientras eso no
exista allí, el interruptor hace el trabajo.

### Los consentimientos se importan aparte, con su evidencia

Las fases no tocan `custom_acepta_promociones` ni
`custom_consentimiento`: en cortec_helpdesk ≥ 1.0.12 esos campos se
derivan del registro de consentimientos (`CORTEC Consent Record`), y
bajo la Ley 8968 la carga de la prueba recae sobre el responsable de la
base de datos.

La evidencia sí existe en Bitrix24: los «Acuerdos» de los formularios
guardan un registro por envío con fecha, IP, URL y la actividad del CRM.
La API REST no permite listarlo, así que se exporta a mano **después de
la corrida real**, un CSV por acuerdo:

1. Formularios del CRM → el formulario → **Acuerdos** → el acuerdo →
   lista de consentimientos («Consentimiento del usuario»).
2. **TODO** para ver todas las filas en una página, seleccionar la tabla
   y pegarla en Excel.
3. Guardar como CSV. Hacen falta las columnas Fecha, Dirección IP, Origen
   y Dirección; el separador (`;`, `,` o tabulador) y la codificación dan
   igual.
4. Subir el CSV como File privado e importar, primero en seco:

```bash
bench --site <sitio> execute cortec_bitrix24.api.import_webform_consents   --kwargs "{'file_url': '/private/files/promociones.csv', 'agreement': 'promociones', 'dry_run': 1}"
```

El informe dice cuántas filas se resolvieron por doctype y lista las
actividades sin titular migrado. Con `dry_run: 0` se crean los
registros con canal «Bitrix24». Repetir la importación no duplica nada.
Para la Política de Privacidad se usa `'agreement': 'politica-privacidad'`.

Por defecto no suscribe a nadie a la lista promocional; `'subscribe': 1`
lo hace. Las fechas son la hora del portal.

### Los documentos suprimidos no se reescriben

Si un titular pidió la supresión de sus datos (cortec_helpdesk ≥ 1.0.13),
sus documentos quedan en `CORTEC Suppressed Document`. `idmap.upsert`
no actualiza esos documentos ni crea notas, correos o adjuntos colgados
de ellos, y la importación de consentimientos los omite. Queda un aviso
en el log de la corrida.

Lo que no se puede evitar desde aquí es que el titular siga en el portal
de Bitrix24 mientras esté activo: hay que borrarlo también allí.

### `custom_account_manager` sí se rellena

El responsable de cada contacto en Bitrix24 se escribe en
`Contact.custom_account_manager` si `cortec_helpdesk` está instalado.
Es lo que hace que su asignación automática de tickets funcione desde el
primer correo que entre tras el corte.

### Las dos cosas que Bitrix24 llama «tarea»

Son colecciones distintas y se migran en fases distintas, ambas a
`CRM Task`:

- **To-dos del timeline del CRM** (`crm.activity.list` con `PROVIDER_ID`
  `CRM_TODO`, `TASK` o `CRM_REMINDER`) — fase 8. Siempre cuelgan de un
  Lead, Deal, Contacto o Empresa.
- **Módulo Tareas y proyectos** (`tasks.task.list`) — fase 9. Se migran
  **todas** las tareas del portal, también las internas sin relación con
  el CRM, que quedan sin referencia (en `CRM Task` solo `title` es
  obligatorio).

Cuidado con las **prioridades**: son dos escalas distintas en el mismo
portal. Las actividades del CRM usan 1/2/3 y las tareas 0/1/2. Aplicar
una a la otra desplaza todas las prioridades un escalón; hay un test que
lo vigila.

El estado 4 de Bitrix24 («supuestamente completada»: el ejecutor la
cerró y falta que el creador la acepte) se migra como `Done`. En un
portal que se va a retirar nadie va a aceptar ya nada, y dejarla
`In Progress` mostraría como pendiente lo que está hecho.

### Lo que no cabe en CRM Task

`CRM Task` tiene ocho campos y una tarea de Bitrix24 tiene muchos más.
Sin inventar Custom Fields, lo que no tiene hueco propio se escribe como
una cabecera dentro de `description`, que es un Text Editor: proyecto,
creador, co-ejecutores, observadores, tiempo estimado e invertido, el
checklist como lista de casillas y los comentarios.

No se migran dependencias entre tareas, sprints, plantillas ni historial
de cambios. Una tarea archivada no necesita sus dependencias: ya no se
va a replanificar nada.

**Los comentarios pueden no llegar.** `task.commentitem.getlist` está
descontinuado desde `tasks 25.700.0` y no funciona con la ficha nueva de
tarea. La fase lo intenta igual y, si el portal ya no lo expone, lo
anota en el informe de la corrida — precisamente para que unas tareas
sin comentarios no se confundan con tareas que nunca los tuvieron.
Recuperarlos exigiría leer el chat de cada tarea con
`im.dialog.messages.get`, que es otra fase entera.

### El importe de los leads

En Bitrix24 un lead tiene importe (`OPPORTUNITY`); en Frappe CRM el
CRM Lead no tiene campo de valor. Se anota dentro de
`custom_comentarios` en vez de forzarlo dentro de `annual_revenue`, que
significa la facturación anual de la empresa y falsearía los informes.

### Etapas en curso → `Open`, no `Ongoing`

`cortec_helpdesk` compara el tipo de estado con la cadena exacta
`"Open"`. Una etapa marcada `Ongoing` haría que un WhatsApp entrante se
tomara como no atendido y generara un Lead duplicado.

## Diagnóstico

### Seguir una corrida

```python
import frappe
frappe.call("cortec_bitrix24.api.migration_status")
```

### Ver los errores de la última corrida

```python
import frappe
frappe.get_all("Bitrix24 Migration Log",
    filters={"level": "Error"},
    fields=["phase", "bitrix_id", "message"],
    order_by="creation desc", limit=20)
```

### Comprobar la correspondencia de un registro concreto

```python
import frappe
from cortec_bitrix24.migration import idmap

print(idmap.get_target(idmap.ENTITY_LEAD, "1234"))     # ('CRM Lead', 'CRM-LEAD-2024-00042')
print(idmap.get_target(idmap.ENTITY_DEAL, "987"))
```

### Usuarios y etapas sin resolver

```python
import frappe

print(frappe.get_all("Bitrix24 User Map",
    filters={"user": ["is", "not set"]},
    fields=["bitrix_user_id", "bitrix_name", "bitrix_email"]))

print(frappe.get_all("Bitrix24 Status Map",
    filters={"crm_status": ["is", "not set"]},
    fields=["entity", "bitrix_status_id", "bitrix_label"]))
```

### Rehacer una fase

Borrar su entrada del campo *Cursores* del `Bitrix24 Migration Run` y
relanzar. Para rehacer una entidad **desde cero**, borrar además sus
filas del ID Map — lo que hará que la siguiente pasada cree documentos
nuevos, no que actualice los existentes:

```python
import frappe
frappe.db.delete("Bitrix24 ID Map", {"entity_type": "lead"})
frappe.db.commit()
```

## Tests

```bash
bench --site sitio.dominio.com run-tests --app cortec_bitrix24
```

Cubren lo que falla en silencio: la paginación por ID del cliente (un
cursor que no avanza da un bucle infinito; uno que salta pierde
registros), la serialización de los comandos de `batch`, que el token
del webhook nunca salga en un log, y la normalización de teléfonos —de
la que depende que el mismo cliente no acabe siendo tres contactos.

## Notas

- No modifica doctypes existentes de Frappe, CRM, Helpdesk ni ERPNext.
- No declara `erpnext` como dependencia (GPL-3.0) para no mezclarlo con
  AGPL-3.0, igual que `cortec_helpdesk`.
- Compatible con Frappe v15/v16 y Frappe CRM v2.x.
- No requiere ERPNext: las tareas van a `CRM Task` de Frappe CRM, y no a
  los doctypes `Task`/`Project` de ERPNext (GPL-3.0).
