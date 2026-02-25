"""Config flow for HVV Geofox – multi-step with live station search."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.selector as sel

from .const import (
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_STATIONS,
    CONF_USERNAME,
    CONF_WALK_OFFSET,
    CONF_SERVER,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_WALK_OFFSET,
    DEFAULT_SERVER,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)

MAX_STATIONS = 3


# ═══════════════════════════════════════════════════════════
#  Helper: run a checkName search using a temporary client
# ═══════════════════════════════════════════════════════════

async def _search_stations(hass, server: str, username: str, password: str, query: str) -> list[dict]:
    """
    Call checkName and return a list of
    {"id": "...", "name": "...", "label": "Name (Typ)"}
    """
    import aiohttp
    from . import GeofoxAPIClient

    try:
        async with aiohttp.ClientSession() as session:
            client = GeofoxAPIClient(username, password, session, min_interval=0, base_url=server)
            result = await client.check_name(query)
            if result.get("returnCode") != "OK":
                return []
            stations = []
            for r in result.get("results", []):
                name = r.get("name", "")
                sid = r.get("id", "")
                city = r.get("city", "")
                vtype = ", ".join(r.get("type", {}).get("simpleType", []) if isinstance(r.get("type", {}).get("simpleType"), list) else [r.get("type", {}).get("simpleType", "")])
                label = f"{name}" + (f", {city}" if city else "") + (f" [{vtype}]" if vtype and vtype != "None" else "")
                if sid and name:
                    stations.append({"id": sid, "name": name, "label": label})
            return stations[:10]
    except Exception as err:
        _LOGGER.warning("checkName failed: %s", err)
        return []


async def _test_credentials(hass, server: str, username: str, password: str) -> str | None:
    """Return None on success, error key on failure."""
    import aiohttp
    from . import GeofoxAPIClient

    try:
        async with aiohttp.ClientSession() as session:
            client = GeofoxAPIClient(username, password, session, min_interval=0, base_url=server)
            result = await client._post("/gti/public/init", {"version": 1, "language": "de"})
            if result.get("returnCode") not in ("OK", "OK_INCOMPLETEDATA"):
                return "invalid_credentials"
        return None
    except Exception:
        return "cannot_connect"


def _station_options(stations: list[dict]) -> list[sel.SelectOptionDict]:
    return [sel.SelectOptionDict(value=s["id"], label=s["label"]) for s in stations]


# ═══════════════════════════════════════════════════════════
#  Config Flow
# ═══════════════════════════════════════════════════════════

class HVVGeofoxConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Steps:
        credentials
        station_{1,2,3}_search  →  station_{1,2,3}_pick
        more_stations (ask after each pick, up to MAX_STATIONS)
        route_origin_search     →  route_origin_pick
        route_dest_search       →  route_dest_pick
        global_settings
    """
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._station_results: list[dict] = []
        self._route_origin_results: list[dict] = []
        self._route_dest_results: list[dict] = []
        self._current_station_index: int = 1  # 1-based

    # ── Step 1: credentials ─────────────────────────────────

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await _test_credentials(
                self.hass,
                user_input[CONF_SERVER],
                user_input[CONF_USERNAME],
                user_input[CONF_PASSWORD],
            )
            if error:
                errors["base"] = error
            else:
                self._data.update(user_input)
                self._current_station_index = 1
                return await self.async_step_station_search()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required(CONF_SERVER, default=DEFAULT_SERVER): sel.TextSelector(
                    sel.TextSelectorConfig(type=sel.TextSelectorType.URL)
                ),
                vol.Required(CONF_USERNAME): sel.TextSelector(),
                vol.Required(CONF_PASSWORD): sel.TextSelector(
                    sel.TextSelectorConfig(type=sel.TextSelectorType.PASSWORD)
                ),
            }),
            errors=errors,
        )

    # ── Step 2: station search ──────────────────────────────

    async def async_step_station_search(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        i = self._current_station_index

        if user_input is not None:
            query = user_input.get("query", "").strip()
            if len(query) < 2:
                errors["query"] = "query_too_short"
            else:
                results = await _search_stations(
                    self.hass,
                    self._data[CONF_SERVER],
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    query,
                )
                if not results:
                    errors["query"] = "no_results"
                else:
                    self._station_results = results
                    return await self.async_step_station_pick()

        return self.async_show_form(
            step_id="station_search",
            data_schema=vol.Schema({
                vol.Required("query"): sel.TextSelector(),
            }),
            errors=errors,
            description_placeholders={"station_number": str(i)},
        )

    # ── Step 2b: station pick ───────────────────────────────

    async def async_step_station_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        i = self._current_station_index

        if user_input is not None:
            selected_id = user_input["station_id"]
            walk = user_input.get("walk_offset", DEFAULT_WALK_OFFSET)
            station = next((s for s in self._station_results if s["id"] == selected_id), None)
            if station is None:
                errors["station_id"] = "invalid_selection"
            else:
                stations: list[dict] = self._data.setdefault(CONF_STATIONS, [])
                stations.append({
                    "id": station["id"],
                    "name": station["name"],
                    "walk_offset": walk,
                })
                # Ask if they want more stations (if < MAX_STATIONS)
                if i < MAX_STATIONS:
                    return await self.async_step_more_stations()
                else:
                    return await self.async_step_route_origin_search()

        options = _station_options(self._station_results)
        default = options[0]["value"] if options else vol.UNDEFINED

        return self.async_show_form(
            step_id="station_pick",
            data_schema=vol.Schema({
                vol.Required("station_id", default=default): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN)
                ),
                vol.Optional("walk_offset", default=DEFAULT_WALK_OFFSET): sel.NumberSelector(
                    sel.NumberSelectorConfig(min=0, max=60, step=1, unit_of_measurement="min", mode=sel.NumberSelectorMode.SLIDER)
                ),
            }),
            errors=errors,
            description_placeholders={"station_number": str(i)},
        )

    # ── Step 2c: more stations? ─────────────────────────────

    async def async_step_more_stations(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            if user_input.get("add_more"):
                self._current_station_index += 1
                self._station_results = []
                return await self.async_step_station_search()
            else:
                return await self.async_step_route_origin_search()

        return self.async_show_form(
            step_id="more_stations",
            data_schema=vol.Schema({
                vol.Required("add_more", default=False): sel.BooleanSelector(),
            }),
            description_placeholders={
                "current_count": str(self._current_station_index),
                "max_count": str(MAX_STATIONS),
            },
        )

    # ── Step 3: route origin search ─────────────────────────

    async def async_step_route_origin_search(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            query = user_input.get("query", "").strip()
            if len(query) < 2:
                errors["query"] = "query_too_short"
            else:
                results = await _search_stations(
                    self.hass,
                    self._data[CONF_SERVER],
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    query,
                )
                if not results:
                    errors["query"] = "no_results"
                else:
                    self._route_origin_results = results
                    return await self.async_step_route_origin_pick()

        return self.async_show_form(
            step_id="route_origin_search",
            data_schema=vol.Schema({vol.Required("query"): sel.TextSelector()}),
            errors=errors,
        )

    # ── Step 3b: route origin pick ──────────────────────────

    async def async_step_route_origin_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = user_input["station_id"]
            station = next((s for s in self._route_origin_results if s["id"] == selected_id), None)
            if not station:
                errors["station_id"] = "invalid_selection"
            else:
                self._data["route_origin"] = station
                return await self.async_step_route_dest_search()

        options = _station_options(self._route_origin_results)
        return self.async_show_form(
            step_id="route_origin_pick",
            data_schema=vol.Schema({
                vol.Required("station_id", default=options[0]["value"] if options else vol.UNDEFINED): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN)
                ),
            }),
            errors=errors,
        )

    # ── Step 4: route destination search ────────────────────

    async def async_step_route_dest_search(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            query = user_input.get("query", "").strip()
            if len(query) < 2:
                errors["query"] = "query_too_short"
            else:
                results = await _search_stations(
                    self.hass,
                    self._data[CONF_SERVER],
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    query,
                )
                if not results:
                    errors["query"] = "no_results"
                else:
                    self._route_dest_results = results
                    return await self.async_step_route_dest_pick()

        return self.async_show_form(
            step_id="route_dest_search",
            data_schema=vol.Schema({vol.Required("query"): sel.TextSelector()}),
            errors=errors,
        )

    # ── Step 4b: route destination pick ─────────────────────

    async def async_step_route_dest_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            selected_id = user_input["station_id"]
            station = next((s for s in self._route_dest_results if s["id"] == selected_id), None)
            if not station:
                errors["station_id"] = "invalid_selection"
            else:
                self._data["route_dest"] = station
                return await self.async_step_global_settings()

        options = _station_options(self._route_dest_results)
        return self.async_show_form(
            step_id="route_dest_pick",
            data_schema=vol.Schema({
                vol.Required("station_id", default=options[0]["value"] if options else vol.UNDEFINED): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN)
                ),
            }),
            errors=errors,
        )

    # ── Step 5: global settings ─────────────────────────────

    async def async_step_global_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            origin = self._data.pop("route_origin")
            dest = self._data.pop("route_dest")
            self._data["routes"] = [{
                "origin_id": origin["id"],
                "origin_name": origin["name"],
                "dest_id": dest["id"],
                "dest_name": dest["name"],
            }]
            self._data[CONF_POLL_INTERVAL] = user_input[CONF_POLL_INTERVAL]
            self._data[CONF_WALK_OFFSET] = user_input[CONF_WALK_OFFSET]

            return self.async_create_entry(
                title=f"HVV ({self._data[CONF_USERNAME]})",
                data=self._data,
            )

        return self.async_show_form(
            step_id="global_settings",
            data_schema=vol.Schema({
                vol.Required(CONF_POLL_INTERVAL, default=DEFAULT_POLL_INTERVAL): sel.NumberSelector(
                    sel.NumberSelectorConfig(
                        min=MIN_POLL_INTERVAL,
                        max=MAX_POLL_INTERVAL,
                        step=1,
                        unit_of_measurement="s",
                        mode=sel.NumberSelectorMode.SLIDER,
                    )
                ),
                vol.Required(CONF_WALK_OFFSET, default=DEFAULT_WALK_OFFSET): sel.NumberSelector(
                    sel.NumberSelectorConfig(
                        min=0, max=60, step=1,
                        unit_of_measurement="min",
                        mode=sel.NumberSelectorMode.SLIDER,
                    )
                ),
            }),
        )

    # ── Options flow ─────────────────────────────────────────

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HVVGeofoxOptionsFlow(config_entry)


