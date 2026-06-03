"""
Custom field manager for syncing local enumerated properties to Asana.

Manages custom field GIDs for priority property,
handling lookup, creation, and value mapping.
"""

import sys

from client import AsanaClientWrapper


class CustomFieldManager:
    """Manages custom fields for syncing local enumerated properties to Asana."""

    # Standard custom field names
    PRIORITY_FIELD_NAME = "Priority"

    # Enum value mappings (local value -> Asana enum option name)
    PRIORITY_VALUES = {
        "critical": "Critical",
        "high": "High",
        "medium": "Medium",
        "low": "Low",
    }

    def __init__(self, client: AsanaClientWrapper, workspace_gid: str):
        self.client = client
        self.workspace_gid = workspace_gid

        # Cache for custom field GIDs (field_name -> gid)
        self._field_gids: dict[str, str] = {}

        # Cache for enum option GIDs (field_gid -> {option_name -> option_gid})
        self._enum_option_gids: dict[str, dict[str, str]] = {}

    async def get_or_create_custom_field(
        self, field_name: str, enum_options: list[str]
    ) -> str | None:
        """
        Get or create a custom field with enum options.

        Returns the custom field GID, or None if creation fails (e.g., free tier).
        """
        # Check cache first
        if field_name in self._field_gids:
            return self._field_gids[field_name]

        try:
            # Try to find existing custom field in workspace
            custom_fields = self.client._with_retry(
                self.client.custom_fields.get_custom_fields_for_workspace,
                self.workspace_gid,
                {"opt_fields": "gid,name,type,enum_options"},
            )

            for cf in custom_fields:
                if cf.get("name") == field_name and cf.get("type") == "enum":
                    field_gid = cf.get("gid")
                    self._field_gids[field_name] = field_gid

                    # Cache enum option GIDs
                    enum_options_list = cf.get("enum_options", [])
                    self._enum_option_gids[field_gid] = {
                        opt.get("name"): opt.get("gid") for opt in enum_options_list
                    }

                    return field_gid

            # Field doesn't exist - try to create it (may fail on free tier)
            try:
                create_data = {
                    "name": field_name,
                    "type": "enum",
                    "enum_options": [{"name": opt} for opt in enum_options],
                }

                new_field = self.client._with_retry(
                    self.client.custom_fields.create_custom_field,
                    {"data": create_data},
                    self.workspace_gid,
                    {},
                )

                field_gid = new_field.get("gid")
                self._field_gids[field_name] = field_gid

                # Cache enum option GIDs
                enum_options_list = new_field.get("enum_options", [])
                self._enum_option_gids[field_gid] = {
                    opt.get("name"): opt.get("gid") for opt in enum_options_list
                }

                return field_gid

            except Exception as e:
                # Custom field creation failed (likely free tier)
                print(
                    f"Warning: Could not create custom field '{field_name}': {e}. "
                    f"This may be due to plan limitations. Property will not be synced to Asana.",
                    file=sys.stderr,
                )
                return None

        except Exception as e:
            print(
                f"Warning: Could not access custom fields for workspace: {e}",
                file=sys.stderr,
            )
            return None

    async def get_enum_option_gid(
        self, field_name: str, option_name: str
    ) -> str | None:
        """Get the enum option GID for a given field and option name."""
        field_gid = await self.get_or_create_custom_field(
            field_name,
            (
                list(self.PRIORITY_VALUES.values())
                if field_name == self.PRIORITY_FIELD_NAME
                else []
            ),
        )

        if not field_gid:
            return None

        # Get the mapped option name (convert local value to Asana display name)
        if field_name == self.PRIORITY_FIELD_NAME:
            mapped_name = self.PRIORITY_VALUES.get(option_name, option_name.title())
        else:
            mapped_name = option_name.title()

        return self._enum_option_gids.get(field_gid, {}).get(mapped_name)

    def get_local_value_from_enum_option(
        self, field_name: str, option_name: str
    ) -> str | None:
        """Convert Asana enum option name back to local value."""
        if field_name == self.PRIORITY_FIELD_NAME:
            for local_val, asana_name in self.PRIORITY_VALUES.items():
                if asana_name == option_name:
                    return local_val
        return option_name.lower() if option_name else None

    async def prepare_custom_fields_for_task(
        self, priority: str | None = None
    ) -> dict[str, str]:
        """
        Prepare custom field data for task creation/update.

        Returns dict mapping custom field GIDs to enum option GIDs,
        formatted for Asana API: {"custom_field_gid": "enum_option_gid"}
        """
        custom_fields_data = {}

        if priority:
            priority_field_gid = await self.get_or_create_custom_field(
                self.PRIORITY_FIELD_NAME, list(self.PRIORITY_VALUES.values())
            )
            if priority_field_gid:
                option_gid = await self.get_enum_option_gid(
                    self.PRIORITY_FIELD_NAME, priority
                )
                if option_gid:
                    custom_fields_data[priority_field_gid] = option_gid

        return custom_fields_data

    def extract_properties_from_custom_fields(
        self, custom_fields: list[dict]
    ) -> dict[str, str | None]:
        """
        Extract priority from task's custom fields.

        Returns dict with 'priority' key.
        """
        result = {"priority": None}

        for cf in custom_fields:
            cf_name = cf.get("name")
            enum_value = cf.get("enum_value")

            if not enum_value:
                continue

            option_name = (
                enum_value.get("name") if isinstance(enum_value, dict) else None
            )
            if not option_name:
                continue

            if cf_name == self.PRIORITY_FIELD_NAME:
                result["priority"] = self.get_local_value_from_enum_option(
                    self.PRIORITY_FIELD_NAME, option_name
                )

        return result
