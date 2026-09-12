# Stage 04 — `src/generate.py` (Part 2: rebuild the winner for Guidde)

Build ONLY this stage. Follow the golden rules and build protocol in `CLAUDE.md`.
This is Part 2 — the generative half. It reads the Part 1 winner and produces a creative
brief + a shot-by-shot storyboard that adapts the winning ad's formula into a Guidde ad.
Do not touch scrape.py, extract.py, dedup.py, or score.py.

## Goal
Take the single winning ad (from `data/winner.json`) and its teardown, analyze what makes
it work, and produce for Guidde: (1) a creative brief, and (2) a shot-by-shot storyboard
with one AI-generated image per shot. Output as Markdown AND a rendered HTML file.

## Inputs
- `data/winner.json` — the winning creative (ad_id 1155616497059211,
  video/talking_head_testimonial) with its descriptive fields and ranking.
- `data/preview/winner_teardown.md` — the manual teardown: verbatim transcript, 10 shots
  with timestamps, and the formula map (hook, problem, product-demo moment, payoff, CTA,
  pacing, tone/casting). THIS is the source of truth for the ad's real formula — the
  pipeline never processed audio, so the teardown carries what the extraction missed.

## The winning formula to adapt (from the teardown — do not re-derive)
Mirror this structure closely (chosen approach: faithful structural adaptation):
- **Story-first, ~80% narrative.** The product appears in only ~2 of 10 shots (~4.5s,
  ~10.5% of runtime), placed dead-center (~52% through). Eight of ten shots have no product.
  The narrative "buys the right" to show the product. DO NOT make this a feature tour.
- **Accelerating edit into the demo.** Overall pacing is slow (~1 cut / 4.8s), but the two
  demo shots are the fastest cuts (~2.4s, ~1.9s), bracketed by slow takes. The storyboard
  must specify faster cutting on the demo shots.
- **Status/leverage payoff, not a benefit claim.** The source ad ends on a status claim
  ("too documented to fire, too prepared to ignore"), never "saves you X hours." Translate
  to a Guidde-appropriate status/leverage framing — NOT a time-savings benefit line.
- **Light CTA.** Source CTA is spoken over a talking head, no end card/logo/URL. For Guidde,
  strengthen it into a clean end card (a small, documented improvement on the source).

## Translate to Guidde, don't copy Scribe
Guidde is an AI how-to / documentation & walkthrough product (auto-captured step-by-step
video guides). Swap Scribe's product-demo moment for Guidde's actual product action
(capturing a process → an auto-generated walkthrough/guide). Keep the testimonial structure,
hook style, pacing, and status-payoff logic; change the product, use-case, and specifics.

## What generate.py produces
1. **Creative brief** (`results/04_brief.md`): objective, target audience, the winning
   formula named and explained (why it works), the adaptation rationale (Scribe → Guidde),
   hook, angle, key message, tone/casting, format specs (9:16 vertical, ~40s), and the
   strengthened CTA. Written to be usable by a real creative team.
2. **Shot-by-shot storyboard** (`results/05_storyboard/` + `results/05_storyboard.md`):
   ~10 shots mirroring the teardown's arc. Per shot: shot number, timestamp/duration,
   the pacing note (slow vs fast-cut), a visual description, and — as TEXT METADATA beside
   the frame, NOT baked into the image — the VO/spoken line, on-screen caption, and whether
   the product appears. One AI-generated image per shot.

## Image generation
- Model: OpenAI `gpt-image-2`. Load `OPENAI_API_KEY` from `.env` via python-dotenv; use the
  `openai` package. Pin the model as a constant (e.g. `IMAGE_MODEL = "gpt-image-2"`).
- Portrait/vertical output (1024×1536) to match the 9:16 ad format.
- IMPORTANT: generate the VISUAL COMPOSITION ONLY. Do NOT ask the model to render on-screen
  text, captions, or the CTA into the image — image models garble text. Captions/VO/CTA live
  as storyboard metadata beside each frame. Prompt each frame for scene, subject, setting,
  mood, framing — not words on screen.
- Save frames as `results/05_storyboard/shot_01.png` … `shot_10.png`.
- One generation per shot; one retry on failure; on persistent failure, record the shot with
  a placeholder note and continue (don't crash the whole run).

## Rendered output
- Also produce a single rendered `results/storyboard.html` that lays out the brief + every
  shot (image + its metadata) in reading order, so a reviewer can open one file and see the
  whole deliverable. (A PDF is optional; HTML is the required rendered form.)

## results/ is COMMITTED (unlike data/)
This is the "real output from a real run" deliverable. Ensure `results/` and
`results/05_storyboard/` are NOT gitignored (the current .gitignore ignores
`results/storyboard/` — fix that so results are committed). The generated brief, storyboard
markdown, HTML, and PNG frames all get committed.

## Prove it worked — print concisely to stdout
- winner loaded (ad_id, pattern), teardown loaded
- number of shots generated, number of images successfully generated vs failed
- the image-generation cost estimate (shots × per-image price)
- paths to brief.md, storyboard.md, storyboard.html, and the frame count
Then STOP.

## Cost
~10 images on gpt-image-2 at ~$0.03–0.05 each ≈ well under $1, plus a small LLM spend for
writing the brief/shot text (claude-sonnet-5 is fine here, reuse the extract client). Print
the total.

## Constraints
- Modify only `src/generate.py` (plus the .gitignore fix for results/).
- Dependencies: `openai` (add to requirements.txt), plus existing anthropic/requests/stdlib.
- No auth, UI, tests, CLI parser, or deployment. Deterministic top-level flow; the LLM
  writes brief/shot copy, the image model renders frames — neither "decides" anything.
- After proof prints, stop and suggest the commit message. Do not run git.
