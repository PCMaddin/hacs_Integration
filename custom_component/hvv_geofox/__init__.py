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
import async_timeout

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

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
PLATFORMS = [Platform.SENSOR]

GTI_BASE_URL = "https://gti.geofox.de"


# ═══════════════════════════════════════════════════════════
#  Global rate-limit gate
#
#  One asyncio.Lock + one float timestamp ensures that no
#  matter how many stations, routes or HA reloads happen,
#  at most 1 HTTP request is in flight at a time and the gap
#  between the END of one call and the START of the next is
#  always >= min_interval seconds.
#
#  Module-level so multiple config entries share the gate.
# ═══════════════════════════════════════════════════════════

_GLOBAL_API_LOCK: asyncio.Lock | None = None   # created lazily (needs running loop)
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
    """
    Send one POST, guaranteed to start no sooner than
    min_interval seconds after the previous call finished.

    Callers queue up on the lock – first in, first out.
    Each caller:
      1. Acquires the lock (blocks until previous holder releases it)
      2. Computes remaining wait = min_interval - time_since_last_end
      3. Sleeps for remaining wait (if any)
      4. Sends the request
      5. Records the finish time
      6. Releases the lock → next caller proceeds

    This guarantees the inter-call gap even across concurrent
    update cycles (e.g. HA restart + normal poll colliding).
    """
    global _LAST_CALL_END_MONO

    lock = _get_lock()
    async with lock:
        elapsed = time.monotonic() - _LAST_CALL_END_MONO
        wait = min_interval - elapsed
        if wait > 0:
            _LOGGER.debug("GTI throttle: waiting %.2fs before next API call", wait)
            await asyncio.sleep(wait)

        try:
            async with async_timeout.timeout(15):
                async with session.post(url, data=body, headers=headers) as resp:
                    resp.raise_for_status()
                    result = await resp.json(content_type=None)
        finally:
            # Update even on error – a failed call still counts
            _LAST_CALL_END_MONO = time.monotonic()

    return result


# ═══════════════════════════════════════════════════════════
#  GTI API client
# ═══════════════════════════════════════════════════════════

class GeofoxAPIClient:
    """Authenticated async client for the Geofox GTI v3 API."""

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        min_interval: float,
    ) -> None:
        self._username = username
        self._password = password.encode("utf-8")
        self._session = session
        self._min_interval = min_interval

    # ── authentication ──────────────────────────────────────

    def _sign(self, body: str) -> str:
        """HMAC-SHA1 of *body*, Base64-encoded (GTI spec)."""
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

    # ── low-level ───────────────────────────────────────────

    async def _post(self, endpoint: str, payload: dict) -> dict:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        return await _throttled_post(
            self._session,
            f"{GTI_BASE_URL}{endpoint}",
            body,
            self._headers(body),
            self._min_interval,
        )

    # ── public endpoints ────────────────────────────────────

    async def departure_list(
        self,
        station_id: str,
        station_name: str,
        walk_offset_min: int = 0,
        fetch_count: int = 20,
    ) -> dict:
        """
        Fetch raw departures.  We always request more than 3 so
        the sensor can filter by walk_offset and still find 3
        reachable ones.  maxTimeOffset is widened to accommodate
        the walk plus a 60-minute lookahead.
        """
        now = datetime.now()
        payload = {
            "version": 1,
            "language": "de",
            "station": {"id": station_id, "name": station_name, "type": "STATION"},
            "time": {
                "date": now.strftime("%d.%m.%Y"),
                "time": now.strftime("%H:%M"),
            },
            "maxList": fetch_count,
            "maxTimeOffset": max(120, walk_offset_min + 60),
            "useRealtime": True,
        }
        return await self._post("/gti/public/departureList", payload)

    async def get_announcements(self) -> dict:
        payload = {"version": 1, "language": "de"}
        return await self._post("/gti/public/getAnnouncements", payload)

    async def get_route(
        self,
        origin_id: str,
        origin_name: str,
        dest_id: str,
        dest_name: str,
        walk_offset_min: int = 0,
    ) -> dict:
        """
        Request connections starting at now + walk_offset so the
        API already returns only connections the user can catch.
        """
        departure_time = datetime.now() + timedelta(minutes=walk_offset_min)
        payload = {
            "version": 1,
            "language": "de",
            "start": {"id": origin_id, "name": origin_name, "type": "STATION"},
            "dest": {"id": dest_id, "name": dest_name, "type": "STATION"},
            "time": {
                "date": departure_time.strftime("%d.%m.%Y"),
                "time": departure_time.strftime("%H:%M"),
            },
            "timeIsDeparture": True,
            "numberOfSchedules": 3,
            "withRealtime": True,
        }
        return await self._post("/gti/public/getRoute", payload)

    async def check_name(self, name: str) -> dict:
        payload = {
            "version": 1,
            "language": "de",
            "theName": {"name": name, "type": "STATION"},
            "maxList": 5,
            "coordinateType": "EPSG_4326",
        }
        return await self._post("/gti/public/checkName", payload)


