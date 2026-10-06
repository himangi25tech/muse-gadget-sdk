# Copyright (c) Meta Platforms, Inc. and affiliates.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Community charging recommendation for the account this gadget runs as.

Reads ~/muse/energy.json only. A caller cannot point it at another path.
Returns a recommendation. It does not set charger power.
"""

from __future__ import annotations

import json
import os
import sys

MAX_FILE_BYTES = 24 * 1024
MAX_CARS = 40
DT = 0.25
RULES = (
    "Recommend only. Never set charger power, spend money, or place an order. "
    "The charger and the car decide whether a rate is allowed."
)
TEMPLATE = {
    "site_limit_kw": 180,
    "building_kw": 100,
    "solar_kw": 0,
    "arrive": "18:00",
    "depart": "07:00",
    "cars": [{"name": "A-101", "kwh": 24, "kw_max": 7}],
}


def _path(root: str | None = None) -> str:
    directory = root or os.path.join(os.path.expanduser("~"), "muse")
    return os.path.join(directory, "energy.json")


def _minutes(value: object) -> int | None:
    if not isinstance(value, str) or len(value) != 5 or value[2] != ":":
        return None
    hour, minute = value[:2], value[3:]
    if not (hour.isdigit() and minute.isdigit()):
        return None
    h, m = int(hour), int(minute)
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def _number(value: object, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number < lo or number > hi:
        return None
    return number


def _slots(start: int, end: int) -> int:
    if end <= start:
        end += 24 * 60
    return (end - start) // 15


def _recommend(data: dict) -> dict:
    limit = _number(data.get("site_limit_kw"), 0.1, 100000)
    building = _number(data.get("building_kw"), 0, 100000)
    solar = _number(data.get("solar_kw"), 0, 100000)
    start = _minutes(data.get("arrive"))
    end = _minutes(data.get("depart"))
    cars = data.get("cars")
    if None in (limit, building, solar, start, end) or not isinstance(cars, list) or not cars:
        raise ValueError("energy.json needs site_limit_kw, building_kw, solar_kw, arrive, depart, and cars")
    if len(cars) > MAX_CARS:
        raise ValueError(f"at most {MAX_CARS} cars")
    count = _slots(start, end)
    if count < 1 or count > 96:
        raise ValueError("departure must be 15 minutes to 24 hours after arrival")
    fleet = []
    for car in cars:
        if not isinstance(car, dict):
            raise ValueError("each car must be an object")
        name = car.get("name")
        kwh = _number(car.get("kwh"), 0.1, 200)
        kw_max = _number(car.get("kw_max"), 0.1, 22)
        if not isinstance(name, str) or not name.strip() or kwh is None or kw_max is None:
            raise ValueError("each car needs name, kwh, and kw_max")
        fleet.append((name.strip(), kwh, kw_max))

    now = [0.0] * count
    unmet_now = 0.0
    for _name, kwh, kw_max in fleet:
        left = kwh
        for t in range(count):
            if left <= 1e-9:
                break
            take = min(kw_max, left / DT)
            now[t] += take
            left -= take * DT
        unmet_now += max(0.0, left)

    power = [[0.0] * count for _ in fleet]
    unmet = [0.0] * len(fleet)
    # ponytail: greedy lowest-slot fill, not MPC. Replace when a meter feed exists.
    for i in range(len(fleet)):
        _name, kwh, kw_max = fleet[i]
        left = kwh
        while left > 1e-6:
            best = None
            best_grid = None
            for t in range(count):
                others = sum(power[j][t] for j in range(len(fleet)))
                grid_now = building + others - solar
                room = min(kw_max - power[i][t], limit - grid_now)
                if room <= 1e-9:
                    continue
                if best_grid is None or grid_now < best_grid:
                    best_grid = grid_now
                    best = t
            if best is None:
                break
            others = sum(power[j][best] for j in range(len(fleet)))
            room = min(kw_max - power[i][best], limit - (building + others - solar), left / DT)
            power[i][best] += room
            left -= room * DT
        unmet[i] = max(0.0, left)

    spread = [sum(power[i][t] for i in range(len(fleet))) for t in range(count)]
    peak_now = max(building + p - solar for p in now)
    peak_spread = max(building + p - solar for p in spread)
    late = [fleet[i][0] for i in range(len(fleet)) if unmet[i] >= 0.05]
    depart = data.get("depart")
    if late:
        say = (
            f"{len(fleet)} cars would draw the site to {peak_now:.0f} kW. "
            f"The cap is {limit:.0f} kW. Spreading still leaves {', '.join(late)} short of energy "
            f"by {depart}. I did not change any charger."
        )
    else:
        say = (
            f"If these {len(fleet)} cars charge at full power now, the site hits {peak_now:.0f} kW"
            f"{'' if peak_now <= limit else f', {peak_now - limit:.0f} kW over the {limit:.0f} kW cap'}. "
            f"Spreading the same energy keeps the peak at {peak_spread:.0f} kW and every car ready by {depart}. "
            "I did not change any charger."
        )
    return {
        "rules": RULES,
        "missing": False,
        "say": say,
        "site_limit_kw": limit,
        "building_kw": building,
        "solar_kw": solar,
        "cars": len(fleet),
        "peak_if_now_kw": round(peak_now, 1),
        "peak_if_spread_kw": round(peak_spread, 1),
        "overload_kw": round(max(0.0, peak_now - limit), 1),
        "on_time": not late,
        "late": late,
        "uncontrolled_unmet_kwh": round(unmet_now, 1),
    }


def load(root: str | None = None) -> dict:
    path = _path(root)
    try:
        with open(path, "r", encoding="utf-8") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return {
            "rules": RULES,
            "missing": True,
            "path": path,
            "say": "No community energy file yet. Fill ~/muse/energy.json, then ask again. I did not change any charger.",
            "template": TEMPLATE,
        }
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("energy.json is over 24 KiB")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("energy.json must be an object")
    return _recommend(data)


def main() -> int:
    try:
        payload = load()
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        json.dump({"ok": False, "error": str(exc)}, sys.stdout)
        return 0
    json.dump({"ok": True, "payload": payload}, sys.stdout)
    return 0


if __name__ == "__main__":
    sys.exit(main())
