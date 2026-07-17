"""Tests for scripts/run_pole_vitals_backfill.py"""

import json
import os

import pytest

from scripts.run_pole_vitals_backfill import load_local_settings_into_env, refuse_if_prod


class TestRefuseIfProd:
    def test_raises_for_prod(self):
        with pytest.raises(SystemExit):
            refuse_if_prod("Prod")

    def test_does_not_raise_for_dev(self):
        refuse_if_prod("Dev")  # must not raise

    def test_does_not_raise_for_other_environments(self):
        refuse_if_prod("Staging")  # must not raise


class TestLoadLocalSettingsIntoEnv:
    def test_returns_false_when_file_missing(self, tmp_path):
        result = load_local_settings_into_env(project_root=tmp_path)
        assert result is False

    def test_loads_values_into_environment(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SOME_TEST_KEY", raising=False)
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"Values": {"SOME_TEST_KEY": "some-value"}}))

        result = load_local_settings_into_env(project_root=tmp_path)

        assert result is True
        assert os.environ["SOME_TEST_KEY"] == "some-value"

    def test_does_not_override_an_already_set_env_var(self, tmp_path, monkeypatch):
        monkeypatch.setenv("SOME_TEST_KEY", "already-set-value")
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"Values": {"SOME_TEST_KEY": "from-file-value"}}))

        load_local_settings_into_env(project_root=tmp_path)

        assert os.environ["SOME_TEST_KEY"] == "already-set-value"

    def test_handles_missing_values_key_gracefully(self, tmp_path):
        settings_file = tmp_path / "local.settings.json"
        settings_file.write_text(json.dumps({"IsEncrypted": False}))

        result = load_local_settings_into_env(project_root=tmp_path)

        assert result is True  # file existed and was valid JSON, just no Values
