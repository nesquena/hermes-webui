from tests.i18n_locale_loader import locale_source_text


def test_split_locale_source_includes_bundle_helper_preambles():
    source = locale_source_text()

    assert "function _i18nProcessedElapsedZh(duration)" in source
    assert "function _i18nProcessedElapsedZhHant(duration)" in source
    assert "processed_elapsed: _i18nProcessedElapsedZh" in source
    assert "processed_elapsed: _i18nProcessedElapsedZhHant" in source
