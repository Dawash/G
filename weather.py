"""
Weather module — uses Open-Meteo API (free, no API key, no signup).

Location comes from config.json (user sets city on first run).
No IP geolocation needed — zero rate limits, zero crashes.

Features:
  - City saved in config (asked once during setup)
  - Geocoding via Open-Meteo (free, no key)
  - Current conditions (temp, description, humidity, wind)
  - Hourly forecast
  - Rain alerts
  - Temperature in both C and F
  - Location cache on disk (geocode only once per city)
"""

import json
import logging
import os
import requests
from datetime import datetime

logger = logging.getLogger(__name__)

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
LOCATION_CACHE = os.path.join(PROJECT_DIR, "location_cache.json")

# WMO weather codes -> natural descriptions
WMO_CODES = {
    0: "clear sky", 1: "mostly clear", 2: "partly cloudy", 3: "overcast",
    45: "foggy", 48: "depositing rime fog",
    51: "light drizzle", 53: "moderate drizzle", 55: "dense drizzle",
    56: "freezing drizzle", 57: "dense freezing drizzle",
    61: "slight rain", 63: "moderate rain", 65: "heavy rain",
    66: "freezing rain", 67: "heavy freezing rain",
    71: "slight snow", 73: "moderate snow", 75: "heavy snow", 77: "snow grains",
    80: "slight rain showers", 81: "moderate rain showers", 82: "violent rain showers",
    85: "slight snow showers", 86: "heavy snow showers",
    95: "thunderstorm", 96: "thunderstorm with slight hail", 99: "thunderstorm with heavy hail",
}

SEVERE_CODES = {65, 66, 67, 75, 82, 86, 95, 96, 99}


