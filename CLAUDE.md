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
# 3. Si cambió el SSH gate, desplegar a los 4 servidores
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
