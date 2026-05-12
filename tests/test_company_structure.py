from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import company_structure  # noqa: E402


def test_company_structure_current_files_are_valid_phase0():
    result = company_structure.validate_company_structure(ROOT / "company")

    assert result["ok"] is True
    assert result["errors"] == []
    assert result["summary"]["departments"] >= 10
    assert result["summary"]["department_dirs"] >= 10
    assert result["summary"]["role_dirs"] >= 4
    assert result["summary"]["employee_files_in_roles"] >= 4
    assert result["summary"]["programs"] >= 1
    assert result["summary"]["migration_manifests"] >= 1


def test_company_structure_rejects_missing_department_dirs(tmp_path):
    company = tmp_path / "company"
    workforce = company / "workforce"
    workforce.mkdir(parents=True)
    (company / "README.md").write_text("company", encoding="utf-8")
    (company / "org.yaml").write_text("company_phase: phase_0_internal_only\n", encoding="utf-8")
    (company / "departments").mkdir()
    (company / "programs").mkdir()
    (company / "migration").mkdir()
    (workforce / "departments.yaml").write_text(
        "departments:\n  - department_id: missing-dept\n    current_roles: [role-a]\n",
        encoding="utf-8",
    )

    result = company_structure.validate_company_structure(company)

    assert result["ok"] is False
    assert "missing department dir" in " ".join(result["errors"])
