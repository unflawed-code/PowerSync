"""Regression tests for Flow Power auto-armed pre-window fill.

Flow Power pays a premium feed-in rate during Happy Hour (17:30 to the
configured end).  The coordinator now auto-arms the pre-window SOC floor at
the Happy Hour start, so the battery is full before the premium export window
opens even without a Charge By Time target.  These tests cover the new slot
helpers, the happy-hour rate resolver, and the arming decision in
`_pre_window_fill_target`.
"""

from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
COMPONENT_ROOT = ROOT / "custom_components" / "power_sync"

_SENTINEL = object()

_STUB_MODULE_NAMES = (
    "homeassistant",
    "homeassistant.core",
    "homeassistant.exceptions",
    "homeassistant.helpers",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.event",
    "homeassistant.helpers.storage",
    "homeassistant.helpers.update_coordinator",
    "homeassistant.util",
    "homeassistant.util.dt",
    "power_sync",
    "power_sync.automations",
    "power_sync.coordinator",
    "power_sync.optimization",
    "power_sync.optimization.battery_optimizer",
    "power_sync.optimization.coordinator",
    "power_sync.optimization.ev_coordinator",
    "power_sync.optimization.executor",
    "power_sync.optimization.load_estimator",
    "power_sync.optimization.schedule_reader",
)


def _install_ha_stubs() -> None:
    ha_root = types.ModuleType("homeassistant")
    ha_core = types.ModuleType("homeassistant.core")
    ha_exceptions = types.ModuleType("homeassistant.exceptions")
    ha_helpers = types.ModuleType("homeassistant.helpers")
    ha_dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    ha_event = types.ModuleType("homeassistant.helpers.event")
    ha_storage = types.ModuleType("homeassistant.helpers.storage")
    ha_update = types.ModuleType("homeassistant.helpers.update_coordinator")
    ha_util = types.ModuleType("homeassistant.util")
    ha_dt = types.ModuleType("homeassistant.util.dt")

    class _Store:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class _DataUpdateCoordinator:
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, hass, logger, name=None, update_interval=None) -> None:
            self.hass = hass
            self.logger = logger
            self.name = name
            self.update_interval = update_interval
            self.data = None

    ha_core.HomeAssistant = type("HomeAssistant", (), {})
    ha_exceptions.ConfigEntryNotReady = type("ConfigEntryNotReady", (Exception,), {})
    ha_exceptions.ConfigEntryAuthFailed = type("ConfigEntryAuthFailed", (Exception,), {})
    ha_storage.Store = _Store
    ha_update.DataUpdateCoordinator = _DataUpdateCoordinator
    ha_dispatcher.async_dispatcher_send = lambda *args, **kwargs: None
    ha_event.async_track_point_in_utc_time = lambda *args, **kwargs: None
    ha_event.async_track_time_change = lambda *args, **kwargs: lambda: None
    ha_dt.now = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.utcnow = lambda *args, **kwargs: datetime(2026, 5, 3, 8, 30, tzinfo=timezone.utc)
    ha_dt.UTC = timezone.utc
    ha_helpers.storage = ha_storage
    ha_helpers.dispatcher = ha_dispatcher
    ha_helpers.event = ha_event
    ha_helpers.update_coordinator = ha_update
    ha_util.dt = ha_dt
    ha_root.helpers = ha_helpers
    ha_root.util = ha_util

    sys.modules["homeassistant"] = ha_root
    sys.modules["homeassistant.core"] = ha_core
    sys.modules["homeassistant.exceptions"] = ha_exceptions
    sys.modules["homeassistant.helpers"] = ha_helpers
    sys.modules["homeassistant.helpers.dispatcher"] = ha_dispatcher
    sys.modules["homeassistant.helpers.event"] = ha_event
    sys.modules["homeassistant.helpers.storage"] = ha_storage
    sys.modules["homeassistant.helpers.update_coordinator"] = ha_update
    sys.modules["homeassistant.util"] = ha_util
    sys.modules["homeassistant.util.dt"] = ha_dt


