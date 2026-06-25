"""
Asana API client wrapper with retry logic and timeout handling.
"""

import sys
import threading
import time
from pathlib import Path
from typing import Any

import asana

# Add current directory to path for imports
sys.path.insert(0, str(Path(__file__).parent))

from config import AsanaConfig


class TimeoutError(Exception):
    """Raised when an API call times out."""

    pass


class AsanaClientWrapper:
    """
    Thin wrapper around the official Asana Python client (v5+ OpenAPI style).

    It exposes grouped API instances (`projects`, `tasks`, `sections`, etc.)
    and provides small helpers used elsewhere in the codebase.
    """

    def __init__(
        self,
        pat: str,
        max_retries: int = 5,
        retry_backoff: float = 1.5,
        timeout: int = 60,
    ):
        self._pat = pat
        self._max_retries = max_retries
        self._retry_backoff = retry_backoff
        self._timeout = timeout

        configuration = asana.Configuration()
        configuration.access_token = pat
        self._client = asana.ApiClient(configuration)

        # Grouped API instances roughly matching the older `Client` layout.
        self.workspaces = asana.WorkspacesApi(self._client)
        self.projects = asana.ProjectsApi(self._client)
        self.tasks = asana.TasksApi(self._client)
        self.sections = asana.SectionsApi(self._client)
        self.stories = asana.StoriesApi(self._client)
        self.attachments = asana.AttachmentsApi(self._client)
        self.tags = asana.TagsApi(self._client)
        self.custom_fields = asana.CustomFieldsApi(self._client)
        self.users = asana.UsersApi(self._client)
        self.user_task_lists = asana.UserTaskListsApi(self._client)

    @classmethod
    def from_config_source(cls, config: AsanaConfig) -> "AsanaClientWrapper":
        return cls(config.source_pat)

    @classmethod
    def from_config_target(cls, config: AsanaConfig) -> "AsanaClientWrapper":
        return cls(config.target_pat)

    @property
    def raw(self) -> "AsanaClientWrapper":
        """
        Return self so callers can access grouped API instances
        like `raw.projects`, `raw.tasks`, etc.
        """
        return self

    def _with_timeout(self, func, *args, **kwargs) -> Any:
        """Execute a function with a timeout."""
        result = [None]
        exception = [None]

        def target():
            try:
                result[0] = func(*args, **kwargs)
            except Exception as e:
                exception[0] = e

        thread = threading.Thread(target=target)
        thread.daemon = True
        thread.start()
        thread.join(timeout=self._timeout)

        if thread.is_alive():
            # Thread is still running, timeout occurred
            raise TimeoutError(f"API call timed out after {self._timeout} seconds")

        if exception[0]:
            raise exception[0]

        return result[0]

    def _with_retry(self, func, *args, **kwargs) -> Any:
        # Basic retry wrapper; Asana's OpenAPI client raises generic ApiException
        # on most errors, including rate limits.
        delay = self._retry_backoff
        for attempt in range(self._max_retries):
            try:
                return self._with_timeout(func, *args, **kwargs)
            except TimeoutError:
                if attempt >= self._max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= self._retry_backoff
            except Exception:  # noqa: BLE001
                # For now, treat all exceptions as retryable up to max_retries.
                if attempt >= self._max_retries - 1:
                    raise
                time.sleep(delay)
                delay *= self._retry_backoff
        raise RuntimeError("Unexpected fallthrough in _with_retry")
