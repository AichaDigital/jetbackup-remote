# jetbackup-remote v0.3.0 — Daemon Loop, Fiabilidad y Observabilidad

**Fecha:** 2026-03-13
**Autor:** Abdelkarim Mateos
**Estado:** Aprobado

## Descripción del problema

jetbackup-remote v0.2.0 usa un timer de systemd (`Type=oneshot`) para disparar la orquestación diaria de backups. Esta arquitectura tiene tres fallos críticos expuestos en el incidente del 2-3 de marzo de 2026:

1. **Ejecuciones en cascada** — Cuando un ciclo tarda más de 24h (habitual por el cuello de botella USB de la Raspberry Pi), la configuración `Persistent=true` del timer (config desplegada) encola la siguiente ejecución inmediatamente tras terminar la primera. El repo se parcheó luego a `Persistent=false`, pero esto causa el problema opuesto: las ejecuciones perdidas se omiten silenciosamente. Ninguna de las dos opciones es correcta para cargas de trabajo de duración variable.
2. **Fallo silencioso** — El timer fue deshabilitado manualmente el 3 de marzo tras un SIGTERM que mató un ciclo en ejecución. No existía ningún sistema de alertas, así que los backups estuvieron parados 10 días sin que nadie se enterase.
3. **Jobs no gestionados** — Varios jobs de JetBackup asignados al destino Raspberry no estaban en la configuración del orquestador, por lo que nunca se ejecutaron (backups MySQL en 3 servidores, Directories en central, el servidor kvm313 completo).

## Decisiones de diseño

### D1: Modelo de loop — Daemon Python con pausa calculada

**Elegido:** Método `run_forever()` en Orchestrator. Servicio systemd `Type=simple` con `Restart=on-failure`.

**Alternativas descartadas:**

- Wrapper shell (`while true; do run; sleep; done`) — Dos capas de señales, cálculo de pausa dinámica engorroso en bash.
- Timer con fichero de guarda — Ni `Persistent=true` (cascada) ni `Persistent=false` (omisión silenciosa) funciona para cargas de duración variable.

**Justificación:** Un solo proceso, un solo log, control total sobre el manejo de SIGTERM tanto durante la ejecución como durante la pausa.

### D2: Timeout por job — 24h con abort

**Elegido:** Timeout por job con valor por defecto de 86400s (24h). Al alcanzar el timeout: abortar via `stopQueueGroup`, enviar email de alerta, pasar al siguiente job.

**Alternativas descartadas:**

- Sin timeout (dejar los jobs correr indefinidamente) — Un job atascado bloquearía el ciclo entero indefinidamente.
- Timeout agresivo sin abort (comportamiento desplegado en v0.2) — Los jobs seguían corriendo después del "timeout", causando contención de I/O en cascada.

**Justificación:** 24h es generoso para el cuello de botella USB de la Raspberry Pi y a la vez detecta jobs genuinamente atascados. El override por job permite timeouts más cortos para jobs de base de datos o configuración.

### D3: Notificaciones — Email en fallo, fichero de estado siempre

**Elegido:** Email SMTP para alertas inmediatas + fichero de estado JSON para integración con monitorización.

**Descartado:** Solo Zabbix (aún no desplegado para esta infraestructura).

### D4: Jobs de JetBackup Config — excluidos del orquestador

**Elegido:** No gestionados. Se ejecutan de forma oportunista cuando el destino está activo durante otros jobs.

**Justificación:** Tamaño trivial (KB), no merece la sobrecarga de orquestación ni el tiempo de ciclo adicional.

## Arquitectura

### Ciclo de vida del loop

`run_forever()` es un nuevo método público de `Orchestrator`. Llama al método `run()` existente para cada ciclo (sin duplicación). El lock se adquiere una sola vez al arrancar. `_global_preflight_check()` permanece dentro de `run()`.

```
el daemon arranca
  → adquirir lock (una vez, se mantiene durante toda la vida del daemon)
  → escribir fichero de estado (daemon_pid, started_at)
  → enviar email "daemon arrancado" (si SMTP falla: loguear error, continuar — ver manejo de fallos SMTP)
  → loop:
      1. run() — ciclo completo (todos los servidores, todos los jobs) — método existente, sin cambios
      2. escribir fichero de estado (resultados del last_run) — escritura atómica via temp+rename
      3. si hay fallos/timeouts → enviar email resumen
      4. calcular pausa = max(target_interval - duración_ciclo, min_pause)
         - target_interval: 86400s (24h), configurable
         - min_pause: 3600s (1h), configurable
      5. _interruptible_sleep(pausa) — usa loop con time.sleep(30) comprobando _shutdown_requested
      6. volver a 1
  → al recibir SIGTERM:
      - si está dormido: despertar, enviar email "daemon parando", salir
      - si está ejecutando ciclo: activar flag de shutdown, el job actual termina limpiamente,
        los jobs/servidores restantes se omiten, enviar email, salir
      - si está dentro de _abort_running_job: la espera del abort se completa (máx 60s), luego procede el shutdown
  → liberar lock
```

