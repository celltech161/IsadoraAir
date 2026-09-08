# Speech text normalization and broadcast-copy audit

Status: current repository audit for r0049. This release documents existing behavior only. It does not move rules into Django, change synthesized wording, or create an operator-editable regular-expression system.

## Scope and safety rule

The repository has three different kinds of speech treatment: canonical preprocessing applied inside the shared TTS service, feature-owned broadcast copy, and source-specific cleanup performed before text reaches TTS. They are not interchangeable. In particular, a workaround verified against Kokoro must not automatically be applied to Piper. Promotion to a shared rule requires listening tests for each supported engine and voice plus a review of factual impact.

The examples below show deterministic output before provider synthesis. “Meaning risk” describes what a faulty edit could do, not evidence that the current rule is wrong.

## Canonical shared-TTS preprocessing

All callers routed through isadoraair.tts ultimately receive these rules from isadoraair/tts/normalization.py:preprocess_text. Tests are in isadoraair/tests/test_tts_runtime.py.

| Rule | Example input → output | Why / scope / provider dependence | Future classification | Meaning risk and coverage |
| --- | --- | --- | --- | --- |
| Remove hashtag tokens | “Alert #KSWX now” → “Alert now” | Preserved production Kokoro-helper behavior; global shared-TTS scope; originally Kokoro-derived | engine-specific pronunciation candidate pending cross-engine listening | Can remove meaningful identifiers; exact removal/order and whitespace are tested |
| Speak phone digits in groups | “(785) 555-0123” → “seven eight five, five five five, zero one two three” | Avoids whole-number readings; global; production Kokoro-derived | global pronunciation candidate if verified on Piper | Wrong matching could alter arbitrary numbers; phone formats and boundaries are tested |
| Speak emergency number digit-by-digit | “Call 911” → “Call nine one one” | Safety-number clarity; global; production Kokoro-derived | global pronunciation candidate | High factual/safety sensitivity; ordering relative to phone matching is tested |
| Speak decimal points | “12.5” → “12 point 5” | Prevents decimal punctuation being treated as a pause; global; production Kokoro-derived | engine-specific pronunciation candidate until Piper is verified | High factual risk for measurements; digit boundaries are tested |
| Collapse whitespace | tabs/newlines/runs → one space | Stable clean input after substitutions; global and engine-neutral | must remain code-owned correctness/sanitization | Low factual risk; final normalization is tested |

The canonical order is hashtags, phone numbers, 911, decimal points, then whitespace. Changing the order can change output and therefore requires regression review.

## Weather forecast pronunciation and formatting

The pronunciation map is weather_ingest/wx_forecast.py:PRONUNCIATION_MAP and is applied by generate_wav_with_piper immediately before the logical shared-TTS call. Despite that historical function name, the selected provider may be Kokoro or Piper.

