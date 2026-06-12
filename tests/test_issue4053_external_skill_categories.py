import pathlib

from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


def _write_skill(root: pathlib.Path, *parts: str) -> pathlib.Path:
    skill_dir = root.joinpath(*parts)
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_md = skill_dir / "SKILL.md"
    skill_md.write_text(
        "---\nname: test-skill\ndescription: test description\n---\n",
        encoding="utf-8",
    )
    return skill_md


def test_external_skill_categories_keep_local_flat_and_label_external_roots(tmp_path):
    from api import routes

    local_root = tmp_path / "skills"
    external_flat_root = tmp_path / "partner-skills"
    external_nested_root = tmp_path / "external-catalog"

    local_skill = _write_skill(local_root, "local-flat-skill")
    flat_external_skill = _write_skill(external_flat_root, "external-flat-skill")
    nested_external_skill = _write_skill(
        external_nested_root,
        "ops",
        "external-nested-skill",
    )

    search_dirs = [local_root, external_flat_root, external_nested_root]

    assert routes._skill_category_from_path(local_skill, search_dirs) is None
    assert (
        routes._skill_category_from_path(flat_external_skill, search_dirs)
        == external_flat_root.name
    )
    assert routes._skill_category_from_path(nested_external_skill, search_dirs) == "ops"
