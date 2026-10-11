"""git status parsing: the bug that deleted a user's file came from trimming the porcelain output."""

from droid_monkey.tools.git import parse_porcelain_z


def test_leading_space_status_keeps_the_full_path():
    out = " M app/src/main/Foo.kt\0?? monkey.yaml\0?? app/src/test/FooTest.kt\0"
    assert parse_porcelain_z(out) == ["app/src/main/Foo.kt", "monkey.yaml", "app/src/test/FooTest.kt"]


def test_renames_skip_the_old_path_and_spaces_in_names_survive():
    out = "R  app/src/New Name.kt\0app/src/Old.kt\0A  app/src/With Space.kt\0"
    assert parse_porcelain_z(out) == ["app/src/New Name.kt", "app/src/With Space.kt"]
