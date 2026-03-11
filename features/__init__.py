"""
Feature modules — self-contained vertical features.

Each feature subdirectory owns its data, logic, and tool handlers:
  - reminders/: NLP time parsing, recurring, background checker
  - weather/: Open-Meteo API, current + forecast + rain alerts
  - news/: Google News RSS, multi-category, BBC fallback
  - email/: SMTP sending with encrypted credentials
  - memory/: SQLite persistent memory, preferences, habits
  - web/: Web reading, DuckDuckGo search, deep research
"""
