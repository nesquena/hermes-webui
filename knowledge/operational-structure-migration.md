# Operational Structure Migration

Pointer note for the company/DFL migration workflow.

Operational truth now lives under `company/`:

- `company/workforce/`
- `company/departments/digital-forensic-lab/`
- `company/programs/phd-research-program/`

Legacy compatibility pointers remain under:

- `knowledge/company-workforce/_MIGRATION_POINTER.md`
- `knowledge/digital-forensic-lab/_MIGRATION_POINTER.md`

Detailed repeatable workflow lives in the Hermes skill:

- `operational-structure-migration`
- reference: `operational-structure-migration/references/company-ops-migration-2026-05-13.md`

Verification commands:

```bash
python tools/company_structure.py --json
python tools/company_workforce.py --json
python tools/digital_forensic_lab.py --json
python -m pytest tests/test_company_structure.py tests/test_company_workforce.py tests/test_digital_forensic_lab.py tests/test_memory_scout.py -q
```
