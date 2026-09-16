"""Gettext coverage for the box-rpg-maker frontend domain."""

from __future__ import annotations

from importlib.resources import files

from box_gui.i18n import _, configure, ngettext


def test_i18n_loads_spanish_before_lower_priority_locales() -> None:
    configure({"LANGUAGE": "es", "LC_MESSAGES": "en", "LANG": "en_US.UTF-8"})

    assert _("Settings") == "Ajustes"


def test_i18n_accepts_a_regional_spanish_locale() -> None:
    configure({"LC_MESSAGES": "es_ES.UTF-8"})

    assert _("Settings") == "Ajustes"


def test_i18n_prioritizes_lc_all_over_lower_priority_locales() -> None:
    configure({"LC_ALL": "es_ES.UTF-8", "LC_MESSAGES": "en", "LANG": "en_US.UTF-8"})

    assert _("Data") == "Datos"


def test_i18n_falls_back_to_english_when_a_catalog_is_unavailable() -> None:
    configure({"LANG": "fr_FR.UTF-8"})

    assert _("Settings") == "Settings"


def test_i18n_translates_core_chrome() -> None:
    configure({"LANGUAGE": "es"})

    assert _("Library") == "Biblioteca"
    assert _("Game Detail") == "Detalle del juego"
    assert _("Display name") == "Nombre visible"
    assert _("Game info") == "Información del juego"
    assert _("Runtime") == "Entorno de ejecución"
    assert (
        _(
            "By default it uses the version defined in Settings; "
            "otherwise the most recently downloaded one."
        )
        == "Por defecto usa la versión definida en Ajustes; si no hay ninguna, "
        "la más reciente descargada."
    )
    assert _("Additional files") == "Archivos adicionales"
    assert (
        _(
            "Some games keep language or settings files in the game root. "
            "Selected files are copied alongside the game on launch."
        )
        == "Algunos juegos guardan sus archivos de idioma o de configuración en la raíz del juego. "
        "Los seleccionados se copian junto al juego al iniciar."
    )
    assert _("Sandbox permissions") == "Permisos de la caja"
    assert (
        _("Permissions apply when mounting the game in the sandbox.")
        == "Los permisos se aplican al montar el juego en la caja."
    )
    assert _("Allow network usage") == "Permitir uso de red"
    assert _("Allow modifying the game") == "Permitir modificar en el juego"
    assert _("For games with automatic updates.") == "Para juegos con actualizaciones automáticas."
    assert _("Allow X11 or XWayland sessions") == "Permitir sesiones de X11 o XWayland"
    assert _("Diagnose") == "Diagnosticar"
    assert _("Launch") == "Iniciar"
    assert _("Allowed game roots") == "Raíces de juego permitidas"
    assert _("Preferred runtime") == "Entorno de ejecución preferido"
    assert _("Preferred NW.js runtime") == "Entorno NW.js preferido"
    assert _("Preferred EasyRPG runtime") == "Entorno EasyRPG preferido"
    assert _("Undefined") == "No definido"
    assert _("Default") == "Por defecto"
    assert _("Game profiles") == "Perfiles de juego"
    assert _("Unknown game") == "Juego desconocido"
    assert (
        _("Additional runtime settings or cache are stored here.")
        == "Se guardan configuraciones adicionales o cache de los entornos."
    )


def test_i18n_uses_spanish_plural_forms_and_preserves_placeholders() -> None:
    configure({"LANGUAGE": "es"})

    assert (
        ngettext(
            "Removed {removed} game; {failed} failed.",
            "Removed {removed} games; {failed} failed.",
            1,
        ).format(removed=1, failed=0)
        == "Se eliminó 1 juego; fallos: 0."
    )
    assert (
        ngettext(
            "Removed {removed} game; {failed} failed.",
            "Removed {removed} games; {failed} failed.",
            2,
        ).format(removed=2, failed=1)
        == "Se eliminaron 2 juegos; fallos: 1."
    )


def test_i18n_keeps_english_without_a_catalog_after_spanish() -> None:
    configure({"LANGUAGE": "es"})
    configure({"LANGUAGE": "en"})

    assert _("Settings") == "Settings"


def test_i18n_includes_the_compiled_spanish_catalog_as_package_data() -> None:
    catalog = files("box_gui").joinpath("locale/es/LC_MESSAGES/box-rpg-maker.mo")

    assert catalog.is_file()
