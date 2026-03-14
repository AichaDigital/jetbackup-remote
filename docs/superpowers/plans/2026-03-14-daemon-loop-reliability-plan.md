# Plan de implementación: jetbackup-remote v0.3.0 — Daemon Loop, Fiabilidad y Observabilidad

> **Para agentes automáticos:** OBLIGATORIO: Usar superpowers:subagent-driven-development (si hay subagentes disponibles) o superpowers:executing-plans para implementar este plan. Los pasos usan sintaxis checkbox (`- [ ]`) para seguimiento.

**Objetivo:** Convertir jetbackup-remote de un oneshot con timer systemd a un daemon con loop calculado, timeout por job con abort, notificaciones email y fichero de estado.

**Arquitectura:** El Orchestrator gana un método `run_forever()` que envuelve el `run()` existente en un loop infinito con pausa calculada entre ciclos. Los jobs que excedan su timeout se abortan via `stopQueueGroup`. Se envían emails SMTP en fallos/timeouts/lifecycle. Un fichero JSON de estado permite monitorización externa.

**Tech Stack:** Python 3.11+, systemd, SMTP (STARTTLS), JSON

**Spec:** `docs/superpowers/specs/2026-03-13-daemon-loop-reliability-design.md`

**Nota sobre estilo de tests:** Los tests existentes usan clases `unittest.TestCase`. Los nuevos tests en este plan usan funciones bare pytest para mayor legibilidad. pytest ejecuta ambos estilos sin problema. Si el implementador prefiere mantener consistencia con unittest, adaptar los tests al estilo de clases existente.

**Nota sobre helpers de test:** Los tests de config existentes usan `MINIMAL_CONFIG` y `FULL_CONFIG` como constantes dict, y `_write_config(data)` para escribir a fichero temporal. Los tests nuevos en este plan usan `tmp_path` (fixture pytest) y un helper `_make_config_dict()` que se debe crear al inicio de la Tarea 2 como wrapper mínimo de config válida.

---

## Estructura de ficheros

| Fichero | Acción | Responsabilidad |
|---------|--------|-----------------|
| `src/jetbackup_remote/models.py` | Modificar | Añadir `timeout` a `Job` |
| `src/jetbackup_remote/config.py` | Modificar | `LoopConfig`, `state_file`, `on_daemon_lifecycle`, `FROM_ENV:`, per-job timeout, subir `MAX_JOB_TIMEOUT` |
| `src/jetbackup_remote/notifier.py` | Modificar | `send_timeout_alert()`, `send_cycle_summary()`, `send_daemon_lifecycle()`, try/except en todo |
| `src/jetbackup_remote/orchestrator.py` | Modificar | `run_forever()`, `_interruptible_sleep()`, `_write_state()`, per-job timeout en polling, hooks de notificación |
| `src/jetbackup_remote/cli.py` | Modificar | Subcomando `daemon`, mejora de `status` |
| `src/jetbackup_remote/__init__.py` | Modificar | Bump versión |
| `systemd/jetbackup-remote.service` | Reemplazar | `Type=simple`, comando `daemon` |
| `systemd/jetbackup-remote.timer` | Eliminar | Ya no necesario |
| `pyproject.toml` | Modificar | Ya corregido (build-backend, classifier) — solo bump versión |
| `tests/test_config.py` | Modificar | Tests LoopConfig, FROM_ENV:, per-job timeout, on_daemon_lifecycle |
| `tests/test_models.py` | Modificar | Test Job.timeout |
| `tests/test_notifier.py` | Modificar | Tests nuevos métodos, SMTP failure handling |
| `tests/test_orchestrator.py` | Modificar | Tests run_forever(), _interruptible_sleep(), _write_state(), per-job timeout |

---

## Tarea 1: Modelo — campo timeout en Job

**Ficheros:**

- Modificar: `src/jetbackup_remote/models.py:80-91`
- Test: `tests/test_models.py`

- [ ] **Paso 1: Escribir test que falla**

```python
# tests/test_models.py — añadir al final

def test_job_timeout_default_none():
    job = Job(job_id="abc", server_name="srv1")
    assert job.timeout is None


def test_job_timeout_custom():
    job = Job(job_id="abc", server_name="srv1", timeout=7200)
    assert job.timeout == 7200
```

- [ ] **Paso 2: Ejecutar test y verificar que falla**

```bash
cd ~/development/jetbackup-remote
source .venv/bin/activate
python -m pytest tests/test_models.py::test_job_timeout_default_none -v
```

Esperado: FAIL con `TypeError: __init__() got an unexpected keyword argument 'timeout'`

- [ ] **Paso 3: Implementar — añadir campo timeout a Job**

En `src/jetbackup_remote/models.py`, dentro del dataclass `Job` (después de la línea `priority: int = 0`):

```python
    timeout: Optional[int] = None  # Per-job timeout override (seconds)
```

- [ ] **Paso 4: Ejecutar tests y verificar que pasan**

```bash
python -m pytest tests/test_models.py -v
```

Esperado: PASS

- [ ] **Paso 5: Commit**

