"""配置模型与加载。校验失败抛 ConfigError，由入口打印原因后退出。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

log = logging.getLogger(__name__)

PermissionMode = Literal["default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"]
SettingSource = Literal["user", "project", "local"]


class ConfigError(Exception):
    """配置文件缺失或不合法。"""


class FeishuConfig(BaseModel):
    app_id: str
    app_secret: str

    @field_validator("app_id", "app_secret")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip() or v.strip().startswith(("cli_xxx", "xxxx")):
            raise ValueError("必须填写真实值（当前为空或仍是示例占位符）")
        return v.strip()


class PolicyConfig(BaseModel):
    model_config = ConfigDict(validate_default=True)

    require_mention_in_group: bool = True
    allowed_open_ids: list[str] = Field(default_factory=list)
    # /new <路径> 只允许这些根目录之下的目录。留空 = /new 不接受路径参数，
    # 只能沿用当前活跃会话的仓库。安全默认：否则等于让飞书上的人在本机任意目录执行工具。
    allowed_workdir_roots: list[Path] = Field(default_factory=list)

    @field_validator("allowed_workdir_roots", mode="after")
    @classmethod
    def _expand_roots(cls, value: list[Path]) -> list[Path]:
        return [Path(p).expanduser().resolve() for p in value]

    def is_allowed(self, open_id: str) -> bool:
        """白名单留空时不限制。"""
        if not self.allowed_open_ids:
            return True
        return open_id in self.allowed_open_ids

    def is_workdir_allowed(self, path: Path) -> bool:
        """留空时一律返回 False —— 调用方据此提示去配白名单。"""
        if not self.allowed_workdir_roots:
            return False
        target = Path(path).expanduser().resolve()
        return any(target == root or root in target.parents for root in self.allowed_workdir_roots)


class RunnerConfig(BaseModel):
    cli_path: str | None = None
    permission_mode: PermissionMode = "bypassPermissions"
    disallowed_tools: list[str] = Field(
        default_factory=lambda: [
            "Bash(rm -rf*)",
            "Bash(git push --force*)",
            "Bash(git reset --hard*)",
            "Bash(git clean -fd*)",
        ]
    )
    setting_sources: list[SettingSource] = Field(default_factory=lambda: ["user", "project"])
    model: str | None = None
    max_turns: int = Field(default=60, gt=0)
    turn_timeout_seconds: int = Field(default=1800, gt=0)
    idle_output_timeout_seconds: int = Field(default=300, gt=0)
    max_queue: int = Field(default=5, gt=0)
    # Claude 连接跨轮复用，空闲这么久之后断开。0 = 永不自动断开。
    # 复用能省掉每轮 8 秒量级的连接成本（大会话上更多），代价是桥一直占着这条会话，
    # 期间更不能在终端里打开同一条会话。
    connection_idle_seconds: int = Field(default=900, ge=0)
    env: dict[str, str] = Field(default_factory=dict)
    system_prompt_append: str = ""


class RenderConfig(BaseModel):
    update_interval_ms: int = Field(default=1200, ge=200)
    # 单张卡片正文的字数上限。飞书卡片本身有体积限制，别调太大
    max_card_chars: int = Field(default=6000, ge=500)
    # 最终回答一张卡片最多装多少字：不超过就一张发完，超过就按这个尺寸拆成多张。
    # 比 max_card_chars 留一点余量，给「带了 N 个附件」这类附注腾地方
    result_inline_max: int = Field(default=5500, ge=500)
    # 回答再长也最多拆这么多张卡片，多出来的截掉并在最后一张上说明
    max_result_cards: int = Field(default=6, ge=1, le=20)
    session_list_limit: int = Field(default=10, gt=0, le=50)

    @model_validator(mode="after")
    def _inline_fits_card(self) -> RenderConfig:
        """单张卡片装不下的回答长度没有意义，收敛到 max_card_chars。

        不报错而是收敛：只调小 max_card_chars 的人不该被迫连 result_inline_max
        一起改，而放任它更大会让单卡路径把回答开头截掉。
        """
        if self.result_inline_max > self.max_card_chars:
            log.warning(
                "result_inline_max（%d）大于 max_card_chars（%d），已按后者处理",
                self.result_inline_max,
                self.max_card_chars,
            )
            self.result_inline_max = self.max_card_chars
        return self


class DocsConfig(BaseModel):
    """消息里出现飞书云文档链接时，先抓成本地 md 再交给 Claude。"""

    # 默认值也要过校验器，否则配置里不写 inbox_dir 时 "~" 不会被展开
    model_config = ConfigDict(validate_default=True)

    enabled: bool = True
    # 抓下来的文档落在这里，通过 add_dirs 授权给 Claude 读；不写进目标仓库
    inbox_dir: Path = Path("~/.oncall-bot/inbox")
    max_chars_per_doc: int = Field(default=200_000, gt=0)
    max_docs_per_message: int = Field(default=5, gt=0, le=20)
    # 文档内嵌图片：下载到 <文档>.assets/ 并在 markdown 里用绝对路径引用
    fetch_images: bool = True
    max_images_per_doc: int = Field(default=40, ge=0, le=200)
    max_image_bytes: int = Field(default=10 * 1024 * 1024, gt=0)

    @field_validator("inbox_dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()


class AttachmentsConfig(BaseModel):
    """飞书消息里带的图片：下载到本地，攒着，附在下一条文字需求上交给 Claude。"""

    model_config = ConfigDict(validate_default=True)

    enabled: bool = True
    dir: Path = Path("~/.oncall-bot/attachments")
    max_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    # 一次最多攒几张，防止有人刷图
    max_pending: int = Field(default=9, gt=0, le=50)
    # 攒着的图片超过这么久就作废（图发完了半小时才说需求，大概已经不相关了）
    pending_ttl_seconds: int = Field(default=600, gt=0)

    @field_validator("dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()


class Config(BaseModel):
    # 同上：state_file / log_dir 用默认值时也要展开 "~"
    model_config = ConfigDict(validate_default=True)

    feishu: FeishuConfig
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    runner: RunnerConfig = Field(default_factory=RunnerConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)
    docs: DocsConfig = Field(default_factory=DocsConfig)
    attachments: AttachmentsConfig = Field(default_factory=AttachmentsConfig)
    state_file: Path = Path("~/.oncall-bot/state.json")
    log_dir: Path = Path("~/.oncall-bot/logs")

    @field_validator("state_file", "log_dir", mode="after")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()


def load_config(path: str | Path) -> Config:
    """读取 YAML 配置。任何问题都抛 ConfigError，消息直接面向用户。"""
    p = Path(path).expanduser()
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}\n可以从 config.example.yaml 复制一份。")
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"配置文件不是合法 YAML：{p}\n{exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"配置文件顶层必须是映射：{p}")
    try:
        cfg = Config.model_validate(raw)
    except ValidationError as exc:
        lines = [f"配置校验失败：{p}"]
        for err in exc.errors():
            loc = ".".join(str(x) for x in err["loc"]) or "<root>"
            lines.append(f"  - {loc}: {err['msg']}")
        raise ConfigError("\n".join(lines)) from exc

    cfg.state_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    if cfg.docs.enabled:
        cfg.docs.inbox_dir.mkdir(parents=True, exist_ok=True)
    if cfg.attachments.enabled:
        cfg.attachments.dir.mkdir(parents=True, exist_ok=True)
    return cfg
