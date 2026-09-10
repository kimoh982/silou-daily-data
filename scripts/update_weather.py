#!/usr/bin/env python3
import json
import math
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")
BASE_URL = "https://apis.data.go.kr/1360000/VilageFcstInfoService_2.0"

# Representative KMA grid coordinates for the 8 primary regions.
REGIONS = {
    "서울": {"nx": 60, "ny": 127},
    "부산": {"nx": 98, "ny": 76},
    "대구": {"nx": 89, "ny": 90},
    "인천": {"nx": 55, "ny": 124},
    "광주": {"nx": 58, "ny": 74},
    "대전": {"nx": 67, "ny": 100},
    "울산": {"nx": 102, "ny": 84},
    "제주": {"nx": 52, "ny": 38},
}

PTY_LABELS = {
    "0": None,
    "1": "비",
    "2": "비/눈",
    "3": "눈",
    "5": "빗방울",
    "6": "빗방울/눈날림",
    "7": "눈날림",
}

SKY_LABELS = {
    "1": "맑음",
    "3": "구름 많음",
    "4": "흐림",
}


def api_get(endpoint: str, params: dict, service_key: str) -> dict:
    # Normalize either Encoding or Decoding key from data.go.kr.
    decoded_key = unquote(service_key.strip())

    # urlencode safely re-encodes +, /, = in the decoded service key.
    query = urlencode({"serviceKey": decoded_key, **params})
    url = f"{BASE_URL}/{endpoint}?{query}"

    request = Request(
        url,
        headers={
            "User-Agent": "SILOU-DAILY/1.0",
            "Accept": "application/json",
        },
    )

    with urlopen(request, timeout=25) as response:
        raw = response.read().decode("utf-8")

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"KMA returned non-JSON response: {raw[:300]}")

    header = payload.get("response", {}).get("header", {})
    if str(header.get("resultCode")) != "00":
        raise RuntimeError(
            f"KMA API error {header.get('resultCode')}: {header.get('resultMsg')}"
        )

    return payload


def items_from(payload: dict) -> list:
    body = payload.get("response", {}).get("body", {})
    items = body.get("items", {})
    if not items:
        return []
    result = items.get("item", [])
    return result if isinstance(result, list) else []


def latest_ultra_base(now: datetime) -> tuple[str, str]:
    # Ultra-short observations can arrive a little after the hour.
    safe = now - timedelta(minutes=40)
    return safe.strftime("%Y%m%d"), safe.strftime("%H00")


def latest_village_base(now: datetime) -> tuple[str, str]:
    # Village forecast base cycles: 02, 05, 08, 11, 14, 17, 20, 23 KST.
    # Add a safety delay so we do not request a cycle before publication.
    safe = now - timedelta(minutes=20)
    cycles = [2, 5, 8, 11, 14, 17, 20, 23]

    available = [h for h in cycles if h <= safe.hour]
    if available:
        hour = max(available)
        base_date = safe.date()
    else:
        hour = 23
        base_date = (safe - timedelta(days=1)).date()

    return base_date.strftime("%Y%m%d"), f"{hour:02d}00"