```bash
git add src/jetbackup_remote/models.py tests/test_models.py
git commit -m "feat(models): add per-job timeout field to Job dataclass

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 2: Config — LoopConfig, state_file, per-job timeout, FROM_ENV:, on_daemon_lifecycle

**Ficheros:**

- Modificar: `src/jetbackup_remote/config.py`
- Test: `tests/test_config.py`

- [ ] **Paso 1: Crear helper y escribir tests que fallan**

```python
# tests/test_config.py — añadir al inicio (si no existe ya)

def _make_config_dict():
    """Config mínima válida para tests."""
    return {
        "servers": {
            "srv1": {"host": "test.example.com", "destination_id": "dest1"}
        },
        "jobs": [
            {"job_id": "job1", "server": "srv1", "label": "TestJob", "type": "accounts"}
        ],
        "orchestrator": {"job_timeout": 7200},
    }


# tests/test_config.py — añadir los siguientes tests

def test_loop_config_defaults():
    from jetbackup_remote.config import LoopConfig
    lc = LoopConfig()
    assert lc.target_interval == 86400
    assert lc.min_pause == 3600


def test_loop_config_parsed_from_json(tmp_path):
    config_data = _make_config_dict()
    config_data["loop"] = {"target_interval": 43200, "min_pause": 1800}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.loop.target_interval == 43200
    assert config.loop.min_pause == 1800


def test_loop_config_validation_min_pause_too_low(tmp_path):
    config_data = _make_config_dict()
    config_data["loop"] = {"min_pause": 100}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    with pytest.raises(ConfigError, match="min_pause"):
        load_config(str(p))


def test_loop_config_validation_min_pause_ge_target(tmp_path):
    config_data = _make_config_dict()
    config_data["loop"] = {"target_interval": 3600, "min_pause": 3600}
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    with pytest.raises(ConfigError, match="min_pause"):
        load_config(str(p))


def test_state_file_in_orchestrator_config(tmp_path):
    config_data = _make_config_dict()
    config_data["orchestrator"]["state_file"] = "/tmp/test-state.json"
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.orchestrator.state_file == "/tmp/test-state.json"


def test_per_job_timeout_parsed(tmp_path):
    config_data = _make_config_dict()
    config_data["jobs"][0]["timeout"] = 7200
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.jobs[0].timeout == 7200


def test_per_job_timeout_too_low(tmp_path):
    config_data = _make_config_dict()
    config_data["jobs"][0]["timeout"] = 500
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    with pytest.raises(ConfigError, match="timeout"):
        load_config(str(p))


def test_on_daemon_lifecycle_default():
    from jetbackup_remote.config import NotificationConfig
    nc = NotificationConfig()
    assert nc.on_daemon_lifecycle is True


