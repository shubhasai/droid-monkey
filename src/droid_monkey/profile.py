"""Static analysis of an Android repo: manifest, dependencies and generic risk patterns. No model, no device.

The profile tells the planner what kinds of faults this app is exposed to and where in the code to look.
Every risk signal is a generic code pattern (not a known bug), reported with file:line.
"""

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

ANDROID = "{http://schemas.android.com/apk/res/android}"

DANGEROUS_PERMISSIONS = {
    "CAMERA", "RECORD_AUDIO", "ACCESS_FINE_LOCATION", "ACCESS_COARSE_LOCATION", "ACCESS_BACKGROUND_LOCATION",
    "READ_CONTACTS", "WRITE_CONTACTS", "READ_CALENDAR", "WRITE_CALENDAR", "READ_PHONE_STATE", "CALL_PHONE",
    "READ_EXTERNAL_STORAGE", "WRITE_EXTERNAL_STORAGE", "READ_MEDIA_IMAGES", "READ_MEDIA_VIDEO", "READ_MEDIA_AUDIO",
    "POST_NOTIFICATIONS", "BODY_SENSORS", "ACTIVITY_RECOGNITION", "BLUETOOTH_CONNECT", "BLUETOOTH_SCAN",
    "NEARBY_WIFI_DEVICES", "SEND_SMS", "READ_SMS",
}

# Dependency keyword -> capability. Matched against Gradle files and the version catalog.
CAPABILITIES = {
    "network": ["okhttp", "retrofit", "ktor", "volley", "apollo", "grpc", "firebase-firestore", "firebase-database"],
    "database": ["room", "sqldelight", "realm", "objectbox"],
    "camera": ["camerax", "camera-camera2", "camera-core"],
    "location": ["play-services-location", "location"],
    "images": ["coil", "glide", "picasso", "fresco"],
    "background_work": ["work-runtime", "workmanager"],
    "fragments": ["fragment"],
    "compose": ["compose"],
    "viewmodel": ["viewmodel"],
    "datastore": ["datastore"],
}


@dataclass
class Signal:
    kind: str
    file: str
    line: int
    snippet: str
    why: str
    attacks: list[str]


@dataclass
class Activity:
    name: str
    exported: bool
    config_changes: list[str]
    screen_orientation: str


@dataclass
class AppProfile:
    package: str
    target_sdk: int | None
    min_sdk: int | None
    permissions: list[str]
    dangerous_permissions: list[str]
    activities: list[Activity]
    services: list[str]
    receivers: list[str]
    capabilities: list[str]
    ui_toolkit: str
    source_files: int
    signals: list[Signal] = field(default_factory=list)
    applicable_attacks: dict[str, str] = field(default_factory=dict)  # attack -> why it applies

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


# ---- manifest -----------------------------------------------------------------------------------

def find_manifest(app_dir: Path, module: str, variant: str) -> Path:
    """Prefer the merged manifest (includes library permissions), fall back to the source manifest."""
    build = app_dir / module / "build" / "intermediates"
    merged = sorted(build.glob(f"merged_manifest*/{variant}/**/AndroidManifest.xml"))
    if merged:
        return merged[0]
    return app_dir / module / "src" / "main" / "AndroidManifest.xml"


def _a(el: ET.Element, name: str, default: str = "") -> str:
    return el.get(ANDROID + name, default)


