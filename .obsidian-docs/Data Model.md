---
tags: [project-note, autogen]
---
# Data Model

Back to [[00_Home]] · Used by [[Catering Engine]] · Exposed via [[API Reference]]

```mermaid
erDiagram
    flights ||--o| passenger_manifests : has
    flights ||--o{ meal_inventories : needs
    flights ||--o{ audit_logs : records
    flights {
        string flight_id PK
        string airline
        string aircraft_type
        datetime departure_time
        string status
        string destination
        int delay_minutes
    }
    passenger_manifests {
        string flight_id PK,FK
        int economy_count
        int business_count
        int first_class_count
        int crew_count
        datetime last_updated
    }
    meal_inventories {
        int id PK
        string flight_id FK
        string meal_type
        int quantity_required
        int quantity_assembled
        string status
    }
    audit_logs {
        int id PK
        string flight_id FK
        string event_type
        text description
        datetime timestamp
    }
```

- Enums (status, aircraft, meal type/status) are stored as text with `CHECK` constraints.
- `UNIQUE(flight_id, meal_type)` on inventories; counts are `CHECK >= 0`.
- Foreign keys are enforced (`PRAGMA foreign_keys=ON`) and cascade on delete.

> [!info] Schema addition
> `flights.delay_minutes` is not in the original spec. It stores cumulative delay so the safety buffer works across multiple events.