def to_number(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def round1(value):
    return round(value, 1) if isinstance(value, (int, float)) else None


def apparent_temperature(temp_c, humidity, wind_ms):
    """Approximate apparent temperature from T/RH/wind.

    This is a display approximation, not an official KMA perceived-temperature index.
    """
    if temp_c is None or humidity is None or wind_ms is None:
        return temp_c
    try:
        vapor = (humidity / 100.0) * 6.105 * math.exp(
            17.27 * temp_c / (237.7 + temp_c)
        )
        at = temp_c + 0.33 * vapor - 0.70 * wind_ms - 4.0
        return round1(at)
    except Exception:
        return round1(temp_c)


def forecast_rows_for_date(items: list, date_key: str) -> list:
    return [i for i in items if str(i.get("fcstDate")) == date_key]


def nearest_future_value(rows: list, category: str, now_hhmm: str):
    candidates = [
        i for i in rows
        if i.get("category") == category and str(i.get("fcstTime", "")) >= now_hhmm
    ]
    if not candidates:
        candidates = [i for i in rows if i.get("category") == category]
    if not candidates:
        return None
    candidates.sort(key=lambda i: str(i.get("fcstTime", "")))
    return candidates[0].get("fcstValue")


def values_for_category(rows: list, category: str):
    result = []
    for i in rows:
        if i.get("category") == category:
            value = to_number(i.get("fcstValue"))
            if value is not None:
                result.append((str(i.get("fcstTime", "")), value))
    return result


def fetch_region(name: str, grid: dict, now: datetime, service_key: str) -> dict:
    nx, ny = grid["nx"], grid["ny"]

    obs_date, obs_time = latest_ultra_base(now)
    obs_payload = api_get(
        "getUltraSrtNcst",
        {
            "pageNo": 1,
            "numOfRows": 1000,
            "dataType": "JSON",
            "base_date": obs_date,
            "base_time": obs_time,
            "nx": nx,
            "ny": ny,
        },
        service_key,
    )

    obs = {}
    for item in items_from(obs_payload):
        obs[item.get("category")] = item.get("obsrValue")

    base_date, base_time = latest_village_base(now)
    fcst_payload = api_get(
        "getVilageFcst",
        {
            "pageNo": 1,
            "numOfRows": 1000,
            "dataType": "JSON",
            "base_date": base_date,
            "base_time": base_time,
            "nx": nx,
            "ny": ny,
        },
        service_key,
    )

    today_key = now.strftime("%Y%m%d")
    today_rows = forecast_rows_for_date(items_from(fcst_payload), today_key)
    hhmm = now.strftime("%H%M")

    current_temp = to_number(obs.get("T1H"))
    humidity = to_number(obs.get("REH"))
    wind_speed = to_number(obs.get("WSD"))

    tmn_values = values_for_category(today_rows, "TMN")
    tmx_values = values_for_category(today_rows, "TMX")
    tmp_values = values_for_category(today_rows, "TMP")

    temp_min = tmn_values[0][1] if tmn_values else (
        min(v for _, v in tmp_values) if tmp_values else current_temp
    )
    temp_max = tmx_values[0][1] if tmx_values else (
        max(v for _, v in tmp_values) if tmp_values else current_temp
    )

    pop_values = values_for_category(today_rows, "POP")
    future_pop = [v for t, v in pop_values if t >= hhmm]
    if future_pop:
        rain_probability = max(future_pop)
    elif pop_values:
        rain_probability = max(v for _, v in pop_values)
    else:
        rain_probability = 0

    current_pty = str(obs.get("PTY", "0"))
    if current_pty in PTY_LABELS and PTY_LABELS[current_pty]:
        weather_label = PTY_LABELS[current_pty]
    else:
        forecast_pty = str(nearest_future_value(today_rows, "PTY", hhmm) or "0")
        if PTY_LABELS.get(forecast_pty):
            weather_label = PTY_LABELS[forecast_pty]
        else:
            sky = str(nearest_future_value(today_rows, "SKY", hhmm) or "1")
            weather_label = SKY_LABELS.get(sky, "날씨 확인")

    feels_like = apparent_temperature(current_temp, humidity, wind_speed)

    return {
        "region": name,
        "grid": {"nx": nx, "ny": ny},
        "weatherLabel": weather_label,
        "currentTemp": round1(current_temp),
        "feelsLike": round1(feels_like),
        "tempMin": round1(temp_min),
        "tempMax": round1(temp_max),
        "humidity": round1(humidity),
        "windSpeed": round1(wind_speed),
        "rainProbability": round(rain_probability) if rain_probability is not None else None,
        "precipitationType": current_pty,
        "rainfall1h": obs.get("RN1"),
        "source": "기상청 단기예보 조회서비스",
        "base": {
            "observationDate": obs_date,
            "observationTime": obs_time,
            "forecastDate": base_date,
            "forecastTime": base_time,
        },
    }


def main():
    service_key = os.environ.get("KMA_SERVICE_KEY", "").strip()
    if not service_key:
        print("ERROR: GitHub Secret KMA_SERVICE_KEY is missing.", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(KST)
    output = {
        "service": "SILOU DAILY",
        "dateKey": now.strftime("%Y-%m-%d"),
        "dateLabel": f"{now.month}월 {now.day}일",
        "updatedAt": now.isoformat(timespec="seconds"),
        "timezone": "Asia/Seoul",
        "source": "KMA",
        "regions": {},
        # These will be connected separately after the weather pipeline is verified.
        "airQualityStatus": "pending",
        "uvStatus": "pending",
    }

    failures = {}

    for name, grid in REGIONS.items():
        print(f"Fetching {name} ({grid['nx']}, {grid['ny']})...")
        try:
            output["regions"][name] = fetch_region(name, grid, now, service_key)
        except Exception as exc:
            failures[name] = str(exc)
            print(f"  FAILED: {exc}", file=sys.stderr)

    if failures:
        output["failures"] = failures

    if not output["regions"]:
        print("ERROR: No region could be fetched.", file=sys.stderr)
        sys.exit(1)

    Path("today.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Updated today.json with {len(output['regions'])} regions.")
    if failures:
        print(f"Partial failures: {failures}", file=sys.stderr)


if __name__ == "__main__":
    main()
