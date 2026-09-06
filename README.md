# 🏛️ MAGI Harness — Evangelion-Style Server Autonomous Agent

Sistema de supervisión, diagnóstico y ejecución autónoma para servidores Linux controlado vía Telegram, con arquitectura inspirada en el superordenador MAGI de Neon Genesis Evangelion.

---

## 🧬 Principios del Sistema

1. **Ningún modelo decide en solitario sobre acciones destructivas:** La AI Ejecutadora formula el diagnóstico y planifica las intervenciones, mientras que el consejo MAGI (tres modelos independientes: MELCHIOR-1, BALTHASAR-2 y CASPER-3) audita de forma concurrente y vota antes de ejecutar cualquier acción mutante.
2. **Verificación Independiente Real:** Cada una de las tres mentes de MAGI cuenta con acceso de solo lectura al sistema para contrastar los hechos por sí misma antes de votar, en lugar de limitarse a opinar sobre el texto de la Ejecutora.
3. **Freno Humano Inviolable:** Para acciones críticas o irreversibles (Tiers 2 y 3), incluso ante un consenso unánime 3-0 de MAGI, se exige confirmación humana explícita del operador a través de Telegram (`CONFIRMO` o botón interactivo).

---

## 🏗️ Arquitectura de Componentes

```
                         ┌───────────────────────────────────────────┐
                         │               MAGI DAEMON                 │
                         │          (systemd user service)           │
                         │                                           │
  Telegram               │   ┌───────────────────────────────────┐   │
 (Usuario ID) ─────────► │   │    Telegram Channel Adapter       │   │
                         │   │  (Allowlist, Rate-Limit, UI Msg)  │   │
                         │   └─────────────────┬─────────────────┘   │
                         │                     │                     │
                         │   ┌─────────────────▼─────────────────┐   │
                         │   │   Session Router & Persistence    │   │
                         │   │       (SQLite WAL + JSONL)        │   │
                         │   └─────────────────┬─────────────────┘   │
                         │                     │                     │
                         │   ┌─────────────────▼─────────────────┐   │
                         │   │           AI EJECUTADORA          │   │
                         │   │ (OpenRouter / SearXNG / ReadOnly) │   │
                         │   └─────────────────┬─────────────────┘   │
                         │                     │ (Plan con Mutación) │
                         │   ┌─────────────────▼─────────────────┐   │
                         │   │           SISTEMA MAGI            │   │
                         │   │   MELCHIOR | BALTHASAR | CASPER   │   │
                         │   │ (3 llamadas paralelas + ReadOnly) │   │
                         │   └─────────────────┬─────────────────┘   │
                         │                     │                     │
                         │   ┌─────────────────▼─────────────────┐   │
                         │   │      Motor de Política de Riesgo  │   │
                         │   │  Tier 0 (Free) | Tier 1 (Mayoría) │   │
                         │   │  Tier 2-3 (Unanimidad + Freno H.) │   │
                         │   └─────────────────┬─────────────────┘   │
                         │                     │                     │
                         │   ┌─────────────────▼─────────────────┐   │
                         │   │  Catálogo de Acciones Mutantes    │   │
                         │   │     (Sin shell arbitrario)        │   │
                         │   └───────────────────────────────────┘   │
                         │                     │                     │
                         │   Unix Socket IPC ──┼─────────────────────┼──► magi-tui (Textual TUI)
                         └───────────────────────────────────────────┘
```

---

## 🛡️ Niveles de Riesgo (Risk Tiers)

| Tier | Categoría | Herramientas | Requisito de Aprobación |
|---|---|---|---|
| **0** | Solo lectura / Informativo | Logs, Docker inspect, métricas, `systemd`, búsqueda web | Sin votación (Paso directo) |
| **1** | Reversible / Bajo impacto | Reiniciar contenedor, crear cronjob, configs temporales | Mayoría simple MAGI (2-1 o 3-0) |
| **2** | Crítico / Difícil de revertir | Instalar/desinstalar paquetes, `docker prune`, borrar volúmenes, cambios en `/etc` | Unanimidad MAGI (3-0) **+ Confirmación Humana** |
| **3** | Irreversible / Auto-referencial | Apagado, reinicio del host, sudoers, credenciales, DB de MAGI | Unanimidad MAGI (3-0) **+ Confirmación Humana SIEMPRE** |

---

## ⌨️ Comandos Disponibles en Telegram

| Comando | Descripción |
|---|---|
| `/start` | Mensaje de bienvenida, estado del sistema y sesión activa |
| `/help` | Manual de operaciones completo |
| `/new [título]` | Crea una nueva sesión congelando los prompts de las mentes |
| `/sessiones` | Selector interactivo de sesiones activas e historial |
| `/del <id>` | Elimina una sesión y sus registros con confirmación |
| `/status` | Telemetría en vivo del host: CPU, RAM, Disco, Uptime y MAGI |
| `/rescan` | Regenera el mapa de estado del servidor (`.init`) bajo demanda |
| `/provider <key>` | Configura y verifica la API Key de OpenRouter |
| `/model-executor` | Configura el modelo para la AI Ejecutadora |
| `/model-melchior` | Configura el modelo para MELCHIOR-1 (Científica) |
| `/model-balthasar` | Configura el modelo para BALTHASAR-2 (Madre) |
| `/model-casper` | Configura el modelo para CASPER-3 (Mujer) |
| `/disable` | Desactiva comité MAGI para Tiers 0-1 (Tier 2-3 sigue protegido) |
| `/enable` | Reactiva el comité MAGI |
| `/cronjobs` | Lista las tareas programadas internas activas |
| `/cronjob del <id>` | Elimina una tarea programada |
| `/cronjob add <expr> <prompt>` | Crea una tarea programada interna |
| `/log <id>` | Muestra el desglose de votación y razonamiento de cada mente |
| `/auditoria` | Muestra los últimos eventos de seguridad y acciones |

---

## 🚀 Despliegue y Operación

### 1. Gestión del Daemon vía systemd
El daemon corre de forma continua y desatendida como servicio de usuario en systemd:
```bash
# Ver estado del servicio
systemctl --user status magi.service

# Ver logs en vivo
journalctl --user -u magi.service -f

# Reiniciar daemon
systemctl --user restart magi.service
```

### 2. Monitor Visual en Terminal (`magi-tui`)
Para visualizar en vivo la deliberación de Melchior, Balthasar y Casper con estética Evangelion:
```bash
uv run magi-tui
```
*`magi-tui` se conecta en modo solo lectura al socket IPC local (`data/magi.sock`). Abrir o cerrar la TUI no interrumpe el daemon.*
