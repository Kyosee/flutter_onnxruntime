import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import zipfile


CMAKE = os.environ.get('ONNX_TEST_CMAKE') or shutil.which('cmake')
SOURCE = (Path(__file__).resolve().parents[1] / 'linux/CMakeLists.txt').read_text()
DOWNLOAD = SOURCE.split('# === ONNX Runtime Configuration ===', 1)[1].split(
    '# Any new source files', 1
)[0]
GITHUB_URL = (
    'https://github.com/microsoft/onnxruntime/releases/download/'
    'v${ONNXRUNTIME_VERSION}/onnxruntime-linux-${ONNXRUNTIME_ARCH}-${ONNXRUNTIME_VERSION}.tgz'
)
NUGET_URL = (
    'https://api.nuget.org/v3-flatcontainer/microsoft.ml.onnxruntime/'
    '${ONNXRUNTIME_VERSION}/microsoft.ml.onnxruntime.${ONNXRUNTIME_VERSION}.nupkg'
)


@unittest.skipUnless(CMAKE, 'CMake is required')
class LinuxRuntimeDownloadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='onnx download ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = self.root / 'runtime.tgz'
        self.nuget = self.root / 'runtime.nupkg'

    def archive(self, arch):
        with tarfile.open(self.github, 'w:gz') as package:
            for name, data in {
                'include/onnxruntime_cxx_api.h': b'header',
                'lib/libonnxruntime.so': b'github-runtime',
            }.items():
                entry = tarfile.TarInfo(f'onnxruntime-linux-{arch}-1.22.0/{name}')
                entry.size = len(data)
                package.addfile(entry, io.BytesIO(data))

    def nuget_package(self):
        with zipfile.ZipFile(self.nuget, 'w') as package:
            package.writestr('build/native/include/onnxruntime_cxx_api.h', b'header')
            for arch in ('x64', 'arm64'):
                package.writestr(
                    f'runtimes/linux-{arch}/native/libonnxruntime.so', arch.encode()
                )

    def configure(self, processor='x86_64'):
        code = DOWNLOAD.replace(GITHUB_URL, self.github.as_uri()).replace(
            NUGET_URL, self.nuget.as_uri()
        )
        script = self.root / 'download.cmake'
        script.write_text(
            'cmake_minimum_required(VERSION 3.10)\n'
            'set(USE_SYSTEM_ONNXRUNTIME OFF CACHE BOOL "")\n'
            'set(CMAKE_SYSTEM_NAME Linux)\n'
            f'set(CMAKE_SYSTEM_PROCESSOR {processor})\n'
            'set(CMAKE_SHARED_LIBRARY_SUFFIX .so)\n'
            + code
            + '\nfile(WRITE "${CMAKE_CURRENT_BINARY_DIR}/resolved.txt" '
            '"${ONNXRUNTIME_LIBRARY}\\n${ONNXRUNTIME_INCLUDE_DIRS}")\n'
        )
        return subprocess.run(
            [CMAKE, '-P', str(script)], cwd=self.root,
            capture_output=True, text=True, timeout=30,
        )

    def assert_runtime(self, result, expected):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        library, headers = (self.root / 'resolved.txt').read_text().splitlines()
        self.assertEqual(Path(library).read_bytes(), expected)
        self.assertEqual((Path(headers) / 'onnxruntime_cxx_api.h').read_bytes(), b'header')
        return Path(library)

    def test_github_archive_for_both_architectures(self):
        for processor, arch in [('x86_64', 'x64'), ('aarch64', 'aarch64')]:
            with self.subTest(arch=arch):
                self.archive(arch)
                self.assert_runtime(self.configure(processor), b'github-runtime')

    def test_nuget_fallback_for_both_architectures_and_cache(self):
        self.nuget_package()
        for processor, arch in [('x86_64', 'x64'), ('aarch64', 'arm64')]:
            with self.subTest(arch=arch):
                library = self.assert_runtime(self.configure(processor), arch.encode())
                self.assertEqual(library.name, 'libonnxruntime.so.1')
        self.nuget.unlink()
        self.assert_runtime(self.configure(), b'x64')

    def test_header_only_cache_is_repaired(self):
        header = self.root / 'onnxruntime/onnxruntime-linux-x64-1.22.0/include/onnxruntime_cxx_api.h'
        header.parent.mkdir(parents=True)
        header.write_bytes(b'header')
        self.nuget_package()
        self.assert_runtime(self.configure(), b'x64')

    def test_library_only_cache_is_replaced(self):
        library = self.root / 'onnxruntime/onnxruntime-linux-x64-1.22.0/lib/libonnxruntime.so'
        library.parent.mkdir(parents=True)
        library.write_bytes(b'incomplete-runtime')
        self.nuget_package()
        self.assert_runtime(self.configure(), b'x64')

    def test_nuget_does_not_reuse_stale_extraction(self):
        library = self.root / 'onnxruntime/nuget/runtimes/linux-x64/native/libonnxruntime.so'
        library.parent.mkdir(parents=True)
        library.write_bytes(b'stale-runtime')
        with zipfile.ZipFile(self.nuget, 'w') as package:
            package.writestr('build/native/include/onnxruntime_cxx_api.h', b'header')
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('libonnxruntime.so', result.stderr)

    def test_nuget_requires_cpp_header(self):
        with zipfile.ZipFile(self.nuget, 'w') as package:
            package.writestr('build/native/include/other.h', b'header')
            package.writestr('runtimes/linux-x64/native/libonnxruntime.so', b'runtime')
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('onnxruntime_cxx_api.h', result.stderr)

    def test_failed_download_reports_source_error(self):
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to download ONNX Runtime', result.stderr)
        self.assertIn('runtime.nupkg', result.stderr)

    def test_corrupt_github_archive_reports_extraction_error(self):
        self.github.write_bytes(b'not a tarball')
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to extract ONNX Runtime', result.stderr)

    def test_corrupt_nuget_archive_reports_extraction_error(self):
        self.nuget.write_bytes(b'not a zip file')
        result = self.configure()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Failed to extract ONNX Runtime', result.stderr)


if __name__ == '__main__':
    unittest.main()
