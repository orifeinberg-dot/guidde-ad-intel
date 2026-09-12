"""
generate.py — Part 2: rebuild the winning competitor ad as a Guidde creative brief
plus a shot-by-shot storyboard.

Reads the Part 1 winner (data/winner.json) and the manual teardown
(data/preview/winner_teardown.md) and produces, for Guidde:
  results/04_brief.md        the creative brief
  results/05_storyboard.md   the shot-by-shot storyboard
  results/05_storyboard/     one generated frame per shot
  results/storyboard.html    everything rendered in reading order

The teardown is the source of truth for the winning FORMULA. The Part 1 pipeline never
processed audio, so the extracted fields describe the ad's surface; the teardown carries
the structure that actually makes it work (where the product appears, how the edit paces
around it, what kind of payoff it lands).

Top-level flow is deterministic. The LLM writes copy and the image model renders frames;
neither decides anything — the winner was chosen by arithmetic in score.py.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path

import anthropic
import imageio_ffmpeg
from dotenv import load_dotenv
from openai import OpenAI

# ----------------------------------------------------------------------
# Config — pinned so a run is reproducible and costable.
# ----------------------------------------------------------------------
BRIEF_MODEL = "claude-sonnet-5"      # writes brief + shot copy
IMAGE_MODEL = "gpt-image-2"          # renders storyboard frames
IMAGE_SIZE = "1024x1536"             # portrait, matches the 9:16 ad format
IMAGE_QUALITY = "medium"
N_SHOTS = 10                         # mirrors the teardown's 10-shot arc

# Spec's published estimate; the real token usage is also reported at the end.
IMAGE_PRICE_USD = 0.04
PRICE_IN_PER_MTOK = 2.00             # claude-sonnet-5
PRICE_OUT_PER_MTOK = 10.00

WINNER_PATH = Path("data/winner.json")
TEARDOWN_PATH = Path("data/preview/winner_teardown.md")
RESULTS = Path("results")
BRIEF_PATH = RESULTS / "04_brief.md"
STORYBOARD_MD = RESULTS / "05_storyboard.md"
FRAMES_DIR = RESULTS / "05_storyboard"
HTML_PATH = RESULTS / "storyboard.html"
PAYLOAD_PATH = RESULTS / "05_storyboard.json"   # cached copy; re-runs must be stable
PDF_PATH = RESULTS / "storyboard.pdf"

# Full-res API output is CACHE, not deliverable: ~2MB per frame is print resolution for
# what are storyboard references. Originals stay in gitignored data/, web-resolution
# copies ship in results/ and get embedded in the HTML.
RAW_FRAMES = Path("data/preview/frames")
WEB_WIDTH = 768          # px; frames render at ~260px in the layout
JPEG_QUALITY = 4         # ffmpeg -q:v scale, 2=best..31=worst; 4 ~= 85%
LOGO_SVG = Path("assets/guidde_logo.svg")       # real Guidde vector wordmark, #CB0000
BRAND_RED = "#CB0000"

# The end card is a BRAND COMPOSITE, not a generated frame. Image models cannot render a
# logo lockup or legible CTA type reliably — they approximate letterforms and invent marks.
# So the final beat is assembled in HTML/CSS over the real vector asset, which is also how
# this is done in real production: the edit leaves a clean plate and the brand lockup is
# applied as vector artwork on top.
END_CARD_SHOT = N_SHOTS

# The formula constants taken from the teardown — asserted against the LLM's output so
# a chatty model cannot quietly turn this into a feature tour.
DEMO_SHOTS_EXPECTED = 2
DEMO_MIDPOINT_RANGE = (0.40, 0.65)   # product must land near the centre of the runtime


PROMPT = """You are a senior creative director writing a real ad brief for Guidde.

Guidde is an AI how-to documentation product: you perform a process once, and Guidde
captures it and auto-generates a step-by-step video walkthrough you can share.

You are ADAPTING A STRUCTURAL FORMULA from a competitor's best-performing ad. Adapt the
STRUCTURE ONLY. Do not reuse its script, its lines, its characters, its setting, or its
specific jokes. Write original Guidde creative.

