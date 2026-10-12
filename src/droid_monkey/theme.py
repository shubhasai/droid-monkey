"""How droid-monkey looks in a terminal: the banner, section headers, monkey-face badges, the banana meter, a spinner.

Plain ASCII art only, so it renders the same in any terminal, font or CI log. Only presentation lives here; every
line the run log records stays plain text. Colour follows click (stripped when stdout is not a terminal) and NO_COLOR.
"""

import itertools
import os
import random
import re
import shutil
import sys
import threading
import time
from contextlib import contextmanager

import click


def style(text, **kw) -> str:
    return str(text) if os.environ.get("NO_COLOR") else click.style(text, **kw)


def width() -> int:
    return min(shutil.get_terminal_size((96, 24)).columns, 96)


MONKEY = r'''
 ~~~~~~~~~~~~~~~~~~~~
          \\
           \\ .-"-.
            \/.-.-.\_
            ( ( o o ) )
             |/  "  \|
              \ '-' /
              /`"""`\
             / /   \ \
            (_/     \_)
'''.strip("\n").splitlines()

TITLE = r'''
 ____  ____   ___ ___ ____    __  __  ___  _   _ _  _______   __
|  _ \|  _ \ / _ \_ _|  _ \  |  \/  |/ _ \| \ | | |/ / ____\ \ / /
| | | | |_) | | | | || | | | | |\/| | | | |  \| | ' /|  _|  \ V /
| |_| |  _ <| |_| | || |_| | | |  | | |_| | |\  | . \| |___  | |
|____/|_| \_\\___/___|____/  |_|  |_|\___/|_| \_|_|\_\_____| |_|
'''.strip("\n").splitlines()

QUIPS = [
    "Releasing the monkey into your app...",
    "Shaking the tree to see what falls out.",
    "No bananas were harmed in the making of this run.",
    "Monkey see, monkey break, monkey fix.",
    "Swinging from Activity to Activity.",
    "Infinite monkeys, finite bugs.",
    "Hide your state: the monkey is coming.",
]


def banner(subtitle: str = "") -> None:
    """A monkey hanging off the vine next to the name; once per command, only on a real terminal."""
    if not sys.stdout.isatty() or os.environ.get("DROID_MONKEY_NO_BANNER"):
        return
    quip = style(f"~ {random.choice(QUIPS)}", fg="bright_black") + (style(f"   [{subtitle}]", fg="cyan") if subtitle else "")
    right = ["", "", ""] + [style(t, fg="yellow", bold=True) for t in TITLE] + ["", quip] \
        if shutil.get_terminal_size((96, 24)).columns >= 92 else ["", "", "", "", style("DROID-MONKEY", fg="yellow", bold=True), "", "", quip]
    click.echo("")
    for i, art in enumerate(MONKEY):
        click.echo(style(f"{art:<26}", fg="yellow") + (right[i] if i < len(right) else ""))


def header(title: str, face: str = "(o.o)") -> None:
    """A section rule:  (o.o)==[ title ]=================="""
    head = f"{face}==[ {title} ]"
    click.echo("\n" + style(face, fg="yellow", bold=True) + style("==[ ", fg="bright_black")
               + style(title, bold=True) + style(" ]" + "=" * max(4, width() - len(head) - 2), fg="bright_black"))


def kv(key: str, value: str, pad: int = 12) -> None:
    click.echo(f"  {style(f'{key:<{pad}}', fg='bright_black')} {value}")


def note(text: str) -> None:
    click.echo(style(f"  .. {text}", fg="bright_black"))


def ok(text: str) -> None:
    click.echo(f"  {style(r'\o/', fg='green', bold=True)} {style(text, fg='green')}")


def warn(text: str) -> None:
    click.echo(f"  {style('o.O', fg='yellow', bold=True)} " + style(text, fg="yellow"))


def bad(text: str) -> None:
    click.echo(f"  {style('x_x', fg='red', bold=True)} " + style(text, fg="red"))


def path_line(label: str, path) -> None:
    click.echo(f"  {style('->', fg='bright_black')}  {style(f'{label:<11}', fg='bright_black')} "
               + style(str(path), underline=True))


def arrow() -> str:
    return style("`--", fg="bright_black")


# ---- outcomes ------------------------------------------------------------------------------------

# Attack outcomes (code decides these) and run statuses: each gets the face the monkey makes at it.
BADGES = {
    # attack outcomes
    "survived":    ("(^.^)", "green",   "survived"),
    "crashed":     ("(x_x)", "red",     "crashed"),
    "lost_state":  ("(o.O)", "yellow",  "lost state"),
    "no_feedback": ("(-.-)", "magenta", "no feedback"),
    "leaked":      ("(;_;)", "cyan",    "leaked"),
    "froze":       ("(*_*)", "blue",    "froze"),
    "anr":         ("(@_@)", "red",     "ANR"),
    "slow_start":  ("(u_u)", "yellow",  "slow start"),
    "skipped":     ("(._.)", "white",   "skipped"),
    # run statuses
    "fixed":       ("(^o^)", "green",   "fixed"),
    "unfixed":     ("(x_x)", "red",     "unfixed"),
    "declined":    ("(-_-)", "yellow",  "declined"),
    "unreachable": ("(?.?)", "yellow",  "unreachable"),
}


