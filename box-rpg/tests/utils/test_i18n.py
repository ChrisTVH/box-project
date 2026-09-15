from importlib.resources import files

import pytest

from box.cli.parser import build_parser
from box.utils.i18n import _, configure, ngettext


def test_i18n_loads_spanish_before_lower_priority_locales() -> None:
    configure({"LANGUAGE": "es", "LC_MESSAGES": "en", "LANG": "en_US.UTF-8"})

    assert _("Cleanup cancelled.") == "Limpieza cancelada."


def test_i18n_accepts_a_regional_spanish_locale() -> None:
    configure({"LC_MESSAGES": "es_ES.UTF-8"})

    assert _("Cleanup cancelled.") == "Limpieza cancelada."


def test_i18n_prioritizes_lc_all_over_lower_priority_locales() -> None:
    configure({"LC_ALL": "es_ES.UTF-8", "LC_MESSAGES": "en", "LANG": "en_US.UTF-8"})

    assert _("Cleanup cancelled.") == "Limpieza cancelada."


def test_i18n_falls_back_to_english_when_a_catalog_is_unavailable() -> None:
    configure({"LANG": "fr_FR.UTF-8"})

    assert _("Cleanup cancelled.") == "Cleanup cancelled."


def test_i18n_uses_spanish_plural_forms_and_preserves_placeholders() -> None:
    configure({"LANGUAGE": "es"})

    assert (
        ngettext(
            "Removed {removed} item; {failed} failed.",
            "Removed {removed} items; {failed} failed.",
            1,
        ).format(removed=1, failed=0)
        == "Se eliminó 1 elemento; fallos: 0."
    )
    assert (
        ngettext(
            "Removed {removed} item; {failed} failed.",
            "Removed {removed} items; {failed} failed.",
            2,
        ).format(removed=2, failed=1)
        == "Se eliminaron 2 elementos; fallos: 1."
    )


def test_i18n_translates_parser_help_after_configuration() -> None:
    configure({"LANGUAGE": "es"})

    assert "Inicia juegos de RPG Maker" in build_parser().format_help()


def test_i18n_translates_argparse_errors_after_configuration(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure({"LANGUAGE": "es"})

    with pytest.raises(SystemExit):
        build_parser().parse_args(["--desconocido"])

    assert "argumentos no reconocidos" in capsys.readouterr().err


def test_i18n_keeps_english_without_a_catalog_after_spanish() -> None:
    configure({"LANGUAGE": "es"})
    configure({"LANGUAGE": "en"})

    assert _("Cleanup cancelled.") == "Cleanup cancelled."


def test_i18n_includes_the_compiled_spanish_catalog_as_package_data() -> None:
    catalog = files("box").joinpath("locale/es/LC_MESSAGES/box.mo")

    assert catalog.is_file()
