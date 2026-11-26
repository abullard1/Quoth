# SPDX-License-Identifier: GPL-3.0-or-later
# SPDX-FileCopyrightText: 2026 Samuel Ruairí Bullard

"""Tests for `config`."""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gi.repository import Gio, GObject

from quoth import APP_ID, config
from quoth.config import Config
from quoth.languages import LANGUAGES
from quoth.models import AVAILABLE_MODELS
from quoth.trigger import MODES

_SCHEMA_XML = Path(__file__).parent.parent / "data" / f"{APP_ID}.gschema.xml"


class _Flag(GObject.Object):
    """A bare property, standing in for the switch a Settings row binds."""

    on = GObject.Property(type=bool, default=False)


def _choices(key: str) -> list[str]:
    kind, values = Config().settings.props.settings_schema.get_key(key).get_range()
    assert kind == "enum"
    return values


class TestSchema:
    def test_defaults_are_the_product_choices(self):
        c = Config()
        assert (c.mode, c.hotkey, c.model, c.device, c.language) == (
            "tap-to-toggle",
            "KEY_PAUSE",
            "base",
            "auto",
            "en",
        )
        assert (c.sound_feedback, c.notification_feedback, c.overlay_style) == (
            "auto",
            "auto",
            "pill",
        )
        assert (c.sound_volume, c.keep_warm_timeout, c.mic_source) == (70, 300, "auto")
        assert c.vad and c.keep_warm and c.launch_at_login
        assert not (c.translate or c.notify_updates or c.crash_reports)
        assert not (c.onboarding_complete or c.calibration_offered)
        assert not c.portal_shortcut_enabled

    def test_modifier_chords_fit_the_hotkey_setting(self):
        c = Config()
        c.hotkey = "CTRL+KEY_F13"

        assert c.hotkey == "CTRL+KEY_F13"

    def test_mode_choices_are_the_trigger_modes(self):
        assert _choices("mode") == list(MODES)

    def test_model_choices_are_the_catalog(self):
        assert _choices("model") == [m.name for m in AVAILABLE_MODELS]

    def test_language_choices_are_the_offered_languages(self):
        assert _choices("language") == [code for _, code in LANGUAGES]

    def test_default_reads_the_schema(self):
        # The app falls back to it while the rejected key is still stored.
        c = Config()
        c.hotkey = "KEY_F13"
        assert c.default("hotkey") == "KEY_PAUSE"


class TestConfig:
    def test_a_write_is_seen_by_another_instance(self):
        Config().overlay_style = "orb"
        assert Config().overlay_style == "orb"

    def test_only_changed_keys_are_stored(self):
        c = Config()
        c.calibration_offered = True
        stored = [
            key
            for key in c.settings.props.settings_schema.list_keys()
            if c.settings.get_user_value(key) is not None
        ]
        assert stored == ["calibration-offered"]

    def test_writing_what_a_reset_key_reads_keeps_it_unset(self):
        # A row re-showing a reset key writes the value straight back.
        c = Config()
        c.mode = "push-to-hold"
        c.settings.reset("mode")
        heard = []
        c.connect_changed(heard.append)
        c.mode = c.default("mode")
        assert c.settings.get_user_value("mode") is None
        assert heard == []

    def test_a_seed_stores_even_the_default_and_only_once(self):
        # Seeding English under an English locale must stop a later locale seeding.
        c = Config()
        c.seed("language", c.default("language"))
        assert c.settings.get_user_value("language") is not None
        c.seed("language", "de")
        assert c.language == c.default("language")

    def test_a_seed_is_checked_like_an_assignment(self):
        with pytest.raises(ValueError, match="'xx' is not a valid language"):
            Config().seed("language", "xx")

    def test_a_subscriber_that_raises_does_not_silence_the_others(self, monkeypatch):
        c = Config()
        heard = []
        reported = []

        def broken(_name):
            raise RuntimeError("subscriber bug")

        monkeypatch.setattr(
            sys, "excepthook", lambda _t, error, _tb: reported.append(error)
        )
        broken_id = c.connect_changed(broken)
        c.connect_changed(heard.append)
        c.mode = "push-to-hold"
        # Python keeps the last reported traceback alive, and this Settings with it, so
        # a still-connected subscriber would raise in later tests.
        c.settings.disconnect(broken_id)
        assert heard == ["mode"]
        assert [str(error) for error in reported] == ["subscriber bug"]

    def test_a_binding_follows_the_setting_both_ways(self):
        flag = _Flag()
        Config().bind("translate", flag, "on")
        Config().translate = True
        assert flag.on is True
        flag.on = False
        assert Config().translate is False

    def test_another_instance_hears_the_change(self):
        heard = []
        listener = Config()
        listener.connect_changed(
            lambda name: heard.append((name, getattr(listener, name)))
        )
        Config().keep_warm_timeout = 600
        assert heard == [("keep_warm_timeout", 600)]

    @pytest.mark.parametrize(
        ("name", "value"),
        [
            ("mode", "streaming"),
            ("model", "huge"),
            ("language", "xx"),
            ("sound_volume", 101),
            ("keep_warm_timeout", 5),
        ],
    )
    def test_an_invalid_value_is_refused(self, name, value):
        with pytest.raises(ValueError, match=f"is not a valid {name}$"):
            setattr(Config(), name, value)

    @pytest.mark.parametrize(
        ("name", "value"),
        [("translate", "yes"), ("sound_volume", True), ("hotkey", 1)],
    )
    def test_a_wrong_type_is_refused(self, name, value):
        with pytest.raises(TypeError):
            setattr(Config(), name, value)

    def test_an_unknown_name_is_refused(self):
        c = Config()
        with pytest.raises(AttributeError):
            c.notify_update  # noqa: B018
        with pytest.raises(AttributeError):
            c.notify_update = True


