<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo.svg" width="96" height="96" alt="logotipo de since-cutoff">
</picture></p>

<h1 align="center">since-cutoff</h1>

<!-- mcp-name: io.github.MohammadHijjawi97/since-cutoff -->

**Para proyectos de Python escritos con un agente de programación: since-cutoff encuentra las API
de tus dependencias que cambiaron después de la fecha de corte de entrenamiento del modelo, mide
en cuáles se equivoca el modelo y corrige esos fallos con notas breves en AGENTS.md, cada una
comprobada con un verificador de tipos o tomada directamente del diff de la API.**

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.es.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.es.svg" width="640" alt="Tu modelo de IA para programar aprendió tus bibliotecas antes de que cambiaran. Claude Opus 4.6 en un único proyecto de ejemplo, medido con since-cutoff 0.1.0: en 7 de 16 cambios de API sondeados usó un nombre o un parámetro que ya se había eliminado; con las notas, los aciertos en 20 tareas reservadas pasaron del 5 % al 65 %. Pruébalo: uvx since-cutoff scan (sin llamadas al modelo, sin clave de API).">
</picture></p>

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md) | **Español** | [Français](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md)

## El problema

Todo modelo tiene una fecha de corte de entrenamiento; tu lockfile, en cambio, no deja de
moverse. Cuando una biblioteca cambia su API pública después de esa fecha, un modelo que aprendió
la versión anterior sigue escribiendo las llamadas antiguas. Parte de ese código falla al
importarse o al ejecutar la llamada. Otra parte sigue funcionando, porque la forma antigua solo
está marcada como obsoleta.

