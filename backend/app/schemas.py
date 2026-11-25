from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


CLI = Literal["claude", "codex", "gemini", "aider", "kimi", "zcode", "custom"]
Environment = Literal["server", "worker"]


class ModelConfig(BaseModel):
    cli: CLI = "custom"
    model: str = ""
    reasoning_effort: str = ""
    environment: Environment = "server"
    command: str | None = None


class FallbackTriggers(BaseModel):
    rate_limit: bool = True
    exit_code: bool = True
    timeout: bool = True
    timeout_minutes: int = Field(default=20, ge=1, le=1440)


class AgentBase(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    avatar_url: str = "🤖"
    role_prompt: str = ""
    concurrency_limit: int = Field(default=1, ge=1, le=100)
    primary_config: ModelConfig
    fallback_configs: list[ModelConfig] = []
    fallback_triggers: FallbackTriggers = FallbackTriggers()


class AgentCreate(AgentBase):
    pass


class AgentUpdate(AgentBase):
    pass


class Agent(AgentBase):
    id: int
    model_config = ConfigDict(from_attributes=True)


class TaskBase(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = ""
    external_issue_id: str | None = None
    milestone: str = ""
    sprint: str = ""
    repository_url: str = ""
    dependencies: list[int] = []
    stage_agents: dict[int, int] = {}
    # None takes the shared budget from the settings, 0 means no limit.
    token_budget: int | None = Field(default=None, ge=0)


class TaskCreate(TaskBase):
    pass


class TaskUpdate(TaskBase):
    pass


class TrackerAnswer(BaseModel):
    answer: str = Field(min_length=1)


class ManualMerge(BaseModel):
    pr_url: str | None = None


class IntegrationSettings(BaseModel):
    tracker_api_token: str = ""
    yougile_project: str = ""
    yougile_board: str = ""
    yougile_sticker: str = ""
    yougile_team_url: str = "https://ru.yougile.com/team/068505cc4436/"
    tracker_webhook_secret: str = ""
    yougile_webhook_id: str = ""
    git_webhook_secret: str = ""
    worker_token: str = ""
    repository_url: str = ""
    max_auto_retries: int = Field(default=2, ge=0, le=10)
    max_returns: int = Field(default=3, ge=0, le=50)
    task_token_budget: int = Field(default=0, ge=0)