| Rule | Example input → output | Why / scope / provider dependence | Future classification | Meaning risk and coverage |
| --- | --- | --- | --- | --- |
| Special wind-zero phrase | “wind 0 to 10 mph” → “wend up to 10 mph” | Avoids an unnatural zero range and primes the following wind workaround; Weather forecast only; empirical provider rationale is not documented | voice-specific pronunciation candidate after listening review | Can change numeric meaning if matched too broadly; map application is covered in weather_ingest/tests/test_wx_forecast.py |
| Wind pronunciation | “Wind west” → “wend west” | Forces the noun pronunciation rather than “wind a clock”; Weather forecast only; historically phonemizer-oriented | engine-specific pronunciation candidate | Homograph risk if generalized; word-boundary behavior is tested |
| Minneapolis spelling | “Minneapolis” → “Minniapolis” | Pronunciation spelling for the station locality; Weather forecast only; provider-specific evidence is not recorded | voice-specific pronunciation candidate | Misspelling is intentional only in speech text; test protects transformed TTS input |
| Southwest hyphenation | “southwest” → “South-west” | Pronunciation pacing; Weather forecast only; provider-specific evidence is not recorded | voice-specific pronunciation candidate | Low factual risk but could sound worse on another engine; tested |
| Decimal spelling | “20.2 feet” → “20 point 2 feet” | Comment records Kokoro dropping “point” and pausing; Weather forecast layer, in addition to canonical preprocessing | engine-specific pronunciation candidate; deduplicate with canonical rule in future design | High measurement risk; digit-only anchoring is tested |
| First mph expansion | First “10 mph” → “10 miles per hour” | Makes the unit explicit once in a multi-period forecast | Weather-specific text treatment | Unit meaning must remain intact; weather_ingest/tests/test_wx_forecast.py protects first use |
| Later mph suppression | Later “15 mph” → “15” | Avoids repetitive “miles per hour” within one forecast | Weather-specific text treatment | Removing a unit can be ambiguous if periods are reordered; repetition behavior is tested |
| Period framing | NWS period plus detail → “Tonight: …” | speak_forecast labels each selected NWS period and joins it into one script | Weather-specific text treatment | Period labels are factual; forecast tests cover assembled output |
| Precipitation clause merge | Adjacent NWS rain/thunder clauses → one time-window summary | Reduces repetitive source prose while retaining the strongest precipitation label/likelihood/window | Weather-specific text treatment | Material factual risk; dedicated precipitation-merger tests cover merge and fail-open behavior |
| Rainfall amounts | 0.25 → “25 hundredths of an inch”; 1.25 → “1 and 25 hundredths of an inch” | Broadcast-friendly gauge totals from format_rainfall_inches | Weather-specific text treatment | High numeric/unit risk; rainfall formatter and announcement tests cover thresholds |

## Weather broadcast copy and alerts

| Rule or phrase | Example input → output | Why / scope / provider dependence | Future classification | Meaning risk and coverage |
| --- | --- | --- | --- | --- |
| Quick temperature line | 72 with equal apparent temperature → “It’s 72 degrees.” | current_temp.build_announcement; deliberately omits wind from the quick hit | station-editable broadcast copy/template, with computation code-owned | Temperature is factual; current-temp tests cover equal/different apparent temperature |
| Apparent-temperature clause | 72 / feels 68 → “It’s 72 degrees, but it feels like 68.” | Adds NWS heat-index/wind-chill result only when rounded values differ | Weather-specific text treatment | High factual risk; formula and phrase gate are tested |
| Quick alert lead-in | active summary → “Weather alert: …” | Separates current-temperature copy from warn.txt summary | station-editable broadcast copy/template | Alert meaning is high risk; summary dedupe/join and phrase are tested |
| Invalid-data sentence | invalid readings → “Weather data is incomplete or invalid.” | Refuses to invent a temperature | must remain code-owned correctness/sanitization | Safety/correctness critical; invalid input is tested |
| Full observation framing | sky/temp/humidity/wind/barometer → “Currently in Minneapolis …” | wx_forecast.build_announcement composes the station observation | station-editable broadcast copy/template around code-owned values | High numeric/directional risk; announcement fixture tests cover branches |
| Calm/light/gusting wind phrases | gust under 1 / under 3 / equal / greater → calm, light and variable, steady, or gusting copy | Deterministic meteorological phrasing | Weather-specific text treatment | Threshold changes alter meaning; branch tests protect it |
| Forecast introductions | 3-day → “Looking ahead over the next three days:” ; 1-day → “Looking ahead” | MODES feature framing | station-editable broadcast copy/template | Low factual risk except stated horizon; mode tests cover selection |
| Rainfall lead-ins | qualifying totals → “We’ve had … since midnight” and optional “Of that … past hour” | Adds daily/hourly gauge context only at 0.01 inch or more | Weather-specific text treatment | Numeric/time-window risk; announcement tests cover threshold and copy |
| Forecast signoff | final append → “For Oak Grove Radio ninety-eight point five, {persona signoff}” | Ensures alerts remain before the persona bow-out | station-editable broadcast copy/template | Station identity and order matter; assembly tests cover final position |
| NWS alert set lead-in/connectors | multiple cores → “The National Weather Service has issued … And, …” | Gives a sentence break and rotating connectors | Weather-specific text treatment | Connectors do not alter alert facts; alert-block tests cover singular/multiple order |
| AMBER-family lead-in/connectors | one/many → singular/plural lead-in, then rotating connectors | Distinguishes AMBER-family content before signoff | Weather-specific text treatment | Alert classification/count risk; amber-block tests cover forms |
| Active-alert summary cleanup | duplicate Flood Warning entries and terminal periods → one comma/conjunction list | current_temp.get_alert_summary avoids repeated flood labels and awkward punctuation | Weather-specific text treatment | Could collapse distinct floods; tests cover the exact current policy |
| NWS alert extraction/tidying | bullet/headline/narrative plus instruction → full urgent text and description-only recurring text | update_local_wx_data.build_watch_warning_text retains safety instructions for urgent clips but omits them from repeated forecasts | must remain code-owned correctness/sanitization | High safety/factual risk; weather_ingest alert-text tests cover source fallbacks, punctuation, times, and variants |
| Alert audio text | wx_alert and amber_alert speak stored text without an additional feature pronunciation map | Preserves the already-built alert wording; canonical TTS preprocessing still applies | must remain code-owned correctness/sanitization | High safety risk; atomic publication tests ensure partial sets never air |

