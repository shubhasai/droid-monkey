"""Predicted attention per screen: where a user's eyes are likely to go first, as a heatmap and as a share per
tappable element. A prediction from the screenshot and the UI structure, not measured user behaviour.

Two signals, blended:
- Image salience: the spectral residual model (Hou & Zhang, CVPR 2007). What stands out visually: high contrast,
  isolated shapes, imagery. Pure numpy on a downscaled grey image.
- UI structure: what the screen dump says about each element. Larger elements, text and tappable controls draw
  more attention, contrast with their surroundings draws more, and people scan from the top-left (the F-pattern and
  top-of-screen bias found in eye-tracking studies of reading on screens).

From the heatmap: each tappable element's share of predicted attention, the screen's likely primary action (a
labelled button whose text is a call to action: next, save, search, continue...) and where it ranks. A primary
action that gets little attention while something non-interactive takes most of it is a finding worth a look.
"""

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from .safety import labels
from .tools import ui

GRID_W = 72          # heatmap resolution (width); height follows the screen's aspect ratio
BLEND = 0.5          # weight of image salience vs UI structure
# Labels of actions that move the user forward or commit something: what a screen's primary button says. Generic
# labels (more, view, share, search) are left out: they are everywhere and rarely the screen's goal.
CTA = re.compile(r"^(next|forward|continue|done|save|add|create|start|get started|play|join|buy|add to cart|checkout|"
                 r"check out|place order|pay|subscribe|sign up|log in|download|apply|confirm|send|submit|try it|"
                 r"try now|book|order|install|enable|turn on|allow|finish)\b", re.I)


@dataclass
class ElementShare:
    label: str
    share: float          # fraction of the screen's predicted attention inside this element
    bounds: tuple[int, int, int, int]
    primary: bool = False


@dataclass
class Attention:
    screen: str
    elements: list[ElementShare] = field(default_factory=list)   # tappable elements, most attention first
    primary: str | None = None
    primary_share: float = 0.0
    primary_rank: int | None = None
    top_region: str = ""     # what takes the most attention overall (an element's label, or "imagery")
    top_share: float = 0.0
    heatmap: str = ""        # overlay image path

    def summary(self) -> dict:
        d = asdict(self)
        d["elements"] = [f"{e.label[:40]}: {e.share:.0%}" for e in self.elements[:6]]
        return {k: v for k, v in d.items() if k not in ("heatmap", "screen")}


def _norm(a: np.ndarray) -> np.ndarray:
    a = a - a.min()
    return a / a.max() if a.max() > 0 else a


def _blur(a: np.ndarray, radius: float) -> np.ndarray:
    img = Image.fromarray((_norm(a) * 255).astype(np.uint8))
    return np.asarray(img.filter(ImageFilter.GaussianBlur(radius)), dtype=float) / 255


def spectral_saliency(img: Image.Image, width: int = GRID_W) -> np.ndarray:
    """Spectral residual salience map at `width` columns."""
    h = max(1, round(img.height * width / img.width))
    grey = np.asarray(img.convert("L").resize((width, h)), dtype=float) / 255
    f = np.fft.fft2(grey)
    log_amp = np.log(np.abs(f) + 1e-9)
    phase = np.angle(f)
    k = np.ones((3, 3)) / 9  # local average of the log spectrum
    padded = np.pad(log_amp, 1, mode="edge")
    avg = sum(padded[i:i + log_amp.shape[0], j:j + log_amp.shape[1]] * k[i, j] for i in range(3) for j in range(3))
    sal = np.abs(np.fft.ifft2(np.exp((log_amp - avg) + 1j * phase))) ** 2
    return _norm(_blur(sal, radius=2.5))


