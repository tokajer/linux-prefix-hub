"""The dependency-free YAML subset.

CI runs the suite once without optional extras, so these tests must exercise
`_loads_lite` directly -- otherwise PyYAML silently answers for it and the
hand-written parser stays unverified.
"""
from __future__ import annotations

from linux_prefix_hub.core import yamlite

LUTRIS_SHAPED = """\
# a Lutris game config
game:
  exe: /mnt/games/game.exe
  prefix: /home/u/.wine
  arch: win64
system:
  env:
    DXVK_HUD: fps
  disable_runtime: false
wine:
  version: 'proton-ge'
  fsync: true
"""


def test_nested_mappings_and_scalars():
    data = yamlite._loads_lite(LUTRIS_SHAPED)
    assert data["game"]["exe"] == "/mnt/games/game.exe"
    assert data["game"]["arch"] == "win64"
    assert data["system"]["env"]["DXVK_HUD"] == "fps"
    assert data["system"]["disable_runtime"] is False
    assert data["wine"]["version"] == "proton-ge"   # quotes stripped
    assert data["wine"]["fsync"] is True


def test_lists_win_over_the_undecided_mapping():
    data = yamlite._loads_lite(
        "game:\n"
        "  args:\n"
        "    - --windowed\n"
        "    - --no-intro\n"
        "  exe: /x/y.exe\n")
    assert data["game"]["args"] == ["--windowed", "--no-intro"]
    assert data["game"]["exe"] == "/x/y.exe"


def test_empty_key_stays_a_mapping_when_nothing_follows():
    data = yamlite._loads_lite("system:\n  env:\n")
    assert data["system"]["env"] == {}


def test_scalar_types():
    data = yamlite._loads_lite(
        "a: 42\nb: 1.5\nc: null\nd: ~\ne: yes\nf: off\ng:\n")
    assert data["a"] == 42
    assert data["b"] == 1.5
    assert data["c"] is None and data["d"] is None
    assert data["e"] is True and data["f"] is False
    assert data["g"] == {}


def test_comments_dropped_but_not_inside_quotes():
    data = yamlite._loads_lite(
        "exe: /x/y.exe  # trailing comment\n"
        "args: '-flag #1'\n"
        "# whole line\n"
        "keep: 1\n")
    assert data["exe"] == "/x/y.exe"
    assert data["args"] == "-flag #1"
    assert data["keep"] == 1


def test_dedent_returns_to_the_right_parent():
    data = yamlite._loads_lite(
        "game:\n  exe: a\nsystem:\n  env:\n    K: v\nwine:\n  fsync: true\n")
    assert set(data) == {"game", "system", "wine"}
    assert data["system"]["env"] == {"K": "v"}
    assert data["wine"]["fsync"] is True


def test_garbage_never_raises():
    for junk in ("", "   \n\n", "]]] not yaml", "- orphan list item\n",
                 "key without colon\n", ":\n", "a: b: c\n"):
        assert isinstance(yamlite._loads_lite(junk), dict)


def test_loads_falls_back_when_pyyaml_returns_a_non_mapping():
    # A YAML document that is a list, not a mapping: callers expect a dict.
    assert yamlite.loads("- a\n- b\n") == {}
