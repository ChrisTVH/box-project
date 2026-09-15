# Contributing translations

This guide shows how to add or edit interface translations for `box-rpg`.

`box-rpg` uses gettext. Runtime strings are written in English in the source as msgids with the `_()` helper, then loaded from compiled catalogs based on the system locale. English is the source and fallback. Other languages live in `box-rpg/src/box/locale/`.

## How it works

- `box-rpg/src/box/utils/i18n.py` loads the catalog for the current locale from `$LANGUAGE`, `$LC_ALL`, `$LC_MESSAGES`, or `$LANG`, with English fallback. It exposes `_()` and `ngettext()`.
- `box-rpg/src/box/locale/<lang>/LC_MESSAGES/box.po` is the human-readable translation per language.
- `box-rpg/src/box/locale/<lang>/LC_MESSAGES/box.mo` is the compiled catalog used at runtime.
- `box-rpg/pyproject.toml` ships `locale/**/*.mo` as package data.

Singular and plural pairs use `ngettext`:

```python
ngettext(
    "Removed {removed} item; {failed} failed.",
    "Removed {removed} items; {failed} failed.",
    removed,
)
```

## Add a new language

1. Create the catalog directory:

   ```bash
   mkdir -p box-rpg/src/box/locale/<lang>/LC_MESSAGES
   ```

2. Generate the template from the sources. This needs `xgettext`:

   ```bash
   xgettext --from-code=UTF-8 --language=Python \
     --keyword=_ --keyword=ngettext:1,2 \
     --output=box-rpg/src/box/locale/<lang>/LC_MESSAGES/box.po \
      install.py $(find box-rpg/src/box -name '*.py')
   ```

3. Fill in the `msgstr` entries. Set the `Language:` and `Plural-Forms:` header fields for your language.

4. Compile the catalog. This needs `msgfmt`:

   ```bash
   msgfmt --check --verbose \
     -o box-rpg/src/box/locale/<lang>/LC_MESSAGES/box.mo \
     box-rpg/src/box/locale/<lang>/LC_MESSAGES/box.po
   ```

5. Make sure both `.po` and `.mo` files are tracked by git, following the locale exceptions in `.gitignore`.

6. Add tests in `box-rpg/tests/utils/test_i18n.py` covering fallback, plural forms, and placeholders.

## Edit an existing language

Edit `box.po` directly, then recompile:

```bash
msgfmt --check --verbose -o box-rpg/src/box/locale/es/LC_MESSAGES/box.mo box-rpg/src/box/locale/es/LC_MESSAGES/box.po
```

To find missing translations, regenerate the template and list untranslated and fuzzy entries. Both commands should print nothing when the catalog is complete:

```bash
xgettext --from-code=UTF-8 --language=Python \
  --keyword=_ --keyword=ngettext:1,2 \
  --output=/tmp/box.pot install.py $(find box-rpg/src/box -name '*.py')
msgattrib --untranslated --only-file=/tmp/box.pot box-rpg/src/box/locale/es/LC_MESSAGES/box.po
msgattrib --only-fuzzy --only-file=/tmp/box.pot box-rpg/src/box/locale/es/LC_MESSAGES/box.po
```

## Rules for translators

- Keep `{placeholder}` markers intact. Copy them exactly. They may be reordered if your language needs it.
- Keep shortcut keys and literal confirmation tokens unchanged, such as `[y/N]`, `DELETE`, and `DELETE ALL`. Translate the surrounding text.
- Do not translate program and product names, command names, options, selectors, configuration keys, paths, or file names, including but not limited to `box-rpg`, `box-rpg-maker`, RPG Maker, NW.js, EasyRPG Player, Chromium, X11, and SDK.
- Match the `Plural-Forms:` header of your language. Spanish uses `plural=(n != 1)` with `msgstr[0]` and `msgstr[1]`.
- Structured output stays untranslated. Keep JSON and JSON Lines keys and selector values unchanged. Shell completions and product documentation stay in English.

## Test your changes

Run the full suite. Tests are pinned to English, so the catalog does not affect them:

```bash
PYTHONPATH=box-rpg/src python -m pytest box-rpg/tests/
```

Check runtime output in your language:

```bash
LANGUAGE=es box-rpg --help
LANGUAGE=es box-rpg cleanup
```

## Notes

Product documentation such as `docs/box-rpg/manual.md`, `security.md`, `development.md`, and example configuration comments is not translated. After adding or changing a `_()` or `ngettext()` string in the code, regenerate the template, merge new entries into each catalog (`msgmerge --update`), translate them, and recompile before committing.
