# jetbackup-remote

## Propósito

Orquestador Python que serializa jobs JetBackup5 de múltiples servidores hacia un NAS RAID5 (Raspberry Pi).
Resuelve saturación del controlador USB (JMicron JMS567) cuando hay escrituras concurrentes.

## Arquitectura

- **stdlib puro** - cero dependencias externas
- **subprocess** para SSH (no paramiko)
- **JSON** para config
- **Sin threading** - la serialización es el objetivo
- **fcntl.flock** para lock file

## Estructura

```
src/jetbackup_remote/
  models.py        - Dataclasses y enums
  config.py        - Carga/validación JSON
  ssh.py           - Wrapper subprocess SSH
  jetbackup_api.py - Abstracción jetbackup5api
  orchestrator.py  - Motor FIFO + polling
  notifier.py      - Email smtplib
  cli.py           - argparse subcomandos
  logging_config.py - RotatingFileHandler
```

## Tests

```bash
python3 -m unittest discover -s tests
```

## Convenciones

- Código en inglés, documentación en español
- Tests obligatorios para cada módulo
- Commits en inglés con Co-Authored-By
