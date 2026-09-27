"""Packaging-only, no-service Python test entry; not a product runtime."""
from pathlib import Path
import os
import sys
import tempfile

# Only reviewed, synthetic, service-free files. Do not collect the whole suite.
TEST_FILES = (
    "tests/test_config.py", "tests/test_domain.py", "tests/test_domain_packs.py",
    "tests/test_discovery.py", "tests/test_reports.py",
)


def deny_external_io(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.bind", "subprocess.Popen", "os.system"}:
        raise RuntimeError("Offline review prohibits network and child-process operations")


def clean_environment(home):
    return {
        "HOME": str(home), "TMPDIR": str(home), "TMP": str(home), "TEMP": str(home),
        "PATH": str(Path(sys.executable).parent) + os.pathsep + "/usr/bin:/bin",
        "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="beep-offline-") as temporary:
        home = Path(temporary)
        os.environ.clear()
        os.environ.update(clean_environment(home))
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(root / "src"))
        os.chdir(root)
        sys.addaudithook(deny_external_io)
        import beep_agent
        assert Path(beep_agent.__file__).resolve().parent == root / "src/beep_agent", "Wrong source imported"
        import pytest
        print("Source: snapshot src/beep_agent; synthetic fixtures; network/process guard enabled")
        return pytest.main([
            "-p", "pytest_asyncio.plugin", "-p", "no:cacheprovider", "-q",
            "--basetemp", str(home / "pytest"), *TEST_FILES,
        ])


if __name__ == "__main__":
    raise SystemExit(main())