def face(outcome: str) -> str:
    return BADGES.get(outcome, ("(o.o)",))[0]


def badge(outcome: str, pad: int = 12) -> str:
    glyph, color, label = BADGES.get(outcome, ("(o.o)", "white", outcome))
    loud = outcome not in ("survived", "skipped")
    return style(glyph, fg=color, bold=loud) + " " + style(f"{label:<{pad}}", fg=color, bold=loud)


def banana_meter(score: int, size: int = 20) -> str:
    """[))))))))))))))......] 72/100  (each ) is a banana)"""
    full = max(0, min(size, round(score / 100 * size)))
    color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
    return (style("[", fg="bright_black") + style(")" * full, fg="yellow", bold=True)
            + style("." * (size - full), fg="bright_black") + style("]", fg="bright_black")
            + " " + style(f"{score}/100", fg=color, bold=True))


# ---- the big monkeys ----------------------------------------------------------------------------

def _head(eyes: str, mouth: str, top: str = "", ears: tuple[str, str] = ("_/", "\\_"), under: str = "`---`") -> list[str]:
    """A monkey head with the given eyes (3 chars) and mouth (5 chars), every row 17 columns wide."""
    rows = [top,
            "     .-\"-.",
            f"   {ears[0]}.-.-.{ears[1]}",
            f"  ( ( {eyes} ) )",
            "   |/  \"  \\|",
            f"    \\{mouth}/",
            f"     {under}"]
    return [f"{r:<17}" for r in rows]


# The face the monkey makes at each outcome. The three wise monkeys cover the quiet failures: see no evil when
# state vanishes, speak no evil when the app says nothing, hear no evil when the attack was skipped.
FACES = {
    "survived":    _head("^ ^", " \\_/ ", top="     \\o/"),
    "fixed":       _head("^ ^", " \\_/ ", top="  *  \\o/  *"),
    "crashed":     _head("x x", "  O  ", top="   * BANG *"),
    "unfixed":     _head("x x", " /^\\ ", top="   * BANG *"),
    "lost_state":  _head("###", " .-. ", top="  see no evil"),
    "no_feedback": _head("o o", "(###)", top=" speak no evil"),
    "skipped":     _head("- -", " --- ", top="  hear no evil", ears=("#|", "|#")),
    "leaked":      _head("T T", " ~~~ ", top="   drip  drip"),
    "froze":       _head("* *", " === ", top="    brrrrr", under="v-v-v"),
    "anr":         _head("@ @", " --- ", top=" [ waiting... ]", under="`---` ;"),
    "slow_start":  _head("- -", " ___ ", top="         zZz"),
    "declined":    _head("- -", " --- ", top="  maybe later"),
    "unreachable": _head("? ?", " .-. ", top="     ?   ?"),
    "worried":     _head("o O", " ~~~ "),
}


def _visible(text: str) -> int:
    return len(click.unstyle(text))


def monkey_says(outcome: str, title: str, lines: list[str], color: str | None = None) -> None:
    """The monkey for this outcome, saying `title` and `lines` in a speech bubble."""
    import textwrap
    art = FACES.get(outcome, FACES["worried"])
    color = color or BADGES.get(outcome, ("", "white"))[1]
    inner = max(20, min(width() - 24, max([_visible(title)] + [_visible(l) for l in lines]) + 2))
    if _visible(title) > inner:  # a long plain title wraps instead of breaking the bubble
        head, *more = textwrap.wrap(title, inner) if _visible(title) == len(title) else [title]
        title, lines = head, more + list(lines)
    text = [style(title, fg=color, bold=True)]
    for l in lines:
        for w in (textwrap.wrap(l, inner - 2, subsequent_indent="  ") if _visible(l) == len(l) else [l]) or [""]:
            text.append(w)
    pad = lambda t: t + " " * (inner - _visible(t))
    bubble = [style(" .-" + "-" * inner + "-.", fg="bright_black")]
    for n, t in enumerate(text):
        edge = "<  " if n == 0 else " | "
        bubble.append(style(edge, fg="bright_black") + pad(t) + style(" |", fg="bright_black"))
    bubble.append(style(" '-" + "-" * inner + "-'", fg="bright_black"))
    start = 2  # the bubble's tail points at the monkey's eyes
    rows = max(len(art), start + len(bubble))
    click.echo("")
    for i in range(rows):
        left = art[i] if i < len(art) else " " * 17
        right = bubble[i - start] if 0 <= i - start < len(bubble) else ""
        click.echo("  " + style(left, fg=color, bold=True) + right)


