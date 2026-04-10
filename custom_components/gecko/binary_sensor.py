"""Binary sensor entities for Gecko spa integration."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
    BinarySensorDeviceClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from gecko_iot_client.models.flow_zone import FlowZone
from gecko_iot_client.models.temperature_control_zone import TemperatureControlZone
from gecko_iot_client.models.zone_types import ZoneType

from .const import DOMAIN
from .coordinator import GeckoVesselCoordinator
from .connection_manager import GECKO_CONNECTION_MANAGER_KEY
from .entity import GeckoEntityAvailabilityMixin
from .telemetry import get_flow_initiators, is_manual_flow_demand

_LOGGER = logging.getLogger(__name__)

BINARY_SENSOR_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = (
    BinarySensorEntityDescription(
        key="gateway_status",
        name="Gateway Status",
        icon="mdi:router-wireless",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="vessel_status",
        name="Spa Status", 
        icon="mdi:hot-tub",
        device_class=BinarySensorDeviceClass.RUNNING,
    ),
    BinarySensorEntityDescription(
        key="transport_connection",
        name="Transport Connection",
        icon="mdi:cloud-check",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
    BinarySensorEntityDescription(
        key="overall_connection",
        name="Overall Connection",
        icon="mdi:connection",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Gecko binary sensor entities from a config entry."""
    
    # Get the vessel coordinators from runtime_data
    if not hasattr(config_entry, 'runtime_data') or not config_entry.runtime_data:
        _LOGGER.error("No runtime_data found for config entry")
        return
    
    coordinators = config_entry.runtime_data.coordinators
    if not coordinators:
        _LOGGER.warning("No vessel coordinators found")
        return

    added_eco_zone_ids: dict[str, set[str]] = {}

    @callback
    def discover_new_binary_sensor_entities(
        coordinator: GeckoVesselCoordinator,
    ) -> None:
        """Discover binary sensors that depend on temperature zones."""
        vessel_key = f"{coordinator.entry_id}_{coordinator.vessel_id}"
        added_eco_zone_ids.setdefault(vessel_key, set())

        new_entities: list[BinarySensorEntity] = []
        for zone in coordinator.get_zones_by_type(ZoneType.TEMPERATURE_CONTROL_ZONE):
            zone_id = str(zone.id)
            if zone_id in added_eco_zone_ids[vessel_key]:
                continue

            if not isinstance(zone, TemperatureControlZone):
                continue

            new_entities.append(GeckoEcoModeBinarySensor(coordinator, zone))
            added_eco_zone_ids[vessel_key].add(zone_id)

        if new_entities:
            async_add_entities(new_entities)

    # Create binary sensor entities for each vessel
    entities = []
    for coordinator in coordinators:
        for description in BINARY_SENSOR_DESCRIPTIONS:
            entity = GeckoBinarySensorEntity(
                coordinator=coordinator,
                config_entry=config_entry,
                description=description,
            )
            entities.append(entity)
            _LOGGER.debug("Created binary sensor entity %s for %s", description.key, coordinator.vessel_name)
        entities.append(
            GeckoSpaInUseBinarySensor(
                coordinator=coordinator,
                config_entry=config_entry,
            )
        )
        discover_new_binary_sensor_entities(coordinator)
        coordinator.register_zone_update_callback(
            lambda coord=coordinator: discover_new_binary_sensor_entities(coord)
        )
    
    if entities:
        _LOGGER.debug("Adding %d binary sensor entities", len(entities))
        async_add_entities(entities)
    else:
        _LOGGER.debug("No binary sensor entities created")


class GeckoBinarySensorEntity(CoordinatorEntity[GeckoVesselCoordinator], BinarySensorEntity):
    """Representation of a Gecko binary sensor."""

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
        description: BinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(coordinator)
        
        self.entity_description = description
        self._monitor_id = coordinator.monitor_id
        self._vessel_name = coordinator.vessel_name
        self._vessel_id = coordinator.vessel_id
        
        # Set up entity attributes
        vessel_id_name = coordinator.vessel_name.lower().replace(" ", "_").replace("-", "_")
        self._attr_name = f"{coordinator.vessel_name} {description.name}"
        self._attr_unique_id = f"{config_entry.entry_id}_{coordinator.vessel_id}_{description.key}"
        self.entity_id = f"binary_sensor.{vessel_id_name}_{description.key}"
        
        # Device info for grouping entities
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )

    async def async_added_to_hass(self) -> None:
        """Called when entity is added to hass."""
        await super().async_added_to_hass()
        
        # Update state immediately when added to hass
        self._update_state()
        _LOGGER.debug("Binary sensor %s added to hass with initial state: %s", self._attr_name, self._attr_is_on)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_state()
        self.async_write_ha_state()

    def _update_state(self) -> None:
        """Update the binary sensor state from spa data."""
        # Access connectivity status through connection manager
        try:
            connection_manager = self.hass.data.get(GECKO_CONNECTION_MANAGER_KEY)
            
            connectivity_status = None
            if connection_manager:
                connection = connection_manager.get_connection(self._monitor_id)
                if connection:
                    # Get connectivity status from connection (updated by gecko client callbacks)
                    connectivity_status = connection.connectivity_status
                    
                    # Fallback to gecko client if connection status not yet updated
                    if not connectivity_status and connection.gecko_client:
                        connectivity_status = connection.gecko_client.connectivity_status
            
            if not connectivity_status:
                self._attr_is_on = False
                return
            
            # Update connectivity binary sensor state
            self._update_connectivity_from_status(connectivity_status)
                
        except Exception as e:
            _LOGGER.debug("Error updating connectivity binary sensor state for %s: %s", self._attr_name, e)
            self._attr_is_on = False

    def _update_connectivity_from_status(self, connectivity_status) -> None:
        """Update connectivity binary sensor state from connectivity status object."""
        try:
            if self.entity_description.key == "gateway_status":
                # Gateway status is "connected" when connected
                status = str(connectivity_status.gateway_status).lower()
                self._attr_is_on = status == "connected"
                
            elif self.entity_description.key == "vessel_status":
                # Vessel status is "running" when running
                status = str(connectivity_status.vessel_status).lower()
                self._attr_is_on = status == "running"
                
            elif self.entity_description.key == "transport_connection":
                # Transport connection is a boolean
                self._attr_is_on = bool(connectivity_status.transport_connected)
                
            elif self.entity_description.key == "overall_connection":
                # Overall connection is fully connected or not
                self._attr_is_on = bool(connectivity_status.is_fully_connected)
                
        except Exception as e:
            _LOGGER.warning("Error updating connectivity binary sensor %s: %s", self._attr_name, e)
            self._attr_is_on = False


