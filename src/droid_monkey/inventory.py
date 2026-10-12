"""The screens in the code, and how each is opened: the explorer's checklist. Static, no model, no device.

droid-monkey has the whole repo, so the explorer should not have to discover screens by trial and error. This pass
lists every Activity in the manifest (with its deep links and whether it launches the app), every Fragment and
every Compose navigation route, finds the code that opens each one (`XActivity.newIntent(...)`,
`XFragment.newInstance(...)`, `navigate("route")`), and resolves the string resources used next to that code
into the labels the user sees on screen (e.g. Settings -> "Customize toolbar"). The explorer then works from this
list, shortest route first, and knows when it is done.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .profile import ANDROID, find_manifest

_SKIP = {"build", ".gradle", "test", "androidTest", "testDebug", ".idea", "generated", "node_modules", ".git"}
_CLASS = re.compile(r"^\s*(?:(?:public|internal|private|open|abstract|sealed|data|final)\s+)*class\s+(\w+)"
                    r"(?:<[^>]*>)?[^{\n]*?(?::|extends)\s*([\w.]+)", re.M)
_ROUTE = re.compile(r"\bcomposable\s*(?:<\s*([\w.]+)\s*>|\(\s*(?:route\s*=\s*)?(?:\"([^\"$/{]+)|([\w.]+)\s*[,)]))")
_COMPOSE_SCREEN = re.compile(r"@Composable\s+(?:(?:internal|public)\s+)?fun\s+(\w+Screen)\s*\(")
CALLER_WINDOW = 12  # lines around the opening code searched for user-visible labels


@dataclass
class ScreenEntry:
    name: str                     # class name or route
    kind: str                     # activity | screen (a Compose *Screen function) | route | fragment | dialog
    file: str = ""                # where it is defined, relative to the repo
    launcher: bool = False
    exported: bool = False
    deep_links: list[str] = field(default_factory=list)
    opened_from: list[dict] = field(default_factory=list)  # {file, line, labels}
    note: str = ""                # debug-only, library, ...


def _sources(app_dir: Path) -> list[Path]:
    out = []
    for f in app_dir.rglob("*"):
        if f.suffix in (".kt", ".java") and not (_SKIP & set(f.relative_to(app_dir).parts)):
            out.append(f)
    return out


def _strings(app_dir: Path) -> dict[str, str]:
    """Default-locale string resources: name -> text."""
    out: dict[str, str] = {}
    for f in app_dir.rglob("values/strings*.xml"):
        if _SKIP & set(f.relative_to(app_dir).parts):
            continue
        try:
            for el in ET.parse(f).getroot().iter("string"):
                if el.get("name") and el.get("name") not in out:
                    out[el.get("name")] = "".join(el.itertext()).strip().strip('"')
        except ET.ParseError:
            continue
    return out


def _deep_links(activity: ET.Element) -> list[str]:
    links = []
    for f in activity.findall("intent-filter"):
        if not any(c.get(ANDROID + "name", "").endswith("BROWSABLE") for c in f.findall("category")):
            continue
        data = f.findall("data")
        schemes = [d.get(ANDROID + "scheme") for d in data if d.get(ANDROID + "scheme")] or ["https"]
        hosts = [d.get(ANDROID + "host") for d in data if d.get(ANDROID + "host")] or [""]
        paths = [d.get(ANDROID + "pathPrefix") or d.get(ANDROID + "path") or d.get(ANDROID + "pathPattern")
                 for d in data if any(d.get(ANDROID + k) for k in ("pathPrefix", "path", "pathPattern"))] or [""]
        links += [f"{schemes[-1]}://{h}{p}" for h in hosts[:2] for p in paths[:2]]
    return sorted(set(links))[:4]


def _manifest_activities(app_dir: Path, module: str, variant: str, package: str) -> list[ScreenEntry]:
    path = find_manifest(app_dir, module, variant)
    if not path.exists():
        return []
    app = ET.parse(path).getroot().find("application")
    out = []
    for act in (app.findall("activity") + app.findall("activity-alias")) if app is not None else []:
        name = act.get(ANDROID + "name", "")
        if not name or act.tag == "activity-alias":
            continue
        name = package + name if name.startswith(".") else name
        launcher = any(c.get(ANDROID + "name", "").endswith("LAUNCHER")
                       for f in act.findall("intent-filter") for c in f.findall("category"))
        out.append(ScreenEntry(name=name.rsplit(".", 1)[-1], kind="activity", launcher=launcher,
                               exported=act.get(ANDROID + "exported") == "true", deep_links=_deep_links(act),
                               note=name))  # full name, replaced by a note once resolved
    return out


def _res_labels(app_dir: Path, strings: dict[str, str]) -> tuple[dict[str, str], dict[str, str]]:
    """Labels the code refers to indirectly. Views and menu items: id (and its view-binding name) -> its text or
    title. Preferences: the key's string name -> the preference's title."""
    def text(v: str | None) -> str:
        if not v:
            return ""
        return strings.get(v.split("/", 1)[1], "") if v.startswith("@string/") else ("" if v.startswith("@") else v)
    ids, prefs = {}, {}
    for f in app_dir.rglob("res/*/*.xml"):
        if _SKIP & set(f.relative_to(app_dir).parts) or f.parent.name.split("-")[0] not in ("layout", "menu", "xml"):
            continue
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        for el in root.iter():
            a = {k.rsplit("}", 1)[-1]: v for k, v in el.attrib.items()}
            label = text(a.get("title")) or text(a.get("text")) or text(a.get("contentDescription"))
            if not label:
                continue
            if a.get("id", "").startswith("@+id/") or a.get("id", "").startswith("@id/"):
                rid = a["id"].split("/", 1)[1]
                ids.setdefault(rid, label)
                ids.setdefault(re.sub(r"_(\w)", lambda m: m.group(1).upper(), rid), label)  # view binding name
            if a.get("key", "").startswith("@string/"):
                prefs.setdefault(a["key"].split("/", 1)[1], label)
    return ids, prefs


_REF = re.compile(r"R\.string\.(\w+)|R\.id\.(\w+)|binding\.(\w+)")


def _labels(lines: list[str], i: int, strings: dict[str, str], ids: dict[str, str], prefs: dict[str, str]) -> list[str]:
    """The user-visible labels referenced right around the code that opens a screen: closest lines first."""
    for above in (4, CALLER_WINDOW):
        found = []
        for m in _REF.finditer("\n".join(lines[max(0, i - above): i + 2])):
            s_ref, id_ref, binding = m.groups()
            t = prefs.get(s_ref) or strings.get(s_ref) if s_ref else ids.get(id_ref or binding, "")
            if t and len(t) <= 40 and "%" not in t and t not in found:
                found.append(t)
        if found:
            return found[-3:]
    return []


def build(app_dir: Path, package: str, module: str = "app", variant: str = "debug") -> list[ScreenEntry]:
    app_dir = app_dir.resolve()
    files = _sources(app_dir)
    texts = {f: f.read_text(errors="ignore") for f in files}
    strings = _strings(app_dir)
    ids, prefs = _res_labels(app_dir, strings)
    defined: dict[str, Path] = {}
    entries: dict[str, ScreenEntry] = {}
    for f, text in texts.items():
        for m in _CLASS.finditer(text):
            name, parent = m.group(1), m.group(2).rsplit(".", 1)[-1]
            defined.setdefault(name, f)
            if "abstract class" in m.group(0) or name.startswith("Base"):
                continue
            if parent.endswith("DialogFragment") or parent.endswith("BottomSheetDialogFragment"):
                entries.setdefault(name, ScreenEntry(name=name, kind="dialog"))
            elif parent.endswith("Fragment") and not parent.endswith("PreferenceFragment"):
                entries.setdefault(name, ScreenEntry(name=name, kind="fragment"))
        for m in _COMPOSE_SCREEN.finditer(text):
            entries.setdefault(m.group(1), ScreenEntry(name=m.group(1), kind="screen", file=str(f.relative_to(app_dir))))
        for m in _ROUTE.finditer(text):
            route = (m.group(1) or m.group(2) or m.group(3)).rsplit(".", 1)[-1]
            entries.setdefault(route, ScreenEntry(name=route, kind="route", file=str(f.relative_to(app_dir))))
    for act in _manifest_activities(app_dir, module, variant, package):
        full, act.note = act.note, ""
        entries[act.name] = act
        if act.name not in defined:
            act.note = "library" if not full.startswith(package.rsplit(".", 1)[0]) else "not in this source set"
    for e in entries.values():
        if e.name in defined and not e.file:
            e.file = str(defined[e.name].relative_to(app_dir))
        if e.file and ("/src/debug/" in e.file or re.search(r"Developer|Playground|Debug", e.name)):
            e.note = "debug-only"
    # Who opens each screen, and the labels next to that code.
    callers = {e.name: re.compile(
        rf"\b{re.escape(e.name)}(?:\.(?:newIntent|newInstance|start|show|getIntent)\b|::class|\.class\b|\(\))"
        if e.kind not in ("route", "screen") else
        rf"\b{re.escape(e.name)}\(" if e.kind == "screen" else rf"navigate\(\s*(?:\w+\.)?\"?{re.escape(e.name)}\b")
        for e in entries.values()}
    for f, text in texts.items():
        rel = str(f.relative_to(app_dir))
        lines = None
        for name, pat in callers.items():
            e = entries[name]
            if len(e.opened_from) >= 3 or name not in text:
                continue
            for m in pat.finditer(text):
                if rel == e.file:
                    break  # its own file: a companion newIntent, not a way in
                lines = lines or text.splitlines()
                line = text.count("\n", 0, m.start())
                e.opened_from.append({"file": rel, "line": line + 1, "labels": _labels(lines, line, strings, ids, prefs)})
                break
    keep = [e for e in entries.values() if e.kind in ("activity", "screen", "route") or e.opened_from]
    return sorted(keep, key=lambda e: ({"activity": 0, "screen": 1, "route": 2, "fragment": 3, "dialog": 4}[e.kind], e.name))


def save(entries: list[ScreenEntry], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(e) for e in entries], indent=2))


def load(path: Path) -> list[ScreenEntry]:
    return [ScreenEntry(**d) for d in json.loads(path.read_text())] if path.exists() else []


def brief(entries: list[ScreenEntry], limit: int = 160) -> str:
    """One line per screen for the explorer's prompt: what it is, how it is opened, and the labels to look for."""
    lines = []
    for e in entries[:limit]:
        bits = [f"{e.name} [{e.kind}]"]
        if e.launcher:
            bits.append("LAUNCHER")
        if e.note:
            bits.append(f"({e.note})")
        if e.file:
            bits.append(e.file)
        for o in e.opened_from[:2]:
            label = f" near labels {o['labels']}" if o["labels"] else ""
            bits.append(f"opened from {o['file'].rsplit('/', 1)[-1]}:{o['line']}{label}")
        if e.deep_links:
            bits.append("deep links " + ", ".join(e.deep_links[:2]))
        lines.append("- " + "; ".join(bits))
    if len(entries) > limit:
        lines.append(f"- ... and {len(entries) - limit} more fragments/dialogs (see screens.json)")
    return "\n".join(lines)
