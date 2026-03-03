# jetbackup-remote

## Propósito

Orquestador Python que serializa jobs JetBackup5 de múltiples servidores hacia un NAS RAID5 (Raspberry Pi).
Resuelve saturación del controlador USB (JMicron JMS567) cuando hay escrituras concurrentes.

## Arquitectura (v0.2)

- **stdlib puro** - cero dependencias externas
- **subprocess** para SSH (no paramiko)
- **JSON** para config
- **Sin threading** - la serialización es el objetivo
- **fcntl.flock** para lock file
- **Procesamiento agrupado por servidor** (no cola FIFO plana)
- **Lifecycle management**: destinos se activan/desactivan, jobs se habilitan/deshabilitan
- **Verificación post-ejecución**: comprobación de queue group status + logs

## Estructura

```
src/jetbackup_remote/
  models.py        - Dataclasses, enums (QueueGroupStatus, DestinationState, ServerRun)
  config.py        - Carga/validación JSON + DestinationConfig
  ssh.py           - Wrapper subprocess SSH
  jetbackup_api.py - Abstracción jetbackup5api (_api_call helper + 15 funciones)
  orchestrator.py  - Motor agrupado por servidor + lifecycle
  notifier.py      - Email smtplib con secciones verification/lifecycle
  cli.py           - argparse subcomandos (run, test, status, validate, destinations)
  logging_config.py - RotatingFileHandler
server/
  jetbackup-ssh-gate.sh - Filtro SSH con whitelist de comandos permitidos
```

## Despliegue producción

| Componente | Ubicación | Notas |
|-----------|-----------|-------|
| Código | raspxer:/opt/jetbackup-remote/ | Git clone (HTTPS) |
| Config | raspxer:/etc/jetbackup-remote/config.json | NO en repo (.gitignore) |
| Wrapper | raspxer:/usr/local/bin/jetbackup-remote | Wrapper bash con PYTHONPATH |
| Systemd | raspxer:/etc/systemd/system/jetbackup-remote.{service,timer} | Timer: 00:01 local |
| SSH gate | 4 servidores:/usr/local/bin/jetbackup-ssh-gate.sh | Whitelist de jetbackup5api |
| Logs | raspxer:/var/log/jetbackup-remote.log | RotatingFile 10MB x 5 |

**Proceso de actualización:**

```bash
# 1. Push cambios a GitHub
git push
# 2. Pull en raspxer
ssh raspxer 'cd /opt/jetbackup-remote && git pull origin main'
# 3. Si cambió el SSH gate, desplegar a los 4 servidores:
#    - servidor02, servidor20, servidor30: root directo (sed o scp)
#    - central: root por puerto 51514 (centralxer conecta como secxer sin sudo)
# 4. Si cambió el systemd service:
ssh raspxer 'cp /opt/jetbackup-remote/systemd/jetbackup-remote.service /etc/systemd/system/ && systemctl daemon-reload'
```

## Tests

```bash
PYTHONPATH=src python3 -m unittest discover -s tests
```

199 tests. Las fixtures en `tests/fixtures/` deben usar el formato envelope real de la API.

## Convenciones

- Código en inglés, documentación en español
- Tests obligatorios para cada módulo
- Commits en inglés con Co-Authored-By

## Lecciones aprendidas

### 2026-02-14 — Funciones list de API devolvían dict en vez de lista

**Contexto:** jetbackup_api.py — `list_queue_groups`, `list_queue_items`, `list_logs`, `list_log_items`
**Problema:** Crash en producción: `AttributeError: 'str' object has no attribute 'get'` al iterar los resultados
**Causa raíz:** La API de JetBackup5 devuelve `{"success":1, "data":{"groups":[...], "total":N}}`. Las funciones usaban `expect_data=False` y devolvían `data["data"]` que es `{"groups":[...], "total":N}` (un dict), no la lista. Al iterar un dict se obtienen sus keys como strings.
**Solución:** Usar `expect_data=True` (que extrae `data["data"]` automáticamente) y luego extraer la key correcta: `.get("groups", [])`, `.get("items", [])`, `.get("logs", [])`.
**Regla derivada:** Toda función `list_*` de la API debe extraer la colección por su nombre de key. Verificar SIEMPRE con `-O json` en producción antes de asumir la estructura.

### 2026-02-14 — Queue group guarda job_id en data._id, no en top-level

**Contexto:** orchestrator.py — `_find_queue_group_for_job`
**Problema:** La verificación post-ejecución no encontraba el queue group del job recién ejecutado
**Causa raíz:** El código buscaba `group["job_id"]` pero JetBackup5 almacena la referencia al job en `group["data"]["_id"]`
**Solución:** Cambiar a `group.get("data", {}).get("_id")` para el matching
**Regla derivada:** Las fixtures de test DEBEN reflejar la estructura real de la API. Usar respuestas capturadas de producción como base para fixtures.

