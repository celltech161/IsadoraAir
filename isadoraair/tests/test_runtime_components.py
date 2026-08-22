"""Runtime Foundation A component-contract tests."""

import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.runtime_components import (
    MANIFEST_PATH,
    RuntimeComponentContractError,
    get_runtime_component,
    load_runtime_components,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]


class RuntimeComponentManifestTests(SimpleTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.manifest = load_runtime_components()

    def test_manifest_is_checked_in_beside_loader(self):
        self.assertEqual(MANIFEST_PATH, PROJECT_ROOT / "isadoraair" / "runtime_components.json")
        self.assertTrue(MANIFEST_PATH.is_file())

    def test_canonical_paths_are_generic_and_absolute(self):
        paths = self.manifest["canonical_paths"]
        self.assertEqual(paths["application_root"], "/opt/isadoraair")
        self.assertEqual(paths["runtime_root"], "/opt/isadoraair-runtime")
        self.assertEqual(paths["tts_asset_root"], "/var/lib/isadoraair/tts")
        self.assertEqual(paths["tts_cli"], "/usr/local/bin/isadoraair-tts")
        self.assertEqual(paths["tts_scratch"], "/run/isadoraair/tts")
        self.assertNotIn("/home/", json.dumps(paths))

    def test_kokoro_versions_and_hashes_are_exact(self):
        kokoro = get_runtime_component("kokoro")
        self.assertEqual(
            kokoro["runtime"]["packages"],
            {
                "kokoro-onnx": "0.4.7",
                "onnxruntime": "1.28.0",
                "numpy": "2.5.1",
                "phonemizer-fork": "3.3.1",
                "espeakng-loader": "0.2.4",
            },
        )
        self.assertEqual(
            kokoro["assets"]["model"]["sha256"],
            "7d5df8ecf7d4b1878015a32686053fd0eebe2bc377234608764cc0ef3636a6c5",
        )
        self.assertEqual(
            kokoro["assets"]["voices"]["sha256"],
            "bca610b8308e8d99f32e6fe4197e7ec01679264efed0cac9140fe9c29f1fbf7d",
        )
        self.assertEqual(kokoro["output"]["sample_rate_hz"], 24000)
        self.assertEqual(kokoro["output"]["channels"], 1)
        self.assertEqual(kokoro["output"]["sample_format"], "signed-16-bit-pcm")
        self.assertEqual(kokoro["runtime"]["provider_module"], "isadoraair.tts.provider_cli")

    def test_kokoro_has_no_fixed_cpu_policy(self):
        execution = get_runtime_component("kokoro")["execution"]
        self.assertEqual(
            execution,
            {"cpu_affinity": None, "onnx_intra_op_threads": None, "process_nice": None},
        )

    def test_piper_is_optional_with_no_product_default_models(self):
        piper = get_runtime_component("piper")
        self.assertEqual(piper["availability"]["policy"], "optional")
        self.assertEqual(piper["availability"]["unselected_absent"], "optional_pass")
        self.assertEqual(piper["availability"]["selected_missing_or_broken"], "fail")
        self.assertEqual(piper["runtime"]["packages"], {"piper-tts": "1.4.2"})
        self.assertEqual(piper["runtime"]["executable"], "/opt/isadoraair-runtime/piper/venv/bin/piper")
        self.assertEqual(piper["models"]["selection_owner"], "station_configuration")
        self.assertEqual(piper["models"]["required_defaults"], [])

    def test_fdkaac_versions_and_source_identities_are_exact(self):
        fdkaac = get_runtime_component("fdkaac")
        self.assertEqual(fdkaac["runtime"]["fdkaac_version"], "1.0.7")
        self.assertEqual(fdkaac["runtime"]["libfdk_aac_version"], "2.0.3")
        self.assertEqual(
            fdkaac["source_archives"]["fdk-aac"],
            {
                "filename": "fdk-aac-2.0.3.tar.gz",
                "sha256": "e25671cd96b10bad896aa42ab91a695a9e573395262baed4e4a2ff178d6a3a78",
            },
        )
        self.assertEqual(
            fdkaac["source_archives"]["fdkaac"],
            {
                "filename": "fdkaac-1.0.7.tar.gz",
                "sha256": "145d4684c9325a2bd650e46a04b03327abe780a7b59cce47e6de8af2064fb2c7",
            },
        )

    def test_station_voice_choices_are_not_product_defaults(self):
        manifest_text = MANIFEST_PATH.read_text(encoding="utf-8")
        for station_voice in ("af_jessica", "am_liam", "am_fenrir", "hfc_female", "hfc_male"):
            self.assertNotIn(station_voice, manifest_text)

    def test_manifest_contains_no_secret_configuration_fields(self):
        def keys(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    yield key.lower()
                    yield from keys(child)
            elif isinstance(value, list):
                for child in value:
                    yield from keys(child)

        all_keys = set(keys(self.manifest))
        self.assertTrue(all(marker not in key for key in all_keys for marker in ("password", "token", "secret")))

    def test_no_large_tts_assets_are_present_in_repository(self):
        forbidden_names = {"kokoro-v1.0.onnx", "voices-v1.0.bin"}
        found = [path for path in PROJECT_ROOT.rglob("*") if path.is_file() and path.name in forbidden_names]
        found.extend(path for path in PROJECT_ROOT.rglob("*.onnx") if path.is_file())
        self.assertEqual(found, [])

    def test_loader_rejects_invalid_hashes(self):
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        manifest["components"]["kokoro"]["assets"]["model"]["sha256"] = "not-a-digest"
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "components.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeComponentContractError, "lowercase SHA-256"):
                load_runtime_components(path)

    def test_unknown_component_fails_clearly(self):
        with self.assertRaisesRegex(RuntimeComponentContractError, "unknown runtime component"):
            get_runtime_component("unknown")