class GeckoSpaInUseBinarySensor(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """Binary sensor for derived spa-in-use state."""

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        config_entry: ConfigEntry,
    ) -> None:
        """Initialize the spa-in-use binary sensor."""
        super().__init__(coordinator)
        vessel_id_name = coordinator.vessel_name.lower().replace(" ", "_").replace("-", "_")
        self._attr_name = f"{coordinator.vessel_name} Spa In Use"
        self._attr_unique_id = (
            f"{config_entry.entry_id}_{coordinator.vessel_id}_spa_in_use"
        )
        self.entity_id = f"binary_sensor.{vessel_id_name}_spa_in_use"
        self._attr_icon = "mdi:hot-tub"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._active_light_zone_ids: list[str] = []
        self._manual_flow_zone_ids: list[str] = []
        self._active_flow_zone_ids: list[str] = []
        self._flow_initiators_by_zone_id: dict[str, list[str]] = {}
        self._update_state()

    def _update_state(self) -> None:
        """Update the derived spa-in-use state."""
        light_zones = self.coordinator.get_zones_by_type(ZoneType.LIGHTING_ZONE)
        flow_zones = self.coordinator.get_zones_by_type(ZoneType.FLOW_ZONE)
        spa_state = self.coordinator.get_spa_state()

        self._active_light_zone_ids = [
            str(zone.id) for zone in light_zones if getattr(zone, "active", False)
        ]
        self._active_flow_zone_ids = [
            str(zone.id) for zone in flow_zones if getattr(zone, "active", False)
        ]
        self._flow_initiators_by_zone_id = {
            str(zone.id): sorted(get_flow_initiators(zone, spa_state))
            for zone in flow_zones
            if isinstance(zone, FlowZone)
        }
        self._manual_flow_zone_ids = [
            str(zone.id)
            for zone in flow_zones
            if isinstance(zone, FlowZone) and is_manual_flow_demand(zone, spa_state)
        ]

        self._attr_is_on = bool(
            self._active_light_zone_ids or self._manual_flow_zone_ids
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the zones that caused the spa to be marked in use."""
        return {
            "active_light_zone_ids": self._active_light_zone_ids,
            "manual_flow_zone_ids": self._manual_flow_zone_ids,
            "active_flow_zone_ids": self._active_flow_zone_ids,
            "flow_initiators_by_zone_id": self._flow_initiators_by_zone_id,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_state()
        self.async_write_ha_state()


class GeckoEcoModeBinarySensor(
    GeckoEntityAvailabilityMixin,
    CoordinatorEntity[GeckoVesselCoordinator],
    BinarySensorEntity,
):
    """Binary sensor for Gecko temperature eco mode."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: GeckoVesselCoordinator,
        zone: TemperatureControlZone,
    ) -> None:
        """Initialize the eco mode binary sensor."""
        super().__init__(coordinator)
        self._zone = zone
        vessel_id_name = coordinator.vessel_name.lower().replace(" ", "_").replace("-", "_")
        self._attr_name = f"{zone.name} Eco Mode"
        self._attr_unique_id = (
            f"{coordinator.entry_id}_{coordinator.vessel_id}_eco_mode_{zone.id}"
        )
        self.entity_id = f"binary_sensor.{vessel_id_name}_eco_mode_{zone.id}"
        self._attr_icon = "mdi:leaf"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, str(coordinator.vessel_id))},
        )
        self._attr_available = False
        self._update_state()

    def _update_state(self) -> None:
        """Update the eco mode state from the temperature zone."""
        mode = getattr(self._zone, "mode", None)
        self._attr_is_on = bool(mode and mode.eco)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return related temperature details."""
        return {
            "status": self._zone.status.name if self._zone.status else None,
            "current_temperature": self._zone.temperature,
            "target_temperature": self._zone.target_temperature,
        }

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._update_state()
        self.async_write_ha_state()
