# Spanish localization guidelines

- Translate only runtime UI strings extracted from Python source.
- Preserve commands, options, selectors, JSON keys, configuration keys, product names, file names, paths, placeholders, and literal confirmation tokens.
- Keep menu shortcuts and accepted input tokens unchanged unless the implementation changes them together.
- Use concise neutral Spanish and retain the original punctuation and whitespace structure where meaningful.
- Do not translate documentation, shell completions, or TOML comments.
- Use `glossary.md` as the source of truth for terminology; if a msgid contains a glossary term, its msgstr must use the matching Spanish term (adjust word order/agreement as Spanish grammar requires).
- For `ngettext()` entries, fill both `msgstr[0]` (singular) and `msgstr[1]` (plural) per the `plural=(n != 1)` header; never leave one empty.