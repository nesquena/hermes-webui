"""Bot Mode (#7174): agent profiles surface Bot roster metadata.

Hermes Desktop's hermes-bots plugin maps each agent profile to a named Bot
(own role / title / avatar). The WebUI's ``/api/profiles`` rows previously
carried no Bot presentation data, so the web client could not render a Bots
roster. This adds a ``bot`` sub-object to every profile row, read from
``profile.yaml``:

- ``title``        -> ``ui_meta['hermes-bots'].title`` (the roster display name)
- ``display_name`` -> ``display_name`` (presentation-only fallback)
- ``description``  -> ``description`` (role blurb)

All three are presentation-only; resolution/switching keeps using ``name``.
"""
import os

from api import profiles as profiles_api

STATIC_DIR = os.path.join(os.path.dirname(__file__), '..', 'static')


def _read(filename):
    return open(os.path.join(STATIC_DIR, filename), encoding='utf-8').read()


BOT_YAML = """\
display_name: Sam Assistant
description: Handles Mike's personal errands over Telegram.
ui_meta:
  hermes-bots:
    title: Sam
"""


class TestProfileBotMeta:
    def test_reads_bot_title_display_name_and_description(self, tmp_path):
        (tmp_path / 'profile.yaml').write_text(BOT_YAML, encoding='utf-8')
        meta = profiles_api._profile_bot_meta(tmp_path)
        assert meta['title'] == 'Sam'
        assert meta['display_name'] == 'Sam Assistant'
        assert meta['description'] == "Handles Mike's personal errands over Telegram."

    def test_missing_profile_yaml_returns_empty_strings(self, tmp_path):
        meta = profiles_api._profile_bot_meta(tmp_path)
        assert meta == {'title': '', 'display_name': '', 'description': ''}

    def test_corrupt_yaml_never_raises(self, tmp_path):
        (tmp_path / 'profile.yaml').write_text('ui_meta: [unclosed', encoding='utf-8')
        meta = profiles_api._profile_bot_meta(tmp_path)
        assert meta == {'title': '', 'display_name': '', 'description': ''}

    def test_ui_meta_without_hermes_bots_key(self, tmp_path):
        (tmp_path / 'profile.yaml').write_text(
            'display_name: Solo\nui_meta:\n  other-plugin:\n    title: Nope\n',
            encoding='utf-8',
        )
        meta = profiles_api._profile_bot_meta(tmp_path)
        assert meta['title'] == ''
        assert meta['display_name'] == 'Solo'

    def test_hermes_bots_title_empty_when_unset(self, tmp_path):
        (tmp_path / 'profile.yaml').write_text(
            'ui_meta:\n  hermes-bots: {}\n', encoding='utf-8'
        )
        meta = profiles_api._profile_bot_meta(tmp_path)
        assert meta['title'] == ''


class TestProfileRowsCarryBotMeta:
    """Every row-builder path must attach the ``bot`` sub-object."""

    def test_fast_row_builder_includes_bot_meta(self):
        src = open(profiles_api.__file__, encoding='utf-8').read()
        start = src.find('def _build_profile_rows_fast')
        assert start >= 0
        end = src.find('\ndef list_profiles_api', start)
        body = src[start:end]
        assert "'bot': _profile_bot_meta(home)" in body, \
            'fast row builder must attach bot metadata (#7174)'

    def test_isolated_and_fallback_rows_include_bot_meta(self):
        src = open(profiles_api.__file__, encoding='utf-8').read()
        start = src.find('def list_profiles_api')
        end = src.find('\ndef _profile_bot_meta', start)
        body = src[start:end]
        assert body.count("'bot': _profile_bot_meta(") >= 3, \
            'isolated-mode rows, isolated fallback row, and upstream-fallback ' \
            'rows must all attach bot metadata (#7174)'

    def test_default_profile_dict_includes_bot_meta(self):
        src = open(profiles_api.__file__, encoding='utf-8').read()
        start = src.find('def _default_profile_dict')
        end = src.find('\ndef _validate_profile_name', start)
        body = src[start:end]
        assert "'bot':" in body, \
            'hermes_cli-less fallback dict must attach bot metadata (#7174)'


class TestProfilesPanelRendersBotTitles:
    def test_profile_card_shows_bot_title(self):
        src = _read('panels.js')
        start = src.find('async function loadProfilesPanel')
        assert start >= 0, 'loadProfilesPanel not found'
        end = src.find('\nfunction _renderProfileConceptHelp', start)
        body = src[start:end]
        assert 'p.bot' in body, \
            'Profiles panel must read the bot sub-object (#7174)'
        assert '.title' in body, \
            'Profiles panel must render the bot roster title (#7174)'
