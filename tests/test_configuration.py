import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from agent_workbench.configuration import Configuration, ConfigurationError, launcher_directory
from agent_workbench.settings import Settings
from agent_workbench.workspace import create_workspace, git, snapshot


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.file = self.root / "workbench.toml"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("AWB_") and key != "CODEX_PROXY_CERT"}
        scope = patch.dict(os.environ, environment, clear=True); scope.start(); self.addCleanup(scope.stop)

    def write(self, value):
        self.file.write_text(value, encoding="utf-8")
        return Configuration.load(self.file)

    def test_file_environment_and_explicit_cli_precedence(self):
        self.write('[workbench]\nport=8451\ndata_dir="file-data"\nmodel="opencode/big-pickle"\nuse_sqlite=true\nmax_active_jobs=3\nmax_containers=5\n')
        with patch.dict(os.environ, {"AWB_PORT": "8452", "AWB_DATA_DIR": str(self.root / "env-data"), "AWB_MODEL": "opencode/glm-5.3-flash-free", "AWB_MAX_CONTAINERS": "6"}):
            config = Configuration.load(self.file)
            self.assertEqual(config.api_url, "http://127.0.0.1:8452")
            self.assertEqual(config.data_dir, self.root / "env-data")
            self.assertEqual(config.max_active_jobs, 3); self.assertEqual(config.max_containers, 6)
            settings = Settings.load(self.root / "cli-data", config_file=self.file)
            self.assertEqual(settings.data_dir, self.root / "cli-data")
            self.assertEqual(settings.model, "opencode/glm-5.3-flash-free")

    def test_sqlite_flag_overrides_external_url_from_file_and_environment(self):
        self.write('[workbench]\nuse_sqlite=true\ndatabase_url="postgresql+missing://fixture:private-marker@invalid/test"\n')
        self.assertTrue(Settings.load(config_file=self.file).database_url.startswith("sqlite:///"))
        with patch.dict(os.environ, {"AWB_USE_SQLITE": "false", "AWB_DATABASE_URL": "postgresql+missing://other:env-marker@invalid/test"}):
            self.assertIn("env-marker", Settings.load(config_file=self.file).database_url)
            self.assertTrue(Settings.load(sqlite=True, config_file=self.file).database_url.startswith("sqlite:///"))

    def test_runtime_port_range_load_export_and_validation(self):
        result = self.write('[workbench]\nport_range_start=21000\nport_range_end=21010\n')
        self.assertEqual((result.port_range_start, result.port_range_end), (21000, 21010))
        self.assertEqual(result.public()["port_range_end"], 21010)
        with patch.dict(os.environ, {"AWB_PORT_RANGE_START": "22000", "AWB_PORT_RANGE_END": "22005"}):
            settings = Settings.load(config_file=self.file)
            self.assertEqual((settings.port_range_start, settings.port_range_end), (22000, 22005))
        result.apply()
        self.assertEqual(os.environ["AWB_PORT_RANGE_START"], "21000")
        self.assertEqual(os.environ["AWB_PORT_RANGE_END"], "21010")
        os.environ.pop("AWB_PORT_RANGE_START"); os.environ.pop("AWB_PORT_RANGE_END")
        for low, high in [(100, 2000), (2000, 65536), (24000, 23000)]:
            with self.subTest(low=low, high=high), self.assertRaises(ConfigurationError):
                self.write(f'[workbench]\nport_range_start={low}\nport_range_end={high}\n')

    def test_log_mode_defaults_file_environment_export_and_validation(self):
        self.assertEqual(self.write('[workbench]\n').log_mode,'concise')
        configured=self.write('[workbench]\nlog_mode="detailed"\n')
        self.assertEqual(configured.public()['log_mode'],'detailed')
        self.assertEqual(Settings.load(config_file=self.file).log_mode,'detailed')
        configured.apply();self.assertEqual(os.environ['AWB_LOG_MODE'],'detailed')
        with patch.dict(os.environ,{'AWB_LOG_MODE':'concise'}):
            self.assertEqual(Configuration.load(self.file).log_mode,'concise')
            self.assertEqual(Settings.load(config_file=self.file).log_mode,'concise')
        os.environ.pop('AWB_LOG_MODE')
        for value in ('verbose','PRIVATE-INVALID-MODE'):
            with self.assertRaises(ConfigurationError) as error:self.write(f'[workbench]\nlog_mode="{value}"\n')
            self.assertNotIn(value,str(error.exception))

    def test_paths_are_relative_to_file_and_utf8_bom_is_accepted(self):
        self.file.write_text('[workbench]\ndata_dir="中文数据"\nweb_dir="页面"\nproxy_ca="cert.pem"\n[launcher]\nlogs_dir="logs"\nvenv_dir="venv"\n', encoding="utf-8-sig")
        result = Configuration.load(self.file)
        self.assertEqual(result.data_dir, self.root / "中文数据")
        self.assertEqual(result.web_dir, self.root / "页面")
        self.assertEqual(result.proxy_ca, self.root / "cert.pem")

    def test_environment_file_is_explicit_and_dash_disables_reading(self):
        self.write('[workbench]\nport=8453\n')
        with patch.dict(os.environ, {"AWB_CONFIG_FILE": str(self.file)}):
            self.assertEqual(Configuration.load().port, 8453)
            self.assertEqual(Configuration.load("-").port, 8340)
        with self.assertRaises(ConfigurationError): Configuration.load(self.root / "missing.toml")

    def test_secret_values_are_not_exposed_or_implicitly_exported(self):
        secret = "fixture-local-config-private-marker"
        result = self.write('[workbench]\ndatabase_url="postgresql://fixture:db-private-marker@invalid/test"\n[environment]\nAGENT_CONFIG_TEST_TOKEN="' + secret + '"\n')
        self.assertNotIn(secret, repr(result)); self.assertNotIn("db-private-marker", repr(result))
        self.assertNotIn(secret, json.dumps(result.public()))
        self.assertNotIn("AGENT_CONFIG_TEST_TOKEN", os.environ)
        with patch.dict(os.environ, {"AGENT_CONFIG_TEST_TOKEN": "inherited-env-marker"}):
            result.apply(); self.assertEqual(os.environ["AGENT_CONFIG_TEST_TOKEN"], "inherited-env-marker")

    def test_runtime_legacy_timeout_and_proxy_aliases_are_applied(self):
        self.write('[workbench]\ntask_timeout_seconds=411\nproxy_ca="file-ca.pem"\n')
        with patch.dict(os.environ, {"AWB_TASK_TIMEOUT": "412", "CODEX_PROXY_CERT": str(self.root / "env-ca.pem")}):
            result = Configuration.load(self.file)
            self.assertEqual(result.task_timeout_seconds, 412)
            self.assertEqual(result.proxy_ca, self.root / "env-ca.pem")
            result.apply(); self.assertEqual(float(os.environ["AWB_TASK_TIMEOUT"]), 412)

    def test_invalid_settings_fail_without_secret_echo_or_fallback(self):
        for body in ('[workbench]\nport="fixture-secret-port"', '[workbench]\nport=0', '[workbench]\nmax_containers=99',
            '[workbench]\napi_url="http://secret:password@127.0.0.1:8340"', '[workbench]\napi_url="http://remote.invalid:8340"',
            '[workbench]\ndocker_endpoint="tcp://remote.invalid:2375"', '[workbench]\nunknown_field=true', '[unknown]\nvalue=true',
            '[environment]\nPATH="untrusted"', '[environment]\nAWB_MODEL="untrusted"', '[workbench]\nmodel="private-key-in-wrong-field"',
            '[agent_cli]\ncredential_env="TOKEN=private"', '[agent_cli]\nenabled="true"', '[workbench]\nport='):
            with self.subTest(body=body):
                self.file.write_text(body, encoding="utf-8")
                with self.assertRaises(ConfigurationError) as raised: Configuration.load(self.file)
                self.assertNotIn("fixture-secret-port", str(raised.exception)); self.assertNotIn("password", str(raised.exception))

    def test_agent_preset_can_be_overridden_by_environment(self):
        self.write('[agent_cli]\nenabled=true\nname="团队 CLI"\nexecutable="file-cli"\ncredential_env=["TEAM_API_KEY"]\n')
        with patch.dict(os.environ, {"AWB_AGENT_CLI_EXECUTABLE": "env-cli", "AWB_AGENT_CLI_TIMEOUT_SECONDS": "415", "AWB_AGENT_CLI_CREDENTIAL_ENV": "TEAM_TOKEN,AGENT_CONFIG"}):
            result = Configuration.load(self.file)
            self.assertEqual(result.agent_cli["executable"], "env-cli")
            self.assertEqual(result.agent_cli["timeout_seconds"], 415)
            self.assertEqual(result.agent_cli["credential_env"], ["TEAM_TOKEN", "AGENT_CONFIG"])

    def test_local_configuration_and_control_credentials_are_not_source_inputs(self):
        source = self.root / "source"; source.mkdir()
        git(source, "init", "-b", "main")
        git(source, "config", "user.name", "Fixture")
        git(source, "config", "user.email", "fixture@example.invalid")
        (source / "main.txt").write_text("application code", encoding="utf-8")
        for name in ("workbench.toml", ".workbench-run/instance.json", ".local-data/workbench/access-token"):
            path = source / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("fixture-private-config-marker", encoding="utf-8")
        git(source, "add", "--all")
        git(source, "-c", "commit.gpgsign=false", "commit", "-m", "fixture")
        result = create_workspace(source, self.root / "copies", "req-a", "project")
        repo = Path(result["workspace"]) / "repo"
        self.assertEqual([item["path"] for item in snapshot(repo)["files"]], ["main.txt"])
        self.assertNotIn("workbench.toml", git(repo, "log", "--all", "--format=", "--name-only").decode())
        self.assertNotIn(b"fixture-private-config-marker", git(repo, "show", "HEAD"))

    def test_control_locator_survives_changes_to_data_directory(self):
        first = self.write('[workbench]\ndata_dir="one"\n')
        second = self.write('[workbench]\ndata_dir="two"\n')
        self.assertNotEqual(first.data_dir, second.data_dir)
        self.assertEqual(launcher_directory(first.file), launcher_directory(second.file))

    def test_preset_is_idempotent_and_creates_immutable_versions(self):
        from agent_workbench.configured_agent import apply_preset
        from agent_workbench.server import migrate
        from agent_workbench.agent_clis import Configurations
        from agent_workbench.store import Store
        config = self.write('[workbench]\ndata_dir="data"\n[agent_cli]\nenabled=true\nname="团队CLI"\nexecutable="team-one"\n')
        settings = Settings.load(config_file=self.file); migrate(settings)
        self.assertIsNone(apply_preset(config, settings)); self.assertIsNone(apply_preset(config, settings))
        store = Store(settings.database_url)
        try:
            first = Configurations(store).read(); self.assertEqual(len(first["configs"]), 2)
            config = self.write('[workbench]\ndata_dir="data"\n[agent_cli]\nenabled=true\nname="团队CLI"\nexecutable="team-two"\n')
            self.assertIsNone(apply_preset(config, settings))
            second = Configurations(store).read(); self.assertEqual(len(second["configs"]), 3)
            self.assertNotEqual(first["default_id"], second["default_id"])
            self.assertTrue(any(item["id"] == first["default_id"] and item["content"]["run_command"].startswith("team-one ") for item in second["configs"]))
        finally: store.engine.dispose()
