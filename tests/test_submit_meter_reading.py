"""Regression tests for Korea Gas App meter-reading submission flow."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import importlib
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
import unittest


def _install_homeassistant_stubs() -> None:
    """Install minimal Home Assistant stubs needed to import the integration."""
    vol = ModuleType("voluptuous")
    vol.Schema = lambda *args, **kwargs: args[0] if args else None
    vol.Optional = lambda key, *args, **kwargs: key
    vol.Required = lambda key, *args, **kwargs: key
    vol.All = lambda *validators: validators[0] if validators else (lambda value: value)
    vol.Coerce = lambda typ: typ
    vol.Range = lambda *args, **kwargs: (lambda value: value)
    sys.modules["voluptuous"] = vol

    ha = ModuleType("homeassistant")
    sys.modules["homeassistant"] = ha

    config_entries = ModuleType("homeassistant.config_entries")
    config_entries.ConfigEntry = object
    config_entries.ConfigEntryState = SimpleNamespace(LOADED="loaded")
    sys.modules["homeassistant.config_entries"] = config_entries

    const = ModuleType("homeassistant.const")
    const.Platform = SimpleNamespace(BINARY_SENSOR="binary_sensor", SENSOR="sensor")
    sys.modules["homeassistant.const"] = const

    core = ModuleType("homeassistant.core")
    core.CALLBACK_TYPE = object
    core.HomeAssistant = object
    core.ServiceCall = object
    sys.modules["homeassistant.core"] = core

    exceptions = ModuleType("homeassistant.exceptions")

    class HomeAssistantError(Exception):
        """Stubbed Home Assistant error."""

    exceptions.HomeAssistantError = HomeAssistantError
    sys.modules["homeassistant.exceptions"] = exceptions

    helpers = ModuleType("homeassistant.helpers")
    sys.modules["homeassistant.helpers"] = helpers

    config_validation = ModuleType("homeassistant.helpers.config_validation")
    config_validation.string = str
    sys.modules["homeassistant.helpers.config_validation"] = config_validation

    event = ModuleType("homeassistant.helpers.event")

    def async_track_time_change(hass: Any, action: Any, **kwargs: Any) -> Any:
        hass.scheduled_callback = action
        hass.scheduled_kwargs = kwargs
        return lambda: None

    event.async_track_time_change = async_track_time_change
    sys.modules["homeassistant.helpers.event"] = event

    storage = ModuleType("homeassistant.helpers.storage")

    class Store:
        """In-memory Store replacement."""

        _data_by_key: dict[str, dict[str, Any]] = {}

        def __init__(self, hass: Any, version: int, key: str) -> None:
            self.key = key

        @classmethod
        def __class_getitem__(cls, item: Any) -> type[Store]:
            return cls

        async def async_load(self) -> dict[str, Any] | None:
            data = self._data_by_key.get(self.key)
            return dict(data) if data is not None else None

        async def async_save(self, data: dict[str, Any]) -> None:
            self._data_by_key[self.key] = dict(data)

    storage.Store = Store
    sys.modules["homeassistant.helpers.storage"] = storage

    util = ModuleType("homeassistant.util")
    sys.modules["homeassistant.util"] = util
    dt = ModuleType("homeassistant.util.dt")
    dt.now = datetime.now
    sys.modules["homeassistant.util.dt"] = dt

    api = ModuleType("custom_components.korea_gasapp.api")

    class KoreaGasAppApiError(Exception):
        """Stubbed API error."""

    api.KoreaGasAppApiError = KoreaGasAppApiError
    api.KoreaGasAppClient = object
    sys.modules["custom_components.korea_gasapp.api"] = api

    coordinator = ModuleType("custom_components.korea_gasapp.coordinator")
    coordinator.KoreaGasAppDataUpdateCoordinator = object
    sys.modules["custom_components.korea_gasapp.coordinator"] = coordinator


class FakeServices:
    """Capture registered services."""

    def __init__(self) -> None:
        self.handlers: dict[tuple[str, str], Any] = {}

    def async_register(
        self,
        domain: str,
        service: str,
        handler: Any,
        *,
        schema: Any = None,
    ) -> None:
        self.handlers[(domain, service)] = handler


class FakeConfigEntries:
    """Return configured entries."""

    def __init__(self, entries: list[Any]) -> None:
        self._entries = entries

    def async_entries(self, domain: str) -> list[Any]:
        return self._entries


class FakeStates:
    """Return states by entity id."""

    def __init__(self, states: dict[str, str]) -> None:
        self._states = states

    def get(self, entity_id: str) -> Any:
        value = self._states.get(entity_id)
        if value is None:
            return None
        return SimpleNamespace(state=value)


class FakeHass:
    """Minimal Home Assistant object for service and scheduler tests."""

    def __init__(self, entry: Any) -> None:
        self.data: dict[str, Any] = {}
        self.services = FakeServices()
        self.config_entries = FakeConfigEntries([entry])
        self.states = FakeStates({"input_number.gas_meter_reading": "5708"})


@dataclass
class FakeResult:
    """Submission result returned by the fake client."""

    input_yn: str = "Y"
    usage: int = 8
    return_message: str = "ok"
    this_month_indicator: int = 5708


class FakeClient:
    """Record submitted readings."""

    def __init__(self) -> None:
        self.submissions: list[int] = []

    async def async_submit_meter_reading(self, reading: int) -> FakeResult:
        self.submissions.append(reading)
        return FakeResult()


class FailOnceClient(FakeClient):
    """Fail the first submission and succeed afterwards."""

    def __init__(self) -> None:
        super().__init__()
        self._should_fail = True

    async def async_submit_meter_reading(self, reading: int) -> FakeResult:
        self.submissions.append(reading)
        if self._should_fail:
            self._should_fail = False
            raise sys.modules[
                "custom_components.korea_gasapp.api"
            ].KoreaGasAppApiError("boom")
        return FakeResult()


class FakeCoordinator:
    """Minimal coordinator with validation data and a client."""

    def __init__(self, client: FakeClient | None = None) -> None:
        self.client = client or FakeClient()
        self.data = SimpleNamespace(last_meter_reading_m3=5700)
        self.refresh_count = 0

    async def async_request_refresh(self) -> None:
        self.refresh_count += 1


class FakeEntry:
    """Minimal config entry."""

    state = "loaded"

    def __init__(self, coordinator: FakeCoordinator) -> None:
        self.entry_id = "entry-1"
        self.title = "Gas account 1111111"
        self.data = {
            "account_id": "1111111",
            "use_contract_num": "1111111",
            "customer_no": "2222222",
            "submit_day": 1,
            "submit_time": "08:00:00",
            "reading_entity_id": "input_number.gas_meter_reading",
            "max_reading_delta": 500,
        }
        self.options: dict[str, Any] = {}
        self.runtime_data = coordinator


class SubmitMeterReadingTest(unittest.IsolatedAsyncioTestCase):
    """Tests for manual submission and scheduled fallback behavior."""

    def setUp(self) -> None:
        for name in list(sys.modules):
            if name == "custom_components.korea_gasapp" or name.startswith(
                "custom_components.korea_gasapp."
            ):
                sys.modules.pop(name)
        _install_homeassistant_stubs()
        self.integration = importlib.import_module("custom_components.korea_gasapp")
        self.integration.sleep = lambda seconds: asyncio.sleep(0)

    async def test_empty_service_data_uses_configured_entity_and_skips_monthly_fallback(
        self,
    ) -> None:
        coordinator = FakeCoordinator()
        entry = FakeEntry(coordinator)
        hass = FakeHass(entry)

        await self.integration.async_setup(hass, {})
        self.integration._schedule_auto_submission(hass, entry, coordinator)

        handler = hass.services.handlers[("korea_gasapp", "submit_meter_reading")]
        await handler(SimpleNamespace(data={}))
        await hass.scheduled_callback(
            datetime(date.today().year, date.today().month, 1, 8)
        )

        self.assertEqual(coordinator.client.submissions, [5708])

    async def test_failed_manual_submission_does_not_suppress_monthly_fallback(
        self,
    ) -> None:
        coordinator = FakeCoordinator(FailOnceClient())
        entry = FakeEntry(coordinator)
        hass = FakeHass(entry)

        await self.integration.async_setup(hass, {})
        self.integration._schedule_auto_submission(hass, entry, coordinator)

        handler = hass.services.handlers[("korea_gasapp", "submit_meter_reading")]
        with self.assertRaises(self.integration.HomeAssistantError):
            await handler(SimpleNamespace(data={}))

        await hass.scheduled_callback(
            datetime(date.today().year, date.today().month, 1, 8)
        )

        self.assertEqual(coordinator.client.submissions, [5708, 5708])


if __name__ == "__main__":
    unittest.main()
