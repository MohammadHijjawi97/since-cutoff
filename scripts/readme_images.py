"""Render the README's designed images: the hero card and the "How it works" diagram.

    python scripts/readme_images.py            # writes docs/img/{hero,how-it-works}{,-dark}{,.es,.fr}.svg

Each image comes in a light and a dark variant; the README picks one with <picture> and
prefers-color-scheme, and PyPI (which drops <source>) shows the light one. The English images
are docs/img/hero.svg and docs/img/how-it-works.svg; README.es.md and README.fr.md (and the
Spanish and French pages under docs/) use the `.es` and `.fr` variants. The terminal captures
(scan.svg, run.svg, run-opus.svg) are real tool output and stay in English.

The SVGs use system fonts only, no scripts and no external resources, so they render the same
through GitHub's image proxy. Keep every line short enough to fit with the widest common
fallback font (DejaVu Sans, about 13% wider than Segoe UI): check a change by rendering the
images in a browser with DejaVu Sans forced, and leave a few percent of slack on each line.

The numbers on the hero are the Claude Opus 4.6 run in the README's results section, measured
with since-cutoff 0.1.0 on examples/agent-app. Keep the sample size and the version next to
them. "7 of 16" are probed API changes (out of 725 that 0.1.0 flagged for that cutoff), and the
stale answers used removed parameters (5) or removed names (2), so the caption says "a name or
parameter", not "a call".
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "img"

SANS = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONO = "ui-monospace, SFMono-Regular, 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"
NBSP = chr(0x00A0)  # French puts a no-break space before : ; ? ! and %
NNBSP = chr(0x202F)  # narrow no-break space, for the "5 %" in the large French figures


@dataclass(frozen=True)
class Theme:
    name: str
    bg: str
    border: str
    panel: str
    text: str
    muted: str
    accent: str
    red: str
    green: str
    code_bg: str


# GitHub's Primer colours; every text colour has at least 4.5:1 contrast on its background.
LIGHT = Theme(
    name="light",
    bg="#ffffff",
    border="#d0d7de",
    panel="#f6f8fa",
    text="#1f2328",
    muted="#59636e",
    accent="#bc4c00",
    red="#cf222e",
    green="#1a7f37",
    code_bg="#f6f8fa",
)
DARK = Theme(
    name="dark",
    bg="#0d1117",
    border="#3d444d",
    panel="#151b23",
    text="#f0f6fc",
    muted="#9198a1",
    accent="#f0883e",
    red="#ff7b72",
    green="#56d364",
    code_bg="#010409",
)


# ------------------------------------------------------------------------------- strings


@dataclass(frozen=True)
class HeroText:
    title: str
    desc: str
    headline: list[str]
    sample: str
    stale_of: str  # " of 16", after the red "7"
    stale_caption: list[str]
    heldout: tuple[str, str]  # before, after
    heldout_caption: list[str]
    command_note: list[str]  # one line, or two when the language needs the room


@dataclass(frozen=True)
class Stage:
    name: str
    sub: str
    steps: list[str]


@dataclass(frozen=True)
class FlowText:
    title: str
    desc: str
    stages: list[Stage]


HERO = {
    "en": HeroText(
        title="since-cutoff: your coding model learned your libraries before they changed",
        desc=(
            "Claude Opus 4.6, tested on one sample project with since-cutoff 0.1.0: on 7 of 16 "
            "probed API changes it used a name or parameter that has since been removed. With the "
            "notes, 5% -> 65% of 20 held-out tasks were correct. Try it: uvx since-cutoff "
            "scan (no model calls, no API key)."
        ),
        headline=["Your coding model learned your", "libraries before they changed."],
        sample="Claude Opus 4.6 on one sample project (since-cutoff 0.1.0):",
        stale_of=" of 16",
        stale_caption=[
            "probed API changes where it",
            "used a name or parameter",
            "that has since been removed",
        ],
        heldout=("5%", "65%"),
        heldout_caption=["of 20 held-out tasks correct,", "without → with the notes"],
        command_note=["no model calls, no API key"],
    ),
    "es": HeroText(
        title=(
            "since-cutoff: tu modelo de IA para programar aprendió tus bibliotecas antes de que "
            "cambiaran"
        ),
        desc=(
            "Claude Opus 4.6, evaluado en un único proyecto de ejemplo con since-cutoff 0.1.0: en "
            "7 de 16 cambios de API sondeados usó un nombre o un parámetro que ya se había "
            "eliminado. Con las notas, los aciertos en 20 tareas reservadas pasaron "
            "del 5 % al 65 %. Pruébalo: uvx since-cutoff scan (sin llamadas al modelo, sin clave "
            "de API)."
        ),
        headline=[
            "Tu modelo de IA para programar",
            "aprendió tus bibliotecas",
            "antes de que cambiaran.",
        ],
        sample="Claude Opus 4.6 en un solo proyecto (since-cutoff 0.1.0):",
        stale_of=" de 16",
        stale_caption=[
            "cambios de API sondeados",
            "en los que usó un nombre o",
            "un parámetro ya eliminado",
        ],
        heldout=("5%", "65%"),
        heldout_caption=["de aciertos en 20 tareas", "reservadas, sin → con notas"],
        command_note=["sin llamadas al modelo,", "sin clave de API"],
    ),
    "fr": HeroText(
        title=(
            "since-cutoff : votre modèle de code a appris vos bibliothèques avant qu'elles ne "
            "changent"
        ).replace(" :", NBSP + ":"),
        desc=(
            "Claude Opus 4.6, testé sur un seul projet d'exemple avec since-cutoff 0.1.0 : sur 7 des "
            "16 changements d'API sondés, il a utilisé un nom ou un paramètre supprimé depuis. Avec "
            "les notes, la part de tâches réservées correctes est passée de 5 % à 65 % "
            "(20 tâches). Essayez : uvx since-cutoff scan (aucun appel au modèle, aucune clé d'API)."
        )
        .replace(" :", NBSP + ":")
        .replace(" %", NBSP + "%"),
        headline=[
            "Votre modèle de code a appris",
            "vos bibliothèques avant",
            "qu'elles ne changent.",
        ],
        sample=f"Claude Opus 4.6 sur un seul projet (since-cutoff 0.1.0){NBSP}:",
        stale_of=" sur 16",
        stale_caption=[
            "changements d'API sondés",
            "où il a utilisé un nom ou",
            "un paramètre supprimé depuis",
        ],
        heldout=(f"5{NNBSP}%", f"65{NNBSP}%"),
        heldout_caption=["de 20 tâches réservées", "réussies, sans → avec notes"],
        command_note=["aucun appel au modèle,", "aucune clé d'API"],
    ),
}

FLOW = {
    "en": FlowText(
        title="How since-cutoff works",
        desc=(
            "Scan, with no model calls: read the lockfile, find each dependency's release at the "
            "model's training cutoff, and diff the public API statically with griffe. Probe: write "
            "short tasks that need a changed API, let the model answer with no tools and no docs, "
            "and type-check the answer with basedpyright against both versions. Write and test "
            "notes: keep a model-written note only if its example type-checks, otherwise state the "
            "change from the API diff; then compare held-out tasks without and with the notes, and "
            "write the block into AGENTS.md with --apply."
        ),
        stages=[
            Stage(
                "1  Scan",
                "no model calls",
                [
                    "Lockfile: your exact versions",
                    "The release at the model's cutoff",
                    "Static API diff with griffe",
                ],
            ),
            Stage(
                "2  Probe",
                "the model, no tools",
                [
                    "Short tasks that need the change",
                    "The model answers from memory",
                    "basedpyright checks both versions",
                ],
            ),
            Stage(
                "3  Write and test notes",
                "the type checker decides",
                [
                    "Notes: type-checked, or from the diff",
                    "Held-out tasks: without vs. with notes",
                    "--apply writes a block to AGENTS.md",
                ],
            ),
        ],
    ),
    "es": FlowText(
        title="Cómo funciona since-cutoff",
        desc=(
            "Análisis, sin llamadas al modelo: se lee el lockfile, se busca la versión de cada "
            "dependencia en la fecha de corte de entrenamiento del modelo y se compara la API "
            "pública de forma estática con griffe. Sondeo: tareas breves que requieren una API "
            "modificada, el modelo responde sin herramientas ni documentación y basedpyright "
            "comprueba la respuesta con ambas versiones. Escribir y probar las notas: una nota "
            "escrita por el modelo solo se conserva si su ejemplo pasa la verificación de tipos; "
            "si no, se usa una descripción del cambio tomada del diff de la API. Después se "
            "comparan las tareas reservadas sin las notas y con ellas, y --apply escribe el bloque "
            "en AGENTS.md."
        ),
        stages=[
            Stage(
                "1  Análisis",
                "sin llamadas al modelo",
                [
                    "Lockfile: tus versiones exactas",
                    "La versión en la fecha de corte del modelo",
                    "Diff estático de la API con griffe",
                ],
            ),
            Stage(
                "2  Sondeo",
                "el modelo, sin herramientas",
                [
                    "Tareas breves que requieren el cambio",
                    "El modelo responde de memoria",
                    "basedpyright comprueba ambas versiones",
                ],
            ),
            Stage(
                "3  Escribir y probar las notas",
                "decide el verificador de tipos",
                [
                    "Notas: tipos comprobados, o del diff",
                    "Tareas reservadas: sin las notas y con ellas",
                    "--apply escribe un bloque en AGENTS.md",
                ],
            ),
        ],
    ),
    "fr": FlowText(
        title="Fonctionnement de since-cutoff",
        desc=(
            "Analyse, sans appel au modèle : le fichier de verrouillage donne vos versions exactes, "
            "since-cutoff trouve la version de chaque dépendance à la date limite d'entraînement du "
            "modèle et compare l'API publique de façon statique avec griffe. Sonde : de courtes "
            "tâches qui nécessitent une API modifiée, le modèle répond sans outils ni "
            "documentation, et basedpyright vérifie la réponse avec les deux versions. Rédaction "
            "et test des notes : une note rédigée par le modèle n'est conservée que si son exemple "
            "passe la vérification de types ; sinon, un constat du changement tiré du diff d'API "
            "la remplace. Le modèle répond ensuite aux tâches réservées sans puis avec les notes, "
            "et --apply écrit le bloc dans AGENTS.md."
        )
        .replace(" :", NBSP + ":")
        .replace(" ;", NBSP + ";"),
        stages=[
            Stage(
                "1  Analyse",
                "aucun appel au modèle",
                [
                    f"Verrouillage{NBSP}: vos versions exactes",
                    "La version à la date limite du modèle",
                    "Diff statique de l'API avec griffe",
                ],
            ),
            Stage(
                "2  Sonde",
                "le modèle, sans outils",
                [
                    "Des tâches qui exigent le changement",
                    "Le modèle répond de mémoire",
                    "basedpyright vérifie les deux versions",
                ],
            ),
            Stage(
                "3  Rédaction et test des notes",
                "le vérificateur de types tranche",
                [
                    f"Notes{NBSP}: typage vérifié, ou tirées du diff",
                    f"Tâches réservées{NBSP}: sans puis avec les notes",
                    "--apply écrit un bloc dans AGENTS.md",
                ],
            ),
        ],
    ),
}


# ------------------------------------------------------------------------------- helpers


def _style(t: Theme) -> str:
    return f"""<style>
    .sans {{ font-family: {SANS}; }}
    .mono {{ font-family: {MONO}; }}
    .text {{ fill: {t.text}; }}
    .muted {{ fill: {t.muted}; }}
    .accent {{ fill: {t.accent}; }}
    .red {{ fill: {t.red}; }}
    .green {{ fill: {t.green}; }}
    .b {{ font-weight: 700; }}
    .sb {{ font-weight: 600; }}
  </style>"""


def _text(x: float, y: float, size: float, cls: str, content: str, anchor: str = "start") -> str:
    a = "" if anchor == "start" else f' text-anchor="{anchor}"'
    return f'<text x="{x:g}" y="{y:g}" font-size="{size:g}" class="{cls}"{a}>{content}</text>'


def _open(width: float, height: float, lang: str, title: str, desc: str) -> list[str]:
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:g}" height="{height:g}" '
        f'viewBox="0 0 {width:g} {height:g}" role="img" aria-labelledby="title desc" '
        f'xml:lang="{lang}">',
        f'<title id="title">{escape(title)}</title>',
        f'<desc id="desc">{escape(desc)}</desc>',
    ]


def _clock(x: float, y: float, size: float, t: Theme) -> str:
    """The project icon (assets/icon.svg) scaled into a size x size box at x, y."""
    s = size / 512
    return (
        f'<g transform="translate({x:g} {y:g}) scale({s:g})">'
        f'<circle cx="256" cy="256" r="148" fill="none" stroke="{t.accent}" stroke-width="44"/>'
        f'<path d="M256 168v88l62 38" fill="none" stroke="{t.text}" stroke-width="40" '
        'stroke-linecap="round" stroke-linejoin="round"/></g>'
    )


# ------------------------------------------------------------------------------------ hero

HERO_W = 640
HEADLINE_SIZE, HEADLINE_STEP = 30, 38
CAPTION_SIZE, CAPTION_STEP = 19, 25
NUMBER_SIZE = 42
CAPTION_X = 272  # from the left padding


def hero(t: Theme, lang: str = "en") -> str:
    """640 wide, so that on a phone (about 360 px) the captions still render at about 11 px."""
    s = HERO[lang]
    pad = 28
    right = HERO_W - pad
    body: list[str] = [
        # brand
        _clock(pad - 3, 29, 28, t),
        _text(pad + 32, 50, 20, "mono b accent", "since-cutoff"),
    ]
    # the problem
    y = 96
    for line in s.headline:
        body.append(_text(pad, y, HEADLINE_SIZE, "sans b text", escape(line)))
        y += HEADLINE_STEP
    body.append(_text(pad, y, 18, "sans muted", escape(s.sample)))
    top = y + 18
    # two result rows: big number left, caption right
    before, after = s.heldout
    rows = [
        (
            f'<tspan class="red">7</tspan><tspan class="text">{escape(s.stale_of)}</tspan>',
            s.stale_caption,
        ),
        (
            f'<tspan class="red">{escape(before)}</tspan>'
            '<tspan class="muted" font-weight="400"> → </tspan>'
            f'<tspan class="green">{escape(after)}</tspan>',
            s.heldout_caption,
        ),
    ]
    height = max(88, 20 + CAPTION_STEP * max(len(c) for _, c in rows))
    for number, caption in rows:
        body.append(
            f'<rect x="{pad}" y="{top}" width="{right - pad}" height="{height}" rx="10" '
            f'fill="{t.panel}" stroke="{t.border}"/>'
        )
        mid = top + height / 2
        body.append(_text(pad + 20, mid + 15, NUMBER_SIZE, "sans b", number))
        # caption lines centred on the box; a line's visual centre is about 0.35 em above its baseline
        first = mid - CAPTION_STEP * (len(caption) - 1) / 2 + 0.35 * CAPTION_SIZE
        for i, line in enumerate(caption):
            body.append(
                _text(
                    pad + CAPTION_X,
                    first + i * CAPTION_STEP,
                    CAPTION_SIZE,
                    "sans text",
                    escape(line),
                )
            )
        top += height + 12
    # the command, and what it does not need (one line, or two lines in a taller box)
    top += 2
    box_h = 44 if len(s.command_note) == 1 else 52
    body += [
        f'<rect x="{pad}" y="{top}" width="{right - pad}" height="{box_h}" rx="8" '
        f'fill="{t.code_bg}" stroke="{t.border}"/>',
        _text(
            pad + 18,
            top + box_h / 2 + 7,
            21,
            "mono",
            '<tspan class="muted">$ </tspan><tspan class="text b">uvx since-cutoff scan</tspan>',
        ),
    ]
    if len(s.command_note) == 1:
        body.append(_text(right - 16, top + 28, 16, "sans muted", escape(s.command_note[0]), "end"))
    else:
        for i, line in enumerate(s.command_note):
            body.append(_text(right - 16, top + 22 + 18 * i, 15, "sans muted", escape(line), "end"))
    total_h = top + box_h + 24
    parts = [
        *_open(HERO_W, total_h, lang, s.title, s.desc),
        _style(t),
        '<clipPath id="card"><rect x="0.5" y="0.5" '
        f'width="{HERO_W - 1}" height="{total_h - 1}" rx="16"/></clipPath>',
        f'<rect x="0.5" y="0.5" width="{HERO_W - 1}" height="{total_h - 1}" rx="16" '
        f'fill="{t.bg}" stroke="{t.border}"/>',
        f'<rect x="0" y="0" width="{HERO_W}" height="6" fill="{t.accent}" clip-path="url(#card)"/>',
        *body,
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


# ---------------------------------------------------------------------------- how it works

FLOW_W = 640
STAGE_SIZE, SUB_SIZE, STEP_SIZE = 22, 17, 20
STEP_GAP = 29  # between step baselines
CARD_GAP = 26  # between cards, room for the arrow


def _fits_one_line(stage: Stage, inner: float) -> bool:
    """Whether the stage name and its subtitle fit on one header line.

    A rough, deliberately generous width estimate (DejaVu Sans proportions), so that the
    subtitle moves to its own line well before it could touch the name.
    """
    name_w = 0.68 * STAGE_SIZE * len(stage.name)
    sub_w = 0.58 * SUB_SIZE * len(stage.sub)
    return name_w + 32 + sub_w <= inner


def flow(t: Theme, lang: str = "en") -> str:
    """Three stage cards stacked top to bottom, each with its three steps on a short rail.

    Stacked rather than side by side, so the steps can be whole phrases at 20 px: in a 640 px
    image shown about 400 px wide on a phone, that is still about 12 px.
    """
    s = FLOW[lang]
    pad = 16
    card_x, card_w = pad, FLOW_W - 2 * pad
    inner = card_w - 40
    rail_x = card_x + 30
    body: list[str] = []
    y = pad
    for i, stage in enumerate(s.stages):
        one_line = _fits_one_line(stage, inner)
        header_h = 46 if one_line else 68
        card_h = header_h + STEP_GAP * (len(stage.steps) - 1) + 30
        body.append(
            f'<rect x="{card_x}" y="{y}" width="{card_w}" height="{card_h}" rx="10" '
            f'fill="{t.panel}" stroke="{t.border}"/>'
        )
        body.append(_text(card_x + 20, y + 33, STAGE_SIZE, "sans b accent", escape(stage.name)))
        if one_line:
            body.append(
                _text(
                    card_x + card_w - 20, y + 32, SUB_SIZE, "sans muted", escape(stage.sub), "end"
                )
            )
        else:
            body.append(_text(card_x + 20, y + 57, SUB_SIZE, "sans muted", escape(stage.sub)))
        first = y + header_h + 18
        last = first + STEP_GAP * (len(stage.steps) - 1)
        # the rail: a line through the step markers, so each stage reads as a sequence
        body.append(
            f'<line x1="{rail_x}" y1="{first - 6}" x2="{rail_x}" y2="{last - 6}" '
            f'stroke="{t.border}" stroke-width="2"/>'
        )
        for j, step in enumerate(stage.steps):
            sy = first + j * STEP_GAP
            body.append(
                f'<circle cx="{rail_x}" cy="{sy - 6}" r="5" fill="{t.bg}" stroke="{t.muted}" '
                'stroke-width="2"/>'
            )
            body.append(_text(rail_x + 18, sy, STEP_SIZE, "sans text", escape(step)))
        y += card_h
        if i < len(s.stages) - 1:
            # down to the next stage
            body.append(
                f'<line x1="{rail_x}" y1="{y + 3}" x2="{rail_x}" y2="{y + CARD_GAP - 4}" '
                f'stroke="{t.muted}" stroke-width="2" marker-end="url(#head)"/>'
            )
            y += CARD_GAP
    total_h = y + pad
    parts = [
        *_open(FLOW_W, total_h, lang, s.title, s.desc),
        _style(t),
        '<defs><marker id="head" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M0 0L10 5L0 10z" fill="{t.muted}"/></marker></defs>',
        f'<rect x="0.5" y="0.5" width="{FLOW_W - 1}" height="{total_h - 1}" rx="14" '
        f'fill="{t.bg}" stroke="{t.border}"/>',
        *body,
        "</svg>",
    ]
    return "\n".join(parts) + "\n"


def main() -> None:
    for lang in ("en", "es", "fr"):
        suffix_lang = "" if lang == "en" else f".{lang}"
        for theme, suffix in ((LIGHT, ""), (DARK, "-dark")):
            for stem, render in (("hero", hero), ("how-it-works", flow)):
                path = OUT / f"{stem}{suffix}{suffix_lang}.svg"
                path.write_text(render(theme, lang), encoding="utf-8", newline="\n")
                print(f"wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    sys.exit(main())