### Subcomando CLI `daemon`

```
jetbackup-remote daemon [-c CONFIG] [--verbose]
```

- Sin filtros `--server` / `--job` (el daemon siempre ejecuta todos los jobs)
- Sin `--dry-run` (el daemon siempre es en vivo)
- Llama a `orchestrator.run_forever()`
- Gestiona adquisición de lock, configuración de señales, configuración de logging
- En `LockError`: código de salida 1 con mensaje "Another instance is already running"

`jetbackup-remote run` permanece sin cambios (ciclo único, retrocompatible).

### Flujo de timeout por job

```
el job arranca
  → resolver timeout: job.timeout o config.orchestrator.job_timeout
  → poll cada 30s hasta que running=False o timeout
  → si timeout (24h por defecto):
      1. encontrar el queue group en ejecución para este job
      2. llamar a stopQueueGroup (abort)
      3. poll hasta 60s para confirmación (NO interrumpible por SIGTERM — debe completarse)
      4. enviar email inmediato de timeout
      5. marcar job como TIMEOUT
      6. deshabilitar job, pasar al siguiente
  → si completa normalmente:
      1. verificar estado del queue group
      2. obtener logs si es problemático
      3. marcar como COMPLETED
```

**Resolución del timeout en `_poll_completion_only`:**

```python
timeout = job.timeout if job.timeout else self.config.orchestrator.job_timeout
```

### Override de timeout por job

Los jobs pueden especificar un campo `timeout` en la configuración (en segundos). Si está ausente o es 0, se aplica el `job_timeout` global. Los timeouts por job se validan de forma independiente: mínimo 1800s, sin máximo (el `MAX_JOB_TIMEOUT` global solo aplica al ajuste global).

```json
{
    "job_id": "...",
    "server": "central",
    "label": "Mysql Xer",
    "type": "database",
    "timeout": 7200
}
```

### Eventos de notificación

| Evento | Email | Fichero de estado |
|--------|-------|-------------------|
| Daemon arranca | Sí | pid, started_at |
| Daemon recibe SIGTERM | Sí | — |
| Timeout de job (abort) | Sí (inmediato) | — |
| Fallo de job (SSH, error API) | Sí (inmediato) | — |
| Ciclo completado con errores | Sí (resumen) | resultados completos |
| Ciclo completado sin errores | No (configurable) | resultados completos |

### Manejo de fallos SMTP

Las notificaciones por email nunca deben bloquear ni crashear el daemon. Política:

- **Todas las llamadas `send_*` envueltas en try/except** — Los errores SMTP se loguean como WARNING, nunca se propagan.
- **Sin reintentos** — Si SMTP está caído, el email se pierde. El fichero de estado queda como fuente de verdad.
- **Fallo SMTP registrado en el fichero de estado** — campo `"last_notification_error"` con timestamp y mensaje de error. Una herramienta de monitorización puede consultar este campo.
- **Sin canal de fallback** — Cuando Zabbix esté disponible, leerá el fichero de estado directamente, proporcionando una vía de alertas independiente.

### Fichero de estado

**Ruta:** `/var/lib/jetbackup-remote/last-run.json`

**Atomicidad:** Se escribe mediante fichero temporal + `os.rename()` para prevenir corrupción si el proceso muere.

**Timestamps:** Siempre en UTC, formato ISO 8601 con sufijo `Z`.

```json
{
    "version": "0.3.0",
    "daemon_pid": 12345,
    "daemon_started_at": "2026-03-13T23:01:00Z",
    "last_notification_error": null,
    "last_run": {
        "started_at": "2026-03-13T23:05:00Z",
        "finished_at": "2026-03-14T21:30:00Z",
        "duration_seconds": 80700,
        "total_jobs": 10,
        "completed": 8,
        "failed": 1,
        "timeout": 1,
        "skipped": 0,
        "success": false,
        "servers": {
            "central": {"status": "ok", "jobs": 3, "duration": 3420, "consecutive_failures": 0},
            "servidor02": {"status": "ok", "jobs": 3, "duration": 15900, "consecutive_failures": 0},
            "servidor20": {"status": "timeout", "jobs": 3, "duration": 86403, "consecutive_failures": 0},
            "servidor30": {"status": "ok", "jobs": 3, "duration": 31320, "consecutive_failures": 0},
            "kvm313": {"status": "failed", "jobs": 2, "duration": 0, "consecutive_failures": 3}
        }
    },
    "next_run_at": "2026-03-15T00:50:00Z"
}
```

