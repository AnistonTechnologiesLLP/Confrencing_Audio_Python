"""Suite-wide isolation: the project-file manager's per-user state directory
(recent files, autosaves, crash markers) is pointed at a throwaway temp dir so
tests never read or write the developer's real state."""
import os
import tempfile

os.environ["CONF_PIPELINE_STATE_DIR"] = tempfile.mkdtemp(prefix="conf-pipeline-test-state-")
