"""Regression tests for Gecko flow-initiator telemetry."""

from __future__ import annotations

from enum import Enum
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest


class FlowZoneInitiator(Enum):
    """Minimal client enum used to load telemetry without HA dependencies."""

    USER_DEMAND = "UD"
    CHECKFLOW = "CF"
    PURGE = "PU"
    FILTRATION = "FI"
    HEATING = "HT"
    COOLDOWN = "CD"
    HEAT_PUMP = "HTP"


class ZoneType(Enum):
    """Minimal client zone type used by telemetry."""

    FLOW_ZONE = "flow"
    TEMPERATURE_CONTROL_ZONE = "temperatureControl"


def _load_telemetry_module():
    """Load telemetry with only the two gecko client enums it imports."""
    modules = {
        "gecko_iot_client": ModuleType("gecko_iot_client"),
        "gecko_iot_client.models": ModuleType("gecko_iot_client.models"),
        "gecko_iot_client.models.flow_zone": ModuleType(
            "gecko_iot_client.models.flow_zone"
        ),
        "gecko_iot_client.models.zone_types": ModuleType(
            "gecko_iot_client.models.zone_types"
        ),
    }
    modules["gecko_iot_client.models.flow_zone"].FlowZoneInitiator = FlowZoneInitiator
    modules["gecko_iot_client.models.zone_types"].ZoneType = ZoneType

    path = (
        Path(__file__).parents[1]
        / "custom_components"
        / "gecko"
        / "telemetry.py"
    )
    spec = importlib.util.spec_from_file_location("gecko_telemetry_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    previous_modules = {name: sys.modules.get(name) for name in modules}
    try:
        sys.modules.update(modules)
        spec.loader.exec_module(module)
    finally:
        for name, previous_module in previous_modules.items():
            if previous_module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous_module
    return module


telemetry = _load_telemetry_module()


class FlowInitiatorTests(unittest.TestCase):
    """Verify automatic flow is distinct from manual pump demand."""

    def setUp(self) -> None:
        self.zone = SimpleNamespace(id="1", active=True, initiators_=None)

    def _spa_state(self, initiators: list[str]) -> dict:
        return {
            "state": {
                "reported": {
                    "zones": {
                        "flow": {
                            "1": {
                                "active": True,
                                "initiators_": initiators,
                            }
                        }
                    }
                }
            }
        }

    def test_filtration_is_active_and_protected_from_turn_off(self) -> None:
        state = self._spa_state(["FI"])

        self.assertTrue(telemetry.is_filtration_flow_active(self.zone, state))
        self.assertFalse(telemetry.is_checkflow_active(self.zone, state))
        self.assertFalse(telemetry.is_user_controlled_flow_active(self.zone, state))
        self.assertFalse(telemetry.is_flow_safe_to_deactivate(self.zone, state))

    def test_checkflow_is_not_mistaken_for_filtration(self) -> None:
        state = self._spa_state(["CF"])

        self.assertFalse(telemetry.is_filtration_flow_active(self.zone, state))
        self.assertTrue(telemetry.is_checkflow_active(self.zone, state))
        self.assertFalse(telemetry.is_user_controlled_flow_active(self.zone, state))
        self.assertFalse(telemetry.is_flow_safe_to_deactivate(self.zone, state))

    def test_user_demand_can_be_deactivated(self) -> None:
        state = self._spa_state(["UD"])

        self.assertTrue(telemetry.is_user_controlled_flow_active(self.zone, state))
        self.assertTrue(telemetry.is_flow_safe_to_deactivate(self.zone, state))
        self.assertEqual(
            telemetry.get_non_user_flow_initiators(self.zone, state),
            set(),
        )

    def test_user_demand_does_not_make_mixed_filtration_safe(self) -> None:
        state = self._spa_state(["FI", "UD"])

        self.assertTrue(telemetry.is_user_controlled_flow_active(self.zone, state))
        self.assertFalse(telemetry.is_flow_safe_to_deactivate(self.zone, state))

    def test_active_zone_without_initiator_data_preserves_legacy_fan_state(self) -> None:
        self.assertTrue(telemetry.is_user_controlled_flow_active(self.zone))

    def test_non_heating_temperature_status_does_not_suppress_user_demand(
        self,
    ) -> None:
        for status_name in (
            "COOLING",
            "HEAT_PUMP_COOLING",
            "HEAT_PUMP_DEFROSTING",
            "IDLE",
        ):
            with self.subTest(status_name=status_name):
                temperature_zones = [
                    SimpleNamespace(status=SimpleNamespace(name=status_name))
                ]
                self.assertEqual(
                    telemetry.get_flow_manual_demand_reason(
                        self.zone,
                        temperature_zones=temperature_zones,
                    ),
                    "no_initiator_fallback",
                )
                self.assertTrue(
                    telemetry.is_manual_flow_demand(
                        self.zone,
                        temperature_zones=temperature_zones,
                    )
                )

    def test_heating_temperature_status_remains_automatic(self) -> None:
        temperature_zones = [SimpleNamespace(status=SimpleNamespace(name="HEATING"))]

        self.assertEqual(
            telemetry.get_flow_manual_demand_reason(
                self.zone,
                temperature_zones=temperature_zones,
            ),
            "automatic_temperature_status",
        )
        self.assertFalse(
            telemetry.is_manual_flow_demand(
                self.zone,
                temperature_zones=temperature_zones,
            )
        )

    def test_user_demand_takes_precedence_over_heating(self) -> None:
        state = self._spa_state(["UD"])
        temperature_zones = [SimpleNamespace(status=SimpleNamespace(name="HEATING"))]

        self.assertEqual(
            telemetry.get_flow_manual_demand_reason(
                self.zone,
                state,
                temperature_zones,
            ),
            "user_demand_initiator",
        )
        self.assertTrue(
            telemetry.is_manual_flow_demand(
                self.zone,
                state,
                temperature_zones,
            )
        )


if __name__ == "__main__":
    unittest.main()
