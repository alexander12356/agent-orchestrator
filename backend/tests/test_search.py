import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from app.search import MAX_BYTES, MAX_LINES


SEARCH = Path(__file__).resolve().parents[1] / "app" / "search.py"
MORE = "Есть дополнительные результаты."


def search(project: Path, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, str(SEARCH), "rg", *args],
        cwd=project, capture_output=True, encoding="utf-8", check=True,
    )
    return result.stdout


@pytest.mark.skipif(shutil.which("rg") is None, reason="requires ripgrep")
def test_assets_file_search_limits_results_and_can_expand(tmp_path):
    assets = tmp_path / "Assets"
    scripts = assets / "Scripts"
    scripts.mkdir(parents=True)
    scenes = assets / "Scenes"
    scenes.mkdir()
    for index in range(1200):
        (scenes / f"Scene-{index:04}.unity").touch()
    (scripts / "Inventory.cs").touch()

    broad = search(tmp_path, "--files", "Assets")
    assert MORE in broad
    assert sum(line.startswith("Assets") for line in broad.splitlines()) == MAX_LINES
    assert search(tmp_path, "--files", "-g", "*Inventory*", "Assets/Scenes") == "Совпадений нет.\n"
    expanded = search(tmp_path, "--files", "-g", "*Inventory*", "Assets")
    assert expanded.rstrip().endswith("Inventory.cs")
    assert MORE not in expanded


@pytest.mark.skipif(shutil.which("rg") is None, reason="requires ripgrep")
def test_assets_content_search_limits_a_long_match(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    (assets / "Big.asset").write_text("Inventory: " + "я" * 10000, encoding="utf-8")

    output = search(tmp_path, "-n", "-F", "Inventory", "Assets")
    assert MORE in output
    assert output.startswith(("Assets/Big.asset:1:Inventory:", "Assets\\Big.asset:1:Inventory:"))
    assert len(output.partition("\n")[0].encode("utf-8")) <= MAX_BYTES