def parse_manifest(path: Path, package_hint: str) -> dict:
    root = ET.parse(path).getroot()
    package = root.get("package") or package_hint
    uses_sdk = root.find("uses-sdk")
    perms = sorted({_a(p, "name") for p in root.findall("uses-permission")} - {""})
    app = root.find("application")
    activities, services, receivers = [], [], []
    if app is not None:
        for act in app.findall("activity"):
            name = _a(act, "name")
            activities.append(Activity(
                name=name if not name.startswith(".") else package + name,
                exported=_a(act, "exported") == "true",
                config_changes=[c for c in _a(act, "configChanges").split("|") if c],
                screen_orientation=_a(act, "screenOrientation"),
            ))
        services = [_a(s, "name") for s in app.findall("service")]
        receivers = [_a(r, "name") for r in app.findall("receiver")]
    return dict(
        package=package,
        target_sdk=int(_a(uses_sdk, "targetSdkVersion")) if uses_sdk is not None and _a(uses_sdk, "targetSdkVersion") else None,
        min_sdk=int(_a(uses_sdk, "minSdkVersion")) if uses_sdk is not None and _a(uses_sdk, "minSdkVersion") else None,
        permissions=perms,
        dangerous_permissions=[p for p in perms if p.rsplit(".", 1)[-1] in DANGEROUS_PERMISSIONS],
        activities=activities,
        services=services,
        receivers=receivers,
    )


# ---- dependencies -------------------------------------------------------------------------------

def capabilities(app_dir: Path, module: str) -> list[str]:
    text = ""
    for f in [app_dir / module / "build.gradle.kts", app_dir / module / "build.gradle", app_dir / "gradle" / "libs.versions.toml"]:
        if f.exists():
            text += f.read_text().lower()
    return sorted(cap for cap, words in CAPABILITIES.items() if any(w in text for w in words))


# ---- source risk signals ------------------------------------------------------------------------

_SKIP_DIRS = {"build", ".gradle", "test", "androidTest", ".idea", "generated"}


def source_files(app_dir: Path, module: str) -> list[Path]:
    src = app_dir / module / "src"
    files = []
    for f in src.rglob("*"):
        if f.suffix in (".kt", ".java") and not (_SKIP_DIRS & set(f.relative_to(src).parts)):
            files.append(f)
    return sorted(files)


def _line_of(text: str, index: int) -> int:
    return text.count("\n", 0, index) + 1


def scan(path: Path, rel: str) -> list[Signal]:
    text = path.read_text(errors="replace")
    lines = text.splitlines()
    out: list[Signal] = []

    def add(kind: str, index: int, why: str, attacks: list[str]) -> None:
        ln = _line_of(text, index)
        out.append(Signal(kind, rel, ln, lines[ln - 1].strip()[:140], why, attacks))

    for m in re.finditer(r"\bGlobalScope\.(launch|async)", text):
        add("unscoped_coroutine", m.start(), "work is not tied to a lifecycle; it can call back into a destroyed screen",
            ["rotation", "process_death"])

    for m in re.finditer(r"class\s+(\w+)\s*(\([^)]*\))?\s*:\s*\w*ViewModel\(", text):
        if "SavedStateHandle" not in text and "mutableStateOf" in text:
            add("viewmodel_state_not_saved", m.start(),
                f"{m.group(1)} keeps UI state in memory without SavedStateHandle; it is lost when the process dies",
                ["process_death"])

    if re.search(r"\b(OutlinedTextField|TextField|BasicTextField)\s*\(", text) and \
            re.search(r"\bremember\s*\{\s*mutableStateOf", text) and "rememberSaveable" not in text:
        m = re.search(r"\bremember\s*\{\s*mutableStateOf", text)
        add("input_state_not_saveable", m.start(), "input state held in remember{} is lost on recreation",
            ["rotation", "process_death"])

    for m in re.finditer(r"\bobject\s+\w+", text):
        body = text[m.end():]
        fm = re.search(r"fun\s+\w+\([^)]*\b\w+\s*:\s*(Activity|FragmentActivity|AppCompatActivity|ComponentActivity)\b", body)
        if fm:
            add("activity_passed_to_singleton", m.end() + fm.start(),
                "a singleton function takes an Activity; async work can outlive it", ["rotation"])

    for m in re.finditer(r"\.show\(\s*\w+\.(supportFragmentManager|fragmentManager)", text):
        add("fragment_transaction_from_callback", m.start(),
            "a dialog/fragment is shown through a stored FragmentManager; it throws once that Activity is destroyed",
            ["rotation"])

    for m in re.finditer(r"\b(viewModelScope|lifecycleScope)\.launch\s*(\([^)]*\))?\s*\{", text):
        block = text[m.end(): m.end() + 600]
        depth, end = 1, 0
        for i, ch in enumerate(block):
            depth += ch == "{"
            depth -= ch == "}"
            if depth == 0:
                end = i
                break
        body = block[:end or len(block)]
        if not re.search(r"\btry\b|runCatching|catch\s*\(|CoroutineExceptionHandler|onFailure", body):
            add("coroutine_without_error_handling", m.start(),
                "a coroutine does work with no error handling; a thrown IOException crashes the app",
                ["network_loss"])

    for m in re.finditer(r"throw\s+(java\.io\.)?IOException", text):
        add("throws_io_exception", m.start(), "code throws IOException; callers must handle it", ["network_loss"])

    uses_guarded_api = re.search(r"\.openCamera\(|requestLocationUpdates|getCurrentLocation|MediaRecorder\(|AudioRecord\(", text)
    if uses_guarded_api and not re.search(r"checkSelfPermission|PermissionChecker|shouldShowRequestPermissionRationale", text):
        add("permission_not_checked_at_use", uses_guarded_api.start(),
            "a permission-protected API is called without checking the permission at use time", ["permission_revoke"])

    for m in re.finditer(r"put(Boolean|String)\(\s*\"?\w*(grant|permission)\w*\"?", text, flags=re.I):
        add("cached_permission_state", m.start(), "permission state is cached; it goes stale when the user revokes it",
            ["permission_revoke"])

    for m in re.finditer(r"\b(runBlocking|Thread\.sleep)\b", text):
        add("blocking_call", m.start(), "a blocking call may run on the main thread (ANR risk)", ["anr"])

    return out


