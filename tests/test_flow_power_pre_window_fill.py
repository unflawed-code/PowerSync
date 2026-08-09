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


def _gate_coordinator(opt_module, **options):
    coordinator = _coordinator(
        opt_module, provider="flow_power", flow_power_state="NSW1", **options
    )
    coordinator._optimizer = SimpleNamespace(efficiency=0.9, max_charge_kw=5.0)
    return coordinator


def _happy_hour_export_prices(n, start=18, end=22):
    prices = [0.0] * n
    for t in range(start, end):
        prices[t] = 0.40
    return prices


def test_flow_power_price_gate_disables_when_import_exceeds_export(opt_module):
    """Buying at 42c to export at 40c is a loss — the fill must not force it."""
    coordinator = _gate_coordinator(opt_module)
    n = 48
    gated = coordinator._price_gated_pre_window_target(
        target_slot=18,
        target_soc=1.0,
        import_prices=[0.42] * n,
        export_prices=_happy_hour_export_prices(n),
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.77,
        capacity_wh=10000,
    )
    assert gated <= 0.77


def test_flow_power_price_gate_keeps_profitable_fill(opt_module):
    """Cheap 25c import below 40c export × round-trip efficiency still arms the fill."""
    coordinator = _gate_coordinator(opt_module)
    n = 48
    gated = coordinator._price_gated_pre_window_target(
        target_slot=18,
        target_soc=1.0,
        import_prices=[0.25] * n,
        export_prices=_happy_hour_export_prices(n),
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.5,
        capacity_wh=10000,
    )
    assert gated == 1.0


