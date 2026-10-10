<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo-dark.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/logo.svg" width="72" height="72" alt="logo de since-cutoff">
</picture></p>

<h1 align="center">since-cutoff</h1>

<!-- mcp-name: io.github.MohammadHijjawi97/since-cutoff -->

**Les données d'entraînement de votre assistant de code s'arrêtent avant les dernières versions
de vos dépendances : il peut donc écrire des appels que vos versions épinglées n'acceptent plus.
since-cutoff montre où votre code utilise une API qui a changé après la date limite
d'entraînement du modèle, et rédige les courtes notes dont votre assistant a besoin pour éviter
l'ancienne forme.**

since-cutoff est un outil en ligne de commande et un serveur MCP pour les projets Python. `scan`
lit votre fichier de verrouillage, prend pour chaque dépendance la dernière version publiée au
plus tard à la date limite d'entraînement de votre modèle de code, compare l'API publique de
cette version à celle que vous épinglez, de façon statique, et montre lesquelles des API modifiées
votre code utilise, où, avec une note pour chacune. `sync` écrit les notes dans AGENTS.md ou
CLAUDE.md et les tient à jour avec le fichier de verrouillage ; `status`, des hooks pre-commit et
une action GitHub vous préviennent quand elles ne le sont plus. `run`, facultatif, mesure sur
lesquels des changements votre modèle se trompe vraiment et si les notes l'aident. Seul `run`
appelle un modèle.

