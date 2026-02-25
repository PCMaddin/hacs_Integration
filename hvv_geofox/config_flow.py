"""Config flow for HVV Geofox integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_STATIONS,
    CONF_USERNAME,
    CONF_WALK_OFFSET,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WALK_OFFSET,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

_STATION_HELP = (
    "Format: Name: StationsID [: FußwegMinuten]\n"
    "Beispiel:\n"
    "  Altona: Master\n"
    "  Hamburg Hbf: Hamburger Straße: 8\n"
    "  Harburg: 34567: 3"
)

_ROUTE_HELP = (
    "Format: origin_id/Origin Name > dest_id/Dest Name [: FußwegMinuten]\n"
    "Beispiel:\n"
    "  Master/Altona > Hamburger Straße/Hamburg Hbf\n"
    "  34567/Harburg > Hamburger Straße/Hamburg Hbf: 5"
)


# ── parsing helpers ─────────────────────────────────────────

def _parse_stations(raw: str) -> list[dict]:
    stations = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(":")]
        if len(parts) >= 2:
            entry: dict = {"name": parts[0], "id": parts[1]}
            if len(parts) >= 3 and parts[2].isdigit():
                entry["walk_offset"] = int(parts[2])
            stations.append(entry)
    return stations


def _stations_to_str(stations: list[dict]) -> str:
    lines = []
    for s in stations:
        base = f"{s['name']}: {s['id']}"
        if "walk_offset" in s:
            base += f": {s['walk_offset']}"
        lines.append(base)
    return "\n".join(lines)


def _parse_routes(raw: str) -> list[dict]:
    routes = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or ">" not in line:
            continue
        # optional walk offset after last ":"
        walk = None
        if line.count(":") >= 3:
            last_colon = line.rfind(":")
            tail = line[last_colon + 1:].strip()
            if tail.isdigit():
                walk = int(tail)
                line = line[:last_colon].strip()

        parts = line.split(">")
        if len(parts) != 2:
            continue

        def _parse_side(s: str):
            s = s.strip()
            if "/" in s:
                sid, sname = s.split("/", 1)
                return sid.strip(), sname.strip()
            return s, s

        orig_id, orig_name = _parse_side(parts[0])
        dest_id, dest_name = _parse_side(parts[1])
        route: dict = {
            "origin_id": orig_id,
            "origin_name": orig_name,
            "dest_id": dest_id,
            "dest_name": dest_name,
        }
        if walk is not None:
            route["walk_offset"] = walk
        routes.append(route)
    return routes


def _routes_to_str(routes: list[dict]) -> str:
    lines = []
    for r in routes:
        base = f"{r['origin_id']}/{r['origin_name']} > {r['dest_id']}/{r['dest_name']}"
        if "walk_offset" in r:
            base += f": {r['walk_offset']}"
        lines.append(base)
    return "\n".join(lines)


# ── flow ────────────────────────────────────────────────────

class HVVGeofoxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            stations = _parse_stations(user_input.get("stations_raw", ""))
            routes = _parse_routes(user_input.get("routes_raw", ""))
            if not stations:
                errors["stations_raw"] = "no_stations"
            else:
                return self.async_create_entry(
                    title=f"HVV ({user_input[CONF_USERNAME]})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                        CONF_STATIONS: stations,
                        "routes": routes,
                        CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                        CONF_WALK_OFFSET: user_input[CONF_WALK_OFFSET],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_USERNAME): str,
                vol.Required(CONF_PASSWORD): str,
                vol.Required("stations_raw"): str,
                vol.Optional("routes_raw", default=""): str,
                vol.Optional(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                ),
                vol.Optional(CONF_WALK_OFFSET, default=DEFAULT_WALK_OFFSET): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=60)
                ),
            }),
            errors=errors,
            description_placeholders={
                "station_help": _STATION_HELP,
                "route_help": _ROUTE_HELP,
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HVVGeofoxOptionsFlow(config_entry)


class HVVGeofoxOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        d = dict(self.config_entry.data)

        if user_input is not None:
            stations = _parse_stations(user_input.get("stations_raw", ""))
            routes = _parse_routes(user_input.get("routes_raw", ""))
            if not stations:
                errors["stations_raw"] = "no_stations"
            else:
                self.hass.config_entries.async_update_entry(
                    self.config_entry,
                    data={
                        **d,
                        CONF_STATIONS: stations,
                        "routes": routes,
                        CONF_POLL_INTERVAL: user_input[CONF_POLL_INTERVAL],
                        CONF_WALK_OFFSET: user_input[CONF_WALK_OFFSET],
                    },
                )
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    "stations_raw",
                    default=_stations_to_str(d.get(CONF_STATIONS, [])),
                ): str,
                vol.Optional(
                    "routes_raw",
                    default=_routes_to_str(d.get("routes", [])),
                ): str,
                vol.Optional(
                    CONF_POLL_INTERVAL,
                    default=d.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                ),
                vol.Optional(
                    CONF_WALK_OFFSET,
                    default=d.get(CONF_WALK_OFFSET, DEFAULT_WALK_OFFSET),
                ): vol.All(
                    vol.Coerce(int), vol.Range(min=0, max=60)
                ),
            }),
            errors=errors,
        )