Day/night sky wording in update_local_wx_data is meteorological solar-state language. It is unrelated to persona slot keys and should remain as weather terminology.

## Web Requests dedication speech

Source: webrequests/services.py:build_dedication_intro_text. Tests: webrequests/tests/test_dedication_intros.py.

| Rule or phrase | Example input → output | Why / scope / provider dependence | Future classification | Meaning risk and coverage |
| --- | --- | --- | --- | --- |
| Featured-artist expansion | “Song (feat. Guest)” → “Song (featuring Guest)” | Avoids “feat.” being spoken as “feet”; title/artist database values stay unchanged | music/dedication-specific normalization; possibly engine-specific after listening | Could change a literal use of “feat.”, so the period and word boundary are constrained; case/title/artist tests exist |
| Intro template | title/artist → “Now here’s TITLE by ARTIST” | Fixed spoken request framing | station-editable broadcast copy/template | Metadata must not be swapped; exact strings are tested |
| Dedication body whitespace | multi-line listener message → one spaced phrase | Prevents newline/pacing artifacts | music/dedication-specific normalization | User wording punctuation remains otherwise intact; whitespace tests exist |
| Sentence punctuation | missing terminal punctuation → append period | Produces a complete sentence before thanks copy | music/dedication-specific normalization | Low meaning risk; punctuation variants are tested |
| Requester thanks | name plus message → “Thanks NAME for your dedication.”; no message → “…request.” | Distinguishes dedication from request and omits the sentence for blank names | station-editable broadcast copy/template | User/name association matters; exact branch tests exist |

The active configuration is WebRequestConfig.dedication_tts_voice plus dedication_tts_timeout_seconds. A blank voice means no intro is attached; the song request can still air.

## Road Conditions / KDOT normalization

Source: road_conditions/text_normalize.py unless noted. Tests: road_conditions/tests/test_kandrive_text_normalize.py and test_kandrive_report.py.

