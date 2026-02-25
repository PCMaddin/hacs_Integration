"""Sensor platform for HVV Geofox integration."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import HVVGeofoxCoordinator
from .const import CONF_STATIONS, DOMAIN

_LOGGER = logging.getLogger(__name__)

SHOW_DEPARTURES = 3  # number of reachable departures to expose


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: HVVGeofoxCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SensorEntity] = []

    for station in entry.data.get(CONF_STATIONS, []):
        entities.append(HVVDepartureSensor(coordinator, station))

    entities.append(HVVAnnouncementSensor(coordinator, entry))

    for route in entry.data.get("routes", []):
        entities.append(HVVRouteSensor(coordinator, route))

    async_add_entities(entities, True)


# ═══════════════════════════════════════════════════════════
#  Departure parsing helpers
# ═══════════════════════════════════════════════════════════

def _gti_time_to_datetime(gti: dict) -> datetime | None:
    """
    Convert a GTI time dict {"date": "DD.MM.YYYY", "time": "HH:MM"}
    to a datetime.  Returns None if the dict is malformed.
    """
    try:
        return datetime.strptime(
            f"{gti['date']} {gti['time']}", "%d.%m.%Y %H:%M"
        )
    except (KeyError, ValueError):
        return None


def _parse_departure(dep: dict, walk_offset_min: int) -> dict | None:
    """
    Parse one raw departure dict.

    Returns None if the departure cannot be reached
    (planned + delay < now + walk_offset).

    The 'reachable' timestamp is:
        planned_dt + delay_minutes  (realtime departure)
    compared against:
        now + walk_offset_min
    """
    time_info = dep.get("time", {})
    planned_dt = _gti_time_to_datetime(time_info)
    if planned_dt is None:
        return None

    delay_min: int = dep.get("delay", 0) or 0
    realtime_dt = planned_dt + timedelta(minutes=delay_min)
    earliest_catch = datetime.now() + timedelta(minutes=walk_offset_min)

    if realtime_dt < earliest_catch:
        return None   # already gone or not catchable

    minutes_left = int((realtime_dt - datetime.now()).total_seconds() / 60)

    line = dep.get("line", {})
    return {
        "line": line.get("name", "?"),
        "direction": line.get("direction", "?"),
        "type": line.get("type", {}).get("simpleType", "?"),
        "planned": f"{time_info.get('date','')} {time_info.get('time','')}".strip(),
        "realtime": realtime_dt.strftime("%H:%M"),
        "delay_min": delay_min,
        "minutes_until": minutes_left,
        "catchable_in_min": max(0, minutes_left - walk_offset_min),
        "cancelled": dep.get("cancelled", False),
        "extra": dep.get("extra", False),
        "platform": dep.get("platform", ""),
    }


def _reachable_departures(raw: list[dict], walk_offset_min: int, count: int = SHOW_DEPARTURES) -> list[dict]:
    """Return the next *count* departures reachable given walk_offset_min."""
    result = []
    for dep in raw:
        parsed = _parse_departure(dep, walk_offset_min)
        if parsed is not None:
            result.append(parsed)
            if len(result) >= count:
                break
    return result


# ═══════════════════════════════════════════════════════════
#  Announcement helpers
# ═══════════════════════════════════════════════════════════

def _parse_announcement(ann: dict) -> dict:
    return {
        "id": ann.get("id", ""),
        "title": ann.get("title", ""),
        "description": ann.get("description", ""),
        "type": ann.get("type", ""),
        "start": ann.get("startDate", ""),
        "end": ann.get("endDate", ""),
        "lines": [l.get("name", "?") for l in ann.get("affectedLines", [])],
        "stations": [s.get("name", "?") for s in ann.get("affectedStations", [])],
        "url": ann.get("url", ""),
    }


def _filter_announcements(announcements: list[dict], lines: set[str]) -> list[dict]:
    result = []
    for ann in announcements:
        affected = {l.get("name", "") for l in ann.get("affectedLines", [])}
        if not lines or affected & lines:
            result.append(_parse_announcement(ann))
    return result


# ═══════════════════════════════════════════════════════════
#  Sensors
# ═══════════════════════════════════════════════════════════

class HVVDepartureSensor(CoordinatorEntity, SensorEntity):
    """
    Shows the next N reachable departures from one station.

    'Reachable' means: realtime departure >= now + walk_offset.
    State = first reachable departure summary, e.g.
        "U3 → Barmbek  14:34 (+2min)  noch 6min"
    """

    def __init__(self, coordinator: HVVGeofoxCoordinator, station: dict) -> None:
        super().__init__(coordinator)
        self._station_id = station["id"]
        self._station_name = station["name"]
        self._attr_name = f"HVV {self._station_name}"
        self._attr_unique_id = f"hvv_dep_{self._station_id}"
        self._attr_icon = "mdi:bus-clock"

    def _station_data(self) -> dict | None:
        return self.coordinator.data.get("stations", {}).get(self._station_id)

    @property
    def native_value(self) -> str | None:
        sd = self._station_data()
        if not sd:
            return None
        deps = _reachable_departures(sd["departures_raw"], sd["walk_offset_min"], 1)
        if not deps:
            return "Keine erreichbaren Abfahrten"
        d = deps[0]
        delay_str = f" (+{d['delay_min']}min)" if d["delay_min"] > 0 else ""
        cancel_str = "  ⚠ AUSFALL" if d["cancelled"] else ""
        return (
            f"{d['line']} → {d['direction']}  "
            f"{d['realtime']}{delay_str}  "
            f"noch {d['minutes_until']}min{cancel_str}"
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        sd = self._station_data()
        if not sd:
            return {}

        walk = sd["walk_offset_min"]
        deps = _reachable_departures(sd["departures_raw"], walk, SHOW_DEPARTURES)
        active_lines = {d["line"] for d in deps}
        disruptions = _filter_announcements(
            self.coordinator.data.get("announcements", []), active_lines
        )

        return {
            "station_id": self._station_id,
            "station_name": self._station_name,
            "walk_offset_min": walk,
            # Explanation: minutes_until = realtime - now
            #              catchable_in_min = minutes_until - walk_offset (time to spare)
            "next_departures": deps,
            "departures_shown": len(deps),
            "disruptions": disruptions,
            "disruption_count": len(disruptions),
        }


class HVVAnnouncementSensor(CoordinatorEntity, SensorEntity):
    """Total count of active HVV announcements."""

    def __init__(self, coordinator: HVVGeofoxCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._attr_name = "HVV Meldungen"
        self._attr_unique_id = f"hvv_ann_{entry.entry_id}"
        self._attr_icon = "mdi:alert-circle-outline"

    @property
    def native_value(self) -> int:
        return len(self.coordinator.data.get("announcements", []))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        anns = self.coordinator.data.get("announcements", [])
        return {
            "total": len(anns),
            "announcements": [_parse_announcement(a) for a in anns],
        }


class HVVRouteSensor(CoordinatorEntity, SensorEntity):
    """
    Next 3 connections on a configured route.

    The API already received 'now + walk_offset' as departure time
    so all returned connections are reachable by definition.
    Additionally shows disruptions affecting any line on the route.
    """

    def __init__(self, coordinator: HVVGeofoxCoordinator, route: dict) -> None:
        super().__init__(coordinator)
        self._route_key = f"{route['origin_id']}_{route['dest_id']}"
        self._origin_name = route["origin_name"]
        self._dest_name = route["dest_name"]
        self._attr_name = f"HVV {self._origin_name} → {self._dest_name}"
        self._attr_unique_id = f"hvv_route_{self._route_key}"
        self._attr_icon = "mdi:train"

    def _route_data(self) -> dict | None:
        return self.coordinator.data.get("routes", {}).get(self._route_key)

    @property
    def native_value(self) -> str | None:
        rd = self._route_data()
        if not rd:
            return None
        schedules = rd.get("schedules", [])
        if not schedules:
            return "Keine Verbindungen"
        first = schedules[0]
        dep = first.get("departure", {}).get("time", "?")
        arr = first.get("arrival", {}).get("time", "?")
        disruptions = self._route_disruptions(rd)
        status = f"  ⚠ {len(disruptions)} Störung(en)" if disruptions else "  ✓ störungsfrei"
        return f"ab {dep} → an {arr}{status}"

    def _route_disruptions(self, rd: dict) -> list[dict]:
        all_lines: set[str] = set()
        for sched in rd.get("schedules", []):
            for elem in sched.get("scheduleElements", []):
                name = elem.get("line", {}).get("name", "")
                if name:
                    all_lines.add(name)
        return _filter_announcements(
            self.coordinator.data.get("announcements", []), all_lines
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        rd = self._route_data()
        if not rd:
            return {}

        connections = []
        for sched in rd.get("schedules", [])[:3]:
            legs = []
            for elem in sched.get("scheduleElements", []):
                line = elem.get("line", {})
                legs.append({
                    "line": line.get("name", "?"),
                    "direction": line.get("direction", ""),
                    "type": line.get("type", {}).get("simpleType", ""),
                    "from": elem.get("from", {}).get("name", "?"),
                    "to": elem.get("to", {}).get("name", "?"),
                    "dep_planned": elem.get("departureTime", {}).get("time", "?"),
                    "arr_planned": elem.get("arrivalTime", {}).get("time", "?"),
                    "delay_dep_min": elem.get("realtimeDepartureDelay", 0),
                    "delay_arr_min": elem.get("realtimeArrivalDelay", 0),
                })
            connections.append({
                "departure": sched.get("departure", {}).get("time", "?"),
                "arrival": sched.get("arrival", {}).get("time", "?"),
                "duration_min": sched.get("time", 0),
                "changes": max(0, len(legs) - 1),
                "legs": legs,
            })

        disruptions = self._route_disruptions(rd)

        return {
            "origin": self._origin_name,
            "destination": self._dest_name,
            "walk_offset_min": rd.get("walk_offset_min", 0),
            "connections": connections,
            "route_ok": len(disruptions) == 0,
            "route_disruptions": disruptions,
            "route_disruption_count": len(disruptions),
        }
