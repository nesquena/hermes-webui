import json
import os
import pathlib
import shutil
import urllib.error
import urllib.parse
import urllib.request

from tests._pytest_port import BASE
from tests.conftest import requires_agent_modules

pytestmark = requires_agent_modules


def _state_dir() -> pathlib.Path:
    return pathlib.Path(os.environ["HERMES_WEBUI_TEST_STATE_DIR"])


def _remove_path(path: pathlib.Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


class _IsolatedSkillsDirs:
    def __init__(self, profile: str):
        self.profile = profile
        self.state = _state_dir()
        self.root_skills = self.state / "skills"
        self.profile_home = self.state / "profiles" / profile
        self.profile_skills = self.profile_home / "skills"
        self._root_was_symlink = False
        self._root_symlink_target = None

    def __enter__(self):
        self._root_was_symlink = self.root_skills.is_symlink()
        if self._root_was_symlink:
            self._root_symlink_target = self.root_skills.resolve()
        _remove_path(self.root_skills)
        _remove_path(self.profile_home)
        self.root_skills.mkdir(parents=True, exist_ok=True)
        self.profile_skills.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type, exc, tb):
        _remove_path(self.profile_home)
        _remove_path(self.root_skills)
        if self._root_was_symlink and self._root_symlink_target is not None:
            self.root_skills.symlink_to(self._root_symlink_target)


def _write_skill(skills_dir: pathlib.Path, name: str, description: str, body: str) -> pathlib.Path:
    skill_dir = skills_dir / name
    (skill_dir / "references").mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "note.md").write_text(
        f"linked file for {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def _get(path: str, *, profile: str | None = None):
    headers = {}
    if profile:
        headers["Cookie"] = f"hermes_profile={profile}"
    req = urllib.request.Request(BASE + path, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def _post(path: str, body: dict, *, profile: str | None = None):
    headers = {"Content-Type": "application/json"}
    if profile:
        headers["Cookie"] = f"hermes_profile={profile}"
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read()), resp.status
    except urllib.error.HTTPError as exc:
        return json.loads(exc.read()), exc.code


def test_api_skills_list_and_content_respect_profile_cookie():
    profile = "skills1880"
    with _IsolatedSkillsDirs(profile) as dirs:
        _write_skill(
            dirs.root_skills,
            "root-only-skill-1880",
            "Root profile skill",
            "This skill belongs to the root profile.",
        )
        _write_skill(
            dirs.profile_skills,
            "profile-only-skill-1880",
            "Secondary profile skill",
            "This skill belongs to the selected browser profile.",
        )

        data, status = _get("/api/skills", profile=profile)

        assert status == 200
        names = {skill.get("name") for skill in data.get("skills", [])}
        assert "profile-only-skill-1880" in names
        assert "root-only-skill-1880" not in names

        root_data, root_status = _get("/api/skills")
        assert root_status == 200
        root_names = {skill.get("name") for skill in root_data.get("skills", [])}
        assert "root-only-skill-1880" in root_names
        assert "profile-only-skill-1880" not in root_names

        detail, detail_status = _get(
            "/api/skills/content?name=profile-only-skill-1880",
            profile=profile,
        )
        assert detail_status == 200
        assert detail.get("name") == "profile-only-skill-1880"
        assert "selected browser profile" in detail.get("content", "")

        linked_path = urllib.parse.quote("references/note.md", safe="")
        linked, linked_status = _get(
            f"/api/skills/content?name=profile-only-skill-1880&file={linked_path}",
            profile=profile,
        )
        assert linked_status == 200
        assert linked.get("content") == "linked file for profile-only-skill-1880\n"


def test_skill_save_and_delete_respect_profile_cookie():
    profile = "skills1880save"
    with _IsolatedSkillsDirs(profile) as dirs:
        content = "---\nname: profile-saved-skill-1880\ndescription: Saved profile skill\n---\n\n# Saved\n"

        saved, save_status = _post(
            "/api/skills/save",
            {"name": "profile-saved-skill-1880", "content": content},
            profile=profile,
        )

        assert save_status == 200
        saved_path = pathlib.Path(saved["path"]).resolve()
        saved_path.relative_to(dirs.profile_skills.resolve())
        assert saved_path.read_text(encoding="utf-8") == content
        assert not (dirs.root_skills / "profile-saved-skill-1880" / "SKILL.md").exists()

        deleted, delete_status = _post(
            "/api/skills/delete",
            {"name": "profile-saved-skill-1880"},
            profile=profile,
        )
        assert delete_status == 200
        assert deleted.get("ok") is True
        assert not saved_path.exists()
