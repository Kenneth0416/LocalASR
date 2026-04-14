import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import config
from config import ASRConfig, LLMConfig, ServerConfig, load_config
from runtime_checks import build_runtime_snapshot, _is_parent_writable


class RuntimeChecksTests(unittest.TestCase):
    @patch.dict(
        "os.environ",
        {
            "OPENAI_API_KEY": "secret",
            "LOCAL_ONLY_MODE": "true",
            "OPENAI_BASE_URL": "",
        },
        clear=False,
    )
    def test_openai_api_key_alone_does_not_flip_local_only_provider(self):
        llm, _, _, _, _, _ = load_config()
        self.assertEqual(llm.provider, "ollama")
        self.assertTrue(llm.local_only)

    @patch.dict(
        "os.environ",
        {
            "OPENAI_BASE_URL": "http://127.0.0.1:8000/v1",
            "LOCAL_ONLY_MODE": "true",
            "OPENAI_API_KEY": "",
        },
        clear=False,
    )
    def test_localhost_openai_base_url_selects_openai_provider(self):
        llm, _, _, _, _, _ = load_config()
        self.assertEqual(llm.provider, "openai")
        self.assertEqual(llm.base_url, "http://127.0.0.1:8000/v1")

    @patch.dict(
        "os.environ",
        {
            "MEETING_RECORDINGS_DIR": "recordings",
            "MEETING_DB_PATH": "data/meeting_realtime_voice.sqlite3",
        },
        clear=False,
    )
    def test_server_paths_are_normalized_to_project_root(self):
        _, _, _, _, server, _ = load_config()
        project_root = Path(config.__file__).resolve().parent
        self.assertEqual(server.recordings_dir, str(project_root / "recordings"))
        self.assertEqual(server.database_path, str(project_root / "data/meeting_realtime_voice.sqlite3"))

    @patch("runtime_checks.Path.mkdir", side_effect=AssertionError("mkdir should not be called"))
    def test_parent_writable_check_is_non_mutating_and_failure_tolerant(self, _mkdir):
        self.assertFalse(_is_parent_writable("/root/forbidden/meeting_realtime_voice.sqlite3"))

    def test_parent_writable_check_uses_nearest_existing_ancestor_for_fresh_nested_paths(self):
        with TemporaryDirectory() as tmpdir:
            nested_path = Path(tmpdir) / "level1" / "level2" / "meeting_realtime_voice.sqlite3"

            self.assertTrue(_is_parent_writable(str(nested_path)))

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_local_only_ollama_profile_reports_ready_capabilities(self, _which, _writable, _exists):
        llm = LLMConfig(
            provider="ollama",
            ollama_base_url="http://localhost:11434",
            ollama_model="qwen3.5:9b",
            local_only=True,
        )
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(
            host="127.0.0.1",
            recordings_dir="recordings",
            database_path="data/meeting_realtime_voice.sqlite3",
        )

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="ok",
        )

        self.assertEqual(snapshot.mode, "local-only")
        self.assertEqual(snapshot.capabilities["transcription_realtime"].state, "ready")
        self.assertEqual(snapshot.capabilities["summary_local"].state, "ready")
        self.assertEqual(snapshot.capabilities["chat_local"].state, "ready")

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value=None)
    def test_missing_ffmpeg_degrades_transcoded_upload_only(self, _which, _writable, _exists):
        llm = LLMConfig(provider="ollama", ollama_base_url="http://localhost:11434", local_only=True)
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(host="127.0.0.1")

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="ok",
        )

        self.assertEqual(snapshot.capabilities["transcription_upload_wav"].state, "ready")
        self.assertEqual(snapshot.capabilities["transcription_upload_transcoded"].state, "degraded")
        self.assertIn("ffmpeg", snapshot.capabilities["transcription_upload_transcoded"].detail.lower())

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value=None)
    def test_missing_ffmpeg_does_not_degrade_upload_when_asr_is_unavailable(self, _which, _writable, _exists):
        llm = LLMConfig(provider="ollama", ollama_base_url="http://localhost:11434", local_only=True)
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(host="127.0.0.1")

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="unavailable",
            asr_error="ASR not initialized",
            llm_status="ok",
        )

        self.assertEqual(snapshot.capabilities["transcription_realtime"].state, "unavailable")
        self.assertEqual(snapshot.capabilities["transcription_upload_wav"].state, "unavailable")
        self.assertEqual(snapshot.capabilities["transcription_upload_transcoded"].state, "unavailable")
        self.assertEqual(
            snapshot.capabilities["transcription_upload_transcoded"].detail,
            snapshot.capabilities["transcription_realtime"].detail,
        )

    @patch("runtime_checks.os.path.exists", return_value=True)
    @patch("runtime_checks._is_parent_writable", return_value=True)
    @patch("runtime_checks.shutil.which", return_value="/usr/bin/ffmpeg")
    def test_remote_openai_endpoint_is_blocked_in_local_only_mode(self, _which, _writable, _exists):
        llm = LLMConfig(
            provider="openai",
            openai_base_url="https://api.openai.com/v1",
            openai_api_key="secret",
            local_only=True,
        )
        asr = ASRConfig(model_path="/tmp/asr-model")
        server = ServerConfig(host="127.0.0.1")

        snapshot = build_runtime_snapshot(
            llm,
            asr,
            server,
            asr_state="ready",
            asr_error=None,
            llm_status="blocked: remote provider disabled in local-only mode",
        )

        self.assertEqual(snapshot.mode, "local-only")
        self.assertEqual(snapshot.capabilities["summary_local"].state, "unavailable")
        self.assertEqual(snapshot.capabilities["chat_local"].state, "unavailable")
        self.assertTrue(snapshot.warnings)

    def test_openai_provider_without_base_url_is_still_compatible(self):
        llm = LLMConfig(provider="openai", openai_api_key="secret")

        self.assertTrue(llm.is_openai_compatible())
        self.assertEqual(llm.base_url, "https://api.openai.com/v1")
