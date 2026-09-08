# Contributing translations

`box-rpg` uses **gettext** for internationalization: runtime user-facing strings are wrapped with the `_()` helper and loaded from compiled catalogs based on the system locale. English is the source language (the msgids) and the default fallback; additional languages live in `src/box/locale/`.

## How it works

- `src/box/utils/i18n.py` — loads the catalog for the current locale (`$LANGUAGE` / `$LC_ALL` / `$LC_MESSAGES` / `$LANG`, with English fallback) and exposes `_()` and `ngettext()`.
- `src/box/locale/<lang>/LC_MESSAGES/box.po` — the human-readable translation file per language.
- `src/box/locale/<lang>/LC_MESSAGES/box.mo` — the compiled catalog used at runtime.
- `pyproject.toml` ships `locale/**/*.mo` as package data.

The source strings are English msgids, e.g. `_("Cleanup cancelled.")`. Singular/plural pairs use `ngettext(singular, plural, n)`, e.g.:

```python
ngettext(
    "Removed {removed} item; {failed} failed.",
    "Removed {removed} items; {failed} failed.",
    removed,
)
```

## Adding a new language

1. Create the catalog directory:

   ```bash
   mkdir -p src/box/locale/<lang>/LC_MESSAGES
   ```

2. Generate the template from the source code (requires `xgettext`):

   ```bash
   xgettext --from-code=UTF-8 --language=Python \
     --keyword=_ --keyword=ngettext:1,2 \
     --output=src/box/locale/<lang>/LC_MESSAGES/box.po \
      install.py $(find src/box -name '*.py')
   ```

3. Fill in the `msgstr` entries for each `msgid`. Set the `Language:` and `Plural-Forms:` header fields for your language.

4. Compile the catalog (requires `msgfmt`):

   ```bash
   msgfmt --check --verbose \
     -o src/box/locale/<lang>/LC_MESSAGES/box.mo \
     src/box/locale/<lang>/LC_MESSAGES/box.po
   ```

5. Make sure the `.po` and `.mo` files are tracked by git (see the locale exceptions in `.gitignore`).

6. Add tests in `tests/test_i18n.py` covering the new language (fallback, plural forms, placeholders).

## Editing an existing language

Edit `box.po` directly, then recompile:

```bash
msgfmt --check --verbose -o src/box/locale/es/LC_MESSAGES/box.mo src/box/locale/es/LC_MESSAGES/box.po
```

To check that no messages are missing or untranslated:

```bash
# Regenerate the current template
xgettext --from-code=UTF-8 --language=Python \
  --keyword=_ --keyword=ngettext:1,2 \
  --output=/tmp/box.pot install.py $(find src/box -name '*.py')

# List untranslated entries
msgattrib --untranslated --only-file=/tmp/box.pot src/box/locale/es/LC_MESSAGES/box.po
```

`msgattrib` should print nothing (0 `msgid` lines) when the catalog is complete.

## Rules for translators

- **Keep placeholders intact.** Strings interpolated at runtime keep `{placeholder}` markers. Copy them exactly into the translation (they may be reordered if the target language requires it). Example: `_("Ignoring unknown argument: {extra}")` → `"Ignorando el argumento desconocido: {extra}"`.
- **Keep prompts and shortcuts unchanged.** Preserve the shortcut keys and literal confirmation tokens present in each prompt, such as `[y/N]`, `[n]`, `[p]`, `[q]`, `[a]`, `DELETE`, and `DELETE ALL`; translate the surrounding text.
- **Do not translate program/tool names.** Keep `box-rpg`, RPG Maker, NW.js, EasyRPG Player, Chromium, command names, options, selectors, configuration keys, paths, and file names unchanged.
- **Plural forms** must match the `Plural-Forms:` header of your language (Spanish uses `plural=(n != 1)`). Provide `msgstr[0]` and `msgstr[1]` (and more if your language needs them).
- **Structured output is not translated.** Keep JSON/JSON Lines keys and values used as stable selectors unchanged. Shell completions and product documentation remain in English.

## Testing your changes

Run the full suite (tests are pinned to English, so the catalog does not affect them):

```bash
python -m pytest
```

Check the runtime output in your language:

```bash
LANGUAGE=es box-rpg --help
LANGUAGE=es box-rpg cleanup
```

## Notes

- `docs/manual.md` and the example configuration comments are **not** translated (they are product documentation, not runtime UI).
- Keep `box.po` in sync with the source: after adding or changing a `_()` / `ngettext()` string in the code, regenerate the template and update the catalogs before committing.
