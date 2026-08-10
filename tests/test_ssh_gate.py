from pathlib import Path
import runpy

import pytest


GATE = runpy.run_path(str(Path(__file__).parents[1] / "tools" / "vps-deployer-ssh-gate"))
validate = GATE["validate"]


@pytest.mark.parametrize("argv", [
    ["install", "-o", "root", "-g", "root", "-m", "0644", "/tmp/vps-deployer-site", "/etc/nginx/sites-available/example"],
    ["ln", "-s", "/etc/nginx/sites-available/example", "/etc/nginx/sites-enabled/example"],
    ["nginx", "-t"],
    ["systemctl", "reload", "nginx"],
])
def test_gate_allows_required_nginx_reconciliation(argv):
    validate(argv)


@pytest.mark.parametrize("argv", [
    ["install", "/tmp/vps-deployer-site", "/etc/nginx/nginx.conf"],
    ["nginx", "-s", "stop"],
    ["systemctl", "restart", "nginx"],
])
def test_gate_rejects_broader_nginx_access(argv):
    with pytest.raises(ValueError):
        validate(argv)