def _install_power_sync_stubs() -> None:
    ps_module = types.ModuleType("power_sync")
    ps_module.__path__ = [str(COMPONENT_ROOT)]
    sys.modules["power_sync"] = ps_module

    optimization_module = types.ModuleType("power_sync.optimization")
    optimization_module.__path__ = [str(COMPONENT_ROOT / "optimization")]
    sys.modules["power_sync.optimization"] = optimization_module

    coordinator_module = types.ModuleType("power_sync.coordinator")
    coordinator_module.normalize_custom_power_kw = (
        lambda value, unit="": float(value) if value is not None else None
    )
    sys.modules["power_sync.coordinator"] = coordinator_module

    automations_module = types.ModuleType("power_sync.automations")
    automations_module.__path__ = []
    sys.modules["power_sync.automations"] = automations_module

    battery_module = types.ModuleType("power_sync.optimization.battery_optimizer")
    battery_module.BatteryOptimizer = type("BatteryOptimizer", (), {})
    battery_module.OptimizerResult = type("OptimizerResult", (), {})
    sys.modules["power_sync.optimization.battery_optimizer"] = battery_module

    schedule_module = types.ModuleType("power_sync.optimization.schedule_reader")
    schedule_module.ScheduleAction = type("ScheduleAction", (), {})
    schedule_module.OptimizationSchedule = type("OptimizationSchedule", (), {})
    sys.modules["power_sync.optimization.schedule_reader"] = schedule_module

    executor_module = types.ModuleType("power_sync.optimization.executor")
    executor_module.ScheduleExecutor = type("ScheduleExecutor", (), {})
    executor_module.ExecutionStatus = type("ExecutionStatus", (), {})
    executor_module.BatteryAction = type("BatteryAction", (), {})
    sys.modules["power_sync.optimization.executor"] = executor_module

    load_module = types.ModuleType("power_sync.optimization.load_estimator")
    load_module.LoadEstimator = type("LoadEstimator", (), {})
    load_module.SolcastForecaster = type("SolcastForecaster", (), {})
    sys.modules["power_sync.optimization.load_estimator"] = load_module

    ev_module = types.ModuleType("power_sync.optimization.ev_coordinator")
    ev_module.EVCoordinator = type("EVCoordinator", (), {})
    ev_module.EVConfig = type("EVConfig", (), {})
    ev_module.EVChargingMode = type("EVChargingMode", (), {})
    sys.modules["power_sync.optimization.ev_coordinator"] = ev_module


@pytest.fixture()
def opt_module():
    saved_modules = {
        name: sys.modules.get(name, _SENTINEL)
        for name in _STUB_MODULE_NAMES
    }
    for name in _STUB_MODULE_NAMES:
        sys.modules.pop(name, None)

    _install_ha_stubs()
    _install_power_sync_stubs()
    module = importlib.import_module("power_sync.optimization.coordinator")
    try:
        yield module
    finally:
        for name in _STUB_MODULE_NAMES:
            if saved_modules[name] is _SENTINEL:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved_modules[name]


def _coordinator(opt_module, provider: str = "flow_power", **options):
    coordinator = object.__new__(opt_module.OptimizationCoordinator)
    base_options = {"electricity_provider": provider}
    base_options.update(options)
    coordinator._entry = SimpleNamespace(options=base_options, data={})
    coordinator._config = opt_module.OptimizationConfig(
        interval_minutes=30,
        horizon_hours=24,
        profit_max_enabled=False,
        charge_by_time_enabled=False,
        charge_by_time_target_time="17:15",
        charge_by_time_target_soc=1.0,
        allow_grid_charge=True,
        grid_charge_soc_cap=1.0,
    )
    coordinator._saving_session_coordinator = None
    coordinator._last_export_boost_allowed_slots = []
    coordinator._last_price_timestamps = None
    coordinator._last_zerohero_bonus_cap_kwh = None
    coordinator._last_zerohero_bonus_prices = None
    coordinator._actual_zerohero_import_kwh_today = 0.0
    coordinator._actual_zerohero_export_kwh_today = 0.0
    coordinator._actual_zerohero_bonus_export_kwh_today = 0.0
    coordinator._actual_zerohero_base_export_earnings_today = 0.0
    coordinator._actual_zerohero_bonus_export_earnings_today = 0.0
    coordinator._actual_zerohero_credit_value_today = 0.0
    coordinator._actual_zerocharge_import_kwh_today = 0.0
    coordinator._actual_zerocharge_credit_value_today = 0.0
    coordinator._pre_idle_backup_reserve = None
    coordinator._idle_hold_reserve = None
    coordinator._optimizer = None
    coordinator.energy_coordinator = None
    return coordinator


