# Contributing translations (box-gui)

This guide shows how to add or edit interface translations for `box-gui`.

`box-gui` uses gettext. Runtime strings are written in English in the source as msgids with the `_()` helper, then loaded from compiled catalogs based on the system locale. English is the source and fallback. Other languages live in `box-gui/src/box_gui/locale/`. Wrap every user-facing string from day one. Never add an unwrapped literal to translate later.

## How it works

- `box-gui/src/box_gui/i18n.py` loads the catalog for the current locale from `$LANGUAGE`, `$LC_ALL`, `$LC_MESSAGES`, or `$LANG`, with English fallback. It exposes `_()` and `ngettext()`. Startup in `app.py` calls this frontend `configure()` and then `box.utils.i18n.configure()` so backend strings shown by the UI follow the same locale.
- `box-gui/src/box_gui/locale/<lang>/LC_MESSAGES/box-rpg-maker.po` is the human-readable translation per language.
- `box-gui/src/box_gui/locale/<lang>/LC_MESSAGES/box-rpg-maker.mo` is the compiled catalog used at runtime.
- `box-gui/pyproject.toml` ships `locale/**/*.mo` as package data.

Singular and plural pairs use `ngettext`:

```python
ngettext(
    "{count} item in {category}.",
    "{count} items in {category}.",
    len(items),
)
```

## Add a new language

1. Create the catalog directory:

   ```bash
   mkdir -p box-gui/src/box_gui/locale/<lang>/LC_MESSAGES
   ```

2. Generate the template from the frontend sources. This needs `xgettext`:

   ```bash
   xgettext --from-code=UTF-8 --language=Python \
     --keyword=_ --keyword=ngettext:1,2 \
     --output=box-gui/src/box_gui/locale/<lang>/LC_MESSAGES/box-rpg-maker.po \
     $(find box-gui/src/box_gui -name '*.py')
   ```

3. Fill in the `msgstr` entries. Set `Language:` and `Plural-Forms:` for your language. Spanish uses `Language: es` and `Plural-Forms: nplurals=2; plural=(n != 1);`.

4. Compile the catalog. This needs `msgfmt`:

   ```bash
   msgfmt --check --verbose \
     -o box-gui/src/box_gui/locale/<lang>/LC_MESSAGES/box-rpg-maker.mo \
     box-gui/src/box_gui/locale/<lang>/LC_MESSAGES/box-rpg-maker.po
   ```

5. Make sure both `.po` and `.mo` files are tracked by git.

6. Add tests in `box-gui/tests/test_i18n_app.py` covering fallback, plural forms, and placeholders.

## Edit an existing language

Edit `box-rpg-maker.po` directly, then recompile:

```bash
msgfmt --check --verbose -o box-gui/src/box_gui/locale/es/LC_MESSAGES/box-rpg-maker.mo box-gui/src/box_gui/locale/es/LC_MESSAGES/box-rpg-maker.po
```

To find missing translations, regenerate the template and list untranslated and fuzzy entries:

```bash
xgettext --from-code=UTF-8 --language=Python \
  --keyword=_ --keyword=ngettext:1,2 \
  --output=/tmp/box-rpg-maker.pot \
  $(find box-gui/src/box_gui -name '*.py')
msgattrib --untranslated --only-file=/tmp/box-rpg-maker.pot box-gui/src/box_gui/locale/es/LC_MESSAGES/box-rpg-maker.po
msgattrib --only-fuzzy --only-file=/tmp/box-rpg-maker.pot box-gui/src/box_gui/locale/es/LC_MESSAGES/box-rpg-maker.po
```

Both commands should print nothing when the catalog is complete. The Spanish catalog keeps one legacy entry without a current source call site because `test_i18n_app.py` still covers it. Keep it until the test goes away.

## Rules for translators

- Keep `{placeholder}` markers intact. Copy them exactly. They may be reordered if your language needs it.
- Keep literal tokens unchanged and translate the surrounding text.
- Do not translate program and product names, command names, options, selectors, configuration keys, paths, or file names, including but not limited to `box-rpg-maker`, `box-rpg`, RPG Maker, NW.js, EasyRPG Player, Chromium, X11, and SDK.
- Match the `Plural-Forms:` header of your language. Spanish uses `plural=(n != 1)` with `msgstr[0]` and `msgstr[1]`.
- Structured data stays untranslated. Keep `library.json` keys and stable selector values unchanged. Product documentation stays in English.

## Test your changes

Run the pinned-English suite. The catalog does not affect it:

```bash
PYTHONPATH=box-gui/src python -m pytest box-gui/tests/
```

Check runtime output in your language:

```bash
LANGUAGE=es box-rpg-maker
```

`test_i18n_app.py` pins the key Spanish strings, including plurals and placeholders. Keep it green when editing the catalog.

## Notes

`docs/box-gui/development.md` and this file are not translated. After adding or changing a `_()` or `ngettext()` string in `src/box_gui`, regenerate the template and update the catalogs before committing. The Settings data page shows as `Datos` in Spanish while its internal page name stays untranslated, and the same display-versus-identifier split applies wherever a mockup shows a localized label.