| Rule | Example input → output | Why / scope / provider dependence | Future classification | Meaning risk and coverage |
| --- | --- | --- | --- | --- |
| Route expansion | I-70 / US 81 / KS 15 / K-15 → Interstate 70 / U.S. 81 / Kansas Highway 15 | Makes Kansas route designators speakable; anchored to a following number | Road/KDOT source-specific normalization | Wrong route is high risk; variants and nonmatches are tested |
| Direction abbreviations | NB/SB/EB/WB → northbound/southbound/eastbound/westbound | Expands whole-word roadway shorthand | Road/KDOT source-specific normalization | Direction is high risk; case, boundaries, and nonmatches are tested |
| Structured link direction | BOTH_DIRECTIONS → “in both directions”; N/NE/etc. → directional word; non-directional sentinels → blank | Maps controlled CARS enum values, never free text | must remain code-owned correctness/sanitization | High directional risk; every known enum and unknown fallback are tested |
| Continuous operation | 24/7 → “twenty four seven” | Avoids slash pronunciation | Road/KDOT source-specific normalization | Low risk in this anchored token; tested |
| Alphabetic slash | “Clay County Line/Dickinson County Line” → “… Line and Dickinson …” | KDOT uses slash between alternate boundary names | Road/KDOT source-specific normalization | “and” may overstate semantics if source usage changes; alphabetic-only and numeric nonmatch tests exist |
| Decimal measurements | 12.5 feet → “12 point 5 feet” | Comment records the same Kokoro decimal pause/drop issue as Weather | engine-specific pronunciation candidate, presently road-scoped | High measurement risk; tested |
| AM/PM spacing | 11:59PM → 11:59 PM | Prevents jammed time reading; leaves CDT/CST unchanged | Road/KDOT source-specific normalization | Time is factual; spacing and already-spaced cases are tested |
| Whitespace collapse | repeated spaces/newlines → one space | Stable spoken KDOT prose | must remain code-owned correctness/sanitization | Low risk; tested |
| Remove next-update administration | trailing “Next update time 2:45 PM CDT, 5/27/26.” → removed | Internal record-maintenance time, not road-event validity | Road/KDOT source-specific normalization | Could remove a useful time if source shape changes; exact anchoring and a real timing nonmatch are tested |
| Remove map instruction | “See map for detour(s).” → removed | Not actionable in radio audio; report supplies posted-detour copy | Road/KDOT source-specific normalization | Could omit route detail if source practice changes; exact marker/mid-sentence cases are tested |
| Remove More Info reference | trailing “More Info: …” → removed | Often broken/raw external reference; station postamble owns the listener pointer | Road/KDOT source-specific normalization | Removes arbitrary suffix by design; marker and unrelated “information” cases are tested |
| Near-duplicate sentence removal | KDOT auto text plus substantially repeated Comment sentence → one copy | Removes observed cumulative restatements; protects U.S. abbreviation while splitting | Road/KDOT source-specific normalization | Highest semantic risk in road cleanup; thresholds, short sentences, distinct facts, U.S. split, idempotence, and real-shaped examples are tested |
| Planned-work prefix | future planned event → “Beginning DATE, this is planned work:” | road_conditions/report.py labels future work from structured start time | station-editable broadcast copy/template around code-owned classification | High time/status risk; report tests cover planned/current branches |
| Structured lead-in | route/counties → “On ROUTE, in COUNTY, KDOT reports:” | Keeps source attribution and location ahead of normalized description | station-editable broadcast copy/template around code-owned data | Location/source are factual; report tests cover missing/present pieces |
| Detour sentence | structured detour present → “Motorists should use the posted detour.” | Replaces non-actionable map text with concise presence-only guidance | station-editable broadcast copy/template around code-owned detection | Could imply detour status; has_detour and report tests cover it |
| Report framing | configured preamble/postamble and {announcer_name}; no-events copy | Operator-owned RoadConditionsConfiguration text composed around event scripts | station-editable broadcast copy/template | Operator text can affect claims; model/form and report-composition tests cover tokens/order |

## Provenance and engine boundaries

The reason recorded in comments is strongest for Kokoro decimal handling and the original canonical helper. Other spelling workarounds have tests proving current output but no repository evidence that they benefit every Kokoro voice, and no evidence that they benefit Piper. The current shared TTS architecture deliberately passes only logical StationTTSVoice names; feature code should not branch on provider IDs.

No rule in this inventory should be exposed as an unrestricted admin regex. Any future editor needs bounded matching, preview/listen workflow, ordering, audit history, and protection against changing numbers, directions, dates, alert instructions, or other factual content.

## Likely future split

1. A pronunciation dictionary/rule layer with explicit global, engine, voice, locale, and feature scope. Engine-specific entries require separate Kokoro/Piper verification and deterministic precedence.
2. Feature-owned formatting/template configuration for station branding, intros, signoffs, and optional framing. Computed values and safety gates remain typed code inputs rather than template-authored facts.
3. Source-specific sanitization retained in code where correctness depends on KDOT/NWS schemas, anchored patterns, ordering, deduplication thresholds, or safety/factual review.

That split supports later Weather Setup and generated-announcement work without conflating operator copy, pronunciation tuning, and source-data correctness.