# ═══════════════════════════════════════════════════════════
#  Options Flow  (edit after setup – same step structure)
# ═══════════════════════════════════════════════════════════

class HVVGeofoxOptionsFlow(config_entries.OptionsFlow):
    """
    Options flow allows editing:
      - Rate-limit + global walk offset (immediately)
      - Per-station walk offsets
      - Route origin/destination (re-search)
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self.config_entry = config_entry
        self._data: dict[str, Any] = dict(config_entry.data)
        self._route_origin_results: list[dict] = []
        self._route_dest_results: list[dict] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show current settings; sliders for rate-limit + global walk."""
        if user_input is not None:
            self._data[CONF_POLL_INTERVAL] = user_input[CONF_POLL_INTERVAL]
            self._data[CONF_WALK_OFFSET] = user_input[CONF_WALK_OFFSET]

            if user_input.get("change_route"):
                return await self.async_step_route_origin_search()

            self.hass.config_entries.async_update_entry(
                self.config_entry, data=self._data
            )
            return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(
                    CONF_POLL_INTERVAL,
                    default=self._data.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                ): sel.NumberSelector(sel.NumberSelectorConfig(
                    min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL, step=1,
                    unit_of_measurement="s", mode=sel.NumberSelectorMode.SLIDER,
                )),
                vol.Required(
                    CONF_WALK_OFFSET,
                    default=self._data.get(CONF_WALK_OFFSET, DEFAULT_WALK_OFFSET),
                ): sel.NumberSelector(sel.NumberSelectorConfig(
                    min=0, max=60, step=1,
                    unit_of_measurement="min", mode=sel.NumberSelectorMode.SLIDER,
                )),
                vol.Optional("change_route", default=False): sel.BooleanSelector(),
            }),
            description_placeholders={
                "current_route": (
                    f"{self._data.get('routes', [{}])[0].get('origin_name','?')} → "
                    f"{self._data.get('routes', [{}])[0].get('dest_name','?')}"
                )
            },
        )

    async def async_step_route_origin_search(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            query = user_input.get("query", "").strip()
            if len(query) < 2:
                errors["query"] = "query_too_short"
            else:
                results = await _search_stations(
                    self.hass,
                    self._data[CONF_SERVER],
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    query,
                )
                if not results:
                    errors["query"] = "no_results"
                else:
                    self._route_origin_results = results
                    return await self.async_step_route_origin_pick()
        return self.async_show_form(
            step_id="route_origin_search",
            data_schema=vol.Schema({vol.Required("query"): sel.TextSelector()}),
            errors=errors,
        )

    async def async_step_route_origin_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            station = next((s for s in self._route_origin_results if s["id"] == user_input["station_id"]), None)
            if not station:
                errors["station_id"] = "invalid_selection"
            else:
                self._data["_new_origin"] = station
                return await self.async_step_route_dest_search()
        options = _station_options(self._route_origin_results)
        return self.async_show_form(
            step_id="route_origin_pick",
            data_schema=vol.Schema({
                vol.Required("station_id", default=options[0]["value"] if options else vol.UNDEFINED): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN)
                ),
            }),
            errors=errors,
        )

    async def async_step_route_dest_search(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            query = user_input.get("query", "").strip()
            if len(query) < 2:
                errors["query"] = "query_too_short"
            else:
                results = await _search_stations(
                    self.hass,
                    self._data[CONF_SERVER],
                    self._data[CONF_USERNAME],
                    self._data[CONF_PASSWORD],
                    query,
                )
                if not results:
                    errors["query"] = "no_results"
                else:
                    self._route_dest_results = results
                    return await self.async_step_route_dest_pick()
        return self.async_show_form(
            step_id="route_dest_search",
            data_schema=vol.Schema({vol.Required("query"): sel.TextSelector()}),
            errors=errors,
        )

    async def async_step_route_dest_pick(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            station = next((s for s in self._route_dest_results if s["id"] == user_input["station_id"]), None)
            if not station:
                errors["station_id"] = "invalid_selection"
            else:
                origin = self._data.pop("_new_origin")
                self._data["routes"] = [{
                    "origin_id": origin["id"],
                    "origin_name": origin["name"],
                    "dest_id": station["id"],
                    "dest_name": station["name"],
                }]
                self.hass.config_entries.async_update_entry(
                    self.config_entry, data=self._data
                )
                return self.async_create_entry(title="", data={})
        options = _station_options(self._route_dest_results)
        return self.async_show_form(
            step_id="route_dest_pick",
            data_schema=vol.Schema({
                vol.Required("station_id", default=options[0]["value"] if options else vol.UNDEFINED): sel.SelectSelector(
                    sel.SelectSelectorConfig(options=options, mode=sel.SelectSelectorMode.DROPDOWN)
                ),
            }),
            errors=errors,
        )