THE FORMULA TO MIRROR (measured from the competitor ad, do not re-derive):
- Story-first. The product appears in only 2 of 10 shots, about 4.5s of a ~43s runtime
  (~10%), placed dead-centre at ~52% through. Eight of ten shots have NO product.
  The narrative buys the right to show the product. This is NOT a feature tour.
- The edit accelerates into the demo. Overall pacing is slow (~1 cut per 4.8s), but the
  two demo shots are the FASTEST cuts (~2.4s and ~1.9s), bracketed by slow takes.
- The payoff is a STATUS/LEVERAGE claim, not a benefit claim. The competitor ends on the
  speaker being professionally un-dismissable — never "saves you X hours". Write a
  Guidde-appropriate status/leverage payoff. Do NOT write a time-savings line.
- One casual presenter, direct address to camera, handheld/UGC feel, no studio gloss.
- The competitor's CTA is weak (spoken only, no end card). Strengthen it for Guidde into
  a clean end card — a small, deliberate improvement on the source.

Return ONLY a JSON object, no prose and no markdown fences, with exactly these keys:

"brief": {
  "objective": str,
  "audience": str,
  "winning_formula": str,        # name the formula and explain WHY it works
  "adaptation_rationale": str,   # what carries over from the competitor, what changes, why
  "hook": str,
  "angle": str,
  "key_message": str,
  "tone_and_casting": str,
  "format_specs": str,           # 9:16 vertical, ~40s, etc.
  "cta": str
}
"shots": [ exactly 10 objects, in order, each:
  {
    "shot": int,                 # 1-10
    "start": float, "end": float,   # seconds; shot 1 starts at 0.0, shot 10 ends ~43.0
    "beat": str,                 # hook | problem | mechanism | product_demo | payoff | cta
    "pacing": str,               # "slow take" or "fast cut" - demo shots must be fast cuts
    "visual": str,               # what the viewer sees
    "vo": str,                   # the spoken line for this shot
    "caption": str,              # burned-in caption text (short fragment)
    "product_visible": bool,     # true for EXACTLY 2 shots, adjacent, near the centre
    "image_prompt": str
  }
]

RULES FOR image_prompt — these render as storyboard frames:
- Describe VISUAL COMPOSITION ONLY: subject, setting, framing, lighting, mood, action.
- Start each with "Vertical 9:16 storyboard frame."
- NEVER ask for text, captions, titles, UI copy, logos, watermarks or brand marks. Image
  models garble text. End every prompt with:
  "No text, no captions, no logos, no watermarks."
- For the two product shots, describe a generic, unbranded software walkthrough on a
  screen (a step list, a highlighted click, a progress of steps) — never a real product's
  UI and never a readable interface.
- Describe ORIGINAL people. No celebrities, no real or well-known characters.
- Keep the same presenter's description consistent across every shot so the frames read
  as one person.
