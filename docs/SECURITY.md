# Seguridad

## Modelo de amenazas

El orquestador ejecuta como root en la Raspberry Pi y tiene acceso SSH a servidores de produccion con JetBackup5. El modelo de seguridad minimiza el impacto si la Pi es comprometida.

## Capas de proteccion

### 1. Clave SSH con restriccion de comando

La clave SSH de la Pi esta restringida en cada servidor mediante `command=` en authorized_keys:

```
command="/usr/local/bin/jetbackup-ssh-gate.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA...
```

Esto significa que incluso si un atacante obtiene la clave privada de la Pi:

- **No puede abrir shell** en los servidores
- **No puede ejecutar comandos arbitrarios**
- **No puede crear tuneles** ni reenviar puertos
- **Solo puede ejecutar** funciones de la API JetBackup5 autorizadas

### 2. Gate script con whitelist

El script `jetbackup-ssh-gate.sh` valida `SSH_ORIGINAL_COMMAND` contra una whitelist:

```
jetbackup5api -F getBackupJob ...
jetbackup5api -F runBackupJobManually ...
jetbackup5api -F listBackupJobs
jetbackup5api -F listQueueGroups ...
jetbackup5api -F stopQueueGroup ...
echo ...
```

Todo lo demas es **DENIED** y registrado via syslog.

### 3. Opciones SSH deshabilitadas

- `no-port-forwarding`: impide tuneles SSH
- `no-X11-forwarding`: impide forwarding X11
- `no-agent-forwarding`: impide reutilizar el agente SSH
- `no-pty`: impide obtener terminal interactiva

### 4. Systemd hardening

El servicio systemd usa:

- `ProtectSystem=strict`: sistema de ficheros solo lectura excepto paths autorizados
- `PrivateTmp=true`: /tmp aislado
- `NoNewPrivileges=true`: sin escalada de privilegios
- `ProtectHome=true`: /home inaccesible
- `ProtectKernelModules=true`: no puede cargar modulos
- `ProtectKernelTunables=true`: no puede modificar sysctl

## Auditoria

### Verificar gate en servidores

```bash
# Intentar comando no autorizado desde la Pi
ssh -i /root/.ssh/id_ed25519 -p 51514 root@servidor "ls /"
# Esperado: "ERROR: Command not allowed: ls /"

# Verificar en syslog del servidor
journalctl -t jetbackup-ssh-gate | tail -5
```

### Verificar restricciones authorized_keys

```bash
# En cada servidor
grep "jetbackup-ssh-gate" /root/.ssh/authorized_keys
# Verificar que tiene command=, no-port-forwarding, etc.
```

## Rotacion de claves

Si se sospecha compromiso de la clave SSH:

```bash
# 1. En la Pi, generar nueva clave
ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519_new -N ""

# 2. En cada servidor, reemplazar la clave publica vieja
# (usar acceso alternativo, no la clave comprometida)

# 3. Verificar acceso con la nueva clave
jetbackup-remote test

# 4. Eliminar la clave vieja
rm /root/.ssh/id_ed25519_old*
```

## Buenas practicas

- **No almacenar credenciales SMTP** en el config.json si el servidor de correo acepta relay sin autenticacion (localhost:25)
- **Limitar IPs** en el firewall de los servidores: solo la IP de la Pi deberia poder conectar por SSH con esta clave
- **Monitorizar los logs** del gate: un DENIED inesperado puede indicar intento de acceso no autorizado
- **Revisar periodicamente** los authorized_keys de los servidores para asegurar que las restricciones siguen activas
