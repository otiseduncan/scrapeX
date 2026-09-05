from scrapex.navigator_observation import bounded_text, parse_aria_snapshot

SAMPLE = """- generic [active] [ref=e1]:
  - heading "Vehicle Select" [level=1] [ref=e2]
  - textbox "Vehicle search" [ref=e3]
  - button "Search" [ref=e4]
  - list
  - iframe [ref=e5]:
    - generic [ref=f1e1]:
      - generic [ref=f1e2]: 2023 Toyota Camry
      - heading "Blind Spot Monitor Beam Axis Calibration Procedure" [level=1] [ref=f1e3]
      - list [ref=f1e4]:
        - listitem [ref=f1e5]: Position the vehicle on a level surface.
"""


def test_parses_quoted_names_and_refs():
    nodes = parse_aria_snapshot(SAMPLE)
    by_ref = {n.ref: n for n in nodes}
    assert by_ref["e2"].role == "heading"
    assert by_ref["e2"].name == "Vehicle Select"
    assert by_ref["e4"].role == "button"
    assert by_ref["e4"].name == "Search"


def test_lines_without_ref_are_skipped():
    nodes = parse_aria_snapshot(SAMPLE)
    # "- list" (no ref) must not appear at all.
    assert not any(n.role == "list" and n.ref == "" for n in nodes)


def test_iframe_content_gets_frame_prefixed_refs():
    nodes = parse_aria_snapshot(SAMPLE)
    by_ref = {n.ref: n for n in nodes}
    assert "f1e3" in by_ref
    assert by_ref["f1e3"].name == "Blind Spot Monitor Beam Axis Calibration Procedure"


def test_generic_node_with_trailing_inline_text_uses_it_as_name():
    nodes = parse_aria_snapshot(SAMPLE)
    by_ref = {n.ref: n for n in nodes}
    assert by_ref["f1e2"].name == "2023 Toyota Camry"
    assert by_ref["f1e5"].name == "Position the vehicle on a level surface."


def test_depth_reflects_indentation():
    nodes = parse_aria_snapshot(SAMPLE)
    by_ref = {n.ref: n for n in nodes}
    assert by_ref["e2"].depth < by_ref["f1e3"].depth


def test_empty_and_malformed_input_produce_no_elements():
    assert parse_aria_snapshot("") == []
    assert parse_aria_snapshot("not a snapshot at all") == []
    assert parse_aria_snapshot(None) == []


def test_max_elements_bounds_output():
    lines = "\n".join(f'- link "item {i}" [ref=e{i}]' for i in range(10))
    nodes = parse_aria_snapshot(lines, max_elements=3)
    assert len(nodes) == 3


def test_bounded_text_collapses_whitespace_and_truncates():
    assert bounded_text("  a   b\n\nc  ") == "a b c"
    assert len(bounded_text("x" * 10_000, max_chars=100)) == 100


def test_icon_font_glyphs_are_filtered_out():
    # Confirmed live against ALLDATA: toolbar icons parse to a name that is
    # a single private-use-area codepoint (U+E000-U+F8FF) -- pure icon-font
    # noise with no semantic value, and no ref a model could sensibly act
    # on by name. These must not crowd out real, actionable elements.
    icon_only = chr(0xE63B)
    icon_with_space = " " + chr(0xE854) + " "
    lines = "\n".join([
        f'- generic [ref=e1]: {icon_only}',
        f'- generic [ref=e2]: {icon_with_space}',
        '- button "Search" [ref=e3]',
    ])
    nodes = parse_aria_snapshot(lines)
    refs = {n.ref for n in nodes}
    assert refs == {"e3"}


def test_a_name_mixing_real_text_and_an_icon_glyph_is_kept():
    mixed = "Search" + chr(0xE204)
    nodes = parse_aria_snapshot(f'- button "{mixed}" [ref=e1]')
    assert nodes[0].name == mixed
