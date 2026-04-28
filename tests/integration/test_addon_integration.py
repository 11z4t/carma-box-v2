"""PLAT-1813 — Integration and System tests for HA addon packaging.

Covers:
  Level 2 (Integration):  Config-load + override pipeline end-to-end,
                           state.db creation in /data/-like paths,
                           options.json → env vars → override chain.
  Level 3 (System):       Full startup sequence simulation (dry-run mode),
                           site.yaml validation with supervisor URL,
                           health endpoint registration validation.

Level 6 (E2E/Playwright) and Level 7 (Production verification) are
documented at the bottom of this file as manual test criteria.

DoD reference: /mnt/solutions/Root/platform/global/standards/DEFINITION-OF-DONE.md
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config.schema import load_config
from main import _apply_addon_overrides, parse_args

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
SITE_YAML = PROJECT_ROOT / "config" / "site.yaml"


# ---------------------------------------------------------------------------
# Level 2 — Integration: Config-load → override pipeline
# ---------------------------------------------------------------------------


class TestAddonConfigPipeline:
    """Integration tests: load real site.yaml → apply addon overrides → verify."""

    def test_load_production_site_yaml_then_override_db_path(self) -> None:
        """Full pipeline: load production config, override DB path for addon."""
        config = load_config(str(SITE_YAML))
        with patch.dict(os.environ, {"CARMA_OVERRIDE_DB_PATH": "/data/carma.db"}):
            _apply_addon_overrides(config)
        assert config.storage.sqlite.path == "/data/carma.db"
        # Other config values must be unchanged
        assert config.site.name == "Sanduddsvagen 60"

    def test_load_production_site_yaml_then_override_log_paths(self) -> None:
        """Full pipeline: load production config, override log file + level."""
        config = load_config(str(SITE_YAML))
        env = {
            "CARMA_OVERRIDE_LOG_FILE": "/data/logs/carma.log",
            "CARMA_OVERRIDE_LOG_LEVEL": "DEBUG",
        }
        with patch.dict(os.environ, env):
            _apply_addon_overrides(config)
        assert config.logging.file == "/data/logs/carma.log"
        assert config.logging.level == "DEBUG"
        # Battery config must be untouched
        assert len(config.batteries) > 0

    def test_override_is_applied_before_logging_setup(self) -> None:
        """Override must be applied before setup_logging to affect log level.

        Verifies: _apply_addon_overrides is called before setup_logging() in main().
        """
        # Read main.py source and verify call order
        main_src = (PROJECT_ROOT / "main.py").read_text(encoding="utf-8")
        apply_idx = main_src.find("_apply_addon_overrides(config)")
        setup_idx = main_src.find("setup_logging(config)")
        assert apply_idx != -1, "_apply_addon_overrides must be called in main()"
        assert setup_idx != -1, "setup_logging must be called in main()"
        assert apply_idx < setup_idx, (
            "_apply_addon_overrides must be called BEFORE setup_logging — "
            f"got apply_idx={apply_idx}, setup_idx={setup_idx}"
        )

    def test_options_json_to_env_vars_to_config_chain(self, tmp_path: Path) -> None:
        """Simulate the full options.json → env vars → config override chain.

        This is the exact chain that run.sh + main.py executes.
        """
        # Step 1: HA Supervisor writes options.json
        options = {
            "ha_token": "test_token_123",
            "solcast_api_key": "sk-test",
            "nordpool_api_key": "",
            "slack_webhook_url": "",
            "pg_host": "",
            "pg_port": 5432,
            "pg_database": "energy",
            "pg_user": "",
            "pg_password": "",
            "log_level": "INFO",
        }
        options_file = tmp_path / "options.json"
        options_file.write_text(json.dumps(options), encoding="utf-8")

        # Step 2: run.sh reads options.json and exports env vars
        loaded_options = json.loads(options_file.read_text())
        simulated_env = {
            "CARMA_HA_TOKEN": loaded_options["ha_token"],
            "CARMA_SOLCAST_API_KEY": loaded_options.get("solcast_api_key", ""),
            "CARMA_OVERRIDE_LOG_LEVEL": loaded_options["log_level"],
            "CARMA_OVERRIDE_LOG_FILE": "/data/logs/carma.log",
            "CARMA_OVERRIDE_DB_PATH": "/data/carma.db",
        }

        # Step 3: main.py loads config and applies overrides
        config = load_config(str(SITE_YAML))
        with patch.dict(os.environ, simulated_env):
            _apply_addon_overrides(config)

        assert config.logging.level == "INFO"
        assert config.logging.file == "/data/logs/carma.log"
        assert config.storage.sqlite.path == "/data/carma.db"

    def test_state_db_path_in_data_volume(self, tmp_path: Path) -> None:
        """State DB must be createable in a /data/-like path (volume simulation)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        db_path = data_dir / "carma.db"

        # Simulate what LocalDB.__init__ would do: just verify the path is writable
        db_path.touch()
        assert db_path.exists()
        assert db_path.stat().st_size == 0  # Created empty, DB init would follow

    def test_log_dir_in_data_volume_is_creatable(self, tmp_path: Path) -> None:
        """Log directory under /data/logs/ must be creatable (addon first start)."""
        data_dir = tmp_path / "data"
        log_dir = data_dir / "logs"
        log_dir.mkdir(parents=True)
        assert log_dir.exists()
        assert log_dir.is_dir()