# ═══════════════════════════════════════════════════════════
#  HA lifecycle
# ═══════════════════════════════════════════════════════════

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


# ═══════════════════════════════════════════════════════════
#  Coordinator
# ═══════════════════════════════════════════════════════════

class HVVGeofoxCoordinator(DataUpdateCoordinator):
    """
    Schedules updates and owns the API client.

    Rate-limit maths
    ────────────────
    Each update cycle makes exactly:
        len(stations) + 1 (announcements) + len(routes)  calls.

    With min_interval = poll_interval seconds, the minimum
    wall-time for one cycle is:
        total_calls × min_interval

    The coordinator warns if poll_interval < that minimum so
    users understand why the next cycle may start late.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        def _cfg(key, default):
            return entry.options.get(key, entry.data.get(key, default))

        poll_interval = int(
            max(MIN_POLL_INTERVAL, min(MAX_POLL_INTERVAL, _cfg(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)))
        )
        # min_interval == poll_interval: rate limit = 1 call / interval
        self._min_interval: float = float(poll_interval)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=poll_interval),
        )
        self.entry = entry
        self._session: aiohttp.ClientSession | None = None
        self._client: GeofoxAPIClient | None = None

        # Advisory warning
        stations = entry.data.get(CONF_STATIONS, [])
        routes = entry.data.get("routes", [])
        total_calls = len(stations) + 1 + len(routes)
        min_cycle = total_calls * self._min_interval
        if min_cycle > poll_interval:
            _LOGGER.warning(
                "HVV Geofox: %d stations + 1 announcement + %d routes = %d API calls. "
                "At min_interval=%.0fs that takes at least %.0fs per cycle, "
                "but poll_interval=%ds. Increase poll_interval to >= %ds to avoid drift.",
                len(stations), len(routes), total_calls,
                self._min_interval, min_cycle, poll_interval, int(min_cycle) + 1,
            )

    def _ensure_client(self) -> GeofoxAPIClient:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        if self._client is None:
            self._client = GeofoxAPIClient(
                self.entry.data[CONF_USERNAME],
                self.entry.data[CONF_PASSWORD],
                self._session,
                self._min_interval,
            )
        return self._client

    def _global_walk(self) -> int:
        return int(
            self.entry.options.get(
                CONF_WALK_OFFSET,
                self.entry.data.get(CONF_WALK_OFFSET, DEFAULT_WALK_OFFSET),
            )
        )

    def _walk_for(self, obj: dict) -> int:
        """Station or route dict may override the global walk offset."""
        return int(obj.get("walk_offset", self._global_walk()))

    async def _async_update_data(self) -> dict:
        client = self._ensure_client()
        stations: list[dict] = self.entry.data.get(CONF_STATIONS, [])
        routes: list[dict] = self.entry.data.get("routes", [])
        result: dict = {"stations": {}, "announcements": [], "routes": {}}

        # ── 1. Departures ───────────────────────────────────
        for station in stations:
            sid = station.get("id", "")
            sname = station.get("name", "")
            walk = self._walk_for(station)
            try:
                data = await client.departure_list(sid, sname, walk_offset_min=walk)
            except Exception as err:
                raise UpdateFailed(f"departureList failed for '{sname}': {err}") from err

            if data.get("returnCode") != "OK":
                _LOGGER.warning(
                    "departureList '%s' for %s: %s",
                    data.get("returnCode"), sname, data.get("errorText"),
                )
                continue

            result["stations"][sid] = {
                "name": sname,
                "walk_offset_min": walk,
                "departures_raw": data.get("departures", []),
            }

        # ── 2. Announcements ────────────────────────────────
        try:
            ann = await client.get_announcements()
            if ann.get("returnCode") == "OK":
                result["announcements"] = ann.get("announcements", [])
            else:
                _LOGGER.warning("getAnnouncements '%s': %s", ann.get("returnCode"), ann.get("errorText"))
        except Exception as err:
            _LOGGER.warning("getAnnouncements failed: %s", err)

        # ── 3. Routes ───────────────────────────────────────
        for route in routes:
            rkey = f"{route['origin_id']}_{route['dest_id']}"
            walk = self._walk_for(route)
            try:
                rdata = await client.get_route(
                    route["origin_id"], route["origin_name"],
                    route["dest_id"], route["dest_name"],
                    walk_offset_min=walk,
                )
            except Exception as err:
                _LOGGER.warning("getRoute failed for %s: %s", rkey, err)
                continue

            if rdata.get("returnCode") != "OK":
                _LOGGER.warning("getRoute '%s': %s", rdata.get("returnCode"), rdata.get("errorText"))
                continue

            result["routes"][rkey] = {
                "origin": route["origin_name"],
                "dest": route["dest_name"],
                "walk_offset_min": walk,
                "schedules": rdata.get("schedules", []),
            }

        return result