class TestOpenSettings:
    @pytest.fixture
    def no_installed_schema(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Gio.SettingsSchemaSource, "get_default", lambda: None)
        return tmp_path

    def test_a_missing_schema_is_a_clear_error(self, no_installed_schema, monkeypatch):
        monkeypatch.setattr(config, "_SOURCE_SCHEMAS", no_installed_schema / "none")
        with pytest.raises(RuntimeError, match="make schemas"):
            config.open_settings()

    def test_a_package_finds_its_schema_installed_below_another(
        self, monkeypatch, tmp_path
    ):
        # The default source is a chain: a user's schema dir before the system's.
        user, system = tmp_path / "user", tmp_path / "system"
        for folder in (user, system):
            folder.mkdir()
        (user / "org.example.Other.gschema.xml").write_text(
            '<schemalist><schema id="org.example.Other"/></schemalist>'
        )
        shutil.copy(_SCHEMA_XML, system)
        for folder in (user, system):
            subprocess.run(["glib-compile-schemas", str(folder)], check=True)
        chain = Gio.SettingsSchemaSource.new_from_directory(
            str(user),
            Gio.SettingsSchemaSource.new_from_directory(str(system), None, False),
            False,
        )
        monkeypatch.setattr(Gio.SettingsSchemaSource, "get_default", lambda: chain)
        monkeypatch.setattr(config, "_SOURCE_SCHEMAS", tmp_path / "no-checkout")
        assert config.open_settings().props.schema_id == APP_ID

    def test_a_source_checkout_uses_its_compiled_schema(
        self, no_installed_schema, monkeypatch, caplog
    ):
        checkout = no_installed_schema / "data"
        checkout.mkdir()
        shutil.copy(_SCHEMA_XML, checkout)
        subprocess.run(["glib-compile-schemas", str(checkout)], check=True)
        monkeypatch.setattr(config, "_SOURCE_SCHEMAS", checkout)
        assert config.open_settings().props.schema_id == APP_ID
        assert caplog.messages == [
            f"Settings schema: {checkout}; backend: GMemorySettingsBackend"
        ]

    def test_a_memory_backend_nobody_asked_for_is_a_warning(self, monkeypatch, caplog):
        # GIO falls back to memory without dconf; the suite asks for it by name. GIO
        # keeps its first default backend, so memory is fixed before the name goes.
        Gio.SettingsBackend.get_default()
        monkeypatch.delenv("GSETTINGS_BACKEND")
        config.open_settings()
        assert caplog.messages[-1] == (
            "Settings are kept in memory only: changes will not persist"
        )

    def test_a_checkout_outranks_an_older_installed_schema(self, monkeypatch, tmp_path):
        installed, checkout = tmp_path / "installed", tmp_path / "data"
        for folder in (installed, checkout):
            folder.mkdir()
        older = _SCHEMA_XML.read_text().replace('name="translate"', 'name="dropped"')
        (installed / _SCHEMA_XML.name).write_text(older)
        shutil.copy(_SCHEMA_XML, checkout)
        for folder in (installed, checkout):
            subprocess.run(["glib-compile-schemas", str(folder)], check=True)
        system = Gio.SettingsSchemaSource.new_from_directory(
            str(installed), None, False
        )
        monkeypatch.setattr(Gio.SettingsSchemaSource, "get_default", lambda: system)
        monkeypatch.setattr(config, "_SOURCE_SCHEMAS", checkout)
        assert config.open_settings().props.settings_schema.has_key("translate")
