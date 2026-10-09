"""The five Phase C recipes -- the proven legacy OGRemote processing, as
fixed compositions of the shared render-step library.

Each reproduces its legacy processor (``ogremote-ingest/processors/*.py``)
with the exact historical settings as defaults; only what a station
genuinely needs to tune is a parameter (which bed, the bed level, the
calendar fade and the news voice delay). Common to all: a 100 Hz high-pass,
48 kHz stereo preparation, two-pass EBU R128 loudness to -16 LUFS / -1 dBTP /
LRA 5, and -- natively -- FLAC 48 kHz instead of the legacy 320 kbps MP3.

Recipes are chosen by ``SpokenContentType.recipe_key``; nothing here keys off
a category or type name. Changing what a recipe outputs requires bumping its
``version``.
"""
from production.services.render import (
    BedMode, BedUnderlay, DelayVoice, Fade, Highpass, Loudnorm, NumberParam, Recipe, ReferenceParam, RenderPlan,
    TrimSilence, register,
)

HIGHPASS = Highpass(hz=100)
LOUDNORM = Loudnorm(integrated=-16.0, true_peak=-1.0, lra=5.0, two_pass=True)
BED_GAIN_HELP = "Bed level relative to the bed file, in dB"


def _cal_events(p):
    return RenderPlan(
        voice=(HIGHPASS, TrimSilence(threshold_db=-35.0, stop_duration=0.5)),
        bed=BedUnderlay("bed", gain_db=p["bed_gain_db"], mode=BedMode.LOOP_FADE, fade=Fade(p["fade_seconds"])),
        loudnorm=LOUDNORM,
    )


def _usd239min(p):
    return RenderPlan(
        voice=(HIGHPASS,),
        bed=BedUnderlay("bed", gain_db=p["bed_gain_db"], mode=BedMode.FADE_IF_LONGER, fade=Fade(3.0)),
        delay=DelayVoice(ms=10),
        loudnorm=LOUDNORM,
    )


def _news_item(p):
    return RenderPlan(
        voice=(HIGHPASS, TrimSilence(threshold_db=-36.0, stop_duration=0.5)),
        bed=BedUnderlay("bed", gain_db=p["bed_gain_db"], mode=BedMode.NATURAL_END),
        delay=DelayVoice(ms=p["voice_delay_ms"]),
        loudnorm=LOUDNORM,
    )


def _3min_ctr(p):
    return RenderPlan(
        voice=(HIGHPASS,),
        bed=BedUnderlay("bed", gain_db=p["bed_gain_db"], mode=BedMode.NATURAL_END),
        loudnorm=LOUDNORM,
    )


def _lcl_psa(p):
    return RenderPlan(voice=(HIGHPASS,), loudnorm=LOUDNORM)


RECIPES = (
    Recipe(
        key="cal_events", version=1, title="Calendar of events", plan=_cal_events,
        description="High-pass, silence removal, voice over a LOOPING bed (-13 dB) that fades out over 5 s "
                    "after the voice; loudness normalized.",
        params={"bed": ReferenceParam("bed.cal_events", help="Reference audio key of the looping bed"),
                "bed_gain_db": NumberParam(-13.0, -30.0, 0.0, help=BED_GAIN_HELP),
                "fade_seconds": NumberParam(5.0, 1.0, 15.0, help="Bed fade-out after the voice ends")},
    ),
    Recipe(
        key="usd239min", version=1, title="USD 239 minute update", plan=_usd239min,
        description="High-pass, voice 10 ms after a bed at -13 dB; a bed longer than the voice fades out over "
                    "3 s after it, a shorter bed ends on its own; loudness normalized.",
        params={"bed": ReferenceParam("bed.usd239min", help="Reference audio key of the bed"),
                "bed_gain_db": NumberParam(-13.0, -30.0, 0.0, help=BED_GAIN_HELP)},
    ),
    Recipe(
        key="news_item", version=1, title="Local news item", plan=_news_item,
        description="High-pass, silence removal, news intro bed (+6 dB) from the start with the voice entering "
                    "4 s in; both play out; loudness normalized.",
        params={"bed": ReferenceParam("bed.news_intro", help="Reference audio key of the news intro bed"),
                "bed_gain_db": NumberParam(6.0, -12.0, 12.0, help=BED_GAIN_HELP),
                "voice_delay_ms": NumberParam(4000, 0, 15000, integer=True,
                                              help="When the voice enters, in ms from the start of the intro")},
    ),
    Recipe(
        key="3min_ctr", version=1, title="3 minutes to center", plan=_3min_ctr,
        description="High-pass, voice over a bed (-10 dB) that plays to its natural end; loudness normalized.",
        params={"bed": ReferenceParam("bed.3min_ctr", help="Reference audio key of the bed"),
                "bed_gain_db": NumberParam(-10.0, -30.0, 0.0, help=BED_GAIN_HELP)},
    ),
    Recipe(
        key="lcl_psa", version=1, title="Local PSA", plan=_lcl_psa,
        description="High-pass and loudness normalization; no bed.",
    ),
)


def register_all():
    for recipe in RECIPES:
        register(recipe)
