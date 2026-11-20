import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import zipfile


CMAKE = os.environ.get('ONNX_TEST_CMAKE') or shutil.which('cmake')
SOURCE = (Path(__file__).resolve().parents[1] / 'windows/CMakeLists.txt').read_text()
DOWNLOAD = SOURCE.split('# === ONNX Runtime Configuration ===', 1)[1].split(
    '# Define Windows-specific preprocessor macros', 1
)[0]
GITHUB_URL = (
    'https://github.com/microsoft/onnxruntime/releases/download/'
    'v${ONNXRUNTIME_VERSION}/onnxruntime-win-${ONNXRUNTIME_ARCH}-${ONNXRUNTIME_VERSION}.zip'
)
NUGET_URL = (
    'https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntime/'
    '${ONNXRUNTIME_VERSION}/microsoft.ml.onnxruntime.${ONNXRUNTIME_VERSION}.nupkg'
)


@unittest.skipUnless(CMAKE, 'CMake is required')
class WindowsRuntimeDownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='onnx windows download ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = self.root / 'runtime.zip'
        self.nuget = self.root / 'runtime.nupkg'

    def archive(self):
        with zipfile.ZipFile(self.github, 'w') as package:
            for arch in ('x64', 'x86'):
                for name, data in {
                    'include/onnxruntime_cxx_api.h': b'header',
                    'lib/onnxruntime.lib': b'import-library',
                    'lib/onnxruntime.dll': arch.encode(),
                    'lib/onnxruntime_providers_shared.dll': b'provider',
                }.items():
                    package.writestr(f'onnxruntime-win-{arch}-1.22.0/{name}', data)

    def nuget_package(self, missing=None):
        with zipfile.ZipFile(self.nuget, 'w') as package:
            if missing != 'header':
                package.writestr('build/native/include/onnxruntime_cxx_api.h', b'header')
            else:
                package.writestr('build/native/include/other.h', b'header')
            for arch in ('x64', 'x86'):
                for name, data in {
                    'onnxruntime.lib': b'import-library',
                    'onnxruntime.dll': arch.encode(),
                    'onnxruntime_providers_shared.dll': b'provider',
                }.items():
                    if name != missing:
                        package.writestr(f'runtimes/win-{arch}/native/{name}', data)

    def configure(self, pointer_size=8):
        code = DOWNLOAD.replace(GITHUB_URL, self.github.as_uri()).replace(
            NUGET_URL, self.nuget.as_uri()
        )
        script = self.root / 'download.cmake'
        script.write_text(
            'cmake_minimum_required(VERSION 3.10)\n'
            'set(USE_SYSTEM_ONNXRUNTIME OFF CACHE BOOL "")\n'
            f'set(CMAKE_SIZEOF_VOID_P {pointer_size})\n'
            + code
            + '\nfile(WRITE "${CMAKE_CURRENT_BINARY_DIR}/resolved.txt" '
            '"${ONNXRUNTIME_LIBRARY}\\n${ONNXRUNTIME_INCLUDE_DIRS}\\n'
            '${flutter_onnxruntime_bundled_libraries}")\n'
        )
        return subprocess.run(
            [CMAKE, '-P', str(script)], cwd=self.root,
            capture_output=True, text=True, timeout=30,
        )

    def assert_runtime(self, result, arch):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        library, headers, bundled = (self.root / 'resolved.txt').read_text().splitlines()
        self.assertEqual(Path(library).read_bytes(), b'import-library')
        self.assertEqual((Path(headers) / 'onnxruntime_cxx_api.h').read_bytes(), b'header')
        dlls = {Path(path).name: Path(path).read_bytes() for path in bundled.split(';')}
        self.assertEqual(dlls, {'onnxruntime.dll': arch.encode(),
                                'onnxruntime_providers_shared.dll': b'provider'})

    def test_github_archive_for_both_architectures(self):
        self.archive()
        for pointer_size, arch in [(8, 'x64'), (4, 'x86')]:
            with self.subTest(arch=arch):
                self.assert_runtime(self.configure(pointer_size), arch)

    def test_nuget_fallback_for_both_architectures_and_cache(self):
        self.nuget_package()
        for pointer_size, arch in [(8, 'x64'), (4, 'x86')]:
            with self.subTest(arch=arch):
                self.assert_runtime(self.configure(pointer_size), arch)
        self.nuget.unlink()
        self.assert_runtime(self.configure(), 'x64')

    def test_partial_cache_is_repaired(self):
        self.nuget_package()
        self.assert_runtime(self.configure(), 'x64')
        for filename in ('onnxruntime.lib', 'onnxruntime.dll'):
            with self.subTest(filename=filename):
                (self.root / 'onnxruntime/onnxruntime-win-x64-1.22.0/lib' / filename).unlink()
                self.assert_runtime(self.configure(), 'x64')

    def test_missing_nuget_files_fail_without_reusing_stale_files(self):
        for missing in ('header', 'onnxruntime.lib', 'onnxruntime.dll'):
            with self.subTest(missing=missing):
                stale = self.root / 'onnxruntime/nuget/runtimes/win-x64/native'
                stale.mkdir(parents=True, exist_ok=True)
                (stale / 'onnxruntime.dll').write_bytes(b'stale-runtime')
                self.nuget_package(missing)
                result = self.configure()
                self.assertNotEqual(result.returncode, 0, result.stdout)
                expected = 'onnxruntime_cxx_api.h' if missing == 'header' else missing
                self.assertIn(expected, result.stderr)

    def test_failed_download_reports_source_error(self):
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to download ONNX Runtime', result.stderr)
        self.assertIn('runtime.nupkg', result.stderr)

    def test_corrupt_archives_report_extraction_error(self):
        for archive in (self.github, self.nuget):
            with self.subTest(archive=archive.name):
                archive.write_bytes(b'not a zip file')
                result = self.configure()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('Failed to extract ONNX Runtime', result.stderr)
                archive.unlink()


if __name__ == '__main__':
    unittest.main()
