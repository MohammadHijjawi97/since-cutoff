<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo.svg" width="72" height="72" alt="logotipo de since-cutoff">
</picture></p>

<h1 align="center">since-cutoff</h1>

<!-- mcp-name: io.github.MohammadHijjawi97/since-cutoff -->

**Los datos de entrenamiento de tu asistente de programación terminan antes de las últimas
versiones de tus dependencias, así que puede escribir llamadas que tus versiones fijadas ya no
aceptan. since-cutoff muestra dónde usa tu código una API que cambió después de la fecha de corte
de entrenamiento del modelo, y escribe las notas breves que tu asistente necesita para no usar la
forma antigua.**

El [proyecto de ejemplo](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app)
llama a `client.messages.create` y fija anthropic 1.8.0. La versión más reciente de anthropic en
la fecha de corte de entrenamiento de Claude Sonnet 4.5 era la 0.60.0, cuyo `create` aún aceptaba
`temperature`; la 1.8.0 lanza `TypeError` con él. Lo que muestra `scan`, recortado a la parte de
anthropic:

```console
$ uvx since-cutoff scan --model anthropic:claude-sonnet-4-5
Your code uses 2 APIs that changed after claude-sonnet-4-5's training cutoff (2025-07-31)

huggingface-hub 0.34.3 -> 2.0.0 (0.34.3 was the latest release at the cutoff; pyproject.toml pins
  2.0.0)
  ...

anthropic 0.60.0 -> 1.8.0 (0.60.0 was the latest release at the cutoff; pyproject.toml pins 1.8.0)
  Messages.create: temperature, top_k and top_p were removed                          uses this API
    app/main.py   calls create
    Note: `Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword
          arguments. If the API still needs them, pass them through its `extra_body` or
          `extra_query` argument. since-cutoff found no replacement in anthropic's deprecation
          text. [diff]

2 notes ready: `since-cutoff sync` writes them to AGENTS.md and keeps them in step with
  pyproject.toml.
```

La llamada de `app/main.py` no pasa ninguno de esos parámetros, así que aparece como «uses this
API» (usa esta API) y no como «old form» (forma antigua): hoy funciona, pero un asistente que
escriba para la 0.60.0 podría añadir `temperature=0.2` al editar la llamada. La nota es lo que
`since-cutoff sync` escribe en AGENTS.md para evitarlo; su etiqueta, `[diff]`, indica en qué se
basa: una comparación estática de las API públicas de las dos versiones. Que un parámetro salga
de la firma no significa que la API haya dejado de aceptar el campo, así que, cuando el método
fijado tiene un argumento `extra_body` o `extra_query`, la nota lo dice en lugar de pedir al
asistente que quite el campo.

**Pruébalo en tu proyecto.** Sin clave de API y sin llamadas al modelo. En la raíz del proyecto:

```bash
uvx since-cutoff scan
```

Detecta tu modelo de programación (o pasa `--model`), lee tu lockfile, toma para cada dependencia
la versión más reciente publicada en la fecha de corte de entrenamiento del modelo o antes, y
compara la API pública de esa versión con la que tienes fijada, de forma estática: no se ejecuta
código de ningún paquete. La fecha de corte solo elige qué cambios mirar; no dice nada de lo que
el modelo haya memorizado. Si tu modelo se equivoca de verdad con ellos, y si las notas ayudan, lo
mide [`since-cutoff run`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#medir-tu-modelo);
llama a tu modelo y es opcional.

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)
[![since-cutoff MCP server on Glama](https://glama.ai/mcp/servers/MohammadHijjawi97/since-cutoff/badges/score.svg)](https://glama.ai/mcp/servers/MohammadHijjawi97/since-cutoff)

<p align="center"><a href="https://github.com/MohammadHijjawi97/since-cutoff/releases/download/v0.5.0/since-cutoff-explainer.mp4"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/explainer-thumbnail.png" width="560" alt="Mira el vídeo explicativo de 2 minutos y medio (con voz en off)"></a><br><a href="https://github.com/MohammadHijjawi97/since-cutoff/releases/download/v0.5.0/since-cutoff-explainer.mp4">▶ Mira el vídeo explicativo de 2 minutos y medio (con voz en off)</a></p>

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md) | **Español** | [Français](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md)

## El problema

Todo modelo tiene una fecha de corte de entrenamiento; tu lockfile, en cambio, no deja de
moverse. Cuando una biblioteca cambia su API pública después de esa fecha, un modelo cuyos datos
de entrenamiento son anteriores al cambio puede seguir escribiendo las llamadas antiguas. Parte de
ese código falla al importarse o al ejecutar la llamada. Otra parte sigue funcionando, porque la
forma antigua solo está marcada como obsoleta, o todavía se acepta con un aviso.

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
El diff estático señala 310 cambios incompatibles y 23 obsolescencias nuevas; el código del
proyecto usa 2 de las API modificadas.

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
   del modelo o antes, compara su API pública con la versión que tienes fijada y muestra cuáles de
   las API modificadas usa tu código, dónde, y una nota para cada una. Sin llamadas al modelo y
   sin clave de API.
2. **`sync`** escribe esas notas en un bloque delimitado de AGENTS.md (o CLAUDE.md) después de
   mostrarte el diff, y las mantiene al día con tu lockfile; `sync --check` y `status` avisan a la
   CI, a pre-commit y a tu agente cuando se quedan atrás. Cada nota se redacta a partir del diff
   de la API y lleva una etiqueta que dice qué se comprobó. Sin llamadas al modelo.
3. **`run`**, opcional, le plantea al modelo tareas breves de programación que requieren las API
   modificadas, sin herramientas ni documentación, y puntúa cada respuesta con un verificador de
   tipos frente a *ambas* versiones: *stale* (desactualizada), *wrong* (incorrecta), *deprecated*
   (obsoleta) o *correct* (correcta). Ningún LLM juzga nada. Para cada fallo escribe una nota,
   conserva una nota escrita por el modelo solo si su ejemplo pasa la verificación de tipos con tu
   versión, y vuelve a evaluar el modelo con tareas reservadas (*held-out*), sin las notas y con
   ellas.

El mismo diff está disponible para los agentes a través de un [servidor MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-desde-cualquier-agente-mcp)
y para la integración continua (CI) a través de una [GitHub Action y hooks de pre-commit](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-en-ci).

## Inicio rápido

```bash
# las API modificadas que usa tu código, con una nota para cada una (sin llamadas al modelo ni clave de API)
uvx since-cutoff scan

