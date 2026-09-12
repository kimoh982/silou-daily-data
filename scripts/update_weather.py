#!/usr/bin/env python3

import json
import math
import os
import ssl
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode, unquote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")

BASE_URL = (
    "https://apis.data.go.kr/"
    "1360000/VilageFcstInfoService_2.0"
)


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


def load_previous_weather():
    """
    Load the existing today.json if it is valid.

    Used as a fallback when KMA temporarily times out.
    """
    path = Path("today.json")

    if not path.exists():
        return None

    try:
        previous = json.loads(
            path.read_text(encoding="utf-8")
        )

        if not isinstance(previous, dict):
            return None

        regions = previous.get("regions")

        if not isinstance(regions, dict) or not regions:
            return None

        return previous

    except Exception as exc:
        print(
            f"WARNING: Could not read existing today.json: {exc}",
            file=sys.stderr,
        )
        return None


def api_get(endpoint: str, params: dict, service_key: str) -> dict:
    """
    Request KMA API with retry protection.

    data.go.kr occasionally responds slowly or times out,
    especially from GitHub Actions runners.
    """

    # Normalize either Encoding or Decoding key from data.go.kr.
    decoded_key = unquote(service_key.strip())

    # urlencode safely re-encodes +, /, = in the decoded service key.
    query = urlencode(
        {
            "serviceKey": decoded_key,
            **params,
        }
    )

    url = f"{BASE_URL}/{endpoint}?{query}"

    request = Request(
        url,
        headers={
            "User-Agent": "SILOU-DAILY/1.0",
            "Accept": "application/json",
            "Connection": "close",
        },
    )

    # data.go.kr HTTPS can fail on newer Linux/OpenSSL defaults.
    # Restrict this request only to TLS 1.2 and
    # a compatible security level.
    ctx = ssl.create_default_context()
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2

    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    except ssl.SSLError:
        pass

    last_error = None
    raw = None

    # 3 attempts:
    # attempt 1 -> wait 2 sec
    # attempt 2 -> wait 4 sec
    # attempt 3 -> fail
    for attempt in range(3):
        try:
            print(
                f"  KMA request {endpoint} "
                f"(attempt {attempt + 1}/3)..."
            )

            with urlopen(
                request,
                timeout=15,
                context=ctx,
            ) as response:
                raw = response.read().decode("utf-8")

            break

        except Exception as exc:
            last_error = exc

            print(
                f"  KMA request failed "
                f"({attempt + 1}/3): {exc}",
                file=sys.stderr,
            )

            if attempt < 2:
                wait_seconds = 2 ** (attempt + 1)

                print(
                    f"  Retrying in {wait_seconds}s...",
                    file=sys.stderr,
                )

                time.sleep(wait_seconds)

    if raw is None:
        raise RuntimeError(
            "KMA request failed after 3 attempts: "
            f"{last_error}"
        )

    try:
        payload = json.loads(raw)

    except json.JSONDecodeError:
        raise RuntimeError(
            "KMA returned non-JSON response: "
            f"{raw[:300]}"
        )

    header = (
        payload
        .get("response", {})
        .get("header", {})
    )

    if str(header.get("resultCode")) != "00":
        raise RuntimeError(
            "KMA API error "
            f"{header.get('resultCode')}: "
            f"{header.get('resultMsg')}"
        )

    return payload


def items_from(payload: dict) -> list:
    body = (
        payload
        .get("response", {})
        .get("body", {})
    )

    items = body.get("items", {})

    if not items:
        return []

    result = items.get("item", [])

    return result if isinstance(result, list) else []


def latest_ultra_base(now: datetime) -> tuple[str, str]:
    """
    Ultra-short observations can arrive
    a little after the hour.
    """

    safe = now - timedelta(minutes=40)

    return (
        safe.strftime("%Y%m%d"),
        safe.strftime("%H00"),
    )


def latest_village_base(now: datetime) -> tuple[str, str]:
    """
    Village forecast base cycles:
    02, 05, 08, 11, 14, 17, 20, 23 KST.

    Add a safety delay so we do not request
    a cycle before publication.
    """

    safe = now - timedelta(minutes=20)

    cycles = [
        2,
        5,
        8,
        11,
        14,
        17,
        20,
        23,
    ]

    available = [
        hour
        for hour in cycles
        if hour <= safe.hour
    ]

    if available:
        hour = max(available)
        base_date = safe.date()

    else:
        hour = 23
        base_date = (
            safe - timedelta(days=1)
        ).date()

    return (
        base_date.strftime("%Y%m%d"),
        f"{hour:02d}00",
    )


