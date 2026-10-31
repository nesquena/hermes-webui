"""The Scripts subtab keys that live in the ``en`` locale only.

The #2316 Scripts subtab added seven user-facing keys. Translated locales
carry their own wording for the ones that were translated; the rest stay in
``en`` and ride the ``t()`` fallback (``_locale[key] ?? LOCALES.en[key]``),
which is what ``docs/GUIDELINES.md`` section 8 requires of new copy:

    new user-facing copy goes in the ``en`` locale and rides the existing
    fallback -- don't paste English into every locale block.

A pasted English copy is a second source of truth: when the English wording
changes, the pasted copy keeps serving the stale string and the two drift
apart silently. The locale-coverage tests use this set to record which keys
are legitimately absent from a locale block, the same way they already record
the ``profile_concept_*`` block (issue #2147) and
``workspace_artifact_source_session``.

Which keys are absent depends on the locale:

* ``scripts_no_directory`` and ``scripts_load_failed`` -- absent from every
  non-English locale (the English sentences were never translated).
* ``cron_tab_scripts`` and ``scripts_detail_empty_title`` -- absent from the
  locales whose word for "scripts" is spelled like the English one
  (es, fr, pt, vi), where the copy was English rather than a translation.

``cron_tab_jobs``, ``scripts_empty`` and ``scripts_detail_empty_sub`` are
translated in every locale and are therefore NOT in this set.
"""

# Absent from EVERY non-English locale block.
SCRIPTS_FALLBACK_KEYS_COMMON = (
    "scripts_no_directory",
    "scripts_load_failed",
    "scripts_unavailable",
)

# Additionally absent from the locales whose "scripts" spelling matches
# English (es, fr, pt, vi).
SCRIPTS_FALLBACK_KEYS_ENGLISH_SPELLED = (
    "cron_tab_scripts",
    "scripts_detail_empty_title",
)

# The union -- what a locale needs when it carries no translation at all for
# the Scripts copy.
SCRIPTS_FALLBACK_KEYS = SCRIPTS_FALLBACK_KEYS_COMMON + SCRIPTS_FALLBACK_KEYS_ENGLISH_SPELLED

# The locales that also omit the English-spelled keys.
ENGLISH_SPELLED_LOCALES = ("es", "fr", "pt", "vi")


def scripts_fallback_keys(locale: str) -> tuple[str, ...]:
    """Return the Scripts keys that ``locale`` legitimately omits."""
    if locale in ENGLISH_SPELLED_LOCALES:
        return SCRIPTS_FALLBACK_KEYS
    return SCRIPTS_FALLBACK_KEYS_COMMON