### 2026-02-25 — editBackupJob no existe, usar manageBackupJob

**Contexto:** jetbackup_api.py — `set_job_enabled`
**Problema:** Jobs con `disabled=1` nunca arrancaban. El orquestador enviaba `editBackupJob -D "enabled=1"` (luego corregido a `disabled=0`), JetBackup devolvía rc=0 pero el campo `disabled` no cambiaba. El job se triggeaba con `runBackupJobManually` pero nunca pasaba a `running=true`, causando startup timeout de 120s.
**Causa raíz:** `editBackupJob` no existe en la API de JetBackup5. La API la acepta silenciosamente (rc=0, 267 bytes) sin hacer nada. La función correcta es **`manageBackupJob`** con `action=modify`.
**Evidencia:** servidor20 funcionaba por casualidad (sus jobs ya tenían `disabled=0`). servidor30 fallaba (jobs con `disabled=1`). El `runBackupJobManually` devolvía 219 bytes (error silencioso) vs 3098 bytes cuando el job realmente arranca.
**Solución:** Cambiar a `manageBackupJob` con params `_id`, `action=modify`, `disabled=0/1`
**Regla derivada:** Listar funciones disponibles con `jetbackup5api -F ""` antes de asumir nombres. El patrón de la API es `manage*` para modificar estado (no `edit*`).

### 2026-02-25 — Formato de parámetros -D en jetbackup5api

**Contexto:** jetbackup_api.py — `_api_call`, `_api_call_no_json`
**Problema:** Múltiples flags `-D "key=value"` funcionaban para algunas funciones pero no para `manageBackupJob` (ignoraba `action=modify` silenciosamente).
**Causa raíz:** El formato documentado de JetBackup5 es un solo `-D` con parámetros separados por `&`: `-D "key1=value1&key2=value2"`. El formato con múltiples `-D` es un comportamiento no documentado que funciona para funciones simples pero no para todas.
**Solución:** Cambiar ambos helpers (`_api_call` y `_api_call_no_json`) para usar `&`-separated params en un solo `-D`.
**Regla derivada:** Usar SIEMPRE el formato canónico `&`-separated. Nunca múltiples `-D`, aunque parezca funcionar.

### 2026-02-25 — JetBackup5 usa campo disabled (no enabled) para jobs

**Contexto:** jetbackup_api.py, fixtures de test
**Problema:** Las fixtures usaban `"enabled": true/false` pero la API real devuelve `"disabled": 0/1`.
**Causa raíz:** Se asumió la estructura del campo sin verificar con la API real.
**Solución:** Fixtures actualizadas a `"disabled": 0` / `"disabled": 1`. El campo `enabled` no existe en la respuesta de `getBackupJob`.
**Regla derivada:** Para destinos Y para jobs, el campo de estado es `disabled` (0/1 o true/false). Nunca `enabled`.

## Referencia rápida API JetBackup5

| Acción | Función API | Params clave |
|--------|-------------|--------------|
| Listar funciones | `jetbackup5api -F ""` | — |
| Estado destino | `manageDestinationState` | `_id`, `disabled=0/1` |
| Modificar job | `manageBackupJob` | `_id`, `action=modify`, `disabled=0/1` |
| Trigger job | `runBackupJobManually` | `_id` |
| Info job | `getBackupJob` | `_id` |
| Info destino | `getDestination` | `_id` |
| Queue groups | `listQueueGroups` | `type=1` (backups) |

**Formato de parámetros:** siempre `&`-separated en un solo `-D`: `-D "_id=xxx&action=modify&disabled=0"`

### 2026-03-03 — Timeout debe abortar, nunca "pasar al siguiente"

**Contexto:** orchestrator.py — `_poll_completion_only`, producción con 4 servidores
**Problema:** 4 backups concurrentes saturaron el controlador USB del NAS (JMicron JMS567). El orquestador, diseñado para max 1 backup activo, permitió 4 simultáneos.
**Causa raíz:** Cuando un job excedía `job_timeout`, el orquestador marcaba TIMEOUT pero NO abortaba el backup — simplemente pasaba al siguiente servidor. El backup original seguía corriendo. Con 4 servidores, se acumularon 4 backups concurrentes. `Persistent=true` en systemd timer agravaba el problema encadenando runs.
**Solución:** Al exceder timeout, abortar el backup via `stopQueueGroup` (status=ABORTED=201), esperar confirmación de parada (hasta 60s), y entonces continuar. Timer systemd cambiado a `Persistent=false`. Añadido `_global_preflight_check` que verifica TODOS los servidores antes de iniciar.
**Regla derivada:** FIFO inviolable. Si un job está atascado, abortarlo limpiamente via `stopQueueGroup` en vez de proceder al siguiente. Nunca debe haber 2 backups escribiendo al NAS simultáneamente.