def to_number(value, default=None):
    try:
        return float(value)

    except (TypeError, ValueError):
        return default


def round1(value):
    if isinstance(value, (int, float)):
        return round(value, 1)

    return None


def apparent_temperature(
    temp_c,
    humidity,
    wind_ms,
):
    """
    Approximate apparent temperature from
    temperature / humidity / wind.

    This is a display approximation,
    not an official KMA perceived-temperature index.
    """

    if (
        temp_c is None
        or humidity is None
        or wind_ms is None
    ):
        return temp_c

    try:
        vapor = (
            (humidity / 100.0)
            * 6.105
            * math.exp(
                17.27
                * temp_c
                / (237.7 + temp_c)
            )
        )

        apparent = (
            temp_c
            + 0.33 * vapor
            - 0.70 * wind_ms
            - 4.0
        )

        return round1(apparent)

    except Exception:
        return round1(temp_c)


def forecast_rows_for_date(
    items: list,
    date_key: str,
) -> list:
    return [
        item
        for item in items
        if str(item.get("fcstDate")) == date_key
    ]


def nearest_future_value(
    rows: list,
    category: str,
    now_hhmm: str,
):
    candidates = [
        item
        for item in rows
        if (
            item.get("category") == category
            and str(
                item.get("fcstTime", "")
            ) >= now_hhmm
        )
    ]

    if not candidates:
        candidates = [
            item
            for item in rows
            if item.get("category") == category
        ]

    if not candidates:
        return None

    candidates.sort(
        key=lambda item: str(
            item.get("fcstTime", "")
        )
    )

    return candidates[0].get("fcstValue")


def values_for_category(
    rows: list,
    category: str,
):
    result = []

    for item in rows:
        if item.get("category") != category:
            continue

        value = to_number(
            item.get("fcstValue")
        )

        if value is not None:
            result.append(
                (
                    str(
                        item.get(
                            "fcstTime",
                            "",
                        )
                    ),
                    value,
                )
            )

    return result


