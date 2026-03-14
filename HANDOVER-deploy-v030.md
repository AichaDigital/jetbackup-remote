# HANDOVER — Despliegue jetbackup-remote v0.3.0

**Fecha:** 2026-03-14
**Estado:** Código completo, pendiente despliegue
**Branch:** main (mergeado desde worktree-v0.3.0-daemon-loop)
**Repo:** ~/development/jetbackup-remote

## Qué se hizo

v0.3.0 implementada y mergeada: daemon loop con pausa calculada, per-job timeout con abort, notificaciones email, fichero de estado. 261 tests pasan, 44 nuevos.

## Qué falta — Tarea 8: Despliegue

### Paso 1: Inventariar kvm313

```bash
ssh -p 51514 -i ~/.ssh/id_rsa root@kvm313.xerintel.com "jetbackup5api -F listBackupJobs -O json"
ssh -p 51514 -i ~/.ssh/id_rsa root@kvm313.xerintel.com "jetbackup5api -F listDestinations -O json"
```

Anotar: destination_id para Raspberry, job_ids existentes.

### Paso 2: Registrar kvm313 en sshctx

```bash
sshctx add kvm313 \
  --host kvm313.xerintel.com \
  --port 51514 \
  --user root \
  --key ~/.ssh/id_rsa \
  --panel directadmin \
  --client Xerintel \
  --role hosting
```

### Paso 3: Crear jobs MySQL via WHM

En servidor02, servidor20, servidor30 — WHM > JetBackup 5 > Backup Jobs > Create:

- **Type:** Database
- **Destination:** Raspberry (la existente)
- **Schedule:** Daily (1x/día)
- **Retention:** 3 (mínima — Raspberry es copia secundaria)

Anotar los 3 job_ids creados.

### Paso 4: Crear job Directories en central via WHM

- **Type:** Directories
- **Destination:** Rasp (la existente)
- **Include list:** copiar del job "Directories" existente hacia Xerbackup02
- **Schedule:** Daily

Anotar el job_id.

### Paso 5: Parar servicios en raspxer

```bash
# En raspxer:
systemctl stop jetbackup-remote.timer
systemctl stop jetbackup-remote.service
systemctl disable jetbackup-remote.timer
```

### Paso 6: Desplegar código

```bash
# Desde local:
rsync -av --delete ~/development/jetbackup-remote/src/ raspxer:/opt/jetbackup-remote/src/ -e "ssh -p 2222"
rsync -av ~/development/jetbackup-remote/systemd/jetbackup-remote.service raspxer:/etc/systemd/system/ -e "ssh -p 2222"
```

### Paso 7: Preparar entorno en raspxer

```bash
# En raspxer:
mkdir -p /var/lib/jetbackup-remote

# Crear fichero de entorno con contraseña SMTP (de ~/claude/.env CASTRIS_MAIL_RELAY_PASS):
echo 'JETBACKUP_SMTP_PASSWORD=ughQP6LzjXJkNvTA7zBF' > /etc/jetbackup-remote/env
chmod 600 /etc/jetbackup-remote/env
```

### Paso 8: Actualizar config.json en raspxer

Editar `/etc/jetbackup-remote/config.json`:

1. **Añadir kvm313** a `servers`:

```json
"kvm313": {
    "host": "kvm313.xerintel.com",
    "port": 51514,
    "user": "root",
    "destination_id": "<DEST_ID_DE_PASO_1>"
}
```

2. **Añadir nuevos jobs** al array `jobs` (IDs de pasos 1, 3 y 4):

```json
{"job_id": "<ID_MYSQL_SVR02>", "server": "servidor02", "label": "Mysql-Ras", "type": "database", "priority": 0, "timeout": 7200},
{"job_id": "<ID_MYSQL_SVR20>", "server": "servidor20", "label": "Mysql-Ras", "type": "database", "priority": 0, "timeout": 7200},
{"job_id": "<ID_MYSQL_SVR30>", "server": "servidor30", "label": "Mysql-Ras", "type": "database", "priority": 0, "timeout": 7200},
{"job_id": "<ID_DIRS_CENTRAL>", "server": "central", "label": "Directories-Ras", "type": "directories", "priority": 0},
{"job_id": "<ID_JOB1_KVM313>", "server": "kvm313", "label": "<LABEL>", "type": "<TYPE>", "priority": 0},
{"job_id": "<ID_JOB2_KVM313>", "server": "kvm313", "label": "<LABEL>", "type": "<TYPE>", "priority": 0}
```

3. **Añadir sección `loop`:**

```json
"loop": {
    "target_interval": 86400,
    "min_pause": 3600
}
```

4. **Actualizar `orchestrator`:**

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

5. **Actualizar `notification`:**

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

### Paso 9: Instalar y arrancar

```bash
# En raspxer:
rm /etc/systemd/system/jetbackup-remote.timer
systemctl daemon-reload
systemctl enable --now jetbackup-remote
```

### Paso 10: Verificar

```bash
# Verificar que el email "daemon started" llega a abdel@xerintel.es
# Verificar logs:
journalctl -u jetbackup-remote -f
# Verificar estado:
jetbackup-remote -c /etc/jetbackup-remote/config.json status
```

## Documentación

- **Spec:** `docs/superpowers/specs/2026-03-13-daemon-loop-reliability-design.md`
- **Plan:** `docs/superpowers/plans/2026-03-14-daemon-loop-reliability-plan.md`
