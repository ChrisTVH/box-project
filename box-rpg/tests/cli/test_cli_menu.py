from box.cli.menu import MenuSelection, choose_paged


def test_choose_paged_selects_an_item_on_the_next_page() -> None:
    choices = iter(("n", "1"))
    output: list[str] = []

    selection = choose_paged(
        "Entries",
        tuple(range(11)),
        str,
        read=lambda _: next(choices),
        write=output.append,
    )

    assert selection == MenuSelection(item=10)
    assert any("page 2/2" in line for line in output)


def test_choose_paged_can_select_all_or_cancel() -> None:
    all_selection = choose_paged("Entries", ("one",), str, allow_all=True, read=lambda _: "a")

    def end_of_input(_: str) -> str:
        raise EOFError

    cancelled = choose_paged("Entries", ("one",), str, read=end_of_input)

    assert all_selection == MenuSelection(select_all=True)
    assert cancelled is None