"""


def load_inputs() -> tuple[dict, str]:
    winner = json.loads(WINNER_PATH.read_text())
    teardown = TEARDOWN_PATH.read_text()
    return winner, teardown


def write_copy(winner: dict, teardown: str, client) -> tuple[dict, dict]:
    """One LLM call -> the brief and all 10 shots. Returns (payload, usage)."""
    rec = winner["winner_record"]
    context = (
        f"The competitor ad being adapted: ad_id {winner['winner_ad_id']}, pattern "
        f"{winner['winning_pattern']}, running {winner['days_running']} days, "
        f"reach standing {winner['reach_standing']}.\n"
        f"Its extracted fields: hook={rec['hook_type']!r}, angle={rec['angle_or_usecase']!r}, "
        f"cta={rec['cta']!r}, on-screen text={rec['on_screen_text_style']!r}.\n\n"
        f"THE TEARDOWN (source of truth for the formula):\n{teardown}"
    )
    resp = client.messages.create(
        model=BRIEF_MODEL,
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        messages=[{"role": "user", "content": PROMPT + "\n\n" + context}],
    )
    text = next(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1].rsplit("```", 1)[0]
    payload = json.loads(text[text.find("{"):text.rfind("}") + 1])
    return payload, {"in": resp.usage.input_tokens, "out": resp.usage.output_tokens}


def validate_shots(shots: list[dict]) -> None:
    """Hard gate: the formula must survive the LLM, not just be described to it."""
    assert len(shots) == N_SHOTS, f"expected {N_SHOTS} shots, got {len(shots)}"
    demo = [s for s in shots if s["product_visible"]]
    assert len(demo) == DEMO_SHOTS_EXPECTED, \
        f"product must appear in exactly {DEMO_SHOTS_EXPECTED} shots, got {len(demo)}"
    idx = sorted(s["shot"] for s in demo)
    assert idx[1] - idx[0] == 1, f"demo shots must be adjacent, got {idx}"
    runtime = shots[-1]["end"]
    mid = (demo[0]["start"] + demo[-1]["end"]) / 2 / runtime
    assert DEMO_MIDPOINT_RANGE[0] <= mid <= DEMO_MIDPOINT_RANGE[1], \
        f"demo lands at {mid:.0%} of runtime; formula wants {DEMO_MIDPOINT_RANGE}"
    for s in demo:
        assert "fast" in s["pacing"].lower(), \
            f"shot {s['shot']} is a demo shot but paced {s['pacing']!r}"
    for s in shots:
        assert s["image_prompt"].rstrip().endswith(
            "No text, no captions, no logos, no watermarks."), \
            f"shot {s['shot']} image_prompt missing the no-text instruction"


def render_frame(shot: dict, client: OpenAI) -> tuple[bytes | None, dict, str | None]:
    """One image per shot, one retry, never crash the run."""
    err = None
    for _ in range(2):
        try:
            r = client.images.generate(model=IMAGE_MODEL, prompt=shot["image_prompt"],
                                       size=IMAGE_SIZE, quality=IMAGE_QUALITY, n=1)
            u = getattr(r, "usage", None)
            return (base64.b64decode(r.data[0].b64_json),
                    {"in": getattr(u, "input_tokens", 0), "out": getattr(u, "output_tokens", 0)},
                    None)
        except Exception as exc:                       # noqa: BLE001 — record and continue
            err = f"{type(exc).__name__}: {exc}"[:200]
    return None, {"in": 0, "out": 0}, err


# ----------------------------------------------------------------------
# Output writers
# ----------------------------------------------------------------------
def write_brief(brief: dict, winner: dict) -> None:
    b, L = brief, []
    L.append("# Guidde creative brief — adapted from the winning competitor ad\n")
    L.append(f"Source: competitor ad `{winner['winner_ad_id']}` "
             f"(`{winner['winning_pattern']}`), {winner['days_running']} days running, "
             f"reach standing {winner['reach_standing']} — selected by `score.py`.\n")
    for title, key in (("Objective", "objective"), ("Audience", "audience"),
                       ("The winning formula — and why it works", "winning_formula"),
                       ("Adaptation rationale", "adaptation_rationale"),
                       ("Hook", "hook"), ("Angle", "angle"),
                       ("Key message", "key_message"),
                       ("Tone and casting", "tone_and_casting"),
                       ("Format", "format_specs"), ("Call to action", "cta")):
        L.append(f"## {title}\n\n{b[key]}\n")
    L.append("## Production note — how the storyboard frames were made\n")
    L.append("Shots 1-9 are AI-generated visual references (`gpt-image-2`, 1024x1536). They "
             "are mood and composition guides for a director, not finished art, and they "
             "carry no text: captions and VO live as metadata beside each frame, because "
             "image models garble type.\n")
    L.append("The end card (shot 10) is deliberately NOT generated. It is a composite: the "
             "real Guidde vector wordmark (`assets/guidde_logo.svg`) over a clean plate, "
             f"with the CTA set as real type on a real button in Guidde red `{BRAND_RED}`. "
             "Brand lockups are graphic elements image models cannot render reliably — they "
             "approximate letterforms and invent marks. Applying the brand as a real vector "
             "overlay is also how this works in production: the edit delivers a clean plate "
             "and the lockup goes on top as artwork.\n")
    BRIEF_PATH.write_text("\n".join(L))


def write_storyboard_md(shots: list[dict], frames: dict[int, str]) -> None:
    L = ["# Storyboard — 10 shots\n",
         "Captions and VO are metadata beside each frame, never rendered into the image.\n"]
    L.append("| # | Time | Beat | Pacing | Product | Frame |")
    L.append("|---|---|---|---|---|---|")
    for s in shots:
        f = frames.get(s["shot"])
        if s["shot"] == END_CARD_SHOT:
            cell = "_brand composite (see storyboard.html)_"
        else:
            cell = f"![shot {s['shot']}]({Path(f).relative_to(RESULTS)})" if f else "_not generated_"
        L.append(f"| {s['shot']} | {s['start']:.1f}-{s['end']:.1f}s | {s['beat']} | "
                 f"{s['pacing']} | {'YES' if s['product_visible'] else 'no'} | {cell} |")
    L.append("")
    for s in shots:
        L.append(f"## Shot {s['shot']} — {s['start']:.1f}-{s['end']:.1f}s "
                 f"({s['end'] - s['start']:.1f}s, {s['pacing']})\n")
        if s["shot"] == END_CARD_SHOT:
            L.append("- **Frame type:** BRAND COMPOSITE — real Guidde vector wordmark "
                     "(`assets/guidde_logo.svg`) over a clean plate, CTA set as a real "
                     "button in Guidde red `#CB0000`. NOT an AI-generated frame: image "
                     "models cannot render a logo lockup or legible CTA type reliably.")
        L.append(f"- **Beat:** {s['beat']}")
        L.append(f"- **Product on screen:** {'yes' if s['product_visible'] else 'no'}")
        L.append(f"- **Visual:** {s['visual']}")
        L.append(f"- **VO:** {s['vo']}")
        L.append(f"- **Caption:** {s['caption']}\n")
    STORYBOARD_MD.write_text("\n".join(L) + "\n")


def make_web_frame(raw: Path, out: Path) -> bool:
    """Downscale a full-res frame to web resolution. Returns True if written."""
    exe = imageio_ffmpeg.get_ffmpeg_exe()
    r = subprocess.run(
        [exe, "-hide_banner", "-loglevel", "error", "-i", str(raw),
         "-vf", f"scale={WEB_WIDTH}:-2", "-q:v", str(JPEG_QUALITY), "-y", str(out)],
        capture_output=True, timeout=120)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 0


def data_uri(path: Path) -> str:
    """Embed a frame so the HTML renders standalone — moved, emailed, anywhere."""
    mime = "image/jpeg" if path.suffix in (".jpg", ".jpeg") else "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def end_card_html(ec: dict, logo_svg: str) -> str:
    """
    The final beat, assembled rather than generated.

    The real Guidde wordmark is INLINED as vector markup — not a raster, not an AI
    approximation of a logo. The CTA is real type on a real button in Guidde red, so it
    is legible and on-brand, which is exactly what an image model cannot deliver.
    """
    # Styles are INLINE on purpose. This block must render when the file is opened by
    # double-click from any browser, with no stylesheet, no CSS custom properties and no
    # aspect-ratio support required — the end card is the one frame that is assembled
    # rather than generated, so it must never be the one frame that fails to appear.
    svg = logo_svg.replace(
        "<svg ", "<svg style=\"width:150px;height:auto;display:block\" ", 1)
    box = ("background:#ffffff;border:1px solid #e3e6ea;border-radius:8px;"
           "min-height:360px;display:flex;flex-direction:column;align-items:center;"
           "justify-content:center;gap:18px;padding:28px;text-align:center")
    return (f"<div id='shot-10' class='endcard' style='{box}'>{svg}"
            f"<div style='font-size:17px;font-weight:640;line-height:1.3;color:#16181d;"
            f"max-width:210px'>{ec['headline']}</div>"
            f"<div style='background:{BRAND_RED};color:#ffffff;border-radius:999px;"
            f"padding:11px 22px;font-size:14px;font-weight:600'>{ec['button']}</div>"
            f"<div style='font-size:13px;color:#5f6672'>{ec['url']}</div></div>")


def _pdf_safe(t: str) -> str:
    """fpdf core fonts are latin-1; fold the typographic characters the copy uses."""
    for a, b in (("\u2014", "-"), ("\u2013", "-"), ("\u2019", "'"), ("\u2018", "'"),
                 ("\u201c", '"'), ("\u201d", '"'), ("\u2026", "..."), ("\u00a0", " ")):
        t = t.replace(a, b)
    return t.encode("latin-1", "replace").decode("latin-1")


def write_pdf(brief: dict, shots: list[dict], frames: dict[int, str], winner: dict,
              end_card: dict) -> None:
    """
    The same content as the HTML, in the most universally openable format.
    The end card is rasterised FROM THE REAL VECTOR ASSET, never redrawn by hand.
    """
    from fpdf import FPDF

    pdf = FPDF(orientation="P", unit="mm", format="A4")
    pdf.set_auto_page_break(True, margin=16)
    pdf.add_page()
    pdf.set_font("helvetica", "B", 19)
    pdf.multi_cell(0, 8, _pdf_safe("Guidde storyboard"), new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(95, 102, 114)
    pdf.multi_cell(0, 4.6, _pdf_safe(
        f"Adapted from competitor ad {winner['winner_ad_id']} "
        f"({winner['winning_pattern']}) - {winner['days_running']} days running, "
        f"reach standing {winner['reach_standing']}."), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)

    for title, key in (("Objective", "objective"), ("Audience", "audience"),
                       ("Winning formula", "winning_formula"),
                       ("Adaptation rationale", "adaptation_rationale"),
                       ("Hook", "hook"), ("Angle", "angle"),
                       ("Key message", "key_message"),
                       ("Tone & casting", "tone_and_casting"),
                       ("Format", "format_specs"), ("CTA", "cta")):
        pdf.set_font("helvetica", "B", 10); pdf.set_text_color(22, 24, 29)
        pdf.multi_cell(0, 5.4, _pdf_safe(title), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 9.2); pdf.set_text_color(50, 55, 64)
        pdf.multi_cell(0, 4.5, _pdf_safe(brief[key]), new_x="LMARGIN", new_y="NEXT"); pdf.ln(2)

    # logo, rasterised from the real SVG for the end-card page
    logo_png = None
    try:
        import cairosvg
        logo_png = str(RAW_FRAMES / "_logo.png")
        cairosvg.svg2png(url=str(LOGO_SVG), write_to=logo_png,
                         output_width=600, background_color="white")
    except Exception:
        logo_png = None

    for sh in shots:
        pdf.add_page()
        pdf.set_font("helvetica", "B", 13); pdf.set_text_color(22, 24, 29)
        pdf.multi_cell(0, 6.5, _pdf_safe(
            f"Shot {sh['shot']} - {sh['start']:.1f}-{sh['end']:.1f}s "
            f"({sh['end']-sh['start']:.1f}s, {sh['pacing']})"),
            new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("helvetica", "", 8.6); pdf.set_text_color(95, 102, 114)
        flag = "  |  PRODUCT ON SCREEN" if sh["product_visible"] else ""
        pdf.multi_cell(0, 4.4, _pdf_safe(f"beat: {sh['beat']}{flag}"),
                       new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)

        top, w = pdf.get_y(), 62.0
        if sh["shot"] == END_CARD_SHOT:
            h = w * 1.5
            pdf.set_fill_color(255, 255, 255); pdf.set_draw_color(227, 230, 234)
            pdf.rect(pdf.l_margin, top, w, h, style="DF")
            if logo_png:
                pdf.image(logo_png, x=pdf.l_margin + (w - 34) / 2, y=top + h * 0.30, w=34)
            pdf.set_xy(pdf.l_margin + 5, top + h * 0.44)
            pdf.set_font("helvetica", "B", 9.5); pdf.set_text_color(22, 24, 29)
            pdf.multi_cell(w - 10, 4.6, _pdf_safe(end_card.get("headline", "")), align="C")
            by = pdf.get_y() + 3
            bw, bh = 40.0, 9.0
            pdf.set_fill_color(203, 0, 0)     # #CB0000
            pdf.rect(pdf.l_margin + (w - bw) / 2, by, bw, bh, style="F",
                     round_corners=True, corner_radius=4.5)
            pdf.set_xy(pdf.l_margin + (w - bw) / 2, by + 2.4)
            pdf.set_font("helvetica", "B", 8.6); pdf.set_text_color(255, 255, 255)
            pdf.cell(bw, 4, _pdf_safe(end_card.get("button", "")), align="C")
            pdf.set_xy(pdf.l_margin + 5, by + bh + 2.5)
            pdf.set_font("helvetica", "", 8); pdf.set_text_color(95, 102, 114)
            pdf.cell(w - 10, 4, _pdf_safe(end_card.get("url", "")), align="C")
            bottom = top + h
        else:
            f = frames.get(sh["shot"])
            if f:
                pdf.image(f, x=pdf.l_margin, y=top, w=w)
                bottom = top + w * 1.5
            else:
                bottom = top

        tx = pdf.l_margin + w + 7
        tw = pdf.w - pdf.r_margin - tx
        y = top
        rows = [("Visual", sh["visual"]), ("VO", sh["vo"]), ("Caption", sh["caption"])]
        if sh["shot"] == END_CARD_SHOT:
            rows.insert(0, ("Frame type", "BRAND COMPOSITE - real Guidde vector wordmark "
                                          "over a clean plate. Not an AI-generated frame."))
        for label, val in rows:
            pdf.set_xy(tx, y)
            pdf.set_font("helvetica", "B", 8.4); pdf.set_text_color(95, 102, 114)
            pdf.multi_cell(tw, 4.2, _pdf_safe(label)); y = pdf.get_y()
            pdf.set_xy(tx, y)
            pdf.set_font("helvetica", "", 9); pdf.set_text_color(22, 24, 29)
            pdf.multi_cell(tw, 4.4, _pdf_safe(val)); y = pdf.get_y() + 2.6
        pdf.set_y(max(bottom, y))

    pdf.output(str(PDF_PATH))


def write_html(brief: dict, shots: list[dict], frames: dict[int, str], winner: dict,
               end_card: dict) -> None:
    logo_svg = LOGO_SVG.read_text().strip() if LOGO_SVG.exists() else ""
    if not logo_svg:
        raise SystemExit(f"Missing brand asset {LOGO_SVG} — the end card needs the real "
                         f"vector wordmark; refusing to fake it.")
    def esc(t):
        return (str(t).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    parts = ["""<!doctype html><meta charset="utf-8">