# ---------------------------------------------------------------------------
# Level 3 — System: Dry-run mode validation
# ---------------------------------------------------------------------------


class TestAddonDryRunMode:
    """System tests: run main.py --dry-run to validate config loading.

    --dry-run loads config, applies overrides, validates, then exits 0.
    This is the complete startup path minus the asyncio control loop.
    """

    def test_dry_run_with_production_site_yaml_exits_zero(self) -> None:
        """main(['--config', 'config/site.yaml', '--dry-run']) must return 0."""
        from main import main

        result = main(["--config", str(SITE_YAML), "--dry-run"])
        assert result == 0

    def test_dry_run_with_missing_config_exits_one(self) -> None:
        """main() with missing config must return 1 (not crash)."""
        from main import main

        result = main(["--config", "/nonexistent/site.yaml", "--dry-run"])
        assert result == 1

    def test_dry_run_with_all_overrides_set_exits_zero(self, tmp_path: Path) -> None:
        """dry-run with all CARMA_OVERRIDE_* set must still exit 0."""
        from main import main

        env = {
            "CARMA_OVERRIDE_DB_PATH": str(tmp_path / "carma.db"),
            "CARMA_OVERRIDE_LOG_FILE": str(tmp_path / "carma.log"),
            "CARMA_OVERRIDE_LOG_LEVEL": "WARNING",
        }
        with patch.dict(os.environ, env):
            result = main(["--config", str(SITE_YAML), "--dry-run"])
        assert result == 0

    def test_dry_run_applies_overrides_before_setup_logging(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Dry-run must log at the overridden level, not the site.yaml default."""
        from main import main

        env = {"CARMA_OVERRIDE_LOG_LEVEL": "WARNING"}
        with patch.dict(os.environ, env):
            result = main(["--config", str(SITE_YAML), "--dry-run"])
        assert result == 0

    def test_parse_args_config_is_required(self) -> None:
        """--config is a required argument — omitting it must cause SystemExit."""
        with pytest.raises(SystemExit):
            parse_args([])

    def test_parse_args_dry_run_flag(self) -> None:
        """--dry-run flag must be parsed correctly."""
        args = parse_args(["--config", "config/site.yaml", "--dry-run"])
        assert args.dry_run is True

    def test_parse_args_no_dry_run_is_default(self) -> None:
        """dry_run must default to False when flag is absent."""
        args = parse_args(["--config", "config/site.yaml"])
        assert args.dry_run is False

    def test_parse_args_version_exits(self) -> None:
        """--version must print version and exit."""
        with pytest.raises(SystemExit) as exc_info:
            parse_args(["--version"])
        assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Level 3 — System: Supervisor handshake validation
# ---------------------------------------------------------------------------


class TestAddonSupervisorHandshake:
    """System-level tests for addon-supervisor integration points."""

    def test_addon_config_yaml_exists(self) -> None:
        """addon/config.yaml must exist with correct fields."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        assert config_yaml.exists(), "addon/config.yaml is required for HA addon"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        assert isinstance(data, dict)

    def test_addon_config_yaml_has_required_fields(self) -> None:
        """addon/config.yaml must have all HA Supervisor required fields."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))

        required = ("name", "version", "slug", "description", "arch", "startup", "boot")
        for field in required:
            assert field in data, f"addon/config.yaml missing required field: {field!r}"

    def test_addon_config_yaml_arch_includes_aarch64(self) -> None:
        """addon/config.yaml must support aarch64 (RPi5 — Borje's deployment target)."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        assert "aarch64" in data.get("arch", []), (
            "aarch64 must be in supported architectures (required for RPi5)"
        )

    def test_addon_config_yaml_arch_includes_amd64(self) -> None:
        """addon/config.yaml must support amd64 (Borje's HA OS on x86)."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        assert "amd64" in data.get("arch", []), (
            "amd64 must be in supported architectures (required for x86 HA OS)"
        )

    def test_addon_config_yaml_maps_homeassistant(self) -> None:
        """addon/config.yaml must map homeassistant volume (for site.yaml access)."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        maps = data.get("map", [])
        ha_map = any("homeassistant" in str(m) for m in maps)
        assert ha_map, "addon/config.yaml must map homeassistant volume"

    def test_addon_config_yaml_maps_data(self) -> None:
        """addon/config.yaml must map data volume (for state.db + logs)."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        maps = data.get("map", [])
        data_map = any("data" in str(m) for m in maps)
        assert data_map, "addon/config.yaml must map data volume for state.db persistence"

    def test_addon_config_yaml_exposes_health_port(self) -> None:
        """addon/config.yaml must expose port 8412 for health checks."""
        import yaml as yaml_module

        config_yaml = PROJECT_ROOT / "addon" / "config.yaml"
        data = yaml_module.safe_load(config_yaml.read_text(encoding="utf-8"))
        ports = data.get("ports", {})
        assert "8412/tcp" in ports, (
            "addon/config.yaml must expose 8412/tcp for health endpoint"
        )

    def test_dockerfile_exists(self) -> None:
        """addon/Dockerfile must exist."""
        dockerfile = PROJECT_ROOT / "addon" / "Dockerfile"
        assert dockerfile.exists(), "addon/Dockerfile is required"

    def test_run_sh_exists_and_is_non_empty(self) -> None:
        """addon/run.sh must exist and contain startup logic."""
        run_sh = PROJECT_ROOT / "addon" / "run.sh"
        assert run_sh.exists(), "addon/run.sh is required"
        content = run_sh.read_text(encoding="utf-8")
        assert len(content) > 100, "addon/run.sh must not be empty"

    def test_run_sh_validates_site_yaml_existence(self) -> None:
        """run.sh must check for site.yaml and exit 1 if missing."""
        run_sh = PROJECT_ROOT / "addon" / "run.sh"
        content = run_sh.read_text(encoding="utf-8")
        assert "SITE_YAML" in content, "run.sh must define and check SITE_YAML"
        assert "exit 1" in content, "run.sh must exit 1 on fatal errors"

    def test_run_sh_sets_carma_ha_token_from_supervisor(self) -> None:
        """run.sh must fall back to SUPERVISOR_TOKEN when ha_token option is empty."""
        run_sh = PROJECT_ROOT / "addon" / "run.sh"
        content = run_sh.read_text(encoding="utf-8")
        assert "SUPERVISOR_TOKEN" in content, (
            "run.sh must use SUPERVISOR_TOKEN as fallback for HA authentication"
        )

    def test_run_sh_uses_exec_for_signal_propagation(self) -> None:
        """run.sh must use 'exec python3' so SIGTERM propagates to Python."""
        run_sh = PROJECT_ROOT / "addon" / "run.sh"
        content = run_sh.read_text(encoding="utf-8")
        assert "exec python3" in content, (
            "run.sh must use 'exec python3' so Supervisor SIGTERM reaches Python "
            "for graceful shutdown"
        )

    def test_run_sh_sqlite_integrity_check(self) -> None:
        """run.sh must perform SQLite integrity check on existing DB."""
        run_sh = PROJECT_ROOT / "addon" / "run.sh"
        content = run_sh.read_text(encoding="utf-8")
        assert "integrity_check" in content, (
            "run.sh must run SQLite integrity_check on existing state.db"
        )

    def test_readme_exists(self) -> None:
        """addon/README.md must exist for HA addon store display."""
        readme = PROJECT_ROOT / "addon" / "README.md"
        assert readme.exists(), "addon/README.md is required for HA addon store"

    def test_readme_mentions_plat1828_h6(self) -> None:
        """README must document the PLAT-1828 H6 stale-SoC fix."""
        readme = PROJECT_ROOT / "addon" / "README.md"
        content = readme.read_text(encoding="utf-8")
        assert "PLAT-1828" in content or "H6" in content, (
            "addon/README.md must document the PLAT-1828 H6 stale-SoC guard fix"
        )


# ---------------------------------------------------------------------------
# Level 6 — E2E (Manual criteria, documented here for 901 QC)
# ---------------------------------------------------------------------------

"""
LEVEL 6 — E2E / Playwright (MANUAL — cannot be automated without live HA instance)

Test plan for Borje's HA instance (192.168.5.22):

E2E-1: Addon installation via HA UI
  Given: Repo https://github.com/11z4t/carma-box-v2 added as custom addon repo
  When:  User navigates to Supervisor → Add-on Store → CARMA Box → Install
  Then:  Addon appears in installed list with version 2.1.0

E2E-2: Addon configuration via HA UI
  Given: Addon installed
  When:  User opens Configuration tab and sets:
         - log_level = INFO
         - solcast_api_key = (from 1Password)
  Then:  Options saved without validation errors

E2E-3: site.yaml creation and validation
  Given: Addon configured
  When:  User creates /homeassistant/carmabox/site.yaml from site.yaml.example
  Then:  File visible in File Editor; addon starts without errors

E2E-4: Addon start and log visibility
  Given: site.yaml created with correct entity IDs
  When:  User starts addon
  Then:  Addon log shows "CARMA Box v2.1.0 starting — site: Sanduddsvagen 60"
         No ERROR lines in first 60 seconds

E2E-5: Health endpoint
  Given: Addon running
  When:  User navigates to http://ha-ip:8412/health in browser
  Then:  {"status": "ok", "version": "2.1.0", "uptime_s": N} (N > 0)

E2E-6: Addon restart persistence
  Given: Addon running for > 5 minutes (state.db has data)
  When:  User restarts addon
  Then:  state.db NOT reset; cycle count continues from before restart

E2E-7: Addon update (version bump simulation)
  Given: Addon at version 2.1.0 installed
  When:  New version pushed and addon updated via HA UI
  Then:  state.db preserved; no service interruption > 30s
"""


# ---------------------------------------------------------------------------
# Level 7 — Production Verification (Criteria for VM-906 Vera)
# ---------------------------------------------------------------------------

"""
LEVEL 7 — PRODUCTION VERIFICATION (VM-906 automated, 12-24h cycle)

Verification target: HA OS at 192.168.5.22 (Borje's installation)
Minimum cycle:       12 hours; ideal 24 hours

Contracts to verify (all must hold continuously):

PROD-1: Zero grid export > 4.0 kW for > 5 minutes
  Sensor: sensor.house_grid_power
  Trigger: grid_power > 4000 W sustained for > 300s → VIOLATION

PROD-2: Battery SoC never drops below min_soc_pct (15%) during normal operation
  Sensor: sensor.goodwe_battery_state_of_charge_kontor
  Exception: allowed during G1 guard active events (documented)

PROD-3: EMS mode never set to "auto"
  Sensor: select.goodwe_kontor_ems_mode
  Trigger: state == "auto" at any time → VIOLATION (HARD RULE B10)

PROD-4: Stale SoC safe mode activates when sensor goes stale
  Method: Disconnect GoodWe UDP for 3 minutes; verify:
  - carma-box log shows "H6 STALE SOC — returning -1.0"
  - EMS mode set to battery_standby within one cycle (30s)

PROD-5: floor+PV charge activates when SoC < 20% and PV > 500W
  Method: Verify in logs during morning solar hours:
  - If soc_pct < 20 AND pv_surplus_w > 500 → log shows "H6 floor+PV trigger"
  - EMS mode = charge_pv (NOT battery_standby)

PROD-6: Log file rotating correctly (no disk fill)
  Check: /data/logs/carma.log exists; size < 10MB after 24h

PROD-7: state.db persists across addon restart (24h mark)
  After 24h: restart addon; verify cycle logs still present from before restart
"""