# escribe esas notas en AGENTS.md (muestra el diff y pregunta antes); repítelo tras actualizar
uvx since-cutoff sync

# opcional: mide en qué cambios se equivoca tu modelo y prueba las notas (llama al modelo)
uvx --with basedpyright since-cutoff run
```

También puedes instalarlo con `pipx install since-cutoff` (o `pip install since-cutoff`) y
ejecutar `since-cutoff`. Ejecútalo desde la raíz del proyecto: lee `uv.lock`, `poetry.lock`,
`pdm.lock`, `pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`, `Pipfile` o
un `.venv` (no `setup.py` ni `setup.cfg`). Sin `--model`, usa el modelo con el que está
configurado tu agente de programación, según los ajustes de Claude Code, Codex, OpenCode o
Aider; para cualquier otro modelo, pasa `--model` (consulta [Elegir el modelo](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#elegir-el-modelo)).
`scan`, `sync` y `status` no llaman a ningún modelo ni necesitan clave de API; `run` envía
prompts al proveedor del modelo y consume tus créditos de API o tu cuota de uso de Claude Code.

Lo que muestra `scan` para el proyecto de ejemplo:

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="since-cutoff scan --model anthropic:claude-sonnet-4-5 en el proyecto de ejemplo. Tu código usa 2 API que cambiaron después de la fecha de corte de entrenamiento de claude-sonnet-4-5 (2025-07-31). huggingface-hub 0.34.3 -> 2.0.0: hf_hub_download, usada en app/main.py, ya no tiene force_filename, local_dir_use_symlinks, resume_download ni proxies en su firma; su nota lleva la etiqueta [diff], y una línea Runtime dice que el código fuente de la 2.0.0 todavía los gestiona, así que las llamadas que los pasan pueden ejecutarse con un aviso. anthropic 0.60.0 -> 1.8.0: Messages.create, llamada en app/main.py, ya no acepta temperature, top_k ni top_p; su nota lleva la etiqueta [diff]. 2 notas listas para AGENTS.md; qué significan uses this API y [diff]; otros 323 cambios en 7 paquetes que el código no usa."></p>

Las dos API aparecen como «uses this API»; un archivo que pasara `resume_download=True` o
`temperature=0.2` las convertiría en «old form». `scan -v` lista todos los archivos que usan una
API (se muestran 3), y `scan --all` añade todos los demás cambios, paquete por paquete.
`scan --json` y `.since-cutoff/results.json` tienen lo mismo en `used_apis` (cada API, dónde se
usa, sus cambios y su nota, con las etiquetas de la nota, las versiones a las que se aplica y lo
que se comprobó), y `.since-cutoff/report.md` empieza por «Used by your code».

### Mantener las notas al día: sync y status

`since-cutoff sync` (desde la versión 0.4.0) escribe las notas que muestra `scan` en un bloque
delimitado: en AGENTS.md, o en CLAUDE.md si solo existe ese archivo, y, si ya hay un bloque, en
ese (`--target` indica otro archivo). Muestra un diff unificado y pregunta
`Write this to AGENTS.md? [y/N]`; `--yes` escribe sin preguntar, y si no hay una terminal en la
que preguntar no escribe nada. El texto fuera del bloque conserva sus bytes, incluidos los saltos
de línea CRLF, y `since-cutoff unapply` elimina el bloque. Para el proyecto de ejemplo, el bloque
termina así:

```markdown
**anthropic 1.8.0** (0.60.0 at the cutoff)
- `Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword arguments. If the API still needs them, pass them through its `extra_body` or `extra_query` argument. since-cutoff found no replacement in anthropic's deprecation text. [diff]

**huggingface-hub 2.0.0** (0.34.3 at the cutoff)
- `huggingface_hub.hf_hub_download()` no longer accepts `proxies`, `force_filename`, `local_dir_use_symlinks` or `resume_download`; do not pass them. huggingface-hub's deprecation text says there is no replacement for `force_filename`, `local_dir_use_symlinks` or `resume_download`. since-cutoff found no replacement for `proxies` in huggingface-hub's deprecation text. [diff]
<!-- since-cutoff:end -->
```

