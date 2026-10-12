"""The screens in the code, and how each is opened: the explorer's checklist. Static, no model, no device.

droid-monkey has the whole repo, so the explorer should not have to discover screens by trial and error. This pass
lists every Activity in the manifest (with its deep links and whether it launches the app), every Fragment and
dialog, every Compose screen (functions named *Screen/*Page, and whatever composable a navigation route shows),
every Navigation-Compose route and Navigation-graph XML destination. It finds the code that opens each one
(`XActivity.newIntent(...)`, `XFragment.newInstance(...)`, `navigate("route")`, `navigate(R.id.action_x)`,
`XScreen(...)`) and resolves the labels used next to that code (string resources, menu and view titles, preference
titles, or text written directly in code) into what the user sees on screen (e.g. Settings -> "Customize toolbar"). The explorer then works from this
list, shortest route first, and knows when it is done.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .profile import ANDROID, find_manifest

_SKIP = {"build", ".gradle", "test", "androidTest", "testDebug", ".idea", "generated", "node_modules", ".git"}
_CLASS_HEAD = re.compile(r"^[ \t]*((?:(?:public|internal|private|protected|open|abstract|sealed|data|final|static)\s+)*)"
                         r"class\s+(\w+)", re.M)
_SUPER = re.compile(r"\s*(?::|extends)\s*([\w.]+)")
_ROUTE = re.compile(r"\bcomposable\s*(?:<\s*([\w.]+)\s*>|\(\s*(?:route\s*=\s*)?(?:\"([^\"$/{]+)|([\w.]+)\s*[,)]))")
_COMPOSABLE_FUN = re.compile(r"@Composable\s+(?:@\w+(?:\([^)]*\))?\s+)*(?:(?:internal|public|private)\s+)?fun\s+(\w+)\s*\(")
_SCREEN_NAME = re.compile(r"(Screen|Page)$")  # composables named like screens are screens wherever they are used
_CALL = re.compile(r"\b([A-Z]\w+)\s*\(")
# Layout and chrome composables that wrap a destination's content rather than being the screen.
_WRAPPER = re.compile(r"Drawer|Scaffold|Theme|Surface|Box$|Column$|Row$|Layout|Wrapper|Container|Provider|Effect")
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


def _skip_balanced(text: str, i: int, open_: str, close: str) -> int:
    """Index just past the bracket group starting at text[i] (which must be `open_`)."""
    depth = 0
    for j in range(i, min(len(text), i + 4000)):
        if text[j] == open_:
            depth += 1
        elif text[j] == close:
            depth -= 1
            if depth == 0:
                return j + 1
    return i


def classes(text: str):
    """(name, first supertype, is_abstract) for each class, in Kotlin or Java, with headers over several lines:
    `class A(\n  val x: Int,\n) : Fragment()`. Generic and constructor parameter lists are skipped, so a
    parameter's type is never taken for the superclass."""
    for m in _CLASS_HEAD.finditer(text):
        i = m.end()
        while i < len(text) and text[i] in " \t\n":
            i += 1
        if i < len(text) and text[i] == "<":
            i = _skip_balanced(text, i, "<", ">")
        j = i
        while j < len(text) and text[j] in " \t\n":
            j += 1
        if text.startswith("constructor", j) or text.startswith("@", j):  # `class A @Inject constructor(...)`
            k = text.find("(", j)
            j = k if k != -1 else j
        if j < len(text) and text[j] == "(":
            i = _skip_balanced(text, j, "(", ")")
        sup = _SUPER.match(text, i)
        if sup:
            yield m.group(2), sup.group(1).rsplit(".", 1)[-1], "abstract" in m.group(1)


def _block(text: str, i: int) -> str:
    """The `{ ... }` block that starts at or after text[i] (e.g. a nav destination's content lambda)."""
    j = text.find("{", i)
    return text[j:_skip_balanced(text, j, "{", "}")] if j != -1 else ""


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