def test_flow_power_price_gate_caps_to_cheap_slots_only(opt_module):
    """Only slots at or below export × round-trip efficiency count toward the target."""
    coordinator = _gate_coordinator(opt_module)
    n = 48
    import_prices = [0.42] * n
    for t in range(0, 2):
        import_prices[t] = 0.25
    gated = coordinator._price_gated_pre_window_target(
        target_slot=18,
        target_soc=1.0,
        import_prices=import_prices,
        export_prices=_happy_hour_export_prices(n),
        solar_forecast=[0.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.3,
        capacity_wh=10000,
    )
    # 2 cheap slots × 2.5 kWh → +0.5 SOC → 0.8 reachable; 42c slots ignored.
    assert gated == pytest.approx(0.8, abs=1e-6)


def test_flow_power_price_gate_uses_free_solar_when_grid_unprofitable(opt_module):
    """Solar surplus still counts toward the fill when grid charging is loss-making."""
    coordinator = _gate_coordinator(opt_module)
    n = 48
    gated = coordinator._price_gated_pre_window_target(
        target_slot=18,
        target_soc=1.0,
        import_prices=[0.42] * n,
        export_prices=_happy_hour_export_prices(n),
        solar_forecast=[6.0] * n,
        load_forecast=[0.0] * n,
        current_soc=0.5,
        capacity_wh=10000,
    )
    assert gated == pytest.approx(1.0, abs=1e-6)


def test_flow_power_price_gate_pass_through_without_optimizer(opt_module):
    """Without an optimizer the gate leaves the configured target untouched."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    assert coordinator._optimizer is None
    gated = coordinator._price_gated_pre_window_target(
        target_slot=18,
        target_soc=1.0,
        import_prices=[0.42] * 48,
        export_prices=_happy_hour_export_prices(48),
        solar_forecast=[0.0] * 48,
        load_forecast=[0.0] * 48,
        current_soc=0.77,
        capacity_wh=10000,
    )
    assert gated == 1.0


def test_flow_power_cheap_charge_guide_discounts_midday(opt_module):
    """Flow Power LP prices inside 10:00-14:00 get the 2c nudge."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    n = 48
    guided = coordinator._flow_power_cheap_charge_guide([0.20] * n)
    assert len(guided) == n
    for idx in range(n):
        expected = 0.18 if 3 <= idx <= 10 else 0.20
        assert guided[idx] == pytest.approx(expected, abs=1e-9), idx


def test_flow_power_cheap_charge_guide_leaves_other_providers_alone(opt_module):
    """Non-Flow-Power providers see unchanged import prices."""
    coordinator = _coordinator(opt_module, provider="agl", flow_power_state="NSW1")
    prices = [0.20] * 48
    guided = coordinator._flow_power_cheap_charge_guide(prices)
    assert guided == prices
    assert guided is not prices


def test_flow_power_cheap_charge_guide_does_not_mutate_input(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    prices = [0.20] * 48
    coordinator._flow_power_cheap_charge_guide(prices)
    assert prices == [0.20] * 48


def test_flow_power_cheap_charge_guide_empty_returns_copy(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    assert coordinator._flow_power_cheap_charge_guide([]) == []


def test_flow_power_cheap_charge_guide_clamps_at_zero(opt_module):
    """A price below the 2c guide cannot go negative inside the window."""
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    n = 48
    prices = [0.01] * n
    guided = coordinator._flow_power_cheap_charge_guide(prices)
    for idx in range(n):
        expected = 0.0 if 3 <= idx <= 10 else 0.01
        assert guided[idx] == pytest.approx(expected, abs=1e-9), idx


def test_flow_power_strict_export_enabled_default_off(opt_module):
    coordinator = _coordinator(opt_module, provider="flow_power", flow_power_state="NSW1")
    assert coordinator._flow_power_strict_export_enabled() is False


def test_flow_power_strict_export_enabled_when_option_set(opt_module):
    coordinator = _coordinator(
        opt_module, provider="flow_power", flow_power_state="NSW1",
        flow_power_strict_export_window=True,
    )
    assert coordinator._flow_power_strict_export_enabled() is True


def test_flow_power_strict_export_enabled_ignored_for_other_provider(opt_module):
    coordinator = _coordinator(
        opt_module, provider="agl", flow_power_state="NSW1",
        flow_power_strict_export_window=True,
    )
    assert coordinator._flow_power_strict_export_enabled() is False


def _strict_schedule(opt_module, n=12, soc0=0.6):
    ScheduleAction = opt_module.ScheduleAction
    OptimizationSchedule = opt_module.OptimizationSchedule
    actions = []
    for pos in range(n):
        action = ScheduleAction()
        action.timestamp = f"t{pos}"
        action.action = "idle"
        action.power_w = 0.0
        action.soc = soc0 if pos == 0 else 0.5
        action.battery_charge_w = 0.0
        action.battery_discharge_w = 0.0
        actions.append(action)
    schedule = OptimizationSchedule()
    schedule.actions = actions
    return schedule


def _strict_coordinator(opt_module, **options):
    coordinator = _coordinator(
        opt_module, provider="flow_power", flow_power_state="NSW1", **options
    )
    coordinator._config.battery_capacity_wh = 10000
    coordinator._config.max_discharge_w = 5000
    coordinator._config.max_grid_export_w = None
    coordinator._optimizer = SimpleNamespace(efficiency=0.9)
    return coordinator


def test_flow_power_strict_export_continuous_to_reserve(opt_module):
    """Window slots export continuously until the reserve floor is reached."""
    coordinator = _strict_coordinator(opt_module)
    schedule = _strict_schedule(opt_module, n=12, soc0=0.6)
    window = [True] * 8 + [False] * 4
    result = coordinator._apply_flow_power_strict_export(
        schedule,
        window,
        reserve_floor=0.2,
        solar_forecast=[0.0] * 12,
        load_forecast=[0.0] * 12,
    )
    actions = result.actions
    assert actions[0].action == "export"
    assert actions[0].power_w == pytest.approx(5000)
    assert actions[0].battery_discharge_w == pytest.approx(5000)
    assert actions[0].soc == pytest.approx(0.3222, abs=1e-3)
    assert actions[1].action == "export"
    # Below floor from slot 2 onwards: hold at self-consumption.
    for pos in range(2, 8):
        assert actions[pos].action == "self_consumption", pos
        assert actions[pos].power_w == 0.0
        assert actions[pos].battery_discharge_w == 0.0
        assert actions[pos].soc == pytest.approx(0.2, abs=1e-9)
    # Outside the window the original plan is untouched.
    assert actions[8].action == "idle"
    assert actions[8].soc == 0.5


def test_flow_power_strict_export_accounts_for_home_load(opt_module):
    """Battery serves the home load first, exporting the leftover headroom."""
    coordinator = _strict_coordinator(opt_module)
    schedule = _strict_schedule(opt_module, n=4, soc0=0.9)
    result = coordinator._apply_flow_power_strict_export(
        schedule,
        [True] * 4,
        reserve_floor=0.2,
        solar_forecast=[0.5] * 4,
        load_forecast=[2.0] * 4,
    )
    action = result.actions[0]
    assert action.action == "export"
    assert action.battery_discharge_w == pytest.approx(5000)
    assert action.power_w == pytest.approx(3500)


def test_flow_power_strict_export_respects_export_cap(opt_module):
    """A configured export cap lowers the continuous export level."""
    coordinator = _strict_coordinator(opt_module)
    coordinator._config.max_grid_export_w = 3000
    schedule = _strict_schedule(opt_module, n=4, soc0=0.9)
    result = coordinator._apply_flow_power_strict_export(
        schedule,
        [True] * 4,
        reserve_floor=0.2,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
    )
    action = result.actions[0]
    assert action.action == "export"
    assert action.power_w == pytest.approx(3000)
    assert action.battery_discharge_w == pytest.approx(3000)


def test_flow_power_strict_export_holds_when_below_reserve(opt_module):
    """Starting below the reserve never manufactures an export."""
    coordinator = _strict_coordinator(opt_module)
    schedule = _strict_schedule(opt_module, n=4, soc0=0.1)
    result = coordinator._apply_flow_power_strict_export(
        schedule,
        [True] * 4,
        reserve_floor=0.2,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
    )
    for action in result.actions[:4]:
        assert action.action == "self_consumption"
        assert action.power_w == 0.0
        assert action.battery_discharge_w == 0.0
        assert action.soc == pytest.approx(0.2, abs=1e-9)


def test_flow_power_strict_export_no_window_is_noop(opt_module):
    coordinator = _strict_coordinator(opt_module)
    schedule = _strict_schedule(opt_module, n=4, soc0=0.9)
    result = coordinator._apply_flow_power_strict_export(
        schedule,
        [False] * 4,
        reserve_floor=0.2,
        solar_forecast=[0.0] * 4,
        load_forecast=[0.0] * 4,
    )
    for action in result.actions:
        assert action.action == "idle"