Encima de las viñetas están el marcador de inicio, una línea de metadatos (el modelo, su fecha de
corte, de dónde salen las versiones, un hash de las dependencias y otro del propio texto del
bloque, para que se note una edición a mano) y una cabecera que nombra el modelo, la fecha de
corte y el archivo del que salen las versiones, explica qué significan las etiquetas y dice que no
se ejecutó código de ninguna biblioteca. La advertencia «Runtime:» queda fuera del bloque: el
consejo («do not pass them», no los pases) es el mismo en cualquier caso.

Vuelve a ejecutar `sync` cuando cambies el lockfile o el código. Añade notas para las API que tu
código empiece a usar, vuelve a comprobar un paquete actualizado y quita las notas de un paquete
cuando deja de ser una dependencia, ya no es más nuevo que la versión de la fecha de corte o tu
código ya no usa sus API modificadas, y dice por qué. Conserva el modelo y la fecha de corte para
los que se escribió el bloque (así, los compañeros cuyos agentes usan otros modelos no lo
reescriben una y otra vez) salvo que pases `--model` o `--cutoff`; `--model a,b` usa la más
temprana de sus fechas de corte. Si nada cambió, no escribe nada, y el archivo conserva sus bytes
y su fecha de modificación.

| comando | qué hace | código de salida |
|---|---|---|
| `since-cutoff sync` | muestra el diff, pregunta y escribe | 0 escrito o ya al día; 3 no escrito (respondiste que no, o no hay terminal en la que preguntar) |
| `since-cutoff sync --yes` | escribe sin preguntar | 0 |
| `since-cutoff sync --dry-run` | muestra el diff y no escribe nada | 0 |
| `since-cutoff sync --check` | no escribe nada (para CI y pre-commit) | 0 al día; 3 desactualizado |
| `since-cutoff status` | compara el bloque con el lockfile, sin conexión | 0 al día, o sin bloque; 3 desactualizado; 1 marcadores rotos |

Si el bloque se editó a mano, `sync` y `sync --check` muestran el diff, no escriben nada y salen
con el código 4; `sync --force` lo sustituye. Si no se puede comprobar un paquete del que tratan
las notas (PyPI inaccesible), `sync` no escribe nada y sale con el código 1.

`since-cutoff status` no usa la red ni lee el código: para cada paquete, la versión para la que
son las notas y la que tiene el lockfile, si cambiaron otras dependencias, y si el modelo con el
que está configurado tu agente de programación tiene una fecha de corte anterior a la de las
notas (con el comando `sync --model` correspondiente). `status --json` es para scripts.
`status --hook` muestra una línea solo cuando las notas están desactualizadas y siempre sale con
0, por ejemplo:

```text
since-cutoff: the library notes in AGENTS.md are out of date: anthropic 1.8.0 in the notes, 0.60.0 in pyproject.toml. `since-cutoff sync` updates them.
```

Más opciones:

- `sync --scope imported` también anota los cambios con más probabilidad de importar en cada
  paquete modificado que importa tu código (hasta 5 API por paquete), para código que aún no usa
  ninguna de las API modificadas. `scan` lo sugiere en ese caso.
- `sync --suggestions` añade los nombres de la versión fijada que solo se parecen a lo que se
  eliminó, con la etiqueta `[not confirmed]`. El bloque registra ambas opciones, y las siguientes
  ejecuciones de sync las mantienen.
- Las notas que escribió `since-cutoff run --apply` llevan la etiqueta `[type-checked]`. `sync`
  conserva cada una, en lugar de la nota del diff para la misma API, mientras su paquete mantenga
  la misma versión y tu código siga usando esa API.