[![PyPI](https://img.shields.io/pypi/v/since-cutoff)](https://pypi.org/project/since-cutoff/)
![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)
[![CI](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml/badge.svg)](https://github.com/MohammadHijjawi97/since-cutoff/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/LICENSE)
![Status: beta](https://img.shields.io/badge/status-beta-orange)
[![since-cutoff MCP server on Glama](https://glama.ai/mcp/servers/MohammadHijjawi97/since-cutoff/badges/score.svg)](https://glama.ai/mcp/servers/MohammadHijjawi97/since-cutoff)

[English](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.md) | [简体中文](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.zh-CN.md) | [Español](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.es.md) | **Français**

## Installation

Python 3.10 ou plus récent, sous Linux, macOS ou Windows.

```bash
pipx install since-cutoff    # ou : pip install since-cutoff
```

Ou lancez-le sans l'installer : `uvx since-cutoff scan` télécharge la version courante et
l'exécute (`uvx since-cutoff@latest scan` prend les nouvelles versions au lieu de réutiliser la
première que uv a mise en cache).

Lancez-le depuis la racine de votre projet. Il lit `uv.lock`, `poetry.lock`, `pdm.lock`,
`pylock.toml`, `Pipfile.lock`, `requirements*.txt`, `pyproject.toml`, `Pipfile` ou un `.venv`
(pas `setup.py` ni `setup.cfg`). Sans `--model`, il utilise le modèle configuré pour votre agent
de code, d'après les réglages de Claude Code, Codex, Gemini CLI, OpenCode ou Aider ; pour tout
autre modèle, passez `--model` (voir
[Choisir le modèle](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#choisir-le-modèle)).
`scan`, `sync` et `status` n'appellent aucun modèle et ne demandent aucune clé d'API ; `run`
envoie des prompts au fournisseur du modèle et consomme vos crédits d'API ou votre quota Claude
Code.

Dans un agent de code, un skill lance ces commandes pour vous : voir
[Dans Claude Code](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#dans-claude-code)
et [Dans d'autres agents de code](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#dans-dautres-agents-de-code).
Le [serveur MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-depuis-nimporte-quel-agent-mcp)
donne le même diff à n'importe quel agent.

## Démarrage rapide

Le [projet d'exemple](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app)
appelle `client.messages.create` et épingle anthropic 1.8.0. La dernière version d'anthropic à la
date limite d'entraînement de Claude Sonnet 4.5 était la 0.60.0, dont `create` acceptait encore
`temperature` ; la 1.8.0 lève `TypeError` pour ce paramètre. Ce qu'affiche `scan`, réduit à la
partie anthropic :

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

L'appel de `app/main.py` ne passe aucun de ces paramètres : il est donc marqué « uses this API »
(utilise cette API) et non « old form » (ancienne forme). Il fonctionne aujourd'hui, mais un
assistant qui écrit pour la 0.60.0 pourrait ajouter `temperature=0.2` en modifiant l'appel. La
note est ce que `since-cutoff sync` écrit dans AGENTS.md pour l'en empêcher ; son étiquette,
`[diff]`, indique sur quoi elle repose : une comparaison statique des API publiques des deux
versions. Qu'un paramètre quitte la signature ne veut pas dire que l'API a abandonné le
champ : quand la méthode épinglée a un argument `extra_body` ou `extra_query`, la note le dit au
lieu de demander à l'assistant de supprimer le champ.

`since-cutoff sync` écrit ensuite les deux notes. Il affiche le bloc sous forme de diff et
demande `Write this to AGENTS.md? [y/N]` (`--yes` écrit sans demander ; `--dry-run` n'affiche
que le diff). Pour le projet d'exemple, le bloc se termine ainsi :

```markdown
**anthropic 1.8.0** (0.60.0 at the cutoff)
- `Messages.create()` no longer accepts `temperature`, `top_k` or `top_p` as keyword arguments. If the API still needs them, pass them through its `extra_body` or `extra_query` argument. since-cutoff found no replacement in anthropic's deprecation text. [diff]

**huggingface-hub 2.0.0** (0.34.3 at the cutoff)
- `huggingface_hub.hf_hub_download()` no longer accepts `proxies`, `force_filename`, `local_dir_use_symlinks` or `resume_download`; do not pass them. huggingface-hub's deprecation text says there is no replacement for `force_filename`, `local_dir_use_symlinks` or `resume_download`. since-cutoff found no replacement for `proxies` in huggingface-hub's deprecation text. [diff]
<!-- since-cutoff:end -->
```

Au-dessus des puces se trouvent le marqueur de début, une ligne de métadonnées (le modèle, sa
date limite, la provenance des versions, une empreinte des dépendances et une autre du texte du
bloc lui-même, pour qu'une modification à la main se voie) et un en-tête qui nomme le modèle, la
date limite et le fichier d'où viennent les versions, explique les étiquettes et précise
qu'aucun code de bibliothèque n'a été exécuté. Le bloc entier fait environ 420 tokens. Le texte
hors du bloc garde ses octets, et `since-cutoff unapply` retire le bloc. Après une mise à jour,
relancez `sync` ; `sync --check` dans la CI et `status` hors ligne vous préviennent quand les
notes ne sont plus à jour ([Tenir les notes à jour](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#tenir-les-notes-à-jour-avec-sync-et-status)).

**Essayez-le sur votre projet.** Aucune clé d'API, aucun appel au modèle. À la racine du projet :

```bash
uvx since-cutoff scan
```

Il détecte votre modèle de code (ou passez `--model`), lit votre fichier de verrouillage, prend
pour chaque dépendance la dernière version publiée au plus tard à la date limite d'entraînement
du modèle et compare l'API publique de cette version à celle que vous épinglez, de façon
statique : aucun code de paquet n'est exécuté. La date limite sert seulement à choisir les
changements à examiner ; elle ne dit rien de ce que le modèle a mémorisé. Si votre modèle se
trompe vraiment sur ces changements, et si les notes l'aident, c'est ce que mesure
[`since-cutoff run`](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#mesurer-votre-modèle) ;
il appelle votre modèle et reste facultatif.

## Quand l'utiliser, et ce qu'il ne fait pas

Utilisez-le si vous écrivez du Python avec un agent de code et épinglez des dépendances qui ont
publié des versions depuis la date limite d'entraînement du modèle ; dans l'écosystème Python de
l'IA, c'est le cas de la plupart ([Le problème](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#le-problème)) :

- `scan` pour un coup d'œil : les API modifiées qu'utilise votre code, où, et une note pour
  chacune ;
- `sync` pour mettre les notes dans AGENTS.md ou CLAUDE.md et les tenir à jour, et
  `sync --check`, `status`, les hooks pre-commit ou l'action GitHub pour être prévenu quand elles
  ne le sont plus ;
- le serveur MCP pour qu'un agent demande ce qui a changé dans une bibliothèque avant d'écrire
  du code ;
- `run` pour savoir si votre modèle écrit vraiment l'ancienne API, et si les notes y remédient.

Ce qu'il ne fait pas :

- appeler un modèle, sauf `run` : `scan`, `sync`, `status`, le serveur MCP, l'action et les hooks
  n'ont besoin d'aucune clé d'API ;
- exécuter du code de paquet ou du code écrit par le modèle : les paquets sont lus de façon
  statique, et les réponses ne passent que par le vérificateur de types ;
- voir les changements de comportement derrière une signature inchangée, ni les dépréciations
  qui n'avertissent qu'à l'exécution ;
- nommer un remplaçant, sauf si le texte de dépréciation de la bibliothèque elle-même l'indique ;
- dire ce que le modèle a mémorisé : la date limite choisit la version de comparaison, et seul
  `run` interroge le modèle ;
- réécrire votre code : pour migrer du code existant, un codemod est l'outil qu'il faut
  ([Comparaison avec d'autres outils](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#comparaison-avec-dautres-outils)) ;
- couvrir d'autres langages pour l'instant : Python seulement, TypeScript est le
  [#1](https://github.com/MohammadHijjawi97/since-cutoff/issues/1) ; et il nomme des fichiers, pas
  des lignes ([#8](https://github.com/MohammadHijjawi97/since-cutoff/issues/8)).

La liste complète est dans [Limites](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#limites).

## Le problème

Chaque modèle a une date limite d'entraînement ; votre fichier de verrouillage, lui, continue
d'évoluer. Quand une bibliothèque modifie son API publique après cette date, un modèle dont les
données d'entraînement précèdent le changement peut continuer d'écrire les anciens appels. Une
partie de ce code échoue à l'import ou à l'appel. Une autre partie s'exécute encore, parce que
l'ancienne façon de faire n'est que dépréciée, ou encore acceptée avec un avertissement.

Quelques-uns des changements que `since-cutoff scan` trouve pour Claude Sonnet 4.5 (date limite
d'entraînement : juillet 2025) dans le [projet d'exemple](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
qui épingle six de ses neuf dépendances sur les versions actuelles (pour les trois autres, non
épinglées, l'outil utilise la dernière version) :

| bibliothèque | version à la date limite | épinglée | ce qui a changé |
|---|---|---|---|
| anthropic | 0.60.0 | 1.8.0 | `messages.create(temperature=..., top_p=..., top_k=...)` n'est plus accepté |
| huggingface-hub | 0.34.3 | 2.0.0 | `hf_hub_download(resume_download=..., force_filename=..., local_dir_use_symlinks=...)` : retirés de la signature en 1.0 (la 2.0.0 les accepte encore à l'exécution, les ignore et émet un avertissement) |
| langchain-core | 0.3.72 | 1.6.5 | `retriever.get_relevant_documents()` et `llm.predict()` supprimés |
| openai | 1.98.0 | 3.19.2 | 22 changements incompatibles, 6 nouvelles dépréciations |

Dans ce projet, 7 dépendances sur 9 ont modifié leur API publique après la date limite. Le diff
statique signale 307 changements incompatibles et 23 nouvelles dépréciations ; le code du projet
utilise 2 des API modifiées.

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
   la date limite du modèle, compare son API publique à celle de la version que vous épinglez, et
   montre lesquelles des API modifiées votre code utilise, où, avec une note pour chacune. Aucun
   appel au modèle, aucune clé d'API.
2. **`sync`** écrit ces notes dans un bloc balisé d'AGENTS.md (ou de CLAUDE.md) après vous avoir
   montré le diff, et les tient à jour avec votre fichier de verrouillage ; `sync --check` et
   `status` préviennent la CI, pre-commit et votre agent quand elles ne sont plus à jour. Chaque
   note est rédigée à partir du diff d'API et porte une étiquette qui dit ce qui a été vérifié.
   Aucun appel au modèle.
3. **`run`**, facultatif, soumet au modèle de courtes tâches de programmation qui nécessitent les
   API modifiées, sans outils ni documentation, et classe chaque réponse en *stale* (périmée :
   valide seulement pour l'ancienne version), *wrong* (erronée), *deprecated* (dépréciée) ou
   *correct* à l'aide d'un vérificateur de types appliqué aux *deux* versions. Aucun LLM ne sert
   de juge. Pour chaque échec, il rédige une note, ne garde une note rédigée par le modèle que si
   son exemple passe la vérification de types avec votre version, et teste à nouveau le modèle
   sur des tâches réservées (held-out), sans puis avec les notes.

Le même diff est accessible aux agents via un [serveur MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-depuis-nimporte-quel-agent-mcp)
et à la CI via une [action GitHub et des hooks pre-commit](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-en-ci).
La vidéo explicative (en anglais) résume cette section en deux minutes et demie.

<!-- La vidéo n'est attachée qu'à la version v0.5.0 (release.yml ne téléverse que dist/*), d'où cette version dans son URL. -->
<p align="center"><a href="https://github.com/MohammadHijjawi97/since-cutoff/releases/download/v0.5.0/since-cutoff-explainer.mp4"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/explainer-thumbnail.png" width="560" alt="Regarder la vidéo explicative de 2 min 30 (commentée)"></a><br><a href="https://github.com/MohammadHijjawi97/since-cutoff/releases/download/v0.5.0/since-cutoff-explainer.mp4">▶ Regarder la vidéo explicative de 2 min 30 (commentée)</a></p>

## Utilisation

```bash
# les API modifiées qu'utilise votre code, avec une note pour chacune (aucun appel au modèle, aucune clé d'API)
uvx since-cutoff scan

# écrire ces notes dans AGENTS.md (montre le diff et demande d'abord) ; à relancer après une mise à jour
uvx since-cutoff sync

# facultatif : mesurer sur quels changements votre modèle se trompe, et tester les notes (appelle le modèle)
uvx since-cutoff run
```

Ce qu'affiche `scan` pour le projet d'exemple, en entier :

<p align="center"><img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/scan.svg" width="100%" alt="since-cutoff scan --model anthropic:claude-sonnet-4-5 sur le projet d'exemple. Votre code utilise 2 API qui ont changé après la date limite d'entraînement de claude-sonnet-4-5 (2025-07-31). huggingface-hub 0.34.3 -> 2.0.0 : hf_hub_download, utilisée dans app/main.py, n'a plus force_filename, local_dir_use_symlinks, resume_download ni proxies dans sa signature ; sa note porte l'étiquette [diff], et une ligne Runtime indique que le code source de la 2.0.0 les traite encore, si bien que les appels qui les passent peuvent s'exécuter avec un avertissement. anthropic 0.60.0 -> 1.8.0 : Messages.create, appelée dans app/main.py, n'accepte plus temperature, top_k ni top_p ; sa note porte l'étiquette [diff]. 2 notes prêtes pour AGENTS.md ; ce que signifient uses this API et [diff] ; 323 autres changements dans 7 paquets que le code n'utilise pas."></p>

Les deux API sont marquées « uses this API » ; un fichier qui passerait `resume_download=True` ou
`temperature=0.2` les ferait passer en « old form ». `scan -v` liste tous les fichiers qui
utilisent une API (3 sont affichés), et `scan --all` ajoute tous les autres changements, paquet
par paquet. `scan --json` et `.since-cutoff/results.json` contiennent la même chose dans
`used_apis` (chaque API, où elle est utilisée, ses changements et sa note, avec les étiquettes
de la note, les versions auxquelles elle s'applique et ce qui a été vérifié), et
`.since-cutoff/report.md` commence par « Used by your code ».

### Tenir les notes à jour avec sync et status

`since-cutoff sync` (depuis la 0.4.0) écrit les notes qu'affiche `scan` dans un bloc balisé :
dans AGENTS.md, ou dans CLAUDE.md si seul ce fichier existe, et, là où un bloc existe déjà, dans
celui-ci (`--target` désigne un autre fichier). Il affiche un diff unifié et demande
`Write this to AGENTS.md? [y/N]` ; `--yes` écrit sans demander, et s'il n'y a pas de terminal où
poser la question, il n'écrit rien. Le texte hors du bloc garde ses octets, fins de ligne CRLF
comprises, et `since-cutoff unapply` retire le bloc. Le bloc du projet d'exemple est dans
[Démarrage rapide](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#démarrage-rapide). La mise en
garde « Runtime: » reste hors du bloc : le conseil (« do not pass them », ne les passez pas) est le
même dans tous les cas.

Relancez `sync` quand vous modifiez le fichier de verrouillage ou le code. Il ajoute des notes
pour les API que votre code se met à utiliser, vérifie à nouveau un paquet mis à jour, et retire
les notes d'un paquet quand il n'est plus une dépendance, n'est plus plus récent que la version
à la date limite, ou quand votre code n'utilise plus ses API modifiées, en disant pourquoi. Il
conserve le modèle et la date limite pour lesquels le bloc a été écrit (pour que des collègues
dont les agents utilisent d'autres modèles ne le réécrivent pas sans cesse), sauf si vous passez
`--model` ou `--cutoff` ; `--model a,b` prend la plus ancienne de leurs dates limites. Si rien n'a
changé, il n'écrit rien, et le fichier garde ses octets et sa date de modification.

| commande | ce qu'elle fait | code de sortie |
|---|---|---|
| `since-cutoff sync` | affiche le diff, demande, écrit | 0 écrit ou déjà à jour ; 3 rien d'écrit (vous avez répondu non, ou il n'y a pas de terminal où demander) |
| `since-cutoff sync --yes` | écrit sans demander | 0 |
| `since-cutoff sync --dry-run` | affiche le diff, n'écrit rien | 0 |
| `since-cutoff sync --check` | n'écrit rien (pour la CI et pre-commit) | 0 à jour ; 3 pas à jour |
| `since-cutoff sync --json --yes` | écrit sans demander ; imprime en JSON les propositions et le résultat de l'écriture | comme `sync --yes` |
| `since-cutoff sync --json --check` | imprime en JSON les propositions ; n'écrit rien | 0 à jour ; 3 pas à jour |
| `since-cutoff sync --json --dry-run` | imprime en JSON les propositions ; n'écrit rien | 0 |
| `since-cutoff status` | compare le bloc au fichier de verrouillage, hors ligne | 0 à jour, ou aucun bloc ; 3 pas à jour ; 1 marqueurs cassés |

Si le bloc a été modifié à la main, `sync` et `sync --check` affichent le diff, n'écrivent rien et
sortent avec le code 4 ; `sync --force` le remplace. Si un paquet dont parlent les notes ne peut
pas être vérifié (PyPI injoignable), `sync` n'écrit rien et sort avec le code 1.

`sync --json` exige `--yes`, `--check` ou `--dry-run` ; sans l'un d'eux, il sort avec le code 2
sans rien demander. La sortie standard ne contient que du JSON (et reste vide pour les erreurs
d'usage) ; la progression, les diffs et les erreurs vont sur stderr. L'objet contient `exit_code`
et `targets`. Chaque cible indique `target`, `action`, `changed`, `edited`, `written`, `model`,
`cutoff`, `scope`, `notes`, `changes` (`package`, `done` et `state` de chaque paquet), `retest` et
`diff_lines`. `written` distingue une proposition appliquée d'un aperçu ou d'une modification à la
main refusée. Les codes de sortie habituels et le comportement de `--force` s'appliquent toujours.

`since-cutoff status` n'utilise pas le réseau et ne lit pas le code : pour chaque paquet, la
version pour laquelle les notes ont été écrites et celle du fichier de verrouillage, si d'autres
dépendances ont changé, et si le modèle configuré pour votre agent de code a une date limite
antérieure à celle des notes (avec la commande `sync --model` correspondante). `status --json`
est destiné aux scripts. `status --hook` n'affiche une ligne que lorsque les notes ne sont plus à
jour et sort toujours avec 0, par exemple :

```text
since-cutoff: the library notes in AGENTS.md are out of date: anthropic 1.8.0 in the notes, 0.60.0 in pyproject.toml. `since-cutoff sync` updates them.
```

Autres options :

- `sync --scope imported` note aussi les changements les plus susceptibles de compter dans chaque
  paquet modifié qu'importe votre code (jusqu'à 5 API par paquet), pour du code qui n'utilise
  encore aucune des API modifiées. `scan` le suggère dans ce cas.
- `sync --suggestions` ajoute les noms de la version épinglée qui ressemblent seulement à ce qui a
  été supprimé, avec l'étiquette `[not confirmed]`. Le bloc enregistre ces deux choix, et les
  exécutions suivantes de sync les conservent.
- Les notes écrites par `since-cutoff run --apply` portent l'étiquette `[type-checked]`. `sync`
  conserve chacune d'elles, à la place de la note tirée du diff pour la même API, tant que son
  paquet garde la même version et que votre code utilise encore cette API.

Claude Code lit CLAUDE.md, et ne lit AGENTS.md que s'il n'y a pas de CLAUDE.md ou si CLAUDE.md
l'importe avec une ligne `@AGENTS.md`
([documentation sur la mémoire](https://code.claude.com/docs/en/memory), en anglais). Quand les
notes vont dans AGENTS.md et que CLAUDE.md ne l'importe pas, `scan` et `sync` le signalent.
L'écriture dans les deux fichiers est l'objet de
[#13](https://github.com/MohammadHijjawi97/since-cutoff/issues/13).

### Mesurer votre modèle

`since-cutoff run` est l'étape facultative qui appelle un modèle. Il lui soumet de courtes tâches
qui nécessitent les API modifiées, sans outils, sans documentation et sans rien de votre code ;
évalue les réponses avec un vérificateur de types sur les deux versions ; rédige une note pour
chaque échec ; et teste les notes sur des tâches réservées
([Fonctionnement](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#fonctionnement)).
Il consomme vos crédits d'API ou votre quota Claude Code et peut prendre de 5 à 20 minutes.

```bash
since-cutoff run --quick    # une exécution plus courte : 12 sondes, 1 tâche réservée, 3 vérifications de non-régression
since-cutoff run --apply    # écrire les notes de cette exécution dans le bloc
```

Sans `--apply`, les notes sont affichées, pas écrites. `run --apply` remplace le bloc par les
notes de cette exécution ; un `since-cutoff sync` ultérieur ajoute les notes tirées du diff pour
les autres API modifiées qu'utilise votre code, et conserve les notes `[type-checked]` de
l'exécution tant que la version de leur paquet ne change pas.

### Dans Claude Code

```text
/plugin marketplace add MohammadHijjawi97/since-cutoff
/plugin install since-cutoff@since-cutoff
```

Demandez ensuite à Claude : « Vérifie pour lesquelles de nos dépendances tes connaissances ne
sont plus à jour », ou lancez `/since-cutoff:since-cutoff`. Le skill lance `since-cutoff scan` et
propose les notes : il lance `since-cutoff sync --dry-run`, vous montre le diff et n'écrit que si
vous êtes d'accord. Si vous lui demandez de mesurer le modèle, la mesure est effectuée par une
nouvelle instance du modèle, sans outils, si bien que l'agent ne peut pas s'évaluer lui-même. Le
plugin démarre aussi le
[serveur MCP](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#utilisation-depuis-nimporte-quel-agent-mcp),
pour que Claude puisse consulter les changements d'une bibliothèque avant d'écrire du code, et
(depuis la 0.4.0) lance `since-cutoff status --hook` au début de chaque session, ce qui ajoute une
ligne à la session quand les notes ne sont plus à jour (la première fois, uvx télécharge
since-cutoff).

### Dans d'autres agents de code

```bash
npx skills add MohammadHijjawi97/since-cutoff
```

Cette commande installe le même skill via la CLI open source [skills](https://github.com/vercel-labs/skills),
pour Codex, Cursor, Gemini CLI, GitHub Copilot, OpenCode et les autres agents qui lisent
`SKILL.md`. since-cutoff lit aussi le modèle dans les réglages de Codex, Gemini CLI, OpenCode et Aider ; pour
les autres agents, indiquez-lui quel modèle utiliser, par exemple
`since-cutoff scan --model openai:gpt-5.4`. Ajoutez le serveur MCP comme indiqué plus bas.

Des prompts qui fonctionnent bien :

- « Quelles API utilisées par notre code ont changé après ta date limite d'entraînement ? »
  L'agent lance `since-cutoff scan` ou appelle l'outil MCP `project_changes`.
- « Ajoute des notes à leur sujet dans AGENTS.md. » L'agent lance `since-cutoff sync --dry-run`,
  vous montre le diff, puis lance `since-cutoff sync --yes` quand vous êtes d'accord.
- « Mesure sur lesquels de ces changements tu te trompes vraiment. » L'agent vous demande d'abord
  votre accord, puis lance `since-cutoff run --quick`.
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

La colonne « nécessite » concerne `run`, qui appelle le modèle ; `scan` et `sync` n'utilisent
que sa date limite d'entraînement. Sans `--model`, les versions 0.3.0 et suivantes utilisent le
modèle configuré pour votre agent de code, et la ligne du modèle indique d'où il vient
(« model from .claude/settings.json ») :

1. `SINCE_CUTOFF_MODEL` (une spécification complète comme `openai:gpt-5.4`) l'emporte toujours.
2. Dans Claude Code (qui définit `CLAUDECODE=1` pour les commandes qu'il exécute), seuls
   comptent les réglages de Claude Code : `ANTHROPIC_MODEL`, puis `.claude/settings.local.json`
   et `.claude/settings.json` du projet, puis `~/.claude/settings.json`.
3. Ailleurs, le réglage le plus spécifique l'emporte : d'abord `ANTHROPIC_MODEL`,
   `GEMINI_MODEL` ou `AIDER_MODEL`, puis les réglages du projet, en commençant par le dossier le plus proche,
   depuis le dossier analysé jusqu'à la racine du dépôt (jamais le dossier personnel), puis les
   réglages de l'utilisateur. Dans un même dossier, les agents comptent dans cet ordre :

| agent | réglages du projet | réglages de l'utilisateur |
|---|---|---|
| Claude Code | `.claude/settings.local.json`, `.claude/settings.json` | `~/.claude/settings.json` |
| Codex | `.codex/config.toml`, avec son profil sélectionné | `$CODEX_HOME/config.toml` ou `~/.codex/config.toml` |
| Gemini CLI | `.gemini/settings.json` | `~/.gemini/settings.json` |
| OpenCode | `opencode.json`, `opencode.jsonc` | `~/.config/opencode/` |
| Aider | `.aider.conf.yml`, avec les alias d'Aider (`4o`, `flash`, `r1`, ...) | `~/.aider.conf.yml` |

Si aucun réglage n'indique de modèle, il utilise le modèle par défaut de Claude Code et le signale.
Seuls les champs du modèle sont lus, et un nom de modèle qu'il ne sait pas identifier arrête
l'exécution avec un message qui nomme le réglage. Un modèle auquel un agent accède via un autre
service (GitHub Copilot, Amazon Bedrock, Vertex AI) est désigné d'après l'entreprise qui l'a conçu,
si bien que `run` appelle l'API de celle-ci (`openai:` nécessite `OPENAI_API_KEY`). `sync`
conserve le modèle pour lequel un bloc a été écrit, quel que soit celui qu'utilise désormais votre
agent.

Les dates limites d'entraînement proviennent de [models.dev](https://models.dev) (un instantané
est inclus pour une utilisation hors ligne). `since-cutoff models sonnet` les affiche ;
`--cutoff 2025-07` remplace la date, et `since-cutoff scan --cutoff 2025-07` sans `--model`
analyse par rapport à cette seule date. `scan` et `sync` n'ont besoin que de la date limite : ils
acceptent donc aussi un identifiant de modèle sans fournisseur (`claude-haiku-4-5`, `sonnet`) ou avec n'importe
quel fournisseur répertorié par models.dev (`google:gemini-2.5-pro`, identifiants Amazon Bedrock
et Vertex AI compris) ; `run` a besoin d'un fournisseur du tableau ci-dessus.

## Questions fréquentes

**La date limite d'entraînement est-elle la date à partir de laquelle le modèle ne sait plus rien
d'une bibliothèque ?** Non. since-cutoff n'utilise la date limite que pour choisir un point de
comparaison : pour chaque dépendance, la version la plus récente publiée au plus tard à cette
date, dont il compare l'API publique à la version que vous épinglez. Les modèles connaissent mal
les mois qui précèdent leur date limite et peuvent connaître une version parue après, si bien que
le scan peut lister des changements que le modèle maîtrise déjà et en manquer d'autres. Si le
modèle écrit l'ancienne API, c'est `run` qui le mesure. Les dates viennent de
[models.dev](https://models.dev) ; `since-cutoff models <nom>` affiche la date limite de chaque
modèle à côté de sa date de sortie, séparées de plusieurs mois (claude-sonnet-4-5 : date limite
2025-07-31, sorti le 2025-09-29). Plus de détails dans
[À quoi sert la date limite d'entraînement](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#à-quoi-sert-la-date-limite-dentraînement).

**Qu'est-ce qui est envoyé, et où ?** `scan`, `sync`, `status`, le serveur MCP, l'action GitHub
et les hooks pre-commit lisent PyPI (métadonnées et wheels) et models.dev, et n'envoient rien à
aucun modèle. `run` envoie des prompts au fournisseur de modèle que vous choisissez : noms de
paquets, versions, signatures publiques et docstrings des API modifiées, tâches générées et, pour
les notes, les propres réponses du modèle ; jamais votre code source. Quels fichiers utilisent une
API modifiée est affiché dans le terminal et dans `.since-cutoff/`, et ne va plus loin que là où
vous l'envoyez : un résumé de CI avec `--markdown`, ou l'agent qui a appelé l'outil MCP
`project_changes`. Aucune télémétrie. Détails :
[Ce qu'il exécute, envoie et stocke](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#ce-quil-exécute-envoie-et-stocke)
et [PRIVACY.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/PRIVACY.md) (en anglais).

**Pourquoi certaines notes disent-elles « since-cutoff found no replacement » ?** Une note ne
nomme un remplaçant que lorsque le texte de dépréciation de la bibliothèque elle-même en indique
un et que ce nom existe dans votre version épinglée (étiquette `[diff + library]`). Sinon, la note
énonce le changement et dit qu'aucun remplaçant n'a été trouvé : un remplaçant deviné, que
l'agent suit ensuite, est pire qu'aucun. Les noms de la version épinglée qui ressemblent
seulement à ce qui a été supprimé sont affichés dans le terminal comme « not confirmed as
replacements » ; `sync --suggestions` les écrit dans les notes avec l'étiquette `[not confirmed]`.
Quand la méthode épinglée a encore un argument `extra_body` ou `extra_query`, comme
`Messages.create()` d'anthropic, la note le signale, car un paramètre de requête retiré de la
signature peut encore être accepté par l'API.

**AGENTS.md ou CLAUDE.md ?** `sync` écrit dans AGENTS.md ; dans CLAUDE.md si seul ce fichier
existe ; et dans celui des deux qui a déjà un bloc (`--target` désigne n'importe quel fichier).
Claude Code lit CLAUDE.md, et AGENTS.md seulement si CLAUDE.md l'importe avec une ligne
`@AGENTS.md` ; quand les notes vont dans AGENTS.md et que CLAUDE.md ne l'importe pas, `scan` et
`sync` le signalent. Pour garder une seule copie, ajoutez `@AGENTS.md` à CLAUDE.md. Pour avoir un
bloc dans les deux fichiers, écrivez le second une fois avec
`sync --target CLAUDE.md --model <le même modèle>` ; ensuite `sync` met les deux à jour et
`status` rend compte des deux. L'écriture dans les deux par défaut fait l'objet de
[#13](https://github.com/MohammadHijjawi97/since-cutoff/issues/13).

**Combien coûtent les notes en tokens ?** L'agent lit le bloc à chaque tour. Pour le projet
d'exemple, il fait environ 420 tokens, dont environ 160 pour les deux notes ; le reste est
l'en-tête, qui nomme le modèle et la date limite et explique les étiquettes (à raison de quatre
caractères par token, comme le compte since-cutoff). `sync` n'écrit des notes que pour les API
modifiées qu'utilise votre code (`--scope imported` en ajoute jusqu'à 5 par paquet importé), et
une note rédigée par le modèle fait au plus 60 mots. Dans le benchmark, les blocs faisaient de 285
à 505 tokens, et les sessions avec les notes ont consommé 0,73 fois le total de tokens des
sessions sans elles. `run` indique la taille de chaque bloc en tokens.

**Fonctionne-t-il hors ligne ?** `status` ne lit rien sur le réseau. `scan` et `sync` ont besoin
de PyPI pour la liste des versions de chaque paquet (en cache pendant 12 heures ; quand PyPI est
injoignable, une copie plus ancienne du cache est utilisée et le scan dit de quel jour elle date)
et pour les sources de chaque paquet modifié ; sources et diffs sont mis en cache, si bien que les
exécutions suivantes prennent quelques secondes. Les dates limites d'entraînement viennent de
models.dev, avec un instantané inclus pour une utilisation hors ligne.

## Résultats

Ce qui a été mesuré jusqu'ici, chaque fois avec sa portée :

- **Un projet, un modèle, since-cutoff 0.1.0** : la carte ci-dessous, Claude Opus 4.6 sur le
  projet d'exemple. Le tableau qui la suit ajoute Claude Haiku 4.5 sur le même projet.
- **Benchmark** : 360 sessions de Claude Code en mode headless (`claude-opus-5-5`) sur 24 tâches
  Python avec des tests cachés, protocole figé avant l'exécution principale. Sur les 17 tâches
  postérieures au cutoff, les sessions avec les notes de since-cutoff 0.4.1 ont coûté 0,80 fois ce
  que coûte Claude Code seul (IC à 95 % : 0,70-0,90), avec 0,83 fois les tours et 0,87 fois la
  durée. Aucune conclusion sur le taux de réussite : Claude Code seul réussissait déjà 94,1 % de
  ces tâches, au-dessus du plafond préenregistré de 90 %. Les variantes avec Context7 ont tourné
  sans clé d'API et, à partir de la 215e des 360 sessions, n'ont reçu que « Monthly quota
  exceeded », ce qui n'affecte pas la comparaison entre les notes et Claude Code seul. Un modèle,
  un agent : [résultats](https://mohammadhijjawi97.github.io/since-cutoff/benchmark.html) (en anglais),
  [tâches, protocole et transcriptions](https://github.com/MohammadHijjawi97/since-cutoff-benchmark).
- **Rapports de développeurs indépendants** : ils seront listés ici, chacun avec son projet, son
  modèle et sa date. Publiez le vôtre dans
  [Share your results](https://github.com/MohammadHijjawi97/since-cutoff/discussions/6)
  (« Partagez vos résultats », en anglais).

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero-dark.fr.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/hero.fr.svg" width="640" alt="Votre modèle de code a appris vos bibliothèques avant qu'elles ne changent. Claude Opus 4.6 sur un seul projet d'exemple, mesuré avec since-cutoff 0.1.0 : sur 7 des 16 changements d'API sondés, il a utilisé un nom ou un paramètre supprimé depuis ; avec les notes, la part de tâches réservées correctes est passée de 5 % à 65 % (20 tâches). Essayez : uvx since-cutoff scan (aucun appel au modèle, aucune clé d'API).">
</picture></p>

Deux modèles Claude sur le projet d'exemple à 9 dépendances de
[`examples/agent-app`](https://github.com/MohammadHijjawi97/since-cutoff/tree/main/examples/agent-app),
**mesurés avec since-cutoff 0.1.0**, les tâches et les notes étant rédigées par Claude Opus 4.6 :

| | Claude Haiku 4.5 | Claude Opus 4.6 |
|---|---|---|
| date limite d'entraînement | févr. 2025 | mai 2025 |
| changements d'API sondés | 20 | 16 |
| **stale** / wrong / deprecated / correct | **5** / 1 / 2 / 12 | **7** / 0 / 3 / 6 |
| bibliothèques avec des appels *stale* | 3 sur 5 sondées | 2 sur 4 sondées |
| notes rédigées (dont avec un exemple qui passe le vérificateur de types) | 8 (7), environ 391 jetons | 10 (7), environ 437 jetons |
| **tâches réservées correctes, sans -> avec notes** | **14 % -> 57 %** (14 paires) | **5 % -> 65 %** (20 paires) |
| API déjà correctes, une fois les notes ajoutées | 6/6 toujours correctes | 6/6 toujours correctes |

Les tâches *réservées* (held-out) sont des reformulations de la tâche qui a servi à sonder chaque
changement sur lequel le modèle a échoué ; le modèle répond deux fois à chacune, sans puis avec
les notes, et les réponses sont évaluées de la même façon. La dernière ligne revérifie les API que le modèle
utilisait déjà correctement, pour repérer les notes qui aggravent les choses.

Dans cet échantillon, le modèle le plus puissant n'était pas plus sûr : Opus 4.6 a utilisé des
API supprimées après sa date limite, dont `anthropic.HUMAN_PROMPT` avec `client.completions`.
Exemples de code périmé tirés des deux exécutions, chacun valide pour la version de comparaison
et rejeté par le vérificateur de types pour la version épinglée : `messages.create(temperature=...)`
(anthropic 1.8), `hf_hub_download(resume_download=...)`, `local_dir_use_symlinks=...`,
`force_filename=...` et `proxies=...` (huggingface-hub 2.0), et `client.beta.vector_stores`
(openai 3.x). À l'exécution, anthropic 1.8.0 lève `TypeError` pour `temperature` ;
huggingface-hub 2.0.0 accepte encore ces quatre arguments de téléchargement, les ignore et émet
un avertissement.

Les notes rédigées lors de l'exécution Claude Haiku 4.5 (extrait, texte original ; format de
since-cutoff 0.1.0) :

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

La note sur huggingface-hub n'est pas tout à fait exacte : `resume_download` a quitté la
signature en 1.0, pas en 2.0.0, et la 2.0.0 l'accepte encore à l'exécution, l'ignore et émet un
avertissement ([code source](https://github.com/huggingface/huggingface_hub/blob/v2.0.0/src/huggingface_hub/utils/_validators.py#L171-L191)).
Le conseil de l'omettre reste juste. La note que la 0.4.0 rédige à partir du diff d'API pour ces
mêmes arguments figure dans
[Démarrage rapide](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/README.fr.md#démarrage-rapide) ;
la mise en garde sur l'exécution apparaît dans le terminal, le rapport et le JSON, pas dans le
bloc.

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
| `project_changes(project_dir, model)` | la même chose pour chaque dépendance d'un projet, à sa version épinglée, en commençant par les API modifiées qu'utilise votre code : pour chacune, les fichiers qui l'utilisent (3 au plus), sa note, la mise en garde sur l'exécution et les noms qui lui ressemblent, non confirmés comme remplaçants |
| `model_cutoff(model)` | la date limite d'entraînement d'un modèle, d'après [models.dev](https://models.dev) |

L'agent transmet son propre identifiant de modèle : la réponse couvre donc ce qui a changé
après la date limite de ce modèle. Les outils lisent PyPI et les sources des paquets de façon
statique : aucun appel au modèle, aucune clé d'API, aucun code de paquet exécuté.

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

Ce que renvoie `api_changes("huggingface-hub", model="claude-haiku-4-5", to_version="2.0.0")`
(sortie réelle, abrégée) :

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

Avec `symbol="hf_hub_download"`, il ne liste que les 8 changements de cette fonction
(`resume_download=`, `force_filename=`, `local_dir_use_symlinks=` et `proxies=`, sur la fonction
et sur `HfApi`). `symbol` accepte aussi un appel tel que le code l'écrit : `client.messages.create`
trouve les changements de `Messages.create`. Le diff lit les signatures : ces quatre paramètres
ont quitté la signature dans huggingface-hub 1.0, et la réponse ajoute que le code source de la
2.0.0 les traite encore, si bien que les appels qui les passent peuvent s'exécuter avec un
avertissement.

## Utilisation en CI

### GitHub Action

L'action analyse le projet à chaque pull request et ajoute un résumé à la page du job : les API
modifiées qu'utilise votre code, avec les fichiers qui les utilisent, le changement, l'usage ou
non de l'ancienne forme et le remplaçant ; puis, repliés, les notes et les changements de chaque
dépendance. Avec `check-notes: true`, elle fait aussi échouer le job quand les notes d'AGENTS.md
ne sont plus à jour. Comme `scan`, elle ne lit que PyPI et models.dev : aucun appel au modèle,
aucune clé d'API.

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
          model: anthropic:claude-sonnet-4-5  # le modèle que votre équipe utilise pour coder
          check-notes: true                   # échoue quand AGENTS.md a besoin de `since-cutoff sync`
```

Sans `paths`, le job s'exécute aussi quand une modification du code commence à utiliser une API
modifiée.

| entrée | valeur par défaut | |
|---|---|---|
| `model` | obligatoire | `provider:model`, comme pour `--model` ; seule sa date limite d'entraînement est utilisée |
| `working-directory` | `.` | le répertoire du projet |
| `only`, `exclude` | | noms PyPI séparés par des virgules |
| `cutoff` | | remplace la date limite d'entraînement (`YYYY-MM` ou `YYYY-MM-DD`) |
| `fail-on-changes` | `false` | fait échouer l'étape quand une dépendance a modifié son API après la date limite |
| `check-notes` | `false` | lance aussi `since-cutoff sync --check`, qui n'écrit rien, et fait échouer le job quand les notes d'AGENTS.md / CLAUDE.md ne sont plus à jour ou que le bloc a été modifié à la main ; les notes gardent le modèle pour lequel elles ont été écrites, et `model` (et `cutoff`) servent pour un projet qui n'a pas encore de bloc |
| `step-summary` | `true` | ajoute le résumé Markdown au résumé du job |
| `cache` | `true` | conserve entre les exécutions les métadonnées PyPI, les sources des paquets et les diffs d'API (y compris quand `fail-on-changes` fait échouer le job) |
| `args` | | arguments supplémentaires pour `since-cutoff scan`, par exemple `--all-deps --limit 20` |
| `since-cutoff-version` | `0.5.0` | la version de since-cutoff à exécuter, ou `latest` |

`args: --fail-on old-form --annotate github` ne fait échouer le job que si votre code utilise une
API modifiée sous l'ancienne forme, et annote chaque fichier qui en utilise une : un
avertissement (*warning*) pour l'ancienne forme, une note (*notice*) sinon.

Sorties : `changed-packages` (liste séparée par des virgules), `changes` (changements incompatibles),
`deprecations`, `markdown` (le chemin du résumé, par exemple pour le publier en commentaire de
la pull request), `report` (le chemin du rapport complet) et, avec `check-notes`, `notes`
(`up-to-date`, `out-of-date`, `edited-by-hand` ou `error`). Un changement accessible par
plusieurs chemins d'import n'est compté qu'une fois.

### pre-commit

```yaml
# .pre-commit-config.yaml
repos:
  - repo: https://github.com/MohammadHijjawi97/since-cutoff
    rev: v0.5.0
    hooks:
      - id: since-cutoff-scan
        args: [--model=anthropic:claude-sonnet-4-5]  # ajoutez --fail-on=old-form pour bloquer le commit
      - id: since-cutoff-sync  # tient à jour les notes d'AGENTS.md ; args: [--check] vérifie seulement
```

`since-cutoff-scan` s'exécute quand un fichier de verrouillage, un fichier `requirements*.txt` ou
`pyproject.toml` change, et affiche le résultat du scan ; passez-lui `--model` dans `args`.
`since-cutoff-sync` s'exécute quand l'un de ces fichiers, AGENTS.md ou CLAUDE.md change, et met
les notes à jour ; s'il modifie le fichier, le hook échoue, comme tout hook pre-commit qui
modifie des fichiers : ajoutez le fichier et refaites le commit. Avec `args: [--check]`, il ne
modifie rien et échoue tant que les notes ne sont pas à jour. Il conserve le modèle pour lequel
les notes ont été écrites ; pour un premier bloc, indiquez-lui en un :
`args: [--model=anthropic:claude-sonnet-4-5]`. Les deux ont besoin d'accéder à PyPI :
désactivez-les donc sur pre-commit.ci (`ci: {skip: [since-cutoff-scan, since-cutoff-sync]}`).
`since-cutoff-status` n'a pas besoin du réseau : il échoue quand les notes ne correspondent pas
au fichier de verrouillage.

### Autres CI

```bash
# résumé Markdown pour n'importe quelle CI ; code de sortie 3 si votre code utilise une API modifiée sous l'ancienne forme
since-cutoff scan --model anthropic:claude-sonnet-4-5 --markdown summary.md --fail-on old-form

# code de sortie 3 si les notes d'AGENTS.md ne sont plus à jour, 4 si le bloc a été modifié à la main
since-cutoff sync --check

# mesurer aussi le modèle (nécessite sa clé d'API, ou la CLI claude)
since-cutoff run --quick --fail-on-stale --json > since-cutoff.json
```

`--markdown -` écrit le résumé sur stdout, et seulement la progression et le chemin du rapport
sur stderr, comme `--json`. Dans un fichier, un pipe ou un journal de CI, il n'y a pas de barre
de progression animée, et la sortie est mise en page sur 160 colonnes (`COLUMNS` fixe une autre
largeur). Codes de sortie :

- `0` OK ;
- `1` erreur (par exemple, `run` n'a pu sonder aucun changement d'API ou n'a pu évaluer aucune
  réponse du modèle, ou `sync` n'a pas pu vérifier un paquet dont parlent ses notes) ;
- `2` erreur d'utilisation ;
- `3` une vérification a échoué : `scan --fail-on changes`, `used` ou `old-form` (une dépendance a
  modifié son API après la date limite, votre code utilise une API modifiée, ou l'utilise sous
  l'ancienne forme ; `--fail-on-changes` équivaut à `--fail-on changes`), `run --fail-on-stale`
  (utilisation d'API périmée détectée), ou notes qui ne sont plus à jour avec `sync --check` ou
  `status` (et avec `sync`, quand il ne les a pas écrites) ;
- `4` `sync` : le bloc a été modifié à la main, et rien n'a été écrit sans `--force` ;
- `141` la sortie a été fermée prématurément (par exemple, redirigée vers `head`).

## Fonctionnement

`scan` et `sync` n'ont besoin d'aucun modèle : ils comparent les API publiques, trouvent où votre
code utilise celles qui ont changé et énoncent chaque changement à partir du diff, avec une
étiquette qui dit ce qui a été vérifié
([détails](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md#notes-without-a-model-scan-and-sync),
en anglais). `run` passe par trois étapes. La première est l'analyse et ne fait appel à aucun
modèle ; dans les deux autres, un vérificateur de types évalue chaque réponse et contrôle chaque
note rédigée par le modèle :

<p align="center"><picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works-dark.fr.svg">
  <img src="https://raw.githubusercontent.com/MohammadHijjawi97/since-cutoff/main/docs/img/how-it-works.fr.svg" width="640" alt="Trois étapes. Analyse (scan), sans appel au modèle : le fichier de verrouillage donne vos versions exactes, puis la version à la date limite du modèle, puis un diff statique de l'API avec griffe. Sonde : de courtes tâches qui nécessitent le changement, le modèle répond de mémoire, basedpyright vérifie la réponse par rapport aux deux versions. Rédaction et test des notes : une note rédigée par le modèle n'est conservée que si son exemple passe la vérification de types, sinon un constat du changement tiré du diff d'API la remplace ; le modèle répond aux tâches réservées sans puis avec les notes, et --apply écrit un bloc dans AGENTS.md.">
</picture></p>

| résultat | signification |
|---|---|
| **stale** | le code est valide pour la version de comparaison (celle de la date limite du modèle) et invalide pour la vôtre, et l'erreur porte sur une API qui a changé |
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
bloc est indiquée en jetons : une exécution montre ainsi ce qu'apportent les notes de
since-cutoff par rapport à elles.

Tout est évalué par un vérificateur de types sur les versions exactes des paquets, chacune dans
un environnement isolé avec les dépendances d'exécution propres à ce paquet. Aucun LLM ne sert
de juge, et chaque chiffre peut être retrouvé dans `results.json`. Détails (en anglais) :
[docs/how-it-works.md](https://github.com/MohammadHijjawi97/since-cutoff/blob/main/docs/how-it-works.md).

### Ce que « verified » veut dire

since-cutoff n'emploie jamais le mot « verified » (vérifié) sans précision. Chaque note porte une
étiquette qui dit ce qui a été vérifié, et rien d'autre n'est affirmé :

| étiquette | ce qui a été vérifié | ce qui ne l'a pas été |
|---|---|---|
| `[diff]` | Le changement figure dans une comparaison statique (griffe) des API publiques de deux versions : la plus récente publiée au plus tard à la date limite d'entraînement du modèle, et la version qu'épingle votre projet. Les sources sont lues, pas importées. Avec `[diff]` seul, aucun remplaçant n'est nommé : la note rapporte ce que dit le texte de dépréciation de la bibliothèque elle-même (« there is no replacement for `resume_download` », il n'y a pas de remplaçant), ou que since-cutoff n'y en a trouvé aucun. | Le comportement, et si un appel fonctionne encore : la version épinglée peut encore accepter un paramètre supprimé avec un avertissement, comme huggingface-hub 2.0.0 le fait pour `resume_download`. Le terminal, report.md, les outils MCP et le JSON ajoutent une ligne « Runtime: » quand le code source épinglé en traite encore un ; le bloc non, puisque le conseil est le même. Si votre modèle se trompe dessus. |
| `[diff + library]` | Comme `[diff]`, et le texte de dépréciation de la bibliothèque elle-même (une docstring, l'entrée d'un paramètre dans la docstring, un message `@deprecated` ou un texte `warnings.warn`, dans l'ancienne version ou, pour une dépréciation, dans la version épinglée) énonce le remplaçant (« Use `stop` instead »), et ce nom existe dans votre version épinglée. Un texte qui ne mentionne un nom qu'à titre de conseil est cité sous `[diff]` ; il n'est pas pris pour un remplaçant. | Que le remplaçant se comporte de la même façon. |
| `[diff + move checked]` | Comme `[diff]`, et l'objet au nouveau chemin est le même objet, autant qu'on puisse le compter : une classe ou un module garde au moins la moitié des noms publics de l'ancien, une fonction garde ses paramètres, une valeur est la même. | Le comportement. |
| `[diff + metadata]` | Le Requires-Dist de l'ancienne version (le METADATA de sa wheel) liste une bibliothèque que la version épinglée ne liste plus, et les endroits de l'API publique qui nommaient des types de cette bibliothèque (paramètres, types de retour, attributs, classes de base, réexportations) nomment des types d'une autre bibliothèque que la version épinglée requiert, ou d'une copie de l'ancienne qu'elle embarque, sans qu'il reste aucun de l'ancienne : openai 3.x, anthropic 1.8, huggingface-hub 2.0 et mcp 2.2 prennent des objets `httpx2` là où ils prenaient des objets `httpx`. | Le comportement : si la version épinglée accepte encore les objets de l'ancienne bibliothèque (openai 3 en convertit certains, anthropic 1.8 lève `TypeError`, d'après leur code source). Le terminal, report.md, les outils MCP et le JSON ajoutent une ligne « Installed: » (votre projet, environnement virtuel compris, a-t-il encore l'ancienne bibliothèque) et une ligne « Runtime: » qui indique où le code source épinglé la nomme encore. |
| `[diff; probable rename]` | Un paramètre à la même position, avec la même annotation, porte un nouveau nom, et aucune note de version de la docstring de la version épinglée (`.. versionadded::`, `.. versionchanged::`) ne dit que l'un a été ajouté ou l'autre supprimé. Une supposition, signalée comme telle. | Qu'il s'agisse du même paramètre. |
| `[type-checked]` | Rédigée par un modèle pendant `since-cutoff run` et conservée parce que son exemple a passé la vérification de types décrite plus bas. | Le comportement ; que l'explication de la puce soit vraie au-delà des noms qu'elle montre. |
| `[not confirmed]` | Seulement avec `sync --suggestions` : des noms de la version épinglée qui ressemblent à ce qui a été supprimé (l'étiquette de la puce est alors `[diff; not confirmed]`). | Que l'un d'eux le remplace. |

Les étiquettes se combinent : les éléments de preuve sont reliés par `+` (`[diff + library]`), et
une supposition vient après `;` (`[diff; probable rename]`, `[diff; not confirmed]`).

Les noms qui ne font que se ressembler ne sont jamais écrits dans les notes par défaut. Le
terminal, report.md et les outils MCP les présentent comme « not confirmed as replacements »
(non confirmés comme remplaçants), et ne les montrent pas du tout quand la bibliothèque dit qu'il
n'y a pas de remplaçant ; `sync --suggestions` les ajoute, avec l'étiquette `[not confirmed]`.

**La vérification de types derrière `[type-checked]`.** Une note rédigée par le modèle n'est
conservée que si toutes ces conditions sont remplies (le modèle a deux essais) :

- son exemple complet s'analyse sans erreur et importe le paquet ;
- basedpyright (mode standard, dépréciations signalées comme des erreurs, commentaires
  `# type: ignore` et `# pyright:` retirés) ne signale aucune erreur attribuée à ce paquet, ni
  aucune de ses dépréciations, quand l'exemple est vérifié sur les sources de la version exacte
  de votre fichier de verrouillage et les dépendances d'exécution de cette version, dans un
  environnement par ailleurs vide ;
- chaque nom de la bibliothèque que la puce met entre accents graves figure dans cet exemple ou
  dans l'entrée du diff d'API dont parle la note ;
- la puce compte au plus 60 mots.

`[type-checked]` signifie donc que les imports, les noms, les paramètres et le nombre
d'arguments qu'utilise l'exemple existent dans votre version et ne sont pas marqués
`@deprecated` (PEP 702). Les erreurs extérieures au paquet (bibliothèque standard, autres
bibliothèques) ne bloquent pas une note. Cela ne signifie pas que le code se comporte
correctement à l'exécution, que le remplaçant est celui que recommandent les mainteneurs, ni que
la note aide le modèle. Quand une note échoue à cette vérification, since-cutoff écrit à la
place la note tirée du diff d'API, avec son étiquette.

**Mesuré** est une autre affaire : `since-cutoff run` fait répondre à des tâches réservées sans
puis avec les notes et en donne les comptes. Rien d'autre dans since-cutoff ne dit si une note
aide.

**Dans `run`, une réponse compte comme correcte** quand elle importe le paquet, utilise l'API
modifiée et ne présente aucune erreur de « connaissance » attribuée au paquet dans votre version
(nom, import ou paramètre inconnu ; argument obligatoire manquant ; mauvais nombre d'arguments).
Les remarques de pure rigueur de typage sont ignorées. Aucune réponse n'est exécutée.

Le bloc d'AGENTS.md contient les puces avec leurs étiquettes et, pour chaque paquet, la version à
laquelle s'appliquent ses notes et la version à la date limite. Le reste se trouve dans
`scan --json` et `results.json` : `used_apis[]` (chaque API modifiée qu'utilise votre code, où,
ses changements, ses remplaçants avec leur source, et sa note avec `tags`, `applies_to` et
`checks`) et, après `run`, `notes_detail[]` (chaque note avec l'exemple du modèle et ce qu'a
mesuré le test sur les tâches réservées ; `verified` est conservé et signifie la même chose que
`checks.example_type_checks`).

## Ce qu'il exécute, envoie et stocke

- **N'exécute aucun code de paquet ni aucun code écrit par le modèle.** Les paquets sont lus de
  façon statique (griffe avec l'inspection désactivée ; seuls les fichiers `.py`/`.pyi` sont
  extraits, avec des contrôles de chemin et de taille). Les réponses du modèle sont uniquement
  soumises au vérificateur de types, en local, avec basedpyright.
- **Récupère** les métadonnées publiques et les wheels des paquets sur PyPI, et les dates limites
  des modèles sur models.dev (un instantané est inclus pour une utilisation hors ligne). Les
  dépendances Git, locales (path), de workspace ou issues d'un index privé ne sont jamais
  recherchées par leur nom sur le PyPI public. `status` ne récupère rien.
- **Envoie** des prompts uniquement lors de `run`, et uniquement au fournisseur de modèle que vous
  choisissez : noms et versions des paquets, signatures publiques et docstrings des API
  modifiées, tâches générées et, pour les notes, les propres réponses du modèle. Jamais votre
  code source. `scan`, `sync`, `status`, le serveur MCP, l'action GitHub et les hooks pre-commit
  n'envoient jamais rien à un modèle.
- **Montre où votre code utilise une API modifiée** (pour l'instant, des noms de fichiers) dans le
  terminal et dans `.since-cutoff/`, et ne l'envoie nulle part. Ce que vous en transmettez ne
  dépend que de vous : `--markdown` et `--annotate github` le placent dans le résumé et les
  annotations d'un job de CI, et l'outil MCP `project_changes` le renvoie à l'agent qui l'a
  appelé, lequel transmet les résultats de ses outils à son modèle.
- **Stocke** les résultats dans `.since-cutoff/` au sein de votre projet (le dossier s'exclut
  lui-même de git) et dans un cache local (`since-cutoff cache path` indique son emplacement,
  `since-cutoff cache clear` le supprime). `sync` (quand vous acceptez, ou avec `--yes`) et
  `run --apply` écrivent un seul bloc balisé dans AGENTS.md ou CLAUDE.md et laissent le reste du
  fichier intact, octet pour octet ; `since-cutoff unapply` retire le bloc.
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
- « Your code uses » (votre code utilise) est une correspondance statique de noms, fichier par
  fichier : imports (noms réexportés compris), appels, lectures d'attributs et arguments nommés.
  Il ne suit pas les accès dynamiques comme `getattr`, et il indique pour l'instant des fichiers,
  pas des lignes ([#8](https://github.com/MohammadHijjawi97/since-cutoff/issues/8)). Un paramètre
  devenu obligatoire, nommé seulement ou positionnel seulement est toujours marqué « uses this
  API », jamais « old form », pour l'instant.
- Une note ne nomme un remplaçant que si le texte de dépréciation de la bibliothèque elle-même
  l'énonce. Un conseil qui ne figure que dans un guide de migration (le
  [MIGRATION.md](https://github.com/anthropics/anthropic-sdk-python/blob/main/MIGRATION.md)
  d'anthropic suggère `extra_body` pour les anciens modèles qui acceptent encore `temperature`)
  n'est pas dans les notes.
- Les sondes portent sur un **échantillon** classé des changements incompatibles (en commençant
  par les symboles que votre code utilise déjà), pas sur leur totalité.
- « La version de comparaison » est la version la plus récente publiée au plus tard à la date
  limite. Les modèles connaissent moins bien les versions récentes, si bien que leurs
  connaissances peuvent être périmées plus tôt.
- Les tâches réservées sont des reformulations du même changement : elles montrent qu'une note
  corrige *ce* changement, pas que le modèle s'est amélioré en général.

### À quoi sert la date limite d'entraînement

La date limite choisit un point de comparaison. Elle ne dit rien de ce qu'un modèle a mémorisé.
since-cutoff prend la date sur models.dev (ou dans `--cutoff`) ; un mois désigne son dernier jour
(`2025-07` correspond au 31 juillet 2025). Pour chaque dépendance, il prend la version finale non
retirée (*yanked*) la plus récente mise en ligne au plus tard à cette date (une préversion seulement
si le paquet n'avait encore aucune version finale, jamais une version de développement), et compare
son API publique avec votre version épinglée. Ce diff est une liste de candidats : des changements
d'API que les données d'entraînement du modèle n'incluent probablement pas.

La date détermine trois choses :

- les paquets à comparer : un paquet dont la version épinglée n'est pas plus récente que cette
  version n'a rien à comparer, et un paquet publié pour la première fois après cette date est
  signalé comme nouveau ;
- dans `run`, les versions des dépendances propres à cette version avec lesquelles le côté ancien
  passe le vérificateur de types (la plus récente que chaque exigence autorisait à cette date) ;
- le côté « ancien » de chaque sonde de `run` : une réponse valide de ce côté et invalide pour votre
  version, avec une erreur sur une API modifiée, compte comme « stale » et non comme « wrong ».

Un modèle peut connaître une version postérieure à sa date limite déclarée, ou ignorer des versions
publiées peu avant, si bien que l'analyse peut lister des changements que le modèle gère déjà et en
manquer certains qu'il ne gère pas. Seul `run` montre si le modèle écrit vraiment l'ancienne API : il
lui pose la question, sans outils, en lui indiquant la version épinglée par le projet.

## Comparaison avec d'autres outils

since-cutoff répond à une question pour un projet : quelles API publiques des versions que vous
épinglez ont changé depuis la version à laquelle renvoie la date limite d'entraînement d'un modèle,
en commençant par celles qu'utilise votre code ? `since-cutoff run` ajoute deux questions
facultatives : ce modèle se trompe-t-il vraiment sur ces API ? Une courte note corrige-t-elle
l'erreur ? La plupart des outils ci-dessous répondent à une autre question (« que dit la
documentation de la bibliothèque aujourd'hui ? ») et se combinent bien avec since-cutoff.

| outil | ce qu'il fait | positionnement de since-cutoff |
|---|---|---|
| [Context7](https://github.com/upstash/context7) (serveur MCP et CLI `ctx7`) | L'agent appelle `resolve-library-id` et `query-docs` pour faire entrer des extraits de documentation dans son contexte pendant qu'il travaille. Il sert une version précise (`/org/project/version`) quand les propriétaires de la bibliothèque ont ajouté cette version (tags ou branches git, 20 au maximum) ; sinon, il sert la branche indexée. Fonctionne sans clé d'API, avec une limite de requêtes anonyme plus basse. | Complémentaire. Context7 fournit de la documentation ; il ne lit pas vos versions épinglées et ne vérifie pas le code écrit par l'agent. since-cutoff liste celles de vos API épinglées qui ont changé après la date limite du modèle, en commençant par celles qu'utilise votre code, pour que vous sachiez où une recherche ou une note est nécessaire. Quand vous interrogez Context7, précisez la version que vous épinglez. |
| Autres serveurs de documentation : [Ref](https://github.com/ref-tools/ref-tools-mcp), [docs-mcp-server](https://github.com/arabold/docs-mcp-server) | Recherche de documentation pour agents, au moment de répondre ; docs-mcp-server peut indexer la documentation en local | Comme pour Context7. |
| [library-skills](https://github.com/tiangolo/library-skills) | Des bibliothèques comme FastAPI et Streamlit livrent des skills pour agents dans leurs paquets ; `uvx library-skills` relie les skills des versions installées dans `.agents/skills` ou `.claude/skills`, si bien qu'ils se mettent à jour avec la bibliothèque | Écrits par les mainteneurs et en phase avec la version installée : quand une bibliothèque en livre un, utilisez-le. since-cutoff couvre les paquets qui ne livrent aucune consigne, et se limite aux changements de la surface de l'API. |
| Plugins de skills des éditeurs, p. ex. [pydantic/skills](https://github.com/pydantic/skills) | Plugins Claude Code, Codex et Cursor et fichiers `SKILL.md` pour Pydantic, Pydantic AI et Logfire, installés depuis le dépôt | Consignes des mainteneurs pour bien utiliser une bibliothèque ; publiées avec le dépôt de plugins, pas avec la version que vous épinglez. Les notes de since-cutoff sont rédigées pour votre lockfile. |
| Codemods : règles [ast-grep](https://ast-grep.github.io/), `openai migrate` d'OpenAI ([Grit](https://github.com/openai/openai-python/discussions/742)) | Réécrivent du code existant avec des règles syntaxiques écrites à la main ; le catalogue d'ast-grep propose une [migration du SDK OpenAI](https://ast-grep.github.io/catalog/python/#migrate-openai-sdk) (de `openai.Completion.create(...)` vers `client.completions.create(...)`) | Pour migrer le code que vous avez déjà, un codemod est le bon outil. since-cutoff porte sur le code que l'assistant va écrire ensuite : il trouve les changements dans le diff d'API plutôt que dans des règles écrites par quelqu'un, et se contente de suggérer ; il ne réécrit rien. |
| Bots de dépendances : [Renovate](https://github.com/renovatebot/renovate), [Dependabot](https://github.com/dependabot/dependabot-core) | Ouvrent des pull requests qui mettent à jour vos versions épinglées | L'action GitHub peut s'exécuter sur ces pull requests, lister les API modifiées qu'utilise votre code et les fichiers qui les utilisent et, avec `check-notes`, échouer jusqu'à ce que les notes soient synchronisées. |
| Benchmarks : [GitChameleon 2.0](https://arxiv.org/abs/2507.12367), [VersiCode](https://arxiv.org/abs/2406.07411), [CodeUpdateArena](https://arxiv.org/abs/2407.06249), [LibEvolutionEval](https://arxiv.org/abs/2412.04478) | Mesurent des modèles sur des ensembles de tâches fixes, construits à partir de vrais changements de version ou de changements synthétiques (CodeUpdateArena) ; GitChameleon 2.0 exécute des tests unitaires | Ils comparent des modèles en général, et certains vérifient le comportement en exécutant des tests. since-cutoff examine les versions épinglées d'un projet, de façon statique : un vérificateur de types voit les noms, les paramètres et les dépréciations, pas le comportement. |

`--compare signatures` de `since-cutoff run` donne au modèle la signature de la nouvelle version et
le premier paragraphe de sa docstring. C'est un substitut local à une recherche de documentation,
pas Context7.

Deux outils plus modestes s'attaquent au même problème : [cutoff](https://github.com/sandeepsirodia/cutoff)
teste une bibliothèque que vous maintenez en exécutant des programmes écrits par le modèle sur sa
version actuelle, et [postcut](https://github.com/justi/postcut) transforme un `Gemfile.lock`
Ruby en un récapitulatif des changements intervenus depuis la date limite. since-cutoff s'appuie
sur [griffe](https://mkdocstrings.github.io/griffe/),
[basedpyright](https://github.com/DetachHead/basedpyright), [models.dev](https://models.dev) et
[rich](https://github.com/Textualize/rich).

### Utiliser since-cutoff avec Context7

`since-cutoff scan` vous dit quelles API chercher ; Context7 peut fournir la documentation.
Précisez la version que vous épinglez quand vous posez la question (« anthropic 1.8.0 »). Context7
ne la trouve que si les propriétaires de la bibliothèque [ont ajouté cette version](https://github.com/upstash/context7/blob/master/docs/howto/claiming-libraries.mdx) :
le 27 septembre 2026, `/openai/openai-python` proposait v1.68.0, v1_105_0, v2.8.1 et v2.11.0, et
`/anthropics/anthropic-sdk-python` aucune, si bien que vous risquez d'obtenir la documentation de
la branche par défaut.

## Travaux de recherche liés

- **API dépréciées dans la complétion de code.** Wang et al., *LLMs Meet Library Evolution:
  Evaluating Deprecated API Usage in LLM-based Code Completion* (ICSE 2025 ;
  [arXiv:2406.09834](https://arxiv.org/abs/2406.09834), d'abord intitulé *How and Why LLMs Use
  Deprecated APIs in Code Completion? An Empirical Study*). 7 modèles, 145 correspondances entre
  une API dépréciée et sa remplaçante dans 8 bibliothèques Python, 28 125 prompts de complétion.
  La plupart des complétions n'utilisaient aucune des deux API. Parmi celles qui en utilisaient une
  (les complétions « plausibles », selon l'article), 25-38 % utilisaient l'API dépréciée sur
  l'ensemble des données : 70-90 % quand le prompt venait de code qui utilisait l'API dépréciée,
  9-18 % quand il venait de code à jour. Deux correctifs de référence ont été testés sur des prompts
  issus de code à jour pour lesquels un modèle avait utilisé l'API dépréciée. ReplaceAPI remplace
  les jetons de l'API dépréciée par la remplaçante pendant le décodage et laisse le modèle terminer
  la ligne : la remplaçante a ensuite été utilisée dans 85,2-99,6 % des cas avec les six modèles
  ouverts (il faut contrôler le décodage, donc pas avec GPT-3.5). InsertPrompt ajoute le commentaire
  `# {dep} is deprecated, use {rep} instead and revise the return value and arguments.` puis
  régénère : 25,7-97,2 % selon le modèle, ce que les auteurs ne jugent pas encore assez efficace ni
  assez précis. Les notes de since-cutoff sont proches d'InsertPrompt, déplacé dans le fichier
  d'instructions du projet ; `since-cutoff run` les mesure sur des tâches réservées au lieu de
  supposer qu'elles fonctionnent.
- **La documentation dans le contexte ne suffit pas à elle seule.** Ashik et al., *When LLMs Lag
  Behind: Knowledge Conflicts from Evolving APIs in Code Generation*
  ([arXiv:2604.09515](https://arxiv.org/abs/2604.09515), prépublication de 2026). 270 mises à jour
  d'API réelles (45 dépréciées ou supprimées, 128 modifiées, 97 nouvelles) tirées de versions de
  8 bibliothèques Python postérieures à décembre 2023, et 11 modèles de 4 familles dont les dates
  limites sont antérieures. Avec une simple description de la mise à jour, les modèles l'ont adoptée
  au moins en partie dans 74,64 % des réponses (selon GPT-5 mini utilisé comme juge), et 42,55 % de
  ces réponses s'exécutaient dans la version de la bibliothèque qui introduisait la mise à jour ;
  avec en plus la documentation de l'API, 92,87 % l'ont adoptée et 66,36 % s'exécutaient. Ajouter
  des prompts de raisonnement pas à pas (*chain-of-thought*) et d'autoréflexion (*self-reflection*)
  a encore amélioré le taux d'exécution de 11,33 %, un gain relatif et non en points de
  pourcentage. Parmi les réponses qui n'adoptaient pas la mise à jour, 42,1 % l'ignoraient
  complètement et 16,4 % utilisaient l'ancienne API ; parmi celles qui l'adoptaient mais ne
  s'exécutaient toujours pas dans la meilleure configuration, la cause la plus fréquente liée à la
  mise à jour était des paramètres incorrects (26,6 % de ces échecs). C'est pourquoi since-cutoff
  vérifie le code par rapport à votre version exacte, et pourquoi `since-cutoff run` teste à nouveau
  le modèle avec les notes au lieu de supposer qu'il les suit.
- **Benchmarks.** [GitChameleon 2.0](https://arxiv.org/abs/2507.12367) : 328 problèmes de
  complétion Python, chacun lié à des versions précises de bibliothèques et vérifié par des tests
  unitaires exécutables ; les modèles commerciaux atteignent 48-51 % au départ, la documentation
  récupérée ajoute jusqu'à environ 10 points (GPT-4.1 : de 48,5 % à 58,5 %) et l'autodébogage
  (*self-debugging*) environ 10 à 20. [VersiCode](https://arxiv.org/abs/2406.07411) : complétion de
  code propre à une version et migration de code tenant compte de la version, sur plus de
  300 bibliothèques Python et plus de 2 000 versions réparties sur 9 ans.
  [CodeUpdateArena](https://arxiv.org/abs/2407.06249) : édition des connaissances du modèle pour
  54 fonctions de 7 paquets Python, avec des mises à jour synthétiques générées par GPT-4 et
  670 exemples de synthèse de programmes ; placer la documentation de la mise à jour en tête du
  prompt n'a pas permis aux modèles ouverts (DeepSeek, CodeLlama) de l'utiliser.
  [LibEvolutionEval](https://arxiv.org/abs/2412.04478)
  ([NAACL 2025](https://aclanthology.org/2025.naacl-long.348/)) : complétion en ligne propre à une
  version sur 8 bibliothèques ; la documentation récupérée pour la version concernée et le
  prompting aident.

Ces études mesurent de nombreux modèles sur des ensembles de tâches fixes ; GitChameleon 2.0 et
Ashik et al. exécutent le code généré. since-cutoff fait quelque chose de plus restreint : pour un
projet, il liste les changements depuis une version de comparaison, en commençant par ceux
qu'utilise votre code, et `run` vérifie statiquement les réponses d'un modèle. Il ne voit pas les
changements de comportement derrière une signature inchangée, ce que voient les tests qui exécutent
le code.

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