def fetch_region(
    name: str,
    grid: dict,
    now: datetime,
    service_key: str,
) -> dict:
    nx = grid["nx"]
    ny = grid["ny"]

    # Current observation
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
        obs[
            item.get("category")
        ] = item.get("obsrValue")

    # Forecast
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

    today_rows = forecast_rows_for_date(
        items_from(fcst_payload),
        today_key,
    )

    hhmm = now.strftime("%H%M")

    current_temp = to_number(
        obs.get("T1H")
    )

    humidity = to_number(
        obs.get("REH")
    )

    wind_speed = to_number(
        obs.get("WSD")
    )

    tmn_values = values_for_category(
        today_rows,
        "TMN",
    )

    tmx_values = values_for_category(
        today_rows,
        "TMX",
    )

    tmp_values = values_for_category(
        today_rows,
        "TMP",
    )

    if tmn_values:
        temp_min = tmn_values[0][1]

    elif tmp_values:
        temp_min = min(
            value
            for _, value in tmp_values
        )

    else:
        temp_min = current_temp

    if tmx_values:
        temp_max = tmx_values[0][1]

    elif tmp_values:
        temp_max = max(
            value
            for _, value in tmp_values
        )

    else:
        temp_max = current_temp

    pop_values = values_for_category(
        today_rows,
        "POP",
    )

    future_pop = [
        value
        for time_key, value in pop_values
        if time_key >= hhmm
    ]

    if future_pop:
        rain_probability = max(future_pop)

    elif pop_values:
        rain_probability = max(
            value
            for _, value in pop_values
        )

    else:
        rain_probability = 0

    current_pty = str(
        obs.get(
            "PTY",
            "0",
        )
    )

    if (
        current_pty in PTY_LABELS
        and PTY_LABELS[current_pty]
    ):
        weather_label = PTY_LABELS[
            current_pty
        ]

    else:
        forecast_pty = str(
            nearest_future_value(
                today_rows,
                "PTY",
                hhmm,
            )
            or "0"
        )

        if PTY_LABELS.get(forecast_pty):
            weather_label = PTY_LABELS[
                forecast_pty
            ]

        else:
            sky = str(
                nearest_future_value(
                    today_rows,
                    "SKY",
                    hhmm,
                )
                or "1"
            )

            weather_label = SKY_LABELS.get(
                sky,
                "날씨 확인",
            )

    feels_like = apparent_temperature(
        current_temp,
        humidity,
        wind_speed,
    )

    return {
        "region": name,
        "grid": {
            "nx": nx,
            "ny": ny,
        },
        "weatherLabel": weather_label,
        "currentTemp": round1(current_temp),
        "feelsLike": round1(feels_like),
        "tempMin": round1(temp_min),
        "tempMax": round1(temp_max),
        "humidity": round1(humidity),
        "windSpeed": round1(wind_speed),
        "rainProbability": (
            round(rain_probability)
            if rain_probability is not None
            else None
        ),
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
    service_key = os.environ.get(
        "KMA_SERVICE_KEY",
        "",
    ).strip()

    if not service_key:
        print(
            "ERROR: GitHub Secret "
            "KMA_SERVICE_KEY is missing.",
            file=sys.stderr,
        )
        sys.exit(1)

    now = datetime.now(KST)

    previous = load_previous_weather()

    previous_regions = {}

    if previous:
        previous_regions = previous.get(
            "regions",
            {},
        )

    output = {
        "service": "SILOU DAILY",
        "dateKey": now.strftime("%Y-%m-%d"),
        "dateLabel": (
            f"{now.month}월 {now.day}일"
        ),
        "updatedAt": now.isoformat(
            timespec="seconds"
        ),
        "timezone": "Asia/Seoul",
        "source": "KMA",
        "regions": {},
        "airQualityStatus": "pending",
        "uvStatus": "pending",
    }

    failures = {}
    fallback_regions = []
    fresh_regions = []

    for name, grid in REGIONS.items():
        print(
            f"Fetching {name} "
            f"({grid['nx']}, {grid['ny']})..."
        )

        try:
            region_data = fetch_region(
                name,
                grid,
                now,
                service_key,
            )

            output["regions"][name] = region_data
            fresh_regions.append(name)

            print(
                f"  SUCCESS: {name}"
            )

        except Exception as exc:
            failures[name] = str(exc)

            print(
                f"  FAILED: {name}: {exc}",
                file=sys.stderr,
            )

            # If this region existed in the previous JSON,
            # keep the previous region instead of removing it.
            if name in previous_regions:
                output["regions"][name] = (
                    previous_regions[name]
                )

                fallback_regions.append(name)

                print(
                    f"  FALLBACK: keeping previous "
                    f"{name} weather data.",
                    file=sys.stderr,
                )

    if failures:
        output["failures"] = failures

    if fallback_regions:
        output["fallbackRegions"] = (
            fallback_regions
        )

    output["freshRegions"] = fresh_regions

    # -------------------------------------------------
    # Important:
    # If every KMA request failed, DO NOT overwrite
    # today.json with stale data carrying a new timestamp.
    #
    # Keep the existing file exactly as it was.
    # This also lets the GitHub Action finish successfully.
    # -------------------------------------------------

    if not fresh_regions:
        if previous_regions:
            print(
                "WARNING: All KMA requests failed.",
                file=sys.stderr,
            )

            print(
                "Keeping the existing today.json "
                "unchanged.",
                file=sys.stderr,
            )

            print(
                "Workflow will finish normally so "
                "SILOU DAILY can continue serving "
                "the previous weather data.",
                file=sys.stderr,
            )

            return

        print(
            "ERROR: No region could be fetched "
            "and no previous weather data exists.",
            file=sys.stderr,
        )

        sys.exit(1)

    # At least one region is fresh.
    # Write the new JSON. Failed regions use
    # previous data when available.
    Path("today.json").write_text(
        json.dumps(
            output,
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    print()
    print(
        "----------------------------------------"
    )

    print(
        f"Fresh regions: "
        f"{len(fresh_regions)}"
    )

    print(
        f"Fallback regions: "
        f"{len(fallback_regions)}"
    )

    print(
        f"Total regions in today.json: "
        f"{len(output['regions'])}"
    )

    print(
        "----------------------------------------"
    )

    print(
        "Updated today.json successfully."
    )

    if failures:
        print(
            f"Partial failures: {failures}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