# ---- the profile --------------------------------------------------------------------------------

def analyze(app_dir: Path, package: str, module: str = "app", variant: str = "debug") -> AppProfile:
    manifest = parse_manifest(find_manifest(app_dir, module, variant), package)
    caps = capabilities(app_dir, module)
    files = source_files(app_dir, module)
    signals: list[Signal] = []
    for f in files:
        signals.extend(scan(f, str(f.relative_to(app_dir))))

    profile = AppProfile(
        **manifest,
        capabilities=caps,
        ui_toolkit="compose" if "compose" in caps else "views",
        source_files=len(files),
        signals=signals,
    )
    profile.applicable_attacks = applicable(profile)
    return profile


def applicable(p: AppProfile) -> dict[str, str]:
    """Which fault types apply to this app, and why. Driven by the manifest, dependencies and signals."""
    out: dict[str, str] = {}
    hits = lambda attack: sum(1 for s in p.signals if attack in s.attacks)  # noqa: E731

    out["process_death"] = "every app can be killed in the background" + (
        f"; {hits('process_death')} state-loss signals in the code" if hits("process_death") else "")

    recreated = [a for a in p.activities if not {"orientation", "screenSize"} <= set(a.config_changes)
                 and a.screen_orientation in ("", "unspecified", "user", "sensor", "fullSensor")]
    if recreated:
        out["rotation"] = f"{len(recreated)} activities are recreated on rotation" + (
            f"; {hits('rotation')} lifecycle signals in the code" if hits("rotation") else "")

    if "android.permission.INTERNET" in p.permissions or "network" in p.capabilities:
        out["network_loss"] = "the app uses the network" + (
            f"; {hits('network_loss')} unhandled-error signals" if hits("network_loss") else "")

    if p.dangerous_permissions:
        perms = ", ".join(x.rsplit(".", 1)[-1] for x in p.dangerous_permissions)
        out["permission_revoke"] = f"declares runtime permissions: {perms}" + (
            f"; {hits('permission_revoke')} permission-handling signals" if hits("permission_revoke") else "")

    if hits("anr"):
        out["anr"] = f"{hits('anr')} blocking calls that may run on the main thread"
    return out
