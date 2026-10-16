"""Tests use disposable storage and never load an installer's private .env."""
import os
import tempfile

os.environ["AUTH_MODE"] = "studio"
os.environ["DATABASE_URL"] = "sqlite:///" + tempfile.mkdtemp(prefix="livereview-tests-") + "/test.db"
os.environ["AUTOPILOT_ENABLED"] = "false"
os.environ["FOCUS_ENABLED"] = "false"
os.environ["AUTO_EMOTION"] = "false"
os.environ["LLM_PROVIDER"] = "mock"

from app.config import Settings  # noqa: E402

Settings.model_config["env_file"] = None
