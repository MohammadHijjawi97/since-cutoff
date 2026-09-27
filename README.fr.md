<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo.svg" width="96" height="96" alt="logo de since-cutoff">
</picture></p>

<h1 align="center">since-cutoff</h1>

**Pour les projets Python écrits avec un agent de code : since-cutoff repère les API de vos
dépendances qui ont changé après la date limite d'entraînement du modèle, mesure celles sur
lesquelles le modèle se trompe et corrige ces erreurs avec de courtes notes AGENTS.md, chacune
validée par un vérificateur de types ou tirée directement du diff d'API.**

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.fr.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.fr.svg" width="640" alt="Votre modèle de code a appris vos bibliothèques avant qu'elles ne changent. Claude Opus 4.6 sur un seul projet d'exemple, mesuré avec since-cutoff 0.1.0 : sur 7 des 16 changements d'API sondés, il a utilisé un nom ou un paramètre supprimé depuis ; avec les notes, la part de tâches réservées correctes est passée de 5 % à 65 % (20 tâches). Essayez : uvx since-cutoff scan (aucun appel au modèle, aucune clé d'API).">
</picture></p>

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md) | [Español](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md) | **Français**

## Le problème

Chaque modèle a une date limite d'entraînement ; votre fichier de verrouillage, lui, continue
d'évoluer. Quand une bibliothèque modifie son API publique après cette date, un modèle qui a
appris l'ancienne version continue d'écrire les anciens appels. Une partie de ce code échoue à
l'import ou à l'appel. Une autre partie s'exécute encore, parce que l'ancienne façon de faire
n'est que dépréciée.