**`consecutive_failures`:** Se incrementa cada ciclo en el que un servidor es inalcanzable (fallo SSH). Se resetea a 0 cuando hay éxito. Se lee del fichero de estado anterior al inicio de cada ciclo. Permite futuros triggers de Zabbix ante fallos sostenidos.

### Mejora del comando `status`

`jetbackup-remote status` lee el fichero de estado + estado live de los servidores:

```
Daemon: running (pid 12345, uptime 22h 30m)
Last cycle: 2026-03-14 22:30 UTC (8 OK, 1 TIMEOUT, 1 FAILED)
Next cycle: ~2026-03-15 01:50 UTC (in 3h 20m)

servidor02: SSH=OK JB5=OK DEST=DISABLED
  RasBackup (accounts): idle
  Directories-Ras (directories): idle
  Mysql02-Ras (database): idle
...
```

## Cambios de configuración

### Nueva sección: `loop`

```json
"loop": {
    "target_interval": 86400,
    "min_pause": 3600
}
```

### Nuevo dataclass: `LoopConfig`

```python
@dataclass
class LoopConfig:
    target_interval: int = 86400   # 24h — tiempo objetivo entre inicios de ciclo
    min_pause: int = 3600          # 1h — pausa mínima tras un ciclo
```

**Validación:**

- `target_interval` >= 3600 (mínimo 1h)
- `min_pause` >= 600 (mínimo 10 min)
- `min_pause` < `target_interval`

### Sección actualizada: `orchestrator`

```json
"orchestrator": {
    "poll_interval": 30,
    "job_timeout": 86400,
    "startup_timeout": 120,
    "state_file": "/var/lib/jetbackup-remote/last-run.json",
    "lock_file": "/tmp/jetbackup-remote.lock",
    "log_file": "/var/log/jetbackup-remote.log",
    "log_max_bytes": 10485760,
    "log_backup_count": 5
}
```

**Cambio de validación:** `MAX_JOB_TIMEOUT` sube a 172800 (48h) para dar margen sobre el default de 86400. Los campos `timeout` por job se validan por separado: mínimo 1800s, sin tope máximo.

### Sección actualizada: `notification`

```json
"notification": {
    "enabled": true,
    "smtp_host": "mail.castris.com",
    "smtp_port": 587,
    "smtp_tls": true,
    "smtp_user": "no-reply@castris.com",
    "smtp_password": "FROM_ENV:JETBACKUP_SMTP_PASSWORD",
    "from_address": "no-reply@castris.com",
    "to_addresses": ["abdel@xerintel.es"],
    "on_failure": true,
    "on_timeout": true,
    "on_complete": false,
    "on_partial": true,
    "on_daemon_lifecycle": true
}
```

**Convención del prefijo `FROM_ENV:`:** Si `smtp_password` empieza por `FROM_ENV:`, el resto se trata como nombre de variable de entorno. El cargador de configuración la resuelve en tiempo de parseo via `os.environ.get()`. Si la variable no existe, se lanza `ConfigError`.

**`on_partial`:** Campo booleano existente en `NotificationConfig`. Se mantiene de v0.2 — controla las notificaciones por completaciones parciales de backup. Default: `true`.

**`on_daemon_lifecycle`:** Nuevo campo booleano en `NotificationConfig`. Controla los emails de "daemon arrancado" y "daemon parando". Default: `true`.

## Nuevos jobs y servidores

### Jobs MySQL a crear (via WHM/JetBackup UI)

Crear jobs de backup de base de datos en servidor02, servidor20 y servidor30 apuntando al destino Raspberry:

- **Programación:** 1 vez al día
- **Retención:** mínima (Raspberry es destino secundario)
- **Modelo:** similar a "Mysql Xer" de central

Tras crearlos, añadir sus job_ids a `config.json`.

### Job Directories en central

Crear un job de Directories en central apuntando al destino Raspberry:

- **Include list:** la misma que el job Directories existente hacia Xerbackup02
- **Programación:** 1 vez al día

### Servidor kvm313

- **Host:** kvm313.xerintel.com
- **Puerto:** 51514
- **Panel:** DirectAdmin (no cPanel)
- **Llave SSH:** a determinar (probablemente RSA según el patrón Xerintel)
- **Acciones:**
  1. Registrar en sshctx
  2. Conectar via SSH directo para inventariar jobs existentes y destination_id
  3. Añadir a `config.json` con los job_ids descubiertos
  4. Nota: los comandos de la API de JetBackup pueden diferir ligeramente en DirectAdmin (verificar disponibilidad de `jetbackup5api`)

## Cambios en systemd

### Eliminar

- `/etc/systemd/system/jetbackup-remote.timer`

### Reemplazar servicio