def test_flow_power_happy_hour_rate_from_state(opt_module):
    """Rate falls back to the regional FLOW_POWER_EXPORT_RATES lookup."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    assert coordinator._flow_power_happy_hour_rate() == 0.35


def test_flow_power_happy_hour_rate_from_configured_cents(opt_module):
    """A configured rate in cents per kWh beats the regional default."""
    coordinator = _coordinator(
        opt_module,
        provider="flow_power",
        flow_power_state="NSW1",
        flow_power_export_rate=4500,
    )
    assert coordinator._flow_power_happy_hour_rate() == 45.0


def test_flow_power_happy_hour_rate_zero_without_state(opt_module):
    """No state and no configured rate means no usable Happy Hour rate."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="")
    assert coordinator._flow_power_happy_hour_rate() == 0.0


def test_flow_power_happy_hour_slot_found(opt_module):
    """Slot index matches the next 17:30 in the LP horizon."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    assert coordinator._next_flow_power_happy_hour_slot() == 18


def test_flow_power_happy_hour_slot_none_for_other_provider(opt_module):
    coordinator = _coordinator(opt_module, provider="agl", flow_power_state="NSW1")
    assert coordinator._next_flow_power_happy_hour_slot() is None


def test_flow_power_happy_hour_slot_none_without_rate(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="")
    assert coordinator._next_flow_power_happy_hour_slot() is None


def test_flow_power_happy_hour_slot_none_without_grid_charge(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    coordinator._config.allow_grid_charge = False
    assert coordinator._next_flow_power_happy_hour_slot() is None


def test_flow_power_export_window_slots_default_end(opt_module):
    """Default 17:30-19:30 window produces four 30-minute slots."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    slots = coordinator._flow_power_export_window_slots(48)
    assert sum(slots) == 4
    assert slots[18] is True
    assert slots[21] is True
    assert slots[22] is False


def test_flow_power_export_window_slots_extended_end(opt_module):
    """Configured 17:30-21:30 window produces eight 30-minute slots."""
    coordinator = _coordinator(
        opt_module,
        provider="flow_power",
        flow_power_state="NSW1",
        flow_power_happy_hour_end="21:30",
    )
    slots = coordinator._flow_power_export_window_slots(48)
    assert sum(slots) == 8
    assert slots[18] is True
    assert slots[23] is True


def test_flow_power_pre_window_fill_arms_at_happy_hour(opt_module):
    """Flow Power auto-arms the pre-window floor with the SOC-cap target."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    slot, target = coordinator._pre_window_fill_target()
    assert slot == 18
    assert target == 1.0


def test_flow_power_pre_window_fill_uses_grid_charge_soc_cap(opt_module):
    """The pre-window target mirrors grid_charge_soc_cap, not 100% always."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    coordinator._config.grid_charge_soc_cap = 0.8
    slot, target = coordinator._pre_window_fill_target()
    assert slot == 18
    assert target == 0.8


def test_charge_by_time_ignored_for_flow_power(opt_module):
    """Charge By Time is disabled for Flow Power; the Happy Hour floor applies."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    coordinator._config.charge_by_time_enabled = True
    coordinator._config.charge_by_time_target_time = "17:15"
    slot, target = coordinator._pre_window_fill_target()
    assert slot == 18
    assert target == 1.0


def test_charge_by_time_enabled_forced_off_for_flow_power(opt_module):
    """The charge_by_time_enabled property reports False for Flow Power."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    coordinator._config.charge_by_time_enabled = True
    assert coordinator.charge_by_time_enabled is False


def test_charge_by_time_enabled_honored_for_other_provider(opt_module):
    """Other providers keep the configured charge-by-time state."""
    coordinator = _coordinator(opt_module, provider="amber", flow_power_state="NSW1")
    coordinator._config.charge_by_time_enabled = True
    assert coordinator.charge_by_time_enabled is True


def test_flow_power_pre_window_fill_none_without_grid_charge(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    coordinator._config.allow_grid_charge = False
    assert coordinator._pre_window_fill_target() == (None, 0.0)


def test_flow_power_pre_window_fill_none_without_rate(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="")
    assert coordinator._pre_window_fill_target() == (None, 0.0)


def test_flow_power_pre_window_fill_none_for_other_provider(opt_module):
    coordinator = _coordinator(opt_module, provider="agl", flow_power_state="NSW1")
    assert coordinator._pre_window_fill_target() == (None, 0.0)
