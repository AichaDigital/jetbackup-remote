# Instalacion

## Requisitos

- **Orquestador (Raspberry Pi):** Python 3.9+, acceso SSH a los servidores
- **Servidores JetBackup:** JetBackup5 con `jetbackup5api` CLI disponible

## 1. Instalar en la Raspberry Pi

```bash
# Clonar repositorio
git clone https://github.com/AichaDigital/jetbackup-remote.git
cd jetbackup-remote

# Instalar como paquete (opcional)
pip3 install .

# O usar directamente con PYTHONPATH
export PYTHONPATH=/ruta/a/jetbackup-remote/src
```

## 2. Configurar

```bash
# Crear directorio de configuracion
mkdir -p /etc/jetbackup-remote

# Copiar y editar config
cp config.example.json /etc/jetbackup-remote/config.json
nano /etc/jetbackup-remote/config.json
```

Campos a personalizar:

- `ssh_key`: ruta a la clave privada SSH
- `servers`: hostnames/IPs y puertos de los servidores JetBackup
- `jobs`: IDs de los backup jobs (obtener con `jetbackup5api -F listBackupJobs -O json`)
- `notification`: configuracion SMTP si se desean alertas por email

## 3. Configurar SSH en los servidores

En **cada servidor JetBackup**, instalar el filtro SSH:

```bash
# Copiar el gate script
scp server/jetbackup-ssh-gate.sh root@servidor:/usr/local/bin/
ssh root@servidor "chmod +x /usr/local/bin/jetbackup-ssh-gate.sh"
```

Agregar la clave publica de la Pi al authorized_keys del servidor:

```bash
# En el servidor, editar /root/.ssh/authorized_keys y agregar:
command="/usr/local/bin/jetbackup-ssh-gate.sh",no-port-forwarding,no-X11-forwarding,no-agent-forwarding,no-pty ssh-ed25519 AAAA... root@raspberrypinas
```

## 4. Verificar conectividad

```bash
# Test SSH + JetBackup API
jetbackup-remote -c /etc/jetbackup-remote/config.json test

# Validar configuracion
jetbackup-remote -c /etc/jetbackup-remote/config.json validate

# Dry run (no ejecuta backups)
jetbackup-remote -c /etc/jetbackup-remote/config.json run --dry-run
```

## 5. Instalar timer systemd

```bash
# Copiar ficheros systemd
cp systemd/jetbackup-remote.service /etc/systemd/system/
cp systemd/jetbackup-remote.timer /etc/systemd/system/

# Editar la hora si es necesario
# Por defecto: 02:00 diario
nano /etc/systemd/system/jetbackup-remote.timer

# Activar
systemctl daemon-reload
systemctl enable --now jetbackup-remote.timer

# Verificar
systemctl list-timers | grep jetbackup
```

## 6. Ejecucion manual

```bash
# Ejecutar todos los jobs
jetbackup-remote -c /etc/jetbackup-remote/config.json run

# Solo un servidor
jetbackup-remote -c /etc/jetbackup-remote/config.json run --server server1

# Solo un job especifico
jetbackup-remote -c /etc/jetbackup-remote/config.json run --job aaaaaaaaaaaaaaaaaaaaaaaa

# Ver estado actual
jetbackup-remote -c /etc/jetbackup-remote/config.json status
```

## Logs

```bash
# Logs del orquestador
tail -f /var/log/jetbackup-remote.log

# Logs del timer systemd
journalctl -u jetbackup-remote.service -f

# Logs del gate SSH (en cada servidor)
journalctl -t jetbackup-ssh-gate
```