_REF = re.compile(r"R\.string\.(\w+)|R\.id\.(\w+)|binding\.(\w+)"
                  r"|\b(?:Text|text|title|label|contentDescription|setTitle|setText)\s*[=(]\s*\"([^\"$\\\\]{1,40})\"")


def _labels(lines: list[str], i: int, strings: dict[str, str], ids: dict[str, str], prefs: dict[str, str]) -> list[str]:
    """The user-visible labels referenced right around the code that opens a screen: closest lines first."""
    for above in (4, CALLER_WINDOW):
        found = []
        for m in _REF.finditer("\n".join(lines[max(0, i - above): i + 2])):
            s_ref, id_ref, binding, literal = m.groups()
            if literal:
                t = literal
            elif s_ref:
                t = prefs.get(s_ref) or strings.get(s_ref)
            else:
                t = ids.get(id_ref or binding, "")
            if t and len(t) <= 40 and "%" not in t and t not in found:
                found.append(t)
        if found:
            return found[-3:]
    return []


def _camel(rid: str) -> str:
    return re.sub(r"_(\w)", lambda m: m.group(1).upper(), rid)


def _nav_graphs(app_dir: Path, strings: dict[str, str]) -> list[dict]:
    """Destinations in Navigation-graph XML (res/navigation/*.xml): the class, its kind, its label, and the tokens
    code uses to go there: `R.id.<action or destination>` or Safe Args' `<Directions>.<actionCamel>(`."""
    found: dict[str, dict] = {}
    for f in app_dir.rglob("res/navigation*/*.xml"):
        if _SKIP & set(f.relative_to(app_dir).parts):
            continue
        try:
            root = ET.parse(f).getroot()
        except ET.ParseError:
            continue
        rel, raw = str(f.relative_to(app_dir)), f.read_text(errors="ignore")
        by_id = {}
        for el in root.iter():
            tag = el.tag.rsplit("}", 1)[-1]
            a = {k.rsplit("}", 1)[-1]: v for k, v in el.attrib.items()}
            if tag in ("fragment", "dialog", "activity") and a.get("name") and a.get("id"):
                rid = a["id"].split("/", 1)[1]
                label = a.get("label", "")
                label = strings.get(label.split("/", 1)[1], "") if label.startswith("@string/") else label
                name = a["name"].rsplit(".", 1)[-1]
                pos = raw.find(a["id"])
                d = found.setdefault(name, {"name": name, "kind": {"dialog": "dialog"}.get(tag, tag), "file": rel,
                                            "line": raw.count("\n", 0, max(pos, 0)) + 1, "label": label,
                                            "tokens": set()})
                d["tokens"].add(f"R.id.{rid}")
                by_id[rid] = d
        for el in root.iter():
            a = {k.rsplit("}", 1)[-1]: v for k, v in el.attrib.items()}
            if el.tag.rsplit("}", 1)[-1] == "action" and a.get("id") and a.get("destination"):
                d = by_id.get(a["destination"].split("/", 1)[1])
                if d:
                    aid = a["id"].split("/", 1)[1]
                    d["tokens"] |= {f"R.id.{aid}", f".{_camel(aid)}("}
    return list(found.values())


