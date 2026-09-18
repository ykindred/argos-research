"""Project-owned charter and YAML configuration; no domain knowledge in the runtime."""

from pathlib import Path

import yaml

from argos.protocols import ProjectConfig


class ProjectLoader:
    def load(self, directory: Path) -> ProjectConfig:
        directory = directory.resolve()
        raw = yaml.safe_load((directory / "project.yaml").read_text())
        if not isinstance(raw, dict):
            raise ValueError("project.yaml must contain a mapping matching ProjectConfig")
        raw["research_charter"] = (directory / "research.md").read_text()
        config = ProjectConfig.model_validate(raw)
        repo = (directory / config.source_repository).resolve()
        if not repo.is_dir():
            raise ValueError("Configured source_repository must exist")
        config.source_repository = str(repo)
        return config
