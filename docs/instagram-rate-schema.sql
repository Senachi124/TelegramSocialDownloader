-- Reference DDL for instagram-api-rate.sqlite3.
-- RequestWindow also initializes singleton rows using the current clock.
-- This file contains structure only, not live state or initialization values.
CREATE TABLE IF NOT EXISTS requests (sent_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), blocked_until REAL NOT NULL);
CREATE TABLE IF NOT EXISTS history_coverage(id INTEGER PRIMARY KEY CHECK(id=1), since REAL NOT NULL);