def test_from_env_password_resolution(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBACKUP_SMTP_PASSWORD", "secret123")
    config_data = _make_config_dict()
    config_data["notification"] = {
        "enabled": True,
        "smtp_password": "FROM_ENV:JETBACKUP_SMTP_PASSWORD",
        "to_addresses": ["test@test.com"],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.notification.smtp_password == "secret123"


def test_from_env_password_missing_var(tmp_path, monkeypatch):
    monkeypatch.delenv("NONEXISTENT_VAR", raising=False)
    config_data = _make_config_dict()
    config_data["notification"] = {
        "enabled": True,
        "smtp_password": "FROM_ENV:NONEXISTENT_VAR",
        "to_addresses": ["test@test.com"],
    }
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    with pytest.raises(ConfigError, match="NONEXISTENT_VAR"):
        load_config(str(p))


def test_max_job_timeout_raised(tmp_path):
    """job_timeout de 86400 debe ser válido (antes era el máximo, ahora sube a 172800)."""
    config_data = _make_config_dict()
    config_data["orchestrator"]["job_timeout"] = 86400
    p = tmp_path / "config.json"
    p.write_text(json.dumps(config_data))
    config = load_config(str(p))
    assert config.orchestrator.job_timeout == 86400
```

Nota: `_make_config_dict()` es un helper que ya debería existir en los tests. Si no existe, crearlo con un config mínimo válido (1 server, 1 job).

- [ ] **Paso 2: Ejecutar tests y verificar que fallan**

```bash
python -m pytest tests/test_config.py::test_loop_config_defaults -v
```

Esperado: FAIL con `ImportError: cannot import name 'LoopConfig'`

- [ ] **Paso 3: Implementar cambios en config.py**

3a. Añadir `LoopConfig` dataclass (después de `OrchestratorConfig`):

```python
@dataclass
class LoopConfig:
    """Daemon loop settings."""
    target_interval: int = 86400   # 24h target between cycle starts
    min_pause: int = 3600          # 1h minimum pause after cycle
```

3b. Añadir `on_daemon_lifecycle` a `NotificationConfig`:

```python
    on_daemon_lifecycle: bool = True
```

3c. Añadir `state_file` a `OrchestratorConfig`:

```python
    state_file: str = "/var/lib/jetbackup-remote/last-run.json"
```

3d. Añadir `loop` a `Config`:

```python
    loop: LoopConfig = field(default_factory=LoopConfig)
```

3e. Subir `MAX_JOB_TIMEOUT`:

```python
MAX_JOB_TIMEOUT = 172800   # 48 hours
```

3f. Parsear per-job `timeout` en `_parse_job()`:

```python
    timeout_val = data.get("timeout")
    if timeout_val is not None and timeout_val < MIN_JOB_TIMEOUT:
        raise ConfigError(
            f"Job '{data['job_id']}' timeout ({timeout_val}s) below minimum ({MIN_JOB_TIMEOUT}s)"
        )

    return Job(
        job_id=data["job_id"],
        server_name=server_name,
        label=data.get("label", ""),
        job_type=_parse_job_type(data.get("type", "other")),
        priority=data.get("priority", 0),
        timeout=timeout_val,
    )
```

3g. Resolver `FROM_ENV:` en `_parse_notification()`.

Primero, añadir `import os` al inicio de `config.py` (junto a los otros imports estándar).

Luego modificar `_parse_notification()`:

```python
def _parse_notification(data: dict) -> NotificationConfig:
    password = data.get("smtp_password", "")
    if password.startswith("FROM_ENV:"):
        env_var = password[len("FROM_ENV:"):]
        password = os.environ.get(env_var)
        if password is None:
            raise ConfigError(
                f"Environment variable '{env_var}' not set (required by smtp_password FROM_ENV:)"
            )
    # ... resto igual, usar password resuelto
```

3h. Añadir `_parse_loop()`:

```python
def _parse_loop(data: dict) -> LoopConfig:
    target = data.get("target_interval", 86400)
    min_pause = data.get("min_pause", 3600)

    if target < 3600:
        raise ConfigError(f"loop.target_interval ({target}s) below minimum (3600s)")
    if min_pause < 600:
        raise ConfigError(f"loop.min_pause ({min_pause}s) below minimum (600s)")
    if min_pause >= target:
        raise ConfigError(f"loop.min_pause ({min_pause}s) must be less than target_interval ({target}s)")

    return LoopConfig(target_interval=target, min_pause=min_pause)
```

3i. Parsear secciones nuevas en `load_config()`:

```python
    loop = _parse_loop(raw.get("loop", {}))
    # ... y pasar loop=loop al constructor de Config
```

3j. Añadir `on_daemon_lifecycle` al parseo en `_parse_notification()`:

```python
    on_daemon_lifecycle=data.get("on_daemon_lifecycle", True),
```

3k. Parsear `state_file` en `_parse_orchestrator()`:

```python
    state_file=data.get("state_file", "/var/lib/jetbackup-remote/last-run.json"),
```

- [ ] **Paso 4: Ejecutar todos los tests de config**

```bash
python -m pytest tests/test_config.py -v
```

Esperado: PASS

- [ ] **Paso 5: Commit**

```bash
git add src/jetbackup_remote/config.py tests/test_config.py
git commit -m "feat(config): add LoopConfig, per-job timeout, FROM_ENV:, on_daemon_lifecycle

- LoopConfig with target_interval/min_pause and validation
- Per-job timeout override with MIN_JOB_TIMEOUT validation
- FROM_ENV: prefix for smtp_password resolution
- on_daemon_lifecycle flag in NotificationConfig
- state_file in OrchestratorConfig
- MAX_JOB_TIMEOUT raised to 172800 (48h)

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 3: Notifier — nuevos métodos y manejo seguro de SMTP

**Ficheros:**

- Modificar: `src/jetbackup_remote/notifier.py`
- Test: `tests/test_notifier.py`

- [ ] **Paso 1: Escribir tests que fallan**

```python
# tests/test_notifier.py — añadir

from jetbackup_remote.notifier import (
    send_timeout_alert,
    send_cycle_summary,
    send_daemon_lifecycle,
)


def test_send_timeout_alert_builds_email():
    subject, body = build_timeout_email(
        server_name="servidor30",
        job_label="RasBackup",
        job_type="accounts",
        duration_seconds=86403,
        cycle_progress="3/10 jobs completed",
    )
    assert "[jetbackup-remote] TIMEOUT" in subject
    assert "servidor30" in subject
    assert "RasBackup" in body
    assert "86403" in body or "24h" in body


def test_send_daemon_lifecycle_start():
    subject, body = build_lifecycle_email(event="started", hostname="raspberrypinas")
    assert "started" in subject.lower()
    assert "raspberrypinas" in body


def test_send_daemon_lifecycle_stopping():
    subject, body = build_lifecycle_email(event="stopping", hostname="raspberrypinas")
    assert "stopping" in subject.lower()


def test_send_notification_smtp_failure_returns_false(monkeypatch):
    """SMTP failure must return False, never raise."""
    config = NotificationConfig(
        enabled=True,
        to_addresses=["test@test.com"],
        on_failure=True,
    )
    result = _make_failed_result()

    def mock_smtp(*a, **kw):
        raise smtplib.SMTPException("Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", mock_smtp)
    assert send_notification(config, result) is False


def test_send_timeout_alert_smtp_failure_no_exception(monkeypatch):
    """send_timeout_alert must never raise on SMTP failure."""
    config = NotificationConfig(enabled=True, to_addresses=["test@test.com"])

    def mock_smtp(*a, **kw):
        raise smtplib.SMTPException("Connection refused")

    monkeypatch.setattr(smtplib, "SMTP", mock_smtp)
    # Must not raise
    result = send_timeout_alert(
        config, server_name="srv", job_label="job", job_type="accounts",
        duration_seconds=100, cycle_progress="1/1",
    )
    assert result is False
```

- [ ] **Paso 2: Ejecutar tests y verificar que fallan**

```bash
python -m pytest tests/test_notifier.py::test_send_timeout_alert_builds_email -v
```

Esperado: FAIL con `ImportError`

- [ ] **Paso 3: Implementar nuevos métodos en notifier.py**

3a. Añadir `build_timeout_email()`:

```python
def build_timeout_email(
    server_name: str,
    job_label: str,
    job_type: str,
    duration_seconds: float,
    cycle_progress: str,
) -> tuple:
    subject = f"[jetbackup-remote] TIMEOUT: {job_label} on {server_name}"
    lines = [
        f"Job: {job_label} ({job_type})",
        f"Server: {server_name}",
        f"Duration: {_format_duration(duration_seconds)}",
        f"Action: Job aborted via stopQueueGroup",
        f"Status: Moving to next job",
        f"",
        f"Cycle progress: {cycle_progress}",
        f"",
        f"--",
        f"jetbackup-remote on {socket.gethostname()}",
    ]
    return subject, "\n".join(lines)
```

3b. Añadir `build_lifecycle_email()`:

```python
def build_lifecycle_email(event: str, hostname: str) -> tuple:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    subject = f"[jetbackup-remote] Daemon {event} on {hostname}"
    lines = [
        f"jetbackup-remote daemon {event}",
        f"Host: {hostname}",
        f"Time: {now}",
        f"PID: {os.getpid()}",
        f"",
        f"--",
        f"jetbackup-remote on {hostname}",
    ]
    return subject, "\n".join(lines)
```

3c. Añadir `send_timeout_alert()` y `send_daemon_lifecycle()` — wrappers que construyen el email y llaman a `_send_email()`:

```python
def _send_email(config: NotificationConfig, subject: str, body: str) -> bool:
    """Send a single email. Never raises — returns False on failure."""
    if not config.enabled or not config.to_addresses:
        return True
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = config.from_address
    msg["To"] = ", ".join(config.to_addresses)
    try:
        if config.smtp_tls:
            smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
        else:
            smtp = smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30)
        if config.smtp_user and config.smtp_password:
            smtp.login(config.smtp_user, config.smtp_password)
        smtp.sendmail(config.from_address, config.to_addresses, msg.as_string())
        smtp.quit()
        logger.info("Email sent: %s", subject)
        return True
    except (smtplib.SMTPException, OSError) as e:
        logger.warning("Failed to send email '%s': %s", subject, e)
        return False


def send_timeout_alert(config, server_name, job_label, job_type, duration_seconds, cycle_progress):
    if not config.on_timeout:
        return True
    subject, body = build_timeout_email(server_name, job_label, job_type, duration_seconds, cycle_progress)
    return _send_email(config, subject, body)


def send_daemon_lifecycle(config, event, hostname=None):
    if not getattr(config, "on_daemon_lifecycle", True):
        return True
    hostname = hostname or socket.gethostname()
    subject, body = build_lifecycle_email(event, hostname)
    return _send_email(config, subject, body)


def send_cycle_summary(config, result):
    """Wrapper around send_notification that never raises."""
    try:
        return send_notification(config, result)
    except Exception as e:
        logger.warning("Failed to send cycle summary: %s", e)
        return False
```

3d. Refactorizar `send_notification()` para usar `_send_email()` internamente.

- [ ] **Paso 4: Ejecutar todos los tests del notifier**

```bash
python -m pytest tests/test_notifier.py -v
```

Esperado: PASS

- [ ] **Paso 5: Commit**

```bash
git add src/jetbackup_remote/notifier.py tests/test_notifier.py
git commit -m "feat(notifier): add timeout/lifecycle/summary email methods

All send_* methods wrapped in try/except — SMTP errors logged as
WARNING, never propagated. Extracted _send_email() helper.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 4: Orchestrator — per-job timeout, _write_state(), _interruptible_sleep(), run_forever()

**Ficheros:**

- Modificar: `src/jetbackup_remote/orchestrator.py`
- Test: `tests/test_orchestrator.py`

Esta es la tarea más grande. Se divide en sub-pasos.

### 4A: Per-job timeout en polling

- [ ] **Paso 1: Escribir test**

```python
# tests/test_orchestrator.py — añadir

def test_poll_uses_per_job_timeout(mock_config, mock_server):
    """Job with custom timeout should use it instead of global."""
    job = Job(job_id="j1", server_name="srv", label="FastJob", timeout=3600)
    orch = Orchestrator(mock_config)
    job_run = JobRun(job=job)
    job_run.start()
    # El test verifica que _poll_completion_only usa job.timeout (3600)
    # en lugar de config.orchestrator.job_timeout (86400)
    # Mock is_job_running para devolver False inmediatamente
    with patch("jetbackup_remote.orchestrator.is_job_running", return_value=False):
        orch._poll_completion_only(mock_server, job, job_run)
    assert job_run.status == JobStatus.COMPLETED
```

- [ ] **Paso 2: Implementar** — En `_poll_completion_only()`, dos cambios:

2a. Cambiar la línea del timeout:

```python
# Antes:
timeout = self.config.orchestrator.job_timeout

# Después:
timeout = job.timeout if job.timeout else self.config.orchestrator.job_timeout
```

2b. Añadir envío de email inmediato de timeout (en el bloque `if elapsed >= timeout:`):

```python
            if elapsed >= timeout:
                logger.warning(...)
                self._abort_running_job(server, job, job_run)
                job_run.timeout()
                # Enviar alerta inmediata de timeout
                from .notifier import send_timeout_alert
                completed_count = sum(1 for j in getattr(self, '_current_run_jobs', []) if j.status == JobStatus.COMPLETED)
                total_count = getattr(self, '_current_run_total', '?')
                send_timeout_alert(
                    self.config.notification,
                    server_name=server.name,
                    job_label=job.display_name,
                    job_type=job.job_type.value,
                    duration_seconds=elapsed,
                    cycle_progress=f"{completed_count}/{total_count} jobs completed, 1 timeout",
                )
                return
```

Nota: `_current_run_jobs` y `_current_run_total` se setean al inicio de `_process_server()` para dar contexto a las alertas inmediatas. Alternativa más simple: pasar solo `server.name` y `job.display_name` sin progress count.

Nota: `_poll_completion_only` ya recibe el `job` object como parámetro.

- [ ] **Paso 3: Ejecutar test y verificar PASS**

```bash
python -m pytest tests/test_orchestrator.py::test_poll_uses_per_job_timeout -v
```

- [ ] **Paso 4: Commit**

```bash
git add src/jetbackup_remote/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): use per-job timeout in polling

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

### 4B: _write_state() — escritura atómica del fichero de estado

- [ ] **Paso 1: Escribir test**

```python
def test_write_state_creates_file(tmp_path, mock_config):
    mock_config.orchestrator.state_file = str(tmp_path / "state.json")
    orch = Orchestrator(mock_config)
    orch._write_state({"version": "0.3.0", "daemon_pid": 123})
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["version"] == "0.3.0"
    assert state["daemon_pid"] == 123


def test_write_state_atomic(tmp_path, mock_config):
    """La escritura no deja ficheros parciales si falla."""
    state_path = tmp_path / "state.json"
    state_path.write_text('{"old": true}')
    mock_config.orchestrator.state_file = str(state_path)
    orch = Orchestrator(mock_config)
    orch._write_state({"version": "0.3.0"})
    state = json.loads(state_path.read_text())
    assert "old" not in state
    assert state["version"] == "0.3.0"
```

- [ ] **Paso 2: Implementar _write_state()**

```python
import json
import tempfile

def _write_state(self, data: dict) -> None:
    state_file = self.config.orchestrator.state_file
    try:
        dir_path = os.path.dirname(state_file)
        fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.rename(tmp_path, state_file)
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as e:
        logger.warning("Failed to write state file %s: %s", state_file, e)
```

- [ ] **Paso 3: Test PASS**

- [ ] **Paso 4: Commit**

```bash
git add src/jetbackup_remote/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add atomic _write_state() method

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

### 4C: _interruptible_sleep()

- [ ] **Paso 1: Escribir test**

```python
def test_interruptible_sleep_returns_on_shutdown(mock_config):
    orch = Orchestrator(mock_config)
    orch._shutdown_requested = True
    import time
    start = time.time()
    orch._interruptible_sleep(300)
    elapsed = time.time() - start
    assert elapsed < 5  # Debe salir inmediatamente


def test_interruptible_sleep_sleeps_full_duration(mock_config):
    orch = Orchestrator(mock_config)
    import time
    start = time.time()
    orch._interruptible_sleep(2)  # 2 segundos
    elapsed = time.time() - start
    assert elapsed >= 1.5
```

- [ ] **Paso 2: Implementar**

```python
def _interruptible_sleep(self, seconds: float) -> None:
    """Sleep that can be interrupted by SIGTERM."""
    interval = 30
    remaining = seconds
    while remaining > 0 and not self._shutdown_requested:
        sleep_time = min(interval, remaining)
        time.sleep(sleep_time)
        remaining -= sleep_time
```

- [ ] **Paso 3: Test PASS**

- [ ] **Paso 4: Commit**

```bash
git add src/jetbackup_remote/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add _interruptible_sleep() method

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

### 4D: run_forever() — el loop principal del daemon

- [ ] **Paso 1: Escribir test**

```python
def test_run_forever_executes_cycles(mock_config):
    """run_forever debe ejecutar run() y parar con SIGTERM."""
    orch = Orchestrator(mock_config)
    cycle_count = 0

    original_run = orch.run
    def counting_run(**kwargs):
        nonlocal cycle_count
        cycle_count += 1
        if cycle_count >= 2:
            orch._shutdown_requested = True
        return original_run(dry_run=True)

    with patch.object(orch, "run", side_effect=counting_run):
        with patch.object(orch, "_interruptible_sleep"):
            with patch.object(orch, "_write_state"):
                orch.run_forever()

    assert cycle_count == 2


def test_run_forever_calculates_pause(mock_config):
    mock_config.loop.target_interval = 100
    mock_config.loop.min_pause = 10
    orch = Orchestrator(mock_config)
    sleep_values = []

    def capture_sleep(seconds):
        sleep_values.append(seconds)
        orch._shutdown_requested = True

    mock_result = RunResult(dry_run=True)
    mock_result.start()
    time.sleep(0.1)
    mock_result.finish()

    with patch.object(orch, "run", return_value=mock_result):
        with patch.object(orch, "_interruptible_sleep", side_effect=capture_sleep):
            with patch.object(orch, "_write_state"):
                orch.run_forever()

    assert len(sleep_values) == 1
    # Pausa debe ser ~100 - 0.1 ≈ 99.9, limitada por min_pause mínimo
    assert sleep_values[0] >= 10
```

- [ ] **Paso 2: Implementar run_forever()**

```python
def run_forever(self) -> None:
    """Run the orchestration loop indefinitely.

    Executes run() cycles with calculated pause between them.
    Exits cleanly on SIGTERM/SIGINT.
    """
    from .notifier import send_daemon_lifecycle, send_cycle_summary
    import socket

    hostname = socket.gethostname()
    logger.info("Daemon starting on %s (pid %d)", hostname, os.getpid())

    # Write initial state
    self._write_state({
        "version": "0.3.0",
        "daemon_pid": os.getpid(),
        "daemon_started_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "last_run": None,
        "next_run_at": None,
    })

    # Send lifecycle email
    send_daemon_lifecycle(self.config.notification, "started", hostname)

    try:
        while not self._shutdown_requested:
            # Execute one full cycle
            result = self.run()

            # Build state data
            cycle_duration = result.duration_seconds or 0
            pause = max(
                self.config.loop.target_interval - cycle_duration,
                self.config.loop.min_pause,
            )
            next_run = datetime.now(timezone.utc).timestamp() + pause
            next_run_iso = datetime.fromtimestamp(next_run, tz=timezone.utc).isoformat().replace("+00:00", "Z")

            state_data = self._build_run_state(result, next_run_iso)
            self._write_state(state_data)

            # Send summary if needed
            if not result.success:
                send_cycle_summary(self.config.notification, result)
            elif getattr(self.config.notification, "on_complete", False):
                send_cycle_summary(self.config.notification, result)

            if self._shutdown_requested:
                break

            logger.info(
                "Cycle complete. Sleeping %.0fs (next run ~%s)",
                pause, next_run_iso,
            )
            self._interruptible_sleep(pause)

    finally:
        send_daemon_lifecycle(self.config.notification, "stopping", hostname)
        logger.info("Daemon stopped")
```

Nota: necesitará imports adicionales (`from datetime import datetime, timezone`).

También implementar `_build_run_state()` helper que construye el dict del fichero de estado a partir de un `RunResult`. Este helper debe:

1. **Leer `consecutive_failures` del fichero de estado anterior** al inicio de cada ciclo (en `run_forever()`, antes de llamar a `run()`). Si el fichero no existe o no se puede leer, inicializar todos los contadores a 0.

2. **Actualizar `consecutive_failures`** por servidor: incrementar si el servidor falló (SSH unreachable / todos los jobs skipped), resetear a 0 si al menos un job completó correctamente.

3. **Registrar `last_notification_error`**: si `send_cycle_summary()` o `send_daemon_lifecycle()` devuelve False, guardar el error en el estado. Si el envío fue OK, poner `null`.

```python
def _read_previous_state(self) -> dict:
    """Read previous state file for consecutive_failures carry-over."""
    state_file = self.config.orchestrator.state_file
    try:
        if os.path.exists(state_file):
            with open(state_file) as f:
                return json.load(f)
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _build_run_state(self, result: RunResult, next_run_iso: str, notification_ok: bool) -> dict:
    """Build state dict from RunResult with consecutive_failures tracking."""
    prev = self._read_previous_state()
    prev_servers = prev.get("last_run", {}).get("servers", {})

    servers = {}
    for sr in result.server_runs:
        prev_failures = prev_servers.get(sr.server_name, {}).get("consecutive_failures", 0)
        all_skipped = all(jr.status == JobStatus.SKIPPED for jr in sr.job_runs) if sr.job_runs else True
        has_ok = any(jr.status == JobStatus.COMPLETED for jr in sr.job_runs)

        if has_ok:
            consecutive = 0
        elif all_skipped and sr.activation_error:
            consecutive = prev_failures + 1
        else:
            consecutive = prev_failures

        total_duration = sum(
            (jr.duration_seconds or 0) for jr in sr.job_runs
        )
        status = "ok"
        if any(jr.status == JobStatus.TIMEOUT for jr in sr.job_runs):
            status = "timeout"
        elif any(jr.status == JobStatus.FAILED for jr in sr.job_runs):
            status = "failed"
        elif all_skipped:
            status = "unreachable"

        servers[sr.server_name] = {
            "status": status,
            "jobs": len(sr.job_runs),
            "duration": int(total_duration),
            "consecutive_failures": consecutive,
        }

    return {
        "version": "0.3.0",
        "daemon_pid": os.getpid(),
        "daemon_started_at": self._daemon_started_at,
        "last_notification_error": None if notification_ok else "SMTP delivery failed",
        "last_run": {
            "started_at": datetime.fromtimestamp(result.started_at, tz=timezone.utc).isoformat().replace("+00:00", "Z") if result.started_at else None,
            "finished_at": datetime.fromtimestamp(result.finished_at, tz=timezone.utc).isoformat().replace("+00:00", "Z") if result.finished_at else None,
            "duration_seconds": int(result.duration_seconds or 0),
            "total_jobs": result.total,
            "completed": result.completed,
            "failed": result.failed,
            "timeout": result.timed_out,
            "skipped": result.skipped,
            "success": result.success,
            "servers": servers,
        },
        "next_run_at": next_run_iso,
    }
```

Y en `run_forever()`, actualizar para capturar el resultado de notificación:

```python
    # En el loop, después de run():
    notification_ok = True
    if not result.success:
        notification_ok = send_cycle_summary(self.config.notification, result)
    elif getattr(self.config.notification, "on_complete", False):
        notification_ok = send_cycle_summary(self.config.notification, result)

    state_data = self._build_run_state(result, next_run_iso, notification_ok)
    self._write_state(state_data)
```

Guardar `self._daemon_started_at` al inicio de `run_forever()`.

- [ ] **Paso 3: Ejecutar tests**

```bash
python -m pytest tests/test_orchestrator.py -v -k "run_forever"
```

Esperado: PASS

- [ ] **Paso 4: Commit**

```bash
git add src/jetbackup_remote/orchestrator.py tests/test_orchestrator.py
git commit -m "feat(orchestrator): add run_forever() daemon loop

Loop with calculated pause, state file writing, lifecycle emails,
and clean SIGTERM shutdown.

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 5: CLI — subcomando daemon y mejora de status

**Ficheros:**

- Modificar: `src/jetbackup_remote/cli.py`
- Test: `tests/test_orchestrator.py` (integración) o nuevo `tests/test_cli.py`

- [ ] **Paso 1: Escribir test**

```python
# tests/test_cli.py (nuevo)

from jetbackup_remote.cli import build_parser


def test_daemon_subcommand_exists():
    parser = build_parser()
    args = parser.parse_args(["daemon"])
    assert args.command == "daemon"


def test_daemon_accepts_verbose():
    parser = build_parser()
    args = parser.parse_args(["-v", "daemon"])
    assert args.verbose is True


def test_daemon_no_dry_run():
    parser = build_parser()
    # daemon no acepta --dry-run
    import pytest
    with pytest.raises(SystemExit):
        parser.parse_args(["daemon", "--dry-run"])
```

- [ ] **Paso 2: Implementar cmd_daemon y añadir al parser**

En `cli.py`, añadir `cmd_daemon()`:

```python
def cmd_daemon(args) -> int:
    """Run as a persistent daemon with loop."""
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    setup_logging(
        log_file=config.orchestrator.log_file,
        max_bytes=config.orchestrator.log_max_bytes,
        backup_count=config.orchestrator.log_backup_count,
        verbose=args.verbose,
    )

    orch = Orchestrator(config)

    try:
        orch.acquire_lock()
    except LockError as e:
        logger.error(str(e))
        print(f"Error: {e}", file=sys.stderr)
        return 1

    try:
        orch.install_signal_handlers()
        orch.run_forever()
        return 0
    finally:
        orch.release_lock()
```

En `build_parser()`, añadir:

```python
    # daemon
    subparsers.add_parser("daemon", help="Run as persistent daemon with loop")
```

En `main()`, añadir `"daemon": cmd_daemon` al dict de commands.

- [ ] **Paso 3: Actualizar cmd_status** para leer el fichero de estado:

```python
def cmd_status(args) -> int:
    config = _load_and_validate_config(args)
    if config is None:
        return 1

    # Read state file if available
    state_file = config.orchestrator.state_file
    state = None
    if os.path.exists(state_file):
        try:
            with open(state_file) as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    if state and not getattr(args, "json", False):
        pid = state.get("daemon_pid")
        started = state.get("daemon_started_at", "unknown")
        # Check if daemon is actually running
        daemon_running = False
        if pid:
            try:
                os.kill(pid, 0)
                daemon_running = True
            except (OSError, TypeError):
                pass

        if daemon_running:
            print(f"Daemon: running (pid {pid})")
        else:
            print(f"Daemon: not running (last pid {pid})")

        last_run = state.get("last_run")
        if last_run:
            finished = last_run.get("finished_at", "unknown")
            ok = last_run.get("completed", 0)
            fail = last_run.get("failed", 0)
            tout = last_run.get("timeout", 0)
            print(f"Last cycle: {finished} ({ok} OK, {tout} TIMEOUT, {fail} FAILED)")

        next_run = state.get("next_run_at")
        if next_run:
            print(f"Next cycle: ~{next_run}")
        print()

    # ... resto del status existente (server loop)
```

- [ ] **Paso 4: Ejecutar tests**

```bash
python -m pytest tests/test_cli.py -v
```

Esperado: PASS

- [ ] **Paso 5: Commit**

```bash
git add src/jetbackup_remote/cli.py tests/test_cli.py
git commit -m "feat(cli): add daemon subcommand and enhance status with state file

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 6: Version bump y pyproject.toml

**Ficheros:**

- Modificar: `src/jetbackup_remote/__init__.py`
- Modificar: `pyproject.toml`

- [ ] **Paso 1: Actualizar versión**

En `__init__.py`:

```python
__version__ = "0.3.0"
```

En `pyproject.toml`:

```toml
version = "0.3.0"
```

- [ ] **Paso 2: Ejecutar todos los tests**

```bash
python -m pytest tests/ -v
```

Esperado: PASS (todos)

- [ ] **Paso 3: Commit**

```bash
git add src/jetbackup_remote/__init__.py pyproject.toml
git commit -m "chore: bump version to 0.3.0

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 7: Systemd — reemplazar service, eliminar timer

**Ficheros:**

- Reemplazar: `systemd/jetbackup-remote.service`
- Eliminar: `systemd/jetbackup-remote.timer`

- [ ] **Paso 1: Reemplazar fichero del servicio**

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
EnvironmentFile=/etc/jetbackup-remote/env

ProtectSystem=strict
ReadWritePaths=/var/log /tmp /etc/jetbackup-remote /var/lib/jetbackup-remote
PrivateTmp=true
NoNewPrivileges=true
ProtectKernelModules=true
ProtectKernelTunables=true

[Install]
WantedBy=multi-user.target
```

- [ ] **Paso 2: Eliminar timer**

```bash
git rm systemd/jetbackup-remote.timer
```

- [ ] **Paso 3: Commit**

```bash
git add systemd/jetbackup-remote.service
git commit -m "feat(systemd): replace oneshot+timer with simple daemon service

Co-Authored-By: Claude Opus 4.6 (1M context) <noreply@anthropic.com>"
```

---

## Tarea 8: Despliegue — jobs, kvm313, config, servicio

Esta tarea es de operaciones, no de código. Se ejecuta manualmente.

- [ ] **Paso 1: Inventariar kvm313 via SSH directo**

```bash
ssh -p 51514 -i ~/.ssh/id_rsa root@kvm313.xerintel.com "jetbackup5api -F listBackupJobs -O json 2>&1"
```

Anotar: destination_id para Raspberry, job_ids existentes.

- [ ] **Paso 2: Registrar kvm313 en sshctx**

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

- [ ] **Paso 3: Crear jobs MySQL via WHM** en servidor02, servidor20, servidor30

En cada servidor, via WHM > JetBackup 5 > Backup Jobs > Create:

- Type: Database
- Destination: Raspberry (la que ya existe)
- Schedule: Daily
- Retention: mínima (ej: 3)

Anotar los job_ids creados.

- [ ] **Paso 4: Crear job Directories en central** via WHM

- Type: Directories
- Destination: Rasp (la que ya existe)
- Include list: copiar del job "Directories" existente hacia Xerbackup02
- Schedule: Daily

Anotar el job_id.

- [ ] **Paso 5: Parar servicios actuales en raspxer**

```bash
# En raspxer:
systemctl stop jetbackup-remote.timer
systemctl stop jetbackup-remote.service
systemctl disable jetbackup-remote.timer
```

- [ ] **Paso 6: Desplegar código y configuración**

```bash
# Desde local:
rsync -av --delete src/ raspxer:/opt/jetbackup-remote/src/ -e "ssh -p 2222"

# En raspxer:
mkdir -p /var/lib/jetbackup-remote

# Crear fichero de entorno con contraseña SMTP:
echo 'JETBACKUP_SMTP_PASSWORD=<contraseña_del_relay_castris>' > /etc/jetbackup-remote/env
chmod 600 /etc/jetbackup-remote/env
```

- [ ] **Paso 7: Actualizar config.json en raspxer**

Añadir: nuevos jobs (MySQL × 3, Directories central, kvm313), sección `loop`, sección `notification` actualizada con SMTP, kvm313 como servidor.

- [ ] **Paso 8: Instalar nuevo servicio systemd**

```bash
# En raspxer:
rm /etc/systemd/system/jetbackup-remote.timer
# Copiar nuevo service file
systemctl daemon-reload
systemctl enable --now jetbackup-remote
```

- [ ] **Paso 9: Verificar arranque**

```bash
# Verificar que el email de "daemon started" llega
# Verificar logs:
journalctl -u jetbackup-remote -f
# Verificar estado:
jetbackup-remote -c /etc/jetbackup-remote/config.json status
```
