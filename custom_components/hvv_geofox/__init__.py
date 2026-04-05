"""HVV Geofox integration for Home Assistant."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import time
from datetime import datetime, timedelta

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_PASSWORD,
    CONF_POLL_INTERVAL,
    CONF_SERVER,
    CONF_STATIONS,
    CONF_USERNAME,
    CONF_WALK_OFFSET,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SERVER,
    DEFAULT_WALK_OFFSET,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]

_GLOBAL_API_LOCK: asyncio.Lock | None = None
_LAST_CALL_END_MONO: float = 0.0


def _get_lock() -> asyncio.Lock:
    global _GLOBAL_API_LOCK
    if _GLOBAL_API_LOCK is None:
        _GLOBAL_API_LOCK = asyncio.Lock()
    return _GLOBAL_API_LOCK


async def _throttled_post(
    session: aiohttp.ClientSession,
    url: str,
    body: str,
    headers: dict,
    min_interval: float,
) -> dict:
    global _LAST_CALL_END_MONO
    lock = _get_lock()
    async with lock:
        elapsed = time.monotonic() - _LAST_CALL_END_MONO
        wait = min_interval - elapsed
        if wait > 0:
            _LOGGER.debug("GTI throttle: waiting %.2fs", wait)
            await asyncio.sleep(wait)
        try:
            async with asyncio.timeout(15):
                async with session.post(url, data=body, headers=headers) as resp:
                    resp.raise_for_status()
                    result = await resp.json(content_type=None)
        finally:
            _LAST_CALL_END_MONO = time.monotonic()
    return result


class GeofoxAPIClient:
    """Authenticated async client for the Geofox GTI v3 API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        min_interval: float,
        base_url: str = DEFAULT_SERVER,
    ) -> None:
        self._username = username
        self._password = password.encode("utf-8")
        self._session = session
        self._min_interval = min_interval
        self._base_url = base_url.rstrip("/")

    def _sign(self, body: str) -> str:
        raw = hmac.new(self._password, body.encode("utf-8"), hashlib.sha1).digest()
        return base64.b64encode(raw).decode("ascii")

    def _headers(self, body: str) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "geofox-auth-type": "HmacSHA1",
            "geofox-auth-user": self._username,
            "geofox-auth-signature": self._sign(body),
        }

    async def _post(self, endpoint: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return await _throttled_post(
            self._session,
            f"{self._base_url}{endpoint}",
            body,
            self._headers(body),
            self._min_interval,
        )

    async def departure_list(self, station_id: str, station_name: str, walk_offset_min: int = 0, fetch_count: int = 20) -> dict:
        now = datetime.now()
        payload = {
            "version": 1,
            "language": "de",
            "station": {"id": station_id, "name": station_name, "type": "STATION"},
            "time": {"date": now.strftime("%d.%m.%Y"), "time": now.strftime("%H:%M")},
            "maxList": fetch_count,
            "maxTimeOffset": max(120, walk_offset_min + 60),
            "useRealtime": True,
        }
        return await self._post("/gti/public/departureList", payload)

    async def get_announcements(self) -> dict:
        return await self._post("/gti/public/getAnnouncements", {"version": 1, "language": "de"})

    async def get_route(self, origin_id: str, origin_name: str, dest_id: str, dest_name: str, walk_offset_min: int = 0) -> dict:
        departure_time = datetime.now() + timedelta(minutes=walk_offset_min)
        payload = {
            "version": 1,
            "language": "de",
            "start": {"id": origin_id, "name": origin_name, "type": "STATION"},
            "dest": {"id": dest_id, "name": dest_name, "type": "STATION"},
            "time": {"date": departure_time.strftime("%d.%m.%Y"), "time": departure_time.strftime("%H:%M")},
            "timeIsDeparture": True,
            "numberOfSchedules": 3,
            "withRealtime": True,
        }
        return await self._post("/gti/public/getRoute", payload)

    async def check_name(self, query: str) -> dict:
        payload = {
            "version": 1,
            "language": "de",
            "theName": {"name": query, "type": "STATION"},
            "maxList": 10,
            "coordinateType": "EPSG_4326",
        }
        return await self._post("/gti/public/checkName", payload)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = HVVGeofoxCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        coordinator: HVVGeofoxCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        if coordinator._session and not coordinator._session.closed:
            await coordinator._session.close()
    return unload_ok


class HVVGeofoxCoordinator(DataUpdateCoordinator):

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        def _cfg(key, default):
            return entry.options.get(key, entry.data.get(key, default))

        poll_interval = int(max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, _cfg(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL))))
        self._min_interval = float(poll_interval)

        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=timedelta(seconds=poll_interval))
        self.entry = entry
        self._session: aiohttp.ClientSession | None = None
        self._client: GeofoxAPIClient | None = None

    def _ensure_client(self) -> GeofoxAPIClient:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        if self._client is None:
            self._client = GeofoxAPIClient(
                self.entry.data[CONF_USERNAME],
                self.entry.data[CONF_PASSWORD],
                self._session,
                self._min_interval,
                base_url=self.entry.data.get(CONF_SERVER, DEFAULT_SERVER),
            )
        return self._client

    def _global_walk(self) -> int:
        return int(self.entry.options.get(CONF_WALK_OFFSET, self.entry.data.get(CONF_WALK_OFFSET, DEFAULT_WALK_OFFSET)))

    def _walk_for(self, obj: dict) -> int:
        return int(obj.get("walk_offset", self._global_walk()))

    async def _async_update_data(self) -> dict:
        client = self._ensure_client()
        result: dict = {"stations": {}, "announcements": [], "routes": {}}

        for station in self.entry.data.get(CONF_STATIONS, []):
            sid, sname, walk = station.get("id", ""), station.get("name", ""), self._walk_for(station)
            try:
                data = await client.departure_list(sid, sname, walk_offset_min=walk)
            except Exception as err:
                raise UpdateFailed(f"departureList failed for '{sname}': {err}") from err
            if data.get("returnCode") != "OK":
                _LOGGER.warning("departureList error for %s: %s", sname, data.get("errorText"))
                continue
            result["stations"][sid] = {"name": sname, "walk_offset_min": walk, "departures_raw": data.get("departures", [])}

        try:
            ann = await client.get_announcements()
            if ann.get("returnCode") == "OK":
                result["announcements"] = ann.get("announcements", [])
        except Exception as err:
            _LOGGER.warning("getAnnouncements failed: %s", err)

        for route in self.entry.data.get("routes", []):
            rkey = f"{route['origin_id']}_{route['dest_id']}"
            walk = self._walk_for(route)
            try:
                rdata = await client.get_route(route["origin_id"], route["origin_name"], route["dest_id"], route["dest_name"], walk_offset_min=walk)
            except Exception as err:
                _LOGGER.warning("getRoute failed for %s: %s", rkey, err)
                continue
            if rdata.get("returnCode") != "OK":
                _LOGGER.warning("getRoute error %s: %s", rkey, rdata.get("errorText"))
                continue
            result["routes"][rkey] = {"origin": route["origin_name"], "dest": route["dest_name"], "walk_offset_min": walk, "schedules": rdata.get("schedules", [])}

        return result