def _scale(bounds, sx: float, sy: float, w: int, h: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bounds
    return (max(0, int(x1 * sx)), max(0, int(y1 * sy)), min(w, max(int(x1 * sx) + 1, int(x2 * sx))),
            min(h, max(int(y1 * sy) + 1, int(y2 * sy))))


def structure_prior(screen: ui.Screen, package: str, grey: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    """Attention from the UI structure: each text or tappable element adds weight over its box."""
    H, W = grey.shape
    sx, sy = W / size[0], H / size[1]
    prior = np.zeros((H, W))
    for n in screen.app_nodes(package):
        if not n.area or n.bounds[1] < 0 or not (n.text or n.desc or n.clickable):
            continue
        x1, y1, x2, y2 = _scale(n.bounds, sx, sy, W, H)
        if x2 <= x1 or y2 <= y1:
            continue
        box_area = (x2 - x1) * (y2 - y1) / (W * H)
        if box_area > 0.5:          # containers and full-screen lists are not a focal point
            continue
        weight = 1.0 + (0.6 if n.clickable else 0) + (0.4 if n.text and len(n.text) <= 40 else 0)
        weight *= min(2.0, 0.6 + 6 * box_area ** 0.5)               # size, with diminishing returns
        region = grey[y1:y2, x1:x2]
        ring = grey[max(0, y1 - 2):min(H, y2 + 2), max(0, x1 - 2):min(W, x2 + 2)]
        weight *= 1 + 1.5 * abs(float(region.mean()) - float(ring.mean())) + float(region.std())  # contrast
        cy, cx = (y1 + y2) / 2 / H, (x1 + x2) / 2 / W
        weight *= np.exp(-1.1 * cy) * (1 - 0.25 * cx)                 # top-left reading bias
        prior[y1:y2, x1:x2] += weight / max(1.0, ((x2 - x1) * (y2 - y1)) ** 0.25)
    return _norm(_blur(prior, radius=1.5))


def predict(png: Path, xml: Path, package: str, sid: str = "") -> tuple[Attention, np.ndarray]:
    img = Image.open(png).convert("RGB")
    sal = spectral_saliency(img)
    H, W = sal.shape
    grey = np.asarray(img.convert("L").resize((W, H)), dtype=float) / 255
    screen = ui.parse(xml.read_text())
    prior = structure_prior(screen, package, grey, img.size)
    heat = BLEND * sal + (1 - BLEND) * prior
    sx, sy = W / img.width, H / img.height
    app = [n for n in screen.app_nodes(package) if n.area]
    if app:  # only the app's own area: not the status bar or the gesture bar
        top = int(min(n.bounds[1] for n in app) * sy)
        bottom = int(max(n.bounds[3] for n in app) * sy) + 1
        heat[:max(0, top)] = 0
        heat[bottom:] = 0
    heat = heat / heat.sum() if heat.sum() > 0 else heat
    taps = [n for n in screen.app_nodes(package) if n.clickable and n.area and n.bounds[1] >= 0]
    # the innermost tappable elements: a card that contains buttons is not counted on top of them
    leaves = [n for n in taps if not any(m is not n and m.area < n.area and n.bounds[0] <= m.center[0] <= n.bounds[2]
                                         and n.bounds[1] <= m.center[1] <= n.bounds[3] for m in taps)]
    out = Attention(screen=sid)
    for n in leaves:
        x1, y1, x2, y2 = _scale(n.bounds, sx, sy, W, H)
        if (x2 - x1) * (y2 - y1) > 0.6 * W * H:
            continue
        name = next(iter(labels(n, screen)), "") or n.rid.rsplit("/", 1)[-1] or f"unlabeled {n.cls.rsplit('.', 1)[-1]}"
        out.elements.append(ElementShare(label=name, share=round(float(heat[y1:y2, x1:x2].sum()), 3), bounds=n.bounds,
                                         primary=bool(CTA.match(name.strip()))))
    out.elements.sort(key=lambda e: -e.share)
    bars = _bars(out.elements)
    for e in out.elements:
        e.primary = e.primary and id(e) not in bars
    ctas = [e for e in out.elements if e.primary]
    if ctas:  # the likeliest primary action: the largest call-to-action button
        p = max(ctas, key=lambda e: (e.bounds[2] - e.bounds[0]) * (e.bounds[3] - e.bounds[1]))
        out.primary, out.primary_share = p.label, p.share
        out.primary_rank = out.elements.index(p) + 1
    tap_share = sum(e.share for e in out.elements)
    if out.elements and out.elements[0].share >= 1 - tap_share:
        out.top_region, out.top_share = out.elements[0].label, out.elements[0].share
    else:
        out.top_region, out.top_share = "non-interactive content (imagery, text)", round(1 - tap_share, 3)
    return out, heat


def _bars(elements: list[ElementShare]) -> set[int]:
    """Elements in a row of 3+ similar controls (a bottom navigation or tab bar): navigation, not a screen's
    primary action."""
    out = set()
    for e in elements:
        cy, h = (e.bounds[1] + e.bounds[3]) / 2, e.bounds[3] - e.bounds[1]
        row = [o for o in elements if abs((o.bounds[1] + o.bounds[3]) / 2 - cy) < 0.25 * h
               and abs((o.bounds[3] - o.bounds[1]) - h) < 0.3 * h]
        if len(row) >= 3:
            out |= {id(o) for o in row}
    return out


def _colour(heat: np.ndarray) -> Image.Image:
    """Blue -> green -> yellow -> red with transparency where attention is low."""
    v = _norm(heat)
    r = np.clip(1.5 - np.abs(4 * v - 3), 0, 1)
    g = np.clip(1.5 - np.abs(4 * v - 2), 0, 1)
    b = np.clip(1.5 - np.abs(4 * v - 1), 0, 1)
    a = np.where(v < 0.15, 0, np.clip((v - 0.15) * 1.5, 0, 0.75))  # no haze where attention is low
    rgba = (np.dstack([r, g, b, a]) * 255).astype(np.uint8)
    return Image.fromarray(rgba, "RGBA")


def overlay(png: Path, heat: np.ndarray, att: Attention, out: Path) -> Path:
    img = Image.open(png).convert("RGBA")
    layer = _colour(heat).resize(img.size, Image.BILINEAR)
    merged = Image.alpha_composite(img, layer)
    if att.primary:  # outline the primary action
        from PIL import ImageDraw
        p = next(e for e in att.elements if e.label == att.primary)
        ImageDraw.Draw(merged).rectangle(p.bounds, outline=(255, 255, 255, 255), width=6)
    out.parent.mkdir(parents=True, exist_ok=True)
    merged.convert("RGB").save(out, quality=85)
    return out


def audit(app_dir: Path, package: str, screen_ids: list[str], out_dir: Path) -> dict[str, Attention]:
    from . import paths
    result = {}
    for sid in screen_ids:
        png, xml = paths.screens_dir(app_dir) / f"{sid}.png", paths.screens_dir(app_dir) / f"{sid}.xml"
        if not (png.exists() and xml.exists()):
            continue
        att, heat = predict(png, xml, package, sid)
        att.heatmap = str(overlay(png, heat, att, out_dir / f"{sid}.jpg"))
        result[sid] = att
    return result


def flags(att: Attention) -> list[str]:
    """Patterns worth a reviewer's look (predictions, not verdicts): the screen's primary action is easy to miss,
    or another control draws clearly more attention than it."""
    if not att.primary or not att.primary_rank:
        return []
    out = []
    lead = att.elements[0]
    if att.primary_rank > 3:
        out.append(f"primary action '{att.primary}' ranks #{att.primary_rank} of {len(att.elements)} controls by "
                   f"predicted attention ({att.primary_share:.0%})")
    elif att.primary_rank > 1 and lead.share >= 2 * att.primary_share:
        out.append(f"'{lead.label[:40]}' is predicted to draw {lead.share / max(att.primary_share, 1e-3):.1f}x the "
                   f"attention of the primary action '{att.primary}' ({lead.share:.0%} vs {att.primary_share:.0%})")
    return out
