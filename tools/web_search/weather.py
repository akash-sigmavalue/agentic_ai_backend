from __future__ import annotations

"""
Current weather lookup for live weather questions.

Uses Open-Meteo's no-key geocoding and forecast APIs so weather queries do not
depend on generic web-search snippets.
"""

import json
import re
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any, Dict, Optional

from core.web_search.config import config


WEATHER_TERMS = (
    "weather",
    "temperature",
    "temp",
    "forecast",
    "humidity",
    "rain",
    "wind",
)


class WeatherLookup:
    """Fetch current weather from Open-Meteo for natural-language queries."""

    geocoding_base_url = "https://geocoding-api.open-meteo.com/v1/search"
    forecast_base_url = "https://api.open-meteo.com/v1/forecast"

    def __init__(self):
        self.client = None
        self._understanding_cache: Dict[str, Optional[Dict[str, Any]]] = {}
        if config.USE_LLM and config.OPENAI_API_KEY:
            try:
                from openai import OpenAI

                self.client = OpenAI(api_key=config.OPENAI_API_KEY, timeout=20)
            except Exception:
                self.client = None

    def is_weather_query(self, query: str) -> bool:
        llm_intent = self._understand_with_llm(query)
        if llm_intent is not None:
            return bool(llm_intent.get("is_weather"))

        query_lower = str(query or "").lower()
        if any(re.search(rf"\b{re.escape(term)}\b", query_lower) for term in ("weather", "forecast", "humidity", "rain", "wind")):
            return True
        if not any(re.search(rf"\b{re.escape(term)}\b", query_lower) for term in ("temperature", "temp")):
            return False
        return bool(
            re.search(r"\b(?:today|current|currently|now|right now|outside)\b", query_lower)
            or re.search(r"\b(?:in|at|for|near|around)\s+[A-Za-z]", query)
        )

    def extract_location(self, query: str) -> str:
        llm_intent = self._understand_with_llm(query)
        if llm_intent and llm_intent.get("is_weather") and llm_intent.get("location"):
            return str(llm_intent["location"]).strip()

        cleaned = re.sub(r"\s+", " ", str(query or "")).strip()
        patterns = [
            r"\b(?:in|at|for|near|around)\s+([A-Za-z][A-Za-z\s,.-]{1,80})",
            r"\b(?:weather|temperature|temp|forecast)\s+(?:of|in|at|for)?\s*([A-Za-z][A-Za-z\s,.-]{1,80})",
        ]

        for pattern in patterns:
            match = re.search(pattern, cleaned, flags=re.IGNORECASE)
            if not match:
                continue
            location = re.split(
                r"\b(?:today|now|currently|right now|tomorrow|yesterday|this|please|with|and)\b|[?!.]",
                match.group(1),
                maxsplit=1,
                flags=re.IGNORECASE,
            )[0]
            location = re.sub(r"\b(?:weather|temperature|temp|forecast)\b", "", location, flags=re.IGNORECASE)
            location = re.sub(r"\s+", " ", location).strip(" ,.-")
            if location:
                return location

        # Fallback: remove common question/weather words and keep title-cased terms.
        words = [
            word
            for word in re.findall(r"[A-Za-z]+", cleaned)
            if word.lower()
            not in {
                "what", "whats", "is", "today", "todays", "current", "currently",
                "now", "the", "temperature", "temp", "weather", "forecast", "in",
                "at", "for", "of", "please", "tell", "me",
            }
        ]
        return " ".join(words[-4:]).strip()

    def _understand_with_llm(self, query: str) -> Optional[Dict[str, Any]]:
        if not self.client or not str(query or "").strip():
            return None
        cache_key = re.sub(r"\s+", " ", query).strip().lower()
        if cache_key in self._understanding_cache:
            return self._understanding_cache[cache_key]

        prompt = f"""Classify whether the user is asking for live/current weather data.
Extract the location only if this is a weather query.

User query: {query}

Return only JSON:
{{
  "is_weather": true/false,
  "location": "city/area, state/country if present, or empty string",
  "reason": "short reason"
}}"""

        try:
            response = self.client.chat.completions.create(
                model=config.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=120,
                temperature=0.0,
            )
            text = response.choices[0].message.content or ""
            parsed = self._parse_json(text)
            self._understanding_cache[cache_key] = parsed
            return parsed
        except Exception:
            self._understanding_cache[cache_key] = None
            return None

    def lookup(self, query: str) -> Dict[str, Any]:
        location_query = self.extract_location(query)
        if not location_query:
            return {
                "success": False,
                "error": "Could not identify a weather location from the query.",
            }

        place = self._geocode(location_query)
        if not place:
            return {
                "success": False,
                "error": f"Could not find coordinates for '{location_query}'.",
                "location_query": location_query,
            }

        weather = self._fetch_current_weather(place["latitude"], place["longitude"])
        if not weather:
            return {
                "success": False,
                "error": f"Could not fetch current weather for {place['name']}.",
                "location_query": location_query,
                "place": place,
            }

        return {
            "success": True,
            "query": query,
            "location_query": location_query,
            "place": place,
            "weather": weather,
            "analysis": self._format_answer(place, weather),
            "results": self._source_results(place, weather),
            "timestamp": datetime.now().isoformat(),
        }

    def _geocode(self, location: str) -> Optional[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "name": location,
            "count": 1,
            "language": "en",
            "format": "json",
        })
        data = self._get_json(f"{self.geocoding_base_url}?{params}")
        results = data.get("results") or []
        if not results:
            return None

        first = results[0]
        return {
            "name": first.get("name"),
            "admin1": first.get("admin1"),
            "country": first.get("country"),
            "latitude": first.get("latitude"),
            "longitude": first.get("longitude"),
            "timezone": first.get("timezone"),
            "source_url": f"{self.geocoding_base_url}?{params}",
        }

    def _fetch_current_weather(self, latitude: float, longitude: float) -> Optional[Dict[str, Any]]:
        params = urllib.parse.urlencode({
            "latitude": latitude,
            "longitude": longitude,
            "current": ",".join([
                "temperature_2m",
                "apparent_temperature",
                "relative_humidity_2m",
                "precipitation",
                "wind_speed_10m",
            ]),
            "timezone": "auto",
        })
        url = f"{self.forecast_base_url}?{params}"
        data = self._get_json(url)
        current = data.get("current")
        units = data.get("current_units") or {}
        if not current:
            return None
        return {
            "current": current,
            "units": units,
            "timezone": data.get("timezone"),
            "source_url": url,
        }

    def _get_json(self, url: str) -> Dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "agentic-ai-web-search/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            return json.loads(response.read().decode("utf-8"))

    def _parse_json(self, text: str) -> Optional[Dict[str, Any]]:
        try:
            return json.loads(text)
        except Exception:
            start = text.find("{")
            end = text.rfind("}") + 1
            if start >= 0 and end > start:
                try:
                    return json.loads(text[start:end])
                except Exception:
                    return None
        return None

    def _format_answer(self, place: Dict[str, Any], weather: Dict[str, Any]) -> str:
        current = weather["current"]
        units = weather["units"]
        display_location = ", ".join(
            part for part in [place.get("name"), place.get("admin1"), place.get("country")] if part
        )

        temp = current.get("temperature_2m")
        temp_unit = units.get("temperature_2m", "degC")
        feels = current.get("apparent_temperature")
        feels_unit = units.get("apparent_temperature", temp_unit)
        humidity = current.get("relative_humidity_2m")
        humidity_unit = units.get("relative_humidity_2m", "%")
        wind = current.get("wind_speed_10m")
        wind_unit = units.get("wind_speed_10m", "km/h")
        precipitation = current.get("precipitation")
        precipitation_unit = units.get("precipitation", "mm")
        observed_at = current.get("time")

        return (
            f"The current temperature in {display_location} is **{temp}{temp_unit}** "
            f"(feels like **{feels}{feels_unit}**). Humidity is **{humidity}{humidity_unit}**, "
            f"wind speed is **{wind} {wind_unit}**, and precipitation is "
            f"**{precipitation}{precipitation_unit}**.\n\n"
            f"Observed at: {observed_at} ({weather.get('timezone') or place.get('timezone')}).\n\n"
            "### Reference URLs\n"
            f"1. [Open-Meteo Forecast API]({weather['source_url']})\n"
            f"2. [Open-Meteo Geocoding API]({place['source_url']})"
        )

    def _source_results(self, place: Dict[str, Any], weather: Dict[str, Any]) -> list[Dict[str, Any]]:
        current = weather["current"]
        display_location = ", ".join(
            part for part in [place.get("name"), place.get("admin1"), place.get("country")] if part
        )
        return [
            {
                "url": weather["source_url"],
                "title": f"Open-Meteo current weather for {display_location}",
                "snippet": (
                    f"Temperature {current.get('temperature_2m')}"
                    f"{weather['units'].get('temperature_2m', 'degC')}; observed at {current.get('time')}."
                ),
                "rank": 1,
                "source": "open-meteo-forecast",
                "search_query": display_location,
                "matched_entities": [display_location],
                "relevance_score": 1.0,
                "source_trust": 0.9,
                "published_date": current.get("time"),
                "time_ago": "Current weather API reading",
                "content": json.dumps(weather, ensure_ascii=False),
            },
            {
                "url": place["source_url"],
                "title": f"Open-Meteo geocoding result for {display_location}",
                "snippet": f"Coordinates: {place.get('latitude')}, {place.get('longitude')}.",
                "rank": 2,
                "source": "open-meteo-geocoding",
                "search_query": display_location,
                "matched_entities": [display_location],
                "relevance_score": 1.0,
                "source_trust": 0.9,
                "published_date": None,
                "time_ago": "Location lookup",
                "content": json.dumps(place, ensure_ascii=False),
            },
        ]