Quelques-uns des changements que `since-cutoff scan` trouve pour Claude Sonnet 4.5 (date limite
d'entraînement : juillet 2025) dans le [projet d'exemple](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
qui épingle six de ses neuf dépendances sur les versions actuelles (pour les trois autres, non
épinglées, l'outil utilise la dernière version) :

| bibliothèque | version à la date limite | épinglée | ce qui casse |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` n'est plus accepté |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` supprimés |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()` et `llm.predict()` supprimés |
| openai | 1.98.0 | 3.19.2 | 21 changements incompatibles, 6 nouvelles dépréciations |

Dans ce projet, 7 dépendances sur 9 ont modifié leur API publique après la date limite. Le diff
statique signale 317 changements incompatibles et 23 nouvelles dépréciations ; une partie
concerne des éléments internes, que les sondes ignorent.

Ce n'est pas l'affaire d'un seul modèle ni d'un seul fournisseur. Sur 36 bibliothèques Python
d'IA très utilisées et 21 modèles d'OpenAI, Anthropic, Google, xAI, DeepSeek, Qwen, Moonshot et
Mistral, même le modèle le plus récent testé (Claude Opus 5.5, date limite de juin 2026) précède
un changement incompatible de l'API publique de 20 des 36
([résultats complets](https://mohammadhijjawi97.github.io/since-cutoff/ai-stack.html), en anglais) :

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/ai-stack.svg" width="860" alt="Diagramme en barres : pour chacun de 21 modèles de 8 fournisseurs, combien de 36 bibliothèques Python d'IA ont modifié leur API publique de façon incompatible et combien sont passées à une nouvelle version majeure depuis la date limite d'entraînement du modèle. De 21 sur 36 pour GPT-4o (13 des bibliothèques n'existaient pas encore) à 33 sur 36 pour les modèles dont la date limite se situe début 2025, et 20 sur 36 pour Claude Opus 5.5 (juin 2026).">
</picture></p>

Pour y remédier, since-cutoff fait trois choses :

1. **`scan`** trouve, pour chaque dépendance, la version la plus récente publiée au plus tard à
   la date limite du modèle et compare son API publique à celle de la version que vous épinglez.
   Aucun appel au modèle, aucune clé d'API.
2. **`run`** soumet au modèle de courtes tâches de programmation qui nécessitent les API
   modifiées, sans outils ni documentation, et classe chaque réponse en *stale* (périmée : valide
   seulement pour l'ancienne version), *wrong* (erronée), *deprecated* (dépréciée) ou *correct* à
   l'aide d'un vérificateur de types appliqué aux *deux* versions. Aucun LLM ne sert de juge.
3. **Notes** : pour chaque échec, il rédige une note d'une ligne pour AGENTS.md / CLAUDE.md. Une
   note rédigée par le modèle n'est conservée que si son exemple passe la vérification de types
   avec votre version ; sinon, un simple constat du changement tiré du diff d'API la remplace.
   Il teste ensuite à nouveau le modèle sur des tâches réservées (held-out), avec et sans les
   notes.

Le même diff est accessible aux agents via un [serveur MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-depuis-nimporte-quel-agent-mcp)
et à la CI via une [action GitHub et un hook pre-commit](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-en-ci).

## Démarrage rapide

```bash
# lister les changements d'API depuis la date limite de votre modèle (aucun appel au modèle, aucune clé d'API)
uvx since-cutoff scan

# sonder le modèle, rédiger des notes vérifiées et les ajouter à AGENTS.md
uvx since-cutoff run --apply
```

Vous pouvez aussi l'installer avec `pipx install since-cutoff` (ou `pip install since-cutoff`)
puis exécuter `since-cutoff`. Lancez-le depuis la racine de votre projet : il lit `uv.lock`,
`poetry.lock`, `pdm.lock`, `pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`,
`Pipfile` ou un `.venv` (pas `setup.py` ni `setup.cfg`). Sans `--model`, il teste le modèle
configuré pour votre agent de code, d'après les réglages de Claude Code, Codex, OpenCode ou
Aider ; pour tout autre modèle, passez `--model` (voir
[Choisir le modèle](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#choisir-le-modèle)).
`scan` est gratuit ; `run` envoie des prompts au fournisseur du modèle et consomme vos crédits
d'API ou votre quota Claude Code.

Ce qu'affiche `scan` pour le projet d'exemple :

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="since-cutoff scan --model anthropic:claude-sonnet-4-5 sur le projet d'exemple : 7 dépendances sur 9 ont modifié leur API après la date limite ; diff statique : 317 changements incompatibles, 23 nouvelles dépréciations ; un tableau donnant, pour chaque paquet, la version épinglée, la version à la date limite et le nombre de changements, et un exemple de changement par paquet"></p>

### Dans Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Demandez ensuite à Claude : « Vérifie pour lesquelles de nos dépendances tes connaissances ne
sont plus à jour », ou lancez `/since-cutoff:since-cutoff`. Le skill exécute la CLI ; la mesure
elle-même est effectuée par une nouvelle instance du modèle, sans outils, si bien que l'agent ne
peut pas s'évaluer lui-même. Le plugin démarre aussi le
[serveur MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-depuis-nimporte-quel-agent-mcp),
pour que Claude puisse consulter les changements d'une bibliothèque avant d'écrire du code.

### Dans d'autres agents de code

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

Cette commande installe le même skill via la CLI open source [skills](https://github.com/vercel-labs/skills),
pour Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode et les autres agents qui lisent
`SKILL.md`. since-cutoff lit aussi le modèle dans les réglages de Codex, OpenCode et Aider ; pour
les autres agents, indiquez-lui quel modèle tester, par exemple
`since-cutoff scan --model openai:gpt-5.4`. Ajoutez le serveur MCP comme indiqué plus bas.

Des prompts qui fonctionnent bien :

- « Lesquelles de nos dépendances ont modifié leur API publique après ta date limite
  d'entraînement ? » L'agent lance `since-cutoff scan` ou appelle l'outil MCP `project_changes`.
- « Mesure sur lesquels de ces changements tu te trompes vraiment, et ajoute les notes vérifiées
  à AGENTS.md. » L'agent vous demande d'abord votre accord, puis lance
  `since-cutoff run --quick --apply`.
- « Avant d'écrire le code httpx, vérifie ce qui a changé dans httpx depuis ta date limite. »
  L'agent appelle l'outil MCP `api_changes`.

### Choisir le modèle

| `--model` | utilise | nécessite |
|---|---|---|
| `claude-code` (par défaut si aucun réglage n'indique de modèle) | votre connexion Claude Code (abonnement ou clé), modèle actuel | la CLI `claude` |
| `claude-code:sonnet`, `claude-code:claude-haiku-4-5` | un modèle Claude précis | la CLI `claude` |
| `anthropic:<model>` | API Anthropic | `ANTHROPIC_API_KEY` |
| `openai:<model>` | API OpenAI | `OPENAI_API_KEY` |
| `openrouter:<vendor/model>` | OpenRouter | `OPENROUTER_API_KEY` |
| `deepseek:<model>` | API DeepSeek | `DEEPSEEK_API_KEY` |
| `ollama:<model>` | Ollama en local | Ollama en cours d'exécution |
| `openai-compatible:<model>` | tout serveur compatible OpenAI | `--base-url`, `OPENAI_API_KEY` facultative |

Sans `--model`, les versions 0.3.0 et suivantes testent le modèle configuré pour votre
agent de code, et la ligne du modèle indique d'où il vient (« model from .claude/settings.json ») :

1. `SINCE_CUTOFF_MODEL` (une spécification complète comme `openai:gpt-5.4`) l'emporte toujours.
2. Dans Claude Code (qui définit `CLAUDECODE=1` pour les commandes qu'il exécute), seuls
   comptent les réglages de Claude Code : `ANTHROPIC_MODEL`, puis `.claude/settings.local.json`
   et `.claude/settings.json` du projet, puis `~/.claude/settings.json`.
3. Ailleurs, le réglage le plus spécifique l'emporte : d'abord `ANTHROPIC_MODEL` ou
   `AIDER_MODEL`, puis les réglages du projet, en commençant par le dossier le plus proche,
   depuis le dossier analysé jusqu'à la racine du dépôt (jamais le dossier personnel), puis les
   réglages de l'utilisateur. Dans un même dossier, les agents comptent dans cet ordre :

| agent | réglages du projet | réglages de l'utilisateur |
|---|---|---|
| Claude Code | `.claude/settings.local.json`, `.claude/settings.json` | `~/.claude/settings.json` |
| Codex | `.codex/config.toml`, avec son profil sélectionné | `$CODEX_HOME/config.toml` ou `~/.codex/config.toml` |
| OpenCode | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/` |
| Aider | `.aider.conf.yml`, avec les alias d'Aider (`4o`, `flash`, `r1`, ...) | `~/.aider.conf.yml` |

Si aucun réglage n'indique de modèle, il teste le modèle par défaut de Claude Code et le signale.
Seuls les champs du modèle sont lus, et un nom de modèle qu'il ne sait pas identifier arrête
l'exécution avec un message qui nomme le réglage. Un modèle auquel un agent accède via un autre
service (GitHub Copilot, Amazon Bedrock, Vertex AI) est désigné d'après l'entreprise qui l'a conçu,
si bien que `run` appelle l'API de celle-ci (`openai:` nécessite `OPENAI_API_KEY`).

Les dates limites d'entraînement proviennent de [models.dev](https://models.dev) (un instantané
est inclus pour une utilisation hors ligne). `since-cutoff models sonnet` les affiche ;
`--cutoff 2025-07` remplace la date, et `since-cutoff scan --cutoff 2025-07` sans `--model`
analyse par rapport à cette seule date. `scan` n'a besoin que de la date limite : il accepte donc
aussi un identifiant de modèle sans fournisseur (`claude-haiku-4-5`, `sonnet`) ou avec n'importe
quel fournisseur répertorié par models.dev (`google:gemini-2.5-pro`, identifiants Amazon Bedrock
et Vertex AI compris) ; `run` a besoin d'un fournisseur du tableau ci-dessus.

## Résultats

Deux modèles Claude sur le projet d'exemple à 9 dépendances de
[`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
**mesurés avec since-cutoff 0.1.0**, les tâches et les notes étant rédigées par Claude Opus 4.6 :

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| date limite d'entraînement | févr. 2025 | mai 2025 |
| changements d'API sondés | 20 | 16 |
| **stale** / wrong / deprecated / correct | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| bibliothèques avec des appels *stale* | 3 sur 5 sondées | 2 sur 4 sondées |
| notes rédigées (dont validées par le vérificateur de types) | 8 (7), environ 391 jetons | 10 (7), environ 437 jetons |
| **tâches réservées correctes, sans -> avec notes** | **14 % -> 57 %** (14 paires) | **5 % -> 65 %** (20 paires) |
| API déjà correctes, une fois les notes ajoutées | 6/6 toujours correctes | 6/6 toujours correctes |

Les tâches *réservées* (held-out) sont des reformulations de la tâche qui a servi à sonder chaque
changement sur lequel le modèle a échoué ; le modèle répond deux fois à chacune, sans puis avec
les notes, et les réponses sont évaluées de la même façon. La dernière ligne revérifie les API que le modèle
utilisait déjà correctement, pour repérer les notes qui aggravent les choses.

Dans cet échantillon, le modèle le plus puissant n'était pas plus sûr : Opus 4.6 a utilisé des
API supprimées après sa date limite, dont `anthropic.HUMAN_PROMPT` avec `client.completions`.
Exemples de code périmé tirés des deux exécutions, chacun valide pour la version que le modèle
a apprise et cassé pour la version épinglée : `messages.create(temperature=...)` (anthropic 1.8),
`hf_hub_download(resume_download=...)`, `local_dir_use_symlinks=...`, `force_filename=...` et
`proxies=...` (huggingface-hub 2.0), et `client.beta.vector_stores` (openai 3.x).

Les notes rédigées lors de l'exécution Claude Haiku 4.5 (extrait, texte original) :

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

Voici le résumé affiché dans le terminal pour l'exécution Claude Opus 4.6, enregistré avec la
version 0.1.0. Les résultats des sondes sont ceux du tableau ci-dessus. Les chiffres du diff sur
l'image sont ceux de la 0.1.0 (« 725 changes flagged ») ; après des corrections apportées au
diff, le `scan` de la 0.2.0 signale 513 changements incompatibles et 48 nouvelles dépréciations
pour la même date limite. Le nombre de changements corrigés (« changes fixed ») et son IC à 95 %
suivent eux aussi la 0.1.0 : l'intervalle porte sur ce nombre, pas sur les taux de 5 % et 65 %,
et jusqu'à la 0.2.0 un changement était compté comme corrigé même quand une réponse réservée
était déjà correcte sans les notes.

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run-opus.svg" width="100%" alt="Exécution de since-cutoff 0.1.0 sur Claude Opus 4.6 : utilisation d'API périmées dans 2 des 4 dépendances sondées ; 16 changements d'API sondés : 7 stale, 0 wrong, 3 deprecated, 6 correct ; 10 notes ; tâches réservées correctes sans -> avec notes : 5 % -> 65 % (20 tâches appariées) ; une liste des appels périmés"></p>

<details>
<summary>Le même résumé pour l'exécution Claude Haiku 4.5 (également enregistré avec la 0.1.0 ; pour sa date limite, le scan de la 0.2.0 signale 491 changements incompatibles et 50 nouvelles dépréciations)</summary>
<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/run.svg" width="100%" alt="Exécution de since-cutoff 0.1.0 sur Claude Haiku 4.5 : utilisation d'API périmées dans 3 des 5 dépendances sondées ; 20 changements d'API sondés : 5 stale, 1 wrong, 2 deprecated, 12 correct ; 8 notes ; tâches réservées correctes sans -> avec notes : 14 % -> 57 % (14 tâches appariées)"></p>
</details>

Petits échantillons, deux modèles, un seul projet : voyez-y une démonstration de la méthode, pas
un benchmark. Chaque exécution écrit son rapport complet (chaque tâche, chaque réponse et chaque
erreur du vérificateur de types) dans `.since-cutoff/report.md`. Pour reproduire l'expérience
avec la version actuelle (son diff et son classement ont changé, les sondes ne seront donc pas
identiques) :
`cd examples/agent-app && since-cutoff run --model claude-code:claude-haiku-4-5 --task-model claude-code:claude-opus-4-6`.
Depuis la 0.3.0, une exécution peut aussi enregistrer les tâches utilisées : ajoutez
`--tasks-out tasks.json`, et chacun pourra refaire l'exécution sur exactement les mêmes tâches avec
`--tasks-from tasks.json`, pour un autre modèle ou un autre jeu de notes. Le fichier indique qui a
rédigé les tâches (modèle, version du prompt, version de since-cutoff), et une exécution sur des
tâches réutilisées le mentionne dans son rapport
([détails](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md#2-probe),
en anglais). Les résultats obtenus sur vos propres projets sont les bienvenus dans la discussion
[Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
(« Partagez vos résultats », en anglais).

Le fonctionnement de la mesure, et ce que ces chiffres montrent ou non, en détail :
[l'article en français](https://mohammadhijjawi97.github.io/since-cutoff/fr/).

## Utilisation depuis n'importe quel agent (MCP)

`since-cutoff mcp` est un serveur MCP qui permet à un agent de code de demander « qu'est-ce qui
a changé dans cette bibliothèque depuis ma date limite d'entraînement ? » avant d'écrire du code.
Il propose trois outils en lecture seule :

| outil | répond à |
|---|---|
| `api_changes(package, model, symbol=...)` | ce qui a changé dans une bibliothèque entre la version à la date limite du modèle et la dernière version (ou une version donnée), incompatibilités franches en premier |
| `project_changes(project_dir, model)` | la même chose pour chaque dépendance d'un projet, à sa version épinglée, en commençant par les API que votre code utilise déjà |
| `model_cutoff(model)` | la date limite d'entraînement d'un modèle, d'après [models.dev](https://models.dev) |

L'agent transmet son propre identifiant de modèle : la réponse couvre donc ce que ce modèle n'a
pas pu voir. Les outils lisent PyPI et les sources des paquets de façon statique :
aucun appel au modèle, aucune clé d'API, aucun code de paquet exécuté.

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

**Cursor** (`~/.cursor/mcp.json`) et **Claude Desktop** (`claude_desktop_config.json`)

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
# ou sous forme d'extension, qui démarre le même serveur :
gemini extensions install https://github.com/MohammadHijjawi97/since-cutoff
```

Avec `@latest`, `uvx` récupère les nouvelles versions au lieu de réutiliser la première qu'il a
mise en cache (le `.mcp.json` du plugin, lui, épingle la version exacte). Si le client ne trouve
pas `uvx`, installez [uv](https://docs.astral.sh/uv/) ou indiquez le chemin complet
(`which uvx`). Le serveur est référencé dans le
[MCP Registry](https://registry.modelcontextprotocol.io) sous le nom
`io.github.MohammadHijjawi97/since-cutoff`.

Sur un projet de grande taille, le premier appel à `project_changes` télécharge les wheels de chaque
dépendance modifiée et peut prendre plusieurs minutes (les très gros paquets comme transformers
sont les plus longs). Les résultats sont mis en cache : les appels suivants ne prennent que
quelques secondes. Pour préchauffer le cache, lancez une fois `since-cutoff scan` dans le
projet ; il partage son cache avec le serveur. Les clients dont le délai d'expiration par défaut
des outils est court peuvent avoir besoin d'un délai plus long, comme dans l'exemple Codex
ci-dessus. `project_changes` limite sa réponse à environ 24 000 caractères : les dépendances
modifiées qui n'y tiennent pas occupent une ligne chacune, et les passer dans `only` affiche
leurs changements.

Ce qu'a renvoyé `api_changes("huggingface-hub", model="claude-haiku-4-5")` avec la 0.2.0 (sortie
réelle, abrégée ; les versions suivantes affinent le diff, leurs chiffres diffèrent donc
légèrement) :

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

Avec `symbol="hf_hub_download"`, il ne liste que les 8 changements de cette fonction
(`resume_download=`, `force_filename=`, `local_dir_use_symlinks=` et `proxies=`, sur la fonction
et sur `HfApi`). `symbol` accepte aussi un appel tel que le code l'écrit : `client.messages.create`
trouve les changements de `Messages.create`.

## Utilisation en CI

### GitHub Action

L'action analyse le projet à chaque pull request et ajoute un résumé à la page du job : pour
chaque dépendance, la version à la date limite du modèle, la version que vous épinglez et les
principaux changements, en commençant par ceux qui touchent des noms utilisés par votre code.
Comme `scan`, elle ne lit que PyPI et models.dev : aucun appel au modèle, aucune clé d'API.

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
          model: anthropic:claude-sonnet-4-5  # le modèle que votre équipe utilise pour coder
```

| entrée | valeur par défaut | |
|---|---|---|
| `model` | obligatoire | `provider:model`, comme pour `--model` ; seule sa date limite d'entraînement est utilisée |
| `working-directory` | `.` | le répertoire du projet |
| `only`, `exclude` | | noms PyPI séparés par des virgules |
| `cutoff` | | remplace la date limite d'entraînement (`YYYY-MM` ou `YYYY-MM-DD`) |
| `fail-on-changes` | `false` | fait échouer l'étape quand une dépendance a modifié son API après la date limite |
| `step-summary` | `true` | ajoute le résumé Markdown au résumé du job |
| `cache` | `true` | conserve entre les exécutions les métadonnées PyPI, les sources des paquets et les diffs d'API (y compris quand `fail-on-changes` fait échouer le job) |
| `args` | | arguments supplémentaires pour `since-cutoff scan`, par exemple `--all-deps --limit 20` |
| `since-cutoff-version` | `0.3.0` | la version de since-cutoff à exécuter, ou `latest` |

Sorties : `changed-packages` (liste séparée par des virgules), `changes` (changements incompatibles),
`deprecations`, `markdown` (le chemin du résumé, par exemple pour le publier en commentaire de
la pull request) et `report` (le chemin du rapport complet). Un changement accessible par
plusieurs chemins d'import n'est compté qu'une fois.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.3.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # ajoutez --fail-on-changes pour bloquer le commit
```

Le hook s'exécute quand un fichier de verrouillage, un fichier `requirements*.txt` ou
`pyproject.toml` change, et affiche le résultat du scan. Passez-lui `--model` dans `args`. Il a besoin d'accéder
à PyPI : désactivez-le donc sur pre-commit.ci (`ci: {skip: [since-cutoff-scan]}`).

### Autres CI

```bash
# résumé Markdown pour n'importe quelle CI ; code de sortie 3 si une dépendance a modifié son API après la date limite
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on-changes

# mesurer aussi le modèle (nécessite sa clé d'API, ou la CLI claude)
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` écrit le résumé sur stdout, et seulement la progression et le chemin du rapport
sur stderr, comme `--json`. Dans un fichier, un pipe ou un journal de CI, il n'y a pas de barre
de progression animée, et la sortie est mise en page sur 160 colonnes (`COLUMNS` fixe une autre
largeur). Codes de sortie : `0` OK, `1` erreur (par exemple, `run` n'a pu sonder aucun
changement d'API ou n'a pu évaluer aucune réponse du modèle), `2` erreur d'utilisation, `3`
utilisation d'API périmée (stale) détectée avec `run --fail-on-stale`, ou changements d'API
détectés avec `scan --fail-on-changes`, `141` la sortie a été fermée prématurément (par
exemple, redirigée vers `head`).

## Fonctionnement

Trois étapes. La première ne fait appel à aucun modèle ; dans les deux autres, un vérificateur
de types évalue chaque réponse et contrôle chaque note rédigée par le modèle :

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.fr.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.fr.svg" width="640" alt="Trois étapes. Analyse (scan), sans appel au modèle : le fichier de verrouillage donne vos versions exactes, puis la version à la date limite du modèle, puis un diff statique de l'API avec griffe. Sonde : de courtes tâches qui nécessitent le changement, le modèle répond de mémoire, basedpyright vérifie la réponse par rapport aux deux versions. Correction et vérification : une note rédigée par le modèle n'est conservée que si son exemple passe la vérification de types, sinon un constat du changement tiré du diff d'API la remplace ; le modèle répond aux tâches réservées sans puis avec les notes, et --apply écrit un bloc dans AGENTS.md.">
</picture></p>

| résultat | signification |
|---|---|
| **stale** | le code est valide pour la version que connaissait le modèle et invalide pour la vôtre, et l'erreur porte sur une API qui a changé |
| **wrong** | invalide pour votre version, sans que cela s'explique par un changement (API inventée ou mal utilisée) |
| **deprecated** | valide, mais utilise une API marquée `@deprecated` dans votre version |
| **correct** | valide pour votre version, et utilise effectivement l'API modifiée |
| untouched / off-task / invalid / error | exclus de tous les taux, et toujours signalés |

Depuis la 0.3.0, le résultat des tâches réservées donne les taux par
tâche sans -> avec notes (avec le nombre de tâches appariées et de changements d'API dont elles
proviennent), leur différence avec un intervalle bootstrap à 95 % qui rééchantillonne les
changements d'API, les changements corrigés par les notes (faux sans elles, justes avec) avec un
intervalle de Wilson à 95 %, les changements qu'elles ont cassés, un test des signes exact des
corrigés contre les cassés, et les paires non comptées, par motif. La vérification de
non-régression indique combien d'API que le modèle utilisait déjà correctement le restent avec
les notes.

`run --compare template,signatures` (depuis la 0.3.0) fait aussi répondre aux
tâches réservées et aux vérifications de non-régression avec des notes de référence qui ne
demandent aucun modèle : `template` énonce chaque changement en échec en une phrase tirée du diff
d'API, et `signatures` donne la nouvelle signature et le premier paragraphe de la docstring de
chaque API modifiée, ou de celle que sa bibliothèque désigne pour la remplacer. Tous les blocs sont
évalués sur les mêmes paires, par rapport aux mêmes réponses sans notes, et la taille de chaque
bloc est indiquée en jetons : une exécution montre ainsi ce qu'apportent les notes vérifiées.

Tout est évalué par un vérificateur de types sur les versions exactes des paquets, chacune dans
un environnement isolé avec les dépendances d'exécution propres à ce paquet. Aucun LLM ne sert
de juge, et chaque chiffre peut être retrouvé dans `results.json`. Détails (en anglais) :
[docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md).

## Ce qu'il exécute, envoie et stocke

- **N'exécute aucun code de paquet ni aucun code écrit par le modèle.** Les paquets sont lus de
  façon statique (griffe avec l'inspection désactivée ; seuls les fichiers `.py`/`.pyi` sont
  extraits, avec des contrôles de chemin et de taille). Les réponses du modèle sont uniquement
  soumises au vérificateur de types, en local, avec basedpyright.
- **Récupère** les métadonnées publiques et les wheels des paquets sur PyPI, et les dates limites
  des modèles sur models.dev (un instantané est inclus pour une utilisation hors ligne). Les
  dépendances Git, locales (path), de workspace ou issues d'un index privé ne sont jamais
  recherchées par leur nom sur le PyPI public.
- **Envoie** des prompts uniquement lors de `run`, et uniquement au fournisseur de modèle que vous
  choisissez : noms et versions des paquets, signatures publiques et docstrings des API
  modifiées, tâches générées et, pour les notes, les propres réponses du modèle. Jamais votre
  code source. `scan`, le serveur MCP, l'action GitHub et le hook pre-commit n'envoient jamais
  rien à un modèle.
- **Stocke** les résultats dans `.since-cutoff/` au sein de votre projet (le dossier s'exclut
  lui-même de git) et dans un cache local (`since-cutoff cache path` indique son emplacement,
  `since-cutoff cache clear` le supprime). Avec `--apply`, il écrit un seul bloc balisé dans
  `AGENTS.md`/`CLAUDE.md` et laisse le reste du fichier intact, octet pour octet ;
  `since-cutoff unapply` retire le bloc.
- **Aucune télémétrie**, aucun compte, aucune donnée personnelle. Les exécutions suivantes sont
  servies par le cache : elles sont donc gratuites et reproductibles (`--fresh` interroge à
  nouveau le modèle). Voir [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md)
  (en anglais).

## Limites

- Python uniquement pour l'instant. TypeScript (diffs de `.d.ts`, `tsc`) est la prochaine étape
  ([#1](https://github.com/MohammadHijjawi97/since-cutoff/issues/1)).
- Un vérificateur de types voit les noms et paramètres erronés, ainsi que les dépréciations
  PEP 702. Il ne voit pas les changements de comportement derrière une signature inchangée, ni
  les dépréciations qui ne déclenchent un avertissement qu'à l'exécution. `scan` liste aussi les
  dépréciations déclarées avec le décorateur propre à une bibliothèque (dont le nom contient
  « deprecat ») et les noms supprimés qu'un module sert encore, avec un avertissement, via
  `__getattr__`, mais `run` ne les sonde pas.
- Le diff couvre l'API publique : les noms `_private`, ainsi que les suites de tests, benchmarks
  et exemples livrés dans un paquet, sont ignorés.
- Les sondes portent sur un **échantillon** classé des changements incompatibles (en commençant
  par les symboles que votre code utilise déjà), pas sur leur totalité.
- « La version que le modèle a vue » est la version la plus récente publiée au plus tard à la
  date limite. Les modèles connaissent moins bien les versions récentes, si bien que leurs
  connaissances peuvent être périmées plus tôt.
- Les tâches réservées sont des reformulations du même changement : elles montrent qu'une note
  corrige *ce* changement, pas que le modèle s'est amélioré en général.

## Comparaison avec d'autres outils

| type d'outil | ce qu'il fait | positionnement de since-cutoff |
|---|---|---|
| Serveurs MCP de recherche de documentation : [Context7](https://github.com/upstash/context7), [Ref](https://github.com/ref-tools/ref-tools-mcp), [docs-mcp-server](https://github.com/arabold/docs-mcp-server) | fournissent à l'agent la documentation à jour quand il consulte une bibliothèque, au moment de répondre | complémentaire : since-cutoff identifie les changements sur lesquels ce modèle se trompe, ce qui indique où une recherche ou une note est nécessaire, et conserve une courte note vérifiée dans le dépôt |
| Skills livrés avec les bibliothèques : [library-skills](https://github.com/tiangolo/library-skills), [pydantic/skills](https://github.com/pydantic/skills) | les mainteneurs de la bibliothèque livrent des consignes pour agents avec le paquet, en phase avec chaque version | fonctionne avec n'importe quel paquet PyPI, y compris ceux qui ne livrent aucune consigne, et mesure si le modèle en a besoin |
| Bots de dépendances : [Renovate](https://github.com/renovatebot/renovate), [Dependabot](https://github.com/dependabot/dependabot-core) | ouvrent des pull requests qui mettent à jour vos versions épinglées | l'action GitHub peut s'exécuter sur ces pull requests et lister les changements d'API que le modèle n'a pas vus |
| Benchmarks : [GitChameleon 2.0](https://arxiv.org/abs/2507.12367), [VersiCode](https://arxiv.org/abs/2406.07411), [CodeUpdateArena](https://arxiv.org/abs/2407.06249), [LibEvolutionEval](https://arxiv.org/abs/2412.04478) | mesurent la façon dont les modèles gèrent les versions de bibliothèques, sur des ensembles de tâches fixes et historiques | mesure ce modèle-ci sur vos versions épinglées, et valide le correctif avec un vérificateur de types |

Deux outils plus modestes s'attaquent au même problème : [cutoff](https://github.com/sandeepsirodia/cutoff)
teste une bibliothèque que vous maintenez en exécutant des programmes écrits par le modèle sur sa
version actuelle, et [postcut](https://github.com/justi/postcut) transforme un `Gemfile.lock`
Ruby en un récapitulatif des changements intervenus depuis la date limite. since-cutoff s'appuie
sur [griffe](https://mkdocstrings.github.io/griffe/),
[basedpyright](https://github.com/DetachHead/basedpyright), [models.dev](https://models.dev) et
[rich](https://github.com/Textualize/rich).

## Contribuer

since-cutoff est un projet jeune. L'aide la plus utile en ce moment :

- **Lancez-le sur votre projet** et publiez ce qu'il a trouvé, faux positifs compris, dans
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
  (« Partagez vos résultats », en anglais).
- **Prenez une [good first issue](https://github.com/MohammadHijjawi97/since-cutoff/labels/good%20first%20issue)** :
  des tâches courtes et autonomes, comme un nouveau format de fichier de verrouillage ou un
  préréglage de fournisseur.
- **Les chantiers plus importants** portent l'étiquette [help wanted](https://github.com/MohammadHijjawi97/since-cutoff/labels/help%20wanted),
  par exemple la [prise en charge de TypeScript](https://github.com/MohammadHijjawi97/since-cutoff/issues/1).
- **Signalez un bug ou un résultat étrange** dans les [issues](https://github.com/MohammadHijjawi97/since-cutoff/issues).

[CONTRIBUTING.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CONTRIBUTING.md)
(en anglais) décrit l'organisation du code et les vérifications ; la suite de tests hors ligne
exécute tout le pipeline avec une bibliothèque jouet et un modèle scripté, donc aucune clé d'API
n'est nécessaire. Problèmes de sécurité : [SECURITY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/SECURITY.md)
(en anglais).

## Citation

Si vous utilisez since-cutoff dans vos travaux de recherche, merci de le citer (voir
[`CITATION.cff`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/CITATION.cff)).

## Licence

MIT © [Mohammad Hijjawi](https://github.com/MohammadHijjawi97)