def build(app_dir: Path, package: str, module: str = "app", variant: str = "debug") -> list[ScreenEntry]:
    app_dir = app_dir.resolve()
    files = _sources(app_dir)
    texts = {f: f.read_text(errors="ignore") for f in files}
    strings = _strings(app_dir)
    ids, prefs = _res_labels(app_dir, strings)
    defined: dict[str, Path] = {}
    entries: dict[str, ScreenEntry] = {}
    composables: dict[str, Path] = {}
    for f, text in texts.items():
        for name, parent, abstract in classes(text):
            defined.setdefault(name, f)
            if abstract or name.startswith("Base"):
                continue
            if parent.endswith("DialogFragment"):
                entries.setdefault(name, ScreenEntry(name=name, kind="dialog"))
            elif parent.endswith("Fragment") and not parent.endswith("PreferenceFragment"):
                entries.setdefault(name, ScreenEntry(name=name, kind="fragment"))
        if "@Composable" in text:
            for m in _COMPOSABLE_FUN.finditer(text):
                composables.setdefault(m.group(1), f)
    for name, f in composables.items():
        if _SCREEN_NAME.search(name) and not name.endswith("Preview"):
            entries.setdefault(name, ScreenEntry(name=name, kind="screen", file=str(f.relative_to(app_dir))))
    # Navigation-Compose destinations: the route, and the composable its content shows, whatever it is called.
    for f, text in texts.items():
        if "composable" not in text:
            continue
        rel = str(f.relative_to(app_dir))
        for m in _ROUTE.finditer(text):
            route = (m.group(1) or m.group(2) or m.group(3)).rsplit(".", 1)[-1]
            entries.setdefault(route, ScreenEntry(name=route, kind="route", file=rel))
            calls = [c for c in _CALL.findall(_block(text, m.end())) if c in composables and not _WRAPPER.search(c)]
            shown = next((c for c in calls if _SCREEN_NAME.search(c)), calls[0] if calls else None)
            if shown:
                e = entries.setdefault(shown, ScreenEntry(name=shown, kind="screen",
                                                          file=str(composables[shown].relative_to(app_dir))))
                if e.kind == "screen" and not e.note:
                    e.note = f"route {route}"
    # Navigation-graph XML destinations.
    nav_tokens: dict[str, set[str]] = {}
    for d in _nav_graphs(app_dir, strings):
        e = entries.setdefault(d["name"], ScreenEntry(name=d["name"], kind=d["kind"]))
        e.opened_from.append({"file": d["file"], "line": d["line"], "labels": [d["label"]] if d["label"] else []})
        nav_tokens[d["name"]] = d["tokens"]
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
    def caller(e: ScreenEntry) -> re.Pattern:
        if e.kind == "screen":
            pat = rf"(?<!fun )\b{re.escape(e.name)}\s*\("  # a call, not the definition
        elif e.kind == "route":
            pat = rf"navigate\(\s*(?:\w+\.)?\"?{re.escape(e.name)}\b"
        else:
            pat = rf"\b{re.escape(e.name)}(?:\.(?:newIntent|newInstance|start|show|getIntent)\b|::class|\.class\b|\(\))"
        extra = [re.escape(t) + (r"\b" if t[-1].isalnum() else "") for t in sorted(nav_tokens.get(e.name, ()))]
        return re.compile("|".join([pat] + extra))
    callers = {e.name: (caller(e), [e.name] + [t.lstrip(".").rstrip("(") for t in nav_tokens.get(e.name, ())])
               for e in entries.values()}
    for f, text in texts.items():
        rel = str(f.relative_to(app_dir))
        lines = None
        for name, (pat, needles) in callers.items():
            e = entries[name]
            if len(e.opened_from) >= 3 or not any(n in text for n in needles):
                continue
            for m in pat.finditer(text):
                if rel == e.file and e.kind in ("activity", "fragment", "dialog"):
                    break  # its own file: a companion newIntent, not a way in
                lines = lines or text.splitlines()
                line = text.count("\n", 0, m.start())
                e.opened_from.append({"file": rel, "line": line + 1, "labels": _labels(lines, line, strings, ids, prefs)})
                break
    # A label next to the code for many screens is chrome (an app bar title), not what opens any one of them.
    seen: dict[str, set[str]] = {}
    for e in entries.values():
        for o in e.opened_from:
            for label in o["labels"]:
                seen.setdefault(label, set()).add(e.name)
    for e in entries.values():
        for o in e.opened_from:
            o["labels"] = [label for label in o["labels"] if len(seen[label]) <= 2]
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