```ini
[Unit]
Description=JetBackup Remote Orchestrator Daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 -m jetbackup_remote -c /etc/jetbackup-remote/config.json daemon
Restart=on-failure
RestartSec=300
Environment=PYTHONPATH=/opt/jetbackup-remote/src

ProtectSystem=strict
ReadWritePaths=/var/log /tmp /etc/jetbackup-remote /var/lib/jetbackup-remote
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelModules=true
ProtectKernelTunables=true

[Install]
WantedBy=multi-user.target
```

**Notas:**

- `ProtectKernelModules` y `ProtectKernelTunables` se mantienen del servicio v0.2.
- `PrivateTmp=true` da a cada instancia del servicio su propio `/tmp`. El fichero de lock en `/tmp/jetbackup-remote.lock` es privado al servicio — no hay problema de lock obsoleto al reiniciar porque systemd limpia el `/tmp` antiguo.
- `Restart=on-failure` + `RestartSec=300`: si el daemon crashea, systemd lo reinicia tras 5 min. El lock se libera al morir el proceso (la muerte del proceso libera el flock), así que la nueva instancia lo adquiere limpiamente.

## Resumen de cambios en código

### Ficheros nuevos

- Ninguno (todos los cambios en módulos existentes)

### Ficheros modificados

| Fichero | Cambios |
|---------|---------|
| `orchestrator.py` | Añadir `run_forever()`, `_interruptible_sleep()` (loop + `time.sleep(30)` comprobando `_shutdown_requested`), `_write_state()` (escritura atómica temp+rename, timestamps UTC), conectar llamadas al notifier en eventos de timeout/fallo/lifecycle, pasar `job.timeout` a `_poll_completion_only` |
| `cli.py` | Añadir subcomando `daemon` (sin filtros, sin dry-run, llama a `run_forever()`), actualizar `status` para leer fichero de estado + mostrar info del daemon |
| `config.py` | Añadir dataclass `LoopConfig` con validación, añadir `on_daemon_lifecycle` a `NotificationConfig`, añadir `state_file` a `OrchestratorConfig`, parsear `timeout` por job en `_parse_job()`, implementar resolución de contraseña `FROM_ENV:` en `_parse_notification()`, subir `MAX_JOB_TIMEOUT` a 172800 |
| `models.py` | Añadir `timeout: Optional[int] = None` al dataclass `Job` |
| `notifier.py` | Añadir métodos `send_timeout_alert()`, `send_cycle_summary()`, `send_daemon_lifecycle()`. Todos envueltos en try/except — errores SMTP logueados, nunca propagados |
| `pyproject.toml` | Bump de versión a 0.3.0, corregir build-backend, eliminar classifier de licencia obsoleto |

### Ficheros de tests

| Fichero | Cambios |
|---------|---------|
| `test_orchestrator.py` | Tests para `run_forever()` (conteo de loops, cálculo de pausa, SIGTERM durante sleep, SIGTERM durante ciclo), resolución de timeout por job, atomicidad de `_write_state()`, `_interruptible_sleep()` |
| `test_config.py` | Tests para parseo/validación de `LoopConfig`, resolución de contraseña `FROM_ENV:`, validación de timeout por job, flag `on_daemon_lifecycle` |
| `test_notifier.py` | Tests para los nuevos métodos `send_*`, manejo de fallos SMTP (no se propaga excepción) |
| `test_cli.py` | Fichero nuevo: tests para el parseo de argumentos del subcomando `daemon` |

### Ficheros de systemd

| Fichero | Acción |
|---------|--------|
| `systemd/jetbackup-remote.service` | Reemplazar (Type=simple, comando daemon) |
| `systemd/jetbackup-remote.timer` | Eliminar |

## Procedimiento de despliegue

1. Parar timer y servicio actuales en raspxer
2. Crear jobs MySQL via WHM en servidor02, servidor20, servidor30
3. Crear job Directories via WHM en central
4. Inventariar jobs de kvm313 via SSH directo
5. Registrar kvm313 en sshctx
6. Copiar código nuevo a `/opt/jetbackup-remote/`
7. Establecer variable de entorno `JETBACKUP_SMTP_PASSWORD` en la unit del servicio (via `Environment=` o credenciales de systemd)
8. Actualizar `/etc/jetbackup-remote/config.json` (nuevos jobs, kvm313, notificación, sección loop)
9. Crear `/var/lib/jetbackup-remote/`
10. Eliminar la unit del timer, instalar nueva unit del servicio
11. `systemctl daemon-reload && systemctl enable --now jetbackup-remote`
12. Verificar que se recibe el email de arranque del daemon
13. Monitorizar el primer ciclo via `jetbackup-remote status` y el log

## Versión

`0.2.0` → `0.3.0`
