"""Offline Runtime Foundation E3 bundle-contract tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from copy import deepcopy
from pathlib import Path

from django.test import SimpleTestCase

from isadoraair.runtime_bundle import (
    BUNDLE_FILENAME,
    RuntimeBundleError,
    current_platform_contract,
    load_runtime_bundle,
    product_contract_digest,
)
from isadoraair.runtime_components import load_runtime_components


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_package(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


class RuntimeBundleFixture(SimpleTestCase):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle_root = self.root / "bundle"
        self.bundle_root.mkdir()
        self.product = deepcopy(load_runtime_components())
        self.components = {}

    def file(self, relative: str, data: bytes) -> dict[str, str]:
        path = self.bundle_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return {"filename": relative, "sha256": digest(data)}

    def add_python_component(self, name: str):
        wheelhouse = f"{name}/wheelhouse"
        wheels = []
        lock_lines = []
        for package, version in sorted(
            self.product["components"][name]["runtime"]["packages"].items()
        ):
            normalized = canonical_package(package)
            filename = f"{normalized.replace('-', '_')}-{version}-py3-none-any.whl"
            data = f"wheel:{package}:{version}".encode()
            wheel_file = self.file(f"{wheelhouse}/{filename}", data)
            wheels.append(
                {
                    "filename": filename,
                    "package": package,
                    "version": version,
                    "sha256": wheel_file["sha256"],
                }
            )
            lock_lines.append(f"{package}=={version} --hash=sha256:{wheel_file['sha256']}")
        lock = self.file(
            f"{name}/requirements.lock", ("\n".join(lock_lines) + "\n").encode()
        )
        provenance = self.file(f"{name}/NOTICE.txt", f"{name} test notice\n".encode())
        component = {
            "lock": lock,
            "wheelhouse": wheelhouse,
            "wheels": wheels,
            "provenance": [provenance],
        }
        self.components[name] = component
        return component

    def add_kokoro(self):
        component = self.add_python_component("kokoro")
        assets = {}
        for name in ("model", "voices"):
            product_asset = self.product["components"]["kokoro"]["assets"][name]
            data = f"tiny-{name}".encode()
            product_asset["sha256"] = digest(data)
            assets[name] = self.file(f"kokoro/assets/{product_asset['filename']}", data)
        component["assets"] = assets
        return component

    def add_piper(self, *, model_id="model-one"):
        component = self.add_python_component("piper")
        model_data = b"tiny-piper-model"
        config_data = json.dumps(
            {"audio": {"sample_rate": 22050}, "language": {"code": "en_US"}}
        ).encode()
        component["models"] = [
            {
                "model_id": model_id,
                "model": self.file(f"piper/models/{model_id}.onnx", model_data),
                "config": self.file(f"piper/models/{model_id}.onnx.json", config_data),
                "language": "en-us",
                "sample_rate_hz": 22050,
            }
        ]
        return component

    def manifest_data(self):
        return {
            "schema_version": 1,
            "bundle_id": "test-bundle-1",
            "platform": current_platform_contract(),
            "product_contract_sha256": product_contract_digest(self.product),
            "components": self.components,
        }

    def write_manifest(self, data=None):
        payload = data if data is not None else self.manifest_data()
        (self.bundle_root / BUNDLE_FILENAME).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
        return payload

    def complete_kokoro_bundle(self):
        self.add_kokoro()
        self.write_manifest()


class RuntimeBundleValidationTests(RuntimeBundleFixture):
    def test_complete_bundle_is_confined_hashed_and_cross_checked(self):
        self.complete_kokoro_bundle()
        bundle = load_runtime_bundle(self.bundle_root, self.product)
        self.assertEqual(bundle.bundle_id, "test-bundle-1")
        self.assertEqual(set(bundle.components), {"kokoro"})
        self.assertEqual(
            bundle.product_contract_sha256, product_contract_digest(self.product)
        )

    def test_unsupported_schema_fails(self):
        self.add_kokoro()
        data = self.manifest_data()
        data["schema_version"] = 999
        self.write_manifest(data)
        with self.assertRaisesRegex(RuntimeBundleError, "schema_version"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_missing_wheel_fails(self):
        self.complete_kokoro_bundle()
        wheel = self.components["kokoro"]["wheels"][0]
        (self.bundle_root / self.components["kokoro"]["wheelhouse"] / wheel["filename"]).unlink()
        with self.assertRaisesRegex(RuntimeBundleError, "missing declared files"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_wrong_wheel_hash_fails(self):
        self.complete_kokoro_bundle()
        wheel = self.components["kokoro"]["wheels"][0]
        (self.bundle_root / self.components["kokoro"]["wheelhouse"] / wheel["filename"]).write_bytes(
            b"tampered"
        )
        with self.assertRaisesRegex(RuntimeBundleError, "checksum does not match"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_undeclared_extra_file_is_rejected(self):
        self.complete_kokoro_bundle()
        (self.bundle_root / "unexpected.bin").write_bytes(b"no")
        with self.assertRaisesRegex(RuntimeBundleError, "undeclared files"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_product_package_disagreement_is_rejected(self):
        self.add_kokoro()
        component = self.components["kokoro"]
        wheel = component["wheels"][0]
        wheel["version"] = "999"
        lines = (self.bundle_root / component["lock"]["filename"]).read_text().splitlines()
        package = wheel["package"]
        lines[0] = f"{package}==999 --hash=sha256:{wheel['sha256']}"
        lock_data = ("\n".join(lines) + "\n").encode()
        (self.bundle_root / component["lock"]["filename"]).write_bytes(lock_data)
        component["lock"]["sha256"] = digest(lock_data)
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeBundleError, "disagrees with product package"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_incomplete_dependency_lock_is_rejected(self):
        self.add_kokoro()
        component = self.components["kokoro"]
        lock_data = (
            (self.bundle_root / component["lock"]["filename"]).read_text().splitlines()[0]
            + "\n"
        ).encode()
        (self.bundle_root / component["lock"]["filename"]).write_bytes(lock_data)
        component["lock"]["sha256"] = digest(lock_data)
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeBundleError, "does not exactly match"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_bundle_cannot_install_a_second_isadoraair_source_copy(self):
        self.add_kokoro()
        component = self.components["kokoro"]
        original = component["wheels"][0]
        source_name = "isadoraair-1.0-py3-none-any.whl"
        source_data = b"uncontrolled-source-copy"
        source_hash = digest(source_data)
        self.file(f"{component['wheelhouse']}/{source_name}", source_data)
        component["wheels"].append(
            {
                "filename": source_name,
                "package": "isadoraair",
                "version": "1.0",
                "sha256": source_hash,
            }
        )
        lock_path = self.bundle_root / component["lock"]["filename"]
        lock_data = lock_path.read_bytes() + (
            f"isadoraair==1.0 --hash=sha256:{source_hash}\n".encode()
        )
        lock_path.write_bytes(lock_data)
        component["lock"]["sha256"] = digest(lock_data)
        self.write_manifest()
        self.assertTrue(original)
        with self.assertRaisesRegex(RuntimeBundleError, "second copy"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_unsupported_platform_or_python_abi_is_rejected(self):
        self.add_kokoro()
        data = self.manifest_data()
        data["platform"]["python_abi"] = "cp999"
        self.write_manifest(data)
        with self.assertRaisesRegex(RuntimeBundleError, "incompatible"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_incompatible_wheel_tag_is_rejected_before_install(self):
        self.add_kokoro()
        component = self.components["kokoro"]
        wheel = component["wheels"][0]
        old_path = self.bundle_root / component["wheelhouse"] / wheel["filename"]
        new_name = f"{wheel['package'].replace('-', '_')}-{wheel['version']}-cp313-cp313-manylinux_2_17_x86_64.whl"
        old_path.rename(old_path.with_name(new_name))
        wheel["filename"] = new_name
        self.write_manifest()
        with self.assertRaisesRegex(RuntimeBundleError, "wheel is incompatible"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_missing_kokoro_asset_fails(self):
        self.complete_kokoro_bundle()
        model = self.components["kokoro"]["assets"]["model"]
        (self.bundle_root / model["filename"]).unlink()
        with self.assertRaisesRegex(RuntimeBundleError, "missing declared files"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_wrong_kokoro_asset_hash_fails(self):
        self.complete_kokoro_bundle()
        voices = self.components["kokoro"]["assets"]["voices"]
        (self.bundle_root / voices["filename"]).write_bytes(b"wrong")
        with self.assertRaisesRegex(RuntimeBundleError, "checksum does not match"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_kokoro_asset_identity_cannot_override_product(self):
        self.add_kokoro()
        data = self.manifest_data()
        asset = data["components"]["kokoro"]["assets"]["model"]
        new_data = b"different-but-internally-valid"
        (self.bundle_root / asset["filename"]).write_bytes(new_data)
        asset["sha256"] = digest(new_data)
        self.write_manifest(data)
        with self.assertRaisesRegex(RuntimeBundleError, "disagrees with the product contract"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_path_traversal_is_rejected(self):
        self.add_kokoro()
        data = self.manifest_data()
        data["components"]["kokoro"]["provenance"][0]["filename"] = "../NOTICE"
        self.write_manifest(data)
        with self.assertRaisesRegex(RuntimeBundleError, "confined POSIX relative path"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_symlink_escape_is_rejected(self):
        self.complete_kokoro_bundle()
        outside = self.root / "outside"
        outside.write_bytes(b"outside")
        os.symlink(outside, self.bundle_root / "escape")
        with self.assertRaisesRegex(RuntimeBundleError, "forbidden symlink"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_hardlinked_declared_file_is_rejected_during_bundle_verification(self):
        self.complete_kokoro_bundle()
        wheel = self.components["kokoro"]["wheels"][0]
        declared = self.bundle_root / self.components["kokoro"]["wheelhouse"] / wheel["filename"]
        os.link(declared, self.root / "outside-hardlink")
        with self.assertRaisesRegex(RuntimeBundleError, "hardlinked file"):
            load_runtime_bundle(self.bundle_root, self.product)

    def test_valid_piper_payload_remains_station_neutral_until_planning(self):
        self.add_piper()
        self.write_manifest()
        bundle = load_runtime_bundle(self.bundle_root, self.product)
        self.assertIn("model-one", bundle.components["piper"].piper_models)
