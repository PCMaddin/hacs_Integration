# HVV Geofox – Home Assistant Custom Integration v2.1

## Rate-Limit-Garantie

Der globale Throttle in `_throttled_post` stellt sicher:

> **Zu jedem Zeitpunkt ist maximal 1 HTTP-Call zur GTI-API unterwegs,
> und der Abstand zwischen dem Ende des letzten Calls und dem Start des
> nächsten beträgt immer ≥ poll_interval Sekunden.**

Das gilt auch über mehrere Config-Entries, HA-Reloads und gleichzeitige
Update-Zyklen hinweg – dank eines `asyncio.Lock` auf Modul-Ebene.

**Hinweis:** Bei vielen Stationen/Routen verlängert sich ein Update-Zyklus
entsprechend (N Calls × poll_interval). HA loggt eine Warnung wenn die
Konfiguration unrealistisch ist.

---

## Fußweg-Offset

Der Offset gibt an, wie lange du zur Haltestelle läufst.
Es werden **nur Bahnen angezeigt, die du noch erreichen kannst**.

| Ebene | Konfiguration |
|---|---|
| Global | `walk_offset` in den Integration-Einstellungen |
| Pro Haltestelle | `Altona: Master: 7` (überschreibt global) |
| Pro Route | `Master/Altona > Hamburger Straße/Hamburg Hbf: 7` |

---

## Installation

1. `hvv_geofox/` nach `config/custom_components/hvv_geofox/` kopieren
2. Home Assistant neu starten
3. **Einstellungen → Integrationen → HVV Geofox hinzufügen**

---

## Haltestellen-Format

```
Name: StationsID
Name: StationsID: FußwegMinuten
```

Beispiel:
```
Altona: Master
Hamburg Hbf: Hamburger Straße: 8
Harburg: 34567: 3
```

## Routen-Format

```
origin_id/Origin Name > dest_id/Dest Name
origin_id/Origin Name > dest_id/Dest Name: FußwegMinuten
```

Beispiel:
```
Master/Altona > Hamburger Straße/Hamburg Hbf
34567/Harburg > Hamburger Straße/Hamburg Hbf: 5
```

---

## Sensor-Attribute: Haltestelle

State: `U3 → Barmbek  14:34 (+2min)  noch 6min`

```yaml
station_id: "Master"
station_name: "Altona"
walk_offset_min: 7
next_departures:
  - line: "U3"
    direction: "Barmbek"
    type: "TRAIN"
    planned: "14.07.2025 14:32"
    realtime: "14:34"        # planned + delay
    delay_min: 2
    minutes_until: 6         # realtime - now
    catchable_in_min: -1     # minutes_until - walk (negativ = schon weg)
    cancelled: false
    platform: "2"
  - ...
disruptions:
  - title: "U3 Signalstörung"
    lines: ["U3"]
disruption_count: 1
```

## Sensor-Attribute: Route

State: `ab 14:35 → an 14:50  ✓ störungsfrei`

```yaml
origin: "Altona"
destination: "Hamburg Hbf"
walk_offset_min: 7
connections:
  - departure: "14:35"
    arrival: "14:50"
    duration_min: 15
    changes: 0
    legs:
      - line: "S3"
        dep_planned: "14:35"
        delay_dep_min: 0
route_ok: true
route_disruption_count: 0
route_disruptions: []
```

---

## Beispiel-Automation: Warnung wenn Route gestört

```yaml
alias: "HVV: Route Altona → Hbf gestört"
trigger:
  - platform: state
    entity_id: sensor.hvv_altona_hamburg_hbf
    attribute: route_ok
    to: false
action:
  - service: notify.mobile_app_meinhandy
    data:
      title: "⚠️ HVV Störung auf deiner Route"
      message: >
        {{ state_attr('sensor.hvv_altona_hamburg_hbf', 'route_disruptions')[0]['title'] }}
```
