# Hardcoded path audit — IsadoraAir 1.2 Phase 3

Every machine-specific absolute path found across all three companion
repos (`syndicated-ingest`, `weather-ingest`, `ogremote-ingest`),
classified per the Phase 3 spec:

```
required production constant
should be environment/config driven
safe default with override
documentation-only issue
```

Method: `git grep -n -E "/home/jreed|/srv/isadoraair|/run/isadoraair|/opt/isadoraair" -- '*.py'`
in each repo's tracked source (2026-08-12).

## Changed in this pass

| Path | Occurrences | Classification | Action taken |
|---|---|---|---|
| `ISADORAAIR_DIR = Path("/home/jreed/isadoraair-django")` | 8 (syndicated-ingest `lib/delivery.py`; weather-ingest `lib/delivery.py`, `lib/notify.py`, `lib/wxconfig.py`, `wx_alert_beep.py`; ogremote-ingest `lib/config.py`, `lib/delivery.py`, `lib/notify.py`) | Should be environment/config driven | Changed to `Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair"))` in all 8 -- see "Why this one, and why now" below. |
| `LIBRARY_ROOT = Path("/srv/isadoraair/music")` (the `lib/delivery.py` module-level constant) | 3 (one per repo's `lib/delivery.py`) | Safe default with override | Changed to `Path(os.environ.get("LIBRARY_ROOT", "/srv/isadoraair/music"))` -- brings these three in line with the pattern 5 individual syndicated-ingest show scripts already used inconsistently (see below). |

Both changes preserve the exact current value as the default -- **zero
behavior change on production today**, confirmed by:
- A functional resolution check: `Path(os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair")).resolve() == Path("/home/jreed/isadoraair-django").resolve()` → `True`.
- Live execution of the actual changed code paths against production: `weather-ingest/lib/wxconfig.py`'s `load_weather_config()` and `ogremote-ingest/lib/config.py`'s `load_ogremote_config()` both run for real through the new default and return real config (13 and 5 top-level keys respectively) -- not just a syntax check.
- `python3 -m py_compile` across every tracked `.py` file in all three repos, clean.

### Why this one, and why now

Section 13 of this phase makes `/opt/isadoraair` the documented
canonical application root. `/opt/isadoraair` already exists in
production as a symlink to `/home/jreed/isadoraair-django` (confirmed
Phase 1/2) -- so switching these constants' *default* from the literal
physical path to the canonical alias, wrapped in an env override, is
the direct, low-risk realization of that canonical-path decision the
task itself pointed at (its own example: `os.environ.get("ISADORAAIR_DIR", "/opt/isadoraair")`
rather than a hardcoded `/home/jreed/...`). It was worth doing now
rather than deferring because:
- It's the exact same one-line shape repeated identically in all 8
  places -- low review burden, not a broad refactor.
- Every one of these constants exists *only* to build a `subprocess.run`
  command line (`[str(ISADORAAIR_PYTHON), "manage.py", ...]`,
  `cwd=str(ISADORAAIR_DIR)`) -- there's no other logic branching on the
  value, so "does it still resolve to the same directory" is the
  complete correctness question, and that was verified directly.
- A future install using a different physical checkout path no longer
  requires editing companion-project source at all -- just setting
  `ISADORAAIR_DIR` in the environment (e.g. the systemd unit's
  `Environment=` line) or, more likely, simply keeping the
  `/opt/isadoraair` convention intact, which every restore procedure
  should do anyway.

## Reviewed, not changed

| Path | Occurrences | Classification | Reasoning |
|---|---|---|---|
| `"Ported from /home/jreed/auto_dl_scripts/..."` / `/home/jreed/wx_scripts/...` docstring comments | ~20, syndicated-ingest and weather-ingest | Documentation-only issue | Historical provenance comments describing the *old* kogr-sc box's script locations, referenced nowhere at runtime. Accurate as history; not a portability concern. |
| `os.environ.get("LIBRARY_ROOT", "/srv/isadoraair/music")` in individual show scripts (`syndicated-ingest/askakansan/get_ask_a_kansan.py`, `dnow/get_dnow.py`, `enhanced/get_enhanced.py`, `kns/get_kns.py`, `rch/get_rch.py`) | 5 | Already environment/config driven | Independent narrow uses (e.g. a legacy-file cleanup helper) that already followed the correct pattern before this pass touched anything. Left alone -- nothing to fix. |
| `ENGINE_CMD_PATH = Path("/run/isadoraair/engine_cmd.json")` | 3 (weather-ingest `amber_alert.py`, `wx_alert.py`; ogremote-ingest `lib/engine_cmd.py`) | Required production constant | `/run/isadoraair` is IsadoraAir's own `systemd-tmpfiles.d`-managed runtime directory (`deploy/isadoraair-tmpfiles.conf`), not a user-home path -- it's already a stable, semantic convention name independent of which Linux user or checkout location is in use. No portability gain from an override here. |
| `NOW_PLAYING_PATH = Path("/run/isadoraair/now_playing.json")` | 1 (syndicated-ingest `bsky/post_now_playing.py`) | Required production constant | Same reasoning as `ENGINE_CMD_PATH`. |
| `KOKORO_BINARY = "/home/jreed/kokoro/bin/kokoro_synth"` | 1 (weather-ingest `lib/voices.py`) | Documentation-only issue / deferred | Genuinely user-specific today. Deliberately **not** touched in this pass -- see `docs/KOKORO_PROVENANCE.md`'s own recommendation; this is exactly the kind of "generic TTS runtime path" question that phase's dedicated inspection covers, and touching it here without that context risked a shallower fix. |
| `PIPER_BINARY = "/home/jreed/weather-ingest/venv/bin/piper"` | 2 (weather-ingest `lib/voices.py`, `wx_alert.py` -- duplicated, not shared) | Documentation-only issue / deferred | Same reasoning; see `docs/PIPER_PROVENANCE.md`. The duplication between `lib/voices.py` and `wx_alert.py` (two independent copies of the same constant) is itself worth a future small cleanup, noted here rather than fixed. |
| `"piper_fallback": "/home/jreed/piper/en_US-hfc_{female,male}-medium.onnx"` | 2 (weather-ingest `lib/voices.py`) | Station-specific, correctly left alone | Voice *selection*, not a runtime-location question -- explicitly out of scope per this phase's own TTS-dependency-boundary instruction ("Do not bake Oak Grove voice preferences into generic dependency tooling"). |

## Canonical path convention (item 13)

**`/opt/isadoraair` is the canonical IsadoraAir application root** for
all generic restore/install tooling and companion-project integration
code. It may be a real checkout directory or a symlink to one --
current production has it as a symlink to
`/home/jreed/isadoraair-django`, and every one of the 8 constants
touched above now reflects that: tooling reads `/opt/isadoraair` (or
the `ISADORAAIR_DIR` env override) and never assumes which case it is.

`deploy/README.md` already uses `@@ISA_ROOT@@` → `/opt/isadoraair` as
its systemd-unit placeholder convention (see that file's install
script) -- this phase extends the same convention to companion-project
Python source, which previously hardcoded the physical path directly
instead.
