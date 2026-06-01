from __future__ import annotations

from dataclasses import dataclass

from .config import AsanaConfig
from .mapping_store import MappingStore
from .models import Project, Task, WorkspaceSnapshot


@dataclass
class ProjectImportPlan:
    source_project: Project
    # In a fuller implementation, this would include resolved tags, custom fields, etc.


@dataclass
class ImportPlan:
    projects: list[ProjectImportPlan]
    standalone_tasks: list[Task]


def build_import_plan(
    snapshot: WorkspaceSnapshot, config: AsanaConfig, mappings: MappingStore
) -> ImportPlan:
    """
    Construct an ordered import plan from a workspace snapshot.

    For now this is a thin wrapper that exposes projects as-is; the shape is
    intended to be extended with richer mapping logic (users, tags, fields).
    """
    plans: list[ProjectImportPlan] = []
    for project in snapshot.projects:
        # This is where we'd consult and update the mapping store; for now, we
        # simply wrap the project.
        plans.append(ProjectImportPlan(source_project=project))
    return ImportPlan(projects=plans, standalone_tasks=snapshot.standalone_tasks)
