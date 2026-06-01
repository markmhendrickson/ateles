from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserRef(BaseModel):
    gid: str
    name: str | None = None
    email: str | None = None


class Tag(BaseModel):
    gid: str
    name: str


class CustomFieldOption(BaseModel):
    gid: str
    name: str
    color: str | None = None


class CustomField(BaseModel):
    gid: str
    name: str
    type: str
    options: list[CustomFieldOption] = Field(default_factory=list)


class AttachmentMeta(BaseModel):
    gid: str
    name: str
    resource_type: str | None = None
    download_url: str | None = None
    local_file_path: str | None = None


class Comment(BaseModel):
    gid: str
    text: str
    html_text: str | None = None
    created_at: datetime | None = None
    created_by: UserRef | None = None


class Dependency(BaseModel):
    predecessor_gid: str
    successor_gid: str


class Subtask(BaseModel):
    gid: str
    name: str
    notes: str | None = None
    html_notes: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    due_on: datetime | None = None
    due_at: datetime | None = None
    start_on: datetime | None = None
    start_at: datetime | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    assignee: UserRef | None = None
    parent_gid: str | None = None
    permalink_url: str | None = None
    tags: list[Tag] = Field(default_factory=list)
    custom_fields: dict[str, CustomField] = Field(default_factory=dict)
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)


class Task(BaseModel):
    gid: str
    name: str
    notes: str | None = None
    html_notes: str | None = None
    completed: bool = False
    completed_at: datetime | None = None
    due_on: datetime | None = None
    due_at: datetime | None = None
    start_on: datetime | None = None
    start_at: datetime | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    assignee: UserRef | None = None
    parent_gid: str | None = None
    permalink_url: str | None = None
    followers: list[UserRef] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    custom_fields: dict[str, CustomField] = Field(default_factory=dict)
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    comments: list[Comment] = Field(default_factory=list)
    subtasks: list[Subtask] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)


class Section(BaseModel):
    gid: str
    name: str
    created_at: datetime | None = None
    modified_at: datetime | None = None
    project_gid: str | None = None
    resource_type: str | None = None
    tasks: list[Task] = Field(default_factory=list)


class Project(BaseModel):
    gid: str
    name: str
    notes: str | None = None
    html_notes: str | None = None
    archived: bool = False
    public: bool | None = None
    created_at: datetime | None = None
    modified_at: datetime | None = None
    color: str | None = None
    icon: str | None = None
    due_date: datetime | None = None
    due_on: datetime | None = None
    start_on: datetime | None = None
    workspace_gid: str | None = None
    team_gid: str | None = None
    permalink_url: str | None = None
    resource_type: str | None = None
    default_view: str | None = None
    owner: UserRef | None = None
    followers: list[UserRef] = Field(default_factory=list)
    members: list[UserRef] = Field(default_factory=list)
    custom_fields: dict[str, CustomField] = Field(default_factory=dict)
    sections: list[Section] = Field(default_factory=list)


class WorkspaceSnapshot(BaseModel):
    schema_version: str = "1.0"
    source_workspace_gid: str
    projects: list[Project] = Field(default_factory=list)
    standalone_tasks: list[Task] = Field(default_factory=list)