def score_monkey(score: int, title: str, lines: list[str]) -> None:
    face_for = "fixed" if score >= 90 else "survived" if score >= 70 else "worried" if score >= 40 else "crashed"
    color = "green" if score >= 80 else "yellow" if score >= 50 else "red"
    monkey_says(face_for, title, lines, color=color)


def verdict_line(score: int) -> str:
    if score >= 90:
        return "Top banana. The monkey couldn't shake it."
    if score >= 70:
        return "Sturdy tree, a few loose branches."
    if score >= 40:
        return "The monkey found some ripe ones."
    return "Total jungle. The monkey had a field day."


# ---- the run loop's log lines --------------------------------------------------------------------

STAGES = {
    "setup":    ("~", "cyan"),
    "plan":     ("#", "cyan"),
    "decide":   ("?", "bright_black"),
    "diagnose": ("o", "blue"),
    "approve":  ("!", "magenta"),
    "declined": ("-", "yellow"),
    "fix":      ("+", "cyan"),
    "verify":   ("%", "blue"),
    "commit":   ("*", "green"),
    "give up":  ("x", "red"),
    "report":   ("=", "green"),
    "watchdog": ("!", "red"),
    "resume":   (">", "cyan"),
    "back on":  ("<", "green"),
}


def loop_echo(line: str) -> None:
    """Pretty-print one line from the run loop. The loop logs plain `stage: message` text; this adds the jungle."""
    body = line.lstrip("\n")
    if body.startswith("== attack "):
        header(body.removeprefix("== ").strip(), "(>.<)")
        return
    if body != line:
        click.echo("")
    m = re.match(r"attack (\S+): (\w+) ?(.*)", body, flags=re.S)
    if m:
        name, outcome, detail = m.groups()
        label = BADGES.get(outcome, ("", "", outcome))[2].upper()
        d = re.match(r"\((.*?)\)(?: at (.*))?$", detail, flags=re.S)
        lines = [d.group(1)] + ([f"at {d.group(2)}"] if d.group(2) else []) if d else ([detail] if detail else [])
        monkey_says(outcome, f"{label}  {name}", lines)
        return
    for stage, (mark, color) in STAGES.items():
        if body.startswith(stage + ":") or body.startswith(stage + " "):
            first, *rest = body.split("\n")
            tag, _, msg = first.partition(":") if first.startswith(stage + ":") else (stage, "", first[len(stage):])
            msg = msg.strip()
            msg_style = {}
            if "FIXED" in msg or stage == "commit":
                msg_style = {"fg": "green", "bold": True}
            elif "not fixed" in msg or "does not build" in msg or stage in ("give up", "watchdog"):
                msg_style = {"fg": "red"}
            click.echo(f"  {style(f'{mark:^5}', fg=color, bold=True)} " + style(f"{tag:<12}", fg=color, bold=True) + " "
                       + (style(msg, **msg_style) if msg_style else msg))
            for r in rest:
                click.echo(style("    |   ", fg="bright_black") + r.removeprefix("  "))
            return
    click.echo(f"  {body}")


# ---- spinner -------------------------------------------------------------------------------------

# A small monkey looking around (and blinking) while a banana bounces above it.
EYES = ["o o", "o o", "o o", "- -", "o o", "oo ", "oo ", "o o", " oo", " oo"]
BANANA = ["   )", "    )", "     )", "      )", "     )", "    )"]


def _spin_frame(n: int, message: str, elapsed: float) -> list[str]:
    eyes = EYES[n % len(EYES)]
    return [style(f"{BANANA[n % len(BANANA)]:<12}", fg="yellow", bold=True),
            style("   .-\"-.    ", fg="yellow"),
            style(f" _/ {eyes} \\_  ", fg="yellow", bold=True) + style(message, fg="cyan"),
            style("( (  \"  ) ) ", fg="yellow") + style(f"{elapsed:.0f}s", fg="bright_black"),
            style("   \\ - /    ", fg="yellow")]


@contextmanager
def working(message: str):
    """An animated monkey while an agent works, with elapsed seconds. One plain line when not a terminal."""
    if not sys.stdout.isatty():
        click.echo(f"  .. {message}")
        yield
        return
    stop = threading.Event()
    start = time.time()
    height = len(_spin_frame(0, "", 0))

    def draw(n: int, first: bool = False):
        out = "" if first else f"\033[{height}F"
        out += "".join(f"\r  {line}\033[K\n" for line in _spin_frame(n, message, time.time() - start))
        sys.stdout.write(out)
        sys.stdout.flush()

    def spin():
        n = 1
        while not stop.wait(0.25):
            draw(n)
            n += 1

    draw(0, first=True)
    t = threading.Thread(target=spin, daemon=True)
    t.start()
    try:
        yield
    finally:
        stop.set()
        t.join()
        sys.stdout.write(f"\033[{height}F\033[J")
        sys.stdout.flush()