<title>Guidde storyboard</title>
<style>
:root{--fg:#16181d;--mut:#5f6672;--line:#e3e6ea;--bg:#fff;--accent:#1c6cf5;
 --brand:__BRAND__}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
 font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:40px 20px 80px}
h1{font-size:30px;margin:0 0 6px} h2{font-size:19px;margin:34px 0 8px}
.sub{color:var(--mut);margin-bottom:28px}
.brief div{margin-bottom:18px}
.shot{display:grid;grid-template-columns:260px 1fr;gap:22px;padding:22px 0;
 border-top:1px solid var(--line)}
.shot img{width:100%;border-radius:8px;display:block}
.ph{width:100%;aspect-ratio:2/3;border:1px dashed var(--line);border-radius:8px;
 display:flex;align-items:center;justify-content:center;color:var(--mut);font-size:13px}
.meta b{display:inline-block;min-width:74px;color:var(--mut);font-weight:600;font-size:13px}
.tag{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;
 background:#eef1f5;color:var(--mut);margin-right:6px}
.demo{background:var(--accent);color:#fff}
.endcard{width:100%;aspect-ratio:2/3;border:1px solid var(--line);border-radius:8px;
 background:#fff;display:flex;flex-direction:column;align-items:center;
 justify-content:center;gap:20px;padding:26px;text-align:center}
.endcard svg{width:132px;height:auto;display:block}
.endcard .hl{font-size:17px;font-weight:640;line-height:1.3;color:#16181d;max-width:200px}
.endcard .btn{background:var(--brand);color:#fff;border-radius:999px;padding:11px 22px;
 font-size:14px;font-weight:600;letter-spacing:.01em}
.endcard .url{font-size:13px;color:var(--mut)}
.composite{background:#fdecec;color:var(--brand)}
@media(max-width:720px){.shot{grid-template-columns:1fr}}
</style><div class="wrap">"""]
    parts[0] = parts[0].replace("__BRAND__", BRAND_RED)
    parts.append(f"<h1>Guidde storyboard</h1><div class='sub'>Adapted from competitor ad "
                 f"{esc(winner['winner_ad_id'])} ({esc(winner['winning_pattern'])}) — "
                 f"{winner['days_running']} days running.</div>")
    parts.append("<h2>Creative brief</h2><div class='brief'>")
    for title, key in (("Objective", "objective"), ("Audience", "audience"),
                       ("Winning formula", "winning_formula"),
                       ("Adaptation rationale", "adaptation_rationale"),
                       ("Hook", "hook"), ("Angle", "angle"),
                       ("Key message", "key_message"),
                       ("Tone & casting", "tone_and_casting"),
                       ("Format", "format_specs"), ("CTA", "cta")):
        parts.append(f"<div><b>{esc(title)}</b><br>{esc(brief[key])}</div>")
    parts.append("</div><h2>Shots</h2>"
                 "<p style='margin:-4px 0 18px'><a href='#shot-10' "
                 "style='color:#CB0000;font-weight:600'>Jump to the end card (shot 10) &rarr;</a>"
                 "</p>")
    for s in shots:
        f = frames.get(s["shot"])
        if s["shot"] == END_CARD_SHOT:
            img = end_card_html(end_card, logo_svg)
        elif f:
            # embedded, not linked: the file must render standalone if moved or emailed
            img = (f"<img loading='lazy' src='{data_uri(Path(f))}' "
                   f"alt='shot {s['shot']}'>")
        else:
            img = "<div class='ph'>frame not generated</div>"
        tag = "<span class='tag demo'>product on screen</span>" if s["product_visible"] else ""
        if s["shot"] == END_CARD_SHOT:
            tag += "<span class='tag composite'>brand composite — real vector logo</span>"
        parts.append(
            f"<div class='shot'><div>{img}</div><div class='meta'>"
            f"<h3 style='margin:0 0 8px'>Shot {s['shot']} · {s['start']:.1f}–{s['end']:.1f}s</h3>"
            f"<span class='tag'>{esc(s['beat'])}</span>"
            f"<span class='tag'>{esc(s['pacing'])}</span>{tag}<br><br>"
            f"<b>Visual</b> {esc(s['visual'])}<br>"
            f"<b>VO</b> {esc(s['vo'])}<br>"
            f"<b>Caption</b> {esc(s['caption'])}</div></div>")
    parts.append("</div>")
    HTML_PATH.write_text("".join(parts))


def main():
    load_dotenv()
    os.environ["ANTHROPIC_API_KEY"], os.environ["OPENAI_API_KEY"]   # fail early, not mid-run
    FRAMES_DIR.mkdir(parents=True, exist_ok=True)
    RAW_FRAMES.mkdir(parents=True, exist_ok=True)

    winner, teardown = load_inputs()
    print(f"\nwinner loaded : {winner['winner_ad_id']}  ({winner['winning_pattern']})")
    print(f"teardown      : {TEARDOWN_PATH} ({len(teardown):,} chars)")

    if PAYLOAD_PATH.exists():
        payload = json.loads(PAYLOAD_PATH.read_text())
        copy_usage = {"in": 0, "out": 0}
        print(f"copy          : reusing cached {PAYLOAD_PATH} (no LLM spend)")
    else:
        payload, copy_usage = write_copy(winner, teardown, anthropic.Anthropic())
        PAYLOAD_PATH.write_text(json.dumps(payload, indent=2))
    brief, shots = payload["brief"], payload["shots"]
    shots.sort(key=lambda s: s["shot"])
    validate_shots(shots)
    demo = [s["shot"] for s in shots if s["product_visible"]]
    runtime = shots[-1]["end"]
    print(f"copy written  : brief + {len(shots)} shots, runtime {runtime:.1f}s")
    print(f"formula check : product in shots {demo} "
          f"({sum(s['end']-s['start'] for s in shots if s['product_visible']):.1f}s, "
          f"{sum(s['end']-s['start'] for s in shots if s['product_visible'])/runtime:.0%} "
          f"of runtime, centred at "
          f"{(shots[demo[0]-1]['start']+shots[demo[-1]-1]['end'])/2/runtime:.0%})")

    oai = OpenAI()
    frames, failures, img_usage = {}, [], {"in": 0, "out": 0}
    for s in shots:
        existing = RAW_FRAMES / f"shot_{s['shot']:02d}.png"
        if s["shot"] == END_CARD_SHOT:
            print(f"  shot {s['shot']:>2}: end card — HTML/CSS brand composite, not generated")
            continue
        if existing.exists():
            frames[s["shot"]] = str(existing)
            print(f"  shot {s['shot']:>2}: {existing.name}  (cached, {existing.stat().st_size:,} bytes)")
            continue
        data, use, err = render_frame(s, oai)
        img_usage["in"] += use["in"]; img_usage["out"] += use["out"]
        if data is None:
            failures.append((s["shot"], err))
            print(f"  shot {s['shot']:>2}: FAILED  {err}")
            continue
        p = RAW_FRAMES / f"shot_{s['shot']:02d}.png"
        p.write_bytes(data)
        frames[s["shot"]] = str(p)
        print(f"  shot {s['shot']:>2}: {p.name}  ({len(data):,} bytes)")

    # Downscale every raw frame into the committed deliverable.
    web, raw_bytes, web_bytes = {}, 0, 0
    for n, rawp in sorted(frames.items()):
        rp = Path(rawp)
        out = FRAMES_DIR / f"shot_{n:02d}.jpg"
        if make_web_frame(rp, out):
            web[n] = str(out)
            raw_bytes += rp.stat().st_size
            web_bytes += out.stat().st_size
        else:
            print(f"  shot {n:>2}: downscale FAILED, keeping full-res reference")
            web[n] = str(rp)
    if raw_bytes:
        print(f"\nframes        : {len(web)} downscaled to {WEB_WIDTH}px wide  "
              f"({raw_bytes/1e6:.1f}MB -> {web_bytes/1e6:.1f}MB)")

    write_brief(brief, winner)
    write_storyboard_md(shots, web)
    write_html(brief, shots, web, winner, payload.get("end_card", {}))
    write_pdf(brief, shots, web, winner, payload.get("end_card", {}))

    copy_cost = (copy_usage["in"] / 1e6 * PRICE_IN_PER_MTOK
                 + copy_usage["out"] / 1e6 * PRICE_OUT_PER_MTOK)
    img_cost = len(frames) * IMAGE_PRICE_USD
    print(f"\nimages        : {len(frames)}/{len(shots)} generated, {len(failures)} failed")
    print(f"copy cost     : {copy_usage['in']:,} in + {copy_usage['out']:,} out "
          f"= ${copy_cost:.3f} ({BRIEF_MODEL})")
    print(f"image cost    : {len(frames)} x ~${IMAGE_PRICE_USD:.2f} = ~${img_cost:.2f} "
          f"({IMAGE_MODEL}, {img_usage['out']:,} image output tokens)")
    print(f"TOTAL         : ~${copy_cost + img_cost:.2f}")
    print(f"  html          : self-contained, {HTML_PATH.stat().st_size/1e6:.1f}MB "
          f"(frames embedded as data-URIs)")
    print(f"\n  {BRIEF_PATH}")
    print(f"  {STORYBOARD_MD}")
    print(f"  {HTML_PATH}")
    print(f"  {PDF_PATH}")
    print(f"  {FRAMES_DIR}/  ({len(frames)} frames)")


if __name__ == "__main__":
    main()
