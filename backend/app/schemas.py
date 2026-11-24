from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


CLI = Literal["claude", "codex", "gemini", "aider", "custom"]
Environment = Literal["server", "worker"]


class ModelConfig(BaseModel):
    cli: CLI = "custom"
    model: str = ""
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
    dependencies: list[int] = []
    stage_agents: dict[int, int] = {}


class TaskCreate(TaskBase):
    pass


class TaskUpdate(TaskBase):
    pass


class TrackerAnswer(BaseModel):
    answer: str = Field(min_length=1)


class ManualMerge(BaseModel):
    pr_url: str | None = None


class IntegrationSettings(BaseModel):
    tracker_provider: Literal["github", "jira", "linear", "youtrack", "custom"] = "github"
    tracker_base_url: str = ""
    tracker_api_token: str = ""
    tracker_project: str = ""
    git_webhook_secret: str = ""
    tracker_webhook_secret: str = ""
    worker_token: str = ""

    @model_validator(mode="after")
    def normalize_urls(self):
        self.tracker_base_url = self.tracker_base_url.rstrip("/")
        return self