Claude Code lee CLAUDE.md, y solo lee AGENTS.md cuando no hay CLAUDE.md o cuando CLAUDE.md lo
importa con una línea `@AGENTS.md` ([documentación sobre la memoria](https://code.claude.com/docs/en/memory),
en inglés). Si las notas van a AGENTS.md y CLAUDE.md no lo importa, `scan` y `sync` lo avisan.
Escribir en ambos archivos es el
[#13](https://github.com/MohammadHijjawi97/since-cutoff/issues/13).

### Medir tu modelo

`since-cutoff run` es el paso opcional que llama a un modelo. Le plantea tareas breves que
requieren las API modificadas, sin herramientas, sin documentación y sin nada de tu código;
puntúa las respuestas con un verificador de tipos frente a ambas versiones; escribe una nota para
cada fallo, y prueba las notas con tareas reservadas
([Cómo funciona](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#cómo-funciona)).
Consume tus créditos de API o tu cuota de uso de Claude Code y puede tardar entre 5 y 20 minutos.

```bash
since-cutoff run --quick    # una ejecución más pequeña: 12 sondeos, 1 tarea reservada, 3 comprobaciones de regresión
since-cutoff run --apply    # escribe las notas de esta ejecución en el bloque
```

Sin `--apply`, las notas se muestran pero no se escriben. `run --apply` sustituye el bloque por
las notas de esa ejecución; un `since-cutoff sync` posterior añade las notas del diff para las
demás API modificadas que usa tu código y conserva las notas `[type-checked]` de la ejecución
mientras la versión de su paquete no cambie.

### En Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Después, pídele a Claude: «Comprueba en cuáles de nuestras dependencias estás desactualizado»,
o ejecuta `/since-cutoff:since-cutoff`. La skill ejecuta `since-cutoff scan` y ofrece las notas:
ejecuta `since-cutoff sync --dry-run`, te muestra el diff y solo escribe si estás de acuerdo. Si
le pides que mida el modelo, la medición la hace una instancia nueva del modelo, sin
herramientas, así que el agente no puede evaluarse a sí mismo. El plugin también inicia el
[servidor MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#uso-desde-cualquier-agente-mcp),
para que Claude pueda consultar los cambios de una biblioteca antes de escribir código, y (desde
la versión 0.4.0) ejecuta `since-cutoff status --hook` al empezar una sesión, lo que añade una
línea a la sesión cuando las notas están desactualizadas (la primera vez, uvx descarga
since-cutoff).

### En otros agentes de programación

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

Esto instala la misma skill mediante la CLI de código abierto [skills](https://github.com/vercel-labs/skills)
para Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode y otros agentes que leen `SKILL.md`.
since-cutoff también lee el modelo de los ajustes de Codex, OpenCode y Aider; para otros
agentes, indícale qué modelo usar, por ejemplo `since-cutoff scan --model openai:gpt-5.4`.
Añade el servidor MCP como se muestra más abajo.

Prompts que funcionan bien:

- «¿Qué API de las que usa nuestro código cambiaron después de tu fecha de corte de
  entrenamiento?». El agente ejecuta `since-cutoff scan` o llama a la herramienta MCP
  `project_changes`.
- «Añade notas sobre ellas a AGENTS.md». El agente ejecuta `since-cutoff sync --dry-run`, te
  muestra el diff y ejecuta `since-cutoff sync --yes` cuando estés de acuerdo.
- «Mide en cuáles de esos cambios te equivocas de verdad». El agente te pide permiso primero y
  luego ejecuta `since-cutoff run --quick`.
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

La columna «requiere» es para `run`, que llama al modelo; `scan` y `sync` solo usan su fecha de
corte de entrenamiento. Sin `--model`, las versiones 0.3.0 y posteriores usan el modelo con el
que está configurado tu agente de programación, y la línea del modelo indica de dónde procede
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

Si ningún ajuste indica un modelo, usa el modelo predeterminado de Claude Code y así lo dice.
Solo se leen los campos del modelo, y un nombre de modelo que no sabe identificar detiene la
ejecución con un mensaje que indica el ajuste. Un modelo al que un agente accede a través de
otro servicio (GitHub Copilot, Amazon Bedrock, Vertex AI) se nombra según su fabricante, así que
`run` llama a la API del fabricante (`openai:` requiere `OPENAI_API_KEY`). `sync` conserva el
modelo para el que se escribió un bloque, sea cual sea el que use ahora tu agente.

Las fechas de corte de entrenamiento se obtienen de [models.dev](https://models.dev) (el paquete
incluye una instantánea para usarla sin conexión). `since-cutoff models sonnet` las lista;
`--cutoff 2025-07` fija la fecha manualmente, y `since-cutoff scan --cutoff 2025-07` sin `--model`
analiza tomando solo esa fecha como referencia. `scan` y `sync` solo necesitan la fecha de
corte, así que también aceptan un identificador de modelo sin proveedor (`claude-haiku-4-5`,
`sonnet`) o con cualquier proveedor que figure en models.dev (`google:gemini-2.5-pro`, incluidos
los identificadores de Amazon Bedrock y Vertex AI); `run` necesita un proveedor de la tabla
anterior.

## Resultados

Lo que se ha medido hasta ahora, cada cosa con su alcance:

- **Un proyecto, un modelo, since-cutoff 0.1.0**: la tarjeta de abajo, Claude Opus 4.6 en el
  proyecto de ejemplo. La tabla que le sigue añade Claude Haiku 4.5 en el mismo proyecto.
- **Benchmark**: 360 sesiones de Claude Code en modo headless (`claude-opus-5-5`) con 24 tareas
  de Python con tests ocultos, con el protocolo congelado antes de la ejecución principal. En las
  17 tareas posteriores al corte, las sesiones con las notas de since-cutoff 0.4.1 costaron 0,80
  veces lo que Claude Code solo (IC del 95 %: 0,70-0,90), con 0,83 veces los turnos y 0,87 veces
  el tiempo. Ninguna conclusión sobre la tasa de aciertos: Claude Code solo ya superó el 94,1 % de
  esas tareas, por encima del techo prerregistrado del 90 %. Las variantes con Context7 se
  ejecutaron sin clave de API y desde la sesión 215 de 360 solo recibieron «Monthly quota
  exceeded», lo que no afecta a la comparación entre las notas y Claude Code solo. Un modelo, un
  agente: [resultados](https://mohammadhijjawi97.github.io/since-cutoff/benchmark.html) (en inglés),
  [tareas, protocolo y transcripciones](https://github.com/MohammadHijjawi97/since-cutoff-benchmark).
- **Informes de desarrolladores independientes**: se listarán aquí, cada uno con su proyecto, su
  modelo y su fecha. Publica el tuyo en
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
  («Comparte tus resultados», en inglés).

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.es.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.es.svg" width="640" alt="Tu modelo de IA para programar aprendió tus bibliotecas antes de que cambiaran. Claude Opus 4.6 en un único proyecto de ejemplo, medido con since-cutoff 0.1.0: en 7 de 16 cambios de API sondeados usó un nombre o un parámetro que ya se había eliminado; con las notas, los aciertos en 20 tareas reservadas pasaron del 5 % al 65 %. Pruébalo: uvx since-cutoff scan (sin llamadas al modelo, sin clave de API).">
</picture></p>

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

Las notas escritas en la ejecución con Claude Haiku 4.5 (extracto literal; formato de
since-cutoff 0.1.0):

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
Aun así, el consejo de omitirlo es correcto. La nota que la versión 0.4.0 escribe a partir del
diff de la API para esos mismos argumentos está en
[Mantener las notas al día](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md#mantener-las-notas-al-día-sync-y-status);
la advertencia sobre el tiempo de ejecución aparece en la terminal, el informe y el JSON, no en
el bloque.

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
| `project_changes(project_dir, model)` | lo mismo para cada dependencia de un proyecto en su versión fijada, empezando por las API modificadas que usa tu código: para cada una, los archivos que la usan (3 como máximo), su nota, la advertencia sobre el tiempo de ejecución y los nombres parecidos, no confirmados como sustitutos |
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

Lo que devuelve `api_changes("huggingface-hub", model="claude-haiku-4-5", to_version="2.0.0")`
(salida real, recortada):

```markdown
# huggingface-hub 0.29.1 -> 2.0.0

- From 0.29.1 (2025-02-20): the newest release on or before 2025-02-28 (training cutoff of claude-haiku-4-5, from models.dev)
- To 2.0.0 (2026-09-24): as requested
- 109 breaking changes, 0 new deprecations (dependencies switched 1, removed or moved 56, parameters removed 43, parameters now required 7, changed kind 1, now keyword-only or positional-only 1)

## Dependencies switched

- huggingface-hub requires `httpx2` instead of `requests`; its Requires-Dist lists `httpx2<3,>=2.0.0`, and `requests` only for its `gradio` extra; 6 places in its public API that named `requests` types name `httpx2` types: `get_session()` returns `httpx2.Client`, `HfHubHTTPError(response=...)` takes `httpx2.Response` and `HfFileSystemStreamFile.response` is `httpx2.Response`; `InferenceTimeoutError`, `HfHubHTTPError` and `TextGenerationError` derive from `httpx2.HTTPError` instead of `requests.HTTPError`

## Removed or moved

- `huggingface_hub.InferenceApi` was removed
...

## Parameters removed

- `huggingface_hub.InferenceClient.text_generation(stop_sequences=...)`: parameter `stop_sequences` was removed; the old docs said: Deprecated argument. Use `stop` instead; also changed under 1 other path, e.g. `huggingface_hub.AsyncInferenceClient.text_generation`
...
- `huggingface_hub.snapshot_download(proxies=...)`: parameter `proxies` was removed; 2.0.0's source still handles `proxies` (huggingface_hub/utils/_validators.py:178), so calls passing it may run with a warning; type checkers reject it
...

Not listed: 69 breaking changes, 0 new deprecations (removed or moved 41, parameters removed 28). Narrow with symbol="..." or raise limit.
```

Con `symbol="hf_hub_download"` solo lista los 8 cambios de esa función (`resume_download=`,
`force_filename=`, `local_dir_use_symlinks=` y `proxies=`, en la función y en `HfApi`). `symbol`
también acepta una llamada tal como se escribe en el código: `client.messages.create` encuentra
los cambios de `Messages.create`. El diff lee firmas: esos cuatro parámetros salieron de la firma
en huggingface-hub 1.0, y la respuesta añade que el código fuente de la 2.0.0 todavía los
gestiona, así que las llamadas que los pasan pueden ejecutarse con un aviso.

## Uso en CI

### GitHub Action

Analiza el proyecto en cada pull request y añade un resumen a la página del trabajo: las API
modificadas que usa tu código, con los archivos que las usan, el cambio, si se usan en la forma
antigua y el sustituto; después, plegados, las notas y los cambios de cada dependencia. Con
`check-notes: true` también hace fallar el trabajo cuando las notas de AGENTS.md están
desactualizadas. Igual que `scan`, solo lee PyPI y models.dev: sin llamadas al modelo y sin clave
de API.

```yaml
# .github/workflows/since-cutoff.yml
name: since-cutoff
on:
  pull_request:
    paths: ["**/*.lock", "**/pylock*.toml", "**/requirements*.txt", "**/pyproject.toml", "**/AGENTS.md", "**/CLAUDE.md"]
jobs:
  scan:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      - uses: MohammadHijjawi97/since-cutoff@v0
        with:
          model: anthropic:claude-sonnet-4-5  # el modelo con el que programa tu equipo
          check-notes: true                   # falla cuando AGENTS.md necesita `since-cutoff sync`
```

Sin `paths`, el trabajo también se ejecuta cuando un cambio en el código empieza a usar una API
modificada.

| entrada | valor predeterminado | |
|---|---|---|
| `model` | obligatorio | `provider:model`, como en `--model`; solo se usa su fecha de corte de entrenamiento |
| `working-directory` | `.` | el directorio del proyecto |
| `only`, `exclude` | | nombres de PyPI separados por comas |
| `cutoff` | | sustituye la fecha de corte de entrenamiento (`YYYY-MM` o `YYYY-MM-DD`) |
| `fail-on-changes` | `false` | hace fallar el paso cuando una dependencia cambió su API después de la fecha de corte |
| `check-notes` | `false` | ejecuta también `since-cutoff sync --check`, que no escribe nada, y hace fallar el trabajo cuando las notas de AGENTS.md / CLAUDE.md están desactualizadas o el bloque se editó a mano; las notas conservan el modelo para el que se escribieron, y `model` (y `cutoff`) sirven para un proyecto que aún no tiene bloque |
| `step-summary` | `true` | añade el resumen en Markdown al resumen del trabajo |
| `cache` | `true` | conserva entre ejecuciones los metadatos de PyPI, el código fuente de los paquetes y los diffs de API (también cuando `fail-on-changes` hace fallar el trabajo) |
| `args` | | más argumentos para `since-cutoff scan`, p. ej. `--all-deps --limit 20` |
| `since-cutoff-version` | `0.5.0` | la versión de since-cutoff que se ejecuta, o `latest` |

`args: --fail-on old-form --annotate github` solo hace fallar el trabajo cuando tu código usa una
API modificada en la forma antigua, y anota cada archivo que usa una: un aviso (*warning*) para la
forma antigua y una nota (*notice*) en los demás casos.

Salidas: `changed-packages` (separados por comas), `changes` (cambios incompatibles),
`deprecations`, `markdown` (la ruta del resumen, por ejemplo para publicarlo como comentario en la
pull request), `report` (la ruta del informe completo) y, con `check-notes`, `notes`
(`up-to-date`, `out-of-date`, `edited-by-hand` o `error`). Un cambio accesible desde varias rutas
de importación se cuenta una sola vez.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.5.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # añade --fail-on=old-form para bloquear el commit
      - id: since-cutoff-sync  # mantiene al día las notas de AGENTS.md; args: [--check] solo comprueba
```

`since-cutoff-scan` se ejecuta cuando cambia un lockfile, un archivo de requirements o
`pyproject.toml`, y muestra el resultado del análisis; pásale `--model` en `args`.
`since-cutoff-sync` se ejecuta cuando cambia uno de esos archivos, AGENTS.md o CLAUDE.md, y
actualiza las notas; si modifica el archivo, el hook falla, como cualquier hook de pre-commit que
modifica archivos: añade el archivo y vuelve a hacer el commit. Con `args: [--check]` no modifica
nada y falla mientras las notas estén desactualizadas. Conserva el modelo para el que se
escribieron las notas; para un primer bloque, indícale uno:
`args: [--model=anthropic:claude-sonnet-4-5]`. Los dos necesitan acceso a PyPI, así que omítelos
en pre-commit.ci (`ci: {skip: [since-cutoff-scan, since-cutoff-sync]}`). `since-cutoff-status`
no necesita red: falla cuando las notas no coinciden con el lockfile.

### Otros sistemas de CI

```bash
# resumen en Markdown para cualquier CI; código de salida 3 si tu código usa una API modificada en la forma antigua
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on old-form

# código de salida 3 si las notas de AGENTS.md están desactualizadas, 4 si el bloque se editó a mano
since-cutoff sync --check

# medir también el modelo (requiere su clave de API o la CLI claude)
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` escribe el resumen en stdout, y en stderr solo el progreso y la ruta del informe,
igual que `--json`. En un archivo, una tubería o un registro de CI no hay barra de progreso
animada, y la salida se compone con 160 columnas de ancho (`COLUMNS` fija otro ancho). Códigos
de salida:

- `0` éxito;
- `1` error (por ejemplo, `run` no pudo sondear ningún cambio de API o no pudo puntuar ninguna
  respuesta del modelo, o `sync` no pudo comprobar un paquete del que tratan sus notas);
- `2` error de uso;
- `3` una comprobación falló: `scan --fail-on changes`, `used` u `old-form` (una dependencia
  cambió su API después de la fecha de corte, tu código usa una API modificada o la usa en la
  forma antigua; `--fail-on-changes` equivale a `--fail-on changes`), `run --fail-on-stale` (se
  detectó uso de API desactualizadas) o notas desactualizadas con `sync --check` o `status` (y
  con `sync`, cuando no las escribió);
- `4` `sync`: el bloque se editó a mano y no se escribió nada sin `--force`;
- `141` la salida se cerró antes de tiempo (por ejemplo, al pasarla por una tubería a `head`).

## Cómo funciona

`scan` y `sync` no necesitan ningún modelo: comparan las API públicas, encuentran dónde usa tu
código las que cambiaron y redactan cada cambio a partir del diff, con una etiqueta que dice qué se
comprobó ([detalles](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md#notes-without-a-model-scan-and-sync),
en inglés). `run` pasa por tres etapas. La primera es el análisis y no necesita ningún modelo; en
las otras dos, un verificador de tipos puntúa cada respuesta y comprueba cada nota que escribe el
modelo:

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.es.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.es.svg" width="640" alt="Tres etapas. Análisis (scan), sin llamadas al modelo: el lockfile da tus versiones exactas, después se busca la versión publicada en la fecha de corte del modelo y se hace un diff estático de la API con griffe. Sondeo: tareas breves que requieren el cambio, el modelo responde de memoria y basedpyright comprueba la respuesta con ambas versiones. Escribir y probar las notas: una nota escrita por el modelo solo se conserva si su ejemplo pasa la verificación de tipos; si no, se usa una descripción del cambio tomada del diff de la API. Las tareas reservadas se responden sin las notas y con ellas, y --apply escribe un bloque en AGENTS.md.">
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

### Qué significa «verified»

since-cutoff no usa la palabra «verified» (verificado) sin más. Cada nota lleva una etiqueta que
dice qué se comprobó, y no se afirma nada más:

| etiqueta | qué se comprobó | qué no |
|---|---|---|
| `[diff]` | El cambio aparece en una comparación estática (griffe) de las API públicas de dos versiones: la más reciente publicada en la fecha de corte de entrenamiento del modelo o antes, y la versión que fija tu proyecto. El código fuente se lee, no se importa. Con `[diff]` solo, no se nombra ningún sustituto: la nota dice lo que dice el propio texto de obsolescencia de la biblioteca («there is no replacement for `resume_download`», no hay sustituto) o que since-cutoff no encontró ninguno en él. | El comportamiento, y si una llamada sigue funcionando: la versión fijada puede seguir aceptando un parámetro eliminado con un aviso, como hace huggingface-hub 2.0.0 con `resume_download`. La terminal, report.md, las herramientas MCP y el JSON añaden una línea «Runtime:» cuando el código fuente fijado todavía gestiona uno; el bloque no, porque el consejo es el mismo. Si tu modelo se equivoca con él. |
| `[diff + library]` | Como `[diff]`, y el propio texto de obsolescencia de la biblioteca (un docstring, la entrada de un parámetro en el docstring, un mensaje de `@deprecated` o un texto de `warnings.warn`, en la versión anterior o, para una obsolescencia, en la fijada) indica el sustituto («Use `stop` instead»), y ese nombre existe en tu versión fijada. Un texto que solo menciona un nombre como consejo se cita bajo `[diff]`; no se toma como sustituto. | Que el sustituto se comporte igual. |
| `[diff + move checked]` | Como `[diff]`, y el objeto en la nueva ruta es el mismo objeto hasta donde se puede contar: una clase o un módulo conserva al menos la mitad de los nombres públicos del anterior, una función conserva sus parámetros, un valor es el mismo. | El comportamiento. |
| `[diff + metadata]` | El Requires-Dist de la versión anterior (el METADATA de su wheel) incluye una biblioteca que la versión fijada ya no incluye, y los lugares de la API pública que nombraban tipos de esa biblioteca (parámetros, tipos de retorno, atributos, clases base, reexportaciones) nombran tipos de otra biblioteca que la versión fijada requiere, o de una copia de la anterior que incluye, sin que quede ninguno de la anterior: openai 3.x, anthropic 1.8, huggingface-hub 2.0 y mcp 2.2 aceptan objetos de `httpx2` donde aceptaban objetos de `httpx`. | El comportamiento: si la versión fijada todavía acepta objetos de la biblioteca anterior (openai 3 convierte algunos, anthropic 1.8 lanza `TypeError`, según su código fuente). La terminal, report.md, las herramientas MCP y el JSON añaden una línea «Installed:» (si tu proyecto, incluido su entorno virtual, todavía tiene la biblioteca anterior) y una línea «Runtime:» que señala dónde el código fuente fijado todavía la nombra. |
| `[diff; probable rename]` | Un parámetro en la misma posición y con la misma anotación tiene un nombre nuevo, y ninguna nota de versión del docstring de la versión fijada (`.. versionadded::`, `.. versionchanged::`) dice que uno se añadió o que el otro se eliminó. Una suposición, señalada como tal. | Que sea el mismo parámetro. |
| `[type-checked]` | La escribió un modelo durante `since-cutoff run` y se conservó porque su ejemplo pasó la comprobación de tipos descrita abajo. | El comportamiento; que la explicación de la viñeta sea cierta más allá de los nombres que muestra. |
| `[not confirmed]` | Solo con `sync --suggestions`: nombres de la versión fijada que se parecen a lo que se eliminó (la etiqueta de la viñeta es entonces `[diff; not confirmed]`). | Que alguno de ellos lo sustituya. |

Las etiquetas se combinan: las pruebas se unen con `+` (`[diff + library]`), y una suposición va
después de `;` (`[diff; probable rename]`, `[diff; not confirmed]`).

Los nombres que solo se parecen nunca se escriben en las notas por defecto. La terminal,
report.md y las herramientas MCP los muestran como «not confirmed as replacements» (no confirmados
como sustitutos), y no los muestran en absoluto cuando la biblioteca dice que no hay sustituto;
`sync --suggestions` los añade, con la etiqueta `[not confirmed]`.

**La comprobación de tipos detrás de `[type-checked]`.** Una nota escrita por el modelo solo se
conserva si se cumple todo esto (el modelo tiene dos intentos):

- su ejemplo completo se analiza sin errores e importa el paquete;
- basedpyright (modo estándar, obsolescencias tratadas como errores, sin los comentarios
  `# type: ignore` ni `# pyright:`) no informa de ningún error atribuido a ese paquete, ni de
  ninguna obsolescencia suya, al comprobar el ejemplo frente al código fuente de la versión exacta
  de tu lockfile más las dependencias de ejecución de esa versión, en un entorno por lo demás
  vacío;
- cada nombre de la biblioteca que la viñeta pone entre comillas invertidas aparece en ese ejemplo
  o en la entrada del diff de la API de la que trata la nota;
- la viñeta tiene como máximo 60 palabras.

Así, `[type-checked]` significa que las importaciones, los nombres, los parámetros y el número de
argumentos que usa el ejemplo existen en tu versión y no están marcados con `@deprecated` (PEP
702). Los errores ajenos al paquete (la biblioteca estándar, otras bibliotecas) no bloquean una
nota. No significa que el código se comporte bien en tiempo de ejecución, que el sustituto sea el
que recomiendan los mantenedores ni que la nota ayude al modelo. Si una nota no pasa la
comprobación, since-cutoff escribe en su lugar la nota del diff de la API, con su etiqueta.

**Medido** es otra cosa: `since-cutoff run` responde a tareas reservadas sin las notas y con
ellas e informa de los recuentos. Nada más en since-cutoff dice si una nota ayuda.

**En `run`, una respuesta cuenta como correcta** si importa el paquete, usa la API modificada y
no tiene ningún error de «conocimiento» atribuido al paquete en tu versión (nombre, importación o
parámetro desconocidos; falta un argumento obligatorio; número incorrecto de argumentos). Las
quejas que son solo de rigor de tipos se ignoran. No se ejecuta ninguna respuesta.

El bloque de AGENTS.md contiene las viñetas con sus etiquetas y, para cada paquete, la versión a
la que se aplican sus notas y la versión de la fecha de corte. El resto está en `scan --json` y
`results.json`: `used_apis[]` (cada API modificada que usa tu código, dónde, sus cambios, sus
sustitutos con su origen, y su nota con `tags`, `applies_to` y `checks`) y, después de `run`,
`notes_detail[]` (cada nota con el ejemplo del modelo y lo que midió la prueba con tareas
reservadas; `verified` se mantiene y significa lo mismo que `checks.example_type_checks`).

## Qué ejecuta, qué envía y qué guarda

- **No ejecuta código de los paquetes ni código escrito por el modelo.** Los paquetes se leen de
  forma estática (griffe con la inspección desactivada; solo se extraen archivos `.py`/`.pyi`,
  con comprobaciones de ruta y tamaño). Las respuestas del modelo solo pasan por la verificación
  de tipos, en local, con basedpyright.
- **Descarga** metadatos públicos y archivos wheel de los paquetes desde PyPI, y las fechas de
  corte de los modelos desde models.dev (el paquete incluye una instantánea para usarla sin
  conexión). Las dependencias
  de Git, de ruta local, de workspace o de índices privados nunca se buscan por nombre en el PyPI
  público. `status` no descarga nada.
- **Envía** prompts solo en `run`, y solo al proveedor de modelos que elijas: nombres de paquetes,
  versiones, firmas públicas y docstrings de las API modificadas, las tareas generadas y, para
  las notas, las propias respuestas del modelo. Nunca tu código fuente. `scan`, `sync`, `status`,
  el servidor MCP, la GitHub Action y los hooks de pre-commit no envían nada a ningún modelo.
- **Muestra dónde usa tu código una API modificada** (por ahora, nombres de archivo) en la
  terminal y en `.since-cutoff/`, y no lo envía a ningún sitio. Lo que compartas depende de ti:
  `--markdown` y `--annotate github` lo ponen en el resumen y las anotaciones de un trabajo de CI,
  y la herramienta MCP `project_changes` se lo da al agente que la llamó, que pasa los resultados
  de las herramientas a su modelo.
- **Guarda** los resultados en `.since-cutoff/` dentro de tu proyecto (el propio directorio se
  excluye de git) y en una caché local (`since-cutoff cache path` la muestra y
  `since-cutoff cache clear` la borra). `sync` (cuando aceptas, o con `--yes`) y `run --apply`
  escriben un único bloque delimitado en AGENTS.md o CLAUDE.md y dejan el resto del archivo
  intacto, byte a byte; `since-cutoff unapply` elimina el bloque.
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
- «Your code uses» (tu código usa) es una coincidencia estática de nombres, archivo por archivo:
  importaciones (incluidos los nombres reexportados), llamadas, lecturas de atributos y argumentos
  por nombre. No sigue accesos dinámicos como `getattr`, y por ahora indica archivos, no líneas
  ([#8](https://github.com/MohammadHijjawi97/since-cutoff/issues/8)). Un parámetro que pasó a ser
  obligatorio, solo por nombre o solo posicional aparece siempre como «uses this API», nunca como
  «old form», por ahora.
- Una nota solo nombra un sustituto cuando el propio texto de obsolescencia de la biblioteca lo
  indica. Los consejos que solo están en una guía de migración (el
  [MIGRATION.md](https://github.com/anthropics/anthropic-sdk-python/blob/main/MIGRATION.md) de
  anthropic sugiere `extra_body` para modelos antiguos que aún aceptan `temperature`) no están en
  las notas.
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
| Bots de dependencias: [Renovate](https://github.com/renovatebot/renovate), [Dependabot](https://github.com/dependabot/dependabot-core) | Abren pull requests que actualizan tus versiones fijadas | La GitHub Action puede ejecutarse en esas pull requests, listar las API modificadas que usa tu código y los archivos que las usan y, con `check-notes`, fallar hasta que las notas estén sincronizadas. |
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