Algunos de los cambios que `since-cutoff scan` encuentra para Claude Sonnet 4.5 (fecha de corte:
julio de 2025) en el [proyecto de ejemplo](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
que fija seis de sus nueve dependencias en las versiones actuales (para las otras tres, que no
están fijadas, la herramienta usa la versión más reciente):

| biblioteca | versión en la fecha de corte | versión fijada | qué cambió |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` ya no se acepta |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)`: parámetros que salieron de la firma en la 1.0 (la 2.0.0 aún los acepta en tiempo de ejecución, los ignora y emite un aviso) |
| langchain-core | 0.3.72 | 1.6.5 | se eliminaron `retriever.get_relevant_documents()` y `llm.predict()` |
| openai | 1.98.0 | 3.19.2 | 21 cambios incompatibles, 6 obsolescencias nuevas |

En ese proyecto, 7 de las 9 dependencias cambiaron su API pública después de la fecha de corte.
El diff estático señala 317 cambios incompatibles y 23 obsolescencias nuevas; algunos afectan a
elementos internos, que los sondeos omiten.

No es cosa de un modelo ni de un proveedor. En 36 bibliotecas de IA de Python muy usadas y 21
modelos de OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, Moonshot y Mistral, incluso el modelo
más reciente probado (Claude Opus 5.5, fecha de corte de junio de 2026) es anterior a un cambio
incompatible en la API pública de 20 de las 36
([resultados completos](https://mohammadhijjawi97.github.io/since-cutoff/ai-stack.html), en inglés):

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack.svg" width="860" alt="Gráfico de barras: para cada uno de 21 modelos de 8 proveedores, cuántas de 36 bibliotecas de IA de Python cambiaron su API pública de forma incompatible y cuántas tienen una nueva versión mayor desde la fecha de corte del modelo. De 21 de 36 para GPT-4o (13 de las bibliotecas aún no existían) a 33 de 36 para los modelos con fecha de corte a principios de 2025, y 20 de 36 para Claude Opus 5.5 (junio de 2026).">
</picture></p>

since-cutoff lo aborda de tres maneras:

1. **`scan`** busca, para cada dependencia, la versión más reciente publicada en la fecha de corte
   del modelo o antes, y compara su API pública con la versión que tienes fijada. Sin llamadas al
   modelo y sin clave de API.
2. **`run`** le plantea al modelo tareas breves de programación que requieren las API
   modificadas, sin herramientas ni documentación, y puntúa cada respuesta con un verificador de
   tipos frente a *ambas* versiones: *stale* (desactualizada), *wrong* (incorrecta), *deprecated*
   (obsoleta) o *correct* (correcta). Ningún LLM juzga nada.
3. **Notas**: para cada fallo escribe una nota de una línea para AGENTS.md / CLAUDE.md. Una
   nota escrita por el modelo solo se conserva si su ejemplo pasa la verificación de tipos con tu
   versión; si no, se usa una descripción del cambio tomada directamente del diff de la API.
   Después vuelve a evaluar el modelo con tareas reservadas (*held-out*), sin las notas y con
   ellas.

El mismo diff está disponible para los agentes a través de un [servidor MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-desde-cualquier-agente-mcp)
y para la integración continua (CI) a través de una [GitHub Action y un hook de pre-commit](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-en-ci).

## Inicio rápido

```bash
# lista los cambios de API desde la fecha de corte de tu modelo (sin llamadas al modelo, sin clave de API)
uvx since-cutoff scan

# sondea el modelo, escribe notas y las añade a AGENTS.md
uvx since-cutoff run --apply
```

También puedes instalarlo con `pipx install since-cutoff` (o `pip install since-cutoff`) y
ejecutar `since-cutoff`. Ejecútalo desde la raíz del proyecto: lee `uv.lock`, `poetry.lock`,
`pdm.lock`, `pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`, `Pipfile` o
un `.venv` (no `setup.py` ni `setup.cfg`). Sin `--model`, evalúa el modelo con el que está
configurado tu agente de programación, según los ajustes de Claude Code, Codex, OpenCode o
Aider; para cualquier otro modelo, pasa `--model` (consulta [Elegir el modelo](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#elegir-el-modelo)).
`scan` es gratuito; `run` envía prompts al proveedor del modelo y consume tus créditos de API o
tu cuota de uso de Claude Code.

Lo que muestra `scan` para el proyecto de ejemplo:

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="since-cutoff scan --model anthropic:claude-sonnet-4-5 en el proyecto de ejemplo: 7 de 9 dependencias cambiaron su API después de la fecha de corte; diff estático: 317 cambios incompatibles, 23 obsolescencias nuevas; una tabla con la versión fijada de cada paquete, su versión en la fecha de corte y el número de cambios, y un cambio de ejemplo por paquete"></p>

### En Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Después, pídele a Claude: «Comprueba en cuáles de nuestras dependencias estás desactualizado»,
o ejecuta `/since-cutoff:since-cutoff`. La skill ejecuta la CLI; la medición en sí la hace una
instancia nueva del modelo, sin herramientas, así que el agente no puede evaluarse a sí mismo. El plugin
también inicia el [servidor MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-desde-cualquier-agente-mcp),
para que Claude pueda consultar los cambios de una biblioteca antes de escribir código.

### En otros agentes de programación

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

Esto instala la misma skill mediante la CLI de código abierto [skills](https://github.com/vercel-labs/skills)
para Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode y otros agentes que leen `SKILL.md`.
since-cutoff también lee el modelo de los ajustes de Codex, OpenCode y Aider; para otros
agentes, indícale qué modelo evaluar, por ejemplo `since-cutoff scan --model openai:gpt-5.4`.
Añade el servidor MCP como se muestra más abajo.

Prompts que funcionan bien:

- «¿Cuáles de nuestras dependencias cambiaron su API pública después de tu fecha de corte de
  entrenamiento?». El agente ejecuta `since-cutoff scan` o llama a la herramienta MCP
  `project_changes`.
- «Mide en cuáles de esos cambios te equivocas de verdad y añade las notas a AGENTS.md». El
  agente te pide permiso primero y luego ejecuta `since-cutoff run --quick --apply`.
- «Antes de escribir el código de httpx, comprueba qué ha cambiado en httpx desde tu fecha de
  corte». El agente llama a la herramienta MCP `api_changes`.

### Elegir el modelo

| `--model` | usa | requiere |
|---|---|---|
| `claude-code` (predeterminado si ningún ajuste indica un modelo) | tu sesión de Claude Code (suscripción o clave), con el modelo actual | la CLI `claude` |
| `claude-code:sonnet`, `claude-code:claude-haiku-4-5` | un modelo de Claude concreto | la CLI `claude` |
| `anthropic:<model>` | API de Anthropic | `ANTHROPIC_API_KEY` |
| `openai:<model>` | API de OpenAI | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | API de DeepSeek | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | Ollama en local | Ollama en ejecución |
| `openai-compatible:<model>` | cualquier servidor compatible con OpenAI | `--base-url`, `OPENAI_API_KEY` opcional |

Sin `--model`, las versiones 0.3.0 y posteriores evalúan el modelo con el que está
configurado tu agente de programación, y la línea del modelo indica de dónde procede
(«model from .claude/settings.json»):

1. `SINCE_CUTOFF_MODEL` (una especificación completa, como `openai:gpt-5.4`) tiene siempre
   prioridad.
2. Dentro de Claude Code (que define `CLAUDECODE=1` para los comandos que ejecuta), solo cuentan
   los ajustes de Claude Code: `ANTHROPIC_MODEL`, después `.claude/settings.local.json` y
   `.claude/settings.json` del proyecto, y después `~/.claude/settings.json`.
3. En otros casos gana el ajuste más específico: primero `ANTHROPIC_MODEL` o `AIDER_MODEL`;
   después los ajustes del proyecto, empezando por la carpeta más cercana, desde la carpeta
   analizada hasta la raíz del repositorio (nunca la carpeta personal); y por último los
   ajustes del usuario. Dentro de una misma carpeta, los agentes cuentan en este orden:

| agente | ajustes del proyecto | ajustes del usuario |
|---|---|---|
| Claude Code | `.claude/settings.local.json`, `.claude/settings.json` | `~/.claude/settings.json` |
| Codex | `.codex/config.toml`, con su perfil seleccionado | `$CODEX_HOME/config.toml` o `~/.codex/config.toml` |
| OpenCode | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/` |
| Aider | `.aider.conf.yml`, con los alias de Aider (`4o`, `flash`, `r1`, ...) | `~/.aider.conf.yml` |

Si ningún ajuste indica un modelo, evalúa el modelo predeterminado de Claude Code y así lo dice.
Solo se leen los campos del modelo, y un nombre de modelo que no sabe identificar detiene la
ejecución con un mensaje que indica el ajuste. Un modelo al que un agente accede a través de
otro servicio (GitHub Copilot, Amazon Bedrock, Vertex AI) se nombra según su fabricante, así que
`run` llama a la API del fabricante (`openai:` requiere `OPENAI_API_KEY`).

Las fechas de corte de entrenamiento se obtienen de [models.dev](https://models.dev) (el paquete
incluye una instantánea para usarla sin conexión). `since-cutoff models sonnet` las lista;
`--cutoff 2025-07` fija la fecha manualmente, y `since-cutoff scan --cutoff 2025-07` sin `--model`
analiza tomando solo esa fecha como referencia. `scan` solo necesita la fecha de corte, así que
también acepta un identificador de modelo sin proveedor (`claude-haiku-4-5`, `sonnet`) o con
cualquier proveedor que figure en models.dev (`google:gemini-2.5-pro`, incluidos los
identificadores de Amazon Bedrock y Vertex AI); `run` necesita un proveedor de la tabla
anterior.

## Resultados

Dos modelos de Claude evaluados en el proyecto de ejemplo de 9 dependencias de
[`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
**medidos con since-cutoff 0.1.0**; Claude Opus 4.6 escribió las tareas y las notas:

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| fecha de corte de entrenamiento | febrero de 2025 | mayo de 2025 |
| cambios de API sondeados | 20 | 16 |
| **stale** / wrong / deprecated / correct | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| bibliotecas con uso desactualizado | 3 de 5 sondeadas | 2 de 4 sondeadas |
| notas escritas (con un ejemplo que pasa el verificador de tipos) | 8 (7), unos 391 tokens | 10 (7), unos 437 tokens |
| **aciertos en tareas reservadas, sin -> con notas** | **14% -> 57%** (14 pares) | **5% -> 65%** (20 pares) |
| API que el modelo ya usaba bien, con las notas | 6/6 siguen bien | 6/6 siguen bien |

Las tareas *reservadas* (*held-out*) son paráfrasis de la tarea con la que se sondeó cada cambio
en el que falló el modelo; cada una se responde dos veces, sin las notas y con ellas, y se puntúa
de la misma forma. La última fila vuelve a comprobar las API que el modelo ya usaba bien, para
detectar notas que empeoren el resultado.

En esta muestra, el modelo más potente no resultó más fiable: Opus 4.6 escribió API que se
eliminaron después de su fecha de corte, como `anthropic.HUMAN_PROMPT` con `client.completions`.
Código desactualizado de ambas ejecuciones, en cada caso válido para la versión de comparación y
rechazado por el verificador de tipos para la fijada: `messages.create(temperature=...)`
(anthropic 1.8), `hf_hub_download(resume_download=...)`, `local_dir_use_symlinks=...`,
`force_filename=...` y `proxies=...` (huggingface-hub 2.0), y `client.beta.vector_stores`
(openai 3.x). En tiempo de ejecución, anthropic 1.8.0 lanza `TypeError` con `temperature`;
huggingface-hub 2.0.0 todavía acepta esos cuatro argumentos de descarga, los ignora y emite un
aviso.

Las notas escritas en la ejecución con Claude Haiku 4.5 (extracto literal):

```markdown
<!-- since-cutoff:start -->
## Library changes after the model's training cutoff

**anthropic 1.8.0**
- `temperature=...` was removed from `messages.create()` in anthropic 1.8.0. Omit the `temperature` parameter entirely; there is no replacement.

**huggingface-hub 2.0.0**
- `hf_hub_download(..., resume_download=True)`: The `resume_download` parameter was removed in huggingface-hub 2.0.0. Omit it; downloads resume automatically.

**openai 3.19.2**
- `client.beta.vector_stores` is removed in openai 3.19.2. Use `client.vector_stores` instead.
<!-- since-cutoff:end -->
```

La nota de huggingface-hub no es del todo exacta: `resume_download` salió de la firma en la 1.0,
no en la 2.0.0, y la 2.0.0 todavía lo acepta en tiempo de ejecución, lo ignora y emite un aviso
([código fuente](https://github.com/huggingface/huggingface_hub/blob/v2.0.0/src/huggingface_hub/utils/_validators.py#L171-L191)).
Aun así, el consejo de omitirlo es correcto.

Este es el resumen en la terminal de la ejecución con Claude Opus 4.6, capturado con la versión
0.1.0. Los resultados de los sondeos son los de la tabla anterior. Las cifras del diff que
aparecen en la tarjeta son las de la versión 0.1.0 («725 changes flagged»); tras corregir varios
fallos del diff, `scan` en la versión 0.2.0 informa de 513 cambios incompatibles y 48
obsolescencias nuevas para la misma fecha de corte. El recuento de cambios corregidos («changes
fixed») de la tarjeta y su IC del 95 % también son los de la versión 0.1.0: el intervalo
corresponde a ese recuento, no a las tasas del 5 % y el 65 %, y hasta la versión 0.2.0 un
cambio contaba como corregido aunque una respuesta reservada ya fuera correcta sin las notas.

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run-opus.svg" width="100%" alt="Ejecución de since-cutoff 0.1.0 con Claude Opus 4.6: uso de API desactualizadas en 2 de 4 dependencias sondeadas; 16 cambios de API sondeados: 7 stale, 0 wrong, 3 deprecated, 6 correct; 10 notas; aciertos en tareas reservadas sin -> con notas: 5% -> 65% (20 tareas emparejadas); una lista de las llamadas desactualizadas"></p>

<details>
<summary>El mismo resumen para la ejecución con Claude Haiku 4.5 (también con la versión 0.1.0; para su fecha de corte, scan en la versión 0.2.0 informa de 491 cambios incompatibles y 50 obsolescencias nuevas)</summary>
<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" width="100%" alt="Ejecución de since-cutoff 0.1.0 con Claude Haiku 4.5: uso de API desactualizadas en 3 de 5 dependencias sondeadas; 20 cambios de API sondeados: 5 stale, 1 wrong, 2 deprecated, 12 correct; 8 notas; aciertos en tareas reservadas sin -> con notas: 14% -> 57% (14 tareas emparejadas)"></p>
</details>

Muestras pequeñas, dos modelos, un proyecto: tómalo como una demostración del método, no como un
benchmark. Cada ejecución escribe su informe completo (cada tarea, cada respuesta y cada error del
verificador de tipos) en `.since-cutoff/report.md`. Para repetir el experimento con la versión
actual (su diff y su orden de prioridad cambiaron, así que los sondeos no serán idénticos):
`cd examples/agent-app && since-cutoff run --model claude-code:claude-haiku-4-5 --task-model claude-code:claude-opus-4-6`.
Desde la versión 0.3.0, una ejecución también guarda las tareas que usó una ejecución: añade
`--tasks-out tasks.json`, y cualquiera podrá repetir la ejecución con exactamente las mismas
tareas usando `--tasks-from tasks.json`, con otro modelo o con otras notas. El archivo indica quién
escribió las tareas (modelo, versión del prompt y versión de since-cutoff), y una ejecución con
tareas reutilizadas lo recoge en su informe
([detalles](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md#2-probe),
en inglés). Los resultados de tus propios proyectos serán muy bienvenidos en
[Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
(«Comparte tus resultados», en inglés).

Cómo funciona la medición y qué dicen y qué no dicen estas cifras, con más detalle:
[el artículo en español](https://mohammadhijjawi97.github.io/since-cutoff/es/).

## Uso desde cualquier agente (MCP)

`since-cutoff mcp` es un servidor MCP que permite a un agente de programación preguntar «¿qué ha
cambiado en esta biblioteca desde mi fecha de corte de entrenamiento?» antes de escribir código.
Ofrece tres herramientas de solo lectura:

| herramienta | qué responde |
|---|---|
| `api_changes(package, model, symbol=...)` | qué cambió en una biblioteca entre la versión publicada en la fecha de corte del modelo y la más reciente (o una versión concreta), con los cambios incompatibles primero |
| `project_changes(project_dir, model)` | lo mismo para cada dependencia de un proyecto en su versión fijada, empezando por las API que tu código ya usa |
| `model_cutoff(model)` | la fecha de corte de entrenamiento de un modelo, según [models.dev](https://models.dev) |

El agente pasa su propio identificador de modelo, así que la respuesta cubre lo que cambió después
de la fecha de corte de ese modelo. Las herramientas leen PyPI y el código fuente de los paquetes
de forma estática: sin llamadas al modelo, sin clave de API y sin ejecutar código de los paquetes.

**Claude Code**

```bash
claude mcp add --scope user since-cutoff -- uvx since-cutoff@latest mcp
```

**Codex** (`~/.codex/config.toml`)

```toml
[mcp_servers.since-cutoff]
command = "uvx"
args = ["since-cutoff@latest", "mcp"]
startup_timeout_sec = 60
tool_timeout_sec = 900
```

**Cursor** (`~/.cursor/mcp.json`) y **Claude Desktop** (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "since-cutoff": { "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**VS Code** (`.vscode/mcp.json`)

```json
{
  "servers": {
    "since-cutoff": { "type": "stdio", "command": "uvx", "args": ["since-cutoff@latest", "mcp"] }
  }
}
```

**Gemini CLI**

```bash
gemini mcp add --scope user since-cutoff uvx since-cutoff@latest mcp
# o como extensión, que inicia el mismo servidor:
gemini extensions install https://github.com/MohammadHijjawi97/since-cutoff
```

`@latest` hace que `uvx` use las versiones nuevas en lugar de reutilizar la primera que guardó en
caché (el `.mcp.json` del propio plugin fija la versión exacta). Si el cliente no encuentra
`uvx`, instala [uv](https://docs.astral.sh/uv/) o indica la ruta completa (`which uvx`). El
servidor figura en el [MCP Registry](https://registry.modelcontextprotocol.io) como
`io.github.MohammadHijjawi97/since-cutoff`.

En un proyecto grande, la primera llamada a `project_changes` descarga los archivos wheel de todas las
dependencias que cambiaron y puede tardar varios minutos (los paquetes muy grandes, como
transformers, son los que más tardan). Los resultados se guardan en caché, así que las llamadas
posteriores tardan segundos. Para precalentar la caché, ejecuta `since-cutoff scan` una vez en el
proyecto; `scan` comparte la caché con el servidor. Los clientes con un tiempo de espera
predeterminado corto para las herramientas pueden necesitar uno más largo, como en el ejemplo de
Codex de arriba. `project_changes` mantiene su respuesta por debajo de unos 24 000 caracteres:
las dependencias modificadas que no caben ocupan una línea cada una, y pasarlas en `only` muestra
sus cambios.

Lo que devolvió `api_changes("huggingface-hub", model="claude-haiku-4-5")` en la versión 0.2.0
(salida real, recortada; las versiones posteriores afinan el diff, así que sus cifras difieren
ligeramente):

```markdown
# huggingface-hub 0.29.1 -> 2.0.0

- From 0.29.1 (2025-02-20): the newest release on or before 2025-02-28 (training cutoff of claude-haiku-4-5, from models.dev)
- To 2.0.0 (2026-09-24): the latest release on PyPI
- 116 breaking changes, 0 new deprecations (removed or moved 62, parameters removed 43, parameters now required 9, changed kind 1, now keyword-only or positional-only 1)

## Removed or moved

- `huggingface_hub.InferenceApi` was removed; similar names now: `inference`, `InferenceEndpoint`, `InferenceClient`
...

## Parameters removed

- `huggingface_hub.snapshot_download(resume_download=...)`: parameter `resume_download` was removed; similar parameters now: `force_download`
- `huggingface_hub.file_download.hf_hub_download(force_filename=...)`: parameter `force_filename` was removed; similar parameters now: `filename`
...

Not listed: 76 breaking changes, 0 new deprecations (removed or moved 47, parameters removed 29). Narrow with symbol="..." or raise limit.
```

Con `symbol="hf_hub_download"` solo lista los 8 cambios de esa función (`resume_download=`,
`force_filename=`, `local_dir_use_symlinks=` y `proxies=`, en la función y en `HfApi`). `symbol`
también acepta una llamada tal como se escribe en el código: `client.messages.create` encuentra
los cambios de `Messages.create`. El diff solo lee firmas: esos cuatro parámetros salieron de la
firma en huggingface-hub 1.0, pero la 2.0.0 todavía los acepta en tiempo de ejecución, los ignora
y emite un aviso.

## Uso en CI

### GitHub Action

Analiza el proyecto en cada pull request y añade un resumen a la página del trabajo: para cada
dependencia, la versión en la fecha de corte del modelo, la versión que tienes fijada y los
cambios principales, empezando por los que afectan a nombres que usa tu código. Igual que `scan`,
solo lee PyPI y models.dev: sin llamadas al modelo y sin clave de API.

```yaml
# .github/workflows/since-cutoff.yml
name: since-cutoff
on:
  pull_request:
    paths: ["**/*.lock", "**/pylock*.toml", "**/requirements*.txt", "**/pyproject.toml"]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: MohammadHijjawi97/since-cutoff@v0
        with:
          model: anthropic:claude-sonnet-4-5  # el modelo con el que programa tu equipo
```

| entrada | valor predeterminado | |
|---|---|---|
| `model` | obligatorio | `provider:model`, como en `--model`; solo se usa su fecha de corte de entrenamiento |
| `working-directory` | `.` | el directorio del proyecto |
| `only`, `exclude` | | nombres de PyPI separados por comas |
| `cutoff` | | sustituye la fecha de corte de entrenamiento (`YYYY-MM` o `YYYY-MM-DD`) |
| `fail-on-changes` | `false` | hace fallar el paso cuando una dependencia cambió su API después de la fecha de corte |
| `step-summary` | `true` | añade el resumen en Markdown al resumen del trabajo |
| `cache` | `true` | conserva entre ejecuciones los metadatos de PyPI, el código fuente de los paquetes y los diffs de API (también cuando `fail-on-changes` hace fallar el trabajo) |
| `args` | | más argumentos para `since-cutoff scan`, p. ej. `--all-deps --limit 20` |
| `since-cutoff-version` | `0.3.2` | la versión de since-cutoff que se ejecuta, o `latest` |

Salidas: `changed-packages` (separados por comas), `changes` (cambios incompatibles),
`deprecations`, `markdown` (la ruta del resumen, por ejemplo para publicarlo como comentario en la
pull request) y `report` (la ruta del informe completo). Un cambio accesible desde varias rutas
de importación se cuenta una sola vez.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.3.2
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # añade --fail-on-changes para bloquear el commit
```

El hook se ejecuta cuando cambia un lockfile, un archivo de requirements o `pyproject.toml`, y
muestra el resultado del análisis. Pásale `--model` en `args`. Necesita acceso a PyPI, así que
omítelo en pre-commit.ci (`ci: {skip: [since-cutoff-scan]}`).

### Otros sistemas de CI

```bash
# resumen en Markdown para cualquier CI; código de salida 3 si una dependencia cambió su API después de la fecha de corte
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on-changes

# medir también el modelo (requiere su clave de API o la CLI claude)
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` escribe el resumen en stdout, y en stderr solo el progreso y la ruta del informe,
igual que `--json`. En un archivo, una tubería o un registro de CI no hay barra de progreso
animada, y la salida se compone con 160 columnas de ancho (`COLUMNS` fija otro ancho). Códigos
de salida: `0` éxito, `1` error (por ejemplo, `run` no pudo sondear ningún cambio de API o no
pudo puntuar ninguna respuesta del modelo), `2` error de uso, `3` uso de API desactualizadas
detectado con `run --fail-on-stale`, o cambios de API detectados con `scan --fail-on-changes`,
`141` la salida se cerró antes de tiempo (por ejemplo, al pasarla por una tubería a `head`).

## Cómo funciona

Tres etapas. La primera no necesita ningún modelo; en las otras dos, un verificador de tipos
puntúa cada respuesta y comprueba cada nota que escribe el modelo:

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.es.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.es.svg" width="640" alt="Tres etapas. Análisis (scan), sin llamadas al modelo: el lockfile da tus versiones exactas, después se busca la versión publicada en la fecha de corte del modelo y se hace un diff estático de la API con griffe. Sondeo: tareas breves que requieren el cambio, el modelo responde de memoria y basedpyright comprueba la respuesta con ambas versiones. Corrección y verificación: una nota escrita por el modelo solo se conserva si su ejemplo pasa la verificación de tipos; si no, se usa una descripción del cambio tomada del diff de la API. Las tareas reservadas se responden sin las notas y con ellas, y --apply escribe un bloque en AGENTS.md.">
</picture></p>

| resultado | significado |
|---|---|
| **stale** | el código es válido para la versión de comparación (la de la fecha de corte del modelo) e inválido para la tuya, y el error afecta a una API que cambió |
| **wrong** | inválido para tu versión, pero no se explica por un cambio (API inventada o mal usada) |
| **deprecated** | válido, pero usa una API marcada con `@deprecated` en tu versión |
| **correct** | válido para tu versión y usa realmente la API modificada |
| untouched / off-task / invalid / error | no cuentan en ninguna tasa y siempre aparecen en el informe |

Desde la versión 0.3.0, el resultado de las tareas reservadas da las tasas de
acierto por tarea sin -> con notas (con el número de tareas emparejadas y de cambios de API de
los que proceden), su diferencia con un intervalo bootstrap del 95 % que remuestrea cambios de
API, los cambios que las notas corrigieron (mal sin ellas, bien con ellas) con un intervalo de
Wilson del 95 %, los cambios que estropearon, una prueba de los signos exacta de corregidos
frente a estropeados y los pares no contados, por motivo. La comprobación de regresiones indica
cuántas de las API que el modelo ya usaba bien siguen bien con las notas.

`run --compare template,signatures` (desde la versión 0.3.0) responde además a las
tareas reservadas y a las comprobaciones de regresiones con notas de referencia que no necesitan
ningún modelo: `template` describe cada cambio fallido en una frase tomada del diff de la API, y
`signatures` da la nueva firma y el primer párrafo del docstring de cada API modificada, o de la
que su biblioteca indica como sustituta. Todos los bloques se puntúan sobre los mismos pares y
frente a las mismas respuestas sin notas, y se indica el tamaño de cada bloque en tokens, de modo
que una ejecución muestra lo que aportan las notas propias de since-cutoff frente a ellas.

Todo lo puntúa un verificador de tipos frente a las versiones exactas de los paquetes, cada una en
un entorno aislado con las dependencias de ejecución propias de ese paquete. Ningún LLM juzga nada, y
cada cifra se puede rastrear hasta `results.json`. Detalles (en inglés): [docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md).

## Qué ejecuta, qué envía y qué guarda

- **No ejecuta código de los paquetes ni código escrito por el modelo.** Los paquetes se leen de
  forma estática (griffe con la inspección desactivada; solo se extraen archivos `.py`/`.pyi`,
  con comprobaciones de ruta y tamaño). Las respuestas del modelo solo pasan por la verificación
  de tipos, en local, con basedpyright.
- **Descarga** metadatos públicos y archivos wheel de los paquetes desde PyPI, y las fechas de
  corte de los modelos desde models.dev (el paquete incluye una instantánea para usarla sin
  conexión). Las dependencias
  de Git, de ruta local, de workspace o de índices privados nunca se buscan por nombre en el PyPI
  público.
- **Envía** prompts solo en `run`, y solo al proveedor de modelos que elijas: nombres de paquetes,
  versiones, firmas públicas y docstrings de las API modificadas, las tareas generadas y, para
  las notas, las propias respuestas del modelo. Nunca tu código fuente. `scan`, el servidor MCP,
  la GitHub Action y el hook de pre-commit no envían nada a ningún modelo.
- **Guarda** los resultados en `.since-cutoff/` dentro de tu proyecto (el propio directorio se
  excluye de git) y en una caché local (`since-cutoff cache path` la muestra y
  `since-cutoff cache clear` la borra). Con `--apply` escribe un único bloque delimitado en
  `AGENTS.md`/`CLAUDE.md` y deja el resto del archivo intacto, byte a byte; `since-cutoff unapply`
  elimina el bloque.
- **Sin telemetría**, sin cuenta y sin datos personales. Las ejecuciones repetidas se sirven
  desde la caché, así que son gratuitas y reproducibles (`--fresh` vuelve a preguntar al modelo).
  Consulta
  [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md) (en inglés).

## Limitaciones

- Por ahora solo Python. TypeScript (diffs de `.d.ts`, `tsc`) es lo siguiente
  ([#1](https://github.com/MohammadHijjawi97/since-cutoff/issues/1)).
- Un verificador de tipos detecta nombres incorrectos, parámetros incorrectos y obsolescencias
  de la PEP 702. No ve los cambios de comportamiento si la firma sigue igual, ni las
  obsolescencias que solo emiten un aviso en tiempo de ejecución. `scan` también muestra las
  obsolescencias declaradas con un decorador propio de la biblioteca (cuyo nombre contiene
  «deprecat») y los nombres eliminados que un módulo sigue sirviendo, con un aviso, a través de
  `__getattr__`, pero `run` no los sondea.
- El diff cubre la API pública: se omiten los nombres `_private`, así como los conjuntos de pruebas,
  los benchmarks y los ejemplos que se distribuyen dentro de un paquete.
- Los sondeos cubren una **muestra** de los cambios incompatibles, ordenada por prioridad (primero
  los símbolos que tu código ya usa), no todos.
- «La versión de comparación» es la más reciente publicada en la fecha de corte o antes. Los
  modelos conocen peor las versiones recientes, así que la desactualización real puede empezar
  antes.
- Las tareas reservadas son paráfrasis sobre el mismo cambio: muestran que una nota corrige *ese*
  cambio, no que el modelo haya mejorado en general.

### Para qué se usa la fecha de corte de entrenamiento

La fecha de corte elige un punto de comparación. No afirma nada sobre lo que un modelo haya
memorizado. since-cutoff toma la fecha de models.dev (o de `--cutoff`); un mes significa su último
día (`2025-07` es el 31 de julio de 2025). Para cada dependencia toma la versión final no retirada
(*yanked*) más reciente subida en esa fecha o antes (una prerrelease solo si el paquete aún no tenía
ninguna versión final, y nunca una versión de desarrollo), y compara su API pública con tu versión
fijada. Ese diff es una lista de candidatos: cambios de API que probablemente no estén en los datos
de entrenamiento del modelo.

La fecha decide tres cosas:

- qué paquetes se comparan: un paquete cuya versión fijada no es más nueva que esa versión no
  tiene nada que comparar, y un paquete publicado por primera vez después de la fecha aparece como
  nuevo;
- en `run`, con qué versiones de las dependencias propias de esa versión se hace la comprobación de
  tipos del lado antiguo (la más reciente que cada requisito permitía en esa fecha);
- el lado «antiguo» de cada sondeo de `run`, de modo que una respuesta válida ahí e inválida para tu
  versión, con el error en una API que cambió, cuenta como «stale» y no como «wrong».

Un modelo puede conocer una versión posterior a su fecha de corte declarada, o no conocer versiones
publicadas poco antes, así que el escaneo puede listar cambios que el modelo ya maneja bien y pasar
por alto algunos que no. Si el modelo escribe de verdad la API antigua solo lo muestra `run`, que se
lo pregunta: sin herramientas y diciéndole qué versión fija el proyecto.

## Comparación con otras herramientas

since-cutoff responde a una pregunta para un proyecto: ¿qué API públicas de las versiones que
fijas cambiaron desde la versión a la que apunta la fecha de corte de entrenamiento de un modelo,
empezando por las que usa tu código? `since-cutoff run` añade dos preguntas opcionales: ¿se
equivoca de verdad este modelo con ellas? ¿Las corrige una nota breve? La mayoría de las
herramientas de abajo responden a otra pregunta («¿qué dice ahora la documentación de la
biblioteca?») y funcionan bien junto a since-cutoff.

| herramienta | qué hace | relación con since-cutoff |
|---|---|---|
| [Context7](https://github.com/upstash/context7) (servidor MCP y CLI `ctx7`) | El agente llama a `resolve-library-id` y `query-docs` para traer fragmentos de documentación a su contexto mientras trabaja. Sirve una versión concreta (`/org/project/version`) cuando los propietarios de la biblioteca han añadido esa versión (etiquetas o ramas de git, 20 como máximo); si no, sirve la rama indexada. Funciona sin clave de API, con un límite de uso anónimo más bajo. | Se complementan. Context7 aporta documentación; no lee tus versiones fijadas ni comprueba el código que escribe el agente. since-cutoff lista cuáles de tus API fijadas cambiaron después de la fecha de corte del modelo, primero las que usa tu código, para que sepas dónde hace falta una consulta o una nota. Cuando preguntes a Context7, indica la versión que fijas. |
| Otros servidores de documentación: [Ref](https://github.com/ref-tools/ref-tools-mcp), [docs-mcp-server](https://github.com/arabold/docs-mcp-server) | Búsqueda de documentación para agentes en el momento de responder; docs-mcp-server puede indexar la documentación en local | Igual que Context7. |
| [library-skills](https://github.com/tiangolo/library-skills) | Bibliotecas como FastAPI y Streamlit incluyen skills para agentes dentro de sus paquetes; `uvx library-skills` enlaza las skills de las versiones que tienes instaladas en `.agents/skills` o `.claude/skills`, así que se actualizan con la biblioteca | Las escriben los mantenedores y van al día con la versión instalada: si una biblioteca incluye una, úsala. since-cutoff cubre los paquetes que no incluyen instrucciones y solo describe cambios en la superficie de la API. |
| Plugins de skills de los proveedores, p. ej. [pydantic/skills](https://github.com/pydantic/skills) | Plugins para Claude Code, Codex y Cursor y archivos `SKILL.md` para Pydantic, Pydantic AI y Logfire, instalados desde el repositorio | Instrucciones de los mantenedores sobre cómo usar bien una biblioteca; se publican con el repositorio de plugins, no con la versión que fijas. Las notas de since-cutoff se escriben para tu lockfile. |
| Codemods: reglas de [ast-grep](https://ast-grep.github.io/), `openai migrate` de OpenAI ([Grit](https://github.com/openai/openai-python/discussions/742)) | Reescriben código ya existente con reglas sintácticas escritas a mano; el catálogo de ast-grep tiene una [migración del SDK de OpenAI](https://ast-grep.github.io/catalog/python/#migrate-openai-sdk) (de `openai.Completion.create(...)` a `client.completions.create(...)`) | Para migrar el código que ya tienes, un codemod es la herramienta adecuada. since-cutoff se ocupa del código que el asistente escribirá a continuación: encuentra los cambios en el diff de la API y no en reglas escritas por alguien, y solo sugiere; no reescribe nada. |
| Bots de dependencias: [Renovate](https://github.com/renovatebot/renovate), [Dependabot](https://github.com/dependabot/dependabot-core) | Abren pull requests que actualizan tus versiones fijadas | La GitHub Action puede ejecutarse en esas pull requests y listar las API que cambiaron, primero las que usa tu código. |
| Benchmarks: [GitChameleon 2.0](https://arxiv.org/abs/2507.12367), [VersiCode](https://arxiv.org/abs/2406.07411), [CodeUpdateArena](https://arxiv.org/abs/2407.06249), [LibEvolutionEval](https://arxiv.org/abs/2412.04478) | Miden modelos con conjuntos de tareas fijos, construidos a partir de cambios reales entre versiones o de cambios sintéticos (CodeUpdateArena); GitChameleon 2.0 ejecuta pruebas unitarias | Comparan modelos en general, y algunos comprueban el comportamiento ejecutando pruebas. since-cutoff examina las versiones fijadas de un proyecto, de forma estática: un verificador de tipos ve nombres, parámetros y obsolescencias, no el comportamiento. |

`--compare signatures` en `since-cutoff run` da al modelo la firma de la nueva versión y el primer
párrafo de su docstring. Es un sustituto local de una consulta de documentación, no Context7.

Dos herramientas más pequeñas abordan el mismo problema: [cutoff](https://github.com/sandeepsirodia/cutoff)
sondea una biblioteca que mantienes ejecutando programas escritos por el modelo con su versión
actual, y [postcut](https://github.com/justi/postcut) convierte un `Gemfile.lock` de Ruby en un
resumen de los cambios desde la fecha de corte. since-cutoff se basa en
[griffe](https://mkdocstrings.github.io/griffe/),
[basedpyright](https://github.com/DetachHead/basedpyright), [models.dev](https://models.dev) y [rich](https://github.com/Textualize/rich).

### Usar since-cutoff con Context7

`since-cutoff scan` te dice qué API consultar; Context7 puede aportar la documentación. Indica la
versión que fijas cuando preguntes («anthropic 1.8.0»). Context7 solo la encuentra si los
propietarios de la biblioteca [añadieron esa versión](https://github.com/upstash/context7/blob/master/docs/howto/claiming-libraries.mdx):
el 27 de septiembre de 2026, `/openai/openai-python` ofrecía v1.68.0, v1_105_0, v2.8.1 y v2.11.0,
y `/anthropics/anthropic-sdk-python` ninguna, así que puedes recibir la documentación de la rama
por defecto.

## Investigación relacionada

- **API obsoletas en el autocompletado de código.** Wang et al., *LLMs Meet Library Evolution:
  Evaluating Deprecated API Usage in LLM-based Code Completion* (ICSE 2025;
  [arXiv:2406.09834](https://arxiv.org/abs/2406.09834), titulado primero *How and Why LLMs Use
  Deprecated APIs in Code Completion? An Empirical Study*). 7 modelos, 145 correspondencias entre
  una API obsoleta y su sustituta en 8 bibliotecas de Python, 28 125 prompts de autocompletado. La
  mayoría de las respuestas no usaron ninguna de las dos API. De las que usaron una de ellas (las
  respuestas «plausibles», en términos del artículo), el 25-38 % usó la obsoleta en el conjunto de
  datos completo: el 70-90 % cuando el prompt venía de código que usaba la API obsoleta y el 9-18 %
  cuando venía de código actualizado. Se probaron dos correcciones de referencia en prompts de
  código actualizado en los que un modelo había usado la API obsoleta. ReplaceAPI sustituye los
  tokens de la API obsoleta por la sustituta durante la decodificación y deja que el modelo termine
  la línea: después se usó la sustituta en el 85,2-99,6 % de los casos con los seis modelos
  abiertos (necesita controlar la decodificación, así que no sirve con GPT-3.5). InsertPrompt añade
  el comentario `# {dep} is deprecated, use {rep} instead and revise the return value and arguments.`
  y vuelve a generar: 25,7-97,2 % según el modelo, algo que los autores aún no consideran
  suficientemente eficaz ni preciso. Las notas de since-cutoff se parecen a InsertPrompt, trasladado
  al archivo de instrucciones del proyecto; `since-cutoff run` las mide con tareas reservadas en
  lugar de dar por hecho que funcionan.
- **Tener la documentación en el contexto no basta por sí solo.** Ashik et al., *When LLMs Lag
  Behind: Knowledge Conflicts from Evolving APIs in Code Generation*
  ([arXiv:2604.09515](https://arxiv.org/abs/2604.09515), preprint de 2026). 270 actualizaciones
  reales de API (45 obsoletas o eliminadas, 128 modificadas, 97 nuevas) de versiones de 8
  bibliotecas de Python posteriores a diciembre de 2023, y 11 modelos de 4 familias con fechas de
  corte anteriores. Con solo una descripción de la actualización, los modelos la adoptaron al menos
  en parte en el 74,64 % de las respuestas (según GPT-5 mini como juez), y el 42,55 % de esas
  respuestas se ejecutó en la versión de la biblioteca que introdujo el cambio; con la documentación
  de la API además, el 92,87 % la adoptó y el 66,36 % se ejecutó. Añadir prompts de cadena de
  pensamiento (*chain-of-thought*) y autorreflexión (*self-reflection*) mejoró la tasa de ejecución
  otro 11,33 %, una mejora relativa y no en puntos porcentuales. De las respuestas que no adoptaron
  la actualización, el 42,1 % la ignoró por completo y el 16,4 % usó la API antigua; de las que la
  adoptaron pero aun así no se ejecutaron en la mejor configuración, la causa más común relacionada
  con la actualización fueron parámetros incorrectos (26,6 % de esos fallos). Por eso since-cutoff
  comprueba el código frente a tu versión exacta, y por eso `since-cutoff run` vuelve a probar el
  modelo con las notas en lugar de dar por hecho que las sigue.
- **Benchmarks.** [GitChameleon 2.0](https://arxiv.org/abs/2507.12367): 328 problemas de
  autocompletado en Python, cada uno ligado a versiones concretas de bibliotecas y comprobado con
  pruebas unitarias ejecutables; los modelos comerciales logran un 48-51 % de base, la
  documentación recuperada añade hasta unos 10 puntos (GPT-4.1: del 48,5 % al 58,5 %) y la
  autodepuración (*self-debugging*) unos 10-20. [VersiCode](https://arxiv.org/abs/2406.07411):
  autocompletado específico de una versión y migración de código consciente de la versión, con más
  de 300 bibliotecas de Python y más de 2000 versiones a lo largo de 9 años.
  [CodeUpdateArena](https://arxiv.org/abs/2407.06249): edición del conocimiento del modelo para 54
  funciones de 7 paquetes de Python, con actualizaciones sintéticas generadas por GPT-4 y 670
  ejemplos de síntesis de programas; anteponer la documentación de la actualización no permitió que
  los modelos abiertos (DeepSeek, CodeLlama) la usaran.
  [LibEvolutionEval](https://arxiv.org/abs/2412.04478)
  ([NAACL 2025](https://aclanthology.org/2025.naacl-long.348/)): autocompletado en línea específico
  de una versión en 8 bibliotecas; ayudan la documentación recuperada de la versión concreta y el
  diseño del prompt.

Estos estudios miden muchos modelos con conjuntos de tareas fijos; GitChameleon 2.0 y Ashik et al.
ejecutan el código generado. since-cutoff hace algo más acotado: para un proyecto, lista los cambios
desde una versión de comparación, primero los que usa tu código, y `run` comprueba de forma
estática las respuestas de un modelo. No ve cambios de comportamiento tras una firma que no cambia,
cosa que sí detectan las pruebas que ejecutan el código.

## Cómo contribuir

since-cutoff es un proyecto joven. La ayuda más útil ahora mismo:

- **Ejecútalo en tu proyecto** y publica lo que encontró, incluidos los falsos positivos, en
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
  («Comparte tus resultados», en inglés).
- **Empieza por un [good first issue](https://github.com/MohammadHijjawi97/since-cutoff/labels/good%20first%20issue)**:
  tareas pequeñas y autocontenidas, como otro formato de lockfile o una configuración
  predefinida para un proveedor.
- **Las tareas más grandes** llevan la etiqueta [help wanted](https://github.com/MohammadHijjawi97/since-cutoff/labels/help%20wanted),
  por ejemplo la [compatibilidad con TypeScript](https://github.com/MohammadHijjawi97/since-cutoff/issues/1).
- **Informa de errores o resultados extraños** en los [issues](https://github.com/MohammadHijjawi97/since-cutoff/issues).

[CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md) (en
inglés) explica la organización del código y las comprobaciones; el conjunto de pruebas sin
conexión ejecuta todo el proceso con una biblioteca de juguete y un modelo con respuestas
predefinidas, así que no hace falta ninguna clave de API. Problemas de seguridad:
[SECURITY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/SECURITY.md) (en inglés).

## Cómo citarlo

Si usas since-cutoff en una investigación, cítalo, por favor (consulta [`CITATION.cff`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CITATION.cff)).

## Licencia

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
