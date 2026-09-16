# Spanish localization guidelines

These rules are global for both projects: the `box-rpg` backend catalog (`box` domain) and the `box-gui` frontend catalog (`box-rpg-maker` domain). Project-specific commands live in each [backend](docs/box-rpg/translations.md) and [frontend](docs/box-gui/translations.md) translation guide.

## What to translate

Translate only runtime UI strings extracted from Python source (`_()` and `ngettext()` msgids).

## What never to translate

Preserve commands, options, selectors, JSON and JSON Lines keys, selector values, configuration keys, product names, file names, paths, placeholders, and literal confirmation tokens. Keep menu shortcuts and accepted input tokens unchanged unless the implementation changes them together. Visible titles and labels do translate. Structured output keys differ per project (CLI selectors vs `library.json`) but stay untranslated in both. Do not translate documentation, shell completions, or TOML comments.

## Language

Use concise neutral Spanish and retain the original punctuation and whitespace structure where meaningful.

## Glossary binding

Use `glossary.md` as the source of truth for terminology. If a msgid contains a glossary term, its msgstr must use the matching Spanish term, adjusting word order and agreement as Spanish grammar requires.

## Plurals

For `ngettext()` entries, fill both `msgstr[0]` (singular) and `msgstr[1]` (plural) per the `plural=(n != 1)` header; never leave one empty.