def _load_cache():
    """Load location cache from disk."""
    if os.path.exists(LOCATION_CACHE):
        try:
            with open(LOCATION_CACHE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_cache(cache):
    """Save location cache to disk."""
    try:
        with open(LOCATION_CACHE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass


def _get_city_from_config():
    """Read city from config.json (optional fallback)."""
    config_file = os.path.join(PROJECT_DIR, "config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
            return config.get("city", "")
        except Exception:
            pass
    return ""


def _auto_detect_location():
    """
    Auto-detect location by IP using multiple services.
    Tries several providers so if one is rate-limited or blocked by VPN,
    the next one picks up. Only needs to succeed ONCE — result is cached.
    """
    services = [
        {
            "url": "http://ip-api.com/json/",
            "lat": "lat", "lon": "lon",
            "city": "city", "country": "country",
        },
        {
            "url": "https://ipapi.co/json/",
            "lat": "latitude", "lon": "longitude",
            "city": "city", "country": "country_name",
        },
        {
            "url": "https://ipwho.is/",
            "lat": "latitude", "lon": "longitude",
            "city": "city", "country": "country",
        },
        {
            "url": "https://freeipapi.com/api/json",
            "lat": "latitude", "lon": "longitude",
            "city": "cityName", "country": "countryName",
        },
    ]

    for svc in services:
        try:
            resp = requests.get(svc["url"], timeout=2)  # 2s per service (was 4s, total was 16s)
            if resp.status_code == 429:
                logger.debug(f"IP geo 429 from {svc['url']}, trying next...")
                continue
            resp.raise_for_status()
            data = resp.json()

            lat = data.get(svc["lat"])
            lon = data.get(svc["lon"])
            city = data.get(svc["city"], "your area")
            country = data.get(svc["country"], "")

            if lat and lon:
                return {
                    "lat": float(lat),
                    "lon": float(lon),
                    "city": city,
                    "country": country,
                }
        except Exception as e:
            logger.debug(f"IP geo failed from {svc['url']}: {e}")
            continue

    return None


def _geocode_city(city):
    """Convert a city name to lat/lon using Open-Meteo geocoding (free)."""
    try:
        resp = requests.get(
            "https://geocoding-api.open-meteo.com/v1/search",
            params={"name": city, "count": 1, "language": "en"},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", [])
        if results:
            return {
                "lat": results[0]["latitude"],
                "lon": results[0]["longitude"],
                "city": results[0].get("name", city),
                "country": results[0].get("country", ""),
            }
    except Exception as e:
        logger.warning(f"Geocoding failed for '{city}': {e}")
    return None


def _get_location(city=None):
    """
    Get lat/lon for weather lookups. Priority:
    1. If city param given → geocode it (and cache)
    2. Disk cache "_auto" → already detected, use it
    3. Auto-detect by IP (tries 4 services) → cache result
    4. Config city fallback → geocode it
    5. Give up gracefully
    """
    cache = _load_cache()

    # Explicit city requested (e.g. "weather in Paris")
    if city:
        city_key = city.lower().strip()
        if city_key in cache:
            return cache[city_key]
        loc = _geocode_city(city)
        if loc:
            cache[city_key] = loc
            _save_cache(cache)
        return loc

    # Check if we already have auto-detected location cached
    if "_auto" in cache:
        return cache["_auto"]

    # Try auto-detecting by IP (multiple services, handles VPN/rate limits)
    loc = _auto_detect_location()
    if loc:
        cache["_auto"] = loc
        _save_cache(cache)
        logger.info(f"Auto-detected location: {loc['city']}, {loc['country']}")
        return loc

    # Fallback: city from config.json
    config_city = _get_city_from_config()
    if config_city:
        city_key = config_city.lower().strip()
        if city_key in cache:
            return cache[city_key]
        loc = _geocode_city(config_city)
        if loc:
            cache[city_key] = loc
            cache["_auto"] = loc  # Also save as auto so we don't retry
            _save_cache(cache)
            return loc

    return None


def set_default_city(city):
    """Set the default city for weather lookups.

    Overrides IP-based auto-detection. Persists to config.json and
    updates the location cache. Use when user says 'set my city to X'
    or when IP geolocation is wrong (VPN, travel).

    Returns:
        str: confirmation or error message.
    """
    if not city or not city.strip():
        return "Please specify a city name."

    loc = _geocode_city(city.strip())
    if not loc:
        return f"Could not find city '{city}'. Check spelling and try again."

    # Update location cache
    cache = _load_cache()
    cache["_auto"] = loc
    cache[city.strip().lower()] = loc
    _save_cache(cache)

    # Also persist to config.json
    config_file = os.path.join(PROJECT_DIR, "config.json")
    try:
        config = {}
        if os.path.exists(config_file):
            with open(config_file, "r", encoding="utf-8") as f:
                config = json.load(f)
        config["city"] = city.strip()
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2)
    except Exception as e:
        logger.warning(f"Could not save city to config: {e}")

    logger.info(f"Default weather city set to: {loc['city']}, {loc['country']}")
    return f"Weather city set to {loc['city']}, {loc['country']}."


def _describe_weather_code(code):
    """Convert WMO weather code to natural description."""
    return WMO_CODES.get(code, "unknown conditions")


def _c_to_f(celsius):
    """Convert Celsius to Fahrenheit."""
    return round(celsius * 9 / 5 + 32)


def get_current_weather(city=None):
    """
    Get current weather. Uses city from config if not specified.
    Returns a natural language string for voice output.
    """
    location = _get_location(city)
    if not location:
        return ("I couldn't detect your location — maybe VPN is blocking it. "
                "Try 'weather in Berlin' or set your city in config.json.")

    try:
        params = {
            "latitude": location["lat"],
            "longitude": location["lon"],
            "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "timezone": "auto",
        }

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json()

        current = data.get("current", {})
        temp_c = current.get("temperature_2m", 0)
        temp_f = _c_to_f(temp_c)
        feels_c = current.get("apparent_temperature", temp_c)
        feels_f = _c_to_f(feels_c)
        humidity = current.get("relative_humidity_2m", 0)
        wind = current.get("wind_speed_10m", 0)
        code = current.get("weather_code", 0)
        description = _describe_weather_code(code)
        city_name = location["city"]

        parts = [f"In {city_name}, it's {temp_f}°F ({temp_c}°C) with {description}."]

        if abs(feels_c - temp_c) >= 3:
            parts.append(f"Feels like {feels_f}°F.")

        if humidity > 80:
            parts.append(f"Humidity is high at {humidity}%.")
        elif humidity < 30:
            parts.append(f"It's quite dry at {humidity}% humidity.")

        if wind > 30:
            parts.append(f"Strong winds at {round(wind)} km/h.")

        if code in SEVERE_CODES:
            parts.insert(0, "Weather alert!")

        return " ".join(parts)

    except Exception as e:
        logger.error(f"Weather error: {e}")
        return "Weather information is unavailable right now."


def get_forecast(city=None):
    """
    Get hourly forecast for the next 6 hours.
    Returns a natural language string for voice output.
    """
    location = _get_location(city)
    if not location:
        return "Can't get the forecast — set your city in config.json."

    try:
        params = {
            "latitude": location["lat"],
            "longitude": location["lon"],
            "hourly": "temperature_2m,weather_code,precipitation_probability",
            "forecast_hours": 12,
            "temperature_unit": "celsius",
            "timezone": "auto",
        }

        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        temps = hourly.get("temperature_2m", [])
        codes = hourly.get("weather_code", [])
        rain_probs = hourly.get("precipitation_probability", [])

        if not times:
            return "No forecast data available."

        parts = [f"Forecast for {location['city']}:"]

        rain_expected = False
        for i in range(min(6, len(times))):
            # Parse actual timestamp from API (e.g., "2026-03-14T15:00")
            try:
                _t = datetime.fromisoformat(times[i])
                time_label = _t.strftime("%I:%M %p").lstrip("0")
            except (ValueError, IndexError):
                time_label = f"Hour {i+1}"
            temp_f = _c_to_f(temps[i]) if i < len(temps) else "?"
            desc = _describe_weather_code(codes[i]) if i < len(codes) else "unknown"
            rain_prob = rain_probs[i] if i < len(rain_probs) else 0

            if rain_prob and rain_prob > 50:
                rain_expected = True

            if i == 0 or i == 2 or i == 5:
                parts.append(f"At {time_label}, {temp_f}F, {desc}.")

        if rain_expected:
            parts.append("Rain is expected — you might want an umbrella.")

        return " ".join(parts)

    except Exception as e:
        logger.error(f"Forecast error: {e}")
        return "Forecast unavailable right now."


def check_rain_alert(city=None):
    """Check if rain is expected in the next few hours. Returns alert string or None."""
    location = _get_location(city)
    if not location:
        return None

    try:
        params = {
            "latitude": location["lat"],
            "longitude": location["lon"],
            "hourly": "precipitation_probability,weather_code",
            "forecast_hours": 6,
            "timezone": "auto",
        }
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params=params,
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()

        hourly = data.get("hourly", {})
        rain_probs = hourly.get("precipitation_probability", [])
        codes = hourly.get("weather_code", [])

        for i, (prob, code) in enumerate(zip(rain_probs, codes)):
            if (prob and prob > 60) or code in SEVERE_CODES:
                hour = (datetime.now().hour + i + 1) % 24
                desc = _describe_weather_code(code)
                return f"Heads up — {desc} expected around {hour:02d}:00. Take an umbrella!"

        return None

    except Exception:
        return None
