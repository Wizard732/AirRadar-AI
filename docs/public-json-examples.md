# Публичный JSON: было / стало (синтетические данные)

Сгенерировано из `test_public_view._private()` через `public_view.build_public`.

## /api/tracks и tracks.json — было
```json
{
 "now": 1700000000,
 "tracks": [
  {
   "id": "t1",
   "weapon": "shahed",
   "status": "active",
   "sources": 2,
   "count": 3,
   "speed_kmh": 180.0,
   "speed_source": "measured",
   "heading": 45,
   "heading_text": "схід",
   "now": {
    "lat": 48.61234,
    "lon": 35.34321,
    "estimated": true
   },
   "points": [
    {
     "ts": 1699999301,
     "lat": 48.6,
     "lon": 35.3,
     "count": 3,
     "place": "X",
     "precision": "place"
    },
    {
     "ts": 1699999901,
     "lat": 48.61,
     "lon": 35.309999999999995,
     "count": 3,
     "place": "Y",
     "precision": "place"
    }
   ],
   "forecast": [
    {
     "name": "Z",
     "lat": 50.1234,
     "lon": 30.1234,
     "eta_min": 20,
     "km": 25
    }
   ],
   "dest": {
    "lat": 50.4501,
    "lon": 30.5234,
    "name": "Київ"
   }
  }
 ],
 "events": [
  {
   "weapon": "kab",
   "lat": 48.601,
   "lon": 35.301,
   "ts": 1699999940,
   "count": 1,
   "place": "P",
   "oblast": "O",
   "precision": "place",
   "toward": false
  }
 ],
 "impacts": [
  {
   "ts": 1699982077,
   "oblast": "Київська",
   "slug": "kyivska",
   "place": "Місто-А",
   "lat": 50.4501,
   "lon": 30.5234,
   "kind": "hit",
   "killed": 1,
   "injured": 2,
   "children": 0,
   "damage": [
    "energy"
   ],
   "flags": "",
   "precision": "place"
  }
 ],
 "feed": [
  {
   "ts": 1699999800,
   "text": "4 на Сумщині — Суми, Низи; курсом на Куп'янськ",
   "region": "sumska",
   "type": "shahed"
  }
 ]
}
```

## /api/tracks и tracks.json — стало
```json
{
 "schema": 1,
 "public": true,
 "notice": "Положення показано узагальнено за районом; точні координати об'єктів не публікуються.",
 "delay_min": 10,
 "now": 1699999800,
 "tracks": [
  {
   "id": "9c51bce0b1",
   "weapon": "cruise_missile",
   "profile": "",
   "status": "active",
   "count": 1,
   "count_bucket": "1",
   "sources": 3,
   "sources_bucket": "3+",
   "official": null,
   "speed_kmh": null,
   "speed_source": "none",
   "heading": null,
   "heading_text": "",
   "now": {
    "lat": 47.21,
    "lon": 30.6,
    "estimated": true
   },
   "points": [
    {
     "ts": 1699999800,
     "lat": 47.21,
     "lon": 30.6,
     "count": 1,
     "place": "Березівський",
     "precision": "region"
    }
   ],
   "forecast": [],
   "dest": null,
   "raion_key": "Березівський|Одеська",
   "category": "missile",
   "region": "odeska"
  }
 ],
 "events": [
  {
   "weapon": "kab",
   "lat": 48.86,
   "lon": 35.22,
   "ts": 1699999800,
   "count": 1,
   "count_bucket": "1",
   "place": "Новомосковський",
   "oblast": "Дніпропетровська",
   "precision": "region",
   "toward": false,
   "raion_key": "Новомосковський|Дніпропетровська",
   "category": "other",
   "region": "dnipropetrovska"
  }
 ],
 "impacts": [
  {
   "ts": 1699974000,
   "when": "14.11 17:00–20:00",
   "oblast": "Київська",
   "slug": "kyivska",
   "place": "",
   "lat": null,
   "lon": null,
   "precision": "oblast",
   "kind": "hit",
   "hits_label": "1",
   "killed_range": "1+",
   "injured_range": "1+",
   "damage": [
    "energy"
   ],
   "flags": "",
   "window_h": 3
  }
 ],
 "feed": [
  {
   "ts": 1699999800,
   "text": "БпЛА — Сумська обл.",
   "region": "sumska",
   "type": "shahed"
  }
 ]
}
```

## /api/public/v1/regions (новый)
```json
{
 "schema": 1,
 "updated_at": 1699999800,
 "delay_min": 10,
 "notice": "Положення показано узагальнено за районом; точні координати об'єктів не публікуються.",
 "regions": [
  {
   "region": "odeska",
   "raion": "Березівський",
   "category": "missile",
   "status": "confirmed",
   "count_bucket": "1",
   "sources_bucket": "3+",
   "first_seen": 1699999200,
   "last_update": 1699999800,
   "stale": false
  },
  {
   "region": "dnipropetrovska",
   "raion": "Новомосковський",
   "category": "drone",
   "status": "confirmed",
   "count_bucket": "2-5",
   "sources_bucket": "2",
   "first_seen": 1699999200,
   "last_update": 1699999800,
   "stale": false
  }
 ],
 "official": {
  "sirens": [
   "dnipropetrovska"
  ],
  "sirens_raions": null,
  "sirens_raions_y": null,
  "sirens_raions_p": null,
  "siren_threats": {
   "dnipropetrovska": [
    "drones"
   ]
  }
 }
}
```

## /api/threats, элемент — было
```json
{
 "ts": 1700000137,
 "type": "shahed",
 "region": "sumska",
 "text": "4 на Сумщині — Суми, Низи; курсом на Куп'янськ",
 "sources": 4,
 "origin": "kurska",
 "destination": "kharkivska",
 "track": {
  "course_deg": 210,
  "speed_kmh": 170,
  "from": [
   51.0,
   34.9
  ],
  "to": [
   49.7,
   37.6
  ]
 },
 "age_min": 7
}
```

## /api/threats, элемент — стало
```json
{
 "ts": 1700000100,
 "type": "shahed",
 "region": "sumska",
 "text": "БпЛА — Сумська обл.",
 "sources": 3,
 "age_min": 5
}
```

## /api/risk — было
```json
{
 "risks": [
  {
   "id": "t1",
   "level": "imminent",
   "distance_km": 4.1,
   "bearing": 40,
   "approaching": true,
   "eta_min": 3,
   "speed_kmh": 180,
   "text": "…"
  }
 ]
}
```

## /api/risk — стало
```json
{
 "risks": [
  {
   "level": "imminent",
   "text": "Загроза поблизу: будьте готові піти в укриття."
  }
 ]
}
```
