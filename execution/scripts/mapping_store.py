import json
from pathlib import Path


class MappingStore:
    """
    Persistent mapping of source_gid -> target_gid for multiple entity types.

    Stored as a JSON file on disk:
    {
      "users": { "src_gid": "tgt_gid", ... },
      "projects": { ... },
      "sections": { ... },
      "tasks": { ... },
      "tags": { ... },
      "custom_fields": { ... },
      "custom_field_options": { ... }
    }
    """

    def __init__(self, path: Path):
        self.path = path
        self._data: dict[str, dict[str, str]] = {
            "users": {},
            "projects": {},
            "sections": {},
            "tasks": {},
            "tags": {},
            "custom_fields": {},
            "custom_field_options": {},
        }
        if self.path.exists():
            self._load()

    def _load(self) -> None:
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for key, value in raw.items():
            if isinstance(value, dict):
                self._data.setdefault(key, {}).update(value)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, indent=2, sort_keys=True), encoding="utf-8"
        )

    def get(self, category: str, source_gid: str) -> str | None:
        return self._data.get(category, {}).get(source_gid)

    def set(self, category: str, source_gid: str, target_gid: str) -> None:
        self._data.setdefault(category, {})[source_gid] = target_gid

    # Convenience helpers per entity type
    def get_user(self, source_gid: str) -> str | None:
        return self.get("users", source_gid)

    def set_user(self, source_gid: str, target_gid: str) -> None:
        self.set("users", source_gid, target_gid)
