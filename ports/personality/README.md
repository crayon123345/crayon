# /ports/personality - user-selected speaking style, no capability claims

Companion-trait build (Muse "user-selected speaking style" + Dots
"personalized appearance WITHOUT confusing personality with
capability"): the owner picks a tone preset or writes a custom style
card; the module stores it and renders a style profile the integration
passes to the model layer. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (14/14 tests) but NOT wired into the
running app. Named limits, exactly:

1. STYLE IS NOT CAPABILITY: style text claiming abilities ("you can
   make calls", "I can book flights", "always-on", "I have access
   to...") is REJECTED at write time against an explicit marker list
   (tested, 500-injection probe). Personality is tone, never a promise
   of ability - and the rendered profile says so in its note field.
2. OWNER-EXPLICIT ONLY: style changes only on explicit owner commands;
   nothing infers or adapts style from behavior.
3. RENDERING IS PURE: profile() returns the same dict for the same
   stored state (1000-render probe). No clocks, no randomness.
4. PRESETS ARE CLOSED: concise / warm / playful / formal. Unknown
   presets refuse with zero effects.
5. BOUNDS REFUSE: 12 traits max, 60 chars per trait, 400 total chars,
   40-char name. Overflow refuses and leaves the existing profile
   untouched.
6. OWNER-STRICT: one profile per owner; a second owner's writes and
   clears have zero effect on the first.
7. NO PROFILE = DEFAULT VOICE: profile() returns None and the app's
   existing voice is unchanged. This port adds NOTHING by default.
8. NOT MOUNTED: the module never touches the model; the integration
   applies the profile. No behavior change to mounted ports.
9. STORAGE IS INJECTED, NOT DURABLE: MemPersonalityStore is in-memory;
   the SQL adapter is integration work.

## Chat surface (explicit grammar)

- `my style`
- `set style <concise|warm|playful|formal>`
- `set custom style <name>: <trait, trait, ...>`
- `clear my style` / `export my style`

Anything else falls through.

## Version

crayon-personality-v1
